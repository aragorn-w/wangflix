#!/bin/bash
# normalize-driver.sh — autonomous movie-normalization watchdog.
#
# Cron entry (every 15 min):
#   */15 * * * * $HOME/media-stack/normalize-driver.sh >> $HOME/media-stack/var/log/normalize-driver.log 2>&1
#
# Behavior per tick:
#   - If $VAR_STATE/normalize-driver.done sentinel exists, exit 0.
#   - Acquire $VAR_RUN/normalize-driver.lock via flock; if held, exit 0.
#   - If a `normalize-audio.py --scan ... movies` is alive AND making progress, exit 0.
#   - If alive but wedged (see STUCK_MIN below) — kill and relaunch.
#   - Else count v1 coverage on the movies tree.
#       * If full coverage, write sentinel, telegram user, exit 0.
#       * Else, setsid+nohup-relaunch the sweep at $JOBS and exit.

set -euo pipefail

# Host identity (MEDIA_STACK_ROOT, MEDIA_ROOT, …) from the shared helper.
_here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/paths.sh
. "$_here/lib/paths.sh"

MEDIA_DIR="$MEDIA_ROOT/movies"
SCRIPT="$MEDIA_STACK_ROOT/normalize-audio.py"
SWEEP_LOG="$VAR_LOG/movies-normalize.out"
DRIVER_LOG="$VAR_LOG/normalize-driver.log"
DONE_SENTINEL="$VAR_STATE/normalize-driver.done"
LOCK_FILE="$VAR_RUN/normalize-driver.lock"
TELEGRAM_ENV=$HOME/.claude/channels/telegram/.env  # global, not stack-local
ENV_FILE="$MEDIA_STACK_ROOT/.env"

# Parse a whitelist of overrides from .env without executing it as shell
# (codex round-10 finding #5 — was `. "$ENV_FILE"`, which is unsafe given
# Docker Compose .env semantics that allow $(...) substitutions).
#
# Precedence: process env > .env > defaults.  Codex round-debloat #3
# caught that this block was overwriting caller-supplied values
# (e.g. `JOBS=3 ./normalize-driver.sh` would be clobbered by .env's
# JOBS).  Snapshot which keys the caller already owns BEFORE the
# parse loop; the case below only exports keys that weren't set in
# process env.
_owned="|"
for _k in TELEGRAM_CHAT_ID JOBS STUCK_MIN; do
  [[ ${!_k+x} ]] && _owned+="$_k|"
done
unset _k
if [[ -r "$ENV_FILE" ]]; then
  while IFS='=' read -r key val || [[ -n "$key" ]]; do
    [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
    key="${key#"${key%%[![:space:]]*}"}"; key="${key%"${key##*[![:space:]]}"}"
    val="${val#"${val%%[![:space:]]*}"}"; val="${val%"${val##*[![:space:]]}"}"
    val="${val%\"}"; val="${val#\"}"
    val="${val%\'}"; val="${val#\'}"
    case "$key" in
      TELEGRAM_CHAT_ID|JOBS|STUCK_MIN)
        if [[ "$_owned" != *"|${key}|"* ]]; then
          export "$key=$val"
        fi
        ;;
    esac
  done < "$ENV_FILE"
fi
unset _owned

# TELEGRAM_CHAT_ID is operator-specific.  Set via .env or process env.
# Absent → notify is skipped (the notify block at the end of the script
# bails out cleanly).
TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID:-}
# JOBS=5 is the production target on this 8-core/HDD setup. History (with
# pass2 timeout floor 3600s, commit 737f552). Two numbers per row because
# small/mid files (≤30GB) sail through but 60-100GB UHDs are at the edge:
#   - JOBS=8: 78% fail rate on UHD batch
#   - JOBS=6: 50% fail rate on UHD batch
#   - JOBS=5: 0% fail on ≤30GB files, ~17-19% fail on 60-100GB UHD long
#       tail / 2.5-3 fixes/hr overall (production winner — AGENTS.md mirrors
#       this). UHD long-tail timeouts can usually be reaped + retried by
#       the watchdog at STUCK_MIN=360.
#   - JOBS=4: not retested under new floor (5 already wins)
#   - JOBS=3: 0% fail / 2.0 fixes/hr
#   - JOBS=2: 0% fail / 1.5 fixes/hr
# Overridable via JOBS=N in $HOME/media-stack/.env (.env sourced above).
JOBS=${JOBS:-5}
TAG_NAME=NORMALIZED_AUDIO
TAG_WANT=v1

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

[[ -f "$DONE_SENTINEL" ]] && exit 0

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "another driver run holds the lock; exit"
  exit 0
fi

# --- Liveness + progress watchdog ---
# A sweep can be "alive but stuck" — process exists, ffmpeg eats CPU, but no
# files complete. Two-condition kill so we don't reap fresh sweeps grinding
# their first UHD cohort: (a) sweep must itself have been running > STUCK_MIN,
# AND (b) no FIXED line newer than STUCK_MIN minutes ago.
NORMALIZE_LOG="$VAR_LOG/normalize-audio.log"
# 360 min covers the worst-case healthy pipeline timing under contention:
#   - pass1 timeout scales `max(3600, size_mb*0.6+1800)` → up to ~6h for 60GB
#   - pass2 timeout scales `max(3600, size_mb/5+1800)` → up to ~6h for 100GB
# A single huge UHD can legitimately spend 4–5h in pipeline before logging
# FIXED. Watchdog fires only if BOTH `sweep_age > STUCK_MIN` AND
# `last_fixed_age > STUCK_MIN`, so a recent FIXED keeps the sweep alive.
STUCK_MIN=${STUCK_MIN:-360}

sweep_pid=$(pgrep -f "${SCRIPT}.*--scan.*movies" | head -1 || true)
if [[ -n "$sweep_pid" ]]; then
  sweep_age_sec=$(ps -o etimes= -p "$sweep_pid" 2>/dev/null | tr -d ' ')
  sweep_age_sec=${sweep_age_sec:-0}
  sweep_age_min=$(( sweep_age_sec / 60 ))

  last_fixed_age_min=99999
  last_fixed_ts=$(grep "/movies/" "$NORMALIZE_LOG" 2>/dev/null \
    | grep "FIXED" | tail -1 | awk '{print $1, $2}')
  if [[ -n "$last_fixed_ts" ]]; then
    last_epoch=$(date -d "$last_fixed_ts UTC" +%s 2>/dev/null || echo 0)
    last_fixed_age_min=$(( ( $(date -u +%s) - last_epoch ) / 60 ))
  fi

  if [[ $sweep_age_min -gt $STUCK_MIN && $last_fixed_age_min -gt $STUCK_MIN ]]; then
    log "sweep wedged: age=${sweep_age_min}min, last FIXED ${last_fixed_age_min}min ago (both >${STUCK_MIN}); killing"
    pkill -9 -f "${SCRIPT}.*--scan.*movies" 2>/dev/null || true
    pkill -9 -f "ffmpeg.*loudnorm.*movies" 2>/dev/null || true
    sleep 5
  else
    log "sweep alive: age=${sweep_age_min}min, last FIXED ${last_fixed_age_min}min ago; exit"
    exit 0
  fi
fi

# Coverage probe — count files vs files with NORMALIZED_AUDIO=v1 in MKV format_tags.
# Exclude *.tmp.mkv staging artifacts from consolidate-subs (or any
# future one-shot helper) (codex round-13 finding #4); without this
# exclusion a failed staging leftover would inflate the denominator
# and the driver might launch normalization against a half-written
# temp file.
mapfile -d '' files < <(find "$MEDIA_DIR" -type f \
  \( -iname "*.mkv" -o -iname "*.mp4" -o -iname "*.m4v" \) \
  ! -iname "*.tmp.mkv" \
  -not -path "*/.normalize-tmp/*" -print0)
total=${#files[@]}

if [[ $total -eq 0 ]]; then
  log "no video files under $MEDIA_DIR; exit"
  exit 0
fi

# `|| true` is required: xargs returns 123 when ANY worker exits non-zero,
# which happens whenever a file lacks the tag (sh's `[ ... ]` returns 1).
# With `set -e` the driver would abort here before ever launching the sweep.
tagged=$(printf '%s\0' "${files[@]}" \
  | xargs -0 -P 8 -I {} sh -c \
      'tag=$(ffprobe -v error -show_entries format_tags='"$TAG_NAME"' -of default=nw=1:nk=1 "$1" 2>/dev/null); [ "$tag" = "'"$TAG_WANT"'" ] && echo y' _ {} 2>/dev/null \
  | wc -l) || true

log "coverage: ${tagged}/${total}"

if [[ $tagged -ge $total ]]; then
  touch "$DONE_SENTINEL"
  log "ALL DONE — wrote $DONE_SENTINEL"
  # Notify via Telegram Bot API.  Token never appears in argv — the
  # `media_stack.cli telegram_send` subcommand reads it from the env
  # we pass through (codex round-6 #7 removed the inline heredoc;
  # delivery now goes through the tested media_stack.clients.telegram
  # adapter, matching the project convention that external service
  # integrations live behind adapters).
  if [[ -r "$TELEGRAM_ENV" ]]; then
    # Codex round-10 #4: `.` / `source` on an external file would
    # execute any shell metacharacters it contains.  Parse the two
    # keys we actually use with the same non-executing pattern lib/
    # paths.sh enforces (split on first `=`, strip one layer of
    # quotes, skip comments + blanks).
    #
    # Codex round-11 #1: only reset TELEGRAM_BOT_TOKEN before reading
    # — TELEGRAM_CHAT_ID may have come from .env (lib/paths.sh) or
    # process env and we want to preserve that fallback when the
    # global token file only carries the token.
    TELEGRAM_BOT_TOKEN=""
    while IFS='=' read -r _tk _tv || [[ -n "$_tk" ]]; do
      [[ -z "$_tk" || "$_tk" =~ ^[[:space:]]*# ]] && continue
      _tk="${_tk#"${_tk%%[![:space:]]*}"}"; _tk="${_tk%"${_tk##*[![:space:]]}"}"
      _tv="${_tv#"${_tv%%[![:space:]]*}"}"; _tv="${_tv%"${_tv##*[![:space:]]}"}"
      _tv="${_tv%\"}"; _tv="${_tv#\"}"
      _tv="${_tv%\'}"; _tv="${_tv#\'}"
      case "$_tk" in
        TELEGRAM_BOT_TOKEN) printf -v TELEGRAM_BOT_TOKEN '%s' "$_tv" ;;
        TELEGRAM_CHAT_ID)   printf -v TELEGRAM_CHAT_ID   '%s' "$_tv" ;;
      esac
    done < "$TELEGRAM_ENV"
    unset _tk _tv
    # Codex round-debloat-3 #2: round-2 made TELEGRAM_CHAT_ID truly
    # optional (default fallback dropped), so the gate must check BOTH
    # token AND chat ID.  Previously only checked the token; if token
    # was set but chat ID was absent, we'd still invoke telegram_send
    # which would fail with a generic "missing TELEGRAM_CHAT_ID" message
    # instead of skipping intentionally.
    if [[ -n "$TELEGRAM_BOT_TOKEN" && -n "$TELEGRAM_CHAT_ID" ]]; then
      export TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
      MSG=$(printf 'Movie normalization complete: %d/%d NORMALIZED_AUDIO=%s' \
        "$tagged" "$total" "$TAG_WANT")
      export MSG
      # Codex round-10 #8: cron invokes this script from $HOME, so
      # `python3 -m media_stack.cli` would fail to find the module.
      # Make the entry explicit via PYTHONPATH so cwd doesn't matter.
      PYTHONPATH="$MEDIA_STACK_ROOT" python3 -m media_stack.cli telegram_send \
        || log "telegram notify failed (non-fatal)"
      unset TELEGRAM_BOT_TOKEN MSG
    elif [[ -z "$TELEGRAM_BOT_TOKEN" ]]; then
      log "TELEGRAM_BOT_TOKEN missing in $TELEGRAM_ENV — skipping notify"
    else
      log "TELEGRAM_CHAT_ID not set (neither in .env nor process env) — skipping notify"
    fi
  else
    log "$TELEGRAM_ENV not readable — skipping notify"
  fi
  exit 0
fi

# Reap orphaned pass2 tmp files. ONLY delete those whose embedded PID is dead
# AND the file is at least 60 min old. Inline normalization (consolidate-subs
# wire-in path) launches normalize-audio.py as single-file invocations whose
# argv does NOT match "--scan.*movies", so the sweep-liveness check above
# misses them. Reaping all pass2.mkv unconditionally would destroy their work.
stale=0
while IFS= read -r -d '' f; do
  base=$(basename "$f")
  # Filename pattern: .<stem>.<pid>.pass2.mkv
  pid="${base##*.}"
  pid="${pid%.pass2.mkv}"
  rest="${base%.pass2.mkv}"
  pid="${rest##*.}"
  [[ "$pid" =~ ^[0-9]+$ ]] || continue
  # Live worker? Don't touch.
  if [[ -d "/proc/$pid" ]]; then
    continue
  fi
  # Recent file? Could be inline-normalize from consolidate-subs that hasn't
  # quite landed yet — give it an hour before claiming it's orphaned.
  if [[ -n "$(find "$f" -mmin -60 -print 2>/dev/null)" ]]; then
    continue
  fi
  rm -f "$f"
  stale=$((stale + 1))
done < <(find "$MEDIA_DIR" -path "*/.normalize-tmp/*.pass2.mkv" -type f -print0 2>/dev/null)
[[ $stale -gt 0 ]] && log "reaped $stale stale pass2 tmp file(s)"

log "relaunching sweep ($((total - tagged)) remaining) with JOBS=$JOBS"
cd "$MEDIA_STACK_ROOT"
# Close FD 9 in the spawned sweep so it doesn't inherit our flock — otherwise
# the long-running python sweep keeps holding the lock for the entire run and
# every subsequent cron tick sees "lock held" until the sweep ends. This bug
# made the watchdog effectively dead for autonomy on 2026-05-15.
setsid nohup "$SCRIPT" --scan "$MEDIA_DIR" --jobs "$JOBS" </dev/null \
  > "$SWEEP_LOG" 2>&1 9>&- &
disown
log "relaunch pid=$!"
