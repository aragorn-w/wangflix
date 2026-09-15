#!/usr/bin/env python3
"""Daily TV episode de-duplication audit + safe auto-resolve.

Sonarr sibling of ``movie-dedupe.py``.  Detects season folders that
contain more than one video file for the same episode and removes the
duplicate, recoverably.

Root cause (see project memory + media_stack/dedupe.py): when Sonarr
auto-upgrades an episode it imports the new file and deletes the one it
was replacing — but the normalization pipeline has often already
renamed/converted that old file (.mp4->.mkv + retag), so Sonarr's delete
targets a path that no longer exists, leaving the old file orphaned
beside the new keeper -> Jellyfin lists both.

TV-specific wrinkle with NO movie-dedupe precedent: Radarr's movie<->file
relationship is 1:1 (a movie's ``movieFile`` IS overwritten in place), but
Sonarr keeps a separate ``episodefile`` table that is only "usually" 1:1
with a live episode.  In practice an orphan ``episodefile`` DB row can
survive an upgrade even when the correct file IS tracked by the live
episode (observed live 2026-07-26: Rick and Morty S09E01 had episodefile
id 3845 = orphan WEBRip-1080p.mkv with no episode pointing at it, while id
3846 = the WEBDL-1080p.mkv the episode actually tracked).  So this tool
does one thing movie-dedupe.py never needed to: after moving an extra
file to recycle, it also looks up and deletes the matching Sonarr
``episodefile`` row via ``ArrClient.delete_episode_file`` — regardless of
whether the case was SAFE or RISKY, since the orphan-row failure mode
shows up in both.

Safety model (the whole point of running unattended) — identical to
movie-dedupe.py:
  * SAFE  — Sonarr already tracks the chosen keeper for that episode; the
            leftover is untracked.  Move the leftover to the recycle.
            Sonarr never loses its file, so there is no re-download risk.
            Auto-resolved.
  * RISKY — Sonarr tracks a NON-keeper (or tracks nothing) for that
            episode.  Re-pointing it unattended could trip a cutoff-unmet
            re-grab, so by default we only REPORT these for manual
            review.  ``--force`` additionally resolves them (move the
            tracked file, RescanSeries so Sonarr re-imports the keeper,
            verify) — for supervised runs.

Episodes are identified by the ``SxxEyy`` token Sonarr's own naming
convention embeds in every filename (media_stack.dedupe.episode_key).  A
video file with no parseable token can't be safely paired with anything,
so it is excluded from dedup grouping but WARNING-logged by name — it
never disappears from the audit silently.

Removals go to ``$MEDIA_ROOT/.dupe-recycle/tv/`` (same mergerfs pool ->
instant rename; recoverable; pruned by the operator).  ``consolidate-
watch`` excludes ``.dupe-recycle`` so recycled files are not
re-normalized.

Default is a DRY RUN.  ``--apply`` performs moves.  The daily cron runs
``--apply --notify`` (safe mode + a Telegram summary).  Jellyfin reflects
the change on its next scan (real-time monitor / scheduled task).

Exit code: 0 normally; 1 if any RISKY items need manual review (and were
not resolved), so a wrapper/monitor can surface them.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from media_stack import paths
from media_stack.clients.arr import ArrClient
from media_stack.clients.telegram import send as telegram_send
from media_stack.dedupe import choose_keeper, episode_key, group_by_episode, is_video
from media_stack.locking import acquire_file_lock
from media_stack.probe import already_processed, probe


def log(msg: str) -> None:
    # Print to stdout; the cron entry redirects to var/log/tv-dedupe.log
    # (same convention as movie-dedupe.py — the script doesn't own the
    # file, cron does).
    print(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def _sonarr_key() -> str:
    """SONARR_API_KEY with process-env precedence over .env (same
    paths-vs-creds split as _radarr_key in movie-dedupe.py)."""
    if "SONARR_API_KEY" in os.environ:
        return os.environ["SONARR_API_KEY"]
    return paths.load_env_file(paths.MEDIA_STACK_ROOT / ".env").get("SONARR_API_KEY", "")


def _unique_recycle_target(dest: Path, name: str) -> Path:
    """Return a collision-free destination path for a recycle move.  A
    fixed `dest / name` target risks `shutil.move` silently overwriting an
    already-recycled file of the same name (e.g. the same duplicate
    re-appearing on a later run before the operator has cleared the
    recycle bin) — that would destroy the earlier recoverable copy,
    defeating the whole point of recycling instead of deleting (codex
    review finding #2)."""
    target = dest / name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    candidate = dest / f"{stem}.{stamp}{suffix}"
    n = 1
    while candidate.exists():
        candidate = dest / f"{stem}.{stamp}-{n}{suffix}"
        n += 1
    return candidate


def _videos_in(folder: Path) -> list[str]:
    return sorted(f.name for f in folder.iterdir()
                  if f.is_file() and is_video(f.name))


def _video_meta(folder: Path, name: str) -> dict:
    p = folder / name
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    info = probe(p)
    # `probed` is tracked separately from `processed`: probe() is lenient and
    # returns None for an unreadable file, which collapsed into the same
    # processed=False as a perfectly readable file carrying no pipeline tag.
    # Keeper selection could then rank a corrupt file over a healthy one.
    return {"name": name, "size": size,
            "processed": already_processed(info or {}),
            "probed": info is not None}


def _notify(summary: str) -> None:
    """Best-effort Telegram summary using the global plugin .env (token
    never lives in the stack .env — same source as movie-dedupe.py)."""
    env_path = Path(os.environ.get(
        "TELEGRAM_ENV", str(Path.home() / ".claude" / "channels" / "telegram" / ".env")))
    vals = paths.load_env_file(env_path)
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or vals.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or vals.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        log("notify skipped: no Telegram credentials")
        return
    try:
        ok = telegram_send(token, chat, summary)
        log("notify sent" if ok else "notify failed (non-fatal)")
    except Exception as e:
        log(f"notify error (non-fatal): {type(e).__name__}")


# Sonarr's container-side root for the default library.  docker-compose mounts
# host $MEDIA_ROOT/tv at /tv inside the sonarr container, so this is the only
# root whose records correspond to the default --tv-dir.
DEFAULT_SONARR_TV_ROOT = os.environ.get("SONARR_TV_ROOT", "/tv")


def _accepted_sonarr_root(series_list: list[dict], override: str | None,
                          is_default_library: bool = True) -> str | None:
    """The single Sonarr root folder that corresponds to the scanned TV dir.

    Sonarr reports CONTAINER paths ("/tv/Show", rootFolderPath "/tv") while we
    scan host paths, so a basename match alone proves nothing: an unregistered
    local "tv/Show" would happily bind to Sonarr's unrelated
    "/other-library/Show" and we would then DELETE that series' episodefile
    record (Sonarr deletes the backing file with it).  Requiring one known root
    makes the mapping explicit.  Returns None when it cannot be established,
    which the caller treats as "no Sonarr data" (=> RISKY => flagged).
    """
    if override:
        return override.rstrip("/") or "/"
    if not is_default_library:
        # An alternate --tv-dir has no known correspondence to any Sonarr root.
        # Auto-detecting "the only root Sonarr returned" would happily bind an
        # unrelated library, so demand the mapping be stated explicitly.
        return None
    root = DEFAULT_SONARR_TV_ROOT.rstrip("/") or "/"
    roots = {
        (s.get("rootFolderPath") or os.path.dirname(s.get("path") or "")).rstrip("/")
        for s in series_list if s.get("path")
    }
    if root not in roots:
        # Sonarr is not serving the library we are scanning.
        return None
    return root


def _tracked_by_episode(arr: ArrClient, series_id: int) -> tuple[dict, dict, dict, bool]:
    """Join episodes(series_id) with episode_files(series_id) into:
      - tracked_by_ep: {(season, episode): basename of the file the live
        episode currently tracks}
      - file_id_by_relpath: {relativePath: episodefile id} for EVERY
        episodefile record (tracked or orphaned) — used to find the
        Sonarr DB row to delete for a moved extra.  Keyed by the FULL
        relativePath, not just the basename: a bare filename like
        "Episode 01.mkv" is not guaranteed unique across a series' season
        folders, and a basename-only index could resolve a duplicate in
        one season to a same-named file's id in a completely different
        season (codex review finding #2) — deleting the wrong record.
      - episode_by_file_id: {episodefile id: {(season, episode), ...}} for
        every id currently linked to a live episode.  A SET because one
        multi-episode file is owned by every episode it covers.  A second, independent
        safety net on top of the relpath keying: cleanup refuses to
        delete an id that's tracked by an episode OTHER than the one it's
        currently resolving — but still allows deleting an episode's own
        now-stale tracked record (the RISKY+force case: the file just
        moved away WAS this episode's tracked file a moment ago).
      - tracking_ok: False if EITHER endpoint failed.  The three maps above
        are still empty-not-None so callers need no None-checks, but an
        empty map from a failed request is NOT evidence that a file has no
        owner — callers must refuse to mutate the series when this is
        False, or the ownership guard silently passes on missing data."""
    raw_efiles = arr.episode_files(series_id)
    raw_episodes = arr.episodes(series_id)
    # A failed request and an empty series both used to collapse to [].  That is
    # unsafe for the ownership guard: "no owners" then looks identical to "we
    # could not ask", and a transient endpoint failure would let --force move a
    # file another episode still owns.  Report usability explicitly.
    tracking_ok = raw_efiles is not None and raw_episodes is not None
    efiles = raw_efiles or []
    ep_files_by_id = {f["id"]: f for f in efiles
                      if isinstance(f.get("id"), int) and f.get("relativePath")}
    file_id_by_relpath = {
        f["relativePath"]: fid
        for fid, f in ep_files_by_id.items()
    }
    tracked_by_ep: dict[tuple[int, int], str] = {}
    episode_by_file_id: dict[int, set[tuple[int, int]]] = {}
    for e in raw_episodes or []:
        if not e.get("hasFile"):
            continue
        fid = e.get("episodeFileId")
        f = ep_files_by_id.get(fid)
        if not f:
            continue
        key = (e.get("seasonNumber"), e.get("episodeNumber"))
        tracked_by_ep[key] = os.path.basename(f["relativePath"])
        # A multi-episode file is linked by EVERY episode it covers, so this
        # must accumulate; assigning would keep only the last owner and let
        # the guards below think the file is unowned by the others.
        episode_by_file_id.setdefault(fid, set()).add(key)
    return tracked_by_ep, file_id_by_relpath, episode_by_file_id, tracking_ok


def _extras_owned_by_other_episode(
    file_id_by_relpath: dict, episode_by_file_id: dict,
    current_episode: tuple[int, int], season_folder_name: str,
    extras: list[dict],
) -> list[str]:
    """Episodes OTHER than `current_episode` that still own one of `extras`.

    Must run BEFORE any physical move.  A multi-episode file is Sonarr's
    tracked file for every episode it covers, so an "extra" for E01 can be
    E02's only library file.  `_cleanup_orphan_episodefiles` also checks
    ownership, but it runs after the move and can only refuse the DB delete —
    it cannot put the file back, leaving the other episode with a tracked row
    pointing at a vacated path.  Returning a non-empty list means: do not
    touch this group, hand it to a human.
    """
    blockers: set[tuple[int, int]] = set()
    for e in extras:
        efid = file_id_by_relpath.get(f"{season_folder_name}/{e['name']}")
        if efid is None:
            continue
        blockers |= (episode_by_file_id.get(efid) or set()) - {current_episode}
    return [f"S{s:02d}E{ep:02d}" for s, ep in sorted(blockers)]


def _cleanup_orphan_episodefiles(
    arr: ArrClient | None, file_id_by_relpath: dict, episode_by_file_id: dict,
    current_episode: tuple[int, int], moved_relpaths: list[str],
) -> list[str]:
    """After extras have been physically moved to recycle, delete any
    Sonarr episodefile DB row still pointing at their (now-vacated) path.
    Looked up by FULL relativePath (season-folder-relative), never bare
    basename — see `_tracked_by_episode`.  Refuses to delete an id that's
    still tracked by a DIFFERENT episode (`owner != current_episode`) as a
    second independent safety net on top of the relpath keying — but still
    allows cleaning up `current_episode`'s own stale record (the RISKY+
    force case, where the just-moved file WAS this episode's tracked file).
    Best-effort: a missing record or a failed DELETE is logged, never
    raised — this cleanup is a nice-to-have on top of the physical move,
    not a precondition for it.  Returns the relpaths whose DELETE failed so
    the caller can surface them; silently reporting the group as fully
    resolved hid Sonarr rows still pointing at vacated paths."""
    failures: list[str] = []
    if arr is None:
        return failures
    for relpath in moved_relpaths:
        efid = file_id_by_relpath.get(relpath)
        if efid is None:
            continue
        others = (episode_by_file_id.get(efid) or set()) - {current_episode}
        if others:
            who = ", ".join(f"S{s:02d}E{e:02d}" for s, e in sorted(others))
            log(f"  WARNING: skipped Sonarr episodefile id={efid} for {relpath!r} — "
                f"still tracked by {who}, refusing to delete")
            continue
        if arr.delete_episode_file(efid):
            log(f"  cleaned orphan Sonarr episodefile id={efid} for {relpath!r}")
        else:
            log(f"  WARNING: failed to clean Sonarr episodefile id={efid} for {relpath!r}")
            failures.append(relpath)
    return failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Resolve duplicate TV episode files (recoverable).")
    ap.add_argument("--apply", action="store_true", help="perform moves (default: dry run)")
    ap.add_argument("--sonarr-root", default=None,
                    help="Sonarr's root folder path for this library as Sonarr "
                         "reports it (container path, e.g. /tv). Auto-detected "
                         "when every series shares one root.")
    ap.add_argument("--force", action="store_true",
                    help="also resolve RISKY cases (re-point Sonarr via rescan)")
    ap.add_argument("--notify", action="store_true", help="send a Telegram summary")
    ap.add_argument("--tv-dir", default=str(paths.MEDIA_ROOT / "tv"))
    args = ap.parse_args(argv)

    tv_dir = Path(args.tv_dir)
    default_tv_dir = paths.MEDIA_ROOT / "tv"
    # Recycle sits beside the tv dir under its own "tv" namespace (so it's
    # on the same mergerfs pool -> instant rename, and doesn't collide with
    # movie-dedupe.py's recycled folders) and OUTSIDE the tv library
    # Jellyfin scans.  consolidate-watch excludes $MEDIA_ROOT/.dupe-recycle.
    recycle_root = tv_dir.parent / ".dupe-recycle" / "tv"
    mode = "APPLY" if args.apply else "DRY-RUN"

    key = _sonarr_key()
    arr = ArrClient(paths.SONARR_URL, key) if key else None
    series_by_folder: dict[str, dict] = {}
    if arr:
        series_list = arr.series()
        if series_list is None:
            log("WARNING: could not list Sonarr series — classification degraded to RISKY")
        else:
            # Index by folder basename, but a basename is NOT unique: Sonarr
            # can hold /tv/Show and /other/Show.  Silently keeping the last one
            # would point this scan at a DIFFERENT series' episodefile ids and
            # let --apply DELETE a foreign record.  Ambiguous basenames are
            # dropped entirely, so the local folder resolves to no series and
            # degrades to RISKY (flagged) instead of mutating the wrong show.
            root = _accepted_sonarr_root(
                series_list, args.sonarr_root,
                is_default_library=(tv_dir.resolve() == default_tv_dir.resolve()))
            if root is None:
                log("WARNING: could not determine a single Sonarr root folder "
                    "(pass --sonarr-root) — classification degraded to RISKY")
                series_list = []
            ambiguous: set[str] = set()
            for s in series_list:
                path = (s.get("path") or "").rstrip("/")
                base = os.path.basename(path)
                if not base:
                    continue
                # Full-path check, not just the basename: the record must live
                # directly under the root that maps to the dir we are scanning.
                if path != f"{root.rstrip('/')}/{base}":
                    log(f"  ignoring Sonarr series {path!r} — outside accepted "
                        f"root {root!r}")
                    continue
                if base in series_by_folder:
                    ambiguous.add(base)
                series_by_folder[base] = s
            for base in sorted(ambiguous):
                series_by_folder.pop(base, None)
                log(f"WARNING: series folder {base!r} matches multiple Sonarr "
                    f"paths — treating as unknown (manual review)")

    if not tv_dir.is_dir():
        log(f"tv dir not found: {tv_dir}")
        return 1

    resolved: list[str] = []        # SAFE auto-resolved
    forced: list[str] = []          # RISKY resolved under --force
    flagged: list[str] = []         # RISKY left for manual review
    skipped_locked: list[str] = []  # a pipeline holds a lock — retried next pass
    stale: list[str] = []           # moved, but a Sonarr row could not be cleaned
    errors: list[str] = []
    reclaimed = 0
    manifest: list[dict] = []

    for series_folder in sorted(p for p in tv_dir.iterdir() if p.is_dir()):
        series_rec = series_by_folder.get(series_folder.name)
        tracked_by_ep: dict[tuple[int, int], str] = {}
        file_id_by_relpath: dict[str, int] = {}
        episode_by_file_id: dict[int, set[tuple[int, int]]] = {}
        tracking_ok = False
        series_id = series_rec.get("id") if series_rec else None
        if arr and series_id is not None:
            (tracked_by_ep, file_id_by_relpath, episode_by_file_id,
             tracking_ok) = _tracked_by_episode(arr, series_id)
        else:
            log(f"WARNING: could not resolve Sonarr series for {series_folder.name!r} — "
                f"classification degraded to RISKY")

        for season_folder in sorted(p for p in series_folder.iterdir() if p.is_dir()):
            vids = _videos_in(season_folder)
            unparseable = [v for v in vids if episode_key(v) is None]
            if unparseable:
                log(f"WARNING: {series_folder.name}/{season_folder.name}: unparseable "
                    f"filenames (no SxxEyy token), excluded from dedup grouping: {unparseable}")
            metas = [_video_meta(season_folder, v) for v in vids]
            groups = group_by_episode(metas)

            for (season_num, ep_num), group_metas in sorted(groups.items()):
                if len(group_metas) <= 1:
                    continue
                keeper, extras = choose_keeper(group_metas)
                keeper_name = keeper["name"]
                extra_names = [e["name"] for e in extras]
                label = f"{series_folder.name}/{season_folder.name} S{season_num:02d}E{ep_num:02d}"

                tracked = tracked_by_ep.get((season_num, ep_num))
                safe = tracked == keeper_name
                tag = "SAFE" if safe else "RISKY"
                log(f"[{tag}] {label}: keep={keeper_name!r} move={extra_names!r} "
                    f"sonarr_tracks={tracked!r}")

                if not safe and not args.force:
                    flagged.append(label)
                    continue

                # An extra that another episode still tracks is a
                # multi-episode file; moving it strips that episode of its
                # only copy.  Checked BEFORE the move, for every extra.
                blockers = _extras_owned_by_other_episode(
                    file_id_by_relpath, episode_by_file_id,
                    (season_num, ep_num), season_folder.name, extras)
                if blockers:
                    log(f"  {label}: REFUSING — an extra is also Sonarr's tracked "
                        f"file for {', '.join(blockers)} (multi-episode file); "
                        f"needs manual review")
                    flagged.append(f"{label} (extra owned by {', '.join(blockers)})")
                    continue

                # RISKY groups are only resolvable via a Sonarr rescan.  Without
                # a usable client + resolved series there is nothing to reconcile
                # with, and moving anyway would report success while leaving
                # Sonarr pointed at a file that is no longer there.
                # Untrusted tracking data => the ownership guard above is
                # blind, so no move (SAFE or forced) may proceed for this
                # series.  SAFE already cannot trigger without a tracked name,
                # but --force would otherwise sail straight past the guard.
                if arr is not None and series_id is not None and not tracking_ok:
                    log(f"  {label}: REFUSING — Sonarr tracking lookup failed for "
                        f"this series, ownership cannot be verified")
                    errors.append(f"{label} (Sonarr tracking lookup failed)")
                    continue

                if not safe and (arr is None or series_id is None):
                    log(f"  {label}: REFUSING — RISKY but no usable Sonarr client/"
                        f"series, cannot reconcile after the move")
                    errors.append(f"{label} (RISKY, Sonarr reconciliation unavailable)")
                    continue

                if not args.apply:
                    (resolved if safe else forced).append(label)
                    continue

                # --- perform the move(s) under the per-file media locks ---
                try:
                    with ExitStack() as locks:
                        # Lock the KEEPER too: its continued existence is what
                        # makes recycling the extras safe, and locking only the
                        # extras left it free to vanish mid-run (which recycled
                        # the last remaining copy of the episode).  Sorted for a
                        # consistent acquisition order between concurrent runs.
                        lock_targets = sorted(
                            [season_folder / keeper_name]
                            + [season_folder / e["name"] for e in extras])
                        if not all(locks.enter_context(acquire_file_lock(t))
                                   for t in lock_targets):
                            log(f"  {label}: a pipeline holds a lock — skipping this pass")
                            skipped_locked.append(label)
                            continue
                        # Re-validate under the locks: the scan snapshot is old
                        # by now, and Sonarr imports do not take these locks.
                        if not (season_folder / keeper_name).is_file():
                            log(f"  {label}: keeper vanished since the scan — "
                                f"refusing to recycle the remaining copies")
                            errors.append(f"{label} (keeper disappeared mid-run)")
                            continue
                        # Existing is not the same as usable.  An unprobeable
                        # keeper may be truncated or corrupt, and recycling the
                        # readable alternative would leave the episode holding
                        # only a broken file.
                        if probe(season_folder / keeper_name) is None:
                            log(f"  {label}: keeper {keeper_name!r} cannot be probed — "
                                f"refusing to recycle the readable alternatives")
                            errors.append(f"{label} (keeper unprobeable)")
                            continue
                        gone = [e["name"] for e in extras
                                if not (season_folder / e["name"]).is_file()]
                        if gone:
                            log(f"  {label}: extras vanished since the scan ({gone}) — "
                                f"skipping, will re-evaluate next pass")
                            skipped_locked.append(label)
                            continue
                        # The tracking snapshot predates the season probe, and
                        # Sonarr imports do not take these locks.  Re-read it
                        # under the locks so SAFE, the ownership guard and the
                        # DELETE targets all reflect current state.
                        use_ids, use_owners = file_id_by_relpath, episode_by_file_id
                        if arr is not None and series_id is not None:
                            (fresh_tracked, fresh_ids, fresh_owners,
                             fresh_ok) = _tracked_by_episode(arr, series_id)
                            if not fresh_ok:
                                log(f"  {label}: Sonarr tracking re-read failed — "
                                    f"skipping this pass")
                                errors.append(f"{label} (tracking re-read failed)")
                                continue
                            if fresh_tracked.get((season_num, ep_num)) != tracked:
                                log(f"  {label}: Sonarr tracking changed during the "
                                    f"scan ({tracked!r} -> "
                                    f"{fresh_tracked.get((season_num, ep_num))!r}) — "
                                    f"re-evaluating next pass")
                                skipped_locked.append(label)
                                continue
                            fresh_blockers = _extras_owned_by_other_episode(
                                fresh_ids, fresh_owners, (season_num, ep_num),
                                season_folder.name, extras)
                            if fresh_blockers:
                                log(f"  {label}: REFUSING — extra became owned by "
                                    f"{', '.join(fresh_blockers)} during the scan")
                                flagged.append(f"{label} (extra owned by "
                                               f"{', '.join(fresh_blockers)})")
                                continue
                            use_ids, use_owners = fresh_ids, fresh_owners

                        dest = recycle_root / series_folder.name / season_folder.name
                        dest.mkdir(parents=True, exist_ok=True)
                        moved_relpaths = []
                        for e in extras:
                            # Collision-safe target: a fixed name risks
                            # overwriting an already-recycled file from a
                            # prior run (codex review finding #2).
                            target = _unique_recycle_target(dest, e["name"])
                            shutil.move(str(season_folder / e["name"]), str(target))
                            reclaimed += e["size"]
                            moved_relpaths.append(f"{season_folder.name}/{e['name']}")
                            # Record each move as it happens (not after the
                            # whole batch) so a LATER extra's move failing
                            # doesn't erase the audit trail for extras that
                            # already succeeded (codex review finding #3).
                            manifest.append({"label": label, "keeper": keeper_name,
                                             "tracked": tracked, "moved": [e["name"]],
                                             "recycled_as": target.name, "safe": safe})

                        cleanup_failures = _cleanup_orphan_episodefiles(
                            arr, use_ids, use_owners,
                            (season_num, ep_num), moved_relpaths)
                        if cleanup_failures:
                            # One entry per ROW, not per group: the summary
                            # labels this a row count, and a single group can
                            # leave several rows behind.
                            stale.extend(f"{label}: {r}" for r in cleanup_failures)

                        if not safe:
                            # RISKY + --force: Sonarr was tracking a moved file ->
                            # rescan so it re-imports the keeper, then verify.
                            if not arr.rescan_series(series_id):
                                log(f"  WARNING: {label}: RescanSeries command "
                                    f"submission failed — verifying anyway")
                            time.sleep(4)
                            # Re-group just THIS episode's remaining files, not
                            # the whole season folder — a season folder almost
                            # always has other unrelated episodes, so counting
                            # every video in it would spuriously fail verify
                            # even on a correct rescan (codex review finding #3).
                            remaining_metas = [_video_meta(season_folder, v)
                                              for v in _videos_in(season_folder)]
                            remaining_group = group_by_episode(remaining_metas).get(
                                (season_num, ep_num), [])
                            remaining_names = [v["name"] for v in remaining_group]
                            new_tracked_by_ep, _, _, _ = _tracked_by_episode(arr, series_id)
                            cur_tracked = new_tracked_by_ep.get((season_num, ep_num))
                            if len(remaining_names) == 1 and cur_tracked == remaining_names[0]:
                                forced.append(label)
                            else:
                                errors.append(f"{label} (rescan left tracks={cur_tracked!r} "
                                              f"remaining={len(remaining_names)})")
                        else:
                            resolved.append(label)
                    # Locks released.  The per-file lock files are deliberately
                    # LEFT IN PLACE.  Unlinking a released lock breaks the shared
                    # helper's persistent-lock invariant: a replacement import can
                    # recreate the media path and another worker can hold that
                    # lock inode, and removing the pathname then lets a third
                    # worker create and lock a DIFFERENT inode for the same path —
                    # two writers, same media.  An empty stale lock file is
                    # harmless; a missing one is not.
                except Exception as e:
                    errors.append(f"{label}: {type(e).__name__}: {e}")

    if args.apply and manifest:
        try:
            recycle_root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().isoformat(timespec="seconds").replace(":", "")
            (recycle_root / f"manifest-{stamp}.json").write_text(
                json.dumps({"when": stamp, "moves": manifest}, indent=2))
        except OSError as e:
            log(f"manifest write failed (non-fatal): {e}")

    gb = reclaimed / (1024 ** 3)
    summary = (f"tv-dedupe {mode}: resolved={len(resolved)} "
               f"flagged(manual)={len(flagged)} forced={len(forced)} "
               f"locked-skipped={len(skipped_locked)} stale-rows={len(stale)} "
               f"errors={len(errors)} reclaimed={gb:.1f}GB")
    log(summary)
    if flagged:
        log("manual review (Sonarr tracks a non-keeper; re-run with --force "
            f"after checking): {flagged}")
    if stale:
        # Deliberately NOT "re-run to retry": cleanup only runs for groups that
        # still have duplicates, and the extras have already moved, so the next
        # pass sees a singleton and skips it.  These rows need a human.
        log("moved, but these Sonarr rows could not be deleted and will NOT be "
            f"retried automatically — clear them in Sonarr: {stale}")
    if errors:
        log(f"errors: {errors}")

    if args.notify and (resolved or forced or flagged or errors or stale):
        body = summary
        if flagged:
            body += "\n\nmanual review needed: " + ", ".join(flagged)
        if stale:
            body += "\n\nstale Sonarr rows: " + ", ".join(stale)
        if errors:
            body += "\n\nerrors: " + "; ".join(errors)
        _notify(body)

    # Non-zero when something needs a human: flagged RISKY episodes, hard
    # errors (failed move / failed post-rescan verify), or stale Sonarr rows
    # (which no later run will retry — see above).  locked-skipped IS retried
    # next pass, so it alone does NOT fail the run.
    return 1 if flagged or errors or stale else 0


if __name__ == "__main__":
    sys.exit(main())
