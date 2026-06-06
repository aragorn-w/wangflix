"""Tests for media_stack.vpn_country — the consensus + country-match
helper that the healthcheck VPN probe runs through.

Codex round-cleanup-3 #4 flagged the absence of regression tests for
this logic.  Each scenario the shell code branches on is covered
here, so future edits can't quietly regress (round-cleanup-2 #3 was
exactly this kind of silent regression).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.vpn_country import (
    name_to_iso, iso_to_name, consensus, country_matches,
)


# --- name_to_iso ---

def test_name_to_iso_long_form():
    assert name_to_iso("Switzerland") == "CH"
    assert name_to_iso("United Kingdom") == "GB"


def test_name_to_iso_iso_input_passes_through_normalized():
    assert name_to_iso("ch") == "CH"
    assert name_to_iso("CH") == "CH"


def test_name_to_iso_handles_us_variants():
    """Codex round-cleanup-2 #3 root cause: ip2location returns
    'United States of America'; this MUST normalise to US, same as
    the short 'United States' or 'USA' variants."""
    assert name_to_iso("United States") == "US"
    assert name_to_iso("United States of America") == "US"
    assert name_to_iso("USA") == "US"
    assert name_to_iso("U.S.A.") == "US"


def test_name_to_iso_unknown_returns_input_unchanged():
    """Codex round-cleanup-3 #1: an operator typo (`VPN_COUNTRY=Helvetia`)
    must NOT silently map to anything — return as-is so the country
    comparison will FAIL and the operator sees the typo."""
    assert name_to_iso("Helvetia") == "Helvetia"


def test_name_to_iso_empty():
    assert name_to_iso("") == ""


# --- iso_to_name ---

def test_iso_to_name_known():
    assert iso_to_name("CH") == "Switzerland"
    assert iso_to_name("ch") == "Switzerland"


def test_iso_to_name_unknown_returns_code():
    """Display fallback: unknown code surfaces in operator-facing
    output instead of being silently mapped to the wrong country."""
    assert iso_to_name("ZZ") == "ZZ"


# --- consensus ---

def test_consensus_agree():
    state, value = consensus("CH", "CH")
    assert state == "ok"
    assert value == "CH"


def test_consensus_disagree():
    state, value = consensus("CH", "GB")
    assert state == "disagree"
    assert "CH" in value and "GB" in value
    assert value.startswith("DISAGREE")


def test_consensus_single_source_returns_its_code():
    """One source available → "single" with that source's canonical ISO
    (not a hard fail).  The earlier strict "unverified" design false-failed
    whenever one provider was merely rate-limited (ipinfo.io on the VPN exit
    IP), spamming false VPN-down alerts.  The caller validates the country,
    so a single source naming the WRONG country still fails downstream."""
    state, value = consensus("CH", "")          # ip2location missing
    assert state == "single"
    assert value == "CH"
    state, value = consensus("", "ch")          # ipinfo missing, lowercase
    assert state == "single"
    assert value == "CH"                         # canonicalised


def test_consensus_single_source_canonicalises_alias():
    state, value = consensus("", "UK")          # only one source, alias code
    assert state == "single"
    assert value == "GB"


def test_consensus_both_empty():
    state, value = consensus("", "")
    assert state == "empty"
    assert value == ""


def test_consensus_case_insensitive_agree():
    """Geo providers can return lowercase; consensus must normalize."""
    state, value = consensus("ch", "CH")
    assert state == "ok"
    assert value == "CH"


def test_consensus_normalizes_alias_codes():
    """REGRESSION codex round 4 #6: consensus only uppercased raw
    values without running them through name_to_iso(), so a provider
    returning the non-canonical alias `UK` and another returning the
    canonical `GB` would be flagged as DISAGREE even though both
    mean the same country.  Must canonicalize before comparing."""
    state, value = consensus("UK", "GB")
    assert state == "ok"
    assert value == "GB"  # canonical wins


def test_consensus_normalizes_alias_to_long_form_input():
    """Defensive — if a provider ever returned a long name like
    'United States' alongside another's 'US', they should still
    agree (name_to_iso handles either form)."""
    state, value = consensus("United States", "US")
    assert state == "ok"
    assert value == "US"


# --- country_matches ---

def test_country_matches_iso_to_long_name():
    assert country_matches("CH", "Switzerland") is True


def test_country_matches_iso_to_iso():
    assert country_matches("CH", "CH") is True


def test_country_matches_us_long_variants():
    """Codex round-cleanup-2 #3 regression: every common US name
    variant must accept the agreed ISO-2 `US`."""
    assert country_matches("US", "United States") is True
    assert country_matches("US", "United States of America") is True
    assert country_matches("US", "USA") is True


def test_country_matches_mismatch():
    assert country_matches("CH", "Germany") is False


def test_country_matches_unmapped_country_fails_cleanly():
    """Codex round-cleanup-3 #1: VPN_COUNTRY=Canada with provider
    consensus CA must MATCH (Canada is in the map).  An UNMAPPED
    country name must NOT silently match."""
    # In-map case
    assert country_matches("CA", "Canada") is True
    # Out-of-map operator-typo case
    assert country_matches("CH", "Helvetia") is False


def test_country_matches_empty_inputs():
    """Both sides must be non-empty to count as a match."""
    assert country_matches("", "Switzerland") is False
    assert country_matches("CH", "") is False
    assert country_matches("", "") is False
