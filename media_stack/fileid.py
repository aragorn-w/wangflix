"""File identity `(st_ino, st_size, st_mtime_ns, st_ctime_ns)`: still the same unmodified file?

ctime cannot be set from userspace, so unlike mtime it catches a same-size in-place rewrite
with the mtime restored. A metadata-only change (hardlink) also moves it: the safe side.
Older persisted identities have three fields `[ino, size, mtime_ns]`.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

Identity = tuple[int, int, int, int]  # (st_ino, st_size, st_mtime_ns, st_ctime_ns)


def identity_from_stat(st: os.stat_result) -> Identity:
    """Identity of a stat/fstat result."""
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def file_identity(path: Path) -> Identity | None:
    """None if `path` cannot be stat'd."""
    try:
        return identity_from_stat(path.stat())
    except OSError:
        return None


def identity_matches(recorded: object, current: Sequence[int] | None, *,
                     allow_legacy: bool) -> bool:
    """A four-field record must equal `current`. A legacy three-field record has no ctime:
    `allow_legacy=False` is a mismatch (for records proving this exact file was verified),
    `allow_legacy=True` compares the shared fields (records meaning only "same release").
    `current` None or a malformed record never matches."""
    if current is None or not isinstance(recorded, (list, tuple)):
        return False
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in recorded):
        return False
    if len(recorded) == len(current):
        return tuple(recorded) == tuple(current)
    if len(recorded) == len(current) - 1:
        return allow_legacy and tuple(recorded) == tuple(current[:-1])
    return False
