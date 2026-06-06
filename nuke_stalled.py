#!/usr/bin/env python3
"""Reap stalled qBittorrent torrents (and their Radarr/Sonarr queue entries).

Runs every minute via cron. Targets torrents in `stalledDL` or `metaDL` state
that have been active longer than STALL_THRESHOLD_SEC. Removes & blocklists
from the originating Arr; falls back to deleting directly from qBit if
neither Arr claims the hash.

Credentials live in `.env` (see `.env.example`).  Service clients come
from `media_stack.clients.{arr,qbit}` — same auth/cookie patterns as
healthcheck.sh + bazarr-profile-audit.py.

Failure model:
  * QBIT_USER unset (empty)        → unauthenticated session, no warning.
    Operator opted into bypass-auth-on-LAN by leaving the var blank.
  * QBIT_USER set, login succeeds  → return session with SID cookie.
  * QBIT_USER set, login FAILS     → sys.exit(1) with a clear message.
    Silently degrading caused stalled torrents to accumulate unreaped
    every cron tick — failing loud surfaces the auth issue in the cron
    log within minutes.
"""

from __future__ import annotations
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from media_stack.paths import (  # noqa: E402
    MEDIA_STACK_ROOT, SONARR_URL, RADARR_URL, QBIT_URL, load_env_file,
)
from media_stack.clients.arr import ArrClient  # noqa: E402
from media_stack.clients.qbit import QBitClient  # noqa: E402


ENV_FILE = MEDIA_STACK_ROOT / ".env"


def _build_env() -> dict[str, str]:
    return {**load_env_file(ENV_FILE), **os.environ}


def _required(env: dict[str, str], key: str) -> str:
    val = env.get(key, "")
    if not val:
        sys.exit(f"missing required env: {key} (set in {ENV_FILE})")
    return val


def build_clients(env: dict[str, str] | None = None) -> tuple[QBitClient, ArrClient, ArrClient]:
    """Construct + log-into the three service clients.  Factored out
    of module-import so unit tests + downstream callers can `import
    nuke_stalled` without touching live services or sys.exit-ing on
    missing creds (codex round-module-split #5)."""
    env = env if env is not None else _build_env()
    sonarr_key = _required(env, "SONARR_API_KEY")
    radarr_key = _required(env, "RADARR_API_KEY")
    qbit_user  = env.get("QBIT_USER", "")
    qbit_pass  = env.get("QBIT_PASS", "")
    qbit = QBitClient(QBIT_URL, qbit_user, qbit_pass)
    if not qbit.login():
        sys.exit(
            f"FATAL: qBit login to {QBIT_URL} failed. "
            f"Check QBIT_USER/QBIT_PASS in .env (or unset QBIT_USER to use "
            f"bypass-auth-on-LAN). Refusing to run unauthenticated when "
            f"credentials are configured — would silently mask real failures."
        )
    return qbit, ArrClient(SONARR_URL, sonarr_key), ArrClient(RADARR_URL, radarr_key)


def nuke_all_stalled(qbit: QBitClient, sonarr: ArrClient, radarr: ArrClient,
                     *, stall_threshold_sec: int = 300) -> int:
    """Return 0 on success (including "no torrents found"), non-zero
    on qBit reachability failure so cron sees a non-success exit
    code instead of silently swallowing the failure (codex round-18
    #6).  Listing failure usually means qBit/Gluetun is down — the
    operator needs to know the reaper missed a pass.
    """
    try:
        torrents = qbit.torrents_info()
    except Exception as e:
        print(f"Error fetching qBit torrents: {e}", file=sys.stderr)
        return 2  # distinguish from "listed OK, nothing to do" (0)

    if not torrents:
        print("No torrents in qBittorrent.")
        return 0

    STALL_THRESHOLD_SEC = stall_threshold_sec  # local alias for legacy clarity
    for t in torrents:
        name = t["name"]
        state = t["state"]
        active_time = t["time_active"]
        t_hash = t["hash"]

        if state in ("stalledDL", "metaDL") and active_time > STALL_THRESHOLD_SEC:
            print(f"\nTargeting stalled torrent: {name}")
            print(f"State: {state}, Active Time: {active_time}s")
            # Try Radarr first, then Sonarr.  Tri-state return lets us
            # distinguish "no match" (safe to fall through to direct
            # qBit delete) from "matched but DELETE failed" (must NOT
            # fall through — direct delete skips Arr blocklisting and
            # causes the same release to re-grab next sweep — codex
            # round-module-split #3).
            radarr_r = radarr.remove_by_download_id(t_hash)
            sonarr_r = sonarr.remove_by_download_id(t_hash)
            if "removed" in (radarr_r, sonarr_r):
                # Handled cleanly on the Arr side; nothing more to do.
                pass
            elif "delete_failed" in (radarr_r, sonarr_r):
                # Matched but the Arr DELETE rejected.  DO NOT fall through
                # to direct qBit delete — that'd skip blocklisting + next
                # sweep would re-grab.  Log loud and leave the torrent
                # alone; next cron tick retries.
                print(f"  ERROR: Arr DELETE failed for hash={t_hash} "
                      f"(radarr={radarr_r} sonarr={sonarr_r}); "
                      f"leaving torrent in place for retry.")
            elif "queue_error" in (radarr_r, sonarr_r):
                # At least one Arr's queue was unreachable.  Can't be
                # confident the torrent isn't tracked there, so don't
                # delete from qBit directly.
                print(f"  WARN: Arr queue lookup failed for hash={t_hash} "
                      f"(radarr={radarr_r} sonarr={sonarr_r}); "
                      f"leaving torrent in place for retry.")
            else:
                # Both Arr queues returned "not_found" cleanly → safe to
                # delete from qBit directly.
                print(f"Not in Arr queues — deleting from qBit directly: {name}")
                try:
                    qbit.delete_torrent(t_hash, delete_files=True)
                except Exception as e:
                    print(f"  ERROR: qBit delete failed for hash={t_hash} name={name!r}: {e}")
        elif state == "downloading":
            print(f"Active download: {name} ({t['progress']*100:.1f}%)")
    return 0


def main() -> int:
    """CLI entrypoint.  Loads env, builds clients (logs into qBit),
    runs one stall-reap pass.  Returns the rc from `nuke_all_stalled`
    so cron sees a non-zero exit on qBit reachability failure."""
    env = _build_env()
    stall_threshold = int(env.get("STALL_THRESHOLD_SEC", "300"))
    qbit, sonarr, radarr = build_clients(env)
    return nuke_all_stalled(qbit, sonarr, radarr, stall_threshold_sec=stall_threshold)


if __name__ == "__main__":
    sys.exit(main())
