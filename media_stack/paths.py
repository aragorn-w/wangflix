"""Host-identity values for the media stack.

Imported by every host-side Python script so the same `$HOME/media-stack`
or `10.0.0.10` value can't drift between scripts.  Mirrors `lib/paths.sh`
for shell scripts — both load from the same `.env`.

Usage::

    from media_stack.paths import MEDIA_STACK_ROOT, MEDIA_ROOT, RADARR_URL
    # ... use as Path / str ...

`media_paths.py` at the repo root is a backward-compat re-export shim.

This is a NON-EXECUTING parser: Docker Compose `.env` syntax can contain
`$(...)` substitutions that `dotenv`-style executors would run.  We
split on `=`, strip whitespace + a single layer of quotes, never invoke
a shell.  Identical pattern to `healthcheck.sh` + `lib/paths.sh`.
"""

from __future__ import annotations

import os
from pathlib import Path


# Resolve MEDIA_STACK_ROOT from this module's location.  This file sits
# at $REPO/media_stack/paths.py so the repo root is two parents up.
_HERE = Path(__file__).resolve().parent.parent


def load_env_file(env_path: Path) -> dict[str, str]:
    """Parse a Docker-compose-style `.env` into a dict.

    Single source of truth for `.env` parsing in Python — keeps the
    same non-executing semantics as `lib/paths.sh` (split on first `=`,
    strip surrounding quotes, skip blanks + comment lines).  Returns
    an empty dict if the file doesn't exist.

    Other host-side scripts (`nuke_stalled.py`,
    `bazarr-profile-audit.py`) import this helper instead of re-
    implementing the same parser (codex round-4 module-split #5).
    """
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


# Process env wins over .env so one-off shell overrides work without
# editing the file.
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

# Service URLs — derived from LAN IP unless explicitly overridden.
SONARR_URL: str = _get("SONARR_URL", f"http://{MEDIA_LAN_IP}:8989")
RADARR_URL: str = _get("RADARR_URL", f"http://{MEDIA_LAN_IP}:7878")
QBIT_URL: str = _get("QBIT_URL", f"http://{MEDIA_LAN_IP}:8090")
BAZARR_URL: str = _get("BAZARR_URL", f"http://{MEDIA_LAN_IP}:6767")

# Runtime artifact tree under var/.  Logs, locks, sentinels, codex reviews —
# everything ephemeral that previously littered the repo root now lives
# here (the var/ runtime refactor, 2026-05-19).  Per-file media locks
# (.consolidate-<name>.lock) stay co-located with the media file because
# flock semantics require same-filesystem.
VAR_DIR: Path = Path(_get("VAR_DIR", str(MEDIA_STACK_ROOT / "var")))
VAR_LOG: Path = Path(_get("VAR_LOG", str(VAR_DIR / "log")))
VAR_RUN: Path = Path(_get("VAR_RUN", str(VAR_DIR / "run")))
VAR_STATE: Path = Path(_get("VAR_STATE", str(VAR_DIR / "state")))
VAR_REVIEWS: Path = Path(_get("VAR_REVIEWS", str(VAR_DIR / "reviews")))


__all__ = [
    "MEDIA_STACK_ROOT", "MEDIA_ROOT", "MEDIA_LAN_IP", "CALIBRE_LIBRARY",
    "PUID", "PGID", "TZ",
    "SONARR_URL", "RADARR_URL", "QBIT_URL", "BAZARR_URL",
    "VAR_DIR", "VAR_LOG", "VAR_RUN", "VAR_STATE", "VAR_REVIEWS",
    "ensure_var_dirs", "load_env_file",
]


def ensure_var_dirs() -> None:
    """Defensive `mkdir -p` for the `var/` tree.  Call from writers
    (entry-point scripts that open log files, state JSON, sentinels)
    BEFORE the first write — `.gitkeep` covers a fresh `git clone`
    into the default `MEDIA_STACK_ROOT`, but a replicator who
    overrides `VAR_DIR` won't have the tracked dirs.

    Importing `media_stack.paths` is intentionally read-only — this
    helper is the single point that performs filesystem writes, so
    read-only consumers (review tools, dry-run scanners) never mkdir a
    runtime tree just by importing the module.

    Fails fast with a stderr message + non-zero exit on permission /
    disk errors: swallowing them silently would let a cron entry start
    without a durable log dir and then die at the first log write, with
    the operator never seeing that the run began.
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
        # Fail-fast: writers can't durably log without these.  Caller
        # gets a SystemExit which propagates the failure to cron mail
        # / journalctl instead of dying silently 5 lines later when
        # the log open raises.
        raise SystemExit(
            f"media_stack.paths.ensure_var_dirs: {len(failures)} "
            f"of 4 var/ dirs failed to create"
        )


def dump_values() -> None:
    """Print every resolved value as `KEY=value` lines.  Used by the
    `__main__` block here AND by the `media_paths.py` shim's CLI
    mode — both must produce identical output for `eval`-style
    consumers (codex round-5 #5)."""
    for k in __all__:
        if k in ("ensure_var_dirs", "load_env_file", "dump_values"):
            continue
        print(f"{k}={globals()[k]}")


__all__.append("dump_values")


if __name__ == "__main__":
    dump_values()
