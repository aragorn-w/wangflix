"""Tests for search-missing.py — the weekly missing-item search.

The property that matters most is NOT "does it search", it is "does it refuse to
search a movie that is not released yet": for films still in cinemas the quality
profile approves pre-release junk, so a wrong pick means grabbing a mislabelled
rip or a different film entirely.  Most movie cases are therefore guards.

Isolation: os.environ is patched with clear=True and load_env_file is stubbed,
so no real API key or ambient variable reaches the code, and both apps are
mocked ArrClients, so there is no network.  (media_stack.paths still reads the
real .env at import for the service URLs; here those are only lookup keys for
the mocks.)
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
_spec = importlib.util.spec_from_file_location(
    "search_missing", str(PROJECT_ROOT / "search-missing.py"))
sm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sm)

ENV = {"SONARR_API_KEY": "s", "RADARR_API_KEY": "r"}


PAST = "2024-10-14T00:00:00Z"      # Radarr's wire format: UTC, literal Z
FUTURE = "2099-01-01T00:00:00Z"


def _movie(mid, title="M", *, monitored=True, has_file=False, available=True,
           status="released", digital=PAST, physical=None, **extra):
    m = {"id": mid, "title": title, "monitored": monitored, "hasFile": has_file,
         "isAvailable": available, "status": status}
    if digital is not None:
        m["digitalRelease"] = digital
    if physical is not None:
        m["physicalRelease"] = physical
    m.update(extra)
    return m


# --- pick_movies: what gets searched -------------------------------------

def test_picks_released_monitored_missing():
    assert sm.pick_movies([_movie(1), _movie(2)], []) == [1, 2]


def test_skips_unreleased():
    # The Odyssey / Spider-Man case: in cinemas, isAvailable False.
    assert sm.pick_movies([_movie(1, available=False)], []) == []


def test_skips_when_isavailable_field_missing():
    # An API shape change must fail toward searching nothing, never toward
    # treating an unknown release state as released.
    m = _movie(1)
    del m["isAvailable"]
    assert sm.pick_movies([m], []) == []


@pytest.mark.parametrize("value", [None, 1, "true", "yes"])
def test_skips_truthy_but_not_true_availability(value):
    assert sm.pick_movies([_movie(1, available=value)], []) == []


@pytest.mark.parametrize("status", ["inCinemas", "announced", "tba", "deleted", None, ""])
def test_skips_available_but_not_released_status(status):
    """codex round-1 #1 — isAvailable follows each movie's own
    minimumAvailability, so a movie set to "In Cinemas" is "available" at the
    theatrical date.  Status must also say released."""
    assert sm.pick_movies([_movie(1, available=True, status=status)], []) == []


def test_skips_when_status_field_missing():
    m = _movie(1)
    del m["status"]
    assert sm.pick_movies([m], []) == []


def test_skips_released_status_without_isavailable():
    assert sm.pick_movies([_movie(1, available=False, status="released")], []) == []


def test_skips_cinema_fallback_with_no_home_release_date():
    """codex round-2 #1 — Radarr calls a film "released" (and available) 90
    days after cinemas when no home-release date is known.  That is not a
    home release, so it is not searched."""
    assert sm.pick_movies([_movie(1, digital=None, physical=None)], []) == []


def test_skips_future_home_release_date():
    assert sm.pick_movies([_movie(1, digital=FUTURE)], []) == []


@pytest.mark.parametrize("bad", ["", "not-a-date", 20241014, True])
def test_skips_malformed_home_release_date(bad):
    assert sm.pick_movies([_movie(1, digital=bad)], []) == []


def test_picks_on_past_physical_release_alone():
    assert sm.pick_movies([_movie(1, digital=None, physical=PAST)], []) == [1]


def test_picks_on_past_digital_when_physical_is_future():
    assert sm.pick_movies([_movie(1, digital=PAST, physical=FUTURE)], []) == [1]


def test_z_suffix_timestamp_is_parsed():
    # Python 3.10 fromisoformat rejects a bare Z; this must still count.
    assert sm._passed("2020-01-01T00:00:00Z", sm.datetime.now(sm.timezone.utc)) is True


def test_skips_movie_that_has_a_file():
    assert sm.pick_movies([_movie(1, has_file=True)], []) == []


def test_skips_when_hasfile_unknown():
    m = _movie(1)
    del m["hasFile"]
    assert sm.pick_movies([m], []) == []


def test_skips_unmonitored():
    assert sm.pick_movies([_movie(1, monitored=False)], []) == []


def test_skips_movie_already_downloading():
    assert sm.pick_movies([_movie(1), _movie(2)], [{"movieId": 2}]) == [1]


def test_skips_bool_movie_id():
    assert sm.pick_movies([_movie(True)], []) == []


def test_bool_queue_movieid_does_not_block_movie_1():
    # True == 1: a malformed queue row must not hide movie 1 from the search.
    assert sm.pick_movies([_movie(1)], [{"movieId": True}]) == [1]


def test_queue_rows_without_movieid_are_ignored():
    assert sm.pick_movies([_movie(1)], [{"title": "pending row"}]) == [1]


# --- main: what gets posted ----------------------------------------------

def _run(argv, sonarr, radarr, env=ENV):
    def factory(url, key):
        return sonarr if url == sm.SONARR_URL else radarr
    with patch.dict("os.environ", {}, clear=True), \
         patch.object(sm, "load_env_file", return_value=dict(env)), \
         patch.object(sm, "ArrClient", side_effect=factory), \
         patch.object(sys, "argv", ["search-missing.py", *argv]):
        return sm.main()


def _apps(movies=(), queue=(), sonarr_cid=11, radarr_cid=22):
    sonarr, radarr = MagicMock(), MagicMock()
    sonarr.run_command.return_value = sonarr_cid
    radarr.movies.return_value = list(movies)
    radarr.get_queue.return_value = list(queue)
    radarr.run_command.return_value = radarr_cid
    return sonarr, radarr


def test_posts_sonarr_missing_search_with_monitored_true():
    sonarr, radarr = _apps()
    assert _run([], sonarr, radarr) == 0
    sonarr.run_command.assert_called_once_with("MissingEpisodeSearch", monitored=True)


def test_posts_radarr_search_for_picked_ids_only():
    sonarr, radarr = _apps(movies=[_movie(5), _movie(6, available=False), _movie(7)],
                           queue=[{"movieId": 7}])
    assert _run([], sonarr, radarr) == 0
    radarr.run_command.assert_called_once_with("MoviesSearch", movieIds=[5])


def test_never_uses_radarr_bulk_missing_search():
    sonarr, radarr = _apps(movies=[_movie(5)])
    _run([], sonarr, radarr)
    names = [c.args[0] for c in radarr.run_command.call_args_list]
    assert "MissingMoviesSearch" not in names


def test_no_candidates_posts_no_radarr_command():
    sonarr, radarr = _apps(movies=[_movie(1, available=False), _movie(2, has_file=True)])
    assert _run([], sonarr, radarr) == 0
    radarr.run_command.assert_not_called()


def test_dry_run_posts_nothing():
    sonarr, radarr = _apps(movies=[_movie(5)])
    assert _run(["--dry-run"], sonarr, radarr) == 0
    sonarr.run_command.assert_not_called()
    radarr.run_command.assert_not_called()


def test_unreadable_movie_list_exits_1_and_searches_no_movies():
    sonarr, radarr = _apps()
    radarr.movies.return_value = None
    assert _run([], sonarr, radarr) == 1
    radarr.run_command.assert_not_called()


def test_unreadable_queue_exits_1_and_searches_no_movies():
    # Without the queue, an already-downloading movie would be searched again.
    sonarr, radarr = _apps(movies=[_movie(5)])
    radarr.get_queue.side_effect = ConnectionError("down")
    assert _run([], sonarr, radarr) == 1
    radarr.run_command.assert_not_called()


def test_sonarr_failure_still_searches_movies_and_exits_1():
    sonarr, radarr = _apps(movies=[_movie(5)], sonarr_cid=None)
    assert _run([], sonarr, radarr) == 1
    radarr.run_command.assert_called_once_with("MoviesSearch", movieIds=[5])


def test_null_title_still_searches():
    """codex round-1 #3 — a null title crashed the log line before the
    MoviesSearch it was about to report, so nothing was queued."""
    sonarr, radarr = _apps(movies=[_movie(5, title=None)])
    assert _run([], sonarr, radarr) == 0
    radarr.run_command.assert_called_once_with("MoviesSearch", movieIds=[5])


def test_radarr_refusal_exits_1():
    sonarr, radarr = _apps(movies=[_movie(5)], radarr_cid=None)
    assert _run([], sonarr, radarr) == 1


def test_missing_sonarr_key_exits_1_but_movies_still_searched():
    sonarr, radarr = _apps(movies=[_movie(5)])
    assert _run([], sonarr, radarr, env={"RADARR_API_KEY": "r"}) == 1
    sonarr.run_command.assert_not_called()
    radarr.run_command.assert_called_once()


def test_missing_radarr_key_exits_1_but_tv_still_searched():
    sonarr, radarr = _apps(movies=[_movie(5)])
    assert _run([], sonarr, radarr, env={"SONARR_API_KEY": "s"}) == 1
    sonarr.run_command.assert_called_once()
    radarr.movies.assert_not_called()
