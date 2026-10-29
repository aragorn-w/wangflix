"""Unit tests for media_stack.lang (canonical_lang, is_english_tag and its CLI)."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.lang import (  # noqa: E402
    canonical_lang,
    is_english_tag,
)


@pytest.mark.parametrize("expected, aliases", [
    ("eng", ("eng", "en", "english", "ENG", "English")),
    ("jpn", ("jpn", "ja", "japanese", "JA", "Japanese")),
    ("kor", ("kor", "ko", "korean")),
], ids=["english", "japanese", "korean"])
def test_family_aliases(expected, aliases):
    for alias in aliases:
        assert canonical_lang(alias) == expected, alias


def test_undetermined_and_empty_map_to_eng():
    """`und` and empty tags are assumed English; consolidate-subs.py and godzilla-add-sdh.sh depend on it."""
    assert canonical_lang("und") == "eng"
    assert canonical_lang("") == "eng"
    assert canonical_lang(None) == "eng"


def test_unknown_family_passes_through_lowercase():
    assert canonical_lang("fre") == "fre"
    assert canonical_lang("FRE") == "fre"
    assert canonical_lang("spa") == "spa"


@pytest.mark.parametrize("expected, tags", [
    ("eng", ("en-US", "en-GB")),
    ("jpn", ("ja-JP",)),
], ids=["english", "japanese"])
def test_regional_variants_collapse_to_the_base_family(expected, tags):
    """The base-subtag mechanism applies to every family (en-US, ja-JP, ...)."""
    for tag in tags:
        assert canonical_lang(tag) == expected, tag


def test_bare_regional_suffix_without_a_real_base_does_not_guess_english():
    """A tag whose base subtag is empty or "und" is not guessed as English."""
    assert canonical_lang("-US") == "-us"  # lowercased, but not promoted to eng
    assert canonical_lang("und-US") == "und-us"


def test_is_english_tag_recognizes_base_and_regional_aliases():
    for tag in ("eng", "en", "english", "EN", "en-US", "en-GB", "eng-US"):
        assert is_english_tag(tag) is True, tag


def test_is_english_tag_rejects_untagged_und_and_foreign():
    """Unlike canonical_lang, is_english_tag does not assume untagged/"und" is English (the stricter subtitle rule)."""
    for tag in ("", None, "und", "ger", "fre", "jpn"):
        assert is_english_tag(tag) is False, tag


def _run_cli(*args: str, stdin: str = "") -> subprocess.CompletedProcess:
    """Run `python3 -m media_stack.lang ...` from the repo root, as the shell callers do."""
    project_root = Path(__file__).resolve().parent.parent
    return subprocess.run(
        [sys.executable, "-m", "media_stack.lang", *args],
        input=stdin, capture_output=True, text=True, timeout=10, cwd=project_root,
    )


@pytest.mark.parametrize("stdin, expected", [
    # blank lines count: an untagged English stream plus `jpn` gives `eng,jpn`, which a dual-audio check needs
    pytest.param("\njpn\n", "eng,jpn", id="blank-plus-jpn"),
    pytest.param("ja\njapanese\njpn\n", "jpn", id="aliases-dedup-to-one"),
])
def test_cli_canonicalize(stdin, expected):
    r = _run_cli("canonicalize", stdin=stdin)
    assert r.returncode == 0
    assert r.stdout.strip() == expected
