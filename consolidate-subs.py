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
    NORMALIZE_TMP_DIR, SIDECAR_EXTS, SWEEP_MIN_AGE_S,
    TEXT_CODECS, TMP_MKV_RE, TRAIL_WS_RE,
)
from media_stack.lang import (  # noqa: E402
    ENG_AUDIO_LANGS, JPN_AUDIO_LANGS, KOR_AUDIO_LANGS, canonical_lang, is_english_tag,
)
from media_stack.probe import (  # noqa: E402
    already_processed, file_key, probe, real_video_streams,
)
from media_stack.state import (  # noqa: E402
    load_state, save_state, update_state_entry,
)
from media_stack.audio import (  # noqa: E402
    confident_default_audio, get_audio_lang_pref, is_commentary_audio,
    is_dub_audio, select_keep_audio,
)
from media_stack.arrlang import original_language_for  # noqa: E402
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
    # Single-file entry points (the watcher, a manual run) never pass through
    # walk(), so the prune above cannot cover them.  This must stay BEFORE
    # acquire_file_lock: locking.py opens the lock file before attempting the
    # flock, so merely reaching that call deposits a .lock beside the scratch
    # file even when the lock is never held.
    # Resolve first: `path.parts` sees only the spelling handed to us, so a
    # relative invocation from inside the workdir would slip past the check.
    # On resolution failure we REFUSE rather than fall back to the unresolved
    # spelling: a guard that falls back to the input it could not verify fails
    # open, which is precisely the bypass this exists to close.
    try:
        _resolved = path.resolve()
    except OSError as e:
        return {"path": path_str, "status": "FAIL",
                "detail": f"cannot resolve path: {e}"}
    if NORMALIZE_TMP_DIR in _resolved.parts:
        return {"path": path_str, "status": "SKIP",
                "detail": "normalize-audio workdir intermediate"}

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
                          path_str: str, tag: bool = True,
                          normalize: bool = True) -> tuple[dict | None, bool]:
    """Swap the muxed `out` into `replacement`, stamp the CONSOLIDATED_SUBS
    idempotency tag, and (optionally) run inline loudness normalization —
    all while holding the destination .mkv's lock.

    `tag=False` skips the CONSOLIDATED_SUBS write entirely (tag_written is
    always False in that case) — used by the no-main-English-subtitle
    AUDIO-AND-SUBTITLE CLEANUP REMUX (`_cleanup_no_main_english`), which
    must NOT mark the file as fully consolidated: it still needs the full
    pass once an English subtitle shows up.

    `normalize=False` skips the inline loudness-normalization subprocess
    entirely — ALSO used by that same cleanup, and for an independent
    reason: `normalize-audio.py` can re-encode the primary audio track
    AND ffmpeg auto-marks the first stream `default` when none is
    flagged, either of which would re-flag audio without going through
    `confident_default_audio`'s confidence gate.  Normalization stays a
    FULL-pass-only step, exactly as it was before this cleanup existed;
    every other caller (the default `True`) is unaffected.

    The swap + lock protocol are identical in every combination of
    `tag`/`normalize`.

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
        tag_written = False
        if tag:
            # Treat a tag-write EXCEPTION like a False return: the remux is
            # already swapped in (and the .mp4 may be gone), so an uncaught
            # raise would escape with state un-updated + sidecars un-cleaned,
            # and the next scan would reprocess the already-mutated file.
            # Record tag_written=False and continue the "consolidated but
            # untagged" path.
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
        if normalize:
            _normalize_audio_inline(replacement, locked_path=replacement)
        return (None, tag_written)


# Fix 2 (codex round 3): a subtitle is droppable as "positively foreign"
# only when its base subtag is a well-formed 2-3 letter ASCII-alphabetic
# language code that ISN'T English and isn't one of these special
# non-language codes (und/mul/mis/zxx) or in the qaa-qtz private-use
# range.  Anything else -- empty, und-*, an unknown shape, a non-ASCII
# "letter" like an accented "éé" (codex round 4 #3: Python's str.isalpha()
# is Unicode-aware and would wrongly accept it) -- is ambiguous and must
# be KEPT, not guessed at.
_SUBTITLE_SPECIAL_LANG_CODES = frozenset({"und", "mul", "mis", "zxx"})
_SUBTITLE_LANG_BASE_RE = re.compile(r"[a-z]{2,3}")


def _is_positively_foreign_sub(s: dict) -> bool:
    """True iff subtitle stream `s` is positively identifiable as a
    non-English language (see module-level comment above `_SUBTITLE_
    SPECIAL_LANG_CODES` for the exact rule). Used by
    `_cleanup_no_main_english` to decide which subtitle tracks are safe
    to drop."""
    lang = ((s.get("tags") or {}).get("language") or "").strip().lower()
    base = lang.split("-", 1)[0]
    if not _SUBTITLE_LANG_BASE_RE.fullmatch(base):
        return False
    if base in _SUBTITLE_SPECIAL_LANG_CODES:
        return False
    if "qaa" <= base <= "qtz":
        return False
    return not is_english_tag(lang)


def _cleanup_no_main_english(
    path: Path, path_str: str, status: str, base_detail: str,
    sub_streams: list[dict], audio_streams: list[dict], video_streams: list[dict],
    forced_text: dict | None, eng_image: list[dict],
    dry_run: bool,
) -> dict:
    """AUDIO-AND-SUBTITLE CLEANUP REMUX for the "no main English subtitle
    yet" situation — DEFECT 1's fix.  Called from FOUR places in
    `_process_locked`: the early-return gate's NEEDS_BAZARR "no English
    candidates" and FAIL "only forced English" branches; and later, past
    the gate (a text/sidecar candidate existed but didn't pan out), the
    NEEDS_BAZARR "only image subs (subliminal fetch failed)" branch and
    the FAIL "all extracts failed" branch (every text/sidecar candidate
    failed extraction, no image candidate to fall back to either).

    Without this, a file that will never get a main English subtitle until
    Bazarr (or subliminal) finds one kept EVERY foreign subtitle track and
    whatever audio track happened to be flagged default, however wrong —
    e.g. a MULTI release with French default audio + English second audio
    + French-only subs kept French as the default audio for as long as
    Bazarr took to find an English sub (can be weeks).

    GOVERNING PRINCIPLE: this cleanup must never DELETE a track that might
    be wanted, and must never silently change a flag it didn't mean to.
    A wrong default flag is recoverable (the next pass, or a manual
    remux, can fix it); a deleted track is not.  Consequences of that,
    each a fix for a real finding:
      - Video is NOT TOUCHED AT ALL (codex round 4 #1) — not dropped,
        not reduced, cover art included.  Cover-art removal stays the
        FULL path's job; this cleanup runs long before an English
        subtitle exists to consolidate onto, so it has no business
        making container-repair decisions. `video_streams` here is the
        caller's RAW, unfiltered list of every video-type stream.
      - Audio is NEVER dropped and NEVER physically reordered — every
        stream from the source survives, in its ORIGINAL physical
        order.  The only audio action available is changing WHICH ONE
        carries the `default` flag, and that only happens when
        `confident_default_audio` (`media_stack.audio`) says so with
        actual confidence (a keyword rule, a KNOWN Radarr/Sonarr
        `original_language`, or an explicit `original` disposition —
        NEVER a bare `default` disposition, NEVER a guess).  `None`
        means "leave the audio exactly as it is" — the thrash guard: an
        Arr outage can't make this cleanup re-remux the same file over
        and over, alternating the default track each time.  mkvmerge's
        `--audio-tracks` is a SELECTOR, not a reorder instruction
        (`--track-order` would be needed for that, and isn't used
        here), so there is no "reorder" to begin with — only the
        `default` flag moves, targeted by source track ID.
      - Subtitles: only a track whose language is POSITIVELY identified
        as foreign is dropped (see `_is_positively_foreign_sub`) — a
        well-formed ASCII 2-3 letter code that isn't English and isn't
        one of the special/private-use codes.  Everything ambiguous
        (untagged, `und`, `mul`, `zxx`, non-ASCII letters, an
        unrecognized shape) is kept; it might be English (or might
        matter) and guessing wrong means deleting it.
      - EXACT flag preservation (codex round 4 #2): mkvmerge does NOT
        leave a track TYPE's `default` flag alone when the source
        provides none for that type at all — it picks one itself.
        Omitting `--default-track` overrides therefore does NOT mean
        "nothing changes"; it means "mkvmerge decides".  So every kept
        track of every type gets an EXPLICIT `--default-track`, and
        every kept subtitle ALSO an explicit `--forced-track`, built
        from the SOURCE dispositions (`_cleanup_track_flags`) — the
        ONLY intentional change anywhere in that map is the confident
        audio pick, if any.
      - A verify-after-mux step (see below) catches anything this
        reasoning missed before the file is swapped into place.
      - Loudness normalization is NOT run from this path at all (see
        `_expose_tag_normalize`'s `normalize=False`): it can re-encode
        audio and ffmpeg auto-marks a stream `default` when none is
        flagged, either of which would re-flag audio outside the
        confidence gate above. Normalization stays FULL-pass-only.

    Keeps: EVERY stream in `video_streams` and `audio_streams` (byte-for-
    byte same tracks, same order; only a `default`-flag change on audio,
    and only when confident), the forced-English text track if present,
    every English image-subtitle track (`eng_image` — a later subliminal
    OCR source; must NOT be dropped here), AND every subtitle that isn't
    positively foreign.

    Does NOT stamp CONSOLIDATED_SUBS and does NOT touch sidecar files:
    the file must still get the FULL pass (sidecar cleanup included) once
    an English subtitle becomes available.  Idempotent: once cleaned,
    `something_to_clean` is False on the next pass, so no remux runs and
    no tag is needed to short-circuit it.

    After a successful remux, the output is re-probed and its track
    counts AND its audio/video default-flag pattern are checked against
    what was INTENDED before the file is swapped in — belt-and-braces
    against mkvmerge (or our own argv construction) silently dropping or
    misflagging something.  A mismatch deletes the tmp output and
    returns FAIL without touching the original.  The audio count must
    equal the ORIGINAL audio count (never fewer — see above); video
    count must equal the ORIGINAL video count (never touched at all).

    `status`/`base_detail` are the caller's original early-return values —
    the returned status is unchanged by this helper except on an actual
    remux/verify failure (FAIL); only `detail` gains a cleanup summary.
    """
    original_language = original_language_for(path)
    confident_idx = confident_default_audio(
        path, audio_streams, original_language=original_language)

    default_flagged = {
        s["index"] for s in audio_streams
        if (s.get("disposition") or {}).get("default")
    }
    if confident_idx is None:
        # Not confident -> leave audio exactly as it is, regardless of
        # whatever is (or isn't) currently flagged default.  This is the
        # thrash guard: an Arr outage must not re-remux the file.
        audio_default_change = False
    elif default_flagged == {confident_idx}:
        audio_default_change = False  # already the sole default -- nothing to do
    else:
        audio_default_change = True

    # Video and audio are NEVER dropped or reordered -- every stream,
    # every type, in its original physical order.
    keep_video_idx = [s["index"] for s in video_streams]
    keep_audio_idx = [s["index"] for s in audio_streams]

    # Start from the tracks we KNOW we want, then also keep anything whose
    # language we can't positively call foreign (untagged / "und" / any
    # tag already English that isn't forced_text/eng_image -- e.g. a
    # second, non-forced English track would stay too, harmlessly).
    keep_sub_indices = {s["index"] for s in eng_image}
    if forced_text is not None:
        keep_sub_indices.add(forced_text["index"])
    for s in sub_streams:
        if s["index"] not in keep_sub_indices and not _is_positively_foreign_sub(s):
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

    # EXACT flag preservation (codex round 4 #2): build the explicit
    # default/forced flag every kept track of every type must carry.
    # The ONLY deliberate change anywhere in this map is the confident
    # audio pick (if audio_default_change); everything else mirrors the
    # SOURCE disposition exactly, because mkvmerge auto-assigns a
    # default per track TYPE when the source has none at all for it --
    # omitting the override is not "leave it alone".
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
    if out.exists():
        out.unlink()
    ok, detail = remux(
        path, out, keep_video_idx, keep_audio_idx, None, None,
        keep_subtitle_indices=sorted(keep_sub_indices),
        cleanup_flags=cleanup_flags,
    )
    if not ok:
        if out.exists():
            out.unlink()
        return {"path": path_str, "status": "FAIL", "detail": f"cleanup remux: {detail}"}

    # Verify the actual output BEFORE swapping it in -- belt-and-braces
    # against mkvmerge (or our own argv) silently dropping or misflagging
    # a track we intended to keep/preserve.  Never touch the original on
    # a mismatch.
    verify_info = probe(out)
    if verify_info is None:
        out.unlink()
        return {"path": path_str, "status": "FAIL", "detail": "cleanup verify: probe failed"}
    verify_streams = verify_info.get("streams", [])
    out_video_streams = [s for s in verify_streams if s.get("codec_type") == "video"]
    out_audio_streams = [s for s in verify_streams if s.get("codec_type") == "audio"]
    out_subs = sum(1 for s in verify_streams if s.get("codec_type") == "subtitle")
    expected_audio = len(audio_streams)  # cleanup NEVER drops audio
    expected_subs = len(keep_sub_indices)
    expected_video = len(video_streams)  # cleanup NEVER touches video
    if (len(out_audio_streams) != expected_audio or out_subs != expected_subs
            or len(out_video_streams) != expected_video):
        out.unlink()
        return {"path": path_str, "status": "FAIL",
                "detail": (f"cleanup verify: expected audio={expected_audio} "
                           f"subs={expected_subs} video={expected_video}, got "
                           f"audio={len(out_audio_streams)} subs={out_subs} "
                           f"video={len(out_video_streams)}")}

    # Codex round 4 #4: counts alone aren't enough -- also verify the
    # output's per-type default-flag PATTERN matches what we intended.
    # Video/audio are never dropped or reordered, so the output streams
    # of each type appear in the same relative order as keep_video_idx/
    # keep_audio_idx; compare flag-by-position against that order.
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

    # Same sibling-collision guard as the full FIXED path (a suffix-
    # changing replace, e.g. .mp4 -> .mkv, must not clobber an unrelated
    # pre-existing sibling .mkv).
    replacement = path.with_suffix(".mkv")
    if replacement != path and replacement.exists():
        if out.exists():
            out.unlink()
        return {"path": path_str, "status": "FAIL",
                "detail": f"collision: sibling .mkv exists for {path.name}"}

    # tag=False: this is a cleanup, not a full consolidation — must NOT
    # stamp CONSOLIDATED_SUBS or this branch would never run again even
    # though no English subtitle has actually been consolidated.
    # normalize=False: loudness normalization is a FULL-pass-only step
    # (see the docstring) — never run it from here.
    early_result, _tag_written = _expose_tag_normalize(
        out, path, replacement, path_str, tag=False, normalize=False)
    if early_result is not None:
        return early_result

    return {"path": str(replacement), "status": status,
            "detail": (f"{base_detail}; cleaned: audio_default_change={audio_default_change} "
                       f"dropped_subs={dropped_subs}")}


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
    # is_english_tag() recognizes regional/script variants (en-US, eng-GB,
    # ...) as English too, not just the bare eng/en/english aliases — an
    # untagged/"und" track is deliberately NOT a candidate here (it might be
    # anything; see media_stack.config.ENG_LANGS's docstring).
    eng_streams = [s for s in sub_streams
                   if is_english_tag((s.get("tags") or {}).get("language"))]

    eng_text   = [s for s in eng_streams if s.get("codec_name") in TEXT_CODECS]
    eng_image  = [s for s in eng_streams if s.get("codec_name") in IMAGE_CODECS]

    # Identify a forced English text track (covers foreign dialogue) — keep alongside main if present.
    forced_text = next((s for s in eng_text if is_forced_track(s)), None)
    main_candidates = [s for s in eng_text if s is not forced_text]

    sidecars = find_sidecar_subs(path)
    sidecar_main = [(p, sdh) for (p, forced, sdh) in sidecars if not forced]
    sidecar_forced = next(((p) for (p, forced, sdh) in sidecars if forced), None)

    # Audio + real-video classification, hoisted above the early-return gate
    # below (the full-FIXED-path code further down used to compute these
    # itself, after the gate) so the "no main English subtitle yet" cleanup
    # branch can also use them. See probe.real_video_streams for why cover
    # art has to be excluded here, not just later.
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    video_streams_all = [s for s in streams if s.get("codec_type") == "video"]
    real_video = real_video_streams(streams)

    # The CLEANUP does not touch video AT ALL (codex round 4 #1) -- not
    # even cover art, which stays the FULL path's job.  It gets the raw,
    # unfiltered `video_streams_all` directly; no selection to make here.

    # The FULL FIXED path still drops cover art and reduces multiple real
    # video streams down to one (belt-and-braces -- e.g. a director's-cut
    # alt angle) when picking the final single-video-track output.
    video_streams = real_video
    if len(video_streams) > 1:
        default_v = next(
            (s for s in video_streams if (s.get("disposition") or {}).get("default")),
            video_streams[0],
        )
        video_streams = [default_v]
    keep_video_idx = [s["index"] for s in video_streams]
    dropped_video = max(0, len(video_streams_all) - len(video_streams))

    if not main_candidates and not eng_image and not sidecar_main:
        # DEFECT 1: this used to be a pure early return that mutated nothing,
        # so a MULTI release with e.g. French default audio + English second
        # + French-only subs kept the French dub as default and every
        # foreign subtitle track for as long as Bazarr took to find an
        # English sub (weeks, sometimes).  Now it still returns the same
        # status (and FAIL/NEEDS_BAZARR keep their original exit-code
        # mapping in main()) but first cleans up audio/subtitle tracks the
        # documented policy would never have kept, if there's anything to
        # clean. See `_cleanup_no_main_english`.
        if sidecar_forced or forced_text:
            return _cleanup_no_main_english(
                path, path_str, "FAIL", "only forced English",
                sub_streams, audio_streams, video_streams_all,
                forced_text, eng_image, dry_run,
            )
        return _cleanup_no_main_english(
            path, path_str, "NEEDS_BAZARR", "no English candidates",
            sub_streams, audio_streams, video_streams_all,
            forced_text, eng_image, dry_run,
        )

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
                    # Same "no main English subtitle yet" situation as the
                    # early-return gate (DEFECT 1) -- we just discovered it
                    # later, after an image-sub-only file's subliminal fetch
                    # failed instead of up front.  Run the same cleanup.
                    return _cleanup_no_main_english(
                        path, path_str, "NEEDS_BAZARR",
                        "only image subs (subliminal fetch failed)",
                        sub_streams, audio_streams, video_streams_all,
                        forced_text, eng_image, dry_run,
                    )
            else:
                # Every English text/sidecar candidate failed extraction
                # and there's no image candidate to fall back to -- same
                # "no main English subtitle yet" situation (DEFECT 1), so
                # run the same cleanup. Status stays FAIL.
                return _cleanup_no_main_english(
                    path, path_str, "FAIL", "all extracts failed",
                    sub_streams, audio_streams, video_streams_all,
                    forced_text, eng_image, dry_run,
                )

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
        # Korean cinema, otherwise eng).  `audio_streams`/`keep_video_idx`/
        # `dropped_video` were computed earlier (hoisted above the
        # no-main-English-subtitle gate so that branch can use them too).
        #
        # Pass audio_streams + original_language_for(path) (Radarr/Sonarr's
        # originalLanguage, via media_stack.arrlang — see DEFECT 2) so
        # foreign-original films can be detected without trusting the
        # Matroska `default` disposition, which release groups routinely set
        # on a DUB. multi_keep is True only for live-action foreign-original
        # films (DUAL_AUDIO_KEYWORDS or the original-language path);
        # anime/Korean keyword paths still use single-best-track selection
        # per the documented audio rule.
        allowed_langs, primary_lang, multi_keep = get_audio_lang_pref(
            path, audio_streams, original_language=original_language_for(path))
        keep_audio_idx = select_keep_audio(audio_streams, allowed_langs, primary_lang, multi_keep)
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
    # Bind the dirnames slot (was `_`) so we can prune.  normalize-audio.py's
    # `.normalize-tmp` holds this pipeline's own 4K intermediates; descending
    # into it made us treat scratch renders as library media and deposit a
    # flock file beside each one.  Mirrors normalize-audio.py:326.
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != NORMALIZE_TMP_DIR]
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
