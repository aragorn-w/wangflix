"""Tests for media_stack/health.py: every probe's FAIL/WARN/OK branch, print routing,
--json shape and exit codes, with subprocess/HTTP/clients mocked."""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from media_stack import health


def _hc(tmp_path, **kw):
    hc = health.HealthCheck(repo_root=tmp_path, **kw)
    hc.env = {}
    return hc


def _assert_results(hc, expected):
    """Each expected entry is (state, check, detail) or (state, check); a 2-tuple ignores the detail."""
    assert [r[:len(e)] for r, e in zip(hc.results, expected)] == expected
    assert len(hc.results) == len(expected)


# ---------------- result recorders / routing ----------------

@pytest.mark.parametrize("kwargs, call, out, err, results", [
    ({}, lambda hc: hc.ok("thing running"), "", "", [("OK", "thing running", "")]),
    ({}, lambda hc: hc.fail("widget", "broke"), "", "FAIL: widget — broke\n",
     [("FAIL", "widget", "broke")]),
    ({"json_mode": True}, lambda hc: (hc.warn("a", "b"), hc.fail("c", "d")), "", "",
     [("WARN", "a", "b"), ("FAIL", "c", "d")]),
], ids=["ok-silent-in-plain-mode", "fail-to-stderr", "json-suppresses-warn-and-fail-inline"])
def test_recorders_print_routing(tmp_path, capsys, kwargs, call, out, err, results):
    hc = _hc(tmp_path, **kwargs)
    call(hc)
    cap = capsys.readouterr()
    assert (cap.out, cap.err) == (out, err)
    assert hc.results == results


# ---------------- summary / exit codes ----------------

@pytest.mark.parametrize("results, rc, line", [
    ([("OK", "a", ""), ("OK", "b", "")], 0, "HEALTH OK: 2 / 2 checks passed"),
    ([("OK", "a", ""), ("WARN", "b", "x")], 2, "HEALTH WARN: 1 warn / 1 ok"),
    ([("OK", "a", ""), ("WARN", "b", "x"), ("FAIL", "c", "y")], 1,
     "HEALTH FAIL: 1 fail / 1 warn / 1 ok"),
], ids=["all-ok-exit0", "warn-only-exit2", "any-fail-exit1"])
def test_summary_exit_code_and_line(tmp_path, capsys, results, rc, line):
    hc = _hc(tmp_path)
    hc.results = results
    assert hc.summary() == rc
    assert line in capsys.readouterr().out


def test_summary_json_shape_and_no_health_line(tmp_path, capsys):
    hc = _hc(tmp_path, json_mode=True)
    hc.results = [("OK", "gluetun-egress ip=1.2.3.4", ""), ("WARN", "w", "detail")]
    rc = hc.summary()
    cap = capsys.readouterr()
    assert rc == 2
    assert "HEALTH" not in cap.out  # json mode prints no HEALTH summary line
    obj = json.loads(cap.out)
    assert obj == {
        "pass": 1, "warn": 1, "fail": 0,
        "results": [
            {"state": "OK", "check": "gluetun-egress ip=1.2.3.4", "detail": ""},
            {"state": "WARN", "check": "w", "detail": "detail"},
        ],
    }


# ---------------- gluetun egress ----------------

def _gluetun_run(vpn_ip, host_ip, ipinfo=None, ip2=None):
    """Fake `_run`: gluetun egresses via `vpn_ip`, geo lookups echo it unless given, host egress is `host_ip` (None: fails)."""
    def fake_run(args, **kw):
        if "ps" in args:
            return (0, "gluetun\n", "")
        if args[:3] == ["docker", "exec", "gluetun"]:
            url = args[-1]
            if "ifconfig.co" in url:
                return (0, f"{vpn_ip}\n", "")
            if "ipinfo.io" in url:
                return (0, f"{vpn_ip}\n" if ipinfo is None else ipinfo, "")
            if "ip2location" in url:
                return (0, f"{vpn_ip}\n" if ip2 is None else ip2, "")
        if args[0] == "curl" and host_ip:
            return (0, f"{host_ip}\n", "")
        return (1, "", "")
    return fake_run


def test_gluetun_not_running_fails(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health, "_run", return_value=(1, "", "")):
        hc.probe_gluetun()
    assert hc.results == [("FAIL", "gluetun", "gluetun container not running")]


def test_gluetun_ok(tmp_path):
    hc = _hc(tmp_path)
    fake = _gluetun_run("146.70.226.218", "8.8.8.8", "CH\n", '{"country_code": "CH"}')  # host != vpn ip
    with patch.object(health, "_run", side_effect=fake):
        hc.probe_gluetun()
    assert hc.results == [("OK", "gluetun-egress ip=146.70.226.218 country=Switzerland", "")]
    assert hc.vpn_country == "Switzerland"


@pytest.mark.parametrize("host_ip, state, frag", [
    ("5.5.5.5", "FAIL", "LEAKING"),     # host egress == vpn ip
    (None, "WARN", "leak check skipped"),   # host egress unavailable
], ids=["leak-fails", "no-host-ip-warns"])
def test_gluetun_host_egress_check(tmp_path, host_ip, state, frag):
    hc = _hc(tmp_path)
    with patch.object(health, "_run", side_effect=_gluetun_run("5.5.5.5", host_ip)):
        hc.probe_gluetun()
    assert hc.results[0][0] == state
    assert frag in hc.results[0][2]


def test_gluetun_single_geo_source_still_verifies_country(tmp_path):
    # ipinfo rate-limited (empty) but ip2location confirms CH: must not fail the country check.
    hc = _hc(tmp_path)
    fake = _gluetun_run("146.70.226.218", "8.8.8.8", "\n", '{"country_code": "CH"}')
    with patch.object(health, "_run", side_effect=fake):
        hc.probe_gluetun()
    assert hc.vpn_country == "Switzerland"   # single source resolved, not a debug string
    hc.probe_vpn_country()                   # uses the real consensus/match helpers
    vc = [r for r in hc.results if r[1].startswith("vpn-country")]
    assert vc and vc[0][0] == "OK"           # was a false FAIL before the fix


# ---------------- containers ----------------

@pytest.mark.parametrize("name, required, cid, status, health_state, expected", [
    ("radarr", True, "", "", "", ("FAIL", "container:radarr", "not running (no container id)")),
    ("watchtower", False, "", "", "", ("WARN", "container:watchtower", "optional, not running")),
    ("sonarr", True, "abc123", "running", "", ("OK", "container:sonarr running", "")),
    ("jellyfin", True, "abc", "running", "unhealthy",
     ("FAIL", "container:jellyfin", "running but health=unhealthy")),
], ids=["required-missing-fails", "optional-missing-warns", "running-no-health", "unhealthy-fails"])
def test_check_container(tmp_path, name, required, cid, status, health_state, expected):
    hc = _hc(tmp_path)

    def fake_run(args, **kw):
        if "-q" in args:
            return (0, f"{cid}\n" if cid else "", "")
        if "{{.State.Status}}" in args:
            return (0, f"{status}\n", "")
        return (0, f"{health_state}\n", "")

    with patch.object(health, "_run", side_effect=fake_run):
        hc.check_container(name, required)
    assert hc.results == [expected]


# ---------------- Arr API reachability ----------------

def test_probe_arr_missing_key_warns(tmp_path):
    hc = _hc(tmp_path)
    hc.probe_arr("radarr", "http://x:7878", "RADARR_API_KEY")
    assert hc.results == [("WARN", "api:radarr",
                           "RADARR_API_KEY missing from .env — reachability not checked")]


@pytest.mark.parametrize("status, expected", [
    ("200", ("OK", "api:radarr reachable", "")),
    ("500", ("FAIL", "api:radarr", "http://x:7878/api/v3/system/status returned 500")),
], ids=["200-ok", "non-200-fails-with-status"])
def test_probe_arr_status(tmp_path, status, expected):
    hc = _hc(tmp_path)
    hc.env = {"RADARR_API_KEY": "k"}
    client = MagicMock()
    client.reachable_status.return_value = status
    with patch.object(health, "ArrClient", return_value=client):
        hc.probe_arr("radarr", "http://x:7878", "RADARR_API_KEY")
    assert hc.results == [expected]


# ---------------- Arr policy probes ----------------

@pytest.mark.parametrize("probe, client_method, val, state, frag", [
    ("probe_arr_hardlinks", "hardlinks_enabled", True, "OK", "hardlinks=enabled"),
    ("probe_arr_hardlinks", "hardlinks_enabled", False, "FAIL", "hardlinks=DISABLED"),
    ("probe_arr_hardlinks", "hardlinks_enabled", None, "WARN",
     "couldn't read /config/mediamanagement"),
    ("probe_arr_format_scores", "format_score_violations", [], "OK", "AV1+12-bit=-10000"),
    ("probe_arr_silent_gap", "unmonitored_no_file_count", 0, "OK", "silent-gap=0"),
    ("probe_arr_silent_gap", "unmonitored_no_file_count", 7, "FAIL", "silent-gap=7"),
], ids=["hardlinks-enabled", "hardlinks-disabled", "hardlinks-unreadable",
        "format-scores-clean", "silent-gap-zero", "silent-gap-nonzero"])
def test_arr_policy_probe(tmp_path, probe, client_method, val, state, frag):
    hc = _hc(tmp_path)
    hc.env = {"RADARR_API_KEY": "k"}
    client = MagicMock()
    getattr(client, client_method).return_value = val
    with patch.object(health, "ArrClient", return_value=client):
        getattr(hc, probe)("radarr", "http://x", "RADARR_API_KEY")
    assert hc.results[0][0] == state
    assert frag in hc.results[0][1] + hc.results[0][2]


def test_probe_format_scores_violations_fail_json(tmp_path):
    hc = _hc(tmp_path)
    hc.env = {"RADARR_API_KEY": "k"}
    client = MagicMock()
    client.format_score_violations.return_value = [{"profile": "Shield", "format": "AV1"}]
    with patch.object(health, "ArrClient", return_value=client):
        hc.probe_arr_format_scores("radarr", "http://x", "RADARR_API_KEY")
    assert hc.results[0][0] == "FAIL"
    assert hc.results[0][2] == 'violations: [{"profile": "Shield", "format": "AV1"}]'


# ---------------- Bazarr ----------------

def test_bazarr_api_no_key_warns(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health, "apikey_from_container", return_value=""):
        hc.probe_bazarr_api("http://b:6767")
    assert hc.results == [("WARN", "api:bazarr", "could not read apikey from container config")]


def test_bazarr_api_ok(tmp_path):
    hc = _hc(tmp_path)
    client = MagicMock()
    client.reachable_status.return_value = "200"
    with patch.object(health, "apikey_from_container", return_value="bk"), \
         patch.object(health, "BazarrClient", return_value=client):
        hc.probe_bazarr_api("http://b:6767")
    assert hc.results == [("OK", "api:bazarr reachable", "")]
    assert hc.bazarr_key == "bk"


def _probe_bazarr_profiles(tmp_path, unprofiled, wrong=None, env=None):
    """Run the probe; `unprofiled`/`wrong` are a count tuple (return value) or an Exception (raised)."""
    hc = _hc(tmp_path)
    hc.bazarr_key = "bk"
    hc.env = env or {}
    client = MagicMock()
    for mock, val in ((client.unprofiled_count, unprofiled), (client.wrong_profile_count, wrong)):
        if isinstance(val, Exception):
            mock.side_effect = val
        elif val is not None:
            mock.return_value = val
    with patch.object(health, "BazarrClient", return_value=client):
        hc.probe_bazarr_profiles("http://b")
    return hc, client


def test_bazarr_profiles_clean(tmp_path):
    hc, _ = _probe_bazarr_profiles(tmp_path, (0, 0), (0, 0))
    assert hc.results[0] == ("OK", "bazarr-profile-coverage: 0 unprofiled (movies + series)", "")
    assert hc.results[1][0] == "OK"
    assert "matches expected profileId=1" in hc.results[1][1]


@pytest.mark.parametrize("unprofiled, wrong, idx, frag", [
    ((94, 0), (0, 0), 0, "94 movies + 0 series have profileId=None"),
    ((0, 0), (3, 1), 1, "3 movies + 1 series assigned to a profileId != 1"),
], ids=["unprofiled", "wrong-profile"])
def test_bazarr_profiles_warn(tmp_path, unprofiled, wrong, idx, frag):
    hc, _ = _probe_bazarr_profiles(tmp_path, unprofiled, wrong)
    assert hc.results[idx][0] == "WARN"
    assert frag in hc.results[idx][2]


def test_bazarr_profiles_endpoint_error_is_warn_not_crash(tmp_path):
    hc, _ = _probe_bazarr_profiles(tmp_path, (None, None), (None, None))
    assert hc.results[0][0] == "WARN"
    assert "endpoint parse error" in hc.results[0][2]
    assert hc.results[1][0] == "WARN"
    assert "endpoint error" in hc.results[1][2]


def test_bazarr_profiles_bad_expected_fails(tmp_path):
    hc, client = _probe_bazarr_profiles(tmp_path, (0, 0), env={"BAZARR_DEFAULT_PROFILE_ID": "abc"})
    eq = [r for r in hc.results if "equality" in r[1]]
    assert eq and eq[0][0] == "FAIL"
    assert "must be a non-negative integer, got 'abc'" in eq[0][2]
    client.wrong_profile_count.assert_not_called()


# ---------------- qBittorrent ----------------

@pytest.mark.parametrize("env, login, reach, expected", [
    ({}, None, "200", ("OK", "api:qbit reachable (bypass-auth-on-LAN)", "")),
    ({}, None, "403", ("FAIL", "api:qbit", "/api/v2/torrents/info returned 403 (no auth)")),
    ({"QBIT_USER": "admin", "QBIT_PASS": "pw"}, "Ok.", "200",
     ("OK", "api:qbit auth round-trip + cookie-verified API call", "")),
    ({"QBIT_USER": "admin", "QBIT_PASS": "pw"}, "Fails.", None,
     ("FAIL", "api:qbit", "login returned Fails.")),
], ids=["bypass-ok", "bypass-fail", "auth-roundtrip-ok", "auth-login-fail"])
def test_probe_qbit(tmp_path, env, login, reach, expected):
    hc = _hc(tmp_path)
    hc.env = env
    client = MagicMock()
    client.login_response.return_value = login
    client.reachable_status.return_value = reach
    with patch.object(health, "QBitClient", return_value=client):
        hc.probe_qbit("http://q:8090")
    assert hc.results == [expected]


# ---------------- UFW ----------------

@pytest.mark.parametrize("which, run_rv, expected", [
    (True, (0, "Status: inactive\n", ""), [("OK", "ufw=inactive (Tailscale perimeter intact)", "")]),
    (True, (0, "Status: active\n", ""), [("FAIL", "ufw=active")]),
    (True, (1, "", ""), [("WARN", "ufw")]),
], ids=["inactive-ok", "active-fails", "no-sudo-warns"])
def test_probe_ufw(tmp_path, which, run_rv, expected):
    hc = _hc(tmp_path)
    with patch.object(health, "_which", return_value=which), \
         patch.object(health, "_run", return_value=run_rv):
        hc.probe_ufw()
    _assert_results(hc, expected)


# ---------------- perimeter ----------------

def test_perimeter_allow_override_skips(tmp_path):
    hc = _hc(tmp_path)
    hc.env = {"ALLOW_PUBLIC_IFACE": "1"}
    hc.probe_perimeter()
    assert hc.results == []


@pytest.mark.parametrize("v4, v6, state, idx, frag", [
    ("1: lo    inet 127.0.0.1/8 scope host lo\n"
     "2: eth0  inet 10.0.0.10/24 brd 10.0.0.255 scope global eth0\n"
     "3: ts0   inet 100.64.0.10/32 scope global ts0\n",
     "2: eth0  inet6 fd7a:115c:a1e0::1/128 scope global\n", "OK", 1, "no public-routable"),
    ("2: eth0 inet 8.8.8.8/24 scope global eth0\n", "", "FAIL", 2, "8.8.8.8"),
], ids=["clean-ok", "public-v4-fails"])
def test_perimeter_addresses(tmp_path, v4, v6, state, idx, frag):
    hc = _hc(tmp_path)

    def fake_run(args, **kw):
        if "-4" in args:
            return (0, v4, "")
        if "-6" in args:
            return (0, v6, "")
        return (1, "", "")

    with patch.object(health, "_which", return_value=True), \
         patch.object(health, "_run", side_effect=fake_run):
        hc.probe_perimeter()
    assert hc.results[0][0] == state
    assert frag in hc.results[0][idx]


def test_perimeter_ip_unusable_fails(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health, "_which", return_value=True), \
         patch.object(health, "_run", return_value=(1, "", "")):
        hc.probe_perimeter()
    assert hc.results[0][0] == "FAIL"
    assert "ip addr show failed" in hc.results[0][2]


# ---------------- realtek / services / vpn-country / ops / sweep ----------------

@pytest.mark.parametrize("link_out, systemctl_rc, expected", [
    ("1: lo: <LOOPBACK>\n", 0, []),
    ("3: enp6s0f1: <BROADCAST>\n", 0,
     [("OK", "realtek-fix.service active (this host has enp6s0f1)", "")]),
    ("3: enp6s0f1: <BROADCAST>\n", 3, [("FAIL", "realtek-fix.service")]),
], ids=["absent-skips", "present-active-ok", "present-inactive-fails"])
def test_probe_realtek(tmp_path, link_out, systemctl_rc, expected):
    hc = _hc(tmp_path)

    def fake_run(args, **kw):
        if args[:3] == ["ip", "-o", "link"]:
            return (0, link_out, "")
        return (systemctl_rc, "", "")

    with patch.object(health, "_run", side_effect=fake_run):
        hc.probe_realtek()
    _assert_results(hc, expected)


@pytest.mark.parametrize("rc, unit, ok_msg, fail_detail, expected", [
    (0, "consolidate-watch.service", "consolidate-watch.service active", "nope",
     ("OK", "consolidate-watch.service active", "")),
    (3, "malware-guard.service", "ok-msg", "inactive — reaped",
     ("FAIL", "malware-guard.service", "inactive — reaped")),
], ids=["active", "inactive"])
def test_probe_service(tmp_path, rc, unit, ok_msg, fail_detail, expected):
    hc = _hc(tmp_path)
    with patch.object(health, "_run", return_value=(rc, "", "")):
        hc.probe_service(unit, ok_msg, fail_detail)
    assert hc.results == [expected]


@pytest.mark.parametrize("country, iso, matches, expected", [
    ("", None, None, ("FAIL", "vpn-country", "could not query VPN country — policy unverified")),
    ("Switzerland", "CH", True, ("OK", "vpn-country=Switzerland", "")),
    ("United Kingdom", "GB", False, ("FAIL", "vpn-country=United Kingdom")),
], ids=["empty-fails", "match-ok", "mismatch-fails"])
def test_probe_vpn_country(tmp_path, country, iso, matches, expected):
    hc = _hc(tmp_path)
    hc.vpn_country = country
    with patch.object(health.vpn_country, "name_to_iso", return_value=iso), \
         patch.object(health.vpn_country, "country_matches", return_value=matches):
        hc.probe_vpn_country()
    _assert_results(hc, [expected])


def test_ops_audit_not_executable_skips(tmp_path):
    hc = _hc(tmp_path)
    hc.probe_ops_audit()  # tmp_path has no ops/audit.sh
    assert hc.results == []


@pytest.mark.parametrize("rc,state,frag", [
    (0, "OK", "cron + systemd match repo"),
    (2, "WARN", "mode-640 unit(s) unverifiable"),
    (1, "FAIL", "live cron or systemd differs"),
])
def test_ops_audit_tristate(tmp_path, rc, state, frag):
    (tmp_path / "ops").mkdir()
    audit = tmp_path / "ops" / "audit.sh"
    audit.write_text("#!/bin/bash\n")
    audit.chmod(0o755)
    hc = _hc(tmp_path)
    with patch.object(health, "_run", return_value=(rc, "", "")):
        hc.probe_ops_audit()
    assert hc.results[0][0] == state
    assert frag in hc.results[0][1] + hc.results[0][2]


# ---------------- repo-root hygiene ----------------

def test_repo_root_clean_ok(tmp_path):
    """Whitelisted entries and tool caches are not flagged."""
    (tmp_path / "AGENTS.md").write_text("")
    for name in ("media_stack", ".mypy_cache", ".pytest_cache"):
        (tmp_path / name).mkdir()
    hc = _hc(tmp_path)
    hc.probe_repo_root()
    assert hc.results == [("OK", "repo-root-hygiene: only whitelisted entries", "")]


def test_repo_root_unexpected_warns(tmp_path):
    (tmp_path / "AGENTS.md").write_text("")
    (tmp_path / "mystery_file").write_text("")
    (tmp_path / "scratch").mkdir()
    hc = _hc(tmp_path)
    hc.probe_repo_root()
    assert hc.results[0][0] == "WARN"
    assert hc.results[0][1] == "repo-root-hygiene"
    # bash %q format: plain names appear verbatim, single trailing space.
    detail = hc.results[0][2]
    assert detail.startswith("unexpected top-level entries: ")
    assert "mystery_file" in detail and "scratch" in detail


def test_repo_root_whitelist_covers_tracked_top_level_entries():
    """Every top-level entry git tracks must be whitelisted, or the hourly healthcheck warns."""
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        pytest.skip("not a git checkout")
    tracked = {p.split("/", 1)[0] for p in r.stdout.split("\0") if p}
    assert tracked
    assert tracked - health.REPO_ROOT_EXPECTED == set()


# ---------------- arg parsing ----------------

def test_main_unknown_arg_exit2(capsys):
    rc = health.main(["--bogus"])
    assert rc == 2
    assert "unknown arg" in capsys.readouterr().err


# ---------------- pipeline OUTCOME probes ----------------
# These answer "is it WORKING", not "is it RUNNING".  They drive the REAL ageing
# helper against a temp state dir with a controlled clock (mocking it would only
# prove the probes call it).

@pytest.fixture
def aged(tmp_path, monkeypatch):
    """Real _age_conditions against a temp state dir, with a movable clock."""
    monkeypatch.setattr(health.paths, "VAR_STATE", tmp_path / "state")
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(health.time, "time", lambda: clock["t"])
    return clock


def _queue_client(records):
    c = MagicMock()
    c.get_queue.return_value = records
    return c


def _bad(did):
    return {"downloadId": did, "errorMessage": "qBittorrent is reporting missing files"}


def _sonarr_hc(tmp_path):
    hc = _hc(tmp_path)
    hc.env = {"SONARR_API_KEY": "k"}
    return hc


def _probe_queue(hc, records):
    with patch.object(health, "ArrClient", return_value=_queue_client(records)):
        hc.probe_arr_stuck_queue("sonarr", "http://x", "SONARR_API_KEY")


def _qbit_client(**attrs):
    c = MagicMock()
    c.login.return_value = True
    for name, val in attrs.items():
        getattr(c, name).return_value = val
    return c


def _probe_port(hc, port_json, qbit=None):
    with patch.object(health, "QBitClient", return_value=qbit or _qbit_client()), \
         patch.object(health, "_run", return_value=(0, port_json, "")):
        hc.probe_forwarded_port("http://q")


def test_queue_condition_ages_across_runs(tmp_path, aged):
    hc = _sonarr_hc(tmp_path)
    _probe_queue(hc, [_bad("a")])
    assert hc.results[-1][0] == "WARN", "first sighting is young"
    aged["t"] += health.HealthCheck.STUCK_AFTER_S + 1
    _probe_queue(hc, [_bad("a")])
    assert hc.results[-1][0] == "FAIL", "same condition, now aged"


def test_recurrence_after_clearing_starts_a_fresh_clock(tmp_path, aged):
    """An item that vanishes and returns must not inherit its old timestamp and FAIL instantly."""
    hc = _sonarr_hc(tmp_path)
    _probe_queue(hc, [_bad("a")])
    aged["t"] += health.HealthCheck.STUCK_AFTER_S + 1
    _probe_queue(hc, [])   # clears
    assert hc.results[-1][0] == "OK"
    _probe_queue(hc, [_bad("a")])
    assert hc.results[-1][0] == "WARN", "recurrence must restart the clock"


def test_unwritable_state_reports_uncertainty_not_a_fake_age(tmp_path, aged, monkeypatch):
    """Swallowing write failures would make the age a lie."""
    hc = _sonarr_hc(tmp_path)
    monkeypatch.setattr(health.state, "mutate_state",
                        MagicMock(side_effect=OSError("read-only fs")))
    _probe_queue(hc, [_bad("a")])
    assert hc.results[-1][0] == "WARN"
    assert "unreadable/unwritable" in hc.results[-1][2]


def test_wrong_shaped_state_json_does_not_crash(tmp_path, aged):
    """Valid JSON that is not an object must not abort the run."""
    sd = tmp_path / "state"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / health.HealthCheck.OUTCOME_STATE).write_text("[]")
    hc = _sonarr_hc(tmp_path)
    _probe_queue(hc, [_bad("a")])
    assert hc.results[-1][0] == "WARN"


def test_matching_port_clears_both_timers(tmp_path, aged):
    hc = _hc(tmp_path)
    _probe_port(hc, '{"port":4242}', _qbit_client(preferences={"listen_port": 4242}))
    assert hc.results[-1][0] == "OK"
    left = json.loads((tmp_path / "state" / health.HealthCheck.OUTCOME_STATE).read_text())
    assert not [k for k in left if k.startswith("portforward:")]


def test_absent_allocation_fails_with_the_real_cause(tmp_path, aged):
    hc = _hc(tmp_path)
    _probe_port(hc, '{"port":0,"ports":[]}')
    aged["t"] += health.HealthCheck.NO_PORT_AFTER_S + 1
    _probe_port(hc, '{"port":0,"ports":[]}')
    assert hc.results[-1][0] == "FAIL"
    assert "NAT-PMP" in hc.results[-1][2]


def test_unreachable_arr_warns_rather_than_faking_a_stuck_queue(tmp_path, aged):
    hc = _sonarr_hc(tmp_path)
    c = MagicMock()
    c.get_queue.side_effect = Exception("down")
    with patch.object(health, "ArrClient", return_value=c):
        hc.probe_arr_stuck_queue("sonarr", "http://x", "SONARR_API_KEY")
    assert hc.results[-1][0] == "WARN"


def test_qbit_missing_files_steers_away_from_mass_deletion(tmp_path, aged):
    hc = _hc(tmp_path)
    c = _qbit_client(torrents_info=[{"hash": "h1", "state": "missingFiles"}])
    with patch.object(health, "QBitClient", return_value=c):
        hc.probe_qbit_missing_files("http://q")
        aged["t"] += health.HealthCheck.STUCK_AFTER_S + 1
        hc.probe_qbit_missing_files("http://q")
    assert hc.results[-1][0] == "FAIL"
    assert "mount" in hc.results[-1][2]


def test_subs_ignores_failed_syncs_whose_file_is_gone(tmp_path, aged):
    """A deleted file is not a current problem."""
    real = tmp_path / "present.mkv"
    real.write_text("x")
    hc = _hc(tmp_path)
    st = {str(real): {"synced": False},
          "/gone/missing.mkv": {"synced": False}}
    with patch.object(health.state, "load_state", return_value=st):
        hc.probe_unsynced_subs()
    assert hc.results[-1][0] == "WARN"
    assert "unsynced=1" in hc.results[-1][1]
    assert "1 failed-sync records whose file is gone" in hc.results[-1][2]


def test_subs_never_claims_all_synced_when_status_is_unknown(tmp_path, aged):
    """Most records carry no `synced` field; absence of evidence is not success."""
    hc = _hc(tmp_path)
    with patch.object(health.state, "load_state",
                      return_value={"/a.mkv": {"status": "skip-tag"}}):
        hc.probe_unsynced_subs()
    assert hc.results[-1][0] == "OK"
    msg = hc.results[-1][1]
    assert "no recorded result" in msg
    assert "all" not in msg.lower(), f"must not claim completeness: {msg}"


def test_subs_malformed_state_warns(tmp_path, aged):
    hc = _hc(tmp_path)
    with patch.object(health.state, "load_state", return_value=[]):
        hc.probe_unsynced_subs()
    assert hc.results[-1][0] == "WARN"


# Persistence edges: a surviving stale timestamp makes the next recurrence FAIL instantly.

def test_unreadable_state_does_not_silently_wipe_history(tmp_path, aged):
    """mutate_state must not treat an unreadable file (load_state maps it to {}) as empty and overwrite history."""
    sd = tmp_path / "state"
    sd.mkdir(parents=True, exist_ok=True)
    f = sd / health.HealthCheck.OUTCOME_STATE
    hc = _hc(tmp_path)
    hc._age_conditions("x:", {"x:A"})                 # seed a real timer
    before = f.read_text()
    f.write_text("{not valid json")                   # corrupt it
    ages, persisted = hc._age_conditions("x:", {"x:A"})
    assert persisted is False, "an unreadable store must report uncertainty"
    f.write_text(before)                              # restoring proves history survived
    ages2, ok2 = hc._age_conditions("x:", {"x:A"})
    assert ok2 and ages2["x:A"] == 0.0


