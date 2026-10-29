"""Behavior tests for ops/healthcheck-alert.sh.

Pins exit-code preservation and recording: a non-zero healthcheck is appended to the issues log
with a WARN/FAIL header and the operator is never paged (no python3/curl is run, even with the
retired HEALTHCHECK_ALERT_NOTIFY=1 and Telegram credentials present); a green run records nothing
and tees the output.

Strategy: stage a minimal repo (stub healthcheck.sh with a controllable exit code) and point the
wrapper's env hooks (HEALTHCHECK_ISSUES_LOG, HEALTHCHECK_BIN) at tempfiles.
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


# Wrapper-control env vars, scrubbed from the inherited environment before each run so an
# operator-exported value cannot redirect the log or the healthcheck binary.
_WRAPPER_CONTROL_VARS = (
    "HEALTHCHECK_ISSUES_LOG",
    "HEALTHCHECK_BIN",
)


def _go(tmp_path, hc_exit, *, hc_stdout="synthetic", issues=None, extra_env=None):
    """Run the wrapper over a stub healthcheck exiting `hc_exit`; return (result, issues log path).

    The environment is the inherited one minus the wrapper-control vars, so the suite never
    depends on the surrounding shell or touches the real logs.
    """
    wrapper = _stage(tmp_path, hc_exit, hc_stdout)
    issues = issues or tmp_path / "issues.log"
    env = {k: v for k, v in os.environ.items() if k not in _WRAPPER_CONTROL_VARS}
    env["HEALTHCHECK_ISSUES_LOG"] = str(issues)
    env.update(extra_env or {})
    r = subprocess.run(["bash", str(wrapper)], capture_output=True, text=True, timeout=15, env=env)
    return r, issues


def test_rc0_silent_no_record_and_tees_stdout(tmp_path):
    """rc=0 -> exit 0, nothing recorded; output is teed so cron's >> capture works."""
    r, issues = _go(tmp_path, 0, hc_stdout="HEALTHCHECK_PROBE_OUT")
    assert r.returncode == 0
    assert not issues.exists()  # green = no record
    assert "HEALTHCHECK_PROBE_OUT" in r.stdout


@pytest.mark.parametrize("rc, severity, wrong_severity", [
    pytest.param(2, "WARN", "FAIL", id="rc2-warn"),
    pytest.param(1, "FAIL", "WARN", id="rc1-fail"),
    # only rc=2 is WARN: a probe that could not even run (exec failure) still surfaces as FAIL
    pytest.param(127, "FAIL", "WARN", id="rc127-fail"),
])
def test_nonzero_preserves_rc_and_records_the_issue(tmp_path, rc, severity, wrong_severity):
    """Exit code preserved, severity header and healthcheck output appended to the issues log."""
    # Neutral stub output (no "FAIL"/"WARN") so severity assertions test the wrapper's header.
    r, issues = _go(tmp_path, rc, hc_stdout="PROBE_BODY_MARKER")
    assert r.returncode == rc
    body = issues.read_text()
    assert f"healthcheck {severity}" in body            # structured severity header
    assert f"healthcheck {wrong_severity}" not in body  # wrong severity must be absent
    assert "PROBE_BODY_MARKER" in body                  # hc output captured in the record
    assert "PROBE_BODY_MARKER" in r.stdout              # and still teed to cron's log


def test_unwritable_issues_log_still_preserves_rc(tmp_path):
    """A log write failure is reported on stderr and never masks the healthcheck exit code."""
    r, _ = _go(tmp_path, 1, issues=tmp_path / "no-such-dir" / "issues.log")
    assert r.returncode == 1
    assert "could not write" in r.stderr


def test_never_pages_even_with_retired_notify_flag_and_credentials(tmp_path):
    """The Telegram path is gone: stub python3/curl first on PATH must never run, whatever the env says."""
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    called = tmp_path / "stub-called.log"
    for name in ("python3", "curl"):
        stub = stub_dir / name
        stub.write_text(f"#!/bin/bash\necho {name} >> {called}\nexit 0\n")
        stub.chmod(0o755)
    tg_env = tmp_path / "telegram.env"
    tg_env.write_text("TELEGRAM_BOT_TOKEN=real\nTELEGRAM_CHAT_ID=real\n")
    r, issues = _go(tmp_path, 1, hc_stdout="PROBE_BODY_MARKER", extra_env={
        "HEALTHCHECK_ALERT_NOTIFY": "1",
        "TELEGRAM_ENV": str(tg_env),
        "TELEGRAM_BOT_TOKEN": "real",
        "TELEGRAM_CHAT_ID": "real",
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
    })
    assert r.returncode == 1
    assert "healthcheck FAIL" in issues.read_text()  # still recorded
    assert not called.exists()                       # nothing tried to page
