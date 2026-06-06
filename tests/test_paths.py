"""Tests for media_stack.paths — host-identity loader + shared
.env parser used by all Python host-side scripts."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.paths import load_env_file, ensure_var_dirs


def test_load_env_missing_file(tmp_path):
    """Missing .env returns an empty dict — same contract as the
    shell loader and consistent with our 'silent default-fall-
    through' policy at the import boundary."""
    assert load_env_file(tmp_path / "absent.env") == {}


def test_load_env_basic(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment line\n"
        "\n"
        "KEY_A=plain\n"
        'KEY_B="double-quoted"\n'
        "KEY_C='single-quoted'\n"
        "  SPACED_KEY  =  spaced-value  \n"
    )
    out = load_env_file(env)
    assert out["KEY_A"] == "plain"
    assert out["KEY_B"] == "double-quoted"
    assert out["KEY_C"] == "single-quoted"
    assert out["SPACED_KEY"] == "spaced-value"
    assert "# comment line" not in out


def test_load_env_first_equals_only(tmp_path):
    """Values containing `=` (URL query strings, base64) must survive
    the split intact."""
    env = tmp_path / ".env"
    env.write_text("WIREGUARD_PRIVATE_KEY=abc/def+gh==\n"
                   "JELLYFIN_URL=http://host:8096/?token=xyz=1\n")
    out = load_env_file(env)
    assert out["WIREGUARD_PRIVATE_KEY"] == "abc/def+gh=="
    assert out["JELLYFIN_URL"] == "http://host:8096/?token=xyz=1"


def test_load_env_no_trailing_newline(tmp_path):
    """A file without a trailing newline must still surface the last
    key — replicators often hand-edit .env without trailing \\n."""
    env = tmp_path / ".env"
    env.write_text("LAST_KEY=present")
    assert load_env_file(env) == {"LAST_KEY": "present"}


def test_ensure_var_dirs_creates_tree(tmp_path, monkeypatch):
    """`ensure_var_dirs()` is the only mkdir-touching helper in
    media_stack.paths (codex round-4 module-split #7).  Importing
    paths is read-only; writers explicitly call this before first
    write."""
    # Point the VAR_* globals at tmp_path before calling.
    import media_stack.paths as p
    monkeypatch.setattr(p, "VAR_LOG", tmp_path / "log")
    monkeypatch.setattr(p, "VAR_RUN", tmp_path / "run")
    monkeypatch.setattr(p, "VAR_STATE", tmp_path / "state")
    monkeypatch.setattr(p, "VAR_REVIEWS", tmp_path / "reviews")
    for d in (tmp_path / "log", tmp_path / "run",
              tmp_path / "state", tmp_path / "reviews"):
        assert not d.exists()
    ensure_var_dirs()
    for d in (tmp_path / "log", tmp_path / "run",
              tmp_path / "state", tmp_path / "reviews"):
        assert d.is_dir()


def test_ensure_var_dirs_raises_on_failure(tmp_path, monkeypatch, capsys):
    """Codex round-6 #4: silent failure here would let a cron job
    "start" and then crash on the first log write with no diagnostic.
    Now ensure_var_dirs raises SystemExit so cron mail / journalctl
    surface the problem."""
    import media_stack.paths as p
    # Point VAR_DIR at a path that can't be created (parent is a
    # regular file, so mkdir will EEXIST/EOTDIR rather than succeed).
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr(p, "VAR_LOG", blocker / "log")
    monkeypatch.setattr(p, "VAR_RUN", blocker / "run")
    monkeypatch.setattr(p, "VAR_STATE", blocker / "state")
    monkeypatch.setattr(p, "VAR_REVIEWS", blocker / "reviews")
    import pytest
    with pytest.raises(SystemExit) as ei:
        ensure_var_dirs()
    err = capsys.readouterr().err
    assert "cannot create" in err
    assert "4 of 4" in str(ei.value) or "4" in str(ei.value)
