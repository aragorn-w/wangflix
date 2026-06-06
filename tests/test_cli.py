"""Tests for media_stack.cli — the shared CLI dispatcher invoked by
shell scripts as `python3 -m media_stack.cli <subcommand>`."""

import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack import cli


def test_main_no_args_prints_usage_and_exit_2(capsys):
    rc = cli.main([])
    captured = capsys.readouterr()
    assert rc == 2
    assert "usage" in captured.err.lower()


def test_main_unknown_subcommand_exit_2(capsys):
    rc = cli.main(["nonexistent_thing"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown" in captured.err.lower()


def test_canonicalize_via_main(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", StringIO("ja\nen\n"))
    rc = cli.main(["canonicalize"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == "eng,jpn"


def test_expand_via_main(capsys):
    rc = cli.main(["expand", "jpn"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    # ja, japanese, jpn — sorted, comma-joined
    assert out == "ja,japanese,jpn"


def test_expand_without_family_errors(capsys):
    rc = cli.main(["expand"])
    assert rc == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_has_eng_sdh_no_args(capsys):
    rc = cli.main(["has_eng_sdh"])
    assert rc == 1
    assert "usage" in capsys.readouterr().err.lower()


def test_dual_audio_no_args(capsys):
    rc = cli.main(["dual_audio"])
    assert rc == 1
    assert "usage" in capsys.readouterr().err.lower()


def test_has_eng_sdh_with_mocked_ffprobe(capsys):
    """Mock ffprobe to return a stream with eng SDH; should print 'yes'."""
    fake_json = (
        '{"streams":[{"tags":{"language":"eng"},'
        '"disposition":{"hearing_impaired":1}}]}'
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = type("R", (), {
            "returncode": 0, "stdout": fake_json, "stderr": "",
        })()
        rc = cli.main(["has_eng_sdh", "/fake/file.mkv"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "yes"


def test_has_eng_sdh_no_match_prints_nothing(capsys):
    """A file with only French SDH should NOT print 'yes'."""
    fake_json = (
        '{"streams":[{"tags":{"language":"fre"},'
        '"disposition":{"hearing_impaired":1}}]}'
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = type("R", (), {
            "returncode": 0, "stdout": fake_json, "stderr": "",
        })()
        rc = cli.main(["has_eng_sdh", "/fake/file.mkv"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


def test_dual_audio_jpn_and_eng(capsys):
    fake_json = (
        '{"streams":['
        '{"tags":{"language":"jpn"}},'
        '{"tags":{"language":"eng"}}'
        ']}'
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = type("R", (), {
            "returncode": 0, "stdout": fake_json, "stderr": "",
        })()
        rc = cli.main(["dual_audio", "/fake/file.mkv"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "eng,jpn"


def test_dual_audio_untagged_collapses_to_eng(capsys):
    """Untagged audio stream should canonicalize to 'eng' per
    media_lang.canonical_lang(None)."""
    fake_json = (
        '{"streams":['
        '{"tags":{}},'
        '{"tags":{"language":"jpn"}}'
        ']}'
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = type("R", (), {
            "returncode": 0, "stdout": fake_json, "stderr": "",
        })()
        rc = cli.main(["dual_audio", "/fake/file.mkv"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "eng,jpn"


def test_telegram_send_missing_env_returns_1(capsys, monkeypatch):
    """Codex round-6 #7 added the telegram_send subcommand; the
    inline-heredoc removal in normalize-driver.sh now routes
    through this dispatch path.  Must fail-loud (rc=1 + stderr)
    if any of the three required env vars is unset, so cron mail
    surfaces the misconfig instead of a silent no-op."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("MSG", raising=False)
    rc = cli.main(["telegram_send"])
    assert rc == 1
    assert "missing" in capsys.readouterr().err.lower()


def test_telegram_send_success_routes_through_adapter(monkeypatch):
    """Successful send: subcommand pulls env, calls the adapter,
    returns 0.  Verifies the dispatcher passes the right values
    through (not the docstring's stale claims)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat-id")
    monkeypatch.setenv("MSG", "normalize complete: 209/209")
    with patch.object(cli, "telegram_send", return_value=True) as mock_send:
        rc = cli.main(["telegram_send"])
    assert rc == 0
    mock_send.assert_called_once_with(
        "123:ABC", "test-chat-id", "normalize complete: 209/209"
    )


def test_print_keywords_default_dumps_all_sets(capsys):
    """Codex round-cleanup #6: the subcommand was documented in
    `media_stack/config.py`'s docstring but never implemented.
    Default invocation dumps every keyword set with section headers."""
    rc = cli.main(["print_keywords"])
    assert rc == 0
    out = capsys.readouterr().out
    # Each set header present
    assert "# japanese" in out
    assert "# korean" in out
    assert "# dual_audio" in out


def test_print_keywords_single_set_no_header(capsys):
    """When a specific set is requested, output is JUST that set's
    keywords — no headers — so shell scripts can `grep -Fxq` cleanly."""
    rc = cli.main(["print_keywords", "japanese"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "anime" in out         # known JAPANESE_KEYWORDS member
    assert "# " not in out        # no section headers in single-set mode
    assert "godzilla" not in out  # dual_audio member shouldn't leak


def test_print_keywords_unknown_set_errors(capsys):
    rc = cli.main(["print_keywords", "klingon"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown set" in err
    assert "klingon" in err


def test_telegram_send_failure_propagates(monkeypatch):
    """Adapter returned False → dispatcher returns 1 so cron sees the
    non-zero exit."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setenv("MSG", "m")
    with patch.object(cli, "telegram_send", return_value=False):
        rc = cli.main(["telegram_send"])
    assert rc == 1
