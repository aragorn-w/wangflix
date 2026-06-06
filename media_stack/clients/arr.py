"""Unified Sonarr / Radarr API client.

Both apps share the v3 API surface; differences are limited to base
URL + which "queue" record types are returned.  Wrap the few endpoints
the host-side scripts actually use:

  - get_queue() — list current download queue records
  - delete_from_queue() — remove + blocklist a download
  - system_status() — health-probe reachability
"""

from __future__ import annotations

import requests


class ArrClient:
    """Thin wrapper around a Sonarr/Radarr `/api/v3` endpoint."""

    def __init__(self, base_url: str, api_key: str, *, timeout: int = 15):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Api-Key": api_key}
        self.timeout = timeout

    def system_status(self) -> dict | None:
        """GET /api/v3/system/status.  Returns the parsed dict or None
        on any failure (network, non-200, malformed JSON)."""
        try:
            r = requests.get(f"{self.base_url}/api/v3/system/status",
                             headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def reachable_status(self) -> str:
        """GET /api/v3/system/status and return the HTTP status code as a
        string ('200', a non-2xx code, ...) or '000' on a connection-level
        failure.  Health probes report the literal code, so this returns it
        directly rather than the parsed body `system_status()` provides."""
        try:
            r = requests.get(f"{self.base_url}/api/v3/system/status",
                             headers=self.headers, timeout=self.timeout)
            return str(r.status_code)
        except Exception:
            return "000"

    def get_queue(self) -> list[dict]:
        """GET /api/v3/queue — paginate until exhausted.

        Arr v3 paginates (default ~10 per page).  A torrent on page 2+
        is invisible to a single-page query — `remove_by_download_id`
        would return `not_found` for it and `nuke_stalled` would fall
        through to a direct qBit delete that skips blocklisting.

        Stop conditions (in priority order):
          1. `totalRecords` known AND `len(records) >= totalRecords`.
          2. `totalRecords` absent AND short page (< pageSize) returns.
          3. Empty page.
          4. 100k safety cap.

        Short-page is NOT a stop signal when `totalRecords` says more
        exist — Arr can server-cap pageSize below the requested value.
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
            records = d.get("records") or []
            if not records:
                break
            out.extend(records)
            total = d.get("totalRecords")
            if total is not None:
                if len(out) >= total:
                    break
                # totalRecords still ahead of us — keep paginating even
                # if this page was short (server-capped pageSize).
            else:
                # No totalRecords hint: short page is the only signal.
                if len(records) < page_size:
                    break
            if len(out) > 100_000:
                break
            page += 1
        return out

    def unmonitored_no_file_count(self) -> int | None:
        """Count Radarr movies with `monitored=False AND hasFile=False`.

        This is the "silent gap" pattern: a movie sits in the library
        with no file AND no plan to grab one, invisible to Radarr's
        search loop until a human notices the wrong number on the
        shelf.  Bit us 2026-05-31 when ~91 movies (40% of the library)
        had silently fallen into this state and Jellyfin was missing
        a third of its expected catalog.

        Healthcheck wires this into a per-cron probe so the drift
        can't recur silently.  Returns the count, or None on endpoint
        failure (caller's responsibility to surface as WARN, not
        false-clear).
        """
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
        # Codex security review #3: also require every list entry to
        # be a dict.  Without this guard, a malformed entry (None,
        # string, number) would raise AttributeError at `.get(...)`
        # and crash the probe instead of surfacing as WARN.  Probe's
        # whole point is robust drift detection — never crash on a
        # weird API shape.
        if not all(isinstance(m, dict) for m in movies):
            return None
        return sum(
            1 for m in movies
            if m.get("monitored") is False and m.get("hasFile") is False
        )

    def media_management(self) -> dict | None:
        """GET /api/v3/config/mediamanagement.  Returns the parsed
        config dict or None on failure.  Useful for verifying
        documented policy invariants from healthchecks (e.g.
        hardlinks must be enabled — AUDIT A7).
        """
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
        """Convenience: True if `copyUsingHardlinks` is True, False if
        explicitly disabled, None on endpoint failure OR if the field
        is absent / non-boolean.  Strict tri-state so the caller can
        distinguish "policy violated" from "couldn't verify" (codex
        round-11 #5: was `bool(cfg.get(...))` which collapsed a
        missing key to False, false-positive policy violation).
        """
        cfg = self.media_management()
        if cfg is None:
            return None
        # Field is `copyUsingHardlinks` on Sonarr+Radarr v3 (the older
        # `enableHardlinks` returns null on current versions).
        v = cfg.get("copyUsingHardlinks")
        if v is True:
            return True
        if v is False:
            return False
        return None  # missing key or non-boolean → "couldn't verify"

    def quality_profiles(self) -> list[dict] | None:
        """GET /api/v3/qualityprofile.  Returns the full list of
        profile dicts (each with `formatItems`, `name`, `id`, etc.)
        or None on endpoint failure.  Used by healthcheck to verify
        Custom Format scores match documented policy (AV1 +
        12-bit HEVC must score -10000 on every active profile)."""
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
        """Check quality profiles for the required Custom Format
        scores.  `required` maps format-name-substring (case-
        insensitive) to required score.  Returns a list of
        `(profile_name, fmt_label, actual_value)` tuples for every
        violation.  An empty list means policy is in compliance.
        None on endpoint failure.

        Two violation kinds:
          - "wrong score": format is in the profile but score != required
              → tuple is (profile, fmt_name, actual_score_int)
          - "missing format": profile matches the target filter but
              the required format isn't in the profile at all
              → tuple is (profile, fmt_needle, "MISSING")

        `profile_name_substring` (case-insensitive) restricts
        enforcement to a subset of profiles — typically "Shield" on
        this stack so a fresh untargeted test profile doesn't fail
        the probe.  When None, every profile is enforced and missing
        formats are NOT counted as violations (codex round-15
        original semantics; behavior preserved for the
        backward-compat case).

        Codex round-16 #1: previously, a profile missing the required
        format entirely would silently pass even when policy says it
        MUST score -10000.  An operator deleting the AV1 custom
        format from Shield would have removed the block without
        any signal.  Now with a profile filter, missing formats on
        matched profiles are FAIL.
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
            # Codex round-17 #1: when a filter is supplied, SKIP
            # non-matching profiles entirely.  Round-16's fix only
            # applied the filter to the missing-format pass, so a
            # non-Shield profile with AV1=0 would still fail the
            # wrong-score loop.  Inconsistent + caused false alerts.
            if ns_lc is not None and not matched_target:
                continue
            any_target_matched = True
            # Track which required-format needles we saw on this profile,
            # for the missing-format pass below.
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
            # Missing-format pass: only enforced when a profile filter
            # was supplied (the matched_target check above already
            # guaranteed we're on a target-matching profile).  Without
            # a filter, we preserve the original "only check what's
            # present" semantics.
            if ns_lc is not None:
                for needle in required:
                    if needle not in seen_needles:
                        violations.append((pname, needle, "MISSING"))
        # Codex round-18 #2: if a filter was supplied but NO profile
        # matched, the function would have returned [] (silent pass).
        # An operator renaming or deleting every Shield profile would
        # have removed the entire enforcement scope without any signal.
        # Flag the empty-target-set case explicitly.
        # `bool(profile_name_substring)` is equivalent to `ns_lc is not None`
        # (ns_lc is derived from it on line 211) — guard on the original so the
        # reported value keeps its case and the type narrows to str.
        if profile_name_substring and not any_target_matched:
            violations.append((
                "<no matching profile>",
                profile_name_substring,
                "NO_TARGET_PROFILE",
            ))
        return violations

    def delete_from_queue(self, queue_id: int, *, blocklist: bool = True) -> None:
        """DELETE /api/v3/queue/<id>?removeFromClient=true&blocklist=true."""
        url = (f"{self.base_url}/api/v3/queue/{queue_id}"
               f"?removeFromClient=true&blocklist={'true' if blocklist else 'false'}")
        r = requests.delete(url, headers=self.headers, timeout=self.timeout)
        r.raise_for_status()

    def remove_by_download_id(self, download_id: str, *,
                              blocklist: bool = True) -> str:
        """Find the queue record matching `download_id` (qBit hash) and
        remove+blocklist it.  Tri-state return so callers can tell
        "no record exists" apart from "record existed but DELETE failed"
        (codex caught: returning False for both let nuke_stalled.py
        fall through to a direct-qBit delete on the failed case, which
        bypasses blocklisting and lets the next grab re-import the
        same torrent).

        Returns one of:
          "removed"         — match found + delete succeeded
          "not_found"       — queue queried OK, no matching downloadId
          "queue_error"     — couldn't list the queue
          "delete_failed"   — matched, but DELETE rejected
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
        """GET /api/v3/movie — every movie with its embedded `movieFile`.
        Returns the list, or None on endpoint failure / malformed shape.
        Used by the movie-dedupe tool to learn which on-disk file Radarr
        currently tracks per movie."""
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

    def rescan_movie(self, movie_id: int) -> bool:
        """POST /api/v3/command {name: RescanMovie, movieId}.  Triggers a
        disk rescan so Radarr re-imports the keeper after the dedupe tool
        removes the file it was tracking.  Returns True on 2xx, False on
        any failure."""
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
