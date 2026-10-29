#!/bin/bash
# inotify watcher (close_write / moved_to): runs the pipeline on new videos, and on the sibling
# video when a sidecar .srt/.ass/.vtt arrives.
set -u
_here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/paths.sh
. "$_here/lib/paths.sh"

ROOT="$MEDIA_ROOT"
SCRIPT="$MEDIA_STACK_ROOT/consolidate-subs.py"
LOG="$VAR_LOG/consolidate-watch.log"
COOLDOWN=15
STATE_DEBOUNCE_DIR="$VAR_RUN/consolidate-watch-debounce"
mkdir -p "$STATE_DEBOUNCE_DIR"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

trigger() {
    local video="$1" reason="$2"
    [ -f "$video" ] || return 0
    local key
    key=$(echo -n "$video" | md5sum | awk '{print $1}')
    local debfile="$STATE_DEBOUNCE_DIR/$key"
    if [ -f "$debfile" ]; then
        local age=$(( $(date +%s) - $(stat -c %Y "$debfile" 2>/dev/null || echo 0) ))
        [ "$age" -lt 60 ] && return 0
    fi
    touch "$debfile"
    (
        sleep "$COOLDOWN"
        [ -f "$video" ] || exit 0
        local s1 s2
        s1=$(stat -c %s "$video" 2>/dev/null || echo 0)
        sleep 5
        s2=$(stat -c %s "$video" 2>/dev/null || echo 0)
        [ "$s1" != "$s2" ] && { log "still writing, skip: $video"; exit 0; }
        local out
        out=$(python3 "$SCRIPT" "$video" 2>&1 | tail -1)
        log "[$reason] $out"
    ) &
}

log "watcher start: ROOT=$ROOT"

# --exclude keeps the watcher out of $ROOT/.dupe-recycle/ (re-normalizing files about to be
# purged cascades); the case-guard below is belt-and-suspenders.
inotifywait -m -r -e close_write,moved_to --exclude '/\.dupe-recycle(/|$)' \
    --format '%w%f|%e' "$ROOT" 2>>"$LOG" |
while IFS='|' read -r path event; do
    case "$path" in
        *.consol.*.tmp.mkv|*.tmp.mkv|*.partial|*.consolidate-subs.state.*) continue ;;
        */.dupe-recycle/*) continue ;;
        */.normalize-tmp/*) continue ;;
    esac
    case "${path,,}" in
        *.mkv|*.mp4)
            log "video event: $path ($event)"
            trigger "$path" "video-import"
            ;;
        *.srt|*.ass|*.ssa|*.vtt)
            dir=$(dirname "$path")
            base=$(basename "$path")
            # "Show.S01E01.en.cc.srt" -> "Show.S01E01": peel up to 3 trailing lang/forced/sdh/
            # generated tags ("generated" = sub-fallback.py's Whisper sidecar). A video named exactly
            # like the stem at a step wins and stops the peeling ("The.Generated.mkv" is never cut).
            # A video takes the sidecar only under media_stack.subtitles.find_sidecar_subs's rule: no
            # tags left after its stem, or an English one (has_en: any tag starting with "en") among
            # them.
            stem="${base%.*}"
            found=""
            has_en=0
            for step in 0 1 2 3; do
                if [ "$step" -eq 0 ] || [ "$has_en" -eq 1 ]; then
                    for ext in mkv mp4; do
                        if [ -f "$dir/$stem.$ext" ]; then
                            found="$dir/$stem.$ext"
                            break 2
                        fi
                    done
                fi
                [ "$step" -eq 3 ] && break
                last="${stem##*.}"
                ll="${last,,}"
                if [[ "$ll" == en* || "$ll" =~ ^(cc|sdh|forced|hi|generated)$ ]]; then
                    if [[ "$ll" == en* ]]; then
                        has_en=1
                    fi
                    stem="${stem%.*}"
                else
                    break
                fi
            done
            if [ -n "$found" ]; then
                log "sidecar event: $path → $found ($event)"
                trigger "$found" "sidecar-arrival"
            fi
            ;;
    esac
done
