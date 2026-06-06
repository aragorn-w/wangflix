#!/bin/bash
# ops/install.sh — render the cron + systemd snapshots with this host's
# MEDIA_STACK_ROOT + system-user values + install them.
#
# Codex round (post host-path refactor) flagged that the cron and systemd
# snapshots checked in under ops/ still hardcoded mediahost-specific values
# (`$HOME/media-stack`, `User=mediauser`, `Group=mediauser`).  Documentation
# said replicators could install them as-is, but they'd point at the wrong
# checkout on any other host.
#
# This installer does the substitution at install time:
#   - cron snapshot's bare absolute paths get prefixed with the actual
#     MEDIA_STACK_ROOT (loaded from .env via lib/paths.sh)
#   - systemd unit's User/Group get replaced with the invoking user
#   - logrotate config's path placeholder gets the actual MEDIA_STACK_ROOT
#     (then installed to /etc/logrotate.d/media-stack)
#   - the resulting files are written to /tmp first for inspection,
#     then offered for install with sudo
#
# Usage:
#   bash ops/install.sh              # render + dry-run (preview only)
#   bash ops/install.sh --apply      # render + actually install
#
# Re-runnable: produces identical output for the same inputs.

set -euo pipefail
# `-e` is mandatory: codex round-5 #1 caught that --apply would silently
# continue past a failed `sudo systemctl enable --now`, leaving the repo
# snapshot and live system out of sync without surfacing the error.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../lib/paths.sh
. "$REPO_ROOT/lib/paths.sh"

APPLY=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    *) printf 'install: unknown arg %q\n' "$arg" >&2; exit 2 ;;
  esac
done

SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE_GROUP="${SERVICE_GROUP:-$(id -gn)}"

printf 'Rendering with:\n'
printf '  MEDIA_STACK_ROOT = %s\n' "$MEDIA_STACK_ROOT"
printf '  SERVICE_USER     = %s\n' "$SERVICE_USER"
printf '  SERVICE_GROUP    = %s\n' "$SERVICE_GROUP"
printf '\n'

# Escape values before they enter sed replacement strings.  The
# replacement side of s|...|VALUE|g treats `\`, `&`, and the `|`
# delimiter specially, so an install path or username containing any of
# them would corrupt the rendered cron/unit (codex review).  Literal-ize
# those three characters.
sed_repl_escape() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }
MEDIA_STACK_ROOT_ESC=$(sed_repl_escape "$MEDIA_STACK_ROOT")
HOME_ESC=$(sed_repl_escape "$HOME")
SERVICE_USER_ESC=$(sed_repl_escape "$SERVICE_USER")
SERVICE_GROUP_ESC=$(sed_repl_escape "$SERVICE_GROUP")

# ---------- crontab ----------
# The snapshot uses absolute paths like $HOME/media-stack/X.sh.  Substitute
# the configured MEDIA_STACK_ROOT.  Use a safe sed delimiter so /-laden paths
# don't trip the s/// command.
#
# Per-run private temp dir (codex round-10 #12 — was predictable
# /tmp/media-stack.* paths which would race or collide on multi-user
# hosts).  Use mktemp -d so each invocation gets a fresh dir; the path
# is printed so operators can inspect before --apply.
INSTALL_TMP=$(mktemp -d -t media-stack-install.XXXXXX)
printf 'Rendering into: %s\n\n' "$INSTALL_TMP"
CRON_OUT="$INSTALL_TMP/media-stack.crontab.rendered"
# Two substitution passes:
#   1. `$HOME/media-stack` → MEDIA_STACK_ROOT (placeholder for this stack's
#      install path; substituted FIRST because it's the more specific match)
#   2. `$HOME` → the operator's home dir (covers other home-rooted paths
#      that aren't under the stack, e.g. `$HOME/nightly-upgrade.sh`)
# The leading `\$` keeps bash from expanding the dollar before sed sees it
# — the snapshot stores the placeholder as the literal `$HOME` string.
sed -e "s|\$HOME/media-stack|${MEDIA_STACK_ROOT_ESC}|g" \
    -e "s|\$HOME|${HOME_ESC}|g" \
    "$REPO_ROOT/ops/cron.d/media-stack.crontab" > "$CRON_OUT"
printf '=== rendered crontab (%s) ===\n' "$CRON_OUT"
head -20 "$CRON_OUT"
printf '...\n\n'

# ---------- systemd units ----------
SYSTEMD_DIR="$INSTALL_TMP/systemd"
mkdir -p "$SYSTEMD_DIR"
for unit in "$REPO_ROOT"/ops/systemd/*.service; do
  unit_name="$(basename "$unit")"
  out="$SYSTEMD_DIR/$unit_name"
  sed -e "s|\$HOME/media-stack|${MEDIA_STACK_ROOT_ESC}|g" \
      -e "s|\$HOME|${HOME_ESC}|g" \
      -e "s|^User=mediauser$|User=${SERVICE_USER_ESC}|" \
      -e "s|^Group=mediauser$|Group=${SERVICE_GROUP_ESC}|" \
      "$unit" > "$out"
  printf '=== rendered %s (%s) ===\n' "$unit_name" "$out"
  head -20 "$out"
  printf '\n'
done

# ---------- logrotate ----------
# Only the path placeholder needs rendering — `su root root` is literal.
LOGROTATE_OUT="$INSTALL_TMP/logrotate-media-stack"
sed -e "s|\$HOME/media-stack|${MEDIA_STACK_ROOT_ESC}|g" \
    "$REPO_ROOT/ops/logrotate.d/media-stack" > "$LOGROTATE_OUT"
printf '=== rendered logrotate (%s) ===\n' "$LOGROTATE_OUT"
head -20 "$LOGROTATE_OUT"
printf '\n'

if [[ $APPLY -ne 1 ]]; then
  printf '\nDRY RUN — nothing installed.  Re-run with --apply to:\n'
  printf '  1. crontab %s\n' "$CRON_OUT"
  printf '  2. sudo install -m 644 <NON-host-specific>.service files from %s\n' "$SYSTEMD_DIR"
  printf '       (host-specific units like realtek-fix.service are SKIPPED\n'
  printf '        at install time by default; override with INCLUDE_HOST_SPECIFIC=1)\n'
  printf '  3. sudo systemctl daemon-reload && enable+start each non-host-specific unit\n'
  printf '  4. sudo install -m 644 %s /etc/logrotate.d/media-stack\n' "$LOGROTATE_OUT"
  exit 0
fi

# ---------- apply ----------
printf '\n=== installing crontab ===\n'
crontab "$CRON_OUT"

printf '=== installing systemd units (requires sudo) ===\n'
# Host-specific units that should NOT be enabled automatically — the
# operator must opt in because the unit only makes sense given specific
# hardware (e.g. the `enp6s0f1` Realtek NIC).  Matches the
# ops/audit.sh is_applicable() list.
HOST_SPECIFIC_UNITS=("realtek-fix.service")
is_host_specific() {
  local u="$1" hs
  for hs in "${HOST_SPECIFIC_UNITS[@]}"; do
    [[ "$u" == "$hs" ]] && return 0
  done
  return 1
}

# Codex round-debloat #4: docs (README + ops/AGENTS.md) tell
# replicators to skip host-specific units entirely.  Previously this
# loop installed every unit (including realtek-fix.service) and only
# skipped ENABLE; the doc/behavior split confused replicators.  Now
# host-specific units are skipped at INSTALL time too — leaving zero
# trace on hosts without the matching hardware.  Override with
# `INCLUDE_HOST_SPECIFIC=1` if you actually want them installed (e.g.
# to render the unit for inspection before manual enable).
for unit in "$SYSTEMD_DIR"/*.service; do
  unit_name="$(basename "$unit")"
  if is_host_specific "$unit_name" && [[ "${INCLUDE_HOST_SPECIFIC:-0}" != "1" ]]; then
    printf '  SKIP install: %s (host-specific; set INCLUDE_HOST_SPECIFIC=1 to override)\n' "$unit_name"
    continue
  fi
  sudo install -m 644 "$unit" "/etc/systemd/system/$unit_name"
  printf '  installed: %s\n' "$unit_name"
done
sudo systemctl daemon-reload

for unit in "$SYSTEMD_DIR"/*.service; do
  unit_name="$(basename "$unit")"
  if is_host_specific "$unit_name"; then
    printf '  SKIP enable: %s (host-specific; install + enable manually if applicable)\n' "$unit_name"
    continue
  fi
  printf '  enable+start: %s\n' "$unit_name"
  sudo systemctl enable --now "$unit_name"
done

printf '\n=== installing logrotate config (requires sudo) ===\n'
sudo install -m 644 "$LOGROTATE_OUT" /etc/logrotate.d/media-stack
printf '  installed: /etc/logrotate.d/media-stack\n'
# Validate the installed config (logrotate refuses non-root-owned configs;
# install -m 644 as root satisfies that).  -d is debug/dry-run — no rotation.
if sudo logrotate -d /etc/logrotate.d/media-stack >/dev/null 2>&1; then
  printf '  ✓ logrotate config validates\n'
else
  printf '  WARN: logrotate -d flagged an issue; inspect /etc/logrotate.d/media-stack\n'
fi

printf '\nDone.  Verify with: bash %s/ops/audit.sh\n' "$MEDIA_STACK_ROOT"
