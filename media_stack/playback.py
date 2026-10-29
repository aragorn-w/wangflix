"""Jellyfin playback-wait gate shared by `sub-fallback.py` and `audio-lang-check.py`.

Fail closed: an unknown Jellyfin status (`None`) counts as "someone is watching".
One `PlaybackWaitBudget` per candidate is cumulative across waits; the exhaustion
check runs after every poll, busy or idle.
"""

from __future__ import annotations

import time
from typing import Callable

from media_stack.clients.jellyfin import JellyfinClient
from media_stack.subverify import ExtractionInterrupted

# Seconds between polls while waiting for playback to clear.
PLAYBACK_POLL_S = 60


class PlaybackWaitTimeout(Exception):
    """Cumulative playback allowance exhausted; callers end the run cleanly."""


class PlaybackWaitBudget:
    """One per candidate, never one per wait."""

    def __init__(self, max_wait_min: float):
        self.remaining_s = max_wait_min * 60

    def spend(self, seconds: float) -> None:
        self.remaining_s -= seconds


def is_playing_or_unknown(jellyfin: JellyfinClient, api_key: str) -> bool:
    """True on playback or unknown status. No API key returns False."""
    if not api_key:
        return False
    sessions = jellyfin.now_playing_sessions(api_key)
    return sessions is None or len(sessions) > 0


def wait_while_playing(
    jellyfin: JellyfinClient, api_key: str, max_wait_min: float,
    budget: PlaybackWaitBudget, *, poll_s: float = PLAYBACK_POLL_S,
    log: Callable[[str], None] = lambda msg: None,
) -> None:
    """Block while playing or unknown; spend `budget` (also when raising)."""
    if not api_key:
        return
    t0 = time.monotonic()
    announced = False
    while True:
        busy = is_playing_or_unknown(jellyfin, api_key)
        elapsed = time.monotonic() - t0
        if elapsed >= budget.remaining_s:
            budget.spend(elapsed)
            raise PlaybackWaitTimeout(
                f"cumulative playback wait for this candidate exceeded {max_wait_min:g} min")
        if not busy:
            break
        if not announced:
            log("pausing: Jellyfin is playing (or its status is unknown -- fail closed)")
            announced = True
        time.sleep(poll_s)
    budget.spend(time.monotonic() - t0)
    if announced:
        log("playback cleared (or became reachable), resuming")


def call_with_playback_pause(
    fn: Callable, jellyfin: JellyfinClient, api_key: str, max_wait_min: float,
    budget: PlaybackWaitBudget, *args, poll_s: float = PLAYBACK_POLL_S,
    log: Callable[[str], None] = lambda msg: None, **kwargs,
):
    """Call `fn(should_abort=...)`; on `ExtractionInterrupted`, wait on `budget`
    and restart the call from scratch."""
    while True:
        try:
            return fn(*args, should_abort=lambda: is_playing_or_unknown(jellyfin, api_key), **kwargs)
        except ExtractionInterrupted:
            wait_while_playing(jellyfin, api_key, max_wait_min, budget, poll_s=poll_s, log=log)
