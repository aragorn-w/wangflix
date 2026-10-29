#!/usr/bin/env python3
"""Consolidate English subtitles in a media file: pick the best text track, ffsubsync it,
clean it, and remux it as the only English subtitle (a real Forced track is kept).

Usage: consolidate-subs.py <file> | --scan <dir> [--jobs N]
Exit codes: 0 ok/skip, 1 hard fail.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, shutil, signal, subprocess, sys, tempfile, time
from typing import NamedTuple
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from media_stack.paths import (  # noqa: E402
    VAR_DIR, VAR_LOG, VAR_QUARANTINE_SUBTITLES, VAR_STATE, ensure_var_dirs,
)
ensure_var_dirs()
from media_stack.config import (  # noqa: E402
    ASS_CODECS, IMAGE_CODECS, PIPELINE_VERSION, NORMALIZE_TMP_DIR,
    SUBLIMINAL_MIN_COVERAGE, TEXT_CODECS, TMP_MKV_RE,
)
from media_stack.lang import is_english_tag  # noqa: E402
from media_stack.probe import (  # noqa: E402
    already_processed, duration_shortfall, file_key, parse_duration, probe,
    real_video_streams,
)
from media_stack.fileid import (  # noqa: E402
    Identity, file_identity, identity_from_stat, identity_matches,
)
from media_stack.state import load_state, mutate_state, update_state_entry  # noqa: E402
from media_stack.audio import (  # noqa: E402
    confident_default_audio, get_audio_lang_pref, select_keep_audio,
)
from media_stack.arrlang import original_language_for  # noqa: E402
from media_stack.subtitles import (  # noqa: E402
    ass_to_srt, clean_srt_text, count_subtitle_lines, extract_track,
    decode_subtitle_bytes, decode_subtitle_text, fetch_via_subliminal, find_sidecar_subs,
    is_forced_track, is_generated_sidecar, is_generated_track, is_sdh_track,
    run_ffsubsync, score_text_track,
)
from media_stack.sublang import classify_candidate  # noqa: E402
from media_stack.mux import (  # noqa: E402
    ENG_GENERATED_TRACK_NAME, ENG_TRACK_NAME, remux,
)
from media_stack.loudness import normalize_budget_s  # noqa: E402
from media_stack.locking import acquire_file_lock  # noqa: E402
from media_stack.tags import set_consolidated_tag  # noqa: E402
from media_stack import sublang, subverify  # noqa: E402
from media_stack.subverify import (  # noqa: E402
    UPGRADE_MIN_PASSING_WINDOWS, generated_track_windows, judge_sidecar_against_windows,
)
from media_stack.sweeps import sweep_orphans as _sweep_orphans  # noqa: E402

STATE_FILE = VAR_STATE / "consolidate-subs.state.json"
LOG_FILE   = VAR_LOG / "consolidate-subs.log"
QUARANTINE_LOG = VAR_LOG / "sub-quarantine.jsonl"

# Separate from STATE_FILE: that cache is keyed on (size, mtime, v), which a landing
# sidecar changes neither of, so it would skip the file forever. Read by sub-fallback.py.
NEEDS_ENGLISH_STATE_FILE = VAR_STATE / "needs-english.json"


def sweep_orphans(root: Path, dry_run: bool = False) -> dict:
    return _sweep_orphans(root, script_basename=Path(__file__).name, dry_run=dry_run)


def _log_sweep_result(res: dict, dry: bool) -> None:
    prefix = "SWEEP-DRY" if dry else "SWEEP"
    for path, pid in res["deleted_files"]:
        log(f"{prefix}: tmp file (pid={pid}): {path}")
    for path in res["deleted_dirs"]:
        log(f"{prefix}: workdir: {path}")
    for kind, path, info in res["skipped"]:
        log(f"{prefix}-SKIP {kind}: {path} ({info})")
    log(f"{prefix} summary: {len(res['deleted_files'])} files, {len(res['deleted_dirs'])} dirs, {len(res['skipped'])} skipped")


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _utc_now_iso() -> str:
    """Microsecond precision: whole seconds would defeat sub-fallback's snapshot-vs-current check."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _needs_english_safe(op_name: str, path_for_log: Path, fn) -> None:
    """Swallow any failure: this bookkeeping must never turn a real result into FAIL."""
    try:
        fn()
    except Exception as exc:
        log(f"WARNING: needs-english.json {op_name} failed for {path_for_log} "
            f"({type(exc).__name__}: {exc}); consolidate result unaffected")


def _file_identity(path: Path) -> list[int] | None:
    """`[st_ino, st_size, st_mtime_ns]`. No ctime (unlike media_stack.fileid): this means
    "same release", and a hardlink change would needlessly reset the grace period."""
    try:
        st = path.stat()
    except OSError:
        return None
    return [st.st_ino, st.st_size, st.st_mtime_ns]


def _clear_needs_english(path: Path, dry_run: bool) -> None:
    """Drop `path`'s needs-english entry once FIXED."""
    if dry_run:
        return
    key = str(path.resolve())

    def _drop(state: dict) -> dict:
        state.pop(key, None)
        return state

    _needs_english_safe("clear", path, lambda: mutate_state(NEEDS_ENGLISH_STATE_FILE, _drop))


def _apply_cleanup_needs_english_update(
    path: Path, pre_identity: list[int] | None, result_path: Path, dry_run: bool,
) -> None:
    """Record NEEDS_BAZARR after the cleanup remux. `first_seen` resets only if the entry
    at `path` stores an identity other than `pre_identity` (a new release replaced the
    file); the entry moves to `result_path` (the suffix may change)."""
    if dry_run:
        return
    old_key = str(path.resolve())
    new_key = str(result_path.resolve())
    now = _utc_now_iso()
    current_identity = _file_identity(result_path)

    def _update(state: dict) -> dict:
        old_entry = state.pop(old_key, None)
        first_seen = None
        if isinstance(old_entry, dict):
            stored_identity = old_entry.get("identity")
            if stored_identity is None or stored_identity == pre_identity:
                first_seen = old_entry.get("first_seen")
        state[new_key] = {
            "first_seen": first_seen or now,
            "last_seen": now,
            "identity": current_identity,
        }
        return state

    _needs_english_safe(
        "cleanup-upsert", result_path, lambda: mutate_state(NEEDS_ENGLISH_STATE_FILE, _update))


def process_file(path_str: str, dry_run: bool = False) -> dict:
    """Returns {path, status: SKIP/FIXED/NEEDS_BAZARR/FAIL, detail}; never raises. dry_run
    mutates nothing (a would-be FIXED returns SKIP with detail prefix "dry-run:")."""
    try:
        return _process_file_inner(path_str, dry_run=dry_run)
    except Exception as e:
        return {"path": path_str, "status": "FAIL",
                "detail": f"unhandled: {type(e).__name__}: {str(e)[:120]}"}


def _process_file_inner(path_str: str, dry_run: bool = False) -> dict:
    path = Path(path_str)
    if not path.is_file():
        return {"path": path_str, "status": "FAIL", "detail": "not a file"}
    if TMP_MKV_RE.search(path.name):
        return {"path": path_str, "status": "SKIP",
                "detail": "tmp file (orphan candidate; run --sweep-only to clean)"}
    # Single-file entry points bypass walk()'s prune. Check BEFORE acquire_file_lock (it
    # creates the .lock file). Resolve first (a relative path inside the workdir would
    # slip past `parts`); on failure REFUSE, since the unverified spelling fails open.
    try:
        _resolved = path.resolve()
    except OSError as e:
        return {"path": path_str, "status": "FAIL",
                "detail": f"cannot resolve path: {e}"}
    if NORMALIZE_TMP_DIR in _resolved.parts:
        return {"path": path_str, "status": "SKIP",
                "detail": "normalize-audio workdir intermediate"}

    with acquire_file_lock(path) as acquired:
        if not acquired:
            return {"path": path_str, "status": "SKIP",
                    "detail": "locked by other runner"}
        return _process_locked(path, path_str, dry_run=dry_run)


def _kill_process_group(proc: subprocess.Popen, grace_s: float = 15.0) -> None:
    """Kill `proc`'s group (start_new_session=True, so pid == pgid): SIGTERM, `grace_s` for
    EVERY member (a grandchild may ignore it), then SIGKILL. The leader is reaped as soon
    as it exits (a zombie still counts as a member). Never raises."""
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            proc.poll()
            os.killpg(pgid, 0)   # ProcessLookupError once the group is empty
            time.sleep(0.1)
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass  # group gone (ProcessLookupError) or no longer signalable
    try:
        proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        pass


def _raise_on_sigterm(signum, frame) -> None:
    raise SystemExit(128 + signum)


def _normalize_audio_inline(path: Path, locked_path: Path | None = None) -> None:
    """Run normalize-audio.py in a subprocess (its process pool and locks must not mix with
    ours). `locked_path` becomes NORMALIZE_INHERIT_LOCK_PATH so the child inherits our lock.
    Errors never propagate: consolidation is already committed. The cap is
    `normalize_budget_s(size)`; on expiry or interrupt the child's whole process group is
    killed, leaving no orphan ffmpeg."""
    script = Path(__file__).parent / "normalize-audio.py"
    env = {**os.environ}
    if locked_path is not None:
        env["NORMALIZE_INHERIT_LOCK_PATH"] = str(locked_path)
    try:
        budget_s = normalize_budget_s(path.stat().st_size)
    except OSError:
        budget_s = 7200
    try:
        proc = None
        try:   # else SIGTERM to us leaves the child (own session) running
            previous = signal.signal(signal.SIGTERM, _raise_on_sigterm)
        except ValueError:   # not the main thread
            previous = None
        try:
            proc = subprocess.Popen(
                [sys.executable, str(script), str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                start_new_session=True,
            )
            stdout, stderr = proc.communicate(timeout=budget_s)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            log(f"audio-normalize TIMEOUT ({_hms(budget_s)} budget), process group killed: {path}")
            return
        except BaseException:
            if proc is not None:
                _kill_process_group(proc)
            raise
        finally:
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)
        # rc=0 is not enough: a "locked" SKIP also exits 0 and is a no-op.
        stdout_tail = (stdout or "").strip().splitlines()[-1:] or [""]
        last = stdout_tail[0]
        if proc.returncode == 0 and "locked by other pipeline" in last:
            log(f"audio-normalize SKIPPED (lock conflict): {path} — {last[:160]}")
        elif proc.returncode == 0:
            log(f"audio-normalize OK: {path}")
        else:
            tail = (stderr or "").strip().splitlines()[-1:] or [""]
            log(f"audio-normalize FAIL rc={proc.returncode}: {path} -- {tail[0][:200]}")
    except Exception as e:
        log(f"audio-normalize ERR: {path} -- {type(e).__name__}: {e}")


def _expose_tag_normalize(out: Path, path: Path, replacement: Path,
                          path_str: str, tag: bool = True,
                          normalize: bool = True) -> tuple[dict | None, bool]:
    """Swap the muxed `out` into `replacement`, stamp CONSOLIDATED_SUBS, optionally run
    inline normalization, all under the destination .mkv's lock (on .mp4 -> .mkv `path`'s
    lock does not cover the new file).

    The no-main-English cleanup passes `tag=False, normalize=False`: it must not mark the
    file consolidated, and normalize-audio could re-encode audio or let ffmpeg auto-flag a
    default, bypassing `confident_default_audio`.

    Returns `(early_result, tag_written)`; `early_result` is a SKIP (destination locked) or
    FAIL (sibling .mkv appeared, or `out` is shorter than the source) dict for the caller to
    return, else None. A failed tag is
    logged, not rolled back (state.json backstops idempotency).
    """
    dest_lock = (acquire_file_lock(replacement) if replacement != path
                 else nullcontext(True))
    with dest_lock as dest_acquired:
        if replacement != path and not dest_acquired:
            out.unlink(missing_ok=True)
            return ({"path": path_str, "status": "SKIP",
                     "detail": f"destination {replacement.name} locked by other runner"},
                    False)
        # Re-check under the lock (TOCTOU with _process_locked's check).
        if replacement != path and replacement.exists():
            out.unlink(missing_ok=True)
            return ({"path": path_str, "status": "FAIL",
                     "detail": f"collision: sibling .mkv appeared for {path.name}"},
                    False)
        # A short remux output once replaced full episodes: compare durations before the swap.
        problem = duration_shortfall(probe(path), probe(out), log)
        if problem:
            out.unlink(missing_ok=True)
            return ({"path": path_str, "status": "FAIL",
                     "detail": f"duration verify: {problem}"}, False)
        # atomic same-filesystem rename: readers see the old file or the finished mux, never a partial one
        os.replace(out, replacement)
        if replacement != path and path.exists():
            path.unlink()
        tag_written = False
        if tag:
            # An uncaught raise would skip the state update and sidecar cleanup.
            try:
                tag_written = set_consolidated_tag(replacement, PIPELINE_VERSION)
            except Exception as exc:
                log(f"ERROR: CONSOLIDATED_SUBS tag write raised for {replacement}: "
                    f"{type(exc).__name__}: {exc} (consolidation done; tag missing)")
            else:
                if not tag_written:
                    log(f"ERROR: CONSOLIDATED_SUBS tag write failed for {replacement} "
                        f"(consolidation done + state recorded; MKV idempotency marker missing)")
        if normalize:
            _normalize_audio_inline(replacement, locked_path=replacement)
        return (None, tag_written)


# "Positively foreign": base subtag is a 2-3 letter ASCII code that is not English, not
# und/mul/mis/zxx, not in the qaa-qtz private-use range. Anything else is KEPT.
# ASCII-only: str.isalpha() would accept e.g. "éé".
_SUBTITLE_SPECIAL_LANG_CODES = frozenset({"und", "mul", "mis", "zxx"})
_SUBTITLE_LANG_BASE_RE = re.compile(r"[a-z]{2,3}")


def _is_positively_foreign_sub(s: dict) -> bool:
    """The cleanup drops only these."""
    lang = ((s.get("tags") or {}).get("language") or "").strip().lower()
    base = lang.split("-", 1)[0]
    if not _SUBTITLE_LANG_BASE_RE.fullmatch(base):
        return False
    if base in _SUBTITLE_SPECIAL_LANG_CODES:
        return False
    if "qaa" <= base <= "qtz":
        return False
    return not is_english_tag(lang)


# Content gate: an English tag or filename is a claim, not proof, so `classify_candidate`
# checks the text. A rejected EMBEDDED candidate is never chosen; a rejected SIDECAR is
# moved to VAR_QUARANTINE_SUBTITLES, never deleted.

_QUARANTINE_NAME_TRIES = 1000


class _Unclassifiable(OSError):
    """Could not classify (over the bound, changing while read, or not text). An OSError so
    candidate loops count it as an extraction failure; never used on its tag or name."""


class _ForcedGate(NamedTuple):
    state: str            # "accepted" | "rejected" | "unreadable"
    text: "str | None"    # cleaned SRT to embed
    info: dict


def _gate_embedded_forced(path: Path, forced: dict) -> _ForcedGate:
    """Gate the embedded forced English track BEFORE any decision depends on it (a foreign
    track tagged English must not make the file "only forced English"); the gate sees the
    bytes before ASS/SSA conversion.

      - accepted: `text` is the cleaned SRT; None if conversion failed.
      - rejected: proven foreign; dropped.
      - unreadable: proves nothing; track is kept.
    """
    idx = forced["index"]
    scratch = Path(tempfile.mkdtemp(prefix=f"consolsub_{os.getpid()}_forced_", dir=path.parent))
    try:
        ext = ".ass" if forced.get("codec_name") in ASS_CODECS else ".srt"
        ftmp = _workfile(scratch, "forced", ext)
        if not extract_track(path, idx, forced.get("codec_name"), ftmp):
            log(f"embedded forced track {idx} in {path} could not be extracted; "
                f"not counted as forced English")
            return _ForcedGate("unreadable", None, {})
        try:
            ok, info = _gate_local_file(ftmp)
        except _Unclassifiable as exc:
            log(f"content-gate SKIP embedded forced track {idx} in {path}: cannot be "
                f"classified ({exc}); not used as the forced track")
            return _ForcedGate("unreadable", None, {})
        except OSError as exc:
            log(f"WARNING: cannot read embedded forced track {idx} in {path}: "
                f"{type(exc).__name__}: {exc}")
            return _ForcedGate("unreadable", None, {})
        if not ok:
            log(f"content-gate REJECT embedded forced track {idx} in {path}: tagged English, "
                f"text scored {info.get('verdict')} (reason={info.get('reason')}) "
                f"-- proven foreign, not used as the forced track")
            return _ForcedGate("rejected", None, info)
        try:
            if ftmp.suffix == ".ass":
                fsrt = _workfile(scratch, "forced-srt", ".srt")
                if not ass_to_srt(ftmp, fsrt):
                    log(f"embedded forced track {idx} in {path} passed the content gate but "
                        f"could not be converted to SRT; not used as the forced track")
                    return _ForcedGate("accepted", None, info)
                ftmp = fsrt
            return _ForcedGate(
                "accepted", clean_srt_text(ftmp.read_text(encoding="utf-8", errors="replace")), info)
        except OSError as exc:
            log(f"WARNING: embedded forced track {idx} in {path} could not be prepared: "
                f"{type(exc).__name__}: {exc}; not used as the forced track")
            return _ForcedGate("accepted", None, info)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _snapshot_text(p: Path) -> "tuple[_Snapshot, str]":
    """Raises `_Unclassifiable` if over the size bound, changing while read, or not text."""
    snap = _read_snapshot(p)
    if snap is None:
        raise _Unclassifiable(f"{p.name}: over the {UPGRADE_MAX_SIDECAR_BYTES}-byte bound "
                              f"or changing while it was read")
    text = decode_subtitle_text(snap.data)
    if text is None:
        raise _Unclassifiable(f"{p.name}: not decodable as text")
    return snap, text


def _gate_local_file(p: Path) -> tuple[bool, dict]:
    """Content gate for a scratch file: `(is_english, info)`. An accepted file is rewritten
    as DECODED UTF-8 so UTF-16/cp1252 never reaches clean, ffsubsync or mkvmerge. Raises
    `_Unclassifiable`."""
    _snap, text = _snapshot_text(p)
    is_eng, info = classify_candidate(text)
    if is_eng:
        p.write_bytes(text.encode("utf-8"))
    return is_eng, info


def _last_cue_start(srt: Path) -> float | None:
    """Start (s) of the latest cue in a UTF-8 SRT; None if none or unreadable."""
    try:
        cues = subverify._sidecar_cues(srt.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return max((start for start, _ in cues), default=None)


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _late_sidecar_detail(base: str, foreign: int, redundant: int, dry_run: bool) -> str:
    verb = "dry-run: would quarantine" if dry_run else "quarantined"
    parts = []
    if foreign:
        parts.append(f"{verb} {foreign} sidecar(s)")
    if redundant:
        parts.append(f"{verb} {redundant} redundant sidecar(s)")
    return "; ".join([base, *parts])


def _quarantine_stamp() -> str:
    """UTC timestamp without colons, safe in a filename."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _log_quarantine(original: Path, dest: Path, verdict_info: dict, reason: str) -> None:
    """Append to QUARANTINE_LOG. Never raises: must not undo a completed move."""
    rec = {
        "timestamp": _utc_now_iso(),
        "original_path": str(original),
        "quarantine_path": str(dest),
        "verdict": verdict_info.get("verdict"),
        "scores": verdict_info,
        "reason": reason,
    }
    try:
        QUARANTINE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with QUARANTINE_LOG.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception as exc:
        log(f"WARNING: sub-quarantine.jsonl write failed for {dest}: "
            f"{type(exc).__name__}: {exc}")


def _quarantine_fsync_chain(quarantine_dir: Path) -> list[Path]:
    """Directories to fsync before the original goes: the copy's and each ancestor up to
    `var/`, on EVERY move (finding a directory existing proves nothing about disk). A
    quarantine dir outside `VAR_DIR` is assumed to use `var/quarantine/subtitles`."""
    top = VAR_DIR if VAR_DIR in quarantine_dir.parents else quarantine_dir.parent.parent
    chain = [quarantine_dir]
    p = quarantine_dir
    while p != top and p.parent != p:
        p = p.parent
        chain.append(p)
    return chain


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _quarantine_sidecar(sidecar: Path, snap: _Snapshot, verdict_info: dict, reason: str) -> bool:
    """Move a content-rejected sidecar into VAR_QUARANTINE_SUBTITLES instead of deleting it
    (`classify_candidate` is a lexical heuristic).

    The copy is written from `snap.data` (the bytes classified, no re-read) with
    O_CREAT|O_EXCL (new suffix on collision; an existing file is never touched), fsynced and
    size-checked, then the directory chain is fsynced, and only then is the original
    unlinked, and only if it still has `snap.identity` (a sidecar replaced meanwhile by
    Bazarr or sub-fallback.py survives and our copy is removed). Nothing is renamed: the
    library and `var/` are different filesystems. Any failure is logged, removes our partial
    copy, keeps the original and never raises.

    Returns True iff the sidecar was moved (a failed jsonl write still returns True).
    """
    dest: Path | None = None
    try:
        VAR_QUARANTINE_SUBTITLES.mkdir(parents=True, exist_ok=True)
        stamp = _quarantine_stamp()
        fd = -1
        for n in range(_QUARANTINE_NAME_TRIES):
            name = f"{stamp}-{sidecar.name}" if n == 0 else f"{stamp}-{n}-{sidecar.name}"
            candidate = VAR_QUARANTINE_SUBTITLES / name
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
            except FileExistsError:
                continue
            dest = candidate
            break
        if dest is None:
            raise OSError(f"no free quarantine name after {_QUARANTINE_NAME_TRIES} tries")
        with os.fdopen(fd, "wb") as fh:
            fh.write(snap.data)
            fh.flush()
            os.fsync(fh.fileno())
            written = os.fstat(fh.fileno()).st_size
        if written != len(snap.data):
            raise OSError(f"quarantine copy is {written} bytes, expected {len(snap.data)}")
        for d in _quarantine_fsync_chain(VAR_QUARANTINE_SUBTITLES):
            _fsync_dir(d)
        now = file_identity(sidecar)
        if now is None:
            raise OSError("sidecar vanished before it could be removed")
        if now != snap.identity:
            raise OSError("sidecar changed after it was classified "
                          "(replaced or modified); left in place")
        sidecar.unlink()
    except Exception as exc:
        log(f"WARNING: quarantine failed for {sidecar}: {type(exc).__name__}: {exc}; "
            f"original left in place")
        if dest is not None:
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
        return False
    _log_quarantine(sidecar, dest, verdict_info, reason)
    return True


def _gate_forced_sidecars(sidecars: list, accepted: dict,
                          unclassified: "list[Path] | None" = None) -> list:
    """FULL-path content gate for FORCED English-named sidecars (a bogus one would block
    `sub-fallback.py` and show in Jellyfin).

    A foreign one is quarantined and dropped from the returned list (also when the
    quarantine failed). An accepted one (short forced files score TOO-LITTLE-TEXT, so only
    clearly foreign ones are rejected) is recorded in `accepted` as `path -> snapshot`;
    FIXED may embed it and only then counts it consumed. An unclassifiable one is left on
    disk and appended to `unclassified` for the caller to report as an extraction failure."""
    kept = []
    for entry in sidecars:
        sc_path, is_forced = entry[0], entry[1]
        if not is_forced:
            kept.append(entry)
            continue
        try:
            snap = _read_snapshot(sc_path)
        except OSError as exc:
            log(f"WARNING: cannot read forced sidecar {sc_path}: {type(exc).__name__}: {exc}; "
                f"left in place, not counted as forced English")
            if unclassified is not None:
                unclassified.append(sc_path)
            continue
        text = None if snap is None else decode_subtitle_text(snap.data)
        if snap is None or text is None:
            log(f"content-gate SKIP forced sidecar {sc_path}: cannot be classified "
                f"({'over the size bound or changing while it was read' if snap is None else 'not decodable as text'}); "
                f"left in place, not counted as forced English")
            if unclassified is not None:
                unclassified.append(sc_path)
            continue
        is_eng, verdict_info = classify_candidate(text)
        if is_eng:
            accepted[sc_path] = snap
            kept.append(entry)
            continue
        log(f"content-gate REJECT forced sidecar {sc_path}: named English, text scored "
            f"{verdict_info.get('verdict')} (reason={verdict_info.get('reason')}) -- quarantining")
        # Out of every decision whether or not the move succeeds.
        if not _quarantine_sidecar(sc_path, snap, verdict_info,
                                   "forced sidecar rejected during consolidation"):
            log(f"content-gate: rejected forced sidecar {sc_path} could not be quarantined; "
                f"left on disk, not counted as forced English")
    return kept


_REDUNDANT_REASON = "redundant: embedded English subtitle present"
_LATE_FOREIGN_REASON = "late sidecar beside already-processed file"


def _embedded_english_text_tracks(info: dict | None) -> list[dict]:
    """Embedded TEXT subtitle streams tagged English."""
    return [s for s in (info or {}).get("streams", [])
            if s.get("codec_type") == "subtitle"
            and s.get("codec_name") in TEXT_CODECS
            and is_english_tag((s.get("tags") or {}).get("language"))]


def _eligible_embedded(tracks: list[dict], is_forced: bool) -> list[dict]:
    """A MAIN sidecar is retired only by a non-forced track, a FORCED one only by a forced
    track (a main track does not cover signs and foreign speech)."""
    return [t for t in tracks if is_forced_track(t) == is_forced]


def _late_english_sidecar_verdict(media: Path, sc_path: Path, is_forced: bool,
                                  tracks: list[dict]) -> str:
    """"redundant" (quarantine), "keep" (the file's only English subtitle), or "upgrade"
    (the only embedded English is a generated Whisper transcript: left to the generated ->
    real upgrade check). Tracks are by TAG; the caller verifies content."""
    eligible = _eligible_embedded(tracks, is_forced)
    if not eligible:
        return "keep"
    # Whisper never makes a forced track, so "upgrade" is main-only.
    if (not is_forced and not is_generated_sidecar(media, sc_path)
            and all(is_generated_track(t) for t in eligible)):
        return "upgrade"
    return "redundant"


_MAX_EMBEDDED_CONTENT_CHECKS = 3
# (start_s, duration_s): ~10 minutes from 5 minutes in (skips intros), else from the start.
_EMBEDDED_SAMPLE_WINDOWS = ((300.0, 600.0), (0.0, 600.0))


def _embedded_track_is_english(path: Path, track: dict, forced: bool = False) -> bool:
    """Does an embedded track tagged English POSITIVELY look like English? Must score `eng`
    (the content gate's benefit of the doubt is not enough: this retires a sidecar); a
    FORCED track may also be too short to score. Unreadable or empty is False (the caller
    keeps the sidecar)."""
    idx = track.get("index")
    text = None
    for start, dur in _EMBEDDED_SAMPLE_WINDOWS:
        try:
            text = subverify.embedded_text_window(
                path, idx, track.get("codec_name") or "", start, dur)
        except Exception as exc:
            log(f"WARNING: cannot sample embedded subtitle {idx} of {path}: "
                f"{type(exc).__name__}: {exc}")
            return False
        if text and text.strip():
            break
    if not text or not text.strip():
        log(f"embedded subtitle {idx} of {path} could not be read for verification")
        return False
    verdict = sublang.score(text)["verdict"]
    if verdict == "eng" or (forced and verdict == "TOO-LITTLE-TEXT"):
        return True
    log(f"embedded subtitle {idx} of {path} is tagged English but its text is not positive "
        f"English evidence (language check: {verdict})")
    return False


def _upgrade_rejection_reasons(path: Path, snaps: dict[Path, _Snapshot | None],
                               sidecars: list, record: object) -> dict[str, str] | None:
    """The upgrade check's decision on the non-generated main English sidecars, from its
    rejection `record`. None if no record still holds for THIS video and THESE sidecars;
    else `{name: reason}` for those judged wrong (possibly empty: a sidecar with no verdict
    must never be given up). A verdict holds per sidecar while its fingerprint and the
    video's identity match. A valid record means generated-only, so no probe is needed."""
    video_identity = file_identity(path)
    if video_identity is None:
        return None
    real = [sc for sc, forced, _sdh in sidecars
            if not forced and not is_generated_sidecar(path, sc)]
    known = {sc: snaps[sc] for sc in real if snaps.get(sc) is not None}
    verdicts = _recorded_verdicts(record, video_identity, known)
    if verdicts is None:
        return None
    return {name: why for name, why in verdicts.items() if why}


_UNSET: object = object()  # no baseline taken (None: tried and failed)


def _quarantine_foreign_sidecars(path: Path, dry_run: bool, info: dict | None = None,
                                 upgrade_rejection: object = None,
                                 video_identity: object = _UNSET) -> tuple[int, int]:
    """Late-sidecar sweep for an ALREADY-PROCESSED video, run after
    `_generated_upgrade_check` declined to upgrade (a late sidecar changes neither the
    cache key nor the tag, so it would otherwise sit there forever). Quarantines, never
    deletes:
      - sidecars that score non-English ("foreign");
      - English sidecars made REDUNDANT by an embedded English text subtitle whose TEXT
        (a ~10-minute sample, not just its tag) is verified English;
      - a non-generated sidecar beside a generated-only file once the upgrade check
        REJECTED it for this exact video and sidecar (`upgrade_rejection`), reason
        "rejected upgrade: <why>". Without that rejection it is kept and the check retries.
    An English sidecar is kept when the video has no embedded English text subtitle, the
    probe fails, or the embedded text is non-English or unreadable.
    Returns `(foreign, redundant)` counts (what WOULD be, in `dry_run`).

    `video_identity` (taken before the caller probed, else here) is re-checked before EACH
    move: on change the sweep stops, keeping everything not yet moved. If the caller's
    baseline could not be read (None) nothing is moved.
    """
    try:
        sidecars = find_sidecar_subs(path)
    except OSError as exc:
        log(f"WARNING: late-sidecar scan failed for {path}: {type(exc).__name__}: {exc}")
        return 0, 0
    if video_identity is _UNSET:
        video_identity = file_identity(path)
    if video_identity is None:
        log(f"WARNING: late-sidecar sweep of {path}: the video's identity could not be read "
            f"before it was probed; keeping everything this call")
        return 0, 0
    snaps: dict[Path, _Snapshot | None] = {}
    for sc_path, _forced, _sdh in sidecars:
        try:
            snaps[sc_path] = _read_snapshot(sc_path)
        except OSError as exc:
            log(f"WARNING: cannot read sidecar {sc_path} for late-sidecar "
                f"classification: {type(exc).__name__}: {exc}")
            snaps[sc_path] = None
            continue
        if snaps[sc_path] is None:
            log(f"WARNING: sidecar {sc_path} too large or changing; left unclassified")

    tracks: list[dict] | None = None   # embedded English text tracks (None: unknown)
    probed = False

    def _embedded_tracks() -> list[dict] | None:
        nonlocal tracks, probed
        if not probed:
            probed = True
            data = info if info is not None else probe(path)
            if data is None:
                log(f"WARNING: cannot probe {path} to judge late English sidecars "
                    f"redundant; keeping them")
            else:
                tracks = _embedded_english_text_tracks(data)
        return tracks

    rejection = _upgrade_rejection_reasons(path, snaps, sidecars, upgrade_rejection)
    content_ok: dict = {}   # (embedded stream index, sidecar is forced) -> positive English
    foreign = redundant = 0
    for sc_path, is_forced, _is_sdh in sidecars:
        snap = snaps[sc_path]
        if snap is None:
            continue
        text = decode_subtitle_text(snap.data)
        if text is None:
            log(f"WARNING: sidecar {sc_path} is not decodable as text; left unclassified")
            continue
        is_eng, verdict_info = classify_candidate(text)
        if is_eng and rejection is not None and not is_forced \
                and not is_generated_sidecar(path, sc_path):
            # A sidecar the upgrade check REJECTED is quarantined; no verdict: kept, retried.
            why = rejection.get(sc_path.name)
            if why is None:
                log(f"kept: possible upgrade for generated subtitle: {sc_path}")
                continue
            reason, is_redundant = f"rejected upgrade: {why}", True
        elif is_eng:
            embedded = _embedded_tracks()
            if embedded is None:
                continue
            verdict = _late_english_sidecar_verdict(path, sc_path, is_forced, embedded)
            if verdict == "keep":
                continue
            if verdict == "upgrade":
                log(f"kept: possible upgrade for generated subtitle: {sc_path}")
                continue
            # CONTENT, not just the tag: a French track tagged `eng` must not cost the file
            # its only real subtitle.
            candidates = sorted(_eligible_embedded(embedded, is_forced),
                                key=is_generated_track)
            verified = False
            for track in candidates[:_MAX_EMBEDDED_CONTENT_CHECKS]:
                key_ = (track.get("index"), is_forced)
                if key_ not in content_ok:
                    content_ok[key_] = _embedded_track_is_english(path, track, is_forced)
                if content_ok[key_]:
                    verified = True
                    break
            if not verified:
                log(f"kept: embedded English subtitle of {path} not verified as English; "
                    f"keeping late sidecar {sc_path}")
                continue
            reason, is_redundant = _REDUNDANT_REASON, True
        else:
            reason, is_redundant = _LATE_FOREIGN_REASON, False
        if dry_run:
            log(f"DRY-RUN: would quarantine late sidecar {sc_path} "
                f"({reason if is_redundant else 'verdict=' + str(verdict_info.get('verdict'))})")
            ok = True
        else:
            if file_identity(path) != video_identity:
                log(f"WARNING: {path} changed while its late sidecars were being judged; "
                    f"keeping the rest (the next scan starts over)")
                break
            ok = _quarantine_sidecar(sc_path, snap, verdict_info, reason)
        if ok:
            if is_redundant:
                redundant += 1
            else:
                foreign += 1
    return foreign, redundant


def _late_sidecar_sweep(path: Path, dry_run: bool, info: dict | None,
                upgrade_rejection: object,
                video_identity: object = _UNSET) -> tuple[int, int]:
    """`_quarantine_foreign_sidecars` that never changes the SKIP result it decorates."""
    try:
        return _quarantine_foreign_sidecars(path, dry_run, info, upgrade_rejection,
                                            video_identity)
    except Exception as exc:
        log(f"WARNING: late-sidecar sweep failed for {path}: {type(exc).__name__}: {exc}")
        return 0, 0


def _cleanup_no_main_english(
    path: Path, path_str: str, status: str, base_detail: str,
    sub_streams: list[dict], audio_streams: list[dict], video_streams: list[dict],
    forced_text: dict | None, eng_image: list[dict],
    dry_run: bool,
    proven_foreign_sub_indices: frozenset[int] = frozenset(),
) -> dict:
    """On NEEDS_BAZARR, record the file in the needs-english tracker for `sub-fallback.py`.
    `pre_identity` is captured BEFORE the impl so a new release still resets `first_seen`
    while the impl's own remux does not."""
    pre_identity = _file_identity(path)
    result = _cleanup_no_main_english_impl(
        path, path_str, status, base_detail,
        sub_streams, audio_streams, video_streams,
        forced_text, eng_image, dry_run, proven_foreign_sub_indices,
    )
    if result.get("status") == "NEEDS_BAZARR":
        _apply_cleanup_needs_english_update(path, pre_identity, Path(result["path"]), dry_run)
    return result


def _cleanup_no_main_english_impl(
    path: Path, path_str: str, status: str, base_detail: str,
    sub_streams: list[dict], audio_streams: list[dict], video_streams: list[dict],
    forced_text: dict | None, eng_image: list[dict],
    dry_run: bool,
    proven_foreign_sub_indices: frozenset[int] = frozenset(),
) -> dict:
    """Cleanup remux for a file with no main English subtitle yet (called from the four
    `_process_locked` NEEDS_BAZARR / FAIL branches), so a MULTI release does not keep every
    foreign subtitle and a foreign default audio for weeks while Bazarr works.

    Never delete a track that might be wanted and never silently change a flag: a wrong
    default is recoverable, a deleted track is not.
      - Video is not touched (cover-art removal is the full path's job).
      - Audio is never dropped or reordered; only the `default` flag moves, and only when
        `confident_default_audio` is confident (never a bare `default`, never a guess).
        `None` leaves audio as is, so an Arr outage cannot re-remux back and forth.
      - Subtitles: only a POSITIVELY foreign track (`_is_positively_foreign_sub`) or one
        proven foreign by the content gate is dropped; ambiguous ones, the forced-English
        text track and every English image subtitle (`eng_image`) are kept.
      - Every kept track gets explicit flags from the SOURCE dispositions
        (`cleanup_flags`); the confident audio pick is the only deliberate change.
      - No loudness normalization (it can re-encode audio and ffmpeg auto-flags a default).

    Does NOT stamp CONSOLIDATED_SUBS or touch sidecars: the file still gets the full pass
    once an English subtitle appears.

    The output is re-probed before the swap: track counts and the default-flag pattern
    must match the intent, else FAIL with the original untouched. Returns the caller's
    `status` (unless remux/verify failed); `detail` gains a cleanup summary.
    """
    original_language = original_language_for(path)
    confident_idx = confident_default_audio(
        path, audio_streams, original_language=original_language)

    default_flagged = {
        s["index"] for s in audio_streams
        if (s.get("disposition") or {}).get("default")
    }
    audio_default_change = confident_idx is not None and default_flagged != {confident_idx}

    keep_video_idx = [s["index"] for s in video_streams]
    keep_audio_idx = [s["index"] for s in audio_streams]

    keep_sub_indices = {s["index"] for s in eng_image}
    if forced_text is not None:
        keep_sub_indices.add(forced_text["index"])
    # forced_text/eng_image are never dropped: they are not gated.
    for s in sub_streams:
        if s["index"] in keep_sub_indices:
            continue
        if s["index"] in proven_foreign_sub_indices or _is_positively_foreign_sub(s):
            continue
        keep_sub_indices.add(s["index"])
    kept_sub_streams = [s for s in sub_streams if s["index"] in keep_sub_indices]

    dropped_subs = sum(1 for s in sub_streams if s["index"] not in keep_sub_indices)

    something_to_clean = audio_default_change or dropped_subs > 0

    if not something_to_clean:
        return {"path": path_str, "status": status,
                "detail": f"{base_detail}; nothing to clean"}

    if dry_run:
        return {"path": path_str, "status": status,
                "detail": (f"{base_detail}; dry-run: would clean "
                           f"audio_default_change={audio_default_change} "
                           f"dropped_subs={dropped_subs}")}

    cleanup_flags: dict[int, dict[str, bool]] = {}
    for s in video_streams:
        cleanup_flags[s["index"]] = {"default": bool((s.get("disposition") or {}).get("default"))}
    for s in audio_streams:
        is_default = (s["index"] == confident_idx) if audio_default_change \
            else bool((s.get("disposition") or {}).get("default"))
        cleanup_flags[s["index"]] = {"default": is_default}
    for s in kept_sub_streams:
        disp = s.get("disposition") or {}
        cleanup_flags[s["index"]] = {
            "default": bool(disp.get("default")),
            "forced": bool(disp.get("forced")),
        }

    out = path.with_suffix(path.suffix + f".consol.{os.getpid()}.tmp.mkv")
    out.unlink(missing_ok=True)
    ok, detail = remux(
        path, out, keep_video_idx, keep_audio_idx, None, None,
        keep_subtitle_indices=sorted(keep_sub_indices),
        cleanup_flags=cleanup_flags,
    )
    if not ok:
        out.unlink(missing_ok=True)
        return {"path": path_str, "status": "FAIL", "detail": f"cleanup remux: {detail}"}

    # Verify BEFORE the swap; never touch the original on a mismatch.
    verify_info = probe(out)
    if verify_info is None:
        out.unlink()
        return {"path": path_str, "status": "FAIL", "detail": "cleanup verify: probe failed"}
    verify_streams = verify_info.get("streams", [])
    out_video_streams = [s for s in verify_streams if s.get("codec_type") == "video"]
    out_audio_streams = [s for s in verify_streams if s.get("codec_type") == "audio"]
    out_subs = sum(1 for s in verify_streams if s.get("codec_type") == "subtitle")
    expected_audio = len(audio_streams)
    expected_subs = len(keep_sub_indices)
    expected_video = len(video_streams)
    if (len(out_audio_streams) != expected_audio or out_subs != expected_subs
            or len(out_video_streams) != expected_video):
        out.unlink()
        return {"path": path_str, "status": "FAIL",
                "detail": (f"cleanup verify: expected audio={expected_audio} "
                           f"subs={expected_subs} video={expected_video}, got "
                           f"audio={len(out_audio_streams)} subs={out_subs} "
                           f"video={len(out_video_streams)}")}

    # Streams of each type keep their relative order: compare by position.
    intended_video_defaults = [cleanup_flags[i]["default"] for i in keep_video_idx]
    intended_audio_defaults = [cleanup_flags[i]["default"] for i in keep_audio_idx]
    out_video_defaults = [bool((s.get("disposition") or {}).get("default")) for s in out_video_streams]
    out_audio_defaults = [bool((s.get("disposition") or {}).get("default")) for s in out_audio_streams]
    if out_video_defaults != intended_video_defaults or out_audio_defaults != intended_audio_defaults:
        out.unlink()
        return {"path": path_str, "status": "FAIL",
                "detail": (f"cleanup verify: default-flag mismatch -- "
                           f"video expected={intended_video_defaults} got={out_video_defaults}, "
                           f"audio expected={intended_audio_defaults} got={out_audio_defaults}")}

    replacement = path.with_suffix(".mkv")
    if replacement != path and replacement.exists():
        out.unlink(missing_ok=True)
        return {"path": path_str, "status": "FAIL",
                "detail": f"collision: sibling .mkv exists for {path.name}"}

    # tag=False: stamping would stop this branch from ever running again.
    early_result, _tag_written = _expose_tag_normalize(
        out, path, replacement, path_str, tag=False, normalize=False)
    if early_result is not None:
        return early_result

    return {"path": str(replacement), "status": status,
            "detail": (f"{base_detail}; cleaned: audio_default_change={audio_default_change} "
                       f"dropped_subs={dropped_subs}")}


def _workfile(workdir: Path, role: str, suffix: str) -> Path:
    """Every workdir temp file comes from here, never from a name derived from the video or
    a sidecar (a video called `forced` with a `forced.srt` sidecar would collide)."""
    fd, name = tempfile.mkstemp(prefix=f"{role}-", suffix=suffix, dir=workdir)
    os.close(fd)
    return Path(name)


def _sidecar_as_srt(sc_path: Path, workdir: Path, data: bytes | None = None) -> Path | None:
    """Copy sidecar `sc_path` (or write `data`) into `workdir`, converted to SRT if
    `.ass`/`.ssa`/`.vtt`. None if conversion failed."""
    local = _workfile(workdir, "sidecar", sc_path.suffix)
    if data is None:
        shutil.copy2(sc_path, local)
    else:
        local.write_bytes(data)
    suffix = local.suffix.lower()
    if suffix in (".ass", ".ssa"):
        srt_p = _workfile(workdir, "sidecar-srt", ".srt")
        if not ass_to_srt(local, srt_p):
            return None
        local = srt_p
    elif suffix == ".vtt":
        srt_p = _workfile(workdir, "sidecar-srt", ".srt")
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(local), str(srt_p)],
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            return None
        local = srt_p
    return local


class _SubCandidate(NamedTuple):
    score: int
    path: Path
    source: str       # "embedded" | "sidecar" | "subliminal"
    generated: bool   # Whisper output (".generated" sidecar / "(generated)" title)
    sdh: bool         # hearing-impaired / SDH / CC variant

    @property
    def rank(self) -> tuple[bool, bool, int]:
        """Higher wins: real beats generated; within a class non-SDH beats SDH (used only
        when it is the sole candidate in its class); score only breaks ties."""
        return (not self.generated, not self.sdh, self.score)


# Generated -> real subtitle upgrade. A file consolidated with only a Whisper transcript
# ("English (generated)") promises a real subtitle will replace it. Runs under the per-file
# lock BEFORE the state-cache / tag-present skips. A sidecar is used only once proven the
# right episode's English subtitle: English text whose cues match the generated track in
# >= 2 of 3 bounded windows (`judge_sidecar_against_windows`).
UPGRADE_REJECTED_KEY = "upgrade_rejected"
# A sidecar is read ONCE; anything bigger is refused, never judged on a prefix.
UPGRADE_MAX_SIDECAR_BYTES = 8 * 1024 * 1024

class _Snapshot(NamedTuple):
    """One sidecar, read once: what was verified is exactly these bytes."""
    identity: Identity  # fstat on the same handle, unchanged across the read
    digest: str          # sha256 of `data`
    data: bytes


class _Verified(NamedTuple):
    srt: bytes           # the verified working SRT bytes
    sdh: bool            # ranking only
    identity: Identity
    digest: str          # sha256 of the sidecar as read (before conversion)


class _Upgrade(NamedTuple):
    # The full pass muxes these exact bytes, then deletes the one it muxed if still exactly
    # what was read, and nothing else.
    sidecars: dict[Path, _Verified]
    # Re-checked before the remux and the swap; any change aborts.
    video_identity: Identity


_KEEPABLE_SUB_CODECS = TEXT_CODECS | IMAGE_CODECS


def _forced_english_tracks(sub_streams: list[dict]) -> list[dict]:
    """Every forced English subtitle track (text or image); an upgrade carries each over."""
    return [s for s in sub_streams
            if is_english_tag((s.get("tags") or {}).get("language")) and is_forced_track(s)]


def _read_snapshot(p: Path) -> _Snapshot | None:
    """Read `p` once (fstat and bytes from the same handle). None if over
    `UPGRADE_MAX_SIDECAR_BYTES` (never verify, mux or quarantine a prefix) or not read
    completely and stably. Raises OSError if unreadable (`_snapshot_sidecar` maps that to
    None)."""
    limit = UPGRADE_MAX_SIDECAR_BYTES
    with p.open("rb") as fh:
        before = os.fstat(fh.fileno())
        if before.st_size > limit:
            return None
        data = fh.read(limit + 1)  # may have grown since fstat
        after = os.fstat(fh.fileno())
    if len(data) > limit:
        return None
    # Complete and stable; a file being written or truncated is retried next scan.
    if len(data) != before.st_size:
        return None
    if identity_from_stat(before)[1:] != identity_from_stat(after)[1:]:
        return None
    return _Snapshot(identity_from_stat(before), hashlib.sha256(data).hexdigest(), data)


def _snapshot_sidecar(p: Path) -> _Snapshot | None:
    """`_read_snapshot`, with unreadable also None (retried next scan)."""
    try:
        return _read_snapshot(p)
    except OSError:
        return None


def _sidecar_fingerprint(snaps: dict[Path, _Snapshot]) -> list[list]:
    """`[name, size, sha256]` per sidecar, for matching a recorded rejection. The hash
    catches a same-size rewrite that size and mtime miss."""
    return [[p.name, s.identity[1], s.digest]
            for p, s in sorted(snaps.items(), key=lambda kv: kv[0].name)]


def _rejection_record(video_identity: Identity, fingerprint: list[list],
                      reasons: dict[str, str] | None = None) -> dict:
    """Fingerprint of each sidecar with a NON-TRANSIENT outcome (a verdict, or undecided)
    plus the video identity; a transiently failed sidecar is left out so it stays
    retryable. `reasons` ({name: why}) is quoted when the late sweep quarantines a rejected
    sidecar; a record without it is the older format and is rejected."""
    return {"video": list(video_identity), "sidecars": fingerprint,
            "reasons": dict(reasons or {})}


def _recorded_verdicts(record: object, video_identity: Identity,
                       snaps: dict[Path, "_Snapshot"]) -> dict[str, str | None] | None:
    """What a rejection `record` still says PER SIDECAR. None if it does not apply to this
    video (old format or identity differs); else `{name: reason or None}` for sidecars
    whose fingerprint still matches (reason = wrong-episode verdict, None = judged with no
    verdict). A changed or unrecorded sidecar is absent and verified again."""
    if not isinstance(record, dict) or not isinstance(record.get("reasons"), dict):
        return None
    if not identity_matches(record.get("video"), video_identity, allow_legacy=False):
        return None
    entries = record.get("sidecars")
    recorded = {e[0]: e for e in entries if isinstance(e, list) and len(e) == 3
                and isinstance(e[0], str)} if isinstance(entries, list) else {}
    reasons = record["reasons"]
    out: dict[str, str | None] = {}
    for sc, snap in snaps.items():
        if recorded.get(sc.name) == [sc.name, snap.identity[1], snap.digest]:
            why = reasons.get(sc.name)
            out[sc.name] = str(why) if why else None
    return out


def _record_upgrade_rejected(key: str, prev: dict, rejection: dict) -> None:
    """Best effort: lets the next scan skip the window reads."""
    try:
        update_state_entry(STATE_FILE, key, {**prev, UPGRADE_REJECTED_KEY: rejection})
    except Exception as exc:
        log(f"UPGRADE-SKIP: could not record rejection for {key}: "
            f"{type(exc).__name__}: {exc}")


def _generated_upgrade_check(path: Path, cur: tuple[int, int], prev: dict | None
                             ) -> tuple[_Upgrade | None, dict | None, dict | None]:
    """Should `path` (locked by the caller) have its generated English subtitle replaced by
    a real sidecar? Returns `(upgrade, info, rejected)`.

    `upgrade` is None unless: a MAIN non-generated English sidecar exists; the file is
    already consolidated (tag, or a `fixed` state entry for the current size/mtime, since
    the tag write can fail); every main English text track is "(generated)"; and a sidecar
    passes the English-text and same-episode checks. Each sidecar is read once and those
    exact bytes go to the full pass.

    `info` is the ffprobe result if this probed. `rejected` is the record to remember when
    verification judged the sidecars wrong, never for anything possibly transient.
    """
    real = [(p, sdh) for (p, forced, sdh) in find_sidecar_subs(path)
            if not forced and not is_generated_sidecar(path, p)]
    if not real:
        return None, None, None

    notes: list[str] = []
    reasons: dict[str, str] = {}  # name -> why REJECTED (a verdict, not a failure)
    judged: dict[Path, _Snapshot] = {}
    snaps: dict[Path, _Snapshot] = {}
    for sc, _sdh in real:
        snap = _snapshot_sidecar(sc)
        if snap is None:
            notes.append(f"{sc.name}: unreadable or larger than "
                         f"{UPGRADE_MAX_SIDECAR_BYTES} bytes")
            continue
        snaps[sc] = snap
    if not snaps:
        log(f"UPGRADE-SKIP: {path.name}: {'; '.join(notes)}")
        return None, None, None

    video_identity = file_identity(path)  # before the probe
    if video_identity is None:
        return None, None, None
    remembered = _recorded_verdicts((prev or {}).get(UPGRADE_REJECTED_KEY),
                                    video_identity, snaps) or {}
    if len(snaps) == len(real) and all(sc.name in remembered for sc in snaps):
        return None, None, None

    info = probe(path)
    consolidated = bool(info) and (
        already_processed(info)
        or (bool(prev) and prev.get("size") == cur[0] and prev.get("mtime") == cur[1]
            and prev.get("v") == PIPELINE_VERSION and prev.get("status") == "fixed"))
    if not info or not consolidated:
        return None, info, None
    gen_tracks = [s for s in info.get("streams", [])
                  if s.get("codec_type") == "subtitle"
                  and is_english_tag((s.get("tags") or {}).get("language"))
                  and s.get("codec_name") in TEXT_CODECS and not is_forced_track(s)]
    if not gen_tracks or not all(is_generated_track(s) for s in gen_tracks):
        return None, info, None
    gen = gen_tracks[0]
    subs_src = [s for s in info.get("streams", []) if s.get("codec_type") == "subtitle"]
    unkeepable = [s for s in _forced_english_tracks(subs_src)
                  if s.get("codec_name") not in _KEEPABLE_SUB_CODECS]
    if unkeepable:
        # The upgrade rebuilds subtitles, so it must carry every forced English track over.
        log(f"UPGRADE-SKIP: {path.name}: forced English subtitle track(s) it cannot keep "
            f"(codec {', '.join(str(s.get('codec_name')) for s in unkeepable)}); "
            f"keeping the generated subtitle")
        return None, info, None

    try:
        duration = float((info.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        duration = 0.0

    sdh_of = dict(real)
    verified: dict[Path, _Verified] = {}
    windows = None
    with tempfile.TemporaryDirectory(prefix="consolsub-upgrade-") as td:
        for sc, snap in snaps.items():
            if sc.name in remembered:
                judged[sc] = snap
                if remembered[sc.name]:
                    reasons[sc.name] = str(remembered[sc.name])
                continue
            decoded = decode_subtitle_text(snap.data)
            if decoded is None:
                notes.append(f"{sc.name}: not decodable as text")
                judged[sc] = snap
                continue
            try:
                srt = _sidecar_as_srt(sc, Path(td), decoded.encode("utf-8"))
                srt_bytes = srt.read_bytes() if srt is not None else None
            except (OSError, subprocess.SubprocessError) as exc:
                notes.append(f"{sc.name}: could not be converted ({type(exc).__name__})")
                continue   # transient: retried next scan
            if srt_bytes is None:
                notes.append(f"{sc.name}: could not be converted to SRT")
                continue
            sc_text = clean_srt_text(srt_bytes.decode("utf-8", errors="replace"))
            verdict = sublang.score(sc_text)["verdict"]
            if verdict != "eng":
                notes.append(f"{sc.name}: not usable English text (language check: {verdict})")
                # No reason: this test is stricter than the content gate ("la la la" or a
                # short unlisted-language file fail it), so it does not prove "foreign".
                judged[sc] = snap
                continue
            if windows is None:
                windows = generated_track_windows(path, gen, duration)
                if windows is None:
                    notes.append("could not read the generated track's cues from the file")
                    break   # transient and video-wide
                if not windows:
                    notes.append("the file's duration is missing or zero, so no verification "
                                 "windows can be placed")
                    break
            ok, why, usable, evidence = judge_sidecar_against_windows(windows, sc_text)
            if ok:
                verified[sc] = _Verified(srt_bytes, sdh_of[sc], snap.identity, snap.digest)
            else:
                notes.append(f"{sc.name}: not this file's subtitle ({why})")
                judged[sc] = snap
                if evidence >= UPGRADE_MIN_PASSING_WINDOWS:
                    # A verdict needs enough text on BOTH sides in >= 2 windows; a partial
                    # sidecar is "not enough evidence" and the late sweep must not act on it.
                    reasons[sc.name] = f"not this file's subtitle ({why})"

    if not verified:
        log(f"UPGRADE-SKIP: {path.name}: {'; '.join(notes)}")
        return None, info, (_rejection_record(video_identity, _sidecar_fingerprint(judged), reasons)
                            if judged else None)
    return _Upgrade(verified, video_identity), info, None


def _upgrade_track_flags(video_streams: list[dict], audio_streams: list[dict],
                         kept_subs: list[dict]) -> dict[int, dict[str, bool]]:
    """Explicit default AND forced flag from the source dispositions (else mkvmerge picks)."""
    flags: dict[int, dict[str, bool]] = {}
    for s in [*video_streams, *audio_streams, *kept_subs]:
        disp = s.get("disposition") or {}
        flags[s["index"]] = {"default": bool(disp.get("default")),
                             "forced": bool(disp.get("forced"))}
    return flags


def _verify_upgrade_output(out: Path, video_streams: list[dict], audio_streams: list[dict],
                           kept_subs: list[dict], flags: dict[int, dict[str, bool]]
                           ) -> str | None:
    """Re-probe `out`; the first mismatch with what an upgrade must produce, else None:
    every source video/audio stream in order with its flags; then every kept forced English
    track (source codec, English tag, flags); then the new main English subtitle (English,
    default, not forced)."""
    info = probe(out)
    if not info:
        return "probe of the remuxed file failed"
    streams = info.get("streams", [])
    out_v = [s for s in streams if s.get("codec_type") == "video"]
    out_a = [s for s in streams if s.get("codec_type") == "audio"]
    out_s = [s for s in streams if s.get("codec_type") == "subtitle"]
    expect_subs = len(kept_subs) + 1
    if (len(out_v), len(out_a), len(out_s)) != (len(video_streams), len(audio_streams), expect_subs):
        return (f"expected video={len(video_streams)} audio={len(audio_streams)} "
                f"subs={expect_subs}, got video={len(out_v)} audio={len(out_a)} "
                f"subs={len(out_s)}")

    def _flags(ss: list[dict]) -> list[tuple[bool, bool]]:
        return [(bool((s.get("disposition") or {}).get("default")),
                 bool((s.get("disposition") or {}).get("forced"))) for s in ss]

    want_v = [(flags[s["index"]]["default"], flags[s["index"]]["forced"]) for s in video_streams]
    want_a = [(flags[s["index"]]["default"], flags[s["index"]]["forced"]) for s in audio_streams]
    if _flags(out_v) != want_v or _flags(out_a) != want_a:
        return (f"default/forced flag mismatch -- video expected={want_v} got={_flags(out_v)}, "
                f"audio expected={want_a} got={_flags(out_a)}")

    def _english(s: dict) -> bool:
        return is_english_tag((s.get("tags") or {}).get("language"))

    got_subs = _flags(out_s)
    for pos, src in enumerate(kept_subs):
        got = out_s[pos]
        if got.get("codec_name") != src.get("codec_name"):
            return (f"kept forced subtitle #{pos + 1} changed codec: "
                    f"{src.get('codec_name')} -> {got.get('codec_name')}")
        if not _english(got):
            return f"kept forced subtitle #{pos + 1} is not tagged English"
        want = (flags[src["index"]]["default"], flags[src["index"]]["forced"])
        if got_subs[pos] != want:
            return (f"kept forced subtitle #{pos + 1} flags changed: default/forced "
                    f"expected={want} got={got_subs[pos]}")
    main = out_s[-1]
    main_default, main_forced = got_subs[-1]
    if not _english(main):
        return "the new main subtitle is not tagged English"
    if not main_default or main_forced:
        return (f"the new main subtitle must be default and not forced, got "
                f"default={main_default} forced={main_forced}")
    return None


def _verify_kept_source_subs(out: Path, n_video: int, n_audio: int, src: "dict | None",
                             kept: dict[int, dict[str, bool]]) -> str | None:
    """Re-probe `out` of a full pass that stream-copied an unreadable embedded forced
    English track (`kept`); the first mismatch, else None. Expects the source track (codec,
    English tag, exact flags), then the new main English subtitle (default, not forced)."""
    if src is None:
        return "no source track to compare with"
    info = probe(out)
    if not info:
        return "probe of the remuxed file failed"
    streams = info.get("streams", [])
    out_v = [s for s in streams if s.get("codec_type") == "video"]
    out_a = [s for s in streams if s.get("codec_type") == "audio"]
    out_s = [s for s in streams if s.get("codec_type") == "subtitle"]
    if (len(out_v), len(out_a), len(out_s)) != (n_video, n_audio, 2):
        return (f"expected video={n_video} audio={n_audio} subs=2, got "
                f"video={len(out_v)} audio={len(out_a)} subs={len(out_s)}")

    def _flags(s: dict) -> tuple[bool, bool]:
        d = s.get("disposition") or {}
        return bool(d.get("default")), bool(d.get("forced"))

    first, main = out_s
    want = kept[src["index"]]
    if first.get("codec_name") != src.get("codec_name"):
        return (f"the kept forced track changed codec: {src.get('codec_name')} -> "
                f"{first.get('codec_name')}")
    if not is_english_tag((first.get("tags") or {}).get("language")):
        return "the kept forced track is not tagged English"
    if _flags(first) != (want["default"], want["forced"]):
        return (f"the kept forced track's flags changed: default/forced expected="
                f"{(want['default'], want['forced'])} got={_flags(first)}")
    if not is_english_tag((main.get("tags") or {}).get("language")):
        return "the new main subtitle is not tagged English"
    if _flags(main) != (True, False):
        return f"the new main subtitle must be default and not forced, got {_flags(main)}"
    return None


def _upgrade_video_changed(path: Path, path_str: str, upgrade: _Upgrade, when: str,
                           out: Path | None = None) -> dict | None:
    """The SKIP result that aborts the upgrade if the video is no longer the file the gate
    probed (temp output removed, retried next scan); else None."""
    if file_identity(path) == upgrade.video_identity:
        return None
    if out is not None:
        out.unlink(missing_ok=True)
    log(f"UPGRADE-SKIP: {path.name}: the video changed {when}; keeping it as it is")
    return {"path": path_str, "status": "SKIP",
            "detail": f"upgrade: the video changed {when}, original kept"}


def _process_locked(path: Path, path_str: str, dry_run: bool = False) -> dict:
    if not path.is_file():
        return {"path": path_str, "status": "SKIP", "detail": "file gone after lock"}

    state = load_state(STATE_FILE)
    key = f"{path.resolve()}"
    cur = file_key(path)
    video_identity = file_identity(path)
    prev = state.get(key)

    # The upgrade check runs first, ahead of the two skips. If it upgrades, the late sweep
    # does NOT run (the full pass consumes the verified sidecars); otherwise the late sweep
    # runs on either skip path, with the check's rejection.
    try:
        upgrade, info, rejected = _generated_upgrade_check(path, cur, prev)
    except Exception as exc:
        log(f"UPGRADE-SKIP: {path.name}: check failed: {type(exc).__name__}: {exc}")
        upgrade, info, rejected = None, None, None
    if upgrade is not None:
        names = ", ".join(sorted(p.name for p in upgrade.sidecars))
        if dry_run:
            return {"path": path_str, "status": "SKIP",
                    "detail": f"dry-run: would UPGRADE generated -> real subtitle ({names})"}
        log(f"UPGRADE: generated -> real subtitle ({names})")
    else:
        remembered = rejected or (prev or {}).get(UPGRADE_REJECTED_KEY)
        if prev and prev.get("size") == cur[0] and prev.get("mtime") == cur[1] and prev.get("v") == PIPELINE_VERSION:
            if rejected and not dry_run:
                _record_upgrade_rejected(key, prev, rejected)
            foreign, redundant = _late_sidecar_sweep(path, dry_run, info, remembered,
                                                     video_identity)
            return {"path": path_str, "status": "SKIP",
                    "detail": _late_sidecar_detail("state cache", foreign, redundant, dry_run)}

        if info is None:
            info = probe(path)
        if not info:
            return {"path": path_str, "status": "FAIL", "detail": "probe failed"}

        if already_processed(info):
            if not dry_run:
                entry = {"size": cur[0], "mtime": cur[1],
                         "v": PIPELINE_VERSION, "status": "skip-tag"}
                if rejected:
                    entry[UPGRADE_REJECTED_KEY] = rejected
                update_state_entry(STATE_FILE, key, entry)
            foreign, redundant = _late_sidecar_sweep(path, dry_run, info, remembered,
                                                     video_identity)
            return {"path": path_str, "status": "SKIP",
                    "detail": _late_sidecar_detail("tag present", foreign, redundant, dry_run)}
    assert info is not None

    streams = info.get("streams", [])
    sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

    # No early return on empty sub_streams: sidecar SRTs are a valid source. An untagged
    # or "und" track is deliberately not a candidate.
    eng_streams = [s for s in sub_streams
                   if is_english_tag((s.get("tags") or {}).get("language"))]

    eng_text   = [s for s in eng_streams if s.get("codec_name") in TEXT_CODECS]
    eng_image  = [s for s in eng_streams if s.get("codec_name") in IMAGE_CODECS]

    forced_text = next((s for s in eng_text if is_forced_track(s)), None)
    main_candidates = [s for s in eng_text if s is not forced_text]

    # The only sidecars FIXED may remove, and only while this identity still holds.
    consumed: dict[Path, Identity] = {}
    accepted_forced: dict[Path, _Snapshot] = {}
    sidecars: list = []
    unclassified_forced: list[Path] = []
    if upgrade is None:
        sidecars = find_sidecar_subs(path)
        if not dry_run:
            sidecars = _gate_forced_sidecars(sidecars, accepted_forced, unclassified_forced)
        sidecar_main = [(p, sdh, is_generated_sidecar(path, p))
                        for (p, forced, sdh) in sidecars if not forced]
        sidecar_forced = next(((p) for (p, forced, sdh) in sidecars if forced), None)
    else:
        # Exactly the verified snapshots; the generated embedded track is being replaced.
        sidecar_main = [(p, v.sdh, False) for p, v in upgrade.sidecars.items()]
        sidecar_forced = None
        main_candidates = []
        eng_image = []
        forced_text = None

    # Skipped in a dry run (no extraction) and an upgrade (forced tracks are kept).
    forced_gate: _ForcedGate | None = None
    rejected_forced_idx: int | None = None
    forced_track_unreadable = False
    unreadable_forced_stream: dict | None = None
    if forced_text is not None and upgrade is None and not dry_run:
        forced_gate = _gate_embedded_forced(path, forced_text)
        if forced_gate.state == "rejected":
            rejected_forced_idx = forced_text["index"]
            forced_text = None
        elif forced_gate.state == "unreadable":
            forced_track_unreadable = True
            unreadable_forced_stream = forced_text
            forced_text = None
    unclassified_parts = []
    if unclassified_forced:
        unclassified_parts.append(f"{len(unclassified_forced)} forced sidecar(s) unreadable or not text")
    if forced_track_unreadable:
        unclassified_parts.append("embedded forced track unreadable or not text")
    unclassified_note = "; ".join(unclassified_parts)
    early_proven_foreign = (frozenset({rejected_forced_idx})
                            if rejected_forced_idx is not None else frozenset())

    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    video_streams_all = [s for s in streams if s.get("codec_type") == "video"]
    real_video = real_video_streams(streams)

    # The cleanup path gets the raw `video_streams_all`; the FULL path drops cover art and
    # keeps one real video stream.
    video_streams = real_video
    if len(video_streams) > 1:
        default_v = next(
            (s for s in video_streams if (s.get("disposition") or {}).get("default")),
            video_streams[0],
        )
        video_streams = [default_v]
    keep_video_idx = [s["index"] for s in video_streams]
    dropped_video = max(0, len(video_streams_all) - len(video_streams))
    if upgrade is not None:
        # An upgrade changes only the English subtitle.
        keep_video_idx = [s["index"] for s in video_streams_all]
        dropped_video = 0

    if not main_candidates and not eng_image and not sidecar_main:
        if sidecar_forced or forced_text:
            return _cleanup_no_main_english(
                path, path_str, "FAIL",
                "only forced English" + (f" ({unclassified_note})" if unclassified_note else ""),
                sub_streams, audio_streams, video_streams_all,
                forced_text, eng_image, dry_run, early_proven_foreign,
            )
        if unclassified_note:
            # A tooling problem, not "no English candidates" (sub-fallback would place a
            # main sidecar beside the unreadable forced one).
            return _cleanup_no_main_english(
                path, path_str, "FAIL",
                f"all extracts failed ({unclassified_note}"
                + ("; 1 rejected by content gate" if rejected_forced_idx is not None else "") + ")",
                sub_streams, audio_streams, video_streams_all,
                forced_text, eng_image, dry_run, early_proven_foreign,
            )
        return _cleanup_no_main_english(
            path, path_str, "NEEDS_BAZARR",
            "no English candidates"
            + (" (rejected by content gate)" if rejected_forced_idx is not None else ""),
            sub_streams, audio_streams, video_streams_all,
            forced_text, eng_image, dry_run, early_proven_foreign,
        )

    if dry_run:
        sources = []
        if main_candidates: sources.append(f"embedded={len(main_candidates)}")
        if sidecar_main:    sources.append(f"sidecar={len(sidecar_main)}")
        if eng_image:       sources.append(f"image={len(eng_image)} (needs subliminal)")
        return {"path": path_str, "status": "SKIP",
                "detail": f"dry-run: would FIX ({', '.join(sources)})"}

    workdir = Path(tempfile.mkdtemp(prefix=f"consolsub_{os.getpid()}_", dir=path.parent))
    try:
        chosen_srt: Path | None = None

        scored: list[_SubCandidate] = []
        # content_rejected: TEXT scored non-English despite an English tag/name.
        # extraction_failed: could not be extracted/read/converted (tooling, kept visible).
        # rejected_embedded: embedded tracks proven non-English (the cleanup drops them).
        content_rejected = 1 if rejected_forced_idx is not None else 0
        extraction_failed = len(unclassified_forced) + (1 if forced_track_unreadable else 0)
        rejected_embedded: set[int] = (
            {rejected_forced_idx} if rejected_forced_idx is not None else set())

        for s in main_candidates:
            ext = ".ass" if s.get("codec_name") in ASS_CODECS else ".srt"
            tmp = _workfile(workdir, f"embedded{s['index']}", ext)
            if not extract_track(path, s["index"], s.get("codec_name"), tmp):
                extraction_failed += 1
                continue
            # Gate the extracted file before any ASS/SSA conversion.
            try:
                is_eng, verdict_info = _gate_local_file(tmp)
            except OSError as exc:
                if isinstance(exc, _Unclassifiable):
                    log(f"content-gate SKIP embedded track {s['index']} in {path}: cannot be "
                        f"classified ({exc}); not a candidate")
                extraction_failed += 1
                continue
            if not is_eng:
                content_rejected += 1
                rejected_embedded.add(s["index"])
                log(f"content-gate REJECT embedded track {s['index']} in {path}: "
                    f"tagged English, text scored {verdict_info.get('verdict')} "
                    f"(reason={verdict_info.get('reason')}) -- treated as foreign")
                continue
            if tmp.suffix == ".ass":
                srt_p = _workfile(workdir, f"embedded{s['index']}-srt", ".srt")
                if not ass_to_srt(tmp, srt_p):
                    extraction_failed += 1
                    continue
                tmp = srt_p
            lc = count_subtitle_lines(tmp)
            scored.append(_SubCandidate(score_text_track(s, lc), tmp, "embedded",
                                        is_generated_track(s), is_sdh_track(s)))

        consumed_from: dict[Path, Path] = {}  # working file -> its sidecar (upgrade)
        for n, (sc_path, is_sdh, sc_generated) in enumerate(sidecar_main):
            sc_ident: Identity | None = None
            if upgrade is not None:
                # The verified bytes, not the file as it is now.
                try:
                    local = _workfile(workdir, f"verified{n}", ".srt")
                    local.write_bytes(upgrade.sidecars[sc_path].srt)
                except OSError:
                    continue
                consumed_from[local] = sc_path
            else:
                # ONE read: classified, the working copy and (if rejected) quarantined.
                try:
                    snap, text = _snapshot_text(sc_path)
                    is_eng, verdict_info = classify_candidate(text)
                    if not is_eng:
                        content_rejected += 1
                        log(f"content-gate REJECT sidecar {sc_path}: named English, text "
                            f"scored {verdict_info.get('verdict')} "
                            f"(reason={verdict_info.get('reason')}) -- quarantining")
                        _quarantine_sidecar(sc_path, snap, verdict_info,
                                            "sidecar candidate rejected during consolidation")
                        continue
                    local = _sidecar_as_srt(sc_path, workdir, data=text.encode("utf-8"))
                    sc_ident = snap.identity
                except _Unclassifiable as exc:
                    log(f"content-gate SKIP sidecar {sc_path}: cannot be classified ({exc}); "
                        f"left in place, not a candidate")
                    extraction_failed += 1
                    continue
                except OSError:
                    extraction_failed += 1
                    continue
                if local is None:
                    extraction_failed += 1
                    continue
            lc = count_subtitle_lines(local)
            sidecar_score = 1100 + min(lc, 2000) // 5 + 50
            scored.append(_SubCandidate(sidecar_score, local, "sidecar",
                                        sc_generated, is_sdh))
            if sc_ident is not None:
                consumed[sc_path] = sc_ident

        proven_foreign = frozenset(rejected_embedded)

        if not scored and upgrade is not None:
            return {"path": path_str, "status": "SKIP",
                    "detail": "upgrade: no verified subtitle could be used, "
                              "kept the generated subtitle"}

        fetched_rejected: dict | None = None
        fetched_coverage_pct: int | None = None
        fetch_read_failed = False
        if not scored and eng_image:
            fetched = fetch_via_subliminal(path, workdir)
            if fetched is not None:
                try:
                    fetched_ok, fetched_info = _gate_local_file(fetched)
                except OSError as exc:
                    fetch_read_failed = True
                    extraction_failed += 1
                    log(f"WARNING: cannot read the subliminal download for {path}: "
                        f"{type(exc).__name__}: {exc}")
                else:
                    # A last cue well before the end means a partial subtitle (disc 1 of 2).
                    # Skipped, never a rejection, if duration or cues are unknown.
                    short: tuple[float, float] | None = None   # (last cue start, duration)
                    if fetched_ok:
                        duration = parse_duration((info.get("format") or {}).get("duration"))
                        last_cue = _last_cue_start(fetched)
                        if duration is None or last_cue is None:
                            log(f"coverage check SKIPPED for subliminal download for {path}: "
                                f"{'duration unknown' if duration is None else 'no timed cues'}")
                        elif last_cue < SUBLIMINAL_MIN_COVERAGE * duration:
                            short = (last_cue, duration)
                    if short is not None:
                        fetched_coverage_pct = int(100 * short[0] / short[1])
                        content_rejected += 1
                        log(f"coverage REJECT subliminal download for {path}: last cue at "
                            f"{_hms(short[0])} of {_hms(short[1])} ({fetched_coverage_pct}%)")
                    elif fetched_ok:
                        lc = count_subtitle_lines(fetched)
                        # Below a sidecar (1100): less vetted.
                        scored.append(_SubCandidate(1000 + min(lc, 2000) // 5, fetched,
                                                    "subliminal", False, False))
                    else:
                        fetched_rejected = fetched_info
                        content_rejected += 1
                        log(f"content-gate REJECT subliminal download for {path}: "
                            f"text scored {fetched_info.get('verdict')} "
                            f"(reason={fetched_info.get('reason')})")

        if not scored:
            # A tooling failure stays visible as FAIL; NEEDS_BAZARR only when every
            # non-accepted candidate was content-rejected (or the fetch came back empty).
            if extraction_failed:
                why = []
                if unclassified_note:
                    why.append(unclassified_note)
                if fetch_read_failed:
                    why.append("subliminal download unreadable")
                if content_rejected:
                    why.append(f"{content_rejected} rejected by content gate")
                return _cleanup_no_main_english(
                    path, path_str, "FAIL",
                    "all extracts failed" + (f" ({'; '.join(why)})" if why else ""),
                    sub_streams, audio_streams, video_streams_all,
                    forced_text, eng_image, dry_run, proven_foreign,
                )
            if eng_image:
                return _cleanup_no_main_english(
                    path, path_str, "NEEDS_BAZARR",
                    f"only image subs (subliminal result covers only {fetched_coverage_pct}% of the film)"
                    if fetched_coverage_pct is not None else
                    "only image subs (subliminal fetch failed)"
                    if fetched_rejected is None else
                    "only image subs (subliminal result rejected by content gate)",
                    sub_streams, audio_streams, video_streams_all,
                    forced_text, eng_image, dry_run, proven_foreign,
                )
            if content_rejected:
                return _cleanup_no_main_english(
                    path, path_str, "NEEDS_BAZARR",
                    "no English candidates (rejected by content gate)",
                    sub_streams, audio_streams, video_streams_all,
                    forced_text, eng_image, dry_run, proven_foreign,
                )
            return _cleanup_no_main_english(
                path, path_str, "FAIL", "all extracts failed",
                sub_streams, audio_streams, video_streams_all,
                forced_text, eng_image, dry_run, proven_foreign,
            )

        scored.sort(key=lambda c: c.rank, reverse=True)
        chosen = scored[0]
        chosen_srt, chosen_generated = chosen.path, chosen.generated

        forced_srt_final = None
        if forced_gate is not None and forced_gate.state == "accepted" and forced_gate.text is not None:
            ftmp = _workfile(workdir, "forced", ".srt")
            ftmp.write_text(forced_gate.text, encoding="utf-8")
            forced_srt_final = ftmp

        # No usable embedded forced track: embed an accepted forced SIDECAR rather than let
        # the full path discard it. Only a sidecar embedded here counts as consumed. Never
        # in an upgrade (it keeps the source's own forced tracks).
        if forced_srt_final is None and accepted_forced and upgrade is None:
            # Until one converts; only one is embedded.
            for fside in sorted(accepted_forced):
                fsnap = accepted_forced[fside]
                try:
                    fsrt = _sidecar_as_srt(
                        fside, workdir, data=decode_subtitle_bytes(fsnap.data).encode("utf-8"))
                    if fsrt is None:
                        log(f"WARNING: forced sidecar {fside} could not be converted to SRT; "
                            f"left in place, trying the next one")
                        continue
                    fsrt.write_text(clean_srt_text(fsrt.read_text(encoding="utf-8", errors="replace")),
                                    encoding="utf-8")
                    forced_srt_final = fsrt
                    consumed[fside] = fsnap.identity
                    break
                except Exception as exc:
                    log(f"WARNING: forced sidecar {fside} could not be embedded: "
                        f"{type(exc).__name__}: {exc}; left in place, trying the next one")

        try:
            txt = chosen_srt.read_text(encoding="utf-8", errors="replace")
            cleaned = clean_srt_text(txt)
            chosen_srt.write_text(cleaned, encoding="utf-8")
        except Exception as e:
            return {"path": path_str, "status": "FAIL", "detail": f"clean failed: {e}"}

        synced_srt = _workfile(workdir, "synced", ".srt")
        sync_ok = run_ffsubsync(path, chosen_srt, synced_srt)
        final_srt = synced_srt if sync_ok else chosen_srt

        # `original_language_for` (Radarr/Sonarr) detects foreign-original films without
        # trusting the Matroska `default` flag, which release groups routinely set on a DUB.
        # multi_keep is True only for live-action foreign-original films; the anime/Korean
        # keyword paths keep a single best track.
        upgrade_flags: dict[int, dict[str, bool]] | None = None
        kept_forced: list[dict] = []
        if upgrade is None:
            allowed_langs, primary_lang, multi_keep = get_audio_lang_pref(
                path, audio_streams, original_language=original_language_for(path))
            keep_audio_idx = select_keep_audio(audio_streams, allowed_langs, primary_lang, multi_keep)
        else:
            # An upgrade changes only the subtitle: audio and forced English tracks stay
            # as they are, with their source flags (via remux's cleanup_flags).
            primary_lang = "unchanged"
            keep_audio_idx = [s["index"] for s in audio_streams]
            kept_forced = _forced_english_tracks(sub_streams)
            upgrade_flags = _upgrade_track_flags(video_streams_all, audio_streams, kept_forced)
        dropped_audio = max(0, len(audio_streams) - len(keep_audio_idx))

        out = path.with_suffix(path.suffix + f".consol.{os.getpid()}.tmp.mkv")
        out.unlink(missing_ok=True)
        if upgrade is not None:
            # ffsubsync ran since the gate probed: remux only if unchanged.
            changed = _upgrade_video_changed(path, path_str, upgrade, "before the remux")
            if changed:
                return changed
        # An unreadable forced track is not proven foreign: unless a readable one replaces
        # it, it is stream-copied unchanged.
        kept_source_subs: dict[int, dict[str, bool]] | None = None
        if (upgrade is None and unreadable_forced_stream is not None
                and forced_srt_final is None):
            disp = unreadable_forced_stream.get("disposition") or {}
            kept_source_subs = {unreadable_forced_stream["index"]: {
                "default": bool(disp.get("default")), "forced": bool(disp.get("forced"))}}
            log(f"keeping the unreadable embedded forced track "
                f"{unreadable_forced_stream['index']} of {path} unchanged")
        ok, detail = remux(
            path, out, keep_video_idx, keep_audio_idx, final_srt, forced_srt_final,
            keep_subtitle_indices=[s["index"] for s in kept_forced] or None,
            cleanup_flags=upgrade_flags,
            eng_track_name=ENG_GENERATED_TRACK_NAME if chosen_generated else ENG_TRACK_NAME,
            **({"kept_source_subs": kept_source_subs} if kept_source_subs else {}))
        if not ok:
            out.unlink(missing_ok=True)
            return {"path": path_str, "status": "FAIL", "detail": f"remux: {detail}"}
        if kept_source_subs is not None:
            problem = _verify_kept_source_subs(
                out, len(keep_video_idx), len(keep_audio_idx), unreadable_forced_stream,
                kept_source_subs)
            if problem:
                out.unlink(missing_ok=True)
                return {"path": path_str, "status": "FAIL",
                        "detail": f"forced track verify: {problem}"}
        if upgrade is not None:
            assert upgrade_flags is not None
            problem = _verify_upgrade_output(
                out, video_streams_all, audio_streams, kept_forced, upgrade_flags)
            if problem:
                out.unlink(missing_ok=True)
                return {"path": path_str, "status": "FAIL",
                        "detail": f"upgrade verify: {problem}"}

        # Refuse to overwrite a sibling .mkv ("Movie.mp4" must not destroy "Movie.mkv").
        replacement = path.with_suffix(".mkv")
        if replacement != path and replacement.exists():
            out.unlink(missing_ok=True)
            return {"path": path_str, "status": "FAIL",
                    "detail": f"collision: sibling .mkv exists for {path.name}"}
        if upgrade is not None:
            changed = _upgrade_video_changed(path, path_str, upgrade, "before the swap", out)
            if changed:
                return changed
        # An upgrade never normalizes: its audio and video must stay the verified source.
        early_result, tag_written = _expose_tag_normalize(
            out, path, replacement, path_str, normalize=upgrade is None)
        if early_result is not None:
            return early_result

        # Delete ONLY sidecars read and accepted as English (`consumed`), and only while the
        # file still has the identity it was read with (one replaced by Bazarr or
        # sub-fallback.py meanwhile stays). An upgrade deletes only the verified sidecar it
        # muxed, and only if identity AND content hash match.
        dropped_sidecars = 0
        if upgrade is None:
            for sc_path, sc_ident in consumed.items():
                if sc_path == replacement:
                    continue
                now_ident = file_identity(sc_path)
                if now_ident is None:
                    continue
                if now_ident != sc_ident:
                    log(f"WARNING: sidecar {sc_path} changed since it was read; left in place")
                    continue
                try:
                    sc_path.unlink()
                    dropped_sidecars += 1
                except OSError:
                    pass
            for sc_path, _is_forced, _is_sdh in sidecars:
                if sc_path not in consumed and sc_path.exists():
                    log(f"sidecar {sc_path} was not consumed by this run; left in place")
        else:
            muxed_from = consumed_from.get(chosen_srt)
            verified = upgrade.sidecars.get(muxed_from) if muxed_from is not None else None
            if muxed_from is not None and verified is not None and muxed_from.exists():
                # Re-read and compare sha256: a same-length rewrite with the mtime restored
                # keeps size and mtime. Unreadable or oversized counts as changed.
                now = _snapshot_sidecar(muxed_from)
                if (now is None or now.identity != verified.identity
                        or now.digest != verified.digest):
                    log(f"UPGRADE: left {muxed_from.name} in place (changed after the check)")
                else:
                    # ACCEPTED RACE: a replacement landing between the re-check and this
                    # unlink is lost. No atomic compare-and-unlink exists (renameat2
                    # RENAME_NOREPLACE gives EINVAL on this mergerfs pool); microseconds,
                    # the same window `_quarantine_sidecar` accepts.
                    try:
                        muxed_from.unlink()
                        dropped_sidecars += 1
                    except OSError:
                        pass

        new_key = f"{replacement.resolve()}"
        new_size, new_mtime = file_key(replacement)
        update_state_entry(STATE_FILE, new_key, {"size": new_size, "mtime": new_mtime,
                                                 "v": PIPELINE_VERSION,
                                                 "status": "fixed", "synced": sync_ok,
                                                 "tag_written": tag_written,
                                                 **({"upgraded": True} if upgrade else {})})
        _clear_needs_english(replacement, dry_run)

        dropped_embedded = max(0, len(sub_streams) - (
            1 + (len(kept_forced) if upgrade else (1 if forced_srt_final else 0))
            + (1 if kept_source_subs else 0)))
        return {"path": str(replacement), "status": "FIXED",
                "detail": (f"{'UPGRADE generated->real ' if upgrade else ''}"
                           f"synced={sync_ok} dropped_subs={dropped_embedded} "
                           f"dropped_audio={dropped_audio} "
                           f"dropped_video={dropped_video} "
                           f"dropped_sidecars={dropped_sidecars} "
                           f"audio_lang={primary_lang}")}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def walk(root: Path):
    # Prune `.normalize-tmp`: its scratch renders were treated as media and got lock files.
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != NORMALIZE_TMP_DIR]
        for f in files:
            fl = f.lower()
            if not fl.endswith((".mkv", ".mp4")):
                continue
            if fl.endswith(".tmp.mkv"):
                continue
            yield Path(r) / f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", help="file or directory")
    ap.add_argument("--scan", help="walk directory")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="cap files in scan mode (0 = no cap)")
    ap.add_argument("--sweep-only", metavar="DIR",
                    help="sweep orphan tmp files / workdirs under DIR and exit")
    ap.add_argument("--no-sweep", action="store_true",
                    help="skip the orphan sweep that runs at the start of --scan")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --sweep-only or --scan: report what would be deleted, delete nothing")
    args = ap.parse_args()

    if args.sweep_only:
        log(f"sweep-only start: {args.sweep_only} (dry_run={args.dry_run})")
        res = sweep_orphans(Path(args.sweep_only), dry_run=args.dry_run)
        _log_sweep_result(res, dry=args.dry_run)
        return 0

    if args.scan:
        if not args.no_sweep:
            log(f"sweep before scan: {args.scan} (dry_run={args.dry_run})")
            res = sweep_orphans(Path(args.scan), dry_run=args.dry_run)
            _log_sweep_result(res, dry=args.dry_run)
        files = list(walk(Path(args.scan)))
        if args.limit:
            files = files[: args.limit]
        log(f"scan start: {len(files)} files, jobs={args.jobs}, dry_run={args.dry_run}")
        if args.jobs <= 1:
            for f in files:
                r = process_file(str(f), dry_run=args.dry_run)
                log(f"{r['status']}: {Path(r['path']).name} | {r.get('detail','')}")
        else:
            with ProcessPoolExecutor(max_workers=args.jobs) as ex:
                futs = {ex.submit(process_file, str(f), args.dry_run): f for f in files}
                for fut in as_completed(futs):
                    r = fut.result()
                    log(f"{r['status']}: {Path(r['path']).name} | {r.get('detail','')}")
        log("scan complete")
        return 0

    if args.target:
        r = process_file(args.target, dry_run=args.dry_run)
        log(f"{r['status']}: {Path(r['path']).name} | {r.get('detail','')}")
        return 0 if r["status"] in ("FIXED", "SKIP", "NEEDS_BAZARR") else 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
