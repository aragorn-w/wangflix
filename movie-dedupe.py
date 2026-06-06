#!/usr/bin/env python3
"""Daily movie de-duplication audit + safe auto-resolve.

Detects movie folders that contain more than one video file and removes the
duplicate, recoverably.

Root cause (see project memory + media_stack/dedupe.py): when Radarr
auto-upgrades a movie it imports the new file and deletes the one it was
replacing — but the normalization pipeline has often already
renamed/converted that old file (.mp4→.mkv + retag), so Radarr's delete
targets a path that no longer exists, leaving the old file orphaned beside
the new keeper → Jellyfin lists both.  This cron makes that self-heal.

Safety model (the whole point of running unattended):
  * SAFE  — Radarr already tracks the chosen keeper; the leftover is
            untracked.  Move the leftover to the recycle.  Radarr never
            loses its file, so there is no re-download risk.  Auto-resolved.
  * RISKY — Radarr tracks a NON-keeper (or tracks nothing).  Re-pointing it
            unattended could trip a cutoff-unmet re-grab, so by default we
            only REPORT these for manual review.  `--force` additionally
            resolves them (move the tracked file, RescanMovie so Radarr
            re-imports the keeper, verify) — for supervised runs.

Removals go to ``$MEDIA_ROOT/.dupe-recycle/`` (same mergerfs pool → instant
rename; recoverable; pruned by the operator).  ``consolidate-watch``
excludes that directory so recycled files are not re-normalized.

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
from media_stack.dedupe import choose_keeper, is_video
from media_stack.locking import acquire_file_lock, lock_path_for
from media_stack.probe import already_processed, probe

def log(msg: str) -> None:
    # Print to stdout; the cron entry redirects to var/log/movie-dedupe.log
    # (same convention as nuke_stalled.py / bazarr-profile-audit.py — the
    # script doesn't own the file, cron does).
    print(f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def _radarr_key() -> str:
    """RADARR_API_KEY with process-env precedence over .env (the key isn't
    carried by media_stack.paths — same paths-vs-creds split as healthcheck)."""
    if "RADARR_API_KEY" in os.environ:
        return os.environ["RADARR_API_KEY"]
    return paths.load_env_file(paths.MEDIA_STACK_ROOT / ".env").get("RADARR_API_KEY", "")


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
    never lives in the stack .env — same source as healthcheck-alert.sh)."""
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Resolve duplicate movie files (recoverable).")
    ap.add_argument("--apply", action="store_true", help="perform moves (default: dry run)")
    ap.add_argument("--force", action="store_true",
                    help="also resolve RISKY cases (re-point Radarr via rescan)")
    ap.add_argument("--notify", action="store_true", help="send a Telegram summary")
    ap.add_argument("--movies-dir", default=str(paths.MEDIA_ROOT / "movies"))
    args = ap.parse_args(argv)

    movies_dir = Path(args.movies_dir)
    # Recycle sits beside the movies dir (so it's on the same mergerfs pool →
    # instant rename) and OUTSIDE the movies library Jellyfin scans.
    # consolidate-watch excludes $MEDIA_ROOT/.dupe-recycle from its inotify.
    recycle = movies_dir.parent / ".dupe-recycle"
    mode = "APPLY" if args.apply else "DRY-RUN"

    key = _radarr_key()
    arr = ArrClient(paths.RADARR_URL, key) if key else None
    by_folder: dict[str, dict] = {}
    if arr:
        movies = arr.movies()
        if movies is None:
            log("WARNING: could not list Radarr movies — classification degraded to RISKY")
        else:
            by_folder = {os.path.basename(m.get("path", "")): m for m in movies}

    if not movies_dir.is_dir():
        log(f"movies dir not found: {movies_dir}")
        return 1

    resolved: list[str] = []        # SAFE auto-resolved
    forced: list[str] = []          # RISKY resolved under --force
    flagged: list[str] = []         # RISKY left for manual review
    skipped_locked: list[str] = []  # a pipeline holds a lock — retried next pass
    errors: list[str] = []
    reclaimed = 0
    manifest: list[dict] = []

    for folder in sorted(p for p in movies_dir.iterdir() if p.is_dir()):
        vids = _videos_in(folder)
        if len(vids) <= 1:
            continue
        metas = [_video_meta(folder, v) for v in vids]
        keeper, extras = choose_keeper(metas)
        keeper_name = keeper["name"]
        extra_names = [e["name"] for e in extras]

        movie = by_folder.get(folder.name)
        tracked = None
        if movie and (movie.get("movieFile") or {}).get("relativePath"):
            tracked = os.path.basename(movie["movieFile"]["relativePath"])

        safe = tracked == keeper_name
        tag = "SAFE" if safe else "RISKY"
        log(f"[{tag}] {folder.name}: keep={keeper_name!r} "
            f"move={extra_names!r} radarr_tracks={tracked!r}")

        if not safe and not args.force:
            flagged.append(folder.name)
            continue
        if not args.apply:
            (resolved if safe else forced).append(folder.name)
            continue

        # --- perform the move(s) under the per-file media locks ---
        # consolidate-subs.py / normalize-audio.py take the same locks; moving
        # a file mid-pipeline would corrupt state / orphan partial output.  If
        # any extra is already locked, skip the whole folder — the next cron
        # pass (or a manual run) handles it once the pipeline finishes.
        try:
            with ExitStack() as locks:
                if not all(locks.enter_context(acquire_file_lock(folder / e["name"]))
                           for e in extras):
                    log(f"  {folder.name}: a pipeline holds a lock — skipping this pass")
                    skipped_locked.append(folder.name)
                    continue
                dest = recycle / folder.name
                dest.mkdir(parents=True, exist_ok=True)
                moved = []
                for e in extras:
                    shutil.move(str(folder / e["name"]), str(dest / e["name"]))
                    reclaimed += e["size"]
                    moved.append(e["name"])
                manifest.append({"folder": folder.name, "keeper": keeper_name,
                                 "tracked": tracked, "moved": moved, "safe": safe})

                if not safe and arr and movie:
                    # RISKY + --force: Radarr was tracking a moved file → rescan
                    # so it re-imports the keeper, then verify it took.
                    arr.rescan_movie(movie["id"])
                    time.sleep(4)
                    remaining = _videos_in(folder)
                    mv = arr.movies() or []
                    cur = next((m for m in mv if m.get("id") == movie["id"]), {})
                    cur_tracked = os.path.basename((cur.get("movieFile") or {}).get("relativePath", "")) or None
                    if len(remaining) == 1 and cur.get("hasFile") and cur_tracked == remaining[0]:
                        forced.append(folder.name)
                    else:
                        errors.append(f"{folder.name} (rescan left tracks={cur_tracked} "
                                      f"remaining={len(remaining)})")
                else:
                    resolved.append(folder.name)
            # Locks released.  The moved extras no longer exist at their old
            # paths, so their per-file lock files are dead — drop them (the
            # "never unlink" rule guards against a fresh file reappearing at
            # the path, which can't happen once the media is gone).
            for name in moved:
                try:
                    os.unlink(lock_path_for(folder / name))
                except OSError:
                    pass
        except Exception as e:
            errors.append(f"{folder.name}: {type(e).__name__}: {e}")

    if args.apply and manifest:
        try:
            recycle.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().isoformat(timespec="seconds").replace(":", "")
            (recycle / f"manifest-{stamp}.json").write_text(
                json.dumps({"when": stamp, "moves": manifest}, indent=2))
        except OSError as e:
            log(f"manifest write failed (non-fatal): {e}")

    gb = reclaimed / (1024 ** 3)
    summary = (f"movie-dedupe {mode}: resolved={len(resolved)} "
               f"flagged(manual)={len(flagged)} forced={len(forced)} "
               f"locked-skipped={len(skipped_locked)} errors={len(errors)} "
               f"reclaimed={gb:.1f}GB")
    log(summary)
    if flagged:
        log("manual review (Radarr tracks a non-keeper; re-run with --force "
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

    # Non-zero when something needs a human: flagged RISKY folders or hard
    # errors (failed move / failed post-rescan verify).  locked-skipped is
    # transient (retried next pass), so it does NOT fail the run.
    return 1 if flagged or errors else 0


if __name__ == "__main__":
    sys.exit(main())
