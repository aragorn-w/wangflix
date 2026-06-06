"""Tests for media_stack/health.py — the typed port of healthcheck.sh (A12).

The live system is healthy, so a byte-identical prod diff only proves the
OK/WARN paths the current state happens to hit.  These tests drive every
probe's FAIL/WARN/OK branch with mocked subprocess/HTTP/clients, plus the
stdout-vs-stderr print routing, the --json shape, and the exit-code logic —
so the behaviour-preserving claim covers the branches prod can't show.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from media_stack import health


def _hc(tmp_path, **kw):
    hc = health.HealthCheck(repo_root=tmp_path, **kw)
    hc.env = {}
    return hc


# ---------------- result recorders / routing ----------------

def test_ok_silent_in_plain_mode(tmp_path, capsys):
    hc = _hc(tmp_path)
    hc.ok("thing running")
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert hc.results == [("OK", "thing running", "")]


def test_ok_prints_only_with_verbose(tmp_path, capsys):
    hc = _hc(tmp_path, verbose=True)
    hc.ok("thing running")
    assert capsys.readouterr().out == "OK:   thing running\n"


def test_ok_suppressed_in_json_even_with_verbose(tmp_path, capsys):
    hc = _hc(tmp_path, verbose=True, json_mode=True)
    hc.ok("thing")
    assert capsys.readouterr().out == ""


def test_fail_goes_to_stderr(tmp_path, capsys):
    hc = _hc(tmp_path)
    hc.fail("widget", "broke")
    cap = capsys.readouterr()
    assert cap.out == ""
    assert cap.err == "FAIL: widget — broke\n"


def test_warn_goes_to_stdout(tmp_path, capsys):
    hc = _hc(tmp_path)
    hc.warn("widget", "iffy")
    cap = capsys.readouterr()
    assert cap.out == "WARN: widget — iffy\n"
    assert cap.err == ""


def test_json_mode_suppresses_warn_and_fail_inline(tmp_path, capsys):
    hc = _hc(tmp_path, json_mode=True)
    hc.warn("a", "b")
    hc.fail("c", "d")
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err == ""


# ---------------- summary / exit codes ----------------

def test_summary_all_ok_exit0(tmp_path, capsys):
    hc = _hc(tmp_path)
    hc.results = [("OK", "a", ""), ("OK", "b", "")]
    assert hc.summary() == 0
    assert "HEALTH OK: 2 / 2 checks passed" in capsys.readouterr().out


def test_summary_warn_only_exit2(tmp_path, capsys):
    hc = _hc(tmp_path)
    hc.results = [("OK", "a", ""), ("WARN", "b", "x")]
    assert hc.summary() == 2
    assert "HEALTH WARN: 1 warn / 1 ok" in capsys.readouterr().out


def test_summary_any_fail_exit1(tmp_path, capsys):
    hc = _hc(tmp_path)
    hc.results = [("OK", "a", ""), ("WARN", "b", "x"), ("FAIL", "c", "y")]
    assert hc.summary() == 1
    assert "HEALTH FAIL: 1 fail / 1 warn / 1 ok" in capsys.readouterr().out


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

def test_gluetun_not_running_fails(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health, "_run", return_value=(1, "", "")):
        hc.probe_gluetun()
    assert hc.results == [("FAIL", "gluetun", "gluetun container not running")]


def test_gluetun_ok(tmp_path):
    hc = _hc(tmp_path)

    def fake_run(args, **kw):
        if args[:3] == ["docker", "compose", "-f"] and "ps" in args:
            return (0, "gluetun\n", "")
        if args[:3] == ["docker", "exec", "gluetun"]:
            url = args[-1]
            if "ifconfig.co" in url:
                return (0, "146.70.226.218\n", "")
            if "ipinfo.io" in url:
                return (0, "CH\n", "")
            if "ip2location" in url:
                return (0, '{"country_code": "CH"}', "")
        if args[0] == "curl":
            return (0, "8.8.8.8\n", "")  # host egress != vpn ip
        return (1, "", "")

    with patch.object(health, "_run", side_effect=fake_run):
        hc.probe_gluetun()
    assert hc.results == [("OK", "gluetun-egress ip=146.70.226.218 country=Switzerland", "")]
    assert hc.vpn_country == "Switzerland"


def test_gluetun_leak_fails(tmp_path):
    hc = _hc(tmp_path)

    def fake_run(args, **kw):
        if "ps" in args:
            return (0, "gluetun\n", "")
        if args[:3] == ["docker", "exec", "gluetun"]:
            return (0, "5.5.5.5\n", "")
        if args[0] == "curl":
            return (0, "5.5.5.5\n", "")  # SAME as vpn ip -> leak
        return (1, "", "")

    with patch.object(health, "_run", side_effect=fake_run):
        hc.probe_gluetun()
    assert hc.results[0][0] == "FAIL"
    assert "LEAKING" in hc.results[0][2]


def test_gluetun_no_host_ip_warns(tmp_path):
    hc = _hc(tmp_path)

    def fake_run(args, **kw):
        if "ps" in args:
            return (0, "gluetun\n", "")
        if args[:3] == ["docker", "exec", "gluetun"]:
            return (0, "5.5.5.5\n", "")
        if args[0] == "curl":
            return (1, "", "")  # host egress unavailable
        return (1, "", "")

    with patch.object(health, "_run", side_effect=fake_run):
        hc.probe_gluetun()
    assert hc.results[0][0] == "WARN"
    assert "leak check skipped" in hc.results[0][2]


def test_gluetun_single_geo_source_still_verifies_country(tmp_path):
    # ipinfo rate-limited (empty) but ip2location confirms CH → must NOT
    # false-fail the country check (the bug that spammed VPN-down alerts).
    hc = _hc(tmp_path)

    def fake_run(args, **kw):
        if "ps" in args:
            return (0, "gluetun\n", "")
        if args[:3] == ["docker", "exec", "gluetun"]:
            url = args[-1]
            if "ifconfig.co" in url:
                return (0, "146.70.226.218\n", "")
            if "ipinfo.io" in url:
                return (0, "\n", "")                       # rate-limited → empty
            if "ip2location" in url:
                return (0, '{"country_code": "CH"}', "")
        if args[0] == "curl":
            return (0, "8.8.8.8\n", "")
        return (1, "", "")

    with patch.object(health, "_run", side_effect=fake_run):
        hc.probe_gluetun()
    assert hc.vpn_country == "Switzerland"   # single source resolved, not a debug string
    hc.probe_vpn_country()                   # uses the real consensus/match helpers
    vc = [r for r in hc.results if r[1].startswith("vpn-country")]
    assert vc and vc[0][0] == "OK"           # was a false FAIL before the fix


def test_gluetun_single_source_wrong_country_still_fails(tmp_path):
    # Only one source AND it names the wrong country → must still FAIL.
    hc = _hc(tmp_path)

    def fake_run(args, **kw):
        if "ps" in args:
            return (0, "gluetun\n", "")
        if args[:3] == ["docker", "exec", "gluetun"]:
            url = args[-1]
            if "ifconfig.co" in url:
                return (0, "5.5.5.5\n", "")
            if "ipinfo.io" in url:
                return (0, "\n", "")
            if "ip2location" in url:
                return (0, '{"country_code": "GB"}', "")    # wrong country
        if args[0] == "curl":
            return (0, "8.8.8.8\n", "")
        return (1, "", "")

    with patch.object(health, "_run", side_effect=fake_run):
        hc.probe_gluetun()
    hc.probe_vpn_country()
    vc = [r for r in hc.results if r[1].startswith("vpn-country")]
    assert vc and vc[0][0] == "FAIL"


# ---------------- containers ----------------

def test_container_required_missing_fails(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health, "_run", return_value=(0, "", "")):
        hc.check_container("radarr", True)
    assert hc.results == [("FAIL", "container:radarr", "not running (no container id)")]


def test_container_optional_missing_warns(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health, "_run", return_value=(0, "", "")):
        hc.check_container("watchtower", False)
    assert hc.results == [("WARN", "container:watchtower", "optional, not running")]


def test_container_running_no_health(tmp_path):
    hc = _hc(tmp_path)

    def fake_run(args, **kw):
        if "-q" in args:
            return (0, "abc123\n", "")
        if "{{.State.Status}}" in args:
            return (0, "running\n", "")
        return (0, "\n", "")  # no health

    with patch.object(health, "_run", side_effect=fake_run):
        hc.check_container("sonarr", True)
    assert hc.results == [("OK", "container:sonarr running", "")]


def test_container_running_healthy_suffix(tmp_path):
    hc = _hc(tmp_path)

    def fake_run(args, **kw):
        if "-q" in args:
            return (0, "abc\n", "")
        if "{{.State.Status}}" in args:
            return (0, "running\n", "")
        return (0, "healthy\n", "")

    with patch.object(health, "_run", side_effect=fake_run):
        hc.check_container("gluetun", True)
    assert hc.results == [("OK", "container:gluetun running health=healthy", "")]


def test_container_unhealthy_fails(tmp_path):
    hc = _hc(tmp_path)

    def fake_run(args, **kw):
        if "-q" in args:
            return (0, "abc\n", "")
        if "{{.State.Status}}" in args:
            return (0, "running\n", "")
        return (0, "unhealthy\n", "")

    with patch.object(health, "_run", side_effect=fake_run):
        hc.check_container("jellyfin", True)
    assert hc.results == [("FAIL", "container:jellyfin", "running but health=unhealthy")]


def test_container_exited_required_fails(tmp_path):
    hc = _hc(tmp_path)

    def fake_run(args, **kw):
        if "-q" in args:
            return (0, "abc\n", "")
        if "{{.State.Status}}" in args:
            return (0, "exited\n", "")
        return (0, "\n", "")

    with patch.object(health, "_run", side_effect=fake_run):
        hc.check_container("radarr", True)
    assert hc.results == [("FAIL", "container:radarr", "state=exited (expected running)")]


# ---------------- Arr API reachability ----------------

def test_probe_arr_missing_key_warns(tmp_path):
    hc = _hc(tmp_path)
    hc.probe_arr("radarr", "http://x:7878", "RADARR_API_KEY")
    assert hc.results == [("WARN", "api:radarr",
                           "RADARR_API_KEY missing from .env — reachability not checked")]


def test_probe_arr_200_ok(tmp_path):
    hc = _hc(tmp_path)
    hc.env = {"RADARR_API_KEY": "k"}
    client = MagicMock()
    client.reachable_status.return_value = "200"
    with patch.object(health, "ArrClient", return_value=client):
        hc.probe_arr("radarr", "http://x:7878", "RADARR_API_KEY")
    assert hc.results == [("OK", "api:radarr reachable", "")]


def test_probe_arr_non200_fails_with_status(tmp_path):
    hc = _hc(tmp_path)
    hc.env = {"RADARR_API_KEY": "k"}
    client = MagicMock()
    client.reachable_status.return_value = "500"
    with patch.object(health, "ArrClient", return_value=client):
        hc.probe_arr("radarr", "http://x:7878", "RADARR_API_KEY")
    assert hc.results == [("FAIL", "api:radarr",
                           "http://x:7878/api/v3/system/status returned 500")]


def test_probe_arr_connection_error_is_000(tmp_path):
    hc = _hc(tmp_path)
    hc.env = {"RADARR_API_KEY": "k"}
    client = MagicMock()
    client.reachable_status.return_value = "000"
    with patch.object(health, "ArrClient", return_value=client):
        hc.probe_arr("radarr", "http://x:7878", "RADARR_API_KEY")
    assert hc.results[0][2].endswith("returned 000")


# ---------------- Arr policy probes ----------------

@pytest.mark.parametrize("val,state,frag", [
    (True, "OK", "hardlinks=enabled"),
    (False, "FAIL", "hardlinks=DISABLED"),
    (None, "WARN", "couldn't read /config/mediamanagement"),
])
def test_probe_hardlinks(tmp_path, val, state, frag):
    hc = _hc(tmp_path)
    hc.env = {"RADARR_API_KEY": "k"}
    client = MagicMock()
    client.hardlinks_enabled.return_value = val
    with patch.object(health, "ArrClient", return_value=client):
        hc.probe_arr_hardlinks("radarr", "http://x", "RADARR_API_KEY")
    assert hc.results[0][0] == state
    blob = hc.results[0][1] + hc.results[0][2]
    assert frag in blob


@pytest.mark.parametrize("val,state,frag", [
    ([], "OK", "AV1+12-bit=-10000"),
    (None, "WARN", "couldn't read /qualityprofile"),
])
def test_probe_format_scores_ok_none(tmp_path, val, state, frag):
    hc = _hc(tmp_path)
    hc.env = {"RADARR_API_KEY": "k"}
    client = MagicMock()
    client.format_score_violations.return_value = val
    with patch.object(health, "ArrClient", return_value=client):
        hc.probe_arr_format_scores("radarr", "http://x", "RADARR_API_KEY")
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


@pytest.mark.parametrize("val,state,frag", [
    (0, "OK", "silent-gap=0"),
    (None, "WARN", "couldn't read /movie"),
    (7, "FAIL", "silent-gap=7"),
])
def test_probe_silent_gap(tmp_path, val, state, frag):
    hc = _hc(tmp_path)
    hc.env = {"RADARR_API_KEY": "k"}
    client = MagicMock()
    client.unmonitored_no_file_count.return_value = val
    with patch.object(health, "ArrClient", return_value=client):
        hc.probe_arr_silent_gap("radarr", "http://x", "RADARR_API_KEY")
    assert hc.results[0][0] == state
    assert frag in hc.results[0][1] + hc.results[0][2]


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


def test_bazarr_profiles_clean(tmp_path):
    hc = _hc(tmp_path)
    hc.bazarr_key = "bk"
    client = MagicMock()
    client.unprofiled_count.return_value = (0, 0)
    client.wrong_profile_count.return_value = (0, 0)
    with patch.object(health, "BazarrClient", return_value=client):
        hc.probe_bazarr_profiles("http://b")
    assert hc.results[0] == ("OK", "bazarr-profile-coverage: 0 unprofiled (movies + series)", "")
    assert hc.results[1][0] == "OK"
    assert "matches expected profileId=1" in hc.results[1][1]


def test_bazarr_profiles_unprofiled_warn(tmp_path):
    hc = _hc(tmp_path)
    hc.bazarr_key = "bk"
    client = MagicMock()
    client.unprofiled_count.return_value = (94, 0)
    client.wrong_profile_count.return_value = (0, 0)
    with patch.object(health, "BazarrClient", return_value=client):
        hc.probe_bazarr_profiles("http://b")
    assert hc.results[0][0] == "WARN"
    assert "94 movies + 0 series have profileId=None" in hc.results[0][2]


def test_bazarr_profiles_endpoint_error(tmp_path):
    hc = _hc(tmp_path)
    hc.bazarr_key = "bk"
    client = MagicMock()
    client.unprofiled_count.return_value = (None, None)
    client.wrong_profile_count.return_value = (None, None)
    with patch.object(health, "BazarrClient", return_value=client):
        hc.probe_bazarr_profiles("http://b")
    assert hc.results[0][0] == "WARN"
    assert "endpoint parse error" in hc.results[0][2]
    assert hc.results[1][0] == "WARN"
    assert "endpoint error" in hc.results[1][2]


def test_bazarr_profiles_client_exception_is_warn_not_crash(tmp_path):
    # A malformed response that escapes the client must not crash the whole
    # monitor — both probes map to their endpoint-error WARN.
    hc = _hc(tmp_path)
    hc.bazarr_key = "bk"
    client = MagicMock()
    client.unprofiled_count.side_effect = Exception("malformed")
    client.wrong_profile_count.side_effect = Exception("malformed")
    with patch.object(health, "BazarrClient", return_value=client):
        hc.probe_bazarr_profiles("http://b")
    assert hc.results[0][0] == "WARN" and "endpoint parse error" in hc.results[0][2]
    assert hc.results[1][0] == "WARN" and "endpoint error" in hc.results[1][2]


def test_bazarr_profiles_bad_expected_fails(tmp_path):
    hc = _hc(tmp_path)
    hc.bazarr_key = "bk"
    hc.env = {"BAZARR_DEFAULT_PROFILE_ID": "abc"}
    client = MagicMock()
    client.unprofiled_count.return_value = (0, 0)
    with patch.object(health, "BazarrClient", return_value=client):
        hc.probe_bazarr_profiles("http://b")
    eq = [r for r in hc.results if "equality" in r[1]]
    assert eq and eq[0][0] == "FAIL"
    assert "must be a non-negative integer, got 'abc'" in eq[0][2]
    client.wrong_profile_count.assert_not_called()


def test_bazarr_profiles_wrong_profile_warn(tmp_path):
    hc = _hc(tmp_path)
    hc.bazarr_key = "bk"
    client = MagicMock()
    client.unprofiled_count.return_value = (0, 0)
    client.wrong_profile_count.return_value = (3, 1)
    with patch.object(health, "BazarrClient", return_value=client):
        hc.probe_bazarr_profiles("http://b")
    assert hc.results[1][0] == "WARN"
    assert "3 movies + 1 series assigned to a profileId != 1" in hc.results[1][2]


# ---------------- qBittorrent ----------------

def test_qbit_bypass_ok(tmp_path):
    hc = _hc(tmp_path)
    client = MagicMock()
    client.reachable_status.return_value = "200"
    with patch.object(health, "QBitClient", return_value=client):
        hc.probe_qbit("http://q:8090")
    assert hc.results == [("OK", "api:qbit reachable (bypass-auth-on-LAN)", "")]


def test_qbit_bypass_fail(tmp_path):
    hc = _hc(tmp_path)
    client = MagicMock()
    client.reachable_status.return_value = "403"
    with patch.object(health, "QBitClient", return_value=client):
        hc.probe_qbit("http://q:8090")
    assert hc.results[0][0] == "FAIL"
    assert "returned 403 (no auth)" in hc.results[0][2]


def test_qbit_auth_roundtrip_ok(tmp_path):
    hc = _hc(tmp_path)
    hc.env = {"QBIT_USER": "admin", "QBIT_PASS": "pw"}
    client = MagicMock()
    client.login_response.return_value = "Ok."
    client.reachable_status.return_value = "200"
    with patch.object(health, "QBitClient", return_value=client):
        hc.probe_qbit("http://q:8090")
    assert hc.results == [("OK", "api:qbit auth round-trip + cookie-verified API call", "")]


def test_qbit_auth_login_fail(tmp_path):
    hc = _hc(tmp_path)
    hc.env = {"QBIT_USER": "admin", "QBIT_PASS": "pw"}
    client = MagicMock()
    client.login_response.return_value = "Fails."
    with patch.object(health, "QBitClient", return_value=client):
        hc.probe_qbit("http://q:8090")
    assert hc.results[0][0] == "FAIL"
    assert "login returned Fails." in hc.results[0][2]


# ---------------- UFW ----------------

def test_ufw_absent_skips(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health, "_which", return_value=False):
        hc.probe_ufw()
    assert hc.results == []


def test_ufw_inactive_ok(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health, "_which", return_value=True), \
         patch.object(health, "_run", return_value=(0, "Status: inactive\n", "")):
        hc.probe_ufw()
    assert hc.results == [("OK", "ufw=inactive (Tailscale perimeter intact)", "")]


def test_ufw_active_fails(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health, "_which", return_value=True), \
         patch.object(health, "_run", return_value=(0, "Status: active\n", "")):
        hc.probe_ufw()
    assert hc.results[0][0] == "FAIL"
    assert hc.results[0][1] == "ufw=active"


def test_ufw_no_sudo_warns(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health, "_which", return_value=True), \
         patch.object(health, "_run", return_value=(1, "", "")):
        hc.probe_ufw()
    assert hc.results[0][0] == "WARN"


# ---------------- perimeter ----------------

def test_perimeter_allow_override_skips(tmp_path):
    hc = _hc(tmp_path)
    hc.env = {"ALLOW_PUBLIC_IFACE": "1"}
    hc.probe_perimeter()
    assert hc.results == []


def test_perimeter_clean_ok(tmp_path):
    hc = _hc(tmp_path)

    def fake_run(args, **kw):
        if "-4" in args:
            return (0, "1: lo    inet 127.0.0.1/8 scope host lo\n"
                       "2: eth0  inet 10.0.0.10/24 brd 10.0.0.255 scope global eth0\n"
                       "3: ts0   inet 100.120.41.57/32 scope global ts0\n", "")
        if "-6" in args:
            return (0, "2: eth0  inet6 fd7a:115c:a1e0::1/128 scope global\n", "")
        return (1, "", "")

    with patch.object(health, "_which", return_value=True), \
         patch.object(health, "_run", side_effect=fake_run):
        hc.probe_perimeter()
    assert hc.results[0][0] == "OK"
    assert "no public-routable" in hc.results[0][1]


def test_perimeter_public_v4_fails(tmp_path):
    hc = _hc(tmp_path)

    def fake_run(args, **kw):
        if "-4" in args:
            return (0, "2: eth0 inet 8.8.8.8/24 scope global eth0\n", "")
        if "-6" in args:
            return (0, "", "")
        return (1, "", "")

    with patch.object(health, "_which", return_value=True), \
         patch.object(health, "_run", side_effect=fake_run):
        hc.probe_perimeter()
    assert hc.results[0][0] == "FAIL"
    assert "8.8.8.8" in hc.results[0][2]


def test_perimeter_public_v6_fails(tmp_path):
    hc = _hc(tmp_path)

    def fake_run(args, **kw):
        if "-4" in args:
            return (0, "", "")
        if "-6" in args:
            return (0, "2: eth0 inet6 2001:db8::1/64 scope global\n", "")
        return (1, "", "")

    with patch.object(health, "_which", return_value=True), \
         patch.object(health, "_run", side_effect=fake_run):
        hc.probe_perimeter()
    assert hc.results[0][0] == "FAIL"
    assert "2001:db8::1" in hc.results[0][2]


def test_perimeter_ip_missing_fails(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health, "_which", return_value=False):
        hc.probe_perimeter()
    assert hc.results[0][0] == "FAIL"
    assert "iproute2" in hc.results[0][2]


def test_perimeter_ip_error_fails(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health, "_which", return_value=True), \
         patch.object(health, "_run", return_value=(1, "", "")):
        hc.probe_perimeter()
    assert hc.results[0][0] == "FAIL"
    assert "ip addr show failed" in hc.results[0][2]


# ---------------- realtek / services / vpn-country / ops / sweep ----------------

def test_realtek_absent_skips(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health, "_run", return_value=(0, "1: lo: <LOOPBACK>\n", "")):
        hc.probe_realtek()
    assert hc.results == []


def test_realtek_present_active_ok(tmp_path):
    hc = _hc(tmp_path)

    def fake_run(args, **kw):
        if args[:3] == ["ip", "-o", "link"]:
            return (0, "3: enp6s0f1: <BROADCAST>\n", "")
        return (0, "", "")  # systemctl is-active -> rc 0

    with patch.object(health, "_run", side_effect=fake_run):
        hc.probe_realtek()
    assert hc.results == [("OK", "realtek-fix.service active (this host has enp6s0f1)", "")]


def test_realtek_present_inactive_fails(tmp_path):
    hc = _hc(tmp_path)

    def fake_run(args, **kw):
        if args[:3] == ["ip", "-o", "link"]:
            return (0, "3: enp6s0f1: <BROADCAST>\n", "")
        return (3, "", "")  # systemctl inactive

    with patch.object(health, "_run", side_effect=fake_run):
        hc.probe_realtek()
    assert hc.results[0][0] == "FAIL"


def test_probe_service_active(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health, "_run", return_value=(0, "", "")):
        hc.probe_service("consolidate-watch.service", "consolidate-watch.service active", "nope")
    assert hc.results == [("OK", "consolidate-watch.service active", "")]


def test_probe_service_inactive(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health, "_run", return_value=(3, "", "")):
        hc.probe_service("malware-guard.service", "ok-msg", "inactive — reaped")
    assert hc.results == [("FAIL", "malware-guard.service", "inactive — reaped")]


def test_vpn_country_empty_fails(tmp_path):
    hc = _hc(tmp_path)
    hc.vpn_country = ""
    hc.probe_vpn_country()
    assert hc.results == [("FAIL", "vpn-country", "could not query VPN country — policy unverified")]


def test_vpn_country_disagree_fails(tmp_path):
    hc = _hc(tmp_path)
    hc.vpn_country = "DISAGREE(ipinfo=CH,ip2=GB)"
    hc.probe_vpn_country()
    assert hc.results[0][0] == "FAIL"
    assert "cannot verify against expected 'Switzerland'" in hc.results[0][2]


def test_vpn_country_match_ok(tmp_path):
    hc = _hc(tmp_path)
    hc.vpn_country = "Switzerland"
    with patch.object(health.vpn_country, "name_to_iso", return_value="CH"), \
         patch.object(health.vpn_country, "country_matches", return_value=True):
        hc.probe_vpn_country()
    assert hc.results == [("OK", "vpn-country=Switzerland", "")]


def test_vpn_country_mismatch_fails(tmp_path):
    hc = _hc(tmp_path)
    hc.vpn_country = "United Kingdom"
    with patch.object(health.vpn_country, "name_to_iso", return_value="GB"), \
         patch.object(health.vpn_country, "country_matches", return_value=False):
        hc.probe_vpn_country()
    assert hc.results[0][0] == "FAIL"
    assert hc.results[0][1] == "vpn-country=United Kingdom"


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


def test_normalize_sweep_sentinel_ok(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health.paths, "VAR_STATE", tmp_path):
        (tmp_path / "normalize-driver.done").write_text("")
        hc.probe_normalize_sweep()
    assert hc.results == [("OK", "normalize sweep complete (sentinel present)", "")]


def test_normalize_sweep_running_ok(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health.paths, "VAR_STATE", tmp_path), \
         patch.object(health, "_run", return_value=(0, "1234\n", "")):
        hc.probe_normalize_sweep()
    assert hc.results == [("OK", "normalize sweep running", "")]


def test_normalize_sweep_stale_fails(tmp_path):
    hc = _hc(tmp_path)
    with patch.object(health.paths, "VAR_STATE", tmp_path), \
         patch.object(health.paths, "VAR_LOG", tmp_path), \
         patch.object(health, "_run", return_value=(1, "", "")):
        # no driver.log -> last_tick 0 -> huge age -> stale FAIL
        hc.probe_normalize_sweep()
    assert hc.results[0][0] == "FAIL"
    assert "driver.log stale" in hc.results[0][2]


# ---------------- repo-root hygiene ----------------

def test_repo_root_clean_ok(tmp_path):
    # Only whitelisted + skipped entries present.
    (tmp_path / "AGENTS.md").write_text("")
    (tmp_path / "media_stack").mkdir()
    (tmp_path / ".mypy_cache").mkdir()      # skipped, not flagged
    (tmp_path / ".pytest_cache").mkdir()    # skipped, not flagged
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


def test_repo_root_tool_caches_not_flagged(tmp_path):
    (tmp_path / "AGENTS.md").write_text("")
    (tmp_path / ".ruff_cache").mkdir()
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "requirements-dev.txt").write_text("")
    hc = _hc(tmp_path)
    hc.probe_repo_root()
    assert hc.results[0][0] == "OK"


# ---------------- arg parsing ----------------

def test_main_unknown_arg_exit2(capsys):
    rc = health.main(["--bogus"])
    assert rc == 2
    assert "unknown arg" in capsys.readouterr().err
