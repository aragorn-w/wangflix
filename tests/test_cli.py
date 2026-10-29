"""Tests for media_stack.cli, the dispatcher shell scripts call as `python3 -m media_stack.cli <subcommand>`."""

import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack import cli


@pytest.mark.parametrize("argv, rc, err_fragments", [
    pytest.param([], 2, ("usage",), id="no-args-prints-usage"),
    pytest.param(["nonexistent_thing"], 2, ("unknown",), id="unknown-subcommand"),
    pytest.param(["expand"], 2, ("usage",), id="expand-without-family"),
    pytest.param(["has_eng_sdh"], 1, ("usage",), id="has_eng_sdh-no-args"),
    pytest.param(["dual_audio"], 1, ("usage",), id="dual_audio-no-args"),
    pytest.param(["print_keywords", "klingon"], 2, ("unknown set", "klingon"), id="print_keywords-unknown-set"),
    # telegram_send fails loud (rc=1 + stderr) if any of the three required env vars is unset
    pytest.param(["telegram_send"], 1, ("missing",), id="telegram_send-missing-env"),
])
def test_main_error_paths_exit_nonzero_with_a_message_on_stderr(capsys, monkeypatch, argv, rc, err_fragments):
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "MSG"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(argv) == rc
    err = capsys.readouterr().err.lower()
    for fragment in err_fragments:
        assert fragment in err


@pytest.mark.parametrize("argv, stdin, expected", [
    pytest.param(["canonicalize"], "ja\nen\n", "eng,jpn", id="canonicalize"),
    pytest.param(["expand", "jpn"], "", "ja,japanese,jpn", id="expand-sorted-comma-joined"),
])
def test_alias_subcommands_via_main(capsys, monkeypatch, argv, stdin, expected):
    monkeypatch.setattr("sys.stdin", StringIO(stdin))
    assert cli.main(argv) == 0
    assert capsys.readouterr().out.strip() == expected


@pytest.mark.parametrize("cmd, streams_json, expected", [
    pytest.param("has_eng_sdh", '{"streams":[{"tags":{"language":"eng"},"disposition":{"hearing_impaired":1}}]}',
                 "yes", id="eng-sdh-prints-yes"),
    pytest.param("has_eng_sdh", '{"streams":[{"tags":{"language":"fre"},"disposition":{"hearing_impaired":1}}]}',
                 "", id="french-sdh-only-prints-nothing"),
    pytest.param("dual_audio", '{"streams":[{"tags":{"language":"jpn"}},{"tags":{"language":"eng"}}]}',
                 "eng,jpn", id="jpn-and-eng"),
    pytest.param("dual_audio", '{"streams":[{"tags":{}},{"tags":{"language":"jpn"}}]}',
                 "eng,jpn", id="untagged-audio-collapses-to-eng"),
])
def test_ffprobe_backed_subcommands_with_mocked_ffprobe(capsys, cmd, streams_json, expected):
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=streams_json, stderr="")):
        assert cli.main([cmd, "/fake/file.mkv"]) == 0
    assert capsys.readouterr().out.strip() == expected


@pytest.mark.parametrize("sent, rc", [(True, 0), (False, 1)], ids=["success", "adapter-failure-propagates"])
def test_telegram_send_routes_through_adapter_and_propagates_the_result(monkeypatch, sent, rc):
    """The adapter gets the env values; its False gives rc=1 so cron sees the failure."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat-id")
    monkeypatch.setenv("MSG", "normalize complete: 209/209")
    with patch.object(cli, "telegram_send", return_value=sent) as mock_send:
        assert cli.main(["telegram_send"]) == rc
    mock_send.assert_called_once_with("123:ABC", "test-chat-id", "normalize complete: 209/209")


def test_print_keywords_default_dumps_all_sets(capsys):
    """The default invocation dumps every keyword set with section headers."""
    rc = cli.main(["print_keywords"])
    assert rc == 0
    out = capsys.readouterr().out
    # Each set header present
    assert "# japanese" in out
    assert "# korean" in out
    assert "# dual_audio" in out


def test_print_keywords_single_set_no_header(capsys):
    """A requested set prints only its keywords, no headers, so scripts can `grep -Fxq`."""
    rc = cli.main(["print_keywords", "japanese"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "anime" in out         # known JAPANESE_KEYWORDS member
    assert "# " not in out        # no section headers in single-set mode
    assert "godzilla" not in out  # dual_audio member shouldn't leak
