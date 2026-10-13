"""Unit tests for media_lang.canonical_lang() — addresses codex finding
'No focused tests for language canonicalization' raised in review rounds
1, 2, and 3."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_lang import (  # noqa: E402
    ENG_AUDIO_LANGS,
    JPN_AUDIO_LANGS,
    KOR_AUDIO_LANGS,
    canonical_lang,
    is_english_tag,
)


def test_english_family_aliases():
    for alias in ("eng", "en", "english", "ENG", "English"):
        assert canonical_lang(alias) == "eng"


def test_japanese_family_aliases():
    for alias in ("jpn", "ja", "japanese", "JA", "Japanese"):
        assert canonical_lang(alias) == "jpn"


def test_korean_family_aliases():
    for alias in ("kor", "ko", "korean"):
        assert canonical_lang(alias) == "kor"


def test_undetermined_and_empty_map_to_eng():
    """`und` and empty tags are assumed to be English by the v2 pipeline —
    consolidate-subs.py and godzilla-add-sdh.sh both depend on this."""
    assert canonical_lang("und") == "eng"
    assert canonical_lang("") == "eng"
    assert canonical_lang(None) == "eng"


def test_unknown_family_passes_through_lowercase():
    assert canonical_lang("fre") == "fre"
    assert canonical_lang("FRE") == "fre"
    assert canonical_lang("spa") == "spa"


def test_regional_english_variants_collapse_to_eng():
    """en-US, en-GB, eng-US, en_US, and any other en-*/eng-* regional or
    script variant must canonicalize to eng, not pass through unchanged."""
    for tag in ("en-US", "en-GB", "eng-US", "eng-GB", "en_US", "EN-us", "english-US"):
        assert canonical_lang(tag) == "eng", tag


def test_regional_japanese_korean_variants_collapse_too():
    """The same base-subtag mechanism applies to every family, not just
    English (ja-JP, ko-KR, ...)."""
    assert canonical_lang("ja-JP") == "jpn"
    assert canonical_lang("ko-KR") == "kor"


def test_bare_regional_suffix_without_a_real_base_does_not_guess_english():
    """A tag whose base subtag is empty or "und" has no meaningful
    "regional variant" reading -- must NOT be guessed as English."""
    assert canonical_lang("-US") == "-us"  # lowercased, but not promoted to eng
    assert canonical_lang("und-US") == "und-us"


def test_is_english_tag_recognizes_base_and_regional_aliases():
    for tag in ("eng", "en", "english", "EN", "en-US", "en-GB", "eng-US"):
        assert is_english_tag(tag) is True, tag


def test_is_english_tag_rejects_untagged_and_und():
    """Unlike canonical_lang, is_english_tag does NOT assume an
    untagged/"und" stream is English -- that's the stricter subtitle-side
    rule (see media_stack.config.ENG_LANGS)."""
    assert is_english_tag("") is False
    assert is_english_tag(None) is False
    assert is_english_tag("und") is False


def test_is_english_tag_rejects_foreign():
    assert is_english_tag("ger") is False
    assert is_english_tag("fre") is False
    assert is_english_tag("jpn") is False


def test_alias_sets_are_disjoint_except_for_und_in_eng():
    """The alias sets must not overlap (excluding the empty/und special case
    that we deliberately route to eng). If a new alias is added to two sets
    by mistake, canonical_lang's behavior becomes order-dependent."""
    eng = ENG_AUDIO_LANGS - {"und", ""}
    assert eng.isdisjoint(JPN_AUDIO_LANGS)
    assert eng.isdisjoint(KOR_AUDIO_LANGS)
    assert JPN_AUDIO_LANGS.isdisjoint(KOR_AUDIO_LANGS)


def test_cli_canonicalize_treats_blank_lines_as_eng(tmp_path, capsys):
    """Regression for codex round-4 finding #4: the CLI mode used to skip
    blank lines, but `canonical_lang("") == "eng"`.  A release with an
    untagged English stream + a `jpn` stream must canonicalize to
    `eng,jpn` so godzilla-add-sdh.sh's dual-audio check fires."""
    import subprocess
    project_root = Path(__file__).resolve().parent.parent
    script = project_root / "media_lang.py"

    # Empty + jpn → eng,jpn (the bug case)
    r = subprocess.run(
        ["python3", str(script), "canonicalize"],
        input="\njpn\n", capture_output=True, text=True, timeout=5,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == "eng,jpn"

    # All empty → eng
    r = subprocess.run(
        ["python3", str(script), "canonicalize"],
        input="\n\n\n", capture_output=True, text=True, timeout=5,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == "eng"


def test_cli_canonicalize_dedups_aliases():
    """ja + japanese + jpn must all collapse to single `jpn`."""
    import subprocess
    project_root = Path(__file__).resolve().parent.parent
    script = project_root / "media_lang.py"
    r = subprocess.run(
        ["python3", str(script), "canonicalize"],
        input="ja\njapanese\njpn\n", capture_output=True, text=True, timeout=5,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == "jpn"
