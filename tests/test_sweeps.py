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


def test_normtmp_dead_pid_old_file_reaped(tmp_path):
    d, f = _normtmp(tmp_path, ".Movie.99999999.pass2.mkv", age_s=7200)
    res = sweep_orphans(tmp_path, script_basename="consolidate-subs.py")
    assert not f.exists()
    assert str(f) in [p for p, _ in res["deleted_files"]]
    assert d.exists(), "workdir is shared; it is deliberately left in place"


def test_normtmp_live_normalize_pid_preserved(tmp_path):
    """The PID belongs to a normalize-audio.py worker, not to the caller.

    The mock is identity-aware on purpose: a blanket return_value=True would
    also accept "consolidate-subs.py", so reverting the rule to the caller's
    script_basename would still pass and the regression would ship.
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


def test_normtmp_fresh_empty_dir_not_removed(tmp_path):
    """THE RACE: normalize-audio.py:254 mkdirs the workdir, :261 writes the
    first file. In that window the dir is empty and a live worker owns it."""
    d = tmp_path / ".normalize-tmp"
    d.mkdir()
    sweep_orphans(tmp_path, script_basename="consolidate-subs.py")
    assert d.exists(), "a just-created empty workdir must NOT be swept"


def test_normtmp_workdir_never_removed(tmp_path):
    """A shared workdir must never be removed, at any age.

    mkdir(exist_ok=True) does NOT restamp an existing directory, so an old
    workdir a live worker has just entered looks identical to an abandoned
    one. Removing it would delete the output dir under a running render.
    """
    for age in (0, 7200):
        d = tmp_path / f"m{age}" / ".normalize-tmp"
        d.mkdir(parents=True)
        if age:
            os.utime(d, (time.time() - age, time.time() - age))
    res = sweep_orphans(tmp_path, script_basename="consolidate-subs.py")
    assert res["deleted_dirs"] == []
    assert (tmp_path / "m0" / ".normalize-tmp").exists()
    assert (tmp_path / "m7200" / ".normalize-tmp").exists()


def test_normtmp_dry_run_deletes_nothing(tmp_path):
    d, f = _normtmp(tmp_path, ".Movie.99999999.pass2.mkv", age_s=7200)
    res = sweep_orphans(tmp_path, script_basename="x", dry_run=True)
    assert f.exists() and d.exists()
    assert str(f) in [p for p, _ in res["deleted_files"]]


def test_normtmp_rule_scoped_to_that_dirname(tmp_path):
    """Same filename shape outside .normalize-tmp is not this rule's business."""
    other = tmp_path / "Season 1"
    other.mkdir()
    f = other / ".Movie.99999999.pass2.mkv"
    f.write_bytes(b"X")
    old = time.time() - 7200
    os.utime(f, (old, old))
    sweep_orphans(tmp_path, script_basename="consolidate-subs.py")
    assert f.exists()


def test_normtmp_relative_path_guard(tmp_path, monkeypatch):
    """A relative single-file invocation from inside the workdir must still
    be refused. `path.parts` sees only the spelling handed in, so the guard
    resolves first; without that, this path reaches lock acquisition and
    deposits a .lock beside a scratch intermediate."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cs", Path(__file__).resolve().parent.parent / "consolidate-subs.py")
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    d = tmp_path / ".normalize-tmp"
    d.mkdir()
    f = d / ".Movie.4242.pass2.mkv"
    f.write_bytes(b"X")
    monkeypatch.chdir(d)
    res = m._process_file_inner(".Movie.4242.pass2.mkv")
    assert res["status"] == "SKIP"
    assert "workdir" in res["detail"]
    assert not list(d.glob(".consolidate-*.lock")), "no lock may be deposited"


def test_normtmp_guard_fails_closed_on_resolve_error(tmp_path, monkeypatch):
    """If the path cannot be resolved the guard must REFUSE, not fall back.

    Falling back to the unresolved spelling reopens the relative-path bypass:
    a bare filename inside .normalize-tmp has no workdir component in .parts,
    so it would reach acquire_file_lock and deposit a .lock on scratch.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cs2", Path(__file__).resolve().parent.parent / "consolidate-subs.py")
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    d = tmp_path / ".normalize-tmp"
    d.mkdir()
    f = d / ".Movie.4242.pass2.mkv"
    f.write_bytes(b"X")
    monkeypatch.chdir(d)
    with patch.object(Path, "resolve", side_effect=OSError("ELOOP")):
        res = m._process_file_inner(".Movie.4242.pass2.mkv")
    assert res["status"] == "FAIL"
    assert "resolve" in res["detail"]
    assert not list(d.glob(".consolidate-*.lock")), "no lock may be deposited"
