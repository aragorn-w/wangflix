"""Behavior tests for ops/healthcheck-alert.sh.

Codex round-14 #3: the alerting wrapper had `bash -n` syntax coverage
only; the operational guarantees (exit-code preservation, behavior on
non-zero, missing TELEGRAM_ENV graceful) were untested.

Operator instruction 2026-06-08 ("don't text me with healthcheck
issues; just resolve them yourself") changed the default: a non-zero
healthcheck is RECORDED to the issues log and the operator is NOT
paged unless HEALTHCHECK_ALERT_NOTIFY=1.  These tests pin that default
(esp. test_notify_off_with_valid_env_does_not_page — the gate must
suppress the page even when Telegram is fully configured) plus the
opt-in page path.

Strategy: stage a minimal repo (stub healthcheck.sh with a controllable
exit code), point the wrapper's env-overridable hooks (TELEGRAM_ENV,
HEALTHCHECK_ISSUES_LOG, HEALTHCHECK_ALERT_NOTIFY) at tempfiles, and
detect any Telegram attempt via a stub python3 on PATH.
"""

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "ops" / "healthcheck-alert.sh"


def _stage(tmp_path: Path, hc_exit: int, hc_stdout: str = "synthetic") -> Path:
    """Stage a minimal repo layout with a stub healthcheck.sh that
    exits `hc_exit`.  Returns the staged wrapper path.

    Layout matches what `_here="$(cd "$(dirname "$0")/.." && pwd)"`
    expects: wrapper at <staged>/ops/healthcheck-alert.sh and the
    parent <staged>/ is treated as the repo root.
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


# Wrapper-control env vars.  These are scrubbed from the inherited
# environment before each run so a value leaking in from the operator's
# shell (e.g. `HEALTHCHECK_ALERT_NOTIFY=1` exported in the dev session)
# can't flip a default-opt-out test into pinging through the stub —
# codex round-rl1 #1.  A test opts into any of these ONLY by passing it
# explicitly via env_overrides.
_WRAPPER_CONTROL_VARS = (
    "HEALTHCHECK_ALERT_NOTIFY",
    "HEALTHCHECK_ISSUES_LOG",
    "HEALTHCHECK_BIN",
    "TELEGRAM_ENV",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)


def _run_wrapper(wrapper: Path, env_overrides: dict) -> subprocess.CompletedProcess:
    """Run the wrapper with a SANITIZED base environment plus explicit
    overrides (TELEGRAM_ENV / HEALTHCHECK_BIN / HEALTHCHECK_ISSUES_LOG /
    HEALTHCHECK_ALERT_NOTIFY).  Scrubbing the control vars from os.environ
    first guarantees a test sees the default unless it opts in via
    env_overrides — so the suite stays deterministic regardless of what
    the surrounding shell exports, and never touches the operator's real
    Telegram .env or production log paths."""
    base = {k: v for k, v in os.environ.items() if k not in _WRAPPER_CONTROL_VARS}
    return subprocess.run(
        ["bash", str(wrapper)],
        capture_output=True, text=True, timeout=15,
        env={**base, **env_overrides},
    )


def _python3_stub(tmp_path: Path) -> tuple[Path, Path]:
    """A stub python3 (first on PATH) that records any invocation +
    the Telegram env keys it saw.  Used to assert whether the wrapper
    attempted a Telegram page."""
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


# --- exit-code preservation + the operator-silent default ---

def test_rc0_silent_no_record(tmp_path):
    """rc=0 → exit 0, nothing recorded, no page even if env present."""
    wrapper = _stage(tmp_path, hc_exit=0)
    tg_env = tmp_path / "telegram.env"
    tg_env.write_text("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=c\n")
    issues = tmp_path / "issues.log"
    r = _run_wrapper(wrapper, {
        "TELEGRAM_ENV": str(tg_env),
        "HEALTHCHECK_ISSUES_LOG": str(issues),
    })
    assert r.returncode == 0
    assert not issues.exists()  # green = no record


def test_rc1_default_records_no_page(tmp_path):
    """DEFAULT (NOTIFY unset): rc=1 → exit 1, FAIL recorded to issues
    log, NO Telegram attempted."""
    # Neutral stub output (no "FAIL"/"WARN" substring) so the severity
    # assertions test the wrapper's header, not the fixture text —
    # codex round-rl1 #4.
    wrapper = _stage(tmp_path, hc_exit=1, hc_stdout="PROBE_BODY_MARKER")
    issues = tmp_path / "issues.log"
    stub_dir, invocation_log = _python3_stub(tmp_path)
    r = _run_wrapper(wrapper, {
        "TELEGRAM_ENV": "/nonexistent",
        "HEALTHCHECK_ISSUES_LOG": str(issues),
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
    })
    assert r.returncode == 1
    assert issues.exists()
    body = issues.read_text()
    assert "healthcheck FAIL" in body       # structured severity header
    assert "healthcheck WARN" not in body   # wrong severity must be absent
    assert "PROBE_BODY_MARKER" in body      # hc output captured in the record
    assert not invocation_log.exists()      # operator was NOT paged


def test_rc2_default_records_warn(tmp_path):
    """DEFAULT: rc=2 (WARN-only) → exit 2, WARN recorded, no page."""
    wrapper = _stage(tmp_path, hc_exit=2, hc_stdout="PROBE_BODY_MARKER")
    issues = tmp_path / "issues.log"
    stub_dir, invocation_log = _python3_stub(tmp_path)
    r = _run_wrapper(wrapper, {
        "TELEGRAM_ENV": "/nonexistent",
        "HEALTHCHECK_ISSUES_LOG": str(issues),
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
    })
    assert r.returncode == 2
    body = issues.read_text()
    assert "healthcheck WARN" in body       # structured severity header
    assert "healthcheck FAIL" not in body   # wrong severity must be absent
    assert not invocation_log.exists()


def test_notify_off_with_valid_env_does_not_page(tmp_path):
    """The operator instruction made concrete: even with a fully valid
    Telegram .env, the DEFAULT (gate off) must NOT page the operator.
    Regression guard against accidentally re-enabling healthcheck texts."""
    wrapper = _stage(tmp_path, hc_exit=1)
    tg_env = tmp_path / "telegram.env"
    tg_env.write_text("TELEGRAM_BOT_TOKEN=real\nTELEGRAM_CHAT_ID=real\n")
    issues = tmp_path / "issues.log"
    stub_dir, invocation_log = _python3_stub(tmp_path)
    r = _run_wrapper(wrapper, {
        "TELEGRAM_ENV": str(tg_env),
        "HEALTHCHECK_ISSUES_LOG": str(issues),
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
    })
    assert r.returncode == 1
    assert issues.exists()
    assert not invocation_log.exists()  # gate suppressed the page


def test_tees_healthcheck_stdout(tmp_path):
    """Wrapper must tee healthcheck output so cron's >> capture still
    works (otherwise the log file would only show wrapper metadata)."""
    wrapper = _stage(tmp_path, hc_exit=0, hc_stdout="HEALTHCHECK_PROBE_OUT")
    r = _run_wrapper(wrapper, {"TELEGRAM_ENV": "/nonexistent"})
    assert r.returncode == 0
    assert "HEALTHCHECK_PROBE_OUT" in r.stdout


# --- opt-in page path (HEALTHCHECK_ALERT_NOTIFY=1) ---

def test_notify_on_invokes_telegram_on_failure(tmp_path):
    """rc=1 + NOTIFY=1 + valid TELEGRAM_ENV → invokes media_stack.cli
    telegram_send with TOKEN/CHAT_ID/MSG in env."""
    wrapper = _stage(tmp_path, hc_exit=1, hc_stdout="PROBE_BODY_MARKER")
    tg_env = tmp_path / "telegram.env"
    tg_env.write_text(
        "TELEGRAM_BOT_TOKEN=fake-token\n"
        "TELEGRAM_CHAT_ID=fake-chat\n"
    )
    stub_dir, invocation_log = _python3_stub(tmp_path)
    r = _run_wrapper(wrapper, {
        "HEALTHCHECK_ALERT_NOTIFY": "1",
        "TELEGRAM_ENV": str(tg_env),
        "HEALTHCHECK_ISSUES_LOG": str(tmp_path / "issues.log"),
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
    })
    assert r.returncode == 1
    assert invocation_log.exists()
    log = invocation_log.read_text()
    assert "-m media_stack.cli telegram_send" in log
    assert "TOKEN: fake-token" in log
    assert "CHAT:  fake-chat" in log
    assert "healthcheck FAIL" in log     # page carries the structured severity
    assert "PROBE_BODY_MARKER" in log    # message body includes hc output


def test_notify_on_missing_env_graceful(tmp_path):
    """rc=1 + NOTIFY=1 + no TELEGRAM_ENV → preserves rc, warns to stderr,
    and the issue is still recorded."""
    wrapper = _stage(tmp_path, hc_exit=1)
    issues = tmp_path / "issues.log"
    r = _run_wrapper(wrapper, {
        "HEALTHCHECK_ALERT_NOTIFY": "1",
        "TELEGRAM_ENV": str(tmp_path / "absent.env"),
        "HEALTHCHECK_ISSUES_LOG": str(issues),
    })
    assert r.returncode == 1
    assert "not readable" in r.stderr or "skipping notify" in r.stderr
    assert issues.exists()  # recorded regardless of page outcome
