"""qBittorrent WebUI API client.

Wraps the cookie-based session auth so callers (nuke_stalled.py,
healthcheck.sh via the CLI) don't each re-implement the login dance.

Auth model:
  - QBIT_USER unset → no login, return an unauthenticated session
    (compatible with bypass-auth-on-LAN configurations)
  - QBIT_USER set + login Ok → session with SID cookie
  - QBIT_USER set + login FAILS → caller decides (most fail-loud via
    sys.exit; healthcheck reports the failure)
"""

from __future__ import annotations

import requests


class QBitClient:
    """qBittorrent WebUI client.  Persists a SID cookie via
    `requests.Session` so subsequent calls authenticate without
    re-login overhead."""

    def __init__(self, base_url: str, username: str = "", password: str = "",
                 *, timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self._logged_in = False

    def login(self) -> bool:
        """Authenticate.  Returns True on success.  Returns True with
        no-op when `username` is empty (bypass mode).  Returns False
        on login failure (caller decides whether to fail loud)."""
        if not self.username:
            self._logged_in = True
            return True
        try:
            r = self.session.post(
                f"{self.base_url}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                headers={"Referer": self.base_url},
                timeout=self.timeout,
            )
            self._logged_in = (r.status_code == 200
                               and r.text.strip().lower() == "ok.")
            return self._logged_in
        except Exception:
            return False

    def login_response(self) -> str:
        """Raw text of the login POST ('Ok.', 'Fails.', ...) or '' on a
        connection error.  Bypass mode (empty username) returns 'Ok.'
        without a request, mirroring `login()`'s no-op.  The health probe
        needs the literal response text to report it; `login()` only
        exposes the boolean verdict, and the POST sets the SID cookie on
        the shared session for a follow-up `reachable_status()`."""
        if not self.username:
            return "Ok."
        try:
            r = self.session.post(
                f"{self.base_url}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                headers={"Referer": self.base_url},
                timeout=self.timeout,
            )
            return r.text
        except Exception:
            return ""

    def reachable_status(self) -> str:
        """GET /api/v2/torrents/info on the session and return the HTTP
        status code string, or '000' on a connection failure.  Serves both
        the no-auth bypass reachability check and the post-login
        cookie-verified call (the session carries the SID cookie set by
        `login_response()`/`login()`)."""
        try:
            r = self.session.get(f"{self.base_url}/api/v2/torrents/info",
                                 timeout=self.timeout)
            return str(r.status_code)
        except Exception:
            return "000"

    def torrents_info(self) -> list[dict]:
        """GET /api/v2/torrents/info.  Returns the parsed list."""
        r = self.session.get(f"{self.base_url}/api/v2/torrents/info",
                             timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def delete_torrent(self, torrent_hash: str, *, delete_files: bool = True) -> None:
        """POST /api/v2/torrents/delete with deleteFiles=true.  Raises
        on HTTP failure so callers can log per-hash diagnostics."""
        r = self.session.post(
            f"{self.base_url}/api/v2/torrents/delete",
            data={"hashes": torrent_hash, "deleteFiles": delete_files},
            timeout=self.timeout,
        )
        r.raise_for_status()
