#!/bin/bash
# Render the cron, systemd and logrotate snapshots with this host's values, then install them.
# Placeholders: `$HOME/media-stack` -> MEDIA_STACK_ROOT (from .env via lib/paths.sh) in cron and
# logrotate; `User=`/`Group=mediauser` -> the invoking user in systemd. Rendered files go to a temp
# dir for inspection first.
# Usage: install.sh [--apply]   (default: render + dry-run preview). Re-runnable: identical output
# for identical inputs.

set -euo pipefail
# `-e` is mandatory: --apply must not continue past a failed `sudo systemctl enable --now`.

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

printf 'Rendering with MEDIA_STACK_ROOT=%s SERVICE_USER=%s SERVICE_GROUP=%s\n' \
    "$MEDIA_STACK_ROOT" "$SERVICE_USER" "$SERVICE_GROUP"

# Escape `\`, `&` and the `|` delimiter in sed replacement strings (must match ops/audit.sh).
sed_repl_escape() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }
MEDIA_STACK_ROOT_ESC=$(sed_repl_escape "$MEDIA_STACK_ROOT")
HOME_ESC=$(sed_repl_escape "$HOME")
SERVICE_USER_ESC=$(sed_repl_escape "$SERVICE_USER")
SERVICE_GROUP_ESC=$(sed_repl_escape "$SERVICE_GROUP")

# Private per-run temp dir (not a predictable /tmp path); its rendered files are listed below.
INSTALL_TMP=$(mktemp -d -t media-stack-install.XXXXXX)
CRON_OUT="$INSTALL_TMP/media-stack.crontab.rendered"
# `$HOME/media-stack` first (more specific), then `$HOME`.
sed -e "s|\$HOME/media-stack|${MEDIA_STACK_ROOT_ESC}|g" \
    -e "s|\$HOME|${HOME_ESC}|g" \
    "$REPO_ROOT/ops/cron.d/media-stack.crontab" > "$CRON_OUT"
printf 'Rendered crontab:   %s\n' "$CRON_OUT"

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
  printf 'Rendered unit:      %s\n' "$out"
done

# Only the path placeholder needs rendering (`su root root` is literal).
LOGROTATE_OUT="$INSTALL_TMP/logrotate-media-stack"
sed -e "s|\$HOME/media-stack|${MEDIA_STACK_ROOT_ESC}|g" \
    "$REPO_ROOT/ops/logrotate.d/media-stack" > "$LOGROTATE_OUT"
printf 'Rendered logrotate: %s\n' "$LOGROTATE_OUT"

if [[ $APPLY -ne 1 ]]; then
  printf 'DRY RUN, nothing installed. --apply installs the crontab, the systemd units (host-specific\n'
  printf 'ones such as realtek-fix.service are skipped unless INCLUDE_HOST_SPECIFIC=1) and the\n'
  printf 'logrotate config at /etc/logrotate.d/media-stack.\n'
  exit 0
fi

printf '\n=== installing crontab ===\n'
crontab "$CRON_OUT"

printf '=== installing systemd units (requires sudo) ===\n'
# Units that only make sense with specific hardware (the `enp6s0f1` Realtek NIC); keep in
# sync with ops/audit.sh is_applicable().
HOST_SPECIFIC_UNITS=("realtek-fix.service")
is_host_specific() {
  local u="$1" hs
  for hs in "${HOST_SPECIFIC_UNITS[@]}"; do
    [[ "$u" == "$hs" ]] && return 0
  done
  return 1
}

# Host-specific units are skipped at INSTALL time, not just enable, so hosts without the
# hardware get no trace of them.  INCLUDE_HOST_SPECIFIC=1 installs them anyway.
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
# -d is debug/dry-run (no rotation); logrotate refuses non-root-owned configs.
if sudo logrotate -d /etc/logrotate.d/media-stack >/dev/null 2>&1; then
  printf '  ✓ logrotate config validates\n'
else
  printf '  WARN: logrotate -d flagged an issue; inspect /etc/logrotate.d/media-stack\n'
fi

printf '\nDone.  Verify with: bash %s/ops/audit.sh\n' "$MEDIA_STACK_ROOT"
