#!/usr/bin/env python3
"""Whisper language-ID spot-check of what each video's audio ACTUALLY is.

Nothing else listens to the audio: `media_stack.audio` trusts the language TAG and
dispositions, and release names and tags can be wrong (a "Dual Spanish" release imported
Spanish-only; a Japanese dub flagged `default`). This samples short whisper LID windows of
each non-commentary audio stream and cross-checks them against the stream's tag (MISLABELED)
and Radarr/Sonarr's `originalLanguage` (`media_stack.arrlang`; NO_ENGLISH_AUDIO). A false
alert costs the operator's time, so anything not determined with confidence is UNVERIFIED.

CLASSIFICATION: A FOREIGN TAG NEVER OUTWEIGHS A CONFIDENT DETECTION
Every decision looks at ALL sampled windows against RAW probabilities (0.7996 is not 0.8).
A stream counts as English when it is English-tagged and not MISLABELED, OR any window
detects English at >= 0.6 whatever its tag. It is ruled OUT only when EVERY window is
confident (>= 0.6) and none says English. NO_ENGLISH_AUDIO fires only when the title's
original language is English AND every non-commentary stream is ruled out. A mixed or
inconclusive stream with nothing confirmed English, or a file with no non-commentary audio,
is UNVERIFIED (no finding, no alert).

MISLABELED needs the SAME non-English language at >= 0.8 in two windows starting >= 60 s
apart, so one hallucination or foreign insert cannot flag a correctly tagged track. The scan
(25/50/10/75% of the file, subject to the 60 s spacing) stops early ONLY on a confident
English window; otherwise every planned window is sampled. A file under 3 minutes cannot
offer two such windows: UNVERIFIED. So is a file with no usable duration (container, audio
stream, then a bounded ffprobe call).

NEVER DELETES, BLOCKLISTS, OR RE-GRABS: Sonarr/Radarr will not import a same-quality
replacement while the file exists, and deleting first risks leaving nothing. Findings go to
Telegram, the state file and the JSONL log.

PLAYBACK: FAILS CLOSED, BOUNDED (`media_stack.playback`)
`None` from `JellyfinClient.now_playing_sessions` means "someone is watching". No library
file is read (ffprobe included) before the gate says idle, and every ffmpeg extraction
checks playback before starting and while running, aborting and retrying. Waits are bounded
by `AUDIO_CHECK_MAX_WAIT_MIN` cumulatively PER FILE; past it the whole run ends cleanly
(exit 0, next hourly tick retries). A real run without `JELLYFIN_API_KEY` refuses to start
(exit 1); `--dry-run` is exempt.

CANDIDATES
`.mkv`/`.mp4` under `MEDIA_ROOT`'s `movies/` and `tv/`, skipping hidden names (including
`.dupe-recycle`) and files modified < 10 minutes ago (still being written). Newest mtime
first, so a fresh import surfaces within the hour while the backlog drains. A file is
re-checked only when its `[st_ino, st_size, st_mtime_ns]` changes. A title added to
Radarr/Sonarr AFTER its file was checked is not revisited until the file changes.

STATE: ONE FILE, VALIDATED WHOLE
`var/state/audio-lang-check.state.json` is the only file read back. Per resolved path:
  identity     [st_ino, st_size, st_mtime_ns] the conclusion was made on
  result       "CHECKED" or "UNVERIFIED"
  findings     MISLABELED / NO_ENGLISH_AUDIO dicts (may be empty)
  first_found, alerted    only with findings
  checked_at
  failed       failed analyses of that identity: identity, attempts, last_result, last_error
Keys of older versions (`streams`, `original_status`, `original_language`,
`original_rechecked_at`, and `retry`, whose counter also counted outages) are ignored on
load, so a leftover `retry` never rules a file out, and dropped whenever the entry is
rewritten. Every update is one `mutate_state` (flock), so
a conclusion, findings, alert flag and identity are recorded together or not at all. The
whole structure is validated on load and the script refuses to WRITE an entry the loader
would reject. An unreadable file or malformed entry is a blinding error (exit 1 before any
state or log write), never "treat as empty".

FAILURES ARE RETRIED A FEW TIMES, NOT CACHED
PROBE_FAILED and ERROR are tried again on every run until the same identity has failed
`MAX_FAILED_ATTEMPTS` times (counted in `failed`); after that the file is skipped until it
changes. ARR_UNAVAILABLE (Radarr/Sonarr could not be asked, as opposed to no usable
language) records nothing (no state, no JSONL line) and is summarised in one log line: an
outage never uses up a file's attempts. DEPENDENCY_MISSING (no `faster_whisper`) records nothing and ends the run
with exit 1.

FINDINGS ARE BOUND TO A FILE IDENTITY: an alert only goes out for an entry whose identity
equals the file's current one; entries for a gone or replaced file are never sent. If the
file changed during analysis, the result is discarded and a later run redoes it.

ONE FILE AT A TIME, IDLE PRIORITY: `AUDIO_CHECK_BUDGET_MIN` stops STARTING new files once
spent. Temp WAVs live in a per-file workdir under `WORKDIR_ROOT` (local disk, never the
media pool). `WHISPER_THREADS` defaults to `min(2, cpu_count)` and cron runs it under
`nice -n19 ionice -c3`; the pause-aware extraction is the real protection.

ALERTING: EACH NEW FINDING IS SENT ONCE
At the end of every real run (also after a playback timeout or a missing `faster_whisper`),
findings that are new or changed (`alerted` not true, file still at the identity they were
made on) go to Telegram, oldest first, at most 15 items and 3800 characters per message
(Telegram rejects > 4096 UTF-16 units), each item cut to 600, as many messages as that
takes. Every finding is marked `alerted` once its message was attempted, so a failed send
is NOT retried: it is logged to stderr and as an `ALERT_FAILED` JSONL record naming the
files. Credentials come from `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` in the process
environment, else the Claude Telegram plugin's `.env` (never this repo's `.env`); with both
in the environment the plugin file is not read, and a failure reading it is logged and fails
soft (the send then fails and is logged as above).

Exit codes:
  0  ok, including nothing to do, a second cron instance finding the lock held
     (exits quietly), and a playback wait that exceeded AUDIO_CHECK_MAX_WAIT_MIN
  1  blinded: no JELLYFIN_API_KEY on a real run, unreadable or malformed state,
     an English-only (`.en`) WHISPER_LID_MODEL, or `faster_whisper` missing
  2  usage error (e.g. `--dry-run` with `--path`)
 75  `--path` only (EX_TEMPFAIL): another run holds the lock; stdout carries
     {"path": ..., "result": "BUSY", "reason": ...}

`--path FILE` checks one file and prints JSON. It records NOTHING (no state, JSONL or
Telegram), so it can never suppress the cron alert for the same file. It cannot be
combined with `--dry-run` (exit 2).

`--dry-run` makes no WRITES: no `var/` directories, no lock file (importing the script
writes nothing; `main()` creates the directories for a run that writes).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

from media_stack.arrlang import ORIGINAL_UNAVAILABLE, original_language_lookup  # noqa: E402
from media_stack.audio import is_commentary_audio  # noqa: E402
from media_stack.clients.jellyfin import JellyfinClient  # noqa: E402
from media_stack.clients.telegram import send as telegram_send  # noqa: E402
from media_stack.envconfig import env_float, env_int  # noqa: E402
from media_stack.lang import is_english_tag  # noqa: E402
from media_stack.locking import (  # noqa: E402
    acquire_singleton_lock, handle_sigterm as _handle_sigterm,
)
from media_stack.paths import (  # noqa: E402
    MEDIA_LAN_IP, MEDIA_ROOT, MEDIA_STACK_ROOT, VAR_DIR, VAR_LOG, VAR_RUN,
    VAR_STATE, ensure_var_dirs, load_env_file,
)
from media_stack.playback import (  # noqa: E402
    PlaybackWaitBudget, PlaybackWaitTimeout, call_with_playback_pause, wait_while_playing,
)
from media_stack.probe import (  # noqa: E402
    audio_stream_duration, parse_duration, probe, probe_duration,
)
from media_stack.state import mutate_state  # noqa: E402
from media_stack.subverify import detect_language  # noqa: E402

# No `ensure_var_dirs()` at import: `--dry-run` writes nothing.

STATE_FILE = VAR_STATE / "audio-lang-check.state.json"
LOCK_FILE = VAR_RUN / "audio-lang-check.lock"
JSONL_FILE = VAR_LOG / "audio-lang-check.jsonl"

# Shared with sub-fallback.py (distinct per-file prefixes).
WORKDIR_ROOT = VAR_DIR / "tmp"

# Younger files are probably still being written or renamed.
MIN_FILE_AGE_S = 10 * 60

LID_WINDOW_S = 30.0
# 0.75 lets a file just over MIN_MISLABEL_DURATION_S have a window far enough from the 25% one.
LID_FRACTIONS = (0.25, 0.50, 0.10, 0.75)
LID_PROBABILITY_FLOOR = 0.6
MISLABEL_PROBABILITY_FLOOR = 0.8
MIN_WINDOW_GAP_S = 60.0   # windows are independent evidence only this far apart
# Below this, two such windows cannot be placed: MISLABELED is skipped.
MIN_MISLABEL_DURATION_S = 180.0

# Results that are never a conclusion. Only the first two are counted against a file.
FAILED_RESULTS = frozenset({"PROBE_FAILED", "ERROR"})
MAX_FAILED_ATTEMPTS = 3
CONCLUSION_RESULTS = frozenset({"CHECKED", "UNVERIFIED"})
FINDING_TYPES = frozenset({"MISLABELED", "NO_ENGLISH_AUDIO"})

MAX_ALERT_ITEMS = 15
# Under Telegram's 4096 UTF-16 units.
TELEGRAM_BUDGET_CHARS = 3800
MAX_ALERT_LINE_CHARS = 600

_MAX_MINUTES_SETTING = 1440.0

# sysexits.h EX_TEMPFAIL.
EXIT_BUSY = 75


@dataclass
class Config:
    jellyfin_url: str
    jellyfin_key: str
    budget_min: float
    max_wait_min: float
    whisper_lid_model: str
    whisper_threads: int


def load_config(env: dict[str, str]) -> Config:
    # The default must respect the bound env_int enforces on overrides.
    cpu_bound = os.cpu_count() or 64
    default_threads = min(2, cpu_bound)
    return Config(
        jellyfin_url=env.get("JELLYFIN_URL") or f"http://{MEDIA_LAN_IP}:8096",
        jellyfin_key=env.get("JELLYFIN_API_KEY", ""),
        budget_min=env_float(env, "AUDIO_CHECK_BUDGET_MIN", 20.0, _MAX_MINUTES_SETTING, log=log),
        max_wait_min=env_float(env, "AUDIO_CHECK_MAX_WAIT_MIN", 15.0, _MAX_MINUTES_SETTING, log=log),
        whisper_lid_model=env.get("WHISPER_LID_MODEL") or "small",
        whisper_threads=env_int(env, "WHISPER_THREADS", default_threads, cpu_bound, log=log),
    )


# `--path` prints JSON on stdout, so logs go to stderr in that mode.
_LOG_TO_STDERR = False


_LONE_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _clean_text(text: str) -> str:
    """Replace lone surrogates with U+FFFD: a file name with invalid UTF-8 arrives with
    `surrogateescape` surrogates and cannot be encoded, so path-derived text goes through here."""
    return _LONE_SURROGATE_RE.sub("�", text)


def log(msg: str, *, err: bool = False) -> None:
    """Timestamped line to stdout (stderr with `err`, and always in `--path` mode)."""
    print(_clean_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"), flush=True,
          file=sys.stderr if err or _LOG_TO_STDERR else sys.stdout)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def iter_media_files(media_root: Path) -> Iterator[Path]:
    """`.mkv`/`.mp4` under `movies/` and `tv/`, skipping hidden names (e.g. `.dupe-recycle`)."""
    for sub in ("movies", "tv"):
        root = media_root / sub
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.startswith("."):
                    continue
                if name.lower().endswith((".mkv", ".mp4")):
                    yield Path(dirpath) / name


def _identity(path: Path, now_s: float | None = None) -> list[int] | None:
    """`[st_ino, st_size, st_mtime_ns]`, or `None` if gone (or, given `now_s`, modified
    inside the `MIN_FILE_AGE_S` settle window).

    Deliberately not `media_stack.fileid`'s four fields: ctime also moves when Radarr/Sonarr
    add or remove a hardlink, which would re-run whisper on and re-alert about unchanged
    audio. A same-size rewrite with mtime restored is missed: a stale verdict, never a
    wrong write."""
    try:
        st = path.stat()
    except OSError:
        return None
    if now_s is not None and now_s - st.st_mtime < MIN_FILE_AGE_S:
        return None
    return [st.st_ino, st.st_size, st.st_mtime_ns]


def _finite_number(value: object) -> bool:
    """Finite int/float, never a bool."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _failed_attempts(entry: object, identity: list[int]) -> int:
    """`PROBE_FAILED`/`ERROR` analyses recorded for exactly this version of the file."""
    failed = entry.get("failed") if isinstance(entry, dict) else None
    attempts = failed.get("attempts") if isinstance(failed, dict) and failed.get("identity") == identity else 0
    return attempts if _is_index(attempts) else 0


def select_candidates(state: dict, media_root: Path, now_s: float) -> list[Path]:
    """Settled files whose identity is not already concluded, or failed
    `MAX_FAILED_ATTEMPTS` times; newest mtime first."""
    found: list[tuple[int, Path]] = []
    for video in iter_media_files(media_root):
        identity = _identity(video, now_s)
        if identity is None:
            continue
        cached = state.get(str(video.resolve()))
        if isinstance(cached, dict) and cached.get("identity") == identity:
            continue
        if _failed_attempts(cached, identity) >= MAX_FAILED_ATTEMPTS:
            continue
        found.append((identity[2], video))
    found.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in found]


def _mislabeled_language(windows: list[dict]) -> tuple[str, float] | None:
    """Two windows with the SAME non-English language at >= `MISLABEL_PROBABILITY_FLOOR`,
    >= `MIN_WINDOW_GAP_S` apart: `(language, lower raw probability)`, else `None`."""
    by_lang: dict[str, list[dict]] = {}
    for w in windows:
        if w["language"] != "en" and w["probability"] >= MISLABEL_PROBABILITY_FLOOR:
            by_lang.setdefault(w["language"], []).append(w)
    for lang, ws in by_lang.items():
        for i, a in enumerate(ws):
            for b in ws[i + 1:]:
                if abs(a["start"] - b["start"]) >= MIN_WINDOW_GAP_S:
                    return lang, min(a["probability"], b["probability"])
    return None


def _lid_scan(
    video: Path, audio_index: int, duration: float, cfg: Config,
    jellyfin: JellyfinClient, budget: PlaybackWaitBudget, workdir: Path,
) -> list[dict]:
    """Whisper LID over a stream's windows in `LID_FRACTIONS` order, stopping early only on
    a confident ENGLISH window. A fraction within `MIN_WINDOW_GAP_S` of a sampled one is
    skipped. Returns windows (`fraction`, `start`, `language`, RAW `probability`), at least
    one. Only `PlaybackWaitTimeout` propagates."""
    windows: list[dict] = []
    for frac in LID_FRACTIONS:
        start = max(0.0, duration * frac)
        if any(abs(start - w["start"]) < MIN_WINDOW_GAP_S for w in windows):
            continue
        lang, prob = call_with_playback_pause(
            detect_language, jellyfin, cfg.jellyfin_key, cfg.max_wait_min, budget,
            video, audio_index, start, LID_WINDOW_S, cfg.whisper_lid_model,
            cfg.whisper_threads, workdir, log=log,
        )
        windows.append({"fraction": frac, "start": start, "language": lang,
                        "probability": prob})
        if lang == "en" and prob >= LID_PROBABILITY_FLOOR:
            break
    return windows


def _classify_windows(windows: list[dict]) -> str:
    """`"english"` (any window English at >= `LID_PROBABILITY_FLOOR`), `"foreign"` (every
    window confident, none English; the only reading that rules a stream out) or `"unclear"`."""
    if any(w["language"] == "en" and w["probability"] >= LID_PROBABILITY_FLOOR for w in windows):
        return "english"
    if windows and all(w["probability"] >= LID_PROBABILITY_FLOOR and w["language"] != "en"
                       for w in windows):
        return "foreign"
    return "unclear"


def _program_indices(streams: list[dict]) -> set[int]:
    """Indices of the scanned streams."""
    return {s["index"] for s in streams if isinstance(s.get("index"), int)}


def _duration_from_info(info: dict, streams: list[dict]) -> float | None:
    """Container duration, else an audio stream's (never video or cover art)."""
    d = parse_duration((info.get("format") or {}).get("duration"))
    if d is not None:
        return d
    first = streams[0].get("index") if streams else None
    return audio_stream_duration(streams, first, _program_indices(streams))


def analyze_candidate(
    video: Path, info: dict, original: str | None, cfg: Config,
    jellyfin: JellyfinClient, budget: PlaybackWaitBudget, workdir: Path,
) -> tuple[list[dict], list[dict], str, str | None]:
    """Analyze every non-commentary audio stream; `original` is the Arr original-language
    code ("eng"/"jpn"/"kor"/`None`). Returns `(findings, stream_records, result, note)`;
    `result` is `"CHECKED"` or `"UNVERIFIED"` (`note` says why). Rules: module docstring.
    UNVERIFIED covers no audio, no usable duration, and a file shorter than
    `MIN_MISLABEL_DURATION_S` with an English-tagged stream (not scanned)."""
    streams = [
        s for s in info.get("streams", [])
        if s.get("codec_type") == "audio" and not is_commentary_audio(s)
    ]
    if not streams:
        return [], [], "UNVERIFIED", "no non-commentary audio stream to check"

    duration = _duration_from_info(info, streams)
    if duration is None:
        # Another read of the library file: same gate, same budget.
        wait_while_playing(jellyfin, cfg.jellyfin_key, cfg.max_wait_min, budget, log=log)
        duration = probe_duration(video, streams[0].get("index"), _program_indices(streams))
    if duration is None:
        return [], [], "UNVERIFIED", "no usable duration; no window sampled"

    mislabel_checkable = duration >= MIN_MISLABEL_DURATION_S
    findings: list[dict] = []
    stream_records: list[dict] = []
    mislabel_skipped = False

    for s in streams:
        idx = s.get("index")
        raw_tag = ((s.get("tags") or {}).get("language") or "").strip().lower()
        tagged_english = is_english_tag(raw_tag)

        if tagged_english and not mislabel_checkable:
            # English by tag; the only check that could overturn it can't run.
            mislabel_skipped = True
            stream_records.append({
                "index": idx, "tag": raw_tag, "detected_language": None, "probability": None,
                "windows": [], "counts_as_english": True, "reading": None,
                "mislabel_skipped": True,
            })
            continue

        windows = _lid_scan(video, idx, duration, cfg, jellyfin, budget, workdir)
        best = max(windows, key=lambda w: w["probability"])  # display only
        reading = _classify_windows(windows)
        # Stored probabilities are rounded; decisions use the raw windows.
        rec = {
            "index": idx,
            "tag": raw_tag or None,
            "detected_language": best["language"],
            "probability": round(best["probability"], 3),
            "windows": [{**w, "probability": round(w["probability"], 3)} for w in windows],
            "reading": reading,
        }

        # A confident English window, even after two foreign ones, rules out MISLABELED.
        mislabel = (_mislabeled_language(windows)
                    if tagged_english and reading != "english" else None)
        if mislabel is not None:
            lang, prob = mislabel
            rec["mislabeled_as"] = lang
            findings.append({
                "type": "MISLABELED",
                "stream_index": idx,
                "tag": raw_tag or None,
                "detected_language": lang,
                "probability": round(prob, 3),
            })

        rec["counts_as_english"] = (tagged_english and mislabel is None) or reading == "english"
        stream_records.append(rec)

    findings, result, note = _decide(original, stream_records, findings, mislabel_skipped)
    return findings, stream_records, result, note


def _decide(
    original: str | None, stream_records: list[dict], mislabeled: list[dict],
    mislabel_skipped: bool,
) -> tuple[list[dict], str, str | None]:
    """The ONE place the NO_ENGLISH_AUDIO / UNVERIFIED rules live. `mislabeled` is the
    MISLABELED findings; a stream's `reading` is `None` when its MISLABELED check was
    skipped."""
    findings = [dict(f) for f in mislabeled]
    any_english = any(r.get("counts_as_english") for r in stream_records)
    any_unclear = any(not r.get("counts_as_english") and r.get("reading") != "foreign"
                      for r in stream_records)
    note: str | None = None
    result = "CHECKED"
    if original == "eng" and not any_english:
        if any_unclear:
            note = "a stream's language could not be determined"
            if not findings:
                result = "UNVERIFIED"
        else:
            findings.append({
                "type": "NO_ENGLISH_AUDIO",
                "original_language": original,
                "streams": [{"index": r["index"], "tag": r["tag"],
                             "detected_language": r["detected_language"],
                             "probability": r["probability"]} for r in stream_records],
            })
    if mislabel_skipped and result == "CHECKED":
        result, note = "UNVERIFIED", (
            f"shorter than {MIN_MISLABEL_DURATION_S:g}s: MISLABELED needs two independent windows")
    return findings, result, note


def process_candidate(
    video: Path, cfg: Config, jellyfin: JellyfinClient, workdir: Path,
) -> dict:
    """Original-language lookup, playback gate, probe, analysis of ONE file. Raises only
    `PlaybackWaitTimeout`; failures come back as a `result` of `PROBE_FAILED`, `ERROR`,
    `ARR_UNAVAILABLE` or `DEPENDENCY_MISSING`. The Arr lookup touches no media so it goes
    first: no whisper time on an untrustworthy result."""
    rec: dict = {"path": str(video), "findings": []}
    budget = PlaybackWaitBudget(cfg.max_wait_min)
    try:
        original, status = original_language_lookup(video)
        rec["original_language"] = original
        rec["original_status"] = status
        if status == ORIGINAL_UNAVAILABLE:
            rec["result"] = "ARR_UNAVAILABLE"
            return rec
        wait_while_playing(jellyfin, cfg.jellyfin_key, cfg.max_wait_min, budget, log=log)
        info = probe(video)
        if info is None:
            rec["result"] = "PROBE_FAILED"
            return rec
        findings, stream_records, result, note = analyze_candidate(
            video, info, original, cfg, jellyfin, budget, workdir)
        rec["result"] = result
        rec["findings"] = findings
        rec["streams"] = stream_records
        if note:
            rec["note"] = note
        return rec
    except PlaybackWaitTimeout:
        raise
    except ImportError as exc:
        rec["result"] = "DEPENDENCY_MISSING"
        rec["error"] = f"{type(exc).__name__}: {exc}"
        return rec
    except Exception as exc:
        rec["result"] = "ERROR"
        rec["error"] = f"{type(exc).__name__}: {exc}"
        return rec


def _is_identity(value: object) -> bool:
    return (isinstance(value, list) and len(value) == 3
            and all(isinstance(x, int) and not isinstance(x, bool) for x in value))


def _is_str_or_none(value: object) -> bool:
    return value is None or isinstance(value, str)


def _is_probability(value: object) -> bool:
    return _finite_number(value) and 0.0 <= value <= 1.0  # type: ignore[operator]


def _is_index(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


_STREAM_FIELDS = {"index": _is_index, "tag": _is_str_or_none, "detected_language": _nonempty_str,
                  "probability": _is_probability}
_FINDING_FIELDS: dict[str, dict] = {
    "MISLABELED": {"stream_index": _is_index, "tag": _is_str_or_none,
                   "detected_language": _nonempty_str, "probability": _is_probability},
    "NO_ENGLISH_AUDIO": {"original_language": _nonempty_str},
}


def _finding_problem(finding: object) -> str | None:
    """Why `finding` is malformed, else `None`, so alert formatting never meets a bad
    value. MISLABELED: stream index, tag, language, probability in [0, 1];
    NO_ENGLISH_AUDIO: original language and a non-empty list of ruled-out streams."""
    if not isinstance(finding, dict):
        return "finding is not an object"
    kind = finding.get("type")
    if not isinstance(kind, str) or kind not in FINDING_TYPES:
        return f"unknown finding type {kind!r}"
    for field, ok in _FINDING_FIELDS[kind].items():
        if field not in finding or not ok(finding[field]):
            return f"{kind}.{field} is missing or has the wrong type"
    if kind == "NO_ENGLISH_AUDIO":
        streams = finding.get("streams")
        if not isinstance(streams, list) or not streams:
            return "NO_ENGLISH_AUDIO.streams is missing or not a non-empty list"
        for s in streams:
            if not isinstance(s, dict):
                return "NO_ENGLISH_AUDIO.streams has a non-object item"
            for field, ok in _STREAM_FIELDS.items():
                if field not in s or not ok(s[field]):
                    return f"NO_ENGLISH_AUDIO.streams[].{field} is missing or has the wrong type"
    return None


def _failed_problem(failed: object) -> str | None:
    if not isinstance(failed, dict):
        return "failed is not an object"
    attempts = failed.get("attempts")
    checks = {
        "identity": _is_identity(failed.get("identity")),
        "attempts": _is_index(attempts) and attempts >= 1,
        "last_result": isinstance(failed.get("last_result"), str)
        and failed["last_result"] in FAILED_RESULTS,
        "last_error": _is_str_or_none(failed.get("last_error")),
    }
    for field, ok in checks.items():
        if not ok:
            return f"failed.{field} is missing or has the wrong type"
    return None


def _entry_problem(entry: object) -> str | None:
    """Why `entry` is malformed, else `None`. Fields may be absent (a failure-only entry has
    no `identity`) but every present one must be valid; failure outcomes appear only inside
    `failed`. Keys this version does not use (module docstring) are not looked at."""
    if not isinstance(entry, dict):
        return "entry is not an object"
    if "result" in entry and not (isinstance(entry["result"], str)
                                  and entry["result"] in CONCLUSION_RESULTS):
        return (f"result {entry['result']!r} is not a conclusion "
                f"({', '.join(sorted(CONCLUSION_RESULTS))})")
    findings = entry.get("findings", [])
    if not isinstance(findings, list):
        return "findings is not a list"
    for f in findings:
        problem = _finding_problem(f)
        if problem:
            return problem
    checks = {
        "identity": _is_identity,
        "first_found": lambda v: isinstance(v, str),
        "alerted": lambda v: isinstance(v, bool),
        "checked_at": lambda v: isinstance(v, str),
    }
    for field, ok in checks.items():
        if field in entry and not ok(entry[field]):
            return f"{field} has the wrong type"
    if "failed" in entry:
        return _failed_problem(entry["failed"])
    return None


def entry_problem(entry: object) -> str | None:
    """`_entry_problem`, never raising."""
    try:
        return _entry_problem(entry)
    except Exception as exc:
        return f"could not be validated ({type(exc).__name__})"


def _load_state(path: Path) -> dict | None:
    """The state, `{}` if absent, `None` (logged) if unreadable or malformed; `main()` then
    exits 1 rather than guess, since wrong guesses lose or repeat alerts."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        log(f"ERROR: {path} could not be read/parsed ({type(exc).__name__}: {exc})")
        return None
    if not isinstance(data, dict):
        log(f"ERROR: {path} is not a JSON object")
        return None
    for key, entry in data.items():
        problem = entry_problem(entry)
        if problem:
            log(f"ERROR: {path}: malformed entry for {key!r}: {problem}")
            return None
    return data


def _conclusion_entry(existing: dict, identity: list[int], rec: dict, checked_at: str) -> dict:
    """The whole entry for a conclusion at `identity`; anything unlisted (a `failed` record or
    a key of an older version too) is superseded. The same file with the same findings keeps
    `alerted` and `first_found`; new findings or a CHANGED identity start `alerted=False`."""
    findings = rec.get("findings") or []
    same_file = existing.get("identity") == identity
    entry: dict = {"identity": identity, "result": rec.get("result"),
                   "findings": findings, "checked_at": checked_at}
    if findings:
        entry["first_found"] = (existing["first_found"]
                                if same_file and isinstance(existing.get("first_found"), str)
                                else checked_at)
        entry["alerted"] = bool(same_file and existing.get("findings") == findings
                                and existing.get("alerted") is True)
    return entry


def _record_result(video: Path, identity: list[int], rec: dict, checked_at: str) -> bool:
    """Record a conclusion in one locked update. `False` on a write failure or an entry the
    loader would reject; the file is then redone next run."""
    key = str(video.resolve())

    def _upsert(state: dict) -> dict:
        existing = state.get(key)
        entry = _conclusion_entry(existing if isinstance(existing, dict) else {},
                                  identity, rec, checked_at)
        # Never write what the loader would reject: it would blind every later run.
        problem = entry_problem(entry)
        if problem:
            raise ValueError(f"refusing to record a malformed entry ({problem})")
        state[key] = entry
        return state

    try:
        mutate_state(STATE_FILE, _upsert)
    except Exception as exc:
        log(f"WARNING: {STATE_FILE} update failed for {video} "
            f"({type(exc).__name__}: {exc}); NOT recorded, will re-check next run")
        return False
    return True


def _record_failure(video: Path, identity: list[int], rec: dict) -> None:
    """Count a failed analysis in a `failed` record INSIDE the entry, leaving any earlier
    conclusion, findings and alert flag untouched; the count restarts when the identity
    changes. A leftover `retry` of an older version is dropped. Write failures are logged
    and swallowed."""
    key = str(video.resolve())

    def _upsert(state: dict) -> dict:
        entry = state.get(key)
        entry = entry if isinstance(entry, dict) else {}
        entry.pop("retry", None)
        entry["failed"] = {"identity": identity, "attempts": _failed_attempts(entry, identity) + 1,
                           "last_result": rec.get("result"), "last_error": rec.get("error")}
        problem = _failed_problem(entry["failed"])
        if problem:
            raise ValueError(f"refusing to record a malformed failure record ({problem})")
        state[key] = entry
        return state

    try:
        mutate_state(STATE_FILE, _upsert)
    except Exception as exc:
        log(f"WARNING: {STATE_FILE} failure record failed for {video} "
            f"({type(exc).__name__}: {exc}); will retry next run")


def _append_jsonl(path: Path, rec: dict) -> None:
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError as exc:
        log(f"WARNING: {path} append failed ({type(exc).__name__}: {exc})")


def _title_for(video: Path) -> str:
    """`"<Folder> / <stem>"`, or the bare filename outside the `movies/`/`tv/` layout."""
    try:
        rel = video.resolve().relative_to(MEDIA_ROOT.resolve())
    except (ValueError, OSError):
        return _clean_text(video.name)
    parts = rel.parts
    if len(parts) >= 2 and parts[0].lower() in ("movies", "tv"):
        folder = parts[1]
        return _clean_text(f"{folder} / {video.stem}" if len(parts) > 2 else folder)
    return _clean_text(video.name)


def _format_one_finding(f: dict) -> str:
    """`f` is a validated finding (`_finding_problem`)."""
    if f["type"] == "MISLABELED":
        return (f"stream {f['stream_index']} tagged {f['tag'] or 'und'} but detected "
                f"{f['detected_language']} (p={f['probability']:.2f})")
    heard = ", ".join(f"{s['index']} tagged {s['tag'] or 'und'} heard {s['detected_language']} "
                      f"(p={s['probability']:.2f})" for s in f["streams"])
    return f"no confirmed English audio (original language: English); streams: {heard}"


def _format_finding_line(path_str: str, findings: list[dict]) -> str:
    """One `- title: finding; finding` line."""
    body = "; ".join(_format_one_finding(f) for f in findings)
    return _clean_text(f"- {_title_for(Path(path_str))}: {body}")


def _tg_len(text: str) -> int:
    """UTF-16 code units, as Telegram counts."""
    return len(_clean_text(text).encode("utf-16-le")) // 2


def _truncate(text: str, limit: int) -> str:
    """Cut to `limit` Telegram characters, ending in an ellipsis if cut."""
    text = _clean_text(text)
    if _tg_len(text) <= limit:
        return text
    cut = text[: max(limit - 1, 0)]
    while cut and _tg_len(cut) + 1 > limit:
        cut = cut[:-1]
    return cut + "…"


def build_alert_message(pending: list[tuple[str, dict]]) -> tuple[str, int]:
    """One message naming the first items of `pending` (oldest first) and how many it names:
    as many as fit `MAX_ALERT_ITEMS` and `TELEGRAM_BUDGET_CHARS`. Lines are cut to
    `MAX_ALERT_LINE_CHARS`, so the first always fits; the caller sends the rest in further
    messages."""
    text = "audio-lang-check: new audio-language findings"
    named = 0
    for path_str, entry in pending[:MAX_ALERT_ITEMS]:
        line = _truncate(_format_finding_line(path_str, entry["findings"]), MAX_ALERT_LINE_CHARS)
        if _tg_len(text) + 1 + _tg_len(line) > TELEGRAM_BUDGET_CHARS:
            break
        text += "\n" + line
        named += 1
    return text, named


def _telegram_credentials() -> tuple[str, str]:
    """`(token, chat_id)`, either possibly empty. Environment wins (both set: `.env` not
    read); else `TELEGRAM_ENV` (default `~/.claude/channels/telegram/.env`, never this
    repo's) fills the gaps. A read failure is logged and fails soft."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if token and chat:
        return token, chat
    env_path = None
    try:
        env_path = Path(os.environ.get("TELEGRAM_ENV")
                        or Path.home() / ".claude" / "channels" / "telegram" / ".env")
        vals = load_env_file(env_path)
    except Exception as exc:
        log(f"notify: could not read the Telegram .env ({env_path}): "
            f"{type(exc).__name__}: {exc}")
        return token, chat
    return token or vals.get("TELEGRAM_BOT_TOKEN", ""), chat or vals.get("TELEGRAM_CHAT_ID", "")


def _notify(message: str) -> str | None:
    """Send `message`: `None` on success, else why not. Only the exception TYPE is reported
    (a message could carry the token)."""
    try:
        token, chat = _telegram_credentials()
        if not token or not chat:
            return "no Telegram credentials"
        if telegram_send(token, chat, _clean_text(message)):
            return None
        return "Telegram did not accept the message"
    except Exception as exc:
        return f"send raised {type(exc).__name__}"


def _alert_failed(reason: str, paths: list[str]) -> None:
    log(f"ALERT NOT DELIVERED, NOT RETRIED ({reason}); findings for: {', '.join(paths)}", err=True)
    _append_jsonl(JSONL_FILE, {"event": "ALERT_FAILED", "reason": reason, "paths": paths,
                               "at": _utc_now_iso()})


def send_alerts() -> None:
    """One send attempt per new finding, oldest first, in as many messages as the length
    bounds need. PENDING: findings present, `alerted` not true, file still at the identity
    they were made on, checked again before each message (a gone or replaced file's findings
    are never sent). Every entry named in a message is then marked `alerted`, under the lock
    and only if unchanged since, whether or not Telegram took the message: a failed send is
    logged, never retried."""
    state = _load_state(STATE_FILE)
    if state is None:
        log("not alerting this run (state unreadable)")
        return
    rest = [(k, v) for k, v in state.items()
            if v.get("findings") and v.get("alerted") is not True and v.get("identity") is not None]
    rest.sort(key=lambda kv: kv[1].get("first_found") or "")
    attempted: dict[str, dict] = {}
    while True:
        # Checked again before every message: a file replaced while earlier ones were sending
        # is stale now.
        rest = [(k, v) for k, v in rest if _identity(Path(k)) == v["identity"]]
        if not rest:
            break
        text, named = build_alert_message(rest)
        reason = _notify(text)
        if reason:
            _alert_failed(reason, [k for k, _ in rest[:named]])
        else:
            log(f"alert sent for {named} file(s)")
        attempted.update(rest[:named])
        rest = rest[named:]
    if not attempted:
        return

    snapshot = attempted

    def _mark(current: dict) -> dict:
        for k, seen in snapshot.items():
            cur = current.get(k)
            # a rewritten entry is a new finding, not yet alerted
            if (isinstance(cur, dict) and cur.get("findings") == seen.get("findings")
                    and cur.get("identity") == seen.get("identity")):
                cur["alerted"] = True
        return current

    try:
        mutate_state(STATE_FILE, _mark)
    except Exception as exc:
        log(f"WARNING: failed to record alert delivery ({type(exc).__name__}: {exc}); "
            f"will alert again next run")


def _process_one(
    video: Path, cfg: Config, jellyfin: JellyfinClient,
) -> dict | None:
    """`process_candidate` in a fresh workdir; `None` if `PlaybackWaitTimeout` ended it."""
    WORKDIR_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="audio-lang-check_", dir=WORKDIR_ROOT) as wd:
            return process_candidate(video, cfg, jellyfin, Path(wd))
    except PlaybackWaitTimeout as exc:
        log(f"{exc}; ending cleanly")
        return None


def _check_candidates(candidates: list[Path], cfg: Config) -> int:
    """Analyze `candidates` one at a time within the run budget and record each result.
    Exit status for `main()`."""
    if not candidates:
        log("nothing to do (0 candidates)")
        return 0

    jellyfin = JellyfinClient(cfg.jellyfin_url)
    deadline = time.monotonic() + cfg.budget_min * 60
    started = arr_unavailable = 0

    for video in candidates:
        if time.monotonic() >= deadline:
            log(f"budget ({cfg.budget_min:g} min) reached; "
                f"deferring {len(candidates) - started} remaining file(s) to the next run")
            break

        # Earlier candidates take minutes: the file may be gone or touched since the scan.
        before = _identity(video, time.time())
        if before is None:
            continue

        started += 1
        log(f"start ({started}/{len(candidates)}): {video}")
        rec = _process_one(video, cfg, jellyfin)
        if rec is None:
            return 0

        if _identity(video, time.time()) != before:
            log(f"DISCARDED (file changed or was touched during analysis): {video}")
            continue

        checked_at = _utc_now_iso()
        rec["checked_at"] = checked_at
        result = rec.get("result")

        if result == "ARR_UNAVAILABLE":
            arr_unavailable += 1
            continue
        if result == "DEPENDENCY_MISSING":
            _append_jsonl(JSONL_FILE, rec)
            log("ERROR: faster_whisper is not installed; every file would fail the "
                "same way -- ending the run")
            return 1
        if result in FAILED_RESULTS:
            _record_failure(video, before, rec)
            _append_jsonl(JSONL_FILE, rec)
            log(f"{result} (at most {MAX_FAILED_ATTEMPTS} attempts per file version): {video}"
                + (f" [{rec['error']}]" if rec.get("error") else ""))
            continue

        if not _record_result(video, before, rec, checked_at):
            continue
        _append_jsonl(JSONL_FILE, rec)
        log(f"{result} ({len(rec['findings'])} finding(s)): {video}")

    if arr_unavailable:
        log(f"{arr_unavailable} file(s) not checked: Radarr/Sonarr could not be asked; "
            f"the next run tries again")
    log(f"done: {started}/{len(candidates)} file(s) started this run")
    return 0


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_sigterm)

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="list candidates and do nothing else (no ASR, no state writes, "
                         "no Telegram); cannot be combined with --path")
    ap.add_argument("--path", metavar="FILE",
                    help="check a single file and print the result as JSON (still honours "
                         "the playback gate); records NOTHING: no state, no findings, no "
                         "Telegram, so it can never suppress the cron job's own alert")
    args = ap.parse_args()
    if args.dry_run and args.path:
        ap.error("--dry-run cannot be combined with --path (--path analyzes the file; "
                 "--dry-run promises not to)")
    global _LOG_TO_STDERR
    _LOG_TO_STDERR = bool(args.path)

    lock_fp = None
    if not args.dry_run:
        ensure_var_dirs()
        lock_fp = acquire_singleton_lock(LOCK_FILE)
        if lock_fp is None:
            if args.path:
                print(json.dumps({
                    "path": str(Path(args.path).resolve()), "result": "BUSY",
                    "reason": "another audio-lang-check run holds the lock"}, indent=2))
                return EXIT_BUSY
            return 0  # cron: another instance is running; exit quietly, not an error

    env = {**load_env_file(MEDIA_STACK_ROOT / ".env"), **os.environ}
    cfg = load_config(env)
    real_run = not args.dry_run

    if cfg.whisper_lid_model.endswith(".en"):
        log(f"ERROR: WHISPER_LID_MODEL={cfg.whisper_lid_model!r} is an English-only model "
            f"and cannot detect language -- it would always report 'en' with high confidence, "
            f"silently defeating the whole point of this check. Use a multilingual model "
            f"(e.g. 'small', 'base', 'medium').")
        if real_run:
            return 1

    if real_run and not cfg.jellyfin_key:
        log("ERROR: JELLYFIN_API_KEY is not set; a real run cannot fail closed on "
            "playback without it -- refusing to start blinded. --dry-run still works.")
        return 1

    if args.path:
        video = Path(args.path).resolve()
        if not video.is_file():
            log(f"ERROR: --path {video} is not a file")
            return 1
        rec = _process_one(video, cfg, JellyfinClient(cfg.jellyfin_url))
        if rec is None:
            print(json.dumps({"path": str(video), "result": "PLAYBACK_WAIT_TIMEOUT"}, indent=2))
            return 0
        # Never recorded: the cron run would skip this file and suppress its alert.
        print(json.dumps(rec, indent=2))
        return 0

    state = _load_state(STATE_FILE)
    if state is None:
        log("run blinded: the state is unreadable or malformed (nothing was written)")
        return 1

    candidates = select_candidates(state, MEDIA_ROOT, time.time())

    if args.dry_run:
        log(f"DRY RUN: {len(candidates)} candidate(s) would be checked "
            f"(nothing probed, nothing transcribed, nothing written)")
        for p in candidates:
            log(f"  would check: {p}")
        return 0

    try:
        return _check_candidates(candidates, cfg)
    finally:
        try:
            send_alerts()
        except Exception as exc:
            log(f"WARNING: alerting failed ({type(exc).__name__}: {exc})", err=True)


if __name__ == "__main__":
    sys.exit(main())
