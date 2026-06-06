"""Backward-compat re-export shim — moved to media_stack.lang.

Import from `media_stack.lang` directly in new code.  This shim exists
so existing call sites (`from media_lang import canonical_lang`) keep
working unchanged.  Will be removed once every caller migrates.

CLI mode (`python3 media_lang.py canonicalize`) still works for shell
scripts via this shim's __main__ block; eventually they'll switch to
`python3 -m media_stack.lang` instead.
"""
from media_stack.lang import *  # noqa: F401,F403
from media_stack.lang import (  # noqa: F401
    ENG_AUDIO_LANGS, JPN_AUDIO_LANGS, KOR_AUDIO_LANGS,
    canonical_lang,
    _cli_canonicalize, _cli_expand,
)

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: media_lang.py {canonicalize|expand FAMILY}", file=sys.stderr)
        sys.exit(2)
    mode = sys.argv[1]
    if mode == "canonicalize":
        sys.exit(_cli_canonicalize())
    if mode == "expand" and len(sys.argv) >= 3:
        sys.exit(_cli_expand(sys.argv[2]))
    print("usage: media_lang.py {canonicalize|expand FAMILY}", file=sys.stderr)
    sys.exit(2)
