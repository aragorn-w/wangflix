"""Movie de-duplication helpers — keeper selection for folders that ended
up with more than one video file.

Root cause (see the project memory): when Radarr auto-upgrades a movie it
imports the new file and deletes the one it was replacing — but our
normalization pipeline had often already renamed/converted that old file
(.mp4→.mkv + retag), so Radarr's delete targets a path that no longer
exists, leaving the old file orphaned beside the new keeper → Jellyfin
lists both.  ``movie-dedupe.py`` uses these helpers to pick the keeper and
recycle the rest.

Pure functions only — no I/O, no network.  The CLI does the ffprobe /
filesystem / Radarr work and feeds the results in here.
"""

from __future__ import annotations

import re

# Video container extensions Jellyfin treats as movies (so a stray .mk3d or
# .avi beside an .mkv still shows as a duplicate — the first dedup pass
# missed .mk3d by only matching mkv/mp4/m4v).
VIDEO_EXTS = (
    ".mkv", ".mp4", ".m4v", ".mk3d", ".avi", ".ts", ".m2ts", ".wmv",
    ".mov", ".flv", ".webm", ".mpg", ".mpeg", ".vob", ".ogv", ".divx",
)

# Files smaller than this are treated as corrupt/partial and never chosen as
# the keeper (a 25MB "Bluray-2160p" was the Zootopia 2 case).
CORRUPT_MAX_BYTES = 100 * 1024 * 1024

_RES_RANK = {"2160p": 4, "1080p": 3, "720p": 2, "480p": 1}
# Source tiers, mirroring Radarr's general quality ordering.
_SRC_RANK = {
    "remux": 5, "bluray": 4, "webdl": 3, "web-dl": 3,
    "webrip": 2, "hdtv": 1, "dvd": 1,
}


def is_video(filename: str) -> bool:
    """True if the name has a recognized video extension (case-insensitive),
    excluding in-progress ``.tmp.mkv`` mux outputs."""
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
    """Sort key for keeper selection (higher == better).  Order of
    precedence: not-corrupt, resolution, source tier, processed-tag, size.
    A corrupt file (tiny/partial) sorts below everything else regardless of
    its name, so it's never chosen over a real file."""
    return (
        0 if is_corrupt(size_bytes) else 1,
        resolution_rank(filename),
        source_rank(filename),
        1 if processed else 0,
        size_bytes,
    )


def choose_keeper(videos: list[dict]) -> tuple[dict, list[dict]]:
    """Given a list of ``{"name", "size", "processed"}`` dicts (one per
    video file in a movie folder), return ``(keeper, extras)`` where keeper
    is the single file to keep and extras is everything else, both ordered
    best-first.  Raises ValueError on an empty list."""
    if not videos:
        raise ValueError("choose_keeper requires at least one video")
    ranked = sorted(
        videos,
        key=lambda v: rank_key(v["name"], v["size"], v.get("processed", False)),
        reverse=True,
    )
    return ranked[0], ranked[1:]
