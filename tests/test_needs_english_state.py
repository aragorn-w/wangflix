"""`needs-english.json` bookkeeping in consolidate-subs.py (read by sub-fallback.py).

NEEDS_BAZARR upserts an entry (`first_seen` kept, `last_seen` moves), FIXED removes it, `--dry-run` writes
nothing, and `consolidate-subs.state.json` is not written on NEEDS_BAZARR (its cache key would hide a later sidecar).
"""

import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "consolidate_subs_needs_english", str(PROJECT_ROOT / "consolidate-subs.py")
)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)


def _media(tmp_path: Path, name: str = "Movie (2024).mkv") -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x1a\x45\xdf\xa3")
    return p


# --- _utc_now_iso: microsecond precision ---------------------

def test_utc_now_iso_is_microsecond_precision_not_whole_second():
    """Two upserts in one second must differ, or sub-fallback's `drop_stale_entries` drops the fresh one."""
    ts = cs._utc_now_iso()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00", ts)
    dt = datetime.fromisoformat(ts)
    assert dt.tzinfo == timezone.utc


# --- _clear_needs_english as a standalone helper ----

def test_clear_needs_english_removes_existing_entry(tmp_path, monkeypatch):
    state_file = tmp_path / "needs-english.json"
    monkeypatch.setattr(cs, "NEEDS_ENGLISH_STATE_FILE", state_file)
    media = _media(tmp_path)
    key = str(media.resolve())
    state_file.write_text(json.dumps({
        key: {"first_seen": "2026-10-01T00:00:00Z", "last_seen": "2026-10-01T00:00:00Z"},
        "/some/other/file.mkv": {"first_seen": "x", "last_seen": "y"},
    }))

    cs._clear_needs_english(media, dry_run=False)

    state = json.loads(state_file.read_text())
    assert key not in state
    assert "/some/other/file.mkv" in state  # sibling entries untouched


# --- integration through _process_locked -----------------------------------

_ENG_ONLY_STREAMS = [
    {"index": 0, "codec_type": "video", "codec_name": "h264"},
    {"index": 1, "codec_type": "audio", "codec_name": "ac3", "tags": {"language": "eng"}},
]


def _run_needs_bazarr(tmp_path, monkeypatch, *, dry_run=False, tracker_text=None):
    """`_process_locked` on a file with no English subtitle: (result, media, needs-english.json, update_state_entry mock)."""
    needs_file = tmp_path / "needs-english.json"
    monkeypatch.setattr(cs, "NEEDS_ENGLISH_STATE_FILE", needs_file)
    if tracker_text is not None:
        needs_file.write_text(tracker_text)
    media = _media(tmp_path)
    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", return_value={"streams": _ENG_ONLY_STREAMS}), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "update_state_entry") as mock_update_state:
        result = cs._process_locked(media, str(media), dry_run=dry_run)
    return result, media, needs_file, mock_update_state


def test_needs_bazarr_via_process_locked_upserts_tracker_and_skips_main_state(tmp_path, monkeypatch):
    """`update_state_entry` on STATE_FILE would cache the file forever: its key does not change when a sidecar arrives."""
    result, media, needs_file, mock_update_state = _run_needs_bazarr(tmp_path, monkeypatch)
    assert result["status"] == "NEEDS_BAZARR"
    assert str(media.resolve()) in json.loads(needs_file.read_text())
    mock_update_state.assert_not_called()


def test_needs_bazarr_via_process_locked_dry_run_writes_nothing(tmp_path, monkeypatch):
    result, _media_path, needs_file, _update = _run_needs_bazarr(tmp_path, monkeypatch, dry_run=True)
    assert result["status"] == "NEEDS_BAZARR"
    assert not needs_file.exists()


def test_fixed_via_process_locked_clears_tracker_entry(tmp_path, monkeypatch):
    """A real FIXED return drops any pre-existing needs-english.json entry for the file."""
    needs_file = tmp_path / "needs-english.json"
    monkeypatch.setattr(cs, "NEEDS_ENGLISH_STATE_FILE", needs_file)
    media = _media(tmp_path)
    key = str(media.resolve())
    needs_file.write_text(json.dumps({
        key: {"first_seen": "2026-10-01T00:00:00Z", "last_seen": "2026-10-01T00:00:00Z"},
    }))

    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "eng"}},
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "eng"}},
    ]

    def _fake_extract(src, idx, codec, dst):
        Path(dst).write_text("1\n00:00:01,000 --> 00:00:02,000\nHello.\n")
        return True

    def _fake_remux(src, dst, video_idx, audio_idx, eng_srt, forced_srt,
                     keep_subtitle_indices=None, cleanup_flags=None,
                     eng_track_name="English"):
        Path(dst).write_bytes(b"MUXED")
        return True, "ok"

    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", return_value={"streams": streams}), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "extract_track", side_effect=_fake_extract), \
         patch.object(cs, "count_subtitle_lines", return_value=10), \
         patch.object(cs, "clean_srt_text", side_effect=lambda t: t), \
         patch.object(cs, "run_ffsubsync", return_value=False), \
         patch.object(cs, "original_language_for", return_value=None), \
         patch.object(cs, "get_audio_lang_pref", return_value=({"eng"}, "eng", False)), \
         patch.object(cs, "select_keep_audio", return_value=[1]), \
         patch.object(cs, "remux", side_effect=_fake_remux), \
         patch.object(cs, "set_consolidated_tag", return_value=True), \
         patch.object(cs, "_normalize_audio_inline"), \
         patch.object(cs, "update_state_entry"):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert not json.loads(needs_file.read_text())


# --- the cleanup remux replaces the file: MP4 -> MKV migrates the entry, a new release resets it ---

# one audio track and one foreign sub to drop, so the cleanup really remuxes and replaces the file
_CLEANUP_SOMETHING_TO_CLEAN_STREAMS = [
    {"index": 0, "codec_type": "video", "codec_name": "h264"},
    {"index": 1, "codec_type": "audio", "codec_name": "ac3",
     "tags": {"language": "eng"}},
    {"index": 2, "codec_type": "subtitle", "codec_name": "subrip",
     "tags": {"language": "ger"}},
]
_CLEANUP_VERIFY_STREAMS = {"streams": [
    {"codec_type": "video", "disposition": {"default": 0}},
    {"codec_type": "audio", "disposition": {"default": 0}},
]}


def _fake_cleanup_remux(src, dst, video_idx, audio_idx, eng_srt, forced_srt,
                        keep_subtitle_indices=None, cleanup_flags=None):
    Path(dst).write_bytes(b"CLEANED")
    return True, "ok"


def _run_cleanup_remux(media, utc_now="2026-10-13T00:00:00Z"):
    """Run `_process_locked` through the "something to clean" cleanup remux (media's identity changes)."""
    def _probe_side_effect(p):
        return ({"streams": _CLEANUP_SOMETHING_TO_CLEAN_STREAMS} if Path(p) == media
                else _CLEANUP_VERIFY_STREAMS)

    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", side_effect=_probe_side_effect), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "remux", side_effect=_fake_cleanup_remux), \
         patch.object(cs, "_normalize_audio_inline"), \
         patch.object(cs, "_utc_now_iso", return_value=utc_now):
        return cs._process_locked(media, str(media), dry_run=False)


def test_needs_bazarr_mp4_replacement_migrates_tracker_entry_to_mkv_path(tmp_path, monkeypatch):
    """The entry follows the `.mp4` -> `.mkv` swap to the new path and keeps its `first_seen`."""
    needs_file = tmp_path / "needs-english.json"
    monkeypatch.setattr(cs, "NEEDS_ENGLISH_STATE_FILE", needs_file)
    media = _media(tmp_path, "Movie (2024).mp4")
    old_key = str(media.resolve())
    needs_file.write_text(json.dumps({
        old_key: {"first_seen": "2026-09-01T00:00:00Z", "last_seen": "2026-09-01T00:00:00Z"},
    }))

    result = _run_cleanup_remux(media)

    assert result["status"] == "NEEDS_BAZARR"
    new_path = Path(result["path"])
    assert new_path.suffix == ".mkv"
    state = json.loads(needs_file.read_text())
    assert old_key not in state
    entry = state[str(new_path.resolve())]
    assert entry["first_seen"] == "2026-09-01T00:00:00Z"  # preserved, not reset
    assert entry["last_seen"] == "2026-10-13T00:00:00Z"
    # first_seen is carried forward unconditionally (the pipeline's own swap, not a new release), but the
    # new path's identity becomes the baseline for later upserts
    assert entry["identity"] == cs._file_identity(new_path)
    assert entry["identity"] is not None


@pytest.mark.parametrize("stored_identity_is_current, expected_first_seen", [
    (True, "2026-09-01T00:00:00Z"),     # same release: kept, even though the remux then replaces it on disk
    (False, "2026-10-13T00:00:00Z"),    # a new release at this path: reset
], ids=["same-release-keeps-first-seen", "new-release-resets-first-seen"])
def test_cleanup_remux_at_the_same_path_refreshes_the_stored_identity(
        tmp_path, monkeypatch, stored_identity_is_current, expected_first_seen):
    needs_file = tmp_path / "needs-english.json"
    monkeypatch.setattr(cs, "NEEDS_ENGLISH_STATE_FILE", needs_file)
    media = _media(tmp_path)  # already .mkv: the replacement path is the media's own path
    key = str(media.resolve())
    pre_swap_identity = cs._file_identity(media)
    stored = pre_swap_identity if stored_identity_is_current else [999999, 999999, 999999]
    needs_file.write_text(json.dumps({
        key: {"first_seen": "2026-09-01T00:00:00Z", "last_seen": "2026-09-01T00:00:00Z",
              "identity": stored},
    }))

    result = _run_cleanup_remux(media)

    assert result["status"] == "NEEDS_BAZARR"
    assert Path(result["path"]).resolve() == media.resolve()  # same path, no suffix change
    entry = json.loads(needs_file.read_text())[key]
    assert entry["first_seen"] == expected_first_seen
    assert entry["last_seen"] == "2026-10-13T00:00:00Z"
    post_swap_identity = cs._file_identity(media)
    assert entry["identity"] == post_swap_identity  # refreshed to the NEW file
    assert post_swap_identity != pre_swap_identity  # the remux actually changed it
    assert entry["identity"] != [999999, 999999, 999999]


# --- needs-english.json read/write failures never change the result -------

def test_process_locked_needs_bazarr_result_unaffected_by_corrupt_tracker(tmp_path, monkeypatch):
    result, _media_path, needs_file, mock_update_state = _run_needs_bazarr(
        tmp_path, monkeypatch, tracker_text="{not valid json")
    assert result["status"] == "NEEDS_BAZARR"
    mock_update_state.assert_not_called()
    assert needs_file.read_text() == "{not valid json"  # left alone, not "fixed up"
