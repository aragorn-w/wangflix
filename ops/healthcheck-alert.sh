#!/bin/bash
# Hourly cron wrapper around healthcheck.sh: a non-zero result is recorded to
# var/log/healthcheck-issues.log. The operator is deliberately NOT paged (self-healing crons plus
# agent review of the log resolve issues).
# Exit code is healthcheck.sh's: 0 silent, 1 FAIL, 2 WARN-only.

set -uo pipefail

_here="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../lib/paths.sh
. "$_here/lib/paths.sh"

# env-overridable for isolated tests (tests/test_healthcheck_alert.py)
HEALTHCHECK_BIN="${HEALTHCHECK_BIN:-$_here/healthcheck.sh}"
ISSUES_LOG="${HEALTHCHECK_ISSUES_LOG:-$VAR_LOG/healthcheck-issues.log}"

HC_OUTPUT=$(mktemp -t healthcheck-alert.XXXXXX)
trap 'rm -f "$HC_OUTPUT"' EXIT

# tee so cron's stdout redirect still captures the output, and keep a copy for the issues log.
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

# The agent resolves issues from this log.
{
  printf '[%s] healthcheck %s on %s (rc=%s)\n' "$stamp" "$severity" "$hostname" "$hc_rc"
  tail -n 30 "$HC_OUTPUT" 2>/dev/null
  printf '\n'
} >> "$ISSUES_LOG" 2>/dev/null \
  || echo "healthcheck-alert: could not write $ISSUES_LOG (hc_rc=$hc_rc)" >&2

exit "$hc_rc"
