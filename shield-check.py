#!/usr/bin/env python3
"""Daily, REPORT-ONLY drift check of the Shield TV Pro units against `ops/shield/profile.json`.

Telegrams ONLY when something changes. Never changes a device (an automatic debloat once broke
Play Store and voice search): the only adb commands are `adb connect`, `adb devices` and ONE
`adb shell` per device running `pm list packages`, `pm list packages -d` and `settings get`.
Each finding gets a remediation HINT line in the log, printed and never executed. Do not add an
apply mode.

Every command in the shell is wrapped in a marker line (per-run random nonce, so device output
can't forge one) and an exit-status line. A failed or status-less command, or cut-short output,
makes the device unreachable for that run: an empty list or setting is never taken on faith.

CHECKED (`_doc` in the profile has the details)
  disabled_packages          must be disabled IF installed (absent is fine)
  required_enabled_packages  must be installed and not disabled
  absent_packages            must not be installed
  settings                   exact `settings get <ns> <key>` strings
  component_lists            colon-separated component settings; every listed component must
                             be present (`pkg/.Cls` equals `pkg/pkg.Cls`)
Device-specific settings (CEC, IR, volume, network, audio, display, name) are not in the
profile, so never flagged.

CONFIGURATION
`SHIELD_DEVICES` in `.env`: space-separated `name=host:port` entries, e.g.
`living=192.0.2.10:5555 bedroom=192.0.2.11:5555`. Addresses live ONLY there (public repo). A
malformed entry or duplicate name exits 2 and sends nothing; unset or empty means nothing to
check (exit 0). `ADB` overrides the adb binary (default: `adb` from PATH).

ALERTING (at most ONE Telegram message per run, combining devices)
  * a device's finding set differs from the last run's: its CURRENT findings (or "back in line
    with the profile"). A first run alerts only a device with findings.
  * unreachable: alert once when the consecutive count reaches 3 (daily runs), and once on
    recovery after such an alert. Unreachable runs leave stored findings alone.
  * `unauthorized` (TV awaiting the ADB debugging prompt) is a finding, not unreachable.
  * State (`var/state/shield-check.json`: per device, the sorted findings and the unreachable
    counter) advances only after the alert was delivered (or none was needed), so a failed send
    is retried next run. A missing or corrupt state file is logged and treated as empty.
  * An alert describes the CURRENT condition against the last DELIVERED state; there is no
    queue, so a condition that clears before delivery is never reported.
  * Every alerted device's header line is always sent; only long finding lists are cut
    ("...and N more (see the log)").

Exit codes:
  0   every checked device matches the profile
  1   drift or an unreachable device
  2   configuration error (SHIELD_DEVICES, the profile, an unknown --device)
  75  another run holds the lock (EX_TEMPFAIL); nothing was checked

`--no-notify` prints findings and hints but sends nothing and writes no state (nor takes the
run lock). `--device NAME` checks one device and leaves the stored state of the others alone.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from media_stack.clients.telegram import send as telegram_send  # noqa: E402
from media_stack.locking import acquire_singleton_lock  # noqa: E402
from media_stack.paths import (  # noqa: E402
    MEDIA_STACK_ROOT, VAR_RUN, VAR_STATE, ensure_var_dirs, load_env_file,
)
from media_stack.state import load_state_locked, save_state  # noqa: E402

PROFILE_PATH = Path(__file__).resolve().parent / "ops" / "shield" / "profile.json"
STATE_FILE = VAR_STATE / "shield-check.json"
LOCK_FILE = VAR_RUN / "shield-check.lock"

# Every adb call is bounded and never retried.
CONNECT_TIMEOUT_S = 20.0
DEVICES_TIMEOUT_S = 10.0
PROBE_TIMEOUT_S = 60.0

UNREACHABLE_ALERT_AT = 3
# Telegram's limit is 4096 characters; leave room for the footer.
MAX_MESSAGE_CHARS = 3800
MAX_VALUE_CHARS = 80
MORE_RESERVE = 40   # room per device for its "  ...and N more (see the log)" line
LOG_POINTER = "Remediation hints are in var/log/shield-check.log"
EXIT_BUSY = 75   # sysexits.h EX_TEMPFAIL

NAMESPACES = ("global", "secure", "system")
PROFILE_KEYS = frozenset({
    "_doc", "disabled_packages", "required_enabled_packages", "absent_packages",
    "settings", "component_lists",
})
PACKAGE_LISTS = ("disabled_packages", "required_enabled_packages", "absent_packages")

DEVICE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
HOSTNAME_RE = re.compile(
    r"(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*")
PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+")
SETTING_KEY_RE = re.compile(r"[A-Za-z0-9_.]+")
COMPONENT_RE = re.compile(r"[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+")

# Marker-line prefix; each run appends a random nonce.
MARK = "@@"

UNAUTHORIZED_FINDING = "unauthorized: accept the debugging prompt on the TV"


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def _snip(text: str, limit: int = 200) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _lines(out: str) -> list[str]:
    """Split on LF, CRLF or the CRCRLF some adb/pty combinations emit."""
    return re.sub(r"\r*\n|\r", "\n", out).split("\n")


def _show(value: str) -> str:
    """ASCII-escaped and bounded; a cut value gets a digest of the FULL value, since the
    finding text is what state compares."""
    text = ascii(value)
    if len(text) <= MAX_VALUE_CHARS:
        return text
    digest = hashlib.sha256(value.encode("utf-8", "backslashreplace")).hexdigest()[:8]
    return f"{text[: MAX_VALUE_CHARS - 3]}...[sha256:{digest}]"


@dataclass(frozen=True)
class Device:
    name: str
    host: str
    port: int

    @property
    def serial(self) -> str:
        return f"{self.host}:{self.port}"


def _valid_host(host: str) -> bool:
    """IPv4 address or RFC 1123 hostname; digits-and-dots must be a real IPv4 (`999.1.1.1` is not)."""
    if re.fullmatch(r"[0-9.]+", host):
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            return False
        return True
    return HOSTNAME_RE.fullmatch(host) is not None


def parse_devices(raw: str) -> tuple[list[Device], list[str]]:
    devices: list[Device] = []
    problems: list[str] = []
    seen: set[str] = set()
    for entry in raw.split():
        name, eq, addr = entry.partition("=")
        host, colon, port_s = addr.rpartition(":")
        if not (eq and colon):
            problems.append(f"{entry!r}: expected name=host:port")
            continue
        entry_problems = []
        if not DEVICE_NAME_RE.fullmatch(name):
            entry_problems.append("name must match [a-z0-9][a-z0-9-]{0,30}")
        if not _valid_host(host):
            entry_problems.append("host must be an IPv4 address or a hostname")
        if not (re.fullmatch(r"[0-9]{1,5}", port_s) and 1 <= int(port_s) <= 65535):
            entry_problems.append("port must be 1-65535")
        if entry_problems:
            problems.append(f"{entry!r}: " + "; ".join(entry_problems))
            continue
        if name in seen:
            problems.append(f"{entry!r}: duplicate device name {name!r}")
            continue
        seen.add(name)
        devices.append(Device(name, host, int(port_s)))
    return devices, problems


class ProfileError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Profile:
    disabled: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    settings: dict[str, dict[str, str]] = field(default_factory=dict)
    components: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)


def _package_list(value: object, key: str, problems: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list):
        problems.append(f"{key}: must be a list of package names")
        return ()
    out = []
    for item in value:
        if isinstance(item, str) and PACKAGE_RE.fullmatch(item):
            out.append(item)
        else:
            problems.append(f"{key}: invalid package name {item!r}")
    return tuple(out)


def _namespaced(value: object, key: str, problems: list[str]) -> dict[str, dict]:
    if not isinstance(value, dict):
        problems.append(f"{key}: must be an object keyed by namespace")
        return {}
    out: dict[str, dict] = {}
    for ns, entries in value.items():
        if ns not in NAMESPACES:
            problems.append(f"{key}: unknown namespace {ns!r} (expected one of {', '.join(NAMESPACES)})")
        elif not isinstance(entries, dict):
            problems.append(f"{key}.{ns}: must be an object")
        else:
            out[ns] = entries
            for name in entries:
                if not SETTING_KEY_RE.fullmatch(name):
                    problems.append(f"{key}.{ns}: invalid setting name {name!r}")
    return out


def parse_profile(raw: object) -> Profile:
    if not isinstance(raw, dict):
        raise ProfileError(["the profile must be a JSON object"])
    problems: list[str] = []
    unknown = sorted(set(raw) - PROFILE_KEYS)
    if unknown:
        problems.append(f"unknown top-level key(s): {', '.join(unknown)}")
    lists = {key: _package_list(raw.get(key, []), key, problems) for key in PACKAGE_LISTS}
    both = sorted(set(lists["disabled_packages"]) & set(lists["required_enabled_packages"]))
    if both:
        problems.append("in both disabled_packages and required_enabled_packages: " + ", ".join(both))
    both = sorted(set(lists["absent_packages"]) & set(lists["required_enabled_packages"]))
    if both:
        problems.append("in both absent_packages and required_enabled_packages: " + ", ".join(both))

    settings: dict[str, dict[str, str]] = {}
    for ns, entries in _namespaced(raw.get("settings", {}), "settings", problems).items():
        settings[ns] = {}
        for name, expected in entries.items():
            if isinstance(expected, str):
                settings[ns][name] = expected
            else:
                problems.append(f"settings.{ns}.{name}: the expected value must be a string")

    components: dict[str, dict[str, tuple[str, ...]]] = {}
    for ns, entries in _namespaced(raw.get("component_lists", {}), "component_lists", problems).items():
        components[ns] = {}
        for name, wanted in entries.items():
            ok = isinstance(wanted, list) and all(
                isinstance(c, str) and COMPONENT_RE.fullmatch(c) for c in wanted)
            if ok:
                components[ns][name] = tuple(wanted)
            else:
                problems.append(f"component_lists.{ns}.{name}: must be a list of pkg/Class components")
    if problems:
        raise ProfileError(problems)
    return Profile(lists["disabled_packages"], lists["required_enabled_packages"],
                   lists["absent_packages"], settings, components)


def load_profile(path: Path) -> Profile:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProfileError([f"cannot read {path.name}: {type(exc).__name__}: {exc}"]) from exc
    return parse_profile(raw)


@dataclass
class AdbResult:
    rc: int
    out: str
    err: str = ""


class AdbError(Exception):
    """adb could not be run or did not finish in time."""


AdbRunner = Callable[[list[str], float], AdbResult]


def make_adb_runner(binary: str) -> AdbRunner:
    def run(args: list[str], timeout: float) -> AdbResult:
        try:
            p = subprocess.run(
                [binary, *args], capture_output=True, encoding="utf-8", errors="replace",
                timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"{binary} {args[0]} timed out after {timeout:g}s") from exc
        except OSError as exc:
            raise AdbError(f"cannot run {binary}: {type(exc).__name__}: {exc}") from exc
        return AdbResult(p.returncode, p.stdout, p.stderr)
    return run


class ProbeError(Exception):
    """The probe output was incomplete or implausible."""


@dataclass(frozen=True)
class Snapshot:
    installed: frozenset[str]
    disabled: frozenset[str]
    settings: dict[tuple[str, str], str]


def wanted_settings(profile: Profile) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    for ns in NAMESPACES:
        keys += [(ns, key) for key in profile.settings.get(ns, {})]
        keys += [(ns, key) for key in profile.components.get(ns, {})]
    return list(dict.fromkeys(keys))


def probe_steps(keys: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return ([("packages", "pm list packages"), ("disabled", "pm list packages -d")]
            + [(f"s:{ns}:{key}", f"settings get {ns} {key}") for ns, key in keys])


def build_probe(keys: list[tuple[str, str]], nonce: str) -> str:
    """Read-only; namespaces and keys were validated at profile load."""
    mark = f"{MARK}{nonce}:"
    parts = []
    for name, command in probe_steps(keys):
        parts += [f"echo {mark}{name}", command, f"echo {mark}rc $?"]
    parts.append(f"echo {mark}end")
    return "; ".join(parts)


def parse_probe(out: str, keys: list[tuple[str, str]], nonce: str) -> Snapshot:
    """Only lines starting with this run's `MARK + nonce + ":"` are markers. Raises ProbeError
    on cut-short output, a missing or non-zero status, or no packages (a broken `pm` must not
    read as "every required package missing")."""
    prefix = f"{MARK}{nonce}:"
    steps = probe_steps(keys)
    names = {name for name, _ in steps}
    sections: dict[str, list[str]] = {}
    status: dict[str, int] = {}
    current: str | None = None
    ended = False
    for line in _lines(out):
        if not line.startswith(prefix):
            if current is not None:
                sections[current].append(line)
            continue
        tag = line[len(prefix):]
        rc = re.fullmatch(r"rc ([0-9]+)", tag)
        if tag == "end":
            ended = True
            break
        if rc:
            if current is None:
                raise ProbeError("exit status line without a command")
            status[current] = int(rc.group(1))
            current = None
        elif tag in names:
            current = tag
            sections[tag] = []
        else:
            raise ProbeError(f"unexpected marker {tag!r}")
    if not ended:
        raise ProbeError("output was cut short (no end marker)")
    for name, command in steps:
        if name not in sections:
            raise ProbeError(f"no output for `{command}`")
        if name not in status:
            raise ProbeError(f"no exit status for `{command}`")
        if status[name] != 0:
            raise ProbeError(f"`{command}` exited {status[name]}")

    def packages(name: str) -> frozenset[str]:
        listed = (ln.strip() for ln in sections[name])
        return frozenset(n[len("package:"):] for n in listed if n.startswith("package:"))

    installed = packages("packages")
    if not installed:
        raise ProbeError("`pm list packages` returned no packages")
    values = {(ns, key): "\n".join(sections[f"s:{ns}:{key}"]) for ns, key in keys}
    return Snapshot(installed, packages("disabled"), values)


def parse_device_state(out: str, serial: str) -> str | None:
    for line in _lines(out):
        parts = line.split()
        if len(parts) >= 2 and parts[0] == serial:
            return parts[1]
    return None


@dataclass(frozen=True)
class Finding:
    text: str   # stable, sortable; what the state and Telegram hold
    hint: str   # printed to the log, never executed


def normalize_component(component: str) -> str:
    """`pkg/.Cls` -> `pkg/pkg.Cls` (Android's ComponentName expansion)."""
    component = component.strip()
    pkg, slash, cls = component.partition("/")
    if slash and cls.startswith("."):
        return f"{pkg}/{pkg}{cls}"
    return component


def parse_component_list(raw: str) -> list[str]:
    """`null` and empty give []. Entries are not normalized."""
    if raw.strip() in ("", "null"):
        return []
    return [c.strip() for c in raw.split(":") if c.strip()]


def evaluate(profile: Profile, snap: Snapshot, serial: str) -> list[Finding]:
    adb = f"adb -s {serial} shell"
    found: dict[str, Finding] = {}

    def add(text: str, hint: str) -> None:
        found[text] = Finding(text, hint)

    for pkg in profile.disabled:
        if pkg in snap.installed and pkg not in snap.disabled:
            add(f"disabled_packages:{pkg} is enabled",
                f"{adb} pm disable-user --user 0 {pkg}")
    for pkg in profile.required:
        if pkg not in snap.installed:
            add(f"required_enabled_packages:{pkg} is not installed",
                f"install {pkg} from the Play Store, or copy the APK from the other Shield")
        elif pkg in snap.disabled:
            add(f"required_enabled_packages:{pkg} is disabled", f"{adb} pm enable {pkg}")
    for pkg in profile.absent:
        if pkg in snap.installed:
            add(f"absent_packages:{pkg} is installed", f"{adb} pm uninstall {pkg}")

    for ns, entries in profile.settings.items():
        for key, expected in entries.items():
            got = snap.settings[(ns, key)]
            if got != expected:
                if expected == "null":
                    fix = f"{adb} settings delete {ns} {key}"
                else:
                    fix = f"{adb} settings put {ns} {key} {shlex.quote(expected)}"
                add(f"settings.{ns}.{key}: expected {_show(expected)}, got {_show(got)}", fix)

    for ns, lists in profile.components.items():
        for key, wanted in lists.items():
            have = parse_component_list(snap.settings[(ns, key)])
            have_norm = {normalize_component(c) for c in have}
            missing = [c for c in wanted if normalize_component(c) not in have_norm]
            merged = shlex.quote(":".join([*have, *(normalize_component(c) for c in missing)]))
            for component in missing:
                add(f"component_lists.{ns}.{key}: missing {component}",
                    f"{adb} settings put {ns} {key} {merged}")
    return sorted(found.values(), key=lambda f: f.text)


@dataclass
class DeviceReport:
    device: Device
    reachable: bool
    findings: list[Finding] = field(default_factory=list)
    detail: str = ""   # why unreachable (log only)

    @property
    def clean(self) -> bool:
        return self.reachable and not self.findings


def check_device(device: Device, profile: Profile, adb: AdbRunner) -> DeviceReport:
    """Never raises for a device problem: any adb failure makes it unreachable."""
    serial = device.serial

    def down(why: str) -> DeviceReport:
        return DeviceReport(device, False, [], why)

    try:
        conn = adb(["connect", serial], CONNECT_TIMEOUT_S)
        log(f"[{device.name}] adb connect: {_snip(conn.out or conn.err)}")
        listing = adb(["devices"], DEVICES_TIMEOUT_S)
        if listing.rc != 0:
            return down(f"adb devices failed rc={listing.rc}: {_snip(listing.err or listing.out)}")
        state = parse_device_state(listing.out, serial)
        if state == "unauthorized":
            return DeviceReport(device, True, [Finding(
                UNAUTHORIZED_FINDING,
                "accept the debugging prompt on the TV, then run shield-check.py "
                f"--device {device.name} again")])
        if state != "device":
            return down(f"adb state is {state}" if state else "not listed by adb devices")
        keys = wanted_settings(profile)
        nonce = secrets.token_hex(8)
        shell = adb(["-s", serial, "shell", build_probe(keys, nonce)], PROBE_TIMEOUT_S)
        if shell.rc != 0:
            return down(f"adb shell failed rc={shell.rc}: {_snip(shell.err or shell.out)}")
        snap = parse_probe(shell.out, keys, nonce)
    except (AdbError, ProbeError) as exc:
        return down(str(exc))
    log(f"[{device.name}] read {len(snap.installed)} packages "
        f"({len(snap.disabled)} disabled) and {len(snap.settings)} settings")
    return DeviceReport(device, True, evaluate(profile, snap, serial))


def log_report(rep: DeviceReport) -> None:
    name = rep.device.name
    if not rep.reachable:
        log(f"[{name}] UNREACHABLE: {rep.detail}")
    elif not rep.findings:
        log(f"[{name}] OK: matches the profile")
    else:
        log(f"[{name}] DRIFT: {len(rep.findings)} finding(s)")
        hinted: set[str] = set()
        for f in rep.findings:
            log(f"[{name}]   {f.text}")
            if f.hint not in hinted:
                hinted.add(f.hint)
                log(f"[{name}]     HINT {f.hint}")


@dataclass(frozen=True)
class DeviceState:
    """Last delivered run's sorted findings and consecutive-unreachable count. No entry
    equals the default, so a first run needs no special case."""
    findings: tuple[str, ...] = ()
    unreachable: int = 0


def _state_entry(entry: object) -> DeviceState | None:
    if not isinstance(entry, dict):
        return None
    findings, count = entry.get("findings"), entry.get("unreachable")
    if not isinstance(findings, list) or not all(isinstance(f, str) for f in findings):
        return None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    return DeviceState(tuple(sorted(set(findings))), count)


def load_states(state_file: Path) -> dict[str, DeviceState]:
    """Never raises: an unreadable file is empty and an invalid entry is skipped (both logged)."""
    try:
        raw = load_state_locked(state_file)
    except OSError as exc:
        log(f"state: {exc}; treating it as empty")
        return {}
    states: dict[str, DeviceState] = {}
    for name, entry in raw.items():
        state = _state_entry(entry) if DEVICE_NAME_RE.fullmatch(str(name)) else None
        if state is None:
            log(f"state: ignoring invalid entry {name!r}")
        else:
            states[name] = state
    return states


def save_states(state_file: Path, states: dict[str, DeviceState]) -> None:
    payload = {name: {"findings": list(s.findings), "unreachable": s.unreachable}
               for name, s in sorted(states.items())}
    try:
        save_state(state_file, payload)
    except OSError as exc:
        log(f"ERROR: could not write {state_file.name}: {type(exc).__name__}: {exc}; "
            f"the next run will repeat today's alerts")


def advance(prev: DeviceState, rep: DeviceReport) -> tuple[DeviceState, list[str]]:
    """`(state to store once delivered, alert lines)`. Lines starting with two spaces detail
    the line above."""
    if not rep.reachable:
        count = prev.unreachable + 1
        lines = []
        if count == UNREACHABLE_ALERT_AT:
            lines.append(f"unreachable for {count} consecutive daily runs")
        return DeviceState(prev.findings, count), lines

    lines = []
    if prev.unreachable >= UNREACHABLE_ALERT_AT:
        lines.append("reachable again")
    current = tuple(sorted({f.text for f in rep.findings}))
    if current != prev.findings:
        if current:
            lines.append(f"{len(current)} finding(s) vs the profile")
            lines += [f"  {text}" for text in current]
        else:
            lines.append("back in line with the profile")
    return DeviceState(current, 0), lines


def build_message(alerts: dict[str, list[str]]) -> str:
    """One message under `MAX_MESSAGE_CHARS`. Header lines always appear; only indented detail
    lines are cut (with a count). Headers are reserved first and the rest shared evenly, so one
    long list can't push another device out. Device names only, no addresses or hints."""
    head = f"Shield check: {len(alerts)} device(s) changed"
    blocks = {}
    room = MAX_MESSAGE_CHARS - len(head) - len(LOG_POINTER) - 4
    for name, lines in alerts.items():
        headers = [f"{name}: {ln}" for ln in lines if not ln.startswith("  ")]
        details = [ln for ln in lines if ln.startswith("  ")]
        blocks[name] = (headers, details)
        room -= 1 + sum(len(h) + 1 for h in headers) + MORE_RESERVE
    kept = {}
    by_size = sorted(blocks, key=lambda n: sum(len(d) + 1 for d in blocks[n][1]))
    for i, name in enumerate(by_size):
        share = room // (len(by_size) - i)
        used = count = 0
        for detail in blocks[name][1]:
            if used + len(detail) + 1 > share:
                break
            used += len(detail) + 1
            count += 1
        kept[name] = count
        room -= used
    out = [head]
    for name, (headers, details) in blocks.items():
        out += ["", *headers, *details[: kept[name]]]
        if kept[name] < len(details):
            out.append(f"  ...and {len(details) - kept[name]} more (see the log)")
    return "\n".join([*out, "", LOG_POINTER])


def telegram_credentials() -> tuple[str, str]:
    """`(token, chat_id)`, either possibly empty. Process env wins; the Telegram plugin's
    `.env` (`TELEGRAM_ENV`, never the repo's `.env`) fills in the rest."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if token and chat:
        return token, chat
    env_path = None
    try:
        env_path = Path(os.environ.get("TELEGRAM_ENV")
                        or Path.home() / ".claude" / "channels" / "telegram" / ".env")
        vals = load_env_file(env_path)
    except Exception as exc:
        log(f"notify: could not read the Telegram .env ({env_path}): "
            f"{type(exc).__name__}: {exc}")
        return token, chat
    return token or vals.get("TELEGRAM_BOT_TOKEN", ""), chat or vals.get("TELEGRAM_CHAT_ID", "")


def send_alert(text: str) -> bool:
    """NEVER raises: missing credentials or a failed send return False and the caller keeps its state."""
    token, chat = telegram_credentials()
    if not token or not chat:
        log("notify skipped: no Telegram credentials")
        return False
    try:
        ok = telegram_send(token, chat, text)
    except Exception as exc:
        log(f"notify error (non-fatal): {type(exc).__name__}")
        return False
    log("notify sent" if ok else "notify failed (non-fatal)")
    return ok


def main(argv: list[str] | None = None, *, adb: AdbRunner | None = None,
         send: Callable[[str], bool] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", metavar="NAME",
                    help="check only this device; the stored state of the others is left as is")
    ap.add_argument("--no-notify", action="store_true",
                    help="print findings and hints only: no Telegram, no state write")
    args = ap.parse_args(argv)

    # Process env wins over .env even when empty (empty SHIELD_DEVICES = nothing configured).
    env = {**load_env_file(MEDIA_STACK_ROOT / ".env"), **os.environ}
    raw_devices = env.get("SHIELD_DEVICES", "")
    configured, problems = parse_devices(raw_devices)
    if problems:
        for problem in problems:
            log(f"ERROR: SHIELD_DEVICES: {problem}")
        return 2
    if not configured:
        log("no Shield devices configured (SHIELD_DEVICES is unset or empty)")
        return 0
    selected = configured
    if args.device:
        selected = [d for d in configured if d.name == args.device]
        if not selected:
            log(f"ERROR: --device {args.device!r} is not in SHIELD_DEVICES "
                f"({', '.join(d.name for d in configured)})")
            return 2
    try:
        profile = load_profile(PROFILE_PATH)
    except ProfileError as exc:
        for problem in exc.problems:
            log(f"ERROR: profile: {problem}")
        return 2

    if adb is None:
        adb = make_adb_runner(env.get("ADB") or "adb")
    notify = not args.no_notify
    lock_fp = None   # keep referenced: the flock goes with it
    stored: dict[str, DeviceState] = {}
    if notify:
        # Read state, maybe send, write state: one run at a time.
        ensure_var_dirs()
        lock_fp = acquire_singleton_lock(LOCK_FILE)
        if lock_fp is None:
            log("another shield-check run holds the lock; nothing was checked")
            return EXIT_BUSY
        stored = load_states(STATE_FILE)
    else:
        log("--no-notify: nothing will be sent and no state written")

    reports = []
    for device in selected:
        report = check_device(device, profile, adb)
        log_report(report)
        reports.append(report)
    status = 0 if all(r.clean for r in reports) else 1

    if notify:
        alerts: dict[str, list[str]] = {}
        advanced: dict[str, DeviceState] = {}
        for report in reports:
            name = report.device.name
            advanced[name], lines = advance(stored.get(name, DeviceState()), report)
            if lines:
                alerts[name] = lines
        # A failed send keeps the old state, so a persisting condition is re-alerted.
        delivered = True
        if alerts:
            delivered = (send or send_alert)(build_message(alerts))
            if not delivered:
                log(f"alert not delivered; the stored state of {', '.join(alerts)} is kept "
                    f"so the next run retries")
        updated = dict(stored)
        for name, state in advanced.items():
            if delivered or name not in alerts:
                updated[name] = state
        if not args.device:
            updated = {n: s for n, s in updated.items() if n in {d.name for d in configured}}
        if updated != stored:
            save_states(STATE_FILE, updated)
    return status


if __name__ == "__main__":
    sys.exit(main())
