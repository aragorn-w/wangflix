"""Tests for media_stack.arrlang — the Radarr/Sonarr original-language
signal used by get_audio_lang_pref to fix DEFECT 2 (a `default`-flagged
dub being mistaken for original-language evidence).

No real network: ArrClient itself is mocked at the class level so these
tests never call `requests.*`.
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


def test_movie_path_maps_to_japanese(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    movie_file = _movie_file(tmp_path, "Godzilla Minus One (2023)")

    fake_client = MagicMock()
    fake_client.movies.return_value = [
        {"path": "/movies/Godzilla Minus One (2023)",
         "originalLanguage": {"id": 1, "name": "Japanese"}},
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        result = arrlang.original_language_for(movie_file)
    assert result == "jpn"


def test_series_path_maps_to_korean(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("SONARR_API_KEY", "key456")
    series_file = _series_file(tmp_path, "Squid Game")

    fake_client = MagicMock()
    fake_client.series.return_value = [
        {"path": "/tv/Squid Game",
         "originalLanguage": {"id": 2, "name": "Korean"}},
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        result = arrlang.original_language_for(series_file)
    assert result == "kor"


def test_english_name_maps_to_eng(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    movie_file = _movie_file(tmp_path, "Some Hollywood Film (2020)")

    fake_client = MagicMock()
    fake_client.movies.return_value = [
        {"path": "/movies/Some Hollywood Film (2020)",
         "originalLanguage": {"id": 3, "name": "English"}},
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        result = arrlang.original_language_for(movie_file)
    assert result == "eng"


def test_unmapped_language_name_returns_none(tmp_path, monkeypatch):
    """A name outside the eng/jpn/kor mapping table (e.g. "French") must
    return None, not a made-up code (a raw lowercased language name is
    not an ISO 639-2 code) -- this keeps the "ISO 639-2 code, or None"
    contract honest and means get_audio_lang_pref sees exactly the same
    "unknown" signal it would on an actual Arr failure."""
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    movie_file = _movie_file(tmp_path, "Amelie (2001)")

    fake_client = MagicMock()
    fake_client.movies.return_value = [
        {"path": "/movies/Amelie (2001)",
         "originalLanguage": {"id": 4, "name": "French"}},
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        result = arrlang.original_language_for(movie_file)
    assert result is None


def test_returns_none_on_client_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    movie_file = _movie_file(tmp_path, "Some Film (2020)")

    fake_client = MagicMock()
    fake_client.movies.return_value = None  # ArrClient's own failure sentinel
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        result = arrlang.original_language_for(movie_file)
    assert result is None


def test_returns_none_on_client_exception(tmp_path, monkeypatch):
    """Belt-and-braces: even if ArrClient somehow raised instead of
    returning None, original_language_for must still never raise."""
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    movie_file = _movie_file(tmp_path, "Some Film (2020)")

    with patch.object(arrlang, "ArrClient", side_effect=RuntimeError("boom")):
        result = arrlang.original_language_for(movie_file)
    assert result is None


def test_returns_none_on_missing_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.delenv("RADARR_API_KEY", raising=False)
    # No .env in this tmp MEDIA_STACK_ROOT either, so the key resolves empty.
    monkeypatch.setattr(paths, "MEDIA_STACK_ROOT", tmp_path)
    movie_file = _movie_file(tmp_path, "Some Film (2020)")

    fake_client_cls = MagicMock()
    with patch.object(arrlang, "ArrClient", fake_client_cls):
        result = arrlang.original_language_for(movie_file)
    assert result is None
    fake_client_cls.assert_not_called()  # no key -> never even construct the client


def test_returns_none_on_no_folder_match(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    movie_file = _movie_file(tmp_path, "Not In Radarr (2020)")

    fake_client = MagicMock()
    fake_client.movies.return_value = [
        {"path": "/movies/A Totally Different Movie (2019)",
         "originalLanguage": {"id": 1, "name": "English"}},
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        result = arrlang.original_language_for(movie_file)
    assert result is None


def test_returns_none_on_non_media_path(tmp_path, monkeypatch):
    """A path that isn't under movies/ or tv/ returns None WITHOUT ever
    constructing ArrClient (no network attempt at all)."""
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    other = tmp_path / "music" / "Some Album" / "track.mp3"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"x")

    fake_client_cls = MagicMock()
    with patch.object(arrlang, "ArrClient", fake_client_cls):
        result = arrlang.original_language_for(other)
    assert result is None
    fake_client_cls.assert_not_called()


def test_returns_none_on_path_outside_media_root(tmp_path, monkeypatch):
    other_root = tmp_path / "elsewhere"
    other_root.mkdir()
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path / "media")
    outside = other_root / "movies" / "Some Film" / "f.mkv"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"x")

    fake_client_cls = MagicMock()
    with patch.object(arrlang, "ArrClient", fake_client_cls):
        result = arrlang.original_language_for(outside)
    assert result is None
    fake_client_cls.assert_not_called()


def test_malformed_original_language_field_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    movie_file = _movie_file(tmp_path, "Weird Metadata (2020)")

    fake_client = MagicMock()
    fake_client.movies.return_value = [
        {"path": "/movies/Weird Metadata (2020)", "originalLanguage": "not-a-dict"},
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        result = arrlang.original_language_for(movie_file)
    assert result is None


def test_caches_second_call_does_not_refetch(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    movie_a = _movie_file(tmp_path, "Movie A (2020)")
    movie_b = _movie_file(tmp_path, "Movie B (2021)")

    fake_client = MagicMock()
    fake_client.movies.return_value = [
        {"path": "/movies/Movie A (2020)",
         "originalLanguage": {"id": 1, "name": "English"}},
        {"path": "/movies/Movie B (2021)",
         "originalLanguage": {"id": 2, "name": "Japanese"}},
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        first = arrlang.original_language_for(movie_a)
        second = arrlang.original_language_for(movie_b)
    assert first == "eng"
    assert second == "jpn"
    fake_client.movies.assert_called_once()  # one fetch for both lookups


def test_record_under_different_root_is_ignored(tmp_path, monkeypatch):
    """Codex round 5: a record whose basename matches but whose PATH'S
    PARENT is an unrelated root (not the configured/default Arr root)
    must be ignored entirely -- None, so the full pass falls back
    safely, rather than trusting a same-named-but-unrelated record."""
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    monkeypatch.delenv("ARR_MOVIES_ROOT", raising=False)
    movie_file = _movie_file(tmp_path, "Shared")

    fake_client = MagicMock()
    fake_client.movies.return_value = [
        {"path": "/unrelated-library/Shared",
         "originalLanguage": {"id": 1, "name": "Japanese"}},
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        result = arrlang.original_language_for(movie_file)
    assert result is None


def test_record_under_configured_root_is_used(tmp_path, monkeypatch):
    """A record under the ARR_MOVIES_ROOT override is used normally."""
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    monkeypatch.setenv("ARR_MOVIES_ROOT", "/data/movies")
    movie_file = _movie_file(tmp_path, "Shared")

    fake_client = MagicMock()
    fake_client.movies.return_value = [
        {"path": "/data/movies/Shared",
         "originalLanguage": {"id": 1, "name": "Japanese"}},
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        result = arrlang.original_language_for(movie_file)
    assert result == "jpn"


def test_record_under_configured_root_with_trailing_slash_still_matches(tmp_path, monkeypatch):
    """Root comparison strips trailing slashes (posix semantics) on
    both the configured root and the record's own path."""
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    monkeypatch.setenv("ARR_MOVIES_ROOT", "/data/movies/")  # trailing slash
    movie_file = _movie_file(tmp_path, "Shared")

    fake_client = MagicMock()
    fake_client.movies.return_value = [
        {"path": "/data/movies/Shared/",  # trailing slash too
         "originalLanguage": {"id": 1, "name": "Japanese"}},
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        result = arrlang.original_language_for(movie_file)
    assert result == "jpn"


def test_two_records_same_basename_under_root_is_ambiguous(tmp_path, monkeypatch):
    """Codex round 5: two ACCEPTED records (both under the correct root)
    sharing a basename must resolve to None -- never guess which one a
    host file with that folder name actually is."""
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    monkeypatch.delenv("ARR_MOVIES_ROOT", raising=False)
    movie_file = _movie_file(tmp_path, "Shared")

    fake_client = MagicMock()
    fake_client.movies.return_value = [
        {"path": "/movies/Shared", "originalLanguage": {"id": 1, "name": "Japanese"}},
        {"path": "/movies/Shared", "originalLanguage": {"id": 2, "name": "English"}},
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        result = arrlang.original_language_for(movie_file)
    assert result is None


def test_ambiguous_basename_does_not_shadow_unambiguous_ones(tmp_path, monkeypatch):
    """Only the colliding basename resolves to None -- a different,
    unambiguous folder in the same fetch is unaffected."""
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    monkeypatch.delenv("ARR_MOVIES_ROOT", raising=False)
    shared_file = _movie_file(tmp_path, "Shared")
    other_file = _movie_file(tmp_path, "Unique Movie (2021)")

    fake_client = MagicMock()
    fake_client.movies.return_value = [
        {"path": "/movies/Shared", "originalLanguage": {"id": 1, "name": "Japanese"}},
        {"path": "/movies/Shared", "originalLanguage": {"id": 2, "name": "English"}},
        {"path": "/movies/Unique Movie (2021)",
         "originalLanguage": {"id": 3, "name": "Korean"}},
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        shared_result = arrlang.original_language_for(shared_file)
        other_result = arrlang.original_language_for(other_file)
    assert shared_result is None
    assert other_result == "kor"


def test_record_under_different_root_does_not_cause_false_ambiguity(tmp_path, monkeypatch):
    """A same-basename record rejected for being under the wrong root
    must not itself trigger the ambiguity rule -- only records that are
    actually ACCEPTED (right root) can collide."""
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    monkeypatch.delenv("ARR_MOVIES_ROOT", raising=False)
    movie_file = _movie_file(tmp_path, "Shared")

    fake_client = MagicMock()
    fake_client.movies.return_value = [
        {"path": "/movies/Shared", "originalLanguage": {"id": 1, "name": "Japanese"}},
        {"path": "/unrelated-library/Shared",
         "originalLanguage": {"id": 2, "name": "English"}},
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        result = arrlang.original_language_for(movie_file)
    assert result == "jpn"  # the one accepted record wins, no false ambiguity


def test_root_override_via_dotenv_file(tmp_path, monkeypatch):
    """ARR_MOVIES_ROOT has the SAME process-env-over-.env precedence as
    the API keys: with no process-env value, a `.env` entry in
    MEDIA_STACK_ROOT is used."""
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    monkeypatch.delenv("ARR_MOVIES_ROOT", raising=False)
    stack_root = tmp_path / "stack"
    stack_root.mkdir()
    (stack_root / ".env").write_text("ARR_MOVIES_ROOT=/custom/movies\n")
    monkeypatch.setattr(paths, "MEDIA_STACK_ROOT", stack_root)
    movie_file = _movie_file(tmp_path, "Shared")

    fake_client = MagicMock()
    fake_client.movies.return_value = [
        {"path": "/custom/movies/Shared",
         "originalLanguage": {"id": 1, "name": "English"}},
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        result = arrlang.original_language_for(movie_file)
    assert result == "eng"


def test_root_process_env_overrides_dotenv_file(tmp_path, monkeypatch):
    """A process-env ARR_MOVIES_ROOT wins over a conflicting `.env`
    entry -- same precedence as the API keys."""
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "key123")
    monkeypatch.setenv("ARR_MOVIES_ROOT", "/env-wins/movies")
    stack_root = tmp_path / "stack"
    stack_root.mkdir()
    (stack_root / ".env").write_text("ARR_MOVIES_ROOT=/dotenv-only/movies\n")
    monkeypatch.setattr(paths, "MEDIA_STACK_ROOT", stack_root)
    movie_file = _movie_file(tmp_path, "Shared")

    fake_client = MagicMock()
    fake_client.movies.return_value = [
        {"path": "/env-wins/movies/Shared",
         "originalLanguage": {"id": 1, "name": "English"}},
        # Would also basename-match, but sits under the .env-only root,
        # which process-env must NOT fall back to.
        {"path": "/dotenv-only/movies/Shared",
         "originalLanguage": {"id": 2, "name": "Japanese"}},
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        result = arrlang.original_language_for(movie_file)
    assert result == "eng"


def test_series_root_override_via_env_works(tmp_path, monkeypatch):
    """ARR_TV_ROOT works symmetrically to ARR_MOVIES_ROOT for series."""
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("SONARR_API_KEY", "key456")
    monkeypatch.setenv("ARR_TV_ROOT", "/data/tv")
    series_file = _series_file(tmp_path, "Squid Game")

    fake_client = MagicMock()
    fake_client.series.return_value = [
        {"path": "/data/tv/Squid Game",
         "originalLanguage": {"id": 1, "name": "Korean"}},
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        result = arrlang.original_language_for(series_file)
    assert result == "kor"


def test_series_under_different_root_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("SONARR_API_KEY", "key456")
    monkeypatch.delenv("ARR_TV_ROOT", raising=False)
    series_file = _series_file(tmp_path, "Squid Game")

    fake_client = MagicMock()
    fake_client.series.return_value = [
        {"path": "/unrelated-library/Squid Game",
         "originalLanguage": {"id": 1, "name": "Korean"}},
    ]
    with patch.object(arrlang, "ArrClient", return_value=fake_client):
        result = arrlang.original_language_for(series_file)
    assert result is None


def test_cache_is_independent_per_kind(tmp_path, monkeypatch):
    """A movie lookup must not consume/shadow the series fetch, and vice
    versa — movies and series are cached under separate keys."""
    monkeypatch.setattr(paths, "MEDIA_ROOT", tmp_path)
    monkeypatch.setenv("RADARR_API_KEY", "rkey")
    monkeypatch.setenv("SONARR_API_KEY", "skey")
    movie_file = _movie_file(tmp_path, "Some Movie (2020)")
    series_file = _series_file(tmp_path, "Some Series")

    radarr_client = MagicMock()
    radarr_client.movies.return_value = [
        {"path": "/movies/Some Movie (2020)",
         "originalLanguage": {"id": 1, "name": "English"}},
    ]
    sonarr_client = MagicMock()
    sonarr_client.series.return_value = [
        {"path": "/tv/Some Series",
         "originalLanguage": {"id": 2, "name": "Japanese"}},
    ]

    def _make_client(url, key, **kwargs):
        return radarr_client if url == paths.RADARR_URL else sonarr_client

    with patch.object(arrlang, "ArrClient", side_effect=_make_client):
        movie_result = arrlang.original_language_for(movie_file)
        series_result = arrlang.original_language_for(series_file)
    assert movie_result == "eng"
    assert series_result == "jpn"
    radarr_client.movies.assert_called_once()
    sonarr_client.series.assert_called_once()
