#!/usr/bin/env python3
"""Weekly "search for anything missing" across Sonarr and Radarr.

Sonarr and Radarr search for an item once, when it is added, then only watch RSS. A request whose
first search came up empty (indexer down that day) or whose download was reaped therefore sits
"processing" in Jellyseerr indefinitely; this re-searches weekly.

  Sonarr: queues MissingEpisodeSearch with monitored=true, the same command as "Search All
    Missing". Its missing list holds only AIRED episodes (unaired-guard.py reaps a grab for an
    unaired one that slips through).
  Radarr: deliberately NOT MissingMoviesSearch. It picks the movies itself (monitored, no file,
    RELEASED, not already queued) and queues MoviesSearch for those. "Released" is stricter than
    Radarr's: status "released" AND isAvailable AND a digital or physical release date that has
    passed. isAvailable alone follows minimumAvailability (an "In Cinemas" movie is available at
    the theatrical date), and both flags fall back to "90 days after cinemas" when no home
    release date is known; for films still in cinemas the quality profile approves pre-release
    junk.

Fire and forget: both apps run the searches themselves, so this reports only what it queued.
Weekly via ops/cron.d/media-stack.crontab.

Exit codes:
  0  every search that was due got queued (including "nothing to search")
  1  a key was missing, an app unreachable, or a list unreadable, so something that should have
     been searched may not have been
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeGuard

sys.path.insert(0, str(Path(__file__).resolve().parent))

from media_stack.clients.arr import ArrClient  # noqa: E402
from media_stack.paths import (  # noqa: E402
    MEDIA_STACK_ROOT, RADARR_URL, SONARR_URL, load_env_file,
)


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def is_real_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _passed(value: object, now: datetime) -> bool:
    """Radarr sends a literal Z suffix, which Python 3.10's fromisoformat rejects."""
    if not isinstance(value, str) or not value:
        return False
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= now


def is_released(movie: dict, now: datetime | None = None) -> bool:
    """Out on home release, whatever minimumAvailability (all three conditions, see the module
    docstring). Anything missing, malformed or in the future counts as NOT released: fail toward
    searching nothing."""
    now = now or datetime.now(timezone.utc)
    return (movie.get("status") == "released"
            and movie.get("isAvailable") is True
            and (_passed(movie.get("digitalRelease"), now)
                 or _passed(movie.get("physicalRelease"), now)))


def pick_movies(movies: list[dict], queue: list[dict]) -> list[int]:
    """Each flag must be literally True/False; anything else excludes the movie."""
    downloading = {r["movieId"] for r in queue if is_real_int(r.get("movieId"))}
    return sorted(
        m["id"] for m in movies
        if is_real_int(m.get("id"))
        and m.get("monitored") is True
        and m.get("hasFile") is False
        and is_released(m)
        and m["id"] not in downloading
    )


def search_tv(env: dict[str, str], dry_run: bool) -> bool:
    key = env.get("SONARR_API_KEY", "")
    if not key:
        log("ERROR: SONARR_API_KEY is not set; TV not searched")
        return False
    if dry_run:
        log("DRY RUN would queue Sonarr MissingEpisodeSearch (monitored)")
        return True
    cid = ArrClient(SONARR_URL, key).run_command("MissingEpisodeSearch", monitored=True)
    if cid is None:
        log("ERROR: Sonarr did not accept MissingEpisodeSearch (unreachable or refused)")
        return False
    log(f"Sonarr: queued MissingEpisodeSearch (monitored), command {cid}")
    return True


def search_movies(env: dict[str, str], dry_run: bool) -> bool:
    key = env.get("RADARR_API_KEY", "")
    if not key:
        log("ERROR: RADARR_API_KEY is not set; movies not searched")
        return False
    radarr = ArrClient(RADARR_URL, key)
    movies = radarr.movies()
    if movies is None:
        log("ERROR: Radarr movie list unreadable; movies not searched")
        return False
    try:
        queue = radarr.get_queue()
    except Exception as exc:
        # without the queue a downloading movie would be searched again
        log(f"ERROR: Radarr queue unreadable ({type(exc).__name__}); movies not searched")
        return False

    ids = pick_movies(movies, queue)
    unreleased = sum(1 for m in movies if m.get("monitored") is True
                     and m.get("hasFile") is False and not is_released(m))
    # a null title must not crash the log line
    titles = {m["id"]: str(m.get("title") or "?") for m in movies if is_real_int(m.get("id"))}
    names = ", ".join(titles[i] for i in ids[:15]) + (" ..." if len(ids) > 15 else "")
    if not ids:
        log(f"Radarr: nothing released is missing ({unreleased} not yet released, skipped)")
        return True
    if dry_run:
        log(f"DRY RUN would search {len(ids)} movie(s): {names}")
        return True
    cid = radarr.run_command("MoviesSearch", movieIds=ids)
    if cid is None:
        log(f"ERROR: Radarr did not accept MoviesSearch for {len(ids)} movie(s)")
        return False
    log(f"Radarr: queued MoviesSearch for {len(ids)} movie(s), command {cid} "
        f"({unreleased} not yet released, skipped): {names}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be searched; queue nothing")
    args = ap.parse_args()

    env = {**load_env_file(MEDIA_STACK_ROOT / ".env"), **os.environ}
    # Both run regardless: a Sonarr outage must not also skip the movie search.
    tv_ok = search_tv(env, args.dry_run)
    movies_ok = search_movies(env, args.dry_run)
    return 0 if tv_ok and movies_ok else 1


if __name__ == "__main__":
    sys.exit(main())
