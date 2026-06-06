"""Per-file exclusive lock for cross-pipeline mutual exclusion.

`consolidate-subs.py` and `normalize-audio.py` can both want to rewrite
the same media file.  They share a per-file flock at
`<media_dir>/.consolidate-<name>.lock` so the two pipelines never step
on each other.

Invariants:
  - Lock files are NEVER unlinked (a fresh file at the same path could
    flock on a different inode while a sibling is mid-release).
  - Only the process that successfully ACQUIRED the lock may release
    it; a BlockingIOError loser must not touch the lock file.
  - `NORMALIZE_INHERIT_LOCK_PATH=<abs path>` lets a child subprocess
    skip its own acquisition when the parent already holds the lock
    on the same media file.  Verification compares resolved absolute
    paths so same-basename / different-directory inputs don't bypass.
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def lock_path_for(media_path: Path) -> Path:
    """Return the lock-file path for a given media file.  Lock files
    live next to the media file because flock requires same-filesystem
    semantics — keeping them on the media tree means mergerfs-backed
    pools, NFS mounts, etc. all work consistently."""
    return media_path.parent / f".consolidate-{media_path.name}.lock"


def _same_file(a: Path, b: Path) -> bool:
    """True iff `a` and `b` resolve to the same absolute filesystem
    path.  Uses `resolve(strict=False)` so not-yet-created targets
    (e.g. mkvmerge staged outputs) still normalise correctly; falls
    back to string compare on resolution failure rather than silently
    treating the paths as equal."""
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
    """Try to take the exclusive per-file lock on `media_path`.

    Yields `True` if we acquired (or inherited) the lock; the caller
    runs its critical section, then we release on context exit.

    Yields `False` if the lock is held by another process; the caller
    should skip and exit.

    `inherit_from` lets a child subprocess piggyback on a parent's
    already-held lock.  The child compares the RESOLVED absolute path
    of `inherit_from` against `media_path.resolve()` — only the same
    file gets the bypass.
    """
    # Inheritance path: parent guarantees the lock on this exact path.
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
        # Deliberately NOT unlinking lp — see module docstring.


def env_inherit_lock_path() -> Path | None:
    """Read `NORMALIZE_INHERIT_LOCK_PATH` and return it as a `Path`,
    or `None` if unset.  Helper for `normalize-audio.py`'s subprocess
    re-entry from `consolidate-subs.py`; the full path lets
    `acquire_file_lock` verify the parent actually holds the lock on
    the same file via `_same_file`."""
    val = os.environ.get("NORMALIZE_INHERIT_LOCK_PATH")
    return Path(val) if val else None
