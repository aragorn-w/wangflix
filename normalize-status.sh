#!/usr/bin/env bash
# normalize-status.sh — one-shot status of the audio-normalization workload.
set -uo pipefail

_here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/paths.sh
. "$_here/lib/paths.sh"

print_section() { printf "\n=== %s ===\n" "$1"; }

print_section "Background processes"
pgrep -af "normalize-audio.py|normalize-driver.sh|normalize-loop.sh" || echo "(none)"

print_section "Library coverage (NORMALIZED_AUDIO=v1)"
# Read the authoritative coverage from normalize-driver.sh, which probes
# the whole library every 15 min via cron and logs `coverage: X/Y`.
# Re-running that ffprobe scan here would take minutes (it's why the old
# version of this script timed out). The .done sentinel means the driver
# reached full coverage and idled.
if [ -f "$VAR_STATE/normalize-driver.done" ]; then
    echo "  COMPLETE — normalize-driver.done present (driver reached 100% and idled)"
fi
cov=$(grep -aE 'coverage: [0-9]+/[0-9]+' "$VAR_LOG/normalize-driver.log" 2>/dev/null | tail -1)
if [ -n "$cov" ]; then
    echo "  latest driver probe: ${cov#*coverage: }"
else
    echo "  (no coverage line in the current driver log — it probes every 15 min)"
fi

print_section "Loudness report (on-demand, loudness-report.py)"
out="$VAR_STATE/movies-loudness.json"
err="$VAR_LOG/movies-loudness.err"
if [ -s "$err" ]; then
    echo "[errors]"
    tail -5 "$err"
fi
if [ -s "$out" ]; then
    echo "  $(stat -c%s "$out") bytes"
    python3 -c "import json; d=json.load(open('$out')); ok=sum(1 for r in d if r.get('status')=='OK'); print(f'  parsed: {len(d)} entries, {ok} OK')"
else
    echo "  (none yet — generate with: python3 loudness-report.py ...)"
fi

print_section "Recent normalize-audio.log lines"
tail -8 "$VAR_LOG/normalize-audio.log" 2>/dev/null || echo "(none)"

print_section "Disk free"
df -h "$MEDIA_ROOT" / 2>/dev/null | grep -v "^Filesystem"

print_section "Load avg / CPU"
cat /proc/loadavg
