"""Subtitle scoring, cleaning, extraction, and sidecar discovery.

The consolidate-subs pipeline picks the best English subtitle from
(possibly several) embedded tracks + (possibly several) sidecar files.
This module owns the scoring rules + the text-cleanup pipeline that
runs before the chosen sub gets re-attached.

The pipeline:
  1. For each candidate sub track: probe with ffprobe → score
  2. For each sidecar file: classify forced/SDH → score
  3. Extract the winner, clean its text, optionally ffsubsync against
     the video for timing alignment
  4. Hand off to mux.py for the final mkvmerge invocation

Score philosophy:
  - text > image (text never needs OCR + always renders)
  - SDH > non-SDH (more completeness)
  - non-forced > forced (forced subs are signs-only)
  - srt > mov_text > ass (player compatibility)
  - longer = more dialogue covered
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from media_stack.config import (
    ASS_OVERRIDE_RE, ENG_SIDECAR_SUFFIXES, HTML_FONT_RE, HTML_OTHER_RE,
    LITERAL_NH_RE, MULTI_BLANK_RE, SIDECAR_EXTS, TEXT_CODECS, TRAIL_WS_RE,
)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def is_forced_track(s: dict) -> bool:
    """True iff the subtitle stream is flagged forced (Matroska
    `forced` disposition or `forced` in the track title)."""
    disp = s.get("disposition") or {}
    title = (s.get("tags") or {}).get("title") or ""
    return bool(disp.get("forced")) or "forced" in title.lower()


def is_sdh_track(s: dict) -> bool:
    """True iff the track title mentions SDH / CC / hearing-impaired."""
    title = (s.get("tags") or {}).get("title") or ""
    t = title.lower()
    return any(k in t for k in ("sdh", "cc", "(cc)", "hearing", "hi"))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_text_track(s: dict, line_count: int) -> int:
    """Score a TEXT subtitle stream.  Higher = preferred."""
    score = 1000  # text base
    if s.get("codec_name") == "subrip":
        score += 200
    elif s.get("codec_name") == "mov_text":
        score += 100
    if is_sdh_track(s):
        score += 150  # prefer SDH/CC for completeness
    if not is_forced_track(s):
        score += 50
    # more lines == more dialogue covered
    score += min(line_count, 2000) // 5
    return score


def score_image_track(s: dict) -> int:
    """Score an IMAGE subtitle stream (PGS, VOBSUB, etc.).  Always
    lower than any text track because image subs need OCR for editing
    and don't play back on every device."""
    score = 100  # image base (lower than any text)
    if is_sdh_track(s):
        score += 50
    if not is_forced_track(s):
        score += 10
    return score


# ---------------------------------------------------------------------------
# Extraction + format conversion
# ---------------------------------------------------------------------------

def extract_track(src: Path, idx: int, codec: str, dst: Path) -> bool:
    """Stream-copy subtitle track `idx` from `src` to `dst`.  Only
    works for codecs in TEXT_CODECS (image subs need a separate path).
    Returns True on success."""
    if codec not in TEXT_CODECS:
        return False
    ext_codec = "srt" if codec != "ass" else "ass"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src),
           "-map", f"0:{idx}", "-c:s", ext_codec, str(dst)]
    # Subtitle extraction is metadata-stream copy, fast even on huge files,
    # but heavy parallel I/O on HDDs can starve it.  Allow generous timeout.
    try:
        size_mb = src.stat().st_size / 1e6
        # Under N-way mergerfs HDD contention, even a stream-copy extract
        # can take 10-30 min on a 4K rip because matroska seek requires
        # walking the segment.  15-min floor + linear term safely covers UHD.
        timeout = max(900, int(size_mb / 30 + 600))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, Exception):
        return False
    return r.returncode == 0 and dst.exists() and dst.stat().st_size > 0


def ass_to_srt(ass_path: Path, srt_path: Path) -> bool:
    """Convert ASS/SSA to SRT via pysubs2."""
    try:
        import pysubs2
        subs = pysubs2.load(str(ass_path))
        subs.save(str(srt_path), format_="srt")
        return srt_path.exists() and srt_path.stat().st_size > 0
    except Exception:
        return False


def count_subtitle_lines(p: Path) -> int:
    """Count the dialogue lines in a sub file (ASS or SRT)."""
    try:
        if p.suffix.lower() == ".ass":
            import pysubs2
            return len(pysubs2.load(str(p)))
        else:
            import srt
            with p.open(encoding="utf-8", errors="replace") as fh:
                return sum(1 for _ in srt.parse(fh.read()))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Timing sync
# ---------------------------------------------------------------------------

def run_ffsubsync(video: Path, srt_in: Path, srt_out: Path) -> bool:
    """Align `srt_in` to `video` audio via ffsubsync, writing `srt_out`.
    Returns True on success."""
    try:
        r = subprocess.run(
            ["ffsubsync", str(video), "-i", str(srt_in), "-o", str(srt_out)],
            capture_output=True, text=True, timeout=600,
        )
        return r.returncode == 0 and srt_out.exists() and srt_out.stat().st_size > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Text cleaning + ALL-CAPS recasing
# ---------------------------------------------------------------------------

_SENT_BOUNDARY_RE = re.compile(r"(^|[.!?…]\s+|[.!?…]['\"”’]\s+)([a-z])")
_LONE_I_RE       = re.compile(r"(?<![A-Za-z])i(?![A-Za-z])")
_I_CONTR_RE      = re.compile(r"(?<![A-Za-z])i'(m|ve|d|ll|s|re)(?![A-Za-z])")
_HAS_LOWER_RE    = re.compile(r"[a-z]")
_LETTERS_RE      = re.compile(r"[A-Za-z]")
_TIMING_RE       = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d")


def _smart_recase(line: str) -> str:
    """Recase a single ALL-CAPS line to sentence case.  Skips lines
    that already have lowercase (already cased), and very short lines
    (<6 letters)."""
    if _HAS_LOWER_RE.search(line):
        return line
    if len(_LETTERS_RE.findall(line)) < 6:
        return line
    cased = line.lower()
    cased = _SENT_BOUNDARY_RE.sub(lambda m: m.group(1) + m.group(2).upper(), cased)
    # Capitalize the first alphabetic character of the line (handles
    # dialogue dashes "- hey", music notes "♪ hey", etc.).
    for i, ch in enumerate(cased):
        if ch.isalpha():
            if ch.islower():
                cased = cased[:i] + ch.upper() + cased[i+1:]
            break
    cased = _LONE_I_RE.sub("I", cased)
    cased = _I_CONTR_RE.sub(lambda m: "I'" + m.group(1), cased)
    return cased


def normalize_subtitle_caps(text: str) -> str:
    """Recase ALL-CAPS subtitle dialogue.  Skips index numbers and
    timing lines so the structure of the SRT stays intact."""
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if not s or s.isdigit() or _TIMING_RE.match(line) or "-->" in s:
            out.append(line)
        else:
            out.append(_smart_recase(line))
    return "\n".join(out)


def clean_srt_text(text: str) -> str:
    """Strip ASS override tags, HTML, literal \\h, trailing whitespace,
    excessive blank lines; recase ALL-CAPS; ensure trailing newline."""
    text = text.lstrip("﻿")  # BOM
    text = ASS_OVERRIDE_RE.sub("", text)
    text = LITERAL_NH_RE.sub(" ", text)
    text = HTML_FONT_RE.sub("", text)
    text = HTML_OTHER_RE.sub("", text)
    text = TRAIL_WS_RE.sub("", text)
    text = MULTI_BLANK_RE.sub("\n\n", text)
    text = normalize_subtitle_caps(text)
    if not text.endswith("\n"):
        text += "\n"
    return text


# ---------------------------------------------------------------------------
# Sidecar discovery + subliminal fallback
# ---------------------------------------------------------------------------

def find_sidecar_subs(media: Path) -> list[tuple[Path, bool, bool]]:
    """Find English sidecar subs (.srt/.ass/.ssa/.vtt) next to `media`.
    Returns `[(path, is_forced, is_sdh)]`.

    Bare sidecars (e.g. `Movie.srt` for `Movie.mkv`) are accepted as
    English — Bazarr's default output uses an `.en.srt` suffix, but
    user-dropped sidecars often omit the language code.  Codex round-
    module-split-2 #4 caught that the prior `rsplit(".", 1)` path
    skipped bare sidecars entirely because `rest == "srt"` produced
    only one part.
    """
    base = media.stem
    out = []
    for entry in media.parent.iterdir():
        if not entry.is_file() or entry == media:
            continue
        n = entry.name
        if not n.lower().endswith(SIDECAR_EXTS):
            continue
        if not n.startswith(base):
            continue
        rest = n[len(base):].lstrip(".").lower()
        # rest looks like "en.srt", "en.cc.srt", "en.sdh.srt", or
        # just "srt" (bare sidecar — assumed English).
        if "." not in rest:
            # Bare basename sidecar: just the extension, no lang tag.
            # Treat as plain English, non-forced, non-SDH.
            out.append((entry, False, False))
            continue
        parts = rest.rsplit(".", 1)
        tags = parts[0].split(".")
        if (not any(t in ENG_SIDECAR_SUFFIXES or t == "" for t in tags)
                and not any(t.startswith("en") for t in tags)):
            continue
        is_forced = any("forced" in t for t in tags)
        is_sdh = any(t in ("sdh", "cc", "hi") for t in tags)
        out.append((entry, is_forced, is_sdh))
    return out


def fetch_via_subliminal(video: Path, workdir: Path) -> Path | None:
    """Download an English SRT via subliminal into `workdir`.

    Used as a fallback for image-sub-only files where Bazarr's async
    sidecar drop hasn't delivered (or won't).  Inline fetch closes the
    gap so files don't sit NEEDS_BAZARR indefinitely.  Caller scores
    and feeds the result through the normal cleanup + ffsubsync +
    remux pipeline.

    Returns the downloaded srt path or None on failure / no-result /
    too-small-to-trust output.
    """
    fetch_dir = workdir / "subliminal"
    fetch_dir.mkdir(exist_ok=True)
    try:
        r = subprocess.run(
            ["subliminal", "download", "-l", "en",
             "--force-embedded-subtitles",
             "-d", str(fetch_dir), str(video)],
            capture_output=True, text=True, timeout=180,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if r.returncode != 0:
        return None
    srt = fetch_dir / f"{video.stem}.en.srt"
    if not srt.exists() or srt.stat().st_size < 1000:
        return None
    return srt
