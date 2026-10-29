#!/usr/bin/env python3
"""Daily TV duplicate-episode audit + safe auto-resolve (Sonarr sibling of ``movie-dedupe.py``).

SAFE: Sonarr tracks the chosen keeper, so the leftover is recycled automatically. RISKY: Sonarr
tracks a non-keeper (or nothing); only REPORTED, because re-pointing it unattended could trip a
cutoff-unmet re-grab. ``--force`` resolves RISKY too (move, RescanSeries, verify). After
recycling, the matching Sonarr ``episodefile`` row is deleted as well (SAFE or RISKY).

Episodes are keyed by ``SxxEyy`` (media_stack.dedupe.episode_key); files without one are
WARNING-logged and not grouped. Removals go to ``$MEDIA_ROOT/.dupe-recycle/tv/``. Default is a
DRY RUN; the cron runs ``--apply --notify``. Exit 1 if RISKY items need manual review, errors
occurred, or Sonarr rows were left stale.
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
    print(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def _sonarr_key() -> str:
    if "SONARR_API_KEY" in os.environ:
        return os.environ["SONARR_API_KEY"]
    return paths.load_env_file(paths.MEDIA_STACK_ROOT / ".env").get("SONARR_API_KEY", "")


def _unique_recycle_target(dest: Path, name: str) -> Path:
    """Collision-free, so `shutil.move` can't overwrite an earlier recycled copy."""
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
    # probe() is None for an unreadable file; `probed` keeps it from outranking a healthy one.
    return {"name": name, "size": size,
            "processed": already_processed(info or {}),
            "probed": info is not None}


def _notify(summary: str) -> None:
    """Best-effort; the token comes from the plugin .env, not the stack .env."""
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


# Sonarr's container-side path for $MEDIA_ROOT/tv.
DEFAULT_SONARR_TV_ROOT = os.environ.get("SONARR_TV_ROOT", "/tv")


def _accepted_sonarr_root(series_list: list[dict], override: str | None,
                          is_default_library: bool = True) -> str | None:
    """Sonarr reports CONTAINER paths ("/tv/Show") while we scan host paths, so a basename match
    could bind to an unrelated "/other-library/Show" and DELETE its episodefile record. None when
    no mapping can be established; the caller treats that as no Sonarr data (RISKY).
    """
    if override:
        return override.rstrip("/") or "/"
    if not is_default_library:
        # An alternate --tv-dir must state its mapping explicitly.
        return None
    root = DEFAULT_SONARR_TV_ROOT.rstrip("/") or "/"
    roots = {
        (s.get("rootFolderPath") or os.path.dirname(s.get("path") or "")).rstrip("/")
        for s in series_list if s.get("path")
    }
    if root not in roots:
        return None
    return root


def _tracked_by_episode(arr: ArrClient, series_id: int) -> tuple[dict, dict, dict, bool]:
    """Returns `(tracked_by_ep, file_id_by_relpath, episode_by_file_id, tracking_ok)`:
      - tracked_by_ep: {(season, episode): basename of the tracked file}
      - file_id_by_relpath: {relativePath: episodefile id} for EVERY record (tracked or orphaned).
        FULL relativePath: a bare basename isn't unique across season folders.
      - episode_by_file_id: {id: {(season, episode), ...}}; a SET because a multi-episode file
        is owned by every episode it covers.
      - tracking_ok: False if EITHER endpoint failed. The maps are then empty, which is NOT
        evidence a file has no owner: callers must refuse to mutate the series."""
    raw_efiles = arr.episode_files(series_id)
    raw_episodes = arr.episodes(series_id)
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
        episode_by_file_id.setdefault(fid, set()).add(key)
    return tracked_by_ep, file_id_by_relpath, episode_by_file_id, tracking_ok


def _extras_owned_by_other_episode(
    file_id_by_relpath: dict, episode_by_file_id: dict,
    current_episode: tuple[int, int], season_folder_name: str,
    extras: list[dict],
) -> list[str]:
    """Episodes OTHER than `current_episode` that still own one of `extras` (a multi-episode file).
    Must run BEFORE any move: `_cleanup_orphan_episodefiles` checks too, but can't put the file back.
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
    """Delete Sonarr episodefile rows still pointing at the vacated paths. Refuses ids still
    tracked by a DIFFERENT episode. Best-effort: failures are logged; returns the relpaths
    whose DELETE failed."""
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
    # Same pool as tv_dir (instant rename), outside the library Jellyfin scans.
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
            # Folder basename is NOT unique (/tv/Show, /other/Show): ambiguous ones are dropped so
            # the folder degrades to RISKY instead of --apply deleting a foreign series' record.
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

                # A multi-episode file: moving it strips another episode of its only copy.
                blockers = _extras_owned_by_other_episode(
                    file_id_by_relpath, episode_by_file_id,
                    (season_num, ep_num), season_folder.name, extras)
                if blockers:
                    log(f"  {label}: REFUSING — an extra is also Sonarr's tracked "
                        f"file for {', '.join(blockers)} (multi-episode file); "
                        f"needs manual review")
                    flagged.append(f"{label} (extra owned by {', '.join(blockers)})")
                    continue

                # A failed tracking lookup blinds the ownership guard: no move for this series.
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

                try:
                    with ExitStack() as locks:
                        # Lock the KEEPER too, sorted for a consistent order.
                        lock_targets = sorted(
                            [season_folder / keeper_name]
                            + [season_folder / e["name"] for e in extras])
                        if not all(locks.enter_context(acquire_file_lock(t))
                                   for t in lock_targets):
                            log(f"  {label}: a pipeline holds a lock — skipping this pass")
                            skipped_locked.append(label)
                            continue
                        # Re-validate under the locks: Sonarr imports don't take them.
                        if not (season_folder / keeper_name).is_file():
                            log(f"  {label}: keeper vanished since the scan — "
                                f"refusing to recycle the remaining copies")
                            errors.append(f"{label} (keeper disappeared mid-run)")
                            continue
                        # An unprobeable keeper may be corrupt; don't leave only a broken file.
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
                        # Re-read tracking under the locks so SAFE, the guard and DELETE targets are current.
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
                            target = _unique_recycle_target(dest, e["name"])
                            shutil.move(str(season_folder / e["name"]), str(target))
                            reclaimed += e["size"]
                            moved_relpaths.append(f"{season_folder.name}/{e['name']}")
                            # Record each move as it happens, so a later failure keeps the trail.
                            manifest.append({"label": label, "keeper": keeper_name,
                                             "tracked": tracked, "moved": [e["name"]],
                                             "recycled_as": target.name, "safe": safe})

                        cleanup_failures = _cleanup_orphan_episodefiles(
                            arr, use_ids, use_owners,
                            (season_num, ep_num), moved_relpaths)
                        if cleanup_failures:
                            stale.extend(f"{label}: {r}" for r in cleanup_failures)

                        if not safe:
                            # RISKY + --force: rescan so Sonarr re-imports the keeper, then verify.
                            if not arr.rescan_series(series_id):
                                log(f"  WARNING: {label}: RescanSeries command "
                                    f"submission failed — verifying anyway")
                            time.sleep(4)
                            # Re-group just THIS episode, or other episodes fail verify spuriously.
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
                    # Lock files are deliberately LEFT IN PLACE: unlinking one lets a third
                    # worker lock a DIFFERENT inode for the same path.
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
        # Not "re-run to retry": the extras already moved, so the next pass skips it.
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

    # locked-skipped is retried next pass, so it alone does not fail the run.
    return 1 if flagged or errors or stale else 0


if __name__ == "__main__":
    sys.exit(main())
