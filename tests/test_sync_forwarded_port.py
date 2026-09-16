"""Tests for sync-forwarded-port.py — the ProtonVPN forwarded-port reconciler.

codex verification review: the client wrappers were covered but the reconciler's
own branches were not, and a bug in those branches writes a wrong listen port
into qBittorrent.  No real docker or qBittorrent is touched; both are mocked.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_spec = importlib.util.spec_from_file_location(
    "sync_forwarded_port",
    Path(__file__).resolve().parent.parent / "sync-forwarded-port.py")
sfp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sfp)


def _docker(stdout="", rc=0):
    return MagicMock(returncode=rc, stdout=stdout, stderr="")


# --- forwarded_port() parsing -------------------------------------------------

def test_reads_bare_port_shape():
    with patch.object(sfp.subprocess, "run", return_value=_docker('{"port":51820}')):
        assert sfp.forwarded_port() == 51820


def test_reads_ports_list_shape():
    with patch.object(sfp.subprocess, "run", return_value=_docker('{"ports":[4242]}')):
        assert sfp.forwarded_port() == 4242


def test_no_allocation_is_zero_not_an_error():
    with patch.object(sfp.subprocess, "run", return_value=_docker('{"port":0,"ports":[]}')):
        assert sfp.forwarded_port() == 0


def test_docker_failure_returns_none():
    with patch.object(sfp.subprocess, "run", return_value=_docker("", rc=6)):
        assert sfp.forwarded_port() is None


def test_docker_timeout_returns_none():
    with patch.object(sfp.subprocess, "run",
                      side_effect=subprocess.TimeoutExpired("docker", 60)):
        assert sfp.forwarded_port() is None


def test_non_json_returns_none():
    with patch.object(sfp.subprocess, "run", return_value=_docker("401 Unauthorized")):
        assert sfp.forwarded_port() is None


@pytest.mark.parametrize("body", [
    '{"port":null,"ports":null}',   # explicit nulls
    '{"port":"8080"}',              # string, not int
    '{"port":1.5}',                 # fractional
    '{"port":70000}',               # out of range
    '{"port":-1}',                  # negative
    '[1,2,3]',                      # array instead of object
])
def test_malformed_port_values_rejected(body):
    """A bad value would be written straight into qBittorrent's listen port."""
    with patch.object(sfp.subprocess, "run", return_value=_docker(body)):
        assert sfp.forwarded_port() is None


def test_true_is_not_accepted_as_a_port():
    """bool is an int subclass in Python; it must not pass as port 1."""
    with patch.object(sfp.subprocess, "run", return_value=_docker('{"port":true}')):
        assert sfp.forwarded_port() is None


# --- main() reconciliation ----------------------------------------------------

def _run(port, listen_port, argv=(), set_result=None, login=True):
    """Drive main() with forwarded_port and QBitClient mocked."""
    client = MagicMock()
    client.login.return_value = login
    seen = {"listen_port": listen_port}
    client.preferences.side_effect = lambda: dict(seen)

    def _set(values):
        if set_result == "ignored":
            return
        if set_result == "raise":
            raise RuntimeError("boom")
        seen.update(values)
    client.set_preferences.side_effect = _set

    with patch.object(sfp, "forwarded_port", return_value=port), \
         patch.object(sfp, "QBitClient", return_value=client), \
         patch.object(sfp, "load_env_file", return_value={}), \
         patch.object(sys, "argv", ["sync-forwarded-port.py", *argv]):
        code = sfp.main()
    return code, client, seen


def test_unreadable_port_exits_1_without_touching_qbittorrent():
    code, client, _ = _run(None, 54321)
    assert code == 1
    client.login.assert_not_called()


def test_no_allocation_exits_0_without_touching_qbittorrent():
    code, client, _ = _run(0, 54321)
    assert code == 0
    client.login.assert_not_called()


def test_already_in_sync_writes_nothing():
    code, client, _ = _run(4242, 4242)
    assert code == 0
    client.set_preferences.assert_not_called()


def test_mismatch_updates_listen_port():
    code, client, seen = _run(4242, 54321)
    assert code == 0
    client.set_preferences.assert_called_once_with({"listen_port": 4242})
    assert seen["listen_port"] == 4242


def test_dry_run_reports_but_writes_nothing():
    code, client, seen = _run(4242, 54321, argv=("--dry-run",))
    assert code == 0
    client.set_preferences.assert_not_called()
    assert seen["listen_port"] == 54321


def test_silently_ignored_write_is_caught():
    """setPreferences answers 200 even when it ignores the body, so a write that
    did not take must fail loudly rather than be reported as success."""
    code, _client, seen = _run(4242, 54321, set_result="ignored")
    assert code == 2
    assert seen["listen_port"] == 54321


def test_login_failure_exits_2():
    code, client, _ = _run(4242, 54321, login=False)
    assert code == 2
    client.set_preferences.assert_not_called()


def test_set_preferences_exception_exits_2():
    code, _client, _ = _run(4242, 54321, set_result="raise")
    assert code == 2
