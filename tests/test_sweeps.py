"""Tests for media_stack.sweeps — orphan .tmp.mkv + workdir cleanup."""

import importlib.util
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.config import SWEEP_MIN_AGE_S
from media_stack.sweeps import pid_is_running_script, sweep_orphans


@pytest.mark.parametrize("pid, script, expected", [
    pytest.param(99999999, "anything", False, id="nonexistent-pid"),
    pytest.param(os.getpid(), "python", True, id="self-with-python-basename"),
])
def test_pid_is_running_script(pid, script, expected):
    assert pid_is_running_script(pid, script) is expected


@pytest.mark.parametrize("dry_run", [False, True], ids=["deletes", "dry-run-keeps-the-file"])
def test_sweep_dead_pid_tmp_file_is_flagged_and_deleted_unless_dry_run(tmp_path, dry_run):
    # File pattern: foo.consol.<PID>.tmp.mkv
    bad = tmp_path / "foo.consol.99999999.tmp.mkv"
    bad.write_bytes(b"X")
    res = sweep_orphans(tmp_path, script_basename="anything", dry_run=dry_run)
    assert len(res["deleted_files"]) == 1
    assert bad.exists() is dry_run


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


@pytest.mark.parametrize("pid, script, deleted", [
    pytest.param(99999999, "anything", True, id="dead-pid-deleted"),
    pytest.param(os.getpid(), "python", False, id="active-pid-preserved"),
])
def test_sweep_consolsub_workdir_by_pid(tmp_path, pid, script, deleted):
    """consolsub_<pid>_<id> dir: swept when its PID is dead, kept while it is alive."""
    wd = tmp_path / f"consolsub_{pid}_abc123"
    wd.mkdir()
    (wd / "scratch.txt").write_text("x")
    res = sweep_orphans(tmp_path, script_basename=script, dry_run=False)
    assert len(res["deleted_dirs"]) == (1 if deleted else 0)
    assert wd.exists() is not deleted


@pytest.mark.parametrize("age_s, deleted", [
    pytest.param(0, False, id="too-young-preserved"),  # might be in-progress work from another tool
    pytest.param(SWEEP_MIN_AGE_S * 2, True, id="old-enough-deleted"),
])
def test_sweep_pidless_workdir_by_age(tmp_path, age_s, deleted):
    wd = tmp_path / "consolsub_scratch"
    wd.mkdir()
    if age_s:
        old = time.time() - age_s
        os.utime(wd, (old, old))
    res = sweep_orphans(tmp_path, script_basename="anything", dry_run=False)
    assert len(res["deleted_dirs"]) == (1 if deleted else 0)
    assert wd.exists() is not deleted
    if not deleted:
        assert any(s[0] == "dir-too-young" for s in res["skipped"])


# --- .normalize-tmp workdir rule (normalize-audio.py intermediates) ---------

def _normtmp(tmp_path, name, age_s=0):
    """Make .normalize-tmp/<name>, backdating the file and the dir."""
    d = tmp_path / ".normalize-tmp"
    d.mkdir(exist_ok=True)
    f = d / name
    f.write_bytes(b"X")
    if age_s:
        old = time.time() - age_s
        os.utime(f, (old, old))
        os.utime(d, (old, old))
    return d, f


@pytest.mark.parametrize("dry_run", [False, True], ids=["reaped", "dry-run-deletes-nothing"])
def test_normtmp_dead_pid_old_file_is_reported_and_reaped_unless_dry_run(tmp_path, dry_run):
    d, f = _normtmp(tmp_path, ".Movie.99999999.pass2.mkv", age_s=7200)
    res = sweep_orphans(tmp_path, script_basename="consolidate-subs.py", dry_run=dry_run)
    assert f.exists() is dry_run
    assert str(f) in [p for p, _ in res["deleted_files"]]
    assert d.exists(), "workdir is shared; it is deliberately left in place"


def test_normtmp_live_normalize_pid_preserved(tmp_path):
    """The PID belongs to a normalize-audio.py worker, not to the caller.

    The mock is identity-aware: a blanket return_value=True would also pass if the rule
    reverted to the caller's script_basename.
    """
    seen = []

    def only_normalize(pid, script_basename):
        seen.append((pid, script_basename))
        return script_basename == "normalize-audio.py"

    d, f = _normtmp(tmp_path, ".Movie.4242.remux.mkv", age_s=7200)
    with patch("media_stack.sweeps.pid_is_running_script", side_effect=only_normalize):
        sweep_orphans(tmp_path, script_basename="consolidate-subs.py")
    assert f.exists(), "live normalize-audio.py worker's file must survive"
    assert (4242, "normalize-audio.py") in seen, \
        "rule must check the PID against normalize-audio.py, not the caller"


def test_normtmp_young_file_preserved(tmp_path):
    d, f = _normtmp(tmp_path, ".Movie.99999999.pass2.mkv", age_s=0)
    sweep_orphans(tmp_path, script_basename="consolidate-subs.py")
    assert f.exists(), "an intermediate inside the 60min floor must survive"


@pytest.mark.parametrize("relpath, age_s", [
    pytest.param(".normalize-tmp", 0, id="fresh-empty-dir"),  # normalize-audio.py mkdirs before its first file
    pytest.param("m7200/.normalize-tmp", 7200, id="nested-old"),
])
def test_normtmp_workdir_never_removed(tmp_path, relpath, age_s):
    """A shared workdir is never removed, at any age.

    mkdir(exist_ok=True) does not restamp an existing directory, so a live worker's old
    workdir looks abandoned; removing it would delete the output dir under a running render.
    """
    d = tmp_path / relpath
    d.mkdir(parents=True)
    if age_s:
        os.utime(d, (time.time() - age_s, time.time() - age_s))
    res = sweep_orphans(tmp_path, script_basename="consolidate-subs.py")
    assert res["deleted_dirs"] == []
    assert d.exists()


def _consolidate_in_workdir(name, tmp_path, monkeypatch):
    """consolidate-subs.py loaded as `name`, plus a relative single-file invocation from inside a .normalize-tmp workdir."""
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parent.parent / "consolidate-subs.py")
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    d = tmp_path / ".normalize-tmp"
    d.mkdir()
    (d / ".Movie.4242.pass2.mkv").write_bytes(b"X")
    monkeypatch.chdir(d)
    return m, d


def test_normtmp_relative_path_guard(tmp_path, monkeypatch):
    """A relative single-file invocation from inside the workdir is still refused.
    `path.parts` sees only the spelling given, so the guard resolves first."""
    m, d = _consolidate_in_workdir("cs", tmp_path, monkeypatch)
    res = m._process_file_inner(".Movie.4242.pass2.mkv")
    assert res["status"] == "SKIP"
    assert "workdir" in res["detail"]
    assert not list(d.glob(".consolidate-*.lock")), "no lock may be deposited"
