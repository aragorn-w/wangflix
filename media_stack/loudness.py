"""EBU R128 -23 LUFS two-pass loudnorm + Atmos downmix.

Per-file flow (the inner loop of normalize-audio.py):
  pass 1: measure_loudness — ffmpeg loudnorm analysis (json output)
  pass 2: render_normalized — ffmpeg re-encode with measured values
          + linear=true so peaks aren't clipped, stream-copy
          everything else, mkv intermediate
  then:   mkvmerge re-mux (mux.mkvmerge_remux_simple) for the proper
          cue index, mkvpropedit (tags.set_normalized_tag) for the
          idempotency tag, atomic rename over the original.

Survey/triage:
  fast_measure_ebur128 — same input I/LRA/TP, ~7x faster than pass1
                         because it skips the loudnorm filter.  Used
                         by --measure-only to scan a library before
                         actually editing.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from media_stack.probe import ffprobe_strict, real_video_streams


# EBU R128 broadcast targets.  Don't change without re-tagging every
# already-normalized file in the library (current count: 214 movies +
# 23 TV shows).
TARGET_I = -23.0
TARGET_LRA = 7.0
TARGET_TP = -2.0

# Re-encode bitrates by channel count (native ffmpeg aac, OK quality).
AAC_BITRATE: dict[int, str] = {1: "128k", 2: "192k", 6: "384k", 8: "512k"}
DEFAULT_AAC_BITRATE = "256k"


def measure_loudness(src: Path, audio_index: int) -> dict:
    """Pass 1: loudnorm analysis on `0:a:audio_index`.

    Returns the measured dict with keys
    `input_i / input_tp / input_lra / input_thresh / target_offset`.
    Raises RuntimeError on ffmpeg failure or missing JSON block."""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(src),
        "-map", f"0:a:{audio_index}",
        "-af", f"loudnorm=I={TARGET_I}:LRA={TARGET_LRA}:TP={TARGET_TP}:print_format=json",
        "-f", "null", "-",
    ]
    # Scale timeout with file size: 1h floor + ~1 min per 100 MB.  Covers
    # 80GB+ UHD Atmos rips on HDD/mergerfs which a fixed-1h ceiling
    # truncated mid-analysis on 4-way parallel sweeps.
    size_mb = src.stat().st_size / 1e6
    timeout = int(max(3600, size_mb * 0.6 + 1800))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"loudnorm pass1 failed: {r.stderr.strip()[-200:]}")
    # ffmpeg writes the json blob to stderr at the end of the run.
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.DOTALL)
    if not m:
        raise RuntimeError("loudnorm pass1: json block missing")
    data = json.loads(m.group(0))
    for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        if k not in data:
            raise RuntimeError(f"loudnorm pass1: missing key {k}")
    return data


def render_normalized(
    src: Path,
    dst: Path,
    audio_index: int,
    audio_codec: str,
    channels: int,
    measured: dict,
    channel_layout: str = "",
) -> None:
    """Pass 2: write dst with the primary audio track re-encoded via
    loudnorm using the pass-1 measured values.  Copied through: the video
    streams `real_video_streams()` accepts, EVERY other audio track,
    subtitles, attachments, chapters and metadata.  Not carried over:
    video that classifier rejects (still-image cover art, deliberately)
    and data streams, which are never mapped.  Single matroska output.
    `audio_index` is audio-relative (0 = first audio stream).

    For Atmos / non-standard >6ch layouts (e.g. `FL+FR+FC+LFE+SL+SR+TFL+TFR`
    on Mandalorian S3 Bluray), native AAC's encoder rejects the layout.
    We downmix to 5.1 in that case — loses the height channels but
    the loudness fix is the point, and AAC has no clean Atmos
    representation anyway.
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
    # Map real motion video only.  A bare `-map 0:v` also carries over any
    # still-image cover art muxed as a video stream (mjpeg/png with no
    # attached_pic flag) that is present in THIS input.  It cannot re-add a track
    # consolidate-subs already removed - consolidation replaces the file before
    # calling us - but a file normalized without that strip (a standalone pass, or
    # one whose consolidation was skipped) kept the malformed track, and players
    # then bound to the thumbnail and stalled at 0s.  Absolute stream indexes keep
    # the mapping unambiguous.
    probe_streams = ffprobe_strict(src).get("streams", [])
    keep_video = real_video_streams(probe_streams)
    if not keep_video:
        raise RuntimeError("loudnorm pass2: no real video stream to map")
    video_maps: list[str] = []
    for _vs in keep_video:
        video_maps += ["-map", f"0:{_vs['index']}"]

    # Map EVERY audio stream, re-encoding only the primary.  Track *selection* is
    # consolidate-subs' job, and it hands us the file it just built: for a
    # dual-audio title that file deliberately holds both the original-language
    # track and the English dub.  Mapping only `0:a:<primary>` here silently threw
    # the dub away again moments after consolidation kept it, and
    # _check_stream_regression never caught it because it used to count subtitles
    # and video but not audio (it counts audio now).  Secondary tracks are
    # stream-copied, so preserving them costs no quality and no encode time.
    audio_streams = [s for s in probe_streams if s.get("codec_type") == "audio"]
    if not 0 <= audio_index < len(audio_streams):
        raise RuntimeError(
            f"loudnorm pass2: audio_index {audio_index} out of range "
            f"({len(audio_streams)} audio stream(s))"
        )
    audio_maps: list[str] = []
    audio_args: list[str] = []
    for pos in range(len(audio_streams)):
        # Output audio stream `pos` == input audio stream `pos`, because the maps
        # below are emitted in input order.  The per-stream specifiers matter: a
        # bare `-c:a`/`-af`/`-ac` would also hit the copied tracks and either
        # re-encode or corrupt them.
        audio_maps += ["-map", f"0:a:{pos}"]
        if pos == audio_index:
            audio_args += [f"-c:a:{pos}", "aac", f"-b:a:{pos}", bitrate,
                           f"-filter:a:{pos}", af]
            if needs_downmix:
                audio_args += [f"-ac:a:{pos}", str(channels_out)]
        else:
            audio_args += [f"-c:a:{pos}", "copy"]

    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-y", "-i", str(src),
        *video_maps,
        *audio_maps,
        "-map", "0:s?",
        "-map", "0:t?",                     # attachments (fonts, etc.)
        "-map_chapters", "0",
        "-map_metadata", "0",
        "-c:v", "copy",
        "-c:s", "copy",
        "-c:t", "copy",
        *audio_args,
        "-f", "matroska", str(dst),
    ]
    # Pass2 re-encodes audio + stream-copies the rest.  3600s floor gives
    # 1080p re-encodes a 60-min budget; size term scales up for UHDs.
    size_mb = src.stat().st_size / 1e6
    timeout = int(max(3600, size_mb / 5 + 1800))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-1:] or [""]
        raise RuntimeError(f"loudnorm pass2 failed: {tail[0][:200]}")
    if not dst.exists() or dst.stat().st_size < 100_000:
        raise RuntimeError("pass2 output too small")


def fast_measure_ebur128(src: Path, audio_index: int) -> dict:
    """Survey-mode loudness measurement.

    ~7x faster than `measure_loudness` because it uses the raw ebur128
    filter, not loudnorm.  Parses the stderr Summary block for
    Integrated / Range / TruePeak.  Used by --measure-only sweeps that
    just want a per-file loudness fingerprint without committing to a
    full pass1 + pass2 rewrite.
    """
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
