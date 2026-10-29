"""Tests for media_stack.probe: ffprobe wrappers and stream metadata helpers (subprocess.run mocked)."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.probe import (
    already_normalized, already_processed, audio_stream_duration, file_key, parse_duration,
    primary_audio_stream, probe, probe_duration, real_video_streams,
)


def test_file_key_returns_size_mtime(tmp_path):
    p = tmp_path / "x.mkv"
    p.write_bytes(b"X" * 1234)
    size, mtime = file_key(p)
    assert size == 1234
    assert mtime > 0


def _tagged(**tags):
    return {"format": {"tags": tags}}


@pytest.mark.parametrize("tags, expected", [
    ({"CONSOLIDATED_SUBS": "v2"}, True),
    ({"CONSOLIDATED_SUBS": "v1"}, False),  # v1 is not current
    ({}, False),
    ({"consolidated_subs": "v2"}, True),  # tag name is case-insensitive
], ids=["v2", "v1-is-not-current", "missing-tag", "case-insensitive"])
def test_already_processed(tags, expected):
    assert already_processed(_tagged(**tags)) is expected


@pytest.mark.parametrize("tags, expected", [({"NORMALIZED_AUDIO": "v1"}, True), ({}, False)],
                         ids=["v1", "missing-tag"])
def test_already_normalized(tags, expected):
    assert already_normalized(_tagged(**tags)) is expected


@pytest.mark.parametrize("streams, expected_index", [
    ([{"codec_type": "video"},
      {"codec_type": "audio", "index": 1, "disposition": {}},
      {"codec_type": "audio", "index": 2, "disposition": {"default": 1}}], 2),
    ([{"codec_type": "audio", "index": 1, "disposition": {}},
      {"codec_type": "audio", "index": 2, "disposition": {}}], 1),
    ([{"codec_type": "video"}], None),
], ids=["prefers-default", "falls-back-to-first", "none-when-no-audio"])
def test_primary_audio_stream(streams, expected_index):
    a = primary_audio_stream({"streams": streams})
    assert (a["index"] if a is not None else None) == expected_index


def _ffprobe_returns(payload, rc=0):
    return MagicMock(returncode=rc, stdout=json.dumps(payload), stderr="")


@pytest.mark.parametrize("run_kwargs, expected", [
    pytest.param({"return_value": MagicMock(returncode=1, stdout="", stderr="ffprobe error")}, None,
                 id="nonzero-exit-is-none"),
    pytest.param({"return_value": _ffprobe_returns({"streams": [{"codec_type": "video"}], "format": {}})},
                 {"streams": [{"codec_type": "video"}], "format": {}}, id="success-returns-dict"),
    pytest.param({"side_effect": Exception("boom")}, None, id="exception-is-none"),
])
def test_probe(run_kwargs, expected):
    with patch("media_stack.probe.subprocess.run", **run_kwargs):
        assert probe(Path("/fake.mkv")) == expected


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
# A still-image cover-art track muxed as a bare video stream (no attached_pic) stalls
# Jellyfin Android TV at 0ms; both muxing steps must agree to drop it.

def _v(index, codec, **disp):
    return {"index": index, "codec_type": "video", "codec_name": codec,
            "disposition": disp}


@pytest.mark.parametrize("streams, kept", [
    pytest.param([_v(0, "hevc")], [0], id="keeps-motion-video"),
    pytest.param([_v(0, "hevc"), _v(1, "mjpeg")], [0], id="drops-unflagged-still-image"),  # the 0ms stall
    pytest.param([_v(0, "h264"), _v(1, "png")], [0], id="drops-png-cover-art"),
    pytest.param([_v(0, "hevc"), _v(1, "mjpeg", attached_pic=1)], [0], id="drops-flagged-attached-pic"),
    pytest.param([_v(0, "hevc"), _v(1, "mjpeg"), _v(2, "mjpeg"), _v(3, "mjpeg")], [0],
                 id="drops-multiple-still-images"),  # Spaceballs shipped three stray mjpeg tracks
    pytest.param([{"index": 0, "codec_type": "audio", "codec_name": "aac"}, _v(1, "hevc"),
                  {"index": 2, "codec_type": "subtitle", "codec_name": "subrip"}], [1],
                 id="ignores-non-video"),
    # alt-angle/dual-video files keep every real track; pruning to one is consolidate-subs' own step
    pytest.param([_v(0, "hevc"), _v(1, "h264"), _v(2, "mjpeg")], [0, 1], id="keeps-multiple-real-tracks"),
    pytest.param([], [], id="empty"),
    pytest.param([{"index": 0, "codec_type": "video", "codec_name": "hevc"}], [0],
                 id="missing-disposition-key"),
])
def test_real_video_streams(streams, kept):
    assert [s["index"] for s in real_video_streams(streams)] == kept


# --- parse_duration / probe_duration (bounded fallback for a missing duration) ---

@pytest.mark.parametrize("value,expected", [
    ("1234.5", 1234.5), (90, 90.0), (0.5, 0.5),
    (None, None), ("N/A", None), ("", None), ("0", None), ("-3", None), (0, None),
    ("nan", None), ("inf", None), ("abc", None), (True, None), ([], None),
])
def test_parse_duration_accepts_only_finite_positive_numbers(value, expected):
    assert parse_duration(value) == expected


_COVER = {"index": 0, "codec_type": "video", "duration": "5.0"}  # durations come from AUDIO streams only


def test_probe_duration_prefers_the_container_value_and_bounds_the_call():
    with patch("media_stack.probe.subprocess.run",
               return_value=_ffprobe_returns({"format": {"duration": "800.5"},
                                              "streams": [{"codec_type": "audio", "duration": "799"}]})) as run:
        assert probe_duration(Path("/v.mkv")) == 800.5
    argv = run.call_args.args[0]
    assert argv[0] == "ffprobe" and "format=duration:stream=index,codec_type,duration" in argv
    assert run.call_args.kwargs["timeout"] == 30


@pytest.mark.parametrize("payload, rc, expected", [
    pytest.param({"format": {"duration": "N/A"}, "streams": [{}, {"codec_type": "audio", "duration": "799"}]},
                 0, 799.0, id="falls-back-to-a-stream-value"),
    pytest.param({"format": {"duration": "N/A"},
                  "streams": [_COVER, {"index": 1, "codec_type": "audio", "duration": "1400.0"}]},
                 0, 1400.0, id="ignores-a-cover-art-video-stream-uses-the-audio-one"),
    pytest.param({"format": {"duration": "900"},
                  "streams": [{"index": 1, "codec_type": "audio", "duration": "1400"}]},
                 0, 900.0, id="container-value-wins-over-any-stream"),
    pytest.param({"format": {}, "streams": [_COVER]}, 0, None, id="only-a-video-stream-duration-is-none"),
    pytest.param({"format": {}, "streams": []}, 0, None, id="none-without-any-value"),
    pytest.param({"format": {"duration": "0"}}, 0, None, id="none-for-zero-duration"),
    pytest.param({}, 1, None, id="none-on-nonzero-exit"),
])
def test_probe_duration_value(payload, rc, expected):
    with patch("media_stack.probe.subprocess.run", return_value=_ffprobe_returns(payload, rc)):
        assert probe_duration(Path("/v.mkv")) == expected


def test_probe_duration_none_on_timeout_or_garbage():
    with patch("media_stack.probe.subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 30)):
        assert probe_duration(Path("/v.mkv")) is None
    with patch("media_stack.probe.subprocess.run",
               return_value=MagicMock(returncode=0, stdout="not json", stderr="")):
        assert probe_duration(Path("/v.mkv")) is None


def test_probe_duration_prefers_the_scanned_audio_stream():
    payload = {"format": {}, "streams": [
        {"index": 1, "codec_type": "audio", "duration": "100"},
        {"index": 2, "codec_type": "audio", "duration": "200"}]}
    with patch("media_stack.probe.subprocess.run", return_value=_ffprobe_returns(payload)):
        assert probe_duration(Path("/v.mkv"), audio_index=2) == 200.0
        assert probe_duration(Path("/v.mkv")) == 100.0
        assert probe_duration(Path("/v.mkv"), audio_index=9) == 100.0  # no such stream: any audio


@pytest.mark.parametrize("streams,expected", [
    ([_COVER], None),
    ([{"codec_type": "audio", "duration": "N/A"}, {"codec_type": "audio", "duration": "70"}], 70.0),
    ([{"duration": "70"}], None),                         # no codec_type: not trusted
    ([{"codec_type": "subtitle", "duration": "70"}], None),
    (["junk", None, {"codec_type": "audio", "duration": "70"}], 70.0),
    (None, None), ("nope", None), ({}, None), ([], None),
])
def test_audio_stream_duration_takes_only_audio_streams(streams, expected):
    assert audio_stream_duration(streams) == expected


# --- allowed_indices: a commentary audio stream is never a source ---

_COMMENTARY = {"index": 2, "codec_type": "audio", "duration": "90.0"}
_PROGRAM_NO_DURATION = {"index": 1, "codec_type": "audio"}


def test_audio_stream_duration_skips_streams_outside_allowed_indices():
    streams = [_PROGRAM_NO_DURATION, _COMMENTARY]
    assert audio_stream_duration(streams, preferred_index=1) == 90.0           # unrestricted: the old hole
    assert audio_stream_duration(streams, preferred_index=1, allowed_indices={1}) is None
    assert audio_stream_duration(streams, preferred_index=1, allowed_indices=set()) is None


def test_audio_stream_duration_takes_the_next_allowed_stream_not_the_commentary():
    streams = [_PROGRAM_NO_DURATION, _COMMENTARY, {"index": 3, "codec_type": "audio", "duration": "1400"}]
    assert audio_stream_duration(streams, preferred_index=1, allowed_indices={1, 3}) == 1400.0


def test_audio_stream_duration_without_an_index_is_not_allowed_when_restricted():
    streams = [{"codec_type": "audio", "duration": "70"}]
    assert audio_stream_duration(streams) == 70.0
    assert audio_stream_duration(streams, allowed_indices={0}) is None


def test_probe_duration_never_takes_a_commentary_streams_duration_when_restricted():
    payload = {"format": {}, "streams": [_PROGRAM_NO_DURATION, _COMMENTARY]}
    with patch("media_stack.probe.subprocess.run", return_value=_ffprobe_returns(payload)):
        assert probe_duration(Path("/v.mkv"), audio_index=1, allowed_indices={1}) is None
        assert probe_duration(Path("/v.mkv"), audio_index=1, allowed_indices=[1]) is None


def test_probe_duration_restriction_still_allows_the_container_value():
    payload = {"format": {"duration": "900"}, "streams": [_PROGRAM_NO_DURATION, _COMMENTARY]}
    with patch("media_stack.probe.subprocess.run", return_value=_ffprobe_returns(payload)):
        assert probe_duration(Path("/v.mkv"), audio_index=1, allowed_indices={1}) == 900.0
