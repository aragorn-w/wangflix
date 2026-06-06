"""Bazarr API client.

Bazarr uses an HTTP API similar to Sonarr/Radarr but with `X-API-KEY`
header (note the differing capitalization vs `X-Api-Key` for the Arrs)
and slightly different endpoint shapes (`/api/system/status`, no v3
prefix).

The apikey lives inside the Bazarr container at
`/config/config/config.yaml` under `general.apikey`.  We don't carry
it in `.env` per the credential policy (don't duplicate live keys
that the container already owns).  `apikey_from_container()` reads
it via `docker exec`.
"""

from __future__ import annotations

import subprocess

import requests


class BazarrClient:
    """Bazarr WebUI/API client."""

    def __init__(self, base_url: str, api_key: str, *, timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-API-KEY": api_key}
        self.timeout = timeout

    def system_status(self) -> dict | None:
        try:
            r = requests.get(f"{self.base_url}/api/system/status",
                             headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def reachable_status(self) -> str:
        """GET /api/system/status and return the HTTP status code as a
        string, or '000' on a connection-level failure.  The health probe
        reports the literal code (not the body `system_status()` parses)."""
        try:
            r = requests.get(f"{self.base_url}/api/system/status",
                             headers=self.headers, timeout=self.timeout)
            return str(r.status_code)
        except Exception:
            return "000"

    def movies(self, length: int = 10000) -> list[dict] | None:
        """GET /api/movies.  Returns the raw items list or None on
        failure.  `length` matches Bazarr's pagination param."""
        try:
            r = requests.get(f"{self.base_url}/api/movies",
                             params={"length": length},
                             headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
            d = r.json()
            return d.get("data") or d.get("items") or []
        except Exception:
            return None

    def series(self, length: int = 10000) -> list[dict] | None:
        try:
            r = requests.get(f"{self.base_url}/api/series",
                             params={"length": length},
                             headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
            d = r.json()
            return d.get("data") or d.get("items") or []
        except Exception:
            return None

    def unprofiled_count(self) -> tuple[int | None, int | None]:
        """Return `(unprofiled_movies, unprofiled_series)` — number of
        items with `profileId == None`.  Either side is None on
        endpoint failure."""
        m = self.movies()
        s = self.series()
        def count_none(items: list[dict] | None) -> int | None:
            if items is None:
                return None
            return sum(1 for it in items if it.get("profileId") is None)
        return count_none(m), count_none(s)

    def wrong_profile_count(self, expected: int) -> tuple[int | None, int | None]:
        """Return `(wrong_movies, wrong_series)` — count of items whose
        `profileId` is not None AND not equal to `expected`.  Codex
        round-13 #2: `unprofiled_count` only catches profileId=None;
        a wrong-but-non-None profile (e.g. someone manually picked a
        Spanish-only profile on an English item) would still pass.

        Items with profileId=None are EXCLUDED from this count —
        they're already covered by `unprofiled_count` and would
        double-flag otherwise.

        Either side is None on endpoint failure.
        """
        m = self.movies()
        s = self.series()
        def count_wrong(items: list[dict] | None) -> int | None:
            if items is None:
                return None
            return sum(
                1 for it in items
                if it.get("profileId") is not None
                and it.get("profileId") != expected
            )
        return count_wrong(m), count_wrong(s)

    def assign_movie_profile(self, radarr_id: int, profile_id: int) -> bool:
        """POST /api/movies with `radarrid` + `profileid` form fields.
        Bazarr's profile-assignment endpoint takes form-encoded body
        (NOT JSON).  Returns True on 2xx, False on any failure.
        """
        try:
            r = requests.post(
                f"{self.base_url}/api/movies",
                data={"radarrid": radarr_id, "profileid": profile_id},
                headers=self.headers, timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except Exception:
            return False

    def assign_series_profile(self, sonarr_series_id: int, profile_id: int) -> bool:
        """POST /api/series with `seriesid` + `profileid` form fields.
        Mirrors `assign_movie_profile` for series items."""
        try:
            r = requests.post(
                f"{self.base_url}/api/series",
                data={"seriesid": sonarr_series_id, "profileid": profile_id},
                headers=self.headers, timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except Exception:
            return False

    def trigger_task(self, task_id: str) -> bool:
        """POST /api/system/tasks?taskid=<id> to kick a Bazarr task —
        e.g. `wanted_search_missing_subtitles_movies` after we just
        assigned a profile to previously-unprofiled items.  Returns
        True on 2xx."""
        try:
            r = requests.post(
                f"{self.base_url}/api/system/tasks",
                params={"taskid": task_id},
                headers=self.headers, timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except Exception:
            return False


def apikey_from_container(container: str = "bazarr",
                          config_path: str = "/config/config/config.yaml",
                          timeout: int = 10) -> str:
    """Extract the Bazarr `general.apikey` from inside its running
    container.  Bazarr's config.yaml has multiple `apikey:` keys (one
    per Arr integration); the top-level general.apikey is indented by
    exactly 2 spaces, so the awk pattern anchors on `^  apikey:`.

    Returns "" on failure.
    """
    try:
        r = subprocess.run(
            ["docker", "exec", container,
             "awk", "-F:",
             "/^  apikey:/{gsub(/[ \"]/,\"\",$2); print $2; exit}",
             config_path],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""
