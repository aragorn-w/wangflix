"""Tests for the jellyfin-mint-api-key.py safety helper.

The script handles credential creation, response-shape parsing, and
several failure paths that lock-the-operator-out if wrong.  Codex
round 1 #3 flagged the missing coverage; this file mocks
`urllib.request.urlopen` to exercise every branch that has a
visible side effect from the main() entrypoint's perspective.

We load the script via importlib (hyphenated filename = not a normal
module) — same pattern as test_audio_selection.py.
"""

import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "jf_mint", str(PROJECT_ROOT / "jellyfin-mint-api-key.py")
)
jf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jf)


# --- helpers ---

def _resp(status: int, body: bytes | str = b""):
    """Build a fake urlopen context manager that yields one read()."""
    if isinstance(body, str):
        body = body.encode()
    cm = MagicMock()
    fake = MagicMock()
    fake.status = status
    fake.read.return_value = body
    cm.__enter__.return_value = fake
    cm.__exit__.return_value = False
    return cm


def _http_error(code: int, body: bytes = b""):
    """Build an HTTPError exception in the same shape urlopen raises."""
    return urllib.error.HTTPError(
        url="http://x", code=code, msg="err", hdrs=None, fp=io.BytesIO(body)
    )


# --- _api network error handling (codex round 1 #2) ---

def test_api_network_failure_raises_controlled_exception():
    """URLError/TimeoutError/OSError must NOT raise out of _api() —
    they must be caught and surfaced as a controlled exception so
    main()'s pre-flight can map them to exit code 1."""
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("conn refused")):
        with pytest.raises(jf.NetworkError):
            jf._api("http://x", "/foo", "key")


def test_api_timeout_raises_controlled_exception():
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timeout")):
        with pytest.raises(jf.NetworkError):
            jf._api("http://x", "/foo", "key")


def test_api_returns_http_error_status_and_body():
    """HTTPError (non-2xx with body) must surface as (code, body) so
    callers can branch on the code without losing the response."""
    with patch("urllib.request.urlopen", side_effect=_http_error(404, b"not found")):
        status, body = jf._api("http://x", "/foo", "key")
    assert status == 404
    assert body == b"not found"


def test_api_success_returns_status_and_body():
    with patch("urllib.request.urlopen", return_value=_resp(200, b"ok")):
        status, body = jf._api("http://x", "/foo", "key")
    assert status == 200
    assert body == b"ok"


# --- list_keys response-shape handling (codex round 1 #1) ---

def test_list_keys_handles_wrapped_items_response():
    """Standard Jellyfin shape: {"Items":[...]}"""
    payload = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd"},
    ]})
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        out = jf.list_keys("http://x", "key")
    assert len(out) == 1
    assert out[0]["AppName"] == "Jellyseerr"


def test_list_keys_handles_bare_list_response():
    """REGRESSION codex round 1 #1: `.get('Items', ...)` on a bare
    list raises AttributeError before the fallback runs.  Some
    Jellyfin versions / proxies return the bare list directly; the
    helper must handle both."""
    payload = json.dumps([
        {"AppName": "Old", "AccessToken": "abcd"},
    ])
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        out = jf.list_keys("http://x", "key")
    assert len(out) == 1
    assert out[0]["AppName"] == "Old"


def test_list_keys_raises_on_unexpected_shape():
    """A scalar / string / number response is a server contract
    violation, not a missing-Items case — must raise, not silently
    return []."""
    payload = json.dumps("not a list or dict")
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        with pytest.raises(RuntimeError):
            jf.list_keys("http://x", "key")


def test_list_keys_raises_on_null_items():
    """REGRESSION codex round 4 #4: {"Items": null} would set pre_keys
    to None and crash later in main() with TypeError on len()/iteration.
    list_keys must validate Items IS a list."""
    payload = json.dumps({"Items": None})
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        with pytest.raises(RuntimeError):
            jf.list_keys("http://x", "key")


def test_list_keys_raises_on_non_dict_items():
    """Each entry must be a dict so downstream `.get('AppName')` /
    `.get('AccessToken')` works.  A list of strings or numbers would
    crash with AttributeError at first iteration."""
    payload = json.dumps({"Items": ["just-a-string", 42]})
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        with pytest.raises(RuntimeError):
            jf.list_keys("http://x", "key")


def test_list_keys_raises_on_non_200():
    with patch("urllib.request.urlopen", side_effect=_http_error(401, b"unauthorized")):
        with pytest.raises(RuntimeError):
            jf.list_keys("http://x", "key")


# --- _redact never leaks full token ---

def test_redact_shows_only_last_4_chars():
    assert jf._redact("supersecrettoken12345abcd") == "…abcd"


def test_redact_short_tokens_never_reveal_full_value():
    """Codex round 2 #5: short tokens (len ≤ 4) used to surface the
    full value prefixed with `…`.  Tokens shouldn't ever appear in
    full in logs, even short ones (test/dev tokens that might leak
    into stderr snapshots are still secrets)."""
    for short in ("x", "xy", "xyz", "wxyz"):
        out = jf._redact(short)
        assert short not in out, f"short token {short!r} leaked in redacted output {out!r}"


def test_redact_empty_returns_empty():
    assert jf._redact("") == ""


# --- main() end-to-end with mocked urlopen ---

def _fake_urlopen_sequence(responses):
    """Return a side_effect callable that walks through `responses`
    in order — each entry can be a urlopen-style context manager (use
    `_resp(...)`) or an exception to raise."""
    iterator = iter(responses)

    def side(req, *_a, **_kw):
        nxt = next(iterator)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    return side


def test_main_missing_bootstrap_key_returns_4(monkeypatch, capsys):
    monkeypatch.delenv("JELLYFIN_API_KEY", raising=False)
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "u")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "p")
    monkeypatch.setattr("sys.argv", ["jf-mint", "newkey"])
    rc = jf.main()
    assert rc == 4
    assert "JELLYFIN_API_KEY" in capsys.readouterr().err


def test_main_preflight_invalid_json_returns_1(monkeypatch, capsys):
    """Codex round 3 #1: /System/Info returning HTTP 200 with non-JSON
    body must surface as rc=1 with a FATAL diagnostic, not crash with
    a JSONDecodeError traceback before mint runs."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "u")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "p")
    monkeypatch.setattr("sys.argv", ["jf-mint", "newkey"])
    with patch("urllib.request.urlopen", return_value=_resp(200, b"not json at all")):
        rc = jf.main()
    assert rc == 1
    assert "FATAL" in capsys.readouterr().err


def test_main_preflight_listkeys_http_error_returns_1(monkeypatch, capsys):
    """Codex round 3 #1: /Auth/Keys returning HTTP 401 raises RuntimeError
    from list_keys; main() must catch it and return 1, not propagate."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "u")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "p")
    monkeypatch.setattr("sys.argv", ["jf-mint", "newkey"])
    sysinfo = json.dumps({"ServerName": "test-server", "Version": "10.x"})
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_sequence([
        _resp(200, sysinfo),
        _http_error(401, b"unauthorized"),
    ])):
        rc = jf.main()
    assert rc == 1
    assert "FATAL" in capsys.readouterr().err


def test_main_preflight_listkeys_bad_shape_returns_1(monkeypatch, capsys):
    """Codex round 3 #1: /Auth/Keys returning a scalar instead of
    list/dict raises RuntimeError; main must catch + map to rc=1."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "u")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "p")
    monkeypatch.setattr("sys.argv", ["jf-mint", "newkey"])
    sysinfo = json.dumps({"ServerName": "test-server", "Version": "10.x"})
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_sequence([
        _resp(200, sysinfo),
        _resp(200, json.dumps("not a list or dict")),
    ])):
        rc = jf.main()
    assert rc == 1
    assert "FATAL" in capsys.readouterr().err


def test_main_whitespace_in_name_normalized_for_duplicate_check(monkeypatch, capsys):
    """REGRESSION codex round 4 #5: name was checked stripped for
    empty-check but used UNTRIMMED for duplicate detection.  A name
    like `' Jellyseerr '` would bypass the duplicate check and create
    a confusing near-duplicate.  Must strip once at parse and use
    the normalized value everywhere."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "u")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "p")
    monkeypatch.setattr("sys.argv", ["jf-mint", " Jellyseerr "])  # padded
    sysinfo = json.dumps({"ServerName": "test-server", "Version": "10.x"})
    keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
    ]})
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_sequence([
        _resp(200, sysinfo),
        _resp(200, keys),
    ])):
        rc = jf.main()
    assert rc == 4
    assert "already exists" in capsys.readouterr().err


def test_main_argparse_failure_uses_documented_exit_code(monkeypatch, capsys):
    """Codex round 3 #4: argparse defaults to exit code 2 on usage
    errors, but the script header documents 4.  Missing required
    arg or unknown option should surface as rc=4."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "u")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "p")
    # Missing required positional arg
    monkeypatch.setattr("sys.argv", ["jf-mint"])
    rc = jf.main()
    assert rc == 4


def test_main_preflight_network_failure_returns_1(monkeypatch, capsys):
    """Codex round 1 #2: a connection refused on the bootstrap
    /System/Info check must map to exit code 1, not bubble up as
    an uncontrolled traceback."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "u")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "p")
    monkeypatch.setattr("sys.argv", ["jf-mint", "newkey"])
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        rc = jf.main()
    assert rc == 1
    assert "FATAL" in capsys.readouterr().err


def test_main_duplicate_name_refused_returns_4(monkeypatch, capsys):
    """Pre-existing key with the same name must abort with rc=4 —
    refusing to add a duplicate that would shadow without UI
    distinction."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "u")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "p")
    monkeypatch.setattr("sys.argv", ["jf-mint", "Jellyseerr"])
    sysinfo = json.dumps({"ServerName": "test-server", "Version": "10.x"})
    keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
    ]})
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_sequence([
        _resp(200, sysinfo),
        _resp(200, keys),
    ])):
        rc = jf.main()
    assert rc == 4
    assert "already exists" in capsys.readouterr().err


def test_main_dry_run_returns_0_no_mint(monkeypatch, capsys):
    """--dry-run must not call POST /Auth/Keys.  We verify by
    asserting urlopen was called exactly twice (sysinfo + list)."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "u")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "p")
    monkeypatch.setattr("sys.argv", ["jf-mint", "--dry-run", "newkey"])
    sysinfo = json.dumps({"ServerName": "test-server", "Version": "10.x"})
    keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
    ]})
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_sequence([
        _resp(200, sysinfo),
        _resp(200, keys),
    ])) as mock_open:
        rc = jf.main()
    assert rc == 0
    assert mock_open.call_count == 2  # only the 2 pre-flight calls; no mint


def test_main_mint_clobber_detection_returns_2(monkeypatch, capsys):
    """If POST /Auth/Keys returns 2xx but the post-list doesn't
    surface exactly 1 new key with the expected name (e.g. another
    client raced and added a different key, OR Jellyfin silently
    dropped the mint), abort with rc=2 — never claim success."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "u")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "p")
    monkeypatch.setattr("sys.argv", ["jf-mint", "claude-ops"])
    sysinfo = json.dumps({"ServerName": "test-server", "Version": "10.x"})
    pre_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
    ]})
    # post-list returns NO new key — mint silently failed
    post_keys = pre_keys
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_sequence([
        _resp(200, sysinfo),
        _resp(200, pre_keys),
        _resp(204, b""),       # POST /Auth/Keys returned 204
        _resp(200, post_keys),  # but post-list is unchanged
    ])):
        rc = jf.main()
    assert rc == 2
    assert "expected exactly 1 new key" in capsys.readouterr().err


def test_main_pre_existing_key_broke_returns_3(monkeypatch, capsys):
    """If a pre-existing key STOPPED working after the mint (rate
    limit, weird DB constraint), abort with rc=3 — the operator
    needs to know they may have just locked out Jellyseerr."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "u")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "p")
    monkeypatch.setattr("sys.argv", ["jf-mint", "claude-ops"])
    sysinfo = json.dumps({"ServerName": "test-server", "Version": "10.x"})
    pre_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
    ]})
    post_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
        {"AppName": "claude-ops", "AccessToken": "newtok", "DateCreated": ""},
    ]})
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_sequence([
        _resp(200, sysinfo),
        _resp(200, pre_keys),
        _resp(204, b""),
        _resp(200, post_keys),
        _resp(200, b'{}'),     # new key verify OK
        _http_error(401, b""),  # pre-existing Jellyseerr key now FAILS
    ])):
        rc = jf.main()
    assert rc == 3
    err = capsys.readouterr().err
    assert "Jellyseerr" in err and "STOPPED" in err


def test_main_login_admin_diag_doesnt_echo_response_value(monkeypatch, capsys):
    """REGRESSION codex round-debloat #1: the admin verification FAIL
    diagnostic previously printed `policy.get('IsAdministrator')!r`
    directly.  That field is derived from a service response body on
    an error path — same risk class as the round-6 #5 cleanup that
    stopped echoing /Auth/Keys bodies because they may contain
    tokens.  A malicious / proxy-injected response could plant a
    token-like string in IsAdministrator and have us write it to
    stderr → ops/healthcheck-alert.sh → Telegram message body.  The
    diagnostic must report only structural metadata (type name),
    never the raw value."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "admin-user")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "pw")
    monkeypatch.setattr("sys.argv", ["jf-mint", "claude-ops"])
    sysinfo = json.dumps({"ServerName": "test-server", "Version": "10.x"})
    pre_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
    ]})
    post_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
        {"AppName": "claude-ops", "AccessToken": "newtok", "DateCreated": ""},
    ]})
    # Plant a token-like string in IsAdministrator — the diagnostic
    # must NOT include this string anywhere.
    leak_sentinel = "TOKEN-LIKE-VALUE-MUST-NOT-LEAK-TO-STDERR-1234"
    spoofed_auth = json.dumps({
        "User": {"Name": "admin-user",
                 "Policy": {"IsAdministrator": leak_sentinel}}
    })
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_sequence([
        _resp(200, sysinfo),
        _resp(200, pre_keys),
        _resp(204, b""),
        _resp(200, post_keys),
        _resp(200, b'{}'),
        _resp(200, b'{}'),
        _resp(200, spoofed_auth),
    ])):
        rc = jf.main()
    assert rc == 3
    captured = capsys.readouterr()
    assert leak_sentinel not in captured.err, (
        f"response-injected value leaked into stderr: {captured.err!r}"
    )
    assert leak_sentinel not in captured.out


def test_main_login_admin_string_false_returns_3(monkeypatch, capsys):
    """REGRESSION codex round 6 #2: `bool("false")` is True (any
    non-empty string is truthy in Python).  A spoofed/garbled auth
    response with IsAdministrator="false" would have PASSED the
    admin check under the previous `bool(...)` coercion.  Must
    require strict `is True`."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "admin-user")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "pw")
    monkeypatch.setattr("sys.argv", ["jf-mint", "claude-ops"])
    sysinfo = json.dumps({"ServerName": "test-server", "Version": "10.x"})
    pre_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
    ]})
    new_token = "newtok"
    post_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
        {"AppName": "claude-ops", "AccessToken": new_token, "DateCreated": ""},
    ]})
    # IsAdministrator is the STRING "false" — truthy under bool(), not True
    spoofed_auth = json.dumps({
        "User": {"Name": "admin-user", "Policy": {"IsAdministrator": "false"}}
    })
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_sequence([
        _resp(200, sysinfo),
        _resp(200, pre_keys),
        _resp(204, b""),
        _resp(200, post_keys),
        _resp(200, b'{}'),
        _resp(200, b'{}'),
        _resp(200, spoofed_auth),
    ])):
        rc = jf.main()
    assert rc == 3
    err = capsys.readouterr().err.lower()
    assert "admin" in err


def test_main_login_admin_missing_policy_returns_3(monkeypatch, capsys):
    """Missing User.Policy entirely → not admin → rc=3."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "admin-user")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "pw")
    monkeypatch.setattr("sys.argv", ["jf-mint", "claude-ops"])
    sysinfo = json.dumps({"ServerName": "test-server", "Version": "10.x"})
    pre_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
    ]})
    post_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
        {"AppName": "claude-ops", "AccessToken": "newtok", "DateCreated": ""},
    ]})
    no_policy_auth = json.dumps({"User": {"Name": "admin-user"}})
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_sequence([
        _resp(200, sysinfo),
        _resp(200, pre_keys),
        _resp(204, b""),
        _resp(200, post_keys),
        _resp(200, b'{}'),
        _resp(200, b'{}'),
        _resp(200, no_policy_auth),
    ])):
        rc = jf.main()
    assert rc == 3


def test_main_login_succeeds_but_no_admin_returns_3(monkeypatch, capsys):
    """Codex round 2 #3: the verification user could still authenticate
    (HTTP 200) but lose IsAdministrator — the script's stated purpose is
    "admin auth still works", so a non-admin success must FAIL the check.
    This is the exact lockout-recovery scenario the helper exists to
    prevent."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "admin-user")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "pw")
    monkeypatch.setattr("sys.argv", ["jf-mint", "claude-ops"])
    sysinfo = json.dumps({"ServerName": "test-server", "Version": "10.x"})
    pre_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
    ]})
    new_token = "newtok"
    post_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
        {"AppName": "claude-ops", "AccessToken": new_token, "DateCreated": ""},
    ]})
    # auth response shows User.Policy.IsAdministrator = FALSE
    non_admin_auth = json.dumps({
        "User": {"Name": "admin-user", "Policy": {"IsAdministrator": False}}
    })
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_sequence([
        _resp(200, sysinfo),
        _resp(200, pre_keys),
        _resp(204, b""),
        _resp(200, post_keys),
        _resp(200, b'{}'),  # new key verify
        _resp(200, b'{}'),  # Jellyseerr verify
        _resp(200, non_admin_auth),  # login OK but IsAdministrator=False
    ])):
        rc = jf.main()
    assert rc == 3
    err = capsys.readouterr().err
    assert "admin" in err.lower()


def test_main_post_mint_network_failure_returns_3(monkeypatch, capsys):
    """Codex round 2 #2: network failure DURING post-mint verification
    must map to exit code 3, not crash uncontrolled.  The mint already
    landed at that point — operator needs the controlled rc=3 +
    diagnostic to investigate."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "admin-user")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "pw")
    monkeypatch.setattr("sys.argv", ["jf-mint", "claude-ops"])
    sysinfo = json.dumps({"ServerName": "test-server", "Version": "10.x"})
    pre_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
    ]})
    post_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
        {"AppName": "claude-ops", "AccessToken": "newtok", "DateCreated": ""},
    ]})
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_sequence([
        _resp(200, sysinfo),
        _resp(200, pre_keys),
        _resp(204, b""),
        _resp(200, post_keys),
        urllib.error.URLError("connection reset during verify"),
    ])):
        rc = jf.main()
    assert rc == 3
    assert "FATAL" in capsys.readouterr().err


def test_main_post_mint_invalid_json_returns_2(monkeypatch, capsys):
    """Codex round 5 #1: round-3 fix added ValueError catch in
    PRE-flight, but the mint/post-list block only caught NetworkError
    + RuntimeError.  Non-JSON / truncated JSON from /Auth/Keys after
    the POST succeeds would still escape uncontrolled.  Must map to
    rc=2 with the "key MAY have landed" diagnostic."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "u")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "p")
    monkeypatch.setattr("sys.argv", ["jf-mint", "claude-ops"])
    sysinfo = json.dumps({"ServerName": "test-server", "Version": "10.x"})
    pre_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
    ]})
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_sequence([
        _resp(200, sysinfo),
        _resp(200, pre_keys),
        _resp(204, b""),  # POST succeeded
        _resp(200, b"truncated json{{{"),  # but post-list is broken
    ])):
        rc = jf.main()
    assert rc == 2
    err = capsys.readouterr().err
    assert "FATAL" in err
    assert "MAY have landed" in err


def test_list_keys_raises_on_missing_access_token():
    """REGRESSION codex round 5 #5: entries with missing/null
    AccessToken would crash later code at `new_keys[0]["AccessToken"]`
    or pass `None` into request headers.  list_keys must validate
    every entry has a non-empty AccessToken string."""
    payload = json.dumps({"Items": [
        {"AppName": "ok", "AccessToken": "abcd"},
        {"AppName": "broken"},  # missing AccessToken entirely
    ]})
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        with pytest.raises(RuntimeError):
            jf.list_keys("http://x", "key")


def test_list_keys_raises_on_null_access_token():
    payload = json.dumps({"Items": [
        {"AppName": "broken", "AccessToken": None},
    ]})
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        with pytest.raises(RuntimeError):
            jf.list_keys("http://x", "key")


def test_list_keys_raises_on_missing_app_name():
    """REGRESSION codex round 7 #1: list_keys validated AccessToken
    but not AppName.  Duplicate detection in main() compares against
    AppName — a missing/null/empty AppName lets a key with that
    name shadow an existing one without triggering the duplicate
    check (or worse, lets the new mint go through despite the
    operator having asked for a name already in use).  Must
    validate AppName too."""
    payload = json.dumps({"Items": [
        {"AppName": "ok", "AccessToken": "abcd"},
        {"AccessToken": "abcd"},  # missing AppName entirely
    ]})
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        with pytest.raises(RuntimeError):
            jf.list_keys("http://x", "key")


def test_list_keys_raises_on_null_app_name():
    payload = json.dumps({"Items": [
        {"AppName": None, "AccessToken": "abcd"},
    ]})
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        with pytest.raises(RuntimeError):
            jf.list_keys("http://x", "key")


def test_list_keys_raises_on_empty_app_name():
    payload = json.dumps({"Items": [
        {"AppName": "", "AccessToken": "abcd"},
    ]})
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        with pytest.raises(RuntimeError):
            jf.list_keys("http://x", "key")


def test_list_keys_raises_on_dict_without_items():
    """REGRESSION codex round 6 #1: a dict response WITHOUT Items
    (e.g. `{"error": "..."}` or `{"TotalRecordCount": 0}` from some
    proxy) previously returned `[]` via .get's default, silently
    skipping pre-existing key snapshot and bypassing duplicate-name
    detection.  Must require Items to be present when the response
    is a dict."""
    payload = json.dumps({"error": "something went wrong"})
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        with pytest.raises(RuntimeError):
            jf.list_keys("http://x", "key")


def test_list_keys_diagnostic_doesnt_leak_tokens():
    """Codex round 6 #5: diagnostic strings used `str(items)[:200]`
    which could include AccessToken values if the malformed shape
    happened to contain them.  /Auth/Keys carries tokens — error
    messages must report only structural metadata, never payload
    fragments."""
    # Bad shape that legitimately carries a token in the values
    payload = json.dumps({
        "Items": [
            {"AppName": "x", "AccessToken": "super-secret-token-must-not-leak"},
            "this-is-the-bad-entry-string-with-token-super-secret-token-must-not-leak",
        ]
    })
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        with pytest.raises(RuntimeError) as exc_info:
            jf.list_keys("http://x", "key")
    msg = str(exc_info.value)
    # The token must NOT appear in the error message
    assert "super-secret-token-must-not-leak" not in msg


def test_list_keys_raises_on_empty_access_token():
    payload = json.dumps({"Items": [
        {"AppName": "broken", "AccessToken": ""},
    ]})
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        with pytest.raises(RuntimeError):
            jf.list_keys("http://x", "key")


def test_main_post_mint_listkeys_network_failure_returns_2(monkeypatch, capsys):
    """If the mint POST succeeded but the post-mint LIST fails on
    network error, we can't confirm the key landed — exit code 2
    (mint failed/unverifiable) + clear diagnostic."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "u")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "p")
    monkeypatch.setattr("sys.argv", ["jf-mint", "claude-ops"])
    sysinfo = json.dumps({"ServerName": "test-server", "Version": "10.x"})
    pre_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
    ]})
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_sequence([
        _resp(200, sysinfo),
        _resp(200, pre_keys),
        _resp(204, b""),  # mint POST succeeded
        urllib.error.URLError("dns died right after"),  # post-list fails
    ])):
        rc = jf.main()
    assert rc == 2
    assert "FATAL" in capsys.readouterr().err


def test_main_login_test_failure_returns_3(monkeypatch, capsys):
    """Post-mint admin login test failure must surface as rc=3 —
    catches policy disruption (rare but exactly the lockout scenario
    we built this script to prevent)."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "admin-user")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "pw")
    monkeypatch.setattr("sys.argv", ["jf-mint", "claude-ops"])
    sysinfo = json.dumps({"ServerName": "test-server", "Version": "10.x"})
    pre_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
    ]})
    post_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
        {"AppName": "claude-ops", "AccessToken": "newtok", "DateCreated": ""},
    ]})
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_sequence([
        _resp(200, sysinfo),
        _resp(200, pre_keys),
        _resp(204, b""),
        _resp(200, post_keys),
        _resp(200, b'{}'),
        _resp(200, b'{}'),         # Jellyseerr still works
        _http_error(401, b""),     # but login as admin-user FAILS
    ])):
        rc = jf.main()
    assert rc == 3
    err = capsys.readouterr().err
    assert "login as 'admin-user'" in err


def test_main_success_prints_new_token_exactly_once(monkeypatch, capsys):
    """Happy path: rc=0 + new token printed to stdout exactly once
    with the capture marker.  Nothing else (stderr is allowed to
    carry redacted snapshots) prints the full token."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "boot")
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "admin-user")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "pw")
    monkeypatch.setattr("sys.argv", ["jf-mint", "claude-ops"])
    sysinfo = json.dumps({"ServerName": "test-server", "Version": "10.x"})
    pre_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
    ]})
    new_token = "newrealtoken123456789xyz"
    post_keys = json.dumps({"Items": [
        {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""},
        {"AppName": "claude-ops", "AccessToken": new_token, "DateCreated": ""},
    ]})
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_sequence([
        _resp(200, sysinfo),
        _resp(200, pre_keys),
        _resp(204, b""),
        _resp(200, post_keys),
        _resp(200, b'{}'),
        _resp(200, b'{}'),
        _resp(200, b'{"User":{"Name":"admin-user","Policy":{"IsAdministrator":true}}}'),
    ])):
        rc = jf.main()
    captured = capsys.readouterr()
    assert rc == 0
    # Full token appears EXACTLY ONCE in stdout
    assert captured.out.count(new_token) == 1
    # Full token MUST NOT appear in stderr (the snapshot/log channel)
    assert new_token not in captured.err
    # But the redacted form (last-4 chars of "...789xyz" = "9xyz") appears
    # in stderr snapshots — proves _redact actually ran on the new token
    assert "…9xyz" in captured.err
