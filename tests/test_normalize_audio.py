"""Tests for normalize-audio.py: `_replace_and_tag` (atomic swap + idempotency tag) and `_check_stream_regression`.

On tag-write failure the original is restored and False returned (caller reports TAG_FAIL),
so no replaced-but-untagged file is left for the driver to re-normalize every sweep.
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


@pytest.mark.parametrize("suffix", [".mkv", ".mp4"])
def test_replace_and_tag_success(tmp_path, suffix):
    src, remux_out, final_path, backup_path = _setup(tmp_path, suffix)
    with patch.object(na, "set_normalized_tag", return_value=True):
        ok = na._replace_and_tag(src, remux_out, final_path, backup_path)
    assert ok is True
    assert final_path.read_bytes() == b"NORMALIZED"   # new file in place
    assert src.exists() == (final_path == src)        # an .mp4 source is replaced by the .mkv
    assert not backup_path.exists()                   # backup dropped after tag
    assert not remux_out.exists()                     # consumed by the swap


@pytest.mark.parametrize("suffix, tag_effect", [
    (".mkv", dict(return_value=False)),
    (".mp4", dict(return_value=False)),
    (".mkv", dict(side_effect=RuntimeError("mkvextract timeout"))),
    (".mp4", dict(side_effect=OSError("xml write failed"))),
], ids=["mkv-tag-false", "mp4-tag-false", "mkv-tag-raises", "mp4-tag-raises"])
def test_replace_and_tag_tagfail_rolls_back(tmp_path, suffix, tag_effect):
    src, remux_out, final_path, backup_path = _setup(tmp_path, suffix)
    with patch.object(na, "set_normalized_tag", **tag_effect):
        ok = na._replace_and_tag(src, remux_out, final_path, backup_path)
    assert ok is False                                # caller -> TAG_FAIL, driver retries
    assert src.read_bytes() == b"ORIGINAL"            # original restored, not left untagged
    assert final_path.exists() == (final_path == src)  # an .mp4 source's orphan .mkv is removed
    assert not backup_path.exists()                   # backup consumed by restore


def test_replace_and_tag_raises_when_dest_locked_mp4(tmp_path):
    from media_stack.locking import acquire_file_lock
    src, remux_out, final_path, backup_path = _setup(tmp_path, ".mp4")
    with acquire_file_lock(final_path) as held:
        assert held
        with patch.object(na, "set_normalized_tag", return_value=True):
            with pytest.raises(RuntimeError):
                na._replace_and_tag(src, remux_out, final_path, backup_path)
    assert src.read_bytes() == b"ORIGINAL"          # nothing swapped


def test_replace_and_tag_raises_on_collision_under_lock_mp4(tmp_path):
    # A sibling .mkv that appeared after the pre-lock check: refuse, don't overwrite its output.
    src, remux_out, final_path, backup_path = _setup(tmp_path, ".mp4")
    final_path.write_bytes(b"SIBLING")              # collision present
    with patch.object(na, "set_normalized_tag", return_value=True):
        with pytest.raises(RuntimeError):
            na._replace_and_tag(src, remux_out, final_path, backup_path)
    assert src.read_bytes() == b"ORIGINAL"          # untouched
    assert final_path.read_bytes() == b"SIBLING"    # sibling not overwritten


# --- _check_stream_regression: pass 2's cover-art strip must not trip the video-count guard ---

def _vid(codec, attached_pic=False):
    return {"codec_type": "video", "codec_name": codec,
            "disposition": {"attached_pic": 1} if attached_pic else {}}


def _aud():
    return {"codec_type": "audio", "codec_name": "aac", "disposition": {}}


def _sub():
    return {"codec_type": "subtitle", "codec_name": "subrip", "disposition": {}}


@pytest.mark.parametrize("before, after", [
    ([_vid("hevc"), _vid("mjpeg"), _aud()], [_vid("hevc"), _aud()]),
    ([_vid("hevc"), _vid("mjpeg", attached_pic=True), _aud()], [_vid("hevc"), _aud()]),
    ([_vid("hevc"), _vid("mjpeg"), _vid("png"), _aud()], [_vid("hevc"), _aud()]),
    ([_vid("hevc"), _aud(), _sub()], [_vid("hevc"), _aud(), _sub()]),
    ([_vid("hevc"), _aud(), _aud(), _sub()], [_vid("hevc"), _aud(), _aud(), _sub()]),
], ids=["malformed-cover-art-removed", "attached-pic-removed", "several-cover-art-removed",
        "unchanged", "unchanged-dual-audio"])
def test_guard_allows(before, after):
    na._check_stream_regression({"streams": before}, {"streams": after})   # must not raise


@pytest.mark.parametrize("before, after, match", [
    ([_vid("hevc"), _vid("h264"), _aud()], [_vid("hevc"), _aud()], "real video count regressed"),
    ([_vid("hevc"), _aud()], [_aud()], "real video count regressed"),
    ([_vid("hevc"), _aud(), _sub(), _sub()], [_vid("hevc"), _aud(), _sub()], "sub count regressed"),
    ([_vid("hevc"), _aud(), _aud()], [_vid("hevc"), _aud()], "audio count regressed"),
], ids=["real-video-loss", "total-video-loss", "subtitle-loss", "audio-loss"])
def test_guard_rejects(before, after, match):
    with pytest.raises(RuntimeError, match=match):
        na._check_stream_regression({"streams": before}, {"streams": after})
