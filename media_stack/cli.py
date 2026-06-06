"""Central CLI dispatcher — `python3 -m media_stack.cli <subcommand>`.

Replaces the ad-hoc inline-python-in-bash heredocs scattered across
shell scripts (AUDIT A3).  Shell scripts that need a small chunk of
Python logic (canonicalize a list of language tags, check for an
English SDH sub track, dump keyword sets, send a Telegram message,
etc.) call into this module instead of embedding the Python source
in a `python3 -c '...'` heredoc.  (Healthcheck JSON rendering still
lives in `healthcheck.sh` itself — that may move here eventually
but is not currently a subcommand.)

Subcommands:
  canonicalize         Read lang tags from stdin; print canonical families.
                       (replaces `media_lang.py canonicalize`)
  has_eng_sdh <file>   Print "yes" + exit 0 if file has English SDH sub.
                       (replaces a one-shot inline ffprobe parser)
  dual_audio <file>    Print canonical audio langs, sorted+comma-joined.
                       (replaces a one-shot inline ffprobe parser)
  telegram_send        Read TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / MSG
                       from env; send via media_stack.clients.telegram.
                       (replaces normalize-driver.sh inline heredoc;
                       codex round-6 #7)
  print_keywords [SET] Dump the language/dual-audio keyword sets from
                       media_stack.config.  SET is one of:
                       japanese|korean|dual_audio (default: all).
                       Lets shell scripts inspect the canonical lists
                       without regex-grepping the live module.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from media_stack.lang import canonical_lang, _cli_canonicalize, _cli_expand
from media_stack.clients.telegram import send as telegram_send


def cmd_canonicalize(_args: list[str]) -> int:
    """Read raw lang tags one-per-line from stdin; print canonical
    families sorted + comma-joined.  Reused via media_stack.lang's CLI
    helper."""
    return _cli_canonicalize()


def cmd_expand(args: list[str]) -> int:
    """Print the alias list for a canonical family (eng/jpn/kor)."""
    if not args:
        print("usage: cli.py expand FAMILY", file=sys.stderr)
        return 2
    return _cli_expand(args[0])


def cmd_has_eng_sdh(args: list[str]) -> int:
    """Print `yes` to stdout + exit 0 iff the MKV at args[0] contains
    a subtitle stream tagged as English (any canonical-eng alias) with
    hearing_impaired disposition.  Otherwise prints nothing + exits 0.
    Exits 1 only on argv/probe failures.
    """
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
    """For an MKV at args[0], probe all audio streams + print the set
    of canonical language families present, sorted + comma-joined.
    Untagged / empty language collapses to canonical `eng` (matches the
    v2 pipeline's untagged-audio-is-English assumption).
    """
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
            return 0  # caller treats empty output as "not dual yet"
        d = json.loads(r.stdout)
    except Exception:
        return 0
    out: set[str] = set()
    for s in d.get("streams", []):
        out.add(canonical_lang((s.get("tags") or {}).get("language")))
    print(",".join(sorted(out)))
    return 0


def cmd_print_keywords(args: list[str]) -> int:
    """Dump pipeline keyword sets from media_stack.config — one keyword
    per line, sorted.  Replaces the documented-but-missing CLI
    referenced in `media_stack/config.py`'s module docstring (codex
    round-cleanup #6).  Optional positional arg: japanese|korean|
    dual_audio (default: all three, separated by blank lines + a
    `# <set>` header for each).
    """
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
    """Read `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `MSG` from
    the environment and POST to the Bot API via
    `media_stack.clients.telegram.send`.  Keeps shell scripts away
    from inline Python heredocs (codex round-6 #7) and routes all
    Telegram traffic through the tested adapter.  Exits 0 on success,
    1 on send failure or missing env.
    """
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
