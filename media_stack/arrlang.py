"""Original-language signal (ISO 639-2) for a library file, from its Radarr/Sonarr record, used
by `get_audio_lang_pref` instead of the Matroska `default` disposition (release groups flag DUBs).

Records are matched by folder BASENAME, so one is indexed only when its path's PARENT equals the
Arr root (`ARR_MOVIES_ROOT` default `/movies`, `ARR_TV_ROOT` default `/tv`; process env over
`.env`), else an unrelated root with a colliding folder name could supply the signal. A basename
shared by 2+ records is AMBIGUOUS and maps to `None`. Never raises: any failure returns `None`.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from media_stack import paths
from media_stack.clients.arr import ArrClient

# best-effort signal on the per-file path; a slow Arr must not stall the pipeline
_TIMEOUT_S = 10

# originalLanguage.name -> ISO 639-2; any other name gives None, never a lowercased name.
_LANG_NAME_TO_CODE: dict[str, str] = {
    "english": "eng",
    "japanese": "jpn",
    "korean": "kor",
}

# Per-process {"movie"|"series": {folder_basename: code_or_None}}; a failed fetch is cached too.
_CACHE: dict[str, dict[str, str | None] | None] = {}


def _code_for_name(name: object) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    n = name.strip().lower()
    return _LANG_NAME_TO_CODE.get(n)


def _api_key(env_var: str) -> str:
    if env_var in os.environ:
        return os.environ[env_var]
    return paths.load_env_file(paths.MEDIA_STACK_ROOT / ".env").get(env_var, "")


def _arr_root(env_var: str, default: str) -> str:
    if env_var in os.environ:
        val = os.environ[env_var]
    else:
        val = paths.load_env_file(paths.MEDIA_STACK_ROOT / ".env").get(env_var, "")
    return val if val else default


def _normalize_root(raw: str) -> str:
    s = (raw or "").rstrip("/")
    return s if s else "/"


def _record_parent(raw_path: str) -> str:
    p = (raw_path or "").rstrip("/")
    if not p:
        return ""
    return str(PurePosixPath(p).parent)


def _folder_lang_map(
    *, cache_key: str, api_key_var: str, url: str, fetch: str,
    root_env_var: str, default_root: str, path_field: str = "path",
) -> dict[str, str | None] | None:
    """None (also cached) on any failure. Applies the root check and ambiguity rule from the
    module docstring."""
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
    """`movies/<Folder>/...` or `tv/<Folder>/...` under MEDIA_ROOT, else None."""
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


ORIGINAL_KNOWN = "known"              # Radarr/Sonarr named English/Japanese/Korean
ORIGINAL_UNKNOWN = "unknown"          # reachable, but no usable answer for this path
ORIGINAL_UNAVAILABLE = "unavailable"  # Radarr/Sonarr could not be asked (no key, down, bad reply)


def original_language_lookup(path: Path) -> tuple[str | None, str]:
    """`(code, status)`. "unknown": Arr answered but gave nothing usable (path outside
    `movies/`/`tv/`, no record, ambiguous basename, other language); retrying won't help.
    "unavailable": Arr couldn't be asked; callers must not cache this against the file. Never
    raises; a cached failure stays "unavailable" for the process.
    """
    try:
        kind_folder = _kind_and_folder(path)
        if kind_folder is None:
            return None, ORIGINAL_UNKNOWN
        kind, folder = kind_folder
        table = _movies_by_folder() if kind == "movie" else _series_by_folder()
        if table is None:
            return None, ORIGINAL_UNAVAILABLE
        code = table.get(folder)
        if code is None:
            return None, ORIGINAL_UNKNOWN
        return code, ORIGINAL_KNOWN
    except Exception:
        return None, ORIGINAL_UNAVAILABLE


def original_language_for(path: Path) -> str | None:
    """Code of `original_language_lookup`; use that to tell an Arr outage from "no answer"."""
    return original_language_lookup(path)[0]
