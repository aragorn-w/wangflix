"""Tests for shield-check.py, the report-only Shield drift check.

The script must never change a device and must send at most one Telegram
message per run, only when something changed.  adb is a fake runner injected
through `main(adb=...)` that raises on any statement outside the read-only set,
so a stray `pm disable` or `settings put` fails a test.  Telegram is a
recording stub; state, lock and profile paths live in `tmp_path` and the
environment is cleaned of ambient SHIELD_DEVICES, ADB and TELEGRAM_* values.

Only RFC 5737 documentation addresses appear here: the repo is public.
"""
import copy
import fcntl
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
_spec = importlib.util.spec_from_file_location(
    "shield_check", str(PROJECT_ROOT / "shield-check.py"))
assert _spec is not None and _spec.loader is not None
sc = importlib.util.module_from_spec(_spec)
# The dataclasses are defined under `from __future__ import annotations`; the
# dataclasses module resolves string annotations through sys.modules.
sys.modules[_spec.name] = sc
_spec.loader.exec_module(sc)

LIVING = "192.0.2.10:5555"
BEDROOM = "192.0.2.11:5555"

BASE = {
    "_doc": "test profile",
    "disabled_packages": ["com.example.bloat", "com.example.junk"],
    "required_enabled_packages": ["com.example.keep", "com.example.voice"],
    "absent_packages": ["com.example.banned"],
    "settings": {
        "global": {"stay_on_while_plugged_in": "0"},
        "secure": {"screensaver_enabled": "0"},
        "system": {"nv_fan_mode": "cool"},
    },
    "component_lists": {"secure": {
        "enabled_accessibility_services": ["com.example.keep/.Service"],
        "enabled_input_methods": ["com.example.ime/com.example.ime.Ime"],
    }},
}
PROFILE = sc.parse_profile(copy.deepcopy(BASE))

BLOAT_ENABLED = "disabled_packages:com.example.bloat is enabled"
BANNED_INSTALLED = "absent_packages:com.example.banned is installed"
FAN_QUIET = "settings.system.nv_fan_mode: expected 'cool', got 'quiet'"
IME_MISSING = "component_lists.secure.enabled_input_methods: missing com.example.ime/com.example.ime.Ime"


# --- the fake adb ----------------------------------------------------------

class FakeDevice:
    """One Shield: what `pm list packages [-d]` and `settings get` would say."""

    def __init__(self, installed=(), disabled=(), settings=None, *, state="device",
                 connect="ok", eol="\n", shell_rc=0, truncate=False, failing=(), post=None):
        self.installed = set(installed)
        self.disabled = set(disabled)
        self.settings = dict(settings or {})
        self.state = state          # what `adb devices` lists once connected
        self.connect = connect      # "ok", "refused" (never listed) or "timeout"
        self.eol = eol
        self.shell_rc = shell_rc
        self.truncate = truncate    # drop the end marker, as a cut-short shell would
        self.failing = set(failing)  # statements that print nothing and exit 1
        self.post = post            # rewrites the output lines, to model a broken shell

    def enable(self, pkg):
        self.installed.add(pkg)
        self.disabled.discard(pkg)

    def run_script(self, script):
        out, status = [], 0
        for stmt in script.split("; "):
            if stmt.startswith("echo ") and stmt.endswith(" $?"):
                out.append(f"{stmt[len('echo '):-len(' $?')]} {status}")
            elif stmt.startswith("echo "):
                out.append(stmt[len("echo "):])
            else:
                if stmt == "pm list packages":
                    lines = [f"package:{p}" for p in sorted(self.installed)]
                elif stmt == "pm list packages -d":
                    lines = [f"package:{p}" for p in sorted(self.disabled)]
                elif stmt.startswith("settings get "):
                    _, _, ns, key = stmt.split(" ")
                    lines = [self.settings.get((ns, key), "null")]
                else:
                    raise AssertionError(f"not an allowed read-only statement: {stmt!r}")
                status = 1 if stmt in self.failing else 0
                out += [] if status else lines
        if self.post:
            out = self.post(out)
        if self.truncate:
            out = out[:-1]
        return self.eol.join(out) + self.eol


def compliant(profile=PROFILE, **kwargs):
    """A device that matches `profile` exactly."""
    settings = {(ns, k): v for ns, kv in profile.settings.items() for k, v in kv.items()}
    for ns, kv in profile.components.items():
        for key, wanted in kv.items():
            settings[(ns, key)] = ":".join(wanted)
    installed = {"android", *profile.required, *profile.disabled}
    return FakeDevice(installed, profile.disabled, settings, **kwargs)


class FakeAdb:
    """The injected runner.  Records every call as (args, timeout)."""

    def __init__(self, devices):
        self.devices = devices
        self.connected = set()
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append((list(args), timeout))
        if args[0] == "connect":
            dev = self.devices.get(args[1])
            if dev is None or dev.connect == "timeout":
                raise sc.AdbError(f"adb connect timed out after {timeout:g}s")
            if dev.connect == "refused":
                return sc.AdbResult(0, f"failed to connect to '{args[1]}'\n")
            self.connected.add(args[1])
            return sc.AdbResult(0, f"connected to {args[1]}\n")
        if args[0] == "devices":
            rows = [f"{s}\t{self.devices[s].state}" for s in sorted(self.connected)]
            return sc.AdbResult(0, "List of devices attached\n" + "\n".join(rows) + "\n\n")
        if args[0] == "-s" and args[2] == "shell":
            dev = self.devices[args[1]]
            if dev.state != "device":
                return sc.AdbResult(1, "", "error: device offline")
            return sc.AdbResult(dev.shell_rc, dev.run_script(args[3]))
        raise AssertionError(f"unexpected adb call: {args}")

    def serials_touched(self):
        return {a[1] for a, _ in self.calls if a[0] in ("connect", "-s")}


class Sender:
    def __init__(self, ok=True):
        self.ok = ok
        self.messages = []

    def __call__(self, text):
        self.messages.append(text)
        return self.ok


class World:
    """Two compliant devices, a recording Telegram stub and a way to run main()."""

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.living = compliant()
        self.bedroom = compliant()
        self.adb = FakeAdb({LIVING: self.living, BEDROOM: self.bedroom})
        self.sender = Sender()
        self.devices(f"living={LIVING} bedroom={BEDROOM}")

    def devices(self, value):
        self.monkeypatch.setenv("SHIELD_DEVICES", value)

    def single(self):
        self.devices(f"living={LIVING}")
        return self

    def run(self, *argv):
        return sc.main(list(argv), adb=self.adb, send=self.sender)

    def state(self):
        return json.loads(sc.STATE_FILE.read_text())

    @property
    def messages(self):
        return self.sender.messages


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    for key in ("SHIELD_DEVICES", "ADB", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(key, raising=False)
    # The plugin's real .env must never be read: point it at a file that is not there.
    monkeypatch.setenv("TELEGRAM_ENV", str(tmp_path / "no-such-telegram.env"))
    (tmp_path / "run").mkdir()
    (tmp_path / "stack").mkdir()
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(BASE))
    monkeypatch.setattr(sc, "PROFILE_PATH", profile)
    monkeypatch.setattr(sc, "STATE_FILE", tmp_path / "state" / "shield-check.json")
    monkeypatch.setattr(sc, "LOCK_FILE", tmp_path / "run" / "shield-check.lock")
    monkeypatch.setattr(sc, "MEDIA_STACK_ROOT", tmp_path / "stack")   # no .env there
    monkeypatch.setattr(sc, "ensure_var_dirs", Mock())


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def snap(installed=(), disabled=(), settings=None):
    base = {("global", "stay_on_while_plugged_in"): "0", ("secure", "screensaver_enabled"): "0",
            ("system", "nv_fan_mode"): "cool",
            ("secure", "enabled_accessibility_services"): "com.example.keep/.Service",
            ("secure", "enabled_input_methods"): "com.example.ime/com.example.ime.Ime"}
    base.update(settings or {})
    return sc.Snapshot(frozenset(installed), frozenset(disabled), base)


def texts(findings):
    return [f.text for f in findings]


# ===========================================================================
# SHIELD_DEVICES
# ===========================================================================

def test_parse_devices_valid_keeps_order_and_builds_the_serial():
    devices, problems = sc.parse_devices(f"living={LIVING} bedroom={BEDROOM}")
    assert problems == []
    assert [(d.name, d.host, d.port) for d in devices] == [
        ("living", "192.0.2.10", 5555), ("bedroom", "192.0.2.11", 5555)]
    assert devices[0].serial == LIVING


def test_parse_devices_accepts_hostnames_odd_whitespace_and_boundary_values():
    raw = "  living=shield-1.example.net:5555\n\tbedroom=a1:65535 " + "a" * 31 + "=192.0.2.12:1"
    devices, problems = sc.parse_devices(raw)
    assert problems == []
    assert [d.name for d in devices] == ["living", "bedroom", "a" * 31]
    assert [d.port for d in devices] == [5555, 65535, 1]


@pytest.mark.parametrize("raw", ["", "   ", "\n\t "])
def test_parse_devices_empty_is_no_devices_and_no_problems(raw):
    assert sc.parse_devices(raw) == ([], [])


@pytest.mark.parametrize("entry", [
    "living",                           # no address
    "living=192.0.2.10",                # no port
    "living=:5555",                     # empty host
    "=192.0.2.10:5555",                 # empty name
    "Living=192.0.2.10:5555",           # uppercase name
    "-living=192.0.2.10:5555",          # leading hyphen
    "living_room=192.0.2.10:5555",      # underscore
    "a" * 32 + "=192.0.2.10:5555",      # 32 characters is one too many
    "living=192.0.2.10:0",
    "living=192.0.2.10:65536",
    "living=192.0.2.10:abc",
    "living=192.0.2.10:-1",
    "living=192.0.2.10:",
    "living=999.1.1.1:5555",            # digits and dots: must be a real IPv4
    "living=192.0.2.10.5:5555",
    "living=1.2.3:5555",
    "living=bad_host:5555",
    "living=-bad.example:5555",
    "living=a..b:5555",
    "living=host.:5555",
    "living=[::1]:5555",                # IPv6 is not supported
])
def test_parse_devices_rejects_a_malformed_entry(entry):
    devices, problems = sc.parse_devices(entry)
    assert devices == []
    assert len(problems) == 1


def test_parse_devices_rejects_a_duplicate_name_and_reports_every_problem():
    devices, problems = sc.parse_devices(
        f"living={LIVING} living={BEDROOM} bad=192.0.2.12:0 other=192.0.2.13:5555")
    assert [d.name for d in devices] == ["living", "other"]
    assert len(problems) == 2
    assert any("duplicate" in p and "living" in p for p in problems)
    assert any("port" in p for p in problems)


@pytest.mark.parametrize("raw", [
    "living", f"living={LIVING} living={BEDROOM}", f"living={LIVING} bad=192.0.2.12:0"])
def test_main_config_error_exits_2_without_touching_anything(world, raw, capsys):
    world.devices(raw)
    assert world.run() == 2
    assert world.adb.calls == []
    assert world.messages == []
    assert not sc.STATE_FILE.exists()
    assert "ERROR: SHIELD_DEVICES" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["", "   "])
@pytest.mark.parametrize("env_file", [False, True], ids=["no-env-file", "empty-process-env-beats-stack-env-file"])
def test_main_no_devices_configured_exits_0(world, tmp_path, value, env_file, capsys):
    if env_file:
        (tmp_path / "stack" / ".env").write_text(f"SHIELD_DEVICES=living={LIVING}\n")
    world.devices(value)                       # present in os.environ, but empty
    assert world.run() == 0
    assert "no Shield devices configured" in capsys.readouterr().out
    assert world.adb.calls == [] and world.messages == [] and not sc.STATE_FILE.exists()


def test_main_reads_shield_devices_from_the_stack_env_file(world, tmp_path, monkeypatch):
    monkeypatch.delenv("SHIELD_DEVICES")
    (tmp_path / "stack" / ".env").write_text(f'SHIELD_DEVICES="living={LIVING}"\n')
    assert world.run("--no-notify") == 0
    assert world.adb.serials_touched() == {LIVING}


def test_main_process_env_beats_the_stack_env_file(world, tmp_path):
    (tmp_path / "stack" / ".env").write_text(f"SHIELD_DEVICES=bedroom={BEDROOM}\n")
    world.single()
    assert world.run("--no-notify") == 0
    assert world.adb.serials_touched() == {LIVING}


# ===========================================================================
# The profile
# ===========================================================================

def _profile_with(tmp_path, mutate):
    data = copy.deepcopy(BASE)
    mutate(data)
    path = tmp_path / "p.json"
    path.write_text(json.dumps(data))
    return path


def test_profile_loads_and_ignores_the_doc_field(tmp_path):
    profile = sc.load_profile(_profile_with(tmp_path, lambda d: None))
    assert profile.disabled == ("com.example.bloat", "com.example.junk")
    assert profile.required == ("com.example.keep", "com.example.voice")
    assert profile.absent == ("com.example.banned",)
    assert profile.settings["system"] == {"nv_fan_mode": "cool"}
    assert profile.components["secure"]["enabled_input_methods"] == (
        "com.example.ime/com.example.ime.Ime",)


def test_profile_without_the_optional_sections_is_valid():
    profile = sc.parse_profile({"_doc": "x"})
    assert profile.disabled == () and profile.settings == {} and profile.components == {}


@pytest.mark.parametrize("mutate, expected", [
    (lambda d: d.update(extra=[]), "unknown top-level key"),
    (lambda d: d["disabled_packages"].append("com.example.keep"), "both disabled_packages and required_enabled_packages"),
    (lambda d: d["absent_packages"].append("com.example.keep"), "both absent_packages and required_enabled_packages"),
    (lambda d: d["disabled_packages"].append("singleword"), "invalid package name"),
    (lambda d: d["disabled_packages"].append("com.example bloat"), "invalid package name"),
    (lambda d: d["absent_packages"].append("com.example;reboot"), "invalid package name"),
    (lambda d: d["absent_packages"].append("1com.example"), "invalid package name"),
    (lambda d: d["absent_packages"].append("com..example"), "invalid package name"),
    (lambda d: d["absent_packages"].append(5), "invalid package name"),
    (lambda d: d.update(disabled_packages="com.example.bloat"), "must be a list"),
    (lambda d: d.update(settings=[]), "settings: must be an object"),
    (lambda d: d["settings"].update(sys={"a": "1"}), "unknown namespace"),
    (lambda d: d["settings"].update(system="x"), "settings.system: must be an object"),
    (lambda d: d["settings"]["system"].update({"bad key": "1"}), "invalid setting name"),
    (lambda d: d["settings"]["system"].update(nv_fan_mode=5), "must be a string"),
    (lambda d: d["component_lists"].update(sys={}), "unknown namespace"),
    (lambda d: d["component_lists"]["secure"].update(enabled_input_methods="a/b"), "list of pkg/Class"),
    (lambda d: d["component_lists"]["secure"].update(enabled_input_methods=["noslash"]), "list of pkg/Class"),
    (lambda d: d["component_lists"]["secure"].update(enabled_input_methods=["a/b c"]), "list of pkg/Class"),
    (lambda d: d["component_lists"]["secure"].update(enabled_input_methods=[5]), "list of pkg/Class"),
])
def test_profile_validation_failures(tmp_path, mutate, expected):
    with pytest.raises(sc.ProfileError) as exc:
        sc.load_profile(_profile_with(tmp_path, mutate))
    assert expected in str(exc.value)


def test_profile_that_is_not_an_object_or_not_json_or_missing_is_an_error(tmp_path):
    path = tmp_path / "p.json"
    for content in ("[]", "{not json", ""):
        path.write_text(content)
        with pytest.raises(sc.ProfileError):
            sc.load_profile(path)
    with pytest.raises(sc.ProfileError):
        sc.load_profile(tmp_path / "missing.json")


def test_profile_reports_every_problem_at_once(tmp_path):
    def mutate(d):
        d["disabled_packages"].append("com.example.keep")
        d["settings"]["system"]["nv_fan_mode"] = 5
        d["extra"] = 1
    with pytest.raises(sc.ProfileError) as exc:
        sc.load_profile(_profile_with(tmp_path, mutate))
    assert len(exc.value.problems) == 3


@pytest.mark.parametrize("content", ['{"nope": 1}', None], ids=["invalid", "missing"])
def test_main_bad_profile_exits_2_and_checks_nothing(world, content, capsys):
    if content is None:
        sc.PROFILE_PATH.unlink()
    else:
        sc.PROFILE_PATH.write_text(content)
    world.single()
    assert world.run() == 2
    assert world.adb.calls == [] and world.messages == [] and not sc.STATE_FILE.exists()
    assert "ERROR: profile" in capsys.readouterr().out


# ===========================================================================
# The real profile
# ===========================================================================

REAL_PROFILE = PROJECT_ROOT / "ops" / "shield" / "profile.json"


def test_real_profile_validates():
    profile = sc.load_profile(REAL_PROFILE)
    assert profile.disabled and profile.required and profile.absent
    assert profile.settings.keys() == {"global", "secure", "system"}
    assert profile.components["secure"].keys() == {
        "enabled_accessibility_services", "enabled_input_methods"}


def test_real_profile_has_the_operator_approved_additions_and_no_overlap():
    profile = sc.load_profile(REAL_PROFILE)
    assert {"com.nvidia.shield.smbserver", "com.google.android.tv"} <= set(profile.disabled)
    assert profile.settings["system"]["nv_power_mode"] == "0"
    assert not set(profile.disabled) & set(profile.required)
    assert not set(profile.absent) & set(profile.required)


def test_real_profile_has_no_address_mac_or_at_sign():
    raw = REAL_PROFILE.read_text()
    assert "@" not in raw
    assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", raw)
    assert not re.search(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b", raw)


def test_real_profile_doc_names_what_is_deliberately_left_out():
    doc = json.loads(REAL_PROFILE.read_text())["_doc"]
    for word in ("HDMI-CEC", "IR remote", "volume-control", "Ethernet", "audio output",
                 "display mode", "device name", "bedroom"):
        assert word in doc


def test_real_profile_leaves_device_specific_settings_out():
    body = json.loads(REAL_PROFILE.read_text())
    body.pop("_doc")      # the prose may name them; the data must not list them
    data = json.dumps(body).lower()
    for word in ("cec", "hdmi", "ir_", "volume_control", "ethernet", "display_mode", "device_name"):
        assert word not in data


def test_a_device_matching_the_real_profile_has_no_findings_in_either_projectivy_form():
    profile = sc.load_profile(REAL_PROFILE)
    service = "com.spocky.projengmenu/com.spocky.projengmenu.services.ProjectivyAccessibilityService"
    for stored in (service, service.replace("/com.spocky.projengmenu.", "/.")):
        device = compliant(profile)
        device.settings[("secure", "enabled_accessibility_services")] = stored
        adb = FakeAdb({LIVING: device})
        report = sc.check_device(sc.Device("living", "192.0.2.10", 5555), profile, adb)
        assert report.reachable and report.findings == []


def test_real_profile_probe_command_stays_short_enough_for_one_adb_shell():
    profile = sc.load_profile(REAL_PROFILE)
    assert len(sc.build_probe(sc.wanted_settings(profile), NONCE)) < 4000


# ===========================================================================
# Component normalization
# ===========================================================================

@pytest.mark.parametrize("given, expected", [
    ("com.a/.Svc", "com.a/com.a.Svc"),
    ("com.a/com.a.Svc", "com.a/com.a.Svc"),
    ("com.a/.sub.Svc", "com.a/com.a.sub.Svc"),
    ("com.a/other.pkg.Svc", "com.a/other.pkg.Svc"),
    ("  com.a/.Svc  ", "com.a/com.a.Svc"),
    ("noslash", "noslash"),
    ("", ""),
])
def test_normalize_component(given, expected):
    assert sc.normalize_component(given) == expected


@pytest.mark.parametrize("raw", ["null", "", "   ", ":", " : "])
def test_component_list_that_is_unset_or_empty_has_no_components(raw):
    assert sc.parse_component_list(raw) == []


def test_component_list_splits_on_colons_and_drops_empty_entries():
    assert sc.parse_component_list("a/.B::c/d.E:") == ["a/.B", "c/d.E"]


def test_short_and_long_component_forms_both_satisfy_the_short_requirement():
    for stored in ("com.example.keep/.Service", "com.example.keep/com.example.keep.Service"):
        findings = sc.evaluate(
            PROFILE, snap(("com.example.keep",), settings={
                ("secure", "enabled_accessibility_services"): f"x/y.Z:{stored}"}), LIVING)
        assert not [f for f in findings if "enabled_accessibility_services" in f.text]


def test_long_form_requirement_is_satisfied_by_the_stored_short_form():
    findings = sc.evaluate(
        PROFILE, snap(settings={("secure", "enabled_input_methods"): "com.example.ime/.Ime"}), LIVING)
    # com.example.ime/.Ime is com.example.ime/com.example.ime.Ime
    assert not [f for f in findings if "enabled_input_methods" in f.text]


# ===========================================================================
# Finding types
# ===========================================================================

OK_INSTALLED = ("com.example.keep", "com.example.voice")


@pytest.mark.parametrize("extra, disabled", [
    (("com.example.bloat",), ("com.example.bloat",)),
    ((), ()),                                   # a disabled-list package that is not installed at all
    (("com.example.junk",), ("com.example.junk",)),
], ids=["compliant-snapshot", "package-not-installed-is-fine", "disabled-package-is-fine"])
def test_a_compliant_snapshot_has_no_findings(extra, disabled):
    assert sc.evaluate(PROFILE, snap(OK_INSTALLED + extra, disabled), LIVING) == []


def test_enabled_package_that_should_be_disabled_is_a_finding_with_a_hint():
    findings = sc.evaluate(PROFILE, snap(OK_INSTALLED + ("com.example.bloat",)), LIVING)
    assert texts(findings) == [BLOAT_ENABLED]
    assert findings[0].hint == f"adb -s {LIVING} shell pm disable-user --user 0 com.example.bloat"


def test_required_package_not_installed():
    findings = sc.evaluate(PROFILE, snap(("com.example.voice",)), LIVING)
    assert texts(findings) == ["required_enabled_packages:com.example.keep is not installed"]
    assert "Play Store" in findings[0].hint and "other Shield" in findings[0].hint


def test_required_package_disabled():
    findings = sc.evaluate(PROFILE, snap(OK_INSTALLED, ("com.example.voice",)), LIVING)
    assert texts(findings) == ["required_enabled_packages:com.example.voice is disabled"]
    assert findings[0].hint == f"adb -s {LIVING} shell pm enable com.example.voice"


def test_absent_package_installed_is_a_finding_even_when_disabled():
    for disabled in ((), ("com.example.banned",)):
        findings = sc.evaluate(
            PROFILE, snap(OK_INSTALLED + ("com.example.banned",), disabled), LIVING)
        assert texts(findings) == [BANNED_INSTALLED]
        assert findings[0].hint == f"adb -s {LIVING} shell pm uninstall com.example.banned"


def test_setting_mismatch_names_expected_and_got():
    findings = sc.evaluate(PROFILE, snap(OK_INSTALLED, settings={("system", "nv_fan_mode"): "quiet"}),
                           LIVING)
    assert texts(findings) == [FAN_QUIET]
    assert findings[0].hint == f"adb -s {LIVING} shell settings put system nv_fan_mode cool"


def test_an_unset_setting_reads_as_the_literal_null():
    findings = sc.evaluate(PROFILE, snap(OK_INSTALLED, settings={("system", "nv_fan_mode"): "null"}),
                           LIVING)
    assert texts(findings) == ["settings.system.nv_fan_mode: expected 'cool', got 'null'"]


def test_expecting_null_means_the_setting_must_be_unset():
    profile = sc.parse_profile({"settings": {"global": {"some_flag": "null"}}})
    clean = sc.Snapshot(frozenset({"android"}), frozenset(), {("global", "some_flag"): "null"})
    assert sc.evaluate(profile, clean, LIVING) == []
    dirty = sc.Snapshot(frozenset({"android"}), frozenset(), {("global", "some_flag"): "1"})
    findings = sc.evaluate(profile, dirty, LIVING)
    assert texts(findings) == ["settings.global.some_flag: expected 'null', got '1'"]
    assert findings[0].hint == f"adb -s {LIVING} shell settings delete global some_flag"


def test_setting_hint_quotes_a_value_the_shell_would_split():
    profile = sc.parse_profile({"settings": {"global": {"a_name": "two words"}}})
    snapshot = sc.Snapshot(frozenset({"android"}), frozenset(), {("global", "a_name"): "x"})
    assert sc.evaluate(profile, snapshot, LIVING)[0].hint.endswith("settings put global a_name 'two words'")


def test_device_supplied_values_are_escaped_and_bounded_in_findings():
    findings = sc.evaluate(
        PROFILE, snap(OK_INSTALLED, settings={("system", "nv_fan_mode"): "caf" + chr(0xE9) + chr(0x263A) + "x" * 500}),
        LIVING)
    assert len(findings) == 1
    assert findings[0].text.isascii()
    assert "\\xe9" in findings[0].text and len(findings[0].text) < 200


def _fan(value):
    return sc.evaluate(PROFILE, snap(OK_INSTALLED, settings={("system", "nv_fan_mode"): value}), LIVING)


def test_long_values_that_differ_after_the_cut_are_different_findings():
    a, b = "x" * 200 + "A", "x" * 200 + "B"
    (fa,), (fb,) = _fan(a), _fan(b)
    assert fa.text != fb.text
    assert "sha256:" in fa.text and "sha256:" in fb.text
    assert len(fa.text) < 200 and len(fb.text) < 200
    assert fa.text == _fan(a)[0].text                         # stable for the same value


def test_short_values_carry_no_digest():
    (finding,) = _fan("quiet")
    assert finding.text == FAN_QUIET and "sha256" not in finding.text


def test_value_just_at_the_limit_is_not_cut_and_one_over_is():
    exact = "y" * (sc.MAX_VALUE_CHARS - 2)                    # the quotes make it 80
    assert "sha256" not in _fan(exact)[0].text
    assert "sha256" in _fan(exact + "y")[0].text


def test_a_change_between_two_long_wrong_values_alerts(world):
    world.single()
    world.living.settings[("system", "nv_fan_mode")] = "x" * 200 + "A"
    world.run()
    world.living.settings[("system", "nv_fan_mode")] = "x" * 200 + "B"
    world.run()
    assert len(world.messages) == 2
    world.run()
    assert len(world.messages) == 2


def test_missing_component_is_a_finding_and_extra_components_are_fine():
    findings = sc.evaluate(
        PROFILE, snap(OK_INSTALLED, settings={
            ("secure", "enabled_input_methods"): "other.ime/other.ime.Ime:more/.Stuff"}), LIVING)
    assert texts(findings) == [IME_MISSING]
    assert findings[0].hint.endswith(
        "settings put secure enabled_input_methods "
        "other.ime/other.ime.Ime:more/.Stuff:com.example.ime/com.example.ime.Ime")


@pytest.mark.parametrize("raw", ["null", ""])
def test_unset_component_list_misses_every_listed_component(raw):
    findings = sc.evaluate(
        PROFILE, snap(OK_INSTALLED, settings={("secure", "enabled_accessibility_services"): raw}),
        LIVING)
    assert texts(findings) == [
        "component_lists.secure.enabled_accessibility_services: missing com.example.keep/.Service"]


def test_findings_are_sorted_unique_and_stable():
    found = sc.evaluate(PROFILE, snap(("com.example.bloat", "com.example.banned"),
                                      settings={("system", "nv_fan_mode"): "quiet"}), LIVING)
    assert texts(found) == sorted(texts(found))
    assert len(set(texts(found))) == len(found)
    assert texts(found) == texts(sc.evaluate(PROFILE, snap(("com.example.banned", "com.example.bloat"),
                                                           settings={("system", "nv_fan_mode"): "quiet"}),
                                             LIVING))
    assert {BLOAT_ENABLED, BANNED_INSTALLED, FAN_QUIET} <= set(texts(found))


# ===========================================================================
# The probe: one read-only shell command, CRLF-robust parsing
# ===========================================================================

ALLOWED_STATEMENT = re.compile(
    r"echo @@\S+(?: \$\?)?|pm list packages|pm list packages -d"
    r"|settings get (?:global|secure|system) [A-Za-z0-9_.]+")
NONCE = "0123456789abcdef"


def mark(tag, nonce=NONCE):
    return f"@@{nonce}:{tag}"


def probe_lines(*lines):
    return "\n".join(lines) + "\n"


def test_probe_command_is_read_only_statements_only():
    script = sc.build_probe(sc.wanted_settings(PROFILE), NONCE)
    for stmt in script.split("; "):
        assert ALLOWED_STATEMENT.fullmatch(stmt), stmt
    assert script.count("settings get") == 5


def test_probe_wraps_every_command_in_a_marker_and_an_exit_status():
    script = sc.build_probe([("system", "x")], NONCE)
    assert script.split("; ") == [
        f"echo {mark('packages')}", "pm list packages", f"echo {mark('rc')} $?",
        f"echo {mark('disabled')}", "pm list packages -d", f"echo {mark('rc')} $?",
        f"echo {mark('s:system:x')}", "settings get system x", f"echo {mark('rc')} $?",
        f"echo {mark('end')}"]


def test_every_probe_gets_a_fresh_nonce():
    adb = FakeAdb({LIVING: compliant()})
    sc.check_device(DEVICE, PROFILE, adb)
    sc.check_device(DEVICE, PROFILE, adb)
    scripts = [c[0][3] for c in adb.calls if c[0][0] == "-s"]
    nonces = [re.search(r"echo @@([0-9a-f]{16}):packages", sc_).group(1) for sc_ in scripts]
    assert len(nonces) == 2 and nonces[0] != nonces[1]


def test_probe_reads_each_profile_setting_once_even_when_listed_twice():
    profile = sc.parse_profile({
        "settings": {"secure": {"enabled_input_methods": "x"}},
        "component_lists": {"secure": {"enabled_input_methods": ["a/b.C"]}}})
    assert sc.wanted_settings(profile) == [("secure", "enabled_input_methods")]


@pytest.mark.parametrize("eol", ["\n", "\r\n", "\r\r\n"])
def test_probe_output_is_read_the_same_with_any_line_ending(eol):
    device = compliant(eol=eol)
    keys = sc.wanted_settings(PROFILE)
    out = device.run_script(sc.build_probe(keys, NONCE))
    parsed = sc.parse_probe(out, keys, NONCE)
    assert parsed.settings[("system", "nv_fan_mode")] == "cool"
    assert parsed.settings[("secure", "enabled_accessibility_services")] == "com.example.keep/.Service"
    assert "com.example.keep" in parsed.installed and "com.example.bloat" in parsed.disabled
    assert not any("\r" in v for v in parsed.settings.values())
    assert not any("\r" in p for p in parsed.installed | parsed.disabled)


def test_crlf_device_matching_the_profile_has_no_findings(world):
    world.single()
    world.living.eol = "\r\n"
    assert world.run("--no-notify") == 0


def test_an_empty_setting_value_reads_as_the_empty_string():
    keys = [("system", "x")]
    device = FakeDevice(installed=("android",), settings={("system", "x"): ""})
    parsed = sc.parse_probe(device.run_script(sc.build_probe(keys, NONCE)), keys, NONCE)
    assert parsed.settings[("system", "x")] == ""


PACKAGES_OK = [mark("packages"), "package:android", mark("rc 0"), mark("disabled"), mark("rc 0")]


def _status_probe(status):
    return [mark("packages"), "package:android", mark("rc 0"), mark("disabled"), mark(status), mark("end")]


@pytest.mark.parametrize("lines, keys, why", [
    (PACKAGES_OK, [], "end marker"),
    ([mark("packages"), mark("rc 0"), mark("disabled"), mark("rc 0"), mark("end")], [], "no packages"),
    (["garbage"], [], "end marker"),
    (PACKAGES_OK + [mark("end")], [("system", "x")], "settings get system x"),
    # a non-zero exit status is an error, not an empty result
    (_status_probe("rc 1"), [], "`pm list packages -d` exited 1"),
    (_status_probe("rc 127"), [], "`pm list packages -d` exited 127"),
    (_status_probe("rc 255"), [], "`pm list packages -d` exited 255"),
    ([mark("packages"), "package:android", mark("disabled"), mark("rc 0"), mark("end")], [],
     "no exit status for `pm list packages`"),
    ([mark("rc 0"), mark("end")], [], "without a command"),
    ([mark("packages"), mark("bogus"), mark("end")], [], "unexpected marker"),
    # cut-short output ending in a lookalike end line still raises
    (PACKAGES_OK + ["@@end"], [], "cut short"),
    (PACKAGES_OK + ["@@" + "0" * 16 + ":end"], [], "cut short"),
    (PACKAGES_OK + ["@@" + NONCE + "end"], [], "cut short"),
    (PACKAGES_OK + ["end"], [], "cut short"),
], ids=["no-end-marker", "no-packages", "garbage", "missing-setting", "rc-1", "rc-127", "rc-255",
        "command-without-status-line", "status-line-without-command", "unexpected-marker",
        "lookalike-@@end", "lookalike-wrong-nonce", "lookalike-nonce-glued-to-end",
        "lookalike-bare-end"])
def test_incomplete_or_malformed_probe_output_is_an_error(lines, keys, why):
    with pytest.raises(sc.ProbeError) as exc:
        sc.parse_probe(probe_lines(*lines), keys, NONCE)
    assert why in str(exc.value)


@pytest.mark.parametrize("value", [
    "@@odd", "@@end", "@@packages", "@@disabled", "@@rc 0", "@@s:system:nv_fan_mode",
    "@@" + NONCE[:8], "@@" + NONCE + "x",
])
def test_device_data_that_looks_like_a_marker_is_just_data(value):
    device = compliant()
    device.settings[("system", "nv_fan_mode")] = value
    keys = sc.wanted_settings(PROFILE)
    parsed = sc.parse_probe(device.run_script(sc.build_probe(keys, NONCE)), keys, NONCE)
    assert parsed.settings[("system", "nv_fan_mode")] == value
    assert parsed.settings[("secure", "screensaver_enabled")] == "0"      # its neighbours are intact
    assert "com.example.keep" in parsed.installed


def test_a_device_whose_output_ends_in_a_forged_end_line_is_unreachable():
    device = compliant(post=lambda out: out[:-1] + ["@@end"])
    report = sc.check_device(DEVICE, PROFILE, FakeAdb({LIVING: device}))
    assert not report.reachable and "cut short" in report.detail


# ===========================================================================
# check_device: unauthorized, unreachable, read-only
# ===========================================================================

DEVICE = sc.Device("living", "192.0.2.10", 5555)


def test_compliant_device_is_reached_with_connect_devices_and_one_shell():
    adb = FakeAdb({LIVING: compliant()})
    report = sc.check_device(DEVICE, PROFILE, adb)
    assert report.reachable and report.findings == [] and report.clean
    assert [c[0][0] for c in adb.calls] == ["connect", "devices", "-s"]
    assert adb.calls[0][0] == ["connect", LIVING]
    assert adb.calls[2][0][:3] == ["-s", LIVING, "shell"]


def test_every_adb_call_has_a_finite_positive_timeout():
    adb = FakeAdb({LIVING: compliant()})
    sc.check_device(DEVICE, PROFILE, adb)
    timeouts = [t for _, t in adb.calls]
    assert timeouts[0] == 20.0
    assert all(isinstance(t, float) and 0 < t < 600 for t in timeouts)


def test_unauthorized_is_a_finding_and_nothing_is_read():
    adb = FakeAdb({LIVING: compliant(state="unauthorized")})
    report = sc.check_device(DEVICE, PROFILE, adb)
    assert report.reachable and not report.clean
    assert texts(report.findings) == ["unauthorized: accept the debugging prompt on the TV"]
    assert [c[0][0] for c in adb.calls] == ["connect", "devices"]


@pytest.mark.parametrize("make, why", [
    (lambda: FakeDevice(connect="timeout"), "timed out"),
    (lambda: FakeDevice(connect="refused"), "not listed"),
    (lambda: FakeDevice(state="offline"), "offline"),
    (lambda: compliant(shell_rc=1), "adb shell failed"),
    (lambda: compliant(truncate=True), "cut short"),
    (lambda: FakeDevice(), "no packages"),
])
def test_anything_else_is_unreachable(make, why):
    adb = FakeAdb({LIVING: make()})
    report = sc.check_device(DEVICE, PROFILE, adb)
    assert not report.reachable and report.findings == [] and not report.clean
    assert why in report.detail


@pytest.mark.parametrize("statement", [
    "pm list packages", "pm list packages -d",
    "settings get system nv_fan_mode", "settings get secure enabled_input_methods",
])
def test_a_failing_command_makes_the_device_unreachable_instead_of_reading_as_empty(statement):
    adb = FakeAdb({LIVING: compliant(failing=[statement])})
    report = sc.check_device(DEVICE, PROFILE, adb)
    assert not report.reachable and report.findings == []
    assert f"`{statement}` exited 1" in report.detail


def _drop_status_line(n):
    """Post-processor: remove the n-th `rc` line, as a shell that lost it would."""
    def post(out):
        seen, kept = -1, []
        for line in out:
            if re.fullmatch(r"@@[0-9a-f]{16}:rc [0-9]+", line):
                seen += 1
                if seen == n:
                    continue
            kept.append(line)
        return kept
    return post


@pytest.mark.parametrize("n", [0, 1, 2, 5, len(sc.wanted_settings(PROFILE)) + 1],
                         ids=["first", "second", "third", "sixth", "last"])
def test_a_missing_status_line_makes_the_device_unreachable(n):
    adb = FakeAdb({LIVING: compliant(post=_drop_status_line(n))})
    report = sc.check_device(DEVICE, PROFILE, adb)
    assert not report.reachable and "no exit status" in report.detail


def test_a_failing_probe_counts_as_unreachable_and_keeps_the_stored_findings(world):
    world.single()
    world.living.enable("com.example.bloat")
    world.run()
    world.living.failing = {"pm list packages -d"}     # would read as "nothing disabled"
    assert world.run() == 1
    assert world.state()["living"] == {"findings": [BLOAT_ENABLED], "unreachable": 1}
    assert len(world.messages) == 1


def test_adb_binary_missing_makes_the_device_unreachable():
    def broken(args, timeout):
        raise sc.AdbError("cannot run adb: FileNotFoundError")
    report = sc.check_device(DEVICE, PROFILE, broken)
    assert not report.reachable and "cannot run adb" in report.detail


def test_devices_listing_failure_is_unreachable():
    class Bad(FakeAdb):
        def __call__(self, args, timeout):
            if args[0] == "devices":
                return sc.AdbResult(1, "", "adb server is down")
            return super().__call__(args, timeout)
    report = sc.check_device(DEVICE, PROFILE, Bad({LIVING: compliant()}))
    assert not report.reachable and "adb devices failed" in report.detail


def test_only_read_only_adb_commands_are_ever_run(world, capsys):
    """With drift in every category, every recorded adb call is connect, devices or an
    allowed read-only shell script."""
    world.single()
    world.living.enable("com.example.bloat")
    world.living.installed.add("com.example.banned")
    world.living.settings[("system", "nv_fan_mode")] = "quiet"
    world.living.settings[("secure", "enabled_input_methods")] = "null"
    world.living.disabled.add("com.example.voice")
    assert world.run() == 1
    assert world.adb.calls
    for args, _timeout in world.adb.calls:
        if args[0] in ("connect", "devices"):
            continue
        assert args[0] == "-s" and args[2] == "shell" and len(args) == 4
        for stmt in args[3].split("; "):
            assert ALLOWED_STATEMENT.fullmatch(stmt), stmt
    # the hints name mutating commands, but only in the log, as text
    assert "HINT" in capsys.readouterr().out


# ===========================================================================
# The state machine, through main()
# ===========================================================================

def test_first_run_clean_device_says_nothing_but_records_itself(world):
    world.single()
    assert world.run() == 0
    assert world.messages == []
    assert world.state() == {"living": {"findings": [], "unreachable": 0}}


def test_first_run_with_findings_alerts_and_records_them(world):
    world.single()
    world.living.enable("com.example.bloat")
    assert world.run() == 1
    assert len(world.messages) == 1
    assert "living" in world.messages[0] and BLOAT_ENABLED in world.messages[0]
    assert world.state() == {"living": {"findings": [BLOAT_ENABLED], "unreachable": 0}}


def test_unchanged_findings_do_not_alert_again(world):
    world.single()
    world.living.enable("com.example.bloat")
    world.run()
    assert world.run() == 1          # still drifted, so the exit status says so
    assert world.run() == 1
    assert len(world.messages) == 1


def test_a_new_finding_alerts_with_the_whole_current_list(world):
    world.single()
    world.living.enable("com.example.bloat")
    world.run()
    world.living.installed.add("com.example.banned")
    world.run()
    assert len(world.messages) == 2
    assert BLOAT_ENABLED in world.messages[1] and BANNED_INSTALLED in world.messages[1]
    assert "2 finding(s)" in world.messages[1]
    assert world.state()["living"]["findings"] == sorted([BLOAT_ENABLED, BANNED_INSTALLED])


def test_a_resolved_finding_alerts_with_what_is_left(world):
    world.single()
    world.living.enable("com.example.bloat")
    world.living.installed.add("com.example.banned")
    world.run()
    world.living.installed.discard("com.example.banned")
    world.run()
    assert len(world.messages) == 2
    assert BLOAT_ENABLED in world.messages[1] and BANNED_INSTALLED not in world.messages[1]


def test_everything_resolved_says_back_in_line_with_the_profile(world):
    world.single()
    world.living.enable("com.example.bloat")
    world.run()
    world.living.disabled.add("com.example.bloat")
    assert world.run() == 0
    assert len(world.messages) == 2
    assert "living: back in line with the profile" in world.messages[1]
    assert world.state()["living"]["findings"] == []


def test_a_changed_finding_text_for_the_same_key_alerts(world):
    world.single()
    world.living.settings[("system", "nv_fan_mode")] = "quiet"
    world.run()
    world.living.settings[("system", "nv_fan_mode")] = "off"
    world.run()
    assert len(world.messages) == 2 and "got 'off'" in world.messages[1]


def test_unauthorized_alerts_once_and_resolves(world):
    world.single()
    world.living.state = "unauthorized"
    assert world.run() == 1
    assert world.run() == 1
    assert len(world.messages) == 1 and "unauthorized" in world.messages[0]
    world.living.state = "device"
    assert world.run() == 0
    assert "back in line" in world.messages[-1]


def test_unreachable_alerts_on_the_third_daily_run_only(world):
    world.single()
    world.living.connect = "timeout"
    for count in (1, 2):
        assert world.run() == 1
        assert world.messages == []
        assert world.state()["living"]["unreachable"] == count
    assert world.run() == 1
    assert len(world.messages) == 1
    assert "living: unreachable for 3 consecutive daily runs" in world.messages[0]
    assert world.state()["living"]["unreachable"] == 3
    for count in (4, 5):
        assert world.run() == 1
        assert len(world.messages) == 1
        assert world.state()["living"]["unreachable"] == count


def test_recovery_after_an_unreachable_alert_alerts_once_and_resets_the_counter(world):
    world.single()
    world.living.connect = "timeout"
    for _ in range(4):
        world.run()
    world.living.connect = "ok"
    assert world.run() == 0
    assert len(world.messages) == 2
    assert "living: reachable again" in world.messages[1]
    assert world.state()["living"] == {"findings": [], "unreachable": 0}
    assert world.run() == 0
    assert len(world.messages) == 2


def test_recovery_before_the_alert_threshold_is_silent(world):
    world.single()
    world.living.connect = "timeout"
    world.run()
    world.run()
    world.living.connect = "ok"
    assert world.run() == 0
    assert world.messages == []
    assert world.state()["living"]["unreachable"] == 0


def test_unreachable_runs_leave_the_stored_findings_alone(world):
    world.single()
    world.living.enable("com.example.bloat")
    world.run()
    world.living.connect = "timeout"
    for _ in range(3):
        world.run()
        assert world.state()["living"]["findings"] == [BLOAT_ENABLED]
    world.living.connect = "ok"
    world.run()
    # back with the SAME findings: only the recovery is news
    assert len(world.messages) == 3
    assert "reachable again" in world.messages[2]
    assert BLOAT_ENABLED not in world.messages[2]


def test_recovery_with_changed_findings_reports_both(world):
    world.single()
    world.living.connect = "timeout"
    for _ in range(3):
        world.run()
    world.living.connect = "ok"
    world.living.installed.add("com.example.banned")
    world.run()
    assert "reachable again" in world.messages[1] and BANNED_INSTALLED in world.messages[1]


def test_unreachable_first_run_then_reachable_with_findings_alerts_as_a_first_run(world):
    world.single()
    world.living.connect = "timeout"
    world.run()
    world.living.connect = "ok"
    world.living.enable("com.example.bloat")
    world.run()
    assert len(world.messages) == 1 and BLOAT_ENABLED in world.messages[0]


def test_failed_send_keeps_the_old_state_and_the_next_run_retries(world):
    world.single()
    world.living.enable("com.example.bloat")
    world.sender.ok = False
    assert world.run() == 1
    assert len(world.messages) == 1
    assert not sc.STATE_FILE.exists() or "living" not in world.state()
    world.sender.ok = True
    world.run()
    assert len(world.messages) == 2 and BLOAT_ENABLED in world.messages[1]
    assert world.state()["living"]["findings"] == [BLOAT_ENABLED]
    world.run()
    assert len(world.messages) == 2


def test_failed_send_leaves_an_existing_entry_as_it_was(world):
    world.single()
    world.living.enable("com.example.bloat")
    world.run()
    world.living.installed.add("com.example.banned")
    world.sender.ok = False
    world.run()
    assert world.state()["living"]["findings"] == [BLOAT_ENABLED]
    world.sender.ok = True
    world.run()
    assert world.state()["living"]["findings"] == sorted([BLOAT_ENABLED, BANNED_INSTALLED])


def test_failed_send_of_the_unreachable_alert_is_retried(world):
    world.single()
    world.living.connect = "timeout"
    world.run()
    world.run()
    world.sender.ok = False
    world.run()
    assert world.state()["living"]["unreachable"] == 2     # not advanced to 3
    world.sender.ok = True
    world.run()
    assert len(world.messages) == 2 and "unreachable for 3" in world.messages[1]
    assert world.state()["living"]["unreachable"] == 3


def test_drift_fixed_before_its_alert_was_delivered_is_never_reported(world):
    """Alerts describe the CURRENT condition against the last DELIVERED state; no queue."""
    world.single()
    world.living.enable("com.example.bloat")
    world.sender.ok = False
    world.run()
    assert len(world.messages) == 1                      # attempted, not delivered
    world.living.disabled.add("com.example.bloat")       # fixed before the next run
    world.sender.ok = True
    assert world.run() == 0
    assert len(world.messages) == 1                      # nothing about the cleared drift
    assert world.state()["living"] == {"findings": [], "unreachable": 0}


def test_unreachable_that_recovers_before_its_alert_got_through_is_not_reported(world):
    world.single()
    world.living.connect = "timeout"
    world.run()
    world.run()
    world.sender.ok = False
    world.run()                                          # third run: the alert fails
    assert len(world.messages) == 1
    assert world.state()["living"]["unreachable"] == 2
    world.living.connect = "ok"
    world.sender.ok = True
    assert world.run() == 0
    assert len(world.messages) == 1                      # neither the outage nor the recovery
    assert world.state()["living"]["unreachable"] == 0


def test_a_device_that_needs_no_alert_advances_even_when_the_send_fails(world):
    world.devices(f"living={LIVING} bedroom={BEDROOM}")
    world.living.connect = "timeout"                       # unreachable day 1: no alert
    world.bedroom.enable("com.example.bloat")       # first-run finding: alert
    world.sender.ok = False
    world.run()
    state = world.state()
    assert state["living"] == {"findings": [], "unreachable": 1}
    assert "bedroom" not in state


def test_missing_telegram_credentials_log_and_continue(world, monkeypatch, capsys):
    world.single()
    world.living.enable("com.example.bloat")
    monkeypatch.setattr(sc, "telegram_send", Mock(side_effect=AssertionError("must not send")))
    assert sc.main([], adb=world.adb) == 1        # the real send_alert, no credentials anywhere
    assert "notify skipped: no Telegram credentials" in capsys.readouterr().out
    assert not sc.STATE_FILE.exists()             # the alert was not delivered, so no state


def test_one_message_per_run_covers_every_device(world):
    world.living.enable("com.example.bloat")
    world.bedroom.installed.add("com.example.banned")
    assert world.run() == 1
    assert len(world.messages) == 1
    message = world.messages[0]
    assert "2 device(s)" in message
    assert "living: 1 finding(s)" in message and "bedroom: 1 finding(s)" in message
    assert BLOAT_ENABLED in message and BANNED_INSTALLED in message


def test_a_clean_device_is_not_mentioned_next_to_a_drifted_one(world):
    world.living.enable("com.example.bloat")
    world.run()
    assert "bedroom" not in world.messages[0]


def test_telegram_message_has_no_addresses_and_no_hints_but_points_at_the_log(world):
    world.living.enable("com.example.bloat")
    world.bedroom.connect = "timeout"
    for _ in range(3):
        world.run()
    message = "\n".join(world.messages)
    assert not re.search(r"\d+\.\d+\.\d+\.\d+", message) and ":5555" not in message
    assert "adb -s" not in message and "pm disable-user" not in message
    assert "var/log/shield-check.log" in message


def test_exit_status_is_1_for_drift_and_for_unreachable_but_not_for_a_failed_send(world):
    world.single()
    assert world.run() == 0
    world.living.connect = "timeout"
    assert world.run() == 1
    world.living.connect = "ok"
    world.living.enable("com.example.bloat")
    world.sender.ok = False
    assert world.run() == 1


# --- --device and --no-notify ------------------------------------------------

def test_device_flag_checks_only_that_device_and_keeps_the_others_state(world):
    world.bedroom.enable("com.example.bloat")
    world.run()                                   # both recorded; bedroom has a finding
    before = world.state()
    world.living.installed.add("com.example.banned")
    world.bedroom.enable("com.example.junk")        # would change bedroom if it were checked
    world.adb.calls.clear()
    assert world.run("--device", "living") == 1
    assert world.adb.serials_touched() == {LIVING}
    state = world.state()
    assert state["bedroom"] == before["bedroom"]
    assert state["living"]["findings"] == [BANNED_INSTALLED]
    assert len(world.messages) == 2 and "bedroom" not in world.messages[1]


def test_device_flag_does_not_prune_other_entries_but_a_full_run_does(world):
    world.single()
    sc.STATE_FILE.parent.mkdir(parents=True)
    sc.STATE_FILE.write_text(json.dumps({
        "retired": {"findings": [], "unreachable": 0},
        "living": {"findings": [], "unreachable": 0}}))
    world.run("--device", "living")
    assert "retired" in world.state()
    world.run()
    assert set(world.state()) == {"living"}


def test_unknown_device_flag_exits_2(world, capsys):
    assert world.run("--device", "kitchen") == 2
    assert world.adb.calls == [] and world.messages == [] and not sc.STATE_FILE.exists()
    assert "kitchen" in capsys.readouterr().out


def test_no_notify_prints_findings_and_hints_and_writes_nothing(world, capsys):
    world.single()
    world.living.enable("com.example.bloat")
    assert world.run("--no-notify") == 1
    out = capsys.readouterr().out
    assert BLOAT_ENABLED in out
    assert f"HINT adb -s {LIVING} shell pm disable-user --user 0 com.example.bloat" in out
    assert world.messages == []
    assert not sc.STATE_FILE.exists()


def test_no_notify_leaves_an_existing_state_file_byte_for_byte(world):
    world.single()
    sc.STATE_FILE.parent.mkdir(parents=True)
    sc.STATE_FILE.write_text('{"living": {"findings": [], "unreachable": 2}}')
    world.living.enable("com.example.bloat")
    world.run("--no-notify")
    assert sc.STATE_FILE.read_text() == '{"living": {"findings": [], "unreachable": 2}}'


def test_no_notify_does_not_need_the_run_lock(world):
    world.single()
    sc.LOCK_FILE.parent.mkdir(exist_ok=True)
    with open(sc.LOCK_FILE, "w") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        assert world.run("--no-notify") == 0


def test_a_second_run_holding_the_lock_checks_nothing(world, capsys):
    world.single()
    with open(sc.LOCK_FILE, "w") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        assert world.run() == sc.EXIT_BUSY
    assert world.adb.calls == [] and world.messages == []
    assert "holds the lock" in capsys.readouterr().out


def test_a_clean_report_logs_ok_and_prints_no_hint(world, capsys):
    world.single()
    world.run("--no-notify")
    out = capsys.readouterr().out
    assert "[living] OK: matches the profile" in out and "HINT" not in out


def test_hints_are_printed_once_per_distinct_command(world, capsys):
    world.single()
    world.living.settings[("secure", "enabled_input_methods")] = "other/.Ime"
    profile = json.loads(sc.PROFILE_PATH.read_text())
    profile["component_lists"]["secure"]["enabled_input_methods"] = ["a.b/.One", "a.b/.Two"]
    sc.PROFILE_PATH.write_text(json.dumps(profile))
    world.run("--no-notify")
    out = capsys.readouterr().out
    assert out.count("missing a.b/") == 2
    assert out.count("HINT") == 1


# --- state validation ----------------------------------------------------------

def test_corrupt_state_is_logged_treated_as_empty_and_repaired(world, capsys):
    world.single()
    sc.STATE_FILE.parent.mkdir(parents=True)
    sc.STATE_FILE.write_text("{this is not json")
    world.living.enable("com.example.bloat")
    assert world.run() == 1
    assert "treating it as empty" in capsys.readouterr().out
    assert len(world.messages) == 1               # first-run behaviour
    assert world.state() == {"living": {"findings": [BLOAT_ENABLED], "unreachable": 0}}


@pytest.mark.parametrize("content", [
    "[]", '"x"', "null", "5",
    '{"living": []}',
    '{"living": {"findings": "x", "unreachable": 0}}',
    '{"living": {"findings": [1], "unreachable": 0}}',
    '{"living": {"findings": [], "unreachable": -1}}',
    '{"living": {"findings": [], "unreachable": true}}',
    '{"living": {"findings": [], "unreachable": 1.5}}',
    '{"living": {"findings": []}}',
    '{"Bad Name": {"findings": [], "unreachable": 0}}',
])
def test_invalid_state_never_crashes_and_counts_as_no_state(world, content):
    world.single()
    sc.STATE_FILE.parent.mkdir(parents=True)
    sc.STATE_FILE.write_text(content)
    world.living.enable("com.example.bloat")
    assert world.run() == 1
    assert len(world.messages) == 1 and BLOAT_ENABLED in world.messages[0]
    assert world.state()["living"]["findings"] == [BLOAT_ENABLED]


def test_valid_entries_survive_next_to_an_invalid_one(world):
    world.single()
    sc.STATE_FILE.parent.mkdir(parents=True)
    sc.STATE_FILE.write_text(json.dumps({
        "living": {"findings": [BLOAT_ENABLED], "unreachable": 0}, "bedroom": 7}))
    world.living.enable("com.example.bloat")
    world.run()
    assert world.messages == []                   # living's stored findings still count


def test_an_unwritable_state_directory_is_logged_not_fatal(world, monkeypatch, capsys):
    world.single()
    world.living.enable("com.example.bloat")
    monkeypatch.setattr(sc, "save_state", Mock(side_effect=PermissionError("read-only")))
    assert world.run() == 1
    assert "could not write" in capsys.readouterr().out


# ===========================================================================
# The Telegram message and sender
# ===========================================================================

def _long(n, tag="finding"):
    return [f"  {tag} number {i} " + "x" * 90 for i in range(n)]


def test_message_is_bounded_and_says_how_much_was_cut():
    message = sc.build_message({"living": ["500 finding(s) vs the profile"] + _long(500)})
    assert len(message) <= sc.MAX_MESSAGE_CHARS
    assert "living: 500 finding(s) vs the profile" in message
    assert re.search(r"^  \.\.\.and [0-9]+ more \(see the log\)$", message, re.M)
    assert message.endswith(sc.LOG_POINTER)


def test_short_message_is_complete():
    message = sc.build_message({"living": ["back in line with the profile"],
                                "bedroom": ["unreachable for 3 consecutive daily runs"]})
    assert message.splitlines()[0] == "Shield check: 2 device(s) changed"
    assert "living: back in line with the profile" in message
    assert "bedroom: unreachable for 3 consecutive daily runs" in message
    assert "more (see the log)" not in message


def test_a_long_list_cannot_push_another_device_out_of_the_message():
    alerts = {
        "living": ["500 finding(s) vs the profile"] + _long(500),
        "bedroom": ["5 finding(s) vs the profile"] + [f"  small {i}" for i in range(5)],
        "den": ["unreachable for 3 consecutive daily runs"],
        "attic": ["reachable again", "2 finding(s) vs the profile", "  one", "  two"],
    }
    message = sc.build_message(alerts)
    assert len(message) <= sc.MAX_MESSAGE_CHARS
    for header in ("living: 500 finding(s) vs the profile", "bedroom: 5 finding(s) vs the profile",
                   "den: unreachable for 3 consecutive daily runs", "attic: reachable again",
                   "attic: 2 finding(s) vs the profile"):
        assert header in message
    # the short lists keep every line; only the long one lost some
    assert "  small 4" in message and "  two" in message
    assert message.count("more (see the log)") == 1


def test_every_device_with_a_long_list_keeps_its_header_and_a_share_of_the_room():
    alerts = {name: ["300 finding(s) vs the profile"] + _long(300, name)
              for name in ("living", "bedroom", "den")}
    message = sc.build_message(alerts)
    assert len(message) <= sc.MAX_MESSAGE_CHARS
    for name in alerts:
        assert f"{name}: 300 finding(s) vs the profile" in message
        assert f"  {name} number 0 " in message          # each got some detail lines
    assert message.count("more (see the log)") == 3


def test_header_lines_are_kept_even_when_the_budget_is_tiny(world, monkeypatch):
    """Both devices' state advances once their alert is sent, so both must be named in it."""
    monkeypatch.setattr(sc, "MAX_MESSAGE_CHARS", 420)
    world.living.enable("com.example.bloat")
    world.living.installed.add("com.example.banned")
    world.living.settings[("system", "nv_fan_mode")] = "quiet"
    world.bedroom.installed.add("com.example.banned")
    assert world.run() == 1
    message = world.messages[0]
    assert len(message) <= 420
    assert "living: 3 finding(s) vs the profile" in message
    assert "bedroom: 1 finding(s) vs the profile" in message
    assert "more (see the log)" in message
    assert set(world.state()) == {"living", "bedroom"}


def _creds(monkeypatch, token="123:secret-token", chat="42"):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", chat)


def test_send_alert_uses_the_environment_credentials(monkeypatch):
    _creds(monkeypatch)
    send = Mock(return_value=True)
    monkeypatch.setattr(sc, "telegram_send", send)
    assert sc.send_alert("hello") is True
    send.assert_called_once_with("123:secret-token", "42", "hello")


def test_send_alert_fills_the_gaps_from_the_plugin_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / "telegram.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=tok\nTELEGRAM_CHAT_ID=99\n")
    monkeypatch.setenv("TELEGRAM_ENV", str(env_file))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "7")           # the environment still wins
    send = Mock(return_value=True)
    monkeypatch.setattr(sc, "telegram_send", send)
    assert sc.send_alert("hi") is True
    send.assert_called_once_with("tok", "7", "hi")


def test_send_alert_without_credentials_does_not_send(monkeypatch, capsys):
    send = Mock()
    monkeypatch.setattr(sc, "telegram_send", send)
    assert sc.send_alert("hello") is False
    send.assert_not_called()
    assert "no Telegram credentials" in capsys.readouterr().out


def test_send_alert_failures_return_false_and_never_log_the_token(monkeypatch, capsys):
    _creds(monkeypatch)
    monkeypatch.setattr(sc, "telegram_send", Mock(return_value=False))
    assert sc.send_alert("x") is False
    monkeypatch.setattr(sc, "telegram_send", Mock(side_effect=OSError("boom secret-token")))
    assert sc.send_alert("x") is False
    out = capsys.readouterr().out
    assert "secret-token" not in out and "notify failed" in out and "OSError" in out


# ===========================================================================
# The real adb runner (subprocess mocked: no adb is ever started)
# ===========================================================================

def test_adb_runner_builds_the_argv_and_bounds_the_call(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess(["adb"], 0, "out", "err"))
    monkeypatch.setattr(sc.subprocess, "run", run)
    result = sc.make_adb_runner("/opt/adb")(["devices"], 10.0)
    assert (result.rc, result.out, result.err) == (0, "out", "err")
    assert run.call_args.args[0] == ["/opt/adb", "devices"]
    assert run.call_args.kwargs["timeout"] == 10.0
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL


def test_adb_runner_turns_a_timeout_or_a_missing_binary_into_adb_error(monkeypatch):
    runner = sc.make_adb_runner("adb")
    monkeypatch.setattr(sc.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("adb", 5)))
    with pytest.raises(sc.AdbError, match="timed out"):
        runner(["connect", LIVING], 5.0)
    monkeypatch.setattr(sc.subprocess, "run", Mock(side_effect=FileNotFoundError("adb")))
    with pytest.raises(sc.AdbError, match="cannot run"):
        runner(["devices"], 5.0)


@pytest.mark.parametrize("env, expected", [(None, "adb"), ("/opt/adb", "/opt/adb")])
def test_adb_binary_comes_from_the_adb_env_var(world, monkeypatch, env, expected):
    world.single()
    if env:
        monkeypatch.setenv("ADB", env)
    made = Mock(return_value=world.adb)
    monkeypatch.setattr(sc, "make_adb_runner", made)
    assert sc.main(["--no-notify"]) == 0
    made.assert_called_once_with(expected)


# ===========================================================================
# What ships in the repo
# ===========================================================================

DOC_RANGES = ("192.0.2.", "198.51.100.", "203.0.113.")
IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")


@pytest.mark.parametrize("relpath", [
    "shield-check.py", "tests/test_shield_check.py", "ops/shield/profile.json"])
def test_new_files_contain_only_documentation_addresses(relpath):
    for address in IPV4.findall((PROJECT_ROOT / relpath).read_text()):
        assert address.startswith(DOC_RANGES), f"{relpath}: {address}"


def test_env_example_documents_shield_devices_with_documentation_addresses():
    lines = (PROJECT_ROOT / ".env.example").read_text().splitlines()
    example = [ln for ln in lines if "SHIELD_DEVICES=" in ln and ln.startswith("# SHIELD_DEVICES=")]
    assert len(example) == 1
    devices, problems = sc.parse_devices(example[0].split("=", 1)[1].strip('" '))
    assert problems == [] and len(devices) == 2
    assert all(d.host.startswith(DOC_RANGES) for d in devices)
    assert not [ln for ln in lines if ln.startswith("SHIELD_DEVICES=")]


def test_crontab_runs_the_check_once_a_day_with_its_own_log():
    lines = [ln for ln in (PROJECT_ROOT / "ops/cron.d/media-stack.crontab").read_text().splitlines()
             if ln.strip() and not ln.startswith("#") and "shield-check.py" in ln]
    assert lines == ["15 12 * * * python3 $HOME/media-stack/shield-check.py "
                     ">> $HOME/media-stack/var/log/shield-check.log 2>&1"]


def test_script_is_executable_with_a_shebang():
    path = PROJECT_ROOT / "shield-check.py"
    assert path.read_text().startswith("#!/usr/bin/env python3\n")
    assert path.stat().st_mode & 0o100
