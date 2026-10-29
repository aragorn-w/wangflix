"""Per-file flock shared by consolidate-subs.py and normalize-audio.py.

Lock files are NEVER unlinked (a fresh file at the same path could flock on a different
inode while a sibling is mid-release). Only the acquirer releases; a BlockingIOError loser
must not touch the lock file.
"""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


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
