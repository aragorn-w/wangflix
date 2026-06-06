#!/usr/bin/env bash
# normalize-loop.sh <library-root>
#
# Drive normalize-audio.py over a library until convergence:
#   1. --scan: process every file, skip already-tagged.
#   2. Re-scan and count files lacking NORMALIZED_AUDIO=v1.
#   3. Loop until count is 0 OR loop count exceeds MAX (in which case
#      remaining stragglers are likely permanent failures, surfaced for
#      manual review).
#
# Logs to $HOME/media-stack/normalize-loop.<basename>.log.

set -uo pipefail

_here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/paths.sh
. "$_here/lib/paths.sh"

ROOT="${1:?usage: normalize-loop.sh <library-root>}"
JOBS="${JOBS:-2}"
MAX="${MAX_ITERS:-5}"
NORM="$MEDIA_STACK_ROOT/normalize-audio.py"

base="$(basename "$ROOT")"
log="$VAR_LOG/normalize-loop.${base// /_}.log"
echo "$(date -Is) START root=$ROOT jobs=$JOBS max=$MAX" >> "$log"

count_untagged() {
  local total tagged
  # Walk all videos, ffprobe each, count how many lack NORMALIZED_AUDIO=v1.
  # Faster than re-running normalize-audio --scan with --dry-run.
  python3 - "$ROOT" <<'PY'
import json, os, subprocess, sys
from pathlib import Path
root = Path(sys.argv[1])
exts = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm")
tagged = total = 0
for p in root.rglob("*"):
    if not p.is_file() or p.suffix.lower() not in exts:
        continue
    if any(part == ".normalize-tmp" for part in p.parts):
        continue
    total += 1
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags",
             "-of", "json", str(p)],
            capture_output=True, text=True, timeout=30,
        )
        tags = (json.loads(r.stdout).get("format") or {}).get("tags") or {}
        if tags.get("NORMALIZED_AUDIO") == "v1":
            tagged += 1
    except Exception:
        pass
print(f"{tagged} {total}")
PY
}

iter=0
while [ "$iter" -lt "$MAX" ]; do
  iter=$((iter+1))
  echo "$(date -Is) iter=$iter scanning..." | tee -a "$log"
  "$NORM" --scan "$ROOT" --jobs "$JOBS" 2>&1 | tee -a "$log" | tail -10
  read tagged total < <(count_untagged)
  echo "$(date -Is) iter=$iter coverage=$tagged/$total" | tee -a "$log"
  if [ "$tagged" = "$total" ] && [ "$total" != "0" ]; then
    echo "$(date -Is) DONE — full coverage." | tee -a "$log"
    exit 0
  fi
done

echo "$(date -Is) MAX_ITERS=$MAX exhausted; $((total - tagged)) files still untagged" | tee -a "$log"
exit 2
