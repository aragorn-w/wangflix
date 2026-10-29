"""Two-provider consensus for the healthcheck VPN-country probe, compared as ISO-2. An unmapped
expected name matches literally, so it shows as a clear mismatch, not a false pass.
"""

from __future__ import annotations


# Countries we might configure as `VPN_COUNTRY=`; others compare as literal ISO-2. No aliases
# (UK) here: the reverse map is built from these values, and a duplicate would make the canonical
# code ambiguous. Aliases go in NAME_TO_ISO.
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

NAME_TO_ISO: dict[str, str] = {
    **{v.lower(): k for k, v in ISO_TO_NAME.items()},
    "united states of america": "US",   # ip2location's exact value
    "usa": "US",
    "u.s.a.": "US",
    "u.s.": "US",
    # "UK" is a non-canonical alias of ISO-2 "GB"
    "uk": "GB",
    "britain": "GB",
    "great britain": "GB",
    "u.k.": "GB",
    "korea": "KR",
    "republic of korea": "KR",
}


def name_to_iso(value: str) -> str:
    """Unknown input is returned unchanged, so it can still be compared against an unmapped
    provider code."""
    if not value:
        return ""
    v = value.strip()
    if len(v) == 2 and v.upper() in ISO_TO_NAME:
        return v.upper()
    return NAME_TO_ISO.get(v.lower(), v)


def iso_to_name(code: str) -> str:
    return ISO_TO_NAME.get((code or "").upper(), code)


def consensus(ipinfo_iso: str, ip2_iso: str) -> tuple[str, str]:
    """`(state, value)`; inputs go through `name_to_iso()` first so `UK`/`GB` don't false-disagree.

      - "ok":       both agree; `value` is the ISO-2 code
      - "disagree": both answered but differ; `value` lists the raw inputs
      - "single":   only one answered; `value` is its ISO-2. Not a hard fail (ipinfo.io rate-limits
                    the exit IP); the caller still checks it against the expected country.
      - "empty":    neither answered; `value` is ""
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
    """Typos and unsupported countries are False (healthcheck reports a mismatch)."""
    if not agreed_iso or not expected:
        return False
    return agreed_iso.upper() == name_to_iso(expected).upper()
