"""Orphan cleanup for the consolidate-subs pipeline: `.tmp.mkv` files and `consolsub_*`
workdirs whose encoded PID is dead (no PID: older than `SWEEP_MIN_AGE_S`), and
`.normalize-tmp` intermediates whose PID is dead AND older than `NORMALIZE_TMP_MIN_AGE_S`.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from media_stack.config import (
    CONSOLSUB_DIR_RE, NORMALIZE_SCRIPT, NORMALIZE_TMP_DIR,
    NORMALIZE_TMP_MIN_AGE_S, NORMALIZE_TMP_RE, SWEEP_MIN_AGE_S, TMP_MKV_RE,
)


def pid_is_running_script(pid: int, script_basename: str) -> bool:
    """True iff /proc/<pid>/cmdline mentions `script_basename`."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False
    return script_basename.encode() in cmdline


def sweep_orphans(
    root: Path, *, script_basename: str, dry_run: bool = False,
) -> dict:
    """Returns {deleted_files, deleted_dirs, skipped: [(kind, path, info)]}."""
    deleted_files: list[tuple[str, int]] = []
    deleted_dirs: list[str] = []
    skipped: list[tuple[str, str, str]] = []
    age_cutoff = time.time() - SWEEP_MIN_AGE_S
    norm_cutoff = time.time() - NORMALIZE_TMP_MIN_AGE_S

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

        # The PID is a normalize-audio.py worker's: testing `script_basename` would read
        # every live encode as dead.
        if Path(r).name == NORMALIZE_TMP_DIR:
            for f in files:
                m = NORMALIZE_TMP_RE.search(f)
                if not m:
                    continue
                p = Path(r) / f
                pid = int(m.group(1))
                if pid_is_running_script(pid, NORMALIZE_SCRIPT):
                    skipped.append(("normtmp-active", str(p), f"pid={pid}"))
                    continue
                try:
                    if p.stat().st_mtime > norm_cutoff:
                        skipped.append(("normtmp-too-young", str(p), f"pid={pid}"))
                        continue
                except OSError:
                    continue
                if dry_run:
                    deleted_files.append((str(p), pid))
                    continue
                try:
                    p.unlink()
                    deleted_files.append((str(p), pid))
                except OSError as e:
                    skipped.append(("file-error", str(p), str(e)))
            # The workdir is NOT removed: shared by every worker in the folder and never
            # restamped, so a live one looks abandoned. normalize-audio.py rmdirs its own.

        # Copy: `dirs` is mutated to stop descent into deleted dirs.
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
