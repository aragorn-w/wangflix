"""`audio-lang-check.py`: the hourly whisper language-ID spot-check of each video's audio against its tags and the Arr original language.

`probe`, `detect_language`, `original_language_lookup`, `JellyfinClient` and `telegram_send` are
mocked; every state/lock/log/workdir/media path is monkeypatched to `tmp_path` and environment
reads run under `patch.dict(os.environ, ..., clear=True)`. Scripted sequences are consumed
lazily (`iter`/`next`), never `list()`ed.
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
    monkeypatch.setattr(alc, "REPORT_FILE", tmp_path / "state" / "report.json")
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


def _idle_jellyfin(*a, **k):
    return _Jellyfin()


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


# a well-formed NO_ENGLISH_AUDIO finding (what the script writes for one Spanish stream)
NO_ENGLISH = [{"type": "NO_ENGLISH_AUDIO", "original_language": "eng",
               "streams": [{"index": 1, "tag": "spa", "detected_language": "es",
                            "probability": 0.95}]}]


def _state():
    """The single authoritative state, as written."""
    return json.loads(alc.STATE_FILE.read_text())


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


def _set_alerted(video, value=True):
    state = _state()
    state[str(video.resolve())]["alerted"] = value
    alc.STATE_FILE.write_text(json.dumps(state))


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
    [("es", 0.95), ("es", 0.7), ("en", 0.2), ("en", 0.2)],   # second confident but under 0.8
    [("es", 0.7), ("es", 0.7), ("en", 0.2), ("en", 0.2)],    # confident (>= 0.6) but never >= 0.8
    [("es", 0.95), ("fr", 0.95), ("de", 0.9), ("it", 0.9)],  # confident windows, DIFFERENT languages
    [("es", 0.95), ("es", 0.3), ("en", 0.9)],          # second inconclusive, third says English
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


def test_two_agreeing_foreign_windows_do_not_end_the_scan():
    """Only a confident English window stops the scan."""
    _, fake = _analyze(_info(_stream(1, "spa")), {1: [("es", 0.99), ("es", 0.7)]})
    assert len(fake.calls) == 4


@pytest.mark.parametrize("script, calls", [
    pytest.param([("en", 0.9)], 1, id="first_window"),
    pytest.param([("es", 0.9), ("en", 0.8)], 2, id="after_a_foreign_window"),
    pytest.param([("es", 0.95), ("es", 0.95), ("es", 0.95), ("en", 0.7)], 4, id="last_planned_window"),
])
def test_a_confident_english_window_is_heard_and_ends_the_scan(script, calls):
    (findings, _, _, _), fake = _analyze(_info(_stream(1, "eng")), {1: script})
    assert len(fake.calls) == calls and findings == []


@pytest.mark.parametrize("tag, script", [
    pytest.param(None, [("en", 0.2), ("en", 0.3), ("en", 0.4), ("en", 0.5)], id="nothing_resolves"),
    pytest.param("eng", [("es", 0.9), ("fr", 0.9), ("de", 0.9), ("it", 0.9)], id="confident_disagreeing_foreign"),
])
def test_windows_are_25_50_10_75_percent_of_duration_30s_each_in_given_workdir(tag, script):
    _, fake = _analyze(_info(_stream(1, tag), duration=2000.0), {1: script})
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


def test_one_inconclusive_stream_among_confidently_foreign_ones_is_unverified():
    (findings, _, result, _), _ = _analyze(
        _info(_stream(1, "spa"), _stream(2, "fra")),
        {1: [("es", 0.95), ("es", 0.9)], 2: [("fr", 0.2), ("fr", 0.3), ("fr", 0.1), ("fr", 0.2)]},
        original="eng")
    assert result == "UNVERIFIED" and findings == []


def test_english_original_with_every_stream_confidently_foreign_is_a_finding():
    (findings, _, result, _), _ = _analyze(
        _info(_stream(1, "spa"), _stream(2, None)),
        {1: [("es", 0.95), ("es", 0.9)], 2: [("fr", 0.9), ("fr", 0.85)]}, original="eng")
    assert result == "CHECKED"
    assert _types(findings) == ["NO_ENGLISH_AUDIO"]


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


@pytest.mark.parametrize("script", [
    pytest.param([("en", 0.59), ("en", 0.5), ("en", 0.4), ("en", 0.3)], id="below_the_floor"),
    pytest.param([("en", 0.5996)] * 4, id="0.5996_displays_as_0.6_but_is_not_confident"),
])
def test_foreign_tag_detected_english_below_the_raw_floor_does_not_count(script):
    (findings, _, result, _), _ = _analyze(_info(_stream(1, "spa")), {1: script}, original="eng")
    assert result == "UNVERIFIED" and findings == []


@pytest.mark.parametrize("tag, prob", [
    pytest.param(None, 0.9, id="untagged"),
    pytest.param("und", 0.65, id="und"),
    pytest.param(None, 0.61, id="untagged_just_over_the_floor"),
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


def test_file_with_only_commentary_tracks_is_unverified_not_no_english_audio():
    only_commentary = _info(_stream(1, "eng", disposition={"comment": 1}),
                            _stream(2, "eng", tags={"language": "eng", "title": "Audio Description"}))
    with patch.object(alc, "probe_duration", side_effect=AssertionError("no streams: no probe")):
        (findings, _, result, _), fake = _analyze(only_commentary, {}, original="eng")
    assert fake.calls == [] and findings == [] and result == "UNVERIFIED"


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


def test_unknown_playback_state_mid_extraction_also_fails_closed(monkeypatch):
    _fake_clock(monkeypatch)

    def always_interrupted(*a, should_abort=None, **k):
        raise ExtractionInterrupted("unknown")

    jf = _Jellyfin([[]], then=None)  # idle for the first gate, unknown ever after
    with pytest.raises(alc.PlaybackWaitTimeout):
        _process(always_interrupted, jf=jf, cfg=_cfg(max_wait_min=2.0))


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
    assert rec["result"] in alc.RETRYABLE_RESULTS and "faster_whisper" in rec["error"]


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
    entry = _state()[str(video.resolve())]
    assert entry == {"identity": alc._identity(video), "result": "CHECKED",
                     "findings": NO_ENGLISH,
                     "checked_at": "2026-10-13T00:00:00+00:00",
                     "first_found": "2026-10-13T00:00:00+00:00", "alerted": False}
    st = video.stat()
    assert entry["identity"] == [st.st_ino, st.st_size, st.st_mtime_ns]


def test_a_conclusion_without_findings_has_no_first_found_or_alerted(tmp_path):
    video = _make_video(tmp_path / "media", "movies/A/a.mkv", HOUR)
    assert alc._record_result(video, alc._identity(video), {"result": "UNVERIFIED", "findings": []}, "t")
    assert _state()[str(video.resolve())] == {
        "identity": alc._identity(video), "result": "UNVERIFIED", "findings": [],
        "checked_at": "t"}


def test_state_write_failure_is_logged_and_never_crashes(tmp_path, capsys):
    video = _make_video(tmp_path / "media", "movies/A/a.mkv", HOUR)
    with patch.object(alc, "mutate_state", side_effect=OSError("disk full")):
        assert alc._record_result(video, [1, 2, 3], {"result": "CHECKED", "findings": []}, "t") is False
        alc._record_retry(video, [1, 2, 3], {"result": "ERROR"}, 1.0)
    out = capsys.readouterr().out
    assert out.count("WARNING") == 2


def test_a_conclusion_replaces_the_whole_entry_including_an_outstanding_retry(tmp_path):
    video = _make_video(tmp_path / "media", "movies/A/a.mkv", HOUR)
    ident = alc._identity(video)
    alc._record_retry(video, ident, {"result": "ERROR", "error": "x"}, 1000.0)
    assert "retry" in _state()[str(video.resolve())]
    assert alc._record_result(video, ident, {"result": "CHECKED", "findings": []}, "t")
    assert "retry" not in _state()[str(video.resolve())]


def test_a_retry_record_leaves_an_earlier_conclusion_and_its_findings_alone(tmp_path):
    video = _make_video(tmp_path / "media", "movies/A/a.mkv", HOUR)
    _seed_finding(video, MISLABEL, "t0", [1, 2, 3])
    before = _state()[str(video.resolve())]
    alc._record_retry(video, alc._identity(video), {"result": "PROBE_FAILED"}, 1000.0)
    after = _state()[str(video.resolve())]
    assert {k: v for k, v in after.items() if k != "retry"} == before
    assert after["retry"]["attempts"] == 1


# --- alerting ----------------------------------------------------------------------

_TG_ENV = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}


def _alert_env(tmp_path, **extra):
    return {"HOME": str(tmp_path), "TELEGRAM_ENV": str(tmp_path / "no-such.env"),
            **_TG_ENV, **extra}


def _finding_video(tmp_path, n=0):
    return _make_video(tmp_path / "media", f"movies/Film {n:02d} (2000)/Film {n:02d}.mkv", HOUR)


MISLABEL = [{"type": "MISLABELED", "stream_index": 1, "tag": "eng",
             "detected_language": "es", "probability": 0.85}]


def test_one_message_lists_new_findings_and_marks_them_alerted(tmp_path):
    v1, v2 = _finding_video(tmp_path, 1), _finding_video(tmp_path, 2)
    _seed_finding(v1, MISLABEL, "2026-10-01T00:00:00+00:00")
    _seed_finding(v2, NO_ENGLISH,
                        "2026-10-02T00:00:00+00:00")
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True), \
         patch.object(alc, "telegram_send", return_value=True) as send:
        alc.retry_alerts()
    assert send.call_count == 1  # ONE message for both files
    token, chat, text = send.call_args.args
    assert (token, chat) == ("tok", "42")
    assert "Film 01 (2000) / Film 01" in text
    assert "stream 1 tagged eng but detected es (p=0.85)" in text
    assert "Film 02 (2000) / Film 02" in text and "no confirmed English audio" in text
    assert text.index("Film 01") < text.index("Film 02")  # oldest first_found first
    state = _findings_view()
    assert all(e["alerted"] for e in state.values())


def test_no_re_alert_for_unchanged_findings_and_none_when_nothing_new(tmp_path):
    v = _finding_video(tmp_path)
    _seed_finding(v, MISLABEL, "t1")
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True), \
         patch.object(alc, "telegram_send", return_value=True) as send:
        alc.retry_alerts()
        assert send.call_count == 1
        # the file is re-checked (identity changed) with IDENTICAL findings
        _seed_finding(v, [dict(MISLABEL[0])], "t2")
        alc.retry_alerts()
        alc.retry_alerts()
        assert send.call_count == 1
    entry = _findings_view()[str(v.resolve())]
    assert entry["alerted"] is True and entry["first_found"] == "t1"


def test_changed_findings_are_alerted_again(tmp_path):
    v = _finding_video(tmp_path)
    _seed_finding(v, MISLABEL, "t1")
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True), \
         patch.object(alc, "telegram_send", return_value=True) as send:
        alc.retry_alerts()
        _seed_finding(v, MISLABEL + NO_ENGLISH, "t2")
        alc.retry_alerts()
    assert send.call_count == 2
    entry = _findings_view()[str(v.resolve())]
    assert entry["first_found"] == "t1" and entry["alerted"] is True


def test_failed_send_leaves_findings_unalerted_and_next_run_retries(tmp_path):
    v = _finding_video(tmp_path)
    _seed_finding(v, MISLABEL, "t1")
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True):
        with patch.object(alc, "telegram_send", return_value=False) as send:
            alc.retry_alerts()  # must not raise
        assert send.call_count == 1
        assert _findings_view()[str(v.resolve())]["alerted"] is False
        with patch.object(alc, "telegram_send", return_value=True) as send:
            alc.retry_alerts()
        assert send.call_count == 1
    assert _findings_view()[str(v.resolve())]["alerted"] is True


def test_send_that_raises_is_not_fatal_and_stays_unalerted(tmp_path):
    v = _finding_video(tmp_path)
    _seed_finding(v, MISLABEL, "t1")
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True), \
         patch.object(alc, "telegram_send", side_effect=OSError("net down")):
        alc.retry_alerts()
    assert _findings_view()[str(v.resolve())]["alerted"] is False


def test_missing_token_skips_send_without_failing_and_keeps_findings_unalerted(tmp_path):
    v = _finding_video(tmp_path)
    _seed_finding(v, MISLABEL, "t1")
    env = {"HOME": str(tmp_path), "TELEGRAM_ENV": str(tmp_path / "no-such.env")}
    with patch.dict(os.environ, env, clear=True), \
         patch.object(alc, "telegram_send") as send:
        alc.retry_alerts()
    send.assert_not_called()
    assert _findings_view()[str(v.resolve())]["alerted"] is False


def test_credentials_fall_back_to_the_telegram_plugin_env_file(tmp_path):
    env_file = tmp_path / "plugin.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=filetok\nTELEGRAM_CHAT_ID=99\n")
    v = _finding_video(tmp_path)
    _seed_finding(v, MISLABEL, "t1")
    env = {"HOME": str(tmp_path), "TELEGRAM_ENV": str(env_file)}
    with patch.dict(os.environ, env, clear=True), \
         patch.object(alc, "telegram_send", return_value=True) as send:
        alc.retry_alerts()
    assert send.call_args.args[:2] == ("filetok", "99")


def test_message_is_capped_at_15_items_plus_and_n_more_and_the_rest_alert_next_run(tmp_path):
    videos = [_finding_video(tmp_path, i) for i in range(17)]
    for i, v in enumerate(videos):
        _seed_finding(v, MISLABEL, f"2026-10-01T00:00:{i:02d}+00:00")
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True), \
         patch.object(alc, "telegram_send", return_value=True) as send:
        alc.retry_alerts()
        first_text = send.call_args.args[2]
        assert first_text.count("\n- ") == 15
        assert "...and 2 more" in first_text
        state = _findings_view()
        assert sum(e["alerted"] for e in state.values()) == 15
        alc.retry_alerts()  # the 2 that did not fit
        assert send.call_count == 2
        assert "...and" not in send.call_args.args[2]
        assert send.call_args.args[2].count("\n- ") == 2
        alc.retry_alerts()
        assert send.call_count == 2


def test_a_clean_conclusion_clears_the_findings_so_nothing_is_alerted(tmp_path):
    v = _finding_video(tmp_path)
    _seed_finding(v, MISLABEL, "t1")
    assert alc._record_result(v, alc._identity(v), {"result": "CHECKED", "findings": []}, "t2")
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True), \
         patch.object(alc, "telegram_send") as send:
        alc.retry_alerts()
    send.assert_not_called()
    assert _findings_view() == {}


def test_title_falls_back_to_the_filename_outside_the_library_layout():
    assert alc._title_for(Path("/elsewhere/odd.mkv")) == "odd.mkv"


# --- main() ----------------------------------------------------------------------

def _run_main(argv, env=None, tmp_path=None):
    env = {"HOME": str(tmp_path or "/nonexistent"), "JELLYFIN_API_KEY": "key", **(env or {})}
    with patch.object(sys, "argv", ["audio-lang-check.py", *argv]), \
         patch.dict(os.environ, env, clear=True):
        return alc.main()


def _seed_library(tmp_path):
    root = tmp_path / "media"
    older = _make_video(root, "movies/Older (1999)/older.mkv", 5 * HOUR, now=_now())
    newer = _make_video(root, "tv/Newer/Season 01/newer.mkv", 2 * HOUR, now=_now())
    return older, newer


def _now():
    return time.time()


def _patch_world(probe_side_effect=None, detect=None, original="eng", sent=True):
    """Patches every external boundary main() touches."""
    return [
        patch.object(alc, "JellyfinClient", _idle_jellyfin),
        patch.object(alc, "probe", side_effect=probe_side_effect),
        patch.object(alc, "original_language_lookup", return_value=_lookup(original)),
        patch.object(alc, "detect_language", detect or _lid_forever(("es", 0.95))),
        patch.object(alc, "telegram_send", return_value=sent),
    ]


def _lid_forever(result):
    def fake(*a, should_abort=None, **k):
        return result
    return fake


def _enter(patches):
    from contextlib import ExitStack
    stack = ExitStack()
    mocks = [stack.enter_context(p) for p in patches]
    return stack, mocks


def test_real_run_without_jellyfin_key_is_blinded(tmp_path):
    _seed_library(tmp_path)
    stack, (_, probe, *_rest) = _enter(_patch_world())
    with stack:
        assert _run_main([], env={"JELLYFIN_API_KEY": ""}, tmp_path=tmp_path) == 1
    probe.assert_not_called()


def test_english_only_lid_model_is_a_config_error_on_a_real_run(tmp_path, capsys):
    _seed_library(tmp_path)
    stack, (_, probe, *_rest) = _enter(_patch_world())
    with stack:
        assert _run_main([], env={"WHISPER_LID_MODEL": "small.en"}, tmp_path=tmp_path) == 1
    probe.assert_not_called()
    assert "English-only" in capsys.readouterr().out


def test_english_only_lid_model_is_only_reported_on_dry_run(tmp_path, capsys):
    _seed_library(tmp_path)
    stack, _m = _enter(_patch_world())
    with stack:
        rc = _run_main(["--dry-run"], env={"WHISPER_LID_MODEL": "medium.en"}, tmp_path=tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "English-only" in out and "DRY RUN" in out


def test_second_instance_exits_quietly_on_the_lock_without_reading_state(tmp_path, capsys):
    _seed_library(tmp_path)
    alc.STATE_FILE.write_text("{corrupt")  # would exit 1 if it were ever read
    held = alc.acquire_singleton_lock(alc.LOCK_FILE)
    assert held is not None
    try:
        stack, (_, probe, *_rest) = _enter(_patch_world())
        with stack:
            assert _run_main([], tmp_path=tmp_path) == 0
        assert capsys.readouterr().out == ""
        probe.assert_not_called()
        assert alc.acquire_singleton_lock(alc.LOCK_FILE) is None
    finally:
        held.close()
    again = alc.acquire_singleton_lock(alc.LOCK_FILE)
    assert again is not None
    again.close()


def test_full_run_processes_newest_first_records_state_and_sends_one_message(tmp_path):
    older, newer = _seed_library(tmp_path)
    probed = []

    def fake_probe(p):
        probed.append(p)
        return _info(_stream(1, "spa"))

    stack, (_, _, _, detect, send) = _enter(_patch_world(probe_side_effect=fake_probe))
    with stack:
        rc = _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
    assert rc == 0
    assert probed == [newer, older]  # newest first, strictly one at a time
    identity = json.loads(alc.STATE_FILE.read_text())
    assert set(identity) == {str(older.resolve()), str(newer.resolve())}
    for p in (older, newer):
        e = identity[str(p.resolve())]
        assert e["identity"] == alc._identity(p)
        assert e["result"] == "CHECKED"
        assert [f["type"] for f in e["findings"]] == ["NO_ENGLISH_AUDIO"]
        assert e["checked_at"]
    assert send.call_count == 1  # ONE message for both files
    text = send.call_args.args[2]
    assert "Older (1999)" in text and "Newer" in text
    assert len(alc.JSONL_FILE.read_text().splitlines()) == 2
    assert not list(alc.WORKDIR_ROOT.iterdir())  # per-file workdirs removed


def test_second_run_skips_unchanged_files_and_does_not_re_alert(tmp_path):
    _seed_library(tmp_path)
    stack, (_, probe, _, _, send) = _enter(
        _patch_world(probe_side_effect=lambda p: _info(_stream(1, "spa"))))
    with stack:
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0
        assert probe.call_count == 2 and send.call_count == 1
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0
    assert probe.call_count == 2  # nothing re-checked
    assert send.call_count == 1   # nothing re-alerted


def test_run_with_no_findings_sends_nothing(tmp_path):
    _seed_library(tmp_path)
    stack, (_, _, _, _, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "eng")),
        detect=_lid_forever(("en", 0.95))))
    with stack:
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0
    send.assert_not_called()
    assert _findings_view() == {}


def test_recheck_that_comes_back_clean_clears_the_stored_finding(tmp_path):
    older, newer = _seed_library(tmp_path)
    # the file was replaced since: the finding belongs to the old identity
    _seed_finding(newer, NO_ENGLISH, "t0",
                  [1, 2, 3])
    stack, _m = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "eng")),
        detect=_lid_forever(("en", 0.95))))
    with stack:
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0
    assert str(newer.resolve()) not in _findings_view()
    assert _identity_entry(newer)["result"] == "CHECKED"


def test_run_budget_stops_starting_new_files(tmp_path, monkeypatch):
    older, newer = _seed_library(tmp_path)

    class _Clock:
        t = 1000.0

    monkeypatch.setattr(alc.time, "monotonic", lambda: _Clock.t)
    processed = []

    def slow_process(video, cfg, jellyfin, workdir):
        processed.append(video)
        _Clock.t += 21 * 60  # longer than the 20-minute default budget
        return {"path": str(video), "result": "CHECKED", "findings": []}

    stack, (_, probe, *_rest) = _enter(_patch_world())
    with stack, patch.object(alc, "process_candidate", slow_process):
        assert _run_main([], tmp_path=tmp_path) == 0
    assert processed == [newer]  # the in-flight file finished, the next never started
    assert set(json.loads(alc.STATE_FILE.read_text())) == {str(newer.resolve())}


def test_playback_wait_timeout_ends_the_run_cleanly_without_starting_more(tmp_path):
    older, newer = _seed_library(tmp_path)
    processed = []

    def exhausted(video, cfg, jellyfin, workdir):
        processed.append(video)
        raise alc.PlaybackWaitTimeout("cumulative playback wait exceeded")

    stack, (_, _, _, _, send) = _enter(_patch_world())
    with stack, patch.object(alc, "process_candidate", exhausted):
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0
    assert processed == [newer]
    assert not alc.STATE_FILE.exists()  # nothing recorded for the interrupted file
    assert not list(alc.WORKDIR_ROOT.iterdir())  # workdir still cleaned up


def test_workdir_is_under_workdir_root_and_removed_even_when_the_check_errors(tmp_path):
    older, newer = _seed_library(tmp_path)
    seen = []

    def detect(video, idx, start, dur, model, threads, workdir, *, should_abort=None):
        seen.append(workdir)
        raise RuntimeError("whisper exploded")

    stack, _m = _enter(_patch_world(probe_side_effect=lambda p: _info(_stream(1, "eng")),
                                    detect=detect))
    with stack:
        assert _run_main([], tmp_path=tmp_path) == 0
    assert seen and all(alc.WORKDIR_ROOT == w.parent for w in seen)
    assert not list(alc.WORKDIR_ROOT.iterdir())
    identity = json.loads(alc.STATE_FILE.read_text())
    assert set(identity) == {str(older.resolve()), str(newer.resolve())}
    # an ERROR is NOT cached against the identity: a retry record only
    assert all("identity" not in e and e["retry"]["last_result"] == "ERROR"
               and "RuntimeError" in e["retry"]["last_error"] for e in identity.values())


def test_candidate_gone_or_already_checked_since_the_scan_is_skipped(tmp_path):
    older, newer = _seed_library(tmp_path)
    gone = tmp_path / "media" / "movies" / "Gone" / "gone.mkv"  # never existed
    checked = _make_video(tmp_path / "media", "movies/Done/done.mkv", HOUR, now=_now())
    stale_state = {str(checked.resolve()): {"identity": alc._identity(checked), "result": "CHECKED"}}
    alc.STATE_FILE.write_text(json.dumps(stale_state))
    processed = []

    def fake_process(video, cfg, jellyfin, workdir):
        processed.append(video)
        return {"path": str(video), "result": "CHECKED", "findings": []}

    stack, _m = _enter(_patch_world())
    with stack, \
         patch.object(alc, "select_candidates", return_value=[gone, checked, newer]), \
         patch.object(alc, "process_candidate", fake_process):
        assert _run_main([], tmp_path=tmp_path) == 0
    assert processed == [newer]


# --- --path -------------------------------------------------------------------------

def test_path_mode_records_nothing_and_never_alerts(tmp_path):
    """--path writes no state, findings, JSONL or Telegram."""
    video = _make_video(tmp_path / "media", "movies/One (2000)/one.mkv", 30.0, now=_now())
    stack, (_, probe, _, _, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "spa"))))
    with stack:
        rc = _run_main(["--path", str(video)], env=_alert_env(tmp_path), tmp_path=tmp_path)
    assert rc == 0
    send.assert_not_called()
    assert not alc.STATE_FILE.exists()
    assert not alc.REPORT_FILE.exists()
    assert not alc.JSONL_FILE.exists()
    assert not list(alc.WORKDIR_ROOT.iterdir())


def test_path_mode_does_not_suppress_the_cron_runs_alert(tmp_path):
    video = _make_video(tmp_path / "media", "movies/One (2000)/one.mkv", 5 * HOUR, now=_now())
    stack, (_, probe, _, _, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "spa")),
        detect=_lid_forever(("es", 0.95))))
    with stack:
        assert _run_main(["--path", str(video)], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0
        send.assert_not_called()
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0
    assert probe.call_count == 2  # the cron run still checked the file ...
    assert send.call_count == 1   # ... and alerted
    assert "one" in send.call_args.args[2]


def test_path_mode_does_not_read_a_corrupt_identity_state(tmp_path):
    video = _make_video(tmp_path / "media", "movies/One (2000)/one.mkv", 30.0, now=_now())
    alc.STATE_FILE.write_text("{corrupt")
    stack, _m = _enter(_patch_world(probe_side_effect=lambda p: _info(_stream(1, "eng")),
                                    detect=_lid_forever(("en", 0.9))))
    with stack:
        assert _run_main(["--path", str(video)], tmp_path=tmp_path) == 0
    assert alc.STATE_FILE.read_text() == "{corrupt"


def test_path_mode_output_is_the_json_result(tmp_path, capsys):
    video = _make_video(tmp_path / "media", "movies/One (2000)/one.mkv", 30.0, now=_now())
    stack, _m = _enter(_patch_world(probe_side_effect=lambda p: _info(_stream(1, "eng")),
                                    detect=_lid_forever(("en", 0.9))))
    with stack:
        assert _run_main(["--path", str(video)], tmp_path=tmp_path) == 0
    rec = json.loads(capsys.readouterr().out)
    assert rec["path"] == str(video.resolve())
    assert rec["result"] == "CHECKED" and rec["findings"] == []
    assert rec["streams"][0]["detected_language"] == "en"
    assert rec["original_language"] == "eng"


def test_path_mode_checks_a_file_already_recorded_at_its_current_identity(tmp_path, capsys):
    video = _make_video(tmp_path / "media", "movies/One (2000)/one.mkv", HOUR, now=_now())
    alc.STATE_FILE.write_text(json.dumps(
        {str(video.resolve()): {"identity": alc._identity(video), "result": "CHECKED"}}))
    stack, (_, probe, *_rest) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "eng")), detect=_lid_forever(("en", 0.9))))
    with stack:
        assert _run_main(["--path", str(video)], tmp_path=tmp_path) == 0
    assert probe.call_count == 1


def test_path_mode_honours_the_playback_gate(tmp_path, monkeypatch, capsys):
    import media_stack.playback as pb
    video = _make_video(tmp_path / "media", "movies/One (2000)/one.mkv", 30.0, now=_now())
    sleeps = []
    monkeypatch.setattr(pb.time, "sleep", lambda s: sleeps.append(s))
    interrupted = iter([ExtractionInterrupted("playing")])

    def detect(*a, should_abort=None, **k):
        for exc in interrupted:
            raise exc
        return ("en", 0.9)

    busy_then_idle = _Jellyfin([[], [{"NowPlayingItem": {"Name": "x"}}], []])
    stack, _m = _enter(_patch_world(probe_side_effect=lambda p: _info(_stream(1, "eng")),
                                    detect=detect))
    with stack, patch.object(alc, "JellyfinClient", lambda url: busy_then_idle):
        assert _run_main(["--path", str(video)], tmp_path=tmp_path) == 0
    assert len(sleeps) == 1
    assert json.loads(capsys.readouterr().out)["result"] == "CHECKED"


def test_path_mode_nonexistent_file_is_an_error(tmp_path):
    stack, _m = _enter(_patch_world())
    with stack:
        assert _run_main(["--path", str(tmp_path / "nope.mkv")], tmp_path=tmp_path) == 1


# --- load_config ---------------------------------------------------------------------

def test_load_config_defaults():
    with patch.object(alc.os, "cpu_count", return_value=4):
        cfg = alc.load_config({})
    assert (cfg.budget_min, cfg.max_wait_min) == (20.0, 15.0)
    assert cfg.whisper_threads == 2  # idle-priority: min(2, cpu_count)
    assert cfg.whisper_lid_model == "small"
    assert cfg.jellyfin_key == ""


@pytest.mark.parametrize("cpus,expected", [(1, 1), (2, 2), (8, 2), (None, 2)])
def test_whisper_threads_default_never_exceeds_the_cpu_count(cpus, expected):
    with patch.object(alc.os, "cpu_count", return_value=cpus):
        assert alc.load_config({}).whisper_threads == expected


def test_whisper_threads_override_is_bounded_by_cpu_count():
    with patch.object(alc.os, "cpu_count", return_value=4):
        assert alc.load_config({"WHISPER_THREADS": "4"}).whisper_threads == 4
        assert alc.load_config({"WHISPER_THREADS": "5"}).whisper_threads == 2  # over the bound
        assert alc.load_config({"WHISPER_THREADS": "0"}).whisper_threads == 2
    with patch.object(alc.os, "cpu_count", return_value=None):
        assert alc.load_config({"WHISPER_THREADS": "64"}).whisper_threads == 64
        assert alc.load_config({"WHISPER_THREADS": "65"}).whisper_threads == 2


@pytest.mark.parametrize("key,default", [("AUDIO_CHECK_BUDGET_MIN", 20.0),
                                         ("AUDIO_CHECK_MAX_WAIT_MIN", 15.0)])
@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "0", "-5", "abc", "1e308", "1440.5"])
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


# --- helper sanity ----------------------------------------------------------------------

def test_lid_helper_repeats_the_last_result_and_fails_loudly_for_an_unscripted_stream():
    fake = _lid({1: [("en", 0.9), ("es", 0.8)]})
    assert fake(Path("/v"), 1, 0.0, 30.0, "small", 2, Path("/w")) == ("en", 0.9)
    assert fake(Path("/v"), 1, 0.0, 30.0, "small", 2, Path("/w")) == ("es", 0.8)
    assert fake(Path("/v"), 1, 0.0, 30.0, "small", 2, Path("/w")) == ("es", 0.8)
    with pytest.raises(KeyError):
        fake(Path("/v"), 2, 0.0, 30.0, "small", 2, Path("/w"))


def _one_video(tmp_path, age=5 * HOUR):
    return _make_video(tmp_path / "media", "movies/One (2000)/one.mkv", age, now=_now())


def _identity_entry(video):
    return json.loads(alc.STATE_FILE.read_text())[str(video.resolve())]


# --- transient failures are retried with backoff, never cached -------------

def test_probe_failure_is_not_cached_and_gets_a_backoff_record(tmp_path):
    video = _one_video(tmp_path)
    stack, (_, probe, _, _, send) = _enter(_patch_world(probe_side_effect=lambda p: None))
    with stack:
        before = time.time()
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0
    entry = _identity_entry(video)
    assert "identity" not in entry and "result" not in entry  # NOT cached against the file
    retry = entry["retry"]
    assert retry["identity"] == alc._identity(video)
    assert retry["attempts"] == 1 and retry["last_result"] == "PROBE_FAILED"
    assert before + 3600 <= retry["next_retry_at"] <= time.time() + 3600
    assert _findings_view() == {}
    send.assert_not_called()
    assert json.loads(alc.JSONL_FILE.read_text().splitlines()[0])["result"] == "PROBE_FAILED"


def test_a_file_backing_off_is_skipped_until_next_retry_at_then_retried_with_double_delay(tmp_path):
    video = _one_video(tmp_path)
    stack, (_, probe, *_rest) = _enter(_patch_world(probe_side_effect=lambda p: None))
    with stack:
        _run_main([], tmp_path=tmp_path)
        assert probe.call_count == 1
        _run_main([], tmp_path=tmp_path)  # still inside the 1 h backoff
        assert probe.call_count == 1
        entry = _identity_entry(video)
        entry["retry"]["next_retry_at"] = time.time() - 1  # backoff elapsed
        alc.STATE_FILE.write_text(json.dumps({str(video.resolve()): entry}))
        _run_main([], tmp_path=tmp_path)
        assert probe.call_count == 2
    retry = _identity_entry(video)["retry"]
    assert retry["attempts"] == 2
    assert retry["next_retry_at"] - time.time() == pytest.approx(7200, abs=60)


@pytest.mark.parametrize("attempts,delay", [
    (1, 3600), (2, 7200), (3, 14400), (4, 28800), (5, 57600), (6, 86400), (7, 86400), (500, 86400)])
def test_backoff_doubles_from_one_hour_and_caps_at_24_hours(attempts, delay):
    assert alc.backoff_s(attempts) == delay


def test_a_changed_file_resets_the_backoff(tmp_path):
    video = _one_video(tmp_path)
    stack, (_, probe, *_rest) = _enter(_patch_world(probe_side_effect=lambda p: None))
    with stack:
        _run_main([], tmp_path=tmp_path)
        entry = _identity_entry(video)
        entry["retry"]["attempts"] = 4
        alc.STATE_FILE.write_text(json.dumps({str(video.resolve()): entry}))
        video.write_bytes(b"re-encoded and longer")  # new size AND mtime
        os.utime(video, (_now() - 5 * HOUR, _now() - 5 * HOUR))
        _run_main([], tmp_path=tmp_path)  # due at once despite the 4-attempt backoff
        assert probe.call_count == 2
    assert _identity_entry(video)["retry"]["attempts"] == 1


@pytest.mark.parametrize("bad", [None, "soon", True, float("nan"), float("inf")])
def test_malformed_next_retry_at_counts_as_due(tmp_path, bad):
    video = _one_video(tmp_path)
    ident = alc._identity(video)
    state = {str(video.resolve()): {"retry": {"identity": ident, "next_retry_at": bad}}}
    assert alc.select_candidates(state, tmp_path / "media", _now()) == [video]


def test_select_candidates_skips_backing_off_files_and_includes_due_ones(tmp_path):
    video = _one_video(tmp_path)
    ident = alc._identity(video)
    future = {str(video.resolve()): {"retry": {"identity": ident, "next_retry_at": NOW * 2}}}
    assert alc.select_candidates(future, tmp_path / "media", _now()) == []
    due = {str(video.resolve()): {"retry": {"identity": ident, "next_retry_at": 1.0}}}
    assert alc.select_candidates(due, tmp_path / "media", _now()) == [video]
    other = {str(video.resolve()): {"retry": {"identity": [0, 0, 0], "next_retry_at": NOW * 2}}}
    assert alc.select_candidates(other, tmp_path / "media", _now()) == [video]  # different file


def test_arr_outage_is_retried_not_cached_and_does_no_media_io(tmp_path):
    """Radarr/Sonarr unreachable is not 'unknown title'."""
    video = _one_video(tmp_path)
    stack, (_, probe, _, detect, send) = _enter(_patch_world())
    with stack, patch.object(alc, "original_language_lookup", return_value=(None, "unavailable")):
        _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
    entry = _identity_entry(video)
    assert "identity" not in entry and entry["retry"]["last_result"] == "ARR_UNAVAILABLE"
    probe.assert_not_called()
    send.assert_not_called()


def test_a_reachable_arr_with_no_answer_is_cached_normally(tmp_path):
    video = _one_video(tmp_path)
    stack, _m = _enter(_patch_world(probe_side_effect=lambda p: _info(_stream(1, "eng")),
                                    detect=_lid_forever(("en", 0.9)), original=None))
    with stack:
        _run_main([], tmp_path=tmp_path)
    assert _identity_entry(video)["result"] == "CHECKED"


def test_transient_failure_does_not_clear_or_change_an_existing_finding(tmp_path):
    video = _one_video(tmp_path)
    finding = NO_ENGLISH
    _seed_finding(video, finding, "t0")
    stack, _m = _enter(_patch_world(probe_side_effect=lambda p: None))
    with stack:
        _run_main([], tmp_path=tmp_path)  # no Telegram credentials: the alert state stays put too
    assert alc.STATE_FILE.exists()
    assert _findings_view() == {
        str(video.resolve()): {"findings": finding, "first_found": "t0", "alerted": False,
                               "identity": alc._identity(video)}}


def test_later_success_replaces_the_retry_record_with_a_cached_result(tmp_path):
    video = _one_video(tmp_path)
    good = {"fail": True}

    def flaky_probe(p):
        return None if good["fail"] else _info(_stream(1, "eng"))

    stack, (_, probe, *_rest) = _enter(_patch_world(probe_side_effect=flaky_probe,
                                                    detect=_lid_forever(("en", 0.9))))
    with stack:
        _run_main([], tmp_path=tmp_path)
        entry = _identity_entry(video)
        entry["retry"]["next_retry_at"] = 0
        alc.STATE_FILE.write_text(json.dumps({str(video.resolve()): entry}))
        good["fail"] = False
        _run_main([], tmp_path=tmp_path)
    entry = _identity_entry(video)
    assert "retry" not in entry and entry["result"] == "CHECKED"
    assert entry["identity"] == alc._identity(video)


def test_missing_whisper_dependency_ends_the_run_with_exit_1_but_still_retries_alerts(tmp_path):
    older, newer = _seed_library(tmp_path)
    pending = _make_video(tmp_path / "media", "movies/Pending (2001)/p.mkv", HOUR, now=_now())
    _seed_finding(pending, MISLABEL, "t0")  # an earlier run's unsent alert
    stack, (_, probe, _, _, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "eng")),
        detect=lambda *a, **k: (_ for _ in ()).throw(ModuleNotFoundError("faster_whisper"))))
    with stack:
        rc = _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
    assert rc == 1
    assert probe.call_count == 1  # the second file was never started
    entries = _state()
    assert [e["retry"]["last_result"] for e in entries.values() if "retry" in e] == [
        "DEPENDENCY_MISSING"]
    assert send.call_count == 1  # the pending alert still went out


# --- arr status plumbing reaches the script's lookup -----------------------

def test_script_uses_the_status_aware_lookup_not_original_language_for():
    assert alc.original_language_lookup.__module__ == "media_stack.arrlang"
    assert not hasattr(alc, "original_language_for")


# --- write order: findings first, identity second ---------------------------

def _spy_mutate(calls, fail_for=None):
    real = alc.mutate_state

    def spy(state_file, fn):
        calls.append(state_file)
        if fail_for is not None and state_file == fail_for:
            raise OSError("disk full")
        return real(state_file, fn)

    return spy


def test_conclusion_findings_and_identity_are_recorded_by_one_state_update(tmp_path):
    """One state file, one locked update: no second file whose consistency could be lost."""
    video = _one_video(tmp_path)
    calls = []
    stack, _m = _enter(_patch_world(probe_side_effect=lambda p: _info(_stream(1, "spa")),
                                    detect=_lid_forever(("es", 0.95))))
    with stack, patch.object(alc, "mutate_state", _spy_mutate(calls)):
        _run_main([], tmp_path=tmp_path)  # no Telegram credentials: no alert update
    assert calls == [alc.STATE_FILE]
    entry = _identity_entry(video)
    assert entry["identity"] == alc._identity(video) and entry["result"] == "CHECKED"
    assert [f["type"] for f in entry["findings"]] == ["NO_ENGLISH_AUDIO"]
    assert entry["alerted"] is False and entry["first_found"] == entry["checked_at"]


def test_failed_state_write_records_nothing_so_the_next_run_rechecks_and_alerts(tmp_path):
    video = _one_video(tmp_path)
    stack, (_, probe, _, _, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "spa")), detect=_lid_forever(("es", 0.95))))
    with stack:
        with patch.object(alc, "mutate_state", _spy_mutate([], fail_for=alc.STATE_FILE)):
            assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0  # still succeeds
        assert not alc.STATE_FILE.exists()  # NOT recorded
        send.assert_not_called()
        assert probe.call_count == 1
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0
    assert probe.call_count == 2  # re-checked
    assert send.call_count == 1   # and alerted
    assert _identity_entry(video)["result"] == "CHECKED"


def test_failed_state_write_leaves_an_earlier_entry_exactly_as_it_was(tmp_path):
    video = _one_video(tmp_path)
    _seed_finding(video, MISLABEL, "t0", [1, 2, 3])
    before = alc.STATE_FILE.read_text()
    stack, _m = _enter(_patch_world(probe_side_effect=lambda p: _info(_stream(1, "eng")),
                                    detect=_lid_forever(("en", 0.9))))
    with stack, patch.object(alc, "mutate_state", _spy_mutate([], fail_for=alc.STATE_FILE)):
        _run_main([], tmp_path=tmp_path)
    assert alc.STATE_FILE.read_text() == before


# --- independent windows, durations -------------------------------------------

def _w(start, lang, prob):
    return {"fraction": 0, "start": start, "language": lang, "probability": prob}


def test_mislabeled_needs_the_agreeing_windows_at_least_60s_apart():
    assert alc._mislabeled_language([_w(100.0, "es", 0.9), _w(130.0, "es", 0.9)]) is None
    assert alc._mislabeled_language([_w(100.0, "es", 0.9), _w(159.9, "es", 0.9)]) is None
    assert alc._mislabeled_language([_w(100.0, "es", 0.9), _w(160.0, "es", 0.85)]) == ("es", 0.85)


def test_mislabeled_pairs_the_windows_that_are_far_enough_apart():
    ws = [_w(100.0, "es", 0.9), _w(120.0, "es", 0.95), _w(300.0, "es", 0.88)]
    assert alc._mislabeled_language(ws) == ("es", 0.88)


def test_scan_skips_fractions_whose_window_would_land_within_60s_of_a_sampled_one():
    _, fake = _analyze(_info(_stream(1, "eng"), duration=200.0),
                       {1: [("es", 0.9), ("es", 0.9)]})
    # 25% -> 50.  50% (100), 10% (20) are < 60 s away: skipped.  75% -> 150.
    assert [c["start"] for c in fake.calls] == [50.0, 150.0]


def test_a_three_minute_file_can_still_be_mislabel_checked():
    (findings, _, result, _), fake = _analyze(
        _info(_stream(1, "eng"), duration=180.0), {1: [("es", 0.9), ("es", 0.9)]})
    assert [c["start"] for c in fake.calls] == [45.0, 135.0]
    assert _types(findings) == ["MISLABELED"] and result == "CHECKED"


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
_COMMENTARY = _stream(2, "eng", disposition={"comment": 1}, duration="90")


@pytest.mark.parametrize("info", [
    *(pytest.param({"streams": [_stream(1, "eng", duration=bad)], "format": {"duration": bad}},
                   id=f"bad_{bad}") for bad in [None, "N/A", "0", "-3", "nan", "inf", True, "abc"]),
    pytest.param({"streams": [_COVER_ART, _stream(1, "eng")], "format": {}},
                 id="only_a_video_stream_has_a_duration"),
    pytest.param({"streams": [_stream(1, "eng"), _COMMENTARY], "format": {}},
                 id="only_a_short_commentary_stream_has_a_duration"),
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
    pytest.param({"streams": [_stream(1, "eng", duration="1000"), _stream(2, "fra", duration="2000")],
                  "format": {}},
                 {1: [("en", 0.9)], 2: [("fr", 0.9), ("fr", 0.9)]}, 250.0,
                 id="first_scanned_audio_stream_is_preferred"),
    pytest.param({"streams": [_stream(1, "eng", disposition={"comment": 1}, duration="90000"),
                              _stream(2, "eng", duration="1200")], "format": {}},
                 {2: [("en", 0.9)]}, 300.0, id="commentary_stream_is_not_a_source"),
    # 25% of stream 3's 1200, not of the commentary's 90
    pytest.param({"streams": [_stream(1, "eng"), _COMMENTARY, _stream(3, "fra", duration="1200")],
                  "format": {}},
                 {1: [("en", 0.9)], 3: [("fr", 0.9), ("fr", 0.9)]}, 300.0,
                 id="next_allowed_stream_when_the_first_has_none"),
])
def test_duration_comes_from_the_first_scanned_audio_stream_that_has_one(info, script, expected_start):
    _, fake = _analyze(info, script)
    assert fake.calls[0]["start"] == expected_start


# --- re-validate identity and the settle rule around the analysis ---------------

def _stub_process(mutate):
    def process(video, cfg, jellyfin, workdir):
        mutate(video)
        return {"path": str(video), "result": "CHECKED", "findings": list(MISLABEL)}
    return process


def _rewrite_mid_analysis(video):
    video.write_bytes(b"replaced mid-analysis")
    os.utime(video, (_now() - 5 * HOUR, _now() - 5 * HOUR))  # settled again, but different


def _touch_into_settle_window(video):
    os.utime(video, None)  # mtime = now: inside the 10-minute window


def _discarded(tmp_path, mutate):
    video = _one_video(tmp_path)
    stack, (_, probe, _, _, send) = _enter(_patch_world())
    with stack, patch.object(alc, "process_candidate", _stub_process(mutate)):
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0
    return video, send


@pytest.mark.parametrize("mutate", [_rewrite_mid_analysis, _touch_into_settle_window],
                         ids=["file_changes_during_analysis", "file_touched_into_the_settle_window"])
def test_result_is_discarded_when_the_file_changes_during_analysis(tmp_path, mutate):
    video, send = _discarded(tmp_path, mutate)
    assert not alc.STATE_FILE.exists()
    assert json.loads(alc.REPORT_FILE.read_text()) == {}  # the derived report: nothing unresolved
    assert not alc.JSONL_FILE.exists()
    send.assert_not_called()


def test_a_discarded_file_is_picked_up_by_a_later_run(tmp_path):
    video = _one_video(tmp_path)
    stack, (_, probe, _, _, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "spa")), detect=_lid_forever(("es", 0.95))))
    with stack:
        with patch.object(alc, "process_candidate", _stub_process(_rewrite_mid_analysis)):
            _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
        assert probe.call_count == 0
        _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
    assert probe.call_count == 1 and send.call_count == 1
    assert _identity_entry(video)["identity"] == alc._identity(video)


def test_a_transient_failure_on_a_file_that_changed_is_discarded_too(tmp_path):
    _one_video(tmp_path)
    stack, _m = _enter(_patch_world())

    def failing(v, cfg, jf, wd):
        _touch_into_settle_window(v)
        return {"path": str(v), "result": "PROBE_FAILED", "findings": []}

    with stack, patch.object(alc, "process_candidate", failing):
        _run_main([], tmp_path=tmp_path)
    assert not alc.STATE_FILE.exists()  # not even a retry record


def test_candidate_touched_after_the_scan_is_skipped_before_processing(tmp_path):
    video = _one_video(tmp_path, age=30.0)  # modified 30 s ago: not settled
    processed = []
    stack, _m = _enter(_patch_world())
    with stack, patch.object(alc, "select_candidates", return_value=[video]), \
         patch.object(alc, "process_candidate", lambda *a: processed.append(a)):
        assert _run_main([], tmp_path=tmp_path) == 0
    assert processed == []


# --- the cron entry ----------------------------------------------------------------

def _minutes(field: str) -> set[int]:
    if field == "*":
        return set(range(60))
    if field.startswith("*/"):
        return set(range(0, 60, int(field[2:])))
    return {int(x) for x in field.split(",")}


def test_cron_entry_start_minute_collides_with_no_other_job():
    lines = [ln for ln in (PROJECT_ROOT / "ops/cron.d/media-stack.crontab").read_text().splitlines()
             if ln.strip() and not ln.startswith("#")]
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


# --- classify on ALL windows, not the best one ---------------------------

def test_spanish_tag_heard_spanish_095_and_english_070_is_english_not_a_finding():
    """The best window is Spanish, but any English window at >= 0.6 makes the stream English."""
    (findings, streams, result, _), fake = _analyze(
        _info(_stream(1, "spa")), {1: [("es", 0.95), ("en", 0.70)]}, original="eng")
    assert findings == [] and result == "CHECKED"
    assert streams[0]["counts_as_english"] is True
    assert streams[0]["detected_language"] == "es"  # display still shows the best window
    assert len(fake.calls) == 2


def test_an_unclear_stream_does_not_block_a_confirmed_english_one():
    (findings, _, result, _), _ = _analyze(
        _info(_stream(1, "spa"), _stream(2, "eng")),
        {1: [("es", 0.95), ("es", 0.3), ("es", 0.2), ("es", 0.1)], 2: [("en", 0.9)]},
        original="eng")
    assert findings == [] and result == "CHECKED"


@pytest.mark.parametrize("probs,expected", [
    ([("en", 0.6)], "english"),
    ([("es", 0.95), ("en", 0.7)], "english"),
    ([("es", 0.95), ("fr", 0.9)], "foreign"),
    ([("es", 0.6)], "foreign"),
    ([("es", 0.95), ("es", 0.59)], "unclear"),
    ([("en", 0.59), ("es", 0.9)], "unclear"),
    ([("en", 0.2)], "unclear"),
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


def test_a_later_english_window_that_is_not_confident_does_not_cancel_mislabeled():
    (findings, _, _, _), fake = _analyze(
        _info(_stream(1, "eng")), {1: [("es", 0.9), ("es", 0.9), ("en", 0.59), ("en", 0.4)]})
    assert len(fake.calls) == 4
    assert _types(findings) == ["MISLABELED"]


# --- raw probabilities, rounding only for display --------------------------------

@pytest.mark.parametrize("script, expected", [
    pytest.param([("es", 0.7996)] * 4, [], id="0.7996_displays_as_0.8_but_is_below_the_bar"),
    pytest.param([("es", 0.8), ("es", 0.8)], ["MISLABELED"], id="exactly_the_bar_counts"),
])
def test_mislabeled_bar_uses_the_raw_probability(script, expected):
    (findings, _, _, _), fake = _analyze(_info(_stream(1, "eng")), {1: script})
    assert _types(findings) == expected and len(fake.calls) == 4


def test_probabilities_are_rounded_only_in_the_stored_record():
    (findings, streams, _, _), _ = _analyze(
        _info(_stream(1, "eng")), {1: [("es", 0.912345), ("es", 0.876543)]})
    assert findings[0]["probability"] == 0.877
    assert streams[0]["probability"] == 0.912
    assert [w["probability"] for w in streams[0]["windows"]] == [0.912, 0.877, 0.877, 0.877]


# --- nothing to listen to -------------------------------------------------------

def test_main_flow_records_a_stream_less_file_as_unverified_without_alerting(tmp_path):
    video = _one_video(tmp_path)
    stack, (_, probe, _, detect, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info()))
    with stack:
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0
    assert _identity_entry(video)["result"] == "UNVERIFIED"
    assert _identity_entry(video)["findings"] == []
    send.assert_not_called()
    assert _findings_view() == {}


# --- findings are bound to the file identity ---------------------------------------

def _replace(video, content=b"a different, longer release", age=5 * HOUR):
    video.write_bytes(content)
    os.utime(video, (_now() - age, _now() - age))


def test_retry_alerts_never_sends_a_finding_for_a_file_that_is_gone(tmp_path):
    video = _finding_video(tmp_path)
    _seed_finding(video, MISLABEL, "t0")
    video.unlink()
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True), \
         patch.object(alc, "telegram_send", return_value=True) as send:
        alc.retry_alerts()
    send.assert_not_called()
    assert _findings_view() == {}


def test_stale_pending_findings_are_left_out_of_the_message_and_its_count(tmp_path):
    fresh, replaced = _finding_video(tmp_path, 1), _finding_video(tmp_path, 2)
    _seed_finding(fresh, MISLABEL, "t1")
    _seed_finding(replaced, MISLABEL, "t2")
    _replace(replaced)
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True), \
         patch.object(alc, "telegram_send", return_value=True) as send:
        alc.retry_alerts()
    text = send.call_args.args[2]
    assert "Film 01" in text and "Film 02" not in text
    assert "1 file(s)" in text


def test_a_finding_without_a_stored_identity_is_treated_as_stale(tmp_path):
    video = _finding_video(tmp_path)
    alc.STATE_FILE.write_text(json.dumps(
        {str(video.resolve()): {"findings": MISLABEL, "first_found": "t0", "alerted": False}}))
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True), \
         patch.object(alc, "telegram_send", return_value=True) as send:
        alc.retry_alerts()
    send.assert_not_called()


def test_stale_cleanup_keeps_an_entry_that_was_rerecorded_in_the_meantime(tmp_path):
    video = _finding_video(tmp_path)
    key = str(video.resolve())
    snapshot = {"findings": MISLABEL, "first_found": "t0", "alerted": False, "identity": [1, 2, 3]}
    fresh = {"findings": MISLABEL, "first_found": "t1", "alerted": False,
             "identity": alc._identity(video)}
    alc.STATE_FILE.write_text(json.dumps({key: fresh}))
    alc._strip_stale_findings({key: snapshot})
    assert _findings_view() == {key: fresh}


def test_alerted_stale_entries_are_not_touched_by_the_pending_cleanup(tmp_path):
    video = _finding_video(tmp_path)
    _seed_finding(video, MISLABEL, "t0")
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True), \
         patch.object(alc, "telegram_send", return_value=True):
        alc.retry_alerts()
        _replace(video)  # already alerted: no re-alert, no churn
        alc.retry_alerts()
    assert _findings_view()[str(video.resolve())]["alerted"] is True


def test_replacement_that_comes_back_unverified_drops_the_old_files_finding(tmp_path):
    video = _one_video(tmp_path)
    old_identity = [1, 2, 3]  # the file that was replaced
    _seed_finding(video, MISLABEL, "t0", old_identity)
    stack, (_, probe, _, _, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "spa")), detect=_lid_forever(("es", 0.2))))
    with stack:
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0
    assert probe.call_count == 1
    assert _identity_entry(video)["result"] == "UNVERIFIED"
    assert _findings_view() == {}
    send.assert_not_called()


@pytest.mark.parametrize("old_findings", [MISLABEL, NO_ENGLISH], ids=["different_findings", "identical_findings"])
def test_a_replacement_is_analysed_on_its_own_merits_and_its_finding_is_alerted(tmp_path, old_findings):
    """A replacement carrying even the same defect is a different file and is alerted on its own."""
    video = _one_video(tmp_path)
    _seed_finding(video, old_findings, "t0", [1, 2, 3])  # the replaced file: already alerted
    _set_alerted(video)
    stack, (_, _, _, _, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "spa")), detect=_lid_forever(("es", 0.95))))
    with stack:
        _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)  # new file: NO_ENGLISH_AUDIO
    entry = _findings_view()[str(video.resolve())]
    assert entry["findings"] == NO_ENGLISH
    assert entry["identity"] == alc._identity(video)
    assert entry["alerted"] is True  # ... because it WAS alerted, once, this run
    assert send.call_count == 1
    assert entry["first_found"] != "t0"  # restarted for the new file


def test_a_finding_rewritten_while_the_message_was_sending_is_not_marked_alerted(tmp_path):
    video = _finding_video(tmp_path)
    _seed_finding(video, MISLABEL, "t0")
    newer = NO_ENGLISH

    def notify_and_meanwhile_rerecord(message):
        _seed_finding(video, newer, "t1")
        return True

    with patch.object(alc, "_notify", notify_and_meanwhile_rerecord):
        alc.retry_alerts()
    entry = _findings_view()[str(video.resolve())]
    assert entry["findings"] == newer and entry["alerted"] is False


# --- the Telegram message is bounded by LENGTH ------------------------------------

def _long_folder_video(tmp_path, n, width=200):
    folder = f"Film {n:02d} " + "x" * (width - 8)
    stem = f"File {n:02d} " + "y" * (width - 8)
    return _make_video(tmp_path / "media", f"movies/{folder}/{stem}.mkv", HOUR, now=_now())


def test_fifteen_very_long_titles_make_one_message_within_telegram_s_limit(tmp_path):
    videos = [_long_folder_video(tmp_path, i) for i in range(15)]
    for i, v in enumerate(videos):
        _seed_finding(v, MISLABEL, f"2026-10-01T00:00:{i:02d}+00:00")
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True), \
         patch.object(alc, "telegram_send", return_value=True) as send:
        alc.retry_alerts()
    assert send.call_count == 1
    text = send.call_args.args[2]
    assert alc._tg_len(text) <= alc.TELEGRAM_BUDGET_CHARS < 4096
    named = text.count("\n- ")
    assert 0 < named < 15                      # not all 15 fit ...
    assert f"...and {15 - named} more" in text  # ... and the tail says how many were left out
    state = _findings_view()
    assert sum(e["alerted"] for e in state.values()) == named  # only the NAMED ones


def test_items_beyond_the_length_budget_are_sent_by_later_runs_oldest_first(tmp_path):
    videos = [_long_folder_video(tmp_path, i) for i in range(15)]
    for i, v in enumerate(videos):
        _seed_finding(v, MISLABEL, f"2026-10-01T00:00:{i:02d}+00:00")
    sent = []
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True), \
         patch.object(alc, "telegram_send", side_effect=lambda t, c, m: sent.append(m) or True):
        for _ in range(10):
            alc.retry_alerts()
            state = _findings_view()
            if all(e["alerted"] for e in state.values()):
                break
    assert all(e["alerted"] for e in state.values())
    assert 1 < len(sent) < 10
    assert all(alc._tg_len(m) <= alc.TELEGRAM_BUDGET_CHARS for m in sent)
    # each finding named exactly once, oldest first across the messages
    order = [int(line.split("Film ")[1][:2]) for m in sent for line in m.splitlines()
             if line.startswith("- Film ")]
    assert order == list(range(15))


def test_a_single_ten_thousand_character_title_is_truncated_with_an_ellipsis(tmp_path):
    video = _finding_video(tmp_path)
    _seed_finding(video, MISLABEL, "t0")
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True), \
         patch.object(alc, "_title_for", return_value="T" * 10_000), \
         patch.object(alc, "telegram_send", return_value=True) as send:
        alc.retry_alerts()
    text = send.call_args.args[2]
    item = text.splitlines()[1]
    assert item.endswith("\u2026") and alc._tg_len(item) == alc.MAX_ALERT_LINE_CHARS
    assert alc._tg_len(text) < 4096
    assert _findings_view()[str(video.resolve())]["alerted"] is True


def test_telegram_counts_utf16_so_astral_characters_are_budgeted_double(tmp_path):
    videos = [_finding_video(tmp_path, i) for i in range(8)]
    for i, v in enumerate(videos):
        _seed_finding(v, MISLABEL, f"2026-10-01T00:00:{i:02d}+00:00")
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True), \
         patch.object(alc, "_title_for", return_value="\U0001F600" * 590), \
         patch.object(alc, "telegram_send", return_value=True) as send:
        alc.retry_alerts()
    text = send.call_args.args[2]
    # Eight ~600-unit lines are ~4800 UTF-16 units but ~2400 code points; counting code points would exceed Telegram's limit.
    assert len(text.encode("utf-16-le")) // 2 <= alc.TELEGRAM_BUDGET_CHARS
    assert len(text) < 3800 and "...and" in text


def test_message_never_exceeds_the_budget_for_any_item_size():
    """Property-style sweep over line lengths and counts, tail included."""
    for width in range(1, 800, 37):
        for n in (1, 2, 5, 15, 40):
            pending = [(f"/m/movies/F{i}/f.mkv", {"findings": MISLABEL, "first_found": str(i)})
                       for i in range(n)]
            with patch.object(alc, "_title_for", return_value="t" * width):
                text, named = alc.build_alert_message(pending)
            assert alc._tg_len(text) <= alc.TELEGRAM_BUDGET_CHARS, (width, n)
            assert 1 <= len(named) <= min(n, alc.MAX_ALERT_ITEMS)  # the oldest always fits
            assert (f"...and {n - len(named)} more" in text) == (len(named) < n)


def test_truncate_helper_edges():
    assert alc._truncate("short", 10) == "short"
    assert alc._truncate("x" * 10, 10) == "x" * 10
    assert alc._truncate("x" * 11, 10) == "x" * 9 + "\u2026"
    assert alc._tg_len(alc._truncate("\U0001F600" * 20, 11)) <= 11


def test_a_failed_send_does_not_block_later_findings_forever(tmp_path):
    """After a failed send the next run still names the oldest items that fit."""
    videos = [_long_folder_video(tmp_path, i) for i in range(15)]
    for i, v in enumerate(videos):
        _seed_finding(v, MISLABEL, f"2026-10-01T00:00:{i:02d}+00:00")
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True):
        with patch.object(alc, "telegram_send", return_value=False) as send:
            alc.retry_alerts()
        assert alc._tg_len(send.call_args.args[2]) <= alc.TELEGRAM_BUDGET_CHARS
        before = _findings_view()
        assert not any(e["alerted"] for e in before.values())
        with patch.object(alc, "telegram_send", return_value=True) as send:
            alc.retry_alerts()
    assert "Film 00" in send.call_args.args[2]  # oldest first
    after = _findings_view()
    assert any(e["alerted"] for e in after.values())


# --- a CHANGED identity resets alerted, an unchanged one keeps it --------------------

def test_record_result_keeps_alerted_only_for_the_same_identity_and_findings(tmp_path):
    video = _finding_video(tmp_path)
    key = str(video.resolve())
    _seed_finding(video, MISLABEL, "t0", [1, 2, 3])
    _set_alerted(video)

    _seed_finding(video, [dict(MISLABEL[0])], "t1", [1, 2, 3])  # same file, same findings
    entry = _findings_view()[key]
    assert entry["alerted"] is True and entry["first_found"] == "t0"

    _seed_finding(video, [dict(MISLABEL[0])], "t2", [9, 9, 9])  # NEW file, same findings
    entry = _findings_view()[key]
    assert entry == {"findings": MISLABEL, "first_found": "t2", "alerted": False,
                     "identity": [9, 9, 9]}


def test_same_identity_with_changed_findings_resets_alerted_but_keeps_first_found(tmp_path):
    video = _finding_video(tmp_path)
    key = str(video.resolve())
    _seed_finding(video, MISLABEL, "t0", [1, 2, 3])
    _set_alerted(video)
    other = NO_ENGLISH
    _seed_finding(video, other, "t1", [1, 2, 3])
    entry = _findings_view()[key]
    assert entry["findings"] == other and entry["alerted"] is False and entry["first_found"] == "t0"


def test_legacy_entry_without_an_identity_counts_as_a_changed_file(tmp_path):
    video = _finding_video(tmp_path)
    key = str(video.resolve())
    alc.STATE_FILE.write_text(json.dumps(
        {key: {"findings": MISLABEL, "first_found": "t0", "alerted": True}}))
    _seed_finding(video, MISLABEL, "t1")
    entry = _findings_view()[key]
    assert entry["alerted"] is False and entry["identity"] == alc._identity(video)


def test_touching_a_file_with_the_same_defect_realerts_it_once(tmp_path):
    """Accepted cost of binding alerts to identity: a content-unchanged touch re-alerts its finding once."""
    video = _one_video(tmp_path)
    stack, (_, _, _, _, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "spa")), detect=_lid_forever(("es", 0.95))))
    with stack:
        _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
        assert send.call_count == 1
        os.utime(video, (_now() - 4 * HOUR, _now() - 4 * HOUR))  # touched, not changed
        _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
        assert send.call_count == 2
        _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
        assert send.call_count == 2


# --- a duration comes from an AUDIO stream only --------------------------------

@pytest.mark.parametrize("streams, script, expected", [
    pytest.param([_stream(3, "eng"), _stream(4, "fra")],
                 {3: [("en", 0.9)], 4: [("fr", 0.9), ("fr", 0.9)]}, (3, {3, 4}),
                 id="first_scanned_stream"),
    pytest.param([_stream(1, "eng"), _stream(2, "eng", disposition={"comment": 1}), _stream(3, "fra")],
                 {1: [("en", 0.9)], 3: [("fr", 0.9), ("fr", 0.9)]}, (1, {1, 3}),
                 id="non_commentary_indices_only"),
])
def test_the_fallback_probe_is_told_the_first_scanned_stream_and_the_allowed_indices(streams, script, expected):
    fake = _lid(script)
    with patch.object(alc, "detect_language", fake), \
         patch.object(alc, "probe_duration", return_value=800.0) as pd:
        alc.analyze_candidate(Path("/m/movies/X/x.mkv"), {"streams": streams, "format": {}}, None,
                              _cfg(), _Jellyfin(), alc.PlaybackWaitBudget(15.0), Path("/work"))
    assert pd.call_args.args == (Path("/m/movies/X/x.mkv"), *expected)


def test_program_indices_are_the_integer_indices_of_the_non_commentary_audio_streams():
    assert alc._program_indices([_stream(1, "eng"), _stream(4, "fra")]) == {1, 4}
    assert alc._program_indices([{"codec_type": "audio"}, {"index": "7"}, _stream(2, None)]) == {2}


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
                      ("JSONL_FILE", "log/x.jsonl"), ("STATE_FILE", "state/i.json"),
                      ("REPORT_FILE", "state/f.json")):
        monkeypatch.setattr(alc, name, fresh / rel)
    return fresh


def test_dry_run_lists_candidates_newest_first_and_mutates_nothing(tmp_path, monkeypatch, capsys):
    older, newer = _seed_library(tmp_path)
    fresh = _point_runtime_paths_at_a_tree_that_does_not_exist(monkeypatch, tmp_path)  # needs no writable var/
    before = _real_var_snapshot()
    stack, (_, probe, _, detect, send) = _enter(_patch_world())
    with stack, patch.object(alc, "acquire_singleton_lock") as lock:
        # no JELLYFIN_API_KEY needed for a dry run
        assert _run_main(["--dry-run"], env={"JELLYFIN_API_KEY": ""}, tmp_path=tmp_path) == 0
    out = capsys.readouterr().out
    assert "DRY RUN: 2 candidate(s)" in out
    assert str(older) in out and str(newer) in out
    assert out.index(str(newer)) < out.index(str(older))  # newest first
    alc.ensure_var_dirs.assert_not_called()
    lock.assert_not_called()
    assert not fresh.exists()
    assert _real_var_snapshot() == before
    probe.assert_not_called()
    send.assert_not_called()


def test_dry_run_combined_with_path_is_a_usage_error_that_touches_nothing(tmp_path, monkeypatch, capsys):
    """No WRITES (reads such as paths.py's import-time .env parse are by design)."""
    video = _make_video(tmp_path / "media", "movies/One (2000)/one.mkv", 30.0, now=_now())
    fresh = _point_runtime_paths_at_a_tree_that_does_not_exist(monkeypatch, tmp_path)
    before = _real_var_snapshot()
    stack, (_, probe, _, detect, send) = _enter(_patch_world())
    with stack, patch.object(alc, "acquire_singleton_lock") as lock:
        with pytest.raises(SystemExit) as exc:
            _run_main(["--dry-run", "--path", str(video)], env={"JELLYFIN_API_KEY": ""}, tmp_path=tmp_path)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--dry-run cannot be combined with --path" in err and "usage:" in err
    probe.assert_not_called()
    send.assert_not_called()
    alc.ensure_var_dirs.assert_not_called()  # no directories created
    lock.assert_not_called()                 # no lock file written
    assert not fresh.exists()                # none of the runtime tree appeared
    assert _real_var_snapshot() == before    # and the real var/ was not written to


def test_real_run_and_path_mode_create_the_runtime_dirs_before_taking_the_lock(tmp_path):
    video = _one_video(tmp_path)
    events = []
    alc.ensure_var_dirs.side_effect = lambda: events.append("dirs")
    real_lock = alc.acquire_singleton_lock

    def spy_lock(path):
        events.append("lock")
        return real_lock(path)

    stack, _m = _enter(_patch_world(probe_side_effect=lambda p: _info(_stream(1, "eng")),
                                    detect=_lid_forever(("en", 0.9))))
    with stack, patch.object(alc, "acquire_singleton_lock", spy_lock):
        assert _run_main([], tmp_path=tmp_path) == 0
        assert events == ["dirs", "lock"]
        events.clear()
        assert _run_main(["--path", str(video)], tmp_path=tmp_path) == 0
        assert events == ["dirs", "lock"]


# --- --path must say so when another run holds the lock ---------------------------

def test_path_mode_reports_busy_with_json_and_exit_75_when_the_lock_is_held(tmp_path, capsys):
    video = _make_video(tmp_path / "media", "movies/One (2000)/one.mkv", 30.0, now=_now())
    held = alc.acquire_singleton_lock(alc.LOCK_FILE)
    assert held is not None
    try:
        stack, (_, probe, _, detect, send) = _enter(_patch_world())
        with stack:
            rc = _run_main(["--path", str(video)], env=_alert_env(tmp_path), tmp_path=tmp_path)
    finally:
        held.close()
    assert rc == 75 == alc.EXIT_BUSY
    out = capsys.readouterr().out
    assert json.loads(out) == {
        "path": str(video.resolve()), "result": "BUSY",
        "reason": "another audio-lang-check run holds the lock"}
    probe.assert_not_called()   # nothing was analysed
    send.assert_not_called()
    assert not alc.STATE_FILE.exists() and not alc.JSONL_FILE.exists()


def test_path_busy_json_is_the_only_thing_on_stdout_and_needs_no_existing_file(tmp_path, capsys):
    held = alc.acquire_singleton_lock(alc.LOCK_FILE)
    try:
        stack, _m = _enter(_patch_world())
        with stack:
            rc = _run_main(["--path", "relative/missing.mkv"], tmp_path=tmp_path)
    finally:
        held.close()
    assert rc == 75
    rec = json.loads(capsys.readouterr().out)  # parses as a whole: nothing else on stdout
    assert rec["result"] == "BUSY" and rec["path"].endswith("relative/missing.mkv")


def test_path_mode_works_normally_once_the_lock_is_free_again(tmp_path, capsys):
    video = _make_video(tmp_path / "media", "movies/One (2000)/one.mkv", 30.0, now=_now())
    held = alc.acquire_singleton_lock(alc.LOCK_FILE)
    held.close()  # taken and released
    stack, _m = _enter(_patch_world(probe_side_effect=lambda p: _info(_stream(1, "eng")),
                                    detect=_lid_forever(("en", 0.9))))
    with stack:
        assert _run_main(["--path", str(video)], tmp_path=tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "CHECKED"


# --- one state file is the single source of truth ---

_OK_IDENT = [1, 2, 3]
_OK_RETRY = {"identity": _OK_IDENT, "attempts": 1, "next_retry_at": 5.0}

_RETRY_TYPE_RESULTS = ["PROBE_FAILED", "ERROR", "ARR_UNAVAILABLE", "DEPENDENCY_MISSING"]

_MALFORMED = {
    **{f"retry-type result {r} cached as a conclusion":
       json.dumps({"/m/a.mkv": {"identity": [1, 2, 3], "result": r, "findings": []}})
       for r in _RETRY_TYPE_RESULTS},
    "top level a list": "[]",
    "top level null": "null",
    "top level a string": '"x"',
    "entry a list": '{"/m/a.mkv": []}',
    "entry a string": '{"/m/a.mkv": "checked"}',
    "entry null": '{"/m/a.mkv": null}',
    "identity too short": '{"/m/a.mkv": {"identity": [1, 2]}}',
    "identity not a list": '{"/m/a.mkv": {"identity": "1,2,3"}}',
    "identity element a string": '{"/m/a.mkv": {"identity": [1, 2, "3"]}}',
    "identity element a bool": '{"/m/a.mkv": {"identity": [1, 2, true]}}',
    "result an int": '{"/m/a.mkv": {"result": 3}}',
    "findings a dict": '{"/m/a.mkv": {"findings": {"type": "X"}}}',
    "findings items strings": '{"/m/a.mkv": {"findings": ["MISLABELED"]}}',
    "finding without a type": '{"/m/a.mkv": {"findings": [{"stream_index": 1}]}}',
    "first_found an int": '{"/m/a.mkv": {"first_found": 5}}',
    "alerted a string": '{"/m/a.mkv": {"alerted": "yes"}}',
    "alerted an int": '{"/m/a.mkv": {"alerted": 1}}',
    "checked_at an int": '{"/m/a.mkv": {"checked_at": 5}}',
    "retry a list": '{"/m/a.mkv": {"retry": []}}',
    "retry without attempts": '{"/m/a.mkv": {"retry": {"identity": [1,2,3], "next_retry_at": 5}}}',
    "retry attempts zero": '{"/m/a.mkv": {"retry": {"identity": [1,2,3], "attempts": 0, "next_retry_at": 5}}}',
    "retry attempts a bool": '{"/m/a.mkv": {"retry": {"identity": [1,2,3], "attempts": true, "next_retry_at": 5}}}',
    "retry next_retry_at a string": '{"/m/a.mkv": {"retry": {"identity": [1,2,3], "attempts": 1, "next_retry_at": "soon"}}}',
    "retry next_retry_at NaN": '{"/m/a.mkv": {"retry": {"identity": [1,2,3], "attempts": 1, "next_retry_at": NaN}}}',
    "retry next_retry_at Infinity": '{"/m/a.mkv": {"retry": {"identity": [1,2,3], "attempts": 1, "next_retry_at": Infinity}}}',
    "retry identity bad": '{"/m/a.mkv": {"retry": {"identity": [1], "attempts": 1, "next_retry_at": 5}}}',
    "retry last_error an int": '{"/m/a.mkv": {"retry": {"identity": [1,2,3], "attempts": 1, "next_retry_at": 5, "last_error": 7}}}',
    "one bad entry among good ones": ('{"/m/ok.mkv": {"identity": [1,2,3], "result": "CHECKED", "findings": []},'
                                      ' "/m/bad.mkv": {"alerted": "no"}}'),
    "truncated json": '{"/m/a.mkv": {"identity": [1, 2',
}


@pytest.mark.parametrize("name", list(_MALFORMED))
def test_malformed_state_is_a_blinding_error_that_writes_nothing(tmp_path, name):
    content = _MALFORMED[name]
    _seed_library(tmp_path)
    alc.STATE_FILE.write_text(content)
    stack, (_, probe, _, _, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "spa")), detect=_lid_forever(("es", 0.95))))
    with stack:
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 1
    probe.assert_not_called()
    send.assert_not_called()
    assert alc.STATE_FILE.read_text() == content   # not repaired, not replaced
    assert not alc.REPORT_FILE.exists()            # no report
    assert not alc.JSONL_FILE.exists()             # no log record
    assert not alc.WORKDIR_ROOT.exists()


@pytest.mark.parametrize("name", ["top level a list", "entry a string", "retry without attempts",
                                  "identity too short", "alerted a string"])
def test_dry_run_reports_malformed_state_the_same_way_and_writes_nothing(tmp_path, name, capsys):
    _seed_library(tmp_path)
    alc.STATE_FILE.write_text(_MALFORMED[name])
    stack, _m = _enter(_patch_world())
    with stack:
        assert _run_main(["--dry-run"], tmp_path=tmp_path) == 1
    assert "run blinded" in capsys.readouterr().out
    assert alc.STATE_FILE.read_text() == _MALFORMED[name]
    assert not alc.REPORT_FILE.exists()


def test_malformed_state_is_reported_with_the_path_and_the_reason(tmp_path, capsys):
    alc.STATE_FILE.write_text(_MALFORMED["alerted a string"])
    assert alc._load_state(alc.STATE_FILE) is None
    out = capsys.readouterr().out
    assert "/m/a.mkv" in out and "alerted has the wrong type" in out


def test_the_runtime_dirs_and_lock_precede_the_state_check_but_nothing_else_is_written(tmp_path):
    """var/ dirs and the lock file are run infrastructure, not data: the lock is taken first so state is read under it."""
    _seed_library(tmp_path)
    alc.STATE_FILE.write_text("{corrupt")
    events = []
    alc.ensure_var_dirs.side_effect = lambda: events.append("dirs")
    real_lock = alc.acquire_singleton_lock

    def spy_lock(path):
        events.append("lock")
        return real_lock(path)

    stack, _m = _enter(_patch_world())
    with stack, patch.object(alc, "acquire_singleton_lock", spy_lock):
        assert _run_main([], tmp_path=tmp_path) == 1
    assert events == ["dirs", "lock"]
    assert alc.STATE_FILE.read_text() == "{corrupt"
    assert not alc.REPORT_FILE.exists() and not alc.JSONL_FILE.exists()


@pytest.mark.parametrize("entry", [
    {},
    {"identity": _OK_IDENT, "result": "CHECKED", "findings": [], "checked_at": "t"},
    {"identity": _OK_IDENT, "result": "CHECKED", "findings": [{"type": "MISLABELED", "stream_index": 1, "tag": "eng", "detected_language": "es",
                              "probability": 0.85}],
     "first_found": "t", "alerted": False, "checked_at": "t"},
    {"retry": _OK_RETRY},
    {"retry": {**_OK_RETRY, "last_result": "ERROR", "last_error": None, "last_attempt_at": "t"}},
    {"identity": _OK_IDENT, "result": "CHECKED", "findings": [], "retry": _OK_RETRY},
    {"retry": {**_OK_RETRY, "next_retry_at": 1.5e9}},
])
def test_well_formed_entries_validate(entry):
    assert alc.entry_problem(entry) is None


# --- pending alerts live in the same state and survive across runs ---------------------

def test_a_pending_alert_survives_a_failed_send_and_is_sent_exactly_once_later(tmp_path):
    video = _one_video(tmp_path)
    stack, (_, probe, _, _, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "spa")), detect=_lid_forever(("es", 0.95)),
        sent=False))
    with stack:
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0  # run still succeeds
        assert send.call_count == 1 and _findings_view()[str(video.resolve())]["alerted"] is False
        send.return_value = True
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0   # nothing new to check ...
        assert probe.call_count == 1 and send.call_count == 2          # ... but the alert is retried
        _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
        _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
    assert send.call_count == 2                                         # exactly once
    assert _findings_view()[str(video.resolve())]["alerted"] is True


def test_a_pending_alert_survives_a_run_blinded_by_a_bad_state_file(tmp_path):
    video = _one_video(tmp_path)
    stack, (_, probe, _, _, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "spa")), detect=_lid_forever(("es", 0.95)),
        sent=False))
    with stack:
        _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)    # recorded, send failed
        good = alc.STATE_FILE.read_text()
        alc.STATE_FILE.write_text("{corrupt")
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 1
        assert send.call_count == 1                                    # blinded: nothing sent
        alc.STATE_FILE.write_text(good)                                # operator restores it
        send.return_value = True
        _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
        _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
    assert send.call_count == 2 and probe.call_count == 1
    assert _findings_view()[str(video.resolve())]["alerted"] is True


def test_every_state_write_goes_through_the_one_locked_mutate_on_the_one_file(tmp_path):
    _one_video(tmp_path)
    calls = []
    stack, (_, _, _, _, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "spa")), detect=_lid_forever(("es", 0.95))))
    with stack, patch.object(alc, "mutate_state", _spy_mutate(calls)):
        _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
    assert send.call_count == 1
    assert calls == [alc.STATE_FILE, alc.STATE_FILE]   # the conclusion, then the alerted mark
    assert not list(alc.STATE_FILE.parent.glob("*.lock")) or \
        {p.name for p in alc.STATE_FILE.parent.glob("*.lock")} == {alc.STATE_FILE.name + ".lock"}


def test_stale_findings_are_stripped_but_the_rest_of_the_entry_stays(tmp_path, capsys):
    video = _finding_video(tmp_path)
    key = str(video.resolve())
    _seed_finding(video, MISLABEL, "t0")
    _replace(video)
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True), \
         patch.object(alc, "telegram_send", return_value=True) as send:
        alc.retry_alerts()
    send.assert_not_called()
    assert "stale finding" in capsys.readouterr().out
    entry = _state()[key]
    assert entry["findings"] == [] and "first_found" not in entry and "alerted" not in entry
    assert entry["result"] == "CHECKED" and entry["identity"] != alc._identity(video)
    assert key not in _findings_view()


def test_retry_alerts_on_an_unreadable_state_sends_and_writes_nothing(tmp_path, capsys):
    video = _finding_video(tmp_path)
    _seed_finding(video, MISLABEL, "t0")
    alc.STATE_FILE.write_text("{corrupt")
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True), \
         patch.object(alc, "telegram_send", return_value=True) as send:
        alc.retry_alerts()
    send.assert_not_called()
    assert alc.STATE_FILE.read_text() == "{corrupt"
    assert "not alerting" in capsys.readouterr().out


# --- the derived report ---------------------------------------------------------------------

def test_the_report_is_regenerated_from_the_state_at_the_end_of_a_real_run(tmp_path):
    video = _one_video(tmp_path)
    stack, _m = _enter(_patch_world(probe_side_effect=lambda p: _info(_stream(1, "spa")),
                                    detect=_lid_forever(("es", 0.95))))
    with stack:
        _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
    report = json.loads(alc.REPORT_FILE.read_text())
    entry = _state()[str(video.resolve())]
    assert report == {str(video.resolve()): {
        "findings": entry["findings"], "first_found": entry["first_found"],
        "alerted": True, "identity": entry["identity"]}}   # post-alert state


def test_the_report_lists_only_unresolved_findings_for_the_current_file(tmp_path):
    fresh, replaced, clean = (_finding_video(tmp_path, i) for i in range(3))
    _seed_finding(fresh, MISLABEL, "t0")
    _seed_finding(replaced, MISLABEL, "t0")
    _replace(replaced)
    assert alc._record_result(clean, alc._identity(clean), {"result": "CHECKED", "findings": []}, "t")
    alc.write_report(_state())
    assert set(json.loads(alc.REPORT_FILE.read_text())) == {str(fresh.resolve())}


def test_the_report_is_never_read_so_an_old_format_file_is_just_overwritten(tmp_path):
    video = _one_video(tmp_path)
    old_format = {str(video.resolve()): {"findings": MISLABEL, "first_found": "t0",
                                         "alerted": True, "identity": alc._identity(video)}}
    alc.REPORT_FILE.write_text(json.dumps(old_format))   # an earlier version's findings file
    stack, (_, _, _, _, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "spa")), detect=_lid_forever(("es", 0.95))))
    with stack:
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0
    assert send.call_count == 1                  # its alerted=true had no say
    report = json.loads(alc.REPORT_FILE.read_text())
    assert [f["type"] for f in report[str(video.resolve())]["findings"]] == ["NO_ENGLISH_AUDIO"]


@pytest.mark.parametrize("garbage", ["{corrupt", "[]", "null", "\x00\x01"])
def test_a_garbage_report_file_neither_blocks_the_run_nor_is_trusted(tmp_path, garbage):
    _seed_library(tmp_path)
    alc.REPORT_FILE.write_text(garbage)
    stack, _m = _enter(_patch_world(probe_side_effect=lambda p: _info(_stream(1, "eng")),
                                    detect=_lid_forever(("en", 0.95))))
    with stack:
        assert _run_main([], tmp_path=tmp_path) == 0
    assert json.loads(alc.REPORT_FILE.read_text()) == {}


def test_the_script_never_opens_the_report_file_for_reading(tmp_path):
    _seed_library(tmp_path)
    alc.REPORT_FILE.write_text("{}")
    real_read_text = Path.read_text
    real_open = Path.open
    touched = []

    def spy_read_text(self, *a, **k):
        if self == alc.REPORT_FILE:
            touched.append("read_text")
        return real_read_text(self, *a, **k)

    def spy_open(self, mode="r", *a, **k):
        if self == alc.REPORT_FILE and "r" in mode:
            touched.append("open")
        return real_open(self, mode, *a, **k)

    stack, _m = _enter(_patch_world(probe_side_effect=lambda p: _info(_stream(1, "eng")),
                                    detect=_lid_forever(("en", 0.95))))
    with stack, patch.object(Path, "read_text", spy_read_text), patch.object(Path, "open", spy_open):
        assert _run_main([], tmp_path=tmp_path) == 0
    assert touched == []


def test_a_report_write_failure_is_logged_and_harmless(tmp_path, monkeypatch, capsys):
    video = _one_video(tmp_path)
    monkeypatch.setattr(alc, "REPORT_FILE", tmp_path / "no-such-dir" / "report.json")
    stack, (_, _, _, _, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "spa")), detect=_lid_forever(("es", 0.95))))
    with stack:
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0
    assert "derived report" in capsys.readouterr().out
    assert send.call_count == 1 and _identity_entry(video)["result"] == "CHECKED"


def test_the_report_is_regenerated_on_every_kind_of_real_run_ending(tmp_path):
    older, newer = _seed_library(tmp_path)
    # nothing to do
    stack, _m = _enter(_patch_world())
    with stack, patch.object(alc, "select_candidates", return_value=[]):
        assert _run_main([], tmp_path=tmp_path) == 0
    assert json.loads(alc.REPORT_FILE.read_text()) == {}
    alc.REPORT_FILE.unlink()
    # a playback wait that ended the run
    stack, _m = _enter(_patch_world())
    def exhausted(*a, **k):  # a fresh exception each call: a shared instance's traceback would
        raise alc.PlaybackWaitTimeout("x")  # keep main()'s frame -- and its lock -- alive

    with stack, patch.object(alc, "process_candidate", exhausted):
        assert _run_main([], tmp_path=tmp_path) == 0
    assert alc.REPORT_FILE.exists()
    alc.REPORT_FILE.unlink()
    # a missing whisper
    stack, _m = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "eng")),
        detect=lambda *a, **k: (_ for _ in ()).throw(ModuleNotFoundError("faster_whisper"))))
    with stack:
        assert _run_main([], tmp_path=tmp_path) == 1
    assert alc.REPORT_FILE.exists()


def test_no_report_for_dry_run_path_or_a_blinded_run(tmp_path):
    video = _make_video(tmp_path / "media", "movies/One (2000)/one.mkv", HOUR, now=_now())
    stack, _m = _enter(_patch_world(probe_side_effect=lambda p: _info(_stream(1, "eng")),
                                    detect=_lid_forever(("en", 0.9))))
    with stack:
        _run_main(["--dry-run"], tmp_path=tmp_path)
        _run_main(["--path", str(video)], tmp_path=tmp_path)
        assert not alc.REPORT_FILE.exists()
        alc.STATE_FILE.write_text("{corrupt")
        assert _run_main([], tmp_path=tmp_path) == 1
    assert not alc.REPORT_FILE.exists()


# --- `result` is a closed set; retry-type outcomes live only in `retry` ---------

@pytest.mark.parametrize("result, valid", [
    *((r, True) for r in ["CHECKED", "UNVERIFIED"]),
    *((r, False) for r in _RETRY_TYPE_RESULTS + ["checked", "", "OK", "FIXED", None, 3]),
])
def test_only_the_two_conclusions_are_valid_cached_results(result, valid):
    problem = alc.entry_problem({"identity": _OK_IDENT, "result": result, "findings": []})
    if valid:
        assert problem is None
    else:
        assert problem


def test_the_conclusion_set_excludes_every_retry_type_outcome():
    assert alc.CONCLUSION_RESULTS == {"CHECKED", "UNVERIFIED"}
    assert not (alc.CONCLUSION_RESULTS & alc.RETRYABLE_RESULTS)


@pytest.mark.parametrize("last, valid", [
    *((r, True) for r in _RETRY_TYPE_RESULTS + [None]),
    *((r, False) for r in ["CHECKED", "UNVERIFIED", "FOO", "", 4]),
])
def test_a_retry_record_names_only_a_retry_type_outcome_or_none_as_its_last_result(last, valid):
    problem = alc.entry_problem({"retry": {**_OK_RETRY, "last_result": last}})
    if valid:
        assert problem is None
    else:
        assert problem


def test_the_script_never_records_what_it_would_not_load(tmp_path):
    video = _make_video(tmp_path / "media", "movies/A/a.mkv", HOUR)
    ident = alc._identity(video)
    for bad in (_RETRY_TYPE_RESULTS + ["???"]):                        # a retry type as a conclusion
        assert alc._record_result(video, ident, {"result": bad, "findings": []}, "t") is False
    assert alc._record_result(video, ident, {"result": "CHECKED", "findings": [
        {"type": "MISLABELED", "stream_index": 1}]}, "t") is False      # a malformed finding
    alc._record_retry(video, ident, {"result": "CHECKED"}, 1.0)         # a conclusion as a retry
    assert not alc.STATE_FILE.exists()                                  # nothing was written


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
    _mislabeled(tag=None),
    _mislabeled(probability=0),
    _mislabeled(probability=1),
    _mislabeled(probability=0.8),
    _no_english(),
    _no_english(streams=[dict(_GOOD_STREAM), dict(_GOOD_STREAM, index=2, tag=None)]),
])
def test_well_formed_findings_validate(finding):
    assert alc.finding_problem(finding) is None


def _without(finding, key):
    return {k: v for k, v in finding.items() if k != key}


@pytest.mark.parametrize("finding", [
    "MISLABELED", None, [], 5,
    {"stream_index": 1, "tag": "eng", "detected_language": "es", "probability": 0.9},   # no type
    {"type": 7},
    {"type": "SOMETHING_ELSE"},
    {"type": "mislabeled"},
    _without(_mislabeled(), "stream_index"),
    _without(_mislabeled(), "tag"),
    _without(_mislabeled(), "detected_language"),
    _without(_mislabeled(), "probability"),
    _mislabeled(stream_index="1"),
    _mislabeled(stream_index=True),
    _mislabeled(stream_index=None),
    _mislabeled(tag=5),
    _mislabeled(detected_language=""),
    _mislabeled(detected_language=None),
    _mislabeled(detected_language=["es"]),
    _mislabeled(probability=1.01),
    _mislabeled(probability=-0.01),
    _mislabeled(probability=2),
    _mislabeled(probability="0.9"),
    _mislabeled(probability=None),
    _mislabeled(probability=True),
    _mislabeled(probability=float("nan")),
    _mislabeled(probability=float("inf")),
    _without(_no_english(), "original_language"),
    _no_english(original_language=""),
    _no_english(original_language=3),
    _without(_no_english(), "streams"),
    _no_english(streams=[]),
    _no_english(streams="spa"),
    _no_english(streams={"index": 1}),
    _no_english(streams=["spa"]),
    _no_english(streams=[None]),
    _no_english(streams=[_without(_GOOD_STREAM, "index")]),
    _no_english(streams=[_without(_GOOD_STREAM, "tag")]),
    _no_english(streams=[_without(_GOOD_STREAM, "detected_language")]),
    _no_english(streams=[_without(_GOOD_STREAM, "probability")]),
    _no_english(streams=[dict(_GOOD_STREAM, probability=1.5)]),
    _no_english(streams=[dict(_GOOD_STREAM, probability=float("nan"))]),
    _no_english(streams=[dict(_GOOD_STREAM, index="1")]),
    _no_english(streams=[dict(_GOOD_STREAM, tag=7)]),
    _no_english(streams=[dict(_GOOD_STREAM), dict(_GOOD_STREAM, detected_language="")]),
])
def test_malformed_findings_are_rejected(finding):
    assert alc.finding_problem(finding)
    assert alc.entry_problem({"identity": _OK_IDENT, "result": "CHECKED", "findings": [finding]})


@pytest.mark.parametrize("finding_json", [
    '{"type": "MISLABELED", "stream_index": 1, "tag": "eng", "detected_language": "es", "probability": NaN}',
    '{"type": "MISLABELED", "stream_index": 1, "tag": "eng", "detected_language": "es", "probability": 1.5}',
    '{"type": "MISLABELED", "stream_index": 1, "tag": "eng", "probability": 0.9}',
    '{"type": "NO_ENGLISH_AUDIO", "original_language": "eng"}',
    '{"type": "NO_ENGLISH_AUDIO", "original_language": "eng", "streams": []}',
    '{"type": "UNKNOWN_KIND"}',
])
def test_a_malformed_finding_in_the_state_is_blinding_before_any_write(tmp_path, finding_json):
    _seed_library(tmp_path)
    content = ('{"/m/a.mkv": {"identity": [1, 2, 3], "result": "CHECKED", "alerted": false, '
               '"first_found": "t", "findings": [' + finding_json + ']}}')
    alc.STATE_FILE.write_text(content)
    stack, (_, probe, _, _, send) = _enter(_patch_world())
    with stack:
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 1
    probe.assert_not_called()
    send.assert_not_called()
    assert alc.STATE_FILE.read_text() == content
    assert not alc.REPORT_FILE.exists() and not alc.JSONL_FILE.exists()


def test_the_no_english_audio_finding_the_script_writes_lists_every_stream(tmp_path):
    video = _one_video(tmp_path)
    stack, _m = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "spa"), _stream(2, None)),
        detect=lambda video, idx, *a, should_abort=None, **k: {1: ("es", 0.951234),
                                                              2: ("fr", 0.9)}[idx]))
    with stack:
        _run_main([], tmp_path=tmp_path)
    finding = _identity_entry(video)["findings"][0]
    assert alc.finding_problem(finding) is None
    assert finding["streams"] == [
        {"index": 1, "tag": "spa", "detected_language": "es", "probability": 0.951},
        {"index": 2, "tag": None, "detected_language": "fr", "probability": 0.9}]
    assert alc.entry_problem(_identity_entry(video)) is None


# --- formatting never raises --------------------------------- (second line of defence) ---

@pytest.mark.parametrize("findings", [
    None, "MISLABELED", 5, [], [None], [5], ["x"], [{}], [{"type": None}], [{"type": "NOPE"}],
    [{"type": "MISLABELED"}],
    [{"type": "MISLABELED", "stream_index": None, "tag": 5, "detected_language": {},
      "probability": "high"}],
    [{"type": "MISLABELED", "stream_index": 1, "tag": "eng", "detected_language": "es",
      "probability": float("nan")}],
    [{"type": "MISLABELED", "stream_index": 1, "tag": "eng", "detected_language": "es",
      "probability": float("inf")}],
    [{"type": "MISLABELED", "stream_index": 1, "tag": "eng", "detected_language": "es",
      "probability": True}],
    [{"type": "NO_ENGLISH_AUDIO"}],
    [{"type": "NO_ENGLISH_AUDIO", "streams": None}],
    [{"type": "NO_ENGLISH_AUDIO", "streams": "x"}],
    [{"type": "NO_ENGLISH_AUDIO", "streams": [None, 3, {}, {"probability": "x"}]}],
])
def test_formatting_a_finding_never_raises(findings):
    line = alc._format_finding_line("/m/movies/Film (2000)/film.mkv", findings)
    assert isinstance(line, str) and line.startswith("- ")


def test_unexpected_values_render_as_a_question_mark():
    line = alc._format_finding_line("/m/movies/F/f.mkv", [
        {"type": "MISLABELED", "stream_index": None, "tag": "eng", "detected_language": None,
         "probability": "high"}])
    assert "stream ? tagged eng but detected ? (p=?)" in line
    nan_line = alc._format_finding_line("/m/movies/F/f.mkv", [
        {"type": "MISLABELED", "stream_index": 1, "tag": "eng", "detected_language": "es",
         "probability": float("nan")}])
    assert "(p=?)" in nan_line


def test_a_well_formed_finding_formats_normally():
    line = alc._format_finding_line("/m/movies/F/f.mkv", MISLABEL + NO_ENGLISH)
    assert "stream 1 tagged eng but detected es (p=0.85)" in line
    assert "no confirmed English audio" in line and "1 tagged spa heard es (p=0.95)" in line


def test_formatting_survives_a_failing_title_lookup():
    with patch.object(alc, "_title_for", side_effect=RuntimeError("boom")):
        line = alc._format_finding_line("/m/movies/F/f.mkv", MISLABEL)
    assert line.startswith("- /m/movies/F/f.mkv: ")


def test_building_a_message_from_junk_entries_still_works():
    pending = [("/m/a.mkv", {"findings": [None, {"type": "X"}], "first_found": 5}),
               ("/m/b.mkv", {"findings": None})]
    text, named = alc.build_alert_message(pending)
    assert isinstance(text, str) and len(named) == 2 and alc._tg_len(text) <= alc.TELEGRAM_BUDGET_CHARS


# --- credentials and an unreadable plugin .env ---------------------------------------

def test_both_credentials_in_the_environment_never_touch_the_plugin_env_file():
    env = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42", "HOME": "/nonexistent"}
    with patch.dict(os.environ, env, clear=True), \
         patch.object(alc, "load_env_file", side_effect=AssertionError("must not be read")), \
         patch.object(alc, "telegram_send", return_value=True) as send:
        assert alc._notify("hello") is True
    send.assert_called_once_with("tok", "42", "hello")


@pytest.mark.parametrize("env, expected", [
    pytest.param({"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}, ("tok", "42"),
                 id="environment_wins_over_the_file"),
    pytest.param({"TELEGRAM_BOT_TOKEN": "tok"}, ("tok", "99"), id="missing_credential_filled_in_from_the_file"),
])
def test_environment_credentials_win_and_a_missing_one_is_filled_in_from_the_plugin_env_file(
        tmp_path, env, expected):
    env_file = tmp_path / "plugin.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=filetok\nTELEGRAM_CHAT_ID=99\n")
    with patch.dict(os.environ, {**env, "TELEGRAM_ENV": str(env_file)}, clear=True), \
         patch.object(alc, "telegram_send", return_value=True) as send:
        alc._notify("hello")
    assert send.call_args.args[:2] == expected


@pytest.mark.parametrize("exc", [PermissionError("denied"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
                                 OSError("io"), RuntimeError("weird")])
def test_a_failure_reading_the_plugin_env_is_caught_logged_and_fails_soft(tmp_path, exc, capsys):
    env = {"TELEGRAM_ENV": str(tmp_path / "plugin.env")}
    with patch.dict(os.environ, env, clear=True), \
         patch.object(alc, "load_env_file", side_effect=exc), \
         patch.object(alc, "telegram_send") as send:
        assert alc._notify("hello") is False
    send.assert_not_called()
    out = capsys.readouterr().out
    assert "could not read the Telegram .env" in out and type(exc).__name__ in out


def test_an_undecodable_plugin_env_file_fails_soft(tmp_path, capsys):
    env_file = tmp_path / "plugin.env"
    env_file.write_bytes(b"\xff\xfe\x00TELEGRAM_BOT_TOKEN=\x80\x81")
    with patch.dict(os.environ, {"TELEGRAM_ENV": str(env_file)}, clear=True), \
         patch.object(alc, "telegram_send") as send:
        assert alc._notify("hello") is False
    send.assert_not_called()
    assert "could not read" in capsys.readouterr().out


@pytest.mark.parametrize("text", [None, "", "garbage\nno equals sign\n", "=\n==\n#c\n", "TELEGRAM_BOT_TOKEN\n"],
                         ids=["missing_file", "empty", "no_equals_sign", "only_punctuation", "key_without_value"])
def test_a_missing_malformed_or_empty_plugin_env_file_just_means_no_credentials(tmp_path, text, capsys):
    env_file = tmp_path / "plugin.env"
    if text is not None:
        env_file.write_text(text)
    with patch.dict(os.environ, {"TELEGRAM_ENV": str(env_file)}, clear=True), \
         patch.object(alc, "telegram_send") as send:
        assert alc._notify("hello") is False
    send.assert_not_called()
    assert "no Telegram credentials" in capsys.readouterr().out


def test_an_unresolvable_home_directory_fails_soft(capsys):
    with patch.dict(os.environ, {}, clear=True), \
         patch.object(alc.Path, "home", side_effect=RuntimeError("no home")), \
         patch.object(alc, "telegram_send") as send:
        assert alc._notify("hello") is False
    send.assert_not_called()
    assert "could not read" in capsys.readouterr().out


def test_an_unreadable_plugin_env_leaves_findings_pending_and_the_report_regenerated(tmp_path):
    video = _one_video(tmp_path)
    env_file = tmp_path / "plugin.env"
    env_file.write_bytes(b"\xff\xfe\x00bad")
    base = {"HOME": str(tmp_path), "TELEGRAM_ENV": str(env_file)}
    stack, (_, probe, _, _, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "spa")), detect=_lid_forever(("es", 0.95))))
    with stack:
        assert _run_main([], env=base, tmp_path=tmp_path) == 0          # fails soft
        send.assert_not_called()
        assert _findings_view()[str(video.resolve())]["alerted"] is False   # still pending
        report = json.loads(alc.REPORT_FILE.read_text())
        assert str(video.resolve()) in report and report[str(video.resolve())]["alerted"] is False
        # credentials appear later: the pending alert goes out exactly once
        good = {**base, "TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "42"}
        _run_main([], env=good, tmp_path=tmp_path)
        _run_main([], env=good, tmp_path=tmp_path)
    assert send.call_count == 1 and probe.call_count == 1
    assert _findings_view()[str(video.resolve())]["alerted"] is True


def test_an_alerting_crash_never_stops_the_report_or_the_run(tmp_path, capsys):
    video = _one_video(tmp_path)
    stack, _m = _enter(_patch_world(probe_side_effect=lambda p: _info(_stream(1, "spa")),
                                    detect=_lid_forever(("es", 0.95))))
    with stack, patch.object(alc, "retry_alerts", side_effect=RuntimeError("bug")):
        assert _run_main([], tmp_path=tmp_path) == 0
    assert "alerting failed" in capsys.readouterr().out
    assert str(video.resolve()) in json.loads(alc.REPORT_FILE.read_text())


# --- a conclusion reached while Arr knew nothing is re-evaluated from stored detections

SPA_REC = {"index": 1, "tag": "spa", "detected_language": "es", "probability": 0.95,
           "counts_as_english": False, "reading": "foreign"}


def _unknown_entry(video, *, streams=None, findings=None, status="unknown", code=None,
                   checked_at="2026-10-01T00:00:00+00:00", result="CHECKED", **extra):
    entry = {"identity": alc._identity(video), "result": result, "findings": findings or [],
             "checked_at": checked_at, "original_status": status, "original_language": code,
             "streams": [dict(SPA_REC)] if streams is None else streams}
    entry.update(extra)
    return entry


def _write_state(entries):
    alc.STATE_FILE.write_text(json.dumps({str(v.resolve()): e for v, e in entries.items()}))


class _LookupSpy:
    """original_language_lookup that answers from a script and counts calls."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        return self.answer


def test_the_first_analysis_stores_the_detections_and_the_arr_answer_it_used(tmp_path):
    video = _one_video(tmp_path)
    stack, _m = _enter(_patch_world(probe_side_effect=lambda p: _info(_stream(1, "spa")),
                                    detect=_lid_forever(("es", 0.95)), original=None))
    with stack:
        _run_main([], tmp_path=tmp_path)
    entry = _identity_entry(video)
    assert entry["original_status"] == "unknown" and entry["original_language"] is None
    assert entry["result"] == "CHECKED" and entry["findings"] == []
    assert entry["streams"] == [SPA_REC]
    assert alc.entry_problem(entry) is None


def test_unknown_then_known_english_original_yields_no_english_audio_alerted_once(tmp_path):
    video = _one_video(tmp_path)
    stack, (_, probe, _, detect, send) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "spa")), detect=_lid_forever(("es", 0.95)),
        original=None, sent=True))
    with stack:
        _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)     # Arr: no record yet
        assert send.call_count == 0 and _findings_view() == {}
        # Radarr now has the title, English-original. Nothing is re-analysed ...
        spy = _LookupSpy(("eng", "known"))
        with patch.object(alc, "original_language_lookup", spy):
            _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
            assert len(spy.calls) == 1
            _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
            _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
            assert len(spy.calls) == 1                                  # known: never asked again
    assert probe.call_count == 1                                         # no audio work redone
    entry = _identity_entry(video)
    assert entry["original_status"] == "known" and entry["original_language"] == "eng"
    assert [f["type"] for f in entry["findings"]] == ["NO_ENGLISH_AUDIO"]
    assert entry["findings"][0]["streams"] == [
        {"index": 1, "tag": "spa", "detected_language": "es", "probability": 0.95}]
    assert entry["alerted"] is True and send.call_count == 1             # alerted exactly once
    assert alc.entry_problem(entry) is None


def test_still_unknown_leaves_the_conclusion_unchanged_and_only_stamps_the_recheck(tmp_path):
    video = _one_video(tmp_path)
    _write_state({video: _unknown_entry(video)})
    before = _state()[str(video.resolve())]
    spy = _LookupSpy((None, "unknown"))
    stack, (_, probe, _, _, send) = _enter(_patch_world())
    with stack, patch.object(alc, "original_language_lookup", spy):
        _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path)
    after = _state()[str(video.resolve())]
    assert len(spy.calls) == 1
    assert {k: v for k, v in after.items() if k != "original_rechecked_at"} == before
    assert isinstance(after["original_rechecked_at"], str)
    probe.assert_not_called()
    send.assert_not_called()


def test_known_entries_are_never_looked_up_again(tmp_path):
    a, b = _finding_video(tmp_path, 1), _finding_video(tmp_path, 2)
    _write_state({a: _unknown_entry(a, status="known", code="eng"),
                  b: _unknown_entry(b, status="known", code="jpn")})
    spy = _LookupSpy(("eng", "known"))
    with patch.object(alc, "original_language_lookup", spy):
        alc.recheck_unknown_originals(_state())
    assert spy.calls == []


def test_entries_without_a_recorded_arr_answer_or_stored_detections_are_not_looked_up(tmp_path):
    legacy, nostreams, retry_only = (_finding_video(tmp_path, i) for i in range(3))
    _write_state({
        legacy: {"identity": alc._identity(legacy), "result": "CHECKED", "findings": []},
        nostreams: _unknown_entry(nostreams, streams=[], result="UNVERIFIED"),
        retry_only: {"retry": {"identity": _OK_IDENT, "attempts": 1, "next_retry_at": 5.0}},
    })
    spy = _LookupSpy(("eng", "known"))
    with patch.object(alc, "original_language_lookup", spy):
        alc.recheck_unknown_originals(_state())
    assert spy.calls == []


def test_a_known_non_english_original_adds_no_finding_but_stops_further_lookups(tmp_path):
    video = _finding_video(tmp_path)
    _write_state({video: _unknown_entry(video)})
    spy = _LookupSpy(("jpn", "known"))
    with patch.object(alc, "original_language_lookup", spy):
        alc.recheck_unknown_originals(_state())
        alc.recheck_unknown_originals(_state())
    entry = _state()[str(video.resolve())]
    assert entry["findings"] == [] and entry["original_status"] == "known"
    assert entry["original_language"] == "jpn" and len(spy.calls) == 1


def test_an_unclear_stream_keeps_the_file_unverified_even_once_the_original_is_known(tmp_path):
    video = _finding_video(tmp_path)
    unclear = dict(SPA_REC, probability=0.3, reading="unclear")
    _write_state({video: _unknown_entry(video, streams=[unclear], result="UNVERIFIED")})
    with patch.object(alc, "original_language_lookup", _LookupSpy(("eng", "known"))):
        alc.recheck_unknown_originals(_state())
    entry = _state()[str(video.resolve())]
    assert entry["result"] == "UNVERIFIED" and entry["findings"] == []
    assert entry["original_status"] == "known"


def test_a_stored_mislabeled_finding_is_kept_and_joined_by_no_english_audio(tmp_path):
    video = _finding_video(tmp_path)
    mislabeled = dict(MISLABEL[0])
    rec = dict(SPA_REC, tag="eng")
    _write_state({video: _unknown_entry(video, streams=[rec], findings=[mislabeled],
                                        first_found="t0", alerted=True)})
    with patch.object(alc, "original_language_lookup", _LookupSpy(("eng", "known"))):
        alc.recheck_unknown_originals(_state())
    entry = _state()[str(video.resolve())]
    assert [f["type"] for f in entry["findings"]] == ["MISLABELED", "NO_ENGLISH_AUDIO"]
    assert entry["alerted"] is False and entry["first_found"] == "t0"   # changed findings: re-alert


def test_unchanged_findings_keep_their_alerted_flag_across_a_recheck(tmp_path):
    video = _finding_video(tmp_path)
    mislabeled = dict(MISLABEL[0])
    english = dict(SPA_REC, tag="eng", detected_language="en", counts_as_english=True,
                   reading="english")
    _write_state({video: _unknown_entry(video, streams=[english], findings=[mislabeled],
                                        first_found="t0", alerted=True)})
    with patch.object(alc, "original_language_lookup", _LookupSpy(("eng", "known"))):
        alc.recheck_unknown_originals(_state())
    entry = _state()[str(video.resolve())]
    assert entry["findings"] == [mislabeled] and entry["alerted"] is True


def test_a_file_that_changed_since_is_not_looked_up_it_is_reanalysed_on_its_own(tmp_path):
    video = _finding_video(tmp_path)
    _write_state({video: _unknown_entry(video)})
    _replace(video)
    spy = _LookupSpy(("eng", "known"))
    with patch.object(alc, "original_language_lookup", spy):
        alc.recheck_unknown_originals(_state())
    assert spy.calls == []
    assert _state()[str(video.resolve())]["original_status"] == "unknown"


def test_an_unavailable_arr_changes_nothing_and_stops_the_recheck(tmp_path, capsys):
    videos = [_finding_video(tmp_path, i) for i in range(3)]
    _write_state({v: _unknown_entry(v) for v in videos})
    before = alc.STATE_FILE.read_text()
    spy = _LookupSpy((None, "unavailable"))
    with patch.object(alc, "original_language_lookup", spy):
        alc.recheck_unknown_originals(_state())
    assert len(spy.calls) == 1                       # no point asking again this run
    assert alc.STATE_FILE.read_text() == before
    assert "unavailable" in capsys.readouterr().out


class _PerKindLookup:
    """original_language_lookup answering per library (movies/ -> Radarr, tv/ -> Sonarr) and counting calls."""

    def __init__(self, movies, tv):
        self.answers = {"movies": movies, "tv": tv}
        self.calls = []

    def __call__(self, path):
        self.calls.append(Path(path))
        kind = "movies" if "/movies/" in str(path) else "tv"
        return self.answers[kind]


def _tv_video(tmp_path, n=0):
    return _make_video(tmp_path / "media", f"tv/Show {n:02d}/Season 01/e{n:02d}.mkv", HOUR)


@pytest.mark.parametrize("down", ["Radarr", "Sonarr"])
def test_one_service_outage_does_not_starve_the_other_services_entries(tmp_path, capsys, down):
    movie, episode = _finding_video(tmp_path), _tv_video(tmp_path)
    dead, live = (movie, episode) if down == "Radarr" else (episode, movie)
    _write_state({dead: _unknown_entry(dead, checked_at="2026-10-01T00:00:00+00:00"),
                  live: _unknown_entry(live, checked_at="2026-10-02T00:00:00+00:00")})
    dead_before = _state()[str(dead.resolve())]
    unavailable, known = (None, "unavailable"), ("eng", "known")
    lookup = (_PerKindLookup(movies=unavailable, tv=known) if down == "Radarr"
              else _PerKindLookup(movies=known, tv=unavailable))
    with patch.object(alc, "original_language_lookup", lookup):
        alc.recheck_unknown_originals(_state())
    assert lookup.calls == [dead.resolve(), live.resolve()]       # the unavailable one sorts first
    state = _state()
    assert state[str(dead.resolve())] == dead_before              # untouched
    live_entry = state[str(live.resolve())]
    assert live_entry["original_status"] == "known" and live_entry["original_language"] == "eng"
    assert [f["type"] for f in live_entry["findings"]] == ["NO_ENGLISH_AUDIO"]
    out = capsys.readouterr().out
    assert f"{down} unavailable" in out and "1 skipped" in out


def test_an_unavailable_service_is_asked_once_not_once_per_entry(tmp_path):
    movies = [_finding_video(tmp_path, i) for i in range(4)]
    episodes = [_tv_video(tmp_path, i) for i in range(3)]
    _write_state({**{v: _unknown_entry(v, checked_at=f"2026-10-01T00:00:0{i}+00:00")
                     for i, v in enumerate(movies)},
                  **{v: _unknown_entry(v, checked_at=f"2026-10-02T00:00:0{i}+00:00")
                     for i, v in enumerate(episodes)}})
    lookup = _PerKindLookup(movies=(None, "unavailable"), tv=(None, "unknown"))
    with patch.object(alc, "original_language_lookup", lookup):
        alc.recheck_unknown_originals(_state())
    asked = [str(p) for p in lookup.calls]
    assert sum("/movies/" in a for a in asked) == 1               # Radarr: once, then skipped
    assert sum("/tv/" in a for a in asked) == 3                   # Sonarr: every entry
    state = _state()
    assert all("original_rechecked_at" not in state[str(v.resolve())] for v in movies)
    assert all("original_rechecked_at" in state[str(v.resolve())] for v in episodes)


def test_skipped_entries_of_an_unavailable_service_do_not_use_up_the_recheck_budget(
        tmp_path, monkeypatch):
    monkeypatch.setattr(alc, "MAX_ORIGINAL_RECHECKS", 3)
    movies = [_finding_video(tmp_path, i) for i in range(6)]       # all sort ahead of the TV ones
    episodes = [_tv_video(tmp_path, i) for i in range(5)]
    _write_state({**{v: _unknown_entry(v, checked_at=f"2026-10-01T00:00:0{i}+00:00")
                     for i, v in enumerate(movies)},
                  **{v: _unknown_entry(v, checked_at=f"2026-10-02T00:00:0{i}+00:00")
                     for i, v in enumerate(episodes)}})
    lookup = _PerKindLookup(movies=(None, "unavailable"), tv=(None, "unknown"))
    with patch.object(alc, "original_language_lookup", lookup):
        alc.recheck_unknown_originals(_state())
    asked = [str(p) for p in lookup.calls]
    assert len(asked) == 3                                         # the bound still holds
    assert sum("/movies/" in a for a in asked) == 1 and sum("/tv/" in a for a in asked) == 2


def test_both_services_down_costs_one_lookup_each_and_changes_nothing(tmp_path):
    movies = [_finding_video(tmp_path, i) for i in range(3)]
    episodes = [_tv_video(tmp_path, i) for i in range(3)]
    _write_state({v: _unknown_entry(v) for v in movies + episodes})
    before = alc.STATE_FILE.read_text()
    lookup = _PerKindLookup(movies=(None, "unavailable"), tv=(None, "unavailable"))
    with patch.object(alc, "original_language_lookup", lookup):
        alc.recheck_unknown_originals(_state())
    assert len(lookup.calls) == 2
    assert alc.STATE_FILE.read_text() == before


def test_a_path_outside_movies_and_tv_is_its_own_group(tmp_path):
    # Never a Radarr/Sonarr question: an "unavailable" for it must not silence a real kind.
    other = _make_video(tmp_path / "media", "misc/Thing/t.mkv", HOUR)
    movie = _finding_video(tmp_path)
    _write_state({other: _unknown_entry(other, checked_at="2026-10-01T00:00:00+00:00"),
                  movie: _unknown_entry(movie, checked_at="2026-10-02T00:00:00+00:00")})

    def lookup(path):
        return (None, "unavailable") if "/misc/" in str(path) else ("eng", "known")

    with patch.object(alc, "original_language_lookup", lookup):
        alc.recheck_unknown_originals(_state())
    assert _state()[str(movie.resolve())]["original_status"] == "known"


def test_the_recheck_is_bounded_per_run_and_works_through_the_backlog_over_runs(tmp_path):
    videos = [_make_video(tmp_path / "media", f"movies/F{i:03d} (2000)/f.mkv", HOUR, now=_now())
              for i in range(alc.MAX_ORIGINAL_RECHECKS + 5)]
    _write_state({v: _unknown_entry(v, checked_at=f"2026-10-01T00:00:{i % 60:02d}.{i:06d}+00:00")
                  for i, v in enumerate(videos)})
    spy = _LookupSpy((None, "unknown"))
    with patch.object(alc, "original_language_lookup", spy):
        alc.recheck_unknown_originals(_state())
        assert len(spy.calls) == alc.MAX_ORIGINAL_RECHECKS
        first = {str(p) for p in spy.calls}
        state = _state()
        rechecked = {k for k, e in state.items() if "original_rechecked_at" in e}
        assert rechecked == first
        spy.calls.clear()
        alc.recheck_unknown_originals(_state())        # the 5 not yet looked at go first
        assert {str(p) for p in spy.calls[:5]} == {str(v.resolve()) for v in videos} - first
    assert len(spy.calls) == alc.MAX_ORIGINAL_RECHECKS


def test_the_recheck_logs_what_it_did(tmp_path, capsys):
    video = _finding_video(tmp_path)
    _write_state({video: _unknown_entry(video)})
    with patch.object(alc, "original_language_lookup", _LookupSpy(("eng", "known"))):
        alc.recheck_unknown_originals(_state())
    out = capsys.readouterr().out
    assert "is now eng" in out and "NO_ENGLISH_AUDIO" in out
    assert "1 looked up, 1 now known, 1 with changed findings" in out


def test_dry_run_does_no_original_language_recheck(tmp_path):
    video = _one_video(tmp_path)
    _write_state({video: _unknown_entry(video)})
    before = alc.STATE_FILE.read_text()
    spy = _LookupSpy(("eng", "known"))
    stack, _m = _enter(_patch_world())
    with stack, patch.object(alc, "original_language_lookup", spy):
        assert _run_main(["--dry-run"], tmp_path=tmp_path) == 0
    assert spy.calls == [] and alc.STATE_FILE.read_text() == before


def test_a_failing_recheck_never_stops_the_run(tmp_path, capsys):
    _seed_library(tmp_path)
    stack, (_, probe, *_rest) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "eng")), detect=_lid_forever(("en", 0.9))))
    with stack, patch.object(alc, "recheck_unknown_originals", side_effect=RuntimeError("boom")):
        assert _run_main([], tmp_path=tmp_path) == 0
    assert "recheck failed" in capsys.readouterr().out and probe.call_count == 2


def test_one_lookup_fetch_per_kind_serves_every_entry_through_the_real_arr_cache(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from media_stack import arrlang
    monkeypatch.setattr(real_paths, "MEDIA_ROOT", tmp_path / "media")
    monkeypatch.setenv("RADARR_API_KEY", "key")
    arrlang._CACHE.clear()
    videos = [_make_video(tmp_path / "media", f"movies/Film {i} (2000)/f.mkv", HOUR, now=_now())
              for i in range(3)]
    fake = MagicMock()
    fake.movies.return_value = [
        {"path": f"/movies/Film {i} (2000)", "originalLanguage": {"name": "English"}}
        for i in range(2)]                                   # Film 2 is still missing
    _write_state({v: _unknown_entry(v) for v in videos})
    try:
        with patch.object(arrlang, "ArrClient", return_value=fake):
            alc.recheck_unknown_originals(_state())
    finally:
        arrlang._CACHE.clear()
    assert fake.movies.call_count == 1
    state = _state()
    statuses = sorted(e["original_status"] for e in state.values())
    assert statuses == ["known", "known", "unknown"]


@pytest.mark.parametrize("attempt", ["eng", "jpn"])
def test_recheck_obeys_the_same_rules_as_the_first_analysis(attempt):
    """Re-deciding from stored detections gives exactly what analysing with the original known would."""
    scenarios = [
        (_info(_stream(1, "spa")), {1: [("es", 0.95), ("es", 0.9)]}),                 # foreign only
        (_info(_stream(1, "spa")), {1: [("es", 0.2), ("es", 0.3), ("es", 0.1)]}),     # unclear
        (_info(_stream(1, "eng")), {1: [("es", 0.9), ("es", 0.9)]}),                  # mislabeled
        (_info(_stream(1, "eng")), {1: [("en", 0.9)]}),                               # fine
        (_info(_stream(1, None)), {1: [("en", 0.7)]}),                                # untagged English
        (_info(_stream(1, "spa"), _stream(2, None)),
         {1: [("es", 0.95)], 2: [("fr", 0.9), ("fr", 0.9)]}),                         # two foreign
        (_info(_stream(1, "spa"), _stream(2, "eng")),
         {1: [("es", 0.95)], 2: [("en", 0.9)]}),                                      # one English
        (_info(_stream(1, "eng"), duration=100.0), {1: [("en", 0.9)]}),               # too short
    ]
    for info, script in scenarios:
        (expected, _, exp_result, _n), _f = _analyze(info, script, original=attempt)
        (f0, recs, _r, _n0), _f = _analyze(info, script, original=None)
        compact = alc._compact_streams(recs)
        mislabeled = [f for f in f0 if f["type"] == "MISLABELED"]
        skipped = any(s.get("mislabel_skipped") for s in compact)
        got, got_result, _ = alc._decide(attempt, compact, mislabeled, skipped)
        assert (got, got_result) == (expected, exp_result), (info, script)


@pytest.mark.parametrize("patch_", [
    {"original_status": "maybe"}, {"original_status": "unavailable"}, {"original_status": None},
    {"original_status": 5},
    {"original_status": "known", "original_language": None},
    {"original_status": "known", "original_language": "fra"},
    {"original_status": "known", "original_language": 7},
    {"original_status": "unknown", "original_language": "eng"},
    {"original_language": "eng"},                                   # language without a status
    {"streams": {"index": 1}}, {"streams": "x"}, {"streams": [None]}, {"streams": [5]},
    {"streams": [{**SPA_REC, "index": "1"}]},
    {"streams": [{k: v for k, v in SPA_REC.items() if k != "reading"}]},
    {"streams": [{k: v for k, v in SPA_REC.items() if k != "counts_as_english"}]},
    {"streams": [{**SPA_REC, "counts_as_english": "yes"}]},
    {"streams": [{**SPA_REC, "reading": "maybe"}]},
    {"streams": [{**SPA_REC, "probability": 1.5}]},
    {"streams": [{**SPA_REC, "probability": "0.9"}]},
    {"streams": [{**SPA_REC, "tag": 3}]},
    {"streams": [{**SPA_REC, "detected_language": 3}]},
    {"streams": [{**SPA_REC, "mislabel_skipped": "yes"}]},
    {"original_rechecked_at": 5},
])
def test_malformed_stored_detection_fields_are_rejected(patch_):
    entry = {"identity": _OK_IDENT, "result": "CHECKED", "findings": [],
             "original_status": "unknown", "original_language": None, "streams": [dict(SPA_REC)]}
    entry.update(patch_)
    assert alc.entry_problem(entry)


@pytest.mark.parametrize("entry", [
    {"identity": _OK_IDENT, "result": "CHECKED", "findings": [], "original_status": "unknown",
     "original_language": None, "streams": [dict(SPA_REC)]},
    {"identity": _OK_IDENT, "result": "CHECKED", "findings": [], "original_status": "known",
     "original_language": "eng", "streams": [dict(SPA_REC), dict(SPA_REC, index=2, tag=None)]},
    {"identity": _OK_IDENT, "result": "UNVERIFIED", "findings": [], "original_status": "unknown",
     "original_language": None, "streams": [], "original_rechecked_at": "t"},
    {"identity": _OK_IDENT, "result": "UNVERIFIED", "findings": [], "streams": [
        {"index": 1, "tag": "eng", "detected_language": None, "probability": None,
         "counts_as_english": True, "reading": None, "mislabel_skipped": True}]},
])
def test_well_formed_stored_detection_fields_validate(entry):
    assert alc.entry_problem(entry) is None


def test_a_malformed_stored_detection_in_the_state_is_blinding_before_any_write(tmp_path):
    _seed_library(tmp_path)
    content = json.dumps({"/m/a.mkv": {"identity": _OK_IDENT, "result": "CHECKED", "findings": [],
                                       "original_status": "known", "original_language": None}})
    alc.STATE_FILE.write_text(content)
    spy = _LookupSpy(("eng", "known"))
    stack, (_, probe, *_rest) = _enter(_patch_world())
    with stack, patch.object(alc, "original_language_lookup", spy):
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 1
    assert alc.STATE_FILE.read_text() == content and spy.calls == []
    probe.assert_not_called()


# --- undecodable file names must not block the alert queue ---------------------------

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


def test_clean_text_replaces_lone_surrogates_only():
    assert alc._clean_text("ok \udcff \ud800 café \U0001F600") == "ok � � café \U0001F600"
    assert alc._clean_text("") == ""


def test_measuring_and_truncating_text_with_lone_surrogates_never_raises():
    s = "Bad\udcff name " * 100
    assert alc._tg_len(s) > 0
    cut = alc._truncate(s, 50)
    cut.encode("utf-8")
    assert alc._tg_len(cut) <= 50 and cut.endswith("…")
    alc._truncate("\udcff", 5).encode("utf-8")


def test_logging_a_path_with_undecodable_bytes_does_not_raise_on_a_strict_stream(monkeypatch):
    import io
    strict = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    monkeypatch.setattr(sys, "stdout", strict)
    alc.log("checking /m/movies/Bad\udcff/x.mkv")
    strict.flush()
    assert "�" in strict.buffer.getvalue().decode("utf-8")


def test_a_message_naming_an_undecodable_file_builds_encodes_and_stays_in_budget(tmp_path):
    bad = _undecodable_video(tmp_path)
    pending = [(str(bad), {"findings": MISLABEL, "first_found": "t0"}),
               ("/m/movies/Weird\udc80\udcff/f\ud800.mkv", {"findings": NO_ENGLISH, "first_found": "t1"})]
    text, named = alc.build_alert_message(pending)
    text.encode("utf-8")
    text.encode("utf-16-le")
    assert alc._tg_len(text) <= alc.TELEGRAM_BUDGET_CHARS and len(named) == 2
    assert "�" in text


def test_build_alert_message_never_raises_even_if_formatting_does():
    pending = [("/m/a.mkv", {"findings": MISLABEL}), ("/m/b.mkv", {"findings": MISLABEL})]
    with patch.object(alc, "_format_finding_line", side_effect=RuntimeError("boom")):
        text, named = alc.build_alert_message(pending)
    assert "- ?" in text and len(named) == 2


def test_a_backlog_headed_by_an_undecodable_name_still_sends_later_findings(tmp_path):
    """An entry whose text cannot be measured or sent does not wedge the oldest-first queue."""
    bad = _undecodable_video(tmp_path)
    good = _finding_video(tmp_path)
    _seed_finding(bad, MISLABEL, "2026-10-01T00:00:00+00:00")        # oldest
    _seed_finding(good, MISLABEL, "2026-10-02T00:00:00+00:00")
    sent = []
    with patch.dict(os.environ, _alert_env(tmp_path), clear=True), \
         patch.object(alc, "telegram_send", side_effect=_strict_send(sent)):
        alc.retry_alerts()
    assert len(sent) == 1 and "Film 00" in sent[0]
    view = _findings_view()
    assert view[str(bad.resolve())]["alerted"] is True and view[str(good.resolve())]["alerted"] is True


def test_an_undecodable_file_name_works_end_to_end_through_a_real_run(tmp_path):
    bad = _undecodable_video(tmp_path)
    good = _one_video(tmp_path)
    sent = []
    stack, (_, probe, *_rest) = _enter(_patch_world(
        probe_side_effect=lambda p: _info(_stream(1, "spa")), detect=_lid_forever(("es", 0.95))))
    with stack, patch.object(alc, "telegram_send", side_effect=_strict_send(sent)):
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 0
    assert probe.call_count == 2
    assert len(sent) == 1 and sent[0].count("\n- ") == 2          # both files in ONE message
    state = _state()
    assert state[str(bad.resolve())]["alerted"] is True
    assert state[str(good.resolve())]["alerted"] is True
    report = json.loads(alc.REPORT_FILE.read_text())
    assert str(bad.resolve()) in report


def test_the_jsonl_and_state_round_trip_an_undecodable_name(tmp_path):
    bad = _undecodable_video(tmp_path)
    assert alc._record_result(bad, alc._identity(bad), {"result": "CHECKED", "findings": []}, "t")
    assert str(bad.resolve()) in alc._load_state(alc.STATE_FILE)
    alc._append_jsonl(alc.JSONL_FILE, {"path": str(bad)})
    assert json.loads(alc.JSONL_FILE.read_text())["path"] == str(bad)


# --- the per-run bound applies to LIVE entries only ---------------------------------

def test_stale_entries_cannot_starve_a_live_one_of_the_recheck_budget(tmp_path):
    """Gone/changed entries sorted ahead of a live one do not consume the lookup budget."""
    n = alc.MAX_ORIGINAL_RECHECKS
    gone = [_finding_video(tmp_path, i) for i in range(n // 2)]
    changed = [_finding_video(tmp_path, 1000 + i) for i in range(n - n // 2)]
    live = _finding_video(tmp_path, 5000)
    entries = {v: _unknown_entry(v, checked_at=f"2026-10-01T00:00:00.{i:06d}+00:00")
               for i, v in enumerate(gone + changed)}
    entries[live] = _unknown_entry(live, checked_at="2026-10-02T00:00:00+00:00")   # sorts LAST
    _write_state(entries)
    for v in gone:
        v.unlink()
    for v in changed:
        _replace(v)
    spy = _LookupSpy(("eng", "known"))
    with patch.object(alc, "original_language_lookup", spy):
        alc.recheck_unknown_originals(_state())
    assert [str(p) for p in spy.calls] == [str(live.resolve())]
    entry = _state()[str(live.resolve())]
    assert entry["original_status"] == "known"
    assert [f["type"] for f in entry["findings"]] == ["NO_ENGLISH_AUDIO"]


def test_the_budget_is_spent_on_live_entries_and_stale_ones_cost_no_lookup(tmp_path):
    n = alc.MAX_ORIGINAL_RECHECKS
    stale = [_finding_video(tmp_path, i) for i in range(50)]
    live = [_make_video(tmp_path / "media", f"movies/L{i:03d} (2000)/l.mkv", HOUR, now=_now())
            for i in range(n + 10)]
    entries = {v: _unknown_entry(v, checked_at=f"2026-10-01T00:00:00.{i:06d}+00:00")
               for i, v in enumerate(stale)}
    entries.update({v: _unknown_entry(v, checked_at=f"2026-10-02T00:00:00.{i:06d}+00:00")
                    for i, v in enumerate(live)})
    _write_state(entries)
    for v in stale:
        v.unlink()
    spy = _LookupSpy((None, "unknown"))
    with patch.object(alc, "original_language_lookup", spy):
        alc.recheck_unknown_originals(_state())
    assert len(spy.calls) == n                                   # the full budget, all on live files
    assert not ({str(p) for p in spy.calls} & {str(v.resolve()) for v in stale})


def test_stale_entries_are_left_exactly_as_they_were(tmp_path):
    gone, live = _finding_video(tmp_path, 1), _finding_video(tmp_path, 2)
    _write_state({gone: _unknown_entry(gone), live: _unknown_entry(live)})
    gone_before = _state()[str(gone.resolve())]
    gone.unlink()
    with patch.object(alc, "original_language_lookup", _LookupSpy((None, "unknown"))):
        alc.recheck_unknown_originals(_state())
    assert _state()[str(gone.resolve())] == gone_before          # no stamp, no drop


def test_a_state_of_only_stale_entries_does_nothing(tmp_path):
    video = _finding_video(tmp_path)
    _write_state({video: _unknown_entry(video)})
    video.unlink()
    before = alc.STATE_FILE.read_text()
    spy = _LookupSpy(("eng", "known"))
    with patch.object(alc, "original_language_lookup", spy):
        alc.recheck_unknown_originals(_state())
    assert spy.calls == [] and alc.STATE_FILE.read_text() == before


# --- validation is TOTAL ------------------------------------------------------------

def _full_entry():
    """An entry exercising every validated field, valid as a whole."""
    return {
        "identity": [1, 2, 3], "result": "CHECKED", "checked_at": "t", "first_found": "t",
        "alerted": False, "original_status": "known", "original_language": "eng",
        "original_rechecked_at": "t",
        "findings": [
            {"type": "MISLABELED", "stream_index": 1, "tag": "eng", "detected_language": "es",
             "probability": 0.9},
            {"type": "NO_ENGLISH_AUDIO", "original_language": "eng",
             "streams": [{"index": 1, "tag": "spa", "detected_language": "es", "probability": 0.9}]},
        ],
        "streams": [{"index": 1, "tag": "spa", "detected_language": "es", "probability": 0.9,
                     "counts_as_english": False, "reading": "foreign", "mislabel_skipped": False}],
        "retry": {"identity": [1, 2, 3], "attempts": 2, "next_retry_at": 5.0,
                  "last_result": "ERROR", "last_error": "x", "last_attempt_at": "t"},
    }


_JUNK = [[], {}, [[]], [{}], {"a": []}, ["x"], [1], None, 0, 1, -1, 1.5, float("nan"),
         float("inf"), float("-inf"), 10 ** 400, -10 ** 400, True, False, "", "x", "CHECKED",
         "known", "foreign"]


def _paths(obj, prefix=()):
    """Every path (tuple of keys/indices) to a leaf or container in `obj`."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(prefix + (k,))
            out.extend(_paths(v, prefix + (k,)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.append(prefix + (i,))
            out.extend(_paths(v, prefix + (i,)))
    return out


def _with(obj, path, value):
    import copy
    new = copy.deepcopy(obj)
    cur = new
    for p in path[:-1]:
        cur = cur[p]
    cur[path[-1]] = value
    return new


def _without_path(obj, path):
    import copy
    new = copy.deepcopy(obj)
    cur = new
    for p in path[:-1]:
        cur = cur[p]
    del cur[path[-1]]
    return new


def test_the_fuzz_baseline_entry_is_valid():
    assert alc.entry_problem(_full_entry()) is None


def test_entry_problem_never_raises_for_any_junk_in_any_validated_field():
    entry = _full_entry()
    paths = _paths(entry)
    assert len(paths) > 40                       # it really does walk every field
    checked = 0
    for path in paths:
        for junk in _JUNK:
            mutated = _with(entry, path, junk)
            problem = alc.entry_problem(mutated)          # must not raise
            assert problem is None or isinstance(problem, str)
            checked += 1
        problem = alc.entry_problem(_without_path(entry, path))   # a missing field must not raise
        assert problem is None or isinstance(problem, str)
    assert checked > 900


def test_finding_problem_never_raises_for_junk_findings_or_junk_fields():
    for finding in _full_entry()["findings"]:
        for path in _paths(finding):
            for junk in _JUNK:
                assert isinstance(alc.finding_problem(_with(finding, path, junk)), (str, type(None)))
    for junk in _JUNK:
        assert isinstance(alc.finding_problem(junk), str)      # not a finding at all


def test_entry_problem_never_raises_for_a_junk_entry_itself():
    for junk in _JUNK + [{"result": j} for j in _JUNK] + [{"findings": j} for j in _JUNK]:
        assert isinstance(alc.entry_problem(junk), (str, type(None)))


@pytest.mark.parametrize("path,junk", [
    (("result",), []), (("result",), {}), (("result",), [[]]),
    (("original_status",), []), (("original_status",), {}),
    (("original_language",), []), (("original_language",), {"a": 1}),
    (("findings", 0, "type"), []), (("findings", 1, "type"), {}),
    (("streams", 0, "reading"), []), (("streams", 0, "reading"), {}),
    (("retry", "last_result"), []), (("retry", "last_result"), {"x": 1}),
    (("findings", 0, "probability"), 10 ** 400), (("retry", "next_retry_at"), 10 ** 400),
    (("streams", 0, "probability"), 10 ** 400),
])
def test_unhashable_or_oversized_values_are_reported_as_malformed_not_raised(path, junk):
    problem = alc.entry_problem(_with(_full_entry(), path, junk))
    assert isinstance(problem, str) and problem


def test_unhashable_result_in_the_state_is_a_blinding_exit_with_path_and_reason(tmp_path, capsys):
    _seed_library(tmp_path)
    content = '{"/m/a.mkv": {"identity": [1, 2, 3], "result": [], "findings": []}}'
    alc.STATE_FILE.write_text(content)
    stack, (_, probe, _, _, send) = _enter(_patch_world())
    with stack:
        assert _run_main([], env=_alert_env(tmp_path), tmp_path=tmp_path) == 1
    out = capsys.readouterr().out
    assert "/m/a.mkv" in out and "result" in out
    probe.assert_not_called()
    send.assert_not_called()
    assert alc.STATE_FILE.read_text() == content
    assert not alc.REPORT_FILE.exists() and not alc.JSONL_FILE.exists()


@pytest.mark.parametrize("entry_json", [
    '{"original_status": [], "original_language": null}',
    '{"original_status": "unknown", "original_language": []}',
    '{"findings": [{"type": []}]}',
    '{"findings": [{"type": {}}]}',
    '{"streams": [{"index": 1, "tag": null, "detected_language": null, "probability": null, '
    '"counts_as_english": true, "reading": []}]}',
    '{"retry": {"identity": [1,2,3], "attempts": 1, "next_retry_at": 5, "last_result": []}}',
    '{"retry": {"identity": [1,2,3], "attempts": 1, "next_retry_at": 1' + "0" * 400 + '}}',
    '{"findings": [{"type": "MISLABELED", "stream_index": 1, "tag": "e", '
    '"detected_language": "es", "probability": 1' + "0" * 400 + '}]}',
])
def test_every_unhashable_or_oversized_field_in_the_state_is_blinding_not_a_crash(tmp_path, entry_json):
    content = '{"/m/a.mkv": ' + entry_json + '}'
    alc.STATE_FILE.write_text(content)
    assert alc._load_state(alc.STATE_FILE) is None             # logged, not raised
    _seed_library(tmp_path)
    stack, (_, probe, *_rest) = _enter(_patch_world())
    with stack:
        assert _run_main([], tmp_path=tmp_path) == 1
        assert _run_main(["--dry-run"], tmp_path=tmp_path) == 1
    probe.assert_not_called()
    assert alc.STATE_FILE.read_text() == content


def test_formatting_and_backoff_survive_an_oversized_integer():
    huge = 10 ** 400
    assert alc._p(huge) == "?" and alc._finite_number(huge) is False
    assert alc._finite_number(True) is False and alc._finite_number(1.5) is True
    entry = {"retry": {"identity": [1, 2, 3], "next_retry_at": huge}}
    assert alc._backing_off(entry, [1, 2, 3], 0.0) is False       # malformed counts as due
