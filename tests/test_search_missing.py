"""Tests for search-missing.py — the weekly missing-item search.

The key property is refusing to search a movie that is not released yet: for films
still in cinemas the quality profile approves pre-release junk.  Most movie cases are
therefore guards.

Isolation: os.environ is patched with clear=True and load_env_file is stubbed; both
apps are mocked ArrClients.  (media_stack.paths still reads the real .env at import;
here the service URLs are only lookup keys for the mocks.)
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

@pytest.mark.parametrize("movies, queue, expected", [
    pytest.param([_movie(1), _movie(2)], [], [1, 2], id="picks-released-monitored-missing"),
    # The Odyssey / Spider-Man case: in cinemas, isAvailable False.
    pytest.param([_movie(1, available=False)], [], [], id="skips-unreleased"),
    # isAvailable follows minimumAvailability ("In Cinemas" is available at the theatrical date); status must say released too
    pytest.param([_movie(1, available=True, status="inCinemas")], [], [], id="skips-available-but-in-cinemas"),
    # Radarr's 90-days-after-cinemas "released" fallback is not a home release
    pytest.param([_movie(1, digital=None, physical=None)], [], [], id="skips-cinema-fallback-with-no-home-release-date"),
    pytest.param([_movie(1, digital=FUTURE)], [], [], id="skips-future-home-release-date"),
    pytest.param([_movie(1, digital=None, physical=PAST)], [], [1], id="picks-on-past-physical-release-alone"),
    pytest.param([_movie(1, has_file=True)], [], [], id="skips-movie-that-has-a-file"),
    pytest.param([_movie(1, monitored=False)], [], [], id="skips-unmonitored"),
    pytest.param([_movie(1), _movie(2)], [{"movieId": 2}], [1], id="skips-movie-already-downloading"),
    pytest.param([_movie(1)], [{"title": "pending row"}], [1], id="queue-rows-without-movieid-are-ignored"),
])
def test_pick_movies(movies, queue, expected):
    assert sm.pick_movies(movies, queue) == expected


def test_z_suffix_timestamp_is_parsed():
    # Python 3.10 fromisoformat rejects a bare Z; this must still count.
    assert sm._passed("2020-01-01T00:00:00Z", sm.datetime.now(sm.timezone.utc)) is True


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


def test_radarr_refusal_exits_1():
    sonarr, radarr = _apps(movies=[_movie(5)], radarr_cid=None)
    assert _run([], sonarr, radarr) == 1


def test_missing_sonarr_key_exits_1_but_movies_still_searched():
    sonarr, radarr = _apps(movies=[_movie(5)])
    assert _run([], sonarr, radarr, env={"RADARR_API_KEY": "r"}) == 1
    sonarr.run_command.assert_not_called()
    radarr.run_command.assert_called_once()
