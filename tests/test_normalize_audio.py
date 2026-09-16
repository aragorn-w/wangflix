"""Regression tests for normalize-audio.py's `_replace_and_tag`.

This is the atomic swap + idempotency-tag step.  The bug it guards against:
on a tag-write failure the old code deleted the backup and reported FIXED,
leaving a replaced-but-UNTAGGED file — which the driver's tag-keyed coverage
probe then re-normalizes (lossy generation loss) every sweep.  The fix tags
BEFORE dropping the backup and rolls back (restores the original, drops any
orphan output) on tag failure, returning False so the caller reports TAG_FAIL.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
_spec = importlib.util.spec_from_file_location(
    "normalize_audio", str(PROJECT_ROOT / "normalize-audio.py"))
na = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(na)


def _setup(tmp_path, suffix):
    src = tmp_path / f"Movie (2024){suffix}"
    src.write_bytes(b"ORIGINAL")
    remux_out = tmp_path / ".Movie.123.remux.mkv"
    remux_out.write_bytes(b"NORMALIZED")
    final_path = src.with_suffix(".mkv")
    backup_path = src.with_suffix(src.suffix + ".pre-norm.bak")
    return src, remux_out, final_path, backup_path


def test_replace_and_tag_success_mkv(tmp_path):
    src, remux_out, final_path, backup_path = _setup(tmp_path, ".mkv")
    with patch.object(na, "set_normalized_tag", return_value=True):
        ok = na._replace_and_tag(src, remux_out, final_path, backup_path)
    assert ok is True
    assert final_path.read_bytes() == b"NORMALIZED"   # new file in place
    assert not backup_path.exists()                   # backup dropped after tag
    assert not remux_out.exists()                     # consumed by the swap


def test_replace_and_tag_success_mp4_becomes_mkv(tmp_path):
    src, remux_out, final_path, backup_path = _setup(tmp_path, ".mp4")
    assert final_path != src
    with patch.object(na, "set_normalized_tag", return_value=True):
        ok = na._replace_and_tag(src, remux_out, final_path, backup_path)
    assert ok is True
    assert final_path.read_bytes() == b"NORMALIZED"   # new .mkv in place
    assert not src.exists()                           # original .mp4 replaced
    assert not backup_path.exists()


def test_replace_and_tag_tagfail_rolls_back_mkv(tmp_path):
    src, remux_out, final_path, backup_path = _setup(tmp_path, ".mkv")
    with patch.object(na, "set_normalized_tag", return_value=False):
        ok = na._replace_and_tag(src, remux_out, final_path, backup_path)
    assert ok is False                                # caller -> TAG_FAIL, driver retries
    # final_path == src here; it must hold the RESTORED original, not the
    # normalized output (the whole point — no silent untagged file).
    assert final_path.read_bytes() == b"ORIGINAL"
    assert not backup_path.exists()                   # backup consumed by restore


def test_replace_and_tag_tagfail_mp4_restores_and_drops_orphan(tmp_path):
    # .mp4 source -> final is a DIFFERENT .mkv path; rollback must restore the
    # .mp4 AND delete the orphan .mkv so nothing untagged is left behind.
    src, remux_out, final_path, backup_path = _setup(tmp_path, ".mp4")
    assert final_path != src
    with patch.object(na, "set_normalized_tag", return_value=False):
        ok = na._replace_and_tag(src, remux_out, final_path, backup_path)
    assert ok is False
    assert src.read_bytes() == b"ORIGINAL"            # .mp4 restored
    assert not final_path.exists()                    # orphan .mkv removed
    assert not backup_path.exists()


def test_replace_and_tag_tag_exception_rolls_back(tmp_path):
    # A tag-write EXCEPTION (mkvextract timeout/OSError), not just a False
    # return, must ALSO roll back — else the swapped-in file is left untagged.
    src, remux_out, final_path, backup_path = _setup(tmp_path, ".mkv")
    with patch.object(na, "set_normalized_tag",
                      side_effect=RuntimeError("mkvextract timeout")):
        ok = na._replace_and_tag(src, remux_out, final_path, backup_path)
    assert ok is False
    assert final_path.read_bytes() == b"ORIGINAL"   # restored, not left untagged
    assert not backup_path.exists()


def test_replace_and_tag_tag_exception_rolls_back_mp4(tmp_path):
    src, remux_out, final_path, backup_path = _setup(tmp_path, ".mp4")
    with patch.object(na, "set_normalized_tag",
                      side_effect=OSError("xml write failed")):
        ok = na._replace_and_tag(src, remux_out, final_path, backup_path)
    assert ok is False
    assert src.read_bytes() == b"ORIGINAL"          # .mp4 restored
    assert not final_path.exists()                  # orphan .mkv removed


def test_replace_and_tag_raises_when_dest_locked_mp4(tmp_path):
    # #2: .mp4→.mkv where another pipeline already holds the destination
    # .mkv lock → refuse (RuntimeError), leave the source intact.
    from media_stack.locking import acquire_file_lock
    src, remux_out, final_path, backup_path = _setup(tmp_path, ".mp4")
    with acquire_file_lock(final_path) as held:
        assert held
        with patch.object(na, "set_normalized_tag", return_value=True):
            with pytest.raises(RuntimeError):
                na._replace_and_tag(src, remux_out, final_path, backup_path)
    assert src.read_bytes() == b"ORIGINAL"          # nothing swapped


def test_replace_and_tag_raises_on_collision_under_lock_mp4(tmp_path):
    # #2: a sibling .mkv that appeared after the pre-lock check → refuse,
    # don't overwrite the other worker's output.
    src, remux_out, final_path, backup_path = _setup(tmp_path, ".mp4")
    final_path.write_bytes(b"SIBLING")              # collision present
    with patch.object(na, "set_normalized_tag", return_value=True):
        with pytest.raises(RuntimeError):
            na._replace_and_tag(src, remux_out, final_path, backup_path)
    assert src.read_bytes() == b"ORIGINAL"          # untouched
    assert final_path.read_bytes() == b"SIBLING"    # sibling not overwritten


# --- _check_stream_regression ----------------------------------------------
# Regression guard for the interaction between pass 2's cover-art strip and
# this post-render validation.  Counting *all* video streams here rejected the
# corrected output ("video count regressed: 2->1") and left the malformed file
# in place, silently defeating the strip.

def _vid(codec, attached_pic=False):
    return {"codec_type": "video", "codec_name": codec,
            "disposition": {"attached_pic": 1} if attached_pic else {}}


def _aud():
    return {"codec_type": "audio", "codec_name": "aac", "disposition": {}}


def _sub():
    return {"codec_type": "subtitle", "codec_name": "subrip", "disposition": {}}


def test_guard_allows_intended_cover_art_removal():
    """hevc + malformed mjpeg in, hevc out — the whole point of the fix."""
    info = {"streams": [_vid("hevc"), _vid("mjpeg"), _aud()]}
    new_info = {"streams": [_vid("hevc"), _aud()]}
    na._check_stream_regression(info, new_info)   # must not raise


def test_guard_allows_attached_pic_removal():
    info = {"streams": [_vid("hevc"), _vid("mjpeg", attached_pic=True), _aud()]}
    new_info = {"streams": [_vid("hevc"), _aud()]}
    na._check_stream_regression(info, new_info)


def test_guard_allows_multiple_cover_art_removal():
    info = {"streams": [_vid("hevc"), _vid("mjpeg"), _vid("png"), _aud()]}
    new_info = {"streams": [_vid("hevc"), _aud()]}
    na._check_stream_regression(info, new_info)


def test_guard_still_rejects_real_video_loss():
    """Losing genuine motion video remains a hard failure."""
    info = {"streams": [_vid("hevc"), _vid("h264"), _aud()]}
    new_info = {"streams": [_vid("hevc"), _aud()]}
    with pytest.raises(RuntimeError, match="real video count regressed"):
        na._check_stream_regression(info, new_info)


def test_guard_rejects_total_video_loss():
    info = {"streams": [_vid("hevc"), _aud()]}
    new_info = {"streams": [_aud()]}
    with pytest.raises(RuntimeError, match="real video count regressed"):
        na._check_stream_regression(info, new_info)


def test_guard_still_rejects_subtitle_loss():
    info = {"streams": [_vid("hevc"), _aud(), _sub(), _sub()]}
    new_info = {"streams": [_vid("hevc"), _aud(), _sub()]}
    with pytest.raises(RuntimeError, match="sub count regressed"):
        na._check_stream_regression(info, new_info)


def test_guard_passes_unchanged_file():
    info = {"streams": [_vid("hevc"), _aud(), _sub()]}
    na._check_stream_regression(info, {"streams": [_vid("hevc"), _aud(), _sub()]})


def test_guard_rejects_audio_loss():
    """The dual-audio defect: pass 2 dropping the English dub must fail loudly
    rather than being accepted as a valid replacement."""
    info = {"streams": [_vid("hevc"), _aud(), _aud()]}
    new_info = {"streams": [_vid("hevc"), _aud()]}
    with pytest.raises(RuntimeError, match="audio count regressed"):
        na._check_stream_regression(info, new_info)


def test_guard_allows_unchanged_dual_audio():
    info = {"streams": [_vid("hevc"), _aud(), _aud(), _sub()]}
    na._check_stream_regression(
        info, {"streams": [_vid("hevc"), _aud(), _aud(), _sub()]})
