"""Flock-protected JSON state file for consolidate-subs idempotency.

The state file maps `str(resolved_path) → {size, mtime, v, status, ...}`
so subsequent scans skip files that already match their stored
fingerprint.  Parallel scan workers each load → mutate → save the dict;
without the file lock two workers loading at T1 + both saving at T2
would lose-update each other's new keys (codex round-2 #2).

The lock file (`<STATE_FILE>.lock`) is created on demand and intentionally
NEVER unlinked — unlink-after-release lets a third process create a
fresh file at the same path and acquire flock on a different inode,
defeating mutual exclusion (codex round-11 #1).
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path


def load_state(state_file: Path) -> dict:
    """Read the state snapshot.  No lock: reads are idempotent and a
    torn read just means a worker reprocesses a file (the tag-check in
    `already_processed` will short-circuit it).  Writes ARE locked —
    see `update_state_entry` / `save_state`."""
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text())
    except Exception:
        return {}


def update_state_entry(state_file: Path, key: str, value: dict) -> None:
    """Atomic single-key upsert under flock.

    Parallel scan workers each load → mutate → save the state dict.
    Without locking, two workers that both load at T1 and both save at
    T2 cause a lost-update: the second writer's snapshot overwrites
    the first's new key.  Lock around load+modify+write keeps the RMW
    atomic but ONLY holds the lock for the JSON read/write window —
    not across slow operations like ffprobe / mkvmerge — so contention
    stays low.
    """
    state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_file.parent / f"{state_file.name}.lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        state = load_state(state_file)
        state[key] = value
        _atomic_write(state_file, state)
        # fcntl lock releases automatically when `lf` is closed on
        # context exit.  No explicit unlock needed.


def save_state(state_file: Path, state: dict) -> None:
    """Single-shot full snapshot write under flock.  Prefer
    `update_state_entry` for per-file updates from worker context —
    this is only for bulk writes (e.g. orphan-cleanup compaction)
    where the full dict is the unit of update."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_file.parent / f"{state_file.name}.lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        _atomic_write(state_file, state)


def _atomic_write(state_file: Path, state: dict) -> None:
    """Write via a per-PID tmp + rename so partial writes can't be
    observed.  Caller must already hold the flock."""
    import os
    tmp = state_file.parent / f".{state_file.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(state))
    tmp.replace(state_file)
