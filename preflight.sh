#!/bin/bash
# preflight.sh — verify every host-side runtime dependency the media stack
# expects is installed and minimally functional. Run before any first-time
# setup or after a host upgrade.
#
# Codex round-3 finding #11 asked for this — Python packages are listed in
# requirements.txt but the harder dependencies (ffmpeg, mkvtoolnix, GPU
# stack, Docker, network helpers) were only mentioned in prose.
#
# Exit codes:
#   0  every required dependency present and the expected version probe
#       succeeded
#   1  one or more required dependencies missing/broken
#   2  optional helpers missing (non-fatal, e.g. nvidia-smi on a node with
#       no GPU); summary printed
#
# Usage:
#   bash preflight.sh
#   bash preflight.sh --verbose          # print version strings even on success
#   bash preflight.sh --strict           # treat optional misses as failure

set -uo pipefail

verbose=0
strict=0
for arg in "$@"; do
  case "$arg" in
    --verbose) verbose=1 ;;
    --strict)  strict=1 ;;
    *) printf 'preflight: unknown arg %q\n' "$arg" >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
fail=0
optfail=0

# ---------- helpers ----------

require_cmd() {
  # require_cmd <bin> <description> [-- <probe argv...>]
  #
  # 1) bin must exist on PATH
  # 2) if a `--` separator is followed by probe argv, the probe MUST
  #    exit 0 — a broken/incompatible binary would otherwise still
  #    pass preflight (codex round-8 #2).
  #
  # Argv-style probe (vs the old `eval "$probe"` string form, codex
  # round-8 #7).  No shell metacharacter expansion on caller-supplied
  # args; easier for agents to extend without escaping bugs.
  local cmd="$1" desc="$2"
  shift 2
  [[ "${1:-}" == "--" ]] && shift
  if ! command -v "$cmd" >/dev/null 2>&1; then
    printf 'MISSING: %s — %s\n' "$cmd" "$desc"
    fail=$((fail + 1))
    return
  fi
  if (( $# > 0 )); then
    local out rc
    out=$("$@" 2>&1 | head -1)
    rc=$?
    if [[ $rc -ne 0 ]]; then
      printf 'BROKEN:  %s — probe exit %d: %s\n' "$cmd" "$rc" "$out"
      fail=$((fail + 1))
      return
    fi
    if [[ $verbose -eq 1 ]]; then
      printf 'OK:      %s — %s\n' "$cmd" "$out"
      return
    fi
  fi
  printf 'OK:      %s\n' "$cmd"
}

optional_cmd() {
  # optional_cmd <bin> <description> [-- <probe argv...>] — same shape
  # as require_cmd; missing or broken is OPT (not FAIL).
  local cmd="$1" desc="$2"
  shift 2
  [[ "${1:-}" == "--" ]] && shift
  if ! command -v "$cmd" >/dev/null 2>&1; then
    printf 'OPT:     %s — %s (not installed)\n' "$cmd" "$desc"
    optfail=$((optfail + 1))
    return
  fi
  if (( $# > 0 )); then
    local out rc
    out=$("$@" 2>&1 | head -1)
    rc=$?
    if [[ $rc -ne 0 ]]; then
      printf 'OPT:     %s — probe exit %d: %s\n' "$cmd" "$rc" "$out"
      optfail=$((optfail + 1))
      return
    fi
    if [[ $verbose -eq 1 ]]; then
      printf 'OK:      %s — %s\n' "$cmd" "$out"
      return
    fi
  fi
  printf 'OK:      %s\n' "$cmd"
}

# ---------- required binaries ----------

require_cmd ffmpeg      "two-pass loudnorm + remux"             -- ffmpeg -version
require_cmd ffprobe     "metadata probe used everywhere"        -- ffprobe -version
require_cmd mkvmerge    "v2 pipeline final mux (matroska cues)" -- mkvmerge --version
require_cmd mkvpropedit "global-tag stamping (NORMALIZED_AUDIO, CONSOLIDATED_SUBS)" -- mkvpropedit --version
require_cmd mkvextract  "tag XML extraction in media_stack/tags.py" -- mkvextract --version
require_cmd python3     "all host-side scripts"                 -- python3 --version
require_cmd docker      "container runtime"                     -- docker --version
# inotifywait --help exits 1 (typical of inotify-tools); use dpkg-query
# directly (no pipe — argv-style probe forbids shell metacharacters).
require_cmd inotifywait "consolidate-watch.service file event source" \
                                                                -- dpkg-query -W -f='${Version}' inotify-tools
require_cmd curl        "ad-hoc API probes in shell scripts"    -- curl --version
require_cmd flock       "cron-driver mutual exclusion"

# ---------- python modules ----------

if command -v python3 >/dev/null 2>&1; then
  for mod in requests pysubs2 srt; do
    if ! python3 -c "import ${mod}" 2>/dev/null; then
      printf 'MISSING py: %s (pip install -r requirements.txt)\n' "$mod"
      fail=$((fail + 1))
    else
      [[ $verbose -eq 1 ]] && printf 'OK py:   %s\n' "$mod"
    fi
  done
  # pytest is only required for `tests/`. Note as optional.
  if ! python3 -c "import pytest" 2>/dev/null; then
    printf 'OPT py:  pytest (run unit tests under tests/)\n'
    optfail=$((optfail + 1))
  else
    [[ $verbose -eq 1 ]] && printf 'OK py:   pytest\n'
  fi
fi

# ---------- python helpers we call as CLIs ----------

require_cmd subliminal "sub fallback chain in media_stack/subtitles.py" -- subliminal --version
require_cmd ffsubsync  "consolidate-subs.py timing sync"     -- ffsubsync --version

# ---------- GPU stack (optional on non-GPU hosts) ----------

optional_cmd nvidia-smi "NVIDIA driver + GPU presence (used by Jellyfin HW transcode)" \
                                                             -- nvidia-smi --query-gpu=name --format=csv,noheader

# ---------- docker compose health ----------

if command -v docker >/dev/null 2>&1; then
  if docker compose -f "$REPO_ROOT/docker-compose.yml" config >/dev/null 2>&1; then
    printf 'OK:      docker-compose.yml parses\n'
  else
    printf 'MISSING: docker-compose.yml fails to parse — fix before bringing the stack up\n'
    fail=$((fail + 1))
  fi
fi

# ---------- repo paths ----------

for f in consolidate-subs.py normalize-audio.py normalize-driver.sh media_lang.py; do
  if [[ ! -r "$REPO_ROOT/$f" ]]; then
    printf 'MISSING repo: %s (expected in %s)\n' "$f" "$REPO_ROOT"
    fail=$((fail + 1))
  fi
done

# ---------- summary ----------

printf '\n'
if [[ $fail -gt 0 ]]; then
  printf 'PREFLIGHT FAILED: %d required dependency miss(es). %d optional miss(es).\n' "$fail" "$optfail"
  exit 1
fi
if [[ $optfail -gt 0 ]]; then
  printf 'PREFLIGHT OK with %d optional miss(es).\n' "$optfail"
  [[ $strict -eq 1 ]] && exit 1
  exit 2
fi
printf 'PREFLIGHT OK: every required + optional dependency present.\n'
exit 0
