#!/usr/bin/env python3
"""Remove Sonarr queue items for episodes that have not aired yet.

WHY THIS EXISTS
Malware is distributed as fake releases for HIGHLY ANTICIPATED, UNAIRED
episodes, because no legitimate release exists to compete with them.  Observed
three times on this host for South Park S29E02 (airs 2026-09-30):

    2026-09-17  ...S29E02 1080p WEB H264 MeGusta          (grabbed, failed)
    2026-09-21  ...S29E02 1080p WEB-DL DDP5 1 x265 FLUX   1,053,351,149 B .exe
    2026-09-25  ...S29E02 1080p WEB-DL x265 EDITH           835,449,339 B .exe

`malware-guard.sh` destroys the payload, and that works.  The gap this closes is
different: a destroyed payload leaves Sonarr stuck in `importPending` forever
waiting for a file that no longer exists (39 hours on the first one), and a
jammed queue item can block that episode from ever being grabbed properly.
Blocklisting the individual release does NOT prevent recurrence -- each attempt
used a different release name, size and hash, so the blocklist was one name
behind every time.

The air date is the discriminator that generalises.  If an episode does not air
for days, no legitimate release of it can exist, whatever it calls itself.

SCOPE: Sonarr only, deliberately.  Radarr has no comparable definitive date --
`inCinemas` long precedes any legitimate digital release, so a cinema-window
movie with a real (if pirated) release would be indistinguishable from a fake.
Applying this logic to movies would produce false positives on ordinary grabs.

CONSERVATIVE BY CONSTRUCTION.  Every rule below fails toward "leave it alone",
because a wrong reap deletes a legitimate in-progress download AND blocklists
its release.  False negatives cost a jammed queue item; false positives cost
data.  Each rule exists because a review round found the case:

  * An episode whose `airDateUtc` is missing or unparseable is NOT unaired.
    A TBA/unknown date is an absence of evidence, not evidence.
  * Reaping is decided per DOWNLOAD.  A season pack is several rows sharing one
    `downloadId`, and `removeFromClient=true` takes the whole download, so a
    pack is reaped only when EVERY episode in it is unaired.
  * Rows with no `downloadId` are skipped and never counted.  Those are Sonarr
    PENDING releases (delay profile, or waiting on a better release), emitted
    one row per episode with the id unset, so they cannot be grouped back into
    their release -- deleting one row would blocklist the whole release even
    when its other episodes have aired (round-1 #1, reproduced).  Skipping
    costs nothing: a pending release has not been grabbed, so there is no
    payload and no jammed import, which is the only thing this guard fixes.
  * A row's `seriesId` must equal the series its episode was FETCHED FROM.
    Without that check, a row carrying a bogus `seriesId` rode in on an episode
    id another series had put in the map (round-2 #1).  The fetched series is
    used rather than the episode's self-reported `seriesId`, because an episode
    missing that field stored None and skipped the check (round-3 #1).  Both
    were reproduced by review.
  * Ids must be real ints.  `bool` is an int subclass and `True == 1`, so an
    `episodeId` of True would otherwise resolve to episode 1 (round-1 #3).
  * An invalid grace falls back to the DEFAULT, never to zero.  Clamping a
    negative value to 0 removed the safety margin entirely (round-1 #4).
  * Air dates are fetched only for series that have an ACTIONABLE row, so an
    unrelated pending row whose series lookup fails cannot disable all reaping
    (round-2 #2).

Exit codes:
  0  nothing to do, or every reap succeeded
  1  Sonarr unreachable, queue unreadable, or air dates for an actionable
     download could not be fetched -- the guard is BLIND, not clean
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
# timedelta(hours=...) raises OverflowError past roughly 1e6 years, and a value
# that large is a typo rather than an intent (round-2 #5).  Ten years is far
# beyond any real air schedule and safely inside timedelta's range.
MAX_GRACE_HOURS = 24.0 * 365 * 10


class Episode(NamedTuple):
    """An episode's air time and the series it provably belongs to.

    `series_id` is the series this episode was FETCHED FROM, not the `seriesId`
    the episode dict reports.  The fetched value is authoritative and always
    present; trusting the reported one left a hole where an episode missing that
    field stored None, which then bypassed the ownership check and let a row
    falsely claiming another series be deleted (round-3 #1, reproduced).
    """
    air: datetime | None
    series_id: int


def log(msg: str) -> None:
    """Emit one line to stdout.

    Deliberately stdout ONLY.  The cron entry redirects stdout into
    var/log/unaired-guard.log, so also writing the file here duplicated every
    line and made log counts misleading (round-2 #6).  This matches how the
    other cron jobs in this stack log.
    """
    print(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {msg}", flush=True)


def parse_air(value: object) -> datetime | None:
    """Sonarr's `airDateUtc` as an aware UTC datetime, or None if unusable.

    Returning None is meaningful: callers must treat it as "air date unknown"
    and leave the item alone.  Python 3.10's `fromisoformat` rejects the "Z"
    suffix Sonarr sends, hence the explicit replacement.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_real_int(value: object) -> TypeGuard[int]:
    """True for a genuine int id, excluding bool.

    `bool` is an int subclass and `True == 1`, so an `episodeId` of True would
    otherwise look up episode 1 and authorize deleting its download
    (round-1 #3).  A TypeGuard rather than a plain bool so the narrowing is
    visible to the type checker at the call sites that index the episode map.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def resolve_grace(cli_value: float | None, env: dict[str, str]) -> float:
    """The grace window in hours, with every invalid input failing SAFE.

    Safe here means MORE margin, not less: an out-of-range value is a mistake,
    and the response to a mistake must not be to start reaping things airing in
    minutes.  An explicit 0 from the operator is honoured.
    """
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
    """Map episodeId -> Episode, plus the series ids whose fetch FAILED.

    Fetched per SERIES, not by episode id.  Sonarr's v3 episode endpoint does
    accept an `episodeIds` filter (round-4 #1 corrected an earlier claim here
    that it did not), so this is a deliberate choice rather than a limitation:
    querying a series is what makes ownership authoritative.  The series is
    known from the request, so it cannot be spoofed by a field in the response,
    which is precisely the check `is_unaired` depends on.

    Failures are returned, not swallowed.  Silently skipping them let a Sonarr
    outage look identical to a clean run -- nothing reaped, exit 0 (round-1 #2).
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
                # series_id is `sid`, the series actually queried -- see Episode.
                out[ep_id] = Episode(air=parse_air(ep.get("airDateUtc")),
                                     series_id=sid)
    return out, failed


def is_unaired(rec: dict, episodes: dict[int, Episode],
               cutoff: datetime) -> datetime | None:
    """The episode's air time if this row is PROVABLY unaired, else None.

    None is returned for every uncertainty -- unknown id, unknown air date,
    episode absent from the fetch, or a `seriesId` that disagrees with the
    episode's own.  Callers must treat None as "do not touch".
    """
    ep_id = rec.get("episodeId")
    if not is_real_int(ep_id):
        return None
    ep = episodes.get(ep_id)
    if ep is None or ep.air is None:
        return None
    rec_series = rec.get("seriesId")
    if not is_real_int(rec_series):
        return None
    # The row must belong to the series the episode was fetched from. No None
    # bypass: an episode dict missing its own seriesId used to skip this check
    # entirely, which is exactly how a row claiming another series got deleted
    # (round-3 #1).
    if ep.series_id != rec_series:
        return None
    return ep.air if ep.air > cutoff else None


def status_code_of(exc: BaseException) -> int | None:
    """HTTP status from a requests exception, if it carries one."""
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

    # Group by download FIRST, so pending rows are excluded before anything
    # else looks at them -- including the episode fetch, whose failure on an
    # unrelated pending row used to disable all reaping (round-2 #2).
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

        # ONE delete per download, not one per row.  Sonarr's DELETE acts on the
        # whole download, so the remaining rows were always redundant and their
        # 404s made a successful reap look like a failure -- while a genuine 500
        # on a later row got written off as "already gone" (round-1 #5,
        # round-2 #3).  A 404 on this single call means a concurrent run got
        # there first, which is a no-op rather than an error.
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
        # Only when something else happened: on a quiet run this would be noise
        # every five minutes.
        log(f"skipped {skipped_pending} pending row(s) with no downloadId")
    return 2 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
