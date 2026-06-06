"""Audio-track selection policy for the consolidate-subs pipeline.

Decision flow:
  1. `get_audio_lang_pref(filepath, audios)` → `(allowed_langs, primary, multi_keep)`
     - Inspects filename keywords + (optionally) probed stream metadata
       to determine WHICH language family to keep + WHETHER to keep one
       stream or one-per-language.
  2. `select_keep_audio(audios, allowed, primary, multi_keep)` → list of
     stream indices.  Scores each stream by language family +
     disposition + codec + channels; either returns the single best or
     one-per-canonical-family depending on `multi_keep`.

Codex iteration history:
  - round-1 #2/#3: alias normalization via canonical_lang
  - round-2 #4: multi_keep gating (anime/Korean keyword paths stay
    single-track; only DUAL_AUDIO_KEYWORDS + canonical-disposition
    paths opt in)
"""

from __future__ import annotations

from pathlib import Path

from media_stack.config import (
    CODEC_PREF, COMMENT_TITLE_RE,
    DUAL_AUDIO_KEYWORDS, JAPANESE_KEYWORDS, KOREAN_KEYWORDS,
)
from media_stack.lang import (
    ENG_AUDIO_LANGS, JPN_AUDIO_LANGS, KOR_AUDIO_LANGS,
    canonical_lang,
)


def is_commentary_audio(s: dict) -> bool:
    """True iff stream `s` is a commentary / audio-description /
    visual-impaired track.  Matches both the disposition flags
    (`comment`, `visual_impaired`) and common title patterns."""
    disp = s.get("disposition") or {}
    if disp.get("comment") or disp.get("visual_impaired"):
        return True
    title = (s.get("tags") or {}).get("title") or ""
    return bool(COMMENT_TITLE_RE.search(title))


def is_dub_audio(s: dict) -> bool:
    """True iff stream `s` is explicitly flagged as a dub via the
    Matroska `dub` disposition.  Release groups rarely set this, so it
    catches a narrow case — most dub detection is heuristic via
    language + track-name conventions."""
    return bool((s.get("disposition") or {}).get("dub"))


def get_audio_lang_pref(
    filepath: Path, audios: list[dict] | None = None
) -> tuple[set, str, bool]:
    """Return `(allowed_langs, primary_lang, multi_keep)`.

    `multi_keep=False`: keep ONE highest-scored allowed-language stream
    (documented "single primary track" rule for anime / Korean / default).
    `multi_keep=True`: keep ONE BEST stream PER allowed canonical language
    family (used for live-action foreign-original films like Godzilla
    Minus One where both original-language and English-dub tracks are
    wanted).

    Decision order (first match wins):
    1. /anime/ path or `JAPANESE_KEYWORDS` in filename → eng+jpn allowed,
       jpn primary, multi_keep=False (anime preference: prune dubs).
    2. `DUAL_AUDIO_KEYWORDS` in filename → eng+jpn allowed, jpn primary,
       multi_keep=True.
    3. `KOREAN_KEYWORDS` → eng+kor allowed, kor primary, multi_keep=False.
    4. Probed audio has both eng + jpn AND a jpn stream carries
       default-OR-original disposition → eng+jpn, jpn primary,
       multi_keep=True (catches non-anime Japanese-original films
       missing from the keyword list).
    5. Same for eng + kor.
    6. Fallback: eng primary, English-only allowed list, multi_keep=False.

    `audios` is optional for backward compatibility.  Omit it to apply
    only the filename rules (rules 1-3, 6).
    """
    pl = str(filepath).lower()
    if "/anime/" in pl or any(kw in pl for kw in JAPANESE_KEYWORDS):
        return ENG_AUDIO_LANGS | JPN_AUDIO_LANGS, "jpn", False
    if any(kw in pl for kw in DUAL_AUDIO_KEYWORDS):
        return ENG_AUDIO_LANGS | JPN_AUDIO_LANGS, "jpn", True
    if any(kw in pl for kw in KOREAN_KEYWORDS):
        return ENG_AUDIO_LANGS | KOR_AUDIO_LANGS, "kor", False

    if audios:
        def _clang(s: dict) -> str:
            return canonical_lang((s.get("tags") or {}).get("language"))

        def _canonical_disp(s: dict) -> bool:
            d = s.get("disposition") or {}
            return bool(d.get("default") or d.get("original"))

        def _has_canonical(target: str) -> bool:
            return any(_clang(s) == target and _canonical_disp(s) for s in audios)

        # Build presence set on canonical families so `ja` reads as jpn,
        # `en` as eng, etc.
        langs_present = {_clang(s) for s in audios}
        if "jpn" in langs_present and "eng" in langs_present and _has_canonical("jpn"):
            return ENG_AUDIO_LANGS | JPN_AUDIO_LANGS, "jpn", True
        if "kor" in langs_present and "eng" in langs_present and _has_canonical("kor"):
            return ENG_AUDIO_LANGS | KOR_AUDIO_LANGS, "kor", True

    return set(ENG_AUDIO_LANGS), "eng", False


def select_keep_audio(
    audios: list[dict],
    allowed_langs: set,
    primary: str,
    multi_keep: bool = False,
) -> list[int]:
    """Return the stream indices to keep.

    Score formula (per stream):
      +100k if canonical lang matches `primary`
      +50k  if raw lang in `allowed_langs`
      -1M   if commentary (always drops)
      -5k   if dub disposition + non-primary
      +200  if disposition.default
      +100  if disposition.original
      +channels*10
      +CODEC_PREF[codec]

    Selection:
      - `multi_keep=False`: return `[best.index]`
      - `multi_keep=True`: walk in-spec streams highest-score-first,
        keep one per CANONICAL language family (dedup `ja`/`jpn`)

    Fallback when no allowed-language streams exist: take the single
    best non-commentary track regardless of `multi_keep` — without
    allowed-language matches we can't express user intent for what
    extras to retain.
    """
    if not audios:
        return []

    def lang_of(s):
        return ((s.get("tags") or {}).get("language") or "").lower()

    def clang_of(s):
        return canonical_lang((s.get("tags") or {}).get("language"))

    def score(s):
        sc = 0
        cl = clang_of(s)
        l = lang_of(s)
        if cl == primary:
            sc += 100_000
        if l in allowed_langs:
            sc += 50_000
        if is_commentary_audio(s):
            sc -= 1_000_000
        if is_dub_audio(s) and cl != primary:
            sc -= 5_000
        disp = s.get("disposition") or {}
        if disp.get("default"):
            sc += 200
        if disp.get("original"):
            sc += 100
        sc += int(s.get("channels") or 0) * 10
        sc += CODEC_PREF.get(s.get("codec_name", ""), 5)
        return sc

    in_spec = [s for s in audios if lang_of(s) in allowed_langs and not is_commentary_audio(s)]
    if in_spec:
        in_spec.sort(key=score, reverse=True)
        if multi_keep:
            seen_langs: set[str] = set()
            kept: list[int] = []
            for s in in_spec:
                cl = clang_of(s)
                if cl in seen_langs:
                    continue
                seen_langs.add(cl)
                kept.append(s["index"])
            return kept
        return [in_spec[0]["index"]]

    cands = [s for s in audios if not is_commentary_audio(s)] or list(audios)
    cands.sort(key=score, reverse=True)
    return [cands[0]["index"]]
