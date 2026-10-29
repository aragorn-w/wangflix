"""Tests for media_stack.clients — service wrappers (Arr, qBit, Bazarr)."""

import io
import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.clients.arr import ArrClient
from media_stack.clients.qbit import QBitClient
from media_stack.clients.bazarr import BazarrClient
from media_stack.clients.telegram import send as telegram_send
from media_stack.clients.jellyfin import JellyfinClient, NetworkError


def _resp(body=None, status=200):
    resp = MagicMock(status_code=status, json=lambda: body)
    resp.raise_for_status = lambda: None
    return resp


def _http(verb, body=None, status=200):
    """Patch requests.<verb> to return `body` as JSON, or to raise it when it is an Exception."""
    if isinstance(body, Exception):
        return patch(f"requests.{verb}", side_effect=body)
    return patch(f"requests.{verb}", return_value=_resp(body, status))


def _get_pages(*bodies):
    return patch("requests.get", side_effect=[_resp(b) for b in bodies])


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


# --- ArrClient ---

@pytest.mark.parametrize("cls, base", [(ArrClient, "http://radarr:7878"),
                                       (BazarrClient, "http://bazarr:6767")],
                         ids=["arr", "bazarr"])
@pytest.mark.parametrize("get_kw, expected", [
    ({"return_value": MagicMock(status_code=200)}, "200"),
    ({"return_value": MagicMock(status_code=503)}, "503"),
    ({"side_effect": Exception("refused")}, "000"),
], ids=["200", "503", "connection-error-is-000"])
def test_reachable_status(cls, base, get_kw, expected):
    with patch("requests.get", **get_kw):
        assert cls(base, "k").reachable_status() == expected


_LIST_CALLS = [
    ("movies", (), "http://radarr:7878", None),
    ("series", (), "http://sonarr:8989", None),
    ("episodes", (29,), "http://sonarr:8989", {"seriesId": 29}),
    ("episode_files", (29,), "http://sonarr:8989", {"seriesId": 29}),
]
_list_calls = pytest.mark.parametrize("method, args, base, params", _LIST_CALLS,
                                      ids=[c[0] for c in _LIST_CALLS])


# Each endpoint's own payload shape: the client must hand back exactly what the API sent, fields intact.
_LIST_PAYLOADS = {
    "movies": [{"id": 1, "path": "/movies/A (2020)", "movieFile": {"relativePath": "A.mkv"}}],
    "series": [{"id": 29, "path": "/tv/Rick and Morty", "title": "Rick and Morty"}],
    "episodes": [{"seasonNumber": 9, "episodeNumber": 1, "hasFile": True, "episodeFileId": 3846}],
    "episode_files": [{"id": 3846, "relativePath": "Season 9/E01 WEBDL-1080p.mkv"}],
}


@_list_calls
def test_arr_list_endpoint_returns_list(method, args, base, params):
    payload = _LIST_PAYLOADS[method]
    with _http("get", payload) as mock_get:
        result = getattr(ArrClient(base, "k"), method)(*args)
    assert result == payload
    if params is not None:
        assert mock_get.call_args.kwargs["params"] == params


@_list_calls
@pytest.mark.parametrize("body", [{"error": "x"}, Exception("down")],
                         ids=["non-list-shape", "failure"])
def test_arr_list_endpoint_none_on_bad_reply(method, args, base, params, body):
    with _http("get", body):
        assert getattr(ArrClient(base, "k"), method)(*args) is None


@pytest.mark.parametrize("method, arg, body", [
    ("rescan_movie", 42, {"name": "RescanMovie", "movieId": 42}),
    ("rescan_series", 29, {"name": "RescanSeries", "seriesId": 29}),
], ids=["movie", "series"])
def test_arr_rescan_posts_command_true_on_2xx_false_on_failure(method, arg, body):
    c = ArrClient("http://radarr:7878", "k")
    with _http("post", None, 201) as mock_post:
        assert getattr(c, method)(arg) is True
    assert mock_post.call_args.kwargs["json"] == body
    with _http("post", Exception("boom")):
        assert getattr(c, method)(arg) is False


def test_arr_run_command_returns_id_and_posts_body():
    with _http("post", {"id": 99}, 201) as mock_post:
        assert ArrClient("http://radarr:7878", "k").run_command("MoviesSearch", movieIds=[1, 2]) == 99
    assert mock_post.call_args.kwargs["json"] == {"name": "MoviesSearch", "movieIds": [1, 2]}
    assert mock_post.call_args.args[0] == "http://radarr:7878/api/v3/command"


@pytest.mark.parametrize("body", [{}, {"id": "7"}, {"id": True}, [], None, Exception("boom")],
                         ids=["no-id", "string-id", "bool-id", "list", "null", "request-fails"])
def test_arr_run_command_none_without_real_int_id(body):
    with _http("post", body, 201):
        assert ArrClient("http://radarr:7878", "k").run_command("MoviesSearch") is None


def test_arr_delete_episode_file_true_on_2xx():
    with _http("delete") as mock_delete:
        assert ArrClient("http://sonarr:8989", "k").delete_episode_file(3845) is True
    assert mock_delete.call_args.args[0] == "http://sonarr:8989/api/v3/episodefile/3845"


def _http_error_resp():
    resp = MagicMock(status_code=404)
    resp.raise_for_status = MagicMock(side_effect=requests.HTTPError("404 Client Error"))
    return resp


@pytest.mark.parametrize("delete_kw", [{"return_value": _http_error_resp()},
                                       {"side_effect": Exception("boom")}],
                         ids=["non-2xx", "request-fails"])
def test_arr_delete_episode_file_false_on_failure(delete_kw):
    with patch("requests.delete", **delete_kw):
        assert ArrClient("http://sonarr:8989", "k").delete_episode_file(3845) is False


@pytest.mark.parametrize("body, expected", [
    ({"records": [{"id": 1}, {"id": 2}]}, [{"id": 1}, {"id": 2}]),
    ({"page": 1, "totalRecords": 0, "records": []}, []),
], ids=["returns-records", "empty-queue-is-empty-list"])
def test_arr_get_queue_returns_records(body, expected):
    with _http("get", body):
        assert ArrClient("http://radarr:7878", "k").get_queue() == expected


@pytest.mark.parametrize("body", [
    {"totalRecords": 1}, {"records": None}, {"records": "x"}, [{"id": 1}],
    {"records": [None]}, {"records": [{"id": 1}, "x"]},
    {"totalRecords": 1, "records": []}, {"totalRecords": 5, "records": []},
], ids=["no-records-key", "null-records", "string-records", "bare-list", "null-record",
        "non-dict-record", "empty-page-short-of-total-1", "empty-page-short-of-total-5"])
def test_arr_get_queue_raises_on_malformed_or_incomplete_reply(body):
    """A 200 without a records list, or an empty page short of totalRecords, must raise, not read as empty."""
    with _http("get", body):
        with pytest.raises(ValueError):
            ArrClient("http://radarr:7878", "k").get_queue()


@pytest.mark.parametrize("total", ["1", 1.5, True, [], {}, None])
@pytest.mark.parametrize("records", [[], [{"id": 1}]])
def test_arr_get_queue_raises_on_non_integer_total(total, records):
    """A present but non-integer totalRecords ("1", null) must raise; only an ABSENT key falls back
    to the short-page rule."""
    with _http("get", {"totalRecords": total, "records": records}):
        with pytest.raises(ValueError):
            ArrClient("http://radarr:7878", "k").get_queue()


@pytest.mark.parametrize("page_sizes, total, calls", [
    ([1000, 500], 1500, 2),
    ([800, 400], 1200, 2),
    ([500], None, 1),
], ids=["walks-every-page", "short-page-with-known-total-keeps-going",
        "short-page-without-total-stops"])
def test_arr_get_queue_pagination(page_sizes, total, calls):
    """Otherwise torrents on page 2+ are missed (Arr can cap pageSize below 1000)."""
    next_id = iter(range(10_000))
    pages = []
    for n in page_sizes:
        body = {"records": [{"id": next(next_id)} for _ in range(n)]}
        if total is not None:
            body["totalRecords"] = total
        pages.append(body)
    with _get_pages(*pages) as mock_get:
        out = ArrClient("http://radarr:7878", "k").get_queue()
    assert len(out) == sum(page_sizes)
    assert mock_get.call_count == calls


_PAGE_1000 = {"records": [{"id": i, "downloadId": f"hash{i}"} for i in range(1000)],
              "totalRecords": 1001}
_PAGE_2 = {"records": [{"id": 1001, "downloadId": "TARGET", "title": "Movie on page 2"}],
           "totalRecords": 1001}


@pytest.mark.parametrize("pages, download_id, delete_url_frag", [
    ([{"records": [{"id": 1, "downloadId": "ABCDEF", "title": "Movie 1"},
                   {"id": 2, "downloadId": "123456", "title": "Movie 2"}]}], "abcdef", "/queue/1?"),
    ([_PAGE_1000, _PAGE_2], "target", "/queue/1001?"),
], ids=["case-insensitive-match", "match-on-page-2"])
def test_arr_remove_by_download_id_removed(pages, download_id, delete_url_frag):
    with _get_pages(*pages), _http("delete") as mock_del:
        assert ArrClient("http://radarr:7878", "k").remove_by_download_id(download_id) == "removed"
    assert delete_url_frag in mock_del.call_args.args[0]


@pytest.mark.parametrize("queue, delete, download_id, expected", [
    ({"records": []}, None, "nomatch", "not_found"),
    # a malformed or unreadable queue is queue_error, not not_found (which skips blocklisting)
    ({"totalRecords": 1}, None, "abc", "queue_error"),
    (Exception("network down"), None, "anything", "queue_error"),
    # matched-but-delete-failed: nuke_stalled skips the direct qBit delete (a re-grab otherwise)
    ({"records": [{"id": 7, "downloadId": "XYZ", "title": "Movie X"}]},
     Exception("503 Service Unavailable"), "xyz", "delete_failed"),
], ids=["no-match", "malformed-queue", "queue-lookup-fails", "delete-fails"])
def test_arr_remove_by_download_id_outcome(queue, delete, download_id, expected):
    with _http("get", queue), _http("delete", delete):
        assert ArrClient("http://radarr:7878", "k").remove_by_download_id(download_id) == expected


def _profiles(*named):
    return [{"name": name, "id": i, "formatItems": [{"name": f, "score": s} for f, s in items]}
            for i, (name, *items) in enumerate(named, 1)]


_WANTED = {"AV1": -10000, "12-bit": -10000}


@pytest.mark.parametrize("profiles, wanted, kwargs, expected", [
    pytest.param(_profiles(("Shield Prioritized", ("AV1", -10000), ("12-bit", -10000), ("x265", 0))),
                 _WANTED, {}, [], id="all-compliant"),
    pytest.param(_profiles(("Shield Prioritized", ("AV1", 0), ("12-bit", -10000)),
                           ("Default", ("AV1", 100), ("12-bit", -5000))),
                 _WANTED, {},
                 [("Shield Prioritized", "AV1", 0), ("Default", "AV1", 100),
                  ("Default", "12-bit", -5000)], id="flags-wrong-score"),
    pytest.param(_profiles(("WithFormats", ("AV1", -10000)), ("NoFormats",)),
                 {"AV1": -10000}, {}, [], id="no-filter-skips-profile-without-format"),
    pytest.param(_profiles(("Shield Prioritized", ("12-bit", -10000)), ("Test Profile",)),
                 _WANTED, {"profile_name_substring": "Shield"},
                 [("Shield Prioritized", "AV1", "MISSING")],
                 id="filter-missing-format-on-target-fails-and-spares-others"),
    pytest.param(_profiles(("Shield Prioritized", ("AV1", -10000), ("12-bit", -10000)),
                           ("Test Profile", ("AV1", 0), ("12-bit", 100))),
                 _WANTED, {"profile_name_substring": "Shield"}, [],
                 id="filter-skips-non-target-wrong-scores"),
    pytest.param(_profiles(("Shield Prioritized", ("AV1", -10000), ("12-bit", -10000)),
                           ("Test Profile",)),
                 _WANTED, {"profile_name_substring": "Shield"}, [], id="filter-compliant"),
    pytest.param(_profiles(("Default", ("AV1", -10000)), ("Test Profile",)),
                 _WANTED, {"profile_name_substring": "Shield"},
                 [("<no matching profile>", "Shield", "NO_TARGET_PROFILE")],
                 id="no-target-profile-is-flagged"),
    pytest.param(Exception("503"), {"AV1": -10000}, {}, None, id="none-on-endpoint-failure"),
])
def test_arr_format_score_violations(profiles, wanted, kwargs, expected):
    with _http("get", profiles):
        vios = ArrClient("http://radarr:7878", "k").format_score_violations(wanted, **kwargs)
    if expected is None:
        assert vios is None
    else:
        assert sorted(vios, key=repr) == sorted(expected, key=repr)


def _movie(monitored, has_file):
    return {"id": 1, "monitored": monitored, "hasFile": has_file}


@pytest.mark.parametrize("movies, expected", [
    # monitored+file, monitored+no file (will grab), unmonitored+file (intentional skip-upgrade)
    ([_movie(True, True), _movie(True, False), _movie(False, True)], 0),
    # only monitored=False with hasFile=False is operationally invisible
    ([_movie(True, True), _movie(False, False), _movie(False, False), _movie(True, False),
      _movie(False, False)], 3),
    # None (schema drift) and 0 (falsy but not `is False`) are not counted
    ([_movie(None, None), _movie(0, 0), _movie(False, False)], 1),
], ids=["no-drift", "counts-silent-drift", "strict-false-check"])
def test_arr_unmonitored_no_file_count(movies, expected):
    with _http("get", movies):
        assert ArrClient("http://radarr:7878", "k").unmonitored_no_file_count() == expected


@pytest.mark.parametrize("body", [
    Exception("503"),
    {"error": "..."},
    [None, _movie(False, False)],
    ["just-a-string"],
    [42, _movie(False, False)],
    [_movie(False, False), None],
], ids=["endpoint-failure", "non-list", "leading-null", "string-entry", "int-entry",
        "trailing-null"])
def test_arr_unmonitored_no_file_count_none_on_unusable_reply(body):
    """None (healthcheck WARN), not a false zero and not an AttributeError."""
    with _http("get", body):
        assert ArrClient("http://radarr:7878", "k").unmonitored_no_file_count() is None


@pytest.mark.parametrize("cfg, expected", [
    ({"copyUsingHardlinks": True, "enableMediaInfo": True}, True),
    ({"copyUsingHardlinks": False}, False),
    (Exception("503"), None),                  # couldn't check (WARN), not policy violated (FAIL)
    ({"enableMediaInfo": True}, None),         # missing field: couldn't verify, not False
    ({"copyUsingHardlinks": "true"}, None),
    ({"copyUsingHardlinks": 1}, None),
    ({"copyUsingHardlinks": None}, None),
    ({"copyUsingHardlinks": "yes"}, None),
    ({"copyUsingHardlinks": 0}, None),
], ids=["enabled", "disabled", "endpoint-failure", "field-missing", "string-true", "int-1",
        "null", "string-yes", "int-0"])
def test_arr_hardlinks_enabled(cfg, expected):
    with _http("get", cfg):
        assert ArrClient("http://radarr:7878", "k").hardlinks_enabled() is expected


# --- QBitClient ---

def test_qbit_login_skipped_when_user_empty():
    c = QBitClient("http://qbit:8090", "", "")
    assert c.login() is True  # bypass mode


@pytest.mark.parametrize("status, text, expected", [(200, "Ok.", True), (403, "Fails.", False)],
                         ids=["success", "failure"])
def test_qbit_login(status, text, expected):
    c = QBitClient("http://qbit:8090", "testuser", "secret")
    c.session = MagicMock()
    c.session.post.return_value = MagicMock(status_code=status, text=text)
    assert c.login() is expected


def test_qbit_login_response_bypass_returns_ok_without_request():
    c = QBitClient("http://qbit:8090", "", "")
    c.session = MagicMock()
    assert c.login_response() == "Ok."          # bypass mode: no HTTP call
    c.session.post.assert_not_called()


@pytest.mark.parametrize("post_kw, expected", [
    ({"return_value": MagicMock(text="Fails.")}, "Fails."),
    ({"side_effect": Exception("refused")}, ""),
], ids=["raw-text", "empty-on-connection-error"])
def test_qbit_login_response(post_kw, expected):
    c = QBitClient("http://qbit:8090", "u", "p")
    c.session = MagicMock()
    c.session.post.configure_mock(**post_kw)
    assert c.login_response() == expected


@pytest.mark.parametrize("get_kw, expected", [
    ({"return_value": MagicMock(status_code=200)}, "200"),
    ({"side_effect": Exception("refused")}, "000"),
], ids=["returns-code", "000-on-connection-error"])
def test_qbit_reachable_status(get_kw, expected):
    c = QBitClient("http://qbit:8090", "", "")
    c.session = MagicMock()
    c.session.get.configure_mock(**get_kw)
    assert c.reachable_status() == expected


# --- BazarrClient ---

def _profile_rows(profile_ids):
    return {"data": [{"profileId": p} for p in profile_ids]}


@pytest.mark.parametrize("method, kwargs, movies, series, expected", [
    ("unprofiled_count", {}, [], [], (0, 0)),
    ("unprofiled_count", {}, [1, None, None], [1, None], (2, 1)),
    ("unprofiled_count", {}, None, None, (None, None)),
    # a wrong-but-non-None profile (5=Spanish where 1=English is expected) is counted here;
    # None is excluded (unprofiled_count covers it)
    ("wrong_profile_count", {"expected": 1}, [1, 5, 3, None, 1], [1, 7], (2, 1)),
    ("wrong_profile_count", {"expected": 1}, [None, None], [], (0, 0)),
    ("wrong_profile_count", {"expected": 1}, None, None, (None, None)),
], ids=["unprofiled-both-zero", "unprofiled-some-missing", "unprofiled-none-on-failure",
        "wrong-profile-catches-non-default", "wrong-profile-excludes-none",
        "wrong-profile-none-on-failure"])
def test_bazarr_profile_counts(method, kwargs, movies, series, expected):
    if movies is None:
        replies = _http("get", Exception("503"))
    else:
        replies = _get_pages(_profile_rows(movies), _profile_rows(series))
    with replies:
        assert getattr(BazarrClient("http://bazarr:6767", "k"), method)(**kwargs) == expected


def test_bazarr_assign_movie_profile_success():
    """The client's POST writer form-encodes the body and surfaces success/failure."""
    with _http("post") as mock_post:
        assert BazarrClient("http://bazarr:6767", "k").assign_movie_profile(42, 1) is True
    assert mock_post.call_args.kwargs["data"] == {"radarrid": 42, "profileid": 1}


def test_bazarr_assign_series_profile_failure_returns_false():
    with _http("post", Exception("bazarr 503")):
        assert BazarrClient("http://bazarr:6767", "k").assign_series_profile(7, 1) is False


def test_bazarr_trigger_task_success():
    with _http("post", None, 204) as mock_post:
        assert BazarrClient("http://bazarr:6767", "k").trigger_task(
            "wanted_search_missing_subtitles_movies") is True
    assert mock_post.call_args.kwargs["params"] == {
        "taskid": "wanted_search_missing_subtitles_movies"
    }


# --- Telegram ---

def test_telegram_send_success():
    with patch("urllib.request.urlopen", return_value=_jf_resp(200)) as mock_open:
        assert telegram_send("123:ABC", "test-chat-id", "hello") is True
    # The URL embeds the token (Bot API requires it in the path) but no query string carries
    # the message text.
    req = mock_open.call_args.args[0]
    assert "/bot123:ABC/sendMessage" in req.full_url
    assert "?text=" not in req.full_url
    assert req.get_method() == "POST"


@pytest.mark.parametrize("urlopen_kw", [{"side_effect": Exception("dns fail")},
                                        {"return_value": _jf_resp(400)}],
                         ids=["network-error", "non-200"])
def test_telegram_send_returns_false_on_failure(urlopen_kw):
    with patch("urllib.request.urlopen", **urlopen_kw):
        assert telegram_send("123:ABC", "test-chat-id", "hi") is False


# --- JellyfinClient (urllib-based; mock urllib.request.urlopen) ---

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


@pytest.mark.parametrize("payload", [
    {"Items": [{"AppName": "a", "AccessToken": "tok1"}]},
    [{"AppName": "a", "AccessToken": "tok1"}],
], ids=["items-envelope", "bare-list"])
def test_jellyfin_list_keys_accepts_both_envelopes(payload):
    with patch("urllib.request.urlopen", return_value=_jf_resp(200, json.dumps(payload).encode())):
        keys = JellyfinClient("http://jf:8096").list_keys("k")
    assert keys == [{"AppName": "a", "AccessToken": "tok1"}]


@pytest.mark.parametrize("payload", [
    {"error": "masked"},
    {"Items": [{"AppName": "a", "AccessToken": None}]},
    {"Items": [{"AppName": "", "AccessToken": "tok"}]},
], ids=["dict-without-items", "invalid-accesstoken", "invalid-appname"])
def test_jellyfin_list_keys_malformed_raises(payload):
    with patch("urllib.request.urlopen", return_value=_jf_resp(200, json.dumps(payload).encode())):
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


# --- JellyfinClient.now_playing_sessions (sub-fallback.py's playback gate) --

def test_jellyfin_now_playing_sessions_filters_to_nowplayingitem():
    """Jellyfin's idle session omits NowPlayingItem entirely ("b", "c"); that is idle, not the
    present-but-null malformed case."""
    payload = json.dumps([
        {"Id": "a", "NowPlayingItem": {"Name": "Movie"}},
        {"Id": "b"},
        {"Id": "c"},
    ]).encode()
    with patch("urllib.request.urlopen", return_value=_jf_resp(200, payload)):
        sessions = JellyfinClient("http://jf:8096").now_playing_sessions("k")
    assert [s["Id"] for s in sessions] == ["a"]


def test_jellyfin_now_playing_sessions_confirmed_empty_when_nobody_playing():
    """A clean 200 with no NowPlayingItem anywhere is a confirmed [], distinct from None (unknown)."""
    payload = json.dumps([{"Id": "a"}, {"Id": "b"}]).encode()
    with patch("urllib.request.urlopen", return_value=_jf_resp(200, payload)):
        assert JellyfinClient("http://jf:8096").now_playing_sessions("k") == []


def _sessions_reply(obj):
    return {"return_value": _jf_resp(200, json.dumps(obj).encode())}


@pytest.mark.parametrize("urlopen_kw", [
    # NowPlayingItem present but {}/null/string is malformed, not idle (idle omits the key)
    _sessions_reply([{"Id": "a", "NowPlayingItem": {}}]),
    _sessions_reply([{"Id": "a", "NowPlayingItem": None}]),
    _sessions_reply([{"Id": "a", "NowPlayingItem": "Movie"}]),
    {"side_effect": urllib.error.URLError("refused")},
    {"side_effect": _jf_http_error(401, b"unauth")},
    {"return_value": _jf_resp(200, b"not json")},
    _sessions_reply({"error": "nope"}),
    # any non-dict element makes the whole response malformed; the bad entry is not skipped
    _sessions_reply(["not-a-session", {"Id": "a", "NowPlayingItem": {"Name": "x"}}]),
    _sessions_reply([None]),
], ids=["empty-dict-nowplayingitem", "null-nowplayingitem", "string-nowplayingitem",
        "network-error", "non-200", "malformed-body", "non-list-body", "non-dict-entry",
        "null-entry"])
def test_jellyfin_now_playing_sessions_fails_closed(urlopen_kw):
    with patch("urllib.request.urlopen", **urlopen_kw):
        assert JellyfinClient("http://jf:8096").now_playing_sessions("k") is None


@pytest.mark.parametrize("method, body", [("now_playing_sessions", b"[]"),
                                          ("list_keys", b'{"Items": []}')])
def test_jellyfin_keyed_calls_send_mediabrowser_authorization_header(method, body):
    """Jellyfin rejects the legacy X-Emby-Token header: send `Authorization: MediaBrowser Token` only."""
    captured: dict = {}

    def _capture(req, timeout=None):
        captured["req"] = req
        return _jf_resp(200, body)

    with patch("urllib.request.urlopen", side_effect=_capture):
        getattr(JellyfinClient("http://jf:8096"), method)("secret-key")

    req = captured["req"]
    assert req.get_header("Authorization") == 'MediaBrowser Token="secret-key"'
    assert req.get_header("X-Emby-Token") is None


# --- QBitClient preferences (forwarded-port reconciler) -----------------------
# setPreferences takes the settings as a JSON string in a `json=` form field.
# A plain form body is accepted with HTTP 200 and then IGNORED, so the encoding
# is load-bearing and sync-forwarded-port.py re-reads to confirm the write.

def test_qbit_preferences_returns_parsed_settings():
    c = QBitClient("http://qbit:8090", "", "")
    with patch.object(c.session, "get") as g:
        g.return_value = MagicMock(status_code=200,
                                   json=lambda: {"listen_port": 51413})
        g.return_value.raise_for_status = lambda: None
        assert c.preferences()["listen_port"] == 51413
        assert g.call_args[0][0].endswith("/api/v2/app/preferences")


def test_qbit_set_preferences_sends_json_encoded_form_field():
    c = QBitClient("http://qbit:8090", "", "")
    with patch.object(c.session, "post") as pst:
        pst.return_value = MagicMock(status_code=200)
        pst.return_value.raise_for_status = lambda: None
        c.set_preferences({"listen_port": 12345})
        assert pst.call_args[0][0].endswith("/api/v2/app/setPreferences")
        body = pst.call_args.kwargs["data"]
        assert set(body) == {"json"}, "settings must go in a single json field"
        assert json.loads(body["json"]) == {"listen_port": 12345}


def test_qbit_set_preferences_raises_on_http_error():
    c = QBitClient("http://qbit:8090", "", "")
    with patch.object(c.session, "post") as pst:
        pst.return_value = MagicMock(status_code=403)
        pst.return_value.raise_for_status = MagicMock(side_effect=RuntimeError("403"))
        with pytest.raises(RuntimeError):
            c.set_preferences({"listen_port": 1})
