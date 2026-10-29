"""File locks shared by the pipeline scripts: the per-file flock of consolidate-subs.py and
normalize-audio.py, and the singleton lock of the cron scripts.

Lock files are NEVER unlinked (a fresh file at the same path could flock on a different
inode while a sibling is mid-release). Only the acquirer releases; a BlockingIOError loser
must not touch the lock file.
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator


def lock_path_for(media_path: Path) -> Path:
    """Next to the media file so flock sees the same filesystem (mergerfs, NFS)."""
    return media_path.parent / f".consolidate-{media_path.name}.lock"


def _same_file(a: Path, b: Path) -> bool:
    """Resolved-path equality (a same basename in another directory must not match)."""
    try:
        return a.resolve(strict=False) == b.resolve(strict=False)
    except (OSError, RuntimeError):
        return str(a) == str(b)


@contextmanager
def acquire_file_lock(
    media_path: Path,
    *,
    inherit_from: Path | None = None,
) -> Iterator[bool]:
    """Non-blocking exclusive lock. Yields True if acquired (or inherited: a parent holding
    the same resolved file passes `inherit_from`), False if another process holds it."""
    if inherit_from is not None and _same_file(inherit_from, media_path):
        yield True
        return

    lp = lock_path_for(media_path)
    lock_fp = open(lp, "w")
    acquired = False
    try:
        try:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        if acquired:
            try:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        lock_fp.close()
        # Deliberately NOT unlinking lp.


def acquire_singleton_lock(lock_path: Path) -> IO[str] | None:
    """The locked file (keep it referenced: closing drops the flock), or `None` if held."""
    lock_fp = open(lock_path, "w")
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fp.close()
        return None
    return lock_fp


def handle_sigterm(signum: int, frame: object) -> None:
    """SystemExit on SIGTERM so `finally` blocks run (else child processes are orphaned)."""
    raise SystemExit(128 + signum)
