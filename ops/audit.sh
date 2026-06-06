#!/bin/bash
# ops/audit.sh — compare live system orchestration state against the
# repo-tracked snapshots under ops/. Exits with a tri-state code so this
# script can distinguish "drift" from "couldn't verify":
#
#   0  no drift, no warnings — every snapshot matched live state
#   1  DRIFT detected — live cron or systemd differs from repo
#   2  warning(s) only — at least one unit is mode-640 AND passwordless
#      sudo is unavailable to read it (the body read is retried under
#      `sudo -n`); drift is *possible* but not confirmed
#
# `healthcheck.sh` maps exit 2 → warn() and exit 1 → fail() so an
# unverifiable mode-640 unit doesn't pollute the overall health verdict
# (codex round-5 follow-up).  On a host with passwordless sudo (the common
# case) the `sudo -n systemctl cat` retry reads the body and the unit is
# verified normally — no warning.
#
# Usage:  bash ops/audit.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../lib/paths.sh
. "$REPO_ROOT/lib/paths.sh"
drift=0
warn=0

# Render the same substitutions as ops/install.sh before diffing.  Without
# this, a host where MEDIA_STACK_ROOT differs from $HOME/media-stack
# or where the service user/group isn't the placeholder `mediauser` would
# report drift forever even when installed correctly via ops/install.sh.
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn)}"

# Escape values before they enter sed replacement strings — must match
# ops/install.sh exactly, or audit would diff a differently-escaped
# rendering against the installer's output.  The replacement side of
# s|...|VALUE|g treats `\`, `&`, and the `|` delimiter specially.
sed_repl_escape() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }
MEDIA_STACK_ROOT_ESC=$(sed_repl_escape "$MEDIA_STACK_ROOT")
HOME_ESC=$(sed_repl_escape "$HOME")
SERVICE_USER_ESC=$(sed_repl_escape "$SERVICE_USER")
SERVICE_GROUP_ESC=$(sed_repl_escape "$SERVICE_GROUP")

render_snapshot() {
  # render_snapshot <snapshot_path>
  # Echoes the snapshot body with MEDIA_STACK_ROOT + SERVICE_USER/GROUP
  # substituted to match what ops/install.sh would have installed.
  # Two `$HOME` substitution passes mirror ops/install.sh:
  #   1. `$HOME/media-stack` → MEDIA_STACK_ROOT (more specific, runs first)
  #   2. `$HOME` → expanded home dir (covers home-rooted out-of-stack paths
  #      like `$HOME/nightly-upgrade.sh` in the cron snapshot)
  # The `\$HOME` escape keeps bash from expanding `$HOME` to the current
  # shell's home directory before sed sees it — the snapshot stores the
  # placeholder as the literal `$HOME` string.
  sed -e "s|\$HOME/media-stack|${MEDIA_STACK_ROOT_ESC}|g" \
      -e "s|\$HOME|${HOME_ESC}|g" \
      -e "s|^User=mediauser$|User=${SERVICE_USER_ESC}|" \
      -e "s|^Group=mediauser$|Group=${SERVICE_GROUP_ESC}|" \
      "$1"
}

# --- crontab ---
expected_cron="$REPO_ROOT/ops/cron.d/media-stack.crontab"
if [[ -r "$expected_cron" ]]; then
  # Render the snapshot with the SAME substitutions ops/install.sh would
  # have applied (MEDIA_STACK_ROOT path swap) so a replicator whose
  # MEDIA_STACK_ROOT differs from the `$HOME/media-stack` placeholder
  # doesn't see permanent drift after a correct install.
  # Codex round-var #4.
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

# --- systemd units ---
#
# Host-specific units: some tracked units (e.g. realtek-fix.service) only
# apply when matching hardware is present.  Skip drift enforcement on hosts
# without the prerequisite — codex round-9 finding #6 caught the gap where
# a replicator following the documented "skip if the NIC is absent" rule
# still triggered drift on `ops/audit.sh`.
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
  # Existence check via list-unit-files — works without sudo even when
  # the underlying unit file is mode 640 (e.g. realtek-fix.service).
  # NB: `grep -q` would close stdin early, killing systemctl with SIGPIPE
  # under `set -o pipefail` and producing a spurious "not installed"
  # verdict for units that DO exist.  Read the full output instead and
  # discard via `>/dev/null` — same exit code, no broken pipe.
  if ! systemctl list-unit-files "$unit" 2>/dev/null | grep -E "^${unit}\\s" >/dev/null; then
    printf 'WARN: systemd unit %s not installed\n' "$unit"
    drift=1
    continue
  fi
  if ! live=$(systemctl cat "$unit" 2>/dev/null); then
    # Unit body is mode-640 (e.g. realtek-fix.service) — unreadable as the
    # normal user.  Retry the READ ALONE under sudo; do NOT run the whole
    # script as root (that would compare root's crontab and render paths
    # with $HOME=/root, producing false drift).  `sudo -n` so an unattended
    # caller (the hourly healthcheck cron) never blocks on a password prompt.
    # Only if passwordless sudo is unavailable do we fall back to a WARN —
    # drift could have slipped in unverified (codex round-5 finding #8), so
    # it stays exit 2, not a silent pass.
    if ! live=$(sudo -n systemctl cat "$unit" 2>/dev/null); then
      printf 'WARN: %s installed but unreadable (mode-640 + no passwordless sudo) — drift check skipped\n' "$unit"
      warn=1
      continue
    fi
  fi
  # Render the snapshot with substitutions (codex round-var #4), then
  # strip the live header + our own snapshot header so we compare unit
  # bodies only.
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

# --- logrotate ---
# Compare the rendered snapshot against the live /etc/logrotate.d/media-stack.
# render_snapshot's path substitution applies; its User/Group subs are no-ops
# here (the logrotate config has no `User=mediauser` lines).  The live file is
# root-owned mode-644, so it's readable without sudo.
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
