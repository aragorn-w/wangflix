"""Mock-based orchestration tests for `_process_locked` in
consolidate-subs.py.

Codex has flagged the orchestration function across multiple rounds
(round-3 #4, round-4 #6, round-5 #4) as large + lightly tested.
Rather than extract stage functions speculatively (no second caller
would consume them), we cover the early-return branches with
import-time mocks so the workflow is at least exercised end-to-end
without spawning ffprobe/mkvmerge.

Each test exercises one decision point in the orchestration:
  - file disappears after lock acquisition → SKIP "file gone"
  - state-cache hit → SKIP "state cache"
  - probe failure → FAIL "probe failed"
  - idempotency tag present → SKIP "tag present"
  - no English embedded subs AND no sidecars → NEEDS_BAZARR
  - only forced English (no main candidate) → FAIL
  - dry-run mode short-circuits before any mutation
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "consolidate_subs_orch", str(PROJECT_ROOT / "consolidate-subs.py")
)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)


def _media(tmp_path: Path) -> Path:
    """Create a fake .mkv file so `path.is_file()` passes."""
    p = tmp_path / "Movie (2024).mkv"
    p.write_bytes(b"\x1a\x45\xdf\xa3")  # EBML magic; content irrelevant for tests
    return p


def test_file_gone_after_lock_returns_skip(tmp_path):
    """Race: a peer renamed/removed the file between scan and lock
    acquisition.  `_process_locked` must not crash; must skip cleanly."""
    ghost = tmp_path / "vanished.mkv"
    # Do NOT create the file
    result = cs._process_locked(ghost, str(ghost), dry_run=False)
    assert result["status"] == "SKIP"
    assert "gone" in result["detail"].lower()


def test_state_cache_hit_returns_skip(tmp_path):
    media = _media(tmp_path)
    cur_size = media.stat().st_size
    # file_key() stores mtime as int(st.st_mtime) — match the cast
    cur_mtime = int(media.stat().st_mtime)
    fake_state = {
        str(media.resolve()): {
            "size": cur_size,
            "mtime": cur_mtime,
            "v": cs.PIPELINE_VERSION,
            "status": "fixed",
        }
    }
    with patch.object(cs, "load_state", return_value=fake_state):
        result = cs._process_locked(media, str(media), dry_run=False)
    assert result["status"] == "SKIP"
    assert result["detail"] == "state cache"


def test_probe_failure_returns_fail(tmp_path):
    media = _media(tmp_path)
    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", return_value=None):
        result = cs._process_locked(media, str(media), dry_run=False)
    assert result["status"] == "FAIL"
    assert "probe" in result["detail"]


def test_already_processed_returns_skip(tmp_path):
    media = _media(tmp_path)
    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", return_value={"streams": []}), \
         patch.object(cs, "already_processed", return_value=True), \
         patch.object(cs, "update_state_entry"):
        result = cs._process_locked(media, str(media), dry_run=False)
    assert result["status"] == "SKIP"
    assert "tag" in result["detail"]


def test_no_english_candidates_returns_needs_bazarr(tmp_path):
    """No embedded English subs, no image subs, no sidecars → kick
    out to Bazarr instead of trying to mux nothing."""
    media = _media(tmp_path)
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "eng"}},
        # German sub track is not eligible
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "ger"}},
    ]
    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", return_value={"streams": streams}), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]):
        result = cs._process_locked(media, str(media), dry_run=False)
    assert result["status"] == "NEEDS_BAZARR"


def test_only_forced_english_returns_fail(tmp_path):
    """A forced English track alone covers foreign dialogue but isn't
    a full sub — caller should re-fetch via Bazarr or subliminal.
    Returning FAIL surfaces it to the operator log."""
    media = _media(tmp_path)
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "eng"}},
        # Forced English sub only
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "eng", "title": "Forced"},
         "disposition": {"forced": 1}},
    ]
    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", return_value={"streams": streams}), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]):
        result = cs._process_locked(media, str(media), dry_run=False)
    assert result["status"] == "FAIL"
    assert "forced" in result["detail"].lower()


def test_dry_run_short_circuits_before_mutation(tmp_path):
    """dry_run=True must NOT call extract/remux/replace — we mock
    those and assert they're never invoked even when the file has
    actionable English subs."""
    media = _media(tmp_path)
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "eng"}},
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": "eng"}},
    ]
    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", return_value={"streams": streams}), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "extract_track") as mock_extract, \
         patch.object(cs, "remux") as mock_remux:
        result = cs._process_locked(media, str(media), dry_run=True)
    assert result["status"] == "SKIP"
    assert "dry-run" in result["detail"]
    # Critical invariant: no extract / remux / replace in dry-run
    assert mock_extract.call_count == 0
    assert mock_remux.call_count == 0


# --- _expose_tag_normalize: destination .mkv lock + tag-return (codex #2/#3) ---

def _mux_and_dest(tmp_path: Path, suffix: str):
    """A muxed temp output + a source path (.mkv → replacement==path;
    .mp4 → replacement is a distinct .mkv)."""
    path = tmp_path / f"Movie (2024){suffix}"
    path.write_bytes(b"ORIGINAL")
    out = tmp_path / ".Movie.123.tmp.mkv"
    out.write_bytes(b"MUXED")
    return out, path, path.with_suffix(".mkv")


def test_expose_tag_normalize_mkv_success(tmp_path):
    out, path, replacement = _mux_and_dest(tmp_path, ".mkv")  # replacement == path
    with patch.object(cs, "set_consolidated_tag", return_value=True), \
         patch.object(cs, "_normalize_audio_inline") as norm:
        skip, tag_written = cs._expose_tag_normalize(out, path, replacement, str(path))
    assert skip is None and tag_written is True
    assert replacement.read_bytes() == b"MUXED"             # swapped into place
    # #2: normalize must inherit the destination lock, not `path`.
    assert norm.call_args.kwargs["locked_path"] == replacement


def test_expose_tag_normalize_tagfail_does_not_roll_back(tmp_path):
    # #3: unlike normalize-audio, consolidate does NOT roll back on tag
    # failure — the file stays consolidated; tag_written=False is recorded
    # (state.json is the primary idempotency backstop).
    out, path, replacement = _mux_and_dest(tmp_path, ".mkv")
    with patch.object(cs, "set_consolidated_tag", return_value=False), \
         patch.object(cs, "_normalize_audio_inline"):
        skip, tag_written = cs._expose_tag_normalize(out, path, replacement, str(path))
    assert skip is None and tag_written is False
    assert replacement.read_bytes() == b"MUXED"             # NOT rolled back


def test_expose_tag_normalize_mp4_to_mkv_drops_original(tmp_path):
    out, path, replacement = _mux_and_dest(tmp_path, ".mp4")  # replacement != path
    assert replacement != path
    with patch.object(cs, "set_consolidated_tag", return_value=True), \
         patch.object(cs, "_normalize_audio_inline") as norm:
        skip, tag_written = cs._expose_tag_normalize(out, path, replacement, str(path))
    assert skip is None and tag_written is True
    assert replacement.read_bytes() == b"MUXED"             # new .mkv in place
    assert not path.exists()                                # original .mp4 removed
    assert norm.call_args.kwargs["locked_path"] == replacement


def test_expose_tag_normalize_skips_when_dest_locked(tmp_path):
    # #2: if a watcher/scan already holds the destination .mkv's lock, we must
    # back off (SKIP) instead of mutating it concurrently.
    from media_stack.locking import acquire_file_lock
    out, path, replacement = _mux_and_dest(tmp_path, ".mp4")
    with acquire_file_lock(replacement) as held:
        assert held
        with patch.object(cs, "set_consolidated_tag") as tag, \
             patch.object(cs, "_normalize_audio_inline") as norm:
            skip, tag_written = cs._expose_tag_normalize(out, path, replacement, str(path))
        assert skip is not None and skip["status"] == "SKIP"
        assert tag.call_count == 0 and norm.call_count == 0  # no mutation
        assert path.exists()                                 # original untouched


def test_expose_tag_normalize_tag_exception_no_rollback(tmp_path):
    # #2/#1 parity: a tag-write EXCEPTION is treated like a False return —
    # tag_written=False, NO rollback (state.json is the backstop), and
    # normalization still runs.  Without the try/except the raise would
    # escape, leaving state un-updated for an already-mutated file.
    out, path, replacement = _mux_and_dest(tmp_path, ".mkv")
    with patch.object(cs, "set_consolidated_tag",
                      side_effect=RuntimeError("mkvpropedit boom")), \
         patch.object(cs, "_normalize_audio_inline") as norm:
        skip, tag_written = cs._expose_tag_normalize(out, path, replacement, str(path))
    assert skip is None and tag_written is False
    assert replacement.read_bytes() == b"MUXED"     # NOT rolled back
    norm.assert_called_once()


def test_expose_tag_normalize_collision_under_lock_fails(tmp_path):
    # #1: a sibling .mkv that appeared between the pre-lock check and our
    # acquiring the destination lock → FAIL (don't overwrite it).
    out, path, replacement = _mux_and_dest(tmp_path, ".mp4")  # replacement != path
    replacement.write_bytes(b"SIBLING")              # collision present under lock
    with patch.object(cs, "set_consolidated_tag", return_value=True), \
         patch.object(cs, "_normalize_audio_inline") as norm:
        early, tag_written = cs._expose_tag_normalize(out, path, replacement, str(path))
    assert early is not None and early["status"] == "FAIL"
    assert replacement.read_bytes() == b"SIBLING"    # not overwritten
    assert norm.call_count == 0                       # never reached normalize
