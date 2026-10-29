"""Behavior tests for ops/healthcheck-alert.sh.

Pins exit-code preservation, graceful handling of a missing TELEGRAM_ENV, and the
default: a non-zero healthcheck is recorded to the issues log and the operator is NOT
paged unless HEALTHCHECK_ALERT_NOTIFY=1 (even with Telegram fully configured).

Strategy: stage a minimal repo (stub healthcheck.sh with a controllable exit code),
point the wrapper's env hooks (TELEGRAM_ENV, HEALTHCHECK_ISSUES_LOG,
HEALTHCHECK_ALERT_NOTIFY) at tempfiles, and detect any Telegram attempt via a stub
python3 on PATH.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "ops" / "healthcheck-alert.sh"


def _stage(tmp_path: Path, hc_exit: int, hc_stdout: str = "synthetic") -> Path:
    """Stage a minimal repo with a stub healthcheck.sh exiting `hc_exit`; return the wrapper path.

    The wrapper sits at <staged>/ops/healthcheck-alert.sh; <staged>/ is the repo root.
    """
    (tmp_path / "ops").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "media_stack").mkdir()
    (tmp_path / "var" / "log").mkdir(parents=True)
    (tmp_path / "var" / "run").mkdir()
    (tmp_path / "var" / "state").mkdir()
    (tmp_path / "var" / "reviews").mkdir()
    # Real wrapper, untouched
    staged_wrapper = tmp_path / "ops" / "healthcheck-alert.sh"
    shutil.copy(WRAPPER, staged_wrapper)
    # Real lib/paths.sh so the wrapper's source line works
    shutil.copy(REPO_ROOT / "lib" / "paths.sh", tmp_path / "lib" / "paths.sh")
    # Stub healthcheck.sh — controllable exit code
    hc = tmp_path / "healthcheck.sh"
    hc.write_text(
        "#!/bin/bash\n"
        f"echo {hc_stdout!r}\n"
        f"exit {hc_exit}\n"
    )
    hc.chmod(0o755)
    # Empty .env so the parser doesn't try to override anything
    (tmp_path / ".env").write_text("")
    return staged_wrapper


# Wrapper-control env vars, scrubbed from the inherited environment before each run
# so an operator-exported value cannot flip a default-opt-out test; a test opts in
# only via env_overrides.
_WRAPPER_CONTROL_VARS = (
    "HEALTHCHECK_ALERT_NOTIFY",
    "HEALTHCHECK_ISSUES_LOG",
    "HEALTHCHECK_BIN",
    "TELEGRAM_ENV",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)


def _run_wrapper(wrapper: Path, env_overrides: dict) -> subprocess.CompletedProcess:
    """Run the wrapper with a sanitized base environment plus explicit overrides, so the
    suite never depends on the surrounding shell or touches the real Telegram .env or logs."""
    base = {k: v for k, v in os.environ.items() if k not in _WRAPPER_CONTROL_VARS}
    return subprocess.run(
        ["bash", str(wrapper)],
        capture_output=True, text=True, timeout=15,
        env={**base, **env_overrides},
    )


def _python3_stub(tmp_path: Path) -> tuple[Path, Path]:
    """A stub python3 (first on PATH) recording any invocation and the Telegram env keys it saw."""
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    invocation_log = tmp_path / "py3-invocations.log"
    (stub_dir / "python3").write_text(
        "#!/bin/bash\n"
        f"echo \"argv: $*\" >> {invocation_log}\n"
        f"echo \"TOKEN: $TELEGRAM_BOT_TOKEN\" >> {invocation_log}\n"
        f"echo \"CHAT:  $TELEGRAM_CHAT_ID\" >> {invocation_log}\n"
        f"echo \"MSG:   $MSG\" >> {invocation_log}\n"
        "exit 0\n"
    )
    (stub_dir / "python3").chmod(0o755)
    return stub_dir, invocation_log


def _go(tmp_path, hc_exit, *, hc_stdout="synthetic", tg_env=None, notify=False):
    """Run the wrapper over a stub healthcheck exiting `hc_exit`.

    `tg_env` is the text of the Telegram .env (None: the file does not exist).
    Returns (result, issues log path, python3 invocation log path).
    """
    wrapper = _stage(tmp_path, hc_exit, hc_stdout)
    tg_env_path = tmp_path / "telegram.env"
    if tg_env is not None:
        tg_env_path.write_text(tg_env)
    stub_dir, invocation_log = _python3_stub(tmp_path)
    issues = tmp_path / "issues.log"
    env = {
        "TELEGRAM_ENV": str(tg_env_path),
        "HEALTHCHECK_ISSUES_LOG": str(issues),
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
    }
    if notify:
        env["HEALTHCHECK_ALERT_NOTIFY"] = "1"
    return _run_wrapper(wrapper, env), issues, invocation_log


# --- exit-code preservation + the operator-silent default ---

def test_rc0_silent_no_record_and_tees_stdout(tmp_path):
    """rc=0 -> exit 0, nothing recorded, no page even if env present; output is teed so cron's >> capture works."""
    r, issues, invocation_log = _go(
        tmp_path, 0, hc_stdout="HEALTHCHECK_PROBE_OUT",
        tg_env="TELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=c\n")
    assert r.returncode == 0
    assert not issues.exists()  # green = no record
    assert not invocation_log.exists()
    assert "HEALTHCHECK_PROBE_OUT" in r.stdout


@pytest.mark.parametrize("rc, severity, wrong_severity, tg_env", [
    pytest.param(1, "FAIL", "WARN", None, id="rc1-fail-recorded"),
    pytest.param(2, "WARN", "FAIL", None, id="rc2-warn-recorded"),
    # even with a fully valid Telegram .env the default (gate off) must not page
    pytest.param(1, "FAIL", "WARN", "TELEGRAM_BOT_TOKEN=real\nTELEGRAM_CHAT_ID=real\n",
                 id="rc1-valid-env-gate-off"),
])
def test_default_records_the_issue_and_does_not_page(tmp_path, rc, severity, wrong_severity, tg_env):
    """DEFAULT (NOTIFY unset): exit code preserved, severity recorded to the issues log, NO Telegram attempted."""
    # Neutral stub output (no "FAIL"/"WARN") so severity assertions test the wrapper's header.
    r, issues, invocation_log = _go(tmp_path, rc, hc_stdout="PROBE_BODY_MARKER", tg_env=tg_env)
    assert r.returncode == rc
    body = issues.read_text()
    assert f"healthcheck {severity}" in body            # structured severity header
    assert f"healthcheck {wrong_severity}" not in body  # wrong severity must be absent
    assert "PROBE_BODY_MARKER" in body                  # hc output captured in the record
    assert not invocation_log.exists()                  # operator was NOT paged


# --- opt-in page path (HEALTHCHECK_ALERT_NOTIFY=1) ---

def test_notify_on_invokes_telegram_on_failure(tmp_path):
    """rc=1 + NOTIFY=1 + valid TELEGRAM_ENV -> invokes media_stack.cli
    telegram_send with TOKEN/CHAT_ID/MSG in env."""
    r, _, invocation_log = _go(
        tmp_path, 1, hc_stdout="PROBE_BODY_MARKER", notify=True,
        tg_env="TELEGRAM_BOT_TOKEN=fake-token\nTELEGRAM_CHAT_ID=fake-chat\n")
    assert r.returncode == 1
    assert invocation_log.exists()
    log = invocation_log.read_text()
    assert "-m media_stack.cli telegram_send" in log
    assert "TOKEN: fake-token" in log
    assert "CHAT:  fake-chat" in log
    assert "healthcheck FAIL" in log     # page carries the structured severity
    assert "PROBE_BODY_MARKER" in log    # message body includes hc output


def test_notify_on_missing_env_graceful(tmp_path):
    """rc=1 + NOTIFY=1 + no TELEGRAM_ENV -> preserves rc, warns to stderr,
    and the issue is still recorded."""
    r, issues, _ = _go(tmp_path, 1, notify=True)
    assert r.returncode == 1
    assert "not readable" in r.stderr or "skipping notify" in r.stderr
    assert issues.exists()  # recorded regardless of page outcome
