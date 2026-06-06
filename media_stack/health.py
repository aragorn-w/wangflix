"""media_stack/health.py — aggregate machine-readable system-health probe.

Typed Python port of ``healthcheck.sh`` (AUDIT A12).  ``healthcheck.sh``
is now a thin wrapper that ``exec``s ``python3 -m media_stack.health``.

This is a *behaviour-preserving* port of the critical live health monitor:
every probe runs in the same order and emits the byte-identical OK/WARN/FAIL
strings, the same stdout-vs-stderr routing, the same ``--json`` shape, and
the same exit codes as the shell original.  The shell's probes already
delegated their heavy logic to ``media_stack`` (``vpn_country``, ``ArrClient``,
``BazarrClient``); this module calls those helpers in-process instead of via
``python3 -c`` subprocesses, and shells out only for the genuinely external
tools (``docker``, ``curl``, ``ip``, ``systemctl``, ``find``, ``pgrep``,
``ops/audit.sh``).

Output contract (identical to the shell):
  * ``pass``  → records OK; prints ``OK:   <check>`` to stdout ONLY with --verbose
  * ``fail``  → records FAIL; prints ``FAIL: <check> — <detail>`` to STDERR
  * ``warn``  → records WARN; prints ``WARN: <check> — <detail>`` to stdout
  * ``--json`` suppresses all per-check prints; emits one JSON object instead
  * exit code: 0 = all pass, 1 = any fail, 2 = warn-only

Usage:
  python3 -m media_stack.health            # quiet — only failed checks print
  python3 -m media_stack.health --verbose  # also print passed checks
  python3 -m media_stack.health --json     # machine-readable JSON summary
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from media_stack import paths, vpn_country
from media_stack.clients.arr import ArrClient
from media_stack.clients.bazarr import BazarrClient, apikey_from_container
from media_stack.clients.qbit import QBitClient

COMPOSE = "docker-compose.yml"

# Required = stack doesn't function without it.  Optional = tolerated absent.
REQUIRED_SVCS = [
    "gluetun", "jellyfin", "sonarr", "radarr", "bazarr", "prowlarr",
    "jellyseerr", "qbittorrent", "unpackerr", "flaresolverr", "tdarr", "kavita",
]
OPTIONAL_SVCS = ["tdarr-node", "watchtower"]

# Whitelist of expected top-level repo entries for the repo-root-hygiene probe.
REPO_ROOT_EXPECTED = {
    "AGENTS.md", "AUDIT.md", "CLAUDE.md", "README.md",
    "docker-compose.yml", ".env", ".env.example", ".git", ".gitignore",
    ".gemini", ".claude",
    "bazarr-profile-audit.py",
    "consolidate-status.sh", "consolidate-subs.py", "consolidate-watch.sh",
    "healthcheck.sh", "jellyfin-mint-api-key.py",
    "loudness-report.py", "malware-guard.sh",
    "media_lang.py", "media_paths.py", "media_stack",
    "movie-dedupe.py",
    "normalize-audio.py", "normalize-driver.sh", "normalize-loop.sh",
    "normalize-status.sh",
    "nuke_stalled.py", "preflight.sh", "pyproject.toml", "requirements-dev.txt",
    "config", "data", "lib", "ops", "requirements.txt", "tests", "reviews", "var",
}
# Dotfiles/dirs the find loop skips entirely (never flagged).  The tool
# caches (.mypy_cache/.ruff_cache) self-ignore inside git (each drops a
# `.gitignore` of `*`), so they never reach a commit; skip them here too,
# same as .pytest_cache, so the monitor doesn't WARN on transient caches.
REPO_ROOT_SKIP = {".", "..", ".DS_Store", ".pytest_cache", "__pycache__",
                  ".mypy_cache", ".ruff_cache"}

# .env keys this monitor needs beyond what media_stack.paths already resolves.
# lib/paths.sh deliberately does NOT carry these credential keys (paths-vs-
# creds separation), so we load them here with the documented
# process-env > .env precedence and "is set" (not "is non-empty") semantics.
_ENV_WHITELIST = [
    "VPN_COUNTRY", "SONARR_API_KEY", "RADARR_API_KEY", "QBIT_USER", "QBIT_PASS",
    "MEDIA_LAN_IP", "ALLOW_PUBLIC_IFACE", "BAZARR_DEFAULT_PROFILE_ID",
]


def _run(args: list[str], *, timeout: float | None = None,
         input_text: str | None = None) -> tuple[int, str, str]:
    """Run a subprocess, returning (rc, stdout, stderr).  Failure to even
    launch (or a timeout) maps to rc=1 with empty output — matching the
    shell's ``|| true`` / ``|| echo ...`` fallbacks."""
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                            timeout=timeout, input=input_text)
        return p.returncode, p.stdout, p.stderr
    except (subprocess.TimeoutExpired, OSError):
        return 1, "", ""


def _which(name: str) -> bool:
    rc, _, _ = _run(["sh", "-c", f"command -v {name}"])
    return rc == 0


def _bash_q_join(items: list[str]) -> str:
    """Format items exactly like bash ``printf '%q ' "${items[@]}"`` (each
    shell-quoted, single trailing space).  Delegated to bash so the
    repo-root-hygiene WARN string is byte-identical to the shell original,
    including escaping of any shell-metacharacter entry names."""
    if not items:
        return ""
    rc, out, _ = _run(["bash", "-c", 'printf "%q " "$@"', "_", *items])
    return out


def _bash_q(s: str) -> str:
    rc, out, _ = _run(["bash", "-c", 'printf "%q" "$1"', "_", s])
    return out


def _resolve_env(repo_root: Path) -> dict[str, str]:
    """Whitelisted .env load with process-env precedence (is-set semantics)."""
    file_vals = paths.load_env_file(repo_root / ".env")
    out: dict[str, str] = {}
    for k in _ENV_WHITELIST:
        if k in os.environ:        # caller owns it (even if explicitly empty)
            out[k] = os.environ[k]
        elif k in file_vals:
            out[k] = file_vals[k]
    return out


# IPv4 ranges treated as private/non-public (RFC1918 + loopback + link-local
# + Tailscale CGNAT 100.64/10).  Anything NOT matching is "public".
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
        # Set by probe_gluetun, consumed by probe_vpn_country.
        self.vpn_country: str = ""
        # Set by probe_bazarr_api, consumed by probe_bazarr_profiles.
        self.bazarr_key: str = ""

    # ---- result recorders (print routing matches the shell exactly) ----
    def ok(self, check: str) -> None:
        self.results.append(("OK", check, ""))
        if self.verbose and not self.json_mode:
            print(f"OK:   {check}")

    def fail(self, check: str, detail: str) -> None:
        self.results.append(("FAIL", check, detail))
        if not self.json_mode:
            print(f"FAIL: {check} — {detail}", file=sys.stderr)

    def warn(self, check: str, detail: str) -> None:
        self.results.append(("WARN", check, detail))
        if not self.json_mode:
            print(f"WARN: {check} — {detail}")

    # ---- service URLs / keys ----
    @property
    def _compose(self) -> str:
        return str(self.repo_root / COMPOSE)

    # ---------- gluetun VPN egress ----------
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
            # "single" (only one geo source answered) is treated like "ok":
            # show the country name and let probe_vpn_country validate it
            # against policy — a single source naming the WRONG country still
            # FAILs there, but one source being rate-limited no longer
            # false-FAILs when the other confirms the policy country.
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

    # ---------- core media containers ----------
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

    # ---------- Arr API reachability ----------
    def probe_arr(self, name: str, url: str, key_var: str) -> None:
        key = self.env.get(key_var, "")
        if not key:
            self.warn(f"api:{name}", f"{key_var} missing from .env — reachability not checked")
            return
        status = ArrClient(url, key).reachable_status()
        if status == "200":
            self.ok(f"api:{name} reachable")
        else:
            self.fail(f"api:{name}", f"{url}/api/v3/system/status returned {status}")

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

    # ---------- Bazarr API reachability ----------
    def probe_bazarr_api(self, bazarr_url: str) -> None:
        try:
            self.bazarr_key = apikey_from_container() or ""
        except Exception:
            self.bazarr_key = ""
        if not self.bazarr_key:
            self.warn("api:bazarr", "could not read apikey from container config")
            return
        status = BazarrClient(bazarr_url, self.bazarr_key).reachable_status()
        if status == "200":
            self.ok("api:bazarr reachable")
        else:
            self.fail("api:bazarr", f"/api/system/status returned {status}")

    # ---------- qBittorrent ----------
    def probe_qbit(self, qbit_url: str) -> None:
        qbit_user = self.env.get("QBIT_USER", "")
        if not qbit_user:
            status = QBitClient(qbit_url).reachable_status()
            if status == "200":
                self.ok("api:qbit reachable (bypass-auth-on-LAN)")
            else:
                self.fail("api:qbit", f"/api/v2/torrents/info returned {status} (no auth)")
            return
        # Authenticated login round-trip + cookie-verified protected call.
        client = QBitClient(qbit_url, qbit_user, self.env.get("QBIT_PASS", ""))
        resp_text = client.login_response()
        if resp_text != "Ok.":
            self.fail("api:qbit", f"login returned {resp_text or '<empty>'}")
            return
        auth_status = client.reachable_status()
        if auth_status == "200":
            self.ok("api:qbit auth round-trip + cookie-verified API call")
        else:
            self.fail("api:qbit", f"login Ok but /torrents/info via cookie returned {auth_status}")

    # ---------- Bazarr profile coverage + equality ----------
    def probe_bazarr_profiles(self, bazarr_url: str) -> None:
        if not self.bazarr_key:
            return
        client = BazarrClient(bazarr_url, self.bazarr_key)
        # The client methods already return None tuples on endpoint failure,
        # but wrap defensively so a malformed response can never crash the
        # whole aggregate monitor (same "never crash on a weird API shape"
        # rule as the other probes) — map any escape to the endpoint-error WARN.
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

    # ---------- UFW must be off ----------
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

    # ---------- admin-port perimeter ----------
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

    # ---------- realtek-fix.service (host-specific) ----------
    def probe_realtek(self) -> None:
        _, out, _ = _run(["ip", "-o", "link", "show"])
        if "enp6s0f1" not in out:
            return
        rc, _, _ = _run(["systemctl", "is-active", "--quiet", "realtek-fix.service"])
        if rc == 0:
            self.ok("realtek-fix.service active (this host has enp6s0f1)")
        else:
            self.fail("realtek-fix.service",
                      "inactive — enp6s0f1 detected; NIC will drop under load")

    # ---------- repo-root hygiene ----------
    def probe_repo_root(self) -> None:
        rc, out, _ = _run(["find", str(self.repo_root), "-maxdepth", "1",
                           "-mindepth", "1", "-print0"])
        entries = [e for e in out.split("\0") if e]
        unexpected: list[str] = []
        for entry in entries:
            base = entry.rsplit("/", 1)[-1]
            if base in REPO_ROOT_SKIP:
                continue
            if base not in REPO_ROOT_EXPECTED:
                unexpected.append(base)
        if unexpected:
            self.warn("repo-root-hygiene",
                      f"unexpected top-level entries: {_bash_q_join(unexpected)}")
        else:
            self.ok("repo-root-hygiene: only whitelisted entries")

    # ---------- simple systemd service probes ----------
    def probe_service(self, unit: str, ok_msg: str, fail_detail: str) -> None:
        rc, _, _ = _run(["systemctl", "is-active", "--quiet", unit])
        if rc == 0:
            self.ok(ok_msg)
        else:
            self.fail(unit, fail_detail)

    # ---------- VPN country policy ----------
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

    # ---------- ops drift audit ----------
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

    # ---------- audio sweep liveness ----------
    def probe_normalize_sweep(self) -> None:
        if (paths.VAR_STATE / "normalize-driver.done").is_file():
            self.ok("normalize sweep complete (sentinel present)")
            return
        rc, _, _ = _run(["pgrep", "-f", "normalize-audio.py.*--scan"])
        if rc == 0:
            self.ok("normalize sweep running")
            return
        log = paths.VAR_LOG / "normalize-driver.log"
        try:
            last_tick = int(log.stat().st_mtime)
        except OSError:
            last_tick = 0
        age = int(time.time()) - last_tick
        if age > 1200:
            self.fail("normalize-driver",
                      f"no sweep + no sentinel + driver.log stale ({age}s)")
        else:
            self.warn("normalize-driver",
                      "no sweep this tick; cron should relaunch within 15m")

    # ---------- orchestration ----------
    def run_all(self) -> int:
        self.probe_gluetun()
        self.probe_containers()
        self.probe_arr("sonarr", paths.SONARR_URL, "SONARR_API_KEY")
        self.probe_arr("radarr", paths.RADARR_URL, "RADARR_API_KEY")
        self.probe_arr_hardlinks("sonarr", paths.SONARR_URL, "SONARR_API_KEY")
        self.probe_arr_hardlinks("radarr", paths.RADARR_URL, "RADARR_API_KEY")
        self.probe_arr_format_scores("sonarr", paths.SONARR_URL, "SONARR_API_KEY")
        self.probe_arr_format_scores("radarr", paths.RADARR_URL, "RADARR_API_KEY")
        self.probe_arr_silent_gap("radarr", paths.RADARR_URL, "RADARR_API_KEY")
        self.probe_bazarr_api(paths.BAZARR_URL)
        self.probe_qbit(paths.QBIT_URL)
        self.probe_bazarr_profiles(paths.BAZARR_URL)
        self.probe_ufw()
        self.probe_perimeter()
        self.probe_realtek()
        self.probe_repo_root()
        self.probe_service("consolidate-watch.service",
                           "consolidate-watch.service active",
                           "inactive — new imports won't auto-trigger v2 pipeline")
        self.probe_service("malware-guard.service",
                           "malware-guard.service active",
                           "inactive — Windows .exe / .bat in /downloads not reaped")
        self.probe_vpn_country()
        self.probe_ops_audit()
        self.probe_normalize_sweep()
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
