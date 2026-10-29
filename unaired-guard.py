#!/usr/bin/env python3
"""Remove Sonarr queue items for episodes that have not aired yet.

Malware is distributed as fake releases of highly anticipated, UNAIRED episodes, since no
legitimate release exists to compete. `malware-guard.sh` destroys the payload, which leaves
Sonarr stuck in `importPending` and can block a proper grab. Blocklisting doesn't help (each
attempt used a different name, size and hash), so the air date is the discriminator. Sonarr
only: Radarr has no comparable definitive date (`inCinemas` long precedes any digital release),
so this would false-positive on ordinary movie grabs.

A wrong reap deletes a legitimate in-progress download and blocklists its release, so every rule
fails toward "leave it alone":

  * A missing or unparseable `airDateUtc` is NOT unaired (TBA is absence of evidence).
  * Reaping is per DOWNLOAD. A season pack is several rows sharing one `downloadId`, and
    `removeFromClient=true` takes the whole download, so a pack is reaped only when EVERY
    episode in it is unaired.
  * Rows with no `downloadId` (Sonarr PENDING releases, one row per episode) are skipped: they
    can't be grouped back into their release, and deleting one would blocklist the whole
    release. They have no payload to jam an import.
  * A row's `seriesId` must equal the series its episode was FETCHED FROM, not the episode's
    self-reported `seriesId` (which may be missing and would bypass the check).
  * Ids must be real ints (`True == 1`).
  * An invalid grace falls back to the DEFAULT, never to zero.
  * Air dates are fetched only for series with an actionable row, so an unrelated pending row
    whose lookup fails can't disable all reaping.

Exit codes:
  0  nothing to do, or every reap succeeded
  1  Sonarr unreachable, queue unreadable, or air dates for an actionable download could not be
     fetched: the guard is BLIND, not clean
  2  at least one reap failed for a reason other than "already gone"
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple, TypeGuard

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from media_stack.clients.arr import ArrClient  # noqa: E402
from media_stack.paths import (  # noqa: E402
    MEDIA_STACK_ROOT, SONARR_URL, load_env_file,
)

DEFAULT_GRACE_HOURS = 24.0
# ten years: far beyond any air schedule, well inside timedelta's range (OverflowError past ~1e6 years)
MAX_GRACE_HOURS = 24.0 * 365 * 10


class Episode(NamedTuple):
    air: datetime | None
    series_id: int


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def parse_air(value: object) -> datetime | None:
    """None means "unknown". The "Z" is replaced because Python 3.10's `fromisoformat` rejects it."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_real_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def resolve_grace(cli_value: float | None, env: dict[str, str]) -> float:
    """Invalid input falls back to the default (more margin, never less); an explicit 0 is honoured."""
    grace = cli_value
    if grace is None:
        try:
            grace = float(env.get("UNAIRED_GRACE_HOURS", DEFAULT_GRACE_HOURS))
        except (TypeError, ValueError):
            return DEFAULT_GRACE_HOURS
    if not math.isfinite(grace) or grace < 0 or grace > MAX_GRACE_HOURS:
        return DEFAULT_GRACE_HOURS
    return grace


def episodes_for(sonarr: ArrClient,
                 series_ids: set[int]) -> tuple[dict[int, Episode], list[int]]:
    """`(episodeId -> Episode, series ids whose fetch FAILED)`. Fetched per series so ownership
    comes from the request, not a response field. Failures are returned, not swallowed, so an
    outage can't look like a clean run.
    """
    out: dict[int, Episode] = {}
    failed: list[int] = []
    for sid in sorted(series_ids):
        try:
            episodes = sonarr.episodes(sid)
        except Exception:
            failed.append(sid)
            continue
        if episodes is None:      # ArrClient returns None on a non-200
            failed.append(sid)
            continue
        for ep in episodes:
            ep_id = ep.get("id")
            if is_real_int(ep_id):
                out[ep_id] = Episode(air=parse_air(ep.get("airDateUtc")),
                                     series_id=sid)
    return out, failed


def is_unaired(rec: dict, episodes: dict[int, Episode],
               cutoff: datetime) -> datetime | None:
    """The air time if PROVABLY unaired, else None for any uncertainty: None means "do not touch"."""
    ep_id = rec.get("episodeId")
    if not is_real_int(ep_id):
        return None
    ep = episodes.get(ep_id)
    if ep is None or ep.air is None:
        return None
    rec_series = rec.get("seriesId")
    if not is_real_int(rec_series):
        return None
    if ep.series_id != rec_series:
        return None
    return ep.air if ep.air > cutoff else None


def status_code_of(exc: BaseException) -> int | None:
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be reaped; change nothing")
    ap.add_argument("--grace-hours", type=float, default=None,
                    help=f"only reap episodes airing more than this far ahead "
                         f"(default {DEFAULT_GRACE_HOURS}, or UNAIRED_GRACE_HOURS)")
    args = ap.parse_args()

    env = {**load_env_file(MEDIA_STACK_ROOT / ".env"), **os.environ}
    api_key = env.get("SONARR_API_KEY", "")
    if not api_key:
        print("ERROR: SONARR_API_KEY is not set", file=sys.stderr)
        return 1
    grace = resolve_grace(args.grace_hours, env)

    sonarr = ArrClient(SONARR_URL, api_key)
    try:
        queue = sonarr.get_queue()
    except Exception as exc:
        log(f"ERROR: reading Sonarr queue: {type(exc).__name__}: {exc}")
        return 1
    if not queue:
        return 0

    # group by download first so pending rows never reach the episode fetch
    groups: dict[str, list[dict]] = defaultdict(list)
    skipped_pending = 0
    for rec in queue:
        dl = rec.get("downloadId")
        if not isinstance(dl, str) or not dl:
            skipped_pending += 1
            continue
        groups[dl].append(rec)
    if not groups:
        return 0

    series_ids = {r["seriesId"] for recs in groups.values() for r in recs
                  if is_real_int(r.get("seriesId"))}
    episodes, fetch_failed = episodes_for(sonarr, series_ids)
    if fetch_failed:
        log(f"ERROR: could not fetch episodes for {len(fetch_failed)} series "
            f"(ids {fetch_failed[:10]}); air dates unknown, reaping nothing")
        return 1

    cutoff = datetime.now(timezone.utc) + timedelta(hours=grace)
    failures = 0
    reaped = 0

    for _dl, recs in sorted(groups.items()):
        airs = [is_unaired(rec, episodes, cutoff) for rec in recs]
        if not all(a is not None for a in airs):
            continue
        soonest = min(a for a in airs if a is not None)

        title = (recs[0].get("title") or "?")[:80]
        ahead = (soonest - datetime.now(timezone.utc)).total_seconds() / 3600
        detail = (f"{len(recs)} episode(s), soonest airs {soonest:%Y-%m-%dT%H:%MZ} "
                  f"({ahead:.1f}h ahead, grace {grace:.0f}h)")

        if args.dry_run:
            log(f"DRY RUN would reap: {detail} :: {title}")
            reaped += 1
            continue

        # one delete per download: DELETE takes the whole download, so extra per-row calls
        # only 404. A 404 here means a concurrent run got there first, not an error.
        try:
            sonarr.delete_from_queue(recs[0]["id"], blocklist=True)
        except requests.HTTPError as exc:
            if status_code_of(exc) == 404:
                log(f"already gone (concurrent run?): {detail} :: {title}")
                continue
            failures += 1
            log(f"ERROR: removing queue id={recs[0].get('id')}: "
                f"{type(exc).__name__}: {exc}")
            continue
        except Exception as exc:
            failures += 1
            log(f"ERROR: removing queue id={recs[0].get('id')}: "
                f"{type(exc).__name__}: {exc}")
            continue
        reaped += 1
        log(f"REAPED unaired: {detail} :: {title}")

    if skipped_pending and (reaped or failures or args.dry_run):
        # only when something else happened, else noise every five minutes
        log(f"skipped {skipped_pending} pending row(s) with no downloadId")
    return 2 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
