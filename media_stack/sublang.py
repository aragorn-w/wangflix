"""Lexical scorer: is a subtitle's TEXT English? Tags and filenames can lie.

Scores the share of tokens in each language's closed-class word list; a language
with no list comes back `UNKNOWN`, never guessed English.
"""

from __future__ import annotations

import re

# Codes are ISO 639-2 bibliographic (`fre`/`ger`), unrelated to `media_stack.lang`'s families.
_WORDS: dict[str, list[str]] = {
    "eng": ("the and you to is it that of a i in what we this be for my on "
            "have me your are not do was he no with just so can know all "
            "but get right they here there don't it's i'm go she him like "
            "now gonna let's okay oh yeah").split(),
    "fre": ("le la les et est un une je tu vous de des que qui pas ne en du "
            "il elle nous on mais pour c'est ça a au avec ce suis j'ai "
            "qu'il oui non bien tout moi toi").split(),
    "spa": ("el la los las y es un una yo tú usted de que no en por para "
            "con se lo qué pero muy está estoy sí bien todo eso esto ella "
            "él nosotros").split(),
    "ger": ("der die das und ist ein eine ich du sie nicht zu es mit auf "
            "den dem ja nein was wir ihr aber auch noch so wie hier mich "
            "dich").split(),
    "ita": ("il lo la gli le e è un una io tu non che di per con mi ti si "
            "ma cosa sono sei bene tutto questo quello lei lui noi").split(),
    "por": ("o a os as e é um uma eu você não que de em para com se mas "
            "está estou sim bem tudo isso isto ela ele nós").split(),
    "dan": ("og der det er en de til at jeg du ikke med han hun vi som så "
            "for på var min dette her nej ja hej tak den et").split(),
    "dut": ("de het een en is dat niet ik je jij u wij zij hij was heb hebt "
            "heeft met voor op aan van naar dit daar hier ja nee goed "
            "mijn").split(),
    "swe": ("och det är en de till att jag har inte med han hon vi som för "
            "på var här ja nej hej tack den ett inte").split(),
    "cze": ("a je to že se na ne jsem jsi jste ty vy on ona my oni ale co "
            "kdo jak proč kde ano ten ta jsme byl byla bylo není mám máš už "
            "jen tak tady teď když nebo jako tohle nic mě tě dobře prosím").split(),
}
_WORD_SETS: dict[str, set[str]] = {k: set(v) for k, v in _WORDS.items()}

_TAG_RE = re.compile(r"\{[^}]*\}|<[^>]+>|\\N|\\h")
# Latin-1 Supplement + Latin Extended-A (Czech ě č ř š ž ů), below the 0x24F `nonlatin` cutoff.
_TOKEN_RE = re.compile(r"[a-zà-ÿ\u0100-\u017f']+")

_MIN_TOKENS = 25
_NONLATIN_THRESHOLD = 0.3
_BEST_RATIO_FLOOR = 0.08
# `eng` needs this ratio AND >= every other language's ratio
_ENG_RATIO_FLOOR = 0.15


# SDH conventions removed before scoring: (...)/[...] text, music spans (note char or lone `#`
# at each end), ALL-CAPS speaker labels. Brackets are bounded so an unclosed one can't
# swallow later dialogue.
_BRACKET_RE = re.compile(r"\([^()]{0,200}\)|\[[^\[\]]{0,200}\]")
_MUSIC_MARK_RE = re.compile(r"[\u266a\u266b\u266c]|(?<!\S)#(?!\S)")
_SPEAKER_RE = re.compile(r"^\s*(?:[-\u2013\u2014]\s*)?[A-Z][A-Z0-9'.\- ]{0,29}:(?!\d)")


# A cue timing LINE never appears in ASS (a literal "-->" inside ASS dialogue doesn't match).
_ASS_SECTION_RE = re.compile(
    r"^\s*\[(?:script info|events|v4\+? styles\+?|aegisub project garbage)\]\s*$", re.I)
_ASS_DIALOGUE_RE = re.compile(
    r"^\s*Dialogue:\s*[^,]*,\s*\d+:\d{2}:\d{2}[.:]\d{2}\s*,\s*\d+:\d{2}:\d{2}[.:]\d{2}\s*,")
_CUE_TIMING_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}")


def _is_ass(lines: list[str]) -> bool:
    """ASS/SSA by structure: header or `Dialogue:` line, and no SRT/VTT timing line."""
    ass = False
    for ln in lines:
        if _CUE_TIMING_RE.match(ln):
            return False
        if not ass and (_ASS_SECTION_RE.match(ln) or _ASS_DIALOGUE_RE.match(ln)):
            ass = True
    return ass


def _cue_dialogue(lines: list[str]) -> str:
    """One cue with SDH removed; a music span may cross lines, not cues."""
    joined = _BRACKET_RE.sub(" ", "\n".join(_SPEAKER_RE.sub(" ", ln) for ln in lines))
    kept: list[str] = []
    inside = False
    for ln in joined.split("\n"):
        for i, segment in enumerate(_MUSIC_MARK_RE.split(ln)):
            if i:
                inside = not inside   # crossing a marker enters or leaves the span
            if not inside:
                kept.append(segment)
    return " ".join(kept)


def _clean(text: str) -> str:
    """Reduce markup to dialogue. ASS keeps only `Dialogue:` text fields (after the 9th
    comma) so header metadata (a French `Title:`) can't skew short English dialogue."""
    out: list[str] = []
    cue: list[str] = []
    lines = text.splitlines()
    is_ass = _is_ass(lines)

    def flush() -> None:
        if cue:
            out.append(_cue_dialogue(cue))
            cue.clear()

    for raw in lines:
        ln = raw.strip()
        if is_ass:
            if ln.startswith("Dialogue:"):
                cue.append(_TAG_RE.sub(" ", ln.split(",", 9)[-1]))
                flush()
            continue
        if not ln or ln.isdigit() or "-->" in ln:
            flush()
            continue
        if ln.startswith("Dialogue:"):
            flush()
            cue.append(_TAG_RE.sub(" ", ln.split(",", 9)[-1]))
            flush()
        elif not ln.startswith(("WEBVTT", "Format:", "Style:")):
            cue.append(_TAG_RE.sub(" ", ln))
    flush()
    return " ".join(out)


def score(text: str) -> dict:
    """Score text against every word list.

      - `tokens`: count after cleaning; `eng` / `best_ratio`: token share in the eng / best list
      - `best`: best-ratio language code (may be `eng`)
      - `best_distinct`: distinct words of `best`'s list present (one word x60 is ratio 1.0, 1 distinct)
      - `nonlatin`: share of letters above U+024F
      - `verdict`: `"NON-LATIN"` (> 30% non-Latin), `"TOO-LITTLE-TEXT"` (< 25 tokens), `"eng"`
        (ratio >= 0.15 and >= every other language), the `best` code (ratio >= 0.08), else `"UNKNOWN"`
    """
    cleaned = _clean(text)
    letters = [c for c in cleaned if c.isalpha()]
    nonlatin = (sum(1 for c in letters if ord(c) > 0x24F) / len(letters)
                if letters else 0.0)
    tokens = _TOKEN_RE.findall(cleaned.lower())
    n = len(tokens)
    ratios = {
        lang: (sum(1 for t in tokens if t in words) / n if n else 0.0)
        for lang, words in _WORD_SETS.items()
    }
    best = max(ratios, key=lambda k: ratios[k])
    best_distinct = len(set(tokens) & _WORD_SETS[best])

    if nonlatin > _NONLATIN_THRESHOLD:
        verdict = "NON-LATIN"
    elif n < _MIN_TOKENS:
        verdict = "TOO-LITTLE-TEXT"
    elif ratios["eng"] >= _ENG_RATIO_FLOOR and ratios["eng"] >= max(
            v for k, v in ratios.items() if k != "eng"):
        verdict = "eng"
    elif ratios[best] >= _BEST_RATIO_FLOOR:
        verdict = best
    else:
        verdict = "UNKNOWN"

    return {
        "verdict": verdict,
        "tokens": n,
        "eng": round(ratios["eng"], 3),
        "best": best,
        "best_ratio": round(ratios[best], 3),
        "best_distinct": best_distinct,
        "nonlatin": round(nonlatin, 3),
    }


# consolidate-subs.py's content gate. A false rejection costs more than a false acceptance
# (a sidecar leaves the library, an embedded track can be dropped): reject only on real evidence.
_UNKNOWN_REJECT_MIN_TOKENS = 200
_UNKNOWN_REJECT_MAX_ENG_RATIO = 0.10
# "Clearly foreign": distinct stopwords AND a ratio multiple, so "la la la" can't decide it.
_FOREIGN_MIN_DISTINCT = 8
_FOREIGN_MIN_RATIO_MULTIPLE = 2.0
# English dialogue plus Japanese sign text overrides NON-LATIN, but only with this many
# English stopword hits (two stray "a"/"i" can't rescue a Japanese file).
_NONLATIN_EXEMPT_ENG_RATIO = 0.15
_NONLATIN_EXEMPT_MIN_ENG_HITS = 3


def classify_candidate(text: str) -> tuple[bool, dict]:
    """`(is_english, info)`; `info` is `score(text)` plus a `"reason"`.

    NOT English only when:
      - a listed non-English verdict has >= 8 DISTINCT stopwords and a ratio >= 2x English;
      - `"NON-LATIN"` without substantial English (ratio < 0.15 or < 3 stopword hits);
      - `"UNKNOWN"` with >= 200 tokens and English ratio < 0.10.

    Everything else is accepted, including `"TOO-LITTLE-TEXT"` (a real 2-cue "Shh!").
    """
    info = score(text)
    verdict = info["verdict"]
    if verdict == "eng":
        return True, {**info, "reason": "eng"}
    if verdict == "TOO-LITTLE-TEXT":
        return True, {**info, "reason": "too-little-text"}
    if verdict == "NON-LATIN":
        eng_hits = round(info["eng"] * info["tokens"])
        if (info["eng"] >= _NONLATIN_EXEMPT_ENG_RATIO
                and eng_hits >= _NONLATIN_EXEMPT_MIN_ENG_HITS):
            return True, {**info, "reason": "non-latin-but-substantial-english"}
        return False, {**info, "reason": "non-latin"}
    if verdict == "UNKNOWN":
        if (info["tokens"] >= _UNKNOWN_REJECT_MIN_TOKENS
                and info["eng"] < _UNKNOWN_REJECT_MAX_ENG_RATIO):
            return False, {**info, "reason": "unknown-language-high-token-low-eng-ratio"}
        return True, {**info, "reason": "unknown-language-low-token"}
    if (info["best_distinct"] >= _FOREIGN_MIN_DISTINCT
            and info["best_ratio"] >= _FOREIGN_MIN_RATIO_MULTIPLE * info["eng"]):
        return False, {**info, "reason": f"listed-non-english:{verdict}"}
    return True, {**info, "reason": f"weak-evidence-for:{verdict}"}
