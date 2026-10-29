"""Host-identity values (roots, LAN IP, service URLs, var/ dirs). Mirrors `lib/paths.sh`; both
load the same `.env`, which is never executed: Compose syntax can contain `$(...)`, so it only
splits on `=` and strips one layer of quotes.
"""

from __future__ import annotations

import os
from pathlib import Path


_HERE = Path(__file__).resolve().parent.parent


def load_env_file(env_path: Path) -> dict[str, str]:
    """Same semantics as `lib/paths.sh`: split on first `=`, strip quotes, skip blanks and comments.
    A missing file gives `{}`."""
    out: dict[str, str] = {}
    if not env_path.is_file():
        return out
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


# process env wins over .env
_ENV = {**load_env_file(_HERE / ".env"), **os.environ}


def _get(key: str, default: str) -> str:
    val = _ENV.get(key, "")
    return val if val else default


MEDIA_STACK_ROOT: Path = Path(_get("MEDIA_STACK_ROOT", str(_HERE)))
MEDIA_ROOT: Path = Path(_get("MEDIA_ROOT", str(MEDIA_STACK_ROOT / "data" / "media")))
MEDIA_LAN_IP: str = _get("MEDIA_LAN_IP", "10.0.0.10")
CALIBRE_LIBRARY: Path = Path(_get("CALIBRE_LIBRARY", "/srv/calibre/library"))

PUID: int = int(_get("PUID", "1000"))
PGID: int = int(_get("PGID", "1000"))
TZ: str = _get("TZ", "UTC")

SONARR_URL: str = _get("SONARR_URL", f"http://{MEDIA_LAN_IP}:8989")
RADARR_URL: str = _get("RADARR_URL", f"http://{MEDIA_LAN_IP}:7878")
QBIT_URL: str = _get("QBIT_URL", f"http://{MEDIA_LAN_IP}:8090")
BAZARR_URL: str = _get("BAZARR_URL", f"http://{MEDIA_LAN_IP}:6767")

# Ephemeral runtime tree. Per-file media locks stay beside the media file (flock needs the same
# filesystem).
VAR_DIR: Path = Path(_get("VAR_DIR", str(MEDIA_STACK_ROOT / "var")))
VAR_LOG: Path = Path(_get("VAR_LOG", str(VAR_DIR / "log")))
VAR_RUN: Path = Path(_get("VAR_RUN", str(VAR_DIR / "run")))
VAR_STATE: Path = Path(_get("VAR_STATE", str(VAR_DIR / "state")))
VAR_REVIEWS: Path = Path(_get("VAR_REVIEWS", str(VAR_DIR / "reviews")))
# Content-gate rejects from consolidate-subs.py: moved here, never deleted.
VAR_QUARANTINE_SUBTITLES: Path = Path(_get(
    "VAR_QUARANTINE_SUBTITLES", str(VAR_DIR / "quarantine" / "subtitles")))


__all__ = [
    "MEDIA_STACK_ROOT", "MEDIA_ROOT", "MEDIA_LAN_IP", "CALIBRE_LIBRARY",
    "PUID", "PGID", "TZ",
    "SONARR_URL", "RADARR_URL", "QBIT_URL", "BAZARR_URL",
    "VAR_DIR", "VAR_LOG", "VAR_RUN", "VAR_STATE", "VAR_REVIEWS",
    "VAR_QUARANTINE_SUBTITLES",
    "ensure_var_dirs", "load_env_file",
]


def ensure_var_dirs() -> None:
    """Writers call this before their first write; importing never writes. Skips
    `VAR_QUARANTINE_SUBTITLES` (an optional dir must not stop the pipeline; consolidate-subs.py
    creates it lazily). Exits non-zero on failure so a cron run can't start with no durable log dir.
    """
    import sys
    failures: list[tuple[Path, Exception]] = []
    for d in (VAR_LOG, VAR_RUN, VAR_STATE, VAR_REVIEWS):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            failures.append((d, e))
    if failures:
        for d, exc in failures:
            print(f"media_stack.paths.ensure_var_dirs: cannot create {d}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(
            f"media_stack.paths.ensure_var_dirs: {len(failures)} "
            f"of 4 var/ dirs failed to create"
        )


def dump_values() -> None:
    for k in __all__:
        if k in ("ensure_var_dirs", "load_env_file", "dump_values"):
            continue
        print(f"{k}={globals()[k]}")


__all__.append("dump_values")


if __name__ == "__main__":
    dump_values()
