"""Tests for tv-dedupe.py, the Sonarr sibling of movie-dedupe.py: SAFE/RISKY classification
and move-to-recycle against a real temp tv tree and a mocked ArrClient, plus cleanup of
Sonarr's orphan `episodefile` DB row after a move."""
import importlib.util
import os
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
_spec = importlib.util.spec_from_file_location(
    "tv_dedupe", str(PROJECT_ROOT / "tv-dedupe.py"))
td = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(td)


MB = 1024 * 1024
KEEPER = "Show - S01E01 - A WEBDL-1080p.mkv"
EXTRA = "Show - S01E01 - A WEBRip-1080p.mkv"
OTHER_EXTRA = "Show - S01E01 - B WEBRip-720p.mkv"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(td.time, "sleep", lambda _s: None)


def _make_season(tv: Path, series: str, season: str, files: dict[str, int]) -> Path:
    # Sparse files (touch + truncate): only the size is read (probe/already_processed are
    # mocked), so real payloads would just burn disk.
    d = tv / series / season
    d.mkdir(parents=True)
    for name, size in files.items():
        p = d / name
        p.touch()
        os.truncate(p, size)
    return d


def _pair(tmp_path: Path, extra: str = EXTRA) -> Path:
    """Season 01 of "Show" holding KEEPER (200 MB) and `extra` (150 MB)."""
    return _make_season(tmp_path / "tv", "Show", "Season 01", {KEEPER: 200 * MB, extra: 150 * MB})


def _videos(d: Path) -> list[str]:
    """Real video files only: persisted .consolidate-*.lock dotfiles would match a *.m* glob."""
    return sorted(p.name for p in d.iterdir() if td.is_video(p.name))


def _ef(fid, name, season=1):
    return {"id": fid, "relativePath": f"Season {season:02d}/{name}"}


def _ep(episode, fid, season=1):
    return {"seasonNumber": season, "episodeNumber": episode,
            "hasFile": bool(fid), "episodeFileId": fid}


def _mock_sonarr(files, episodes, *, series=None):
    client = MagicMock()
    client.series.return_value = [{"id": 1, "path": "/tv/Show"}] if series is None else series
    client.episode_files.return_value = files
    client.episodes.return_value = episodes
    client.rescan_series.return_value = True
    client.delete_episode_file.return_value = True
    return client


def _scripted_sonarr(*snapshots):
    """Sonarr whose state changes between reads: each (episode_files, episodes) answers one read."""
    client = _mock_sonarr([], [])
    client.episode_files.side_effect = [files for files, _ in snapshots]
    client.episodes.side_effect = [eps for _, eps in snapshots]
    return client


def _sonarr_tracking(series: str, season: int, episode: int, tracked_file: str, *, extra_files=()):
    """One episode tracking `tracked_file`; `extra_files` are (id, name) episodefile rows linked to no episode."""
    # Zero-padded to match the on-disk "Season NN" folder names (full-relpath lookup).
    efiles = [_ef(200, tracked_file, season)] + [_ef(fid, n, season) for fid, n in extra_files]
    return _mock_sonarr(efiles, [_ep(episode, 200, season)],
                        series=[{"id": 1, "path": f"/tv/{series}"}])


def _default_library(tmp_path):
    """Make tmp_path/tv the CONFIGURED library: an alternate --tv-dir needs an explicit Sonarr root."""
    return patch.object(td.paths, "MEDIA_ROOT", tmp_path)


def _run(tmp_path, argv, client, **overrides):
    """td.main over tmp_path/tv with Sonarr mocked; `overrides` replace td attributes (client=None keeps ArrClient real)."""
    patches = {"_sonarr_key": MagicMock(return_value="k"),
               "probe": MagicMock(return_value={}),
               "already_processed": MagicMock(return_value=False),
               "_notify": MagicMock()}
    if client is not None:
        patches["ArrClient"] = MagicMock(return_value=client)
    patches.update(overrides)
    with ExitStack() as stack:
        stack.enter_context(_default_library(tmp_path))
        for name, new in patches.items():
            stack.enter_context(patch.object(td, name, new))
        return td.main(["--tv-dir", str(tmp_path / "tv"), *argv])


def test_safe_case_moves_untracked_leftover_and_cleans_orphan_db_row(tmp_path):
    # Sonarr tracks the WEBDL keeper; the WEBRip is the untracked leftover with its own
    # orphan episodefile DB row (id 3845).
    series = "Rick and Morty"
    season_dir = _make_season(tmp_path / "tv", series, "Season 09", {
        "Rick and Morty - S09E01 - Title WEBDL-1080p.mkv": 200 * MB,
        "Rick and Morty - S09E01 - Title WEBRip-1080p.mkv": 150 * MB,
    })
    client = _sonarr_tracking(
        series, 9, 1, "Rick and Morty - S09E01 - Title WEBDL-1080p.mkv",
        extra_files=[(3845, "Rick and Morty - S09E01 - Title WEBRip-1080p.mkv")])
    rc = _run(tmp_path, ["--apply"], client)

    assert rc == 0
    assert _videos(season_dir) == ["Rick and Morty - S09E01 - Title WEBDL-1080p.mkv"]
    recycled = (tmp_path / ".dupe-recycle" / "tv" / series / "Season 09"
                / "Rick and Morty - S09E01 - Title WEBRip-1080p.mkv")
    assert recycled.exists()
    # SAFE case must NOT rescan (Sonarr's tracked file is untouched)
    client.rescan_series.assert_not_called()
    # ...but MUST clean the orphan Sonarr DB row for the moved extra.
    client.delete_episode_file.assert_called_once_with(3845)


def test_dry_run_moves_nothing(tmp_path):
    season_dir = _pair(tmp_path)
    client = _sonarr_tracking("Show", 1, 1, KEEPER)
    rc = _run(tmp_path, [], client)   # no --apply
    assert rc == 0
    assert len(_videos(season_dir)) == 2   # untouched
    assert not (tmp_path / ".dupe-recycle").exists()
    client.delete_episode_file.assert_not_called()


def test_risky_case_flagged_not_moved_without_force(tmp_path):
    # Sonarr tracks the WEBRip (a NON-keeper): risky to re-point unattended.
    season_dir = _pair(tmp_path)
    client = _sonarr_tracking("Show", 1, 1, EXTRA)
    rc = _run(tmp_path, ["--apply"], client)   # no --force
    assert rc == 1                              # flagged -> exit 1
    assert len(_videos(season_dir)) == 2        # nothing moved
    assert not (tmp_path / ".dupe-recycle").exists()
    client.rescan_series.assert_not_called()


def test_risky_case_resolved_with_force_rescans(tmp_path):
    season_dir = _pair(tmp_path)
    # Initially only the WEBRip is tracked (read twice: tv-dedupe re-reads under the locks,
    # before the move).  After the move + RescanSeries, Sonarr tracks the WEBDL keeper.
    stale = ([_ef(200, EXTRA)], [_ep(1, 200)])
    client = _scripted_sonarr(stale, stale, ([_ef(201, KEEPER)], [_ep(1, 201)]))

    rc = _run(tmp_path, ["--apply", "--force"], client)

    assert rc == 0
    client.rescan_series.assert_called_once_with(1)
    # The moved WEBRip's own (now-stale) episodefile row gets cleaned up too.
    client.delete_episode_file.assert_called_once_with(200)
    assert _videos(season_dir) == [KEEPER]


def test_risky_force_verify_scoped_to_the_resolved_episode_not_whole_season(tmp_path):
    # A season folder holds other episodes: the post-rescan verify must count files for the
    # specific (season, episode) group only.
    other = "Show - S01E02 - B WEBDL-1080p.mkv"
    season_dir = _make_season(tmp_path / "tv", "Show", "Season 01",
                              {KEEPER: 200 * MB, EXTRA: 150 * MB, other: 180 * MB})
    stale = ([_ef(200, EXTRA), _ef(400, other)], [_ep(1, 200), _ep(2, 400)])
    client = _scripted_sonarr(stale, stale,
                              ([_ef(201, KEEPER), _ef(400, other)], [_ep(1, 201), _ep(2, 400)]))

    rc = _run(tmp_path, ["--apply", "--force"], client)

    assert rc == 0   # would have been 1 (spurious error) before the fix
    assert _videos(season_dir) == [KEEPER, other]


def test_recycle_move_does_not_overwrite_existing_recycled_file(tmp_path):
    # A same-named file already in the recycle dir must never be silently overwritten.
    season_dir = _pair(tmp_path)
    recycle_dir = tmp_path / ".dupe-recycle" / "tv" / "Show" / "Season 01"
    recycle_dir.mkdir(parents=True)
    preexisting = recycle_dir / EXTRA
    preexisting.touch()
    os.truncate(preexisting, 999 * MB)   # distinct size from the new move

    rc = _run(tmp_path, ["--apply"], _sonarr_tracking("Show", 1, 1, KEEPER))

    assert rc == 0
    assert _videos(season_dir) == [KEEPER]
    # The old recycled file must survive untouched...
    assert preexisting.exists()
    assert preexisting.stat().st_size == 999 * MB
    # ...and the newly-moved duplicate must land under a DIFFERENT name in
    # the same recycle dir (not silently discarded).
    recycled_now = sorted(p.name for p in recycle_dir.iterdir())
    assert len(recycled_now) == 2
    assert EXTRA in recycled_now


def test_locked_folder_skipped_not_moved(tmp_path):
    # A pipeline holds a per-file lock -> dedupe must NOT move the file; skip
    # the episode (retried next pass) and exit 0 (transient, not a failure).
    season_dir = _pair(tmp_path)
    client = _sonarr_tracking("Show", 1, 1, KEEPER, extra_files=[(300, EXTRA)])   # SAFE

    @contextmanager
    def held_lock(_path):
        yield False   # pretend consolidate-subs/normalize-audio holds it

    rc = _run(tmp_path, ["--apply"], client, acquire_file_lock=held_lock)

    assert rc == 0
    assert len(_videos(season_dir)) == 2   # nothing moved
    assert not (tmp_path / ".dupe-recycle").exists()
    client.delete_episode_file.assert_not_called()


def test_force_rescan_verify_failure_is_error_exit1(tmp_path):
    # RISKY + --force, but after the move+rescan Sonarr does NOT end up
    # tracking the keeper (verify fails) -> recorded as an error -> exit 1.
    _pair(tmp_path)
    stale = ([_ef(200, EXTRA)], [_ep(1, 200)])
    client = _scripted_sonarr(stale, stale, ([], [_ep(1, 0)]))   # post-rescan: keeper not picked up

    assert _run(tmp_path, ["--apply", "--force"], client) == 1   # non-zero so cron/monitoring notices


def test_no_dupes_is_noop(tmp_path):
    _make_season(tmp_path / "tv", "Show", "Season 01", {KEEPER: 1024})
    rc = _run(tmp_path, ["--apply"], _mock_sonarr([], [], series=[]))
    assert rc == 0
    assert not (tmp_path / ".dupe-recycle").exists()


@pytest.mark.parametrize("owners, deleted", [
    ({(1, 2)}, False),
    ({(1, 1)}, True),
], ids=["tracked-by-different-episode-refused", "own-stale-record-allowed"])
def test_cleanup_deletes_only_records_no_other_episode_owns(owners, deleted):
    """Safety net on top of relpath keying; (1, 1) is the episode being resolved."""
    client = MagicMock()
    client.delete_episode_file.return_value = True
    td._cleanup_orphan_episodefiles(client, {"Season 01/A.mkv": 42}, {42: owners},
                                    (1, 1), ["Season 01/A.mkv"])
    if deleted:
        client.delete_episode_file.assert_called_once_with(42)
    else:
        client.delete_episode_file.assert_not_called()


def test_unparseable_filename_excluded_but_other_dupes_still_resolved(tmp_path):
    # A file with no SxxEyy token (e.g. a bonus/special) must never be grouped or moved,
    # while a real duplicate pair in the same season folder is still resolved normally.
    season_dir = _make_season(tmp_path / "tv", "Show", "Season 01", {
        KEEPER: 200 * MB, EXTRA: 150 * MB, "Show - Special Feature.mkv": 50 * MB})
    client = _sonarr_tracking("Show", 1, 1, KEEPER, extra_files=[(300, EXTRA)])
    rc = _run(tmp_path, ["--apply"], client)

    assert rc == 0
    assert _videos(season_dir) == sorted([KEEPER, "Show - Special Feature.mkv"])


# --- pre-move ownership guard ----------------------------------------------
# The post-move cleanup guard can only refuse the Sonarr DELETE; by then the
# file has already left the library.  Ownership must be checked BEFORE moving.

@pytest.mark.parametrize("argv", [["--apply"], ["--apply", "--force"]], ids=["apply", "force"])
def test_extra_tracked_by_another_episode_is_not_moved(tmp_path, argv):
    """The extra is another episode's only library file: refuse the group, flag for review."""
    season_dir = _pair(tmp_path, OTHER_EXTRA)
    # Sonarr tracks KEEPER for S01E01 and OTHER_EXTRA for S01E02.
    client = _mock_sonarr([_ef(200, KEEPER), _ef(201, OTHER_EXTRA)], [_ep(1, 200), _ep(2, 201)])
    rc = _run(tmp_path, argv, client)

    assert rc == 1, "must flag for manual review"
    assert _videos(season_dir) == sorted([KEEPER, OTHER_EXTRA]), "nothing may be moved"
    client.delete_episode_file.assert_not_called()


def test_multi_episode_file_records_every_owner():
    """One episodefile covering two episodes must map to BOTH owners."""
    client = _mock_sonarr([_ef(7, "Show - S01E01-02 - T.mkv")], [_ep(1, 7), _ep(2, 7)])
    _, _, episode_by_file_id, tracking_ok = td._tracked_by_episode(client, 1)
    assert tracking_ok is True
    assert episode_by_file_id[7] == {(1, 1), (1, 2)}


# --- RISKY without Sonarr ---------------------------------------------------

def test_risky_force_refuses_without_sonarr_client(tmp_path):
    """--force must not move RISKY files it cannot reconcile afterwards."""
    season_dir = _pair(tmp_path, OTHER_EXTRA)
    rc = _run(tmp_path, ["--apply", "--force"], None, _sonarr_key=MagicMock(return_value=""))

    assert rc == 1, "unreconcilable RISKY group must not report success"
    assert _videos(season_dir) == sorted([KEEPER, OTHER_EXTRA]), "nothing may be moved"


# --- persistent lock files --------------------------------------------------


# --- untrusted Sonarr tracking data ----------------------------------------
# An empty tracking map from a FAILED request is not evidence that a file has no owner.


def test_failed_tracking_lookup_blocks_forced_move(tmp_path):
    """One endpoint fails (None) while the other succeeds: files look unowned."""
    season_dir = _pair(tmp_path, OTHER_EXTRA)
    client = _mock_sonarr([_ef(201, OTHER_EXTRA)], None)
    rc = _run(tmp_path, ["--apply", "--force"], client)

    assert rc == 1
    assert _videos(season_dir) == sorted([KEEPER, OTHER_EXTRA]), "nothing may be moved"
    client.delete_episode_file.assert_not_called()


# --- Sonarr series identity -------------------------------------------------

def test_single_foreign_basename_match_is_rejected(tmp_path):
    """A same-basename Sonarr series under a WRONG root must not be bound: Sonarr's DELETE removes files."""
    season_dir = _pair(tmp_path)
    client = _mock_sonarr([_ef(300, KEEPER)], [_ep(1, 300)], series=[
        {"id": 9, "path": "/unrelated-library/Show", "rootFolderPath": "/unrelated-library"}])
    # --sonarr-root pins the mapping to this library's real root.
    rc = _run(tmp_path, ["--apply", "--sonarr-root", "/tv"], client)

    assert rc == 1, "unmatched series must flag, not resolve"
    assert _videos(season_dir) == sorted([KEEPER, EXTRA]), "nothing may be moved"
    client.delete_episode_file.assert_not_called()


_TV_ONE = [{"path": "/tv/A", "rootFolderPath": "/tv"}]
_FOREIGN = [{"path": "/unrelated-library/A", "rootFolderPath": "/unrelated-library"}]


@pytest.mark.parametrize("series, explicit, kwargs, expected", [
    (_TV_ONE + [{"path": "/tv/B", "rootFolderPath": "/tv"}], None, {}, "/tv"),
    ([{"path": "/tv/A"}], None, {}, "/tv"),
    (_FOREIGN, None, {}, None),
], ids=["configured-root", "falls-back-to-path-parent", "foreign-sole-root-rejected"])
def test_accepted_sonarr_root(series, explicit, kwargs, expected):
    """A foreign sole root must not be trusted; extra roots are filtered by the per-series path check."""
    assert td._accepted_sonarr_root(series, explicit, **kwargs) == expected


# --- keeper must survive to the move ---------------------------------------

def test_keeper_vanishing_under_lock_aborts_the_move(tmp_path):
    """A keeper vanishing under the lock must abort the move, not recycle the last copy."""
    season_dir = _pair(tmp_path)
    client = _sonarr_tracking("Show", 1, 1, KEEPER, extra_files=[(300, EXTRA)])

    @contextmanager
    def vanishing_lock(path):
        # simulate a competing operation removing the keeper mid-run
        kp = season_dir / KEEPER
        if kp.exists():
            kp.unlink()
        yield True

    rc = _run(tmp_path, ["--apply"], client, acquire_file_lock=vanishing_lock)

    assert rc == 1, "a vanished keeper is an error, not a success"
    assert _videos(season_dir) == [EXTRA], "the last remaining copy must survive"
    client.delete_episode_file.assert_not_called()


# --- keeper readability -----------------------------------------------------


def test_unprobeable_keeper_blocks_recycling_the_readable_alternative(tmp_path):
    """An unreadable keeper must not get the readable alternative recycled."""
    season_dir = _pair(tmp_path)
    client = _sonarr_tracking("Show", 1, 1, KEEPER, extra_files=[(300, EXTRA)])

    def probe_by_name(path):
        return None if Path(path).name == KEEPER else {"streams": []}

    rc = _run(tmp_path, ["--apply"], client, probe=probe_by_name)

    assert rc == 1, "an unreadable keeper is an error, not a clean resolve"
    assert _videos(season_dir) == sorted([KEEPER, EXTRA]), \
        "the readable alternative must not be recycled"
    client.delete_episode_file.assert_not_called()


# --- orphan cleanup failures stay visible -----------------------------------

def test_failed_orphan_cleanup_is_reported_not_silently_resolved(tmp_path, capsys):
    series = "Rick and Morty"
    keeper = "Rick and Morty - S09E01 - Title WEBDL-1080p.mkv"
    extra = "Rick and Morty - S09E01 - Title WEBRip-1080p.mkv"
    season_dir = _make_season(tmp_path / "tv", series, "Season 09",
                              {keeper: 200 * MB, extra: 150 * MB})
    client = _sonarr_tracking(series, 9, 1, keeper, extra_files=[(3845, extra)])
    client.delete_episode_file.return_value = False   # Sonarr refuses the DELETE
    rc = _run(tmp_path, ["--apply"], client)

    out = capsys.readouterr().out
    assert "stale-rows=1" in out, "a failed DELETE must be counted, not hidden"
    assert rc == 1, "a row nothing will retry needs a human, so exit non-zero"
    # Must not promise a retry: cleanup only runs for groups that still have
    # duplicates, and the extras are already gone by now.
    assert "re-run to retry" not in out
    assert "will NOT be retried automatically" in out
    # the physical move still happened; only the DB row is stale
    assert _videos(season_dir) == [keeper]


def test_lock_file_survives_dedupe(tmp_path):
    """Unlinking a released lock lets two workers lock the same media path."""
    season_dir = _pair(tmp_path)
    client = _sonarr_tracking("Show", 1, 1, KEEPER, extra_files=[(3845, EXTRA)])
    rc = _run(tmp_path, ["--apply"], client)

    assert rc == 0
    assert _videos(season_dir) == [KEEPER]
    from media_stack.locking import lock_path_for
    assert lock_path_for(season_dir / EXTRA).exists(), \
        "the moved extra's lock file must be left in place"


def test_tracking_change_between_scan_and_move_aborts(tmp_path):
    """Sonarr tracking the EXTRA after the snapshot but before the move must abort the move."""
    season_dir = _pair(tmp_path)
    client = _scripted_sonarr(
        ([_ef(300, KEEPER)], [_ep(1, 300)]),   # snapshot: SAFE
        ([_ef(301, EXTRA)], [_ep(1, 301)]))    # re-read: changed
    _run(tmp_path, ["--apply"], client)

    assert _videos(season_dir) == sorted([KEEPER, EXTRA]), "nothing may be moved"
    client.delete_episode_file.assert_not_called()
