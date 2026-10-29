"""Language-alias families (ENG / JPN / KOR) and canonicalization: the single source of truth for
audio/subtitle language tags. Also a CLI (`canonicalize`, `expand FAMILY`) for shell scripts.
"""

from __future__ import annotations

import sys


# Empty and `und` count as English: unmarked audio is assumed English unless other signals say so.
ENG_AUDIO_LANGS: set[str] = {"eng", "en", "english", "und", ""}
JPN_AUDIO_LANGS: set[str] = {"jpn", "ja", "japanese"}
KOR_AUDIO_LANGS: set[str] = {"kor", "ko", "korean"}

_FAMILY_MAP: dict[str, str] = {
    **{a: "eng" for a in ENG_AUDIO_LANGS},
    **{a: "jpn" for a in JPN_AUDIO_LANGS},
    **{a: "kor" for a in KOR_AUDIO_LANGS},
}


def canonical_lang(raw: str | None) -> str:
    """Unmapped input is returned lowercased. Regional variants (`en-US`, `ja_JP`) collapse via the
    subtag before the first `-`/`_`, unless that base is empty/`und` (a bare `-US` stays unmapped).
    """
    r = (raw or "").lower()
    if r in _FAMILY_MAP:
        return _FAMILY_MAP[r]
    base = r.split("-", 1)[0].split("_", 1)[0]
    if base and base != "und" and base in _FAMILY_MAP:
        return _FAMILY_MAP[base]
    return r


def is_english_tag(raw: str | None) -> bool:
    """Unlike `canonical_lang`, empty/`und` is NOT English: an untagged subtitle could be anything."""
    r = (raw or "").strip().lower()
    if r in ("", "und"):
        return False
    return canonical_lang(r) == "eng"


def _cli_canonicalize() -> int:
    """Blank lines count as `eng` (untagged audio), so an untagged English stream beside a
    Japanese one isn't missed."""
    out: set[str] = set()
    for line in sys.stdin.read().splitlines():
        out.add(canonical_lang(line.strip()))
    print(",".join(sorted(out)))
    return 0


def _cli_expand(family: str) -> int:
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
        print("usage: python3 -m media_stack.lang {canonicalize|expand FAMILY}", file=sys.stderr)
        sys.exit(2)
    mode = sys.argv[1]
    if mode == "canonicalize":
        sys.exit(_cli_canonicalize())
    if mode == "expand" and len(sys.argv) >= 3:
        sys.exit(_cli_expand(sys.argv[2]))
    print("usage: python3 -m media_stack.lang {canonicalize|expand FAMILY}", file=sys.stderr)
    sys.exit(2)
