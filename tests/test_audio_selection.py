"""Unit tests for consolidate-subs.py's audio-selection policy.

Codifies the 15-case synthetic suite that drove the round-1-through-3 codex
iterations on the multi-language audio retention patch. Covers:
- canonical-language alias normalization (codex round-1 #2+#3)
- multi_keep gating (codex round-2 #4): anime/Korean keyword paths
  preserve single-track behavior; only DUAL_AUDIO_KEYWORDS and the
  canonical-disposition path opt in to multi-keep
- Hollywood + side foreign-dub-track false-positive guard
- canonical-family dedup (jpn + ja duplicate kept once)

`consolidate-subs.py` ships with a hyphen in its name and is the entry
script, not a module. Load it via importlib so the tests work without
requiring a rename or sys.path tweak for the file itself.
"""

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "consolidate_subs", str(PROJECT_ROOT / "consolidate-subs.py")
)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)


def _stream(index, lang=None, channels=6, codec="ac3",
            default=False, original=False, comment=False,
            visual_impaired=False, dub=False, title=None):
    tags = {}
    if lang is not None:
        tags["language"] = lang
    if title is not None:
        tags["title"] = title
    disp = {}
    if default:
        disp["default"] = 1
    if original:
        disp["original"] = 1
    if comment:
        disp["comment"] = 1
    if visual_impaired:
        disp["visual_impaired"] = 1
    if dub:
        disp["dub"] = 1
    return {
        "index": index,
        "codec_name": codec,
        "channels": channels,
        "tags": tags,
        "disposition": disp,
    }


# ---------------------------------------------------------------------------
# get_audio_lang_pref
# ---------------------------------------------------------------------------


def test_default_english_no_audios():
    allowed, primary, multi = cs.get_audio_lang_pref(Path("/movies/Foo/x.mkv"))
    assert primary == "eng"
    assert multi is False
    assert "eng" in allowed


def test_anime_path_single_jpn():
    """/anime/ path picks jpn primary AND multi_keep=False — anime
    preference is to prune English dubs (codex round-2 #4)."""
    audios = [_stream(1, "jpn"), _stream(2, "eng")]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/anime/Akira (1988)/x.mkv"), audios
    )
    assert primary == "jpn"
    assert multi is False


def test_japanese_keyword_single_jpn():
    """Naruto title triggers JAPANESE_KEYWORDS path → single jpn primary."""
    audios = [_stream(1, "jpn"), _stream(2, "eng")]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Naruto Movie 1/x.mkv"), audios
    )
    assert primary == "jpn"
    assert multi is False


def test_dual_audio_keyword_multi_keep():
    """Godzilla Minus One is in DUAL_AUDIO_KEYWORDS → multi-keep jpn+eng."""
    audios = [_stream(1, "jpn"), _stream(2, "eng")]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Godzilla Minus One (2023)/x.mkv"), audios
    )
    assert primary == "jpn"
    assert multi is True


def test_korean_keyword_single_kor():
    """Parasite is in KOREAN_KEYWORDS → single kor primary, eng dub pruned."""
    audios = [_stream(1, "kor"), _stream(2, "eng")]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Parasite (2019)/x.mkv"), audios
    )
    assert primary == "kor"
    assert multi is False


def test_canonical_disposition_promotes_jpn_multi():
    """No keyword match, but input has jpn with default+original
    disposition → auto-promote to multi-keep eng+jpn."""
    audios = [
        _stream(1, "jpn", default=True, original=True),
        _stream(2, "eng"),
    ]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Some Random Film (2024)/x.mkv"), audios
    )
    assert primary == "jpn"
    assert multi is True


def test_canonical_promotion_handles_ja_alias():
    """A release tagged with ISO-639-1 `ja` instead of `jpn` must still
    trigger canonical-disposition promotion (codex round-1 #3)."""
    audios = [
        _stream(1, "ja", default=True, original=True),
        _stream(2, "en"),
    ]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Random Foreign Film/x.mkv"), audios
    )
    assert primary == "jpn"
    assert multi is True


def test_hollywood_with_side_jpn_dub_does_not_promote():
    """English-original Hollywood film with a side jpn dub track that
    carries NO canonical disposition flag → no multi-keep, no false
    positive (codex round-2 #4 root cause)."""
    audios = [
        _stream(1, "eng", default=True, original=True),
        _stream(2, "jpn", channels=2),
    ]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Iron Man (2008)/x.mkv"), audios
    )
    assert primary == "eng"
    assert multi is False


# ---------------------------------------------------------------------------
# select_keep_audio
# ---------------------------------------------------------------------------


def test_select_returns_empty_for_no_audio():
    assert cs.select_keep_audio([], cs.ENG_AUDIO_LANGS, "eng") == []


def test_select_single_eng_track():
    audios = [_stream(1, "eng", default=True)]
    assert cs.select_keep_audio(audios, cs.ENG_AUDIO_LANGS, "eng") == [1]


def test_select_picks_higher_scored_of_two_eng_tracks():
    """Two eng tracks: truehd 8ch must beat ac3 6ch (codec_pref + channels)."""
    audios = [
        _stream(1, "eng", channels=6, codec="ac3"),
        _stream(2, "eng", channels=8, codec="truehd", default=True),
    ]
    assert cs.select_keep_audio(audios, cs.ENG_AUDIO_LANGS, "eng") == [2]


def test_select_excludes_commentary():
    """Commentary disposition must drop the track regardless of language."""
    audios = [
        _stream(1, "eng", default=True),
        _stream(2, "eng", title="Director's commentary", comment=True),
    ]
    assert cs.select_keep_audio(audios, cs.ENG_AUDIO_LANGS, "eng") == [1]


def test_select_multi_keep_returns_primary_first():
    """multi_keep=True with Godzilla-style input → keep eng + jpn,
    primary (jpn) first so mkvmerge marks it default."""
    audios = [_stream(1, "jpn", default=True), _stream(2, "eng")]
    allowed = cs.ENG_AUDIO_LANGS | cs.JPN_AUDIO_LANGS
    kept = cs.select_keep_audio(audios, allowed, "jpn", multi_keep=True)
    assert set(kept) == {1, 2}
    assert kept[0] == 1  # primary first


def test_select_multi_keep_dedups_canonical_family():
    """jpn + ja duplicates (both jpn canonical family) must dedup to one,
    plus the eng stream — total 2 streams kept (codex round-1 #2)."""
    audios = [
        _stream(1, "jpn", default=True, channels=6),
        _stream(2, "ja", channels=2),
        _stream(3, "eng", channels=6),
    ]
    allowed = cs.ENG_AUDIO_LANGS | cs.JPN_AUDIO_LANGS
    kept = cs.select_keep_audio(audios, allowed, "jpn", multi_keep=True)
    assert len(kept) == 2
    assert 3 in kept  # eng kept
    assert (1 in kept) ^ (2 in kept)  # exactly one jpn-family
    assert kept[0] in (1, 2)  # primary first


def test_select_single_mode_returns_only_best():
    """multi_keep=False on input with multiple allowed-lang tracks must
    still return only the single best — the documented anime/Korean
    single-track behavior (codex round-2 #4 fix)."""
    audios = [_stream(1, "jpn", default=True), _stream(2, "eng")]
    allowed = cs.ENG_AUDIO_LANGS | cs.JPN_AUDIO_LANGS
    kept = cs.select_keep_audio(audios, allowed, "jpn", multi_keep=False)
    assert kept == [1]
