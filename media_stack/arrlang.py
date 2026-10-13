"""Original-language signal from Radarr/Sonarr.

`get_audio_lang_pref` (see `media_stack.audio`) needs to know whether a
title's production-original language is Japanese/Korean/English so it
doesn't have to infer it from the Matroska `default` disposition, which
release groups routinely set on a DUB.  Radarr/Sonarr already carry this
as `originalLanguage.name` on the movie/series record — this module maps
a library file's path back to that record and returns the ISO 639-2 code.

Network boundary: per `media_stack/clients/AGENTS.md`, all HTTP calls go
through `media_stack.clients.arr.ArrClient` (`movies()` / `series()`).
This module does no `requests.*` calls of its own.

Root check (codex round 5): records are matched by folder BASENAME only
(see `_kind_and_folder`), so without a sanity check an Arr record at an
UNRELATED root (e.g. `/unrelated-library/Shared`) could supply the
signal for a host file under `movies/Shared` purely because the two
folder names happen to collide.  A record is only indexed when its
path's PARENT directory matches the expected Arr root for its kind —
`ARR_MOVIES_ROOT` (default `/movies`) for Radarr, `ARR_TV_ROOT` (default
`/tv`) for Sonarr, same process-env-over-`.env` precedence as the API
keys, compared with trailing slashes stripped (posix semantics). If,
even after that filter, two or more records still share a basename, the
basename is AMBIGUOUS and maps to `None` rather than guessing.

`original_language_for()` NEVER raises — on any failure (API
unreachable, missing key, no match, ambiguous match, malformed data,
path not under `MEDIA_ROOT`) it returns `None`, which callers treat as
"unknown" (see `get_audio_lang_pref`'s rule 6).
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from media_stack import paths
from media_stack.clients.arr import ArrClient

# Short timeout: this is a best-effort signal on the hot per-file path,
# not a health probe — a slow/unreachable Arr instance must not stall
# the consolidate-subs pipeline.
_TIMEOUT_S = 10

# originalLanguage.name (Radarr/Sonarr) -> ISO 639-2 code.  Only these three
# change get_audio_lang_pref's behavior.  A name NOT in this table returns
# None (unknown), not the lowercased name — `original_language_for`'s
# contract is "an ISO 639-2 code, or None", and a raw lowercased English
# language NAME (e.g. "french") is not an ISO 639-2 code, so passing it
# through would be a lie callers could reasonably rely on.
_LANG_NAME_TO_CODE: dict[str, str] = {
    "english": "eng",
    "japanese": "jpn",
    "korean": "kor",
}

# Per-process cache: {"movie": {folder_basename: code_or_None}} /
# {"series": {...}}.  Populated lazily, one fetch each, so a library scan
# (one call per file, potentially thousands of files per worker process)
# doesn't hammer Radarr/Sonarr.  `None` means "fetch failed/unavailable"
# and is cached too, so a missing API key doesn't retry every file.
_CACHE: dict[str, dict[str, str | None] | None] = {}


def _code_for_name(name: object) -> str | None:
    """Map an `originalLanguage.name` string to its ISO 639-2 code, or
    None when it isn't a non-empty string or isn't in `_LANG_NAME_TO_CODE`
    (i.e. anything other than English/Japanese/Korean)."""
    if not isinstance(name, str) or not name.strip():
        return None
    n = name.strip().lower()
    return _LANG_NAME_TO_CODE.get(n)


def _api_key(env_var: str) -> str:
    """Process-env precedence over `.env` — same pattern as
    movie-dedupe.py / tv-dedupe.py (the key isn't carried by
    media_stack.paths)."""
    if env_var in os.environ:
        return os.environ[env_var]
    return paths.load_env_file(paths.MEDIA_STACK_ROOT / ".env").get(env_var, "")


def _arr_root(env_var: str, default: str) -> str:
    """The expected Arr library root for one kind (movies/series).  SAME
    process-env-over-`.env` precedence as `_api_key`, but with a real
    `default` (Radarr `/movies`, Sonarr `/tv`) rather than `""` — this is
    non-secret path config, not a credential, so unlike the API keys it
    always resolves to something usable."""
    if env_var in os.environ:
        val = os.environ[env_var]
    else:
        val = paths.load_env_file(paths.MEDIA_STACK_ROOT / ".env").get(env_var, "")
    return val if val else default


def _normalize_root(raw: str) -> str:
    """Strip trailing slashes for root-path comparison (posix
    semantics) — `/movies/` and `/movies` must compare equal."""
    s = (raw or "").rstrip("/")
    return s if s else "/"


def _record_parent(raw_path: str) -> str:
    """The PARENT directory of an Arr record's `path`, posix semantics,
    trailing slashes stripped first so `/movies/Foo/` and `/movies/Foo`
    give the same parent."""
    p = (raw_path or "").rstrip("/")
    if not p:
        return ""
    return str(PurePosixPath(p).parent)


def _folder_lang_map(
    *, cache_key: str, api_key_var: str, url: str, fetch: str,
    root_env_var: str, default_root: str, path_field: str = "path",
) -> dict[str, str | None] | None:
    """Fetch+cache a {last-path-component: original-language-code} map
    for either Radarr movies or Sonarr series.  Returns None (and caches
    None) on any failure — unreachable, missing key, or a malformed
    response the client already guards against.

    Codex round 5: a record is only indexed when its path's PARENT
    directory matches the expected root for this kind (`root_env_var`,
    defaulting to `default_root`) — otherwise an Arr record at an
    unrelated root could supply the signal for a host file purely
    because the folder basenames happen to collide.  If, even after
    that filter, two or more ACCEPTED records still share a basename,
    that basename is AMBIGUOUS and maps to `None` rather than guessing
    which one the host file actually is.
    """
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    result: dict[str, str | None] | None = None
    try:
        key = _api_key(api_key_var)
        if key:
            client = ArrClient(url, key, timeout=_TIMEOUT_S)
            records = getattr(client, fetch)()
            if records is not None:
                expected_root = _normalize_root(_arr_root(root_env_var, default_root))
                seen: dict[str, str | None] = {}
                ambiguous: set[str] = set()
                for rec in records:
                    raw_path = rec.get(path_field) or ""
                    folder = os.path.basename(raw_path.rstrip("/"))
                    if not folder:
                        continue
                    if _record_parent(raw_path) != expected_root:
                        continue
                    if folder in seen:
                        ambiguous.add(folder)
                        continue
                    lang = rec.get("originalLanguage")
                    name = lang.get("name") if isinstance(lang, dict) else None
                    seen[folder] = _code_for_name(name)
                result = {f: (None if f in ambiguous else v) for f, v in seen.items()}
    except Exception:
        result = None
    _CACHE[cache_key] = result
    return result


def _movies_by_folder() -> dict[str, str | None] | None:
    return _folder_lang_map(
        cache_key="movie", api_key_var="RADARR_API_KEY",
        url=paths.RADARR_URL, fetch="movies",
        root_env_var="ARR_MOVIES_ROOT", default_root="/movies",
    )


def _series_by_folder() -> dict[str, str | None] | None:
    return _folder_lang_map(
        cache_key="series", api_key_var="SONARR_API_KEY",
        url=paths.SONARR_URL, fetch="series",
        root_env_var="ARR_TV_ROOT", default_root="/tv",
    )


def _kind_and_folder(path: Path) -> tuple[str, str] | None:
    """Classify `path` as a Radarr movie or Sonarr series file by its
    location under `paths.MEDIA_ROOT`: `movies/<Folder>/...` or
    `tv/<Folder>/...`.  Returns `(kind, folder_basename)` or None for
    anything else (including any resolution failure)."""
    try:
        rel = path.resolve().relative_to(paths.MEDIA_ROOT.resolve())
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if len(parts) < 2:
        return None
    top = parts[0].lower()
    if top == "movies":
        return "movie", parts[1]
    if top == "tv":
        return "series", parts[1]
    return None


def original_language_for(path: Path) -> str | None:
    """Return the ISO 639-2 original-language code ("eng"/"jpn"/"kor") for
    the movie/series that owns `path`, or `None` if unknown, unmapped (any
    `originalLanguage.name` other than English/Japanese/Korean), or on ANY
    failure.  Never raises.

    `path` must live under `movies/<Folder>/...` (matched against
    Radarr's `movie["path"]` last component) or `tv/<Folder>/...`
    (Sonarr's `series["path"]`) relative to `paths.MEDIA_ROOT`; anything
    else returns None without making a network call.
    """
    try:
        kind_folder = _kind_and_folder(path)
        if kind_folder is None:
            return None
        kind, folder = kind_folder
        table = _movies_by_folder() if kind == "movie" else _series_by_folder()
        if table is None:
            return None
        return table.get(folder)
    except Exception:
        return None
