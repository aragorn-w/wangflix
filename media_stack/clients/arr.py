"""Sonarr / Radarr v3 API client. Read helpers return None on endpoint failure rather than raising
(`reachable_status` returns the HTTP status or '000'; `get_queue` and `delete_from_queue` raise)."""

from __future__ import annotations

import requests


class ArrClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Api-Key": api_key}
        self.timeout = timeout

    def reachable_status(self) -> str:
        """HTTP status as a string; '000' on a connection failure."""
        try:
            r = requests.get(f"{self.base_url}/api/v3/system/status",
                             headers=self.headers, timeout=self.timeout)
            return str(r.status_code)
        except Exception:
            return "000"

    def get_queue(self) -> list[dict]:
        """Paginated until exhausted: a single page hides page-2+ torrents, so
        `remove_by_download_id` would say `not_found` and `nuke_stalled` would skip blocklisting.
        A short page is NOT a stop signal when `totalRecords` says more exist (Arr can cap
        pageSize server-side); 100k safety cap.
        """
        out: list[dict] = []
        page = 1
        page_size = 1000
        while True:
            r = requests.get(
                f"{self.base_url}/api/v3/queue",
                params={"page": page, "pageSize": page_size},
                headers=self.headers, timeout=self.timeout,
            )
            r.raise_for_status()
            d = r.json()
            records = d.get("records") if isinstance(d, dict) else None
            if not isinstance(records, list) or not all(isinstance(x, dict) for x in records):
                # Malformed, not empty: [] would read as "nothing is downloading".
                raise ValueError("queue response has no list of record objects")
            total = d.get("totalRecords")
            if "totalRecords" in d and (not isinstance(total, int) or isinstance(total, bool)):
                # Absent is the short-page fallback; a present non-int ("1", null) is malformed.
                raise ValueError("queue response has a non-integer totalRecords")
            if not records:
                if total is not None and len(out) < total:
                    # Incomplete read, not the end of the queue; failing one run is the safe side.
                    raise ValueError("queue page is empty but totalRecords says more remain")
                break
            out.extend(records)
            if total is not None:
                if len(out) >= total:
                    break
            else:
                if len(records) < page_size:
                    break
            if len(out) > 100_000:
                break
            page += 1
        return out

    def unmonitored_no_file_count(self) -> int | None:
        """Radarr movies with `monitored=False AND hasFile=False`: invisible to the search loop."""
        try:
            r = requests.get(
                f"{self.base_url}/api/v3/movie",
                headers=self.headers, timeout=self.timeout,
            )
            r.raise_for_status()
            movies = r.json()
        except Exception:
            return None
        if not isinstance(movies, list):
            return None
        if not all(isinstance(m, dict) for m in movies):
            return None
        return sum(
            1 for m in movies
            if m.get("monitored") is False and m.get("hasFile") is False
        )

    def media_management(self) -> dict | None:
        try:
            r = requests.get(
                f"{self.base_url}/api/v3/config/mediamanagement",
                headers=self.headers, timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def hardlinks_enabled(self) -> bool | None:
        """Tri-state `copyUsingHardlinks`; None (endpoint failure, absent or non-boolean field)
        keeps "couldn't verify" distinct from "policy violated".
        """
        cfg = self.media_management()
        if cfg is None:
            return None
        # `enableHardlinks` is null on current versions.
        v = cfg.get("copyUsingHardlinks")
        if v is True:
            return True
        if v is False:
            return False
        return None

    def quality_profiles(self) -> list[dict] | None:
        try:
            r = requests.get(
                f"{self.base_url}/api/v3/qualityprofile",
                headers=self.headers, timeout=self.timeout,
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def format_score_violations(
        self, required: dict[str, int],
        *, profile_name_substring: str | None = None,
    ) -> list[tuple[str, str, object]] | None:
        """`required` maps a case-insensitive format-name substring to its required score.
        Returns `(profile, fmt_label, actual)` per violation (empty = compliant); `actual` is the
        score, or "MISSING". `profile_name_substring` limits checks to matching profiles; only
        with it is a missing format a violation, so deleting the AV1 format can't silently drop
        the block.
        """
        profiles = self.quality_profiles()
        if profiles is None:
            return None
        violations: list[tuple[str, str, object]] = []
        ns_lc = profile_name_substring.lower() if profile_name_substring else None
        any_target_matched = False
        for p in profiles:
            pname = p.get("name", "<unnamed>")
            matched_target = (ns_lc is None) or (ns_lc in pname.lower())
            if ns_lc is not None and not matched_target:
                continue
            any_target_matched = True
            seen_needles: set[str] = set()
            for fmt in p.get("formatItems") or []:
                fname = fmt.get("name", "")
                fname_lc = fname.lower()
                for needle, want_score in required.items():
                    if needle.lower() in fname_lc:
                        seen_needles.add(needle)
                        actual = fmt.get("score")
                        if actual != want_score:
                            violations.append((pname, fname, actual))
            if ns_lc is not None:
                for needle in required:
                    if needle not in seen_needles:
                        violations.append((pname, needle, "MISSING"))
        # A filter matching nothing (every Shield profile renamed or deleted) must not pass silently.
        if profile_name_substring and not any_target_matched:
            violations.append((
                "<no matching profile>",
                profile_name_substring,
                "NO_TARGET_PROFILE",
            ))
        return violations

    def delete_from_queue(self, queue_id: int, *, blocklist: bool = True) -> None:
        url = (f"{self.base_url}/api/v3/queue/{queue_id}"
               f"?removeFromClient=true&blocklist={'true' if blocklist else 'false'}")
        r = requests.delete(url, headers=self.headers, timeout=self.timeout)
        r.raise_for_status()

    def remove_by_download_id(self, download_id: str, *,
                              blocklist: bool = True) -> str:
        """Remove+blocklist the queue record whose downloadId (qBit hash) matches. Returns
        "removed", "not_found", "queue_error" or "delete_failed"; a failed DELETE must not look
        like "no record", or callers fall back to a qBit delete that skips blocklisting.
        """
        try:
            records = self.get_queue()
        except Exception:
            return "queue_error"
        for item in records:
            if item.get("downloadId", "").lower() == download_id.lower():
                try:
                    self.delete_from_queue(item["id"], blocklist=blocklist)
                    return "removed"
                except Exception:
                    return "delete_failed"
        return "not_found"

    def movies(self) -> list[dict] | None:
        try:
            r = requests.get(f"{self.base_url}/api/v3/movie",
                             headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return None
        if not isinstance(data, list) or not all(isinstance(m, dict) for m in data):
            return None
        return data

    def run_command(self, name: str, **fields: object) -> int | None:
        """The queued command's id, or None on any failure (including a non-int id). Asynchronous."""
        try:
            r = requests.post(
                f"{self.base_url}/api/v3/command",
                json={"name": name, **fields},
                headers=self.headers, timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            return None
        cid = data.get("id") if isinstance(data, dict) else None
        if isinstance(cid, int) and not isinstance(cid, bool):
            return cid
        return None

    def rescan_movie(self, movie_id: int) -> bool:
        """So Radarr re-imports the keeper after dedupe removes the file it tracked; True on 2xx."""
        try:
            r = requests.post(
                f"{self.base_url}/api/v3/command",
                json={"name": "RescanMovie", "movieId": movie_id},
                headers=self.headers, timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except Exception:
            return False

    def series(self) -> list[dict] | None:
        try:
            r = requests.get(f"{self.base_url}/api/v3/series",
                             headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except Exception:
            return None
        if not isinstance(data, list) or not all(isinstance(s, dict) for s in data):
            return None
        return data

    def episodes(self, series_id: int) -> list[dict] | None:
        try:
            r = requests.get(
                f"{self.base_url}/api/v3/episode",
                params={"seriesId": series_id},
                headers=self.headers, timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            return None
        if not isinstance(data, list) or not all(isinstance(e, dict) for e in data):
            return None
        return data

    def episode_files(self, series_id: int) -> list[dict] | None:
        """A record may be an orphan row left after an upgrade-import (see `delete_episode_file`)."""
        try:
            r = requests.get(
                f"{self.base_url}/api/v3/episodefile",
                params={"seriesId": series_id},
                headers=self.headers, timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            return None
        if not isinstance(data, list) or not all(isinstance(f, dict) for f in data):
            return None
        return data

    def rescan_series(self, series_id: int) -> bool:
        """So Sonarr re-imports the keeper after dedupe removes the file it tracked; True on 2xx."""
        try:
            r = requests.post(
                f"{self.base_url}/api/v3/command",
                json={"name": "RescanSeries", "seriesId": series_id},
                headers=self.headers, timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except Exception:
            return False

    def delete_episode_file(self, episode_file_id: int) -> bool:
        """Removes the DB row AND any file still on disk, so tv-dedupe.py must move the file to
        recycle first. True on 2xx; never raises."""
        try:
            r = requests.delete(
                f"{self.base_url}/api/v3/episodefile/{episode_file_id}",
                headers=self.headers, timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except Exception:
            return False
