"""Backward-compat re-export shim — moved to media_stack.paths.

Import from `media_stack.paths` directly in new code.  This shim exists
so existing call sites (`from media_paths import MEDIA_STACK_ROOT`)
keep working unchanged.  Will be removed once every caller migrates.

Also preserves the `python3 media_paths.py` CLI dump behavior — the
pre-split script printed all resolved values; operators / agents may
have shell aliases or external diagnostics relying on that output
(codex round-5 #5).
"""
from media_stack.paths import *  # noqa: F401,F403
from media_stack.paths import __all__  # noqa: F401


if __name__ == "__main__":
    # Delegate to the package helper so both entry points produce
    # identical KEY=value output for `eval`-style consumers.
    from media_stack.paths import dump_values
    dump_values()
