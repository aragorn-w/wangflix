"""Tests for movie-dedupe.py, the daily de-dup orchestration.

Drives SAFE/RISKY classification and move-to-recycle against a temp movies tree and a
mocked Radarr client; the recycle lands beside the temp dir.
"""
import importlib.util
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
_spec = importlib.util.spec_from_file_location(
    "movie_dedupe", str(PROJECT_ROOT / "movie-dedupe.py"))
md = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(md)


def _make_pair(movies: Path, folder: str) -> Path:
    """A folder holding a 2160p .mkv (the keeper) and a 1080p .mp4 (the leftover)."""
    d = movies / folder
    d.mkdir(parents=True)
    (d / f"{folder} Bluray-2160p.mkv").write_bytes(b"x" * 200 * 1024 * 1024)
    (d / f"{folder} Bluray-1080p.mp4").write_bytes(b"x" * 150 * 1024 * 1024)
    return d


def _videos(d: Path) -> list[str]:
    """Real video files in a folder (excludes dotfiles like .consolidate-*.lock, which *.m* would match)."""
    return sorted(p.name for p in d.iterdir() if md.is_video(p.name))


def _record(folder: str, tracked_file: str | None, *, has_file=True, movie_id=1) -> dict:
    return {"id": movie_id, "path": f"/movies/{folder}", "hasFile": has_file,
            "movieFile": {"relativePath": tracked_file} if tracked_file else {}}


def _radarr_tracking(folder: str, tracked_file: str):
    """A fake ArrClient whose movies() reports one movie in `folder` tracking `tracked_file`."""
    client = MagicMock()
    client.movies.return_value = [_record(folder, tracked_file)]
    client.rescan_movie.return_value = True
    return client


def _run(tmp_path, argv, client, lock=None):
    movies = tmp_path / "movies"
    # Patch the module's deps: Radarr client, ffprobe-backed processed check,
    # the Radarr key load (so an ArrClient is constructed), and notify.
    with patch.object(md, "ArrClient", return_value=client), \
         patch.object(md, "_radarr_key", return_value="k"), \
         patch.object(md, "probe", return_value={}), \
         patch.object(md, "already_processed", return_value=False), \
         patch.object(md, "_notify"), \
         (patch.object(md, "acquire_file_lock", lock) if lock else nullcontext()):
        rc = md.main(["--movies-dir", str(movies), *argv])
    return rc, movies


def test_safe_case_moves_untracked_leftover(tmp_path):
    # Radarr tracks the 2160p keeper; the 1080p .mp4 is the untracked leftover.
    folder = "Sound of Freedom (2023)"
    _make_pair(tmp_path / "movies", folder)
    client = _radarr_tracking(folder, f"{folder} Bluray-2160p.mkv")
    rc, movies = _run(tmp_path, ["--apply"], client)

    assert rc == 0
    assert _videos(movies / folder) == [f"{folder} Bluray-2160p.mkv"]
    # leftover moved to the recycle beside movies/
    assert (tmp_path / ".dupe-recycle" / folder / f"{folder} Bluray-1080p.mp4").exists()
    # SAFE case must NOT rescan (Radarr's tracked file is untouched)
    client.rescan_movie.assert_not_called()


def test_dry_run_moves_nothing(tmp_path):
    folder = "Rango (2011)"
    _make_pair(tmp_path / "movies", folder)
    client = _radarr_tracking(folder, f"{folder} Bluray-2160p.mkv")
    rc, movies = _run(tmp_path, [], client)   # no --apply
    assert rc == 0
    assert len(_videos(movies / folder)) == 2   # untouched
    assert not (tmp_path / ".dupe-recycle").exists()


def test_risky_case_flagged_not_moved_without_force(tmp_path):
    # Radarr tracks the 1080p .mp4 (a NON-keeper) -- risky to re-point unattended.
    folder = "Ted 2 (2015)"
    _make_pair(tmp_path / "movies", folder)
    client = _radarr_tracking(folder, f"{folder} Bluray-1080p.mp4")
    rc, movies = _run(tmp_path, ["--apply"], client)   # no --force
    assert rc == 1                                      # flagged -> exit 1
    assert len(_videos(movies / folder)) == 2          # nothing moved
    assert not (tmp_path / ".dupe-recycle").exists()
    client.rescan_movie.assert_not_called()


@pytest.mark.parametrize("post_rescan, rc", [
    pytest.param(lambda f: _record(f, f"{f} Bluray-2160p.mkv"), None, id="radarr-ends-up-tracking-the-keeper"),
    # verify fails -> recorded as an error -> exit 1 so cron/monitoring notices
    pytest.param(lambda f: _record(f, None, has_file=False), 1, id="verify-failure-is-error-exit1"),
])
def test_risky_case_resolved_with_force_rescans(tmp_path, post_rescan, rc):
    folder = "Ted 2 (2015)"
    _make_pair(tmp_path / "movies", folder)
    client = _radarr_tracking(folder, f"{folder} Bluray-1080p.mp4")
    client.movies.side_effect = [
        [_record(folder, f"{folder} Bluray-1080p.mp4")],   # initial
        [post_rescan(folder)],                               # post-rescan
    ]
    with patch.object(md.time, "sleep"):
        got_rc, movies = _run(tmp_path, ["--apply", "--force"], client)
    client.rescan_movie.assert_called_once()
    assert _videos(movies / folder) == [f"{folder} Bluray-2160p.mkv"]
    if rc is not None:
        assert got_rc == rc


def test_locked_folder_skipped_not_moved(tmp_path):
    # A pipeline holds a per-file lock -> dedupe must NOT move the file; skip
    # the folder (retried next pass) and exit 0 (transient, not a failure).
    folder = "Rango (2011)"
    _make_pair(tmp_path / "movies", folder)
    client = _radarr_tracking(folder, f"{folder} Bluray-2160p.mkv")   # SAFE

    @contextmanager
    def held_lock(_path):
        yield False   # pretend consolidate-subs/normalize-audio holds it

    rc, movies = _run(tmp_path, ["--apply"], client, lock=held_lock)

    assert rc == 0
    assert len(list((movies / folder).glob("*.m*"))) == 2     # nothing moved
    assert not (tmp_path / ".dupe-recycle").exists()


def test_no_dupes_is_noop(tmp_path):
    movies = tmp_path / "movies"
    folder = movies / "Solo Movie (2020)"
    folder.mkdir(parents=True)
    (folder / "Solo Movie (2020) Bluray-2160p.mkv").write_bytes(b"x" * 1024)
    client = MagicMock()
    client.movies.return_value = []
    rc, movies = _run(tmp_path, ["--apply"], client)
    assert rc == 0
    assert not (tmp_path / ".dupe-recycle").exists()
