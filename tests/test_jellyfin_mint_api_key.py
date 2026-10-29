"""Tests for the jellyfin-mint-api-key.py safety helper.

Mocks `urllib.request.urlopen` to exercise every main() branch with a visible side
effect: credential creation, response-shape parsing, and failure paths that could lock
the operator out.  The hyphenated script is loaded via importlib.
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


# --- _api network error handling ---

@pytest.mark.parametrize("exc", [urllib.error.URLError("conn refused"), TimeoutError("timeout")],
                         ids=["urlerror", "timeout"])
def test_api_network_failure_raises_controlled_exception(exc):
    """Network errors must surface as NetworkError so main()'s pre-flight maps them to exit 1."""
    with patch("urllib.request.urlopen", side_effect=exc):
        with pytest.raises(jf.NetworkError):
            jf._api("http://x", "/foo", "key")


@pytest.mark.parametrize("urlopen_kw, status, body", [
    ({"side_effect": _http_error(404, b"not found")}, 404, b"not found"),
    ({"return_value": _resp(200, b"ok")}, 200, b"ok"),
], ids=["http-error-surfaces-code-and-body", "success"])
def test_api_returns_status_and_body(urlopen_kw, status, body):
    with patch("urllib.request.urlopen", **urlopen_kw):
        assert jf._api("http://x", "/foo", "key") == (status, body)


def test_api_sends_mediabrowser_authorization_header():
    """Newer Jellyfin rejects the legacy X-Emby-Token header (401): _api must send `Authorization: MediaBrowser Token`."""
    captured: dict = {}

    def _capture(req, timeout=None):
        captured["req"] = req
        return _resp(200, b"ok")

    with patch("urllib.request.urlopen", side_effect=_capture):
        jf._api("http://x", "/foo", "secret-key")

    req = captured["req"]
    assert req.get_header("Authorization") == 'MediaBrowser Token="secret-key"'
    assert req.get_header("X-Emby-Token") is None


# --- list_keys response-shape handling ---

@pytest.mark.parametrize("payload, app_name", [
    ({"Items": [{"AppName": "Jellyseerr", "AccessToken": "abcd"}]}, "Jellyseerr"),
    ([{"AppName": "Old", "AccessToken": "abcd"}], "Old"),
], ids=["wrapped-items", "bare-list"])
def test_list_keys_accepts_wrapped_and_bare_list_responses(payload, app_name):
    with patch("urllib.request.urlopen", return_value=_resp(200, json.dumps(payload))):
        out = jf.list_keys("http://x", "key")
    assert len(out) == 1
    assert out[0]["AppName"] == app_name


@pytest.mark.parametrize("payload", [
    "not a list or dict",
    {"Items": None},
    {"Items": ["just-a-string", 42]},
    {"error": "something went wrong"},
    {"Items": [{"AppName": "ok", "AccessToken": "abcd"}, {"AppName": "broken"}]},
    {"Items": [{"AppName": "broken", "AccessToken": None}]},
    {"Items": [{"AppName": "broken", "AccessToken": ""}]},
    {"Items": [{"AppName": "ok", "AccessToken": "abcd"}, {"AccessToken": "abcd"}]},
    {"Items": [{"AppName": None, "AccessToken": "abcd"}]},
    {"Items": [{"AppName": "", "AccessToken": "abcd"}]},
], ids=["scalar", "null-items", "non-dict-items", "dict-without-items", "missing-access-token",
        "null-access-token", "empty-access-token", "missing-app-name", "null-app-name",
        "empty-app-name"])
def test_list_keys_raises_on_malformed_response(payload):
    """A contract violation must raise, never return [] and skip the duplicate-name check."""
    with patch("urllib.request.urlopen", return_value=_resp(200, json.dumps(payload))):
        with pytest.raises(RuntimeError):
            jf.list_keys("http://x", "key")


def test_list_keys_raises_on_non_200():
    with patch("urllib.request.urlopen", side_effect=_http_error(401, b"unauthorized")):
        with pytest.raises(RuntimeError):
            jf.list_keys("http://x", "key")


def test_list_keys_diagnostic_doesnt_leak_tokens():
    """Diagnostics must report only structural metadata: /Auth/Keys carries tokens."""
    payload = json.dumps({
        "Items": [
            {"AppName": "x", "AccessToken": "super-secret-token-must-not-leak"},
            "this-is-the-bad-entry-string-with-token-super-secret-token-must-not-leak",
        ]
    })
    with patch("urllib.request.urlopen", return_value=_resp(200, payload)):
        with pytest.raises(RuntimeError) as exc_info:
            jf.list_keys("http://x", "key")
    assert "super-secret-token-must-not-leak" not in str(exc_info.value)


# --- _redact never leaks full token ---

@pytest.mark.parametrize("token, expected", [
    ("supersecrettoken12345abcd", "…abcd"),
    ("", ""),
], ids=["last-4-chars-only", "empty"])
def test_redact(token, expected):
    assert jf._redact(token) == expected


def test_redact_short_tokens_never_reveal_full_value():
    """Short tokens (len <= 4) must never appear in full in logs; even test tokens are secrets."""
    for short in ("x", "xy", "xyz", "wxyz"):
        out = jf._redact(short)
        assert short not in out, f"short token {short!r} leaked in redacted output {out!r}"


# --- main() end-to-end with mocked urlopen ---

SYSINFO = json.dumps({"ServerName": "test-server", "Version": "10.x"})
_JELLYSEERR = {"AppName": "Jellyseerr", "AccessToken": "abcd", "DateCreated": ""}
PRE_KEYS = json.dumps({"Items": [_JELLYSEERR]})


def _fake_urlopen_sequence(responses):
    """side_effect walking `responses` in order; each is a `_resp(...)` or an exception to raise."""
    iterator = iter(responses)

    def side(req, *_a, **_kw):
        nxt = next(iterator)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    return side


def _ok():
    return _resp(200, b"{}")


def _preflight():
    return [_resp(200, SYSINFO), _resp(200, PRE_KEYS)]


def _mint_sequence(*after_list, token="newtok"):
    """Pre-flight, the mint POST (204) and a post-mint key list showing the new key, then `after_list`."""
    post_keys = json.dumps({"Items": [
        _JELLYSEERR, {"AppName": "claude-ops", "AccessToken": token, "DateCreated": ""}]})
    return [*_preflight(), _resp(204, b""), _resp(200, post_keys), *after_list]


def _auth(**user):
    return _resp(200, json.dumps({"User": {"Name": "admin-user", **user}}))


def _admin_auth(is_admin):
    return _auth(Policy={"IsAdministrator": is_admin})


def _run_main(monkeypatch, argv, responses, *, api_key="boot"):
    if api_key is None:
        monkeypatch.delenv("JELLYFIN_API_KEY", raising=False)
    else:
        monkeypatch.setenv("JELLYFIN_API_KEY", api_key)
    monkeypatch.setenv("JELLYFIN_VERIFY_USER", "admin-user")
    monkeypatch.setenv("JELLYFIN_VERIFY_PW", "pw")
    monkeypatch.setattr("sys.argv", ["jf-mint", *argv])
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen_sequence(responses)) as mock_open:
        rc = jf.main()
    return rc, mock_open


def test_main_missing_bootstrap_key_returns_4(monkeypatch, capsys):
    rc, _ = _run_main(monkeypatch, ["newkey"], [], api_key=None)
    assert rc == 4
    assert "JELLYFIN_API_KEY" in capsys.readouterr().err


def test_main_argparse_failure_uses_documented_exit_code(monkeypatch):
    """argparse's default exit code is 2, but the script documents 4 for usage errors."""
    rc, _ = _run_main(monkeypatch, [], [])
    assert rc == 4


@pytest.mark.parametrize("name", [" Jellyseerr ", "Jellyseerr"],
                         ids=["padded-name-normalized", "exact-name"])
def test_main_duplicate_name_refused_returns_4(monkeypatch, capsys, name):
    """A pre-existing key with the same name aborts (no shadowing duplicate), even when padded."""
    rc, _ = _run_main(monkeypatch, [name], _preflight())
    assert rc == 4
    assert "already exists" in capsys.readouterr().err


def test_main_dry_run_returns_0_no_mint(monkeypatch):
    """--dry-run must not call POST /Auth/Keys (urlopen runs twice: sysinfo + list)."""
    rc, mock_open = _run_main(monkeypatch, ["--dry-run", "newkey"], _preflight())
    assert rc == 0
    assert mock_open.call_count == 2


@pytest.mark.parametrize("responses, rc, err_has", [
    pytest.param(lambda: [_resp(200, b"not json at all")], 1, ["FATAL"],
                 id="preflight-invalid-json"),
    pytest.param(lambda: [_resp(200, SYSINFO), _http_error(401, b"unauthorized")], 1, ["FATAL"],
                 id="preflight-listkeys-http-error"),
    pytest.param(lambda: [_resp(200, SYSINFO), _resp(200, json.dumps("not a list or dict"))], 1,
                 ["FATAL"], id="preflight-listkeys-bad-shape"),
    pytest.param(lambda: [urllib.error.URLError("refused")], 1, ["FATAL"],
                 id="preflight-network-failure"),
    pytest.param(lambda: [*_preflight(), _resp(204, b""), _resp(200, PRE_KEYS)], 2,
                 ["expected exactly 1 new key"], id="mint-clobber-detection"),
    pytest.param(lambda: [*_preflight(), _resp(204, b""), _resp(200, b"truncated json{{{")], 2,
                 ["FATAL", "MAY have landed"], id="post-mint-list-invalid-json"),
    pytest.param(lambda: [*_preflight(), _resp(204, b""),
                          urllib.error.URLError("dns died right after")], 2, ["FATAL"],
                 id="post-mint-list-network-failure"),
    pytest.param(lambda: _mint_sequence(urllib.error.URLError("connection reset during verify")),
                 3, ["FATAL"], id="post-mint-verify-network-failure"),
    pytest.param(lambda: _mint_sequence(_ok(), _http_error(401, b"")), 3,
                 ["Jellyseerr", "STOPPED"], id="pre-existing-key-broke"),
    pytest.param(lambda: _mint_sequence(_ok(), _ok(), _http_error(401, b"")), 3,
                 ["login as 'admin-user'"], id="admin-login-failure"),
    pytest.param(lambda: _mint_sequence(_ok(), _ok(), _admin_auth("false")), 3,
                 ["admin policy was disrupted"], id="admin-string-false-not-strict-true"),
    pytest.param(lambda: _mint_sequence(_ok(), _ok(), _admin_auth(False)), 3,
                 ["admin policy was disrupted"], id="login-ok-but-not-admin"),
    pytest.param(lambda: _mint_sequence(_ok(), _ok(), _auth()), 3,
                 ["admin policy was disrupted"], id="admin-missing-policy"),
])
def test_main_failure_exit_codes(monkeypatch, capsys, responses, rc, err_has):
    got, _ = _run_main(monkeypatch, ["claude-ops"], responses())
    assert got == rc
    err = capsys.readouterr().err
    for needle in err_has:
        assert needle in err


def test_main_login_admin_diag_doesnt_echo_response_value(monkeypatch, capsys):
    """The admin FAIL diagnostic reports the value's type only: it flows to stderr, then a Telegram alert."""
    leak_sentinel = "TOKEN-LIKE-VALUE-MUST-NOT-LEAK-TO-STDERR-1234"
    rc, _ = _run_main(monkeypatch, ["claude-ops"],
                      _mint_sequence(_ok(), _ok(), _admin_auth(leak_sentinel)))
    assert rc == 3
    captured = capsys.readouterr()
    assert leak_sentinel not in captured.err, (
        f"response-injected value leaked into stderr: {captured.err!r}"
    )
    assert leak_sentinel not in captured.out


def test_main_success_prints_new_token_exactly_once(monkeypatch, capsys):
    """Happy path: the new token reaches stdout exactly once; stderr only ever sees the redacted form."""
    new_token = "newrealtoken123456789xyz"
    rc, _ = _run_main(monkeypatch, ["claude-ops"],
                      _mint_sequence(_ok(), _ok(), _admin_auth(True), token=new_token))
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.count(new_token) == 1
    assert new_token not in captured.err
    # last 4 chars of the new token: proves _redact ran on it
    assert "…9xyz" in captured.err
