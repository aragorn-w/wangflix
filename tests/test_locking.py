"""Tests for media_stack.locking — per-file flock for cross-pipeline
mutual exclusion between consolidate-subs and normalize-audio."""

import multiprocessing
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.locking import (
    acquire_file_lock, env_inherit_lock_path, lock_path_for,
)


def test_lock_path_co_located_with_media(tmp_path):
    media = tmp_path / "subdir" / "Movie (2024).mkv"
    media.parent.mkdir(parents=True)
    media.touch()
    lp = lock_path_for(media)
    assert lp.parent == media.parent
    assert lp.name == ".consolidate-Movie (2024).mkv.lock"


def test_acquire_first_holder(tmp_path):
    media = tmp_path / "x.mkv"
    media.touch()
    with acquire_file_lock(media) as acquired:
        assert acquired is True


def _holder(media_path_str, hold_seconds, ready_event):
    """Background process: acquire lock + hold."""
    from media_stack.locking import acquire_file_lock as afl
    media = Path(media_path_str)
    with afl(media) as acquired:
        if acquired:
            ready_event.set()
            time.sleep(hold_seconds)


def test_second_acquirer_skips(tmp_path):
    """While process A holds the lock, process B's attempt returns
    False instead of blocking."""
    media = tmp_path / "y.mkv"
    media.touch()
    ready = multiprocessing.Event()
    p = multiprocessing.Process(target=_holder, args=(str(media), 2, ready))
    p.start()
    try:
        assert ready.wait(timeout=3), "holder didn't acquire"
        # Now try to acquire from this process — should skip
        with acquire_file_lock(media) as acquired:
            assert acquired is False
    finally:
        p.join(timeout=5)


def test_inherit_from_bypasses_acquisition(tmp_path):
    """When inherit_from is set AND names match, the helper trusts
    the parent's lock and yields True without trying to acquire."""
    media = tmp_path / "z.mkv"
    media.touch()
    # Even if a process were holding the lock, inherit-mode skips:
    with acquire_file_lock(media, inherit_from=media) as acquired:
        assert acquired is True


def test_inherit_from_name_mismatch_acquires_fresh(tmp_path):
    """If inherit_from has a DIFFERENT name than media, the parent's
    lock guards the wrong file — fall through to a real acquisition.
    (codex round-13 #1: .mp4 → .mkv suffix change scenario)"""
    src_mp4 = tmp_path / "movie.mp4"
    src_mp4.touch()
    dst_mkv = tmp_path / "movie.mkv"
    dst_mkv.touch()
    # Caller passes locked_path=mp4 but we're operating on the mkv
    with acquire_file_lock(dst_mkv, inherit_from=src_mp4) as acquired:
        # Names differ → must acquire fresh, succeeds because nobody
        # else holds the .mkv lock
        assert acquired is True


def test_inherit_from_same_basename_different_dir_does_not_bypass(tmp_path):
    """REGRESSION (codex round-4 module-split #2; codex round-9 #3
    sharpened the assertion): basename-only comparison let two
    unrelated files share an inherited lock.  `dir_a/Movie.mkv`
    and `dir_b/Movie.mkv` are different files; the bypass MUST NOT
    trigger.

    Critical: this test DISTINGUISHES the bug from the fix.  We
    hold a real lock on `file_b` from a background process before
    calling `acquire_file_lock(file_b, inherit_from=file_a)`:
      - OLD broken behavior (basename-only): would bypass + yield
        True (incorrectly claiming we hold a lock we don't).
      - NEW correct behavior (resolved-path compare): would NOT
        bypass, then try to acquire flock on `file_b`'s lock,
        find it held, and yield False.
    Without the held lock on `file_b`, both behaviors yield True
    and the test would silently rubber-stamp the bug (round-4 test
    had this exact flaw).
    """
    dir_a = tmp_path / "anime" / "Show Name"
    dir_b = tmp_path / "movies" / "Show Name"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    file_a = dir_a / "S01E01.mkv"
    file_b = dir_b / "S01E01.mkv"
    file_a.touch()
    file_b.touch()
    # Sanity precondition: the lock files for these two media files
    # must be distinct (one per media file path).  If this fails
    # everything below is moot.
    assert lock_path_for(file_a) != lock_path_for(file_b)
    # Hold file_b's lock from a background process so the fresh-
    # acquisition path will visibly fail (yield False), letting the
    # test catch the OLD basename-only bypass (which would have
    # yielded True instead).
    ready = multiprocessing.Event()
    p = multiprocessing.Process(target=_holder, args=(str(file_b), 5, ready))
    p.start()
    try:
        assert ready.wait(timeout=3), "background holder didn't acquire file_b"
        with acquire_file_lock(file_b, inherit_from=file_a) as acquired:
            # MUST be False under the resolved-path semantics.  Under
            # the OLD basename-only bypass this would be True.
            assert acquired is False, (
                "lock inheritance MUST reject same-basename / different-"
                "directory inputs; otherwise two unrelated files can be "
                "mutated concurrently"
            )
    finally:
        p.join(timeout=5)


def test_inherit_from_relative_vs_absolute_same_file_bypasses(tmp_path, monkeypatch):
    """When inherit_from is the SAME file expressed as a relative path,
    `_same_file` must normalise both via `.resolve()` and recognise
    the match — otherwise the consolidate→normalize subprocess hand-
    off would re-acquire a lock the parent already holds and
    deadlock (or skip, depending on flock contention).

    Critical (codex round-9 #3): differentiates correct behavior from
    broken.  We hold the real lock on `media_abs` from a background
    process FIRST.  If `_same_file` correctly recognises the
    relative-vs-absolute aliasing → bypass triggers → yields True
    despite the held lock.  If `_same_file` is broken (e.g. naive
    `inherit_from == media_path` string compare) → no bypass → tries
    to acquire the held lock → yields False.  Without the held lock,
    a fresh acquisition also yields True and the test rubber-stamps
    bugs.
    """
    monkeypatch.chdir(tmp_path)
    media_abs = tmp_path / "show" / "ep.mkv"
    media_abs.parent.mkdir()
    media_abs.touch()
    media_rel = Path("show/ep.mkv")
    ready = multiprocessing.Event()
    p = multiprocessing.Process(target=_holder, args=(str(media_abs), 5, ready))
    p.start()
    try:
        assert ready.wait(timeout=3), "background holder didn't acquire media_abs"
        with acquire_file_lock(media_abs, inherit_from=media_rel) as acquired:
            # Inheritance MUST recognise the same file via resolve()
            # and bypass acquisition — yields True.  A broken compare
            # would attempt to acquire the held lock and yield False.
            assert acquired is True, (
                "lock inheritance MUST recognise relative-vs-absolute "
                "aliasing on the same file; broken compare would re-"
                "acquire the lock the parent already holds"
            )
    finally:
        p.join(timeout=5)


def test_env_inherit_lock_path_set(monkeypatch, tmp_path):
    p = tmp_path / "x.mkv"
    monkeypatch.setenv("NORMALIZE_INHERIT_LOCK_PATH", str(p))
    assert env_inherit_lock_path() == p


def test_env_inherit_lock_path_unset(monkeypatch):
    monkeypatch.delenv("NORMALIZE_INHERIT_LOCK_PATH", raising=False)
    assert env_inherit_lock_path() is None
