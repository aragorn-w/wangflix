#!/bin/bash
# Hourly cron wrapper around healthcheck.sh: a non-zero result is recorded to
# var/log/healthcheck-issues.log. The operator is deliberately NOT paged by default (self-healing
# crons plus agent review of the log resolve issues); Telegram is opt-in (HEALTHCHECK_ALERT_NOTIFY=1),
# with token + chat ID from $HOME/.claude/channels/telegram/.env, parsed without sourcing it.
# Exit code is healthcheck.sh's: 0 silent, 1 FAIL, 2 WARN-only.

set -uo pipefail

_here="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../lib/paths.sh
. "$_here/lib/paths.sh"

# env-overridable for isolated tests (tests/test_healthcheck_alert.py)
TELEGRAM_ENV="${TELEGRAM_ENV:-$HOME/.claude/channels/telegram/.env}"
HEALTHCHECK_BIN="${HEALTHCHECK_BIN:-$_here/healthcheck.sh}"

NOTIFY_OPERATOR="${HEALTHCHECK_ALERT_NOTIFY:-0}"
ISSUES_LOG="${HEALTHCHECK_ISSUES_LOG:-$VAR_LOG/healthcheck-issues.log}"

HC_OUTPUT=$(mktemp -t healthcheck-alert.XXXXXX)
trap 'rm -f "$HC_OUTPUT"' EXIT

# tee so cron's stdout redirect still captures the output, and keep a copy for the alert body.
"$HEALTHCHECK_BIN" 2>&1 | tee "$HC_OUTPUT"
hc_rc=${PIPESTATUS[0]}

if (( hc_rc == 0 )); then
  exit 0
fi

# Only rc=2 is WARN; every other non-zero (126/127 exec failures, 128+N signals) is FAIL,
# so a probe that could not even run still surfaces.
if (( hc_rc == 2 )); then
  severity="WARN"
else
  severity="FAIL"
fi
hostname=$(hostname -s 2>/dev/null || echo unknown)
stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ALWAYS record: the agent resolves issues from this log.
{
  printf '[%s] healthcheck %s on %s (rc=%s)\n' "$stamp" "$severity" "$hostname" "$hc_rc"
  tail -n 30 "$HC_OUTPUT" 2>/dev/null
  printf '\n'
} >> "$ISSUES_LOG" 2>/dev/null \
  || echo "healthcheck-alert: could not write $ISSUES_LOG (hc_rc=$hc_rc)" >&2

if [[ "$NOTIFY_OPERATOR" != "1" ]]; then
  exit "$hc_rc"
fi

# Alert failures must not mask the original healthcheck exit code.
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

MSG=$(printf '[%s] healthcheck %s on %s\n\n%s' \
  "$stamp" "$severity" "$hostname" \
  "$(tail -n 30 "$HC_OUTPUT" 2>/dev/null)")

export TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID MSG
PYTHONPATH="$MEDIA_STACK_ROOT" python3 -m media_stack.cli telegram_send \
  || echo "healthcheck-alert: telegram notify failed (non-fatal)" >&2
unset TELEGRAM_BOT_TOKEN MSG

exit "$hc_rc"
