# Sourceable host-identity loader; precedence matches media_stack/paths.py: process env > .env > defaults.
#
# NON-EXECUTING .env parser: Compose syntax allows command substitutions that `source` would
# execute. Lines are split on the FIRST `=`, comments and blanks skipped, quotes stripped.
# `|| [[ -n "$_k" ]]` rescues a last line without a trailing newline.

# Snapshot which keys the caller owns in the process env BEFORE auto-deriving MEDIA_STACK_ROOT,
# or the derived value would look caller-owned and block its own .env override.
_paths_env_owned="|"
for _k in MEDIA_STACK_ROOT MEDIA_ROOT MEDIA_LAN_IP CALIBRE_LIBRARY \
          SONARR_URL RADARR_URL QBIT_URL BAZARR_URL \
          VAR_DIR VAR_LOG VAR_RUN VAR_STATE VAR_REVIEWS \
          PUID PGID TZ ALLOW_PUBLIC_IFACE; do
  # "is set" (`+x`), not "is non-empty": an explicitly-empty process-env value wins over .env.
  [[ ${!_k+x} ]] && _paths_env_owned+="$_k|"
done

# Auto-derive MEDIA_STACK_ROOT (needed to find .env) from $REPO/lib/paths.sh; .env can still override it.
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
        # CRITICAL: `printf -v`, NOT `eval`, which would execute a command substitution in the value.
        if [[ "$_paths_env_owned" != *"|${_k}|"* ]]; then
          printf -v "$_k" '%s' "$_v"
        fi
        ;;
    esac
  done < "$_env_file"
fi
unset _env_file _k _v _paths_env_owned

: "${MEDIA_STACK_ROOT:=$HOME/media-stack}"
: "${MEDIA_ROOT:=${MEDIA_STACK_ROOT}/data/media}"
: "${MEDIA_LAN_IP:=10.0.0.10}"
: "${CALIBRE_LIBRARY:=/srv/calibre/library}"
: "${PUID:=1000}"
: "${PGID:=1000}"
: "${TZ:=UTC}"

: "${SONARR_URL:=http://${MEDIA_LAN_IP}:8989}"
: "${RADARR_URL:=http://${MEDIA_LAN_IP}:7878}"
: "${QBIT_URL:=http://${MEDIA_LAN_IP}:8090}"
: "${BAZARR_URL:=http://${MEDIA_LAN_IP}:6767}"

# Runtime tree under var/. Per-file media locks stay next to the media file (flock needs the same
# filesystem).
: "${VAR_DIR:=${MEDIA_STACK_ROOT}/var}"
: "${VAR_LOG:=${VAR_DIR}/log}"
: "${VAR_RUN:=${VAR_DIR}/run}"
: "${VAR_STATE:=${VAR_DIR}/state}"
: "${VAR_REVIEWS:=${VAR_DIR}/reviews}"

export MEDIA_STACK_ROOT MEDIA_ROOT MEDIA_LAN_IP CALIBRE_LIBRARY
export PUID PGID TZ
export SONARR_URL RADARR_URL QBIT_URL BAZARR_URL
export VAR_DIR VAR_LOG VAR_RUN VAR_STATE VAR_REVIEWS

# A custom VAR_DIR may not exist yet: create quietly rather than fail a cron redirect on first run.
mkdir -p "$VAR_LOG" "$VAR_RUN" "$VAR_STATE" "$VAR_REVIEWS" 2>/dev/null || true
