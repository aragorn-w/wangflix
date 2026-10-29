"""EBU R128 -23 LUFS two-pass loudnorm + Atmos downmix (inner loop of normalize-audio.py)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from media_stack.mux import remux_timeout_s
from media_stack.probe import ffprobe_strict, real_video_streams


# Changing these needs a re-tag of every already-normalized file.
TARGET_I = -23.0
TARGET_LRA = 7.0
TARGET_TP = -2.0

AAC_BITRATE: dict[int, str] = {1: "128k", 2: "192k", 6: "384k", 8: "512k"}
DEFAULT_AAC_BITRATE = "256k"

# The Shield's AAC decoder tops out at 48 kHz. A source rate is kept only if the native
# AAC encoder accepts it (an odd one such as 44056 makes ffmpeg fail).
MAX_AAC_SAMPLE_RATE = 48000
AAC_SAMPLE_RATES = frozenset(
    {48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350})

# Slack over the per-step budgets so the outer cap never fires inside a step's own.
NORMALIZE_MARGIN_S = 600


def pass1_timeout_s(size_bytes: int) -> int:
    """Pass-1 budget: 1h floor + ~1 min per 100 MB (a fixed 1h truncated 80GB+ UHD rips)."""
    return int(max(3600, size_bytes / 1e6 * 0.6 + 1800))


def pass2_timeout_s(size_bytes: int) -> int:
    """Pass-2 budget: 60-min floor, scaled up by size."""
    return int(max(3600, size_bytes / 1e6 / 5 + 1800))


def normalize_budget_s(size_bytes: int) -> int:
    """Outer cap for the inline normalize call in consolidate-subs.py: all step budgets
    back to back plus the margin."""
    return (pass1_timeout_s(size_bytes) + pass2_timeout_s(size_bytes)
            + remux_timeout_s(size_bytes) + NORMALIZE_MARGIN_S)


def measure_loudness(src: Path, audio_index: int) -> dict:
    """Pass 1: loudnorm analysis on `0:a:audio_index`. Returns the measured dict
    (`input_i/input_tp/input_lra/input_thresh/target_offset`); raises RuntimeError."""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(src),
        "-map", f"0:a:{audio_index}",
        "-af", f"loudnorm=I={TARGET_I}:LRA={TARGET_LRA}:TP={TARGET_TP}:print_format=json",
        "-f", "null", "-",
    ]
    timeout = pass1_timeout_s(src.stat().st_size)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"loudnorm pass1 failed: {r.stderr.strip()[-200:]}")
    # The json blob is on stderr.
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.DOTALL)
    if not m:
        raise RuntimeError("loudnorm pass1: json block missing")
    data = json.loads(m.group(0))
    for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        if k not in data:
            raise RuntimeError(f"loudnorm pass1: missing key {k}")
    return data


def _aac_sample_rate(stream: dict) -> int:
    """Source rate if AAC accepts it, else MAX_AAC_SAMPLE_RATE."""
    try:
        rate = int(stream.get("sample_rate") or 0)
    except (TypeError, ValueError):
        return MAX_AAC_SAMPLE_RATE
    return rate if rate in AAC_SAMPLE_RATES else MAX_AAC_SAMPLE_RATE


def render_normalized(
    src: Path,
    dst: Path,
    audio_index: int,
    channels: int,
    measured: dict,
    channel_layout: str = "",
) -> None:
    """Pass 2: re-encode the primary audio track (`audio_index` is audio-relative) with the
    pass-1 values; stream-copy everything else except rejected video and data streams.

    Atmos / >6ch height layouts (e.g. `FL+FR+FC+LFE+SL+SR+TFL+TFR`) are rejected by the
    native AAC encoder, so they are downmixed to 5.1.
    """
    needs_downmix = channels > 6 and (
        "TFL" in channel_layout or "TFR" in channel_layout
        or "TBL" in channel_layout or "TBR" in channel_layout
        or "WL" in channel_layout or "WR" in channel_layout
        or channels > 8  # exotic
    )
    channels_out = 6 if needs_downmix else channels
    bitrate = AAC_BITRATE.get(channels_out, DEFAULT_AAC_BITRATE)
    af = (
        f"loudnorm=I={TARGET_I}:LRA={TARGET_LRA}:TP={TARGET_TP}"
        f":measured_I={measured['input_i']}"
        f":measured_LRA={measured['input_lra']}"
        f":measured_TP={measured['input_tp']}"
        f":measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}"
        ":linear=true:print_format=summary"
    )
    # Real motion video only: a bare `-map 0:v` would carry still-image cover art (no
    # attached_pic flag) through a standalone pass, and players then stall at 0s.
    probe_streams = ffprobe_strict(src).get("streams", [])
    keep_video = real_video_streams(probe_streams)
    if not keep_video:
        raise RuntimeError("loudnorm pass2: no real video stream to map")
    video_maps: list[str] = []
    for _vs in keep_video:
        video_maps += ["-map", f"0:{_vs['index']}"]

    # Map EVERY audio stream, re-encoding only the primary: track selection is
    # consolidate-subs' job, and mapping only the primary would drop a kept dub.
    audio_streams = [s for s in probe_streams if s.get("codec_type") == "audio"]
    if not 0 <= audio_index < len(audio_streams):
        raise RuntimeError(
            f"loudnorm pass2: audio_index {audio_index} out of range "
            f"({len(audio_streams)} audio stream(s))"
        )
    audio_maps: list[str] = []
    audio_args: list[str] = []
    for pos in range(len(audio_streams)):
        # Maps are in input order, so output `pos` == input `pos`. Per-stream specifiers:
        # a bare `-c:a`/`-af`/`-ac` would also hit the copied tracks.
        audio_maps += ["-map", f"0:a:{pos}"]
        if pos == audio_index:
            # loudnorm outputs 192 kHz and AAC would snap to 96 kHz, which the Shield
            # cannot decode: pin the rate.
            audio_args += [f"-c:a:{pos}", "aac", f"-b:a:{pos}", bitrate,
                           f"-filter:a:{pos}", af,
                           f"-ar:a:{pos}", str(_aac_sample_rate(audio_streams[pos]))]
            if needs_downmix:
                audio_args += [f"-ac:a:{pos}", str(channels_out)]
        else:
            audio_args += [f"-c:a:{pos}", "copy"]

    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-y", "-i", str(src),
        *video_maps,
        *audio_maps,
        "-map", "0:s?",
        "-map", "0:t?",
        "-map_chapters", "0",
        "-map_metadata", "0",
        "-c:v", "copy",
        "-c:s", "copy",
        "-c:t", "copy",
        *audio_args,
        "-f", "matroska", str(dst),
    ]
    timeout = pass2_timeout_s(src.stat().st_size)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-1:] or [""]
        raise RuntimeError(f"loudnorm pass2 failed: {tail[0][:200]}")
    if not dst.exists() or dst.stat().st_size < 100_000:
        raise RuntimeError("pass2 output too small")


def fast_measure_ebur128(src: Path, audio_index: int) -> dict:
    """--measure-only survey: ~7x faster than `measure_loudness` (raw ebur128 filter, no
    loudnorm); parses the stderr Summary block."""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(src),
        "-map", f"0:a:{audio_index}",
        "-af", "ebur128=peak=true",
        "-f", "null", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"ebur128 failed: {r.stderr.strip()[-200:]}")
    text = r.stderr

    def grab(pat: str) -> float:
        m = re.search(pat, text)
        if not m:
            raise RuntimeError(f"ebur128: missing {pat}")
        return float(m.group(1))

    return {
        "input_i":   grab(r"Integrated loudness:\s*\n\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS"),
        "input_lra": grab(r"Loudness range:\s*\n\s*LRA:\s*(-?\d+(?:\.\d+)?)\s*LU"),
        "input_tp":  grab(r"True peak:\s*\n\s*Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS"),
    }
