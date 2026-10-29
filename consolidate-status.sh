#!/bin/bash
# Quick status snapshot of subtitle-consolidation activity.
_here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/paths.sh
. "$_here/lib/paths.sh"

echo "=== inotify watcher ==="
systemctl is-active consolidate-watch.service 2>&1
echo
echo "=== nightly cron ==="
crontab -l 2>/dev/null | grep -i consolidate-subs | head -2
echo
echo "=== state file ==="
_sf="$VAR_STATE/consolidate-subs.state.json"
if [ -s "$_sf" ]; then
    # single-line JSON, so `wc -l` reads 0: report the entry count + bytes
    printf '  %s entries, %s bytes\n' \
        "$(jq 'length' "$_sf" 2>/dev/null || echo '?')" \
        "$(wc -c < "$_sf" 2>/dev/null)"
else
    echo "  (empty or absent)"
fi
echo
echo "=== bulk runner activity ==="
N=$(pgrep -fc 'consolidate-subs.py --scan' 2>/dev/null | head -1)
N=${N:-0}
if [ "$N" -gt 0 ]; then
    pgrep -af 'consolidate-subs.py --scan' | head -1 | awk '{etime="?";"ps -o etime= -p "$1 | getline etime; printf "  parent PID=%s etime=%s\n",$1,etime}'
    pgrep -fc 'mkvmerge.*\.consol\.' | awk '{print "  active mkvmerge muxes: "$1}'
    pgrep -fc ffsubsync | awk '{print "  active ffsubsync runs: "$1}'
    echo "  recent FIXED entries (last hour):"
    awk -v cut="$(date -d '-1 hour' '+%Y-%m-%d %H:%M')" '$0 >= cut && /FIXED:/' "$VAR_LOG/consolidate-subs.log" | tail -5 | sed 's/^/    /'
else
    echo "no bulk runners active"
fi
echo
echo "=== last 10 log lines ==="
tail -10 "$VAR_LOG/consolidate-subs.log" 2>/dev/null
echo
echo "=== Bazarr badges (missing subs) ==="
BAZAPI=$(PYTHONPATH="$_here" python3 -c "from media_stack.clients.bazarr import apikey_from_container; print(apikey_from_container())" 2>/dev/null)
if [ -n "$BAZAPI" ]; then
    # Header goes in a 0600 curl config so the key is not in curl's argv (/proc/<pid>/cmdline).
    _curl_cfg=$(mktemp -t consolidate-status.curlrc.XXXXXX)
    chmod 600 "$_curl_cfg"
    trap 'rm -f "$_curl_cfg"' EXIT
    printf 'header = "X-API-KEY: %s"\n' "$BAZAPI" > "$_curl_cfg"
    curl -s -K "$_curl_cfg" "$BAZARR_URL/api/badges"
fi
echo
