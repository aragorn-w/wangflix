#!/bin/bash
# tests/test_env_parser_no_eval.sh — regression for codex round-10 #1.
#
# `lib/paths.sh` is documented as a NON-EXECUTING .env parser.  The
# round-10 audit caught that the assignment was using `eval "$_k=$_v"`,
# which would execute any command substitution inside the value.  This
# test writes a synthetic .env with a command-substitution payload
# (`MEDIA_ROOT="$(touch <sentinel>)"`), sources the parser, and asserts
# the sentinel was NOT created.
#
# Run from anywhere:
#   bash tests/test_env_parser_no_eval.sh
# Exits 0 on success, non-zero on regression.

set -uo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_repo="$(cd "$_here/.." && pwd)"

tmp=$(mktemp -d -t test-env-parser.XXXXXX)
trap 'rm -rf "$tmp"' EXIT

# Mirror the repo's layout so paths.sh's auto-derive finds our fake .env.
mkdir -p "$tmp/lib"
cp "$_repo/lib/paths.sh" "$tmp/lib/paths.sh"

sentinel="$tmp/PARSER_EXECUTED_INJECTION"

# Construct a .env that, IF the parser eval'd values, would create the
# sentinel.  We exercise multiple keys + quote styles to cover the
# different code paths.
cat > "$tmp/.env" <<EOF
MEDIA_ROOT=\$(touch $sentinel)
MEDIA_LAN_IP="\$(touch ${sentinel}.dq)"
CALIBRE_LIBRARY='\$(touch ${sentinel}.sq)'
EOF

# Source the parser.  Use a subshell so the assignments don't leak.
(
  unset MEDIA_ROOT MEDIA_LAN_IP CALIBRE_LIBRARY
  . "$tmp/lib/paths.sh"
  # Verify values were captured as LITERAL text (not executed).
  if [[ "$MEDIA_ROOT" != "\$(touch $sentinel)" ]]; then
    echo "FAIL: MEDIA_ROOT is not literal: $MEDIA_ROOT" >&2
    exit 1
  fi
  if [[ "$MEDIA_LAN_IP" != "\$(touch ${sentinel}.dq)" ]]; then
    echo "FAIL: MEDIA_LAN_IP is not literal: $MEDIA_LAN_IP" >&2
    exit 1
  fi
  if [[ "$CALIBRE_LIBRARY" != "\$(touch ${sentinel}.sq)" ]]; then
    echo "FAIL: CALIBRE_LIBRARY is not literal: $CALIBRE_LIBRARY" >&2
    exit 1
  fi
) || exit 1

# The sentinel files MUST NOT exist.  If any of them do, the parser
# executed the command substitution.
if [[ -e "$sentinel" || -e "${sentinel}.dq" || -e "${sentinel}.sq" ]]; then
  echo "FAIL: parser executed injected command (sentinel created)" >&2
  ls -la "${sentinel}"* 2>&1 >&2
  exit 1
fi

echo "PASS: lib/paths.sh parser is non-executing (codex round-10 #1 regression)"
exit 0
