"""Tests for unaired-guard.py — reaping Sonarr queue items for unaired episodes.

The safety property is that it refuses to reap anything it cannot prove is unaired:
a wrong reap deletes a legitimate download and blocklists its release, so most cases
are false-positive guards.

Isolation: `os.environ` is patched with clear=True so an ambient UNAIRED_GRACE_HOURS
cannot change a verdict; the module writes no log file; Sonarr is a mocked ArrClient.
"""
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
_spec = importlib.util.spec_from_file_location(
    "unaired_guard", str(PROJECT_ROOT / "unaired-guard.py"))
ug = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ug)

NOW = datetime.now(timezone.utc)
AIRED = -timedelta(days=3)
UNAIRED = timedelta(days=5)


def _iso(delta: timedelta) -> str:
    """Sonarr's wire format: UTC with a literal Z suffix."""
    return (NOW + delta).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ep(ep_id: int, delta: timedelta | None, series_id: int = 1) -> dict:
    return {"id": ep_id, "seriesId": series_id,
            "airDateUtc": None if delta is None else _iso(delta)}


def _row(queue_id: int, ep_id, dl="D", series_id=1, title="Rel") -> dict:
    return {"id": queue_id, "seriesId": series_id, "episodeId": ep_id,
            "downloadId": dl, "title": title}


def _client(queue, episodes, delete_side_effect=None) -> MagicMock:
    c = MagicMock()
    c.get_queue.return_value = queue
    if isinstance(episodes, Exception):
        c.episodes.side_effect = episodes
    else:
        c.episodes.return_value = episodes
    if delete_side_effect is not None:
        c.delete_from_queue.side_effect = delete_side_effect
    return c


def _run(client: MagicMock, argv=("unaired-guard.py",), extra_env=None) -> int:
    env = {"SONARR_API_KEY": "k"}
    if extra_env:
        env.update(extra_env)
    with patch.object(ug, "ArrClient", return_value=client), \
         patch.object(ug, "load_env_file", return_value={}), \
         patch.dict("os.environ", env, clear=True), \
         patch.object(sys, "argv", list(argv)):
        return ug.main()


def _http_error(code: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = code
    return requests.HTTPError(f"{code}", response=resp)


# ---------------------------------------------------------------- parse_air

@pytest.mark.parametrize("raw", ["2026-10-01T02:00:00Z", "2026-10-01T02:00:00+00:00"],
                         ids=["z-suffix", "explicit-offset"])
def test_parse_air_accepts_z_suffix_and_explicit_offset(raw):
    """Python 3.10's fromisoformat rejects 'Z'; the helper must handle it."""
    assert ug.parse_air(raw) == datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad", [None, "TBA"])
def test_parse_air_rejects_unusable_values(bad):
    """None is the signal for "unknown", which callers must not read as unaired."""
    assert ug.parse_air(bad) is None


# ----------------------------------------------------------- resolve_grace

@pytest.mark.parametrize("cli, env, expected", [
    pytest.param(5.0, {"UNAIRED_GRACE_HOURS": "99"}, 5.0, id="cli-beats-env"),
    pytest.param(None, {"UNAIRED_GRACE_HOURS": "7"}, 7.0, id="env-when-cli-absent"),
    # an operator asking for no margin gets none; 0 is valid, not invalid
    pytest.param(0.0, {}, 0.0, id="explicit-zero-cli"),
])
def test_resolve_grace(cli, env, expected):
    assert ug.resolve_grace(cli, env) == expected


@pytest.mark.parametrize("cli, env", [
    pytest.param(-1.0, {}, id="cli-negative"),
    pytest.param(None, {"UNAIRED_GRACE_HOURS": "soon"}, id="env-not-a-number"),
])
def test_resolve_grace_fails_to_default_not_zero(cli, env):
    """A bad value must restore 24h, not clamp to 0."""
    assert ug.resolve_grace(cli, env) == ug.DEFAULT_GRACE_HOURS


# ------------------------------------------------- the core reaping decision

def test_reaps_episode_airing_well_beyond_grace():
    """The malware case: an S29E02-style grab days before the episode airs."""
    c = _client([_row(77, 4103, dl="ABC", series_id=10, title="Show S29E02 EDITH")],
                [_ep(4103, UNAIRED, series_id=10)])
    assert _run(c) == 0
    c.delete_from_queue.assert_called_once_with(77, blocklist=True)


_DRY = ("unaired-guard.py", "--dry-run")
_PENDING = {"id": 30, "seriesId": 1, "episodeId": 1}  # PENDING rows carry no downloadId


@pytest.mark.parametrize("queue, episodes, argv", [
    pytest.param([_row(2, 5)], [_ep(5, AIRED)], None, id="already-aired"),
    # air dates trail reality (timezones, early streaming drops): a few hours out survives the 24h grace
    pytest.param([_row(3, 6)], [_ep(6, timedelta(hours=3))], None, id="imminent-airing-within-grace"),
    pytest.param([_row(4, 7)], [{"id": 7, "seriesId": 1, "airDateUtc": None}], None, id="unknown-air-date"),
    pytest.param([_row(5, 999)], [_ep(7, UNAIRED)], None, id="episode-absent-from-the-fetch"),
    # removeFromClient takes the whole download, so one unaired episode must not drag aired siblings down
    pytest.param([_row(10, 1, dl="PACK"), _row(11, 2, dl="PACK")], [_ep(1, AIRED), _ep(2, UNAIRED)], None,
                 id="mixed-season-pack"),
    # PENDING rows cannot be grouped; acting on one would blocklist the whole release
    pytest.param([_PENDING, {**_PENDING, "id": 31, "episodeId": 2}], [_ep(1, AIRED), _ep(2, UNAIRED)], None,
                 id="rows-without-download-id"),
    pytest.param([_row(50, 1)], [_ep(1, UNAIRED)], _DRY, id="dry-run"),
])
def test_is_never_reaped(queue, episodes, argv):
    c = _client(queue, episodes)
    assert _run(c, **({"argv": argv} if argv else {})) == 0
    c.delete_from_queue.assert_not_called()


@pytest.mark.parametrize("argv, extra_env", [
    pytest.param(("unaired-guard.py", "--grace-hours", "1"), None, id="cli"),
    pytest.param(("unaired-guard.py",), {"UNAIRED_GRACE_HOURS": "0"}, id="env"),
])
def test_grace_is_configurable_downward(argv, extra_env):
    """The same 3h-ahead episode IS reaped once grace is tightened below it."""
    c = _client([_row(3, 6)], [_ep(6, timedelta(hours=3))])
    assert _run(c, argv=argv, extra_env=extra_env) == 0
    c.delete_from_queue.assert_called_once()


# --------------------------------------------- id validation / cross-series

def test_row_whose_claimed_series_does_not_supply_the_episode_is_not_reaped():
    """A row claiming series 2 for an episode series 2 does not have.

    Only series 2 is fetched, so episode 1 is absent from the map; the check against a
    populated map is test_episode_missing_its_own_series_id_still_blocks_cross_series.
    The side_effect mock matters: episode ids are globally unique, so a flat return_value
    would wrongly make series 2 contain series 1's episode."""
    def episodes(series_id):
        return [_ep(1, UNAIRED, series_id=1)] if series_id == 1 else \
               [_ep(55, UNAIRED, series_id=2)]
    c = MagicMock()
    c.get_queue.return_value = [_row(102, 1, series_id=2)]
    c.episodes.side_effect = episodes
    assert _run(c) == 0
    c.delete_from_queue.assert_not_called()


# ----------------------------------------------------- season-pack grouping

def test_fully_unaired_pack_is_deleted_exactly_once():
    """Sonarr's DELETE acts on the whole download: one call suffices and avoids 404s on later rows."""
    c = _client([_row(20, 1, dl="PACK"), _row(21, 2, dl="PACK")],
                [_ep(1, UNAIRED), _ep(2, UNAIRED + timedelta(days=7))])
    assert _run(c) == 0
    c.delete_from_queue.assert_called_once_with(20, blocklist=True)


# ------------------------------------------------------------ failure modes

def test_unreadable_queue_exits_1_and_changes_nothing():
    """Failing loud: a silent 0 would hide the guard being permanently blind."""
    c = _client([], [])
    c.get_queue.side_effect = RuntimeError("connection refused")
    assert _run(c) == 1
    c.delete_from_queue.assert_not_called()


def test_missing_api_key_exits_1():
    with patch.object(ug, "load_env_file", return_value={}), \
         patch.dict("os.environ", {}, clear=True), \
         patch.object(sys, "argv", ["unaired-guard.py"]):
        assert ug.main() == 1


@pytest.mark.parametrize("episodes", [RuntimeError("500"), None], ids=["raises", "returns-none"])
def test_episode_fetch_failure_exits_1(episodes):
    """A Sonarr outage must not look like a clean run (ArrClient.episodes() returns None on a non-200)."""
    c = _client([_row(60, 1)], episodes)
    assert _run(c) == 1
    c.delete_from_queue.assert_not_called()


@pytest.mark.parametrize("failure, exit_code", [
    pytest.param(_http_error(404), 0, id="404-already-gone-is-a-race-not-an-error"),
    pytest.param(_http_error(500), 2, id="500-is-not-already-gone"),
])
def test_delete_failure_exit_code(failure, exit_code):
    c = _client([_row(80, 1)], [_ep(1, UNAIRED)], delete_side_effect=failure)
    assert _run(c) == exit_code


def test_empty_queue_is_a_clean_no_op():
    c = _client([], [_ep(1, UNAIRED)])
    assert _run(c) == 0
    c.delete_from_queue.assert_not_called()
