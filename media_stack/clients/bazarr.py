"""Bazarr API client (`X-API-KEY`). The apikey lives in the container's config.yaml, not `.env`:
see `apikey_from_container()`."""

from __future__ import annotations

import subprocess

import requests


class BazarrClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-API-KEY": api_key}
        self.timeout = timeout

    def reachable_status(self) -> str:
        """'000' on a connection failure."""
        try:
            r = requests.get(f"{self.base_url}/api/system/status",
                             headers=self.headers, timeout=self.timeout)
            return str(r.status_code)
        except Exception:
            return "000"

    def movies(self, length: int = 10000) -> list[dict] | None:
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
        """`(movies, series)` with `profileId == None`; a side is None on endpoint failure."""
        m = self.movies()
        s = self.series()
        def count_none(items: list[dict] | None) -> int | None:
            if items is None:
                return None
            return sum(1 for it in items if it.get("profileId") is None)
        return count_none(m), count_none(s)

    def wrong_profile_count(self, expected: int) -> tuple[int | None, int | None]:
        """Counts whose `profileId` is neither None (see `unprofiled_count`) nor `expected`."""
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
        """Form-encoded (NOT JSON) body; True on 2xx."""
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
    """`general.apikey` from the running container, "" on failure. config.yaml has one `apikey:`
    per Arr integration; general's is indented exactly 2 spaces.
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
