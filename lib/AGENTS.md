# lib/ — shared shell infrastructure

Sourceable bash helpers used across the stack's shell scripts.  All
files in this directory are meant to be `source`d (or `.`-loaded)
from another script, never executed standalone.

## Files

- **`paths.sh`** — host-identity loader.  Resolves `MEDIA_STACK_ROOT`
  + `MEDIA_LAN_IP` + Calibre + PUID/PGID/TZ + service URLs (Sonarr,
  Radarr, qBit, Bazarr) + the `var/` tree (`VAR_LOG`, `VAR_RUN`,
  `VAR_STATE`, `VAR_REVIEWS`) from `.env` with sensible defaults.
  Mirror of `media_stack/paths.py` for Python.

## Operating rules

- **Non-executing `.env` parser.**  `paths.sh` splits each line on
  the first `=`, strips a single layer of surrounding quotes, and
  skips comment/blank lines.  It NEVER does `eval`/`source` on the
  file — Docker-compose `.env` syntax can contain `$(...)` command
  substitutions that bash's `.` / `source` would execute.  Any new
  parser in this directory MUST preserve that property.
- **Precedence: process env > .env > defaults.**  `paths.sh`
  snapshots which keys the caller already owns in the process env
  BEFORE auto-deriving `MEDIA_STACK_ROOT`, so `.env` can still
  override the auto-derived value while real process-env values
  always win (codex round-3 #1 caught the inverted ordering).
  Any helper that adds new env-derived variables MUST follow the
  same pattern.
- **Side effects allowed: `mkdir -p` on the `var/` tree.**  This
  matches `media_stack.paths.ensure_var_dirs()` — replicators who
  override `VAR_DIR` won't have the tracked `.gitkeep` dirs.  The
  mkdir uses `|| true` so a permission failure doesn't break
  sourcing; the calling script's first write will surface any real
  problem.  (Python side fail-fasts here — codex round-6 #4 —
  because Python writers have stderr; shell writers use cron mail.)
- **Export everything by default.**  Variables loaded here are
  expected to be visible to subprocess invocations (e.g. cron
  passing them to Python scripts via env), so the file ends with
  one explicit `export` per key.
- **Never `set -e` at the top of a sourceable helper.**  The
  caller's `set` flags are theirs; we don't impose ours.

## Validation

After editing `paths.sh`:

```bash
bash -n lib/paths.sh                                    # syntax
diff <(python3 media_paths.py) <(python3 -m media_stack.paths) \
  && echo "shim ↔ module CLI parity OK"
# Smoke-test precedence:
unset MEDIA_LAN_IP; bash -c '. lib/paths.sh; echo $MEDIA_LAN_IP'  # → .env or default
MEDIA_LAN_IP=1.2.3.4 bash -c '. lib/paths.sh; echo $MEDIA_LAN_IP'  # → 1.2.3.4
```

## When to add a new helper here

- Two or more shell scripts already inline the same parsing /
  derivation logic.  One inline copy doesn't justify a helper yet.
- The helper has no side effects beyond the documented `mkdir -p`
  scope.  Anything that opens network sockets, calls `docker`, or
  spawns a long-running process belongs in a dedicated script, not
  a sourceable helper.
