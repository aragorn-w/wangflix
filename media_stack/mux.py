"""mkvmerge orchestration — both the full consolidate-subs v2 mux
(audio narrowing + clean English srt re-attachment) and the simple
normalize-audio remux (just a cue-index rewrite).

Why mkvmerge instead of ffmpeg for the final container write:
ffmpeg's matroska muxer wrote zero-cue files which broke seeking on
every player and tripped Intro Skipper.  mkvmerge always emits a
proper cue index.

Multi-audio default-flag rule:
When multiple audio tracks are kept (e.g. dual-audio eng+jpn original
release), ONLY the FIRST (caller orders primary-first) gets
default=yes.  Marking >1 default produces an invalid Matroska file.

`eng_srt=None` cleanup mode:
`remux()` also serves the "no main English subtitle yet" AUDIO-AND-
SUBTITLE CLEANUP REMUX (consolidate-subs.py's `_cleanup_no_main_english`,
the DEFECT 1 fix) so that logic doesn't have to duplicate the mkvmerge
argv-building here.  Passing `eng_srt=None` means "no external subtitle
to attach" — instead of `--no-subtitles`, the SOURCE subtitle tracks
named in `keep_subtitle_indices` (e.g. a forced-English text track,
English image subs) are kept via `--subtitle-tracks`, unmodified.
`forced_srt` is ignored in this mode (it only applies to the external-
attach path).  The full consolidate-subs call site (`eng_srt` set) is
byte-for-byte unchanged by this addition.

`cleanup_flags`:
the cleanup never drops OR physically reorders a video or audio track
(see `_cleanup_no_main_english` / `media_stack.audio.
confident_default_audio`) — `--audio-tracks` is a SELECTOR, not a
reorder instruction; mkvmerge does not change output track order based
on the order IDs are listed in it (`--track-order` would be required
for that, and isn't used here).

EXACT flag preservation (codex round 4 #2): mkvmerge does NOT leave a
track TYPE's `default` flag alone when the source provides none for
that type at all — it auto-picks one itself.  So in cleanup mode,
omitting `--default-track` overrides does NOT mean "unchanged"; it
means "mkvmerge decides".  `cleanup_flags` (a `{track_id: {"default":
bool, "forced": bool}}` map the caller builds from the probed SOURCE
dispositions, changing only what it actually intends to change — e.g.
the confident audio pick) makes every kept video/audio/subtitle track
carry an EXPLICIT `--default-track`, and every kept subtitle ALSO an
explicit `--forced-track`, built from that map — never omitted, never
left to mkvmerge's own judgement.

`cleanup_flags` is None in the full path's only mode: the ORIGINAL
behavior is unchanged there — every video index gets `default=yes`;
only the FIRST audio index gets `default=yes` (caller orders the list
primary-first); subtitles go through the `eng_srt`/`forced_srt`
external-attach path.  The full-path argv stays byte-for-byte identical.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def remux(
    src: Path,
    dst: Path,
    video_indices: list[int],
    audio_indices: list[int],
    eng_srt: Path | None,
    forced_srt: Path | None,
    keep_subtitle_indices: list[int] | None = None,
    cleanup_flags: dict[int, dict[str, bool]] | None = None,
) -> tuple[bool, str]:
    """v2 full consolidate-subs mux, or the no-main-English-sub cleanup
    remux when `cleanup_flags` is given (see module docstring; cleanup
    mode also always passes `eng_srt=None`).

    Builds a new MKV from `src` with:
      - video tracks: `video_indices` only (drops alt cuts in the full
        path; the cleanup never excludes any). Default flag: `yes` for
        every one in the full path, or `cleanup_flags[id]["default"]`
        explicitly in cleanup mode.
      - audio tracks: `audio_indices` ONLY (drops commentary, foreign
        dubs not in user's policy, in the full path; the cleanup never
        drops any). Default flag: only the FIRST gets `yes` in the full
        path, or `cleanup_flags[id]["default"]` explicitly in cleanup
        mode (the caller's only intentional change there is the
        confident audio pick, if any).
      - subtitle tracks:
        - `eng_srt` given (full path): ALL source subs dropped; one
          clean English srt re-attached as default; optional forced
          srt as second track.
        - `eng_srt=None` + `cleanup_flags` given (cleanup mode): source
          subtitle tracks are dropped EXCEPT `keep_subtitle_indices`,
          each kept with an EXPLICIT `--default-track` and
          `--forced-track` from `cleanup_flags` (mirroring its source
          disposition — the cleanup never changes a subtitle's own
          flags, it just has to state them explicitly; see module
          docstring).

    Returns `(success, detail)`.
    """
    cmd = ["mkvmerge", "--quiet", "--output", str(dst)]
    if video_indices:
        cmd += ["--video-tracks", ",".join(str(i) for i in video_indices)]
        if cleanup_flags is not None:
            for i in video_indices:
                d = cleanup_flags.get(i, {})
                cmd += ["--default-track", f"{i}:{'yes' if d.get('default') else 'no'}"]
        else:
            # Force default-flag on the kept video — source might have had
            # it on a track we dropped (e.g. a director's-cut alt video),
            # leaving the survivor without it.
            for i in video_indices:
                cmd += ["--default-track", f"{i}:yes"]
    if audio_indices:
        cmd += ["--audio-tracks", ",".join(str(i) for i in audio_indices)]
        if cleanup_flags is not None:
            # Explicit per-ID flag, independent of list/output order (see
            # module docstring: --audio-tracks is a selector, not a
            # reorder).  The caller already resolved exactly one `True`
            # here when a confident change is needed, so there's never
            # more than one default audio track.
            for i in audio_indices:
                d = cleanup_flags.get(i, {})
                cmd += ["--default-track", f"{i}:{'yes' if d.get('default') else 'no'}"]
        else:
            # Full-path behavior, unchanged: when multiple audio tracks
            # are kept (e.g. dual-audio eng+jpn original release), only
            # the FIRST in the caller's list gets default=yes — caller
            # orders the list primary-first.  Marking >1 default produces
            # an invalid Matroska file.
            for pos, i in enumerate(audio_indices):
                cmd += ["--default-track", f"{i}:{'yes' if pos == 0 else 'no'}"]
    else:
        cmd += ["--no-audio"]
    if eng_srt is not None:
        cmd += ["--no-subtitles"]
    elif keep_subtitle_indices:
        cmd += ["--subtitle-tracks", ",".join(str(i) for i in keep_subtitle_indices)]
        if cleanup_flags is not None:
            for i in keep_subtitle_indices:
                d = cleanup_flags.get(i, {})
                cmd += ["--default-track", f"{i}:{'yes' if d.get('default') else 'no'}"]
                cmd += ["--forced-track", f"{i}:{'yes' if d.get('forced') else 'no'}"]
    else:
        cmd += ["--no-subtitles"]
    cmd += [str(src)]
    if eng_srt is not None:
        cmd += ["--language", "0:eng",
                "--track-name", "0:English",
                "--default-track", "0:yes",
                "--forced-track", "0:no",
                str(eng_srt)]
        if forced_srt:
            cmd += ["--language", "0:eng",
                    "--track-name", "0:Forced",
                    "--default-track", "0:no",
                    "--forced-track", "0:yes",
                    str(forced_srt)]
    # Generous timeout: stream copies are I/O bound and mergerfs HDD
    # contention under N-way parallelism can drop effective per-process
    # throughput to <15MB/s.  30-min floor + 1MB/15kB linear term safely
    # covers a ~70GB UHD remux on a saturated pool.
    size_mb = src.stat().st_size / 1e6
    timeout = int(max(1800, size_mb / 15 + 1200))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "mkvmerge timeout"
    # mkvmerge: 0=ok, 1=warnings (output is still valid), 2=hard error
    if r.returncode > 1:
        msg = (r.stderr or r.stdout or "").strip().splitlines()[-1] \
            if (r.stderr or r.stdout) else ""
        return False, msg[:300]
    if not dst.exists() or dst.stat().st_size < 1_000_000:
        return False, "output too small"
    return True, "ok"


def mkvmerge_remux_simple(src: Path, dst: Path) -> None:
    """Simple cue-index rewrite for normalize-audio.

    ffmpeg's matroska muxer writes zero cues — broke seeking + Intro
    Skipper.  This thin wrapper re-muxes through mkvmerge to fix that
    without touching the streams.  Raises RuntimeError on failure.
    """
    cmd = ["mkvmerge", "--quiet", "--output", str(dst), str(src)]
    size_mb = src.stat().st_size / 1e6
    timeout = int(max(1800, size_mb / 15 + 1200))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode > 1:  # 1 = warnings-only, output still valid
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
        raise RuntimeError(f"mkvmerge remux failed: {tail[0][:200]}")
    if not dst.exists() or dst.stat().st_size < 100_000:
        raise RuntimeError("mkvmerge output too small")
