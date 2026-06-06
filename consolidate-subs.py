#!/usr/bin/env python3
r"""
Consolidate English subtitles in a media file:
- Pick highest-scoring English text track (drop redundants, drop image-based when text exists)
- Run ffsubsync against the audio to align timing
- Regex-clean artifacts (\h, ASS override tags, stray HTML, etc.)
- Remux as the only English subtitle (default=1), preserve a real Forced track if present
- Idempotent via state file keyed on (path, size, mtime)

Usage:
  consolidate-subs.py <file>            # single file (Sonarr/Radarr custom script mode)
  consolidate-subs.py --scan <dir>      # walk directory
  consolidate-subs.py --scan <dir> --jobs 4  # parallel walk

Exit codes: 0 ok/skip, 1 hard fail.
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import nullcontext
from pathlib import Path

# Host identity + extracted helpers from the media_stack package.  All
# the pure-helper code (probing, audio/sub policy, tag manipulation,
# state-file locking, mkvmerge orchestration, orphan sweeping) lives
# there; this script keeps only the per-file workflow + CLI dispatch.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from media_stack.paths import VAR_LOG, VAR_STATE, ensure_var_dirs  # noqa: E402
ensure_var_dirs()  # we're a writer (log + state); guarantee dirs exist
from media_stack.config import (  # noqa: E402
    ASS_OVERRIDE_RE, CODEC_PREF, COMMENT_TITLE_RE, CONSOLSUB_DIR_RE,
    DUAL_AUDIO_KEYWORDS, ENG_LANGS, ENG_SIDECAR_SUFFIXES,
    HTML_FONT_RE, HTML_OTHER_RE, IMAGE_CODECS, JAPANESE_KEYWORDS,
    KOREAN_KEYWORDS, LITERAL_NH_RE, MULTI_BLANK_RE, PIPELINE_VERSION,
    SIDECAR_EXTS, STILL_IMAGE_VIDEO_CODECS, SWEEP_MIN_AGE_S,
    TEXT_CODECS, TMP_MKV_RE, TRAIL_WS_RE,
)
from media_stack.lang import (  # noqa: E402
    ENG_AUDIO_LANGS, JPN_AUDIO_LANGS, KOR_AUDIO_LANGS, canonical_lang,
)
from media_stack.probe import (  # noqa: E402
    already_processed, file_key, probe,
)
from media_stack.state import (  # noqa: E402
    load_state, save_state, update_state_entry,
)
from media_stack.audio import (  # noqa: E402
    get_audio_lang_pref, is_commentary_audio, is_dub_audio, select_keep_audio,
)
from media_stack.subtitles import (  # noqa: E402
    ass_to_srt, clean_srt_text, count_subtitle_lines, extract_track,
    fetch_via_subliminal, find_sidecar_subs, is_forced_track, is_sdh_track,
    normalize_subtitle_caps, run_ffsubsync, score_image_track,
    score_text_track,
)
from media_stack.mux import remux  # noqa: E402
from media_stack.tags import set_consolidated_tag  # noqa: E402
from media_stack.sweeps import sweep_orphans as _sweep_orphans  # noqa: E402

STATE_FILE = VAR_STATE / "consolidate-subs.state.json"
LOG_FILE   = VAR_LOG / "consolidate-subs.log"


def sweep_orphans(root: Path, dry_run: bool = False) -> dict:
    """Thin wrapper — delegate to media_stack.sweeps with this
    script's basename as the live-PID identifier."""
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



def process_file(path_str: str, dry_run: bool = False) -> dict:
    """Returns a dict with status: SKIP/FIXED/NEEDS_BAZARR/FAIL and detail.
    Wrapped to always return a dict — no exception escapes.

    When `dry_run=True`, the file is analyzed but never mutated: no remux,
    no tag writes, no sidecar deletion, no state-file updates, no audio
    normalization. Files that would normally be FIXED return SKIP with
    detail prefix "dry-run:".
    """
    try:
        return _process_file_inner(path_str, dry_run=dry_run)
    except Exception as e:
        return {"path": path_str, "status": "FAIL",
                "detail": f"unhandled: {type(e).__name__}: {str(e)[:120]}"}


def _process_file_inner(path_str: str, dry_run: bool = False) -> dict:
    from media_stack.locking import acquire_file_lock
    path = Path(path_str)
    if not path.is_file():
        return {"path": path_str, "status": "FAIL", "detail": "not a file"}
    if TMP_MKV_RE.search(path.name):
        return {"path": path_str, "status": "SKIP",
                "detail": "tmp file (orphan candidate; run --sweep-only to clean)"}

    # Per-file exclusive lock — shared with normalize-audio.py via the
    # centralized `media_stack.locking.acquire_file_lock` helper so the
    # sweep and the watcher's consolidate pipeline can't both mutate the
    # same MKV simultaneously.
    with acquire_file_lock(path) as acquired:
        if not acquired:
            return {"path": path_str, "status": "SKIP",
                    "detail": "locked by other runner"}
        return _process_locked(path, path_str, dry_run=dry_run)


def _normalize_audio_inline(path: Path, locked_path: Path | None = None) -> None:
    """Hand `path` to normalize-audio.py as an out-of-process subprocess.

    Subprocess isolation is deliberate: normalize-audio uses its own
    ProcessPoolExecutor + ffmpeg+mkvmerge pipeline, and mixing those into
    the in-process consolidator caused fcntl-lock and state-file confusion.

    Idempotent: normalize-audio short-circuits on NORMALIZED_AUDIO=v1, so a
    file that's already normalized returns in seconds.

    Lock-inheritance protocol: pass the parent's locked path via
    NORMALIZE_INHERIT_LOCK_PATH — the subprocess feeds it into
    `media_stack.locking.acquire_file_lock(src, inherit_from=...)`
    which only honours the inheritance when both paths resolve to
    the same absolute filesystem path (not basename-only).  This closes
    the hazard of a bare "trust me" env flag that would bypass locking
    even for the wrong path (a leaked NORMALIZE_INHERIT_LOCK=1 in
    someone's shell, a misrouted cron, .mp4→.mkv suffix change, or
    a same-basename file in a different directory — see
    locking.py docstring).

    Errors here MUST NOT propagate. Consolidation is already committed at
    this point; audio normalization is a best-effort follow-on. The 2-hour
    cap covers a 30GB UHD movie comfortably."""
    script = Path(__file__).parent / "normalize-audio.py"
    env = {**os.environ}
    if locked_path is not None:
        env["NORMALIZE_INHERIT_LOCK_PATH"] = str(locked_path)
    try:
        r = subprocess.run(
            [sys.executable, str(script), str(path)],
            capture_output=True, text=True, timeout=7200, env=env,
        )
        # Detect the "skipped, not normalized" status emitted by
        # normalize-audio.py.  Plain rc=0 isn't enough: the script's
        # process_file() returns SKIP with rc=0 for "already v1" (success
        # we DO want to log green) AND for "locked" (a no-op we MUST NOT
        # confuse with success).  Parse the trailing line for the explicit
        # locked-by-pipeline marker.
        stdout_tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
        last = stdout_tail[0]
        if r.returncode == 0 and "locked by other pipeline" in last:
            log(f"audio-normalize SKIPPED (lock conflict): {path} — {last[:160]}")
        elif r.returncode == 0:
            log(f"audio-normalize OK: {path}")
        else:
            tail = (r.stderr or "").strip().splitlines()[-1:] or [""]
            log(f"audio-normalize FAIL rc={r.returncode}: {path} -- {tail[0][:200]}")
    except subprocess.TimeoutExpired:
        log(f"audio-normalize TIMEOUT (2h): {path}")
    except Exception as e:
        log(f"audio-normalize ERR: {path} -- {type(e).__name__}: {e}")


def _expose_tag_normalize(out: Path, path: Path, replacement: Path,
                          path_str: str) -> tuple[dict | None, bool]:
    """Swap the muxed `out` into `replacement`, stamp the CONSOLIDATED_SUBS
    idempotency tag, and run inline loudness normalization — all while
    holding the destination .mkv's lock.

    Returns `(early_result, tag_written)`:
      - `early_result` is a result dict the caller must return as-is when we
        have to abort before mutating: SKIP (destination .mkv already locked
        by another runner) or FAIL (a sibling .mkv appeared under the lock).
        None when we proceeded.
      - `tag_written` is whether the idempotency tag landed.  A failed tag
        is NOT rolled back (consolidation succeeded; state.json is the
        primary idempotency backstop) but IS logged + recorded in state so
        a later state-cache loss doesn't silently re-process the file.

    We hold `path`'s lock already, but on a suffix-changing replace
    (.mp4 → .mkv) that lock does NOT cover the new .mkv — without locking
    the destination here, a watcher/scan could grab the freshly-created
    .mkv and process it concurrently mid-tag/normalize.  When there's no
    suffix change the .mkv IS `path` (already locked), so `nullcontext`
    keeps a single code path.  Passing `locked_path=replacement` lets the
    normalize subprocess inherit THIS lock instead of racing to re-acquire.
    """
    from media_stack.locking import acquire_file_lock
    dest_lock = (acquire_file_lock(replacement) if replacement != path
                 else nullcontext(True))
    with dest_lock as dest_acquired:
        if replacement != path and not dest_acquired:
            if out.exists():
                out.unlink()
            return ({"path": path_str, "status": "SKIP",
                     "detail": f"destination {replacement.name} locked by other runner"},
                    False)
        # Re-check the sibling collision UNDER the lock: the check in
        # _process_locked ran before we held this lock, so a concurrent
        # worker could have created `replacement` in between (TOCTOU).
        if replacement != path and replacement.exists():
            if out.exists():
                out.unlink()
            return ({"path": path_str, "status": "FAIL",
                     "detail": f"collision: sibling .mkv appeared for {path.name}"},
                    False)
        os.replace(out, replacement)
        if replacement != path and path.exists():
            path.unlink()
        # Treat a tag-write EXCEPTION like a False return: the remux is
        # already swapped in (and the .mp4 may be gone), so an uncaught raise
        # would escape with state un-updated + sidecars un-cleaned, and the
        # next scan would reprocess the already-mutated file.  Record
        # tag_written=False and continue the "consolidated but untagged" path.
        try:
            tag_written = set_consolidated_tag(replacement, PIPELINE_VERSION)
        except Exception as exc:
            tag_written = False
            log(f"ERROR: CONSOLIDATED_SUBS tag write raised for {replacement}: "
                f"{type(exc).__name__}: {exc} (consolidation done; tag missing)")
        else:
            if not tag_written:
                log(f"ERROR: CONSOLIDATED_SUBS tag write failed for {replacement} "
                    f"(consolidation done + state recorded; MKV idempotency marker missing)")
        _normalize_audio_inline(replacement, locked_path=replacement)
        return (None, tag_written)


def _process_locked(path: Path, path_str: str, dry_run: bool = False) -> dict:
    # Re-check is_file after lock acquisition — the file may have been
    # renamed/removed by a peer that was previously holding the lock.
    if not path.is_file():
        return {"path": path_str, "status": "SKIP", "detail": "file gone after lock"}

    state = load_state(STATE_FILE)
    key = f"{path.resolve()}"
    cur = file_key(path)
    prev = state.get(key)
    if prev and prev.get("size") == cur[0] and prev.get("mtime") == cur[1] and prev.get("v") == PIPELINE_VERSION:
        return {"path": path_str, "status": "SKIP", "detail": "state cache"}

    info = probe(path)
    if not info:
        return {"path": path_str, "status": "FAIL", "detail": "probe failed"}

    if already_processed(info):
        if not dry_run:
            update_state_entry(STATE_FILE, key, {"size": cur[0], "mtime": cur[1],
                                                 "v": PIPELINE_VERSION, "status": "skip-tag"})
        return {"path": path_str, "status": "SKIP", "detail": "tag present"}

    streams = info.get("streams", [])
    sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    av_streams  = [s for s in streams if s.get("codec_type") in ("video", "audio")]

    # Note: do NOT early-return on empty sub_streams or empty eng_streams here.
    # Sidecar SRTs (Bazarr et al) are evaluated below and are a valid source —
    # MP4 rips with no embedded subs but rich sidecars must still flow through.
    eng_streams = [s for s in sub_streams
                   if ((s.get("tags") or {}).get("language", "").lower() in ENG_LANGS)]

    eng_text   = [s for s in eng_streams if s.get("codec_name") in TEXT_CODECS]
    eng_image  = [s for s in eng_streams if s.get("codec_name") in IMAGE_CODECS]

    # Identify a forced English text track (covers foreign dialogue) — keep alongside main if present.
    forced_text = next((s for s in eng_text if is_forced_track(s)), None)
    main_candidates = [s for s in eng_text if s is not forced_text]

    sidecars = find_sidecar_subs(path)
    sidecar_main = [(p, sdh) for (p, forced, sdh) in sidecars if not forced]
    sidecar_forced = next(((p) for (p, forced, sdh) in sidecars if forced), None)

    if not main_candidates and not eng_image and not sidecar_main:
        if sidecar_forced or forced_text:
            return {"path": path_str, "status": "FAIL", "detail": "only forced English"}
        return {"path": path_str, "status": "NEEDS_BAZARR", "detail": "no English candidates"}

    if dry_run:
        # Past this point would mutate (workdir, extract, remux, replace, tag,
        # sidecar deletion, audio normalization, state writes). Report and stop.
        sources = []
        if main_candidates: sources.append(f"embedded={len(main_candidates)}")
        if sidecar_main:    sources.append(f"sidecar={len(sidecar_main)}")
        if eng_image:       sources.append(f"image={len(eng_image)} (needs subliminal)")
        return {"path": path_str, "status": "SKIP",
                "detail": f"dry-run: would FIX ({', '.join(sources)})"}

    # Pick winner
    workdir = Path(tempfile.mkdtemp(prefix=f"consolsub_{os.getpid()}_", dir=path.parent))
    try:
        chosen_srt: Path | None = None

        scored: list[tuple[int, Path, str]] = []  # (score, srt_path, source-tag)

        # Embedded text candidates
        for s in main_candidates:
            ext = ".ass" if s.get("codec_name") == "ass" else ".srt"
            tmp = workdir / f"track{s['index']}{ext}"
            if not extract_track(path, s["index"], s.get("codec_name"), tmp):
                continue
            if tmp.suffix == ".ass":
                srt_p = tmp.with_suffix(".srt")
                if not ass_to_srt(tmp, srt_p):
                    continue
                tmp = srt_p
            lc = count_subtitle_lines(tmp)
            scored.append((score_text_track(s, lc), tmp, "embedded"))

        # Sidecar candidates (Bazarr-downloaded files)
        for sc_path, is_sdh in sidecar_main:
            local = workdir / sc_path.name
            shutil.copy2(sc_path, local)
            if local.suffix == ".ass" or local.suffix == ".ssa":
                srt_p = local.with_suffix(".srt")
                if not ass_to_srt(local, srt_p):
                    continue
                local = srt_p
            elif local.suffix == ".vtt":
                # convert via ffmpeg
                srt_p = local.with_suffix(".srt")
                r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(local), str(srt_p)],
                                   capture_output=True, timeout=60)
                if r.returncode != 0:
                    continue
                local = srt_p
            lc = count_subtitle_lines(local)
            # Treat sidecar like a high-quality text track; SDH gets +150
            sidecar_score = 1100 + (150 if is_sdh else 0) + min(lc, 2000) // 5 + 50
            scored.append((sidecar_score, local, "sidecar"))

        if not scored:
            if eng_image:
                fetched = fetch_via_subliminal(path, workdir)
                if fetched is not None:
                    lc = count_subtitle_lines(fetched)
                    # Score below sidecar (1100) — fetched-on-demand is less
                    # vetted than a Bazarr-curated sidecar but better than nothing.
                    scored.append((1000 + min(lc, 2000) // 5, fetched, "subliminal"))
                else:
                    return {"path": path_str, "status": "NEEDS_BAZARR",
                            "detail": "only image subs (subliminal fetch failed)"}
            else:
                return {"path": path_str, "status": "FAIL", "detail": "all extracts failed"}

        scored.sort(key=lambda x: x[0], reverse=True)
        _, chosen_srt, _ = scored[0]

        # Cleanup transforms
        try:
            txt = chosen_srt.read_text(encoding="utf-8", errors="replace")
            cleaned = clean_srt_text(txt)
            chosen_srt.write_text(cleaned, encoding="utf-8")
        except Exception as e:
            return {"path": path_str, "status": "FAIL", "detail": f"clean failed: {e}"}

        # Sync against audio
        synced_srt = workdir / "main.synced.srt"
        sync_ok = run_ffsubsync(path, chosen_srt, synced_srt)
        final_srt = synced_srt if sync_ok else chosen_srt

        # Forced track: extract+clean if present
        forced_srt_final = None
        if forced_text:
            ext = ".ass" if forced_text.get("codec_name") == "ass" else ".srt"
            ftmp = workdir / f"forced{ext}"
            if extract_track(path, forced_text["index"], forced_text.get("codec_name"), ftmp):
                if ftmp.suffix == ".ass":
                    fsrt = ftmp.with_suffix(".srt")
                    if ass_to_srt(ftmp, fsrt):
                        ftmp = fsrt
                try:
                    ftmp.write_text(clean_srt_text(ftmp.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
                    forced_srt_final = ftmp
                except Exception:
                    pass

        # v2: choose the single audio track to keep (prune commentary,
        # descriptive, and unwanted-language dubs; jpn for anime, kor for
        # Korean cinema, otherwise eng).
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]

        # Real motion video streams. Filters out:
        #  - attached_pic-disposition cover art (legitimate flag),
        #  - and still-image codecs (mjpeg/png/etc.) embedded as video streams
        #    without the disposition flag — these cause black-screen-no-audio
        #    until the player is forced to re-pick on seek.
        video_streams_all = [s for s in streams if s.get("codec_type") == "video"]
        video_streams = [
            s for s in video_streams_all
            if not (s.get("disposition") or {}).get("attached_pic")
            and s.get("codec_name") not in STILL_IMAGE_VIDEO_CODECS
        ]
        # Belt-and-braces: if multiple real video tracks still remain (rare —
        # e.g. director's-cut alt angle), keep only the default-disposition one,
        # falling back to the first.
        if len(video_streams) > 1:
            default_v = next(
                (s for s in video_streams if (s.get("disposition") or {}).get("default")),
                video_streams[0],
            )
            video_streams = [default_v]
        dropped_video = max(0, len(video_streams_all) - len(video_streams))

        # Pass audio_streams so foreign-original films (input already carries
        # eng+jpn or eng+kor with canonical disposition) can be detected and
        # preserve all language tracks. multi_keep is True only for live-
        # action foreign-original films (DUAL_AUDIO_KEYWORDS or the
        # canonical-disposition path); anime/Korean keyword paths still use
        # single-best-track selection per the documented audio rule.
        allowed_langs, primary_lang, multi_keep = get_audio_lang_pref(path, audio_streams)
        keep_audio_idx = select_keep_audio(audio_streams, allowed_langs, primary_lang, multi_keep)
        keep_video_idx = [s["index"] for s in video_streams]
        dropped_audio = max(0, len(audio_streams) - len(keep_audio_idx))

        out = path.with_suffix(path.suffix + f".consol.{os.getpid()}.tmp.mkv")
        if out.exists():
            out.unlink()
        ok, detail = remux(path, out, keep_video_idx, keep_audio_idx,
                           final_srt, forced_srt_final)
        if not ok:
            if out.exists():
                out.unlink()
            return {"path": path_str, "status": "FAIL", "detail": f"remux: {detail}"}

        # Atomic replace; preserve target suffix as .mkv (the new container).
        # Refuse to overwrite an unrelated sibling .mkv with the same stem
        # (e.g. processing "Movie.mp4" must not destroy a pre-existing
        # "Movie.mkv" — that's a different file, not our temp output).
        replacement = path.with_suffix(".mkv")
        if replacement != path and replacement.exists():
            if out.exists():
                out.unlink()
            return {"path": path_str, "status": "FAIL",
                    "detail": f"collision: sibling .mkv exists for {path.name}"}
        # Swap the new container into place, stamp the idempotency tag, and
        # run inline loudness normalization — all under the destination .mkv's
        # lock (see _expose_tag_normalize).
        early_result, tag_written = _expose_tag_normalize(
            out, path, replacement, path_str)
        if early_result is not None:
            return early_result

        # Sidecar SRTs are now redundant: English ones are embedded, foreign ones
        # aren't wanted (English-only policy for embedded).
        # Drop every sidecar matching this basename so Jellyfin doesn't re-surface them.
        dropped_sidecars = 0
        for entry in replacement.parent.iterdir():
            if not entry.is_file() or entry == replacement:
                continue
            if entry.name.startswith(replacement.stem) and entry.name.lower().endswith(SIDECAR_EXTS):
                try:
                    entry.unlink()
                    dropped_sidecars += 1
                except OSError:
                    pass

        # Update state — use the per-key upsert so we don't clobber
        # concurrent worker updates (codex round-2 #2).
        new_key = f"{replacement.resolve()}"
        new_size, new_mtime = file_key(replacement)
        update_state_entry(STATE_FILE, new_key, {"size": new_size, "mtime": new_mtime,
                                                 "v": PIPELINE_VERSION,
                                                 "status": "fixed", "synced": sync_ok,
                                                 "tag_written": tag_written})

        dropped_embedded = max(0, len(sub_streams) - (1 + (1 if forced_srt_final else 0)))
        return {"path": str(replacement), "status": "FIXED",
                "detail": (f"synced={sync_ok} dropped_subs={dropped_embedded} "
                           f"dropped_audio={dropped_audio} "
                           f"dropped_video={dropped_video} "
                           f"dropped_sidecars={dropped_sidecars} "
                           f"audio_lang={primary_lang}")}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def walk(root: Path):
    for r, _, files in os.walk(root):
        for f in files:
            fl = f.lower()
            if not fl.endswith((".mkv", ".mp4")):
                continue
            # Skip ALL tmp.mkv staging files, not just the .consol.PID.tmp.mkv
            # pattern (consolidate-watch.sh defensively writes
            # .consol.*.tmp.mkv; future one-shots may follow other suffix
            # conventions like .add-*.tmp.mkv).  Without this broad guard, a
            # half-finished staging file from any helper could be picked up
            # by the nightly sweep and processed as a real media file (codex
            # round-6 finding #3).
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
                log(f"{r['status']}: {Path(r['path']).name[:70]} | {r.get('detail','')}")
        else:
            with ProcessPoolExecutor(max_workers=args.jobs) as ex:
                futs = {ex.submit(process_file, str(f), args.dry_run): f for f in files}
                for fut in as_completed(futs):
                    r = fut.result()
                    log(f"{r['status']}: {Path(r['path']).name[:70]} | {r.get('detail','')}")
        log("scan complete")
        return 0

    if args.target:
        r = process_file(args.target, dry_run=args.dry_run)
        log(f"{r['status']}: {Path(r['path']).name[:70]} | {r.get('detail','')}")
        return 0 if r["status"] in ("FIXED", "SKIP", "NEEDS_BAZARR") else 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
