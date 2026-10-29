"""Unit tests for consolidate-subs.py's audio-selection policy.

Covers canonical-language alias normalization, multi_keep gating (anime/Korean keyword
paths stay single-track; only DUAL_AUDIO_KEYWORDS and the canonical-disposition path
multi-keep), the Hollywood-with-side-dub false-positive guard, and canonical-family dedup.

`consolidate-subs.py` is a hyphenated entry script, so it is loaded via importlib.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "consolidate_subs", str(PROJECT_ROOT / "consolidate-subs.py")
)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)

from media_stack.audio import confident_default_audio  # noqa: E402
from media_stack.lang import ENG_AUDIO_LANGS, JPN_AUDIO_LANGS, KOR_AUDIO_LANGS  # noqa: E402


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


# get_audio_lang_pref

FILM = "/movies/Some Random Film (2024)/x.mkv"
FOREIGN = "/movies/Some Foreign Film (2021)/x.mkv"
GODZILLA = "/movies/Godzilla Minus One (2023)/x.mkv"
# raw streams (no index/codec) for the keyword-vs-anime ordering cases
DUAL_AUDIO_STREAMS = [
    {"codec_type": "audio", "tags": {"language": "jpn"}, "disposition": {"default": 1}},
    {"codec_type": "audio", "tags": {"language": "eng"}, "disposition": {"default": 0}}]
JPN_ENG = [_stream(1, "jpn"), _stream(2, "eng")]
ENG_JPN_ORIGINAL = [_stream(1, "eng"), _stream(2, "jpn", original=True)]


def test_default_english_no_audios():
    allowed, primary, multi = cs.get_audio_lang_pref(Path("/movies/Foo/x.mkv"))
    assert primary == "eng"
    assert multi is False
    assert "eng" in allowed


@pytest.mark.parametrize("path, audios, kwargs, primary, multi, allowed", [
    pytest.param("/movies/anime/Akira (1988)/x.mkv", JPN_ENG, {}, "jpn", False, None,
                 id="anime-path-single-jpn-dub-pruned"),
    pytest.param("/movies/Naruto Movie 1/x.mkv", JPN_ENG, {}, "jpn", False, None,
                 id="japanese-keyword-single-jpn"),
    pytest.param(GODZILLA, JPN_ENG, {}, "jpn", True, None,
                 id="dual-audio-keyword-multi-keep"),
    pytest.param("/movies/Parasite (2019)/x.mkv", [_stream(1, "kor"), _stream(2, "eng")], {},
                 "kor", False, None, id="korean-keyword-single-kor"),
    pytest.param(FILM, [_stream(1, "jpn", default=True, original=True), _stream(2, "eng")], {},
                 "jpn", True, None, id="default-plus-original-disposition-promotes-jpn-multi"),
    pytest.param("/movies/Random Foreign Film/x.mkv",
                 [_stream(1, "ja", default=True, original=True), _stream(2, "en")], {},
                 "jpn", True, None, id="ja-alias-still-promotes"),
    pytest.param("/movies/Iron Man (2008)/x.mkv",
                 [_stream(1, "eng", default=True, original=True), _stream(2, "jpn", channels=2)], {},
                 "eng", False, None, id="hollywood-side-jpn-dub-does-not-promote"),
    pytest.param("/movies/Some Hollywood Film (2020)/x.mkv",
                 [_stream(1, "eng"), _stream(2, "jpn", default=True)], {"original_language": "eng"},
                 "eng", False, ENG_AUDIO_LANGS, id="eng-original-jpn-dub-flagged-default-stays-eng"),
    pytest.param(FILM, ENG_JPN_ORIGINAL, {"original_language": None}, "jpn", True, None,
                 id="unknown-original-jpn-original-disposition-promotes"),
    pytest.param(FILM, ENG_JPN_ORIGINAL, {}, "jpn", True, None,
                 id="original-language-omitted-is-unknown"),
    pytest.param(FOREIGN, [_stream(1, "eng"), _stream(2, "jpn")], {"original_language": "jpn"},
                 "jpn", True, ENG_AUDIO_LANGS | JPN_AUDIO_LANGS,
                 id="original-jpn-with-eng-and-jpn-multi-keep"),
    pytest.param("/movies/Some Korean Film (2021)/x.mkv", [_stream(1, "eng"), _stream(2, "kor")],
                 {"original_language": "kor"}, "kor", True, ENG_AUDIO_LANGS | KOR_AUDIO_LANGS,
                 id="original-kor-with-eng-and-kor-multi-keep"),
    pytest.param(FOREIGN, [_stream(1, "jpn")], {"original_language": "jpn"}, "jpn", False, None,
                 id="original-jpn-without-eng-stream-single-keep"),
    pytest.param(GODZILLA, JPN_ENG, {"original_language": "eng"}, "jpn", True, None,
                 id="dual-audio-keyword-beats-original-language"),
    pytest.param("/movies/Some Danish Film (2022)/x.mkv",
                 [_stream(1, "eng"), _stream(2, "dan", default=True, original=True)],
                 {"original_language": "dan"}, "eng", False, ENG_AUDIO_LANGS,
                 id="other-original-language-falls-back"),
    pytest.param("/m/tv/May I Ask for One Final Thing!/Season 1/ep.mkv", DUAL_AUDIO_STREAMS, {},
                 "jpn", True, None, id="dual-audio-optin-beats-anime-rule"),
    # Japanese-DEFAULT stream: anime pruning must still win over default-disposition inference.
    pytest.param("/m/tv/Aggretsuko/Season 1/ep.mkv", DUAL_AUDIO_STREAMS, {},
                 "jpn", False, None, id="plain-anime-jpn-default-still-prunes-the-dub"),
    pytest.param("/m/anime/Some Show/Season 1/ep.mkv", DUAL_AUDIO_STREAMS, {},
                 "jpn", False, None, id="anime-path-jpn-default-still-prunes-the-dub"),
    pytest.param("/m/movies/Godzilla Minus One (2023)/f.mkv", DUAL_AUDIO_STREAMS, {},
                 "jpn", True, None, id="existing-dual-audio-title-jpn-default-unaffected"),
])
def test_get_audio_lang_pref(path, audios, kwargs, primary, multi, allowed):
    got_allowed, got_primary, got_multi = cs.get_audio_lang_pref(Path(path), audios, **kwargs)
    assert got_primary == primary
    assert got_multi is multi
    if allowed is not None:
        assert got_allowed == allowed


@pytest.mark.parametrize("lang, langs", [("jpn", JPN_AUDIO_LANGS), ("kor", KOR_AUDIO_LANGS)])
def test_unknown_original_foreign_default_only_does_not_promote_but_keeps_both(lang, langs):
    """A wrong default flag is recoverable, a deleted track is not."""
    audios = [_stream(1, "eng"), _stream(2, lang, default=True)]
    allowed, primary, multi = cs.get_audio_lang_pref(Path(FILM), audios, original_language=None)
    assert primary == "eng"
    assert multi is True
    assert allowed == ENG_AUDIO_LANGS | langs
    kept = cs.select_keep_audio(audios, allowed, primary, multi)
    assert set(kept) == {1, 2}
    assert kept[0] == 1  # eng is primary/default; the foreign track is still present, just not default


def test_arr_fetch_failure_on_japanese_original_style_file_keeps_both_tracks():
    """Losing the jpn original because Arr was briefly down is data loss."""
    audios = [_stream(1, "jpn", default=True), _stream(2, "eng")]
    allowed, primary, multi = cs.get_audio_lang_pref(Path(FOREIGN), audios, original_language=None)
    kept = cs.select_keep_audio(audios, allowed, primary, multi)
    assert set(kept) == {1, 2}, "both tracks must survive an Arr fetch failure"


def test_jpn_default_does_not_outrank_kor_original():
    """`original` is checked for both languages before either's `default`."""
    audios = [_stream(1, "eng"), _stream(2, "jpn", default=True), _stream(3, "kor", original=True)]
    allowed, primary, multi = cs.get_audio_lang_pref(Path(FILM), audios, original_language=None)
    assert primary == "kor"
    assert multi is True
    kept = cs.select_keep_audio(audios, allowed, primary, multi)
    assert 3 in kept


def test_select_keep_audio_family_based_matching_ja_jp_with_jpn_allowed():
    """eng + ja-JP, original_language="jpn": both kept (ja-JP's family is jpn)."""
    audios = [_stream(1, "eng"), _stream(2, "ja-JP")]
    allowed, primary, multi = cs.get_audio_lang_pref(Path(FOREIGN), audios, original_language="jpn")
    assert primary == "jpn"
    assert multi is True
    kept = cs.select_keep_audio(audios, allowed, primary, multi)
    assert set(kept) == {1, 2}


def test_select_keep_audio_en_us_with_jpn_anime_keyword_unaffected():
    """en-US + jpn on the anime keyword path: jpn is the sole kept track."""
    audios = [_stream(1, "en-US"), _stream(2, "jpn")]
    allowed, primary, multi = cs.get_audio_lang_pref(Path("/movies/anime/Some Show/x.mkv"), audios)
    assert primary == "jpn"
    assert multi is False
    kept = cs.select_keep_audio(audios, allowed, primary, multi)
    assert kept == [2]


# select_keep_audio

_ENG_JPN = ENG_AUDIO_LANGS | JPN_AUDIO_LANGS


@pytest.mark.parametrize("audios, allowed, primary, kwargs, expected", [
    pytest.param([], ENG_AUDIO_LANGS, "eng", {}, [], id="no-audio-empty"),
    pytest.param([_stream(1, "eng", default=True)], ENG_AUDIO_LANGS, "eng", {}, [1],
                 id="single-eng-track"),
    pytest.param([_stream(1, "eng", channels=6, codec="ac3"),
                  _stream(2, "eng", channels=8, codec="truehd", default=True)],
                 ENG_AUDIO_LANGS, "eng", {}, [2], id="higher-scored-of-two-eng-tracks"),
    pytest.param([_stream(1, "eng", default=True),
                  _stream(2, "eng", title="Director's commentary", comment=True)],
                 ENG_AUDIO_LANGS, "eng", {}, [1], id="commentary-excluded-regardless-of-language"),
    pytest.param([_stream(1, "jpn", default=True), _stream(2, "eng")], _ENG_JPN, "jpn",
                 {"multi_keep": False}, [1], id="single-mode-returns-only-best"),
])
def test_select_keep_audio(audios, allowed, primary, kwargs, expected):
    assert cs.select_keep_audio(audios, allowed, primary, **kwargs) == expected


def test_select_multi_keep_returns_primary_first():
    """Godzilla-style input: keep eng + jpn, primary (jpn) first so mkvmerge marks it default."""
    audios = [_stream(1, "jpn", default=True), _stream(2, "eng")]
    kept = cs.select_keep_audio(audios, _ENG_JPN, "jpn", multi_keep=True)
    assert set(kept) == {1, 2}
    assert kept[0] == 1  # primary first


def test_select_multi_keep_dedups_canonical_family():
    """jpn + ja duplicates (same canonical family) dedup to one, plus eng: 2 streams kept."""
    audios = [
        _stream(1, "jpn", default=True, channels=6),
        _stream(2, "ja", channels=2),
        _stream(3, "eng", channels=6),
    ]
    kept = cs.select_keep_audio(audios, _ENG_JPN, "jpn", multi_keep=True)
    assert len(kept) == 2
    assert 3 in kept  # eng kept
    assert (1 in kept) ^ (2 in kept)  # exactly one jpn-family
    assert kept[0] in (1, 2)  # primary first


# confident_default_audio: delete-free signal for _cleanup_no_main_english. It only says
# which ONE stream should carry `default`, and only when confident.

COMMENTARY = _stream(1, "eng", title="Director's commentary", comment=True)


@pytest.mark.parametrize("path, audios, original_language, expected", [
    pytest.param(GODZILLA, [_stream(1, "eng"), _stream(2, "jpn")], None, 2, id="dual-audio-keyword"),
    pytest.param("/movies/Parasite (2019)/x.mkv", [_stream(1, "eng"), _stream(2, "kor")], None, 2,
                 id="korean-keyword"),
    pytest.param("/movies/Some Foreign Film/x.mkv", JPN_ENG, "jpn", 1, id="known-original-language"),
    pytest.param("/movies/Some Foreign Film/x.mkv", ENG_JPN_ORIGINAL, None, 2,
                 id="original-disposition-when-unknown"),
    pytest.param("/movies/Some Foreign Film/x.mkv", [_stream(1, "eng"), _stream(2, "jpn", default=True)],
                 None, None, id="default-only-is-not-confident"),
    pytest.param("/movies/Some Danish Film/x.mkv", ENG_JPN_ORIGINAL, "dan", None,
                 id="irrelevant-known-language-is-not-confident"),
    pytest.param("/movies/X/x.mkv", [], "jpn", None, id="empty-audios"),
    pytest.param("/movies/X/x.mkv", None, "jpn", None, id="no-audios"),
    pytest.param("/movies/Some Film/x.mkv", [COMMENTARY, _stream(2, "eng", channels=6)], "eng", 2,
                 id="excludes-commentary"),
    pytest.param("/movies/Some Film/x.mkv", [_stream(1, None)], "eng", None,
                 id="requires-explicit-tag-not-untagged"),
    pytest.param("/movies/Some Film/x.mkv",
                 [_stream(1, "eng", channels=2, codec="aac"), _stream(2, "eng", channels=8, codec="truehd")],
                 "eng", 2, id="picks-best-scored-candidate"),
    pytest.param("/movies/Some Film/x.mkv", [_stream(1, "en-US")], "eng", 1,
                 id="regional-tag-counts-as-explicit"),
    pytest.param("/movies/Some Film/x.mkv", [_stream(1, "eng")], "jpn", None,
                 id="no-candidate-for-decided-language"),
])
def test_confident_default_audio(path, audios, original_language, expected):
    assert confident_default_audio(Path(path), audios, original_language=original_language) == expected


def test_arr_outage_toggle_does_not_alternate_the_default():
    """Thrash avoidance: after a jpn pick sets jpn `default` (mkvmerge never touches
    `original`), an Arr outage on the next pass gives None (no flip back), and Arr
    returning gives the same answer as pass 1."""
    p = Path("/movies/Some Foreign Film/x.mkv")

    audios_pass1 = [_stream(1, "eng"), _stream(2, "jpn")]
    idx1 = confident_default_audio(p, audios_pass1, original_language="jpn")
    assert idx1 == 2

    # the remux's own effect: jpn now flagged `default`
    audios_pass2 = [_stream(1, "eng"), _stream(2, "jpn", default=True)]
    idx2 = confident_default_audio(p, audios_pass2, original_language=None)
    assert idx2 is None  # Arr down -> no confidence -> no further remux

    idx3 = confident_default_audio(p, audios_pass2, original_language="jpn")
    assert idx3 == 2  # Arr back up -> same answer as pass 1, no alternation
