#!/bin/bash
# ops/healthcheck-alert.sh — hourly cron wrapper around healthcheck.sh.
#
# On a non-zero healthcheck it RECORDS the failure to
# var/log/healthcheck-issues.log.  Operator Telegram notification is
# OPT-IN (HEALTHCHECK_ALERT_NOTIFY=1) and OFF by default.
#
# Per operator instruction 2026-06-08 ("don't text me with healthcheck
# issues; just resolve them yourself") the operator is NOT paged about
# healthcheck issues.  Resolution comes from the self-healing crons
# (bazarr-profile-audit 04:30, movie-dedupe 04:45, normalize-driver */15)
# plus agent review of the issues log.  (This script originally closed
# AUDIT A9 / codex round-13 #1 as a Telegram alerter; the page is now
# opt-in — see the Closed-log note in AUDIT.md.)
#
# Exit codes (preserved from healthcheck.sh):
#   0 = all green   → silent, nothing recorded
#   1 = FAIL        → recorded to issues log; Telegram only if NOTIFY=1
#   2 = WARN-only   → recorded to issues log; Telegram only if NOTIFY=1
#
# The hourly cron entry writes full healthcheck output to
# var/log/healthcheck.log regardless of result; this wrapper adds the
# issue record (and the opt-in page) on top of that.
#
# Token + chat ID source (only when NOTIFY=1): $HOME/.claude/channels/telegram/.env
# (same source as normalize-driver.sh).  Parsed with the same
# non-executing pattern (codex round-10 #4 — no `.` / `source`).

set -uo pipefail

_here="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../lib/paths.sh
. "$_here/lib/paths.sh"

# Codex round-15 #7: TELEGRAM_ENV + the healthcheck binary path are
# both overridable via env so the wrapper is testable in isolation
# (see tests/test_healthcheck_alert.py).  Defaults match production:
# global Telegram plugin .env + the repo-root healthcheck.sh.
TELEGRAM_ENV="${TELEGRAM_ENV:-$HOME/.claude/channels/telegram/.env}"
HEALTHCHECK_BIN="${HEALTHCHECK_BIN:-$_here/healthcheck.sh}"

# Operator paging is opt-in (default OFF) — see header + operator
# instruction 2026-06-08.  When OFF, non-zero healthchecks are recorded to
# the issues log only.  Both the gate and the log path are env-overridable
# so the wrapper stays testable in isolation.
NOTIFY_OPERATOR="${HEALTHCHECK_ALERT_NOTIFY:-0}"
ISSUES_LOG="${HEALTHCHECK_ISSUES_LOG:-$VAR_LOG/healthcheck-issues.log}"

HC_OUTPUT=$(mktemp -t healthcheck-alert.XXXXXX)
trap 'rm -f "$HC_OUTPUT"' EXIT

# Run the healthcheck, tee its output so cron's stdout redirect still
# captures it, AND keep a copy for the alert body.
"$HEALTHCHECK_BIN" 2>&1 | tee "$HC_OUTPUT"
hc_rc=${PIPESTATUS[0]}

# Exit 0 → green, nothing to record or alert.
if (( hc_rc == 0 )); then
  exit 0
fi

# Non-zero.  Classify severity first (codex round-16 #4: only rc=2 is
# WARN; every other non-zero — incl. 126/127 exec failures, 128+N signals
# — is FAIL so a wrapper that couldn't even run the probe still surfaces).
if (( hc_rc == 2 )); then
  severity="WARN"
else
  severity="FAIL"
fi
hostname=$(hostname -s 2>/dev/null || echo unknown)
stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ALWAYS record the issue to the issues log — this is the operator-silent
# record the agent reviews and resolves from, in place of paging.
{
  printf '[%s] healthcheck %s on %s (rc=%s)\n' "$stamp" "$severity" "$hostname" "$hc_rc"
  tail -n 30 "$HC_OUTPUT" 2>/dev/null
  printf '\n'
} >> "$ISSUES_LOG" 2>/dev/null \
  || echo "healthcheck-alert: could not write $ISSUES_LOG (hc_rc=$hc_rc)" >&2

# Operator Telegram is OPT-IN only.  Default: do not text the operator
# about healthcheck issues (operator instruction 2026-06-08).
if [[ "$NOTIFY_OPERATOR" != "1" ]]; then
  exit "$hc_rc"
fi

# --- opt-in Telegram page path ---
# Don't propagate alert failures — cron must continue to log the original
# healthcheck result via the >> redirect in the crontab entry.
if [[ ! -r "$TELEGRAM_ENV" ]]; then
  echo "healthcheck-alert: $TELEGRAM_ENV not readable; skipping notify (hc_rc=$hc_rc)" >&2
  exit "$hc_rc"
fi

TELEGRAM_BOT_TOKEN=""
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
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

if [[ -z "$TELEGRAM_BOT_TOKEN" || -z "$TELEGRAM_CHAT_ID" ]]; then
  echo "healthcheck-alert: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID; skipping notify (hc_rc=$hc_rc)" >&2
  exit "$hc_rc"
fi

# Build the page body from the severity/hostname/stamp computed above
# (reused so the page and the issues-log record stay consistent).
MSG=$(printf '[%s] healthcheck %s on %s\n\n%s' \
  "$stamp" "$severity" "$hostname" \
  "$(tail -n 30 "$HC_OUTPUT" 2>/dev/null)")

export TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID MSG
PYTHONPATH="$MEDIA_STACK_ROOT" python3 -m media_stack.cli telegram_send \
  || echo "healthcheck-alert: telegram notify failed (non-fatal)" >&2
unset TELEGRAM_BOT_TOKEN MSG

exit "$hc_rc"
