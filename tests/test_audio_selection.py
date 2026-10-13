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


# --- dual-audio opt-in vs the anime default --------------------------------------
# A title can match BOTH JAPANESE_KEYWORDS (it is an anime) and DUAL_AUDIO_KEYWORDS
# (the operator wants both tracks).  The explicit opt-in must win, otherwise the anime
# rule prunes the English dub on import and a dual-audio release can never yield a
# dual-audio library file.

def _dual_audio_streams():
    return [{"codec_type": "audio", "tags": {"language": "jpn"}, "disposition": {"default": 1}},
            {"codec_type": "audio", "tags": {"language": "eng"}, "disposition": {"default": 0}}]


def test_dual_audio_optin_beats_anime_rule():
    from media_stack.audio import get_audio_lang_pref
    p = Path("/m/tv/May I Ask for One Final Thing!/Season 1/ep.mkv")
    _allowed, primary, multi = get_audio_lang_pref(p, _dual_audio_streams())
    assert primary == "jpn"
    assert multi is True, "opted-in title must keep both audio tracks"


def test_plain_anime_still_prunes_the_dub():
    """Regression: reordering the checks must not change ordinary anime handling."""
    from media_stack.audio import get_audio_lang_pref
    p = Path("/m/tv/Aggretsuko/Season 1/ep.mkv")
    _allowed, primary, multi = get_audio_lang_pref(p, _dual_audio_streams())
    assert primary == "jpn"
    assert multi is False


def test_anime_path_still_prunes_the_dub():
    from media_stack.audio import get_audio_lang_pref
    p = Path("/m/anime/Some Show/Season 1/ep.mkv")
    _allowed, primary, multi = get_audio_lang_pref(p, _dual_audio_streams())
    assert primary == "jpn" and multi is False


def test_existing_dual_audio_titles_unaffected():
    from media_stack.audio import get_audio_lang_pref
    p = Path("/m/movies/Godzilla Minus One (2023)/f.mkv")
    _allowed, primary, multi = get_audio_lang_pref(p, _dual_audio_streams())
    assert primary == "jpn" and multi is True


# ---------------------------------------------------------------------------
# original_language signal (DEFECT 2 fix) — `default` is not evidence of
# original language; release groups routinely flag a DUB `default`.
# ---------------------------------------------------------------------------


def test_english_original_with_jpn_dub_flagged_default_stays_eng_primary():
    """An English-original film with a Japanese DUB flagged `default`
    (the exact DEFECT 2 scenario) must keep eng primary / multi False,
    regardless of what any stream's disposition claims."""
    audios = [
        _stream(1, "eng"),
        _stream(2, "jpn", default=True),
    ]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Some Hollywood Film (2020)/x.mkv"),
        audios, original_language="eng",
    )
    assert primary == "eng"
    assert multi is False
    assert allowed == cs.ENG_AUDIO_LANGS


def test_unknown_original_jpn_default_only_does_not_promote_but_keeps_both():
    """Unknown original_language (None) + a jpn stream flagged `default`
    only (no `original` disposition) must NOT promote jpn to primary —
    `default` alone is not evidence — but must ALSO NOT drop the jpn
    track: a wrong default flag is recoverable, a deleted track is not.
    Both tracks are kept, eng primary/default, multi_keep=True."""
    audios = [
        _stream(1, "eng"),
        _stream(2, "jpn", default=True),
    ]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Some Random Film (2024)/x.mkv"),
        audios, original_language=None,
    )
    assert primary == "eng"
    assert multi is True
    assert allowed == cs.ENG_AUDIO_LANGS | cs.JPN_AUDIO_LANGS
    kept = cs.select_keep_audio(audios, allowed, primary, multi)
    assert set(kept) == {1, 2}
    assert kept[0] == 1  # eng is primary/default; jpn still present, just not default


def test_unknown_original_kor_default_only_does_not_promote_but_keeps_both():
    """Symmetric kor case for the same rule."""
    audios = [
        _stream(1, "eng"),
        _stream(2, "kor", default=True),
    ]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Some Random Film (2024)/x.mkv"),
        audios, original_language=None,
    )
    assert primary == "eng"
    assert multi is True
    assert allowed == cs.ENG_AUDIO_LANGS | cs.KOR_AUDIO_LANGS
    kept = cs.select_keep_audio(audios, allowed, primary, multi)
    assert set(kept) == {1, 2}
    assert kept[0] == 1


def test_arr_fetch_failure_on_japanese_original_style_file_keeps_both_tracks():
    """Integration-shaped regression: simulate an Arr fetch failure
    (original_language=None, exactly what arrlang.original_language_for
    returns on any failure) on a file shaped like a Japanese-original
    title (jpn flagged default, eng dub, no `original` disposition
    anywhere since the release group only ever sets `default`). Both
    tracks must survive — losing the jpn original track because Arr was
    briefly unreachable would be data loss for something recoverable."""
    audios = [
        _stream(1, "jpn", default=True),
        _stream(2, "eng"),
    ]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Some Foreign Film (2021)/x.mkv"),
        audios, original_language=None,
    )
    kept = cs.select_keep_audio(audios, allowed, primary, multi)
    assert set(kept) == {1, 2}, "both tracks must survive an Arr fetch failure"


def test_unknown_original_jpn_original_disposition_still_promotes():
    """Unknown original_language but a jpn stream flagged `original`
    (not just `default`) must still promote — backward-compat with the
    pre-arrlang disposition rule, sharpened to `original`-only."""
    audios = [
        _stream(1, "eng"),
        _stream(2, "jpn", original=True),
    ]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Some Random Film (2024)/x.mkv"),
        audios, original_language=None,
    )
    assert primary == "jpn"
    assert multi is True


def test_original_language_jpn_with_eng_and_jpn_present_multi_keep():
    """original_language="jpn" (from Radarr/Sonarr) with both eng + jpn
    streams present → jpn primary, multi_keep=True — no disposition flags
    needed at all, the Arr signal is authoritative."""
    audios = [
        _stream(1, "eng"),
        _stream(2, "jpn"),
    ]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Some Foreign Film (2021)/x.mkv"),
        audios, original_language="jpn",
    )
    assert primary == "jpn"
    assert multi is True
    assert allowed == cs.ENG_AUDIO_LANGS | cs.JPN_AUDIO_LANGS


def test_original_language_kor_with_eng_and_kor_present_multi_keep():
    """Symmetric kor case for the same rule."""
    audios = [
        _stream(1, "eng"),
        _stream(2, "kor"),
    ]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Some Korean Film (2021)/x.mkv"),
        audios, original_language="kor",
    )
    assert primary == "kor"
    assert multi is True
    assert allowed == cs.ENG_AUDIO_LANGS | cs.KOR_AUDIO_LANGS


def test_original_language_jpn_without_eng_stream_single_keep():
    """original_language="jpn" but no eng stream actually present →
    jpn primary, multi_keep=False (nothing to dual-keep)."""
    audios = [_stream(1, "jpn")]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Some Foreign Film (2021)/x.mkv"),
        audios, original_language="jpn",
    )
    assert primary == "jpn"
    assert multi is False


def test_keyword_rule_beats_original_language():
    """DUAL_AUDIO_KEYWORDS is an explicit per-title opt-in and must beat
    the original_language signal even when Radarr reports "eng" (e.g. a
    bad/incomplete Arr match) — keyword rules are checked first."""
    audios = [_stream(1, "jpn"), _stream(2, "eng")]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Godzilla Minus One (2023)/x.mkv"),
        audios, original_language="eng",
    )
    assert primary == "jpn"
    assert multi is True


def test_original_language_other_value_unchanged_fallback():
    """An original_language outside eng/jpn/kor (e.g. "dan") must behave
    exactly like the fallback — no behavior change for languages this
    mechanism doesn't recognize."""
    audios = [_stream(1, "eng"), _stream(2, "dan", default=True, original=True)]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Some Danish Film (2022)/x.mkv"),
        audios, original_language="dan",
    )
    assert primary == "eng"
    assert multi is False
    assert allowed == cs.ENG_AUDIO_LANGS


def test_original_language_omitted_preserves_old_default_or_original_rule():
    """Callers that don't pass original_language at all keep getting
    rule 6 (unknown) behavior — this test specifically pins that rule 6
    is reached (not some new default) when the kwarg is simply absent."""
    audios = [_stream(1, "eng"), _stream(2, "jpn", original=True)]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Some Random Film (2024)/x.mkv"), audios,
    )
    assert primary == "jpn"
    assert multi is True


# ---------------------------------------------------------------------------
# Codex round 2: a jpn `default` must never outrank a kor `original`
# ---------------------------------------------------------------------------


def test_jpn_default_does_not_outrank_kor_original():
    """eng + jpn(default only) + kor(original), unknown original_language
    -> `original` is checked for BOTH languages before either's `default`
    is considered at all, so kor (the trustworthy signal) wins primacy
    even though jpn's untrustworthy `default` flag would be checked
    first under a naive single-language-at-a-time loop. kor must also
    actually be KEPT (not just reported as primary)."""
    audios = [
        _stream(1, "eng"),
        _stream(2, "jpn", default=True),
        _stream(3, "kor", original=True),
    ]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Some Random Film (2024)/x.mkv"),
        audios, original_language=None,
    )
    assert primary == "kor"
    assert multi is True
    kept = cs.select_keep_audio(audios, allowed, primary, multi)
    assert 3 in kept


# ---------------------------------------------------------------------------
# confident_default_audio — the delete-free signal for the "no main
# English subtitle yet" cleanup (consolidate-subs.py's
# _cleanup_no_main_english). Never drops a track; only ever says which
# ONE stream should carry the `default` flag, and only when confident.
# ---------------------------------------------------------------------------


def test_confident_default_audio_dual_audio_keyword():
    from media_stack.audio import confident_default_audio
    audios = [_stream(1, "eng"), _stream(2, "jpn")]
    idx = confident_default_audio(
        Path("/movies/Godzilla Minus One (2023)/x.mkv"), audios, original_language=None)
    assert idx == 2


def test_confident_default_audio_korean_keyword():
    from media_stack.audio import confident_default_audio
    audios = [_stream(1, "eng"), _stream(2, "kor")]
    idx = confident_default_audio(
        Path("/movies/Parasite (2019)/x.mkv"), audios, original_language=None)
    assert idx == 2


def test_confident_default_audio_known_original_language():
    from media_stack.audio import confident_default_audio
    audios = [_stream(1, "jpn"), _stream(2, "eng")]
    idx = confident_default_audio(
        Path("/movies/Some Foreign Film/x.mkv"), audios, original_language="jpn")
    assert idx == 1


def test_confident_default_audio_original_disposition_when_unknown():
    from media_stack.audio import confident_default_audio
    audios = [_stream(1, "eng"), _stream(2, "jpn", original=True)]
    idx = confident_default_audio(
        Path("/movies/Some Foreign Film/x.mkv"), audios, original_language=None)
    assert idx == 2


def test_confident_default_audio_default_only_is_not_confident():
    """`default` alone (no `original`, original_language unknown) must
    NOT be treated as confident -- leave the audio alone entirely."""
    from media_stack.audio import confident_default_audio
    audios = [_stream(1, "eng"), _stream(2, "jpn", default=True)]
    idx = confident_default_audio(
        Path("/movies/Some Foreign Film/x.mkv"), audios, original_language=None)
    assert idx is None


def test_confident_default_audio_irrelevant_known_language_is_not_confident():
    """A KNOWN but irrelevant original_language (e.g. "dan") is not one
    of the three confidence sources -- must not fall through to checking
    disposition either."""
    from media_stack.audio import confident_default_audio
    audios = [_stream(1, "eng"), _stream(2, "jpn", original=True)]
    idx = confident_default_audio(
        Path("/movies/Some Danish Film/x.mkv"), audios, original_language="dan")
    assert idx is None


def test_confident_default_audio_no_audios_returns_none():
    from media_stack.audio import confident_default_audio
    assert confident_default_audio(Path("/movies/X/x.mkv"), [], original_language="jpn") is None
    assert confident_default_audio(Path("/movies/X/x.mkv"), None, original_language="jpn") is None


def test_confident_default_audio_excludes_commentary():
    from media_stack.audio import confident_default_audio
    audios = [
        _stream(1, "eng", title="Director's commentary", comment=True),
        _stream(2, "eng", channels=6),
    ]
    idx = confident_default_audio(
        Path("/movies/Some Film/x.mkv"), audios, original_language="eng")
    assert idx == 2


def test_confident_default_audio_requires_explicit_tag_not_und():
    """An untagged/`und` stream might be anything -- it's never picked as
    THE default just because canonical_lang("") == "eng"."""
    from media_stack.audio import confident_default_audio
    audios = [_stream(1, None)]  # no language tag at all -> raw ""
    idx = confident_default_audio(
        Path("/movies/Some Film/x.mkv"), audios, original_language="eng")
    assert idx is None


def test_confident_default_audio_picks_best_scored_candidate():
    """Two streams both explicitly eng -> the better one (more channels,
    better codec) wins, same inputs as select_keep_audio's scoring."""
    from media_stack.audio import confident_default_audio
    audios = [
        _stream(1, "eng", channels=2, codec="aac"),
        _stream(2, "eng", channels=8, codec="truehd"),
    ]
    idx = confident_default_audio(
        Path("/movies/Some Film/x.mkv"), audios, original_language="eng")
    assert idx == 2


def test_confident_default_audio_regional_tag_counts_as_explicit():
    """en-US is an EXPLICIT English tag (regional variant), not an
    untagged/und stream -- it qualifies as a candidate."""
    from media_stack.audio import confident_default_audio
    audios = [_stream(1, "en-US")]
    idx = confident_default_audio(
        Path("/movies/Some Film/x.mkv"), audios, original_language="eng")
    assert idx == 1


def test_confident_default_audio_no_candidate_for_decided_language():
    """original_language says jpn, but no stream is actually tagged jpn
    -- nothing to point `default` at, so None (not some other language)."""
    from media_stack.audio import confident_default_audio
    audios = [_stream(1, "eng")]
    idx = confident_default_audio(
        Path("/movies/Some Film/x.mkv"), audios, original_language="jpn")
    assert idx is None


def test_arr_outage_toggle_does_not_alternate_the_default():
    """Thrash-avoidance: Arr up (original_language="jpn") picks jpn with
    confidence. The cleanup remux that decision drives sets jpn's
    `default` disposition (not `original` -- mkvmerge's --default-track
    never touches `original`). Arr then goes down (original_language=
    None) on the NEXT pass over the same (now-updated) streams: jpn's
    `default` flag must NOT be read as confidence, so the result is None
    (no further remux) -- not eng, not a flip back. And once Arr is back
    up with the SAME answer, the result is IDENTICAL to pass 1 -- no
    alternation between two different answers run after run."""
    from media_stack.audio import confident_default_audio
    p = Path("/movies/Some Foreign Film/x.mkv")

    audios_pass1 = [_stream(1, "eng"), _stream(2, "jpn")]
    idx1 = confident_default_audio(p, audios_pass1, original_language="jpn")
    assert idx1 == 2

    # Simulate the remux's own effect: jpn now flagged `default`.
    audios_pass2 = [_stream(1, "eng"), _stream(2, "jpn", default=True)]
    idx2 = confident_default_audio(p, audios_pass2, original_language=None)
    assert idx2 is None  # Arr down -> no confidence -> no further remux

    idx3 = confident_default_audio(p, audios_pass2, original_language="jpn")
    assert idx3 == 2  # Arr back up -> same answer as pass 1, no alternation


# ---------------------------------------------------------------------------
# Codex round 3 #1: select_keep_audio's in-spec test must be FAMILY-based
# (canonical_lang), not a literal raw-string match against allowed_langs --
# a ja-JP/en-US regional tag was silently excluded even when its family
# was allowed.
# ---------------------------------------------------------------------------


def test_select_keep_audio_family_based_matching_ja_jp_with_jpn_allowed():
    """eng + ja-JP, original_language="jpn" -> both must be kept (ja-JP's
    family IS jpn, which IS allowed) -- a literal raw-string match would
    wrongly exclude ja-JP and keep only eng."""
    audios = [_stream(1, "eng"), _stream(2, "ja-JP")]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/Some Foreign Film (2021)/x.mkv"),
        audios, original_language="jpn",
    )
    assert primary == "jpn"
    assert multi is True
    kept = cs.select_keep_audio(audios, allowed, primary, multi)
    assert set(kept) == {1, 2}


def test_select_keep_audio_en_us_with_jpn_anime_keyword_unaffected():
    """en-US (eng family) + jpn, anime keyword path (multi_keep=False) ->
    behaves exactly as before: jpn is the sole kept track."""
    audios = [_stream(1, "en-US"), _stream(2, "jpn")]
    allowed, primary, multi = cs.get_audio_lang_pref(
        Path("/movies/anime/Some Show/x.mkv"), audios,
    )
    assert primary == "jpn"
    assert multi is False
    kept = cs.select_keep_audio(audios, allowed, primary, multi)
    assert kept == [2]
