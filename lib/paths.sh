# lib/paths.sh — sourceable host-identity loader for shell scripts.
#
# Source this from any *.sh that needs MEDIA_STACK_ROOT, MEDIA_LAN_IP,
# CALIBRE_LIBRARY, or any of the derived service URLs.  Single source of
# truth so the same values aren't duplicated/drifted across every script.
#
# Caller pattern:
#
#     # Resolve repo root relative to this script + source the helper
#     _here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#     . "$_here/lib/paths.sh"
#
#     # All MEDIA_* + *_URL values are now exported
#     curl -s "$RADARR_URL/api/v3/system/status"
#
# Or for a script at a known absolute location:
#
#     . $HOME/media-stack/lib/paths.sh
#
# This is a NON-EXECUTING parser — Docker Compose .env syntax allows
# command substitutions that bash `.` / `source` would execute (see
# healthcheck.sh's parser, which this mirrors).  Each line is split on
# the FIRST `=`, comments + blanks skipped, surrounding quotes stripped.
# `|| [[ -n "$_k" ]]` rescues the last line of files without a trailing
# newline.

# Documented precedence (matches media_stack/paths.py): process env >
# .env > defaults.  To honour that for MEDIA_STACK_ROOT specifically, we
# first snapshot which keys the caller actually owns in the process env,
# THEN auto-derive defaults — that way the .env loader below can still
# override anything we auto-derived, while leaving real process-env
# values alone (codex round-3 #1: lib/paths.sh was auto-deriving
# MEDIA_STACK_ROOT first, then the "already set?" guard treated it as
# caller-owned and skipped the .env line, breaking the contract).
_paths_env_owned="|"
for _k in MEDIA_STACK_ROOT MEDIA_ROOT MEDIA_LAN_IP CALIBRE_LIBRARY \
          SONARR_URL RADARR_URL QBIT_URL BAZARR_URL \
          VAR_DIR VAR_LOG VAR_RUN VAR_STATE VAR_REVIEWS \
          PUID PGID TZ ALLOW_PUBLIC_IFACE; do
  # Codex round-15 #2: use "is set" (`+x`) not "is non-empty" (`-n`)
  # so explicitly-empty process-env overrides win over .env values.
  [[ ${!_k+x} ]] && _paths_env_owned+="$_k|"
done

# Auto-derive MEDIA_STACK_ROOT for the purpose of finding .env.  This
# file lives at $REPO/lib/paths.sh, so the repo root is the parent
# directory of this script's directory.  A MEDIA_STACK_ROOT= line in
# .env can still override the auto-derived value (just not a real
# process-env value — see _paths_env_owned).
if [[ -z "${MEDIA_STACK_ROOT:-}" ]]; then
  _paths_self="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  MEDIA_STACK_ROOT="$_paths_self"
  unset _paths_self
fi

_env_file="${MEDIA_STACK_ROOT}/.env"
if [[ -r "$_env_file" ]]; then
  while IFS='=' read -r _k _v || [[ -n "$_k" ]]; do
    [[ -z "$_k" || "$_k" =~ ^[[:space:]]*# ]] && continue
    _k="${_k#"${_k%%[![:space:]]*}"}"; _k="${_k%"${_k##*[![:space:]]}"}"
    _v="${_v#"${_v%%[![:space:]]*}"}"; _v="${_v%"${_v##*[![:space:]]}"}"
    _v="${_v%\"}"; _v="${_v#\"}"
    _v="${_v%\'}"; _v="${_v#\'}"
    case "$_k" in
      MEDIA_STACK_ROOT|MEDIA_ROOT|MEDIA_LAN_IP|CALIBRE_LIBRARY|\
      SONARR_URL|RADARR_URL|QBIT_URL|BAZARR_URL|\
      VAR_DIR|VAR_LOG|VAR_RUN|VAR_STATE|VAR_REVIEWS|\
      PUID|PGID|TZ|ALLOW_PUBLIC_IFACE)
        # Only let .env set/override if the caller didn't already own
        # this key in the process env.  We compare against the snapshot
        # taken before auto-derivation so MEDIA_STACK_ROOT specifically
        # is still overridable here.
        #
        # CRITICAL (codex round-10 #1): use `printf -v` for the
        # assignment, NOT `eval`.  eval would expand command
        # substitutions in the value — e.g. a malicious `.env` line
        # `MEDIA_ROOT="$(touch /tmp/poc)"` would have executed when
        # sourced.  The whole point of the non-executing parser is
        # broken if we then `eval` the parsed value.  `printf -v`
        # assigns the literal string to the named variable with no
        # expansion of the value.
        if [[ "$_paths_env_owned" != *"|${_k}|"* ]]; then
          printf -v "$_k" '%s' "$_v"
        fi
        ;;
    esac
  done < "$_env_file"
fi
unset _env_file _k _v _paths_env_owned

# Defaults for anything still unset
: "${MEDIA_STACK_ROOT:=$HOME/media-stack}"
: "${MEDIA_ROOT:=${MEDIA_STACK_ROOT}/data/media}"
: "${MEDIA_LAN_IP:=10.0.0.10}"
: "${CALIBRE_LIBRARY:=/srv/calibre/library}"
: "${PUID:=1000}"
: "${PGID:=1000}"
: "${TZ:=UTC}"

# Derived service URLs — overridable via .env for split-host setups
: "${SONARR_URL:=http://${MEDIA_LAN_IP}:8989}"
: "${RADARR_URL:=http://${MEDIA_LAN_IP}:7878}"
: "${QBIT_URL:=http://${MEDIA_LAN_IP}:8090}"
: "${BAZARR_URL:=http://${MEDIA_LAN_IP}:6767}"

# Runtime artifact tree under var/.  Logs, locks, sentinels, codex reviews —
# everything ephemeral that previously littered the repo root now lives
# here (the var/ runtime refactor, 2026-05-19).  Per-file media locks
# (.consolidate-<name>.lock) stay co-located with the media file because
# flock semantics require same-filesystem.
: "${VAR_DIR:=${MEDIA_STACK_ROOT}/var}"
: "${VAR_LOG:=${VAR_DIR}/log}"
: "${VAR_RUN:=${VAR_DIR}/run}"
: "${VAR_STATE:=${VAR_DIR}/state}"
: "${VAR_REVIEWS:=${VAR_DIR}/reviews}"

export MEDIA_STACK_ROOT MEDIA_ROOT MEDIA_LAN_IP CALIBRE_LIBRARY
export PUID PGID TZ
export SONARR_URL RADARR_URL QBIT_URL BAZARR_URL
export VAR_DIR VAR_LOG VAR_RUN VAR_STATE VAR_REVIEWS

# Ensure var/ dirs exist for writers.  Cron entries redirect to
# var/log/*.log, scripts write sentinels to var/state/, etc.  On a fresh
# checkout the .gitkeep tracked dirs are in place, but a `git clone` into
# a clean dir + custom MEDIA_STACK_ROOT/VAR_DIR override might not.
# Quietly mkdir -p so we never fail a redirect on first run.
mkdir -p "$VAR_LOG" "$VAR_RUN" "$VAR_STATE" "$VAR_REVIEWS" 2>/dev/null || true
