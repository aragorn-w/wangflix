"""Tests for sync-forwarded-port.py, the ProtonVPN forwarded-port reconciler.

A bug in its branches writes a wrong listen port into qBittorrent.  Docker and
qBittorrent are mocked.
"""

import importlib.util
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

@pytest.mark.parametrize("run_kwargs, expected", [
    pytest.param({"return_value": _docker('{"port":51820}')}, 51820, id="bare-port-shape"),
    pytest.param({"return_value": _docker('{"ports":[4242]}')}, 4242, id="ports-list-shape"),
    pytest.param({"return_value": _docker('{"port":0,"ports":[]}')}, 0, id="no-allocation-is-zero-not-an-error"),
    pytest.param({"return_value": _docker("", rc=6)}, None, id="docker-failure"),
    pytest.param({"return_value": _docker("401 Unauthorized")}, None, id="non-json"),
    # a bad value would be written straight into qBittorrent's listen port
    pytest.param({"return_value": _docker('{"port":70000}')}, None, id="out-of-range"),
])
def test_forwarded_port_parsing(run_kwargs, expected):
    with patch.object(sfp.subprocess, "run", **run_kwargs):
        assert sfp.forwarded_port() == expected


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
        seen.update(values)
    client.set_preferences.side_effect = _set

    with patch.object(sfp, "forwarded_port", return_value=port), \
         patch.object(sfp, "QBitClient", return_value=client), \
         patch.object(sfp, "load_env_file", return_value={}), \
         patch.object(sys, "argv", ["sync-forwarded-port.py", *argv]):
        code = sfp.main()
    return code, client, seen


@pytest.mark.parametrize("port, exit_code", [(None, 1), (0, 0)], ids=["unreadable-port-exits-1", "no-allocation-exits-0"])
def test_no_usable_port_does_not_touch_qbittorrent(port, exit_code):
    code, client, _ = _run(port, 54321)
    assert code == exit_code
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


@pytest.mark.parametrize("run_kwargs, write_attempted", [
    # setPreferences answers 200 even when it ignores the body, so a write that did not take must fail loudly
    pytest.param({"set_result": "ignored"}, True, id="silently-ignored-write-is-caught"),
    pytest.param({"login": False}, False, id="login-failure"),
])
def test_failed_write_exits_2_and_leaves_the_port_alone(run_kwargs, write_attempted):
    code, client, seen = _run(4242, 54321, **run_kwargs)
    assert code == 2
    assert client.set_preferences.called is write_attempted
    assert seen["listen_port"] == 54321
