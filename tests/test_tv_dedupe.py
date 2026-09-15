"""Tests for tv-dedupe.py — the daily TV episode de-dup orchestration.

Sonarr sibling of tests/test_movie_dedupe.py.  Drives the SAFE/RISKY
classification and the move-to-recycle behaviour with a real temp tv tree
and a mocked ArrClient (no network, no ffprobe, no real media).  The
recycle lands beside the temp tv dir, so nothing touches the live media
root.  Also covers the TV-specific wrinkle movie-dedupe.py never needed:
cleaning up Sonarr's own orphan `episodefile` DB row after a move.
"""
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
_spec = importlib.util.spec_from_file_location(
    "tv_dedupe", str(PROJECT_ROOT / "tv-dedupe.py"))
td = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(td)


def _make_season(tv: Path, series: str, season: str, files: dict[str, int]) -> Path:
    # Sparse files (touch + truncate) instead of real byte content — the
    # size is all _video_meta() reads (probe/already_processed are mocked
    # in every test here), so writing real 100-200MB payloads just slows
    # the suite and burns disk for no coverage benefit (codex review #4).
    d = tv / series / season
    d.mkdir(parents=True)
    for name, size in files.items():
        p = d / name
        p.touch()
        os.truncate(p, size)
    return d


def _videos(d: Path) -> list[str]:
    """Real video files in a folder (excludes dotfiles like the persisted
    .consolidate-*.lock, which a glob like *.m* would wrongly match)."""
    return sorted(p.name for p in d.iterdir() if td.is_video(p.name))


def _sonarr_tracking(series: str, season: int, episode: int, tracked_file: str,
                     *, extra_files=(), has_file=True, series_id=1, tracked_file_id=200):
    """A fake ArrClient whose series()/episodes()/episode_files() report one
    series with one episode (season, episode) tracking `tracked_file`.
    `extra_files` are additional (id, name) episodefile records that exist
    in Sonarr's DB but aren't linked to any episode — the orphan rows this
    tool is responsible for cleaning up via delete_episode_file()."""
    client = MagicMock()
    client.series.return_value = [{"id": series_id, "path": f"/tv/{series}"}]
    client.episodes.return_value = [{
        "seasonNumber": season, "episodeNumber": episode,
        "hasFile": has_file, "episodeFileId": tracked_file_id if has_file else 0,
    }]
    # Zero-padded to match the on-disk "Season NN" folder names used
    # throughout these tests — a mismatch here was invisible under the old
    # basename-only lookup but would silently break the new full-relpath
    # lookup (codex review #2 fix).
    efiles = []
    if has_file:
        efiles.append({"id": tracked_file_id,
                       "relativePath": f"Season {season:02d}/{tracked_file}"})
    for fid, name in extra_files:
        efiles.append({"id": fid, "relativePath": f"Season {season:02d}/{name}"})
    client.episode_files.return_value = efiles
    client.rescan_series.return_value = True
    client.delete_episode_file.return_value = True
    return client


def _default_library(tmp_path):
    """Treat tmp_path/tv as the CONFIGURED library dir.

    tv-dedupe only auto-detects Sonarr's root for the default library; an
    alternate --tv-dir must state the mapping explicitly (otherwise a local
    tree could bind to an unrelated Sonarr library).  Tests scan a tmp dir, so
    point MEDIA_ROOT at it rather than weakening that guard.
    """
    return patch.object(td.paths, "MEDIA_ROOT", tmp_path)


def _run(tmp_path, argv, client):
    tv = tmp_path / "tv"
    with _default_library(tmp_path), \
         patch.object(td, "ArrClient", return_value=client), \
         patch.object(td, "_sonarr_key", return_value="k"), \
         patch.object(td, "probe", return_value={}), \
         patch.object(td, "already_processed", return_value=False), \
         patch.object(td, "_notify"):
        rc = td.main(["--tv-dir", str(tv), *argv])
    return rc, tv


def test_safe_case_moves_untracked_leftover_and_cleans_orphan_db_row(tmp_path):
    # Sonarr tracks the WEBDL keeper; the WEBRip is the untracked leftover
    # AND has its own orphan episodefile DB row (id 3845 — the exact shape
    # observed live for Rick and Morty S09E01).
    series = "Rick and Morty"
    season_dir = _make_season(tmp_path / "tv", series, "Season 09", {
        "Rick and Morty - S09E01 - Title WEBDL-1080p.mkv": 200 * 1024 * 1024,
        "Rick and Morty - S09E01 - Title WEBRip-1080p.mkv": 150 * 1024 * 1024,
    })
    client = _sonarr_tracking(
        series, 9, 1, "Rick and Morty - S09E01 - Title WEBDL-1080p.mkv",
        extra_files=[(3845, "Rick and Morty - S09E01 - Title WEBRip-1080p.mkv")])
    rc, tv = _run(tmp_path, ["--apply"], client)

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
    series = "Show"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01", {
        "Show - S01E01 - A WEBDL-1080p.mkv": 200 * 1024 * 1024,
        "Show - S01E01 - A WEBRip-1080p.mkv": 150 * 1024 * 1024,
    })
    client = _sonarr_tracking(series, 1, 1, "Show - S01E01 - A WEBDL-1080p.mkv")
    rc, tv = _run(tmp_path, [], client)   # no --apply
    assert rc == 0
    assert len(_videos(season_dir)) == 2   # untouched
    assert not (tmp_path / ".dupe-recycle").exists()
    client.delete_episode_file.assert_not_called()


def test_risky_case_flagged_not_moved_without_force(tmp_path):
    # Sonarr tracks the WEBRip (a NON-keeper) — risky to re-point unattended.
    series = "Show"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01", {
        "Show - S01E01 - A WEBDL-1080p.mkv": 200 * 1024 * 1024,
        "Show - S01E01 - A WEBRip-1080p.mkv": 150 * 1024 * 1024,
    })
    client = _sonarr_tracking(series, 1, 1, "Show - S01E01 - A WEBRip-1080p.mkv")
    rc, tv = _run(tmp_path, ["--apply"], client)   # no --force
    assert rc == 1                                  # flagged -> exit 1
    assert len(_videos(season_dir)) == 2            # nothing moved
    assert not (tmp_path / ".dupe-recycle").exists()
    client.rescan_series.assert_not_called()


def test_risky_case_resolved_with_force_rescans(tmp_path):
    series = "Show"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01", {
        "Show - S01E01 - A WEBDL-1080p.mkv": 200 * 1024 * 1024,
        "Show - S01E01 - A WEBRip-1080p.mkv": 150 * 1024 * 1024,
    })
    client = MagicMock()
    client.series.return_value = [{"id": 1, "path": f"/tv/{series}"}]
    # Initial state: only the WEBRip is known to Sonarr (tracked).  After the
    # move + RescanSeries, Sonarr discovers the WEBDL keeper and tracks it.
    client.episode_files.side_effect = [
        [{"id": 200, "relativePath": "Season 01/Show - S01E01 - A WEBRip-1080p.mkv"}],
        # (repeated: tv-dedupe re-reads tracking under the locks, before the move)
        [{"id": 200, "relativePath": "Season 01/Show - S01E01 - A WEBRip-1080p.mkv"}],
        [{"id": 201, "relativePath": "Season 01/Show - S01E01 - A WEBDL-1080p.mkv"}],
    ]
    client.episodes.side_effect = [
        [{"seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 200}],
        # (repeated: tv-dedupe re-reads tracking under the locks, before the move)
        [{"seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 200}],
        [{"seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 201}],
    ]
    client.rescan_series.return_value = True
    client.delete_episode_file.return_value = True

    with patch.object(td.time, "sleep"):
        rc, tv = _run(tmp_path, ["--apply", "--force"], client)

    assert rc == 0
    client.rescan_series.assert_called_once_with(1)
    # The moved WEBRip's own (now-stale) episodefile row gets cleaned up too.
    client.delete_episode_file.assert_called_once_with(200)
    assert _videos(season_dir) == ["Show - S01E01 - A WEBDL-1080p.mkv"]


def test_risky_force_verify_scoped_to_the_resolved_episode_not_whole_season(tmp_path):
    # codex review #3 — a season folder almost always has OTHER episodes.
    # The post-rescan verify must count remaining files for the SPECIFIC
    # (season, episode) group being resolved, not every video in the
    # season folder, or it spuriously reports an error even when the
    # rescan succeeded correctly.
    series = "Show"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01", {
        "Show - S01E01 - A WEBDL-1080p.mkv": 200 * 1024 * 1024,
        "Show - S01E01 - A WEBRip-1080p.mkv": 150 * 1024 * 1024,
        "Show - S01E02 - B WEBDL-1080p.mkv": 180 * 1024 * 1024,   # unrelated episode
    })
    client = MagicMock()
    client.series.return_value = [{"id": 1, "path": f"/tv/{series}"}]
    client.episode_files.side_effect = [
        [{"id": 200, "relativePath": "Season 01/Show - S01E01 - A WEBRip-1080p.mkv"},
         {"id": 400, "relativePath": "Season 01/Show - S01E02 - B WEBDL-1080p.mkv"}],
        # (repeated: tv-dedupe re-reads tracking under the locks, before the move)
        [{"id": 200, "relativePath": "Season 01/Show - S01E01 - A WEBRip-1080p.mkv"},
         {"id": 400, "relativePath": "Season 01/Show - S01E02 - B WEBDL-1080p.mkv"}],
        [{"id": 201, "relativePath": "Season 01/Show - S01E01 - A WEBDL-1080p.mkv"},
         {"id": 400, "relativePath": "Season 01/Show - S01E02 - B WEBDL-1080p.mkv"}],
    ]
    client.episodes.side_effect = [
        [{"seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 200},
         {"seasonNumber": 1, "episodeNumber": 2, "hasFile": True, "episodeFileId": 400}],
        # (repeated: tv-dedupe re-reads tracking under the locks, before the move)
        [{"seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 200},
         {"seasonNumber": 1, "episodeNumber": 2, "hasFile": True, "episodeFileId": 400}],
        [{"seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 201},
         {"seasonNumber": 1, "episodeNumber": 2, "hasFile": True, "episodeFileId": 400}],
    ]
    client.rescan_series.return_value = True
    client.delete_episode_file.return_value = True

    with patch.object(td.time, "sleep"):
        rc, tv = _run(tmp_path, ["--apply", "--force"], client)

    assert rc == 0   # would have been 1 (spurious error) before the fix
    assert sorted(_videos(season_dir)) == [
        "Show - S01E01 - A WEBDL-1080p.mkv",
        "Show - S01E02 - B WEBDL-1080p.mkv",
    ]


def test_recycle_move_does_not_overwrite_existing_recycled_file(tmp_path):
    # codex review #2 — a same-named file already sitting in the recycle
    # dir (e.g. from a previous run) must never be silently overwritten.
    series = "Show"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01", {
        "Show - S01E01 - A WEBDL-1080p.mkv": 200 * 1024 * 1024,
        "Show - S01E01 - A WEBRip-1080p.mkv": 150 * 1024 * 1024,
    })
    recycle_dir = tmp_path / ".dupe-recycle" / "tv" / series / "Season 01"
    recycle_dir.mkdir(parents=True)
    preexisting = recycle_dir / "Show - S01E01 - A WEBRip-1080p.mkv"
    preexisting.touch()
    os.truncate(preexisting, 999 * 1024 * 1024)   # distinct size from the new move

    client = _sonarr_tracking(series, 1, 1, "Show - S01E01 - A WEBDL-1080p.mkv")
    rc, tv = _run(tmp_path, ["--apply"], client)

    assert rc == 0
    assert _videos(season_dir) == ["Show - S01E01 - A WEBDL-1080p.mkv"]
    # The old recycled file must survive untouched...
    assert preexisting.exists()
    assert preexisting.stat().st_size == 999 * 1024 * 1024
    # ...and the newly-moved duplicate must land under a DIFFERENT name in
    # the same recycle dir (not silently discarded).
    recycled_now = sorted(p.name for p in recycle_dir.iterdir())
    assert len(recycled_now) == 2
    assert "Show - S01E01 - A WEBRip-1080p.mkv" in recycled_now


def test_locked_folder_skipped_not_moved(tmp_path):
    # A pipeline holds a per-file lock -> dedupe must NOT move the file; skip
    # the episode (retried next pass) and exit 0 (transient, not a failure).
    from contextlib import contextmanager
    series = "Show"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01", {
        "Show - S01E01 - A WEBDL-1080p.mkv": 200 * 1024 * 1024,
        "Show - S01E01 - A WEBRip-1080p.mkv": 150 * 1024 * 1024,
    })
    client = _sonarr_tracking(
        series, 1, 1, "Show - S01E01 - A WEBDL-1080p.mkv",   # SAFE
        extra_files=[(300, "Show - S01E01 - A WEBRip-1080p.mkv")])

    @contextmanager
    def held_lock(_path):
        yield False   # pretend consolidate-subs/normalize-audio holds it

    with _default_library(tmp_path), \
         patch.object(td, "ArrClient", return_value=client), \
         patch.object(td, "_sonarr_key", return_value="k"), \
         patch.object(td, "probe", return_value={}), \
         patch.object(td, "already_processed", return_value=False), \
         patch.object(td, "_notify"), \
         patch.object(td, "acquire_file_lock", held_lock):
        rc = td.main(["--tv-dir", str(tmp_path / "tv"), "--apply"])

    assert rc == 0
    assert len(_videos(season_dir)) == 2   # nothing moved
    assert not (tmp_path / ".dupe-recycle").exists()
    client.delete_episode_file.assert_not_called()


def test_force_rescan_verify_failure_is_error_exit1(tmp_path):
    # RISKY + --force, but after the move+rescan Sonarr does NOT end up
    # tracking the keeper (verify fails) -> recorded as an error -> exit 1.
    series = "Show"
    _make_season(tmp_path / "tv", series, "Season 01", {
        "Show - S01E01 - A WEBDL-1080p.mkv": 200 * 1024 * 1024,
        "Show - S01E01 - A WEBRip-1080p.mkv": 150 * 1024 * 1024,
    })
    client = MagicMock()
    client.series.return_value = [{"id": 1, "path": f"/tv/{series}"}]
    client.episode_files.side_effect = [
        [{"id": 200, "relativePath": "Season 01/Show - S01E01 - A WEBRip-1080p.mkv"}],
        # (repeated: tv-dedupe re-reads tracking under the locks, before the move)
        [{"id": 200, "relativePath": "Season 01/Show - S01E01 - A WEBRip-1080p.mkv"}],
        [],   # post-rescan: Sonarr still hasn't picked up the keeper
    ]
    client.episodes.side_effect = [
        [{"seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 200}],
        # (repeated: tv-dedupe re-reads tracking under the locks, before the move)
        [{"seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 200}],
        [{"seasonNumber": 1, "episodeNumber": 1, "hasFile": False, "episodeFileId": 0}],
    ]
    client.rescan_series.return_value = True
    client.delete_episode_file.return_value = True

    with patch.object(td.time, "sleep"):
        rc, tv = _run(tmp_path, ["--apply", "--force"], client)
    assert rc == 1   # error -> non-zero so cron/monitoring notices


def test_no_dupes_is_noop(tmp_path):
    series = "Show"
    _make_season(tmp_path / "tv", series, "Season 01",
                 {"Show - S01E01 - A WEBDL-1080p.mkv": 1024})
    client = MagicMock()
    client.series.return_value = []
    rc, tv = _run(tmp_path, ["--apply"], client)
    assert rc == 0
    assert not (tmp_path / ".dupe-recycle").exists()


def test_extra_delete_uses_full_relpath_not_bare_basename_across_seasons(tmp_path):
    # codex review #2 — two different seasons of the same series can each
    # independently contain a file with the SAME basename.  Cleanup must key
    # off the full relativePath: resolving the Season 01 duplicate must
    # NEVER delete a same-named Season 02 record.
    series = "Show"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01", {
        "Show - S01E01 - A WEBDL-1080p.mkv": 200 * 1024 * 1024,
        "Show - S01E01 - A WEBRip-1080p.mkv": 150 * 1024 * 1024,
    })
    client = MagicMock()
    client.series.return_value = [{"id": 1, "path": f"/tv/{series}"}]
    client.episodes.return_value = [
        {"seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 200},
    ]
    client.episode_files.return_value = [
        {"id": 200, "relativePath": "Season 01/Show - S01E01 - A WEBDL-1080p.mkv"},
        {"id": 300, "relativePath": "Season 01/Show - S01E01 - A WEBRip-1080p.mkv"},
        # Same basename as id=300, but a DIFFERENT season — must never be
        # targeted when resolving the Season 01 duplicate.
        {"id": 999, "relativePath": "Season 02/Show - S01E01 - A WEBRip-1080p.mkv"},
    ]
    client.rescan_series.return_value = True
    client.delete_episode_file.return_value = True

    rc, tv = _run(tmp_path, ["--apply"], client)

    assert rc == 0
    assert _videos(season_dir) == ["Show - S01E01 - A WEBDL-1080p.mkv"]
    client.delete_episode_file.assert_called_once_with(300)


def test_cleanup_refuses_to_delete_a_file_tracked_by_a_different_episode():
    # Independent safety net on top of relpath keying: refuse to delete a
    # record that's still the tracked file for a DIFFERENT episode.
    client = MagicMock()
    client.delete_episode_file.return_value = True
    file_id_by_relpath = {"Season 01/A.mkv": 42}
    episode_by_file_id = {42: {(1, 2)}}  # tracked by S01E02, not the S01E01 we're resolving
    td._cleanup_orphan_episodefiles(client, file_id_by_relpath, episode_by_file_id,
                                    (1, 1), ["Season 01/A.mkv"])
    client.delete_episode_file.assert_not_called()


def test_cleanup_allows_deleting_the_current_episodes_own_stale_record():
    # The RISKY+force self-cleanup case: the record IS still tracked, but
    # by the SAME episode being resolved (its own now-stale file, moved a
    # moment ago) — this must be ALLOWED, not refused.
    client = MagicMock()
    client.delete_episode_file.return_value = True
    file_id_by_relpath = {"Season 01/A.mkv": 42}
    episode_by_file_id = {42: {(1, 1)}}
    td._cleanup_orphan_episodefiles(client, file_id_by_relpath, episode_by_file_id,
                                    (1, 1), ["Season 01/A.mkv"])
    client.delete_episode_file.assert_called_once_with(42)


def test_unparseable_filename_excluded_but_other_dupes_still_resolved(tmp_path):
    # A file with no SxxEyy token (e.g. a bonus/special) must never be
    # grouped or moved, while a real duplicate pair elsewhere in the same
    # season folder is still resolved normally.
    series = "Show"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01", {
        "Show - S01E01 - A WEBDL-1080p.mkv": 200 * 1024 * 1024,
        "Show - S01E01 - A WEBRip-1080p.mkv": 150 * 1024 * 1024,
        "Show - Special Feature.mkv": 50 * 1024 * 1024,
    })
    client = _sonarr_tracking(
        series, 1, 1, "Show - S01E01 - A WEBDL-1080p.mkv",
        extra_files=[(300, "Show - S01E01 - A WEBRip-1080p.mkv")])
    rc, tv = _run(tmp_path, ["--apply"], client)

    assert rc == 0
    remaining = _videos(season_dir)
    assert "Show - Special Feature.mkv" in remaining          # untouched
    assert "Show - S01E01 - A WEBDL-1080p.mkv" in remaining
    assert "Show - S01E01 - A WEBRip-1080p.mkv" not in remaining  # moved


# --- pre-move ownership guard ----------------------------------------------
# The post-move cleanup guard can only refuse the Sonarr DELETE; by then the
# file has already left the library.  Ownership must be checked BEFORE moving.

def _multi_owner_client(series, keeper, extra, *, keeper_id=200, extra_id=201):
    """Sonarr tracks `keeper` for S01E01 and `extra` for S01E02."""
    client = MagicMock()
    client.series.return_value = [{"id": 1, "path": f"/tv/{series}"}]
    client.episodes.return_value = [
        {"seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": keeper_id},
        {"seasonNumber": 1, "episodeNumber": 2, "hasFile": True, "episodeFileId": extra_id},
    ]
    client.episode_files.return_value = [
        {"id": keeper_id, "relativePath": f"Season 01/{keeper}"},
        {"id": extra_id, "relativePath": f"Season 01/{extra}"},
    ]
    client.rescan_series.return_value = True
    client.delete_episode_file.return_value = True
    return client


def test_extra_tracked_by_another_episode_is_not_moved(tmp_path):
    """The extra is another episode's only library file — refuse the group."""
    series = "Show"
    keeper = "Show - S01E01 - A WEBDL-1080p.mkv"
    extra = "Show - S01E01 - B WEBRip-720p.mkv"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01",
                              {keeper: 200 * 1024 * 1024, extra: 150 * 1024 * 1024})
    client = _multi_owner_client(series, keeper, extra)
    rc, tv = _run(tmp_path, ["--apply"], client)

    assert rc == 1, "must flag for manual review"
    assert _videos(season_dir) == sorted([keeper, extra]), "nothing may be moved"
    client.delete_episode_file.assert_not_called()


def test_extra_tracked_by_another_episode_not_moved_even_with_force(tmp_path):
    series = "Show"
    keeper = "Show - S01E01 - A WEBDL-1080p.mkv"
    extra = "Show - S01E01 - B WEBRip-720p.mkv"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01",
                              {keeper: 200 * 1024 * 1024, extra: 150 * 1024 * 1024})
    client = _multi_owner_client(series, keeper, extra)
    rc, tv = _run(tmp_path, ["--apply", "--force"], client)

    assert rc == 1
    assert _videos(season_dir) == sorted([keeper, extra])


def test_extras_owned_by_other_episode_helper():
    file_id_by_relpath = {"Season 01/a.mkv": 10, "Season 01/b.mkv": 11}
    episode_by_file_id = {10: {(1, 1)}, 11: {(1, 2)}}
    assert td._extras_owned_by_other_episode(
        file_id_by_relpath, episode_by_file_id, (1, 1), "Season 01",
        [{"name": "b.mkv"}]) == ["S01E02"]
    # the current episode's own file is not a blocker
    assert td._extras_owned_by_other_episode(
        file_id_by_relpath, episode_by_file_id, (1, 1), "Season 01",
        [{"name": "a.mkv"}]) == []
    # a file Sonarr doesn't know about is not a blocker
    assert td._extras_owned_by_other_episode(
        file_id_by_relpath, episode_by_file_id, (1, 1), "Season 01",
        [{"name": "unknown.mkv"}]) == []


def test_multi_episode_file_records_every_owner():
    """One episodefile covering two episodes must map to BOTH; the old dict
    assignment kept only the last and made the file look unowned."""
    client = MagicMock()
    client.episode_files.return_value = [
        {"id": 7, "relativePath": "Season 01/Show - S01E01-02 - T.mkv"}]
    client.episodes.return_value = [
        {"seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 7},
        {"seasonNumber": 1, "episodeNumber": 2, "hasFile": True, "episodeFileId": 7},
    ]
    _, _, episode_by_file_id, tracking_ok = td._tracked_by_episode(client, 1)
    assert tracking_ok is True
    assert episode_by_file_id[7] == {(1, 1), (1, 2)}


def test_cleanup_refuses_when_any_other_owner_remains():
    client = MagicMock()
    client.delete_episode_file.return_value = True
    td._cleanup_orphan_episodefiles(
        client, {"Season 01/x.mkv": 42}, {42: {(1, 1), (1, 2)}},
        (1, 1), ["Season 01/x.mkv"])
    client.delete_episode_file.assert_not_called()


# --- RISKY without Sonarr ---------------------------------------------------

def test_risky_force_refuses_without_sonarr_client(tmp_path):
    """--force must not move RISKY files it cannot reconcile afterwards."""
    series = "Show"
    a = "Show - S01E01 - A WEBDL-1080p.mkv"
    b = "Show - S01E01 - B WEBRip-720p.mkv"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01",
                              {a: 200 * 1024 * 1024, b: 150 * 1024 * 1024})
    tv = tmp_path / "tv"
    with _default_library(tmp_path), \
         patch.object(td, "_sonarr_key", return_value=""), \
         patch.object(td, "probe", return_value={}), \
         patch.object(td, "already_processed", return_value=False), \
         patch.object(td, "_notify"):
        rc = td.main(["--tv-dir", str(tv), "--apply", "--force"])

    assert rc == 1, "unreconcilable RISKY group must not report success"
    assert _videos(season_dir) == sorted([a, b]), "nothing may be moved"


# --- persistent lock files --------------------------------------------------

def test_lock_file_survives_dedupe(tmp_path):
    """Unlinking a released lock lets two workers lock the same media path."""
    series = "Rick and Morty"
    keeper = "Rick and Morty - S09E01 - Title WEBDL-1080p.mkv"
    extra = "Rick and Morty - S09E01 - Title WEBRip-1080p.mkv"
    season_dir = _make_season(tmp_path / "tv", series, "Season 09",
                              {keeper: 200 * 1024 * 1024, extra: 150 * 1024 * 1024})
    client = _sonarr_tracking(series, 9, 1, keeper, extra_files=[(3845, extra)])
    rc, tv = _run(tmp_path, ["--apply"], client)

    assert rc == 0
    assert _videos(season_dir) == [keeper]
    from media_stack.locking import lock_path_for
    assert lock_path_for(season_dir / extra).exists(), \
        "the moved extra's lock file must be left in place"


# --- untrusted Sonarr tracking data ----------------------------------------
# An empty tracking map from a FAILED request is not evidence that a file has
# no owner.  Both of these previously let --force mutate on bad data.

def test_ambiguous_series_basename_is_not_resolved_to_a_foreign_series(tmp_path):
    """Two Sonarr series share a folder basename under different roots."""
    series = "Show"
    keeper = "Show - S01E01 - A WEBDL-1080p.mkv"
    extra = "Show - S01E01 - B WEBRip-720p.mkv"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01",
                              {keeper: 200 * 1024 * 1024, extra: 150 * 1024 * 1024})
    client = MagicMock()
    client.series.return_value = [
        {"id": 1, "path": "/data/media/tv/Show"},
        {"id": 2, "path": "/data/media/other/Show"},     # same basename
    ]
    # If the wrong series won, this tracking data would classify the group SAFE.
    client.episodes.return_value = [
        {"seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 201}]
    client.episode_files.return_value = [
        {"id": 201, "relativePath": f"Season 01/{keeper}"}]
    client.delete_episode_file.return_value = True
    rc, tv = _run(tmp_path, ["--apply"], client)

    assert rc == 1, "ambiguous series must be flagged, not silently resolved"
    assert _videos(season_dir) == sorted([keeper, extra]), "nothing may be moved"
    client.delete_episode_file.assert_not_called()
    client.episodes.assert_not_called()


def test_failed_episodes_lookup_blocks_forced_move(tmp_path):
    """episodes() fails, episode_files() succeeds: files look unowned."""
    series = "Show"
    keeper = "Show - S01E01 - A WEBDL-1080p.mkv"
    extra = "Show - S01E01 - B WEBRip-720p.mkv"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01",
                              {keeper: 200 * 1024 * 1024, extra: 150 * 1024 * 1024})
    client = MagicMock()
    client.series.return_value = [{"id": 1, "path": f"/tv/{series}"}]
    client.episodes.return_value = None                   # transient failure
    client.episode_files.return_value = [
        {"id": 201, "relativePath": f"Season 01/{extra}"}]
    client.rescan_series.return_value = True
    client.delete_episode_file.return_value = True
    rc, tv = _run(tmp_path, ["--apply", "--force"], client)

    assert rc == 1
    assert _videos(season_dir) == sorted([keeper, extra]), "nothing may be moved"
    client.delete_episode_file.assert_not_called()


def test_failed_episode_files_lookup_blocks_forced_move(tmp_path):
    """The mirror case: episode_files() fails, episodes() succeeds."""
    series = "Show"
    keeper = "Show - S01E01 - A WEBDL-1080p.mkv"
    extra = "Show - S01E01 - B WEBRip-720p.mkv"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01",
                              {keeper: 200 * 1024 * 1024, extra: 150 * 1024 * 1024})
    client = MagicMock()
    client.series.return_value = [{"id": 1, "path": f"/tv/{series}"}]
    client.episodes.return_value = [
        {"seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 201}]
    client.episode_files.return_value = None              # transient failure
    client.rescan_series.return_value = True
    client.delete_episode_file.return_value = True
    rc, tv = _run(tmp_path, ["--apply", "--force"], client)

    assert rc == 1
    assert _videos(season_dir) == sorted([keeper, extra])
    client.delete_episode_file.assert_not_called()


def test_tracking_ok_false_when_either_endpoint_fails():
    client = MagicMock()
    client.episode_files.return_value = []
    client.episodes.return_value = None
    assert td._tracked_by_episode(client, 1)[3] is False

    client.episode_files.return_value = None
    client.episodes.return_value = []
    assert td._tracked_by_episode(client, 1)[3] is False

    client.episode_files.return_value = []
    client.episodes.return_value = []
    assert td._tracked_by_episode(client, 1)[3] is True, \
        "genuinely empty is usable; only a failed request is not"


# --- Sonarr series identity -------------------------------------------------

def test_single_foreign_basename_match_is_rejected(tmp_path):
    """One Sonarr series, right basename, WRONG library root.

    Sonarr's DELETE removes the backing file, so binding a local folder to an
    unrelated series' records can destroy that other library's media.
    """
    series = "Show"
    keeper = "Show - S01E01 - A WEBDL-1080p.mkv"
    extra = "Show - S01E01 - A WEBRip-1080p.mkv"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01",
                              {keeper: 200 * 1024 * 1024, extra: 150 * 1024 * 1024})
    client = MagicMock()
    client.series.return_value = [
        {"id": 9, "path": "/unrelated-library/Show", "rootFolderPath": "/unrelated-library"}]
    client.episodes.return_value = [
        {"seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 300}]
    client.episode_files.return_value = [
        {"id": 300, "relativePath": f"Season 01/{keeper}"}]
    client.delete_episode_file.return_value = True
    # --sonarr-root pins the mapping to this library's real root.
    tv = tmp_path / "tv"
    with _default_library(tmp_path), \
         patch.object(td, "ArrClient", return_value=client), \
         patch.object(td, "_sonarr_key", return_value="k"), \
         patch.object(td, "probe", return_value={}), \
         patch.object(td, "already_processed", return_value=False), \
         patch.object(td, "_notify"):
        rc = td.main(["--tv-dir", str(tv), "--apply", "--sonarr-root", "/tv"])

    assert rc == 1, "unmatched series must flag, not resolve"
    assert _videos(season_dir) == sorted([keeper, extra]), "nothing may be moved"
    client.delete_episode_file.assert_not_called()


def test_accepted_sonarr_root_uses_the_configured_root():
    one = [{"path": "/tv/A", "rootFolderPath": "/tv"},
           {"path": "/tv/B", "rootFolderPath": "/tv"}]
    assert td._accepted_sonarr_root(one, None) == "/tv"
    # falls back to the path's parent when rootFolderPath is absent
    assert td._accepted_sonarr_root([{"path": "/tv/A"}], None) == "/tv"


def test_accepted_sonarr_root_rejects_a_foreign_sole_root():
    """The bug: trusting "the only root Sonarr returned" binds an unrelated
    library when the local tree is not the one Sonarr serves."""
    foreign = [{"path": "/unrelated-library/A", "rootFolderPath": "/unrelated-library"}]
    assert td._accepted_sonarr_root(foreign, None) is None
    assert td._accepted_sonarr_root(foreign, "/unrelated-library") == "/unrelated-library", \
        "an explicit mapping is still honoured"


def test_accepted_sonarr_root_requires_explicit_mapping_for_alternate_library():
    one = [{"path": "/tv/A", "rootFolderPath": "/tv"}]
    assert td._accepted_sonarr_root(one, None, is_default_library=False) is None
    assert td._accepted_sonarr_root(one, "/tv", is_default_library=False) == "/tv"


def test_accepted_sonarr_root_ignores_extra_roots_alongside_the_configured_one():
    mixed = [{"path": "/tv/A", "rootFolderPath": "/tv"},
             {"path": "/other/B", "rootFolderPath": "/other"}]
    assert td._accepted_sonarr_root(mixed, None) == "/tv", \
        "series outside the root are filtered by the per-series path check"


# --- keeper must survive to the move ---------------------------------------

def test_keeper_vanishing_under_lock_aborts_the_move(tmp_path):
    """Locks covered only the extras, so the keeper could disappear mid-run
    and dedupe would recycle the last remaining copy of the episode."""
    from contextlib import contextmanager
    series = "Show"
    keeper = "Show - S01E01 - A WEBDL-1080p.mkv"
    extra = "Show - S01E01 - A WEBRip-1080p.mkv"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01",
                              {keeper: 200 * 1024 * 1024, extra: 150 * 1024 * 1024})
    client = _sonarr_tracking(series, 1, 1, keeper, extra_files=[(300, extra)])

    @contextmanager
    def vanishing_lock(path):
        # simulate a competing operation removing the keeper mid-run
        kp = season_dir / keeper
        if kp.exists():
            kp.unlink()
        yield True

    with _default_library(tmp_path), \
         patch.object(td, "ArrClient", return_value=client), \
         patch.object(td, "_sonarr_key", return_value="k"), \
         patch.object(td, "probe", return_value={}), \
         patch.object(td, "already_processed", return_value=False), \
         patch.object(td, "_notify"), \
         patch.object(td, "acquire_file_lock", vanishing_lock):
        rc = td.main(["--tv-dir", str(tmp_path / "tv"), "--apply"])

    assert rc == 1, "a vanished keeper is an error, not a success"
    assert _videos(season_dir) == [extra], "the last remaining copy must survive"
    client.delete_episode_file.assert_not_called()


def test_keeper_is_locked_alongside_the_extras(tmp_path):
    from contextlib import contextmanager
    locked: list[str] = []
    series = "Show"
    keeper = "Show - S01E01 - A WEBDL-1080p.mkv"
    extra = "Show - S01E01 - A WEBRip-1080p.mkv"
    _make_season(tmp_path / "tv", series, "Season 01",
                 {keeper: 200 * 1024 * 1024, extra: 150 * 1024 * 1024})
    client = _sonarr_tracking(series, 1, 1, keeper, extra_files=[(300, extra)])

    @contextmanager
    def recording_lock(path):
        locked.append(Path(path).name)
        yield True

    with _default_library(tmp_path), \
         patch.object(td, "ArrClient", return_value=client), \
         patch.object(td, "_sonarr_key", return_value="k"), \
         patch.object(td, "probe", return_value={}), \
         patch.object(td, "already_processed", return_value=False), \
         patch.object(td, "_notify"), \
         patch.object(td, "acquire_file_lock", recording_lock):
        td.main(["--tv-dir", str(tmp_path / "tv"), "--apply"])

    assert keeper in locked, "the keeper must be locked, not just the extras"
    assert extra in locked
    assert locked == sorted(locked), "consistent lock ordering"


def test_tracking_change_between_scan_and_move_aborts(tmp_path):
    """Sonarr starts tracking the EXTRA after the snapshot but before the move.

    The snapshot said SAFE; acting on it would recycle Sonarr's tracked file
    and delete its record while reporting success.
    """
    series = "Show"
    keeper = "Show - S01E01 - A WEBDL-1080p.mkv"
    extra = "Show - S01E01 - A WEBRip-1080p.mkv"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01",
                              {keeper: 200 * 1024 * 1024, extra: 150 * 1024 * 1024})
    client = MagicMock()
    client.series.return_value = [{"id": 1, "path": f"/tv/{series}"}]
    client.episode_files.side_effect = [
        [{"id": 300, "relativePath": f"Season 01/{keeper}"}],   # snapshot: SAFE
        [{"id": 301, "relativePath": f"Season 01/{extra}"}],    # re-read: changed
    ]
    client.episodes.side_effect = [
        [{"seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 300}],
        [{"seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "episodeFileId": 301}],
    ]
    client.rescan_series.return_value = True
    client.delete_episode_file.return_value = True
    rc, tv = _run(tmp_path, ["--apply"], client)

    assert _videos(season_dir) == sorted([keeper, extra]), "nothing may be moved"
    client.delete_episode_file.assert_not_called()


# --- keeper readability -----------------------------------------------------

def test_video_meta_records_probe_success_separately(tmp_path):
    """A failed probe must not look like "readable but untagged"."""
    d = tmp_path / "s"
    d.mkdir()
    (d / "x.mkv").write_bytes(b"x")
    with patch.object(td, "probe", return_value=None), \
         patch.object(td, "already_processed", return_value=False):
        assert td._video_meta(d, "x.mkv")["probed"] is False
    with patch.object(td, "probe", return_value={"streams": []}), \
         patch.object(td, "already_processed", return_value=False):
        meta = td._video_meta(d, "x.mkv")
    assert meta["probed"] is True and meta["processed"] is False


def test_unprobeable_keeper_blocks_recycling_the_readable_alternative(tmp_path):
    """The keeper outranks on name/size but cannot be read.

    Recycling the healthy alternative would leave the episode holding only a
    broken file.
    """
    series = "Show"
    keeper = "Show - S01E01 - A WEBDL-1080p.mkv"
    alt = "Show - S01E01 - A WEBRip-1080p.mkv"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01",
                              {keeper: 200 * 1024 * 1024, alt: 150 * 1024 * 1024})
    client = _sonarr_tracking(series, 1, 1, keeper, extra_files=[(300, alt)])

    def probe_by_name(path):
        return None if Path(path).name == keeper else {"streams": []}

    tv = tmp_path / "tv"
    with _default_library(tmp_path), \
         patch.object(td, "ArrClient", return_value=client), \
         patch.object(td, "_sonarr_key", return_value="k"), \
         patch.object(td, "probe", side_effect=probe_by_name), \
         patch.object(td, "already_processed", return_value=False), \
         patch.object(td, "_notify"):
        rc = td.main(["--tv-dir", str(tv), "--apply"])

    assert rc == 1, "an unreadable keeper is an error, not a clean resolve"
    assert _videos(season_dir) == sorted([keeper, alt]), \
        "the readable alternative must not be recycled"
    client.delete_episode_file.assert_not_called()


# --- orphan cleanup failures stay visible -----------------------------------

def test_failed_orphan_cleanup_is_reported_not_silently_resolved(tmp_path, capsys):
    series = "Rick and Morty"
    keeper = "Rick and Morty - S09E01 - Title WEBDL-1080p.mkv"
    extra = "Rick and Morty - S09E01 - Title WEBRip-1080p.mkv"
    season_dir = _make_season(tmp_path / "tv", series, "Season 09",
                              {keeper: 200 * 1024 * 1024, extra: 150 * 1024 * 1024})
    client = _sonarr_tracking(series, 9, 1, keeper, extra_files=[(3845, extra)])
    client.delete_episode_file.return_value = False   # Sonarr refuses the DELETE
    rc, tv = _run(tmp_path, ["--apply"], client)

    out = capsys.readouterr().out
    assert "stale-rows=1" in out, "a failed DELETE must be counted, not hidden"
    assert rc == 1, "a row nothing will retry needs a human, so exit non-zero"
    # Must not promise a retry: cleanup only runs for groups that still have
    # duplicates, and the extras are already gone by now.
    assert "re-run to retry" not in out
    assert "will NOT be retried automatically" in out
    # the physical move still happened; only the DB row is stale
    assert _videos(season_dir) == [keeper]


def test_cleanup_returns_failed_relpaths():
    client = MagicMock()
    client.delete_episode_file.return_value = False
    failed = td._cleanup_orphan_episodefiles(
        client, {"Season 01/x.mkv": 42}, {42: {(1, 1)}}, (1, 1), ["Season 01/x.mkv"])
    assert failed == ["Season 01/x.mkv"]
    client.delete_episode_file.return_value = True
    assert td._cleanup_orphan_episodefiles(
        client, {"Season 01/x.mkv": 42}, {42: {(1, 1)}}, (1, 1), ["Season 01/x.mkv"]) == []


def test_stale_rows_counts_rows_not_groups(tmp_path, capsys):
    """Two failed DELETEs in ONE episode group must report two stale rows."""
    series = "Show"
    keeper = "Show - S01E01 - A WEBDL-2160p.mkv"
    extra1 = "Show - S01E01 - B WEBRip-1080p.mkv"
    extra2 = "Show - S01E01 - C WEBRip-720p.mkv"
    season_dir = _make_season(tmp_path / "tv", series, "Season 01", {
        keeper: 300 * 1024 * 1024,
        extra1: 200 * 1024 * 1024,
        extra2: 150 * 1024 * 1024,
    })
    client = _sonarr_tracking(series, 1, 1, keeper,
                              extra_files=[(401, extra1), (402, extra2)])
    client.delete_episode_file.return_value = False
    rc, tv = _run(tmp_path, ["--apply"], client)

    out = capsys.readouterr().out
    assert "stale-rows=2" in out, "one entry per row, not per episode group"
    assert rc == 1
    assert _videos(season_dir) == [keeper]
