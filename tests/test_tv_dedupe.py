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


def _run(tmp_path, argv, client):
    tv = tmp_path / "tv"
    with patch.object(td, "ArrClient", return_value=client), \
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
        [{"id": 201, "relativePath": "Season 01/Show - S01E01 - A WEBDL-1080p.mkv"}],
    ]
    client.episodes.side_effect = [
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

    with patch.object(td, "ArrClient", return_value=client), \
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
        [],   # post-rescan: Sonarr still hasn't picked up the keeper
    ]
    client.episodes.side_effect = [
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
    episode_by_file_id = {42: (1, 2)}   # tracked by S01E02, not the S01E01 we're resolving
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
    episode_by_file_id = {42: (1, 1)}
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
