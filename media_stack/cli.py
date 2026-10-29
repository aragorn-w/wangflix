"""CLI dispatcher (`python3 -m media_stack.cli <subcommand>`) for shell scripts.

  canonicalize         lang tags on stdin -> canonical families
  expand FAMILY        alias list for eng/jpn/kor
  has_eng_sdh <file>   "yes" if the file has an English SDH sub
  dual_audio <file>    canonical audio langs, sorted and comma-joined
  telegram_send        send $MSG via TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
  print_keywords [SET] japanese|korean|dual_audio (default: all)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from media_stack.lang import canonical_lang, _cli_canonicalize, _cli_expand
from media_stack.clients.telegram import send as telegram_send


def cmd_canonicalize(_args: list[str]) -> int:
    return _cli_canonicalize()


def cmd_expand(args: list[str]) -> int:
    if not args:
        print("usage: cli.py expand FAMILY", file=sys.stderr)
        return 2
    return _cli_expand(args[0])


def cmd_has_eng_sdh(args: list[str]) -> int:
    """Exit 1 only on argv/probe failure."""
    if not args:
        print("usage: cli.py has_eng_sdh <file>", file=sys.stderr)
        return 1
    path = args[0]
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "s",
             "-print_format", "json", "-show_streams", path],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return 1
        d = json.loads(r.stdout)
    except Exception:
        return 1
    for s in d.get("streams", []):
        lang = canonical_lang(((s.get("tags") or {}).get("language")))
        hi = (s.get("disposition") or {}).get("hearing_impaired", 0) == 1
        if lang == "eng" and hi:
            print("yes")
            return 0
    return 0


def cmd_dual_audio(args: list[str]) -> int:
    """Untagged counts as `eng`. Probe failure prints nothing, which callers read as "not dual yet"."""
    if not args:
        print("usage: cli.py dual_audio <file>", file=sys.stderr)
        return 1
    path = args[0]
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-print_format", "json", "-show_streams", path],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return 0
        d = json.loads(r.stdout)
    except Exception:
        return 0
    out: set[str] = set()
    for s in d.get("streams", []):
        out.add(canonical_lang((s.get("tags") or {}).get("language")))
    print(",".join(sorted(out)))
    return 0


def cmd_print_keywords(args: list[str]) -> int:
    from media_stack.config import (
        JAPANESE_KEYWORDS, KOREAN_KEYWORDS, DUAL_AUDIO_KEYWORDS,
    )
    sets = {
        "japanese":   JAPANESE_KEYWORDS,
        "korean":     KOREAN_KEYWORDS,
        "dual_audio": DUAL_AUDIO_KEYWORDS,
    }
    if not args:
        for name in sorted(sets):
            print(f"# {name}")
            for kw in sorted(sets[name]):
                print(kw)
            print()
        return 0
    target = args[0].lower()
    if target not in sets:
        print(f"unknown set: {target!r} (valid: {', '.join(sorted(sets))})",
              file=sys.stderr)
        return 2
    for kw in sorted(sets[target]):
        print(kw)
    return 0


def cmd_telegram_send(_args: list[str]) -> int:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    msg     = os.environ.get("MSG")
    if not token or not chat_id or not msg:
        print("telegram_send: missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / MSG",
              file=sys.stderr)
        return 1
    return 0 if telegram_send(token, chat_id, msg) else 1


_SUBCOMMANDS = {
    "canonicalize":   cmd_canonicalize,
    "expand":         cmd_expand,
    "has_eng_sdh":    cmd_has_eng_sdh,
    "dual_audio":     cmd_dual_audio,
    "telegram_send":  cmd_telegram_send,
    "print_keywords": cmd_print_keywords,
}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(f"usage: python3 -m media_stack.cli "
              f"{{{'|'.join(_SUBCOMMANDS)}}} [...]", file=sys.stderr)
        return 2
    name, rest = args[0], args[1:]
    fn = _SUBCOMMANDS.get(name)
    if fn is None:
        print(f"unknown subcommand: {name!r}", file=sys.stderr)
        return 2
    return fn(rest)


if __name__ == "__main__":
    sys.exit(main())
