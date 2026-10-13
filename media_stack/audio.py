"""Audio-track selection policy for the consolidate-subs pipeline.

Decision flow:
  1. `get_audio_lang_pref(filepath, audios, original_language=...)` →
     `(allowed_langs, primary, multi_keep)`
     - Inspects filename keywords + (optionally) probed stream metadata
       + (optionally) the title's Radarr/Sonarr original-language signal
       to determine WHICH language family to keep + WHETHER to keep one
       stream or one-per-language.  Used by the FULL consolidate-subs
       path (a main English subtitle exists/was just built) together
       with `select_keep_audio`, which CAN drop tracks (the file is
       being fully normalized to policy anyway).
  2. `select_keep_audio(audios, allowed, primary, multi_keep)` → list of
     stream indices.  Scores each stream by language family +
     disposition + codec + channels; either returns the single best or
     one-per-canonical-family depending on `multi_keep`.
  3. `confident_default_audio(filepath, audios, original_language=...)`
     → `int | None`.  Used by the "no main English subtitle yet" cleanup
     (`consolidate-subs.py`'s `_cleanup_no_main_english`), which must
     NEVER drop an audio track — only reorder/re-flag `default` on one,
     and only when the language is known with confidence (never from a
     bare `default` disposition, never from a guess).

Codex iteration history:
  - round-1 #2/#3: alias normalization via canonical_lang
  - round-2 #4: multi_keep gating (anime/Korean keyword paths stay
    single-track; only DUAL_AUDIO_KEYWORDS + canonical-disposition
    paths opt in)
  - DEFECT 2 fix: `default` disposition is not evidence of original
    language (release groups routinely flag a DUB `default`) — a
    non-keyword jpn/kor promotion now requires either the Radarr/Sonarr
    `original_language` signal (`media_stack.arrlang`) or, when that's
    unavailable, the stricter `original` disposition (never `default`).
  - Codex round 2 on the DEFECT 1 cleanup: `get_audio_lang_pref` +
    `select_keep_audio` can still drop a wanted track (e.g. no audio
    stream matches the allowed family at all), which is fine for the
    FULL path but not for a cleanup that runs while still waiting on
    Bazarr. `confident_default_audio` is the delete-free alternative for
    that one call site; it also fixes a jpn-`default`-outranks-kor-
    `original` ordering bug in `get_audio_lang_pref` rule 6 along the way.
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
    filepath: Path,
    audios: list[dict] | None = None,
    *,
    original_language: str | None = None,
) -> tuple[set, str, bool]:
    """Return `(allowed_langs, primary_lang, multi_keep)`.

    `multi_keep=False`: keep ONE highest-scored allowed-language stream
    (documented "single primary track" rule for anime / Korean / default).
    `multi_keep=True`: keep ONE BEST stream PER allowed canonical language
    family (used for live-action foreign-original films like Godzilla
    Minus One where both original-language and English-dub tracks are
    wanted).

    `original_language` is the Radarr/Sonarr "original language" signal for
    this title (see `media_stack.arrlang.original_language_for`) — an ISO
    639-2 code ("eng"/"jpn"/"kor"/other), or `None` when it couldn't be
    determined.  It exists because the Matroska `default` disposition is
    NOT reliable evidence of original language: release groups routinely
    flag a DUB as `default` (e.g. an English film with a Japanese dub
    flagged default), and the old rules 4-5 treated `default` the same as
    `original` and promoted the dub to primary.  `original` alone (set by
    the encoder to mean "this track is in the production's original
    language") is trustworthy; `default` (which only means "play this one
    unless the user picks another") is not.

    Decision order (first match wins):
    1. `DUAL_AUDIO_KEYWORDS` in the path → eng+jpn allowed, jpn primary,
       multi_keep=True.  Checked FIRST so an explicit per-title opt-in beats
       the broad anime default below; a title may legitimately be in both sets.
    2. /anime/ path or `JAPANESE_KEYWORDS` in the path → eng+jpn allowed,
       jpn primary, multi_keep=False (anime preference: prune dubs).
    3. `KOREAN_KEYWORDS` → eng+kor allowed, kor primary, multi_keep=False.
    4. `original_language == "eng"` → eng primary, English-only allowed,
       multi_keep=False.  An English-original title never gets a foreign
       primary, no matter what any stream's disposition says.
    5. `original_language` is "jpn" or "kor" and `audios` given → that
       language is primary; multi_keep=True when an eng stream AND a
       stream of that language are both present, else multi_keep=False.
    6. `original_language is None` (unknown — e.g. Radarr/Sonarr
       unreachable, no API key, title not found) and `audios` given:
       the old disposition-based promotion, SHARPENED so `default` can no
       longer PROMOTE a dub to primary, but — just as importantly — can no
       longer cause the foreign track to be DROPPED either:
         - eng + jpn present AND a jpn stream carries `original` →
           eng+jpn, jpn primary, multi_keep=True (unchanged: trustworthy
           evidence).
         - eng + jpn present AND a jpn stream carries `default` but NOT
           `original` → eng+jpn, **eng primary**, multi_keep=True.  Both
           tracks are kept either way; only which one is marked `default`
           in the output changes. A wrong default flag is recoverable
           (re-run once Radarr/Sonarr resolve the title); a deleted track
           is not.
         - same for kor.
    7. Fallback: eng primary, English-only allowed list, multi_keep=False.
       This is also what any OTHER `original_language` value (e.g.
       "dan", "fre") resolves to — only eng/jpn/kor change behavior here.

    Rules 1-3 are case-insensitive SUBSTRING matches against the whole
    lowercased path, not just the basename, so a keyword in a parent
    directory (series folder, collection folder, library root) matches too.

    `audios` is optional for backward compatibility.  Omit it to apply
    only the path rules (rules 1-3, 7) plus rule 4.  `original_language`
    is also optional and keyword-only; omitting it preserves the old
    (pre-arrlang) rule-6 behavior for existing callers.
    """
    pl = str(filepath).lower()
    # DUAL_AUDIO_KEYWORDS is checked FIRST: it is an explicit per-title opt-in to keeping
    # both tracks, and it must beat the broad anime default below, which prunes dubs.  A
    # title can legitimately be in both sets - "May I Ask for One Final Thing" is an anime
    # (so it matches JAPANESE_KEYWORDS by name) that the operator wants dual-audio.  With
    # the old ordering the anime rule won and silently discarded the English dub on import,
    # so grabbing a dual-audio release could never produce a dual-audio library file.
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

        # Build presence set on canonical families so `ja` reads as jpn,
        # `en` as eng, etc.
        langs_present = {_clang(s) for s in audios}
        if "eng" in langs_present:
            # `original` is checked for BOTH jpn and kor BEFORE either's
            # `default` is considered at all: `original` is trustworthy
            # evidence regardless of which language carries it, and
            # `default` is never trustworthy (release groups routinely
            # flag a DUB `default`) — so a jpn `default` must never
            # outrank a kor `original`, or vice versa, by being checked
            # (and returned on) first.
            if "jpn" in langs_present and _has_flag("jpn", _original_disp):
                return ENG_AUDIO_LANGS | JPN_AUDIO_LANGS, "jpn", True
            if "kor" in langs_present and _has_flag("kor", _original_disp):
                return ENG_AUDIO_LANGS | KOR_AUDIO_LANGS, "kor", True
            if "jpn" in langs_present and _has_flag("jpn", _default_disp):
                # Not promoted to primary — but not dropped either: a
                # wrong default flag is recoverable later, a deleted
                # track is not. Keep both, English as the safe default.
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
    """Return the audio stream `index` that should carry the `default`
    flag, or `None` to leave every stream's default flag exactly as it
    is in the source.

    This is deliberately narrower than `get_audio_lang_pref`: it NEVER
    drops a track (callers must keep every audio stream — see
    `consolidate-subs.py`'s `_cleanup_no_main_english`, which uses this
    to decide whether a "no main English subtitle yet" cleanup remux
    needs to move the default flag at all), and it only acts when the
    primary language is CONFIDENTLY known — never from a `default`
    disposition, never from the eng fallback. Confidence sources, in
    priority order (mirrors `get_audio_lang_pref`'s own precedence):
      1. A keyword rule on `filepath` (`DUAL_AUDIO_KEYWORDS` -> jpn;
         `/anime/` or `JAPANESE_KEYWORDS` -> jpn; `KOREAN_KEYWORDS` ->
         kor).
      2. A KNOWN Radarr/Sonarr `original_language` in `("eng", "jpn",
         "kor")`.
      3. `original_language is None` (unknown) AND some audio stream
         carries the `original` disposition for jpn or kor (jpn checked
         first, same tie-break as `get_audio_lang_pref`'s rule 6 — see
         there for why `default` is excluded here entirely).

    None of these firing => return None: an Arr outage, or any other
    reason the language can't be pinned down, must leave the file's
    audio untouched rather than flip-flopping its default flag on every
    retry (that would mean re-remuxing the same file every time Arr is
    briefly unreachable and then back).

    Once a language is confidently decided, the candidate pool is EVERY
    non-commentary stream whose raw language tag EXPLICITLY names that
    canonical family (regional variants like `en-US` count; an
    untagged/`und` stream does not — it might be anything, so it's never
    picked as THE default just because its canonical reading happens to
    default to English). The best-scored candidate (disposition +
    channels + codec preference, same inputs as `select_keep_audio`'s
    scoring) is returned; if no candidate stream exists at all for the
    decided language, returns None.
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
    # else: a KNOWN but irrelevant original_language (e.g. "dan") -- no
    # confidence source applies; primary stays None.

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
    """Return the stream indices to keep.

    Score formula (per stream):
      +100k if canonical lang matches `primary`
      +50k  if canonical lang's FAMILY is one of `allowed_langs`' families
      -1M   if commentary (always drops)
      -5k   if dub disposition + non-primary
      +200  if disposition.default
      +100  if disposition.original
      +channels*10
      +CODEC_PREF[codec]

    `allowed_langs` is a set of RAW alias strings (e.g. `ENG_AUDIO_LANGS`).
    Membership is FAMILY-based, not a literal raw-string match: a stream's
    raw tag is canonicalized (`canonical_lang`, which also collapses
    regional/script variants like `ja-JP`/`en-US`) and compared against
    the set of canonical families `allowed_langs` itself maps to. A raw
    literal match (`lang_of(s) in allowed_langs`) missed `ja-JP` even when
    `JPN_AUDIO_LANGS` was in `allowed_langs`, because `"ja-jp"` the string
    is never literally `"ja"`/`"jpn"`/`"japanese"` — silently excluding a
    stream whose family was in fact allowed.

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
