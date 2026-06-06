"""Jellyfin HTTP adapter — used by `jellyfin-mint-api-key.py`.

Unlike the other adapters (ArrClient/BazarrClient/QBitClient bind a single
API key in __init__), the API-key minter juggles MANY keys in one run — the
bootstrap key, the freshly-minted key, and every pre-existing key it
re-verifies.  So this client binds only the base URL and takes the key
per call.  Uses urllib (no `requests` dependency for this ops-only path).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class NetworkError(Exception):
    """Transport-level failure (connection refused, DNS, timeout, generic
    OSError) — distinct from a non-2xx HTTP response, which is returned via
    the (status, body) tuple so callers can branch on the code.  Lets the
    minter map unreachable-Jellyfin to a controlled exit instead of letting
    an uncontrolled traceback escape."""


class JellyfinClient:
    """Thin Jellyfin HTTP wrapper.  Base URL bound; key passed per call."""

    def __init__(self, base_url: str, *, timeout: int = 10) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, path: str, key: str, *, method: str = "GET",
                body: dict | None = None) -> tuple[int, bytes]:
        """Return (status, body) for ANY HTTP response (2xx or otherwise) so
        the caller can branch on the code without losing the body.

        Raises `NetworkError` on transport-level failure (connection refused,
        DNS failure, timeout, etc.) — the caller lets it propagate to the
        top-level handler, which catches once and exits non-zero.
        """
        url = f"{self.base}{path}"
        headers = {"X-Emby-Token": key, "Content-Type": "application/json"}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            # Non-2xx with body — surface for the caller's status check.
            return e.code, e.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # URLError covers connection-refused + DNS; TimeoutError covers
            # socket timeout; OSError covers any other socket-layer failure.
            raise NetworkError(f"{method} {url}: {type(e).__name__}: {e}") from e

    def authenticate(self, username: str, password: str) -> tuple[int, bytes]:
        """POST /Users/AuthenticateByName to confirm a username+password
        combo still works.  Returns (status, body) so the caller can also
        verify User.Policy.IsAdministrator (a user who can still log in but
        lost admin would pass a status-only check).  Raises NetworkError on
        transport failure (same contract as request())."""
        url = f"{self.base}/Users/AuthenticateByName"
        headers = {
            "Content-Type": "application/json",
            # Jellyfin requires SOME Authorization header even for the
            # password-only login endpoint.  Identifier values are arbitrary.
            "Authorization": (
                'MediaBrowser Client="jellyfin-mint-api-key", '
                'Device="mediahost", DeviceId="mint-script", Version="1.0"'
            ),
        }
        body = json.dumps({"Username": username, "Pw": password}).encode()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise NetworkError(f"POST {url}: {type(e).__name__}: {e}") from e

    def list_keys(self, key: str) -> list[dict]:
        """Snapshot the current ApiKeys list.  Each entry has `AppName`
        (Jellyfin's API field — direct SQL calls it `Name`), `AccessToken`,
        and `DateCreated`.

        Standard Jellyfin returns `{"Items": [...]}`; some versions / proxy
        layers return the bare list.  Branch on type explicitly, REQUIRE
        `Items` on a dict response (a dict without it — `{"error": ...}` —
        must not silently return [] and mask existing keys), and validate
        every entry is a dict with a non-empty string AccessToken AND AppName
        (both are indexed later; the dup-name check keys on AppName).  Raises
        RuntimeError on any malformed shape; error messages never echo the
        response body (it may carry tokens)."""
        status, raw = self.request("/Auth/Keys", key)
        if status != 200:
            raise RuntimeError(f"/Auth/Keys returned HTTP {status}")
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            items = parsed
        elif isinstance(parsed, dict):
            if "Items" not in parsed:
                raise RuntimeError(
                    f"/Auth/Keys dict response missing 'Items' field "
                    f"(keys: {sorted(parsed.keys())})"
                )
            items = parsed["Items"]
        else:
            raise RuntimeError(
                f"/Auth/Keys returned unexpected JSON shape: {type(parsed).__name__}"
            )
        if not isinstance(items, list):
            raise RuntimeError(
                f"/Auth/Keys Items field is not a list: {type(items).__name__}"
            )
        for i, entry in enumerate(items):
            if not isinstance(entry, dict):
                raise RuntimeError(
                    f"/Auth/Keys Items[{i}] is not a dict: {type(entry).__name__}"
                )
            tok = entry.get("AccessToken")
            if not isinstance(tok, str) or not tok:
                raise RuntimeError(
                    f"/Auth/Keys Items[{i}] has invalid AccessToken: "
                    f"type={type(tok).__name__} empty={not tok}"
                )
            app_name = entry.get("AppName")
            if not isinstance(app_name, str) or not app_name:
                raise RuntimeError(
                    f"/Auth/Keys Items[{i}] has invalid AppName: "
                    f"type={type(app_name).__name__} empty={not app_name}"
                )
        return items

    def create_key(self, key: str, name: str) -> None:
        """POST /Auth/Keys?App=<name>.  Jellyfin doesn't return the new key
        value in the POST response on any 10.x version, so callers re-list
        and identify the new row by token-not-in-pre-snapshot.  This just
        performs the mutation.  Raises RuntimeError on non-2xx (status only,
        never the body — it may carry tokens); NetworkError on transport."""
        name_q = urllib.parse.quote(name, safe="")
        status, _ = self.request(f"/Auth/Keys?App={name_q}", key, method="POST")
        if status not in (200, 204):
            raise RuntimeError(f"POST /Auth/Keys returned HTTP {status}")
