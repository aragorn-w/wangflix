"""Flock-protected JSON state file: `str(resolved_path) -> {size, mtime, v, status, ...}`.

Every write takes the flock (parallel workers would lose-update each other's keys). The
lock file `<STATE_FILE>.lock` is NEVER unlinked: a fresh file would flock a different inode.
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path


def load_state(state_file: Path) -> dict:
    """Unlocked read; missing or unreadable returns {}. Use `load_state_locked` when
    unreadable must not look empty."""
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text())
    except Exception:
        return {}


def update_state_entry(state_file: Path, key: str, value: dict) -> None:
    """Atomic single-key upsert under flock (never hold it across ffprobe/mkvmerge)."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_file.parent / f"{state_file.name}.lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        state = load_state(state_file)
        state[key] = value
        _atomic_write(state_file, state)


def save_state(state_file: Path, state: dict) -> None:
    """Full snapshot write under flock; per-file updates use `update_state_entry`."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_file.parent / f"{state_file.name}.lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        _atomic_write(state_file, state)


def _atomic_write(state_file: Path, state: dict) -> None:
    """Per-PID tmp + rename. Caller holds the flock."""
    import os
    tmp = state_file.parent / f".{state_file.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(state))
    tmp.replace(state_file)


def load_state_locked(state_file: Path) -> dict:
    """Read under a shared flock. Missing or non-dict returns {}; unreadable raises OSError."""
    if not state_file.parent.exists():
        return {}
    lock_path = state_file.parent / f"{state_file.name}.lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_SH)
        if not state_file.exists():
            return {}
        try:
            raw = json.loads(state_file.read_text())
        except Exception as exc:
            raise OSError(f"state file unreadable: {state_file}: {exc}") from exc
        return raw if isinstance(raw, dict) else {}


def mutate_state(state_file: Path, fn) -> dict:
    """Locked read-modify-write: `fn(loaded) -> dict to persist`. The read is INSIDE the
    lock so a stale snapshot cannot resurrect a deleted key. Unreadable raises OSError."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_file.parent / f"{state_file.name}.lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        # Not load_state(): it maps unreadable to {} and we would overwrite real history.
        cur: dict = {}
        if state_file.exists():
            try:
                raw = json.loads(state_file.read_text())
            except Exception as exc:
                raise OSError(f"state file unreadable: {state_file}: {exc}") from exc
            # Wrong-shaped JSON holds no history: reset.
            cur = raw if isinstance(raw, dict) else {}
        new = fn(cur)
        _atomic_write(state_file, new)
        return new
