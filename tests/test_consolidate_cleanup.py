"""Tests for the AUDIO-AND-SUBTITLE CLEANUP REMUX (`_cleanup_no_main_english`), run from `_process_locked`'s
four "no main English subtitle yet" call sites.

Principle: never delete a track that might be wanted, never silently change a flag that was not meant to.
Video is untouched; audio is never dropped or reordered, only its `default` flag may move (when
`confident_default_audio` says so); only positively foreign subtitle tags are dropped; every kept track gets
an explicit default (and, for subtitles, forced) flag mirroring its source, since mkvmerge picks its own
default when the source has none.

`remux`/`_normalize_audio_inline`/`set_consolidated_tag`/`probe` are mocked. Media lives under `tmp_path`,
outside `MEDIA_ROOT`, so `original_language_for` returns None without a network call unless a test patches it.
"""

import importlib.util
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "consolidate_subs_cleanup", str(PROJECT_ROOT / "consolidate-subs.py")
)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)


@pytest.fixture(autouse=True)
def _isolate_needs_english_state(tmp_path, monkeypatch):
    """Point NEEDS_ENGLISH_STATE_FILE at `tmp_path` so no test writes the checkout's real tracker."""
    monkeypatch.setattr(cs, "NEEDS_ENGLISH_STATE_FILE", tmp_path / "needs-english.json")


def _media(tmp_path: Path, name: str = "Movie (2024).mkv") -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x1a\x45\xdf\xa3")
    return p


def _v(index=0, codec="h264"):
    return {"index": index, "codec_type": "video", "codec_name": codec}


def _a(index, lang, codec="ac3", **disposition):
    s = {"index": index, "codec_type": "audio", "codec_name": codec, "tags": {"language": lang}}
    if disposition:
        s["disposition"] = disposition
    return s


def _s(index, lang=None, codec="subrip", title=None, **disposition):
    s = {"index": index, "codec_type": "subtitle", "codec_name": codec}
    tags = {k: v for k, v in (("language", lang), ("title", title)) if v}
    if tags:
        s["tags"] = tags
    if disposition:
        s["disposition"] = disposition
    return s


def _verify_streams(*, video_defaults=(False,), audio_defaults=(), subs: int = 0) -> dict:
    """A probe()-shaped dict for the post-remux verify: one video per `video_defaults` entry, one audio per
    `audio_defaults` entry (its disposition.default), plus `subs` bare subtitle streams."""
    streams = [{"codec_type": "video", "disposition": {"default": int(bool(d))}}
               for d in video_defaults]
    streams += [{"codec_type": "audio", "disposition": {"default": int(bool(d))}}
                for d in audio_defaults]
    streams += [{"codec_type": "subtitle"} for _ in range(subs)]
    return {"streams": streams}


def _record_remux(calls: list):
    """A fake `remux()` that records every call (including `cleanup_flags`) and writes a marker file."""
    def _fake_remux(src, dst, video_idx, audio_idx, eng_srt, forced_srt,
                    keep_subtitle_indices=None, cleanup_flags=None):
        calls.append({
            "video_idx": video_idx, "audio_idx": audio_idx,
            "eng_srt": eng_srt, "forced_srt": forced_srt,
            "keep_subtitle_indices": keep_subtitle_indices,
            "cleanup_flags": cleanup_flags,
        })
        Path(dst).write_bytes(b"CLEANED")
        return True, "ok"
    return _fake_remux


def _run(tmp_path, streams, *, name="Movie (2024).mkv", verify=None, dry_run=False, arr_lang=None,
         **patches):
    """`_process_locked` over probed `streams`. `verify` is what the post-remux probe sees (default: the
    original streams); `arr_lang` patches `original_language_for`; each extra keyword patches that `cs`
    attribute with the given kwargs."""
    media = _media(tmp_path, name)
    original = {"streams": streams}
    calls: list = []
    with ExitStack() as stack:
        def patched(target, **kw):
            return stack.enter_context(patch.object(cs, target, **kw))

        patched("load_state", return_value={})
        patched("probe", side_effect=lambda p: original if Path(p) == media else (verify or original))
        patched("already_processed", return_value=False)
        patched("find_sidecar_subs", return_value=[])
        patched("remux", side_effect=_record_remux(calls))
        tag = patched("set_consolidated_tag")
        norm = patched("_normalize_audio_inline")
        if arr_lang is not None:
            patched("original_language_for", return_value=arr_lang)
        for target, kw in patches.items():
            patched(target, **kw)
        result = cs._process_locked(media, str(media), dry_run=dry_run)
    return SimpleNamespace(result=result, calls=calls, tag=tag, norm=norm, media=media)


def _cleanup_direct(media, *, subs, audio, verify, eng_image=()):
    """`_cleanup_no_main_english` called directly (no English candidates) with `verify` as the post-remux probe."""
    calls: list = []
    with patch.object(cs, "remux", side_effect=_record_remux(calls)), \
         patch.object(cs, "probe", return_value=verify), \
         patch.object(cs, "original_language_for", return_value=None), \
         patch.object(cs, "set_consolidated_tag") as tag, \
         patch.object(cs, "_normalize_audio_inline") as norm:
        result = cs._cleanup_no_main_english(
            media, str(media), "NEEDS_BAZARR", "no English candidates",
            subs, audio, [_v()], None, list(eng_image), False)
    return SimpleNamespace(result=result, calls=calls, tag=tag, norm=norm)


def _forced_english_extract(src, idx, codec, dst):
    """extract_track fake: the embedded forced track holds a short English line (TOO-LITTLE-TEXT, accepted)."""
    Path(dst).write_text("1\n00:00:01,000 --> 00:00:02,000\nWhere is the station?\n")
    return True


# --- audio never deleted or reordered, video never touched, exact flags re-emitted for kept tracks ---

@pytest.mark.parametrize("arr_lang, audio_defaults, flags", [
    ("eng", (False, True), {1: False, 2: True}),    # Radarr says English-original: eng gets default, by ID
    (None, (True, False), {1: True, 2: False}),     # no confidence signal: fre's OWN default is preserved
], ids=["confident-english-original", "no-confidence-signal"])
def test_foreign_default_audio_with_english_second_both_kept(tmp_path, arr_lang, audio_defaults, flags):
    """French default audio + English second audio + French-only subs: ONE cleanup remux keeps BOTH audio
    tracks in original order, leaves video alone, drops the French sub, writes no CONSOLIDATED_SUBS tag, and
    stays NEEDS_BAZARR."""
    r = _run(tmp_path, [_v(), _a(1, "fre", default=1), _a(2, "eng", "aac"), _s(3, "fre")],
             verify=_verify_streams(audio_defaults=audio_defaults), arr_lang=arr_lang)

    assert r.result["status"] == "NEEDS_BAZARR"
    assert "cleaned" in r.result["detail"]
    assert len(r.calls) == 1
    call = r.calls[0]
    assert call["video_idx"] == [0]
    assert call["audio_idx"] == [1, 2]            # ORIGINAL order, NEVER reordered
    assert {i: call["cleanup_flags"][i]["default"] for i in (1, 2)} == flags
    assert call["cleanup_flags"][0]["default"] is False  # video: source had none
    assert call["eng_srt"] is None                # cleanup mode
    assert call["keep_subtitle_indices"] == []    # fre sub dropped
    assert r.tag.call_count == 0                  # must NOT stamp CONSOLIDATED_SUBS
    r.norm.assert_not_called()                    # never normalize from here


def test_only_forced_english_branch_also_cleans_without_dropping_audio(tmp_path):
    """FAIL "only forced English" runs the same cleanup and stays FAIL; the forced English track is kept
    with its forced flag re-emitted, the French sub dropped, French default audio loses `default`."""
    r = _run(tmp_path, [_v(), _a(1, "fre", default=1), _a(2, "eng", "aac"),
                        _s(3, "eng", title="Forced", forced=1), _s(4, "fre")],
             verify=_verify_streams(audio_defaults=(False, True), subs=1), arr_lang="eng",
             extract_track=dict(side_effect=_forced_english_extract))

    assert r.result["status"] == "FAIL"
    assert r.result["detail"].startswith("only forced English")
    assert "cleaned" in r.result["detail"]
    assert len(r.calls) == 1
    call = r.calls[0]
    assert call["audio_idx"] == [1, 2]          # ORIGINAL order, BOTH kept
    assert call["cleanup_flags"][2]["default"] is True   # eng gets default, by ID
    assert call["keep_subtitle_indices"] == [3]
    assert call["cleanup_flags"][3] == {"default": False, "forced": True}
    assert r.tag.call_count == 0


def _timed_probe(media, streams, verify, src_duration, out_duration):
    """A `probe` fake giving the source `streams` a container duration and `verify` (the cleanup output) its own."""
    def fake(p):
        if Path(p) == media:
            return {"streams": streams, "format": {"duration": src_duration}}
        return {**verify, "format": {"duration": out_duration}}
    return fake


_DUB_STREAMS = [_v(), _a(1, "fre", default=1), _a(2, "eng", "aac"), _s(3, "fre")]


def _run_timed_cleanup(tmp_path, src_duration, out_duration):
    media = tmp_path / "Movie (2024).mkv"
    return _run(tmp_path, _DUB_STREAMS, arr_lang="eng",
                probe=dict(side_effect=_timed_probe(
                    media, _DUB_STREAMS, _verify_streams(audio_defaults=(False, True)),
                    src_duration, out_duration)))


def test_cleanup_remux_shorter_than_the_source_fails_and_leaves_the_original(tmp_path):
    """A truncated cleanup mux (22 min in, 2:49 out) with otherwise correct counts and flags: FAIL, nothing swapped."""
    r = _run_timed_cleanup(tmp_path, "1320.0", "169.0")

    assert r.result["status"] == "FAIL" and r.result["detail"].startswith("duration verify: ")
    assert r.media.read_bytes() == b"\x1a\x45\xdf\xa3"           # original untouched
    assert not list(tmp_path.glob("*.tmp.mkv"))                  # tmp output removed
    assert r.tag.call_count == 0
    r.norm.assert_not_called()


@pytest.mark.parametrize("src_duration, out_duration", [
    ("1320.0", "1320.0"), (None, "169.0"),
], ids=["equal", "source-duration-missing"])
def test_cleanup_remux_as_long_as_the_source_or_unchecked_swaps(tmp_path, src_duration, out_duration):
    r = _run_timed_cleanup(tmp_path, src_duration, out_duration)

    assert r.result["status"] == "NEEDS_BAZARR" and "cleaned" in r.result["detail"]
    assert r.media.read_bytes() == b"CLEANED"


@pytest.mark.parametrize("name, streams, status, detail_prefix", [
    ("Movie (2024).mkv", [_v(), _a(1, "eng")], "NEEDS_BAZARR", ""),
    ("Godzilla Minus One (2023).mkv",       # the confident pick (jpn) is already the only default
     [_v(), _a(1, "jpn", "aac", default=1), _a(2, "eng")], "NEEDS_BAZARR", ""),
    ("Movie (2024).mkv", [_v(), _a(1, "eng"), _s(2, "eng", title="Forced", forced=1)],
     "FAIL", "only forced English"),
], ids=["single-english-audio", "confident-pick-already-sole-default", "forced-english-alone"])
def test_nothing_to_clean_triggers_no_remux(tmp_path, name, streams, status, detail_prefix):
    r = _run(tmp_path, streams, name=name, extract_track=dict(side_effect=_forced_english_extract))
    assert r.result["status"] == status
    assert r.result["detail"].startswith(detail_prefix)
    assert "nothing to clean" in r.result["detail"]
    assert r.calls == []


def test_dry_run_reports_plan_without_mutating(tmp_path):
    """dry_run=True reports what WOULD be cleaned but calls neither remux nor the tag/normalize steps."""
    r = _run(tmp_path, [_v(), _a(1, "fre", default=1), _a(2, "eng", "aac"), _s(3, "fre")],
             dry_run=True)

    assert r.result["status"] == "NEEDS_BAZARR"
    assert "dry-run" in r.result["detail"]
    assert "audio_default_change" in r.result["detail"]
    assert r.calls == []
    assert r.tag.call_count == 0
    assert r.norm.call_count == 0
    assert r.media.read_bytes() == b"\x1a\x45\xdf\xa3"


def test_cleanup_drops_foreign_sub_and_never_normalizes(tmp_path):
    """Loudness normalization never runs from the cleanup, even on a successful swap: it can re-encode audio
    and ffmpeg auto-marks a stream default when none is flagged, re-flagging audio outside
    confident_default_audio's gate."""
    r = _run(tmp_path, [_v(), _a(1, "eng"), _s(2, "ger")],
             verify=_verify_streams(audio_defaults=(False,)))

    assert r.result["status"] == "NEEDS_BAZARR"
    assert "cleaned" in r.result["detail"]
    assert len(r.calls) == 1
    assert r.calls[0]["eng_srt"] is None                      # cleanup mode, no external srt
    assert r.calls[0]["audio_idx"] == [1]                      # the eng audio track kept
    assert 2 not in (r.calls[0]["keep_subtitle_indices"] or [])  # German sub dropped
    r.norm.assert_not_called()


# --- default flag only, no physical reorder ---

@pytest.mark.parametrize("streams, verify_defaults, pick, other", [
    # both flagged default: needs fixing (two defaults is invalid Matroska), by flag per ID
    ([_v(), _a(1, "jpn", "aac", default=1), _a(2, "eng", default=1)], (True, False), 1, 2),
    # nothing flagged and the confident pick (jpn) is not physically first: still fixed by flag, not by reordering
    ([_v(), _a(1, "eng"), _a(2, "jpn", "aac")], (False, True), 2, 1),
], ids=["two-streams-flagged-default", "nothing-flagged"])
def test_confident_pick_gets_the_default_flag_without_physical_reorder(
        tmp_path, streams, verify_defaults, pick, other):
    """DUAL_AUDIO_KEYWORDS title: jpn is confidently primary."""
    r = _run(tmp_path, streams, name="Godzilla Minus One (2023).mkv",
             verify=_verify_streams(audio_defaults=verify_defaults))

    assert r.result["status"] == "NEEDS_BAZARR"
    assert len(r.calls) == 1          # cleanup DID run
    call = r.calls[0]
    assert call["audio_idx"] == [1, 2]        # original order, both kept
    assert call["cleanup_flags"][pick]["default"] is True
    assert call["cleanup_flags"][other]["default"] is False


# --- the cleanup must not touch video at all ---

def test_cover_art_also_survives_cleanup(tmp_path):
    """Cover art survives the cleanup like any other video stream (its removal is the full path's job)."""
    r = _run(tmp_path, [_v(), _v(1, "mjpeg"), _a(2, "eng", default=1), _s(3, "fre")],
             verify=_verify_streams(video_defaults=(False, False), audio_defaults=(True,)))

    assert r.result["status"] == "NEEDS_BAZARR"
    assert len(r.calls) == 1
    call = r.calls[0]
    assert set(call["video_idx"]) == {0, 1}   # BOTH kept, cover art included
    assert call["cleanup_flags"][1]["default"] is False  # cover art's own flag, mirrored


# --- only well-formed, non-special, non-English codes are "positively foreign" ---

@pytest.mark.parametrize("subs, foreign", [
    # mul and zxx are ambiguous, not positively foreign
    ([{"tags": {"language": lang}} for lang in ("mul", "zxx")], False),
    ([{"tags": {"language": lang}} for lang in ("fre", "fr-CA")], True),
], ids=["special-codes-kept", "well-formed-foreign-dropped"])
def test_positively_foreign_sub_classification(subs, foreign):
    for sub in subs:
        assert cs._is_positively_foreign_sub(sub) is foreign, sub


def test_untagged_and_und_subtitles_survive_cleanup(tmp_path):
    """Untagged and "und" subtitles might be English, so both are kept; only the clearly foreign German track is dropped."""
    r = _run(tmp_path, [_v(), _a(1, "eng", default=1), _s(2), _s(3, "und"), _s(4, "ger")],
             verify=_verify_streams(audio_defaults=(True,), subs=2))

    assert r.result["status"] == "NEEDS_BAZARR"
    assert len(r.calls) == 1
    kept = set(r.calls[0]["keep_subtitle_indices"] or [])
    assert {2, 3} <= kept           # untagged + "und" survive
    assert 4 not in kept            # German is positively foreign, dropped


def test_english_image_subs_preserved_by_cleanup_helper(tmp_path):
    """English image subtitles are never dropped (a later subliminal OCR source); only foreign subs are."""
    subs = [_s(2, "fre"), _s(3, "eng", "hdmv_pgs_subtitle")]
    d = _cleanup_direct(_media(tmp_path), subs=subs, audio=[_a(1, "eng")], eng_image=[subs[1]],
                        verify=_verify_streams(audio_defaults=(False,), subs=1))

    assert d.result["status"] == "NEEDS_BAZARR"
    assert "cleaned" in d.result["detail"]
    assert set(d.calls[0]["keep_subtitle_indices"] or []) == {3}   # only the eng image sub kept, fre dropped
    assert d.tag.call_count == 0


# --- verify counts AND per-type default-flag pattern ---

_FRE_DEFAULT_AND_ENG = [_a(1, "fre", default=1), _a(2, "eng", "aac")]


@pytest.mark.parametrize("verify, detail_fragment", [
    # audio is never dropped, so the verify expects the ORIGINAL audio count (2), not 1
    (_verify_streams(audio_defaults=(True,)), "cleanup verify"),
    # counts match but mkvmerge auto-picked another video default instead of the source-mirrored False
    (_verify_streams(video_defaults=(True,), audio_defaults=(True, False)), "default-flag mismatch"),
], ids=["track-count-mismatch", "default-flag-mismatch"])
def test_cleanup_verify_mismatch_leaves_original_untouched(tmp_path, verify, detail_fragment):
    media = _media(tmp_path)
    d = _cleanup_direct(media, subs=[_s(3, "fre")], audio=_FRE_DEFAULT_AND_ENG, verify=verify)

    assert d.result["status"] == "FAIL"
    assert "cleanup verify" in d.result["detail"]
    assert detail_fragment in d.result["detail"]
    assert media.read_bytes() == b"\x1a\x45\xdf\xa3"   # original untouched
    assert not any(p.name.endswith(".tmp.mkv") for p in media.parent.iterdir() if p != media)
    assert d.tag.call_count == 0
    assert d.norm.call_count == 0               # never reached normalize/swap


def test_cleanup_verify_match_allows_swap(tmp_path):
    """With the intended counts AND default-flag pattern the swap proceeds, and normalization still never runs."""
    media = _media(tmp_path)
    d = _cleanup_direct(media, subs=[_s(3, "fre")], audio=_FRE_DEFAULT_AND_ENG,
                        verify=_verify_streams(audio_defaults=(True, False)))

    assert d.result["status"] == "NEEDS_BAZARR"
    assert "cleaned" in d.result["detail"]
    assert media.read_bytes() == b"CLEANED"      # swap happened
    assert d.tag.call_count == 0                 # still no CONSOLIDATED_SUBS
    d.norm.assert_not_called()                   # never normalize
