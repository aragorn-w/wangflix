"""`warm-subtitle-cache.py`: target selection, the playback gate, the deadline and the failure
abort, with a scripted fake Jellyfin client, a fake clock and no network."""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from media_stack.clients.jellyfin import NetworkError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "warm_subtitle_cache", str(PROJECT_ROOT / "warm-subtitle-cache.py"))
wsc = importlib.util.module_from_spec(_spec)
# The module defines dataclasses under `from __future__ import annotations`.
sys.modules[_spec.name] = wsc
_spec.loader.exec_module(wsc)

KEY = "SECRET-API-KEY"
_BUSY = [{"Id": "s", "NowPlayingItem": {"Id": "x"}}]


class Clock:
    """`sleep` advances it, like the real thing; so does each scripted fetch."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeJF:
    """Serves both roles: `request` for fetches, `now_playing_sessions` for the gate.

    `results` yields (status or NetworkError, seconds the fetch took), then (200, 0.1) forever;
    `sessions` yields what the gate sees, then idle ([]). Both are consumed lazily."""

    def __init__(self, clock, results=(), sessions=()):
        self.clock = clock
        self.results = iter(results)
        self.sessions = iter(sessions)
        self.paths = []

    def request(self, path, key, **kwargs):
        assert key == KEY
        self.paths.append(path)
        outcome, secs = next(self.results, (200, 0.1))
        self.clock.t += secs
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, b"1\n00:00:00,000 --> 00:00:01,000\nHi\n"

    def now_playing_sessions(self, key):
        return next(self.sessions, [])


def _sub(index, *, text=True, external=False, kind="Subtitle", codec="subrip"):
    return {"Type": kind, "Index": index, "Codec": codec, "IsExternal": external,
            "IsTextSubtitleStream": text}


def _item(item_id, kind="Movie", created="2026-01-01T00:00:00Z", streams=(), name=None):
    return {"Id": item_id, "Type": kind, "Name": name or item_id, "DateCreated": created,
            "MediaSources": [{"Id": f"src-{item_id}", "MediaStreams": list(streams)}]}


def _targets(n):
    return [wsc.Target(f"i{k}", f"s{k}", 2, "Movie", "2026-01-01T00:00:00Z", f"t{k}")
            for k in range(n)]


def _run(targets, results=(), sessions=(), **kwargs):
    clock = Clock()
    jf = FakeJF(clock, results, sessions)
    kwargs.setdefault("deadline", 10 ** 9)
    stats = wsc.run(targets, jf, jf, KEY, clock=clock, sleep=clock.sleep, **kwargs)
    return stats, jf, clock


def test_only_embedded_text_subtitle_tracks_are_targets():
    streams = [_sub(0, kind="Video"), _sub(1, kind="Audio"), _sub(2), _sub(3, text=False,
               codec="hdmv_pgs_subtitle"), _sub(4, external=True), _sub(5)]
    [a, b] = wsc.select_targets([_item("m1", streams=streams)])
    assert (a.item_id, a.source_id, a.stream) == ("m1", "src-m1", 2)
    assert b.stream == 5


def test_movies_come_before_episodes_and_the_newest_comes_first():
    sub = [_sub(2)]
    items = [
        _item("ep-old", "Episode", "2026-03-01T00:00:00Z", sub),
        _item("mov-old", "Movie", "2026-01-01T00:00:00Z", sub),
        _item("ep-new", "Episode", "2026-05-01T00:00:00Z", sub),
        _item("mov-new", "Movie", "2026-02-01T00:00:00Z", sub),
    ]
    assert [t.item_id for t in wsc.select_targets(items)] == [
        "mov-new", "mov-old", "ep-new", "ep-old"]


def test_every_track_of_a_file_is_requested_in_stream_order():
    ts = wsc.select_targets([_item("m1", streams=[_sub(2), _sub(3), _sub(7)])])
    assert [t.stream for t in ts] == [2, 3, 7]


def test_fetch_url_uses_item_source_and_stream_index():
    _, jf, _ = _run([wsc.Target("item", "source", 4, "Movie", "", "n")])
    assert jf.paths == ["/Videos/item/source/Subtitles/4/0/Stream.srt"]


def test_gate_waits_while_playing_then_proceeds(capsys):
    stats, jf, clock = _run(_targets(1), sessions=[_BUSY, None, _BUSY])
    out = capsys.readouterr().out
    assert len(jf.paths) == 1 and stats.cached == 1
    assert clock.t == pytest.approx(3 * wsc.PLAYBACK_POLL_S + 0.1)  # three sleeps, then the fetch
    assert out.count("waiting for playback") == 1  # announced once per wait, not per poll


def test_gate_is_checked_before_every_fetch():
    seen = []

    class Spy(FakeJF):
        def now_playing_sessions(self, key):
            seen.append(len(self.paths))
            return []

    clock = Clock()
    jf = Spy(clock)
    wsc.run(_targets(3), jf, jf, KEY, deadline=10 ** 9, clock=clock, sleep=clock.sleep)
    assert seen == [0, 1, 2]


def test_deadline_stops_new_fetches_and_reports_what_is_left(capsys):
    results = [(200, 40.0)] * 10  # each fetch takes 40 s; deadline 100 s allows three starts
    stats, jf, _ = _run(_targets(10), results, deadline=100)
    assert len(jf.paths) == 3
    assert (stats.extracted, stats.left, stats.aborted) == (3, 7, False)
    assert "7 target(s) left" in capsys.readouterr().out


def test_deadline_during_a_playback_wait_starts_nothing():
    stats, jf, _ = _run(_targets(4), sessions=iter(lambda: _BUSY, None), deadline=300)
    assert jf.paths == [] and stats.left == 4


class SlowSessionsJF(FakeJF):
    """The gate's /Sessions call itself takes 2 s."""

    def now_playing_sessions(self, key):
        self.clock.t += 2.0
        return []


def test_an_idle_answer_that_arrives_after_the_deadline_starts_nothing():
    clock = Clock()
    clock.t = 99.0
    jf = SlowSessionsJF(clock)
    stats = wsc.run(_targets(1), jf, jf, KEY, deadline=100.0, clock=clock, sleep=clock.sleep)
    assert jf.paths == [] and stats.left == 1


def test_ten_consecutive_failures_abort_the_run(capsys):
    results = [(500, 0.1)] * 15
    stats, jf, _ = _run(_targets(15), results)
    out = capsys.readouterr().out
    assert len(jf.paths) == 10
    assert (stats.failed, stats.left, stats.aborted) == (10, 5, True)
    assert "ERROR: 10 consecutive failures" in out


def test_a_success_resets_the_consecutive_failure_count():
    results = [(500, 0.1)] * 9 + [(200, 0.1)] + [(NetworkError("boom"), 0.1)] * 9 + [(200, 0.1)]
    stats, jf, _ = _run(_targets(20), results)
    assert len(jf.paths) == 20
    assert (stats.failed, stats.cached, stats.left, stats.aborted) == (18, 2, 0, False)


def test_failures_are_logged_without_the_api_key(capsys):
    _run(_targets(2), [(404, 0.5), (NetworkError("GET http://jf/x: TimeoutError"), 0.5)])
    out = capsys.readouterr().out
    assert "HTTP 404" in out and "TimeoutError" in out
    assert KEY not in out


def test_summary_counts_split_cached_from_extracted(capsys):
    results = [(200, 0.2), (200, 90.0), (200, 3.0), (500, 1.0)]
    stats, _, _ = _run(_targets(5), results)
    assert (stats.total, stats.cached, stats.extracted, stats.failed, stats.left) == (
        5, 2, 2, 1, 0)  # fifth target hits the default 0.1 s cache-hit result
    out = capsys.readouterr().out
    assert out.count("extracted") == 2 and out.count("FAILED") == 1  # no line per cache hit


def test_limit_stops_after_n_fetches():
    stats, jf, _ = _run(_targets(5), limit=2)
    assert len(jf.paths) == 2 and stats.left == 3


# main(): everything under tmp_path, Jellyfin replaced by FakeJF, no network.

@pytest.fixture
def world(tmp_path, monkeypatch):
    clock = Clock()
    items = [_item("m1", streams=[_sub(2), _sub(3)]),
             _item("e1", "Episode", streams=[_sub(2)]),
             _item("m2", streams=[_sub(2, text=False)])]
    jf = FakeJF(clock)
    real_request = jf.request

    def request(path, key, **kwargs):
        if path.startswith("/Items"):
            return 200, json.dumps({"Items": items, "TotalRecordCount": 3}).encode()
        return real_request(path, key, **kwargs)

    jf.request = request
    monkeypatch.setattr(wsc, "JellyfinClient", lambda base, timeout=10: jf)
    monkeypatch.setattr(wsc, "LOCK_FILE", tmp_path / "warm.lock")
    monkeypatch.setattr(wsc, "MEDIA_STACK_ROOT", tmp_path / "stack")
    monkeypatch.setattr(wsc, "ensure_var_dirs", lambda: None)
    monkeypatch.setattr(wsc.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["warm-subtitle-cache.py"])
    monkeypatch.setenv("JELLYFIN_API_KEY", KEY)
    return jf


def test_dry_run_lists_targets_and_fetches_nothing(world, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["warm-subtitle-cache.py", "--dry-run"])
    assert wsc.main() == 0
    out = capsys.readouterr().out
    assert "3 target(s)" in out
    assert out.index("Movie m1") < out.index("Episode e1")
    assert world.paths == []
    assert not wsc.LOCK_FILE.exists()


def test_a_real_run_fetches_every_target_and_logs_a_summary(world, capsys):
    assert wsc.main() == 0
    assert len(world.paths) == 3
    assert "total=3 cached=3 extracted=0 failed=0 left=0" in capsys.readouterr().out


def test_a_held_lock_exits_zero_without_fetching(world, capsys):
    held = wsc.acquire_singleton_lock(wsc.LOCK_FILE)
    assert held is not None
    assert wsc.main() == 0
    assert world.paths == [] and "holds the lock" in capsys.readouterr().out


def test_no_api_key_is_an_error(world, monkeypatch, capsys):
    monkeypatch.delenv("JELLYFIN_API_KEY")
    assert wsc.main() == 1
    assert "JELLYFIN_API_KEY is not set" in capsys.readouterr().out


def test_cron_entry_runs_nightly_at_minute_10_with_its_own_log():
    lines = [ln for ln in (PROJECT_ROOT / "ops/cron.d/media-stack.crontab").read_text()
             .splitlines() if "warm-subtitle-cache.py" in ln and not ln.startswith("#")]
    assert lines == ["10 1 * * * python3 $HOME/media-stack/warm-subtitle-cache.py "
                     ">> $HOME/media-stack/var/log/warm-subtitle-cache.log 2>&1"]


def test_script_is_executable_with_a_shebang():
    path = PROJECT_ROOT / "warm-subtitle-cache.py"
    assert path.read_text().startswith("#!/usr/bin/env python3\n")
    assert os.access(path, os.X_OK)
