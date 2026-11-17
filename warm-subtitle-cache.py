#!/usr/bin/env python3
"""Warm Jellyfin's embedded-subtitle cache overnight so a playing client never waits on it.

Jellyfin extracts an embedded text subtitle the first time a TRANSCODING client asks for it as
an external stream. That read covers the whole file (minutes for a large 4K movie on a USB
disk) and the client gives up. Once extracted, Jellyfin caches the subtitle in its own data dir
(valid while the file's size and mtime are unchanged) and later requests take milliseconds.
This script requests every embedded text subtitle track of every Movie and Episode through
Jellyfin's own endpoint (`/Videos/{item}/{source}/Subtitles/{index}/0/Stream.srt`), strictly one
at a time, so Jellyfin does the extraction and owns the cache. Movies first, newest first.

PLAYBACK: before EVERY fetch the pause-aware gate `media_stack.playback.is_playing_or_unknown`
must say idle (it fails closed: an unreachable Jellyfin counts as "someone is watching"); while
it does not, this sleeps 60 s and re-checks until it clears or the deadline passes.
ACCEPTED RESIDUAL: Jellyfin runs the extraction with no cancellation, so one already in flight
when playback starts runs to completion. This script deliberately never kills Jellyfin's ffmpeg.

BOUNDS: `--max-hours` (default 6) ends the run: no new fetch starts after it and the remaining
count is logged. The cache is persistent, so the next night resumes where this one stopped. Ten
CONSECUTIVE failed fetches (non-200 or a transport error: a USB disk dropout, a broken
Jellyfin) abort the run with exit 1. A singleton lock makes a second instance exit 0.

Needs `JELLYFIN_API_KEY` (and optionally `JELLYFIN_URL`, else `http://<MEDIA_LAN_IP>:8096`)
from the environment or `.env`. `--dry-run` lists targets and fetches nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from media_stack.clients.jellyfin import JellyfinClient, NetworkError  # noqa: E402
from media_stack.locking import (  # noqa: E402
    acquire_singleton_lock, handle_sigterm as _handle_sigterm,
)
from media_stack.paths import (  # noqa: E402
    MEDIA_LAN_IP, MEDIA_STACK_ROOT, VAR_RUN, ensure_var_dirs, load_env_file,
)
from media_stack.playback import PLAYBACK_POLL_S, is_playing_or_unknown  # noqa: E402

LOCK_FILE = VAR_RUN / "warm-subtitle-cache.lock"

ITEMS_PATH = ("/Items?Recursive=true&IncludeItemTypes=Movie,Episode&HasSubtitles=true"
              "&Fields=MediaSources,DateCreated&EnableImages=false&EnableUserData=false")
# The big /Items response needs more than the 10 s default; /Sessions shares this client.
LIST_TIMEOUT_S = 120
# Above Jellyfin's 30 minute SubtitleExtractionTimeoutMinutes, so Jellyfin gives up first.
FETCH_TIMEOUT_S = 35 * 60
MAX_CONSECUTIVE_FAILURES = 10
EXTRACTED_MIN_S = 2.0   # slower than this was a real extraction, not a cache hit (log only)


class Target(NamedTuple):
    item_id: str
    source_id: str
    stream: int     # MediaStream Index
    kind: str       # "Movie" or "Episode"
    created: str    # ISO 8601 DateCreated (UTC, sortable as text)
    name: str


@dataclass
class Stats:
    total: int
    cached: int = 0
    extracted: int = 0
    failed: int = 0
    aborted: bool = False

    @property
    def done(self) -> int:
        return self.cached + self.extracted + self.failed

    @property
    def left(self) -> int:
        return self.total - self.done


def log(msg: str) -> None:
    """Timestamped stdout line (cron redirects to var/log/warm-subtitle-cache.log)."""
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def select_targets(items: list[dict]) -> list[Target]:
    """Every embedded text subtitle track, Movies before Episodes, newest first within each."""
    out = []
    for item in items:
        name = item.get("Name") or item.get("Id", "?")
        if item.get("SeriesName"):
            name = f"{item['SeriesName']}: {name}"
        for src in item.get("MediaSources") or []:
            for s in src.get("MediaStreams") or []:
                if (s.get("Type") == "Subtitle" and not s.get("IsExternal")
                        and s.get("IsTextSubtitleStream")):
                    out.append(Target(item["Id"], src["Id"], s["Index"], item.get("Type", ""),
                                      item.get("DateCreated") or "", name))
    out.sort(key=lambda t: t.created, reverse=True)
    out.sort(key=lambda t: t.kind != "Movie")  # stable: keeps newest-first inside each group
    return out


def wait_for_idle(jf: JellyfinClient, key: str, deadline: float,
                  clock: Callable[[], float], sleep: Callable[[float], None]) -> bool:
    """Block while playing or unknown. False if the deadline passed first."""
    announced = False
    while clock() < deadline:
        if not is_playing_or_unknown(jf, key):
            return clock() < deadline  # the /Sessions call itself can outlast the deadline
        if not announced:
            log("waiting for playback (or an unknown Jellyfin status) to clear")
            announced = True
        sleep(PLAYBACK_POLL_S)
    return False


def run(targets: list[Target], jf: JellyfinClient, fetch_jf: JellyfinClient, key: str, *,
        deadline: float, limit: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep) -> Stats:
    """Fetch each target in order, one at a time, behind the playback gate."""
    stats = Stats(total=len(targets))
    consecutive = 0
    for t in targets:
        if limit is not None and stats.done >= limit:
            break
        if not wait_for_idle(jf, key, deadline, clock, sleep):
            log(f"deadline reached; {stats.left} target(s) left for the next run")
            break
        label = f"{t.name} (stream {t.stream})"
        started = clock()
        try:
            status, _ = fetch_jf.request(
                f"/Videos/{t.item_id}/{t.source_id}/Subtitles/{t.stream}/0/Stream.srt", key)
            problem = None if status == 200 else f"HTTP {status}"
        except NetworkError as exc:
            problem = str(exc)  # method, URL and exception only; never the key or headers
        secs = clock() - started
        if problem:
            stats.failed += 1
            consecutive += 1
            log(f"FAILED {label} after {secs:.1f}s: {problem}")
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                log(f"ERROR: {consecutive} consecutive failures; aborting the run "
                    f"({stats.left} target(s) left)")
                stats.aborted = True
                break
            continue
        consecutive = 0
        if secs >= EXTRACTED_MIN_S:
            stats.extracted += 1
            log(f"extracted {label} in {secs:.1f}s")
        else:
            stats.cached += 1
    return stats


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_sigterm)

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the target count and the first 10 targets; fetch nothing")
    ap.add_argument("--max-hours", type=float, default=6.0,
                    help="start no new fetch after this many hours (default 6)")
    ap.add_argument("--limit", type=int, metavar="N",
                    help="stop after N fetches (smoke tests)")
    args = ap.parse_args()

    lock_fp = None
    if not args.dry_run:
        ensure_var_dirs()
        lock_fp = acquire_singleton_lock(LOCK_FILE)
        if lock_fp is None:
            log("another run holds the lock; exiting")
            return 0

    env = {**load_env_file(MEDIA_STACK_ROOT / ".env"), **os.environ}
    key = env.get("JELLYFIN_API_KEY", "")
    if not key:
        log("ERROR: JELLYFIN_API_KEY is not set")
        return 1
    base = env.get("JELLYFIN_URL") or f"http://{MEDIA_LAN_IP}:8096"
    jf = JellyfinClient(base, timeout=LIST_TIMEOUT_S)

    try:
        status, body = jf.request(ITEMS_PATH, key)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        targets = select_targets(json.loads(body)["Items"])
    except (NetworkError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        log(f"ERROR: could not list items: {type(exc).__name__}: {exc}")
        return 1

    if args.dry_run:
        log(f"DRY RUN: {len(targets)} target(s); nothing fetched")
        for t in targets[:10]:
            log(f"  {t.kind} {t.name} (stream {t.stream})")
        return 0

    started = time.monotonic()
    log(f"start: {len(targets)} target(s), up to {args.max_hours:g} h")
    stats = run(targets, jf, JellyfinClient(base, timeout=FETCH_TIMEOUT_S), key,
                deadline=started + args.max_hours * 3600, limit=args.limit)
    log(f"done: total={stats.total} cached={stats.cached} extracted={stats.extracted} "
        f"failed={stats.failed} left={stats.left} "
        f"elapsed={(time.monotonic() - started) / 60:.1f} min")
    return 1 if stats.aborted else 0


if __name__ == "__main__":
    sys.exit(main())
