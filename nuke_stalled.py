#!/usr/bin/env python3
"""Reap stalled qBittorrent torrents (`stalledDL`/`metaDL` active longer than STALL_THRESHOLD_SEC)
and their Radarr/Sonarr queue entries. Runs every minute via cron.

Removes and blocklists from the originating Arr; deletes directly from qBit only if neither Arr
claims the hash. QBIT_USER empty means unauthenticated (bypass-auth-on-LAN); set with a failed
login exits 1, so an auth problem shows in the cron log instead of stalled torrents silently
piling up.
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
    """Not run at import time, so tests can import this module without live services."""
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
    """0 on success (including no torrents), 2 if qBit can't be listed, so cron sees the missed pass."""
    try:
        torrents = qbit.torrents_info()
    except Exception as e:
        print(f"Error fetching qBit torrents: {e}", file=sys.stderr)
        return 2

    if not torrents:
        print("No torrents in qBittorrent.")
        return 0

    STALL_THRESHOLD_SEC = stall_threshold_sec
    for t in torrents:
        name = t["name"]
        state = t["state"]
        active_time = t["time_active"]
        t_hash = t["hash"]

        if state in ("stalledDL", "metaDL") and active_time > STALL_THRESHOLD_SEC:
            print(f"\nTargeting stalled torrent: {name}")
            print(f"State: {state}, Active Time: {active_time}s")
            # "not_found" is safe to fall through to a direct qBit delete; "matched but
            # DELETE failed" is not (it would skip blocklisting and re-grab the release).
            radarr_r = radarr.remove_by_download_id(t_hash)
            sonarr_r = sonarr.remove_by_download_id(t_hash)
            if "removed" in (radarr_r, sonarr_r):
                pass
            elif "delete_failed" in (radarr_r, sonarr_r):
                # leave it for the next tick; direct delete would skip blocklisting
                print(f"  ERROR: Arr DELETE failed for hash={t_hash} "
                      f"(radarr={radarr_r} sonarr={sonarr_r}); "
                      f"leaving torrent in place for retry.")
            elif "queue_error" in (radarr_r, sonarr_r):
                # can't be sure the torrent isn't tracked there, so don't delete directly
                print(f"  WARN: Arr queue lookup failed for hash={t_hash} "
                      f"(radarr={radarr_r} sonarr={sonarr_r}); "
                      f"leaving torrent in place for retry.")
            else:
                print(f"Not in Arr queues — deleting from qBit directly: {name}")
                try:
                    qbit.delete_torrent(t_hash, delete_files=True)
                except Exception as e:
                    print(f"  ERROR: qBit delete failed for hash={t_hash} name={name!r}: {e}")
        elif state == "downloading":
            print(f"Active download: {name} ({t['progress']*100:.1f}%)")
    return 0


def main() -> int:
    env = _build_env()
    stall_threshold = int(env.get("STALL_THRESHOLD_SEC", "300"))
    qbit, sonarr, radarr = build_clients(env)
    return nuke_all_stalled(qbit, sonarr, radarr, stall_threshold_sec=stall_threshold)


if __name__ == "__main__":
    sys.exit(main())
