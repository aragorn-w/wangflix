"""qBittorrent WebUI client (cookie session auth). No username means no login, for
bypass-auth-on-LAN configs; a failed login is the caller's to handle."""

from __future__ import annotations

import json

import requests


class QBitClient:
    def __init__(self, base_url: str, username: str = "", password: str = "",
                 *, timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()

    def _post_login(self) -> tuple[int, str] | None:
        try:
            r = self.session.post(
                f"{self.base_url}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                headers={"Referer": self.base_url},
                timeout=self.timeout,
            )
            return r.status_code, r.text
        except Exception:
            return None

    def login(self) -> bool:
        """A no-op True when `username` is empty (bypass mode); False on login failure."""
        if not self.username:
            return True
        resp = self._post_login()
        return resp is not None and resp[0] == 200 and resp[1].strip().lower() == "ok."

    def login_response(self) -> str:
        """Raw login text ('Ok.', 'Fails.'), '' on a connection error, 'Ok.' without a request in
        bypass mode. Sets the SID cookie."""
        if not self.username:
            return "Ok."
        resp = self._post_login()
        return "" if resp is None else resp[1]

    def reachable_status(self) -> str:
        """'000' on a connection failure."""
        try:
            r = self.session.get(f"{self.base_url}/api/v2/torrents/info",
                                 timeout=self.timeout)
            return str(r.status_code)
        except Exception:
            return "000"

    def torrents_info(self) -> list[dict]:
        r = self.session.get(f"{self.base_url}/api/v2/torrents/info",
                             timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def delete_torrent(self, torrent_hash: str, *, delete_files: bool = True) -> None:
        r = self.session.post(
            f"{self.base_url}/api/v2/torrents/delete",
            data={"hashes": torrent_hash, "deleteFiles": delete_files},
            timeout=self.timeout,
        )
        r.raise_for_status()

    def preferences(self) -> dict:
        r = self.session.get(f"{self.base_url}/api/v2/app/preferences",
                             timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def set_preferences(self, values: dict) -> None:
        """JSON in a `json=` form field; a plain form body is silently ignored, so verify the
        change took effect rather than trust the 200."""
        r = self.session.post(
            f"{self.base_url}/api/v2/app/setPreferences",
            data={"json": json.dumps(values)},
            timeout=self.timeout,
        )
        r.raise_for_status()
