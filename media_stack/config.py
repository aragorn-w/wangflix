"""Pipeline constants — codec preferences, keyword lists, regex patterns,
file conventions.  Importing this module is side-effect-free.

Lives here (not as scattered top-level constants in consolidate-subs.py
or normalize-audio.py) so:
  - tests can import them via the package without `importlib` shims
  - changes to a constant don't risk merge conflicts with logic edits
  - shell helpers that need (e.g.) the JAPANESE_KEYWORDS list can
    `python3 -m media_stack.cli print_keywords` instead of regex-grepping
    the live scripts
"""

from __future__ import annotations

import re


# Pipeline version — bumped to trigger a full re-flow when the global
# subtitle pipeline changes.  Files carrying a lower version tag
# (CONSOLIDATED_SUBS=v1) get re-processed; equal-version files are
# short-circuited via the state file's idempotency check.
PIPELINE_VERSION: int = 2


# ffprobe subtitle codec families.  Image-based subs (PGS, VOBSUB, etc.)
# can't be rendered by every player and require OCR to convert to text.
TEXT_CODECS: set[str] = {"subrip", "ass", "ssa", "webvtt", "mov_text"}
IMAGE_CODECS: set[str] = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}

# Subtitle language tags that this pipeline treats as English for
# scoring/selection purposes.  Distinct from media_stack.lang.ENG_AUDIO_LANGS
# (which includes `und` + empty because untagged audio defaults to
# English) — subtitle scoring is stricter: an `und` sub track is treated
# as unknown, not assumed English.
ENG_LANGS: set[str] = {"eng", "en", "english"}

# Sidecar subtitle file extensions we consider.
SIDECAR_EXTS: tuple[str, ...] = (".srt", ".ass", ".ssa", ".vtt")

# Sidecar language-suffix variants Bazarr and other tools write
# (e.g. `<basename>.en.srt`, `<basename>.en-cc.srt`).  Match these to
# the English family for scoring.
ENG_SIDECAR_SUFFIXES: tuple[str, ...] = (
    "en", "eng", "en-us", "en-gb", "en-cc", "en-sdh", "en-forced",
)


# Subtitle cleaning regex patterns.
ASS_OVERRIDE_RE = re.compile(r"\{\\[^}]*\}")
HTML_FONT_RE    = re.compile(r"</?font[^>]*>", re.IGNORECASE)
HTML_OTHER_RE   = re.compile(r"</?(?:span|p|div)[^>]*>", re.IGNORECASE)
LITERAL_NH_RE   = re.compile(r"\\h")
TRAIL_WS_RE     = re.compile(r"[ \t]+$", re.MULTILINE)
MULTI_BLANK_RE  = re.compile(r"\n{3,}")


# Audio language pref by filename keyword.
# Titles where the user prefers a SINGLE Japanese primary audio track and
# pruning of English dubs (anime purist preference).  Keep separate from
# DUAL_AUDIO_KEYWORDS, which is live-action foreign-original films where
# BOTH the original-language and English-dub tracks are wanted.
JAPANESE_KEYWORDS: set[str] = {
    "aggretsuko", "one piece", "naruto", "dragon ball", "attack on titan",
    "demon slayer", "jujutsu kaisen", "my hero academia", "fullmetal",
    "cowboy bebop", "death note", "mob psycho", "spy x family",
    "chainsaw man", "vinland saga", "bleach", "hunter x hunter",
    "neon genesis", "evangelion", "akira", "ghost in the shell",
    "pokemon", "digimon", "sailor moon", "inuyasha", "trigun",
    "samurai champloo", "steins gate", "code geass", "tokyo ghoul",
    "one punch man", "sword art online", "spirited away", "howl",
    "totoro", "mononoke", "ponyo", "kiki", "ghibli", "anime",
    "dragonball", "dbz", "boruto", "fairy tail", "rurouni kenshin",
    "erased", "violet evergarden", "your name", "weathering with you",
    "suzume", "doraemon", "lupin", "berserk", "parasyte", "initial d",
    "pop team epic", "may i ask for one final thing",
}

# Live-action foreign-original films where the user wants BOTH the
# foreign original-language track AND the English-dub kept (so they can
# pick at playback time, e.g. Godzilla Minus One).  Distinct from
# JAPANESE_KEYWORDS above which is anime — there, English dubs are
# pruned by preference.
DUAL_AUDIO_KEYWORDS: set[str] = {
    "godzilla minus one", "shin godzilla",
}

KOREAN_KEYWORDS: set[str] = {
    "parasite", "squid game", "oldboy", "train to busan", "snowpiercer",
    "kingdom", "minari", "the handmaiden", "memories of murder",
    "all of us are dead", "sweet home", "hellbound", "alive",
    "peninsula", "the host", "the wailing", "burning",
    "decision to leave", "broker", "past lives", "extraordinary attorney woo",
    "crash landing", "itaewon class", "vincenzo",
}


# Commentary / audio-description detection.  Matches the typical title
# patterns ffprobe stream tags use.
COMMENT_TITLE_RE = re.compile(
    r"\b(commentar(y|ies)|director'?s|isolated\s*(score|track|music)|"
    r"audio\s*description|descript(ive|ion)|described|narration|"
    r"karaoke|sing.?along|filmmaker)\b",
    re.IGNORECASE,
)


# Codec scoring — higher = preferred when picking which audio track to
# keep.  TrueHD/DTS/etc. take precedence over re-encoded AAC.
CODEC_PREF: dict[str, int] = {
    "truehd": 80, "dts": 70, "eac3": 60, "ac3": 50, "flac": 40,
    "opus": 35, "aac": 30, "mp3": 20, "vorbis": 15,
}


# Cover-art codecs that are sometimes embedded as a *video stream* (not
# a true Matroska Attachment) without the attached_pic disposition flag
# set.  When a real motion video stream coexists with one of these, web
# players sometimes bind to the still-image stream — black-screen-no-
# audio playback until the user seeks (which forces a re-pick).  Always
# strip these.
STILL_IMAGE_VIDEO_CODECS: set[str] = {
    "mjpeg", "png", "jpeg", "bmp", "gif", "webp", "tiff", "ppm", "pgm",
}


# Orphan-sweep patterns for the consolidate-subs sweep.  `.tmp.mkv`
# files always carry the writing PID; consolsub_* dirs may carry it.
TMP_MKV_RE       = re.compile(r"\.consol\.(\d+)\.tmp\.mkv$")
CONSOLSUB_DIR_RE = re.compile(r"^consolsub_(?:(\d+)_)?[A-Za-z0-9]+$")
SWEEP_MIN_AGE_S: int = 30 * 60  # PID-less consolsub_* dirs must be at least this old to be swept

# normalize-audio.py's `.normalize-tmp` workdir (normalize-audio.py:253-254).
# The DIRECTORY name carries no PID -- concurrent workers share one per media
# folder via mkdir(exist_ok=True) -- so liveness is decided per FILE, from the
# PID in `.<stem>.<pid>.pass2.mkv` / `.<stem>.<pid>.remux.mkv`.
NORMALIZE_TMP_DIR = ".normalize-tmp"
NORMALIZE_TMP_RE  = re.compile(r"\.(\d+)\.(?:pass2|remux)\.mkv$")
# The PID belongs to a normalize-audio.py worker, never to the sweep's caller.
NORMALIZE_SCRIPT  = "normalize-audio.py"
# Own floor, no less conservative than normalize-driver.sh's `find -mmin -60`.
NORMALIZE_TMP_MIN_AGE_S: int = 60 * 60
