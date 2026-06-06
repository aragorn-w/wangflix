"""ffprobe wrappers and stream-metadata helpers.

`probe()` is the lenient form (returns None on failure) used by the
consolidate-subs sweep where some files might legitimately fail to
probe.  `ffprobe_strict()` is the raise-on-fail form used by
normalize-audio where any probe failure on a queued file is a real
error.

Stream-classification helpers (already_processed, already_normalized,
file_key, primary_audio_stream) live here too so audio.py / mux.py /
loudness.py don't need to duplicate them.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from media_stack.config import PIPELINE_VERSION


CONSOLIDATED_TAG = "CONSOLIDATED_SUBS"
NORMALIZED_TAG = "NORMALIZED_AUDIO"


def file_key(p: Path) -> tuple[int, int]:
    """`(size, mtime_int)` fingerprint used as the state-file cache key.
    Files whose fingerprint matches their stored value skip re-probing."""
    st = p.stat()
    return st.st_size, int(st.st_mtime)


def probe(p: Path) -> dict | None:
    """Lenient ffprobe.  Returns the parsed JSON or None on any failure
    (subprocess error, JSON parse error, timeout).  Used by sweeps that
    must tolerate the occasional unreadable file."""
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


def ffprobe_strict(path: Path) -> dict:
    """Strict ffprobe.  Raises RuntimeError on failure.  Used by
    normalize-audio's single-file invocations where any probe failure
    is a fatal condition for that file."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


def already_processed(info: dict) -> bool:
    """True iff the probed file already carries CONSOLIDATED_SUBS=v<PIPELINE_VERSION>."""
    fmt = info.get("format") or {}
    tags = fmt.get("tags") or {}
    want = f"v{PIPELINE_VERSION}"
    for k, v in tags.items():
        if k.upper() == CONSOLIDATED_TAG and str(v) == want:
            return True
    return False


def already_normalized(info: dict, audio_version: int = 1) -> bool:
    """True iff the probed file already carries NORMALIZED_AUDIO=v<audio_version>."""
    fmt = info.get("format") or {}
    tags = fmt.get("tags") or {}
    want = f"v{audio_version}"
    for k, v in tags.items():
        if k.upper() == NORMALIZED_TAG and str(v) == want:
            return True
    return False


def primary_audio_stream(info: dict) -> dict | None:
    """Pick the canonical audio stream from a probed dict.

    Consolidate-subs v2 collapsed to a single primary audio stream and
    forced default-flag on it, so prefer the default-flagged track if
    present, else the first audio stream.  Returns None for files with
    no audio."""
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        return None
    for s in audio:
        if (s.get("disposition") or {}).get("default"):
            return s
    return audio[0]
