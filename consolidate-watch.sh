#!/bin/bash
# Watch the media library for new/imported video files AND new sidecar subtitles.
# Triggered on close_write (final write of a copy) and moved_to (atomic mv from staging).
# When a video file shows up: run the consolidation pipeline.
# When a sidecar .srt/.ass/.vtt shows up next to a video: re-run the pipeline on that video.
set -u
_here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/paths.sh
. "$_here/lib/paths.sh"

ROOT="$MEDIA_ROOT"
SCRIPT="$MEDIA_STACK_ROOT/consolidate-subs.py"
LOG="$VAR_LOG/consolidate-watch.log"
COOLDOWN=15
# Stack-local debounce state (codex round-10 #11: was /tmp/consolidate-
# watch-debounce, outside the documented var/ convention + not covered
# by audit/cleanup tooling).
STATE_DEBOUNCE_DIR="$VAR_RUN/consolidate-watch-debounce"
mkdir -p "$STATE_DEBOUNCE_DIR"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

trigger() {
    local video="$1" reason="$2"
    [ -f "$video" ] || return 0
    local key
    key=$(echo -n "$video" | md5sum | awk '{print $1}')
    local debfile="$STATE_DEBOUNCE_DIR/$key"
    # Debounce: skip if we triggered within the last 60s
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

# --exclude keeps the watcher out of the de-dup recycle bin: movie-dedupe.py
# moves duplicate videos into $ROOT/.dupe-recycle/, and without this the
# recursive watcher would fire close_write/moved_to on those files and
# re-normalize content that's about to be purged (the cascade hit on
# 2026-06-01).  The case-guard below is belt-and-suspenders.
inotifywait -m -r -e close_write,moved_to --exclude '/\.dupe-recycle(/|$)' \
    --format '%w%f|%e' "$ROOT" 2>>"$LOG" |
while IFS='|' read -r path event; do
    case "$path" in
        *.consol.*.tmp.mkv|*.tmp.mkv|*.partial|*.consolidate-subs.state.*) continue ;;
        */.dupe-recycle/*) continue ;;
    esac
    case "${path,,}" in
        *.mkv|*.mp4)
            log "video event: $path ($event)"
            trigger "$path" "video-import"
            ;;
        *.srt|*.ass|*.ssa|*.vtt)
            # Find sibling video with same base name
            dir=$(dirname "$path")
            base=$(basename "$path")
            # Strip lang/forced/sdh tags and ext: "Show.S01E01.en.cc.srt" -> "Show.S01E01"
            stem="${base%.*}"
            # peel off up to 3 dotted suffixes that look like language/forced/sdh tags
            for _ in 1 2 3; do
                last="${stem##*.}"
                ll="${last,,}"
                if [[ "$ll" =~ ^(en|eng|en-us|en-gb|en-cc|en-sdh|en-forced|cc|sdh|forced|hi)$ ]]; then
                    stem="${stem%.*}"
                else
                    break
                fi
            done
            for ext in mkv mp4; do
                vid="$dir/$stem.$ext"
                if [ -f "$vid" ]; then
                    log "sidecar event: $path → $vid ($event)"
                    trigger "$vid" "sidecar-arrival"
                    break
                fi
            done
            ;;
    esac
done
