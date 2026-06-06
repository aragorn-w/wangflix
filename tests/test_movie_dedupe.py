"""Tests for movie-dedupe.py — the daily de-dup orchestration.

Drives the SAFE/RISKY classification and the move-to-recycle behaviour with
a real temp movies tree and a mocked Radarr client (no network, no ffprobe,
no real media).  The recycle lands beside the temp movies dir, so nothing
touches the live media root.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
_spec = importlib.util.spec_from_file_location(
    "movie_dedupe", str(PROJECT_ROOT / "movie-dedupe.py"))
md = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(md)


def _make_movie(movies: Path, folder: str, files: dict[str, int]) -> Path:
    d = movies / folder
    d.mkdir(parents=True)
    for name, size in files.items():
        (d / name).write_bytes(b"x" * size)
    return d


def _videos(d: Path) -> list[str]:
    """Real video files in a folder (excludes dotfiles like the persisted
    .consolidate-*.lock, which a glob like *.m* would wrongly match)."""
    return sorted(p.name for p in d.iterdir() if md.is_video(p.name))


def _radarr_tracking(folder: str, tracked_file: str, *, has_file=True, movie_id=1):
    """A fake ArrClient whose movies() reports one movie in `folder` tracking
    `tracked_file`."""
    client = MagicMock()
    client.movies.return_value = [{
        "id": movie_id, "path": f"/movies/{folder}", "hasFile": has_file,
        "movieFile": {"relativePath": tracked_file},
    }]
    client.rescan_movie.return_value = True
    return client


def _run(tmp_path, argv, client):
    movies = tmp_path / "movies"
    # Patch the module's deps: Radarr client, ffprobe-backed processed check,
    # the Radarr key load (so an ArrClient is constructed), and notify.
    with patch.object(md, "ArrClient", return_value=client), \
         patch.object(md, "_radarr_key", return_value="k"), \
         patch.object(md, "probe", return_value={}), \
         patch.object(md, "already_processed", return_value=False), \
         patch.object(md, "_notify"):
        rc = md.main(["--movies-dir", str(movies), *argv])
    return rc, movies


def test_safe_case_moves_untracked_leftover(tmp_path):
    # Radarr tracks the 2160p keeper; the 1080p .mp4 is the untracked leftover.
    movies = tmp_path / "movies"
    folder = "Sound of Freedom (2023)"
    _make_movie(movies, folder, {
        "Sound of Freedom (2023) Bluray-2160p.mkv": 200 * 1024 * 1024,
        "Sound of Freedom (2023) Bluray-1080p.mp4": 150 * 1024 * 1024,
    })
    client = _radarr_tracking(folder, "Sound of Freedom (2023) Bluray-2160p.mkv")
    rc, movies = _run(tmp_path, ["--apply"], client)

    assert rc == 0
    assert _videos(movies / folder) == ["Sound of Freedom (2023) Bluray-2160p.mkv"]
    # leftover moved to the recycle beside movies/
    recycled = tmp_path / ".dupe-recycle" / folder / "Sound of Freedom (2023) Bluray-1080p.mp4"
    assert recycled.exists()
    # SAFE case must NOT rescan (Radarr's tracked file is untouched)
    client.rescan_movie.assert_not_called()


def test_dry_run_moves_nothing(tmp_path):
    folder = "Rango (2011)"
    movies = tmp_path / "movies"
    _make_movie(movies, folder, {
        "Rango (2011) Bluray-2160p.mkv": 200 * 1024 * 1024,
        "Rango (2011) Bluray-1080p.mp4": 150 * 1024 * 1024,
    })
    client = _radarr_tracking(folder, "Rango (2011) Bluray-2160p.mkv")
    rc, movies = _run(tmp_path, [], client)   # no --apply
    assert rc == 0
    assert len(_videos(movies / folder)) == 2   # untouched
    assert not (tmp_path / ".dupe-recycle").exists()


def test_risky_case_flagged_not_moved_without_force(tmp_path):
    # Radarr tracks the 1080p .mp4 (a NON-keeper) — risky to re-point unattended.
    folder = "Ted 2 (2015)"
    movies = tmp_path / "movies"
    _make_movie(movies, folder, {
        "Ted 2 (2015) Bluray-2160p.mkv": 200 * 1024 * 1024,
        "Ted 2 (2015) Bluray-1080p.mp4": 150 * 1024 * 1024,
    })
    client = _radarr_tracking(folder, "Ted 2 (2015) Bluray-1080p.mp4")
    rc, movies = _run(tmp_path, ["--apply"], client)   # no --force
    assert rc == 1                                      # flagged → exit 1
    assert len(_videos(movies / folder)) == 2          # nothing moved
    assert not (tmp_path / ".dupe-recycle").exists()
    client.rescan_movie.assert_not_called()


def test_risky_case_resolved_with_force_rescans(tmp_path):
    folder = "Ted 2 (2015)"
    movies = tmp_path / "movies"
    _make_movie(movies, folder, {
        "Ted 2 (2015) Bluray-2160p.mkv": 200 * 1024 * 1024,
        "Ted 2 (2015) Bluray-1080p.mp4": 150 * 1024 * 1024,
    })
    client = _radarr_tracking(folder, "Ted 2 (2015) Bluray-1080p.mp4")
    # After the move + rescan, Radarr reports it tracking the remaining keeper.
    client.movies.side_effect = [
        [{"id": 1, "path": f"/movies/{folder}", "hasFile": True,
          "movieFile": {"relativePath": "Ted 2 (2015) Bluray-1080p.mp4"}}],   # initial
        [{"id": 1, "path": f"/movies/{folder}", "hasFile": True,
          "movieFile": {"relativePath": "Ted 2 (2015) Bluray-2160p.mkv"}}],   # post-rescan
    ]
    with patch.object(md.time, "sleep"):
        rc, movies = _run(tmp_path, ["--apply", "--force"], client)
    client.rescan_movie.assert_called_once()
    assert _videos(movies / folder) == ["Ted 2 (2015) Bluray-2160p.mkv"]


def test_locked_folder_skipped_not_moved(tmp_path):
    # A pipeline holds a per-file lock → dedupe must NOT move the file; skip
    # the folder (retried next pass) and exit 0 (transient, not a failure).
    from contextlib import contextmanager
    folder = "Rango (2011)"
    movies = tmp_path / "movies"
    _make_movie(movies, folder, {
        "Rango (2011) Bluray-2160p.mkv": 200 * 1024 * 1024,
        "Rango (2011) Bluray-1080p.mp4": 150 * 1024 * 1024,
    })
    client = _radarr_tracking(folder, "Rango (2011) Bluray-2160p.mkv")   # SAFE

    @contextmanager
    def held_lock(_path):
        yield False   # pretend consolidate-subs/normalize-audio holds it

    with patch.object(md, "ArrClient", return_value=client), \
         patch.object(md, "_radarr_key", return_value="k"), \
         patch.object(md, "probe", return_value={}), \
         patch.object(md, "already_processed", return_value=False), \
         patch.object(md, "_notify"), \
         patch.object(md, "acquire_file_lock", held_lock):
        rc = md.main(["--movies-dir", str(movies), "--apply"])

    assert rc == 0
    assert len(list((movies / folder).glob("*.m*"))) == 2     # nothing moved
    assert not (tmp_path / ".dupe-recycle").exists()


def test_force_rescan_verify_failure_is_error_exit1(tmp_path):
    # RISKY + --force, but after the move+rescan Radarr does NOT end up
    # tracking the keeper (verify fails) → recorded as an error → exit 1.
    folder = "Ted 2 (2015)"
    movies = tmp_path / "movies"
    _make_movie(movies, folder, {
        "Ted 2 (2015) Bluray-2160p.mkv": 200 * 1024 * 1024,
        "Ted 2 (2015) Bluray-1080p.mp4": 150 * 1024 * 1024,
    })
    client = _radarr_tracking(folder, "Ted 2 (2015) Bluray-1080p.mp4")   # RISKY
    client.movies.side_effect = [
        [{"id": 1, "path": f"/movies/{folder}", "hasFile": True,
          "movieFile": {"relativePath": "Ted 2 (2015) Bluray-1080p.mp4"}}],   # initial
        [{"id": 1, "path": f"/movies/{folder}", "hasFile": False,
          "movieFile": {}}],                                                  # post-rescan: missing
    ]
    with patch.object(md.time, "sleep"):
        rc, movies = _run(tmp_path, ["--apply", "--force"], client)
    assert rc == 1   # error → non-zero so cron/monitoring notices


def test_no_dupes_is_noop(tmp_path):
    movies = tmp_path / "movies"
    _make_movie(movies, "Solo Movie (2020)", {"Solo Movie (2020) Bluray-2160p.mkv": 1024})
    client = MagicMock()
    client.movies.return_value = []
    rc, movies = _run(tmp_path, ["--apply"], client)
    assert rc == 0
    assert not (tmp_path / ".dupe-recycle").exists()
