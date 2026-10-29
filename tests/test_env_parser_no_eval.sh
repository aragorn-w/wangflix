#!/bin/bash
# Regression: `lib/paths.sh` must parse .env without executing it.  Writes a synthetic
# .env with command-substitution payloads (`MEDIA_ROOT="$(touch <sentinel>)"`), sources the
# parser, and asserts the values are literal and no sentinel was created.
#
#   bash tests/test_env_parser_no_eval.sh    # exit 0 on success

set -uo pipefail

_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_repo="$(cd "$_here/.." && pwd)"

tmp=$(mktemp -d -t test-env-parser.XXXXXX)
trap 'rm -rf "$tmp"' EXIT

# Mirror the repo layout so paths.sh's auto-derive finds the fake .env.
mkdir -p "$tmp/lib"
cp "$_repo/lib/paths.sh" "$tmp/lib/paths.sh"

sentinel="$tmp/PARSER_EXECUTED_INJECTION"

# Several keys and quote styles, each creating a sentinel if eval'd.
cat > "$tmp/.env" <<EOF
MEDIA_ROOT=\$(touch $sentinel)
MEDIA_LAN_IP="\$(touch ${sentinel}.dq)"
CALIBRE_LIBRARY='\$(touch ${sentinel}.sq)'
EOF

# Subshell so the assignments don't leak.
(
  unset MEDIA_ROOT MEDIA_LAN_IP CALIBRE_LIBRARY
  . "$tmp/lib/paths.sh"
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

if [[ -e "$sentinel" || -e "${sentinel}.dq" || -e "${sentinel}.sq" ]]; then
  echo "FAIL: parser executed injected command (sentinel created)" >&2
  ls -la "${sentinel}"* 2>&1 >&2
  exit 1
fi

echo "PASS: lib/paths.sh parser is non-executing (codex round-10 #1 regression)"
exit 0
