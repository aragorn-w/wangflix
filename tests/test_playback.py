"""`media_stack.playback`: the fail-closed Jellyfin playback-wait gate shared by pause-aware ffmpeg extraction callers."""

import itertools

import pytest

import media_stack.playback as pb
from media_stack.playback import (
    PlaybackWaitBudget, PlaybackWaitTimeout, call_with_playback_pause,
    is_playing_or_unknown, wait_while_playing,
)
from media_stack.subverify import ExtractionInterrupted


class _FakeJellyfin:
    """Scripted now_playing_sessions: each call returns the next of list / [] / None."""

    def __init__(self, sequence):
        # Consumed lazily: tests pass itertools.repeat for "busy forever"; list() would never return.
        self._it = iter(sequence)

    def now_playing_sessions(self, key):
        try:
            return next(self._it)
        except StopIteration:
            raise AssertionError("now_playing_sessions called more times than scripted") from None


class _FakeClock:
    """monotonic()/sleep() pair where sleep(s) advances the clock by exactly s."""

    def __init__(self, start: float = 0.0):
        self.t = start

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


# --- is_playing_or_unknown: fail closed ------------------------------------

_PLAYING = [{"NowPlayingItem": {"Name": "x"}}]


@pytest.mark.parametrize("script, key, expected", [
    pytest.param([_PLAYING], "key", True, id="active_sessions"),
    pytest.param([None], "key", True, id="none_unknown_state"),
    pytest.param([[]], "key", False, id="confirmed_empty"),
    pytest.param([], "", False, id="no_api_key_never_polls"),
])
def test_is_playing_or_unknown(script, key, expected):
    assert is_playing_or_unknown(_FakeJellyfin(script), key) is expected


# --- wait_while_playing: fail closed + bounded -----------------------------

@pytest.mark.parametrize("script", [
    pytest.param([_PLAYING, _PLAYING, []], id="polls_until_confirmed_idle"),
    pytest.param([None, None, []], id="none_is_busy_and_keeps_waiting"),
])
def test_wait_while_playing_sleeps_one_poll_per_busy_reading(monkeypatch, script):
    sleeps = []
    monkeypatch.setattr(pb.time, "sleep", lambda s: sleeps.append(s))
    wait_while_playing(_FakeJellyfin(script), "key", 30.0, PlaybackWaitBudget(30.0), poll_s=5.0)
    assert sleeps == [5.0, 5.0]


@pytest.mark.parametrize("key, script", [
    pytest.param("", [], id="without_api_key_does_not_poll"),
    pytest.param("key", [[]], id="when_confirmed_idle"),
])
def test_wait_while_playing_returns_immediately(key, script):
    wait_while_playing(_FakeJellyfin(script), key, 30.0, PlaybackWaitBudget(30.0))


def test_wait_while_playing_raises_after_max_wait_exceeded(monkeypatch):
    jf = _FakeJellyfin(itertools.repeat(_PLAYING))
    monkeypatch.setattr(pb.time, "sleep", lambda s: None)
    times = itertools.chain([0.0], itertools.count(70.0, 70.0))
    monkeypatch.setattr(pb.time, "monotonic", lambda: next(times))
    with pytest.raises(PlaybackWaitTimeout):
        wait_while_playing(jf, "key", 1.0, PlaybackWaitBudget(1.0), poll_s=5.0)


def test_wait_while_playing_cumulative_across_separate_calls_exceeds_budget(monkeypatch):
    """Two calls sharing one budget, each under it alone, raise once their sum crosses it."""
    clock = _FakeClock()
    jf1 = _FakeJellyfin([_PLAYING, []])  # one 60s busy poll, then idle
    jf2 = _FakeJellyfin([_PLAYING, []])
    monkeypatch.setattr(pb.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(pb.time, "sleep", clock.sleep)
    budget = PlaybackWaitBudget(1.5)  # 90s total allowance
    wait_while_playing(jf1, "key", 1.5, budget, poll_s=60.0)  # spends 60s
    assert budget.remaining_s == pytest.approx(30.0)
    with pytest.raises(PlaybackWaitTimeout):
        wait_while_playing(jf2, "key", 1.5, budget, poll_s=60.0)  # needs another 60s, only 30s left


# --- call_with_playback_pause ----------------------------------------------

def test_call_with_playback_pause_passes_through_on_success():
    jf = _FakeJellyfin([])

    def _fn(*args, should_abort, **kwargs):
        assert callable(should_abort)
        return "ok"

    result = call_with_playback_pause(_fn, jf, "key", 30.0, PlaybackWaitBudget(30.0))
    assert result == "ok"


def test_call_with_playback_pause_retries_after_extraction_interrupted(monkeypatch):
    jf = _FakeJellyfin([])  # no key -> wait_while_playing is a no-op, never polls
    calls = {"n": 0}

    def _fn(*args, should_abort, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ExtractionInterrupted("boom")
        return "ok"

    monkeypatch.setattr(pb.time, "sleep", lambda s: None)
    result = call_with_playback_pause(_fn, jf, "", 30.0, PlaybackWaitBudget(30.0))
    assert result == "ok"
    assert calls["n"] == 2


def test_call_with_playback_pause_propagates_playback_wait_timeout(monkeypatch):
    jf = _FakeJellyfin(itertools.repeat(_PLAYING))
    monkeypatch.setattr(pb.time, "sleep", lambda s: None)
    times = itertools.chain([0.0], itertools.count(70.0, 70.0))
    monkeypatch.setattr(pb.time, "monotonic", lambda: next(times))

    def _fn(*args, should_abort, **kwargs):
        raise ExtractionInterrupted("boom")

    with pytest.raises(PlaybackWaitTimeout):
        call_with_playback_pause(_fn, jf, "key", 1.0, PlaybackWaitBudget(1.0), poll_s=5.0)
