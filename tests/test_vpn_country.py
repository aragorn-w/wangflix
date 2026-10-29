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
    ("Switzerland", "CH"),
    ("ch", "CH"),  # ISO input passes through normalized
    # ip2location's 'United States of America' normalises to US
    ("United States of America", "US"),
    # an operator typo (`VPN_COUNTRY=Helvetia`) is returned as-is so the comparison fails visibly
    ("Helvetia", "Helvetia"),
])
def test_name_to_iso(value, expected):
    assert name_to_iso(value) == expected


# --- iso_to_name ---

@pytest.mark.parametrize("code, expected", [
    ("CH", "Switzerland"),
    ("ZZ", "ZZ"),  # an unknown code is shown as-is, not mapped to the wrong country
])
def test_iso_to_name(code, expected):
    assert iso_to_name(code) == expected


# --- consensus ---

@pytest.mark.parametrize("a, b, state, value", [
    ("CH", "CH", "ok", "CH"),
    # one source gives "single" with its canonical ISO, not a hard fail (a rate-limited provider
    # must not raise false VPN-down alerts); the caller still fails a wrong country
    ("CH", "", "single", "CH"),
    ("", "", "empty", ""),
], ids=["agree", "single-ip2location-only", "both-empty"])
def test_consensus(a, b, state, value):
    assert consensus(a, b) == (state, value)


def test_consensus_disagree():
    state, value = consensus("CH", "GB")
    assert state == "disagree"
    assert "CH" in value and "GB" in value
    assert value.startswith("DISAGREE")


# --- country_matches ---

@pytest.mark.parametrize("iso, name, expected", [
    ("CH", "Switzerland", True),
    ("CH", "Germany", False),
    ("", "Switzerland", False),  # both sides must be non-empty to count as a match
])
def test_country_matches(iso, name, expected):
    assert country_matches(iso, name) is expected
