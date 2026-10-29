"""System-health probe behind ``healthcheck.sh``.

FAIL prints to stderr; WARN and (with --verbose) OK to stdout; --json suppresses per-check
prints. Exit: 0 all pass, 1 any fail, 2 warn-only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from media_stack import paths, state, vpn_country
from media_stack.clients.arr import ArrClient
from media_stack.clients.bazarr import BazarrClient, apikey_from_container
from media_stack.clients.qbit import QBitClient

COMPOSE = "docker-compose.yml"

REQUIRED_SVCS = [
    "gluetun", "jellyfin", "sonarr", "radarr", "bazarr", "prowlarr",
    "jellyseerr", "qbittorrent", "unpackerr", "flaresolverr", "tdarr", "kavita",
]
OPTIONAL_SVCS = ["tdarr-node", "watchtower"]

# .env keys beyond what media_stack.paths resolves. Process env wins over .env with "is set"
# (not "is non-empty") semantics.
_ENV_WHITELIST = [
    "VPN_COUNTRY", "SONARR_API_KEY", "RADARR_API_KEY", "QBIT_USER", "QBIT_PASS",
    "MEDIA_LAN_IP", "ALLOW_PUBLIC_IFACE", "BAZARR_DEFAULT_PROFILE_ID",
]


def _run(args: list[str], *, timeout: float | None = None,
         input_text: str | None = None) -> tuple[int, str, str]:
    """(rc, stdout, stderr); a launch failure or timeout maps to rc=1 with empty output."""
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                            timeout=timeout, input=input_text)
        return p.returncode, p.stdout, p.stderr
    except (subprocess.TimeoutExpired, OSError):
        return 1, "", ""


def _which(name: str) -> bool:
    rc, _, _ = _run(["sh", "-c", f"command -v {name}"])
    return rc == 0


# `user.mergerfs.branches` reads like "/mnt/disk1=RW:/mnt/disk2=RO,50G"; the `=MODE[,minfree]` tail
# is not part of the path.
_BRANCH_MODE = re.compile(r"=[A-Z]+(,[^:=]*)?$")
_MOUNTS = "/proc/self/mounts"
_MOUNT_HINT = ("a dropped disk leaves an empty mountpoint, so the library looks deleted: "
               "check findmnt and dmesg before deleting or re-importing anything")


def _mount_problem(path: Path) -> str:
    """Why `path` is not a live mount (not a mountpoint, empty, unreadable), or "" when it is.
    Read-only: a few lstats and the first real directory entry (an ext4 root always has
    `lost+found`, which proves nothing), never a walk or a SMART query.
    """
    if not os.path.ismount(os.path.realpath(path)):
        return "not a mountpoint"
    try:
        with os.scandir(path) as it:
            if next((e for e in it if e.name != "lost+found"), None) is None:
                return "mounted but empty"
    except OSError as e:
        return f"unreadable ({e.strerror or e.__class__.__name__})"
    return ""


def _fs_type(path: Path) -> str:
    """Type of the filesystem mounted exactly at `path` per the kernel mount table (topmost
    wins), or "" when it is not listed or the table cannot be read."""
    target = os.path.realpath(path)
    fstype = ""
    try:
        with open(_MOUNTS) as f:
            for line in f:
                fields = line.split(" ")
                if len(fields) >= 3 and re.sub(
                        r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[1]) == target:
                    fstype = fields[2]
    except OSError:
        return ""
    return fstype


def _bash_q(s: str) -> str:
    rc, out, _ = _run(["bash", "-c", 'printf "%q" "$1"', "_", s])
    return out


def _resolve_env(repo_root: Path) -> dict[str, str]:
    file_vals = paths.load_env_file(repo_root / ".env")
    out: dict[str, str] = {}
    for k in _ENV_WHITELIST:
        if k in os.environ:        # even if explicitly empty
            out[k] = os.environ[k]
        elif k in file_vals:
            out[k] = file_vals[k]
    return out


# Private IPv4: RFC1918, loopback, link-local, Tailscale CGNAT 100.64/10.
_PRIVATE_V4 = re.compile(
    r"^(10\.|192\.168\.|127\.|169\.254\.|"
    r"172\.(1[6-9]|2[0-9]|3[01])\.|"
    r"100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.)"
)


class HealthCheck:
    def __init__(self, *, verbose: bool = False, json_mode: bool = False,
                 repo_root: Path | None = None) -> None:
        self.verbose = verbose
        self.json_mode = json_mode
        self.repo_root = repo_root or paths.MEDIA_STACK_ROOT
        self.results: list[tuple[str, str, str]] = []
        self.env = _resolve_env(self.repo_root)
        self.vpn_country: str = ""
        self.bazarr_key: str = ""

    def _record(self, state: str, check: str, detail: str = "") -> None:
        self.results.append((state, check, detail))
        if self.json_mode or (state == "OK" and not self.verbose):
            return
        if state == "OK":
            print(f"OK:   {check}")
        else:
            print(f"{state}: {check} — {detail}",
                  file=sys.stderr if state == "FAIL" else sys.stdout)

    def ok(self, check: str) -> None:
        self._record("OK", check)

    def warn(self, check: str, detail: str) -> None:
        self._record("WARN", check, detail)

    def fail(self, check: str, detail: str) -> None:
        self._record("FAIL", check, detail)

    def _verdict(self, passed: bool, ok_check: str, bad_check: str, detail: str,
                 bad_state: str = "FAIL") -> None:
        """OK under `ok_check` when `passed`, else `bad_state` under `bad_check` with `detail`."""
        if passed:
            self.ok(ok_check)
        else:
            self._record(bad_state, bad_check, detail)

    @property
    def _compose(self) -> str:
        return str(self.repo_root / COMPOSE)

    def probe_mounts(self) -> None:
        """The media pool and each mergerfs branch behind it must be a live, non-empty mount.
        A USB disk that drops out leaves an empty mountpoint and the library looks deleted.
        Branches come from the pool's control file; a pool that is not mergerfs, or whose
        branch list cannot be read, is not a failure.
        """
        pool = paths.MEDIA_ROOT
        problem = _mount_problem(pool)
        self._verdict(not problem, f"mount:pool {pool} mounted, non-empty", "mount:pool",
                      f"{pool} is {problem}: {_MOUNT_HINT}")
        if problem == "not a mountpoint":
            return
        try:
            raw = os.getxattr(pool / ".mergerfs", "user.mergerfs.branches")
        except OSError as e:
            # Not mergerfs is fine; a mergerfs pool (or an unknown one) we cannot ask is a gap.
            fstype = _fs_type(pool)
            self._verdict(bool(fstype) and "mergerfs" not in fstype,
                          f"mount:branches not checked (pool is {fstype}, not mergerfs)",
                          "mount:branches",
                          f"not checked, could not read the branch list ({e.strerror})", "WARN")
            return
        listed = raw.decode(errors="replace").strip("\0").split(":")
        branches = [_BRANCH_MODE.sub("", b) for b in listed if b]
        if not branches:
            self.warn("mount:branches", "not checked, the pool reported no branches")
        for branch in branches:
            problem = _mount_problem(Path(branch))
            self._verdict(not problem, f"mount:branch {branch} mounted, non-empty",
                          f"mount:branch {branch}", f"{problem}: {_MOUNT_HINT}")

    def probe_gluetun(self) -> None:
        rc, out, _ = _run(["docker", "compose", "-f", self._compose, "ps",
                           "--status", "running", "gluetun"])
        if rc != 0 or "gluetun" not in out:
            self.fail("gluetun", "gluetun container not running")
            return

        def exec_wget(url: str) -> str:
            _, o, _ = _run(["docker", "exec", "gluetun", "wget", "-qO-", url],
                           timeout=15)
            return o

        vpn_ip = re.sub(r"\s", "", exec_wget("https://ifconfig.co"))
        ipinfo_iso = exec_wget("https://ipinfo.io/country").replace("\r", "").replace("\n", "").strip()
        ip2_raw = exec_wget("https://api.ip2location.io")
        try:
            ip2_iso = (json.loads(ip2_raw).get("country_code", "") if ip2_raw else "").strip()
        except Exception:
            ip2_iso = ""

        try:
            state, value = vpn_country.consensus(ipinfo_iso, ip2_iso)
            # "single" (one geo source answered) counts as ok, so one rate-limited source
            # doesn't false-FAIL; probe_vpn_country still FAILs a wrong country.
            display = vpn_country.iso_to_name(value) if state in ("ok", "single") else value
        except Exception:
            state, display = "empty", ""
        self.vpn_country = display if state in ("ok", "single", "disagree") else ""

        rc, host_out, _ = _run(["curl", "-fsS", "https://ifconfig.co"], timeout=15)
        host_ip = re.sub(r"\s", "", host_out) if rc == 0 else ""

        if not vpn_ip:
            self.fail("gluetun-egress", "could not query VPN IP via exec gluetun")
        elif not host_ip:
            self.warn("gluetun-egress",
                      f"VPN ip={vpn_ip} — could not query host egress, leak check skipped")
        elif vpn_ip == host_ip:
            self.fail("gluetun-egress",
                      f"VPN IP {vpn_ip} matches host IP — tunnel is LEAKING direct traffic")
        else:
            self.ok(f"gluetun-egress ip={vpn_ip} country={self.vpn_country or '?'}")

    def check_container(self, svc: str, required: bool) -> None:
        rc, cid_out, _ = _run(["docker", "compose", "-f", self._compose, "ps", "-q", svc])
        cid = cid_out.strip()
        if not cid:
            if required:
                self.fail(f"container:{svc}", "not running (no container id)")
            else:
                self.warn(f"container:{svc}", "optional, not running")
            return
        _, state_out, _ = _run(["docker", "inspect", "-f", "{{.State.Status}}", cid])
        state = state_out.strip() or "?"
        _, health_out, _ = _run(
            ["docker", "inspect", "-f",
             "{{if .State.Health}}{{.State.Health.Status}}{{end}}", cid])
        health = health_out.strip()
        if state != "running":
            if required:
                self.fail(f"container:{svc}", f"state={state} (expected running)")
            else:
                self.warn(f"container:{svc}", f"optional state={state}")
        elif health and health != "healthy":
            self.fail(f"container:{svc}", f"running but health={health}")
        else:
            suffix = f" health={health}" if health else ""
            self.ok(f"container:{svc} running{suffix}")

    def probe_containers(self) -> None:
        for svc in REQUIRED_SVCS:
            self.check_container(svc, True)
        for svc in OPTIONAL_SVCS:
            self.check_container(svc, False)

    def probe_arr(self, name: str, url: str, key_var: str) -> None:
        key = self.env.get(key_var, "")
        if not key:
            self.warn(f"api:{name}", f"{key_var} missing from .env — reachability not checked")
            return
        status = ArrClient(url, key).reachable_status()
        self._verdict(status == "200", f"api:{name} reachable", f"api:{name}",
                      f"{url}/api/v3/system/status returned {status}")

    def probe_arr_hardlinks(self, name: str, url: str, key_var: str) -> None:
        key = self.env.get(key_var, "")
        if not key:
            return
        try:
            v = ArrClient(url, key).hardlinks_enabled()
        except Exception:
            v = None
        if v is True:
            self.ok(f"policy:{name} hardlinks=enabled")
        elif v is False:
            self.fail(f"policy:{name} hardlinks=DISABLED",
                      "copyUsingHardlinks must be true — disabling doubles disk usage on import")
        else:
            self.warn(f"policy:{name} hardlinks",
                      "couldn't read /config/mediamanagement — Arr unreachable?")

    def probe_arr_format_scores(self, name: str, url: str, key_var: str) -> None:
        key = self.env.get(key_var, "")
        if not key:
            return
        try:
            v = ArrClient(url, key).format_score_violations(
                {"AV1": -10000, "12-bit": -10000}, profile_name_substring="Shield")
        except Exception:
            v = None
        if v is None:
            self.warn(f"policy:{name} custom-format-scores",
                      "couldn't read /qualityprofile — Arr unreachable?")
        elif not v:
            self.ok(f"policy:{name} custom-format-scores "
                    "AV1+12-bit=-10000 in every applicable profile")
        else:
            self.fail(f"policy:{name} custom-format-scores",
                      f"violations: {json.dumps(v)}")

    def probe_arr_silent_gap(self, name: str, url: str, key_var: str) -> None:
        key = self.env.get(key_var, "")
        if not key:
            return
        try:
            v = ArrClient(url, key).unmonitored_no_file_count()
        except Exception:
            v = None
        if v == 0:
            self.ok(f"policy:{name} silent-gap=0 (no monitored=False+hasFile=False movies)")
        elif v is None:
            self.warn(f"policy:{name} silent-gap",
                      "couldn't read /movie — Arr unreachable?")
        else:
            self.fail(f"policy:{name} silent-gap={v}",
                      "movies are monitored=False AND hasFile=False — they're invisible to "
                      "search; bulk re-monitor via PUT /movie/editor or audit each")

    # Pipeline OUTCOME probes. Detection only, never delete or blocklist: a dropped USB mount
    # makes every download look like it lost its files.
    OUTCOME_STATE = "health-outcomes.json"
    STUCK_AFTER_S = 6 * 3600
    NO_PORT_AFTER_S = 30 * 60
    _RESET_FAILED = ("could not reset outcome history — ageing is unreliable "
                     "until var/state is writable")
    _CANNOT_AGE = "cannot age this: the outcome state file is unreadable/unwritable"

    def _age_conditions(self, prefix: str, active: set[str]) -> tuple[dict[str, float], bool]:
        """`({key: seconds persisted}, persisted)`. Stored keys under `prefix` not in `active`
        are dropped, so a condition that clears and returns starts a fresh clock. One flock
        (state.mutate_state) keeps overlapping runs from resurrecting each other's deletions.
        persisted=False means the store was unreadable/unwritable: the age is not meaningful.
        """
        now = time.time()
        ages: dict[str, float] = {}

        def _mutate(st: dict) -> dict:
            for stale in [k for k in st if k.startswith(prefix) and k not in active]:
                st.pop(stale, None)
            for k in active:
                rec = st.get(k)
                first = rec.get("first_seen") if isinstance(rec, dict) else None
                if isinstance(first, (int, float)) and not isinstance(first, bool):
                    ages[k] = max(0.0, now - float(first))
                else:
                    st[k] = {"first_seen": now}
                    ages[k] = 0.0
            return st

        try:
            state.mutate_state(paths.VAR_STATE / self.OUTCOME_STATE, _mutate)
        except Exception:
            return ({k: 0.0 for k in active}, False)
        return (ages, True)

    def probe_arr_stuck_queue(self, name: str, url: str, key_var: str) -> None:
        key = self.env.get(key_var, "")
        if not key:
            return
        try:
            records = ArrClient(url, key).get_queue()
        except Exception:
            self.warn(f"outcome:{name} queue", "couldn't read /queue — Arr unreachable?")
            return
        bad = [r for r in records
               if r.get("errorMessage")
               or str(r.get("trackedDownloadStatus", "")).lower() in ("warning", "error")]
        prefix = f"{name}:queue:"
        active = {f"{prefix}{r.get('downloadId') or r.get('id')}" for r in bad}
        ages, persisted = self._age_conditions(prefix, active)
        oldest = max(ages.values(), default=0.0)
        if not bad:
            # Not persisted: the clear did not stick; a recurrence would FAIL instantly on
            # stale history.
            self._verdict(persisted,
                          f"outcome:{name} queue clean ({len(records)} item(s), none erroring)",
                          f"outcome:{name} queue clean ({len(records)} item(s))",
                          self._RESET_FAILED, "WARN")
        elif not persisted:
            self.warn(f"outcome:{name} queue erroring={len(bad)}", self._CANNOT_AGE)
        elif oldest >= self.STUCK_AFTER_S:
            self.fail(f"outcome:{name} queue stuck={len(bad)}",
                      f"oldest has been erroring {oldest / 3600:.1f}h — a persistently failing "
                      f"queue item can stop Arr grabbing a replacement for that episode; "
                      f"inspect /queue and clear the dead ones")
        else:
            self.warn(f"outcome:{name} queue erroring={len(bad)}",
                      f"oldest {oldest / 60:.0f}m — transient if it clears on its own")

    def probe_qbit_missing_files(self, qbit_url: str) -> None:
        client = QBitClient(qbit_url, self.env.get("QBIT_USER", ""),
                            self.env.get("QBIT_PASS", ""))
        if not client.login():
            return  # probe_qbit already reports auth failures
        try:
            torrents = client.torrents_info()
        except Exception:
            self.warn("outcome:qbit torrents", "couldn't read /torrents/info")
            return
        bad = [t for t in torrents
               if str(t.get("state", "")).lower() in ("missingfiles", "error")]
        active = {f"qbit:{t.get('hash')}" for t in bad}
        ages, persisted = self._age_conditions("qbit:", active)
        oldest = max(ages.values(), default=0.0)
        if not bad:
            self._verdict(persisted,
                          f"outcome:qbit no missing-file torrents ({len(torrents)} total)",
                          "outcome:qbit no missing-file torrents", self._RESET_FAILED, "WARN")
        elif not persisted:
            self.warn(f"outcome:qbit missing-files={len(bad)}", self._CANNOT_AGE)
        elif oldest >= self.STUCK_AFTER_S:
            self.fail(f"outcome:qbit missing-files={len(bad)}",
                      f"oldest {oldest / 3600:.1f}h — if MANY appeared at once suspect a dropped "
                      f"mount (check findmnt) before deleting anything")
        else:
            self.warn(f"outcome:qbit missing-files={len(bad)}", f"oldest {oldest / 60:.0f}m")

    def probe_forwarded_port(self, qbit_url: str) -> None:
        """Forwarded port vs qBittorrent's listen port. Observation only; sync-forwarded-port.py repairs it."""
        rc, out, _err = _run(["docker", "exec", "gluetun", "wget", "-qO-", "--timeout=10",
                              "http://127.0.0.1:8000/v1/portforward"], timeout=60)
        if rc != 0:
            self.warn("outcome:port-forward", "couldn't query gluetun's control server")
            return
        try:
            port = int(json.loads(out).get("port") or 0)
        except Exception:
            self.warn("outcome:port-forward", f"unparseable response: {out.strip()[:60]}")
            return

        if port <= 0:
            # Only "none" is active: this also clears a stale mismatch timer.
            ages, persisted = self._age_conditions("portforward:", {"portforward:none"})
            age = ages.get("portforward:none", 0.0)
            if not persisted:
                self.warn("outcome:port-forward none", "no allocation (cannot age it)")
            elif age >= self.NO_PORT_AFTER_S:
                self.fail("outcome:port-forward none",
                          f"no allocation for {age / 60:.0f}m — gluetun does NOT retry a failed "
                          f"initial allocation, so recreate it; check the WireGuard key has "
                          f"NAT-PMP enabled")
            else:
                self.warn("outcome:port-forward none", f"no allocation yet ({age / 60:.0f}m)")
            return

        # Clear the no-allocation timer first so an early return below can't leave it running.
        _ages, cleared = self._age_conditions("portforward:none", set())
        if not cleared:
            self.warn("outcome:port-forward", "could not reset the no-allocation timer")

        client = QBitClient(qbit_url, self.env.get("QBIT_USER", ""),
                            self.env.get("QBIT_PASS", ""))
        if not client.login():
            return
        try:
            listen = int(client.preferences().get("listen_port", 0))
        except Exception:
            self.warn("outcome:port-forward", "couldn't read qBittorrent's listen_port")
            return

        if listen == port:
            _a, ok_clear = self._age_conditions("portforward:", set())
            self._verdict(ok_clear, f"outcome:port-forward {port} == qBittorrent listen_port",
                          f"outcome:port-forward {port} == listen_port",
                          "could not reset outcome history", "WARN")
            return
        ages, persisted = self._age_conditions("portforward:", {"portforward:mismatch"})
        age = ages.get("portforward:mismatch", 0.0)
        if not persisted:
            self.warn(f"outcome:port-forward mismatch {port} != {listen}", "cannot age it")
        elif age >= self.NO_PORT_AFTER_S:
            self.fail(f"outcome:port-forward mismatch {port} != {listen}",
                      f"persisted {age / 60:.0f}m — the */5 reconciler should have fixed this; "
                      f"check var/log/sync-forwarded-port.log")
        else:
            self.warn(f"outcome:port-forward mismatch {port} != {listen}",
                      "reconciler runs every 5 min; transient right after a reconnect")

    def probe_unsynced_subs(self) -> None:
        """Files embedded with an UNSYNCED subtitle (ffsubsync failed: `fixed`, synced=False).
        Never reports "all synced": most records carry no `synced` field, so absence of
        evidence is unknown, not success.
        """
        raw = state.load_state(paths.VAR_STATE / "consolidate-subs.state.json")
        if not isinstance(raw, dict) or not raw:
            self.warn("outcome:subs", "consolidate state missing, empty or malformed")
            return
        current = historical = unknown = synced = 0
        for path_str, v in raw.items():
            if not isinstance(v, dict):
                continue
            flag = v.get("synced")
            if flag is False:
                if Path(path_str).exists():
                    current += 1
                else:
                    historical += 1
            elif flag is True:
                synced += 1
            else:
                unknown += 1
        if current:
            self.warn(f"outcome:subs unsynced={current}",
                      f"ffsubsync failed and the UNSYNCED subtitle was embedded anyway; these "
                      f"are the files most likely to be visibly out of sync "
                      f"({synced} synced, {unknown} no recorded result, "
                      f"{historical} failed-sync records whose file is gone)")
        else:
            self.ok(f"outcome:subs no failed syncs among files still present "
                    f"({synced} synced, {unknown} no recorded result, {historical} historical)")

    def probe_bazarr_api(self, bazarr_url: str) -> None:
        try:
            self.bazarr_key = apikey_from_container() or ""
        except Exception:
            self.bazarr_key = ""
        if not self.bazarr_key:
            self.warn("api:bazarr", "could not read apikey from container config")
            return
        status = BazarrClient(bazarr_url, self.bazarr_key).reachable_status()
        self._verdict(status == "200", "api:bazarr reachable", "api:bazarr",
                      f"/api/system/status returned {status}")

    def probe_qbit(self, qbit_url: str) -> None:
        qbit_user = self.env.get("QBIT_USER", "")
        if not qbit_user:
            status = QBitClient(qbit_url).reachable_status()
            self._verdict(status == "200", "api:qbit reachable (bypass-auth-on-LAN)",
                          "api:qbit", f"/api/v2/torrents/info returned {status} (no auth)")
            return
        client = QBitClient(qbit_url, qbit_user, self.env.get("QBIT_PASS", ""))
        resp_text = client.login_response()
        if resp_text != "Ok.":
            self.fail("api:qbit", f"login returned {resp_text or '<empty>'}")
            return
        auth_status = client.reachable_status()
        self._verdict(auth_status == "200", "api:qbit auth round-trip + cookie-verified API call",
                      "api:qbit", f"login Ok but /torrents/info via cookie returned {auth_status}")

    def probe_bazarr_profiles(self, bazarr_url: str) -> None:
        if not self.bazarr_key:
            return
        client = BazarrClient(bazarr_url, self.bazarr_key)
        try:
            m, s = client.unprofiled_count()
        except Exception:
            m, s = None, None
        um = "?" if m is None else str(m)
        us = "?" if s is None else str(s)
        if um == "0" and us == "0":
            self.ok("bazarr-profile-coverage: 0 unprofiled (movies + series)")
        elif um == "?" or us == "?":
            self.warn("bazarr-profile-coverage",
                      f"movies='{um}' series='{us}' — endpoint parse error")
        else:
            self.warn("bazarr-profile-coverage",
                      f"{um} movies + {us} series have profileId=None "
                      "(bazarr-profile-audit cron should fix on next 04:30)")

        expected_raw = self.env.get("BAZARR_DEFAULT_PROFILE_ID", "")
        bazarr_expected = expected_raw if expected_raw != "" else "1"
        if not re.fullmatch(r"[0-9]+", bazarr_expected):
            self.fail("bazarr-profile-equality",
                      f"BAZARR_DEFAULT_PROFILE_ID must be a non-negative integer, "
                      f"got '{bazarr_expected}' — fix in .env")
            return
        try:
            wm, ws = client.wrong_profile_count(int(bazarr_expected))
        except Exception:
            wm, ws = None, None
        if wm is None or ws is None:
            self.warn("bazarr-profile-equality",
                      f"endpoint error — couldn't verify items against profileId={bazarr_expected}")
        elif wm == 0 and ws == 0:
            self.ok(f"bazarr-profile-equality: every assigned item matches "
                    f"expected profileId={bazarr_expected}")
        else:
            self.warn("bazarr-profile-equality",
                      f"{wm} movies + {ws} series assigned to a profileId != {bazarr_expected} "
                      "(manually re-assign or update BAZARR_DEFAULT_PROFILE_ID in .env)")

    def probe_ufw(self) -> None:
        if not _which("ufw"):
            return
        rc, out, _ = _run(["sudo", "-n", "ufw", "status"])
        first = out.splitlines()[0] if out.splitlines() else ""
        parts = first.split()
        ufw_state = parts[1] if len(parts) >= 2 else ""
        if ufw_state == "inactive":
            self.ok("ufw=inactive (Tailscale perimeter intact)")
        elif not ufw_state:
            self.warn("ufw", "cannot check without sudo — assume operator audited")
        else:
            self.fail(f"ufw={ufw_state}",
                      "must be inactive — Tailscale handles encryption; UFW locks out SSH")

    def probe_perimeter(self) -> None:
        if self.env.get("ALLOW_PUBLIC_IFACE", "0") == "1":
            return
        if not _which("ip"):
            self.fail("perimeter",
                      "iproute2 'ip' command not available — cannot verify admin-port "
                      "perimeter; install iproute2 or set ALLOW_PUBLIC_IFACE=1 if you've "
                      "fronted services with auth")
            return
        v4_status, v4_raw, _ = _run(["ip", "-o", "-4", "addr", "show"])
        v6_status, v6_raw, _ = _run(["ip", "-o", "-6", "addr", "show", "scope", "global"])
        if v4_status != 0 or v6_status != 0:
            self.fail("perimeter",
                      f"ip addr show failed (v4 rc={v4_status}, v6 rc={v6_status}) — cannot "
                      "verify admin-port perimeter; investigate iproute2 or set ALLOW_PUBLIC_IFACE=1")
            return

        def addrs(raw: str) -> list[str]:
            out = []
            for line in raw.splitlines():
                f = line.split()
                if len(f) >= 4:
                    out.append(f[3].split("/")[0])
            return out

        public_v4 = [ip for ip in addrs(v4_raw) if not _PRIVATE_V4.match(ip)]
        public_v6 = [ip for ip in addrs(v6_raw) if not ip.lower().startswith(("fc", "fd"))]
        if not public_v4 and not public_v6:
            self.ok("perimeter: no public-routable IPv4 or IPv6 interface "
                    "(admin-port 0.0.0.0 bind is safe)")
        else:
            detail = ""
            if public_v4:
                detail += "IPv4=" + " ".join(public_v4) + " "
            if public_v6:
                detail += " IPv6=" + " ".join(public_v6) + " "
            self.fail("perimeter",
                      f"host has public address(es) ({detail}) — admin ports bind to all "
                      "interfaces and would be exposed; either narrow compose ports to a "
                      "specific LAN/Tailnet IP, front with an auth proxy, or set "
                      "ALLOW_PUBLIC_IFACE=1 if intentional")

    def probe_realtek(self) -> None:
        _, out, _ = _run(["ip", "-o", "link", "show"])
        if "enp6s0f1" not in out:
            return
        self.probe_service("realtek-fix.service",
                           "realtek-fix.service active (this host has enp6s0f1)",
                           "inactive — enp6s0f1 detected; NIC will drop under load")

    def probe_service(self, unit: str, ok_msg: str, fail_detail: str) -> None:
        rc, _, _ = _run(["systemctl", "is-active", "--quiet", unit])
        self._verdict(rc == 0, ok_msg, unit, fail_detail)

    def probe_vpn_country(self) -> None:
        vc_raw = self.env.get("VPN_COUNTRY", "")
        expected_country = vc_raw if vc_raw != "" else "Switzerland"
        if not self.vpn_country:
            self.fail("vpn-country", "could not query VPN country — policy unverified")
        elif self.vpn_country.startswith("DISAGREE(") or self.vpn_country.startswith("UNVERIFIED("):
            self.fail("vpn-country",
                      f"{self.vpn_country} (cannot verify against expected '{expected_country}')")
        else:
            agreed_iso = vpn_country.name_to_iso(self.vpn_country)
            if vpn_country.country_matches(agreed_iso, expected_country):
                self.ok(f"vpn-country={self.vpn_country}")
            else:
                self.fail(f"vpn-country={self.vpn_country}",
                          f"expected {expected_country} — VPN routing policy broken")

    def probe_ops_audit(self) -> None:
        audit = self.repo_root / "ops" / "audit.sh"
        if not os.access(audit, os.X_OK):
            return
        rc, _, _ = _run(["bash", str(audit)])
        if rc == 0:
            self.ok("ops/audit.sh (cron + systemd match repo)")
        elif rc == 2:
            self.warn("ops/audit.sh",
                      "mode-640 unit(s) unverifiable — passwordless sudo unavailable for the body read")
        else:
            self.fail("ops drift",
                      "live cron or systemd differs from ops/ snapshot — run ops/audit.sh")

    def run_all(self) -> int:
        self.probe_mounts()
        self.probe_gluetun()
        self.probe_containers()
        self.probe_arr("sonarr", paths.SONARR_URL, "SONARR_API_KEY")
        self.probe_arr("radarr", paths.RADARR_URL, "RADARR_API_KEY")
        self.probe_arr_hardlinks("sonarr", paths.SONARR_URL, "SONARR_API_KEY")
        self.probe_arr_hardlinks("radarr", paths.RADARR_URL, "RADARR_API_KEY")
        self.probe_arr_format_scores("sonarr", paths.SONARR_URL, "SONARR_API_KEY")
        self.probe_arr_format_scores("radarr", paths.RADARR_URL, "RADARR_API_KEY")
        self.probe_arr_silent_gap("radarr", paths.RADARR_URL, "RADARR_API_KEY")
        self.probe_arr_stuck_queue("sonarr", paths.SONARR_URL, "SONARR_API_KEY")
        self.probe_arr_stuck_queue("radarr", paths.RADARR_URL, "RADARR_API_KEY")
        self.probe_bazarr_api(paths.BAZARR_URL)
        self.probe_qbit(paths.QBIT_URL)
        self.probe_qbit_missing_files(paths.QBIT_URL)
        self.probe_forwarded_port(paths.QBIT_URL)
        self.probe_unsynced_subs()
        self.probe_bazarr_profiles(paths.BAZARR_URL)
        self.probe_ufw()
        self.probe_perimeter()
        self.probe_realtek()
        self.probe_service("consolidate-watch.service",
                           "consolidate-watch.service active",
                           "inactive — new imports won't auto-trigger v2 pipeline")
        self.probe_service("malware-guard.service",
                           "malware-guard.service active",
                           "inactive — Windows .exe / .bat in /downloads not reaped")
        self.probe_vpn_country()
        self.probe_ops_audit()
        return self.summary()

    def summary(self) -> int:
        fails = sum(1 for r in self.results if r[0] == "FAIL")
        warns = sum(1 for r in self.results if r[0] == "WARN")
        passes = sum(1 for r in self.results if r[0] == "OK")
        if self.json_mode:
            items = [{"state": s, "check": c, "detail": d} for s, c, d in self.results]
            print(json.dumps({"pass": passes, "warn": warns, "fail": fails,
                              "results": items}))
        if fails > 0:
            if not self.json_mode:
                print(f"\nHEALTH FAIL: {fails} fail / {warns} warn / {passes} ok")
            return 1
        if warns > 0:
            if not self.json_mode:
                print(f"\nHEALTH WARN: {warns} warn / {passes} ok")
            return 2
        if not self.json_mode:
            print(f"\nHEALTH OK: {passes} / {passes} checks passed")
        return 0


def main(argv: list[str]) -> int:
    verbose = False
    json_mode = False
    for arg in argv:
        if arg == "--verbose":
            verbose = True
        elif arg == "--json":
            json_mode = True
        else:
            print(f"healthcheck: unknown arg {_bash_q(arg)}", file=sys.stderr)
            return 2
    hc = HealthCheck(verbose=verbose, json_mode=json_mode)
    return hc.run_all()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
