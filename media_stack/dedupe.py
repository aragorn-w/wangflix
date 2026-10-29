"""Keeper selection for folders holding more than one video file (Radarr's upgrade-delete misses
a file the pipeline already renamed, leaving an orphan beside the keeper). Pure functions, no I/O.
"""

from __future__ import annotations

import re

# Container extensions Jellyfin treats as movies (a stray .mk3d/.avi beside an .mkv is a duplicate).
VIDEO_EXTS = (
    ".mkv", ".mp4", ".m4v", ".mk3d", ".avi", ".ts", ".m2ts", ".wmv",
    ".mov", ".flv", ".webm", ".mpg", ".mpeg", ".vob", ".ogv", ".divx",
)

# Smaller files are treated as corrupt/partial and never chosen as the keeper.
CORRUPT_MAX_BYTES = 100 * 1024 * 1024

_RES_RANK = {"2160p": 4, "1080p": 3, "720p": 2, "480p": 1}
_SRC_RANK = {
    "remux": 5, "bluray": 4, "webdl": 3, "web-dl": 3,
    "webrip": 2, "hdtv": 1, "dvd": 1,
}


def is_video(filename: str) -> bool:
    """Excludes in-progress ``.tmp.mkv`` outputs."""
    low = filename.lower()
    if low.endswith(".tmp.mkv"):
        return False
    return low.endswith(VIDEO_EXTS)


def resolution_rank(filename: str) -> int:
    m = re.search(r"(2160p|1080p|720p|480p)", filename)
    return _RES_RANK.get(m.group(1), 0) if m else 0


def source_rank(filename: str) -> int:
    low = filename.lower()
    for token, rank in _SRC_RANK.items():
        if token in low:
            return rank
    return 0


def is_corrupt(size_bytes: int, *, min_bytes: int = CORRUPT_MAX_BYTES) -> bool:
    return size_bytes < min_bytes


def rank_key(filename: str, size_bytes: int, processed: bool) -> tuple:
    """Higher is better: not-corrupt, resolution, source tier, processed-tag, size."""
    return (
        0 if is_corrupt(size_bytes) else 1,
        resolution_rank(filename),
        source_rank(filename),
        1 if processed else 0,
        size_bytes,
    )


def choose_keeper(videos: list[dict]) -> tuple[dict, list[dict]]:
    """`(keeper, extras)`, both best-first, from ``{"name", "size", "processed"}`` dicts."""
    if not videos:
        raise ValueError("choose_keeper requires at least one video")
    ranked = sorted(
        videos,
        key=lambda v: rank_key(v["name"], v["size"], v.get("processed", False)),
        reverse=True,
    )
    return ranked[0], ranked[1:]


_EPISODE_RE = re.compile(r"[Ss](\d{1,4})[Ee](\d{1,4})")

# A SECOND episode marker right after the first ("S01E01E02", "S01E01-E02", bare "S01E01-02")
# marks a multi-episode file; keyed as its first episode it would "dedupe" away the only copy of E02.
# The bare "-NN" branch must not match a quality tag glued to the number: `(?!\d)` stops "1080"
# matching as "-10", `(?![PpIi])` stops "-720p"/"-1080i".
_MULTI_EP_SUFFIX_RE = re.compile(r"-?[Ee]\d{1,4}|-\d{1,4}(?!\d)(?![PpIi])")


def episode_key(filename: str) -> tuple[int, int] | None:
    """None if there is no token or it is a multi-episode release: it can't be safely paired."""
    matches = list(_EPISODE_RE.finditer(filename))
    if not matches:
        return None
    # Sonarr's "Duplicate" style repeats the whole token ("S01E01 - S01E02"): also multi-episode.
    keys = {(int(m.group(1)), int(m.group(2))) for m in matches}
    if len(keys) > 1:
        return None
    first = matches[0]
    if _MULTI_EP_SUFFIX_RE.match(filename, first.end()):
        return None
    return int(first.group(1)), int(first.group(2))


def group_by_episode(videos: list[dict]) -> dict[tuple[int, int], list[dict]]:
    """Videos with no key are excluded (never auto-resolved); the caller must log them for
    manual review."""
    groups: dict[tuple[int, int], list[dict]] = {}
    for v in videos:
        key = episode_key(v["name"])
        if key is None:
            continue
        groups.setdefault(key, []).append(v)
    return groups
