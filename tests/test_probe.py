"""Tests for media_stack.probe — ffprobe wrappers + stream metadata
helpers.  Avoid actually invoking ffprobe by mocking subprocess.run."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.probe import (
    already_normalized, already_processed, file_key,
    primary_audio_stream, probe,
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
