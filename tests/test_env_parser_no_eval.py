"""Pytest wrapper for `tests/test_env_parser_no_eval.sh`, which proves `lib/paths.sh` does not eval values.

The shell script holds the assertions (injection payloads in a synthetic .env, sentinel
not created, values captured as literal text); this wrapper runs it so pytest picks it up.
"""

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SHELL_TEST = REPO_ROOT / "tests" / "test_env_parser_no_eval.sh"


def test_lib_paths_sh_is_non_executing():
    """`lib/paths.sh` must use a non-eval assignment (`printf -v`) so `MEDIA_ROOT="$(touch /tmp/poc)"` in `.env` stays literal text."""
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
