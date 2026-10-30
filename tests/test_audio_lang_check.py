"""`audio-lang-check.py`: the hourly whisper language-ID spot-check of each video's audio against its tags and the Arr original language.

The detection rules are exercised through `analyze_candidate` with a scripted `detect_language`.
Everything `main()` touches (`probe`, `detect_language`, `original_language_lookup`,
`JellyfinClient`, `telegram_send`) is mocked by the `world` fixture, which also points every
state/lock/log/workdir/media path at `tmp_path` and runs `main()` under `patch.dict(os.environ,
..., clear=True)`. Scripted sequences are consumed lazily (`iter`/`next`), never `list()`ed.
"""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from media_stack import paths as real_paths
from media_stack.subverify import ExtractionInterrupted

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "audio_lang_check", str(PROJECT_ROOT / "audio-lang-check.py")
)
alc = importlib.util.module_from_spec(_spec)
# Config is a dataclass under `from __future__ import annotations`; dataclasses resolves its annotations via sys.modules.
sys.modules[_spec.name] = alc
_spec.loader.exec_module(alc)

NOW = 1_800_000_000.0
HOUR = 3600.0


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(alc, "STATE_FILE", tmp_path / "state" / "identity.json")
    monkeypatch.setattr(alc, "LOCK_FILE", tmp_path / "audio-lang-check.lock")
    monkeypatch.setattr(alc, "JSONL_FILE", tmp_path / "audio-lang-check.jsonl")
    monkeypatch.setattr(alc, "WORKDIR_ROOT", tmp_path / "workdir-root")
    monkeypatch.setattr(alc, "MEDIA_ROOT", tmp_path / "media")
    monkeypatch.setattr(alc, "MEDIA_STACK_ROOT", tmp_path / "stack")
    monkeypatch.setattr(alc.signal, "signal", lambda *a, **k: None)
    # main() creates the runtime directories itself; never in this checkout's real var/.
    monkeypatch.setattr(alc, "ensure_var_dirs", Mock())
    monkeypatch.setattr(alc, "_LOG_TO_STDERR", False)  # main(--path) flips it; restored at teardown
    (tmp_path / "state").mkdir()
    (tmp_path / "media").mkdir()


def _cfg(**overrides) -> "alc.Config":
    base = dict(jellyfin_url="http://jf:8096", jellyfin_key="key", budget_min=20.0,
                max_wait_min=15.0, whisper_lid_model="small", whisper_threads=2)
    base.update(overrides)
    return alc.Config(**base)


class _Jellyfin:
    """Scripted now_playing_sessions; once exhausted it answers `then` forever (`None` = unknown).

    Each call appends "gate" to `events`.
    """

    def __init__(self, sequence=(), then=(), events=None):
        self._it = iter(sequence)
        self._then = then
        self.events = events if events is not None else []

    def now_playing_sessions(self, key):
        self.events.append("gate")
        try:
            return next(self._it)
        except StopIteration:
            return None if self._then is None else list(self._then)


def _stream(index, lang="eng", **extra):
    s = {"index": index, "codec_type": "audio", "tags": {}, "disposition": {}}
    if lang is not None:
        s["tags"]["language"] = lang
    s.update(extra)
    return s


def _info(*streams, duration=1200.0):
    return {"streams": list(streams), "format": {"duration": str(duration)}}


def _lid(script):
    """Fake `detect_language`: `script` maps audio_index -> (lang, prob) results in call order.

    A stream whose script runs out keeps repeating its last result; an unscripted stream raises KeyError.
    """
    its = {k: iter(v) for k, v in script.items()}
    last: dict = {}
    calls = []

    def fake(video, idx, start, dur, model, threads, workdir, *, should_abort=None):
        calls.append({"idx": idx, "start": start, "dur": dur, "model": model,
                      "threads": threads, "workdir": workdir})
        try:
            last[idx] = next(its[idx])
        except StopIteration:
            pass
        return last[idx]

    fake.calls = calls
    return fake


def _lid_forever(result):
    def fake(*a, should_abort=None, **k):
        return result
    return fake


def _raises(exc_type, *args):
    """A side effect raising a FRESH exception each call: a re-raised instance keeps the earlier
    traceback, whose frames keep `main()` (and its singleton lock) alive into the next run."""
    def fail(*a, **k):
        raise exc_type(*args)
    return fail


def _lookup(original):
    """What `original_language_lookup` returns for a reachable Arr."""
    return (original, "known" if original else "unknown")


def _analyze(info, script, original=None, cfg=None, jellyfin=None, probe_duration_value=None):
    fake = _lid(script)
    with patch.object(alc, "detect_language", fake), \
         patch.object(alc, "probe_duration", return_value=probe_duration_value):
        out = alc.analyze_candidate(
            Path("/m/movies/X/x.mkv"), info, original, cfg or _cfg(), jellyfin or _Jellyfin(),
            alc.PlaybackWaitBudget(15.0), Path("/work"))
    return out, fake


def _types(findings):
    return [f["type"] for f in findings]


# well-formed findings, as the script writes them
NO_ENGLISH = [{"type": "NO_ENGLISH_AUDIO", "original_language": "eng",
               "streams": [{"index": 1, "tag": "spa", "detected_language": "es",
                            "probability": 0.95}]}]
MISLABEL = [{"type": "MISLABELED", "stream_index": 1, "tag": "eng",
             "detected_language": "es", "probability": 0.85}]


def _now():
    return time.time()


def _make_video(root: Path, rel: str, age_s: float, now: float = NOW, size: int = 10) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    os.utime(p, (now - age_s, now - age_s))
    return p


def _replace(video, content=b"a different, longer release", age=5 * HOUR):
    """A new file at the same path: other size and mtime, so another identity."""
    video.write_bytes(content)
    os.utime(video, (_now() - age, _now() - age))


def _state():
    return json.loads(alc.STATE_FILE.read_text())


def _entry(video):
    return _state()[str(video.resolve())]


def _jsonl():
    return [json.loads(line) for line in alc.JSONL_FILE.read_text().splitlines()]


def _seed_finding(video, findings, checked_at="t0", identity=None):
    """Record a CHECKED conclusion with `findings` through the production writer."""
    ident = alc._identity(video) if identity is None else identity
    assert alc._record_result(video, ident, {"result": "CHECKED", "findings": findings},
                              checked_at)


def _findings_view():
    """{path: {findings, first_found, alerted, identity}} for every state entry with findings."""
    if not alc.STATE_FILE.exists():
        return {}
    return {k: {f: e[f] for f in ("findings", "first_found", "alerted", "identity") if f in e}
            for k, e in _state().items() if e.get("findings")}


class _World:
    """Every boundary `main()` touches, mocked, with named handles.

    Defaults: one Spanish-tagged stream heard as Spanish, an English-original title, a idle
    Jellyfin, Telegram credentials in the environment and a Telegram that accepts the message
    (so a library file is a NO_ENGLISH_AUDIO finding). `run(**env)` overrides or, given
    `None`, removes an environment variable.
    """

    def __init__(self, tmp_path, monkeypatch):
        self.tmp_path = tmp_path
        self.probe = Mock(return_value=_info(_stream(1, "spa")))
        self.lookup = Mock(return_value=_lookup("eng"))
        self.detect = Mock(return_value=("es", 0.95))
        self.send = Mock(return_value=True)
        self.jellyfin = _Jellyfin()
        monkeypatch.setattr(alc, "JellyfinClient", lambda url: self.jellyfin)
        monkeypatch.setattr(alc, "probe", self.probe)
        monkeypatch.setattr(alc, "original_language_lookup", self.lookup)
        monkeypatch.setattr(alc, "detect_language", self.detect)
        monkeypatch.setattr(alc, "telegram_send", self.send)

    def video(self, rel="movies/One (2000)/one.mkv", age=5 * HOUR):
        return _make_video(self.tmp_path / "media", rel, age, now=_now())

    def library(self):
        """(older, newer): two settled library files."""
        return (self.video("movies/Older (1999)/older.mkv", 5 * HOUR),
                self.video("tv/Newer/Season 01/newer.mkv", 2 * HOUR))

    def finding_video(self, n=0):
        return self.video(f"movies/Film {n:02d} (2000)/Film {n:02d}.mkv", HOUR)

    def env(self, **overrides):
        env = {"HOME": str(self.tmp_path), "TELEGRAM_ENV": str(self.tmp_path / "no-such.env"),
               "TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42", "JELLYFIN_API_KEY": "key",
               **overrides}
        return {k: v for k, v in env.items() if v is not None}

    def run(self, *argv, **env):
        with patch.object(sys, "argv", ["audio-lang-check.py", *argv]), \
             patch.dict(os.environ, self.env(**env), clear=True):
            return alc.main()

    def alert(self, **env):
        with patch.dict(os.environ, self.env(**env), clear=True):
            alc.send_alerts()

    def messages(self):
        return [c.args[2] for c in self.send.call_args_list]


@pytest.fixture
def world(tmp_path, monkeypatch):
    return _World(tmp_path, monkeypatch)


# --- MISLABELED needs two confident windows ---------------------------------

def test_mislabeled_when_two_windows_agree_on_a_foreign_language():
    (findings, streams, result, _), fake = _analyze(
        _info(_stream(1, "eng")), {1: [("es", 0.9), ("es", 0.85)]}, original=None)
    assert result == "CHECKED"
    assert _types(findings) == ["MISLABELED"]
    f = findings[0]
    assert (f["stream_index"], f["tag"], f["detected_language"]) == (1, "eng", "es")
    assert f["probability"] == 0.85  # the lower of the two agreeing windows
    assert len(fake.calls) == 4      # every planned window is heard before the verdict
    assert streams[0]["mislabeled_as"] == "es"


@pytest.mark.parametrize("script", [
    [("es", 0.95), ("en", 0.9)],                       # second window says English
    [("es", 0.7), ("es", 0.7), ("en", 0.2), ("en", 0.2)],    # confident (>= 0.6) but never >= 0.8
    [("es", 0.95), ("fr", 0.95), ("de", 0.9), ("it", 0.9)],  # confident windows, DIFFERENT languages
])
def test_single_confident_foreign_window_is_not_mislabeled(script):
    (findings, _, _, _), _ = _analyze(_info(_stream(1, "eng")), {1: script})
    assert findings == []


@pytest.mark.parametrize("script", [
    pytest.param([("es", 0.95), ("es", 0.3), ("es", 0.9)], id="confident_inconclusive_confirming"),
    pytest.param([("es", 0.3), ("es", 0.9), ("es", 0.9)], id="inconclusive_confident_confirming"),
])
def test_an_inconclusive_window_falls_through_to_a_third_that_can_corroborate(script):
    (findings, streams, result, _), fake = _analyze(_info(_stream(1, "eng")), {1: script})
    assert [c["start"] for c in fake.calls] == [300.0, 600.0, 120.0, 900.0]
    assert _types(findings) == ["MISLABELED"] and result == "CHECKED"
    assert findings[0]["probability"] == 0.9
    assert streams[0]["mislabeled_as"] == "es"


@pytest.mark.parametrize("script, calls", [
    pytest.param([("en", 0.9)], 1, id="first_window"),
    pytest.param([("es", 0.9), ("en", 0.8)], 2, id="after_a_foreign_window"),
])
def test_a_confident_english_window_is_heard_and_ends_the_scan(script, calls):
    (findings, _, _, _), fake = _analyze(_info(_stream(1, "eng")), {1: script})
    assert len(fake.calls) == calls and findings == []


def test_windows_are_25_50_10_75_percent_of_duration_30s_each_in_given_workdir():
    script = [("en", 0.2), ("en", 0.3), ("en", 0.4), ("en", 0.5)]
    _, fake = _analyze(_info(_stream(1, None), duration=2000.0), {1: script})
    assert [c["start"] for c in fake.calls] == [500.0, 1000.0, 200.0, 1500.0]
    assert {c["dur"] for c in fake.calls} == {30.0}
    assert {c["workdir"] for c in fake.calls} == {Path("/work")}
    assert {c["model"] for c in fake.calls} == {"small"}
    assert {c["threads"] for c in fake.calls} == {2}


def test_regional_english_tag_counts_as_tagged_english_for_mislabeled():
    (findings, _, _, _), _ = _analyze(
        _info(_stream(1, "en-US")), {1: [("ja", 0.9), ("ja", 0.9)]})
    assert _types(findings) == ["MISLABELED"]


def test_untagged_stream_is_never_mislabeled():
    (findings, _, _, _), _ = _analyze(
        _info(_stream(1, None)), {1: [("ja", 0.99), ("ja", 0.99)]}, original=None)
    assert findings == []


# --- UNVERIFIED --------------------------------------------------------------

@pytest.mark.parametrize("tag, script", [
    pytest.param("und", [("en", 0.3), ("fr", 0.4), ("en", 0.2), ("en", 0.1)], id="und_tag_all_inconclusive"),
    pytest.param("spa", [("es", 0.2), ("es", 0.3), ("es", 0.1), ("es", 0.2)], id="foreign_tag_all_inconclusive"),
    pytest.param("spa", [("es", 0.95), ("es", 0.4), ("es", 0.9)], id="foreign_tag_one_inconclusive_among_confident"),
])
def test_a_stream_that_stays_unclear_is_unverified_not_a_finding(tag, script):
    """A foreign tag is not evidence the audio is foreign: nothing confirmed plus an unclear scan is no finding."""
    (findings, streams, result, note), fake = _analyze(_info(_stream(1, tag)), {1: script}, original="eng")
    assert result == "UNVERIFIED"
    assert findings == []
    assert note and len(fake.calls) == 4
    assert streams[0]["counts_as_english"] is False


# --- NO_ENGLISH_AUDIO ----------------------------------------------------------

@pytest.mark.parametrize("script", [
    pytest.param([("es", 0.95), ("es", 0.9)], id="same_language"),
    pytest.param([("es", 0.95), ("fr", 0.9), ("de", 0.9), ("it", 0.9)], id="across_languages"),
])
def test_no_english_audio_when_original_is_english_and_nothing_is_english(script):
    (findings, _, result, _), fake = _analyze(_info(_stream(1, "spa")), {1: script}, original="eng")
    assert result == "CHECKED"
    assert findings == [{"type": "NO_ENGLISH_AUDIO", "original_language": "eng",
                         "streams": [{"index": 1, "tag": "spa", "detected_language": "es",
                                      "probability": 0.95}]}]
    assert len(fake.calls) == 4


@pytest.mark.parametrize("original", [None, "jpn", "kor"])
def test_non_english_or_unknown_originals_never_produce_no_english_audio(original):
    (findings, _, result, _), _ = _analyze(
        _info(_stream(1, "spa")), {1: [("es", 0.95), ("es", 0.9)]}, original=original)
    assert findings == []
    assert result == "CHECKED"


def test_spanish_tagged_stream_detected_as_english_is_not_a_finding():
    """A stream tagged `spa` that whisper confidently hears as English is English; the tag never outweighs a confident detection."""
    (findings, streams, result, _), _ = _analyze(
        _info(_stream(1, "spa")), {1: [("en", 0.92)]}, original="eng")
    assert findings == [] and result == "CHECKED"
    assert streams[0]["counts_as_english"] is True


def test_foreign_tag_detected_english_below_the_raw_floor_does_not_count():
    script = [("en", 0.59), ("en", 0.5), ("en", 0.4), ("en", 0.3)]
    (findings, _, result, _), _ = _analyze(_info(_stream(1, "spa")), {1: script}, original="eng")
    assert result == "UNVERIFIED" and findings == []


@pytest.mark.parametrize("tag, prob", [
    pytest.param(None, 0.9, id="untagged"),
    pytest.param("und", 0.65, id="und"),
])
def test_untagged_or_und_stream_detected_english_counts_as_english(tag, prob):
    (findings, streams, result, _), _ = _analyze(
        _info(_stream(1, tag)), {1: [("en", prob)]}, original="eng")
    assert findings == []
    assert result == "CHECKED"
    assert streams[0]["counts_as_english"] is True


def test_tagged_english_with_inconclusive_lid_still_counts_as_english():
    (findings, _, result, _), _ = _analyze(
        _info(_stream(1, "eng")), {1: [("en", 0.2), ("de", 0.3), ("en", 0.4), ("en", 0.1)]}, original="eng")
    assert findings == []
    assert result == "CHECKED"


def test_mislabeled_english_tag_does_not_count_as_english_for_no_english_audio():
    (findings, _, _, _), _ = _analyze(
        _info(_stream(1, "eng")), {1: [("es", 0.9), ("es", 0.9)]}, original="eng")
    assert _types(findings) == ["MISLABELED", "NO_ENGLISH_AUDIO"]


def test_a_second_genuine_english_stream_prevents_no_english_audio():
    (findings, _, _, _), _ = _analyze(
        _info(_stream(1, "spa"), _stream(2, "eng")),
        {1: [("es", 0.95), ("es", 0.9)], 2: [("en", 0.95)]}, original="eng")
    assert findings == []


def test_commentary_and_audio_description_tracks_are_ignored():
    commentary = _stream(1, "eng", disposition={"comment": 1})
    described = _stream(2, "eng", tags={"language": "eng", "title": "Audio Description"})
    (findings, streams, _, _), fake = _analyze(
        _info(commentary, described, _stream(3, "spa")),
        {3: [("es", 0.95), ("es", 0.9)]}, original="eng")
    assert [s["index"] for s in streams] == [3]
    assert {c["idx"] for c in fake.calls} == {3}  # never even scanned
    assert _types(findings) == ["NO_ENGLISH_AUDIO"]  # English commentary doesn't count


def test_file_with_no_audio_streams_is_unverified_not_no_english_audio():
    """A video with nothing to listen to is never a finding and gets no duration probe."""
    (findings, streams, result, note), fake = _analyze(_info(), {}, original="eng")
    assert streams == [] and fake.calls == [] and findings == []
    assert result == "UNVERIFIED" and "no non-commentary audio" in note


# --- process_candidate -----------------------------------------------------------

def _process(detect=None, *, probe=None, lookup=None, jf=None, cfg=None):
    """`process_candidate` on /v.mkv; defaults: one English-tagged stream, Arr answer unknown, `detect_language` a bare Mock."""
    probe = Mock(return_value=_info(_stream(1, "eng"))) if probe is None else probe
    lookup = _lookup(None) if lookup is None else lookup
    with patch.object(alc, "probe", probe), \
         patch.object(alc, "original_language_lookup", return_value=lookup), \
         patch.object(alc, "detect_language", Mock() if detect is None else detect):
        return alc.process_candidate(Path("/v.mkv"), cfg or _cfg(), jf or _Jellyfin(), Path("/w"))


def test_process_candidate_probe_failure():
    with patch.object(alc, "probe", return_value=None):  # the real Arr lookup runs: /v.mkv is outside the library
        rec = alc.process_candidate(Path("/v.mkv"), _cfg(), _Jellyfin(), Path("/w"))
    assert rec["result"] == "PROBE_FAILED" and rec["findings"] == []


def test_process_candidate_unexpected_exception_is_recorded_as_error():
    rec = _process(Mock(side_effect=RuntimeError("boom")))
    assert rec["result"] == "ERROR" and "boom" in rec["error"] and rec["findings"] == []


def test_process_candidate_lets_playback_wait_timeout_propagate():
    with pytest.raises(alc.PlaybackWaitTimeout):
        _process(Mock(side_effect=alc.PlaybackWaitTimeout("x")))


def test_extraction_waits_while_playing_then_retries(monkeypatch):
    import media_stack.playback as pb
    sleeps = []
    monkeypatch.setattr(pb.time, "sleep", lambda s: sleeps.append(s))
    results = iter([ExtractionInterrupted("playing"), ("en", 0.9)])

    def fake(*a, should_abort=None, **k):
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    # initial gate: idle; interrupted extraction -> wait: busy once, then idle
    jf = _Jellyfin([[], [{"NowPlayingItem": {"Name": "x"}}], []])
    rec = _process(fake, probe=Mock(return_value=_info(_stream(1, None))), lookup=_lookup("eng"), jf=jf)
    assert rec["result"] == "CHECKED" and rec["findings"] == []
    assert len(sleeps) == 1  # waited out the one busy poll


class _Clock:
    def __init__(self):
        self.t = 0.0


def _fake_clock(monkeypatch):
    import media_stack.playback as pb
    clock = _Clock()
    monkeypatch.setattr(pb.time, "monotonic", lambda: clock.t)
    monkeypatch.setattr(pb.time, "sleep", lambda s: setattr(clock, "t", clock.t + s))
    return clock


def test_unknown_playback_state_fails_closed_before_the_file_is_even_probed(monkeypatch):
    """`None` from now_playing_sessions is busy: with the budget exhausted, PlaybackWaitTimeout and neither ffprobe nor whisper touched the file."""
    _fake_clock(monkeypatch)
    jf = _Jellyfin(then=None)  # Jellyfin unreachable, forever
    probe, detect = Mock(), Mock()
    with pytest.raises(alc.PlaybackWaitTimeout):
        _process(detect, probe=probe, jf=jf, cfg=_cfg(max_wait_min=2.0))
    probe.assert_not_called()
    detect.assert_not_called()


def test_ffprobe_runs_only_after_the_gate_says_idle(monkeypatch):
    """Order is gate(busy) -> wait -> gate(idle) -> probe, and the wait is charged to the per-file budget."""
    import media_stack.playback as pb
    events = []
    monkeypatch.setattr(pb.time, "sleep", lambda s: events.append("sleep"))
    jf = _Jellyfin([[{"NowPlayingItem": {"Name": "x"}}], []], events=events)

    def fake_probe(p):
        events.append("probe")
        return _info(_stream(1, "eng"))

    rec = _process(_lid_forever(("en", 0.9)), probe=fake_probe, jf=jf)
    assert rec["result"] == "CHECKED"
    assert events[:4] == ["gate", "sleep", "gate", "probe"]


def test_no_gate_check_precedes_the_probe_when_arr_is_unavailable():
    """The Arr lookup touches no media so it may come first; an unavailable Arr stops the candidate before any file read."""
    probe, detect = Mock(), Mock()
    rec = _process(detect, probe=probe, lookup=(None, "unavailable"))
    assert rec["result"] == "ARR_UNAVAILABLE"
    probe.assert_not_called()
    detect.assert_not_called()


@pytest.mark.parametrize("answer", [("eng", "known"), (None, "unknown")])
def test_known_and_unknown_arr_answers_are_both_usable(answer):
    assert _process(_lid_forever(("en", 0.9)), lookup=answer)["result"] == "CHECKED"


def test_missing_whisper_dependency_is_dependency_missing_not_a_cached_result():
    rec = _process(Mock(side_effect=ModuleNotFoundError("faster_whisper")))
    assert rec["result"] == "DEPENDENCY_MISSING"
    assert rec["result"] not in alc.CONCLUSION_RESULTS and "faster_whisper" in rec["error"]


def test_the_no_english_audio_finding_the_script_writes_lists_every_stream(world):
    video = world.video()
    world.probe.return_value = _info(_stream(1, "spa"), _stream(2, None))
    world.detect.side_effect = lambda video, idx, *a, should_abort=None, **k: {
        1: ("es", 0.951234), 2: ("fr", 0.9)}[idx]
    world.run()
    finding = _entry(video)["findings"][0]
    assert alc._finding_problem(finding) is None
    assert finding["streams"] == [
        {"index": 1, "tag": "spa", "detected_language": "es", "probability": 0.951},
        {"index": 2, "tag": None, "detected_language": "fr", "probability": 0.9}]
    assert alc.entry_problem(_entry(video)) is None


def test_a_stream_less_file_is_recorded_as_unverified_without_alerting(world):
    video = world.video()
    world.probe.return_value = _info()
    world.run()
    assert _entry(video)["result"] == "UNVERIFIED" and _entry(video)["findings"] == []
    world.send.assert_not_called()


def test_a_title_unknown_to_arr_is_checked_and_not_revisited_until_the_file_changes(world):
    video = world.video()
    world.lookup.return_value = _lookup(None)   # Arr is reachable but has no record yet
    world.run()
    assert _entry(video)["result"] == "CHECKED" and _entry(video)["findings"] == []
    world.lookup.return_value = _lookup("eng")   # Radarr learns the title later
    world.run()
    assert world.lookup.call_count == 1 and world.probe.call_count == 1
    assert _entry(video)["findings"] == [] and world.send.call_count == 0
    _replace(video)   # a new version of the file is checked against what Arr says now
    world.run()
    assert _entry(video)["findings"] == NO_ENGLISH and world.send.call_count == 1


def test_workdir_is_under_workdir_root_and_removed_even_when_the_check_errors(world):
    world.library()
    seen = []

    def detect(video, idx, start, dur, model, threads, workdir, *, should_abort=None):
        seen.append(workdir)
        raise RuntimeError("whisper exploded")

    world.probe.return_value = _info(_stream(1, "eng"))
    world.detect.side_effect = detect
    assert world.run() == 0
    assert seen and all(alc.WORKDIR_ROOT == w.parent for w in seen)
    assert not list(alc.WORKDIR_ROOT.iterdir())

# --- candidate selection ---------------------------------------------------------

def _make_video(root: Path, rel: str, age_s: float, now: float = NOW, size: int = 10) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    os.utime(p, (now - age_s, now - age_s))
    return p


def test_select_candidates_newest_first_and_only_mkv_mp4_under_movies_and_tv(tmp_path):
    root = tmp_path / "media"
    old = _make_video(root, "movies/A (2000)/a.mkv", 3 * HOUR)
    mid = _make_video(root, "tv/Show/Season 01/e.mp4", 2 * HOUR)
    new = _make_video(root, "movies/B (2001)/b.MKV", 1 * HOUR)
    _make_video(root, "movies/B (2001)/b.en.srt", HOUR)
    _make_video(root, "movies/B (2001)/b.avi", HOUR)
    _make_video(root, "music/c.mkv", HOUR)  # outside movies/ and tv/
    assert alc.select_candidates({}, root, NOW) == [new, mid, old]


def test_select_candidates_excludes_hidden_names_and_dupe_recycle(tmp_path):
    root = tmp_path / "media"
    keep = _make_video(root, "movies/A/a.mkv", HOUR)
    _make_video(root, "movies/A/.hidden.mkv", HOUR)
    _make_video(root, "movies/.hidden-dir/h.mkv", HOUR)
    _make_video(root, "movies/.dupe-recycle/A/old.mkv", HOUR)
    _make_video(root, "tv/.dupe-recycle/S/old.mkv", HOUR)
    assert alc.select_candidates({}, root, NOW) == [keep]


def test_select_candidates_skips_files_modified_under_ten_minutes_ago(tmp_path):
    root = tmp_path / "media"
    _make_video(root, "movies/Fresh/f.mkv", 9 * 60 + 59)
    settled = _make_video(root, "movies/Settled/s.mkv", 10 * 60 + 1)
    assert alc.select_candidates({}, root, NOW) == [settled]


def test_select_candidates_skips_unchanged_identity_and_rechecks_on_change(tmp_path):
    root = tmp_path / "media"
    video = _make_video(root, "movies/A/a.mkv", HOUR)
    state = {str(video.resolve()): {"identity": alc._identity(video), "result": "CHECKED"}}
    assert alc.select_candidates(state, root, NOW) == []
    # a re-encode changes size and mtime
    video.write_bytes(b"y" * 99)
    os.utime(video, (NOW - 2 * HOUR, NOW - 2 * HOUR))
    assert alc.select_candidates(state, root, NOW) == [video]


def test_select_candidates_with_missing_media_dirs_is_empty(tmp_path):
    assert alc.select_candidates({}, tmp_path / "does-not-exist", NOW) == []


def test_select_candidates_skips_a_file_that_failed_the_attempt_bound_times_in_this_version(tmp_path):
    root = tmp_path / "media"
    video = _make_video(root, "movies/A/a.mkv", HOUR)
    ident, key = alc._identity(video), str(video.resolve())

    def state(identity, attempts):
        return {key: {"failed": {"identity": identity, "attempts": attempts, "last_result": "ERROR",
                                 "last_error": None}}}

    assert alc.select_candidates(state(ident, alc.MAX_FAILED_ATTEMPTS), root, NOW) == []
    assert alc.select_candidates(state(ident, alc.MAX_FAILED_ATTEMPTS - 1), root, NOW) == [video]
    assert alc.select_candidates(state([0, 0, 0], alc.MAX_FAILED_ATTEMPTS), root, NOW) == [video]


# --- independent windows, durations -------------------------------------------

def _w(start, lang, prob):
    return {"fraction": 0, "start": start, "language": lang, "probability": prob}


def test_mislabeled_needs_the_agreeing_windows_at_least_60s_apart():
    assert alc._mislabeled_language([_w(100.0, "es", 0.9), _w(130.0, "es", 0.9)]) is None
    assert alc._mislabeled_language([_w(100.0, "es", 0.9), _w(159.9, "es", 0.9)]) is None
    assert alc._mislabeled_language([_w(100.0, "es", 0.9), _w(160.0, "es", 0.85)]) == ("es", 0.85)


def test_file_under_three_minutes_skips_mislabeled_and_is_unverified():
    (findings, streams, result, note), fake = _analyze(
        _info(_stream(1, "eng"), duration=179.0), {})
    assert fake.calls == []  # nothing to learn, so the English-tagged stream is not scanned
    assert findings == [] and result == "UNVERIFIED" and "MISLABELED" in note
    assert streams[0]["mislabel_skipped"] is True


def test_short_file_without_an_english_tag_is_still_checked_for_no_english_audio():
    (findings, _, result, _), _ = _analyze(
        _info(_stream(1, "spa"), duration=120.0), {1: [("es", 0.9), ("es", 0.9)]}, original="eng")
    assert _types(findings) == ["NO_ENGLISH_AUDIO"] and result == "CHECKED"


def test_duration_falls_back_to_a_bounded_ffprobe_call_made_after_the_gate():
    events = []
    jf = _Jellyfin(events=events)
    info = {"streams": [_stream(1, None)], "format": {}}
    fake = _lid({1: [("en", 0.9)]})

    def fake_probe_duration(p, audio_index=None, allowed_indices=None):
        events.append("probe_duration")
        return 800.0

    with patch.object(alc, "detect_language", fake), \
         patch.object(alc, "probe_duration", fake_probe_duration):
        alc.analyze_candidate(Path("/m/movies/X/x.mkv"), info, None, _cfg(), jf,
                              alc.PlaybackWaitBudget(15.0), Path("/work"))
    assert events[:2] == ["gate", "probe_duration"]
    assert fake.calls[0]["start"] == 200.0


_COVER_ART = {"index": 0, "codec_type": "video", "duration": "5.0"}


@pytest.mark.parametrize("info", [
    *(pytest.param({"streams": [_stream(1, "eng", duration=bad)], "format": {"duration": bad}},
                   id=f"bad_{bad}") for bad in ["N/A", "0"]),
    pytest.param({"streams": [_COVER_ART, _stream(1, "eng")], "format": {}},
                 id="only_a_video_stream_has_a_duration"),
])
def test_no_usable_duration_anywhere_is_unverified_and_samples_no_window(info):
    (findings, streams, result, note), fake = _analyze(info, {}, original="eng")
    assert fake.calls == [] and findings == [] and streams == []
    assert result == "UNVERIFIED" and "duration" in note


@pytest.mark.parametrize("info, script, expected_start", [
    pytest.param({"streams": [_stream(1, None, duration="2000.0")], "format": {"duration": "N/A"}},
                 {1: [("en", 0.9)]}, 500.0, id="audio_stream_when_the_container_has_none"),
    # a 5 s cover-art video and a 1,400 s audio stream give 1,400 (windows at 25% = 350)
    pytest.param({"streams": [{**_COVER_ART, "disposition": {"attached_pic": 1}},
                              _stream(1, None, duration="1400.0")], "format": {}},
                 {1: [("en", 0.9)]}, 350.0, id="cover_art_video_stream_is_never_used"),
    pytest.param({"streams": [_stream(1, "eng", disposition={"comment": 1}, duration="90000"),
                              _stream(2, "eng", duration="1200")], "format": {}},
                 {2: [("en", 0.9)]}, 300.0, id="commentary_stream_is_not_a_source"),
])
def test_duration_comes_from_the_first_scanned_audio_stream_that_has_one(info, script, expected_start):
    _, fake = _analyze(info, script)
    assert fake.calls[0]["start"] == expected_start


# --- classify on ALL windows, not the best one ---------------------------


@pytest.mark.parametrize("probs,expected", [
    ([("es", 0.95), ("en", 0.7)], "english"),
    ([("es", 0.95), ("fr", 0.9)], "foreign"),
    ([("es", 0.95), ("es", 0.59)], "unclear"),
])
def test_classify_windows_looks_at_every_window(probs, expected):
    windows = [_w(float(i * 100), lang, p) for i, (lang, p) in enumerate(probs)]
    assert alc._classify_windows(windows) == expected


# --- an inconclusive window must not stop the scan ---------------------------

@pytest.mark.parametrize("tag, original", [
    pytest.param("eng", None, id="english_tag"),
    pytest.param("spa", "eng", id="foreign_tag_on_english_original"),
])
def test_two_agreeing_foreign_windows_then_an_english_one_is_english_with_no_finding(tag, original):
    """The scan does not stop at an agreeing foreign pair; a third English window means conflicting evidence, not MISLABELED or NO_ENGLISH_AUDIO."""
    (findings, streams, result, _), fake = _analyze(
        _info(_stream(1, tag)), {1: [("es", 0.9), ("es", 0.9), ("en", 0.8)]}, original=original)
    assert len(fake.calls) == 3
    assert findings == [] and result == "CHECKED"
    assert streams[0]["counts_as_english"] is True and "mislabeled_as" not in streams[0]


# --- raw probabilities, rounding only for display --------------------------------

def test_mislabeled_bar_uses_the_raw_probability():
    """0.7996 displays as 0.8 but is below the bar."""
    (findings, _, _, _), _ = _analyze(_info(_stream(1, "eng")), {1: [("es", 0.7996)] * 4})
    assert findings == []


# --- load_config ---------------------------------------------------------------------

def test_load_config_defaults():
    with patch.object(alc.os, "cpu_count", return_value=4):
        cfg = alc.load_config({})
    assert (cfg.budget_min, cfg.max_wait_min) == (20.0, 15.0)
    assert cfg.whisper_threads == 2  # idle-priority: min(2, cpu_count)
    assert cfg.whisper_lid_model == "small"
    assert cfg.jellyfin_key == ""


@pytest.mark.parametrize("cpus,expected", [(1, 1), (8, 2)])
def test_whisper_threads_default_never_exceeds_the_cpu_count(cpus, expected):
    with patch.object(alc.os, "cpu_count", return_value=cpus):
        assert alc.load_config({}).whisper_threads == expected


@pytest.mark.parametrize("key,default", [("AUDIO_CHECK_BUDGET_MIN", 20.0),
                                         ("AUDIO_CHECK_MAX_WAIT_MIN", 15.0)])
@pytest.mark.parametrize("bad", ["0"])
def test_minute_settings_fall_back_to_the_default_with_a_warning(key, default, bad, capsys):
    cfg = alc.load_config({key: bad})
    assert getattr(cfg, "budget_min" if "BUDGET" in key else "max_wait_min") == default
    assert key in capsys.readouterr().out


@pytest.mark.parametrize("key,attr", [("AUDIO_CHECK_BUDGET_MIN", "budget_min"),
                                      ("AUDIO_CHECK_MAX_WAIT_MIN", "max_wait_min")])
def test_minute_settings_accept_valid_values_up_to_exactly_1440(key, attr):
    assert getattr(alc.load_config({key: "7.5"}), attr) == 7.5
    assert getattr(alc.load_config({key: "1440"}), attr) == 1440.0


def test_lid_model_and_jellyfin_settings_are_read_from_env():
    cfg = alc.load_config({"WHISPER_LID_MODEL": "medium", "JELLYFIN_API_KEY": "k",
                           "JELLYFIN_URL": "http://jf:1"})
    assert (cfg.whisper_lid_model, cfg.jellyfin_key, cfg.jellyfin_url) == ("medium", "k", "http://jf:1")


# --- the one state file --------------------------------------------------------------

def test_load_state_missing_corrupt_and_wrongly_shaped(tmp_path):
    f = tmp_path / "s.json"
    assert alc._load_state(f) == {}
    f.write_text("{not json")
    assert alc._load_state(f) is None
    f.write_text("[1, 2]")
    assert alc._load_state(f) is None
    f.write_text('{"a": {}}')
    assert alc._load_state(f) == {"a": {}}


def test_record_result_round_trips_the_documented_entry_shape(tmp_path):
    video = _make_video(tmp_path / "media", "movies/A/a.mkv", HOUR)
    rec = {"result": "CHECKED", "findings": NO_ENGLISH}
    assert alc._record_result(video, alc._identity(video), rec, "2026-10-13T00:00:00+00:00")
    st = video.stat()
    assert _entry(video) == {
        "identity": [st.st_ino, st.st_size, st.st_mtime_ns], "result": "CHECKED",
        "findings": NO_ENGLISH, "checked_at": "2026-10-13T00:00:00+00:00",
        "first_found": "2026-10-13T00:00:00+00:00", "alerted": False}


def test_a_conclusion_without_findings_has_no_first_found_or_alerted(tmp_path):
    video = _make_video(tmp_path / "media", "movies/A/a.mkv", HOUR)
    assert alc._record_result(video, alc._identity(video), {"result": "UNVERIFIED", "findings": []}, "t")
    assert _entry(video) == {"identity": alc._identity(video), "result": "UNVERIFIED",
                             "findings": [], "checked_at": "t"}


def test_state_write_failure_is_logged_and_never_crashes(tmp_path, capsys):
    video = _make_video(tmp_path / "media", "movies/A/a.mkv", HOUR)
    with patch.object(alc, "mutate_state", side_effect=OSError("disk full")):
        assert alc._record_result(video, [1, 2, 3], {"result": "CHECKED", "findings": []}, "t") is False
        alc._record_failure(video, [1, 2, 3], {"result": "ERROR"})
    assert capsys.readouterr().out.count("WARNING") == 2


def test_a_conclusion_replaces_the_whole_entry_including_a_failure_record_and_keys_of_older_versions(tmp_path):
    video = _make_video(tmp_path / "media", "movies/A/a.mkv", HOUR)
    ident = alc._identity(video)
    alc.STATE_FILE.write_text(json.dumps({str(video.resolve()): {
        "identity": ident, "result": "CHECKED", "findings": [], "checked_at": "t0",
        "original_status": "unknown", "original_language": None, "streams": [],
        "original_rechecked_at": "t0",
        "retry": {"identity": ident, "attempts": 2, "next_retry_at": 5.0},
        "failed": {"identity": ident, "attempts": 2, "last_result": "ERROR", "last_error": None}}}))
    assert alc._record_result(video, ident, {"result": "CHECKED", "findings": []}, "t")
    assert _entry(video) == {"identity": ident, "result": "CHECKED", "findings": [], "checked_at": "t"}


def test_record_result_keeps_alerted_only_for_the_same_identity_and_findings(tmp_path):
    video = _make_video(tmp_path / "media", "movies/A/a.mkv", HOUR)

    def mark_alerted():
        state = _state()
        state[str(video.resolve())]["alerted"] = True
        alc.STATE_FILE.write_text(json.dumps(state))

    _seed_finding(video, MISLABEL, "t0", [1, 2, 3])
    _seed_finding(video, [dict(MISLABEL[0])], "t1", [1, 2, 3])   # same file, same findings, still unsent
    assert _findings_view()[str(video.resolve())] == {
        "findings": MISLABEL, "first_found": "t0", "alerted": False, "identity": [1, 2, 3]}
    mark_alerted()
    _seed_finding(video, [dict(MISLABEL[0])], "t2", [1, 2, 3])   # ... and now already sent
    assert _entry(video)["alerted"] is True and _entry(video)["first_found"] == "t0"
    _seed_finding(video, [dict(MISLABEL[0])], "t3", [9, 9, 9])   # a NEW file with the same findings
    assert _entry(video)["alerted"] is False and _entry(video)["first_found"] == "t3"
    mark_alerted()
    _seed_finding(video, NO_ENGLISH, "t4", [9, 9, 9])   # CHANGED findings of the same file
    assert _entry(video)["alerted"] is False and _entry(video)["first_found"] == "t3"


def test_a_failure_record_leaves_an_earlier_conclusion_and_its_findings_alone(tmp_path):
    video = _make_video(tmp_path / "media", "movies/A/a.mkv", HOUR)
    _seed_finding(video, MISLABEL, "t0", [1, 2, 3])
    before = _entry(video)
    alc._record_failure(video, alc._identity(video), {"result": "PROBE_FAILED"})
    after = _entry(video)
    assert {k: v for k, v in after.items() if k != "failed"} == before
    assert after["failed"]["attempts"] == 1


def test_the_script_never_records_what_it_would_not_load(tmp_path):
    video = _make_video(tmp_path / "media", "movies/A/a.mkv", HOUR)
    ident = alc._identity(video)
    for bad in (sorted(alc.FAILED_RESULTS) + ["ARR_UNAVAILABLE", "???"]):   # not a conclusion
        assert alc._record_result(video, ident, {"result": bad, "findings": []}, "t") is False
    assert alc._record_result(video, ident, {"result": "CHECKED", "findings": [
        {"type": "MISLABELED", "stream_index": 1}]}, "t") is False          # a malformed finding
    alc._record_failure(video, [1, 2], {"result": "ERROR"})                 # a malformed identity
    assert not alc.STATE_FILE.exists()                                      # nothing was written


# --- failed analyses: a few tries, never cached --------------------------------------

def test_a_probe_failure_is_counted_not_cached(world):
    video = world.video()
    world.probe.return_value = None
    assert world.run() == 0
    entry = _entry(video)
    assert "identity" not in entry and "result" not in entry   # NOT cached against the file
    assert entry["failed"] == {"identity": alc._identity(video), "attempts": 1,
                               "last_result": "PROBE_FAILED", "last_error": None}
    world.send.assert_not_called()
    assert _jsonl()[0]["result"] == "PROBE_FAILED"


def _make_it_fail(world, kind):
    if kind == "probe":
        world.probe.return_value = None
    else:
        world.probe.return_value = _info(_stream(1, "eng"))
        world.detect.side_effect = _raises(RuntimeError, "whisper exploded")


@pytest.mark.parametrize("kind", ["probe", "whisper"])
def test_a_failing_file_is_tried_every_run_until_the_attempt_bound_then_skipped(world, kind):
    video = world.video()
    _make_it_fail(world, kind)
    for n in range(1, alc.MAX_FAILED_ATTEMPTS + 1):
        assert world.run() == 0
        assert _entry(video)["failed"]["attempts"] == n
    assert world.run() == 0   # given up on this version of the file
    assert _entry(video)["failed"]["attempts"] == alc.MAX_FAILED_ATTEMPTS
    assert world.probe.call_count == alc.MAX_FAILED_ATTEMPTS
    if kind == "whisper":
        assert "RuntimeError" in _entry(video)["failed"]["last_error"]
    assert not list(alc.WORKDIR_ROOT.iterdir())   # per-file workdirs removed even on errors


def test_a_changed_file_gets_a_fresh_attempt_count(world):
    video = world.video()
    _make_it_fail(world, "probe")
    for _ in range(alc.MAX_FAILED_ATTEMPTS + 1):
        world.run()
    assert world.probe.call_count == alc.MAX_FAILED_ATTEMPTS
    _replace(video)   # a re-encode: new size and mtime
    world.run()
    assert world.probe.call_count == alc.MAX_FAILED_ATTEMPTS + 1
    failed = _entry(video)["failed"]
    assert failed["attempts"] == 1 and failed["identity"] == alc._identity(video)


def test_an_arr_outage_records_nothing_uses_no_attempts_and_reads_no_media(world, capsys):
    world.library()
    world.lookup.return_value = (None, "unavailable")
    for _ in range(alc.MAX_FAILED_ATTEMPTS + 2):
        assert world.run() == 0
    assert not alc.STATE_FILE.exists() and not alc.JSONL_FILE.exists()
    world.probe.assert_not_called()
    world.detect.assert_not_called()
    world.send.assert_not_called()
    assert "2 file(s) not checked: Radarr/Sonarr could not be asked" in capsys.readouterr().out
    world.lookup.return_value = _lookup("eng")   # Arr is back: both files are checked at once
    world.run()
    assert len(_state()) == 2 and world.probe.call_count == 2


def test_a_later_success_replaces_the_failure_record(world):
    video = world.video()
    world.probe.return_value = None
    world.run()
    assert "failed" in _entry(video)
    world.probe.return_value = _info(_stream(1, "eng"))
    world.detect.return_value = ("en", 0.9)
    world.run()
    entry = _entry(video)
    assert "failed" not in entry and entry["result"] == "CHECKED"
    assert entry["identity"] == alc._identity(video)


def test_missing_whisper_ends_the_run_with_exit_1_records_nothing_and_still_sends_pending_alerts(world):
    world.library()
    pending = world.finding_video()
    _seed_finding(pending, MISLABEL, "t0")   # an unsent finding of an earlier run
    world.probe.return_value = _info(_stream(1, "eng"))
    world.detect.side_effect = _raises(ModuleNotFoundError, "faster_whisper")
    assert world.run() == 1
    assert world.probe.call_count == 1   # the second file was never started
    assert set(_state()) == {str(pending.resolve())}
    assert _jsonl()[0]["result"] == "DEPENDENCY_MISSING"
    assert world.send.call_count == 1


# --- one locked write per update; a failed write records nothing ---------------------

def _spy_mutate(calls, fail=False):
    real = alc.mutate_state

    def spy(state_file, fn):
        calls.append(state_file)
        if fail:
            raise OSError("disk full")
        return real(state_file, fn)

    return spy


def test_failed_state_write_records_nothing_so_the_next_run_rechecks_and_alerts(world):
    video = world.video()
    with patch.object(alc, "mutate_state", _spy_mutate([], fail=True)):
        assert world.run() == 0   # still succeeds
    assert not alc.STATE_FILE.exists()
    world.send.assert_not_called()
    assert world.run() == 0
    assert world.probe.call_count == 2   # re-checked
    assert world.send.call_count == 1    # and alerted
    assert _entry(video)["result"] == "CHECKED"


# --- alerting: each new finding is sent once -----------------------------------------

def test_one_message_lists_new_findings_oldest_first_and_marks_them_alerted(world):
    v1, v2 = world.finding_video(1), world.finding_video(2)
    _seed_finding(v1, MISLABEL, "2026-10-02T00:00:00+00:00")
    _seed_finding(v2, NO_ENGLISH, "2026-10-01T00:00:00+00:00")
    world.alert()
    assert world.send.call_count == 1   # ONE message for both files
    token, chat, text = world.send.call_args.args
    assert (token, chat) == ("tok", "42")
    assert "Film 01 (2000) / Film 01" in text
    assert "stream 1 tagged eng but detected es (p=0.85)" in text
    assert "Film 02 (2000) / Film 02" in text and "no confirmed English audio" in text
    assert "1 tagged spa heard es (p=0.95)" in text
    assert text.index("Film 02") < text.index("Film 01")   # oldest first_found first, not by path
    assert all(e["alerted"] for e in _findings_view().values())


def test_a_finding_is_sent_once_and_only_a_new_or_changed_one_again(world):
    v = world.finding_video()
    key = str(v.resolve())
    _seed_finding(v, MISLABEL, "t1")
    world.alert()
    world.alert()
    assert world.send.call_count == 1
    _seed_finding(v, [dict(MISLABEL[0])], "t2")   # re-checked with IDENTICAL findings
    world.alert()
    assert world.send.call_count == 1
    _seed_finding(v, MISLABEL + NO_ENGLISH, "t3")   # CHANGED findings
    world.alert()
    assert world.send.call_count == 2
    assert _findings_view()[key]["first_found"] == "t1" and _findings_view()[key]["alerted"] is True


def test_a_replacement_file_is_analysed_on_its_own_merits_and_alerted(world):
    """A replacement carrying even the same defect is a different file and is alerted on its own."""
    video = world.video()
    _seed_finding(video, NO_ENGLISH, "t0", [1, 2, 3])   # the replaced file: already alerted
    state = _state()
    state[str(video.resolve())]["alerted"] = True
    alc.STATE_FILE.write_text(json.dumps(state))
    world.run()   # the new file: NO_ENGLISH_AUDIO
    entry = _findings_view()[str(video.resolve())]
    assert entry["findings"] == NO_ENGLISH and entry["identity"] == alc._identity(video)
    assert entry["alerted"] is True and entry["first_found"] != "t0"
    assert world.send.call_count == 1


@pytest.mark.parametrize("failure", ["rejected", "raises", "no_credentials"])
def test_a_failed_send_is_logged_to_stderr_and_jsonl_and_never_retried(world, capsys, failure):
    video = world.video()
    env = {}
    if failure == "rejected":
        world.send.return_value = False
    elif failure == "raises":
        world.send.side_effect = _raises(OSError, "net down")
    else:
        env = {"TELEGRAM_BOT_TOKEN": None}
    assert world.run(**env) == 0   # the run still succeeds
    key = str(video.resolve())
    assert "ALERT NOT DELIVERED, NOT RETRIED" in capsys.readouterr().err
    failed = [r for r in _jsonl() if r.get("event") == "ALERT_FAILED"]
    assert len(failed) == 1 and failed[0]["paths"] == [key] and failed[0]["reason"]
    assert _entry(video)["alerted"] is True   # handled either way
    sent_so_far = world.send.call_count
    world.send.side_effect = None
    world.send.return_value = True
    world.run()   # credentials or Telegram are back: still nothing is resent
    world.alert()
    assert world.send.call_count == sent_so_far
    held = alc.acquire_singleton_lock(alc.LOCK_FILE)   # the runs above really ran: lock is free
    assert held is not None
    held.close()


def test_a_pending_finding_is_sent_even_when_the_run_has_nothing_to_check(world):
    video = world.finding_video()
    _seed_finding(video, MISLABEL, "t0")   # recorded at the file's current identity, never sent
    assert world.run() == 0
    assert world.send.call_count == 1 and _entry(video)["alerted"] is True


def test_findings_of_a_gone_replaced_or_identityless_entry_are_never_sent(world):
    gone, replaced, anonymous, vanished_anonymous, fresh = (world.finding_video(i) for i in range(5))
    for v in (gone, replaced, anonymous, vanished_anonymous, fresh):
        _seed_finding(v, MISLABEL, "t0")
    state = _state()
    for v in (anonymous, vanished_anonymous):
        del state[str(v.resolve())]["identity"]
    alc.STATE_FILE.write_text(json.dumps(state))
    gone.unlink()
    vanished_anonymous.unlink()   # no file and no identity must not count as "unchanged"
    _replace(replaced)
    world.alert()
    text = world.send.call_args.args[2]
    assert "Film 04" in text and not any(f"Film 0{i}" in text for i in (0, 1, 2, 3))
    view = _findings_view()
    assert [view[str(v.resolve())]["alerted"] for v in (gone, replaced, anonymous, vanished_anonymous, fresh)] == [
        False, False, False, False, True]


def test_alerts_on_an_unreadable_state_send_and_write_nothing(world, capsys):
    video = world.finding_video()
    _seed_finding(video, MISLABEL, "t0")
    alc.STATE_FILE.write_text("{corrupt")
    world.alert()
    world.send.assert_not_called()
    assert alc.STATE_FILE.read_text() == "{corrupt"
    assert "not alerting" in capsys.readouterr().out


def test_an_alerting_crash_never_changes_the_exit_status(world, capsys):
    world.video()
    with patch.object(alc, "send_alerts", side_effect=RuntimeError("bug")):
        assert world.run() == 0
    assert "alerting failed" in capsys.readouterr().err


# --- the Telegram message is bounded by LENGTH, the rest goes in further messages ----

def _long_title_video(world, n, width=200):
    folder = f"Film {n:02d} " + "x" * (width - 8)
    stem = f"File {n:02d} " + "y" * (width - 8)
    return world.video(f"movies/{folder}/{stem}.mkv", HOUR)


def test_more_findings_than_one_message_holds_go_out_in_further_messages_once_each(world):
    for i in range(17):
        _seed_finding(world.finding_video(i), MISLABEL, f"2026-10-01T00:00:{i:02d}+00:00")
    world.alert()
    first, second = world.messages()
    assert first.count("\n- ") == alc.MAX_ALERT_ITEMS == 15 and second.count("\n- ") == 2
    assert "Film 00" in first and "Film 16" in second
    assert all(e["alerted"] for e in _findings_view().values())
    world.alert()
    assert world.send.call_count == 2


def test_very_long_titles_are_split_over_messages_within_telegram_s_limit_oldest_first(world):
    for i in range(15):
        _seed_finding(_long_title_video(world, i), MISLABEL, f"2026-10-01T00:00:{i:02d}+00:00")
    world.alert()
    sent = world.messages()
    assert 1 < len(sent) < 15
    assert all(alc._tg_len(m) <= alc.TELEGRAM_BUDGET_CHARS < 4096 for m in sent)
    order = [int(line.split("Film ")[1][:2]) for m in sent for line in m.splitlines()
             if line.startswith("- Film ")]
    assert order == list(range(15))   # each finding named exactly once, oldest first
    assert all(e["alerted"] for e in _findings_view().values())


# --- credentials ------------------------------------------------------------------------

def test_both_credentials_in_the_environment_never_touch_the_plugin_env_file():
    env = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42", "HOME": "/nonexistent"}
    with patch.dict(os.environ, env, clear=True), \
         patch.object(alc, "load_env_file", side_effect=AssertionError("must not be read")), \
         patch.object(alc, "telegram_send", return_value=True) as send:
        assert alc._notify("hello") is None
    send.assert_called_once_with("tok", "42", "hello")


@pytest.mark.parametrize("env, expected", [
    pytest.param({"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}, ("tok", "42"),
                 id="environment_wins_over_the_file"),
    pytest.param({"TELEGRAM_BOT_TOKEN": "tok"}, ("tok", "99"), id="missing_credential_filled_in_from_the_file"),
    pytest.param({}, ("filetok", "99"), id="both_from_the_file"),
])
def test_credentials_come_from_the_environment_then_the_plugin_env_file(tmp_path, env, expected):
    env_file = tmp_path / "plugin.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=filetok\nTELEGRAM_CHAT_ID=99\n")
    with patch.dict(os.environ, {**env, "TELEGRAM_ENV": str(env_file)}, clear=True), \
         patch.object(alc, "telegram_send", return_value=True) as send:
        assert alc._notify("hello") is None
    assert send.call_args.args[:2] == expected


@pytest.mark.parametrize("exc", [PermissionError("denied")])
def test_a_failure_reading_the_plugin_env_is_logged_and_fails_soft(tmp_path, exc, capsys):
    env = {"TELEGRAM_ENV": str(tmp_path / "plugin.env")}
    with patch.dict(os.environ, env, clear=True), \
         patch.object(alc, "load_env_file", side_effect=exc), \
         patch.object(alc, "telegram_send") as send:
        assert alc._notify("hello") == "no Telegram credentials"
    send.assert_not_called()
    out = capsys.readouterr().out
    assert "could not read the Telegram .env" in out and type(exc).__name__ in out


@pytest.mark.parametrize("content", [None, b"garbage\nno equals sign\n"],
                         ids=["missing_file", "no_equals_sign"])
def test_a_missing_malformed_or_undecodable_plugin_env_file_just_means_no_credentials(tmp_path, content):
    env_file = tmp_path / "plugin.env"
    if content is not None:
        env_file.write_bytes(content)
    with patch.dict(os.environ, {"TELEGRAM_ENV": str(env_file)}, clear=True), \
         patch.object(alc, "telegram_send") as send:
        assert alc._notify("hello") == "no Telegram credentials"
    send.assert_not_called()


# --- undecodable file names must not break the message or the send --------------------

def _undecodable_video(tmp_path, age=5 * HOUR):
    """A real file whose name is not valid UTF-8; Python holds it as lone surrogates."""
    d = os.fsencode(str(tmp_path / "media" / "movies")) + b"/Bad\xff Name (2000)"
    try:
        os.makedirs(d, exist_ok=True)
        fn = d + b"/bad\xfe file.mkv"
        with open(fn, "wb") as fh:
            fh.write(b"x" * 10)
    except OSError:
        pytest.skip("filesystem rejects non-UTF-8 file names")
    os.utime(fn, (_now() - age, _now() - age))
    path = Path(os.fsdecode(fn))
    assert any(0xD800 <= ord(c) <= 0xDFFF for c in str(path))
    return path


def _strict_send(sent):
    """telegram_send that encodes like urllib/Telegram do: raises on lone surrogates."""
    def send(token, chat, text, **kw):
        text.encode("utf-8")
        text.encode("utf-16-le")
        sent.append(text)
        return True
    return send


def test_an_undecodable_file_name_works_end_to_end_through_a_real_run(world):
    bad = _undecodable_video(world.tmp_path)
    good = world.video()
    sent = []
    world.send.side_effect = _strict_send(sent)
    assert world.run() == 0
    assert world.probe.call_count == 2
    assert len(sent) == 1 and sent[0].count("\n- ") == 2   # both files in ONE message
    assert _entry(bad)["alerted"] is True and _entry(good)["alerted"] is True
    assert str(bad.resolve()) in alc._load_state(alc.STATE_FILE)
    assert str(bad) in {r["path"] for r in _jsonl()}   # the JSONL round-trips the name


# --- main() ----------------------------------------------------------------------

def test_real_run_without_jellyfin_key_is_blinded(world):
    world.library()
    assert world.run(JELLYFIN_API_KEY="") == 1
    world.probe.assert_not_called()


def test_english_only_lid_model_is_a_config_error_on_a_real_run(world, capsys):
    world.library()
    assert world.run(WHISPER_LID_MODEL="small.en") == 1
    world.probe.assert_not_called()
    assert "English-only" in capsys.readouterr().out


def test_english_only_lid_model_is_only_reported_on_dry_run(world, capsys):
    world.library()
    assert world.run("--dry-run", WHISPER_LID_MODEL="medium.en") == 0
    out = capsys.readouterr().out
    assert "English-only" in out and "DRY RUN" in out


def test_second_instance_exits_quietly_on_the_lock_without_reading_state(world, capsys):
    world.library()
    alc.STATE_FILE.write_text("{corrupt")   # would exit 1 if it were ever read
    held = alc.acquire_singleton_lock(alc.LOCK_FILE)
    assert held is not None
    try:
        assert world.run() == 0
        assert capsys.readouterr().out == ""
        world.probe.assert_not_called()
        assert alc.acquire_singleton_lock(alc.LOCK_FILE) is None
    finally:
        held.close()
    again = alc.acquire_singleton_lock(alc.LOCK_FILE)
    assert again is not None
    again.close()


def test_full_run_processes_newest_first_records_state_and_sends_one_message(world):
    older, newer = world.library()
    assert world.run() == 0
    assert [c.args[0] for c in world.probe.call_args_list] == [newer, older]   # newest first
    assert set(_state()) == {str(older.resolve()), str(newer.resolve())}
    for p in (older, newer):
        e = _entry(p)
        assert e["identity"] == alc._identity(p) and e["result"] == "CHECKED" and e["checked_at"]
        assert [f["type"] for f in e["findings"]] == ["NO_ENGLISH_AUDIO"] and e["alerted"] is True
    assert world.send.call_count == 1   # ONE message for both files
    assert "Older (1999)" in world.messages()[0] and "Newer" in world.messages()[0]
    assert len(_jsonl()) == 2
    assert not list(alc.WORKDIR_ROOT.iterdir())   # per-file workdirs removed


def test_second_run_skips_unchanged_files_and_does_not_re_alert(world):
    world.library()
    assert world.run() == 0
    assert world.probe.call_count == 2 and world.send.call_count == 1
    assert world.run() == 0
    assert world.probe.call_count == 2   # nothing re-checked
    assert world.send.call_count == 1    # nothing re-alerted


def test_run_with_no_findings_sends_nothing(world):
    world.library()
    world.probe.return_value = _info(_stream(1, "eng"))
    world.detect.return_value = ("en", 0.95)
    assert world.run() == 0
    world.send.assert_not_called()
    assert _findings_view() == {}


@pytest.mark.parametrize("probe_result, detected", [
    pytest.param(_info(_stream(1, "spa")), ("es", 0.2), id="nothing_determined_with_confidence"),
])
def test_a_replacement_that_comes_back_unverified_drops_the_old_files_finding_and_alerts_nothing(
        world, probe_result, detected):
    video = world.video()
    _seed_finding(video, MISLABEL, "t0", [1, 2, 3])   # the file that was replaced
    world.probe.return_value = probe_result
    world.detect.return_value = detected
    assert world.run() == 0
    assert _entry(video)["result"] == "UNVERIFIED" and _entry(video)["findings"] == []
    assert _findings_view() == {}
    world.send.assert_not_called()


def test_run_budget_stops_starting_new_files(world, monkeypatch):
    older, newer = world.library()

    class _Clock:
        t = 1000.0

    monkeypatch.setattr(alc.time, "monotonic", lambda: _Clock.t)
    processed = []

    def slow_process(video, cfg, jellyfin, workdir):
        processed.append(video)
        _Clock.t += 21 * 60   # longer than the 20-minute default budget
        return {"path": str(video), "result": "CHECKED", "findings": []}

    with patch.object(alc, "process_candidate", slow_process):
        assert world.run() == 0
    assert processed == [newer]   # the in-flight file finished, the next never started
    assert set(_state()) == {str(newer.resolve())}


def test_playback_wait_timeout_ends_the_run_cleanly_but_still_sends_pending_alerts(world):
    older, newer = world.library()
    pending = world.finding_video()
    _seed_finding(pending, MISLABEL, "t0")
    processed = []

    def exhausted(video, cfg, jellyfin, workdir):
        processed.append(video)
        raise alc.PlaybackWaitTimeout("cumulative playback wait exceeded")

    with patch.object(alc, "process_candidate", exhausted):
        assert world.run() == 0
    assert processed == [newer]
    assert set(_state()) == {str(pending.resolve())}   # nothing recorded for the interrupted file
    assert not list(alc.WORKDIR_ROOT.iterdir())        # workdir still cleaned up
    assert world.send.call_count == 1 and _entry(pending)["alerted"] is True


@pytest.mark.parametrize("result", ["CHECKED", "PROBE_FAILED"])
def test_a_result_is_discarded_when_the_file_changes_during_analysis(world, result):
    world.video()

    def process(v, cfg, jf, wd):
        _replace(v)
        return {"path": str(v), "result": result, "findings": list(MISLABEL) if result == "CHECKED" else []}

    with patch.object(alc, "process_candidate", process):
        assert world.run() == 0
    assert not alc.STATE_FILE.exists()   # not even a failure record
    assert not alc.JSONL_FILE.exists()
    world.send.assert_not_called()


# --- --path -------------------------------------------------------------------------

def test_path_mode_records_nothing_and_never_alerts(world):
    video = world.video(age=30.0)
    assert world.run("--path", str(video)) == 0
    world.send.assert_not_called()
    assert not alc.STATE_FILE.exists() and not alc.JSONL_FILE.exists()
    assert not list(alc.WORKDIR_ROOT.iterdir())


def test_path_mode_does_not_read_a_corrupt_state_and_checks_an_already_recorded_file(world, capsys):
    video = world.video(age=30.0)
    alc.STATE_FILE.write_text("{corrupt")
    world.probe.return_value = _info(_stream(1, "eng"))
    world.detect.return_value = ("en", 0.9)
    assert world.run("--path", str(video)) == 0
    assert alc.STATE_FILE.read_text() == "{corrupt"
    rec = json.loads(capsys.readouterr().out)
    assert rec["path"] == str(video.resolve())
    assert rec["result"] == "CHECKED" and rec["findings"] == []
    assert rec["streams"][0]["detected_language"] == "en" and rec["original_language"] == "eng"
    alc.STATE_FILE.write_text(json.dumps(
        {str(video.resolve()): {"identity": alc._identity(video), "result": "CHECKED"}}))
    assert world.run("--path", str(video)) == 0
    assert world.probe.call_count == 2   # still analysed: --path ignores the cache


def test_path_mode_honours_the_playback_gate(world, monkeypatch, capsys):
    import media_stack.playback as pb
    video = world.video(age=30.0)
    sleeps = []
    monkeypatch.setattr(pb.time, "sleep", lambda s: sleeps.append(s))
    world.jellyfin = _Jellyfin([[], [{"NowPlayingItem": {"Name": "x"}}], []])
    world.probe.return_value = _info(_stream(1, "eng"))
    world.detect.side_effect = [ExtractionInterrupted("playing"), ("en", 0.9)]
    assert world.run("--path", str(video)) == 0
    assert len(sleeps) == 1
    assert json.loads(capsys.readouterr().out)["result"] == "CHECKED"


def test_path_mode_nonexistent_file_is_an_error(world):
    assert world.run("--path", str(world.tmp_path / "nope.mkv")) == 1


def test_path_mode_reports_busy_with_json_and_exit_75_when_the_lock_is_held(world, capsys):
    video = world.video(age=30.0)
    held = alc.acquire_singleton_lock(alc.LOCK_FILE)
    assert held is not None
    try:
        rc = world.run("--path", str(video))
    finally:
        held.close()
    assert rc == 75 == alc.EXIT_BUSY
    assert json.loads(capsys.readouterr().out) == {
        "path": str(video.resolve()), "result": "BUSY",
        "reason": "another audio-lang-check run holds the lock"}
    world.probe.assert_not_called()
    world.send.assert_not_called()
    assert not alc.STATE_FILE.exists() and not alc.JSONL_FILE.exists()


# --- --dry-run writes nothing; importing creates nothing -----------------------------

def _real_var_snapshot():
    """Names, modes, sizes and mtimes of everything under the real var/."""
    root = real_paths.VAR_DIR
    if not root.exists():
        return None
    snap = {str(root): root.lstat().st_mtime_ns}
    for p in sorted(root.rglob("*")):
        try:
            st = p.lstat()
        except OSError:
            continue
        snap[str(p)] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return snap


def test_importing_the_script_calls_no_directory_creator_and_touches_no_real_var():
    before = _real_var_snapshot()
    spec = importlib.util.spec_from_file_location(
        "audio_lang_check_import_probe", str(PROJECT_ROOT / "audio-lang-check.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spy = Mock()
    try:
        with patch.object(real_paths, "ensure_var_dirs", spy):
            spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    spy.assert_not_called()
    assert _real_var_snapshot() == before


def _point_runtime_paths_at_a_tree_that_does_not_exist(monkeypatch, tmp_path):
    fresh = tmp_path / "fresh-var"
    for name, rel in (("LOCK_FILE", "run/x.lock"), ("WORKDIR_ROOT", "tmp"),
                      ("JSONL_FILE", "log/x.jsonl"), ("STATE_FILE", "state/i.json")):
        monkeypatch.setattr(alc, name, fresh / rel)
    return fresh


def test_dry_run_lists_candidates_newest_first_and_mutates_nothing(world, monkeypatch, capsys):
    older, newer = world.library()
    fresh = _point_runtime_paths_at_a_tree_that_does_not_exist(monkeypatch, world.tmp_path)
    before = _real_var_snapshot()
    with patch.object(alc, "acquire_singleton_lock") as lock:
        assert world.run("--dry-run", JELLYFIN_API_KEY="") == 0   # no key needed for a dry run
    out = capsys.readouterr().out
    assert "DRY RUN: 2 candidate(s)" in out
    assert out.index(str(newer)) < out.index(str(older))   # newest first
    alc.ensure_var_dirs.assert_not_called()
    lock.assert_not_called()
    assert not fresh.exists() and _real_var_snapshot() == before
    world.probe.assert_not_called()
    world.send.assert_not_called()


def test_dry_run_combined_with_path_is_a_usage_error_that_touches_nothing(world, monkeypatch, capsys):
    video = world.video(age=30.0)
    fresh = _point_runtime_paths_at_a_tree_that_does_not_exist(monkeypatch, world.tmp_path)
    before = _real_var_snapshot()
    with patch.object(alc, "acquire_singleton_lock") as lock:
        with pytest.raises(SystemExit) as exc:
            world.run("--dry-run", "--path", str(video), JELLYFIN_API_KEY="")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--dry-run cannot be combined with --path" in err and "usage:" in err
    world.probe.assert_not_called()
    world.send.assert_not_called()
    alc.ensure_var_dirs.assert_not_called()
    lock.assert_not_called()
    assert not fresh.exists() and _real_var_snapshot() == before


# --- state compatibility: the schema of the previous version ----------------------------
# Shaped like the live var/state file (per-stream detections, the Arr answer used,
# `original_rechecked_at`, an alerted finding); paths and titles are made up. The `retry`
# record and the unsent finding are the other shapes the previous version wrote.

_OLD_STREAM = {"index": 1, "tag": "eng", "detected_language": "en", "probability": 0.874,
               "counts_as_english": True, "reading": "english"}
_OLD_ENTRIES = {
    "clean": {"result": "CHECKED", "findings": [], "checked_at": "2026-10-20T22:38:16.584204+00:00",
              "original_status": "known", "original_language": "eng", "streams": [_OLD_STREAM]},
    "unknown_original": {
        "result": "CHECKED", "findings": [], "checked_at": "2026-10-20T22:50:12.677259+00:00",
        "original_status": "unknown", "original_language": None, "streams": [_OLD_STREAM],
        "original_rechecked_at": "2026-10-29T04:37:01.498215+00:00"},
    "short_file": {
        "result": "UNVERIFIED", "findings": [], "checked_at": "2026-10-20T22:38:16.584204+00:00",
        "original_status": "known", "original_language": "eng",
        "streams": [{"index": 1, "tag": "eng", "detected_language": None, "probability": None,
                     "counts_as_english": True, "reading": None, "mislabel_skipped": True}]},
    "alerted_finding": {
        "result": "CHECKED", "findings": NO_ENGLISH, "checked_at": "2026-10-21T02:45:52.525467+00:00",
        "original_status": "known", "original_language": "eng",
        "streams": [{"index": 1, "tag": None, "detected_language": "fr", "probability": 0.984,
                     "counts_as_english": False, "reading": "foreign"}],
        "first_found": "2026-10-21T02:45:52.525467+00:00", "alerted": True},
    "unsent_finding": {
        "result": "CHECKED", "findings": MISLABEL, "checked_at": "2026-10-22T02:45:52.525467+00:00",
        "original_status": "unknown", "original_language": None, "streams": [_OLD_STREAM],
        "first_found": "2026-10-22T02:45:52.525467+00:00", "alerted": False},
    "backing_off": {
        "retry": {"identity": None, "attempts": 1, "next_retry_at": 4102444800.0,
                  "last_result": "PROBE_FAILED", "last_error": None,
                  "last_attempt_at": "2026-10-28T10:00:00.000000+00:00"}},
}


def _old_schema_state(world):
    """Write the old-schema state against real files; returns {name: (video, entry as written)}."""
    written, out = {}, {}
    for name, template in _OLD_ENTRIES.items():
        video = world.video(f"movies/Old {name} (2000)/old.mkv", 5 * HOUR)
        entry = json.loads(json.dumps(template))
        if name == "backing_off":
            entry["retry"]["identity"] = alc._identity(video)
        else:
            entry["identity"] = alc._identity(video)
        written[str(video.resolve())] = entry
        out[name] = (video, entry)
    alc.STATE_FILE.write_text(json.dumps(written))
    return out


def test_old_schema_state_loads_without_a_malformed_error(world):
    _old_schema_state(world)
    state = alc._load_state(alc.STATE_FILE)
    assert state is not None and len(state) == len(_OLD_ENTRIES)
    assert all(alc.entry_problem(e) is None for e in state.values())


def test_old_schema_run_skips_concluded_files_and_alerts_only_the_unsent_finding_once(world):
    old = _old_schema_state(world)
    alerted, unsent = old["alerted_finding"], old["unsent_finding"]
    assert world.run() == 0
    # only the file with a leftover `retry` is a candidate (its backoff is ignored); the concluded ones are not
    assert [c.args[0] for c in world.probe.call_args_list] == [old["backing_off"][0]]
    assert world.send.call_count == 1
    assert "Old unsent_finding" in world.messages()[0] and "Old alerted_finding" not in world.messages()[0]
    assert _entry(unsent[0])["alerted"] is True
    assert _entry(alerted[0]) == alerted[1]   # the alerted finding: not re-alerted, not even touched
    assert "retry" not in _entry(old["backing_off"][0])   # superseded by its new conclusion
    world.run()
    world.run()
    assert world.send.call_count == 1


@pytest.mark.parametrize("last_result, attempts", [
    # the old counter also counted outages, so even a PROBE_FAILED record at the cap may be
    # mostly ARR_UNAVAILABLE / DEPENDENCY_MISSING history
    ("PROBE_FAILED", alc.MAX_FAILED_ATTEMPTS + 4), ("ARR_UNAVAILABLE", alc.MAX_FAILED_ATTEMPTS + 4),
])
def test_a_leftover_retry_record_never_rules_a_file_out_and_is_replaced_by_the_new_count(
        world, last_result, attempts):
    video = world.video()
    alc.STATE_FILE.write_text(json.dumps({str(video.resolve()): {"retry": {
        "identity": alc._identity(video), "attempts": attempts, "next_retry_at": 4102444800.0,
        "last_result": last_result, "last_error": "x", "last_attempt_at": "t"}}}))
    assert alc._load_state(alc.STATE_FILE) is not None   # not malformed, whatever it holds
    world.probe.return_value = None
    world.run()
    assert world.probe.call_count == 1   # tried again at once: the backoff time is ignored too
    entry = _entry(video)
    assert "retry" not in entry and entry["failed"]["attempts"] == 1


# --- strict state validation: malformed state is a blinding error that writes nothing ---

_OK_IDENT = [1, 2, 3]
_NON_CONCLUSIONS = ["PROBE_FAILED", "ERROR", "ARR_UNAVAILABLE", "DEPENDENCY_MISSING"]

_MALFORMED = {
    "truncated json": '{"/m/a.mkv": {"identity": [1, 2',
    "top level a list": "[]",
    "alerted a string": '{"/m/a.mkv": {"alerted": "yes"}}',
}


@pytest.mark.parametrize("name", list(_MALFORMED))
def test_malformed_state_is_a_blinding_error_that_writes_nothing(world, name):
    content = _MALFORMED[name]
    world.library()
    alc.STATE_FILE.write_text(content)
    assert world.run() == 1
    world.probe.assert_not_called()
    world.send.assert_not_called()
    assert alc.STATE_FILE.read_text() == content   # not repaired, not replaced
    assert not alc.JSONL_FILE.exists()             # no log record
    assert not alc.WORKDIR_ROOT.exists()


@pytest.mark.parametrize("name", ["top level a list"])
def test_dry_run_reports_malformed_state_the_same_way_and_writes_nothing(world, name, capsys):
    world.library()
    alc.STATE_FILE.write_text(_MALFORMED[name])
    assert world.run("--dry-run") == 1
    assert "run blinded" in capsys.readouterr().out
    assert alc.STATE_FILE.read_text() == _MALFORMED[name]


def test_malformed_state_is_reported_with_the_path_and_the_reason(capsys):
    alc.STATE_FILE.write_text(_MALFORMED["alerted a string"])
    assert alc._load_state(alc.STATE_FILE) is None
    out = capsys.readouterr().out
    assert "/m/a.mkv" in out and "alerted has the wrong type" in out


@pytest.mark.parametrize("entry", [
    {},
    {"identity": _OK_IDENT, "result": "CHECKED", "findings": [], "checked_at": "t"},
    {"identity": _OK_IDENT, "result": "CHECKED", "findings": MISLABEL + NO_ENGLISH,
     "first_found": "t", "alerted": False, "checked_at": "t"},
])
def test_well_formed_entries_validate(entry):
    assert alc.entry_problem(entry) is None


def test_only_the_two_conclusions_are_valid_cached_results():
    for result in ["CHECKED", "UNVERIFIED"]:
        assert alc.entry_problem({"identity": _OK_IDENT, "result": result, "findings": []}) is None
    for result in _NON_CONCLUSIONS:
        assert alc.entry_problem({"identity": _OK_IDENT, "result": result, "findings": []})


# --- each finding is validated for its kind -----------------------------------------

_GOOD_STREAM = {"index": 1, "tag": "spa", "detected_language": "es", "probability": 0.95}


def _no_english(**over):
    f = {"type": "NO_ENGLISH_AUDIO", "original_language": "eng", "streams": [dict(_GOOD_STREAM)]}
    f.update(over)
    return f


def _mislabeled(**over):
    f = dict(MISLABEL[0])
    f.update(over)
    return f


@pytest.mark.parametrize("finding", [
    MISLABEL[0],
    _no_english(),
])
def test_well_formed_findings_validate(finding):
    assert alc._finding_problem(finding) is None


def _without(finding, key):
    return {k: v for k, v in finding.items() if k != key}


@pytest.mark.parametrize("finding", [
    {"type": "SOMETHING_ELSE"},
    _without(_mislabeled(), "probability"),
    _no_english(streams=[]),
])
def test_malformed_findings_are_rejected(finding):
    assert alc._finding_problem(finding)
    assert alc.entry_problem({"identity": _OK_IDENT, "result": "CHECKED", "findings": [finding]})

# --- the cron entry ----------------------------------------------------------------

def _minutes(field: str) -> set[int]:
    if field == "*":
        return set(range(60))
    if field.startswith("*/"):
        return set(range(0, 60, int(field[2:])))
    return {int(x) for x in field.split(",")}


def test_cron_entry_start_minute_collides_with_no_other_job():
    lines = [ln for ln in (PROJECT_ROOT / "ops/cron.d/media-stack.crontab").read_text().splitlines()
             if ln.strip() and not ln.startswith("#") and "=" not in ln.split()[0]]  # jobs, not PATH=
    ours = [ln for ln in lines if "audio-lang-check.py" in ln]
    assert len(ours) == 1
    fields = ours[0].split()
    assert fields[1:5] == ["*", "*", "*", "*"]  # hourly
    assert ours[0].split(None, 5)[5].startswith("nice -n19 ionice -c3 python3 ")
    assert "var/log/audio-lang-check.log" in ours[0]
    minute = _minutes(fields[0])
    assert len(minute) == 1
    for ln in lines:
        if ln is ours[0]:
            continue
        other = ln.split()[0]
        if other == "*":
            continue  # the every-minute stall reaper cannot be avoided by any entry
        assert not (minute & _minutes(other)), f"minute collision with: {ln}"
