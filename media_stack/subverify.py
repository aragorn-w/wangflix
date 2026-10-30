"""ASR-based English-subtitle verification for `sub-fallback.py` and `consolidate-subs.py`.

Subliminal can return the WRONG episode's subtitle, a foreign file can be named
`.en.srt`, and an UNTAGGED audio stream may not be English. This module runs speech
recognition on a window of the file's own English audio and compares it with the
candidate (`overlap()`); `media_stack.sublang` catches the language case, this catches
the wrong-episode case. `faster_whisper` is imported lazily.

Playback-aware extraction: `_extract_wav` calls an optional `should_abort` before
spawning ffmpeg and every `_ABORT_POLL_S` while it runs; on a hit it kills ffmpeg,
deletes the partial wav and raises `ExtractionInterrupted` (caller waits, retries).

CLI mode (`python3 -m media_stack.subverify transcribe <video> <audio_index> <model>
<threads>`): `sub-fallback.py` runs the full-file transcription as a subprocess for an
external wall-clock timeout. The child builds its own Jellyfin gate from the parent's
`JELLYFIN_URL`/`JELLYFIN_API_KEY` (a missing key is never "idle"). `SUB_FALLBACK_MAX_WAIT_MIN`
is the candidate's REMAINING cumulative allowance, checked after every poll. On every exit
the seconds spent waiting go to `CHILD_PLAYBACK_WAIT_REPORT_FILENAME` in `TMPDIR` so the
parent can deduct them. Exit codes: 0 success (SRT on stdout), 1 failure, 2 playback wait
exceeded (parent ends the run), 3 no `JELLYFIN_API_KEY`.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from media_stack.audio import is_commentary_audio
from media_stack.lang import is_english_tag

# 4+ letters only for `word_ratio`: short function words would inflate overlap.
_WORD_RE = re.compile(r"[a-z']+")
_MIN_OVERLAP_WORD_LEN = 4

_ALL_WORDS_RE = re.compile(r"[a-z']+")

_CUE_TIME_RE = re.compile(r"(\d+:\d+:\d+[,.]\d+)\s*-->\s*(\d+:\d+:\d+[,.]\d+)")

_ABORT_POLL_S = 5

CHILD_PLAYBACK_WAIT_REPORT_FILENAME = "child_playback_wait_spent_s.txt"


class ExtractionInterrupted(Exception):
    """`should_abort()` fired; the partial wav is deleted, retry from scratch."""


def english_audio_stream(probe_info: dict) -> int | None:
    """Audio stream index to run ASR against, commentary tracks excluded:
      1. explicitly tagged English; the `default`-flagged one wins, else the first.
      2. untagged / `und`, first match: UNVERIFIED (`sub-fallback.py` requires a passing
         `detect_language` first).
      3. `None`: all confidently non-English; skip rather than score it as a mismatch.
    """
    audio = [s for s in probe_info.get("streams", []) if s.get("codec_type") == "audio"]
    candidates = [s for s in audio if not is_commentary_audio(s)]
    if not candidates:
        return None
    explicit = [s for s in candidates
                if is_english_tag((s.get("tags") or {}).get("language"))]
    if explicit:
        default = next(
            (s for s in explicit if (s.get("disposition") or {}).get("default")), None)
        return (default or explicit[0])["index"]
    for s in candidates:
        lang = ((s.get("tags") or {}).get("language") or "").strip().lower()
        if lang in ("", "und"):
            return s["index"]
    return None


def is_audio_explicitly_english(probe_info: dict, audio_index: int) -> bool:
    """Stream at `audio_index` has an explicit English tag (tier 1 above)."""
    for s in probe_info.get("streams", []):
        if s.get("codec_type") == "audio" and s.get("index") == audio_index:
            return is_english_tag((s.get("tags") or {}).get("language"))
    return False


def _extract_wav(video: Path, audio_index: int, start_s: float | None,
                  dur_s: float | None, *, workdir: Path | None = None,
                  should_abort: Callable[[], bool] | None = None) -> Path:
    """16 kHz mono wav of `audio_index` in a fresh temp file (`None` start/dur = whole
    file); the caller deletes it. In-process callers must pass `workdir` (per-candidate
    local disk); `None` (tempfile default, honors `TMPDIR`) is for CLI mode only.
    `should_abort` raises `ExtractionInterrupted`."""
    if should_abort is not None and should_abort():
        raise ExtractionInterrupted(
            f"playback active/unknown; not starting extraction: {video}")

    fd, wav_name = tempfile.mkstemp(suffix=".wav", prefix="subverify_", dir=workdir)
    os.close(fd)
    wav = Path(wav_name)
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if start_s is not None:
        cmd += ["-ss", str(start_s)]
    if dur_s is not None:
        cmd += ["-t", str(dur_s)]
    cmd += ["-i", str(video), "-map", f"0:{audio_index}",
            "-ac", "1", "-ar", "16000", str(wav)]
    timeout = max(120, int(dur_s) + 60) if dur_s is not None else 1800
    deadline = time.monotonic() + timeout

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout = stderr = ""
    try:
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=_ABORT_POLL_S)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.communicate()
                    raise RuntimeError(
                        f"ffmpeg audio extract timed out after {timeout}s: {video}")
                if should_abort is not None and should_abort():
                    proc.terminate()
                    try:
                        proc.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate()
                    raise ExtractionInterrupted(
                        f"playback active/unknown; aborted extraction: {video}")
        if proc.returncode != 0 or not wav.exists() or wav.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg audio extract failed ({video}, stream {audio_index}): "
                               f"{(stderr or '').strip()[:300]}")
    except BaseException:
        # Single cleanup point: no temp wav survives any failure path.
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        wav.unlink(missing_ok=True)
        raise

    return wav


_MODEL_CACHE: dict[tuple[str, int], object] = {}


def _get_model(model: str, threads: int):
    """Cached `WhisperModel` by (name, threads); loading is expensive."""
    key = (model, threads)
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached
    from faster_whisper import WhisperModel  # lazy: heavy import, only needed to transcribe
    inst = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=threads)
    _MODEL_CACHE[key] = inst
    return inst


def asr_window(video: Path, audio_index: int, start_s: float | None, dur_s: float | None,
                model: str, threads: int, workdir: Path, *,
                should_abort: Callable[[], bool] | None = None) -> str:
    """Transcribe a window with faster-whisper, forcing English (use `detect_language`
    for a stream's real language). The temp wav is always removed."""
    wav = _extract_wav(video, audio_index, start_s, dur_s,
                       workdir=workdir, should_abort=should_abort)
    try:
        whisper = _get_model(model, threads)
        segments, _ = whisper.transcribe(
            str(wav), language="en", vad_filter=True,
            condition_on_previous_text=False,
        )
        return " ".join(seg.text for seg in segments)
    finally:
        wav.unlink(missing_ok=True)


def detect_language(video: Path, audio_index: int, start_s: float, dur_s: float,
                     model: str, threads: int, workdir: Path, *,
                     should_abort: Callable[[], bool] | None = None) -> tuple[str, float]:
    """Whisper language-ID `(code, probability)` on a window, to vet an untagged/`und`
    stream. `model` must be multilingual (`WHISPER_LID_MODEL`, default `small`)."""
    wav = _extract_wav(video, audio_index, start_s, dur_s,
                       workdir=workdir, should_abort=should_abort)
    try:
        whisper = _get_model(model, threads)
        _, info = whisper.transcribe(str(wav), vad_filter=True)
        return info.language, info.language_probability
    finally:
        wav.unlink(missing_ok=True)


def _srt_timestamp(seconds: float) -> str:
    """Seconds -> `HH:MM:SS,mmm`, rounding total ms first so `1.9996` carries to `00:00:02,000`."""
    total_ms = int(round(seconds * 1000))
    hours, rem_ms = divmod(total_ms, 3_600_000)
    minutes, rem_ms = divmod(rem_ms, 60_000)
    secs, millis = divmod(rem_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


# Whisper's per-segment output is lowercase, unpunctuated and up to ~43 s: a punctuated
# prompt plus word timestamps fixes the text, and cues are rebuilt from the words.
_WHISPER_INITIAL_PROMPT = "Hello. Welcome back, everyone! How are you? I'm fine, thanks."
_CUE_MAX_CHARS = 84          # whole cue (two lines of up to 42)
_CUE_LINE_CHARS = 42
_CUE_MAX_SECONDS = 7.0
_CUE_SENTENCE_SPLIT_MIN_SECONDS = 1.0   # don't split at "." before this
_REPEAT_COLLAPSE_AT = 4      # a word repeated this many times in a row ...
_REPEAT_KEEP = 2             # ... is cut down to this many (ASR loop)
_SENTENCE_END_RE = re.compile(r"""[.!?]["')\]]*$""")
_REPEAT_KEY_RE = re.compile(r"[^\w']")

_Word = tuple[str, float, float]   # (text, start_s, end_s)


def _repeat_key(text: str) -> str:
    return _REPEAT_KEY_RE.sub("", text.lower())


def _collapse_repeats(words: list[_Word]) -> list[_Word]:
    """Cut `_REPEAT_COLLAPSE_AT`+ identical words (ignoring case/punctuation) to
    `_REPEAT_KEEP`: Whisper's loop over silence. Shorter runs ("no no no") stay."""
    out: list[_Word] = []
    i = 0
    while i < len(words):
        key = _repeat_key(words[i][0])
        j = i + 1
        if key:
            while j < len(words) and _repeat_key(words[j][0]) == key:
                j += 1
        run = words[i:j]
        out.extend(run[:_REPEAT_KEEP] if key and len(run) >= _REPEAT_COLLAPSE_AT else run)
        i = j
    return out


def _wrap_two_lines(words: list[str]) -> list[str] | None:
    """One line if it fits, else the most even two-line split; `None` if none fits."""
    whole = " ".join(words)
    if len(whole) <= _CUE_LINE_CHARS:
        return [whole]
    best: list[str] | None = None
    best_width = _CUE_LINE_CHARS + 1
    for k in range(1, len(words)):
        a, b = " ".join(words[:k]), " ".join(words[k:])
        width = max(len(a), len(b))
        if width <= _CUE_LINE_CHARS and width < best_width:
            best, best_width = [a, b], width
    return best


def _build_cues(words: list[_Word]) -> list[tuple[float, float, str]]:
    """Timed `words` -> cues `(start_s, end_s, text)`: split at sentence end after
    `_CUE_SENTENCE_SPLIT_MIN_SECONDS`, and BEFORE a word that would exceed the char,
    second or two-line limits. Repeat loops collapse first."""
    cleaned = [(t.strip(), s, e) for t, s, e in words]
    cleaned = _collapse_repeats([w for w in cleaned if w[0]])
    cues: list[tuple[float, float, str]] = []
    cur: list[_Word] = []

    def flush() -> None:
        if not cur:
            return
        texts = [w[0] for w in cur]
        text = "\n".join(_wrap_two_lines(texts) or [" ".join(texts)])
        start = cur[0][1]
        # Clamp: one badly aligned word could outlast the limit.
        end = min(max(cur[-1][2], start), start + _CUE_MAX_SECONDS)
        cues.append((start, end, text))
        cur.clear()

    for text, start, end in cleaned:
        if cur:
            candidate = [w[0] for w in cur] + [text]
            if (len(" ".join(candidate)) > _CUE_MAX_CHARS
                    or end - cur[0][1] > _CUE_MAX_SECONDS
                    or _wrap_two_lines(candidate) is None):
                flush()
        cur.append((text, start, end))
        if (_SENTENCE_END_RE.search(text)
                and end - cur[0][1] >= _CUE_SENTENCE_SPLIT_MIN_SECONDS):
            flush()
    flush()
    return cues


def _segment_words(seg) -> list[_Word]:
    """Timed words; with none from the aligner, spread the text over the span by characters."""
    words = getattr(seg, "words", None)
    if words:
        return [(w.word, w.start, w.end) for w in words]
    parts = (getattr(seg, "text", "") or "").split()
    total = sum(len(p) for p in parts)
    if not total:
        return []
    span = max(seg.end - seg.start, 0.0)
    out: list[_Word] = []
    pos = seg.start
    for p in parts:
        nxt = pos + span * len(p) / total
        out.append((p, pos, nxt))
        pos = nxt
    return out


def transcribe_to_srt(video: Path, audio_index: int, model: str, threads: int, *,
                       should_abort: Callable[[], bool] | None = None) -> str:
    """Whole-stream transcription as an SRT document. Slow: run via CLI mode."""
    wav = _extract_wav(video, audio_index, None, None, should_abort=should_abort)
    try:
        whisper = _get_model(model, threads)
        segments, _ = whisper.transcribe(
            str(wav), language="en", vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=_WHISPER_INITIAL_PROMPT, word_timestamps=True,
        )
        words: list[_Word] = []
        for seg in segments:
            words.extend(_segment_words(seg))
        return "\n".join(
            f"{n}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n{text}\n"
            for n, (start, end, text) in enumerate(_build_cues(words), 1)
        )
    finally:
        wav.unlink(missing_ok=True)


def _words(text: str) -> set[str]:
    """Distinct lowercased 4+ letter words."""
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) >= _MIN_OVERLAP_WORD_LEN}


def _all_words(text: str) -> list[str]:
    """Every lowercased word in order."""
    return _ALL_WORDS_RE.findall(text.lower())


def _bigram_set(words: list[str]) -> set[tuple[str, str]]:
    return set(zip(words, words[1:]))


def _parse_srt_timestamp(ts: str) -> float:
    h, m, s = ts.replace(".", ",").split(":")
    secs, _, millis = s.partition(",")
    return int(h) * 3600 + int(m) * 60 + int(secs) + (int(millis or 0) / 1000 if millis else 0)


# Seconds either side of an ASR window searched for sidecar cues (timing drift).
OVERLAP_PAD_S = 30.0


def _sidecar_cues(srt_text: str) -> list[tuple[float, str]]:
    """`(start_s, text)` per timed cue."""
    cues = []
    for block in re.split(r"\n\s*\n", srt_text):
        m = _CUE_TIME_RE.search(block)
        if not m:
            continue
        lines = [ln for ln in block.splitlines() if "-->" not in ln]
        cues.append((_parse_srt_timestamp(m.group(1)), " ".join(lines)))
    return cues


def _match_ratios(asr_text: str, window_text: str) -> tuple[float, float, int]:
    """`overlap()`'s `(word_ratio, bigram_ratio, asr_word_count)`."""
    asr_words = _words(asr_text)
    window_words = _words(window_text)
    word_ratio = (len(asr_words & window_words) / len(asr_words)) if asr_words else 0.0

    asr_bigrams = _bigram_set(_all_words(asr_text))
    window_bigrams = _bigram_set(_all_words(window_text))
    bigram_ratio = (len(asr_bigrams & window_bigrams) / len(asr_bigrams)) if asr_bigrams else 0.0

    return word_ratio, bigram_ratio, len(asr_words)


def overlap(asr_text: str, srt_text: str, start_s: float, dur_s: float
           ) -> tuple[float, float, int]:
    """ASR text vs sidecar cues in `[start_s - 30, start_s + dur_s + 30]`:
    `(word_ratio, bigram_ratio, asr_word_count)`.
      - `word_ratio`: share of the ASR's distinct 4+-letter words found in the window.
      - `bigram_ratio`: share of ASR ordered word pairs (all words) found. A wrong
        episode shares vocabulary (names, catchphrases) but not pairs.
      - `asr_word_count`: vocabulary behind `word_ratio`; a high ratio over few words proves nothing.

    Calibration (small.en, 150 s, 9 right vs 18 wrong same-show pairings): word_ratio
    right 0.88-0.98, wrong up to 0.31; bigram_ratio right 0.77-0.94, wrong 0.01-0.09.
    """
    window_text = " ".join(
        text for cue_start, text in _sidecar_cues(srt_text)
        if start_s - OVERLAP_PAD_S <= cue_start <= start_s + dur_s + OVERLAP_PAD_S)
    return _match_ratios(asr_text, window_text)


# Acceptance thresholds for `overlap()` (calibration there); BOTH ratios required.
MIN_WORD_RATIO = 0.60
MIN_BIGRAM_RATIO = 0.40
MIN_ASR_WORDS = 15


# Verifying a real sidecar against an embedded GENERATED track, itself an ASR transcript
# standing in for the ASR text. Read in DISJOINT windows (middle of each third) so one
# shared passage cannot count as several matches.
UPGRADE_WINDOW_COUNT = 3
UPGRADE_WINDOW_S = 300.0
UPGRADE_MIN_PASSING_WINDOWS = 2
UPGRADE_FFPROBE_TIMEOUT_S = 60.0

_MARKUP_RE = re.compile(r"\{[^}]*\}|<[^>]+>")
_ESCAPE_RE = re.compile(r"\\[Nnh]")


def _decode_ffprobe_data(dump: str) -> bytes:
    """Bytes of an ffprobe `-show_data` hexdump (`%08x: ` + 40-char hex field + ASCII)."""
    out = bytearray()
    for line in dump.split("\n"):
        hexpart = line[10:50].replace(" ", "")
        if not hexpart:
            continue
        try:
            out += bytes.fromhex(hexpart)
        except ValueError:
            continue
    return bytes(out)


def _packet_text(data: bytes, codec: str) -> str:
    """Packet dialogue without markup; ASS cut to its text field (after the 8th comma)."""
    text = data.decode("utf-8", errors="replace")
    if codec == "ass":
        text = text.split(",", 8)[-1]
    return " ".join(_ESCAPE_RE.sub(" ", _MARKUP_RE.sub("", text)).split())


def embedded_text_window(video: Path, stream_index: int, codec: str,
                         start_s: float, dur_s: float,
                         timeout_s: float = UPGRADE_FFPROBE_TIMEOUT_S) -> str | None:
    """Dialogue of embedded text stream in `[start_s, start_s + dur_s)` via ffprobe; `None`
    when unreadable or the codec is not `subrip`/`ass`.

    Interval is `start%end` (absolute), not `start%+dur`: ffprobe measures `+dur` from the
    first packet it reads, so a seek landing in a quiet stretch silently moves the window.
    Packets before `start` that the seek drags in are dropped by `pts_time`."""
    if codec not in ("subrip", "ass"):
        return None
    end_s = start_s + dur_s
    cmd = ["ffprobe", "-v", "error", "-read_intervals", f"{start_s:.3f}%{end_s:.3f}",
           "-select_streams", str(stream_index), "-show_packets", "-show_data",
           "-of", "json", str(video)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        if r.returncode != 0:
            return None
        packets = json.loads(r.stdout).get("packets")
    except (subprocess.SubprocessError, OSError, ValueError, AttributeError):
        return None
    if not isinstance(packets, list):
        return None
    texts = []
    for p in packets:
        if not isinstance(p, dict) or not isinstance(p.get("data"), str):
            continue
        try:
            pts = float(p["pts_time"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (start_s <= pts < end_s):  # half-open: a boundary packet is in one window only
            continue
        t = _packet_text(_decode_ffprobe_data(p["data"]), codec)
        if t:
            texts.append(t)
    return " ".join(texts)


def upgrade_windows(duration_s: float) -> list[tuple[float, float]]:
    """Disjoint `(start_s, dur_s)` windows at the middle of each equal third; empty for a
    non-positive or non-finite duration."""
    if not (isinstance(duration_s, (int, float)) and math.isfinite(duration_s)) or duration_s <= 0:
        return []
    third = duration_s / UPGRADE_WINDOW_COUNT
    dur = min(UPGRADE_WINDOW_S, third)
    return [(third * i + (third - dur) / 2, dur) for i in range(UPGRADE_WINDOW_COUNT)]


def generated_track_windows(video: Path, stream: dict, duration_s: float
                            ) -> list[tuple[float, float, str]] | None:
    """Generated track `(start_s, dur_s, text)` per window; `None` if any is unreadable
    (a partial read is not evidence)."""
    windows = []
    for start, dur in upgrade_windows(duration_s):
        text = embedded_text_window(video, stream["index"], stream.get("codec_name") or "",
                                    start, dur)
        if text is None:
            return None
        windows.append((start, dur, text))
    return windows


def sidecar_ranges(windows: list[tuple[float, float, str]]) -> list[tuple[float, float]]:
    """Half-open `[lo, hi)` sidecar ranges: `OVERLAP_PAD_S` either side, capped at half
    the gap to the neighbour so ranges never overlap. Windows disjoint, in order."""
    ranges = []
    for i, (start, dur, _text) in enumerate(windows):
        end = start + dur
        lo = start - OVERLAP_PAD_S
        hi = end + OVERLAP_PAD_S
        if i > 0:
            prev_end = windows[i - 1][0] + windows[i - 1][1]
            lo = start - min(OVERLAP_PAD_S, max(0.0, start - prev_end) / 2)
        if i < len(windows) - 1:
            hi = end + min(OVERLAP_PAD_S, max(0.0, windows[i + 1][0] - end) / 2)
        ranges.append((lo, hi))
    return ranges


def judge_sidecar_against_windows(windows: list[tuple[float, float, str]], srt_text: str
                                  ) -> tuple[bool, str, int, int]:
    """Is `srt_text` the right episode's subtitle? A window is USABLE with >=
    `MIN_ASR_WORDS` generated words and PASSES at `MIN_WORD_RATIO` AND
    `MIN_BIGRAM_RATIO`; `UPGRADE_MIN_PASSING_WINDOWS` passes accept. Too little text
    is a refusal, never a pass. Evidence is disjoint: an overlapping window is not
    counted and each sidecar cue backs at most one window.

    Returns `(ok, reason, usable, evidence)`. `evidence` counts windows where BOTH
    sides have >= `MIN_ASR_WORDS` words; only those support a NEGATIVE ("wrong
    episode") verdict, so a correct but partial sidecar is undecided, not wrong."""
    if not windows:
        return False, "no verification windows (unknown duration)", 0, 0
    kept: list[tuple[float, float, str]] = []
    notes = []
    last_end = -math.inf
    for start, dur, gen_text in sorted(windows, key=lambda w: w[0]):
        if start < last_end - 1e-6:
            notes.append(f"{start:.0f}s: overlaps the window before it, not counted")
            continue
        last_end = start + dur
        kept.append((start, dur, gen_text))

    cues = _sidecar_cues(srt_text)
    ranges = sidecar_ranges(kept)
    assigned: set[int] = set()
    passing = usable = evidence = 0
    for (start, _dur, gen_text), (lo, hi) in zip(kept, ranges):
        mine = []
        for n, (cue_start, cue_text) in enumerate(cues):
            if n not in assigned and lo <= cue_start < hi:
                assigned.add(n)  # a cue backs one window only
                mine.append(cue_text)
        window_text = " ".join(mine)
        word_ratio, bigram_ratio, nwords = _match_ratios(gen_text, window_text)
        sidecar_words = len(_words(window_text))
        notes.append(f"{start:.0f}s: {nwords} words, word {word_ratio:.2f}, bigram {bigram_ratio:.2f}"
                     f", sidecar {sidecar_words} words")
        if nwords < MIN_ASR_WORDS:
            continue
        usable += 1
        if sidecar_words >= MIN_ASR_WORDS:
            evidence += 1
        if word_ratio >= MIN_WORD_RATIO and bigram_ratio >= MIN_BIGRAM_RATIO:
            passing += 1
    ok = passing >= UPGRADE_MIN_PASSING_WINDOWS
    return ok, (f"{passing} of {usable} usable window(s) match, "
                f"{UPGRADE_MIN_PASSING_WINDOWS} needed ({'; '.join(notes)})"), usable, evidence


def _cli_transcribe(argv: list[str]) -> int:
    import argparse

    from media_stack.clients.jellyfin import JellyfinClient
    from media_stack.paths import MEDIA_LAN_IP, MEDIA_STACK_ROOT, load_env_file

    ap = argparse.ArgumentParser(prog="python3 -m media_stack.subverify transcribe")
    ap.add_argument("video")
    ap.add_argument("audio_index", type=int)
    ap.add_argument("model")
    ap.add_argument("threads", type=int)
    ns = ap.parse_args(argv)

    env = {**load_env_file(MEDIA_STACK_ROOT / ".env"), **os.environ}
    jellyfin_key = env.get("JELLYFIN_API_KEY", "")
    jellyfin_url = env.get("JELLYFIN_URL") or f"http://{MEDIA_LAN_IP}:8096"
    if not jellyfin_key:
        # Never treat "no key" as idle (fail open).
        print("JELLYFIN_API_KEY is not set; refusing to extract blinded to "
              "Jellyfin playback state (a missing key is NOT treated as idle)",
              file=sys.stderr)
        return 3
    raw_max_wait = env.get("SUB_FALLBACK_MAX_WAIT_MIN")
    max_wait_min = 30.0
    # 24h bound as in the parent's `env_float`: `1e308 * 60` overflows to inf.
    max_wait_min_limit = 1440.0
    if raw_max_wait:
        # Unlike the parent's `env_float`, `0` is ACCEPTED: an exhausted allowance
        # means "no waiting", not the 30-minute default.
        try:
            parsed = float(raw_max_wait)
        except ValueError:
            print(f"SUB_FALLBACK_MAX_WAIT_MIN={raw_max_wait!r} is not a valid "
                  f"number; using default 30", file=sys.stderr)
        else:
            if math.isfinite(parsed) and 0 <= parsed <= max_wait_min_limit:
                max_wait_min = parsed
            else:
                print(f"SUB_FALLBACK_MAX_WAIT_MIN={raw_max_wait!r} must be a "
                      f"finite, non-negative number <= {max_wait_min_limit:g}; "
                      f"using default 30", file=sys.stderr)
    jellyfin = JellyfinClient(jellyfin_url)

    def _busy() -> bool:
        sessions = jellyfin.now_playing_sessions(jellyfin_key)
        return sessions is None or len(sessions) > 0

    remaining_s = max_wait_min * 60   # cumulative across retries
    spent_s = 0.0                     # reported to the parent on every exit
    workdir = env.get("TMPDIR")

    def _report_spent() -> None:
        if not workdir:
            return
        try:
            (Path(workdir) / CHILD_PLAYBACK_WAIT_REPORT_FILENAME).write_text(
                f"{spent_s:.3f}")
        except OSError:
            pass

    try:
        while True:
            try:
                srt = transcribe_to_srt(Path(ns.video), ns.audio_index, ns.model, ns.threads,
                                        should_abort=_busy)
                sys.stdout.write(srt)
                return 0
            except ExtractionInterrupted:
                t0 = time.monotonic()
                while True:
                    busy = _busy()
                    elapsed = time.monotonic() - t0
                    # After EVERY poll: an idle last poll must not mask an exhausted budget.
                    if elapsed >= remaining_s:
                        spent_s += elapsed
                        print("playback wait exceeded SUB_FALLBACK_MAX_WAIT_MIN "
                              "(cumulative across every retry this transcription has made)",
                              file=sys.stderr)
                        return 2
                    if not busy:
                        break
                    time.sleep(_ABORT_POLL_S)
                this_wait = time.monotonic() - t0
                remaining_s -= this_wait
                spent_s += this_wait
                continue
            except Exception as exc:
                print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
                return 1
    finally:
        _report_spent()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "transcribe":
        raise SystemExit(_cli_transcribe(sys.argv[2:]))
    print("usage: python3 -m media_stack.subverify transcribe "
          "<video> <audio_index> <model> <threads>", file=sys.stderr)
    raise SystemExit(2)
