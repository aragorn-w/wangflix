#!/bin/bash
# Compare live cron, systemd and logrotate state against the snapshots under ops/ (comments and
# blanks are ignored on both sides). Exit codes:
#   0  no drift, no warnings
#   1  DRIFT: live state differs from the repo
#   2  warnings only: a mode-640 unit unreadable even under `sudo -n`, so drift is possible but
#      unconfirmed (healthcheck.sh maps this to warn, not fail)

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../lib/paths.sh
. "$REPO_ROOT/lib/paths.sh"
drift=0
warn=0

# Same substitutions as ops/install.sh, or a host whose MEDIA_STACK_ROOT or service user differs
# from the placeholders reports drift forever.
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn)}"

# Escape `\`, `&` and the `|` delimiter for sed replacement strings; must match ops/install.sh.
sed_repl_escape() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }
MEDIA_STACK_ROOT_ESC=$(sed_repl_escape "$MEDIA_STACK_ROOT")
HOME_ESC=$(sed_repl_escape "$HOME")
SERVICE_USER_ESC=$(sed_repl_escape "$SERVICE_USER")
SERVICE_GROUP_ESC=$(sed_repl_escape "$SERVICE_GROUP")

render_snapshot() {
  # Mirrors install.sh: `$HOME/media-stack` -> MEDIA_STACK_ROOT first (more specific), then `$HOME`.
  sed -e "s|\$HOME/media-stack|${MEDIA_STACK_ROOT_ESC}|g" \
      -e "s|\$HOME|${HOME_ESC}|g" \
      -e "s|^User=mediauser$|User=${SERVICE_USER_ESC}|" \
      -e "s|^Group=mediauser$|Group=${SERVICE_GROUP_ESC}|" \
      "$1"
}

expected_cron="$REPO_ROOT/ops/cron.d/media-stack.crontab"
if [[ -r "$expected_cron" ]]; then
  rendered_cron=$(render_snapshot "$expected_cron" | grep -vE '^\s*(#|$)')
  if ! diff -u \
      <(crontab -l 2>/dev/null | grep -vE '^\s*(#|$)') \
      <(printf '%s\n' "$rendered_cron") \
      > /dev/null
  then
    printf 'DRIFT: crontab differs from %s (rendered with MEDIA_STACK_ROOT=%s)\n' \
        "$expected_cron" "$MEDIA_STACK_ROOT"
    diff -u \
        <(crontab -l 2>/dev/null | grep -vE '^\s*(#|$)') \
        <(printf '%s\n' "$rendered_cron")
    drift=1
  else
    printf 'OK: crontab matches ops/cron.d/media-stack.crontab\n'
  fi
fi

# Host-specific units (e.g. realtek-fix.service) are skipped on hosts without the hardware.
is_applicable() {
  case "$1" in
    realtek-fix.service)
      ip -o link show 2>/dev/null | grep -qw enp6s0f1
      ;;
    *)
      return 0
      ;;
  esac
}

for unit_file in "$REPO_ROOT"/ops/systemd/*.service; do
  [[ -r "$unit_file" ]] || continue
  unit=$(basename "$unit_file")
  if ! is_applicable "$unit"; then
    printf 'SKIP: %s (host-specific; prerequisite not present)\n' "$unit"
    continue
  fi
  # list-unit-files works without sudo even for a mode-640 unit. Not `grep -q`: it would close the
  # pipe early, SIGPIPE systemctl under pipefail, and report a spurious "not installed".
  if ! systemctl list-unit-files "$unit" 2>/dev/null | grep -E "^${unit}\\s" >/dev/null; then
    printf 'WARN: systemd unit %s not installed\n' "$unit"
    drift=1
    continue
  fi
  if ! live=$(systemctl cat "$unit" 2>/dev/null); then
    # Mode-640 body: retry the READ ALONE under sudo (the whole script as root would compare root's
    # crontab and /root paths). `sudo -n` so cron never blocks on a password; without it, WARN
    # (exit 2), not a silent pass.
    if ! live=$(sudo -n systemctl cat "$unit" 2>/dev/null); then
      printf 'WARN: %s installed but unreadable (mode-640 + no passwordless sudo) — drift check skipped\n' "$unit"
      warn=1
      continue
    fi
  fi
  rendered_unit=$(render_snapshot "$unit_file" | grep -vE '^\s*(#|$)')
  if ! diff -u \
      <(printf '%s\n' "$live" | grep -vE '^\s*(#|$)') \
      <(printf '%s\n' "$rendered_unit") \
      > /dev/null
  then
    printf 'DRIFT: %s body differs from %s (rendered with MEDIA_STACK_ROOT=%s)\n' \
        "$unit" "$unit_file" "$MEDIA_STACK_ROOT"
    diff -u \
        <(printf '%s\n' "$live" | grep -vE '^\s*(#|$)') \
        <(printf '%s\n' "$rendered_unit")
    drift=1
  else
    printf 'OK: %s matches\n' "$unit"
  fi
done

# Live /etc/logrotate.d/media-stack is root-owned 644, readable without sudo; render_snapshot's
# User/Group subs are no-ops here.
expected_logrotate="$REPO_ROOT/ops/logrotate.d/media-stack"
live_logrotate="/etc/logrotate.d/media-stack"
if [[ -r "$expected_logrotate" ]]; then
  if [[ ! -r "$live_logrotate" ]]; then
    printf 'DRIFT: logrotate config %s not installed (run: bash ops/install.sh --apply)\n' "$live_logrotate"
    drift=1
  else
    rendered_lr=$(render_snapshot "$expected_logrotate" | grep -vE '^\s*(#|$)')
    if ! diff -u \
        <(grep -vE '^\s*(#|$)' "$live_logrotate") \
        <(printf '%s\n' "$rendered_lr") \
        > /dev/null
    then
      printf 'DRIFT: %s differs from %s (rendered with MEDIA_STACK_ROOT=%s)\n' \
          "$live_logrotate" "$expected_logrotate" "$MEDIA_STACK_ROOT"
      diff -u \
          <(grep -vE '^\s*(#|$)' "$live_logrotate") \
          <(printf '%s\n' "$rendered_lr")
      drift=1
    else
      printf 'OK: logrotate config matches ops/logrotate.d/media-stack\n'
    fi
  fi
fi

if [[ $drift -ne 0 ]]; then
  exit 1
fi
if [[ $warn -ne 0 ]]; then
  exit 2
fi
exit 0
