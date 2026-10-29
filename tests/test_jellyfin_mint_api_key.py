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


# --- _redact never leaks full token ---

@pytest.mark.parametrize("token, expected", [
    ("supersecrettoken12345abcd", "…abcd"),
    ("", ""),
], ids=["last-4-chars-only", "empty"])
def test_redact(token, expected):
    assert jf._redact(token) == expected


def test_redact_short_tokens_never_reveal_full_value():
    """Short tokens (len <= 4) must never appear in full in logs; even test tokens are secrets."""
    for short in ("x", "wxyz"):
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
    pytest.param(lambda: [_resp(200, SYSINFO), _http_error(401, b"unauthorized")], 1, ["FATAL"],
                 id="preflight-listkeys-http-error"),
    pytest.param(lambda: [urllib.error.URLError("refused")], 1, ["FATAL"],
                 id="preflight-network-failure"),
    pytest.param(lambda: [*_preflight(), _resp(204, b""), _resp(200, PRE_KEYS)], 2,
                 ["expected exactly 1 new key"], id="mint-clobber-detection"),
    pytest.param(lambda: [*_preflight(), _resp(204, b""),
                          urllib.error.URLError("dns died right after")], 2, ["FATAL"],
                 id="post-mint-list-network-failure"),
    pytest.param(lambda: _mint_sequence(_ok(), _http_error(401, b"")), 3,
                 ["Jellyseerr", "STOPPED"], id="pre-existing-key-broke"),
    pytest.param(lambda: _mint_sequence(_ok(), _ok(), _http_error(401, b"")), 3,
                 ["login as 'admin-user'"], id="admin-login-failure"),
    pytest.param(lambda: _mint_sequence(_ok(), _ok(), _admin_auth(False)), 3,
                 ["admin policy was disrupted"], id="login-ok-but-not-admin"),
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
