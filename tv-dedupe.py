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
from media_stack.locking import acquire_file_lock, lock_path_for
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
    processed = already_processed(probe(p) or {})
    return {"name": name, "size": size, "processed": processed}


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


def _tracked_by_episode(arr: ArrClient, series_id: int) -> tuple[dict, dict, dict]:
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
      - episode_by_file_id: {episodefile id: (season, episode)} for every
        id currently linked to a live episode.  A second, independent
        safety net on top of the relpath keying: cleanup refuses to
        delete an id that's tracked by an episode OTHER than the one it's
        currently resolving — but still allows deleting an episode's own
        now-stale tracked record (the RISKY+force case: the file just
        moved away WAS this episode's tracked file a moment ago).
    All three are empty (not None) on endpoint failure so callers don't
    need a separate None-check; classification just degrades to RISKY
    and orphan-row cleanup is skipped for this series on this pass."""
    efiles = arr.episode_files(series_id) or []
    ep_files_by_id = {f["id"]: f for f in efiles
                      if isinstance(f.get("id"), int) and f.get("relativePath")}
    file_id_by_relpath = {
        f["relativePath"]: fid
        for fid, f in ep_files_by_id.items()
    }
    tracked_by_ep: dict[tuple[int, int], str] = {}
    episode_by_file_id: dict[int, tuple[int, int]] = {}
    for e in arr.episodes(series_id) or []:
        if not e.get("hasFile"):
            continue
        fid = e.get("episodeFileId")
        f = ep_files_by_id.get(fid)
        if not f:
            continue
        key = (e.get("seasonNumber"), e.get("episodeNumber"))
        tracked_by_ep[key] = os.path.basename(f["relativePath"])
        episode_by_file_id[fid] = key
    return tracked_by_ep, file_id_by_relpath, episode_by_file_id


def _cleanup_orphan_episodefiles(
    arr: ArrClient | None, file_id_by_relpath: dict, episode_by_file_id: dict,
    current_episode: tuple[int, int], moved_relpaths: list[str],
) -> None:
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
    not a precondition for it."""
    if arr is None:
        return
    for relpath in moved_relpaths:
        efid = file_id_by_relpath.get(relpath)
        if efid is None:
            continue
        owner = episode_by_file_id.get(efid)
        if owner is not None and owner != current_episode:
            log(f"  WARNING: skipped Sonarr episodefile id={efid} for {relpath!r} — "
                f"still tracked by S{owner[0]:02d}E{owner[1]:02d}, refusing to delete")
            continue
        if arr.delete_episode_file(efid):
            log(f"  cleaned orphan Sonarr episodefile id={efid} for {relpath!r}")
        else:
            log(f"  WARNING: failed to clean Sonarr episodefile id={efid} for {relpath!r}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Resolve duplicate TV episode files (recoverable).")
    ap.add_argument("--apply", action="store_true", help="perform moves (default: dry run)")
    ap.add_argument("--force", action="store_true",
                    help="also resolve RISKY cases (re-point Sonarr via rescan)")
    ap.add_argument("--notify", action="store_true", help="send a Telegram summary")
    ap.add_argument("--tv-dir", default=str(paths.MEDIA_ROOT / "tv"))
    args = ap.parse_args(argv)

    tv_dir = Path(args.tv_dir)
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
            series_by_folder = {os.path.basename(s.get("path", "")): s for s in series_list}

    if not tv_dir.is_dir():
        log(f"tv dir not found: {tv_dir}")
        return 1

    resolved: list[str] = []        # SAFE auto-resolved
    forced: list[str] = []          # RISKY resolved under --force
    flagged: list[str] = []         # RISKY left for manual review
    skipped_locked: list[str] = []  # a pipeline holds a lock — retried next pass
    errors: list[str] = []
    reclaimed = 0
    manifest: list[dict] = []

    for series_folder in sorted(p for p in tv_dir.iterdir() if p.is_dir()):
        series_rec = series_by_folder.get(series_folder.name)
        tracked_by_ep: dict[tuple[int, int], str] = {}
        file_id_by_relpath: dict[str, int] = {}
        episode_by_file_id: dict[int, tuple[int, int]] = {}
        series_id = series_rec.get("id") if series_rec else None
        if arr and series_id is not None:
            tracked_by_ep, file_id_by_relpath, episode_by_file_id = _tracked_by_episode(arr, series_id)
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
                if not args.apply:
                    (resolved if safe else forced).append(label)
                    continue

                # --- perform the move(s) under the per-file media locks ---
                try:
                    with ExitStack() as locks:
                        if not all(locks.enter_context(acquire_file_lock(season_folder / e["name"]))
                                   for e in extras):
                            log(f"  {label}: a pipeline holds a lock — skipping this pass")
                            skipped_locked.append(label)
                            continue
                        dest = recycle_root / series_folder.name / season_folder.name
                        dest.mkdir(parents=True, exist_ok=True)
                        moved = []
                        moved_relpaths = []
                        for e in extras:
                            # Collision-safe target: a fixed name risks
                            # overwriting an already-recycled file from a
                            # prior run (codex review finding #2).
                            target = _unique_recycle_target(dest, e["name"])
                            shutil.move(str(season_folder / e["name"]), str(target))
                            reclaimed += e["size"]
                            moved.append(e["name"])
                            moved_relpaths.append(f"{season_folder.name}/{e['name']}")
                            # Record each move as it happens (not after the
                            # whole batch) so a LATER extra's move failing
                            # doesn't erase the audit trail for extras that
                            # already succeeded (codex review finding #3).
                            manifest.append({"label": label, "keeper": keeper_name,
                                             "tracked": tracked, "moved": [e["name"]],
                                             "recycled_as": target.name, "safe": safe})

                        _cleanup_orphan_episodefiles(
                            arr, file_id_by_relpath, episode_by_file_id,
                            (season_num, ep_num), moved_relpaths)

                        if not safe and arr and series_id is not None:
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
                            new_tracked_by_ep, _, _ = _tracked_by_episode(arr, series_id)
                            cur_tracked = new_tracked_by_ep.get((season_num, ep_num))
                            if len(remaining_names) == 1 and cur_tracked == remaining_names[0]:
                                forced.append(label)
                            else:
                                errors.append(f"{label} (rescan left tracks={cur_tracked!r} "
                                              f"remaining={len(remaining_names)})")
                        else:
                            resolved.append(label)
                    # Locks released.  The moved extras no longer exist at their
                    # old paths, so their per-file lock files are dead — drop
                    # them (same "never unlink" rule as movie-dedupe.py guards
                    # against a fresh file reappearing at the path).
                    for name in moved:
                        try:
                            os.unlink(lock_path_for(season_folder / name))
                        except OSError:
                            pass
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
               f"locked-skipped={len(skipped_locked)} errors={len(errors)} "
               f"reclaimed={gb:.1f}GB")
    log(summary)
    if flagged:
        log("manual review (Sonarr tracks a non-keeper; re-run with --force "
            f"after checking): {flagged}")
    if errors:
        log(f"errors: {errors}")

    if args.notify and (resolved or forced or flagged or errors):
        body = summary
        if flagged:
            body += "\n\nmanual review needed: " + ", ".join(flagged)
        if errors:
            body += "\n\nerrors: " + "; ".join(errors)
        _notify(body)

    # Non-zero when something needs a human: flagged RISKY episodes or hard
    # errors (failed move / failed post-rescan verify).  locked-skipped is
    # transient (retried next pass), so it does NOT fail the run.
    return 1 if flagged or errors else 0


if __name__ == "__main__":
    sys.exit(main())
