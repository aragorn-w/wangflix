"""Tests for media_stack.locking: the per-file flock shared by consolidate-subs and normalize-audio, and the cron scripts' singleton lock."""

import multiprocessing
import signal
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.locking import (
    acquire_file_lock, acquire_singleton_lock, handle_sigterm, lock_path_for,
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


@contextmanager
def _held_by_another_process(media, hold_seconds):
    """A background process holds `media`'s lock for the duration of the block."""
    ready = multiprocessing.Event()
    p = multiprocessing.Process(target=_holder, args=(str(media), hold_seconds, ready))
    p.start()
    try:
        assert ready.wait(timeout=3), f"background holder didn't acquire {media.name}"
        yield
    finally:
        p.join(timeout=5)


def test_second_acquirer_skips(tmp_path):
    """While another process holds the lock, an attempt returns False instead of blocking."""
    media = tmp_path / "y.mkv"
    media.touch()
    with _held_by_another_process(media, 2):
        with acquire_file_lock(media) as acquired:
            assert acquired is False


def test_inherit_from_bypasses_acquisition(tmp_path):
    """With inherit_from set and matching, the parent's lock is trusted: True without acquiring."""
    media = tmp_path / "z.mkv"
    media.touch()
    # Even if a process were holding the lock, inherit-mode skips:
    with acquire_file_lock(media, inherit_from=media) as acquired:
        assert acquired is True


def test_inherit_from_name_mismatch_acquires_fresh(tmp_path):
    """A different name means the parent's lock guards the wrong file (.mp4 -> .mkv): acquire fresh."""
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
    """`dir_a/Movie.mkv` and `dir_b/Movie.mkv` are different files; a basename-only compare
    must not bypass the lock.

    A background process holds `file_b`'s lock so the two behaviors differ: the broken
    bypass would yield True, the resolved-path compare tries the held lock and yields False.
    Without the held lock both yield True and the test would pass vacuously.
    """
    dir_a = tmp_path / "anime" / "Show Name"
    dir_b = tmp_path / "movies" / "Show Name"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    file_a = dir_a / "S01E01.mkv"
    file_b = dir_b / "S01E01.mkv"
    file_a.touch()
    file_b.touch()
    # Precondition: one lock file per media file path.
    assert lock_path_for(file_a) != lock_path_for(file_b)
    # Hold file_b's lock so a fresh acquisition visibly fails (False).
    with _held_by_another_process(file_b, 5):
        with acquire_file_lock(file_b, inherit_from=file_a) as acquired:
            # False under resolved-path semantics; True under a basename-only bypass.
            assert acquired is False, (
                "lock inheritance MUST reject same-basename / different-"
                "directory inputs; otherwise two unrelated files can be "
                "mutated concurrently"
            )


def test_inherit_from_relative_vs_absolute_same_file_bypasses(tmp_path, monkeypatch):
    """The same file given as a relative path must be recognised via `.resolve()`; otherwise the
    consolidate -> normalize hand-off would re-acquire a lock the parent already holds.

    A background process holds `media_abs`'s lock first: a correct compare bypasses (True),
    a naive string compare tries the held lock (False). Without it both yield True.
    """
    monkeypatch.chdir(tmp_path)
    media_abs = tmp_path / "show" / "ep.mkv"
    media_abs.parent.mkdir()
    media_abs.touch()
    media_rel = Path("show/ep.mkv")
    with _held_by_another_process(media_abs, 5):
        with acquire_file_lock(media_abs, inherit_from=media_rel) as acquired:
            # Recognised via resolve(): bypass, True. A broken compare would yield False.
            assert acquired is True, (
                "lock inheritance MUST recognise relative-vs-absolute "
                "aliasing on the same file; broken compare would re-"
                "acquire the lock the parent already holds"
            )


def test_singleton_lock_is_exclusive_until_released(tmp_path):
    lock = tmp_path / "x.lock"
    held = acquire_singleton_lock(lock)
    assert held is not None
    try:
        assert acquire_singleton_lock(lock) is None
    finally:
        held.close()
    again = acquire_singleton_lock(lock)
    assert again is not None
    again.close()


def test_handle_sigterm_exits_with_128_plus_the_signal():
    with pytest.raises(SystemExit) as exc:
        handle_sigterm(signal.SIGTERM, None)
    assert exc.value.code == 128 + signal.SIGTERM
