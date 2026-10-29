#!/usr/bin/env python3
"""Supply a verified English subtitle for files waiting too long on Bazarr.

`consolidate-subs.py` records files with no English candidate in
`var/state/needs-english.json` (first_seen/last_seen, keyed by resolved path). Once a
file has waited past `SUB_FALLBACK_AGE_HOURS` (default 24), this script tries subliminal
(via `media_stack.subtitles.fetch_via_subliminal`), then local whisper transcription,
and accepts only a result verified to belong to THIS file. Working files live in a
per-file temp directory under `WORKDIR_ROOT` (local disk, never the media pool).

VERIFICATION
  - Subliminal can return the WRONG episode: `media_stack.subverify.overlap()` needs BOTH
    a unigram word ratio AND an ordered-bigram ratio (calibration in its docstring).
  - A file named `<episode>.en.srt` may not be English: `media_stack.sublang` scores the TEXT.
  - An UNTAGGED/`und` audio stream may not be English: `detect_language` (a MULTILINGUAL
    model, never `.en`) must confirm it first.
  If subliminal's candidate fails, the whole file is transcribed. An ASR of the episode
  itself is always the right episode, so only "did it produce speech" applies. A Whisper
  transcript is published as `<stem>.en.generated.srt` (subliminal's stays `<stem>.en.srt`)
  so consolidate-subs.py prefers any real subtitle later and names the muxed track
  "English (generated)".

NO-CLOBBER PLACEMENT (`_place_sidecar`): right before writing, the file is re-probed; if
CONSOLIDATED_SUBS appeared or any English sidecar now exists, this run backs off
(ALREADY_CONSOLIDATED / SIDECAR_APPEARED). The sidecar goes to a hidden, UNIQUE-per-attempt
temp file (`tempfile.mkstemp`) and is published with `os.link()`, never a rename, so an
existing file is never touched and no truncated subtitle reaches the library.

SAME-STEM SIBLING VIDEOS: with `Episode.mp4` and `Episode.mkv` both present, a sidecar is
ambiguous (`consolidate-watch.sh` tries `.mkv` first). Such a candidate is skipped
(SIBLING_COLLISION) before any work and re-checked before publishing.

PLAYBACK: FAILS CLOSED, BOUNDED. `None` from `JellyfinClient.now_playing_sessions` means
"someone is watching"; every ffmpeg extraction is pause-aware (`media_stack.subverify`
`should_abort`). A wait past `SUB_FALLBACK_MAX_WAIT_MIN` ends the WHOLE run cleanly (exit 0;
the next hourly tick retries). A real run without `JELLYFIN_API_KEY` refuses to start
(exit 1); `--dry-run` is exempt. The transcription child gets this process's validated
key/URL and refuses to extract without one.

RUNTIME BOUNDS: full-file transcription (the only unbounded step) runs as `python3 -m
media_stack.subverify transcribe ...` under a hard wall-clock timeout
(`SUB_FALLBACK_ASR_TIMEOUT_MIN`) so it can be killed (ASR_TIMEOUT). One is never STARTED with
under 10 minutes of the run's `SUB_FALLBACK_BUDGET_MIN` left (DEFERRED_BUDGET; the tracker
entry is untouched).

ONE FILE AT A TIME: `consolidate-watch.sh` has no concurrency cap on sidecar arrivals, so
this places one sidecar, waits for that file's pipeline run to finish or time out, then
starts the next.

Exit codes:
  0  ok, including nothing to do, a second instance finding the lock held, and a
     playback wait that exceeded SUB_FALLBACK_MAX_WAIT_MIN
  1  blinded: needs-english.json unreadable, or a real run with no JELLYFIN_API_KEY
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from media_stack.clients.jellyfin import JellyfinClient  # noqa: E402
from media_stack.envconfig import env_float, env_int  # noqa: E402
from media_stack.fileid import identity_matches  # noqa: E402
from media_stack.locking import acquire_file_lock  # noqa: E402
from media_stack.paths import (  # noqa: E402
    MEDIA_LAN_IP, MEDIA_STACK_ROOT, VAR_DIR, VAR_LOG, VAR_RUN, VAR_STATE,
    ensure_var_dirs, load_env_file,
)
from media_stack.playback import (  # noqa: E402
    PLAYBACK_POLL_S, PlaybackWaitBudget, PlaybackWaitTimeout, call_with_playback_pause,
    wait_while_playing as _shared_wait_while_playing,
)
from media_stack.probe import already_processed, probe  # noqa: E402
from media_stack.state import load_state_locked, mutate_state  # noqa: E402
from media_stack.sublang import score as sublang_score  # noqa: E402
from media_stack.subtitles import fetch_via_subliminal, find_sidecar_subs  # noqa: E402
from media_stack.subverify import (  # noqa: E402
    CHILD_PLAYBACK_WAIT_REPORT_FILENAME, MIN_ASR_WORDS, MIN_BIGRAM_RATIO,
    MIN_WORD_RATIO, asr_window, detect_language, english_audio_stream,
    is_audio_explicitly_english, overlap,
)

ensure_var_dirs()  # writer: lock file + jsonl log

NEEDS_ENGLISH_STATE_FILE = VAR_STATE / "needs-english.json"   # written by consolidate-subs.py
# Sidecars this script placed (and so verified); see `_sidecar_matches_our_placement`.
SUB_FALLBACK_PLACEMENTS_FILE = VAR_STATE / "sub-fallback-placements.json"
LOCK_FILE = VAR_RUN / "sub-fallback.lock"
JSONL_FILE = VAR_LOG / "sub-fallback.jsonl"

# Local disk: a temp file on the pool would compete for the I/O the playback gate protects.
WORKDIR_ROOT = VAR_DIR / "tmp"

MIN_WHISPER_CUES = 1   # fewer cues than this means no speech

LID_PROBABILITY_FLOOR = 0.6
LID_WINDOW_S = 30.0
# 25% first (avoids cold-open music/silence); a low-confidence result retries at 50%, then 10%.
LID_FRACTIONS = (0.25, 0.50, 0.10)

# Never START a full transcription with less than this left of the run budget.
DEFER_TRANSCRIPTION_IF_BUDGET_UNDER_S = 10 * 60

PIPELINE_POLL_S = 20
SUBPROCESS_EXIT_POLL_S = 0.2

# mkstemp's 0600 would survive the hard link and hide the file from Jellyfin/Bazarr.
SIDECAR_MODE = 0o644
# ".generated" lets a real subtitle outrank a Whisper one; find_sidecar_subs and
# consolidate-watch.sh treat it as a tag beside ".en".
PLAIN_SIDECAR_SUFFIX = ".en.srt"
GENERATED_SIDECAR_SUFFIX = ".en.generated.srt"


class ASRTimeout(Exception):
    """Transcription child exceeded `Config.asr_timeout_min` and was killed."""


class MissingJellyfinKey(Exception):
    """Transcription child exited 3 (no key); the parent always passes one, so it is a bug signal."""


@dataclass
class Config:
    jellyfin_url: str
    jellyfin_key: str
    age_hours: float
    budget_min: float
    pipeline_timeout_min: float
    max_wait_min: float
    asr_timeout_min: float
    whisper_model: str
    whisper_verify_model: str
    whisper_lid_model: str
    whisper_threads: int


def _env_float(env: dict[str, str], key: str, default: float, max_value: float) -> float:
    """See `media_stack.envconfig.env_float`."""
    return env_float(env, key, default, max_value, log=log)


def _env_int(env: dict[str, str], key: str, default: int, max_value: int) -> int:
    """Integer `_env_float`."""
    return env_int(env, key, default, max_value, log=log)


# Past these a value is a typo that overflows on conversion (`* 60` -> inf, `timedelta`).
_MAX_MINUTES_SETTING = 1440.0
_MAX_AGE_HOURS = 8760.0


def load_config(env: dict[str, str]) -> Config:
    # The default must respect the bound `_env_int` enforces on overrides.
    cpu_bound = os.cpu_count() or 64
    default_threads = min(6, cpu_bound)
    return Config(
        jellyfin_url=env.get("JELLYFIN_URL") or f"http://{MEDIA_LAN_IP}:8096",
        jellyfin_key=env.get("JELLYFIN_API_KEY", ""),
        age_hours=_env_float(env, "SUB_FALLBACK_AGE_HOURS", 24.0, _MAX_AGE_HOURS),
        budget_min=_env_float(env, "SUB_FALLBACK_BUDGET_MIN", 50.0, _MAX_MINUTES_SETTING),
        pipeline_timeout_min=_env_float(
            env, "SUB_FALLBACK_PIPELINE_TIMEOUT_MIN", 60.0, _MAX_MINUTES_SETTING),
        max_wait_min=_env_float(env, "SUB_FALLBACK_MAX_WAIT_MIN", 30.0, _MAX_MINUTES_SETTING),
        asr_timeout_min=_env_float(
            env, "SUB_FALLBACK_ASR_TIMEOUT_MIN", 90.0, _MAX_MINUTES_SETTING),
        whisper_model=env.get("WHISPER_MODEL") or "medium.en",
        whisper_verify_model=env.get("WHISPER_VERIFY_MODEL") or "small.en",
        whisper_lid_model=env.get("WHISPER_LID_MODEL") or "small",
        whisper_threads=_env_int(env, "WHISPER_THREADS", default_threads, cpu_bound),
    )


def log(msg: str) -> None:
    """Timestamped stdout line (cron redirects to var/log/sub-fallback.log)."""
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: object) -> datetime | None:
    """Parse both formats `needs-english.json` holds: `isoformat()` (`+00:00`; 3.10's
    `fromisoformat` rejects `Z`) and the old `%Y-%m-%dT%H:%M:%SZ`. The file is never migrated."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_needs_english(path: Path) -> dict | None:
    """Tracker dict, `{}` if absent, `None` if unreadable or not an object (main() exits 1).
    Not `load_state`, which maps "unreadable" to `{}`: a corrupt tracker must be loud."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def select_candidates(
    state: dict,
    now: datetime,
    age_hours: float,
    file_exists: Callable[[Path], bool],
    is_consolidated: Callable[[Path], bool],
) -> tuple[list[Path], list[str]]:
    """`(candidates, stale_keys)`: files waiting >= `age_hours` that exist and lack
    CONSOLIDATED_SUBS, oldest first; and gone/consolidated/malformed entries to drop."""
    aged: list[tuple[datetime, Path]] = []
    stale: list[str] = []
    for key, entry in state.items():
        path = Path(key)
        if not file_exists(path) or is_consolidated(path):
            stale.append(key)
            continue
        first_seen = _parse_iso(entry.get("first_seen")) if isinstance(entry, dict) else None
        if first_seen is None:
            stale.append(key)
            continue
        if now - first_seen >= timedelta(hours=age_hours):
            aged.append((first_seen, path))
    aged.sort(key=lambda t: t[0])
    return [p for _, p in aged], stale


def _has_consolidated_tag(path: Path) -> bool:
    info = probe(path)
    return bool(info) and already_processed(info)


def drop_stale_entries(stale_entries: dict[str, object]) -> int:
    """Remove a key only if its CURRENT entry still equals the snapshot that judged it
    stale (`consolidate-subs.py` may have upserted a FRESH one), compared inside
    `mutate_state`'s lock. Returns the number removed."""
    if not stale_entries:
        return 0
    removed_count = 0

    def _drop(state: dict) -> dict:
        nonlocal removed_count
        removed_count = 0
        for k, snapshot_entry in stale_entries.items():
            if state.get(k) == snapshot_entry:
                state.pop(k, None)
                removed_count += 1
        return state

    mutate_state(NEEDS_ENGLISH_STATE_FILE, _drop)
    return removed_count


def wait_while_playing(jellyfin: JellyfinClient, cfg: Config,
                       budget: PlaybackWaitBudget) -> None:
    """`media_stack.playback.wait_while_playing` bound to `cfg`."""
    _shared_wait_while_playing(jellyfin, cfg.jellyfin_key, cfg.max_wait_min, budget,
                               poll_s=PLAYBACK_POLL_S, log=log)


def _call_with_playback_pause(fn: Callable, jellyfin: JellyfinClient, cfg: Config,
                              budget: PlaybackWaitBudget, *args, **kwargs):
    """`media_stack.playback.call_with_playback_pause` bound to `cfg`."""
    return call_with_playback_pause(fn, jellyfin, cfg.jellyfin_key, cfg.max_wait_min, budget,
                                    *args, poll_s=PLAYBACK_POLL_S, log=log, **kwargs)


def _read_child_playback_wait_report(workdir: Path) -> float:
    """Seconds the child spent waiting, from its report file; missing or bad is `0.0`
    (bookkeeping must never fail a successful transcription)."""
    report = workdir / CHILD_PLAYBACK_WAIT_REPORT_FILENAME
    try:
        return float(report.read_text().strip())
    except (OSError, ValueError):
        return 0.0


def _wait_for_exit_unreaped(pid: int, timeout_s: float) -> bool:
    """Poll for `pid`'s exit WITHOUT reaping it (`os.waitid` + `WNOWAIT`): reaping frees
    the pid and process-group id for reuse, so a later `killpg` could hit an unrelated
    group. The zombie keeps the id reserved until the caller kills the group, then reaps.
    `True` if the wait timed out (caller must kill)."""
    t0 = time.monotonic()
    while True:
        try:
            exited = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            exited = True
        if exited:
            return False
        if time.monotonic() - t0 >= timeout_s:
            return True
        time.sleep(SUBPROCESS_EXIT_POLL_S)


def _transcribe_in_subprocess(video: Path, audio_idx: int, cfg: Config,
                              budget: PlaybackWaitBudget, workdir: Path,
                              timeout_s: float) -> str:
    """Run the transcribe child (`TMPDIR=workdir`) under a hard wall-clock timeout.

    The child gets `cfg`'s key/URL and `SUB_FALLBACK_MAX_WAIT_MIN` = `budget`'s CURRENT
    remaining allowance; the seconds it spent waiting are deducted from `budget` on EVERY
    exit path. Exit 2 maps to `PlaybackWaitTimeout`, exit 3 to `MissingJellyfinKey`.

    `start_new_session=True` puts the child and its ffmpeg in one process group, which the
    `finally` ALWAYS kills (timeout, Ctrl-C, SIGTERM's `SystemExit`, clean exit); only a
    timeout becomes `ASRTimeout`. stdout/stderr go to FILES, never PIPEs, and the wait uses
    `_wait_for_exit_unreaped`: kill the group FIRST, reap SECOND, leader unreaped between,
    which makes `killpg` safe against pid reuse.
    """
    cmd = [sys.executable, "-m", "media_stack.subverify", "transcribe",
           str(video), str(audio_idx), cfg.whisper_model, str(cfg.whisper_threads)]
    remaining_wait_min = max(0.0, budget.remaining_s) / 60.0
    env = {
        **os.environ,
        "TMPDIR": str(workdir),
        "JELLYFIN_API_KEY": cfg.jellyfin_key,
        "JELLYFIN_URL": cfg.jellyfin_url,
        "SUB_FALLBACK_MAX_WAIT_MIN": f"{remaining_wait_min:g}",
    }
    stdout_path = workdir / "_transcribe.stdout"
    stderr_path = workdir / "_transcribe.stderr"
    with open(stdout_path, "wb") as out_fh, open(stderr_path, "wb") as err_fh:
        proc = subprocess.Popen(
            cmd, cwd=str(MEDIA_STACK_ROOT), env=env,
            stdout=out_fh, stderr=err_fh,
            start_new_session=True,
        )
    pgid = proc.pid  # new session: pgid == pid
    timed_out = False
    try:
        timed_out = _wait_for_exit_unreaped(pgid, timeout_s)
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        stdout = stdout_path.read_text(errors="replace")
        stderr = stderr_path.read_text(errors="replace")
        budget.spend(_read_child_playback_wait_report(workdir))
    if timed_out:
        raise ASRTimeout(f"transcription exceeded {timeout_s:g}s: {video}")
    if proc.returncode == 2:
        raise PlaybackWaitTimeout(
            f"transcription subprocess hit its own playback-wait bound: {video}")
    if proc.returncode == 3:
        raise MissingJellyfinKey(
            f"transcription subprocess had no JELLYFIN_API_KEY: {video}")
    if proc.returncode != 0:
        raise RuntimeError(f"transcribe subprocess failed ({video}): {(stderr or '').strip()[:300]}")
    return stdout


def _pipeline_lock_is_free(path: Path) -> bool:
    """The tag is written BEFORE the inline normalization that holds this lock, so the tag
    alone does not mean the pipeline is done."""
    with acquire_file_lock(path) as acquired:
        return acquired


def wait_for_pipeline(video: Path, timeout_s: float) -> tuple[bool, int]:
    """Poll until `video` has the CONSOLIDATED_SUBS tag AND its per-file lock is free
    (pipeline FINISHED). Returns `(reached, waited_seconds)`.

    Consolidation can replace an `.mp4` with a same-stem `.mkv`, so the `.mkv` is checked,
    but ONLY once `video` no longer exists: a `.mkv` beside a present `.mp4` is a sibling
    collision, and crediting its tag here would be as wrong as placing a sidecar for it.
    """
    mkv_alt = video.with_suffix(".mkv")
    accept_mkv_alt = mkv_alt != video
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        info = probe(video)
        if info and already_processed(info) and _pipeline_lock_is_free(video):
            return True, int(time.monotonic() - t0)
        if accept_mkv_alt and not video.exists():
            info = probe(mkv_alt)
            if info and already_processed(info) and _pipeline_lock_is_free(mkv_alt):
                return True, int(time.monotonic() - t0)
        time.sleep(PIPELINE_POLL_S)
    return False, int(time.monotonic() - t0)


def _video_identity(video: Path) -> tuple[int, int, int, int, int] | None:
    """`(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)`; ctime exposes a same-size
    rewrite with mtime restored. `None` if gone."""
    try:
        st = video.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _video_identity_sans_dev(video: Path) -> list[int] | None:
    """`_video_identity` minus `st_dev`, as a list, for the placement record. A legacy
    three-field record (no ctime) counts as changed (no retrigger); the tracker identity
    means only "same release" (see `media_stack.fileid.identity_matches`)."""
    identity = _video_identity(video)
    return list(identity[1:]) if identity else None


# `consolidate-watch.sh` matches a sidecar's stem only against these (`.mkv` first).
_WATCHER_STEM_MATCH_EXTS = (".mkv", ".mp4")


def _sibling_video_collision(video: Path) -> Path | None:
    """A same-stem sibling with another of `_WATCHER_STEM_MATCH_EXTS` (`Episode.mp4`
    beside `Episode.mkv`), else `None`; no sidecar may be placed while one exists."""
    if video.suffix.lower() not in _WATCHER_STEM_MATCH_EXTS:
        return None
    stem = video.stem
    try:
        entries = list(video.parent.iterdir())
    except OSError:
        return None
    for entry in entries:
        if entry == video:
            continue
        suffix = entry.suffix.lower()
        if suffix not in _WATCHER_STEM_MATCH_EXTS or suffix == video.suffix.lower():
            continue
        if entry.stem != stem:
            continue
        try:
            if entry.is_file():
                return entry
        except OSError:
            continue
    return None


def _main_sidecars(video: Path) -> list[tuple[Path, bool, bool]]:
    """Non-forced `find_sidecar_subs(video)`; callers check `_has_forced_sidecar` first."""
    return [s for s in find_sidecar_subs(video) if not s[1]]


def _has_forced_sidecar(video: Path) -> bool:
    """ANY forced English sidecar exists. consolidate-subs.py owns those (content-gates,
    embeds, removes), so this script leaves such a file to it and never touches one."""
    return any(is_forced for _, is_forced, _ in find_sidecar_subs(video))


def _load_placements(path: Path) -> dict:
    """`{}` on any failure (warning unless missing). Not blinding: no record means no
    retrigger (`SIDECAR_PRESENT`)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        log(f"WARNING: {path} unreadable ({type(exc).__name__}: {exc}); "
            f"treating as no known placements this run")
        return {}
    if not isinstance(data, dict):
        log(f"WARNING: {path} has an unexpected shape (not a JSON object); "
            f"treating as no known placements this run")
        return {}
    return data


def _record_placement(video: Path, dest: Path, text: str, source: str) -> None:
    """Record that this script placed (and verified) `dest`, bound to the video's identity
    (catches a replacement under an untouched sidecar), to authorize a later retrigger.
    Failures are logged and swallowed so a placement is never reported as failed."""
    key = str(video.resolve())
    record = {
        "sidecar": str(dest),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "source": source,
        "placed_at": _utc_now_iso(),
        "video_identity": _video_identity_sans_dev(video),
    }

    def _upsert(state: dict) -> dict:
        state[key] = record
        return state

    try:
        mutate_state(SUB_FALLBACK_PLACEMENTS_FILE, _upsert)
    except Exception as exc:
        log(f"WARNING: {SUB_FALLBACK_PLACEMENTS_FILE} upsert failed for "
            f"{video} ({type(exc).__name__}: {exc}); placement not recorded")


def _clear_placement(key: str) -> None:
    """Drop `key` from the placements file; failures are logged and swallowed."""
    def _drop(state: dict) -> dict:
        state.pop(key, None)
        return state

    try:
        mutate_state(SUB_FALLBACK_PLACEMENTS_FILE, _drop)
    except Exception as exc:
        log(f"WARNING: {SUB_FALLBACK_PLACEMENTS_FILE} clear failed for {key} "
            f"({type(exc).__name__}: {exc})")


def _sidecar_matches_our_placement(video: Path, sidecar: Path) -> bool:
    """`sidecar` is the one this script placed for `video` (same path and sha256) AND
    `video` is unchanged since; any other sidecar or a swapped video must not be
    retriggered onto."""
    record = _load_placements(SUB_FALLBACK_PLACEMENTS_FILE).get(str(video.resolve()))
    if not isinstance(record, dict):
        return False
    recorded_sidecar = record.get("sidecar")
    recorded_sha256 = record.get("sha256")
    recorded_video_identity = record.get("video_identity")
    if not isinstance(recorded_sidecar, str) or not isinstance(recorded_sha256, str):
        return False
    if Path(recorded_sidecar) != sidecar:
        return False
    current = _video_identity_sans_dev(video)
    if not identity_matches(recorded_video_identity, current, allow_legacy=False):
        return False
    try:
        current_bytes = sidecar.read_bytes()
    except OSError:
        return False
    return hashlib.sha256(current_bytes).hexdigest() == recorded_sha256


def _run_consolidate_subs_directly(video: Path, timeout_s: float, workdir: Path) -> tuple[bool, str]:
    """Run `consolidate-subs.py` as the watcher's inotify trigger would, when the
    `close_write` event could not be generated. Safe alongside a real watcher run (the
    per-file flock makes the loser a harmless SKIP).

    Same process-group discipline as `_transcribe_in_subprocess`. Returns
    `(ran_ok, detail)`, informational only: `wait_for_pipeline` verifies the tag landed.
    """
    script = MEDIA_STACK_ROOT / "consolidate-subs.py"
    stdout_path = workdir / "_consolidate_direct.stdout"
    stderr_path = workdir / "_consolidate_direct.stderr"
    with open(stdout_path, "wb") as out_fh, open(stderr_path, "wb") as err_fh:
        proc = subprocess.Popen(
            [sys.executable, str(script), str(video)],
            stdout=out_fh, stderr=err_fh,
            start_new_session=True,
        )
    pgid = proc.pid  # new session: pgid == pid
    timed_out = False
    try:
        timed_out = _wait_for_exit_unreaped(pgid, timeout_s)
    finally:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        stdout = stdout_path.read_text(errors="replace")
        stderr = stderr_path.read_text(errors="replace")
    if timed_out:
        return False, f"timed out after {timeout_s:g}s"
    detail = (stdout or stderr or "").strip()[-300:]
    return proc.returncode == 0, detail


def _sidecar_dest(video: Path, source: str) -> Path:
    """`<stem>.en.generated.srt` for Whisper, `<stem>.en.srt` otherwise."""
    suffix = GENERATED_SIDECAR_SUFFIX if source == "whisper" else PLAIN_SIDECAR_SUFFIX
    return video.with_name(video.stem + suffix)


_SIDECAR_TEMP_SUFFIX = ".sfb-tmp"


def _sidecar_temp_glob(dest: Path) -> str:
    """Glob for every `_place_sidecar` temp name for `dest`."""
    return "." + dest.name + ".*" + _SIDECAR_TEMP_SUFFIX


def _cleanup_stale_sidecar_temps(dest: Path) -> None:
    """Unlink temps from a crashed attempt; safe even if hard-linked to `dest` (drops a link)."""
    try:
        for stale in dest.parent.glob(_sidecar_temp_glob(dest)):
            stale.unlink(missing_ok=True)
    except OSError:
        pass


def _place_sidecar(video: Path, dest: Path, text: str, source: str,
                   *, video_identity: tuple[int, int, int, int, int] | None,
                   pipeline_timeout_s: float, rec: dict, workdir: Path) -> str | None:
    """Publish `text` as `dest` atomically, never exposing partial content or clobbering.

    On this mergerfs pool `renameat2(RENAME_NOREPLACE)` returns `EINVAL` but `os.link()`
    works and raises `EEXIST` on an existing target. So write the FULL text to a hidden,
    UNIQUE temp file in `dest`'s directory (not a subtitle extension, not starting with the
    stem, so neither `find_sidecar_subs` nor the watcher sees it), fsync, THEN `os.link()`.
    The name is unique per attempt: with a fixed one, a run dying after the link but before
    its `finally` leaves two names for one inode and the next `O_TRUNC` would truncate the
    published sidecar. Leftovers are swept first.

    Right before the link: re-probe (`ALREADY_CONSOLIDATED`), any forced sidecar
    (`FORCED_SIDECAR_PRESENT`), any main sidecar (`SIDECAR_APPEARED`), `video_identity`
    (`VIDEO_CHANGED`), same-stem sibling (`SIBLING_COLLISION`); any leaves `dest` untouched.
    `FileExistsError` is the real no-clobber guarantee (`SIDECAR_APPEARED`); any other
    `OSError` is `PLACE_FAILED`, NEVER a fallback to a clobbering rename.

    After the link, `dest` is opened `O_WRONLY|O_APPEND` and closed unwritten: `os.link()`
    fires only `IN_CREATE`, so this generates the `close_write` the watcher reacts to. The
    placement is recorded IMMEDIATELY after the link, before the nudge, so a nudge failure
    cannot skip it (`_record_placement` itself logs and swallows a failed state write). A failing nudge is recoverable: fall
    back to `_run_consolidate_subs_directly` (bounded by `pipeline_timeout_s`), record
    `rec["event_fallback_*"]`, still return `None`; `wait_for_pipeline` verifies the outcome.
    """
    _cleanup_stale_sidecar_temps(dest)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix="." + dest.name + ".", suffix=_SIDECAR_TEMP_SUFFIX,
        dir=str(dest.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            # fchmod is umask-independent.
            os.fchmod(fh.fileno(), SIDECAR_MODE)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())

        info = probe(video)
        if info is None:
            return "PROBE_FAILED"
        if already_processed(info):
            return "ALREADY_CONSOLIDATED"
        if _has_forced_sidecar(video):
            return "FORCED_SIDECAR_PRESENT"
        if _main_sidecars(video):
            return "SIDECAR_APPEARED"
        if _video_identity(video) != video_identity:
            return "VIDEO_CHANGED"
        if _sibling_video_collision(video) is not None:
            return "SIBLING_COLLISION"

        try:
            os.link(tmp, dest)
        except FileExistsError:
            return "SIDECAR_APPEARED"
        except OSError:
            return "PLACE_FAILED"

        _record_placement(video, dest, text, source)

        try:
            append_fd = os.open(dest, os.O_WRONLY | os.O_APPEND)
            os.close(append_fd)
        except OSError as exc:
            rec["event_fallback_used"] = True
            rec["event_fallback_error"] = f"{type(exc).__name__}: {exc}"
            try:
                ran_ok, detail = _run_consolidate_subs_directly(video, pipeline_timeout_s, workdir)
            except Exception as helper_exc:
                # A failing recovery must not turn a published sidecar into ERROR.
                rec["event_fallback_helper_error"] = f"{type(helper_exc).__name__}: {helper_exc}"
            else:
                rec["event_fallback_pipeline_ran_ok"] = ran_ok
                rec["event_fallback_pipeline_detail"] = detail
        return None
    finally:
        tmp.unlink(missing_ok=True)


def _verify_untagged_audio_english(
    video: Path, audio_idx: int, duration: float, cfg: Config,
    jellyfin: JellyfinClient, budget: PlaybackWaitBudget, workdir: Path, rec: dict,
) -> bool:
    """Whisper LID (MULTILINGUAL `cfg.whisper_lid_model`) says an UNTAGGED/`und` stream is
    English; retries other windows while probability < `LID_PROBABILITY_FLOOR` (e.g. a
    music bed). Records the final language/probability in `rec`."""
    lang: str | None = None
    prob = 0.0
    for frac in LID_FRACTIONS:
        start = max(0.0, duration * frac)
        lang, prob = _call_with_playback_pause(
            detect_language, jellyfin, cfg, budget,
            video, audio_idx, start, LID_WINDOW_S,
            cfg.whisper_lid_model, cfg.whisper_threads, workdir,
        )
        if prob >= LID_PROBABILITY_FLOOR:
            break
    rec["audio_lid_language"] = lang
    rec["audio_lid_probability"] = round(prob, 3)
    return lang == "en" and prob >= LID_PROBABILITY_FLOOR


def _verify_subliminal_candidate(
    video: Path, audio_idx: int, srt_text: str, start: float, window: float,
    cfg: Config, jellyfin: JellyfinClient, budget: PlaybackWaitBudget,
    workdir: Path, rec: dict,
) -> bool:
    """Accept `srt_text` only if it scores English AND an ASR window overlaps it on BOTH
    ratios; a second, later window is tried when the first has too little speech.
    Records every number in `rec`."""
    lang = sublang_score(srt_text)
    rec["subliminal_lang_verdict"] = lang["verdict"]
    rec["subliminal_lang_eng_ratio"] = lang["eng"]
    if lang["verdict"] != "eng":
        return False

    asr_text = _call_with_playback_pause(
        asr_window, jellyfin, cfg, budget, video, audio_idx, start, window,
        cfg.whisper_verify_model, cfg.whisper_threads, workdir)
    word_ratio, bigram_ratio, nwords = overlap(asr_text, srt_text, start, window)
    if nwords < MIN_ASR_WORDS:
        start2 = start + window
        asr_text2 = _call_with_playback_pause(
            asr_window, jellyfin, cfg, budget, video, audio_idx, start2, window,
            cfg.whisper_verify_model, cfg.whisper_threads, workdir)
        word_ratio, bigram_ratio, nwords = overlap(asr_text2, srt_text, start2, window)
    rec["subliminal_word_ratio"] = round(word_ratio, 3)
    rec["subliminal_bigram_ratio"] = round(bigram_ratio, 3)
    rec["subliminal_asr_words"] = nwords
    if nwords < MIN_ASR_WORDS:
        rec["subliminal_reject_reason"] = "too little speech to verify"
        return False
    if bigram_ratio < MIN_BIGRAM_RATIO or word_ratio < MIN_WORD_RATIO:
        rec["subliminal_reject_reason"] = "wrong episode (low overlap)"
        return False
    return True


def _revalidate_candidate(video: Path, cfg: Config) -> str | None:
    """Re-apply `select_candidates`'s rules to the CURRENT tracker entry: earlier
    candidates take minutes, during which consolidate-subs.py may have cleared it (FIXED)
    or re-recorded it with a fresh `first_seen` (file replaced). `load_state_locked`
    avoids a torn read. `None` if still valid, else a `REVALIDATION_SKIP` reason."""
    try:
        state = load_state_locked(NEEDS_ENGLISH_STATE_FILE)
    except OSError as exc:
        return f"tracker unreadable: {exc}"
    entry = state.get(str(video.resolve()))
    if not isinstance(entry, dict):
        return "entry no longer present"
    first_seen = _parse_iso(entry.get("first_seen"))
    if first_seen is None:
        return "entry malformed (no first_seen)"
    if datetime.now(timezone.utc) - first_seen < timedelta(hours=cfg.age_hours):
        return "first_seen no longer past the grace period (entry was reset)"
    if not video.is_file():
        return "file no longer exists"
    if _has_consolidated_tag(video):
        return "already consolidated"
    stored_identity = entry.get("identity")
    if stored_identity is not None:
        current = _video_identity_sans_dev(video)
        # Tracker identity has three fields (ino, size, mtime_ns).
        if not identity_matches(stored_identity, current, allow_legacy=True):
            return "file identity no longer matches the tracked entry"
    return None


def process_candidate(video: Path, cfg: Config, jellyfin: JellyfinClient,
                      workdir: Path, budget_deadline: float) -> dict:
    """Give `video` a verified English sidecar. Raises only `PlaybackWaitTimeout`, which
    ends the run cleanly.

    `budget_deadline` is an ABSOLUTE `time.monotonic()` run deadline, checked when a
    transcription would actually start. The local `PlaybackWaitBudget` is per candidate,
    so `SUB_FALLBACK_MAX_WAIT_MIN` bounds their TOTAL.
    """
    rec: dict = {"path": str(video)}
    t0 = time.monotonic()
    try:
        revalidation_reason = _revalidate_candidate(video, cfg)
        if revalidation_reason is not None:
            rec["result"] = "REVALIDATION_SKIP"
            rec["reason"] = revalidation_reason
            return rec

        if _sibling_video_collision(video) is not None:
            rec["result"] = "SIBLING_COLLISION"
            return rec

        initial_identity = _video_identity(video)  # before the wait: a long wait is when a file gets replaced
        budget = PlaybackWaitBudget(cfg.max_wait_min)  # cumulative across every wait below
        wait_while_playing(jellyfin, cfg, budget)

        info = probe(video)
        if info is None:
            rec["result"] = "PROBE_FAILED"
            return rec
        if already_processed(info):
            rec["result"] = "ALREADY_CONSOLIDATED"
            return rec

        if _has_forced_sidecar(video):
            rec["result"] = "FORCED_SIDECAR_PRESENT"
            return rec

        main_sidecars = _main_sidecars(video)
        if main_sidecars:
            # Untagged video with a main sidecar: the close_write was probably missed.
            # Retrigger ONLY for exactly one sidecar that is our recorded placement for this
            # exact video (language alone can't tell the right episode from a wrong one);
            # anything else is `SIDECAR_PRESENT`, no action.
            if len(main_sidecars) == 1 and _sidecar_matches_our_placement(
                    video, main_sidecars[0][0]):
                ran_ok, detail = _run_consolidate_subs_directly(
                    video, cfg.pipeline_timeout_min * 60, workdir)
                reached, waited_s = wait_for_pipeline(video, cfg.pipeline_timeout_min * 60)
                rec["result"] = "PIPELINE_RETRIGGERED"
                rec["pipeline_retrigger_ran_ok"] = ran_ok
                rec["pipeline_retrigger_detail"] = detail
                rec["pipeline_retrigger_tag_landed"] = reached
                rec["pipeline_retrigger_wait_s"] = waited_s
                return rec
            rec["result"] = "SIDECAR_PRESENT"
            return rec

        duration = float((info.get("format") or {}).get("duration") or 0)

        audio_idx = english_audio_stream(info)
        if audio_idx is None:
            rec["result"] = "NO_ENGLISH_AUDIO"
            return rec

        if not is_audio_explicitly_english(info, audio_idx):
            if not _verify_untagged_audio_english(video, audio_idx, duration, cfg,
                                                  jellyfin, budget, workdir, rec):
                rec["result"] = "AUDIO_LANGUAGE_UNVERIFIED"
                return rec

        start = min(300.0, max(60.0, duration * 0.25))
        window = min(150.0, max(60.0, duration * 0.2))

        chosen_text: str | None = None
        source: str | None = None

        fetched = fetch_via_subliminal(video, workdir)
        if fetched is not None:
            txt = fetched.read_text(encoding="utf-8", errors="replace")
            if _verify_subliminal_candidate(video, audio_idx, txt, start, window,
                                            cfg, jellyfin, budget, workdir, rec):
                chosen_text, source = txt, "subliminal"
            fetched.unlink(missing_ok=True)

        if chosen_text is None:
            remaining_budget_s = budget_deadline - time.monotonic()
            if remaining_budget_s < DEFER_TRANSCRIPTION_IF_BUDGET_UNDER_S:
                rec["result"] = "DEFERRED_BUDGET"
                return rec
            if budget.remaining_s <= 0:
                # Defensive, normally unreachable: an exhausted allowance would make the child time out at once.
                raise PlaybackWaitTimeout(
                    f"cumulative playback wait for this candidate exceeded "
                    f"{cfg.max_wait_min:g} min")
            try:
                srt_text = _transcribe_in_subprocess(
                    video, audio_idx, cfg, budget, workdir, cfg.asr_timeout_min * 60)
            except ASRTimeout:
                rec["result"] = "ASR_TIMEOUT"
                return rec
            except MissingJellyfinKey as exc:
                rec["result"] = "MISSING_JELLYFIN_KEY"
                rec["error"] = str(exc)
                return rec
            cue_count = srt_text.count(" --> ")
            rec["whisper_cues"] = cue_count
            if cue_count >= MIN_WHISPER_CUES:
                chosen_text, source = srt_text, "whisper"
            else:
                rec["result"] = "TOO_FEW_CUES"
                return rec

        rec["source"] = source
        wait_while_playing(jellyfin, cfg, budget)  # ASR/subliminal take minutes; re-check before placing
        dest = _sidecar_dest(video, source)
        block_reason = _place_sidecar(video, dest, chosen_text, source,
                                      video_identity=initial_identity,
                                      pipeline_timeout_s=cfg.pipeline_timeout_min * 60,
                                      rec=rec, workdir=workdir)
        if block_reason is not None:
            rec["result"] = block_reason
            return rec

        reached, waited_s = wait_for_pipeline(video, cfg.pipeline_timeout_min * 60)
        rec["pipeline_reached"] = reached
        rec["pipeline_wait_s"] = waited_s
        rec["result"] = "FIXED" if reached else "PIPELINE_TIMEOUT"
        return rec
    except PlaybackWaitTimeout:
        raise
    except Exception as exc:
        rec["result"] = "ERROR"
        rec["error"] = f"{type(exc).__name__}: {exc}"
        return rec
    finally:
        rec["elapsed_s"] = round(time.monotonic() - t0, 1)


def _append_jsonl(path: Path, rec: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def acquire_singleton_lock(lock_path: Path):
    """The locked file (keep it referenced: closing drops the flock), or `None` if held."""
    lock_fp = open(lock_path, "w")
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fp.close()
        return None
    return lock_fp


def _handle_sigterm(signum: int, frame: object) -> None:
    """SystemExit on SIGTERM so `finally` blocks run (else the transcribe child and ffmpeg are orphaned)."""
    raise SystemExit(128 + signum)


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_sigterm)

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="list candidates and what would be attempted; fetch/mutate nothing")
    args = ap.parse_args()

    lock_fp = acquire_singleton_lock(LOCK_FILE)
    if lock_fp is None:
        return 0  # another instance is running; exit quietly, not an error

    env = {**load_env_file(MEDIA_STACK_ROOT / ".env"), **os.environ}
    cfg = load_config(env)

    if cfg.whisper_lid_model.endswith(".en"):
        log(f"ERROR: WHISPER_LID_MODEL={cfg.whisper_lid_model!r} is an English-only model "
            f"and cannot detect language -- it would always report 'en' with high "
            f"confidence, silently defeating the whole point of verifying an untagged "
            f"audio stream. Use a multilingual model (e.g. 'small', 'base', 'medium').")
        if not args.dry_run:
            return 1

    if not args.dry_run and not cfg.jellyfin_key:
        log("ERROR: JELLYFIN_API_KEY is not set; a real run cannot fail closed on "
            "playback without it -- refusing to start blinded. --dry-run still works.")
        return 1

    state = load_needs_english(NEEDS_ENGLISH_STATE_FILE)
    if state is None:
        log(f"ERROR: {NEEDS_ENGLISH_STATE_FILE} exists but could not be read/parsed; run blinded")
        return 1

    now = datetime.now(timezone.utc)
    candidates, stale = select_candidates(
        state, now, cfg.age_hours, lambda p: p.is_file(), _has_consolidated_tag)

    if args.dry_run:
        log(f"DRY RUN: {len(candidates)} candidate(s) due, {len(stale)} stale "
            f"entries would be dropped (nothing fetched, nothing written)")
        for p in candidates:
            log(f"  would attempt: {p}")
        return 0

    if stale:
        # `drop_stale_entries` skips keys re-recorded since the snapshot.
        removed = drop_stale_entries({k: state[k] for k in stale})
        kept = len(stale) - removed
        msg = f"dropped {removed} stale tracker entries (file gone or already consolidated)"
        if kept:
            msg += f"; {kept} re-recorded since the snapshot and kept"
        log(msg)
        # Every stale key, even one kept above: losing a retrigger in that race is harmless.
        for key in stale:
            _clear_placement(key)

    if not candidates:
        log("nothing to do (0 candidates past the age threshold)")
        return 0

    jellyfin = JellyfinClient(cfg.jellyfin_url)
    WORKDIR_ROOT.mkdir(parents=True, exist_ok=True)
    budget_s = cfg.budget_min * 60
    t_run_start = time.monotonic()
    budget_deadline = t_run_start + budget_s
    started = 0

    for video in candidates:
        if time.monotonic() >= budget_deadline:
            log(f"budget ({cfg.budget_min:g} min) reached; "
                f"deferring {len(candidates) - started} remaining file(s) to the next run")
            break
        started += 1
        log(f"start ({started}/{len(candidates)}): {video}")
        try:
            with tempfile.TemporaryDirectory(prefix="sub-fallback_", dir=WORKDIR_ROOT) as workdir:
                rec = process_candidate(video, cfg, jellyfin, Path(workdir), budget_deadline)
        except PlaybackWaitTimeout as exc:
            log(f"{exc}; ending run cleanly, the next hourly run retries")
            return 0
        rec["ts"] = _utc_now_iso()
        _append_jsonl(JSONL_FILE, rec)
        log(f"{rec.get('result')} ({rec.get('source', '-')}, {rec.get('elapsed_s', '?')}s): {video}")

    log(f"done: {started}/{len(candidates)} file(s) started this run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
