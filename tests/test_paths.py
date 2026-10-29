"""Tests for media_stack.paths: host-identity loader and shared .env parser."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import media_stack.paths as p
from media_stack.paths import load_env_file, ensure_var_dirs


@pytest.mark.parametrize("content, expected", [
    pytest.param(None, {}, id="missing-file-is-empty-like-the-shell-loader"),
    pytest.param(
        "# comment line\n"
        "\n"
        "KEY_A=plain\n"
        'KEY_B="double-quoted"\n'
        "KEY_C='single-quoted'\n"
        "  SPACED_KEY  =  spaced-value  \n",
        {"KEY_A": "plain", "KEY_B": "double-quoted", "KEY_C": "single-quoted", "SPACED_KEY": "spaced-value"},
        id="comments-blank-lines-quotes-and-spacing"),
    # values containing `=` (URL query strings, base64) survive the split
    pytest.param(
        "WIREGUARD_PRIVATE_KEY=abc/def+gh==\n"
        "JELLYFIN_URL=http://host:8096/?token=xyz=1\n",
        {"WIREGUARD_PRIVATE_KEY": "abc/def+gh==", "JELLYFIN_URL": "http://host:8096/?token=xyz=1"},
        id="first-equals-only"),
])
def test_load_env_file(tmp_path, content, expected):
    env = tmp_path / ".env"
    if content is not None:
        env.write_text(content)
    assert load_env_file(env) == expected


def _point_var_dirs_at(monkeypatch, base: Path):
    for name in ("log", "run", "state", "reviews"):
        monkeypatch.setattr(p, f"VAR_{name.upper()}", base / name)


def test_ensure_var_dirs_creates_tree(tmp_path, monkeypatch):
    """`ensure_var_dirs()` is the only mkdir helper in media_stack.paths; importing paths is read-only."""
    _point_var_dirs_at(monkeypatch, tmp_path)
    dirs = [tmp_path / n for n in ("log", "run", "state", "reviews")]
    assert not any(d.exists() for d in dirs)
    ensure_var_dirs()
    assert all(d.is_dir() for d in dirs)


def test_ensure_var_dirs_raises_on_failure(tmp_path, monkeypatch, capsys):
    """ensure_var_dirs raises SystemExit rather than letting a cron job crash later on its first log write."""
    # The parent is a regular file, so mkdir fails rather than succeeds.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    _point_var_dirs_at(monkeypatch, blocker)
    with pytest.raises(SystemExit) as ei:
        ensure_var_dirs()
    err = capsys.readouterr().err
    assert "cannot create" in err
    assert "4 of 4" in str(ei.value) or "4" in str(ei.value)
