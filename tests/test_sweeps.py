"""Tests for media_stack.sweeps — orphan .tmp.mkv + workdir cleanup."""

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.sweeps import pid_is_running_script, sweep_orphans


def test_pid_is_running_script_dead_pid():
    # PID 1 is init — basename won't match an arbitrary script name
    assert pid_is_running_script(1, "nonexistent-script.py") is False


def test_pid_is_running_script_invalid_pid():
    # 99999999 — almost certainly doesn't exist
    assert pid_is_running_script(99999999, "anything") is False


def test_pid_is_running_script_self():
    # Our own PID with python3 as the basename should match
    assert pid_is_running_script(os.getpid(), "python") is True


def test_sweep_dead_pid_tmp_file(tmp_path):
    """A .tmp.mkv with a dead PID should be flagged for deletion."""
    # File pattern: foo.consol.<PID>.tmp.mkv
    bad = tmp_path / "foo.consol.99999999.tmp.mkv"
    bad.write_bytes(b"X")
    res = sweep_orphans(tmp_path, script_basename="anything", dry_run=False)
    assert len(res["deleted_files"]) == 1
    assert not bad.exists()


def test_sweep_dry_run_doesnt_delete(tmp_path):
    bad = tmp_path / "foo.consol.99999999.tmp.mkv"
    bad.write_bytes(b"X")
    res = sweep_orphans(tmp_path, script_basename="anything", dry_run=True)
    assert len(res["deleted_files"]) == 1
    assert bad.exists()  # NOT deleted in dry run


def test_sweep_active_pid_preserves_file(tmp_path):
    """A .tmp.mkv whose PID IS alive should be skipped, not deleted."""
    my_pid = os.getpid()
    live = tmp_path / f"foo.consol.{my_pid}.tmp.mkv"
    live.write_bytes(b"X")
    res = sweep_orphans(tmp_path, script_basename="python", dry_run=False)
    assert len(res["deleted_files"]) == 0
    assert any(s[0] == "file-active" for s in res["skipped"])
    assert live.exists()


def test_sweep_ignores_non_tmp_files(tmp_path):
    """Regular .mkv files (not matching the .consol.<PID>.tmp.mkv pattern)
    should be untouched."""
    regular = tmp_path / "Movie (2024).mkv"
    regular.write_bytes(b"X")
    res = sweep_orphans(tmp_path, script_basename="anything", dry_run=False)
    assert len(res["deleted_files"]) == 0
    assert regular.exists()


def test_sweep_consolsub_workdir_dead_pid(tmp_path):
    """consolsub_<pid>_<id> dir whose PID is dead should be deleted."""
    wd = tmp_path / "consolsub_99999999_abc123"
    wd.mkdir()
    (wd / "scratch.txt").write_text("x")
    res = sweep_orphans(tmp_path, script_basename="anything", dry_run=False)
    assert len(res["deleted_dirs"]) == 1
    assert not wd.exists()


def test_sweep_consolsub_workdir_active_pid_preserved(tmp_path):
    my_pid = os.getpid()
    wd = tmp_path / f"consolsub_{my_pid}_abc123"
    wd.mkdir()
    res = sweep_orphans(tmp_path, script_basename="python", dry_run=False)
    assert len(res["deleted_dirs"]) == 0
    assert wd.exists()


def test_sweep_pidless_workdir_too_young_preserved(tmp_path):
    """A pidless consolsub_* dir whose mtime is recent should NOT be
    swept (might be in-progress work from another tool)."""
    wd = tmp_path / "consolsub_freshscratch"
    wd.mkdir()
    # mtime is "now" by default — younger than SWEEP_MIN_AGE_S
    res = sweep_orphans(tmp_path, script_basename="anything", dry_run=False)
    assert len(res["deleted_dirs"]) == 0
    assert any(s[0] == "dir-too-young" for s in res["skipped"])
    assert wd.exists()


def test_sweep_pidless_workdir_old_enough_deleted(tmp_path):
    """A pidless consolsub_* dir older than SWEEP_MIN_AGE_S should be swept."""
    from media_stack.config import SWEEP_MIN_AGE_S
    wd = tmp_path / "consolsub_oldscratch"
    wd.mkdir()
    # backdate it to 2× the threshold ago
    old = time.time() - SWEEP_MIN_AGE_S * 2
    os.utime(wd, (old, old))
    res = sweep_orphans(tmp_path, script_basename="anything", dry_run=False)
    assert len(res["deleted_dirs"]) == 1
    assert not wd.exists()
