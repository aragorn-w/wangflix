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


def _run_pass2(tmp_path, streams, audio_index=0, channels=6, layout="5.1"):
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
        render_normalized(src, dst, audio_index, "aac", channels, MEASURED, layout)
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


# --- audio-track preservation ------------------------------------------------
# Pass 2 used to map only `0:a:<primary>`.  consolidate-subs decides which audio
# tracks a library file keeps (a dual-audio title keeps the original AND the
# English dub) and then calls straight into normalization, so that single map
# silently destroyed the dub moments after consolidation had kept it.

def _audio_maps(cmd):
    return [cmd[i + 1] for i, a in enumerate(cmd)
            if a == "-map" and cmd[i + 1].startswith("0:a:")]


def _arg_after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def _dual_audio_streams():
    return _streams(
        (0, "video", "hevc", False),
        (1, "audio", "aac", False),      # primary (original language)
        (2, "audio", "ac3", False),      # English dub
    )


def test_pass2_keeps_every_audio_track(tmp_path):
    cmd = _run_pass2(tmp_path, _dual_audio_streams())
    assert _audio_maps(cmd) == ["0:a:0", "0:a:1"]


def test_pass2_reencodes_primary_and_copies_the_rest(tmp_path):
    cmd = _run_pass2(tmp_path, _dual_audio_streams())
    assert _arg_after(cmd, "-c:a:0") == "aac"
    assert _arg_after(cmd, "-c:a:1") == "copy"
    assert "-filter:a:0" in cmd, "loudnorm must be applied to the primary"
    assert "-filter:a:1" not in cmd, "loudnorm must not touch a copied track"


def test_pass2_uses_no_global_audio_flags(tmp_path):
    """A bare -c:a / -af / -ac / -b:a also hits the stream-copied tracks,
    which would re-encode or mangle them."""
    cmd = _run_pass2(tmp_path, _dual_audio_streams())
    for bad in ("-c:a", "-af", "-ac", "-b:a"):
        assert bad not in cmd, f"{bad} applies to every audio stream"


def test_pass2_honours_which_track_is_primary(tmp_path):
    """audio_index selects which mapped track gets loudnorm, not which is kept."""
    cmd = _run_pass2(tmp_path, _dual_audio_streams(), audio_index=1)
    assert _audio_maps(cmd) == ["0:a:0", "0:a:1"]
    assert _arg_after(cmd, "-c:a:0") == "copy"
    assert _arg_after(cmd, "-c:a:1") == "aac"
    assert "-filter:a:1" in cmd
    assert "-filter:a:0" not in cmd


def test_pass2_single_audio_file_still_reencodes(tmp_path):
    cmd = _run_pass2(tmp_path, _streams(
        (0, "video", "hevc", False),
        (1, "audio", "aac", False),
    ))
    assert _audio_maps(cmd) == ["0:a:0"]
    assert _arg_after(cmd, "-c:a:0") == "aac"


def test_pass2_downmix_targets_only_the_primary(tmp_path):
    """Atmos downmix is per-stream; applied globally it would mangle the
    stream-copied secondary track."""
    cmd = _run_pass2(tmp_path, _streams(
        (0, "video", "hevc", False),
        (1, "audio", "truehd", False),
        (2, "audio", "ac3", False),
    ), channels=8, layout="FL+FR+FC+LFE+SL+SR+TFL+TFR")
    assert _arg_after(cmd, "-ac:a:0") == "6"
    assert "-ac" not in cmd
    assert "-ac:a:1" not in cmd


def test_pass2_raises_when_audio_index_out_of_range(tmp_path):
    with pytest.raises(RuntimeError, match="audio_index"):
        _run_pass2(tmp_path, _streams(
            (0, "video", "hevc", False),
            (1, "audio", "aac", False),
        ), audio_index=3)


def test_pass2_raises_when_there_is_no_audio(tmp_path):
    with pytest.raises(RuntimeError, match="audio_index"):
        _run_pass2(tmp_path, _streams((0, "video", "hevc", False)))
