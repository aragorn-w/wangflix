#!/usr/bin/env python3
"""
EBU R128 audio loudness normalization for the media library.

Per file:
  1. ffprobe to read existing tags + audio layout. Skip if NORMALIZED_AUDIO=v1.
  2. Pass 1: ffmpeg loudnorm analysis (json) on the primary audio stream.
  3. Pass 2: ffmpeg re-encode primary audio with loudnorm linear=true
     (linear gain when peaks allow, dynamic only when clipping would occur),
     stream-copy video + subs, mkv intermediate.
  4. mkvmerge re-mux for a real cue index (same doctrine as consolidate-subs.py).
  5. mkvpropedit: set NORMALIZED_AUDIO=v1, preserve any existing tags
     (including CONSOLIDATED_SUBS=v2).
  6. Atomic rename over the original.

Target: I=-23 LUFS, LRA=7, TP=-2 dBFS (EBU R128 broadcast).

Usage:
  normalize-audio.py <file>
  normalize-audio.py --scan <dir>
  normalize-audio.py --scan <dir> --jobs 2
  normalize-audio.py --measure-only <file-or-dir>   # report-only, no edits
  normalize-audio.py --dry-run <file>               # measure + plan, no edits

Exit codes: 0 ok/skip, 1 hard fail.
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import nullcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from media_stack.paths import VAR_LOG, ensure_var_dirs  # noqa: E402
ensure_var_dirs()  # we're a writer (log file); guarantee dirs exist
from media_stack.probe import (  # noqa: E402
    ffprobe_strict as ffprobe, already_normalized, primary_audio_stream,
)
from media_stack.loudness import (  # noqa: E402
    AAC_BITRATE, DEFAULT_AAC_BITRATE, TARGET_I, TARGET_LRA, TARGET_TP,
    fast_measure_ebur128, measure_loudness, render_normalized,
)
from media_stack.mux import mkvmerge_remux_simple as mkvmerge_remux  # noqa: E402
from media_stack.tags import set_normalized_tag  # noqa: E402
from media_stack.locking import acquire_file_lock  # noqa: E402

LOG_FILE = VAR_LOG / "normalize-audio.log"
PIPELINE_VERSION = 1
TAG_NAME = "NORMALIZED_AUDIO"

VIDEO_EXTS = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm")


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} {msg}\n"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass
    sys.stderr.write(line)


def measure_only(path: Path, fast: bool = True) -> dict:
    info = ffprobe(path)
    a = primary_audio_stream(info)
    if not a:
        return {"path": str(path), "status": "NO_AUDIO"}
    audio_streams = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    audio_idx = audio_streams.index(a)
    measured = fast_measure_ebur128(path, audio_idx) if fast \
        else measure_loudness(path, audio_idx)
    out = {
        "path": str(path),
        "status": "OK",
        "input_i": float(measured["input_i"]),
        "input_tp": float(measured["input_tp"]),
        "input_lra": float(measured["input_lra"]),
        "channels": a.get("channels"),
        "codec": a.get("codec_name"),
        "duration": float((info.get("format") or {}).get("duration") or 0),
    }
    if not fast:
        out["target_offset"] = float(measured["target_offset"])
    return out


def process_file(path_str: str, dry_run: bool = False) -> dict:
    try:
        return _process_file_inner(path_str, dry_run=dry_run)
    except Exception as e:
        return {"path": path_str, "status": "FAIL",
                "detail": f"{type(e).__name__}: {str(e)[:160]}"}


def _process_file_inner(path_str: str, dry_run: bool) -> dict:
    src = Path(path_str)
    if not src.is_file():
        return {"path": path_str, "status": "FAIL", "detail": "not a file"}
    if src.suffix.lower() not in VIDEO_EXTS:
        return {"path": path_str, "status": "SKIP", "detail": "non-video ext"}

    # Shared per-file lock with consolidate-subs.py via media_stack.locking.
    # Inheritance protocol (closes codex round-module-split #1, which caught
    # that the bare `NORMALIZE_INHERIT_LOCK=1` env var would bypass the lock
    # for ANY path, including ones the parent isn't actually holding):
    #
    #   - Parent (consolidate-subs._normalize_audio_inline) sets
    #     NORMALIZE_INHERIT_LOCK_PATH=<absolute parent-locked-path> when
    #     invoking us as a subprocess.
    #   - We pass that as `inherit_from` to acquire_file_lock, which only
    #     trusts the inheritance when both paths resolve to the same
    #     absolute filesystem path (codex round-4 module-split #2:
    #     basename-only was unsafe — two unrelated files sharing a
    #     basename in different directories would falsely match).
    #   - Without the env var, we acquire a fresh lock or skip on conflict.
    inherit_path_str = os.environ.get("NORMALIZE_INHERIT_LOCK_PATH")
    inherit_from = Path(inherit_path_str) if inherit_path_str else None

    with acquire_file_lock(src, inherit_from=inherit_from) as acquired:
        if not acquired:
            return {"path": path_str, "status": "SKIP",
                    "detail": "locked by other pipeline (consolidate or sibling normalize)"}
        return _process_file_inner_locked(src, path_str, dry_run)


def _replace_and_tag(src: Path, remux_out: Path, final_path: Path,
                     backup_path: Path) -> bool:
    """Swap the normalized `remux_out` into `final_path` (backing up `src`
    to `backup_path`), then stamp the NORMALIZED_AUDIO idempotency tag, all
    under the DESTINATION .mkv's lock.

    Returns True on full success.  Tagging happens BEFORE the backup is
    dropped: the driver's coverage probe keys on this tag, so a
    replaced-but-untagged file would be re-normalized (lossy generation
    loss) every sweep.  If the tag write fails (False OR an exception), the
    swap is rolled back (original restored; orphan output removed) and False
    is returned so the caller reports TAG_FAIL and the driver retries.

    Holds the destination .mkv's lock across the whole swap/tag/rollback:
    on a suffix-changing convert (.mp4 → .mkv) that lock differs from
    `src`'s (which the caller already holds), so without it a watcher/scan
    could grab the fresh .mkv mid-swap.  No suffix change → the .mkv IS
    `src` (already locked) → nullcontext.  Raises RuntimeError if the
    destination is already locked by another pipeline, or if a sibling .mkv
    appeared under the lock (re-checked TOCTOU); raises (after restoring
    `src`) if the rename itself fails.
    """
    from media_stack.locking import acquire_file_lock
    dest_lock = (acquire_file_lock(final_path) if final_path != src
                 else nullcontext(True))
    with dest_lock as dest_acquired:
        if final_path != src:
            if not dest_acquired:
                raise RuntimeError(
                    f"destination {final_path.name} locked by another pipeline")
            # Re-check the collision UNDER the lock — the pre-lock check in
            # _process_file_inner_locked could race a concurrent worker that
            # created the sibling .mkv in between.
            if final_path.exists():
                raise RuntimeError(f"collision: sibling .mkv appeared for {src.name}")

        os.replace(src, backup_path)
        try:
            os.replace(remux_out, final_path)
        except Exception:
            # Restore on a failed swap (disk full, perms, …) then re-raise.
            os.replace(backup_path, src)
            raise

        # Treat a tag-write EXCEPTION (mkvextract timeout, OSError, XML write
        # failure) exactly like a False return.  The swap is already done, so
        # an uncaught raise here would escape and leave a replaced-but-
        # UNTAGGED file with no rollback — the generation-loss bug we guard.
        try:
            tagged = set_normalized_tag(final_path, PIPELINE_VERSION)
        except Exception as exc:
            tagged = False
            log(f"ERROR: NORMALIZED_AUDIO tag write raised for {final_path}: "
                f"{type(exc).__name__}: {exc}")
        if not tagged:
            # Roll back: restore the original (os.replace overwrites the new
            # file in place when final_path == src), then drop the orphan
            # output that lingers when the suffix changed (.mp4 → .mkv).
            os.replace(backup_path, src)
            if final_path != src:
                try:
                    final_path.unlink()
                except FileNotFoundError:
                    pass
            return False

        # Tag succeeded — now it's safe to drop the backup.  (If src was .mp4
        # and final .mkv, the .pre-norm.bak keeps the old extension; harmless.)
        try:
            backup_path.unlink()
        except FileNotFoundError:
            pass
        return True


def _process_file_inner_locked(src: Path, path_str: str, dry_run: bool) -> dict:

    info = ffprobe(src)
    if already_normalized(info):
        return {"path": path_str, "status": "SKIP", "detail": "already v1"}

    a = primary_audio_stream(info)
    if not a:
        return {"path": path_str, "status": "SKIP", "detail": "no audio"}

    audio_streams = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    audio_idx = audio_streams.index(a)
    channels = int(a.get("channels") or 2)
    codec = a.get("codec_name", "?")
    channel_layout = a.get("channel_layout", "") or ""

    t0 = time.time()
    measured = measure_loudness(src, audio_idx)
    in_i  = float(measured["input_i"])
    in_tp = float(measured["input_tp"])
    in_lra = float(measured["input_lra"])
    offset = float(measured["target_offset"])

    if dry_run:
        return {
            "path": path_str, "status": "DRY",
            "in_i": in_i, "in_tp": in_tp, "in_lra": in_lra, "offset": offset,
            "channels": channels, "codec": codec,
            "measure_s": round(time.time() - t0, 1),
        }

    # Already on target? Within ±0.3 LU and peak headroom OK = skip the
    # re-encode entirely, just stamp the tag.
    #
    # mkvpropedit is Matroska-only — for non-MKV inputs (.mp4, .m4v,
    # .avi, …) the tag stamp can't write a NORMALIZED_AUDIO Matroska
    # global tag, so the "already on target" file would loop as a
    # permanent TAG_FAIL on every sweep (codex round-module-split-2 #5).
    # For non-MKV files we still need a real pass2 + mkvmerge so the
    # output container holds the tag; bypass the tag-only shortcut.
    if (src.suffix.lower() == ".mkv"
            and abs(in_i - TARGET_I) < 0.3
            and in_tp < TARGET_TP - 0.5):
        ok = set_normalized_tag(src, PIPELINE_VERSION)
        return {"path": path_str,
                "status": "TAG_ONLY" if ok else "TAG_FAIL",
                "in_i": in_i, "in_tp": in_tp, "channels": channels,
                "codec": codec}

    # Stage temp files next to the source — disk3 has 6T free, /tmp shares
    # the root LV (only 692G).
    workdir = src.parent / ".normalize-tmp"
    workdir.mkdir(exist_ok=True)
    # Mangle name into the temp dir to avoid colliding with siblings.
    stem_safe = re.sub(r"[^\w.-]+", "_", src.stem)[:120]
    pass2_out = workdir / f".{stem_safe}.{os.getpid()}.pass2.mkv"
    remux_out = workdir / f".{stem_safe}.{os.getpid()}.remux.mkv"

    try:
        render_normalized(src, pass2_out, audio_idx, codec, channels, measured, channel_layout)
        mkvmerge_remux(pass2_out, remux_out)
        try:
            pass2_out.unlink()
        except FileNotFoundError:
            pass

        # Atomic-ish replace — same filesystem so rename is atomic, but we
        # have to migrate the destination filename (might have .mkv even if
        # source was .mp4; we always emit .mkv).
        final_path = src.with_suffix(".mkv")
        backup_path = src.with_suffix(src.suffix + ".pre-norm.bak")

        # Collision guard: if src is non-MKV and a sibling .mkv already exists,
        # refuse rather than silently destroy it (it's a different file).
        if final_path != src and final_path.exists():
            try:
                remux_out.unlink()
            except FileNotFoundError:
                pass
            raise RuntimeError(
                f"collision: sibling .mkv exists for {src.name}; refusing to overwrite"
            )

        # Verify the renderer didn't somehow drop a sub track silently.
        new_info = ffprobe(remux_out)
        old_subs = sum(1 for s in info["streams"] if s.get("codec_type") == "subtitle")
        new_subs = sum(1 for s in new_info["streams"] if s.get("codec_type") == "subtitle")
        if new_subs < old_subs:
            raise RuntimeError(f"sub count regressed: {old_subs}->{new_subs}")
        old_video = sum(1 for s in info["streams"] if s.get("codec_type") == "video")
        new_video = sum(1 for s in new_info["streams"] if s.get("codec_type") == "video")
        if new_video < old_video:
            raise RuntimeError(f"video count regressed: {old_video}->{new_video}")

        # Atomic swap + idempotency tag, with rollback if tagging fails
        # (see _replace_and_tag).  A failed tag must NOT report FIXED — the
        # driver's coverage probe keys on the tag, so an untagged file would
        # be re-normalized (lossy) every sweep.
        if not _replace_and_tag(src, remux_out, final_path, backup_path):
            return {"path": path_str, "status": "TAG_FAIL",
                    "detail": "NORMALIZED_AUDIO tag write failed; restored original"}

    finally:
        for p in (pass2_out, remux_out):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        try:
            workdir.rmdir()
        except OSError:
            pass  # not empty, another worker still using it

    elapsed = round(time.time() - t0, 1)
    return {"path": path_str, "status": "FIXED",
            "in_i": round(in_i, 2), "in_tp": round(in_tp, 2),
            "in_lra": round(in_lra, 2), "offset": round(offset, 2),
            "channels": channels, "codec": codec, "elapsed_s": elapsed}


def walk_videos(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            # Skip our own temp-dir crumbs.
            if any(part == ".normalize-tmp" for part in p.parts):
                continue
            # Skip staging temp files from consolidate-subs.py / future
            # helpers.  Without this guard a leftover
            # `*.consol.<pid>.tmp.mkv` or any future `*.tmp.mkv` would be
            # processed as if it were a real media file (codex round-13
            # finding #4).
            if p.name.lower().endswith(".tmp.mkv"):
                continue
            yield p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", help="single file (default mode)")
    ap.add_argument("--scan", help="walk a directory")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--measure-only", help="report-only loudness scan over a file or dir")
    ap.add_argument("--dry-run", action="store_true",
                    help="measure + plan, don't modify files")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap number of files (debug)")
    args = ap.parse_args()

    if args.measure_only:
        root = Path(args.measure_only)
        files = [root] if root.is_file() else list(walk_videos(root))
        if args.limit:
            files = files[:args.limit]
        results = []
        if args.jobs > 1:
            with ProcessPoolExecutor(max_workers=args.jobs) as ex:
                futs = {ex.submit(measure_only, f): f for f in files}
                for fut in as_completed(futs):
                    try:
                        results.append(fut.result())
                    except Exception as e:
                        results.append({"path": str(futs[fut]), "status": "FAIL",
                                        "detail": str(e)[:160]})
        else:
            for f in files:
                try:
                    results.append(measure_only(f))
                except Exception as e:
                    results.append({"path": str(f), "status": "FAIL",
                                    "detail": str(e)[:160]})
        json.dump(results, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    if args.scan:
        files = list(walk_videos(Path(args.scan)))
        if args.limit:
            files = files[:args.limit]
        log(f"scan start: {len(files)} files, jobs={args.jobs}, dry_run={args.dry_run}")
        n_fixed = n_skip = n_fail = n_tag = 0
        if args.jobs > 1:
            with ProcessPoolExecutor(max_workers=args.jobs) as ex:
                futs = {ex.submit(process_file, str(f), args.dry_run): f
                        for f in files}
                for fut in as_completed(futs):
                    r = fut.result()
                    log(f"{r.get('status'):8s} {r.get('path')} -- "
                        f"{ {k: v for k, v in r.items() if k not in ('path','status')} }")
                    s = r.get("status")
                    if s == "FIXED": n_fixed += 1
                    elif s == "TAG_ONLY": n_tag += 1
                    elif s in ("SKIP", "DRY"): n_skip += 1
                    else: n_fail += 1
        else:
            for f in files:
                r = process_file(str(f), args.dry_run)
                log(f"{r.get('status'):8s} {r.get('path')} -- "
                    f"{ {k: v for k, v in r.items() if k not in ('path','status')} }")
                s = r.get("status")
                if s == "FIXED": n_fixed += 1
                elif s == "TAG_ONLY": n_tag += 1
                elif s in ("SKIP", "DRY"): n_skip += 1
                else: n_fail += 1
        log(f"scan end: fixed={n_fixed} tag_only={n_tag} skip={n_skip} fail={n_fail}")
        sys.exit(0 if n_fail == 0 else 1)

    if args.path:
        r = process_file(args.path, args.dry_run)
        log(f"{r.get('status'):8s} {r.get('path')} -- "
            f"{ {k: v for k, v in r.items() if k not in ('path','status')} }")
        sys.exit(0 if r.get("status") in ("FIXED", "SKIP", "TAG_ONLY", "DRY") else 1)

    ap.print_help()
    sys.exit(2)


if __name__ == "__main__":
    main()
