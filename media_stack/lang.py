"""Shared language-canonicalization policy for the media stack.

Single source of truth for the ENG / JPN / KOR alias families and the
canonical-family mapping used across consolidate-subs.py + any future
scripts that touch audio/subtitle language tags.

Without a shared module, the alias sets were duplicated inline in multiple
files. When one was updated (e.g. adding `und` to the English family) the
others silently drifted — a category of bug codex called out across three
review rounds.

This module is import-safe for downstream Python scripts and is also
designed to be executed as a CLI so shell scripts can call into it
deterministically:

    $ echo -e 'ja\\nen' | python3 media_lang.py canonicalize
    eng,jpn

    $ python3 media_lang.py expand jpn
    ja,japanese,jpn
"""

from __future__ import annotations

import sys


# Language-family alias sets. Keep these as Python sets so membership tests
# (`raw_tag in ENG`) stay O(1). The empty string and `und` are mapped into
# the English family because the v2 pipeline assumes unmarked tracks are
# English unless filename/keyword/disposition signals otherwise.
ENG_AUDIO_LANGS: set[str] = {"eng", "en", "english", "und", ""}
JPN_AUDIO_LANGS: set[str] = {"jpn", "ja", "japanese"}
KOR_AUDIO_LANGS: set[str] = {"kor", "ko", "korean"}

# Reverse map alias → canonical family code. Built once at import.
_FAMILY_MAP: dict[str, str] = {
    **{a: "eng" for a in ENG_AUDIO_LANGS},
    **{a: "jpn" for a in JPN_AUDIO_LANGS},
    **{a: "kor" for a in KOR_AUDIO_LANGS},
}


def canonical_lang(raw: str | None) -> str:
    """Map any ffprobe language alias to its canonical family code.

    `eng`/`en`/`english`/`und`/empty → `eng`
    `jpn`/`ja`/`japanese`           → `jpn`
    `kor`/`ko`/`korean`             → `kor`
    Anything else                   → returned lowercase unchanged.
    """
    r = (raw or "").lower()
    return _FAMILY_MAP.get(r, r)


def _cli_canonicalize() -> int:
    """CLI mode: read raw lang tags one per line from stdin; print the unique
    set of canonical families, sorted, comma-joined.

    Empty / whitespace-only lines collapse to canonical `eng` because the v2
    pipeline treats untagged audio streams as English (consistent with
    `canonical_lang("") == "eng"`).  Without this, a release that ships an
    untagged English audio stream alongside a Japanese stream would have
    callers see canonical `jpn` only and miss the eng track (codex round-4
    finding #4 — originally caught by the now-retired godzilla-add-sdh.sh)."""
    out: set[str] = set()
    for line in sys.stdin.read().splitlines():
        out.add(canonical_lang(line.strip()))
    print(",".join(sorted(out)))
    return 0


def _cli_expand(family: str) -> int:
    """CLI mode: print the comma-joined sorted alias set for a given family."""
    f = family.lower()
    if f == "eng":
        aliases = ENG_AUDIO_LANGS
    elif f == "jpn":
        aliases = JPN_AUDIO_LANGS
    elif f == "kor":
        aliases = KOR_AUDIO_LANGS
    else:
        print(f"unknown family: {family!r}", file=sys.stderr)
        return 2
    print(",".join(sorted(aliases)))
    return 0


if __name__ == "__main__":
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
