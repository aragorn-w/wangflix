"""Subtitle scoring, cleaning, extraction, and sidecar discovery for consolidate-subs.

Preferences: text > image (no OCR); non-SDH > SDH (consolidate-subs.py ranks SDH last,
used only as sole candidate); non-forced > forced (signs-only); srt > mov_text > ass;
more lines.
"""

from __future__ import annotations

import codecs
import re
import subprocess
from pathlib import Path

from media_stack.config import (
    ASS_CODECS, ASS_OVERRIDE_RE, ENG_SIDECAR_SUFFIXES, HTML_FONT_RE, HTML_OTHER_RE,
    LITERAL_NH_RE, MULTI_BLANK_RE, SIDECAR_EXTS, TEXT_CODECS, TRAIL_WS_RE,
)


def is_forced_track(s: dict) -> bool:
    """Forced disposition, or `forced` in the track title."""
    disp = s.get("disposition") or {}
    title = (s.get("tags") or {}).get("title") or ""
    return bool(disp.get("forced")) or "forced" in title.lower()


def is_generated_track(s: dict) -> bool:
    """Title marks it machine-generated (`English (generated)`, Whisper subs)."""
    title = (s.get("tags") or {}).get("title") or ""
    return "(generated)" in title.lower()


# Whole-token match: a substring test misfires on "Chinese", "Hindi", "this".
_HI_RE = re.compile(
    r"(?<![a-z0-9])(?:sdh|cc|hi"
    r"|hearing[^a-z0-9]+impaired"
    r"|hard[^a-z0-9]+of[^a-z0-9]+hearing"
    r"|closed[^a-z0-9]+captions?)(?![a-z0-9])")


def is_hi_text(text: str) -> bool:
    """`text` (track title or joined sidecar tags) has a hearing-impaired marker."""
    return _HI_RE.search(text.lower()) is not None


def is_sdh_track(s: dict) -> bool:
    """`hearing_impaired` disposition, or a marker in the title (`_HI_RE`)."""
    if (s.get("disposition") or {}).get("hearing_impaired"):
        return True
    return is_hi_text((s.get("tags") or {}).get("title") or "")


def score_text_track(s: dict, line_count: int) -> int:
    """Score a TEXT subtitle stream; higher is preferred."""
    score = 1000  # text base
    if s.get("codec_name") == "subrip":
        score += 200
    elif s.get("codec_name") == "mov_text":
        score += 100
    # No SDH bonus: consolidate-subs.py's sort key already ranks non-SDH first.
    if not is_forced_track(s):
        score += 50
    score += min(line_count, 2000) // 5
    return score


def extract_track(src: Path, idx: int, codec: str, dst: Path) -> bool:
    """Extract TEXT_CODECS track `idx` to `dst`; True on success. ASS/SSA is written
    natively (`dst` ends in `.ass`) so the caller can gate raw bytes before `ass_to_srt`;
    other codecs come out as SRT."""
    if codec not in TEXT_CODECS:
        return False
    ext_codec = "ass" if codec in ASS_CODECS else "srt"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(src),
           "-map", f"0:{idx}", "-c:s", ext_codec, str(dst)]
    try:
        size_mb = src.stat().st_size / 1e6
        # A 4K rip's extract can take 10-30 min under HDD contention.
        timeout = max(900, int(size_mb / 30 + 600))
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, Exception):
        return False
    return r.returncode == 0 and dst.exists() and dst.stat().st_size > 0


def ass_to_srt(ass_path: Path, srt_path: Path) -> bool:
    """ASS/SSA to SRT via pysubs2."""
    try:
        import pysubs2
        subs = pysubs2.load(str(ass_path))
        subs.save(str(srt_path), format_="srt")
        return srt_path.exists() and srt_path.stat().st_size > 0
    except Exception:
        return False


def count_subtitle_lines(p: Path) -> int:
    """Dialogue line count of an ASS or SRT file."""
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


def decode_subtitle_bytes(data: bytes) -> str:
    """Decode for language classification (never raises): BOM first; BOM-less UTF-16
    (>= 1/4 NUL bytes) by which byte positions hold the NULs; else UTF-8, then cp1252."""
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig", errors="replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace")  # consumes the BOM
    sample = data[:4096]
    if sample and sample.count(0) * 4 >= len(sample):
        be = sample[0::2].count(0) > sample[1::2].count(0)
        return data.decode("utf-16-be" if be else "utf-16-le", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


# More U+FFFD than this means the bytes were not text in any known encoding.
_MAX_REPLACEMENT_FRACTION = 0.01


def decode_subtitle_text(data: bytes) -> str | None:
    """`decode_subtitle_bytes`, or None if it holds a NUL or > 1% U+FFFD."""
    text = decode_subtitle_bytes(data)
    if "\x00" in text:
        return None
    if text and text.count("\ufffd") > len(text) * _MAX_REPLACEMENT_FRACTION:
        return None
    return text


def run_ffsubsync(video: Path, srt_in: Path, srt_out: Path) -> bool:
    """Align `srt_in` to `video` audio via ffsubsync."""
    try:
        r = subprocess.run(
            ["ffsubsync", str(video), "-i", str(srt_in), "-o", str(srt_out)],
            capture_output=True, text=True, timeout=600,
        )
        return r.returncode == 0 and srt_out.exists() and srt_out.stat().st_size > 0
    except Exception:
        return False


_SENT_BOUNDARY_RE = re.compile(r"(^|[.!?…]\s+|[.!?…]['\"”’]\s+)([a-z])")
_LONE_I_RE       = re.compile(r"(?<![A-Za-z])i(?![A-Za-z])")
_I_CONTR_RE      = re.compile(r"(?<![A-Za-z])i'(m|ve|d|ll|s|re)(?![A-Za-z])")
_HAS_LOWER_RE    = re.compile(r"[a-z]")
_LETTERS_RE      = re.compile(r"[A-Za-z]")
_TIMING_RE       = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d")


def _smart_recase(line: str) -> str:
    """Sentence-case an ALL-CAPS line; lines with lowercase or < 6 letters pass through."""
    if _HAS_LOWER_RE.search(line):
        return line
    if len(_LETTERS_RE.findall(line)) < 6:
        return line
    cased = line.lower()
    cased = _SENT_BOUNDARY_RE.sub(lambda m: m.group(1) + m.group(2).upper(), cased)
    for i, ch in enumerate(cased):
        if ch.isalpha():
            if ch.islower():
                cased = cased[:i] + ch.upper() + cased[i+1:]
            break
    cased = _LONE_I_RE.sub("I", cased)
    cased = _I_CONTR_RE.sub(lambda m: "I'" + m.group(1), cased)
    return cased


def normalize_subtitle_caps(text: str) -> str:
    """Recase ALL-CAPS dialogue; index and timing lines untouched."""
    out = []
    for line in text.split("\n"):
        s = line.strip()
        if not s or s.isdigit() or _TIMING_RE.match(line) or "-->" in s:
            out.append(line)
        else:
            out.append(_smart_recase(line))
    return "\n".join(out)


def clean_srt_text(text: str) -> str:
    """Strip ASS/HTML markup and literal \\h, collapse blanks, recase ALL-CAPS."""
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


_VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".m4v")


def _english_sidecar_tags(name: str, stem: str) -> list[str] | None:
    """Tags between `stem` and the extension ([] for a bare `<stem>.srt`) if `name` is an
    English sidecar of that stem (dot boundary; no tags, or one starting `en`), else None."""
    if not name.lower().endswith(SIDECAR_EXTS) or not name.startswith(stem + "."):
        return None
    rest = name[len(stem):].lstrip(".").lower()
    if "." not in rest:
        return []  # bare: just the extension, no language tag
    tags = rest.rsplit(".", 1)[0].split(".")
    if (not any(t in ENG_SIDECAR_SUFFIXES or t == "" for t in tags)
            and not any(t.startswith("en") for t in tags)):
        return None
    return tags


def find_sidecar_subs(media: Path) -> list[tuple[Path, bool, bool]]:
    """English sidecars next to `media` as `[(path, is_forced, is_sdh)]`.

    A `generated` tag (sub-fallback.py Whisper output) counts as plain English; see
    `is_generated_sidecar`. A sidecar must start with the stem plus a dot (`Movie2.en.srt`
    is not `Movie.mkv`'s). A sidecar that is a valid English sidecar of a longer-stemmed
    sibling video (`Movie.2020.mkv`) belongs to that video, and only then
    (`Show.en.generated.srt` stays `Show.mkv`'s beside `Show.en.mkv`; `consolidate-watch.sh`
    applies the same rule). Bare `Movie.srt` is accepted as English.
    """
    base = media.stem
    entries = list(media.parent.iterdir())
    longer_stems = [e.stem for e in entries
                    if e != media and e.suffix.lower() in _VIDEO_EXTS
                    and e.stem.startswith(base + ".") and e.is_file()]
    out = []
    for entry in entries:
        if not entry.is_file() or entry == media:
            continue
        tags = _english_sidecar_tags(entry.name, base)
        if tags is None:
            continue
        if any(_english_sidecar_tags(entry.name, longer) is not None for longer in longer_stems):
            continue  # the longer-stemmed sibling video owns it
        if not tags:
            out.append((entry, False, False))
            continue
        is_forced = any("forced" in t for t in tags)
        is_sdh = is_hi_text(" ".join(tags))
        out.append((entry, is_forced, is_sdh))
    return out


def is_generated_sidecar(media: Path, sidecar: Path) -> bool:
    """`sidecar` has the `generated` tag; only tags after `media`'s stem count."""
    rest = sidecar.name[len(media.stem):].lstrip(".").lower()
    if "." not in rest:
        return False
    return "generated" in rest.rsplit(".", 1)[0].split(".")


def fetch_via_subliminal(video: Path, workdir: Path) -> Path | None:
    """English SRT via subliminal into `workdir` for image-sub-only files Bazarr hasn't
    served. None on failure or output too small to trust."""
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
