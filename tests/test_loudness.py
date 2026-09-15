"""Tests for media_stack.loudness — the loudnorm pass-2 ffmpeg command.

ffmpeg is never actually invoked; subprocess.run is mocked so the tests
assert on the constructed argv.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.loudness import render_normalized

MEASURED = {
    "input_i": "-21.0", "input_lra": "7.0", "input_tp": "-2.0",
    "input_thresh": "-31.0", "target_offset": "0.0",
}


def _streams(*specs):
    """specs: (index, codec_type, codec_name, attached_pic)"""
    return [{"index": i, "codec_type": t, "codec_name": c,
             "disposition": {"attached_pic": 1} if ap else {}}
            for i, t, c, ap in specs]


def _run_pass2(tmp_path, streams):
    """Invoke render_normalized with ffprobe + ffmpeg mocked; return argv."""
    src = tmp_path / "in.mkv"
    src.write_bytes(b"X" * 200_000)
    dst = tmp_path / "out.mkv"
    captured = {}

    def fake_run(cmd, *a, **kw):
        captured["cmd"] = cmd
        dst.write_bytes(b"Y" * 200_000)          # satisfy the size sanity check
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("media_stack.loudness.ffprobe_strict",
               return_value={"streams": streams}), \
         patch("media_stack.loudness.subprocess.run", side_effect=fake_run):
        render_normalized(src, dst, 0, "aac", 6, MEASURED, "5.1")
    return captured["cmd"]


def _video_maps(cmd):
    return [cmd[i + 1] for i, a in enumerate(cmd)
            if a == "-map" and cmd[i + 1].startswith("0:")
            and not cmd[i + 1].startswith(("0:a", "0:s", "0:t"))]


def test_pass2_maps_only_real_video(tmp_path):
    """The regression: a stray mjpeg cover-art track must not be mapped.

    `-map 0:v` used to carry it over, silently undoing the strip that
    consolidate-subs had already performed.
    """
    cmd = _run_pass2(tmp_path, _streams(
        (0, "video", "hevc", False),
        (1, "video", "mjpeg", False),     # malformed cover art
        (2, "audio", "aac", False),
    ))
    assert "0:v" not in cmd, "bare 0:v re-maps every video stream"
    assert _video_maps(cmd) == ["0:0"]


def test_pass2_maps_plain_file_unchanged(tmp_path):
    cmd = _run_pass2(tmp_path, _streams(
        (0, "video", "hevc", False),
        (1, "audio", "aac", False),
    ))
    assert _video_maps(cmd) == ["0:0"]


def test_pass2_drops_flagged_attached_pic(tmp_path):
    cmd = _run_pass2(tmp_path, _streams(
        (0, "video", "hevc", False),
        (1, "video", "mjpeg", True),
        (2, "audio", "aac", False),
    ))
    assert _video_maps(cmd) == ["0:0"]


def test_pass2_keeps_both_real_video_tracks(tmp_path):
    cmd = _run_pass2(tmp_path, _streams(
        (0, "video", "hevc", False),
        (1, "video", "h264", False),
        (2, "video", "png", False),
        (3, "audio", "aac", False),
    ))
    assert _video_maps(cmd) == ["0:0", "0:1"]


def test_pass2_video_index_is_absolute_not_relative(tmp_path):
    """Real video sitting after the cover art must map by absolute index."""
    cmd = _run_pass2(tmp_path, _streams(
        (0, "video", "mjpeg", False),
        (1, "video", "hevc", False),
        (2, "audio", "aac", False),
    ))
    assert _video_maps(cmd) == ["0:1"]


def test_pass2_audio_map_stays_relative(tmp_path):
    """audio_index is audio-relative; mixing it with absolute video maps is
    intentional and must not regress."""
    cmd = _run_pass2(tmp_path, _streams(
        (0, "video", "hevc", False),
        (1, "video", "mjpeg", False),
        (2, "audio", "aac", False),
        (3, "audio", "ac3", False),
    ))
    assert "0:a:0" in cmd


def test_pass2_raises_when_no_real_video(tmp_path):
    with pytest.raises(RuntimeError, match="no real video stream"):
        _run_pass2(tmp_path, _streams(
            (0, "video", "mjpeg", False),
            (1, "audio", "aac", False),
        ))
