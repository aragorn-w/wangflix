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


def test_parse_air_assumes_utc_when_naive():
    """A naive value must come back aware, or comparisons raise TypeError."""
    dt = ug.parse_air("2026-10-01T02:00:00")
    assert dt is not None and dt.tzinfo is timezone.utc


@pytest.mark.parametrize("bad", [None, "", "TBA", "not-a-date", 12345, [], {}])
def test_parse_air_rejects_unusable_values(bad):
    """None is the signal for "unknown", which callers must not read as unaired."""
    assert ug.parse_air(bad) is None


# ------------------------------------------------------------- is_real_int

def test_is_real_int_rejects_bools_and_non_ints():
    assert ug.is_real_int(3) is True
    assert ug.is_real_int(0) is True
    assert ug.is_real_int(True) is False
    assert ug.is_real_int(False) is False
    assert ug.is_real_int(3.0) is False
    assert ug.is_real_int("3") is False
    assert ug.is_real_int(None) is False


# ----------------------------------------------------------- resolve_grace

@pytest.mark.parametrize("cli, env, expected", [
    pytest.param(5.0, {"UNAIRED_GRACE_HOURS": "99"}, 5.0, id="cli-beats-env"),
    pytest.param(None, {"UNAIRED_GRACE_HOURS": "7"}, 7.0, id="env-when-cli-absent"),
    # an operator asking for no margin gets none; 0 is valid, not invalid
    pytest.param(0.0, {}, 0.0, id="explicit-zero-cli"),
    pytest.param(None, {"UNAIRED_GRACE_HOURS": "0"}, 0.0, id="explicit-zero-env"),
])
def test_resolve_grace(cli, env, expected):
    assert ug.resolve_grace(cli, env) == expected


@pytest.mark.parametrize("cli, env", [
    *[pytest.param(bad, {}, id=f"cli-{bad}") for bad in (-1.0, float("nan"), float("inf"), float("-inf"), 1e20)],
    *[pytest.param(None, {"UNAIRED_GRACE_HOURS": bad}, id=f"env-{bad!r}")
      for bad in ("soon", "", "nan", "-5", "1e30")],
])
def test_resolve_grace_fails_to_default_not_zero(cli, env):
    """A bad value must restore 24h, not clamp to 0; a huge finite value must not overflow timedelta."""
    assert ug.resolve_grace(cli, env) == ug.DEFAULT_GRACE_HOURS


# ------------------------------------------------- the core reaping decision

def test_reaps_episode_airing_well_beyond_grace():
    """The malware case: an S29E02-style grab days before the episode airs."""
    c = _client([_row(77, 4103, dl="ABC", series_id=10, title="Show S29E02 EDITH")],
                [_ep(4103, UNAIRED, series_id=10)])
    assert _run(c) == 0
    c.delete_from_queue.assert_called_once_with(77, blocklist=True)


_DRY = ("unaired-guard.py", "--dry-run")
_HUGE_GRACE = ("unaired-guard.py", "--grace-hours=1e20")
_PENDING = {"id": 30, "seriesId": 1, "episodeId": 1}  # PENDING rows carry no downloadId


@pytest.mark.parametrize("queue, episodes, argv", [
    pytest.param([_row(2, 5)], [_ep(5, AIRED)], None, id="already-aired"),
    # air dates trail reality (timezones, early streaming drops): a few hours out survives the 24h grace
    pytest.param([_row(3, 6)], [_ep(6, timedelta(hours=3))], None, id="imminent-airing-within-grace"),
    *[pytest.param([_row(4, 7)], [{"id": 7, "seriesId": 1, "airDateUtc": bad}], None,
                   id=f"unknown-air-date-{bad!r}") for bad in (None, "", "TBA")],
    pytest.param([_row(5, 999)], [_ep(7, UNAIRED)], None, id="episode-absent-from-the-fetch"),
    # bool is an int subclass (True == 1), so episodeId=True must not resolve to episode 1
    pytest.param([_row(70, True)], [_ep(1, UNAIRED)], None, id="boolean-episode-id"),
    # removeFromClient takes the whole download, so one unaired episode must not drag aired siblings down
    pytest.param([_row(10, 1, dl="PACK"), _row(11, 2, dl="PACK")], [_ep(1, AIRED), _ep(2, UNAIRED)], None,
                 id="mixed-season-pack"),
    # PENDING rows cannot be grouped; acting on one would blocklist the whole release
    pytest.param([_PENDING, {**_PENDING, "id": 31, "episodeId": 2}], [_ep(1, AIRED), _ep(2, UNAIRED)], None,
                 id="rows-without-download-id"),
    pytest.param([_row(32, 2, dl="")], [_ep(2, UNAIRED)], None, id="empty-string-download-id"),
    pytest.param([_row(50, 1)], [_ep(1, UNAIRED)], _DRY, id="dry-run"),
    # timedelta(hours=1e20) must not crash the run; it falls back to 24h
    pytest.param([_row(1, 1)], [_ep(1, timedelta(hours=3))], _HUGE_GRACE, id="huge-grace-applies-default-margin"),
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

def test_bogus_series_id_cannot_ride_in_on_another_series_episode():
    """After a valid row populates the episode map, a row with seriesId=True must not be deleted."""
    c = _client([_row(100, 1, dl="A", series_id=1),
                 _row(101, 1, dl="B", series_id=True)],
                [_ep(1, UNAIRED, series_id=1)])
    assert _run(c) == 0
    c.delete_from_queue.assert_called_once_with(100, blocklist=True)


def test_episode_missing_its_own_series_id_still_blocks_cross_series():
    """The episode map records the series actually FETCHED, so a row falsely claiming series 2
    cannot be deleted off series 1's episode 7 when the episode has no `seriesId`."""
    def episodes(series_id):
        # series 1 returns episode 7 with no seriesId field at all; series 2 has none
        return [{"id": 7, "airDateUtc": _iso(UNAIRED)}] if series_id == 1 else []
    c = MagicMock()
    c.get_queue.return_value = [_row(10, 7, dl="A", series_id=1),
                                _row(20, 7, dl="B", series_id=2)]
    c.episodes.side_effect = episodes
    assert _run(c) == 0
    # Only the row whose series genuinely supplied episode 7 may be reaped.
    c.delete_from_queue.assert_called_once_with(10, blocklist=True)


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


def test_pending_row_in_another_series_does_not_block_a_real_reap():
    """Only actionable rows drive the episode fetch, so an unrelated pending row cannot block a reap."""
    def episodes(series_id):
        return None if series_id == 99 else [_ep(1, UNAIRED)]
    c = MagicMock()
    c.get_queue.return_value = [_row(40, 1, dl="REAL", series_id=1),
                                {"id": 41, "seriesId": 99, "episodeId": 500}]
    c.episodes.side_effect = episodes
    assert _run(c) == 0
    c.delete_from_queue.assert_called_once_with(40, blocklist=True)


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
    pytest.param(RuntimeError("boom"), 2, id="non-http-exception"),
])
def test_delete_failure_exit_code(failure, exit_code):
    c = _client([_row(80, 1)], [_ep(1, UNAIRED)], delete_side_effect=failure)
    assert _run(c) == exit_code


@pytest.mark.parametrize("queue", [[], [_PENDING]], ids=["empty-queue", "only-pending-rows"])
def test_nothing_actionable_skips_the_episode_fetch(queue):
    c = _client(queue, [_ep(1, UNAIRED)])
    assert _run(c) == 0
    c.episodes.assert_not_called()
    c.delete_from_queue.assert_not_called()
