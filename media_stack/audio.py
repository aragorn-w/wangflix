"""Audio-track selection policy for the consolidate-subs pipeline.

`get_audio_lang_pref` + `select_keep_audio` are the FULL path, which may drop tracks.
`confident_default_audio` serves the "no main English subtitle yet" cleanup, which must never
drop a track: it only picks the `default` stream, and only when the language is known with
confidence (never from a bare `default` disposition or a guess).
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
    """Commentary or audio description, by disposition flag or title pattern."""
    disp = s.get("disposition") or {}
    if disp.get("comment") or disp.get("visual_impaired"):
        return True
    title = (s.get("tags") or {}).get("title") or ""
    return bool(COMMENT_TITLE_RE.search(title))


def is_dub_audio(s: dict) -> bool:
    return bool((s.get("disposition") or {}).get("dub"))


def get_audio_lang_pref(
    filepath: Path,
    audios: list[dict] | None = None,
    *,
    original_language: str | None = None,
) -> tuple[set, str, bool]:
    """`(allowed_langs, primary_lang, multi_keep)`. `multi_keep` keeps the best stream per
    allowed family (foreign-original films wanting original + English dub), else one stream.

    `original_language` is the Arr ISO 639-2 signal (`arrlang`), or None. The `default`
    disposition is NOT evidence of original language (release groups flag DUBs `default`);
    `original` is. Rules 1-3 are substring matches on the whole lowercased path. First match wins:
    1. `DUAL_AUDIO_KEYWORDS`: eng+jpn, jpn primary, multi_keep. Before rule 2 so a per-title
       opt-in beats the anime default.
    2. `/anime/` or `JAPANESE_KEYWORDS`: eng+jpn, jpn primary, single (prune dubs).
    3. `KOREAN_KEYWORDS`: eng+kor, kor primary, single.
    4. `original_language == "eng"`: English only.
    5. `original_language` jpn/kor with `audios`: that language primary; multi_keep if both an
       eng stream and one of that language exist.
    6. `original_language is None` with `audios` and an eng-family stream: a jpn/kor stream carrying `original` -> eng +
       that language, it primary, multi_keep; carrying only `default` -> same but **eng primary**
       (a wrong default flag is recoverable, a deleted track is not).
    7. Otherwise (including any other `original_language`, e.g. "dan"): English only, single.
    Without `audios`, only rules 1-4 and 7 apply.
    """
    pl = str(filepath).lower()
    if any(kw in pl for kw in DUAL_AUDIO_KEYWORDS):
        return ENG_AUDIO_LANGS | JPN_AUDIO_LANGS, "jpn", True
    if "/anime/" in pl or any(kw in pl for kw in JAPANESE_KEYWORDS):
        return ENG_AUDIO_LANGS | JPN_AUDIO_LANGS, "jpn", False
    if any(kw in pl for kw in KOREAN_KEYWORDS):
        return ENG_AUDIO_LANGS | KOR_AUDIO_LANGS, "kor", False

    if original_language == "eng":
        return set(ENG_AUDIO_LANGS), "eng", False

    def _clang(s: dict) -> str:
        return canonical_lang((s.get("tags") or {}).get("language"))

    if original_language in ("jpn", "kor") and audios:
        fam_langs = JPN_AUDIO_LANGS if original_language == "jpn" else KOR_AUDIO_LANGS
        allowed = ENG_AUDIO_LANGS | fam_langs
        langs_present = {_clang(s) for s in audios}
        multi_keep = "eng" in langs_present and original_language in langs_present
        return allowed, original_language, multi_keep

    if original_language is None and audios:
        def _original_disp(s: dict) -> bool:
            return bool((s.get("disposition") or {}).get("original"))

        def _default_disp(s: dict) -> bool:
            return bool((s.get("disposition") or {}).get("default"))

        def _has_flag(target: str, check) -> bool:
            return any(_clang(s) == target and check(s) for s in audios)

        langs_present = {_clang(s) for s in audios}
        if "eng" in langs_present:
            # `original` for BOTH jpn and kor before either's `default`
            if "jpn" in langs_present and _has_flag("jpn", _original_disp):
                return ENG_AUDIO_LANGS | JPN_AUDIO_LANGS, "jpn", True
            if "kor" in langs_present and _has_flag("kor", _original_disp):
                return ENG_AUDIO_LANGS | KOR_AUDIO_LANGS, "kor", True
            if "jpn" in langs_present and _has_flag("jpn", _default_disp):
                return ENG_AUDIO_LANGS | JPN_AUDIO_LANGS, "eng", True
            if "kor" in langs_present and _has_flag("kor", _default_disp):
                return ENG_AUDIO_LANGS | KOR_AUDIO_LANGS, "eng", True

    return set(ENG_AUDIO_LANGS), "eng", False


def confident_default_audio(
    filepath: Path,
    audios: list[dict] | None,
    *,
    original_language: str | None = None,
) -> int | None:
    """Audio stream `index` that should carry `default`, or None to leave every flag as in the source.

    Callers keep every stream; this acts only when the primary language is CONFIDENTLY known, in
    order: a path keyword rule (dual/anime/Japanese -> jpn, Korean -> kor); a known Arr
    `original_language` in eng/jpn/kor; with `original_language is None`, a jpn (first) or kor
    stream carrying `original`. Otherwise None, so an Arr outage can't flip the default back and
    forth and re-remux on every retry.

    Candidates are non-commentary streams whose RAW tag names the decided family (`en-US` counts;
    untagged/`und` doesn't). Best by disposition, channels and codec (as `select_keep_audio`) wins.
    """
    if not audios:
        return None

    def _clang(s: dict) -> str:
        return canonical_lang((s.get("tags") or {}).get("language"))

    pl = str(filepath).lower()
    primary: str | None = None
    if any(kw in pl for kw in DUAL_AUDIO_KEYWORDS):
        primary = "jpn"
    elif "/anime/" in pl or any(kw in pl for kw in JAPANESE_KEYWORDS):
        primary = "jpn"
    elif any(kw in pl for kw in KOREAN_KEYWORDS):
        primary = "kor"
    elif original_language in ("eng", "jpn", "kor"):
        primary = original_language
    elif original_language is None:
        def _has_original(target: str) -> bool:
            return any(
                _clang(s) == target and bool((s.get("disposition") or {}).get("original"))
                for s in audios
            )
        if _has_original("jpn"):
            primary = "jpn"
        elif _has_original("kor"):
            primary = "kor"
    # a known but irrelevant original_language (e.g. "dan") leaves primary None

    if primary is None:
        return None

    def _raw(s: dict) -> str:
        return ((s.get("tags") or {}).get("language") or "").strip().lower()

    candidates = [
        s for s in audios
        if _clang(s) == primary and _raw(s) not in ("", "und") and not is_commentary_audio(s)
    ]
    if not candidates:
        return None

    def _quality(s: dict) -> int:
        disp = s.get("disposition") or {}
        sc = 0
        if disp.get("default"):
            sc += 200
        if disp.get("original"):
            sc += 100
        sc += int(s.get("channels") or 0) * 10
        sc += CODEC_PREF.get(s.get("codec_name", ""), 5)
        return sc

    candidates.sort(key=_quality, reverse=True)
    return candidates[0]["index"]


def select_keep_audio(
    audios: list[dict],
    allowed_langs: set,
    primary: str,
    multi_keep: bool = False,
) -> list[int]:
    """Indices to keep. Score: +100k primary, +50k allowed family, -1M commentary, -5k non-primary
    dub, +200 default, +100 original, +10 per channel, +CODEC_PREF.

    `allowed_langs` holds RAW alias strings but membership is FAMILY-based (`canonical_lang`, so
    `ja-JP` matches); a literal match would silently exclude it. `multi_keep` keeps the best per
    family, else the best stream. With no allowed-language stream, falls back to the single best
    non-commentary track.
    """
    if not audios:
        return []

    allowed_families = {canonical_lang(a) for a in allowed_langs}

    def clang_of(s):
        return canonical_lang((s.get("tags") or {}).get("language"))

    def score(s):
        sc = 0
        cl = clang_of(s)
        if cl == primary:
            sc += 100_000
        if cl in allowed_families:
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

    in_spec = [s for s in audios if clang_of(s) in allowed_families and not is_commentary_audio(s)]
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
