"""Jellyfin HTTP adapter (urllib) for the API-key minter. Binds only the base URL and takes the
key per call, since the minter juggles many keys."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


class NetworkError(Exception):
    """Transport-level failure; a non-2xx response is returned as (status, body) instead."""


class JellyfinClient:
    def __init__(self, base_url: str, *, timeout: int = 10) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, path: str, key: str, *, method: str = "GET",
                body: dict | None = None) -> tuple[int, bytes]:
        """(status, body) for ANY HTTP response; raises `NetworkError` on transport failure."""
        url = f"{self.base}{path}"
        # This Jellyfin answers `X-Emby-Token` and `?api_key=` with 401 even for a valid key.
        # Never log `headers` or `key`.
        headers = {
            "Authorization": f'MediaBrowser Token="{key}"',
            "Content-Type": "application/json",
        }
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise NetworkError(f"{method} {url}: {type(e).__name__}: {e}") from e

    def authenticate(self, username: str, password: str) -> tuple[int, bytes]:
        """(status, body), so the caller can check User.Policy.IsAdministrator."""
        url = f"{self.base}/Users/AuthenticateByName"
        headers = {
            "Content-Type": "application/json",
            # Required even for login; values are arbitrary.
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
        """Raises RuntimeError on any malformed shape (a dict without `Items` must not read as []
        and mask existing keys), never echoing the body (it may carry tokens)."""
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

    def now_playing_sessions(self, key: str) -> list[dict] | None:
        """Sessions with an active `NowPlayingItem`. FAILS CLOSED: None on ANY failure
        (unreachable, bad key, non-2xx, non-list body, non-dict element, or a `NowPlayingItem`
        present but not a non-empty dict: idle is the key being omitted). Callers must treat None
        as "someone is watching" and check it explicitly: None and [] are both falsy. Never raises."""
        try:
            status, body = self.request("/Sessions", key)
        except NetworkError:
            return None
        if status != 200:
            return None
        try:
            sessions = json.loads(body)
        except ValueError:
            return None
        if not isinstance(sessions, list):
            return None
        if any(not isinstance(s, dict) for s in sessions):
            return None
        for s in sessions:
            if "NowPlayingItem" in s:
                item = s["NowPlayingItem"]
                if not (isinstance(item, dict) and item):
                    return None
        return [s for s in sessions if s.get("NowPlayingItem")]

    def create_key(self, key: str, name: str) -> None:
        """The response doesn't carry the new key: callers re-list and diff. RuntimeError on
        non-2xx (status only, never the body)."""
        name_q = urllib.parse.quote(name, safe="")
        status, _ = self.request(f"/Auth/Keys?App={name_q}", key, method="POST")
        if status not in (200, 204):
            raise RuntimeError(f"POST /Auth/Keys returned HTTP {status}")
