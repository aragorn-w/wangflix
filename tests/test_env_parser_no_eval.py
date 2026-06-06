"""Pytest wrapper for the shell regression test that proves
`lib/paths.sh` does not eval values.

The actual test logic lives in `tests/test_env_parser_no_eval.sh`
(synthetic .env with injection payloads, source the parser, assert
sentinel was NOT created, assert values captured as literal text).
Keeping it as bash makes the bash-specific assertion easy to read,
but agents running the canonical `python3 -m pytest tests/ -q`
command would otherwise miss it (codex round-12 #1).  This wrapper
shells out, captures rc + output, and asserts pass.
"""

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SHELL_TEST = REPO_ROOT / "tests" / "test_env_parser_no_eval.sh"


def test_lib_paths_sh_is_non_executing():
    """Regression for codex round-10 #1: `lib/paths.sh` must use
    `printf -v` (or another non-eval assignment) so a malicious
    `MEDIA_ROOT="$(touch /tmp/poc)"` line in `.env` is captured as
    literal text and the touch is NOT executed."""
    r = subprocess.run(
        ["bash", str(SHELL_TEST)],
        capture_output=True, text=True, timeout=30,
        cwd=str(REPO_ROOT),
    )
    assert r.returncode == 0, (
        f"shell regression failed (rc={r.returncode}):\n"
        f"STDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    )
    assert "PASS" in r.stdout, r.stdout
