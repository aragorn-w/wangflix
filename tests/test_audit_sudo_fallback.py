"""Shell-level regression test for ops/audit.sh's mode-640 sudo fallback.

Review LOW #3 (2026-06-04): when `systemctl cat <unit>` fails as the normal
user (a mode-640 unit like realtek-fix.service), audit.sh retries the read
with `sudo -n systemctl cat` — scoped to that single read, NOT the whole
script (running the script as root would read root's crontab/$HOME and emit
false drift).  This stages a temp repo + PATH stubs for systemctl/sudo/crontab
and asserts the per-unit verdict for the two new branches:

  * user `cat` fails BUT `sudo -n cat` succeeds → unit is read + verified
  * user `cat` fails AND `sudo -n cat` fails    → WARN (unverifiable)

(Case A — the normal user read succeeds — is the pre-existing path, exercised
by every real run.)  Assertions are on the per-unit output line, so the cron
and logrotate checks in the same run don't affect the result.
"""
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT = REPO_ROOT / "ops" / "audit.sh"

# No placeholders → render_snapshot() leaves it unchanged, so the "live" body
# the sudo stub returns matches the snapshot exactly.
UNIT_BODY = "[Unit]\nDescription=Test\n[Service]\nType=simple\nExecStart=/bin/true\n"


def _stage(tmp_path: Path) -> Path:
    for d in ("ops/systemd", "ops/cron.d", "ops/logrotate.d", "lib", "bin"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    shutil.copy(AUDIT, tmp_path / "ops" / "audit.sh")
    shutil.copy(REPO_ROOT / "lib" / "paths.sh", tmp_path / "lib" / "paths.sh")
    (tmp_path / ".env").write_text("")
    (tmp_path / "ops" / "systemd" / "test-unit.service").write_text(UNIT_BODY)
    (tmp_path / "ops" / "cron.d" / "media-stack.crontab").write_text("# header only\n")
    (tmp_path / "ops" / "logrotate.d" / "media-stack").write_text("# lr\n")
    (tmp_path / "unit-body.txt").write_text(UNIT_BODY)

    bindir = tmp_path / "bin"
    # systemctl stub: unit is "installed"; the normal-user `cat` always fails
    # (simulating mode-640), forcing audit.sh down the sudo-retry path.
    (bindir / "systemctl").write_text(
        '#!/bin/bash\n'
        'if [ "$1" = "list-unit-files" ]; then echo "test-unit.service enabled enabled"; exit 0; fi\n'
        'if [ "$1" = "cat" ]; then exit 1; fi\n'
        'exit 0\n'
    )
    # sudo stub: `sudo -n systemctl cat <unit>` prints the body + exits 0 when
    # SUDO_CAT_OK=1, else exits 1 (passwordless sudo unavailable).
    (bindir / "sudo").write_text(
        '#!/bin/bash\n'
        '[ "$1" = "-n" ] && shift\n'
        'if [ "$1" = "systemctl" ] && [ "$2" = "cat" ]; then\n'
        '  if [ "${SUDO_CAT_OK:-0}" = "1" ]; then cat "$UNIT_BODY_FILE"; exit 0; else exit 1; fi\n'
        'fi\n'
        'exit 0\n'
    )
    # crontab stub: empty → matches the comment-only snapshot (no cron noise).
    (bindir / "crontab").write_text('#!/bin/bash\nexit 0\n')
    for f in ("systemctl", "sudo", "crontab"):
        (bindir / f).chmod(0o755)
    return tmp_path


def _run(tmp_path: Path, sudo_cat_ok: bool) -> str:
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path / 'bin'}:{env['PATH']}"
    env["SUDO_CAT_OK"] = "1" if sudo_cat_ok else "0"
    env["UNIT_BODY_FILE"] = str(tmp_path / "unit-body.txt")
    r = subprocess.run(["bash", str(tmp_path / "ops" / "audit.sh")],
                       env=env, capture_output=True, text=True)
    return r.stdout + r.stderr


def test_sudo_retry_verifies_mode640_unit(tmp_path):
    _stage(tmp_path)
    out = _run(tmp_path, sudo_cat_ok=True)
    # user `cat` failed, `sudo -n cat` succeeded → unit read + compared → OK
    assert "OK: test-unit.service matches" in out
    assert "test-unit.service installed but unreadable" not in out


def test_warns_when_sudo_also_unavailable(tmp_path):
    _stage(tmp_path)
    out = _run(tmp_path, sudo_cat_ok=False)
    # both reads failed → the graceful WARN (not a silent pass, not a verify)
    assert "test-unit.service installed but unreadable (mode-640 + no passwordless sudo)" in out
    assert "OK: test-unit.service matches" not in out
