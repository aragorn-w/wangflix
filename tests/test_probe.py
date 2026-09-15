"""Tests for media_stack.probe — ffprobe wrappers + stream metadata
helpers.  Avoid actually invoking ffprobe by mocking subprocess.run."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.probe import (
    already_normalized, already_processed, file_key,
    primary_audio_stream, probe, real_video_streams,
)


def test_file_key_returns_size_mtime(tmp_path):
    p = tmp_path / "x.mkv"
    p.write_bytes(b"X" * 1234)
    size, mtime = file_key(p)
    assert size == 1234
    assert mtime > 0


def test_already_processed_v2():
    info = {"format": {"tags": {"CONSOLIDATED_SUBS": "v2"}}}
    assert already_processed(info) is True


def test_already_processed_v1_is_not_current():
    info = {"format": {"tags": {"CONSOLIDATED_SUBS": "v1"}}}
    assert already_processed(info) is False


def test_already_processed_missing_tag():
    info = {"format": {"tags": {}}}
    assert already_processed(info) is False


def test_already_processed_case_insensitive():
    info = {"format": {"tags": {"consolidated_subs": "v2"}}}
    assert already_processed(info) is True


def test_already_normalized_v1():
    info = {"format": {"tags": {"NORMALIZED_AUDIO": "v1"}}}
    assert already_normalized(info) is True


def test_already_normalized_missing_tag():
    info = {"format": {"tags": {}}}
    assert already_normalized(info) is False


def test_primary_audio_stream_prefers_default():
    info = {"streams": [
        {"codec_type": "video"},
        {"codec_type": "audio", "index": 1, "disposition": {}},
        {"codec_type": "audio", "index": 2, "disposition": {"default": 1}},
    ]}
    a = primary_audio_stream(info)
    assert a is not None
    assert a["index"] == 2


def test_primary_audio_stream_falls_back_to_first():
    info = {"streams": [
        {"codec_type": "audio", "index": 1, "disposition": {}},
        {"codec_type": "audio", "index": 2, "disposition": {}},
    ]}
    a = primary_audio_stream(info)
    assert a is not None
    assert a["index"] == 1


def test_primary_audio_stream_none_when_no_audio():
    info = {"streams": [{"codec_type": "video"}]}
    assert primary_audio_stream(info) is None


def test_probe_returns_none_on_subprocess_failure():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = type("R", (), {
            "returncode": 1, "stdout": "", "stderr": "ffprobe error",
        })()
        assert probe(Path("/fake.mkv")) is None


def test_probe_returns_dict_on_success():
    fake_json = '{"streams":[{"codec_type":"video"}],"format":{}}'
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = type("R", (), {
            "returncode": 0, "stdout": fake_json, "stderr": "",
        })()
        out = probe(Path("/fake.mkv"))
    assert out == {"streams": [{"codec_type": "video"}], "format": {}}


def test_probe_returns_none_on_exception():
    with patch("subprocess.run", side_effect=Exception("boom")):
        assert probe(Path("/fake.mkv")) is None


# --- real_video_streams -----------------------------------------------------
# Regression guard: a still-image cover-art track muxed as a bare video stream
# (no attached_pic flag) made Jellyfin Android TV bind to the thumbnail and
# stall at 0ms.  Both muxing steps must agree to drop it.

def _v(index, codec, **disp):
    return {"index": index, "codec_type": "video", "codec_name": codec,
            "disposition": disp}


def test_real_video_streams_keeps_motion_video():
    streams = [_v(0, "hevc")]
    assert [s["index"] for s in real_video_streams(streams)] == [0]


def test_real_video_streams_drops_unflagged_still_image():
    """The malformed case that caused the 0ms stall."""
    streams = [_v(0, "hevc"), _v(1, "mjpeg")]
    assert [s["index"] for s in real_video_streams(streams)] == [0]


def test_real_video_streams_drops_png_cover_art():
    streams = [_v(0, "h264"), _v(1, "png")]
    assert [s["index"] for s in real_video_streams(streams)] == [0]


def test_real_video_streams_drops_flagged_attached_pic():
    """The well-formed cover-art case is dropped too."""
    streams = [_v(0, "hevc"), _v(1, "mjpeg", attached_pic=1)]
    assert [s["index"] for s in real_video_streams(streams)] == [0]


def test_real_video_streams_drops_multiple_still_images():
    """Spaceballs shipped three stray mjpeg tracks."""
    streams = [_v(0, "hevc"), _v(1, "mjpeg"), _v(2, "mjpeg"), _v(3, "mjpeg")]
    assert [s["index"] for s in real_video_streams(streams)] == [0]


def test_real_video_streams_ignores_non_video():
    streams = [
        {"index": 0, "codec_type": "audio", "codec_name": "aac"},
        _v(1, "hevc"),
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip"},
    ]
    assert [s["index"] for s in real_video_streams(streams)] == [1]


def test_real_video_streams_keeps_multiple_real_tracks():
    """Alt-angle/dual-video files keep every real track; pruning to one is
    consolidate-subs' separate 'belt-and-braces' step, not this helper's job."""
    streams = [_v(0, "hevc"), _v(1, "h264"), _v(2, "mjpeg")]
    assert [s["index"] for s in real_video_streams(streams)] == [0, 1]


def test_real_video_streams_empty():
    assert real_video_streams([]) == []


def test_real_video_streams_missing_disposition_key():
    streams = [{"index": 0, "codec_type": "video", "codec_name": "hevc"}]
    assert [s["index"] for s in real_video_streams(streams)] == [0]
