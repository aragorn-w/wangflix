"""Tests for media_stack.arrlang, the Radarr/Sonarr original-language signal.

No real network: ArrClient is mocked at the class level.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack import arrlang, paths


@pytest.fixture(autouse=True)
def _clear_cache():
    """The per-process cache must not leak state between tests."""
    arrlang._CACHE.clear()
    yield
    arrlang._CACHE.clear()


@pytest.fixture
def arr(tmp_path, monkeypatch):
    """MEDIA_ROOT in tmp_path, both Arr keys set, no root overrides, no `.env`."""
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setattr(paths, "MEDIA_STACK_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    monkeypatch.setenv("SONARR_API_KEY", "key456")
    monkeypatch.delenv("ARR_MOVIES_ROOT", raising=False)
    monkeypatch.delenv("ARR_TV_ROOT", raising=False)
    return tmp_path


def _movie_file(tmp_path: Path, folder: str) -> Path:
    p = tmp_path / "movies" / folder / "f.mkv"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    return p


def _series_file(tmp_path: Path, folder: str) -> Path:
    p = tmp_path / "tv" / folder / "Season 01" / "ep.mkv"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    return p


def _ask(path, kind, records, fn=arrlang.original_language_for):
    """`fn(path)` with ArrClient mocked so `kind` ("movies" or "series") returns `records`."""
    client = MagicMock()
    getattr(client, kind).return_value = records
    with patch.object(arrlang, "ArrClient", return_value=client):
        return fn(path)


def _rec(path, name=None):
    return {"path": path, "originalLanguage": {"name": name}} if name else {"path": path}


@pytest.mark.parametrize("kind, folder, records, env, dotenv, expected", [
    pytest.param("movies", "Godzilla Minus One (2023)",
                 [_rec("/movies/Godzilla Minus One (2023)", "Japanese")], {}, None, "jpn",
                 id="movie-japanese"),
    pytest.param("series", "Squid Game", [_rec("/tv/Squid Game", "Korean")], {}, None, "kor",
                 id="series-korean"),
    pytest.param("movies", "Some Hollywood Film (2020)",
                 [_rec("/movies/Some Hollywood Film (2020)", "English")], {}, None, "eng",
                 id="english-name"),
    pytest.param("movies", "Amelie (2001)", [_rec("/movies/Amelie (2001)", "French")], {}, None, None,
                 id="unmapped-language-is-never-a-made-up-code"),
    pytest.param("movies", "Some Film (2020)", None, {}, None, None,
                 id="client-failure-sentinel"),
    pytest.param("movies", "Not In Radarr (2020)",
                 [_rec("/movies/A Totally Different Movie (2019)", "English")], {}, None, None,
                 id="no-folder-match"),
    pytest.param("movies", "Weird Metadata (2020)",
                 [{"path": "/movies/Weird Metadata (2020)", "originalLanguage": "not-a-dict"}],
                 {}, None, None, id="malformed-original-language-field"),
    pytest.param("movies", "Shared", [_rec("/unrelated-library/Shared", "Japanese")], {}, None, None,
                 id="record-under-different-root-ignored"),
    pytest.param("movies", "Shared", [_rec("/data/movies/Shared", "Japanese")],
                 {"ARR_MOVIES_ROOT": "/data/movies"}, None, "jpn", id="record-under-configured-root-used"),
    pytest.param("movies", "Shared", [_rec("/data/movies/Shared/", "Japanese")],
                 {"ARR_MOVIES_ROOT": "/data/movies/"}, None, "jpn",
                 id="trailing-slashes-stripped-on-both-sides"),
    pytest.param("movies", "Shared",
                 [_rec("/movies/Shared", "Japanese"), _rec("/movies/Shared", "English")], {}, None, None,
                 id="two-records-same-basename-ambiguous"),
    pytest.param("movies", "Shared",
                 [_rec("/movies/Shared", "Japanese"), _rec("/unrelated-library/Shared", "English")],
                 {}, None, "jpn", id="wrong-root-record-causes-no-false-ambiguity"),
    pytest.param("movies", "Shared", [_rec("/custom/movies/Shared", "English")], {},
                 "ARR_MOVIES_ROOT=/custom/movies\n", "eng", id="root-override-via-dotenv-alone"),
    pytest.param("movies", "Shared",
                 # the .env-only root also basename-matches but process env must NOT fall back to it
                 [_rec("/env-wins/movies/Shared", "English"), _rec("/dotenv-only/movies/Shared", "Japanese")],
                 {"ARR_MOVIES_ROOT": "/env-wins/movies"}, "ARR_MOVIES_ROOT=/dotenv-only/movies\n", "eng",
                 id="process-env-root-beats-dotenv"),
    pytest.param("series", "Squid Game", [_rec("/data/tv/Squid Game", "Korean")],
                 {"ARR_TV_ROOT": "/data/tv"}, None, "kor", id="series-root-override-via-env"),
    pytest.param("series", "Squid Game", [_rec("/unrelated-library/Squid Game", "Korean")], {}, None, None,
                 id="series-under-different-root-ignored"),
])
def test_original_language_for_maps_the_matching_record(
        arr, monkeypatch, kind, folder, records, env, dotenv, expected):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if dotenv is not None:
        (arr / "stack").mkdir()
        (arr / "stack" / ".env").write_text(dotenv)
        monkeypatch.setattr(paths, "MEDIA_STACK_ROOT", arr / "stack")
    make = _movie_file if kind == "movies" else _series_file
    assert _ask(make(arr, folder), kind, records) == expected


def test_returns_none_on_missing_api_key(arr, monkeypatch):
    monkeypatch.delenv("RADARR_API_KEY")
    movie_file = _movie_file(arr, "Some Film (2020)")

    fake_client_cls = MagicMock()
    with patch.object(arrlang, "ArrClient", fake_client_cls):
        result = arrlang.original_language_for(movie_file)
    assert result is None
    fake_client_cls.assert_not_called()  # no key -> never even construct the client


@pytest.mark.parametrize("fn, expected, relpath, media_root", [
    pytest.param(arrlang.original_language_for, None, "music/Some Album/track.mp3", "",
                 id="for-non-media-path"),
    pytest.param(arrlang.original_language_for, None, "elsewhere/movies/Some Film/f.mkv", "media",
                 id="for-path-outside-media-root"),
    pytest.param(arrlang.original_language_lookup, (None, "unknown"), "music/x.mkv", "",
                 id="lookup-outside-movies-and-tv"),
])
def test_paths_arr_cannot_own_never_reach_the_client(arr, monkeypatch, fn, expected, relpath, media_root):
    monkeypatch.setattr(paths, "MEDIA_ROOT", arr / media_root)
    path = arr / relpath
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")

    fake_client_cls = MagicMock()
    with patch.object(arrlang, "ArrClient", fake_client_cls):
        assert fn(path) == expected
    fake_client_cls.assert_not_called()


def test_ambiguous_basename_does_not_shadow_unambiguous_ones(arr):
    """Only the colliding basename resolves to None."""
    shared_file = _movie_file(arr, "Shared")
    other_file = _movie_file(arr, "Unique Movie (2021)")

    fake_client = MagicMock()
    fake_client.movies.return_value = [
        _rec("/movies/Shared", "Japanese"),
        _rec("/movies/Shared", "English"),
        _rec("/movies/Unique Movie (2021)", "Korean"),
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        shared_result = arrlang.original_language_for(shared_file)
        other_result = arrlang.original_language_for(other_file)
    assert shared_result is None
    assert other_result == "kor"


def test_caches_second_call_does_not_refetch(arr):
    movie_a = _movie_file(arr, "Movie A (2020)")
    movie_b = _movie_file(arr, "Movie B (2021)")

    fake_client = MagicMock()
    fake_client.movies.return_value = [
        _rec("/movies/Movie A (2020)", "English"),
        _rec("/movies/Movie B (2021)", "Japanese"),
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        first = arrlang.original_language_for(movie_a)
        second = arrlang.original_language_for(movie_b)
    assert first == "eng"
    assert second == "jpn"
    fake_client.movies.assert_called_once()  # one fetch for both lookups


def test_cache_is_independent_per_kind(arr):
    """Movies and series are cached under separate keys."""
    movie_file = _movie_file(arr, "Some Movie (2020)")
    series_file = _series_file(arr, "Some Series")

    radarr_client = MagicMock()
    radarr_client.movies.return_value = [_rec("/movies/Some Movie (2020)", "English")]
    sonarr_client = MagicMock()
    sonarr_client.series.return_value = [_rec("/tv/Some Series", "Japanese")]

    def _make_client(url, key, **kwargs):
        return radarr_client if url == paths.RADARR_URL else sonarr_client

    with patch.object(arrlang, "ArrClient", side_effect=_make_client):
        movie_result = arrlang.original_language_for(movie_file)
        series_result = arrlang.original_language_for(series_file)
    assert movie_result == "eng"
    assert series_result == "jpn"
    radarr_client.movies.assert_called_once()
    sonarr_client.series.assert_called_once()


# --- original_language_lookup: "Arr could not be asked" vs "no usable answer" ---
# (audio-lang-check.py must not cache a result computed while Arr was down.)

def test_lookup_known_for_a_mapped_language(arr):
    movie_file = _movie_file(arr, "Heat (1995)")
    records = [_rec("/movies/Heat (1995)", "English")]
    assert _ask(movie_file, "movies", records, arrlang.original_language_lookup) == ("eng", "known")


def test_lookup_unavailable_when_the_api_key_is_missing(arr, monkeypatch):
    monkeypatch.setattr(paths, "load_env_file", lambda p: {})
    monkeypatch.delenv("RADARR_API_KEY")
    assert arrlang.original_language_lookup(_movie_file(arr, "Heat (1995)")) == (None, "unavailable")


@pytest.mark.parametrize("failure", ["returns-none", "raises"])
@pytest.mark.parametrize("kind, make_file, folder", [
    ("movies", _movie_file, "Heat (1995)"),       # Radarr
    ("series", _series_file, "Squid Game"),       # Sonarr
], ids=["radarr-movie", "sonarr-series"])
def test_arr_failure_is_none_for_original_language_for_and_unavailable_for_lookup(
        arr, failure, kind, make_file, folder):
    """original_language_for never raises, even if ArrClient does."""
    movie_file = make_file(arr, folder)
    client = MagicMock()
    getattr(client, kind).return_value = None  # ArrClient: unreachable / bad reply
    kwargs = ({"return_value": client} if failure == "returns-none"
              else {"side_effect": RuntimeError("boom")})
    for fn, expected in ((arrlang.original_language_for, None),
                         (arrlang.original_language_lookup, (None, "unavailable"))):
        arrlang._CACHE.clear()
        with patch.object(arrlang, "ArrClient", **kwargs):
            assert fn(movie_file) == expected


@pytest.mark.parametrize("records", [
    [_rec("/movies/Elsewhere (2000)", "English")],                                  # no match
    [_rec("/movies/Heat (1995)", "French")],                                         # unmapped language
    [_rec("/movies/Heat (1995)")],                                                   # no language at all
    [_rec("/movies/Heat (1995)", "English"), _rec("/movies/Heat (1995)/", "Japanese")],  # ambiguous basename
], ids=["no-match", "unmapped-language", "no-language", "ambiguous-basename"])
def test_lookup_unknown_when_arr_answered_but_has_nothing_usable(arr, records):
    movie_file = _movie_file(arr, "Heat (1995)")
    assert _ask(movie_file, "movies", records, arrlang.original_language_lookup) == (None, "unknown")


def test_original_language_for_contract_is_unchanged_by_the_lookup(arr):
    """None for BOTH an outage and an unusable answer, a code when known."""
    movie_file = _movie_file(arr, "Heat (1995)")
    assert _ask(movie_file, "movies", [_rec("/movies/Heat (1995)", "Korean")]) == "kor"
    arrlang._CACHE.clear()
    with patch.object(arrlang, "ArrClient", side_effect=RuntimeError("down")):
        assert arrlang.original_language_for(movie_file) is None
