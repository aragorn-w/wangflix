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
