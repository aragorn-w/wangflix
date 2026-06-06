"""Tests for media_stack.state — flock-protected JSON state writes.

The critical invariant: two parallel workers updating different keys
must NOT lose-update each other.  Pre-lock implementation lost the
second writer's snapshot when both loaded → mutated → saved
concurrently.
"""

import json
import multiprocessing
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.state import load_state, save_state, update_state_entry


def test_load_state_returns_empty_on_missing_file(tmp_path):
    state_file = tmp_path / "missing.json"
    assert load_state(state_file) == {}


def test_load_state_returns_empty_on_corrupt_file(tmp_path):
    state_file = tmp_path / "corrupt.json"
    state_file.write_text("{not valid json")
    assert load_state(state_file) == {}


def test_update_state_entry_creates_file(tmp_path):
    state_file = tmp_path / "state.json"
    update_state_entry(state_file, "a", {"v": 1})
    assert load_state(state_file) == {"a": {"v": 1}}


def test_update_state_entry_preserves_other_keys(tmp_path):
    state_file = tmp_path / "state.json"
    update_state_entry(state_file, "a", {"v": 1})
    update_state_entry(state_file, "b", {"v": 2})
    update_state_entry(state_file, "c", {"v": 3})
    state = load_state(state_file)
    assert state == {"a": {"v": 1}, "b": {"v": 2}, "c": {"v": 3}}


def test_update_state_entry_overwrites_existing_key(tmp_path):
    state_file = tmp_path / "state.json"
    update_state_entry(state_file, "a", {"v": 1})
    update_state_entry(state_file, "a", {"v": 99})
    assert load_state(state_file) == {"a": {"v": 99}}


def test_save_state_writes_full_snapshot(tmp_path):
    state_file = tmp_path / "state.json"
    save_state(state_file, {"x": 1, "y": 2})
    assert load_state(state_file) == {"x": 1, "y": 2}


def _worker_update(state_file_str, key, count):
    """Run by multiprocessing — write N keys all with this worker's id."""
    state_file = Path(state_file_str)
    for i in range(count):
        update_state_entry(state_file, f"{key}_{i}", {"i": i})


def test_concurrent_updates_no_lost_updates(tmp_path):
    """Spin up 4 processes, each writes 25 unique keys.  After all join,
    the state file should contain all 100 keys.  Pre-flock implementation
    would lose keys here."""
    state_file = tmp_path / "state.json"
    save_state(state_file, {})  # initialize
    procs = []
    for worker_id in ("a", "b", "c", "d"):
        p = multiprocessing.Process(
            target=_worker_update,
            args=(str(state_file), worker_id, 25),
        )
        p.start()
        procs.append(p)
    for p in procs:
        p.join()
    state = load_state(state_file)
    # 4 workers × 25 keys = 100 entries; none lost
    assert len(state) == 100
    for worker_id in ("a", "b", "c", "d"):
        for i in range(25):
            assert f"{worker_id}_{i}" in state
