"""Tests for unaired-guard.py — reaping Sonarr queue items for unaired episodes.

The safety-critical property is NOT "does it reap", it is "does it refuse to
reap anything it cannot prove is unaired".  A wrong reap deletes a legitimate
in-progress download AND blocklists its release, so the bulk of these cases are
false-positive guards rather than happy-path coverage.

Every codex finding from both review rounds has a named regression case here.

ISOLATION (round-2 #4): `os.environ` is patched with clear=True so an ambient
UNAIRED_GRACE_HOURS on the developer's machine cannot change a verdict, and the
module writes no log file at all (stdout only), so nothing here can touch the
production log.  Sonarr is always a mocked ArrClient — no network.
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

def test_parse_air_accepts_sonarrs_z_suffix():
    """Python 3.10's fromisoformat rejects 'Z'; the helper must handle it."""
    assert ug.parse_air("2026-10-01T02:00:00Z") == \
        datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)


def test_parse_air_accepts_explicit_offset():
    assert ug.parse_air("2026-10-01T02:00:00+00:00") == \
        datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc)


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

def test_resolve_grace_prefers_cli_over_env():
    assert ug.resolve_grace(5.0, {"UNAIRED_GRACE_HOURS": "99"}) == 5.0


def test_resolve_grace_reads_env_when_cli_absent():
    assert ug.resolve_grace(None, {"UNAIRED_GRACE_HOURS": "7"}) == 7.0


def test_resolve_grace_honours_explicit_zero():
    """An operator asking for no margin gets none; 0 is valid, not invalid."""
    assert ug.resolve_grace(0.0, {}) == 0.0
    assert ug.resolve_grace(None, {"UNAIRED_GRACE_HOURS": "0"}) == 0.0


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"),
                                 float("-inf"), 1e20])
def test_resolve_grace_fails_to_default_not_zero(bad):
    """round-1 #4 + round-2 #5. Clamping a bad value to 0 removed the safety
    margin; a huge finite value overflowed timedelta. Both must restore 24h."""
    assert ug.resolve_grace(bad, {}) == ug.DEFAULT_GRACE_HOURS


@pytest.mark.parametrize("bad", ["soon", "", "nan", "-5", "1e30"])
def test_resolve_grace_fails_to_default_on_bad_env(bad):
    assert ug.resolve_grace(None, {"UNAIRED_GRACE_HOURS": bad}) == \
        ug.DEFAULT_GRACE_HOURS


def test_huge_grace_does_not_crash_and_applies_the_default_margin():
    """round-2 #5: timedelta(hours=1e20) raised OverflowError mid-run.  The run
    must survive AND fall back to the 24h margin, which a 3h-ahead episode then
    sits inside — so nothing is reaped."""
    c = _client([_row(1, 1)], [_ep(1, timedelta(hours=3))])
    assert _run(c, argv=("unaired-guard.py", "--grace-hours=1e20")) == 0
    c.delete_from_queue.assert_not_called()


# ------------------------------------------------- the core reaping decision

def test_reaps_episode_airing_well_beyond_grace():
    """The malware case: an S29E02-style grab days before the episode airs."""
    c = _client([_row(77, 4103, dl="ABC", series_id=10, title="Show S29E02 EDITH")],
                [_ep(4103, UNAIRED, series_id=10)])
    assert _run(c) == 0
    c.delete_from_queue.assert_called_once_with(77, blocklist=True)


def test_reap_always_blocklists():
    """Removing without blocklisting lets the identical release straight back."""
    c = _client([_row(1, 1)], [_ep(1, UNAIRED)])
    assert _run(c) == 0
    assert c.delete_from_queue.call_args.kwargs["blocklist"] is True


def test_leaves_already_aired_episode_alone():
    c = _client([_row(2, 5)], [_ep(5, AIRED)])
    assert _run(c) == 0
    c.delete_from_queue.assert_not_called()


def test_grace_window_protects_an_episode_airing_imminently():
    """Air dates trail reality (timezones, early streaming drops), so an
    episode a few hours out must survive the default 24h grace."""
    c = _client([_row(3, 6)], [_ep(6, timedelta(hours=3))])
    assert _run(c) == 0
    c.delete_from_queue.assert_not_called()


def test_grace_is_configurable_downward():
    """The same 3h-ahead episode IS reaped once grace is tightened below it."""
    c = _client([_row(3, 6)], [_ep(6, timedelta(hours=3))])
    assert _run(c, argv=("unaired-guard.py", "--grace-hours", "1")) == 0
    c.delete_from_queue.assert_called_once()


@pytest.mark.parametrize("bad_air", [None, "", "TBA"])
def test_unknown_air_date_is_never_reaped(bad_air):
    """A TBA/missing date is absence of evidence, not evidence of a fake."""
    c = _client([_row(4, 7)], [{"id": 7, "seriesId": 1, "airDateUtc": bad_air}])
    assert _run(c) == 0
    c.delete_from_queue.assert_not_called()


def test_episode_absent_from_the_fetch_is_never_reaped():
    c = _client([_row(5, 999)], [_ep(7, UNAIRED)])
    assert _run(c) == 0
    c.delete_from_queue.assert_not_called()


# --------------------------------------------- id validation / cross-series

def test_boolean_episode_id_never_authorizes_a_delete():
    """round-1 #3. bool is an int subclass and True == 1, so episodeId=True
    would otherwise resolve to episode 1 and reap its download."""
    c = _client([_row(70, True)], [_ep(1, UNAIRED)])
    assert _run(c) == 0
    c.delete_from_queue.assert_not_called()


def test_bogus_series_id_cannot_ride_in_on_another_series_episode():
    """round-2 #1, reproduced by codex. A valid row populates the episode map;
    a second row with seriesId=True must NOT then be deleted."""
    c = _client([_row(100, 1, dl="A", series_id=1),
                 _row(101, 1, dl="B", series_id=True)],
                [_ep(1, UNAIRED, series_id=1)])
    assert _run(c) == 0
    c.delete_from_queue.assert_called_once_with(100, blocklist=True)


def test_episode_missing_its_own_series_id_still_blocks_cross_series():
    """round-3 #1, reproduced by codex. The episode map used to store a missing
    episode `seriesId` as None and then SKIP the ownership check, so a row
    falsely claiming series 2 got deleted off series 1's episode 7. The map now
    records the series actually FETCHED, which is always known."""
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

    Named for what it actually exercises (round-4 #2): because only series 2 is
    fetched here, episode 1 is simply absent from the map. The ownership check
    against a POPULATED map is covered by
    test_episode_missing_its_own_series_id_still_blocks_cross_series.

    The mock uses a side_effect rather than a flat return_value deliberately:
    episode ids are globally unique in Sonarr, so a fetch of series 2 can never
    return series 1's episode. The old flat mock did exactly that, which meant
    "series 2 contains episode 1" — not a mismatch — so once the map began
    recording the fetched series the test asserted the opposite of its name."""
    def episodes(series_id):
        return [_ep(1, UNAIRED, series_id=1)] if series_id == 1 else \
               [_ep(55, UNAIRED, series_id=2)]
    c = MagicMock()
    c.get_queue.return_value = [_row(102, 1, series_id=2)]
    c.episodes.side_effect = episodes
    assert _run(c) == 0
    c.delete_from_queue.assert_not_called()


# ----------------------------------------------------- season-pack grouping

def test_mixed_season_pack_is_left_entirely_alone():
    """removeFromClient takes the whole download, so one unaired episode must
    not drag its already-aired siblings down with it."""
    c = _client([_row(10, 1, dl="PACK"), _row(11, 2, dl="PACK")],
                [_ep(1, AIRED), _ep(2, UNAIRED)])
    assert _run(c) == 0
    c.delete_from_queue.assert_not_called()


def test_fully_unaired_pack_is_deleted_exactly_once():
    """round-2 #3. Sonarr's DELETE acts on the whole download, so one call is
    both sufficient and the only way to avoid spurious 404s on later rows."""
    c = _client([_row(20, 1, dl="PACK"), _row(21, 2, dl="PACK")],
                [_ep(1, UNAIRED), _ep(2, UNAIRED + timedelta(days=7))])
    assert _run(c) == 0
    c.delete_from_queue.assert_called_once_with(20, blocklist=True)


def test_rows_without_download_id_are_skipped_entirely():
    """round-1 #1. Sonarr emits PENDING releases one row per episode with
    downloadId unset, so they cannot be grouped back into their release;
    acting on one row would blocklist the whole release."""
    c = _client([{"id": 30, "seriesId": 1, "episodeId": 1},
                 {"id": 31, "seriesId": 1, "episodeId": 2}],
                [_ep(1, AIRED), _ep(2, UNAIRED)])
    assert _run(c) == 0
    c.delete_from_queue.assert_not_called()


def test_empty_string_download_id_is_also_skipped():
    c = _client([_row(32, 2, dl="")], [_ep(2, UNAIRED)])
    assert _run(c) == 0
    c.delete_from_queue.assert_not_called()


def test_pending_row_in_another_series_does_not_block_a_real_reap():
    """round-2 #2. Episode dates used to be fetched for every series in the
    queue, so an unrelated pending row whose series lookup failed exited 1 and
    reaped nothing.  Only actionable rows may drive the fetch."""
    def episodes(series_id):
        return None if series_id == 99 else [_ep(1, UNAIRED)]
    c = MagicMock()
    c.get_queue.return_value = [_row(40, 1, dl="REAL", series_id=1),
                                {"id": 41, "seriesId": 99, "episodeId": 500}]
    c.episodes.side_effect = episodes
    assert _run(c) == 0
    c.delete_from_queue.assert_called_once_with(40, blocklist=True)


# ------------------------------------------------------------ failure modes

def test_dry_run_changes_nothing():
    c = _client([_row(50, 1)], [_ep(1, UNAIRED)])
    assert _run(c, argv=("unaired-guard.py", "--dry-run")) == 0
    c.delete_from_queue.assert_not_called()


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


def test_episode_fetch_raising_exits_1():
    """round-1 #2. Swallowing this made a Sonarr outage look like a clean run."""
    c = _client([_row(60, 1)], RuntimeError("500"))
    assert _run(c) == 1
    c.delete_from_queue.assert_not_called()


def test_episode_fetch_returning_none_exits_1():
    """ArrClient.episodes() returns None on a non-200 rather than raising."""
    c = _client([_row(61, 1)], None)
    assert _run(c) == 1
    c.delete_from_queue.assert_not_called()


def test_404_on_delete_is_a_no_op_not_a_failure():
    """round-2 #3. A concurrent run having already removed the download is the
    expected shape of a race, not an error worth exit 2."""
    c = _client([_row(80, 1)], [_ep(1, UNAIRED)],
                delete_side_effect=_http_error(404))
    assert _run(c) == 0


def test_non_404_http_error_on_delete_exits_2():
    """round-2 #3's other half: a genuine 500 must NOT be written off as
    'already gone'. The queue is still jammed and the operator needs to know."""
    c = _client([_row(81, 1)], [_ep(1, UNAIRED)],
                delete_side_effect=_http_error(500))
    assert _run(c) == 2


def test_non_http_exception_on_delete_exits_2():
    c = _client([_row(82, 1)], [_ep(1, UNAIRED)],
                delete_side_effect=RuntimeError("boom"))
    assert _run(c) == 2


def test_empty_queue_is_a_no_op():
    c = _client([], [])
    assert _run(c) == 0
    c.episodes.assert_not_called()


def test_queue_of_only_pending_rows_skips_the_episode_fetch():
    """Nothing actionable means no reason to talk to Sonarr again."""
    c = _client([{"id": 90, "seriesId": 1, "episodeId": 1}], [_ep(1, UNAIRED)])
    assert _run(c) == 0
    c.episodes.assert_not_called()
    c.delete_from_queue.assert_not_called()


def test_ambient_grace_env_cannot_change_a_verdict():
    """round-2 #4: the suite must not depend on the developer's environment."""
    c = _client([_row(95, 1)], [_ep(1, timedelta(hours=3))])
    assert _run(c, extra_env={"UNAIRED_GRACE_HOURS": "0"}) == 0
    c.delete_from_queue.assert_called_once()
