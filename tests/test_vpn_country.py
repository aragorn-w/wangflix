"""Tests for media_stack.vpn_country, the consensus and country-match helper behind the healthcheck VPN probe."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.vpn_country import (
    name_to_iso, iso_to_name, consensus, country_matches,
)


# --- name_to_iso ---

@pytest.mark.parametrize("value, expected", [
    ("Switzerland", "CH"), ("United Kingdom", "GB"),
    ("ch", "CH"), ("CH", "CH"),  # ISO input passes through normalized
    # ip2location's 'United States of America' normalises to US like 'United States' and 'USA'
    ("United States", "US"), ("United States of America", "US"), ("USA", "US"), ("U.S.A.", "US"),
    # an operator typo (`VPN_COUNTRY=Helvetia`) is returned as-is so the comparison fails visibly
    ("Helvetia", "Helvetia"),
    ("", ""),
])
def test_name_to_iso(value, expected):
    assert name_to_iso(value) == expected


# --- iso_to_name ---

@pytest.mark.parametrize("code, expected", [
    ("CH", "Switzerland"), ("ch", "Switzerland"),
    ("ZZ", "ZZ"),  # an unknown code is shown as-is, not mapped to the wrong country
])
def test_iso_to_name(code, expected):
    assert iso_to_name(code) == expected


# --- consensus ---

@pytest.mark.parametrize("a, b, state, value", [
    ("CH", "CH", "ok", "CH"),
    ("ch", "CH", "ok", "CH"),  # geo providers can return lowercase
    ("UK", "GB", "ok", "GB"),  # aliases are canonicalized before comparing; canonical wins
    ("United States", "US", "ok", "US"),  # a long name agrees with its ISO code
    # one source gives "single" with its canonical ISO, not a hard fail (a rate-limited provider
    # must not raise false VPN-down alerts); the caller still fails a wrong country
    ("CH", "", "single", "CH"),
    ("", "ch", "single", "CH"),
    ("", "UK", "single", "GB"),  # alias canonicalised
    ("", "", "empty", ""),
], ids=["agree", "case-insensitive-agree", "alias-codes", "long-form-vs-iso", "single-ip2location-only",
        "single-ipinfo-only-lowercase", "single-alias", "both-empty"])
def test_consensus(a, b, state, value):
    assert consensus(a, b) == (state, value)


def test_consensus_disagree():
    state, value = consensus("CH", "GB")
    assert state == "disagree"
    assert "CH" in value and "GB" in value
    assert value.startswith("DISAGREE")


# --- country_matches ---

@pytest.mark.parametrize("iso, name, expected", [
    ("CH", "Switzerland", True), ("CH", "CH", True),
    # every common US name variant accepts the agreed ISO-2 `US`
    ("US", "United States", True), ("US", "United States of America", True), ("US", "USA", True),
    ("CH", "Germany", False),
    ("CA", "Canada", True),  # in-map case
    ("CH", "Helvetia", False),  # an unmapped country name (operator typo) never silently matches
    # both sides must be non-empty to count as a match
    ("", "Switzerland", False), ("CH", "", False), ("", "", False),
])
def test_country_matches(iso, name, expected):
    assert country_matches(iso, name) is expected
