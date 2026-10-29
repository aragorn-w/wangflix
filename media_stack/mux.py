"""mkvmerge orchestration: the consolidate-subs full mux and the normalize-audio cue-index remux.

mkvmerge, not ffmpeg, writes the final container: ffmpeg's matroska muxer wrote zero-cue
files that broke seeking and Intro Skipper.

Cleanup mode (`cleanup_flags` given): mkvmerge auto-picks a default per track type when the
source has none, so omitting `--default-track` does NOT mean "unchanged". Every kept
video/audio/subtitle track therefore carries an explicit `--default-track` (and every kept
subtitle an explicit `--forced-track`; video/audio only when their entry has a "forced" key).
`--audio-tracks` is a selector, not a reorder.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# "(generated)" lets a Whisper transcript be told apart from a real subtitle later.
ENG_TRACK_NAME = "English"
ENG_GENERATED_TRACK_NAME = "English (generated)"


def remux_timeout_s(size_bytes: int) -> int:
    """mkvmerge wall-clock budget: 30-min floor plus 1 s per 15 MB."""
    return int(max(1800, size_bytes / 1e6 / 15 + 1200))


def remux(
    src: Path,
    dst: Path,
    video_indices: list[int],
    audio_indices: list[int],
    eng_srt: Path | None,
    forced_srt: Path | None,
    keep_subtitle_indices: list[int] | None = None,
    cleanup_flags: dict[int, dict[str, bool]] | None = None,
    eng_track_name: str = ENG_TRACK_NAME,
    kept_source_subs: dict[int, dict[str, bool]] | None = None,
) -> tuple[bool, str]:
    """Build a new MKV from `src`; returns `(success, detail)`.

    Full path (`cleanup_flags` None): only the given video/audio indices are kept, source
    subtitles are dropped, and `eng_srt` is attached as default with an optional
    `forced_srt` second. `kept_source_subs` ({track id: {"default", "forced"}}, full path
    only) stream-copies those source subtitles ahead of `eng_srt` instead of
    `--no-subtitles`. Cleanup mode: see the module docstring.
    """
    cmd = ["mkvmerge", "--quiet", "--output", str(dst)]
    if video_indices:
        cmd += ["--video-tracks", ",".join(str(i) for i in video_indices)]
        if cleanup_flags is not None:
            for i in video_indices:
                d = cleanup_flags.get(i, {})
                cmd += ["--default-track", f"{i}:{'yes' if d.get('default') else 'no'}"]
                if "forced" in d:
                    cmd += ["--forced-track", f"{i}:{'yes' if d['forced'] else 'no'}"]
        else:
            # The default may have been on a dropped alt-cut video track.
            for i in video_indices:
                cmd += ["--default-track", f"{i}:yes"]
    if audio_indices:
        cmd += ["--audio-tracks", ",".join(str(i) for i in audio_indices)]
        if cleanup_flags is not None:
            for i in audio_indices:
                d = cleanup_flags.get(i, {})
                cmd += ["--default-track", f"{i}:{'yes' if d.get('default') else 'no'}"]
                if "forced" in d:
                    cmd += ["--forced-track", f"{i}:{'yes' if d['forced'] else 'no'}"]
        else:
            # Only the first (primary) audio track is default; >1 is invalid Matroska.
            for pos, i in enumerate(audio_indices):
                cmd += ["--default-track", f"{i}:{'yes' if pos == 0 else 'no'}"]
    else:
        cmd += ["--no-audio"]
    if keep_subtitle_indices and (eng_srt is None or cleanup_flags is not None):
        # With an external `eng_srt` (upgrade path) explicit flags are required.
        cmd += ["--subtitle-tracks", ",".join(str(i) for i in keep_subtitle_indices)]
        if cleanup_flags is not None:
            for i in keep_subtitle_indices:
                d = cleanup_flags.get(i, {})
                cmd += ["--default-track", f"{i}:{'yes' if d.get('default') else 'no'}"]
                cmd += ["--forced-track", f"{i}:{'yes' if d.get('forced') else 'no'}"]
    elif kept_source_subs and eng_srt is not None and cleanup_flags is None:
        cmd += ["--subtitle-tracks", ",".join(str(i) for i in kept_source_subs)]
        for i, d in kept_source_subs.items():
            cmd += ["--default-track", f"{i}:{'yes' if d.get('default') else 'no'}",
                    "--forced-track", f"{i}:{'yes' if d.get('forced') else 'no'}"]
    else:
        cmd += ["--no-subtitles"]
    cmd += [str(src)]
    if eng_srt is not None:
        cmd += ["--language", "0:eng",
                "--track-name", f"0:{eng_track_name}",
                "--default-track", "0:yes",
                "--forced-track", "0:no",
                str(eng_srt)]
        if forced_srt:
            cmd += ["--language", "0:eng",
                    "--track-name", "0:Forced",
                    "--default-track", "0:no",
                    "--forced-track", "0:yes",
                    str(forced_srt)]
    timeout = remux_timeout_s(src.stat().st_size)
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
    """Re-mux to give an ffmpeg-written file a cue index. Raises RuntimeError on failure."""
    cmd = ["mkvmerge", "--quiet", "--output", str(dst), str(src)]
    timeout = remux_timeout_s(src.stat().st_size)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode > 1:  # 1 = warnings-only, output still valid
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
        raise RuntimeError(f"mkvmerge remux failed: {tail[0][:200]}")
    if not dst.exists() or dst.stat().st_size < 100_000:
        raise RuntimeError("mkvmerge output too small")
