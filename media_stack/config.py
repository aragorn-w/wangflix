"""Pipeline constants. Side-effect-free on import."""

from __future__ import annotations

import re


# Bump to re-flow everything: files tagged with a lower version are reprocessed.
PIPELINE_VERSION: int = 2


TEXT_CODECS: set[str] = {"subrip", "ass", "ssa", "webvtt", "mov_text"}
# SubStation family: extracted as a native .ass so the content gate sees the raw bytes.
ASS_CODECS: frozenset[str] = frozenset({"ass", "ssa"})
IMAGE_CODECS: set[str] = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}

# Subtitle tags treated as English. Stricter than lang.ENG_AUDIO_LANGS: an `und` sub is unknown.
ENG_LANGS: set[str] = {"eng", "en", "english"}

SIDECAR_EXTS: tuple[str, ...] = (".srt", ".ass", ".ssa", ".vtt")

# Suffixes Bazarr etc. write (`<basename>.en.srt`, `.en-cc.srt`), scored as English.
ENG_SIDECAR_SUFFIXES: tuple[str, ...] = (
    "en", "eng", "en-us", "en-gb", "en-cc", "en-sdh", "en-forced",
)

# A subliminal download is rejected as a partial subtitle when its last cue STARTS before
# this fraction of the video's duration (margin for long silent endings/credits).
SUBLIMINAL_MIN_COVERAGE: float = 0.70

# A processed output may run at most max(OUTPUT_DURATION_SLACK_S, OUTPUT_DURATION_SLACK_FRAC *
# source) shorter than its source before it may replace it (probe.duration_shortfall).
OUTPUT_DURATION_SLACK_S: float = 2.0
OUTPUT_DURATION_SLACK_FRAC: float = 0.01


ASS_OVERRIDE_RE = re.compile(r"\{\\[^}]*\}")
HTML_FONT_RE    = re.compile(r"</?font[^>]*>", re.IGNORECASE)
HTML_OTHER_RE   = re.compile(r"</?(?:span|p|div)[^>]*>", re.IGNORECASE)
LITERAL_NH_RE   = re.compile(r"\\h")
TRAIL_WS_RE     = re.compile(r"[ \t]+$", re.MULTILINE)
MULTI_BLANK_RE  = re.compile(r"\n{3,}")


# Substrings of the lowercased FULL PATH: keep a single Japanese track and prune English dubs
# (the anime default). A title also in DUAL_AUDIO_KEYWORDS keeps both (that set is checked first).
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

# Per-title opt-in to keep BOTH the original track and the English dub.
DUAL_AUDIO_KEYWORDS: set[str] = {
    "godzilla minus one", "shin godzilla",
    "may i ask for one final thing",
}

KOREAN_KEYWORDS: set[str] = {
    "parasite", "squid game", "oldboy", "train to busan", "snowpiercer",
    "kingdom", "minari", "the handmaiden", "memories of murder",
    "all of us are dead", "sweet home", "hellbound", "alive",
    "peninsula", "the host", "the wailing", "burning",
    "decision to leave", "broker", "past lives", "extraordinary attorney woo",
    "crash landing", "itaewon class", "vincenzo",
}


COMMENT_TITLE_RE = re.compile(
    r"\b(commentar(y|ies)|director'?s|isolated\s*(score|track|music)|"
    r"audio\s*description|descript(ive|ion)|described|narration|"
    r"karaoke|sing.?along|filmmaker)\b",
    re.IGNORECASE,
)


# Higher = preferred when picking which audio track to keep.
CODEC_PREF: dict[str, int] = {
    "truehd": 80, "dts": 70, "eac3": 60, "ac3": 50, "flac": 40,
    "opus": 35, "aac": 30, "mp3": 20, "vorbis": 15,
}


# Cover art embedded as a real video stream without attached_pic: players can bind to it (black
# screen, no audio until a seek), so always strip.
STILL_IMAGE_VIDEO_CODECS: set[str] = {
    "mjpeg", "png", "jpeg", "bmp", "gif", "webp", "tiff", "ppm", "pgm",
}


# Orphan-sweep patterns: `.tmp.mkv` files always carry the writing PID; consolsub_* dirs may.
TMP_MKV_RE       = re.compile(r"\.consol\.(\d+)\.tmp\.mkv$")
CONSOLSUB_DIR_RE = re.compile(r"^consolsub_(?:(\d+)_)?[A-Za-z0-9]+$")
SWEEP_MIN_AGE_S: int = 30 * 60  # PID-less consolsub_* dirs must be at least this old to be swept

# normalize-audio.py's `.normalize-tmp` carries no PID (workers share one per folder), so liveness
# is decided per FILE from the PID in `.<stem>.<pid>.pass2.mkv` / `.remux.mkv`.
NORMALIZE_TMP_DIR = ".normalize-tmp"
NORMALIZE_TMP_RE  = re.compile(r"\.(\d+)\.(?:pass2|remux)\.mkv$")
# That PID belongs to a normalize-audio.py worker, never to the sweep's caller.
NORMALIZE_SCRIPT  = "normalize-audio.py"
# A leftover whose worker is gone is swept only once older than this.
NORMALIZE_TMP_MIN_AGE_S: int = 60 * 60
