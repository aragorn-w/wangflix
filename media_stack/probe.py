"""ffprobe wrappers: `probe()` returns None on failure, `ffprobe_strict()` raises."""

from __future__ import annotations

import json
import math
import subprocess
from collections.abc import Callable, Collection
from pathlib import Path

from media_stack.config import (
    OUTPUT_DURATION_SLACK_FRAC, OUTPUT_DURATION_SLACK_S, PIPELINE_VERSION,
    STILL_IMAGE_VIDEO_CODECS,
)


CONSOLIDATED_TAG = "CONSOLIDATED_SUBS"
NORMALIZED_TAG = "NORMALIZED_AUDIO"


def file_key(p: Path) -> tuple[int, int]:
    """State-file cache key."""
    st = p.stat()
    return st.st_size, int(st.st_mtime)


def probe(p: Path) -> dict | None:
    """Parsed ffprobe JSON, or None on any failure."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", str(p)],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except Exception:
        return None


def parse_duration(value: object) -> float | None:
    """Finite seconds > 0 from an ffprobe field ("N/A", zero, garbage: None)."""
    if isinstance(value, bool):  # float(True) == 1.0 would pass for a duration
        return None
    try:
        d = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return d if math.isfinite(d) and d > 0 else None


def duration_shortfall(
    src_info: dict | None, out_info: dict | None, log: Callable[[str], None],
) -> str | None:
    """Why `out_info` may not replace `src_info` (both `probe()` results), else None: either
    file unprobeable, or the output's container duration shorter than the source's by more than
    the config slack (a remux that stopped early once replaced full episodes). A readable source
    with an unknown or zero duration skips the check (logged); an output with none fails it."""
    if src_info is None:
        return "source could not be probed"
    if out_info is None:
        return "output could not be probed"
    src = parse_duration((src_info.get("format") or {}).get("duration"))
    if src is None:
        log("duration check SKIPPED: source duration unknown")
        return None
    out = parse_duration((out_info.get("format") or {}).get("duration"))
    if out is None:
        return f"output duration unreadable (source is {src:.1f}s)"
    slack = max(OUTPUT_DURATION_SLACK_S, OUTPUT_DURATION_SLACK_FRAC * src)
    if out < src - slack:
        return f"output is {out:.1f}s, source {src:.1f}s (over {slack:.1f}s shorter)"
    return None


def audio_stream_duration(
    streams: object, preferred_index: int | None = None,
    allowed_indices: Collection[int] | None = None,
) -> float | None:
    """Duration from an AUDIO stream only (a cover-art video stream's 5 s says nothing about
    the feature), preferring `preferred_index`. `allowed_indices` (the non-commentary audio)
    keeps a short commentary track from standing in."""
    if not isinstance(streams, list):
        return None
    audio = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"]
    if allowed_indices is not None:
        audio = [s for s in audio if s.get("index") in allowed_indices]
    audio.sort(key=lambda s: s.get("index") != preferred_index)  # stable: preferred first
    for s in audio:
        d = parse_duration(s.get("duration"))
        if d is not None:
            return d
    return None


def probe_duration(
    p: Path, audio_index: int | None = None, allowed_indices: Collection[int] | None = None,
) -> float | None:
    """Fallback when `probe()` had no usable duration: container, else an audio stream's."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=index,codec_type,duration",
             "-of", "json", str(p)],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        d = parse_duration((data.get("format") or {}).get("duration"))
        if d is not None:
            return d
        return audio_stream_duration(data.get("streams"), audio_index, allowed_indices)
    except Exception:
        return None


def ffprobe_strict(path: Path) -> dict:
    """ffprobe JSON; raises RuntimeError on failure."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


def already_processed(info: dict) -> bool:
    """Carries CONSOLIDATED_SUBS=v<PIPELINE_VERSION>."""
    fmt = info.get("format") or {}
    tags = fmt.get("tags") or {}
    want = f"v{PIPELINE_VERSION}"
    for k, v in tags.items():
        if k.upper() == CONSOLIDATED_TAG and str(v) == want:
            return True
    return False


def already_normalized(info: dict, audio_version: int = 1) -> bool:
    """Carries NORMALIZED_AUDIO=v<audio_version>."""
    fmt = info.get("format") or {}
    tags = fmt.get("tags") or {}
    want = f"v{audio_version}"
    for k, v in tags.items():
        if k.upper() == NORMALIZED_TAG and str(v) == want:
            return True
    return False


def primary_audio_stream(info: dict) -> dict | None:
    """Default-flagged audio stream, else the first."""
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        return None
    for s in audio:
        if (s.get("disposition") or {}).get("default"):
            return s
    return audio[0]


def real_video_streams(streams: list[dict]) -> list[dict]:
    """Drops `attached_pic` cover art and still-image codecs muxed as bare video streams:
    players bind to such a thumbnail and stall at 0s, so every muxing step must drop them."""
    return [
        s for s in streams
        if s.get("codec_type") == "video"
        and not (s.get("disposition") or {}).get("attached_pic")
        and s.get("codec_name") not in STILL_IMAGE_VIDEO_CODECS
    ]
