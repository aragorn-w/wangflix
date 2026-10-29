# lib/: shared shell infrastructure

Bash helpers that are `source`d from other scripts, never executed standalone.

- **`paths.sh`**: host-identity loader (mirror of `media_stack/paths.py`): `MEDIA_STACK_ROOT`, `MEDIA_LAN_IP`, Calibre, PUID/PGID/TZ, service URLs and the `var/` tree (`VAR_LOG`, `VAR_RUN`, `VAR_STATE`, `VAR_REVIEWS`) from `.env` with defaults.

## Rules

- **Non-executing `.env` parser:** split each line on the first `=`, strip one layer of surrounding quotes, skip comment/blank lines, and NEVER `eval` or `source` the file (compose `.env` syntax can contain `$(...)`). Any new parser here MUST keep that property.
- **Precedence: process env > `.env` > defaults.** `paths.sh` snapshots which keys the caller already owns BEFORE auto-deriving `MEDIA_STACK_ROOT`, so `.env` can still override the derived value while real process-env values always win. Any helper adding env-derived variables MUST follow the same pattern.
- **The only side effect allowed is `mkdir -p` on the `var/` tree** (matching `media_stack.paths.ensure_var_dirs()`), with `|| true` so a permission failure doesn't break sourcing (the caller's first write surfaces it). Anything that opens sockets, calls `docker` or spawns a long-running process belongs in a dedicated script.
- **Export every key explicitly** at the end of the file (cron passes them to Python scripts).
- **Never `set -e` at the top of a sourceable helper:** the caller's `set` flags are theirs.
- Add a helper here only when two or more shell scripts already inline the same parsing/derivation.

After editing `paths.sh`: `bash -n lib/paths.sh`; smoke precedence with `unset MEDIA_LAN_IP; bash -c '. lib/paths.sh; echo $MEDIA_LAN_IP'` (-> `.env` or default) and `MEDIA_LAN_IP=1.2.3.4 bash -c '. lib/paths.sh; echo $MEDIA_LAN_IP'` (-> `1.2.3.4`).
