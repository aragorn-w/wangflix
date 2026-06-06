"""Orphan-file + workdir cleanup for the consolidate-subs pipeline.

A `.tmp.mkv` is orphan iff its encoded PID is no longer a live
consolidate-subs.py process.
A `consolsub_*` workdir is orphan iff its encoded PID is dead, OR (when
no PID is encoded) its mtime is older than `SWEEP_MIN_AGE_S`.

Caller passes the per-script identity (basename to grep in /proc) so
this module isn't tied to a specific script name.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from media_stack.config import (
    CONSOLSUB_DIR_RE, SWEEP_MIN_AGE_S, TMP_MKV_RE,
)


def pid_is_running_script(pid: int, script_basename: str) -> bool:
    """True iff /proc/<pid>/cmdline mentions `script_basename`.

    Used to decide whether a `.tmp.mkv.<pid>` artifact's writer is
    still alive (and thus the file should be left alone) or dead
    (orphan, safe to reap).
    """
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False
    return script_basename.encode() in cmdline


def sweep_orphans(
    root: Path, *, script_basename: str, dry_run: bool = False,
) -> dict:
    """Delete orphaned `.tmp.mkv` files and `consolsub_*` workdirs
    under `root`.  Returns a result dict with deleted_files,
    deleted_dirs, skipped (list of (kind, path, info) tuples).
    """
    deleted_files: list[tuple[str, int]] = []
    deleted_dirs: list[str] = []
    skipped: list[tuple[str, str, str]] = []
    age_cutoff = time.time() - SWEEP_MIN_AGE_S

    for r, dirs, files in os.walk(root):
        for f in files:
            m = TMP_MKV_RE.search(f)
            if not m:
                continue
            p = Path(r) / f
            pid = int(m.group(1))
            if pid_is_running_script(pid, script_basename):
                skipped.append(("file-active", str(p), f"pid={pid}"))
                continue
            if dry_run:
                deleted_files.append((str(p), pid))
                continue
            try:
                p.unlink()
                deleted_files.append((str(p), pid))
            except OSError as e:
                skipped.append(("file-error", str(p), str(e)))

        # Walk a copy so we can mutate `dirs` to prevent descent into deleted ones
        for d in list(dirs):
            m = CONSOLSUB_DIR_RE.match(d)
            if not m:
                continue
            p = Path(r) / d
            pid_str = m.group(1)
            if pid_str:
                pid = int(pid_str)
                if pid_is_running_script(pid, script_basename):
                    skipped.append(("dir-active", str(p), f"pid={pid}"))
                    continue
            else:
                try:
                    if p.stat().st_mtime > age_cutoff:
                        skipped.append(("dir-too-young", str(p),
                                        f"mtime>{int(age_cutoff)}"))
                        continue
                except OSError:
                    continue
            if dry_run:
                deleted_dirs.append(str(p))
                if d in dirs:
                    dirs.remove(d)
                continue
            try:
                shutil.rmtree(p, ignore_errors=False)
                deleted_dirs.append(str(p))
                if d in dirs:
                    dirs.remove(d)
            except OSError as e:
                skipped.append(("dir-error", str(p), str(e)))

    return {
        "deleted_files": deleted_files,
        "deleted_dirs": deleted_dirs,
        "skipped": skipped,
    }
