"""VPN country resolution helper.

Pure-Python consensus logic for the healthcheck VPN-country probe.
Lives here (not in healthcheck.sh) so the comparison + naming
normalisation can be unit-tested and reused.

Codex round-cleanup-2 #3 + round-cleanup-3 #1 caught successive bugs
in shell-only consensus code:
  - long-name comparison hit `US` (mapped) vs `United States of America`
    (ip2location's exact value) → false-fail
  - ISO-2 fix only covered ~11 countries → `VPN_COUNTRY=Canada` with
    provider consensus `CA` mapped back to `CA` (not `"Canada"`),
    false-fail at the policy comparison

Resolution: compare ISO-2 throughout.  Accept the expected country
in either long form (".env: VPN_COUNTRY=Switzerland") OR as ISO-2
("VPN_COUNTRY=CH" / "VPN_COUNTRY_CODE=CH").  The long-form path
goes through `name_to_iso()` and falls back to literal-match against
the agreed code (so an unmapped country name produces a clear
mismatch the operator can fix instead of a confusing pass).
"""

from __future__ import annotations


# ISO-2 ↔ canonical long name.  Source of truth for the VPN country
# normalization layer.  Only ProtonVPN-supported countries that we
# might realistically configure (`VPN_COUNTRY=`) need entries — others
# fall through to literal ISO-2 comparison.
# Canonical ISO-2 → long name.  Only canonical ISO codes here — DON'T
# include aliases like UK→"United Kingdom", because the reverse map
# below would then non-deterministically pick the alias as the
# canonical (dict last-wins on duplicate values — bug caught by
# `test_name_to_iso_long_form` during round-cleanup-3 #1 fix).
ISO_TO_NAME: dict[str, str] = {
    "AT": "Austria",
    "AU": "Australia",
    "BE": "Belgium",
    "BR": "Brazil",
    "CA": "Canada",
    "CH": "Switzerland",
    "DE": "Germany",
    "DK": "Denmark",
    "ES": "Spain",
    "FI": "Finland",
    "FR": "France",
    "GB": "United Kingdom",
    "HK": "Hong Kong",
    "IE": "Ireland",
    "IS": "Iceland",
    "IT": "Italy",
    "JP": "Japan",
    "KR": "South Korea",
    "NL": "Netherlands",
    "NO": "Norway",
    "NZ": "New Zealand",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "SE": "Sweden",
    "SG": "Singapore",
    "US": "United States",
}

# Long name → ISO-2.  Built from ISO_TO_NAME plus a handful of common
# alternate spellings + non-canonical ISO codes the geolocation
# providers / human config can return.  Aliases live HERE so the
# canonical ISO-2 reverse-mapping above stays unambiguous.
NAME_TO_ISO: dict[str, str] = {
    **{v.lower(): k for k, v in ISO_TO_NAME.items()},
    # US variants
    "united states of america": "US",   # ip2location's exact value
    "usa": "US",
    "u.s.a.": "US",
    "u.s.": "US",
    # UK variants — "UK" is a non-canonical alias of ISO-2 "GB"
    "uk": "GB",
    "britain": "GB",
    "great britain": "GB",
    "u.k.": "GB",
    # Korea (some providers omit "South")
    "korea": "KR",
    "republic of korea": "KR",
}


def name_to_iso(value: str) -> str:
    """Normalise a string (long country name OR ISO-2 code) to ISO-2.
    Empty input returns empty.  Unknown input is returned unchanged so
    the caller can still string-compare against an unmapped ISO-2
    coming from the geolocation providers (operator typo path)."""
    if not value:
        return ""
    v = value.strip()
    if len(v) == 2 and v.upper() in ISO_TO_NAME:
        return v.upper()
    return NAME_TO_ISO.get(v.lower(), v)


def iso_to_name(code: str) -> str:
    """ISO-2 → long form for human display.  Unknown codes are
    returned unchanged so the operator sees the actual code instead
    of a silently-mapped wrong value."""
    return ISO_TO_NAME.get((code or "").upper(), code)


def consensus(ipinfo_iso: str, ip2_iso: str) -> tuple[str, str]:
    """Two-source consensus.  Returns `(state, value)` where `state`
    is one of:
      - "ok"          → both responded + agree; `value` is the
                        canonical ISO-2 code
      - "disagree"    → both responded but differ; `value` is a
                        debug string listing the raw inputs
      - "single"      → exactly ONE responded; `value` is that source's
                        canonical ISO-2.  The caller still validates it
                        against the expected country, so a single source
                        naming the WRONG country still FAILs — but a source
                        merely being unavailable no longer false-FAILs when
                        the other one confirms the policy country.
      - "empty"       → both empty; `value` is ""

    Why "single" isn't a hard fail (revises the earlier round-cleanup #1
    strict design): requiring BOTH providers to *respond* made the probe
    fail whenever one was transiently unavailable — ipinfo.io routinely
    rate-limits the ProtonVPN exit IP — which spammed false "VPN down"
    alerts even though egress was correctly in-country.  We still
    cross-check both when both answer (disagree → FAIL) and the downstream
    `country_matches` guards a single source returning a wrong country.
    The narrow residual risk (one source stale AND wrong AND the other
    down AND the real country wrong) is far rarer than the rate-limit
    false-fail it replaces.

    Round 4 #6: each input is canonicalised through `name_to_iso()`
    BEFORE comparison, so non-canonical aliases (`UK` ↔ `GB`,
    `United States` ↔ `US`, …) don't false-DISAGREE.  Debug strings
    on disagree preserve the raw inputs so the operator can see which
    provider returned which form.
    """
    a_raw = (ipinfo_iso or "").strip()
    b_raw = (ip2_iso or "").strip()
    a = name_to_iso(a_raw).upper() if a_raw else ""
    b = name_to_iso(b_raw).upper() if b_raw else ""
    if a and b and a == b:
        return ("ok", a)
    if a and b:
        return ("disagree", f"DISAGREE(ipinfo={a_raw!s},ip2location={b_raw!s})")
    if a or b:
        return ("single", a or b)
    return ("empty", "")


def country_matches(agreed_iso: str, expected: str) -> bool:
    """True if the consensus ISO-2 matches the expected country.
    `expected` can be ISO-2 (`CH`) or a long name (`Switzerland`,
    `United States`, `United States of America`).  Anything else
    (typos, unsupported country) → False, surfaced as a mismatch
    FAIL in the healthcheck output."""
    if not agreed_iso or not expected:
        return False
    return agreed_iso.upper() == name_to_iso(expected).upper()
