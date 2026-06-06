"""Tests for media_stack.clients — service wrappers (Arr, qBit, Bazarr)."""

import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.clients.arr import ArrClient
from media_stack.clients.qbit import QBitClient
from media_stack.clients.bazarr import BazarrClient
from media_stack.clients.telegram import send as telegram_send
from media_stack.clients.jellyfin import JellyfinClient, NetworkError


# --- ArrClient ---

def test_arr_system_status_success():
    with patch("requests.get") as mock_get:
        mock_get.return_value = MagicMock(
            status_code=200, json=lambda: {"version": "5.0.0"},
        )
        mock_get.return_value.raise_for_status = lambda: None
        c = ArrClient("http://radarr:7878", "abc123")
        assert c.system_status() == {"version": "5.0.0"}


def test_arr_system_status_returns_none_on_failure():
    with patch("requests.get", side_effect=Exception("network down")):
        c = ArrClient("http://radarr:7878", "abc123")
        assert c.system_status() is None


def test_arr_reachable_status_returns_code():
    with patch("requests.get", return_value=MagicMock(status_code=200)):
        assert ArrClient("http://radarr:7878", "k").reachable_status() == "200"
    with patch("requests.get", return_value=MagicMock(status_code=503)):
        assert ArrClient("http://radarr:7878", "k").reachable_status() == "503"


def test_arr_reachable_status_000_on_connection_error():
    with patch("requests.get", side_effect=Exception("refused")):
        assert ArrClient("http://radarr:7878", "k").reachable_status() == "000"


def test_arr_movies_returns_list():
    payload = [{"id": 1, "path": "/movies/A (2020)", "movieFile": {"relativePath": "A.mkv"}}]
    with patch("requests.get") as mock_get:
        resp = MagicMock(status_code=200, json=lambda: payload)
        resp.raise_for_status = lambda: None
        mock_get.return_value = resp
        assert ArrClient("http://radarr:7878", "k").movies() == payload


def test_arr_movies_none_on_non_list_shape():
    with patch("requests.get") as mock_get:
        resp = MagicMock(status_code=200, json=lambda: {"error": "x"})
        resp.raise_for_status = lambda: None
        mock_get.return_value = resp
        assert ArrClient("http://radarr:7878", "k").movies() is None


def test_arr_movies_none_on_failure():
    with patch("requests.get", side_effect=Exception("down")):
        assert ArrClient("http://radarr:7878", "k").movies() is None


def test_arr_rescan_movie_true_on_2xx():
    with patch("requests.post") as mock_post:
        resp = MagicMock(status_code=201)
        resp.raise_for_status = lambda: None
        mock_post.return_value = resp
        assert ArrClient("http://radarr:7878", "k").rescan_movie(42) is True
        # posts the RescanMovie command for the right movie id
        _, kwargs = mock_post.call_args
        assert kwargs["json"] == {"name": "RescanMovie", "movieId": 42}


def test_arr_rescan_movie_false_on_failure():
    with patch("requests.post", side_effect=Exception("boom")):
        assert ArrClient("http://radarr:7878", "k").rescan_movie(42) is False


def test_arr_get_queue_returns_records():
    with patch("requests.get") as mock_get:
        resp = MagicMock(status_code=200,
                         json=lambda: {"records": [{"id": 1}, {"id": 2}]})
        resp.raise_for_status = lambda: None
        mock_get.return_value = resp
        c = ArrClient("http://radarr:7878", "k")
        assert c.get_queue() == [{"id": 1}, {"id": 2}]


def test_arr_remove_by_download_id_success():
    fake_records = [
        {"id": 1, "downloadId": "ABCDEF", "title": "Movie 1"},
        {"id": 2, "downloadId": "123456", "title": "Movie 2"},
    ]
    with patch("requests.get") as mock_get, patch("requests.delete") as mock_del:
        mock_get.return_value = MagicMock(
            status_code=200, json=lambda: {"records": fake_records},
        )
        mock_get.return_value.raise_for_status = lambda: None
        mock_del.return_value = MagicMock(status_code=200)
        mock_del.return_value.raise_for_status = lambda: None
        c = ArrClient("http://radarr:7878", "k")
        assert c.remove_by_download_id("abcdef") == "removed"
        # Verify the DELETE went to the right queue id
        assert "/queue/1?" in mock_del.call_args.args[0]


def test_arr_remove_by_download_id_no_match():
    with patch("requests.get") as mock_get:
        mock_get.return_value = MagicMock(
            status_code=200, json=lambda: {"records": []},
        )
        mock_get.return_value.raise_for_status = lambda: None
        c = ArrClient("http://radarr:7878", "k")
        assert c.remove_by_download_id("nomatch") == "not_found"


def test_arr_remove_by_download_id_queue_error():
    """Codex round-module-split #3: queue lookup failure must be
    distinguishable from a clean 'not found' so the caller doesn't
    fall through to a direct qBit delete (which skips blocklisting)."""
    with patch("requests.get", side_effect=Exception("network down")):
        c = ArrClient("http://radarr:7878", "k")
        assert c.remove_by_download_id("anything") == "queue_error"


def test_arr_get_queue_paginates_until_exhausted():
    """Codex round-module-split-2 #2: the queue endpoint paginates and
    the client must walk pages or it silently misses torrents on
    page 2+."""
    page1 = {
        "records": [{"id": i} for i in range(1000)],
        "totalRecords": 1500,
    }
    page2 = {
        "records": [{"id": i} for i in range(1000, 1500)],
        "totalRecords": 1500,
    }
    with patch("requests.get") as mock_get:
        responses = []
        for d in (page1, page2):
            r = MagicMock(status_code=200, json=lambda d=d: d)
            r.raise_for_status = lambda: None
            responses.append(r)
        mock_get.side_effect = responses
        c = ArrClient("http://radarr:7878", "k")
        out = c.get_queue()
    assert len(out) == 1500
    # Both pages were fetched
    assert mock_get.call_count == 2


def test_arr_get_queue_keeps_paginating_on_short_page_when_total_is_known():
    """Codex round-7 #2: Arr can cap pageSize below the requested 1000
    server-side.  A short page with `totalRecords` still ahead must
    NOT short-circuit — otherwise records on page 2+ are missed and
    `remove_by_download_id` falls through to a direct qBit delete that
    skips Arr blocklisting."""
    # Page 1 has 800 records but totalRecords claims 1200 (server cap)
    page1 = {
        "records": [{"id": i} for i in range(800)],
        "totalRecords": 1200,
    }
    page2 = {
        "records": [{"id": i} for i in range(800, 1200)],
        "totalRecords": 1200,
    }
    with patch("requests.get") as mock_get:
        responses = []
        for d in (page1, page2):
            r = MagicMock(status_code=200, json=lambda d=d: d)
            r.raise_for_status = lambda: None
            responses.append(r)
        mock_get.side_effect = responses
        c = ArrClient("http://radarr:7878", "k")
        out = c.get_queue()
    assert len(out) == 1200
    assert mock_get.call_count == 2  # both pages fetched despite short page 1


def test_arr_get_queue_short_page_fallback_when_no_total():
    """When `totalRecords` is absent, short page IS the only stop
    signal — must continue to use that as the fallback."""
    page1 = {
        "records": [{"id": i} for i in range(500)],
        # NOTE: no totalRecords field
    }
    with patch("requests.get") as mock_get:
        r = MagicMock(status_code=200, json=lambda: page1)
        r.raise_for_status = lambda: None
        mock_get.return_value = r
        c = ArrClient("http://radarr:7878", "k")
        out = c.get_queue()
    assert len(out) == 500
    assert mock_get.call_count == 1  # short page + no total → stop


def test_arr_remove_by_download_id_finds_match_on_page_2():
    """Pagination is what gives `remove_by_download_id` its 'removed'
    return when the match is on page 2; without it the original code
    returned 'not_found' for the same scenario."""
    # page_size is hardcoded to 1000 in get_queue.  page1 must hit
    # 1000 records to NOT short-circuit on "fewer than page_size".
    page1 = {
        "records": [{"id": i, "downloadId": f"hash{i}"} for i in range(1000)],
        "totalRecords": 1001,
    }
    page2 = {
        "records": [{"id": 1001, "downloadId": "TARGET", "title": "Movie on page 2"}],
        "totalRecords": 1001,
    }
    with patch("requests.get") as mock_get, patch("requests.delete") as mock_del:
        responses = []
        for d in (page1, page2):
            r = MagicMock(status_code=200, json=lambda d=d: d)
            r.raise_for_status = lambda: None
            responses.append(r)
        mock_get.side_effect = responses
        mock_del.return_value = MagicMock(status_code=200)
        mock_del.return_value.raise_for_status = lambda: None
        c = ArrClient("http://radarr:7878", "k")
        assert c.remove_by_download_id("target") == "removed"


def test_arr_format_score_violations_all_compliant():
    """Codex round-15 #1 + AUDIT A7: AV1 + 12-bit HEVC must score
    -10000 in every quality profile that carries those formats.
    Compliant config returns empty violations list."""
    profiles = [
        {"name": "Shield Prioritized", "id": 1, "formatItems": [
            {"name": "AV1", "score": -10000},
            {"name": "12-bit", "score": -10000},
            {"name": "x265", "score": 0},
        ]},
    ]
    with patch("requests.get") as mock_get:
        r = MagicMock(status_code=200, json=lambda: profiles)
        r.raise_for_status = lambda: None
        mock_get.return_value = r
        c = ArrClient("http://radarr:7878", "k")
        vios = c.format_score_violations({"AV1": -10000, "12-bit": -10000})
    assert vios == []


def test_arr_format_score_violations_flags_wrong_score():
    """If AV1 scores 0 (or anything != -10000), it's flagged with
    profile/format/actual-score tuples."""
    profiles = [
        {"name": "Shield Prioritized", "id": 1, "formatItems": [
            {"name": "AV1", "score": 0},          # wrong
            {"name": "12-bit", "score": -10000},  # correct
        ]},
        {"name": "Default", "id": 2, "formatItems": [
            {"name": "AV1", "score": 100},        # wrong
            {"name": "12-bit", "score": -5000},   # wrong
        ]},
    ]
    with patch("requests.get") as mock_get:
        r = MagicMock(status_code=200, json=lambda: profiles)
        r.raise_for_status = lambda: None
        mock_get.return_value = r
        c = ArrClient("http://radarr:7878", "k")
        vios = c.format_score_violations({"AV1": -10000, "12-bit": -10000})
    assert ("Shield Prioritized", "AV1", 0) in vios
    assert ("Default", "AV1", 100) in vios
    assert ("Default", "12-bit", -5000) in vios
    assert len(vios) == 3


def test_arr_format_score_violations_skips_profiles_without_format():
    """Backward-compat (no profile filter): a profile that doesn't
    carry the format at all is silently allowed.  Use the profile
    filter argument to enforce presence (see next test)."""
    profiles = [
        {"name": "WithFormats", "id": 1, "formatItems": [
            {"name": "AV1", "score": -10000},
        ]},
        {"name": "NoFormats", "id": 2, "formatItems": []},
    ]
    with patch("requests.get") as mock_get:
        r = MagicMock(status_code=200, json=lambda: profiles)
        r.raise_for_status = lambda: None
        mock_get.return_value = r
        c = ArrClient("http://radarr:7878", "k")
        assert c.format_score_violations({"AV1": -10000}) == []


def test_arr_format_score_violations_missing_format_on_target_profile_fails():
    """Codex round-16 #1: with `profile_name_substring`, a target
    profile that's MISSING a required format must FAIL — otherwise
    an operator deleting the AV1 custom format from Shield would
    silently remove the policy block."""
    profiles = [
        {"name": "Shield Prioritized", "id": 1, "formatItems": [
            {"name": "12-bit", "score": -10000},
            # NOTE: no AV1 format at all
        ]},
        # Non-Shield profile without AV1 must NOT fail (filter excludes it)
        {"name": "Test Profile", "id": 2, "formatItems": []},
    ]
    with patch("requests.get") as mock_get:
        r = MagicMock(status_code=200, json=lambda: profiles)
        r.raise_for_status = lambda: None
        mock_get.return_value = r
        c = ArrClient("http://radarr:7878", "k")
        vios = c.format_score_violations(
            {"AV1": -10000, "12-bit": -10000},
            profile_name_substring="Shield",
        )
    assert ("Shield Prioritized", "AV1", "MISSING") in vios
    # Non-shield profile must NOT be flagged (filter exempts it)
    non_shield = [v for v in vios if v[0] != "Shield Prioritized"]
    assert non_shield == [], f"non-Shield profile flagged: {non_shield}"


def test_arr_format_score_violations_filter_skips_non_target_wrong_scores():
    """REGRESSION codex round-17 #1: round-16's filter only applied
    to the missing-format check, not the wrong-score loop.  A
    non-Shield profile with AV1=0 would still fail.  Filter must
    apply to BOTH checks consistently — non-matching profiles are
    skipped entirely."""
    profiles = [
        {"name": "Shield Prioritized", "id": 1, "formatItems": [
            {"name": "AV1", "score": -10000},
            {"name": "12-bit", "score": -10000},
        ]},
        {"name": "Test Profile", "id": 2, "formatItems": [
            {"name": "AV1", "score": 0},       # would fail wrong-score loop
            {"name": "12-bit", "score": 100},  # would fail wrong-score loop
        ]},
    ]
    with patch("requests.get") as mock_get:
        r = MagicMock(status_code=200, json=lambda: profiles)
        r.raise_for_status = lambda: None
        mock_get.return_value = r
        c = ArrClient("http://radarr:7878", "k")
        vios = c.format_score_violations(
            {"AV1": -10000, "12-bit": -10000},
            profile_name_substring="Shield",
        )
    # Under the OLD buggy filter, Test Profile's wrong scores would
    # appear in violations.  Correct behavior: filter excludes it.
    assert vios == [], (
        f"non-Shield profile leaked into violations: {vios}"
    )


def test_arr_format_score_violations_target_filter_compliant():
    """With filter + all required formats present + correct scores
    → empty (compliant).  This is the live mediahost state."""
    profiles = [
        {"name": "Shield Prioritized", "id": 1, "formatItems": [
            {"name": "AV1", "score": -10000},
            {"name": "12-bit", "score": -10000},
        ]},
        {"name": "Test Profile", "id": 2, "formatItems": []},
    ]
    with patch("requests.get") as mock_get:
        r = MagicMock(status_code=200, json=lambda: profiles)
        r.raise_for_status = lambda: None
        mock_get.return_value = r
        c = ArrClient("http://radarr:7878", "k")
        vios = c.format_score_violations(
            {"AV1": -10000, "12-bit": -10000},
            profile_name_substring="Shield",
        )
    assert vios == []


def test_arr_format_score_violations_no_target_profile_is_flagged():
    """Codex round-18 #2: previously, `profile_name_substring="Shield"`
    with no matching profile silently returned []  — operator renaming
    or deleting Shield profiles would have removed the entire
    enforcement scope without any signal.  Now the empty-target-set
    case is flagged explicitly."""
    profiles = [
        {"name": "Default", "id": 1, "formatItems": [
            {"name": "AV1", "score": -10000},
        ]},
        {"name": "Test Profile", "id": 2, "formatItems": []},
        # NOTE: no profile contains "Shield" in its name
    ]
    with patch("requests.get") as mock_get:
        r = MagicMock(status_code=200, json=lambda: profiles)
        r.raise_for_status = lambda: None
        mock_get.return_value = r
        c = ArrClient("http://radarr:7878", "k")
        vios = c.format_score_violations(
            {"AV1": -10000, "12-bit": -10000},
            profile_name_substring="Shield",
        )
    assert ("<no matching profile>", "Shield", "NO_TARGET_PROFILE") in vios


def test_arr_format_score_violations_none_on_endpoint_failure():
    with patch("requests.get", side_effect=Exception("503")):
        c = ArrClient("http://radarr:7878", "k")
        assert c.format_score_violations({"AV1": -10000}) is None


def test_arr_unmonitored_no_file_count_zero():
    """REGRESSION: drift to monitored=False+hasFile=False makes a
    movie silently invisible to Radarr's search loop.  Bit us
    2026-05-31: ~91 movies had drifted into this state without any
    operator action and the library was missing a third of its
    expected catalog.  This count probe wires into healthcheck so
    the drift can't recur silently."""
    movies = [
        {"id": 1, "monitored": True,  "hasFile": True},   # OK
        {"id": 2, "monitored": True,  "hasFile": False},  # OK (will grab)
        {"id": 3, "monitored": False, "hasFile": True},   # OK (intentional skip-upgrade)
    ]
    with patch("requests.get") as mock_get:
        r = MagicMock(status_code=200, json=lambda: movies)
        r.raise_for_status = lambda: None
        mock_get.return_value = r
        c = ArrClient("http://radarr:7878", "k")
        assert c.unmonitored_no_file_count() == 0


def test_arr_unmonitored_no_file_count_counts_silent_drift():
    """The silent-drift case: monitored=False AND hasFile=False
    movies must be counted.  This is the only state that's
    operationally invisible — Radarr won't search, Jellyfin can't
    show it, no log records it."""
    movies = [
        {"id": 1, "monitored": True,  "hasFile": True},
        {"id": 2, "monitored": False, "hasFile": False},  # drift
        {"id": 3, "monitored": False, "hasFile": False},  # drift
        {"id": 4, "monitored": True,  "hasFile": False},  # OK (will grab)
        {"id": 5, "monitored": False, "hasFile": False},  # drift
    ]
    with patch("requests.get") as mock_get:
        r = MagicMock(status_code=200, json=lambda: movies)
        r.raise_for_status = lambda: None
        mock_get.return_value = r
        c = ArrClient("http://radarr:7878", "k")
        assert c.unmonitored_no_file_count() == 3


def test_arr_unmonitored_no_file_count_strict_false_check():
    """Tri-state defense: `monitored=None` / `hasFile=None` from a
    schema drift must NOT count toward the silent-drift bucket.
    Only explicit `False` for both fields qualifies — otherwise we
    over-alert on Arr API changes."""
    movies = [
        {"id": 1, "monitored": None, "hasFile": None},   # unknown, don't count
        {"id": 2, "monitored": 0,    "hasFile": 0},      # truthy-False but not is-False
        {"id": 3, "monitored": False, "hasFile": False},  # the real thing
    ]
    with patch("requests.get") as mock_get:
        r = MagicMock(status_code=200, json=lambda: movies)
        r.raise_for_status = lambda: None
        mock_get.return_value = r
        c = ArrClient("http://radarr:7878", "k")
        assert c.unmonitored_no_file_count() == 1


def test_arr_unmonitored_no_file_count_none_on_endpoint_failure():
    """Endpoint unreachable → None (caller surfaces as WARN,
    not a false-zero PASS that masks the actual problem)."""
    with patch("requests.get", side_effect=Exception("503")):
        c = ArrClient("http://radarr:7878", "k")
        assert c.unmonitored_no_file_count() is None


def test_arr_unmonitored_no_file_count_none_on_bad_shape():
    """Non-list response (e.g. error envelope) → None, same as
    endpoint failure."""
    with patch("requests.get") as mock_get:
        r = MagicMock(status_code=200, json=lambda: {"error": "..."})
        r.raise_for_status = lambda: None
        mock_get.return_value = r
        c = ArrClient("http://radarr:7878", "k")
        assert c.unmonitored_no_file_count() is None


def test_arr_unmonitored_no_file_count_none_on_non_dict_entries():
    """REGRESSION codex security review #3: list-shape was validated
    but per-element shape wasn't.  A list containing a string / null /
    number would have raised AttributeError at `m.get(...)` instead
    of returning None like other malformed-shape paths.  The probe's
    whole point is robust drift detection — must never crash on weird
    API responses.  Returns None so the healthcheck surfaces WARN
    rather than a confusing traceback."""
    bad_shapes = [
        [None, {"id": 1, "monitored": False, "hasFile": False}],
        ["just-a-string"],
        [42, {"id": 1, "monitored": False, "hasFile": False}],
        [{"id": 1, "monitored": False, "hasFile": False}, None],
    ]
    for shape in bad_shapes:
        with patch("requests.get") as mock_get:
            r = MagicMock(status_code=200, json=lambda s=shape: s)
            r.raise_for_status = lambda: None
            mock_get.return_value = r
            c = ArrClient("http://radarr:7878", "k")
            assert c.unmonitored_no_file_count() is None, (
                f"shape {shape!r} should map to None"
            )


def test_arr_hardlinks_enabled_true():
    """Codex round-9 #2 + AUDIT A7: hardlinks enforcement is documented
    policy.  The convenience helper returns True when copyUsingHardlinks
    is set."""
    cfg = {"copyUsingHardlinks": True, "enableMediaInfo": True}
    with patch("requests.get") as mock_get:
        r = MagicMock(status_code=200, json=lambda: cfg)
        r.raise_for_status = lambda: None
        mock_get.return_value = r
        c = ArrClient("http://radarr:7878", "k")
        assert c.hardlinks_enabled() is True


def test_arr_hardlinks_enabled_false_when_disabled():
    cfg = {"copyUsingHardlinks": False}
    with patch("requests.get") as mock_get:
        r = MagicMock(status_code=200, json=lambda: cfg)
        r.raise_for_status = lambda: None
        mock_get.return_value = r
        c = ArrClient("http://radarr:7878", "k")
        assert c.hardlinks_enabled() is False


def test_arr_hardlinks_enabled_none_on_endpoint_failure():
    """Tri-state: None on endpoint failure lets the caller distinguish
    'policy violated' from 'couldn't check' (a healthcheck WARN vs
    FAIL distinction)."""
    with patch("requests.get", side_effect=Exception("503")):
        c = ArrClient("http://radarr:7878", "k")
        assert c.hardlinks_enabled() is None


def test_arr_hardlinks_enabled_none_when_field_missing():
    """Codex round-11 #5: missing field must be None (couldn't
    verify), not False (policy violation).  An Arr schema change
    that drops `copyUsingHardlinks` would otherwise cause hourly
    healthcheck noise / pager fires."""
    cfg = {"enableMediaInfo": True}  # NOTE: no copyUsingHardlinks
    with patch("requests.get") as mock_get:
        r = MagicMock(status_code=200, json=lambda: cfg)
        r.raise_for_status = lambda: None
        mock_get.return_value = r
        c = ArrClient("http://radarr:7878", "k")
        assert c.hardlinks_enabled() is None


def test_arr_hardlinks_enabled_none_when_field_non_boolean():
    """Non-boolean values (string, int, null) → None too.  Defends
    against API drift returning truthy non-bool sentinel."""
    for v in ("true", 1, None, "yes", 0):
        cfg = {"copyUsingHardlinks": v}
        with patch("requests.get") as mock_get:
            r = MagicMock(status_code=200, json=lambda c=cfg: c)
            r.raise_for_status = lambda: None
            mock_get.return_value = r
            c = ArrClient("http://radarr:7878", "k")
            assert c.hardlinks_enabled() is None, f"value {v!r} should be None"


def test_arr_remove_by_download_id_delete_failed():
    """Codex round-module-split #3: matched-but-delete-failed must be
    distinguishable so nuke_stalled doesn't fall through to a direct
    qBit delete (next sweep would re-grab the same release)."""
    fake_records = [{"id": 7, "downloadId": "XYZ", "title": "Movie X"}]
    with patch("requests.get") as mock_get, patch("requests.delete") as mock_del:
        mock_get.return_value = MagicMock(
            status_code=200, json=lambda: {"records": fake_records},
        )
        mock_get.return_value.raise_for_status = lambda: None
        # The DELETE raises (e.g. 503 from Radarr)
        mock_del.side_effect = Exception("503 Service Unavailable")
        c = ArrClient("http://radarr:7878", "k")
        assert c.remove_by_download_id("xyz") == "delete_failed"


# --- QBitClient ---

def test_qbit_login_skipped_when_user_empty():
    c = QBitClient("http://qbit:8090", "", "")
    assert c.login() is True  # bypass mode


def test_qbit_login_success():
    with patch.object(QBitClient, "__init__", lambda self, *a, **k: None):
        c = QBitClient("u", "p")
        c.base_url = "http://qbit:8090"
        c.username = "testuser"
        c.password = "secret"
        c.timeout = 15
        c._logged_in = False
        c.session = MagicMock()
        c.session.post.return_value = MagicMock(status_code=200, text="Ok.")
        assert c.login() is True


def test_qbit_login_failure():
    with patch.object(QBitClient, "__init__", lambda self, *a, **k: None):
        c = QBitClient("u", "p")
        c.base_url = "http://qbit:8090"
        c.username = "testuser"
        c.password = "wrong"
        c.timeout = 15
        c._logged_in = False
        c.session = MagicMock()
        c.session.post.return_value = MagicMock(status_code=403, text="Fails.")
        assert c.login() is False


def test_qbit_login_response_bypass_returns_ok_without_request():
    c = QBitClient("http://qbit:8090", "", "")
    c.session = MagicMock()
    assert c.login_response() == "Ok."          # bypass mode: no HTTP call
    c.session.post.assert_not_called()


def test_qbit_login_response_returns_raw_text():
    c = QBitClient("http://qbit:8090", "u", "p")
    c.session = MagicMock()
    c.session.post.return_value = MagicMock(text="Fails.")
    assert c.login_response() == "Fails."


def test_qbit_login_response_empty_on_connection_error():
    c = QBitClient("http://qbit:8090", "u", "p")
    c.session = MagicMock()
    c.session.post.side_effect = Exception("refused")
    assert c.login_response() == ""


def test_qbit_reachable_status_returns_code():
    c = QBitClient("http://qbit:8090", "", "")
    c.session = MagicMock()
    c.session.get.return_value = MagicMock(status_code=200)
    assert c.reachable_status() == "200"


def test_qbit_reachable_status_000_on_connection_error():
    c = QBitClient("http://qbit:8090", "", "")
    c.session = MagicMock()
    c.session.get.side_effect = Exception("refused")
    assert c.reachable_status() == "000"


# --- BazarrClient ---

def test_bazarr_system_status_returns_none_on_failure():
    with patch("requests.get", side_effect=Exception("oops")):
        c = BazarrClient("http://bazarr:6767", "k")
        assert c.system_status() is None


def test_bazarr_reachable_status_returns_code():
    with patch("requests.get", return_value=MagicMock(status_code=200)):
        assert BazarrClient("http://bazarr:6767", "k").reachable_status() == "200"


def test_bazarr_reachable_status_000_on_connection_error():
    with patch("requests.get", side_effect=Exception("oops")):
        assert BazarrClient("http://bazarr:6767", "k").reachable_status() == "000"


def test_bazarr_unprofiled_count_both_zero():
    with patch("requests.get") as mock_get:
        resp = MagicMock(status_code=200, json=lambda: {"data": []})
        resp.raise_for_status = lambda: None
        mock_get.return_value = resp
        c = BazarrClient("http://bazarr:6767", "k")
        assert c.unprofiled_count() == (0, 0)


def test_bazarr_unprofiled_count_some_missing():
    fake_movies = {"data": [
        {"profileId": 1}, {"profileId": None}, {"profileId": None},
    ]}
    fake_series = {"data": [{"profileId": 1}, {"profileId": None}]}
    with patch("requests.get") as mock_get:
        responses = []
        for d in (fake_movies, fake_series):
            r = MagicMock(status_code=200, json=lambda d=d: d)
            r.raise_for_status = lambda: None
            responses.append(r)
        mock_get.side_effect = responses
        c = BazarrClient("http://bazarr:6767", "k")
        m, s = c.unprofiled_count()
        assert m == 2
        assert s == 1


def test_bazarr_unprofiled_count_none_on_failure():
    with patch("requests.get", side_effect=Exception("nope")):
        c = BazarrClient("http://bazarr:6767", "k")
        assert c.unprofiled_count() == (None, None)


def test_bazarr_wrong_profile_count_catches_non_default():
    """Codex round-13 #2: `unprofiled_count` only catches None;
    a wrong-but-non-None profile (e.g. 5=Spanish on an item that
    should be 1=English) passes the prior check.  `wrong_profile_
    count` flags those."""
    fake_movies = {"data": [
        {"profileId": 1},     # correct
        {"profileId": 5},     # wrong  (Spanish)
        {"profileId": 3},     # wrong  (French)
        {"profileId": None},  # excluded (covered by unprofiled_count)
        {"profileId": 1},     # correct
    ]}
    fake_series = {"data": [
        {"profileId": 1},
        {"profileId": 7},     # wrong
    ]}
    with patch("requests.get") as mock_get:
        responses = []
        for d in (fake_movies, fake_series):
            r = MagicMock(status_code=200, json=lambda d=d: d)
            r.raise_for_status = lambda: None
            responses.append(r)
        mock_get.side_effect = responses
        c = BazarrClient("http://bazarr:6767", "k")
        m, s = c.wrong_profile_count(expected=1)
        assert m == 2  # the two non-1, non-None
        assert s == 1


def test_bazarr_wrong_profile_count_excludes_none():
    """profileId=None must NOT be counted — that path is
    already covered by `unprofiled_count` and would double-flag."""
    fake_movies = {"data": [{"profileId": None}, {"profileId": None}]}
    fake_series = {"data": []}
    with patch("requests.get") as mock_get:
        responses = []
        for d in (fake_movies, fake_series):
            r = MagicMock(status_code=200, json=lambda d=d: d)
            r.raise_for_status = lambda: None
            responses.append(r)
        mock_get.side_effect = responses
        c = BazarrClient("http://bazarr:6767", "k")
        assert c.wrong_profile_count(expected=1) == (0, 0)


def test_bazarr_wrong_profile_count_none_on_failure():
    with patch("requests.get", side_effect=Exception("503")):
        c = BazarrClient("http://bazarr:6767", "k")
        assert c.wrong_profile_count(expected=1) == (None, None)


def test_bazarr_assign_movie_profile_success():
    """Codex round-4 module-split #3: bazarr-profile-audit.py was
    calling Bazarr via raw urllib instead of going through the
    client.  The client's POST writer must form-encode the body and
    surface success/failure."""
    with patch("requests.post") as mock_post:
        resp = MagicMock(status_code=200)
        resp.raise_for_status = lambda: None
        mock_post.return_value = resp
        c = BazarrClient("http://bazarr:6767", "k")
        assert c.assign_movie_profile(42, 1) is True
        # Verify form-encoded body (not JSON)
        assert mock_post.call_args.kwargs["data"] == {"radarrid": 42, "profileid": 1}


def test_bazarr_assign_series_profile_failure_returns_false():
    with patch("requests.post", side_effect=Exception("bazarr 503")):
        c = BazarrClient("http://bazarr:6767", "k")
        assert c.assign_series_profile(7, 1) is False


def test_bazarr_trigger_task_success():
    with patch("requests.post") as mock_post:
        resp = MagicMock(status_code=204)
        resp.raise_for_status = lambda: None
        mock_post.return_value = resp
        c = BazarrClient("http://bazarr:6767", "k")
        assert c.trigger_task("wanted_search_missing_subtitles_movies") is True
        # Verify taskid in params, not in URL path
        assert mock_post.call_args.kwargs["params"] == {
            "taskid": "wanted_search_missing_subtitles_movies"
        }


# --- Telegram (codex round-5 #6: client had zero coverage) ---

def test_telegram_send_success():
    with patch("urllib.request.urlopen") as mock_open:
        cm = MagicMock()
        cm.__enter__ = lambda self: MagicMock(status=200)
        cm.__exit__ = lambda *a: False
        mock_open.return_value = cm
        assert telegram_send("123:ABC", "test-chat-id", "hello") is True
        # Verify the URL embeds the token (Bot API requires it in path)
        # but no query string carries the message text.
        req = mock_open.call_args.args[0]
        assert "/bot123:ABC/sendMessage" in req.full_url
        assert "?text=" not in req.full_url
        assert req.get_method() == "POST"


def test_telegram_send_returns_false_on_network_error():
    with patch("urllib.request.urlopen", side_effect=Exception("dns fail")):
        assert telegram_send("123:ABC", "test-chat-id", "hi") is False


def test_telegram_send_returns_false_on_non_200():
    with patch("urllib.request.urlopen") as mock_open:
        cm = MagicMock()
        cm.__enter__ = lambda self: MagicMock(status=400)
        cm.__exit__ = lambda *a: False
        mock_open.return_value = cm
        assert telegram_send("123:ABC", "test-chat-id", "hi") is False


# --- JellyfinClient (urllib-based; mock urllib.request.urlopen) ---

def _jf_resp(status, body=b""):
    """A urlopen() context-manager mock yielding a response with .status/.read."""
    r = MagicMock()
    r.status = status
    r.read = lambda: body
    cm = MagicMock()
    cm.__enter__ = lambda self: r
    cm.__exit__ = lambda *a: False
    return cm


def _jf_http_error(code, body=b""):
    return urllib.error.HTTPError("http://jf:8096/x", code, "err", {}, io.BytesIO(body))


def test_jellyfin_request_success():
    with patch("urllib.request.urlopen", return_value=_jf_resp(200, b"ok")):
        status, body = JellyfinClient("http://jf:8096").request("/System/Info", "k")
    assert status == 200 and body == b"ok"


def test_jellyfin_request_http_error_returns_status_and_body():
    # Non-2xx is returned (not raised) so the caller can branch on the code.
    with patch("urllib.request.urlopen", side_effect=_jf_http_error(404, b"nf")):
        status, body = JellyfinClient("http://jf:8096").request("/x", "k")
    assert status == 404 and body == b"nf"


def test_jellyfin_request_transport_raises_networkerror():
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        with pytest.raises(NetworkError):
            JellyfinClient("http://jf:8096").request("/x", "k")


def test_jellyfin_request_strips_trailing_slash_on_base():
    assert JellyfinClient("http://jf:8096/").base == "http://jf:8096"


def test_jellyfin_list_keys_items_envelope():
    payload = json.dumps({"Items": [{"AppName": "a", "AccessToken": "tok1"}]}).encode()
    with patch("urllib.request.urlopen", return_value=_jf_resp(200, payload)):
        keys = JellyfinClient("http://jf:8096").list_keys("k")
    assert keys == [{"AppName": "a", "AccessToken": "tok1"}]


def test_jellyfin_list_keys_bare_list_envelope():
    payload = json.dumps([{"AppName": "a", "AccessToken": "tok1"}]).encode()
    with patch("urllib.request.urlopen", return_value=_jf_resp(200, payload)):
        assert len(JellyfinClient("http://jf:8096").list_keys("k")) == 1


def test_jellyfin_list_keys_dict_without_items_raises():
    payload = json.dumps({"error": "masked"}).encode()
    with patch("urllib.request.urlopen", return_value=_jf_resp(200, payload)):
        with pytest.raises(RuntimeError):
            JellyfinClient("http://jf:8096").list_keys("k")


def test_jellyfin_list_keys_invalid_accesstoken_raises():
    payload = json.dumps({"Items": [{"AppName": "a", "AccessToken": None}]}).encode()
    with patch("urllib.request.urlopen", return_value=_jf_resp(200, payload)):
        with pytest.raises(RuntimeError):
            JellyfinClient("http://jf:8096").list_keys("k")


def test_jellyfin_list_keys_invalid_appname_raises():
    payload = json.dumps({"Items": [{"AppName": "", "AccessToken": "tok"}]}).encode()
    with patch("urllib.request.urlopen", return_value=_jf_resp(200, payload)):
        with pytest.raises(RuntimeError):
            JellyfinClient("http://jf:8096").list_keys("k")


def test_jellyfin_list_keys_non200_raises():
    with patch("urllib.request.urlopen", side_effect=_jf_http_error(401, b"unauth")):
        with pytest.raises(RuntimeError):
            JellyfinClient("http://jf:8096").list_keys("k")


def test_jellyfin_create_key_non2xx_raises():
    with patch("urllib.request.urlopen", side_effect=_jf_http_error(500, b"")):
        with pytest.raises(RuntimeError):
            JellyfinClient("http://jf:8096").create_key("k", "name")


def test_jellyfin_create_key_204_ok():
    with patch("urllib.request.urlopen", return_value=_jf_resp(204, b"")):
        assert JellyfinClient("http://jf:8096").create_key("k", "name") is None
