#!/usr/bin/env python3
"""Assign profileId=1 (English) to any Bazarr movie/series with profileId=None.

Bazarr's movie/serie_default_enabled only auto-assigns items added AFTER the flag was set; older
ones stay invisible to subtitle search. This is the backstop.
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from media_stack.paths import (  # noqa: E402
    BAZARR_URL, MEDIA_STACK_ROOT, load_env_file,
)
from media_stack.clients.bazarr import (  # noqa: E402
    BazarrClient, apikey_from_container,
)


# process env wins over .env
_ENV = {**load_env_file(MEDIA_STACK_ROOT / ".env"), **os.environ}
BAZARR = _ENV.get("BAZARR_URL") or BAZARR_URL
PROFILE_ID = int(_ENV.get("BAZARR_DEFAULT_PROFILE_ID") or "1")  # 1 = "English"


def main() -> int:
    key = apikey_from_container()
    if not key:
        raise SystemExit("apikey not found in bazarr config.yaml")

    client = BazarrClient(BAZARR, key, timeout=30)

    movies = client.movies()
    series = client.series()
    if movies is None or series is None:
        print("bazarr movies/series endpoint unreachable; skipping audit",
              file=sys.stderr)
        return 1

    fixed_movies: list[tuple[int, str]] = []
    fixed_series: list[tuple[int, str]] = []
    failed_movies: list[tuple[int, str]] = []
    failed_series: list[tuple[int, str]] = []

    for m in movies:
        if m.get("profileId") is None:
            if client.assign_movie_profile(m["radarrId"], PROFILE_ID):
                fixed_movies.append((m["radarrId"], m["title"]))
            else:
                failed_movies.append((m["radarrId"], m["title"]))

    for s in series:
        if s.get("profileId") is None:
            if client.assign_series_profile(s["sonarrSeriesId"], PROFILE_ID):
                fixed_series.append((s["sonarrSeriesId"], s["title"]))
            else:
                failed_series.append((s["sonarrSeriesId"], s["title"]))

    # a failed trigger means the assignment landed but the search won't run until Bazarr's next scan
    task_failures: list[str] = []
    if fixed_movies:
        if not client.trigger_task("wanted_search_missing_subtitles_movies"):
            task_failures.append("wanted_search_missing_subtitles_movies")
    if fixed_series:
        if not client.trigger_task("wanted_search_missing_subtitles_series"):
            task_failures.append("wanted_search_missing_subtitles_series")

    if fixed_movies or fixed_series:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[{ts}] assigned profileId={PROFILE_ID}")
        for rid, title in fixed_movies:
            print(f"  movie {rid}: {title}")
        for sid, title in fixed_series:
            print(f"  series {sid}: {title}")

    # non-zero exit so cron mail / journalctl surface broken Bazarr writes
    if failed_movies or failed_series or task_failures:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[{ts}] FAILURES during profile audit:", file=sys.stderr)
        for rid, title in failed_movies:
            print(f"  movie {rid}: {title} — assign FAILED", file=sys.stderr)
        for sid, title in failed_series:
            print(f"  series {sid}: {title} — assign FAILED", file=sys.stderr)
        for task in task_failures:
            print(f"  trigger {task} FAILED", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
