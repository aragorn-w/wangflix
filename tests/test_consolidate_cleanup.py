"""Tests for the DEFECT 1 fix: the AUDIO-AND-SUBTITLE CLEANUP REMUX that
runs when there is no main English subtitle to consolidate onto yet
(`_cleanup_no_main_english`, called from `_process_locked`'s FOUR "no
main English subtitle yet" call sites).

Without this, a MULTI release with e.g. French default audio + English
second audio + French-only subs kept French as the default audio and
every foreign subtitle track for as long as Bazarr took to find an
English sub.

GOVERNING PRINCIPLE under test throughout (codex rounds 2-4): the
cleanup must never DELETE a track that might be wanted, and must never
silently change a flag it didn't mean to.
  - Video is NOT TOUCHED AT ALL (round 4 #1) -- not dropped, not
    reduced, cover art included; that stays the FULL path's job.
  - Audio is NEVER dropped and NEVER physically reordered -- only the
    `default` flag may change, and only when `confident_default_audio`
    says so with actual confidence.
  - Subtitles: only POSITIVELY foreign tags are dropped (ASCII-only
    2-3 letter codes, excluding English/special/private-use codes).
  - EXACT flag preservation (round 4 #2): every kept track of every
    type gets an EXPLICIT default flag (and, for subtitles, forced
    flag) mirroring its source disposition -- mkvmerge does NOT leave
    a track type's default alone when the source has none for it.

No real ffmpeg/mkvmerge: `remux`/`_normalize_audio_inline`/
`set_consolidated_tag`/`probe` (for the post-remux verify step) are
mocked.  Media files all live under `tmp_path`, which is never under
`media_stack.paths.MEDIA_ROOT`, so `original_language_for` short-circuits
to None without any network call UNLESS a test explicitly patches it
(simulating a Radarr/Sonarr answer) — see test_arrlang.py for arrlang's
own tests.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "consolidate_subs_cleanup", str(PROJECT_ROOT / "consolidate-subs.py")
)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)


def _media(tmp_path: Path, name: str = "Movie (2024).mkv") -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x1a\x45\xdf\xa3")
    return p


def _verify_streams(*, video_defaults=(False,), audio_defaults=(), subs: int = 0) -> dict:
    """A probe()-shaped dict for the post-cleanup-remux verify step: one
    video stream per entry in `video_defaults`, one audio stream per
    entry in `audio_defaults` (each entry is that stream's own
    disposition.default), plus `subs` bare subtitle streams."""
    streams = [{"codec_type": "video", "disposition": {"default": int(bool(d))}}
               for d in video_defaults]
    streams += [{"codec_type": "audio", "disposition": {"default": int(bool(d))}}
                for d in audio_defaults]
    streams += [{"codec_type": "subtitle"} for _ in range(subs)]
    return {"streams": streams}


def _probe_side_effect(media: Path, original: dict, verify: dict):
    """probe() is called on the ORIGINAL path first, then on the cleanup
    tmp output to verify it — this builds a side_effect returning the
    right one for each."""
    def _fn(p):
        return original if Path(p) == media else verify
    return _fn


def _record_remux(calls: list):
    """A fake `remux()` that records every call (including
    `cleanup_flags`) and writes a marker file."""
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


# ---------------------------------------------------------------------------
# A: audio is never deleted or reordered; video is never touched at all;
# EXACT flags are re-emitted for every kept track (codex rounds 2, 3, 4)
# ---------------------------------------------------------------------------


def test_foreign_default_audio_confidently_defaulted_both_kept(tmp_path):
    """French default audio + English second audio + French-only subs,
    no English subtitle candidate anywhere, Radarr says this title is
    English-original -> ONE cleanup remux: BOTH audio tracks survive in
    their ORIGINAL order (French is NOT deleted, NOT reordered); English
    gets the default flag explicitly, by ID; video is untouched; French
    sub dropped; no CONSOLIDATED_SUBS tag write; status stays
    NEEDS_BAZARR."""
    media = _media(tmp_path)
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "fre"}, "disposition": {"default": 1}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac",
         "tags": {"language": "eng"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "fre"}},
    ]
    calls: list = []

    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", side_effect=_probe_side_effect(
             media, {"streams": streams},
             _verify_streams(video_defaults=(False,), audio_defaults=(False, True), subs=0))), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "original_language_for", return_value="eng"), \
         patch.object(cs, "remux", side_effect=_record_remux(calls)), \
         patch.object(cs, "set_consolidated_tag") as mock_tag, \
         patch.object(cs, "_normalize_audio_inline") as mock_norm:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "NEEDS_BAZARR"
    assert "cleaned" in result["detail"]
    assert len(calls) == 1
    call = calls[0]
    assert call["video_idx"] == [0]
    assert call["audio_idx"] == [1, 2]            # ORIGINAL order, NEVER reordered
    assert call["cleanup_flags"][1]["default"] is False
    assert call["cleanup_flags"][2]["default"] is True   # eng gets default, by ID
    assert call["cleanup_flags"][0]["default"] is False  # video: source had none
    assert call["eng_srt"] is None                # cleanup mode
    assert call["keep_subtitle_indices"] == []    # fre sub dropped
    assert mock_tag.call_count == 0        # must NOT stamp CONSOLIDATED_SUBS
    mock_norm.assert_not_called()           # Fix 4 (round 3): never normalize from here


def test_no_confidence_signal_leaves_audio_completely_untouched(tmp_path):
    """Same French-default/English-second shape, but NO original_language
    signal available (Radarr unreachable) and no keyword/disposition
    confidence either -- confident_default_audio returns None, so the
    cleanup_flags it builds PRESERVE French's own default=True exactly
    (not "reset to no"). Only the foreign subtitle is cleaned."""
    media = _media(tmp_path)
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "fre"}, "disposition": {"default": 1}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac",
         "tags": {"language": "eng"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "fre"}},
    ]
    calls: list = []

    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", side_effect=_probe_side_effect(
             media, {"streams": streams},
             _verify_streams(video_defaults=(False,), audio_defaults=(True, False), subs=0))), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "remux", side_effect=_record_remux(calls)), \
         patch.object(cs, "set_consolidated_tag") as mock_tag, \
         patch.object(cs, "_normalize_audio_inline"):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "NEEDS_BAZARR"
    assert len(calls) == 1
    call = calls[0]
    assert call["audio_idx"] == [1, 2]              # unchanged order, BOTH kept
    assert call["cleanup_flags"][1]["default"] is True   # fre's OWN default, preserved
    assert call["cleanup_flags"][2]["default"] is False
    assert mock_tag.call_count == 0


def test_only_forced_english_branch_also_cleans_without_dropping_audio(tmp_path):
    """The FAIL "only forced English" branch runs the same cleanup —
    status stays FAIL (its original exit-code mapping is unchanged).
    Radarr says this title is English-original, so the French default
    audio loses the default flag (but is NOT deleted or reordered); the
    unrelated foreign subtitle is dropped; the forced English text
    track is kept, with its OWN forced flag re-emitted explicitly."""
    media = _media(tmp_path)
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "fre"}, "disposition": {"default": 1}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac",
         "tags": {"language": "eng"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "eng", "title": "Forced"},
         "disposition": {"forced": 1}},
        {"index": 4, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "fre"}},
    ]
    calls: list = []

    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", side_effect=_probe_side_effect(
             media, {"streams": streams},
             _verify_streams(video_defaults=(False,), audio_defaults=(False, True), subs=1))), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "original_language_for", return_value="eng"), \
         patch.object(cs, "remux", side_effect=_record_remux(calls)), \
         patch.object(cs, "set_consolidated_tag") as mock_tag, \
         patch.object(cs, "_normalize_audio_inline"):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FAIL"
    assert "cleaned" in result["detail"]
    assert len(calls) == 1
    call = calls[0]
    assert call["audio_idx"] == [1, 2]          # ORIGINAL order, BOTH kept
    assert call["cleanup_flags"][2]["default"] is True   # eng gets default, by ID
    assert call["keep_subtitle_indices"] == [3]
    assert call["cleanup_flags"][3] == {"default": False, "forced": True}
    assert mock_tag.call_count == 0


def test_already_clean_file_triggers_no_remux(tmp_path):
    """Single, correctly-default-flagged English audio track + a kept
    forced English text track and nothing else -> nothing to clean, no
    remux attempted."""
    media = _media(tmp_path)
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "eng"}, "disposition": {"default": 1}},
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "eng", "title": "Forced"},
         "disposition": {"forced": 1}},
    ]
    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", return_value={"streams": streams}), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "remux") as mock_remux:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FAIL"
    assert "nothing to clean" in result["detail"]
    assert mock_remux.call_count == 0


def test_dry_run_reports_plan_without_mutating(tmp_path):
    """dry_run=True must report what WOULD be cleaned but call neither
    remux nor the tag/normalize steps, and must not touch the file."""
    media = _media(tmp_path)
    original_bytes = media.read_bytes()
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "fre"}, "disposition": {"default": 1}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac",
         "tags": {"language": "eng"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "fre"}},
    ]
    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", return_value={"streams": streams}), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "remux") as mock_remux, \
         patch.object(cs, "set_consolidated_tag") as mock_tag, \
         patch.object(cs, "_normalize_audio_inline") as mock_norm:
        result = cs._process_locked(media, str(media), dry_run=True)

    assert result["status"] == "NEEDS_BAZARR"
    assert "dry-run" in result["detail"]
    assert "audio_default_change" in result["detail"]
    assert mock_remux.call_count == 0
    assert mock_tag.call_count == 0
    assert mock_norm.call_count == 0
    assert media.read_bytes() == original_bytes


def test_french_original_spanish_dub_no_english_keeps_both_audio_tracks(tmp_path):
    """No eng/jpn/kor signal at all (French original + Spanish dub, no
    English track, no Arr answer) -- confident_default_audio has nothing
    to decide, so audio's OWN default flags are mirrored exactly
    (Spanish's existing default=True is preserved, not changed); only
    the foreign subtitle is cleaned."""
    media = _media(tmp_path)
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "fre"}, "disposition": {"original": 1}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac",
         "tags": {"language": "spa"}, "disposition": {"default": 1}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "fre"}},
    ]
    calls: list = []

    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", side_effect=_probe_side_effect(
             media, {"streams": streams},
             _verify_streams(video_defaults=(False,), audio_defaults=(False, True), subs=0))), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "remux", side_effect=_record_remux(calls)), \
         patch.object(cs, "set_consolidated_tag") as mock_tag, \
         patch.object(cs, "_normalize_audio_inline"):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "NEEDS_BAZARR"
    assert len(calls) == 1
    call = calls[0]
    assert call["audio_idx"] == [1, 2]              # BOTH kept, untouched order
    assert call["cleanup_flags"][1]["default"] is False  # fre: source had none
    assert call["cleanup_flags"][2]["default"] is True   # spa: source default PRESERVED
    assert mock_tag.call_count == 0


def test_only_image_subs_subliminal_failure_cleans_without_dropping_audio(tmp_path):
    """A file that HAS an English image subtitle (so the main
    early-return gate does NOT fire — it requires `not eng_image`) but no
    text candidate and no sidecar, whose subliminal fetch then fails,
    hits the SAME cleanup as the main gate.  Exercised through the real
    `_process_locked` gate.  No Arr signal here -- audio untouched."""
    media = _media(tmp_path)
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "fre"}, "disposition": {"default": 1}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac",
         "tags": {"language": "eng"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
         "tags": {"language": "eng"}},
        {"index": 4, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "fre"}},
    ]
    calls: list = []

    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", side_effect=_probe_side_effect(
             media, {"streams": streams},
             _verify_streams(video_defaults=(False,), audio_defaults=(True, False), subs=1))), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "fetch_via_subliminal", return_value=None), \
         patch.object(cs, "remux", side_effect=_record_remux(calls)), \
         patch.object(cs, "set_consolidated_tag") as mock_tag, \
         patch.object(cs, "_normalize_audio_inline"):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "NEEDS_BAZARR"
    assert "only image subs" in result["detail"]
    assert "cleaned" in result["detail"]
    assert len(calls) == 1
    call = calls[0]
    assert call["audio_idx"] == [1, 2]                        # BOTH kept, untouched
    assert call["cleanup_flags"][1]["default"] is True        # fre default PRESERVED
    assert call["keep_subtitle_indices"] == [3]                # eng image sub kept
    assert mock_tag.call_count == 0


def test_all_extracts_failed_branch_also_cleans(tmp_path):
    """Every English text/sidecar candidate failing extraction (and no
    image candidate) must also run the cleanup — status stays FAIL,
    detail gains the cleanup summary. Exercised through the real gate: a
    main English text candidate exists (so the early gate does NOT
    fire) but extract_track fails for it. The failed-extraction eng
    text track is NOT positively foreign -- it survives the cleanup."""
    media = _media(tmp_path)
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "fre"}, "disposition": {"default": 1}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac",
         "tags": {"language": "eng"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "eng"}},
        {"index": 4, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "fre"}},
    ]
    calls: list = []

    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", side_effect=_probe_side_effect(
             media, {"streams": streams},
             _verify_streams(video_defaults=(False,), audio_defaults=(False, True), subs=1))), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "extract_track", return_value=False), \
         patch.object(cs, "original_language_for", return_value="eng"), \
         patch.object(cs, "remux", side_effect=_record_remux(calls)), \
         patch.object(cs, "set_consolidated_tag") as mock_tag, \
         patch.object(cs, "_normalize_audio_inline"):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FAIL"
    assert "all extracts failed" in result["detail"]
    assert "cleaned" in result["detail"]
    assert len(calls) == 1
    call = calls[0]
    assert call["audio_idx"] == [1, 2]
    assert call["cleanup_flags"][2]["default"] is True   # eng gets default, by ID
    assert call["keep_subtitle_indices"] == [3]           # idx3 (eng) kept, idx4 (fre) dropped
    assert mock_tag.call_count == 0


def test_cleanup_never_calls_normalize_audio_inline(tmp_path):
    """Loudness normalization must NEVER run from this cleanup path,
    even on a successful swap -- it can re-encode audio and ffmpeg
    auto-marks a stream default when none is flagged, either of which
    would re-flag audio outside confident_default_audio's gate.
    Normalization stays a FULL-pass-only step."""
    media = _media(tmp_path)
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "eng"}},
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "ger"}},
    ]
    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", side_effect=_probe_side_effect(
             media, {"streams": streams},
             _verify_streams(video_defaults=(False,), audio_defaults=(False,), subs=0))), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "remux", side_effect=_record_remux([])), \
         patch.object(cs, "set_consolidated_tag"), \
         patch.object(cs, "_normalize_audio_inline") as mock_norm:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "NEEDS_BAZARR"
    mock_norm.assert_not_called()


# --- Fix 5 (round 3, verified still correct under round 4): default-flag-
# only, no physical reorder -----------------------------------------------

def test_two_streams_flagged_default_sets_explicit_default_both_kept(tmp_path):
    """DUAL_AUDIO_KEYWORDS title (jpn confidently primary via keyword) —
    BOTH jpn and eng wrongly flagged default. Needs fixing (two defaults
    is invalid Matroska) but nothing is dropped OR reordered — the
    default flag is set per-ID."""
    media = _media(tmp_path, "Godzilla Minus One (2023).mkv")
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac",
         "tags": {"language": "jpn"}, "disposition": {"default": 1}},
        {"index": 2, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "eng"}, "disposition": {"default": 1}},
    ]
    calls: list = []

    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", side_effect=_probe_side_effect(
             media, {"streams": streams},
             _verify_streams(video_defaults=(False,), audio_defaults=(True, False), subs=0))), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "remux", side_effect=_record_remux(calls)), \
         patch.object(cs, "set_consolidated_tag"), \
         patch.object(cs, "_normalize_audio_inline"):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "NEEDS_BAZARR"
    assert len(calls) == 1          # cleanup DID run
    call = calls[0]
    assert call["audio_idx"] == [1, 2]        # original order, both kept
    assert call["cleanup_flags"][1]["default"] is True    # jpn (confident pick)
    assert call["cleanup_flags"][2]["default"] is False


def test_nothing_flagged_confident_sets_default_without_physical_reorder(tmp_path):
    """Nothing is flagged default anywhere; the confident pick (jpn, via
    DUAL_AUDIO_KEYWORDS) isn't physically first in the file. Under the
    sharpened rule this still needs fixing (the ONLY "already correct"
    case is "already the sole flagged default" — nothing flagged is
    never that), but the fix is setting the default flag by ID, NOT
    reordering the track list."""
    media = _media(tmp_path, "Godzilla Minus One (2023).mkv")
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "eng"}},   # physically first, no disposition
        {"index": 2, "codec_type": "audio", "codec_name": "aac",
         "tags": {"language": "jpn"}},   # physically second, no disposition
    ]
    calls: list = []

    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", side_effect=_probe_side_effect(
             media, {"streams": streams},
             _verify_streams(video_defaults=(False,), audio_defaults=(False, True), subs=0))), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "remux", side_effect=_record_remux(calls)), \
         patch.object(cs, "set_consolidated_tag"), \
         patch.object(cs, "_normalize_audio_inline"):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "NEEDS_BAZARR"
    assert len(calls) == 1
    call = calls[0]
    assert call["audio_idx"] == [1, 2]        # NOT reordered -- eng stays first
    assert call["cleanup_flags"][2]["default"] is True    # jpn gets default, by ID only
    assert call["cleanup_flags"][1]["default"] is False


def test_confident_pick_already_sole_default_skips_audio_change(tmp_path):
    """The "already correct" guard per codex round 3 #5: the confident
    pick is already the ONLY default-flagged audio stream -> no audio
    override at all, and with nothing else to clean, no remux at all."""
    media = _media(tmp_path, "Godzilla Minus One (2023).mkv")
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac",
         "tags": {"language": "jpn"}, "disposition": {"default": 1}},
        {"index": 2, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "eng"}},
    ]
    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", return_value={"streams": streams}), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "remux") as mock_remux:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "NEEDS_BAZARR"
    assert "nothing to clean" in result["detail"]
    assert mock_remux.call_count == 0


# --- Fix 1 (round 4): the cleanup must NOT touch video AT ALL --------------

def test_cover_art_also_survives_cleanup(tmp_path):
    """Codex round 4 #1: cover-art removal stays the FULL path's job.
    A cover-art mjpeg stream survives the cleanup untouched, exactly
    like any other video stream -- nothing about video is dropped,
    reduced, or reflagged beyond re-emitting its own source default."""
    media = _media(tmp_path)
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "video", "codec_name": "mjpeg"},  # cover art
        {"index": 2, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "eng"}, "disposition": {"default": 1}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "fre"}},
    ]
    calls: list = []

    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", side_effect=_probe_side_effect(
             media, {"streams": streams},
             _verify_streams(video_defaults=(False, False), audio_defaults=(True,), subs=0))), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "remux", side_effect=_record_remux(calls)), \
         patch.object(cs, "set_consolidated_tag"), \
         patch.object(cs, "_normalize_audio_inline"):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "NEEDS_BAZARR"
    assert len(calls) == 1
    call = calls[0]
    assert set(call["video_idx"]) == {0, 1}   # BOTH kept, cover art included
    assert call["cleanup_flags"][1]["default"] is False  # cover art's own flag, mirrored


def test_cover_art_alone_does_not_trigger_cleanup(tmp_path):
    """Cover art is no longer the cleanup's concern at all (round 4 #1)
    -- its mere presence, with nothing else wrong, must NOT trigger a
    remux (contrast with round 3, where dropping it alone used to
    count as "something to clean")."""
    media = _media(tmp_path)
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "video", "codec_name": "mjpeg"},
        {"index": 2, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "eng"}, "disposition": {"default": 1}},
    ]
    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", return_value={"streams": streams}), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "remux") as mock_remux:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "NEEDS_BAZARR"
    assert "nothing to clean" in result["detail"]
    assert mock_remux.call_count == 0


# --- Fix 2 (round 3, extended round 4 #3): only well-formed, ASCII,
# non-special, non-English codes are "positively foreign" ------------------

def test_special_and_private_use_subtitle_codes_survive_cleanup():
    """mul (multiple languages), und-Latn (unknown + script subtag), zxx
    (no linguistic content), and qaa (private-use range start) must all
    be treated as ambiguous, not positively foreign -- kept."""
    for lang in ("mul", "und-Latn", "zxx", "qaa"):
        assert cs._is_positively_foreign_sub({"tags": {"language": lang}}) is False, lang


def test_well_formed_foreign_codes_are_dropped():
    """fre, spa, jpn (plain 2-3 letter codes) and fr-CA (regional
    variant) are well-formed, non-English, non-special -- positively
    foreign, droppable."""
    for lang in ("fre", "spa", "jpn", "fr-CA"):
        assert cs._is_positively_foreign_sub({"tags": {"language": lang}}) is True, lang


def test_unknown_shapes_and_empty_tag_survive_cleanup():
    """An untagged subtitle and an unrecognizable shape (too long, not
    alphabetic) are ambiguous -- never guessed at, always kept."""
    for s in ({}, {"tags": {}}, {"tags": {"language": ""}},
              {"tags": {"language": "englishified"}}, {"tags": {"language": "1"}}):
        assert cs._is_positively_foreign_sub(s) is False, s


def test_non_ascii_letters_are_kept_not_treated_as_foreign():
    """Codex round 4 #3: Python's str.isalpha() is Unicode-aware and
    would wrongly accept a non-ASCII "letter" sequence like "éé" as a
    well-formed 2-letter code. The base-subtag check must be ASCII-only
    (`^[a-z]{2,3}$` after lowercasing) -- kept, not guessed at."""
    assert cs._is_positively_foreign_sub({"tags": {"language": "éé"}}) is False
    assert cs._is_positively_foreign_sub({"tags": {"language": "日本語"}}) is False


def test_untagged_and_und_subtitles_survive_cleanup(tmp_path):
    """An untagged subtitle (no language tag at all) and one explicitly
    tagged "und" might be English — neither is POSITIVELY foreign, so
    the cleanup must keep both.  Only the clearly-foreign German track
    is dropped."""
    media = _media(tmp_path)
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "eng"}, "disposition": {"default": 1}},
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip"},  # no language tag
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "und"}},
        {"index": 4, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "ger"}},
    ]
    calls: list = []

    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", side_effect=_probe_side_effect(
             media, {"streams": streams},
             _verify_streams(video_defaults=(False,), audio_defaults=(True,), subs=2))), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "remux", side_effect=_record_remux(calls)), \
         patch.object(cs, "set_consolidated_tag"), \
         patch.object(cs, "_normalize_audio_inline"):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "NEEDS_BAZARR"
    assert len(calls) == 1
    kept = set(calls[0]["keep_subtitle_indices"] or [])
    assert {2, 3} <= kept           # untagged + "und" survive
    assert 4 not in kept            # German is positively foreign, dropped


def test_regional_english_subtitle_recognized_as_candidate_not_needs_bazarr(tmp_path):
    """An en-US tagged subtitle is a legitimate English candidate — a
    file with only one, no other subs, must NOT hit the "no main
    English subtitle" gate at all (dry-run smoke: reports "would FIX",
    not NEEDS_BAZARR)."""
    media = _media(tmp_path)
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "eng"}},
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "en-US"}},
    ]
    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", return_value={"streams": streams}), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]):
        result = cs._process_locked(media, str(media), dry_run=True)

    assert result["status"] == "SKIP"
    assert "would FIX" in result["detail"]
    assert "embedded=1" in result["detail"]


def test_regional_english_subtitle_never_dropped_by_cleanup_foreign_check(tmp_path):
    """Direct helper test pinning `_is_positively_foreign_sub`'s use of
    `is_english_tag`: an en-US subtitle handed to the cleanup (as if it
    had somehow not been classified as a main candidate upstream) must
    never be treated as positively foreign."""
    media = _media(tmp_path)
    sub_streams = [
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "en-US"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "ger"}},
    ]
    audio_streams = [
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "eng"}},
    ]
    video_streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"}]
    calls: list = []

    with patch.object(cs, "remux", side_effect=_record_remux(calls)), \
         patch.object(cs, "probe", return_value=_verify_streams(
             video_defaults=(False,), audio_defaults=(False,), subs=1)), \
         patch.object(cs, "set_consolidated_tag") as mock_tag, \
         patch.object(cs, "_normalize_audio_inline"):
        result = cs._cleanup_no_main_english(
            media, str(media), "NEEDS_BAZARR", "no English candidates",
            sub_streams, audio_streams, video_streams, None, [], False,
        )

    assert result["status"] == "NEEDS_BAZARR"
    assert len(calls) == 1
    kept = set(calls[0]["keep_subtitle_indices"] or [])
    assert 2 in kept       # en-US survives
    assert 3 not in kept   # German dropped
    assert mock_tag.call_count == 0


def test_english_image_subs_preserved_by_cleanup_helper(tmp_path):
    """English image-subtitle streams must never be dropped by the
    cleanup (they're a later subliminal OCR source) — only foreign subs
    are."""
    media = _media(tmp_path)
    sub_streams = [
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "fre"}},
        {"index": 3, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
         "tags": {"language": "eng"}},
    ]
    audio_streams = [
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "eng"}},
    ]
    video_streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"}]
    eng_image = [sub_streams[1]]
    calls: list = []

    with patch.object(cs, "remux", side_effect=_record_remux(calls)), \
         patch.object(cs, "probe", return_value=_verify_streams(
             video_defaults=(False,), audio_defaults=(False,), subs=1)), \
         patch.object(cs, "set_consolidated_tag") as mock_tag, \
         patch.object(cs, "_normalize_audio_inline"):
        result = cs._cleanup_no_main_english(
            media, str(media), "NEEDS_BAZARR", "no English candidates",
            sub_streams, audio_streams, video_streams, None, eng_image, False,
        )

    assert result["status"] == "NEEDS_BAZARR"
    assert "cleaned" in result["detail"]
    kept = set(calls[0]["keep_subtitle_indices"] or [])
    assert kept == {3}           # only the eng image sub kept, fre dropped
    assert mock_tag.call_count == 0


# --- Fix 6 / round 4 #4: verify counts AND per-type default-flag pattern ---

def test_cleanup_verify_count_mismatch_leaves_original_untouched(tmp_path):
    """If the post-remux probe doesn't show the TRACK COUNTS we intended
    to keep, the tmp output is deleted and FAIL is returned WITHOUT
    touching the original file.  Expected audio count is the ORIGINAL
    count (2 — audio is never dropped); the mock probe reports 1."""
    media = _media(tmp_path)
    original_bytes = media.read_bytes()
    video_streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"}]
    audio_streams = [
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "fre"}, "disposition": {"default": 1}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac",
         "tags": {"language": "eng"}},
    ]
    sub_streams = [
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "fre"}},
    ]

    with patch.object(cs, "remux", side_effect=_record_remux([])), \
         patch.object(cs, "probe", return_value=_verify_streams(
             video_defaults=(False,), audio_defaults=(True,), subs=0)), \
         patch.object(cs, "original_language_for", return_value=None), \
         patch.object(cs, "set_consolidated_tag") as mock_tag, \
         patch.object(cs, "_normalize_audio_inline") as mock_norm:
        result = cs._cleanup_no_main_english(
            media, str(media), "NEEDS_BAZARR", "no English candidates",
            sub_streams, audio_streams, video_streams, None, [], False,
        )

    assert result["status"] == "FAIL"
    assert "cleanup verify" in result["detail"]
    assert media.read_bytes() == original_bytes   # original untouched
    assert not any(p.name.endswith(".tmp.mkv") for p in media.parent.iterdir() if p != media)
    assert mock_tag.call_count == 0
    assert mock_norm.call_count == 0               # never reached normalize/swap


def test_cleanup_verify_default_flag_mismatch_leaves_original_untouched(tmp_path):
    """Codex round 4 #4: counts can match while the per-track default-
    flag PATTERN doesn't (e.g. mkvmerge auto-picked a different video
    default than intended). Must still FAIL and leave the original
    untouched."""
    media = _media(tmp_path)
    original_bytes = media.read_bytes()
    video_streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"}]
    audio_streams = [
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "fre"}, "disposition": {"default": 1}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac",
         "tags": {"language": "eng"}},
    ]
    sub_streams = [
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "fre"}},
    ]
    # Counts match (1 video, 2 audio, 0 subs) but the video default is
    # True where the intended (source-mirrored) value is False.
    with patch.object(cs, "remux", side_effect=_record_remux([])), \
         patch.object(cs, "probe", return_value=_verify_streams(
             video_defaults=(True,), audio_defaults=(True, False), subs=0)), \
         patch.object(cs, "original_language_for", return_value=None), \
         patch.object(cs, "set_consolidated_tag") as mock_tag, \
         patch.object(cs, "_normalize_audio_inline") as mock_norm:
        result = cs._cleanup_no_main_english(
            media, str(media), "NEEDS_BAZARR", "no English candidates",
            sub_streams, audio_streams, video_streams, None, [], False,
        )

    assert result["status"] == "FAIL"
    assert "cleanup verify" in result["detail"]
    assert "default-flag mismatch" in result["detail"]
    assert media.read_bytes() == original_bytes
    assert mock_tag.call_count == 0
    assert mock_norm.call_count == 0


def test_cleanup_verify_match_allows_swap(tmp_path):
    """If the post-remux probe DOES show the intended track counts AND
    the intended default-flag pattern (1 video default=False; 2 audio
    -- fre's own default=True preserved, eng=False; 0 subs), the swap
    proceeds normally. Normalization still never runs (Fix 4, round 3)."""
    media = _media(tmp_path)
    video_streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"}]
    audio_streams = [
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "fre"}, "disposition": {"default": 1}},
        {"index": 2, "codec_type": "audio", "codec_name": "aac",
         "tags": {"language": "eng"}},
    ]
    sub_streams = [
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "fre"}},
    ]

    def _fake_remux(src, dst, video_idx, audio_idx, eng_srt, forced_srt,
                     keep_subtitle_indices=None, cleanup_flags=None):
        Path(dst).write_bytes(b"CLEANED-CORRECTLY")
        return True, "ok"

    with patch.object(cs, "remux", side_effect=_fake_remux), \
         patch.object(cs, "probe", return_value=_verify_streams(
             video_defaults=(False,), audio_defaults=(True, False), subs=0)), \
         patch.object(cs, "original_language_for", return_value=None), \
         patch.object(cs, "set_consolidated_tag") as mock_tag, \
         patch.object(cs, "_normalize_audio_inline") as mock_norm:
        result = cs._cleanup_no_main_english(
            media, str(media), "NEEDS_BAZARR", "no English candidates",
            sub_streams, audio_streams, video_streams, None, [], False,
        )

    assert result["status"] == "NEEDS_BAZARR"
    assert "cleaned" in result["detail"]
    assert media.read_bytes() == b"CLEANED-CORRECTLY"   # swap happened
    assert mock_tag.call_count == 0                      # still no CONSOLIDATED_SUBS
    mock_norm.assert_not_called()                         # Fix 4 (round 3): never normalize
