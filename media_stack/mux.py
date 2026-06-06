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
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def remux(
    src: Path,
    dst: Path,
    video_indices: list[int],
    audio_indices: list[int],
    eng_srt: Path,
    forced_srt: Path | None,
) -> tuple[bool, str]:
    """v2 full consolidate-subs mux.

    Builds a new MKV from `src` with:
      - video tracks: `video_indices` only (drops alt cuts), first
        forced default=yes
      - audio tracks: `audio_indices` only (drops commentary, foreign
        dubs not in user's policy), FIRST = default=yes, rest =
        default=no
      - subtitle tracks: ALL dropped from source; one clean English
        srt re-attached as default; optional forced srt as second
        track

    Returns `(success, detail)`.
    """
    cmd = ["mkvmerge", "--quiet", "--output", str(dst)]
    if video_indices:
        cmd += ["--video-tracks", ",".join(str(i) for i in video_indices)]
        # Force default-flag on the kept video — source might have had it on
        # a track we dropped (e.g. a director's-cut alt video), leaving the
        # survivor without it.
        for i in video_indices:
            cmd += ["--default-track", f"{i}:yes"]
    if audio_indices:
        cmd += ["--audio-tracks", ",".join(str(i) for i in audio_indices)]
        # When multiple audio tracks are kept (e.g. dual-audio eng+jpn
        # original release), only the FIRST gets default=yes — caller
        # orders the list primary-first.  Marking >1 default produces
        # an invalid Matroska file.
        for pos, i in enumerate(audio_indices):
            cmd += ["--default-track", f"{i}:{'yes' if pos == 0 else 'no'}"]
    else:
        cmd += ["--no-audio"]
    cmd += ["--no-subtitles"]
    cmd += [str(src)]
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
