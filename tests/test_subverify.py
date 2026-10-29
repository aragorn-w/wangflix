"""`media_stack.subverify`: ASR-based English-subtitle verification.

No real ffmpeg, whisper model or Jellyfin: `subprocess.Popen`, `subverify._get_model`
and (in CLI-mode tests) `JellyfinClient` are mocked.
"""

import itertools
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from media_stack import subverify as sv


# --- english_audio_stream / is_audio_explicitly_english -----------------

def _astream(index, lang=None, codec_type="audio"):
    return {"index": index, "codec_type": codec_type,
            "tags": ({"language": lang} if lang is not None else {})}


def _stream(index, tags=None, **extra):
    return {"index": index, "codec_type": "audio", "tags": tags or {}, **extra}


@pytest.mark.parametrize("streams, expected", [
    pytest.param([_astream(1, "jpn"), _astream(2, "eng")], 2, id="prefers_explicit_english_tag"),
    pytest.param([_astream(1, "jpn"), _astream(3, None)], 3, id="falls_back_to_untagged_when_no_english"),
    pytest.param([_astream(1, "jpn"), _astream(2, "fre")], None, id="none_when_every_stream_confidently_foreign"),
    pytest.param([_stream(1, {"language": "eng", "title": "Director's Commentary"}),
                  _stream(2, {"language": "eng"})], 2, id="excludes_commentary_track_listed_first"),
    pytest.param([_stream(1, {"language": "eng"}, disposition={"visual_impaired": 1}),
                  _stream(2)], 2, id="excludes_audio_description_so_pick_falls_to_untagged_tier"),
    pytest.param([_stream(1, {"language": "eng"}),
                  _stream(2, {"language": "eng"}, disposition={"default": 1})], 2,
                 id="prefers_default_flagged_among_explicit_english"),
])
def test_english_audio_stream(streams, expected):
    result = sv.english_audio_stream({"streams": streams})
    assert result == expected
    assert (result is None) is (expected is None)


@pytest.mark.parametrize("streams, index, expected", [
    pytest.param([_astream(1, "jpn"), _astream(2, "eng")], 2, True, id="explicit_tag"),
    pytest.param([_astream(1, "jpn"), _astream(3, None)], 3, False, id="untagged"),
])
def test_is_audio_explicitly_english(streams, index, expected):
    assert sv.is_audio_explicitly_english({"streams": streams}, index) is expected


# --- overlap(): word_ratio + bigram_ratio + asr_word_count --------------

_SRT = """1
00:00:00,000 --> 00:00:03,000
Hello there, this episode begins right now.

2
00:04:50,000 --> 00:04:53,000
Something completely unrelated happens here later.

3
00:05:10,000 --> 00:05:13,000
The captain orders everyone to abandon ship immediately.

4
00:06:00,000 --> 00:06:03,000
Long after the window ends nobody mentions submarines again.
"""


def test_overlap_right_episode_passes_both_thresholds():
    asr_text = "the captain orders everyone abandon ship immediately right now"
    word_ratio, bigram_ratio, n = sv.overlap(asr_text, _SRT, start_s=300, dur_s=30)
    assert word_ratio >= 0.60
    assert bigram_ratio >= 0.40
    assert n >= 5


def test_overlap_wrong_episode_fails_threshold():
    asr_text = "purple bicycles orbit distant mountains quietly every morning"
    word_ratio, bigram_ratio, n = sv.overlap(asr_text, _SRT, start_s=300, dur_s=30)
    assert word_ratio < 0.60
    assert bigram_ratio < 0.40


# A wrong episode can share most of the right one's vocabulary in different word pairs;
# bigram_ratio must catch what word_ratio misses.
_WRONG_EPISODE_SRT = """1
00:00:00,000 --> 00:00:03,000
Peter tells homer the chicken fight starts tonight behind the bar.

2
00:00:04,000 --> 00:00:07,000
Lois warns meg that stewie brian quahog plans could ruin everything later.
"""
_WRONG_EPISODE_ASR = (
    "stewie warns everything later that brian ruin plans quahog could "
    "homer tells peter the fight chicken starts behind tonight bar lois meg"
)


def test_overlap_wrong_episode_with_shared_vocabulary_fails_on_bigram_despite_high_word_ratio():
    word_ratio, bigram_ratio, n = sv.overlap(
        _WRONG_EPISODE_ASR, _WRONG_EPISODE_SRT, start_s=0, dur_s=10)
    assert word_ratio >= 0.60   # same vocabulary
    assert bigram_ratio < 0.40  # scrambled order
    assert n >= 15


@pytest.mark.parametrize("asr_text, dur_s, expected", [
    # window = [270, 350] (start=300, dur=20); cue 4 at t=360 is outside it
    pytest.param("submarines mentioned nobody window ends long after", 20, 0.0,
                 id="excludes_cues_outside_the_padded_range"),
    # cue 2 at t=290s is before start_s=300 but within the 30s lookback pad
    pytest.param("something completely unrelated happens here later", 10, 1.0,
                 id="includes_cue_within_the_30s_pad"),
])
def test_overlap_window_padding(asr_text, dur_s, expected):
    word_ratio, _, _ = sv.overlap(asr_text, _SRT, start_s=300, dur_s=dur_s)
    assert word_ratio == expected


def test_overlap_no_asr_words_returns_zero_for_both_ratios():
    word_ratio, bigram_ratio, n = sv.overlap("", _SRT, start_s=300, dur_s=10)
    assert word_ratio == 0.0
    assert bigram_ratio == 0.0
    assert n == 0


# --- _extract_wav: Popen-based, playback-aware ---------------------------

class _FakeProc:
    """Popen stand-in; `communicate_plan` holds `("timeout",)` (raises TimeoutExpired) or `("ok", stdout, stderr)`, one per call."""

    def __init__(self, communicate_plan, returncode=0):
        self._plan = list(communicate_plan)
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self._exited = False

    def communicate(self, timeout=None):
        step = self._plan.pop(0)
        if step[0] == "timeout":
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
        self._exited = True
        return step[1], step[2]

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def poll(self):
        return self.returncode if (self.terminated or self.killed or self._exited) else None


def _touch_output(cmd):
    """ffmpeg's real side effect: write to the output path (the last argv)."""
    Path(cmd[-1]).write_bytes(b"RIFF....WAVEfmt ")


class _FakePopen:
    """`Popen` replacement: writes the output file like ffmpeg (unless `touch=False`) and keeps the `cmd` and `proc` it served."""

    def __init__(self, plan=(("ok", "", ""),), returncode=0, touch=True):
        self.plan, self.returncode, self.touch = plan, returncode, touch

    def __call__(self, cmd, **kwargs):
        self.cmd = cmd
        if self.touch:
            _touch_output(cmd)
        self.proc = _FakeProc(self.plan, self.returncode)
        return self.proc


@pytest.fixture
def extract():
    """Run `_extract_wav` on /video.mkv against a fake ffmpeg; any wav it returns is removed at teardown."""
    made = []

    def run(popen, *args, **kwargs):
        with patch.object(sv.subprocess, "Popen", side_effect=popen):
            wav = sv._extract_wav(Path("/video.mkv"), *args, **kwargs)
        made.append(wav)
        return wav

    yield run
    for wav in made:
        wav.unlink(missing_ok=True)


@pytest.mark.parametrize("args, ss, t", [
    pytest.param((2, 10.0, 5.0), "10.0", "5.0", id="window"),
    pytest.param((0, None, None), None, None, id="whole_file_omits_ss_and_t"),
])
def test_extract_wav_builds_argv(extract, args, ss, t):
    popen = _FakePopen()
    assert extract(popen, *args).exists()
    cmd = popen.cmd
    assert cmd[cmd.index("-map") + 1] == f"0:{args[0]}"
    assert "/video.mkv" in cmd
    for flag, value in (("-ss", ss), ("-t", t)):
        if value is None:
            assert flag not in cmd
        else:
            assert cmd[cmd.index(flag) + 1] == value


def test_extract_wav_lands_in_given_workdir_and_is_removable(extract, tmp_path):
    """The wav lands inside the given workdir (never the system temp dir) and the caller can remove it."""
    wav = extract(_FakePopen(), 0, 1.0, 2.0, workdir=tmp_path)
    assert wav.exists()
    assert wav.parent == tmp_path
    wav.unlink()
    assert not wav.exists()


def test_extract_wav_ffmpeg_failure_raises_and_cleans_up(extract):
    with pytest.raises(RuntimeError, match="ffmpeg audio extract failed"):
        extract(_FakePopen([("ok", "", "boom")], returncode=1, touch=False), 0, 1.0, 2.0)


def test_extract_wav_checks_playback_before_ever_starting_ffmpeg():
    """A busy/unknown pre-start check raises ExtractionInterrupted without spawning ffmpeg."""
    with patch.object(sv.subprocess, "Popen") as mock_popen:
        with pytest.raises(sv.ExtractionInterrupted):
            sv._extract_wav(Path("/video.mkv"), 0, 1.0, 2.0, should_abort=lambda: True)
    mock_popen.assert_not_called()


def test_extract_wav_aborts_mid_run_terminates_and_cleans_up(extract):
    """should_abort firing after ffmpeg started terminates the running process."""
    popen = _FakePopen([("timeout",), ("ok", "", "")])
    # pass the pre-start check, fire on the first poll
    with pytest.raises(sv.ExtractionInterrupted):
        extract(popen, 0, 1.0, 2.0, should_abort=Mock(side_effect=[False, True]))
    assert popen.proc.terminated is True


def test_extract_wav_does_not_abort_when_should_abort_is_false(extract):
    extract(_FakePopen([("timeout",), ("ok", "", "")]), 0, 1.0, 2.0, should_abort=lambda: False)


def test_extract_wav_hard_deadline_kills_process_and_raises(extract):
    # plan[0]: ffmpeg still running; plan[1]: the blocking communicate() after kill().
    popen = _FakePopen([("timeout",), ("ok", "", "")], touch=False)
    # monotonic() jumps past the deadline on the first poll; repeat() tail avoids StopIteration.
    times = itertools.chain([0.0, 10_000_000.0], itertools.repeat(10_000_000.0))
    with patch.object(sv.time, "monotonic", side_effect=lambda: next(times)):
        with pytest.raises(RuntimeError, match="timed out"):
            extract(popen, 0, 1.0, 2.0, should_abort=lambda: False)


# --- _srt_timestamp: rounds TOTAL ms first, never a 4-digit ms field ----

@pytest.mark.parametrize("seconds, expected", [
    pytest.param(1.9996, "00:00:02,000", id="rounding_overflow_carries_into_seconds"),  # per-field rounding gave 00:00:01,1000
    pytest.param(3725.125, "01:02:05,125", id="ordinary_value"),
])
def test_srt_timestamp(seconds, expected):
    assert sv._srt_timestamp(seconds) == expected


# --- asr_window / transcribe_to_srt / detect_language (mocked extraction) -

_FakeSegment = SimpleNamespace


def _fake_wav(tmp_path: Path) -> Path:
    p = tmp_path / "fake.wav"
    p.write_bytes(b"RIFF....WAVEfmt ")
    return p


def _model(segments=(), info=None):
    return SimpleNamespace(transcribe=lambda path, **kw: (list(segments), info))


@contextmanager
def _mocked_asr(wav, model):
    """Patch `_extract_wav` to hand back `wav` and `_get_model` to hand back `model`; yields both mocks."""
    with patch.object(sv, "_extract_wav", return_value=wav) as mock_extract, \
         patch.object(sv, "_get_model", return_value=model) as mock_get_model:
        yield mock_extract, mock_get_model


def _srt_of(tmp_path, segments):
    """`transcribe_to_srt` over fake segments; the extracted wav must be gone afterwards."""
    wav = _fake_wav(tmp_path)
    with _mocked_asr(wav, _model(segments)):
        srt = sv.transcribe_to_srt(Path("/video.mkv"), 1, "medium.en", 6)
    assert not wav.exists()
    return srt


def test_asr_window_concatenates_segment_text_and_cleans_up_wav(tmp_path):
    wav = _fake_wav(tmp_path)
    model = _model([_FakeSegment(text=" hello "), _FakeSegment(text="world ")])
    with _mocked_asr(wav, model) as (mock_extract, mock_get_model):
        text = sv.asr_window(Path("/video.mkv"), 1, 300, 30, "small.en", 4, tmp_path)

    assert text == " hello  world "
    mock_extract.assert_called_once_with(
        Path("/video.mkv"), 1, 300, 30, workdir=tmp_path, should_abort=None)
    mock_get_model.assert_called_once_with("small.en", 4)
    assert not wav.exists()


def test_asr_window_forwards_should_abort_to_extract_wav(tmp_path):
    sentinel = lambda: False  # noqa: E731
    with _mocked_asr(_fake_wav(tmp_path), _model()) as (mock_extract, _):
        sv.asr_window(Path("/video.mkv"), 1, 300, 30, "small.en", 4, Path("/work"),
                     should_abort=sentinel)
    mock_extract.assert_called_once_with(
        Path("/video.mkv"), 1, 300, 30, workdir=Path("/work"), should_abort=sentinel)


def test_transcribe_to_srt_without_word_timings_still_builds_sequential_cues(tmp_path):
    """Segments with no `.words` fall back to spreading the text over the segment's span."""
    srt = _srt_of(tmp_path, [
        _FakeSegment(text=" Hello there. ", start=0.0, end=2.5),
        _FakeSegment(text="", start=2.5, end=2.6),  # empty text dropped
        _FakeSegment(text="General Kenobi.", start=3.0, end=5.0),
    ])

    assert "1\n00:00:00,000 --> 00:00:02,500\nHello there." in srt
    assert "2\n00:00:03,000 --> 00:00:05,000\nGeneral Kenobi." in srt


def test_a_30_second_segment_without_word_timings_is_cut_into_cues_of_at_most_7_seconds(tmp_path):
    """A long segment with no alignment is cut into consecutive cues of at most 7 s and two 42-character lines."""
    text_ = " ".join(f"word{i}" for i in range(60)) + "."
    srt = _srt_of(tmp_path, [_FakeSegment(text=" " + text_, start=10.0, end=40.0)])

    spans = [tuple(sv._parse_srt_timestamp(t) for t in m.groups())
             for m in sv._CUE_TIME_RE.finditer(srt)]
    assert len(spans) >= 5
    assert all(end - start <= 7.0 + 1e-6 for start, end in spans)
    assert spans[0][0] == pytest.approx(10.0) and spans[-1][1] == pytest.approx(40.0, abs=0.01)
    assert all(a[1] <= b[0] + 1e-6 for a, b in zip(spans, spans[1:]))  # consecutive, no overlap
    body_lines = [ln for ln in srt.split("\n") if ln and "-->" not in ln and not ln.isdigit()]
    assert " ".join(body_lines) == text_
    assert all(len(ln) <= 42 for ln in body_lines)


def test_transcribe_to_srt_empty_segments_yields_empty_string(tmp_path):
    assert _srt_of(tmp_path, []) == ""


def _sw(word, start, end):
    """A faster-whisper-shaped Word."""
    return SimpleNamespace(word=word, start=start, end=end, probability=0.9)


def test_transcribe_to_srt_builds_cues_from_words_not_segments(tmp_path):
    """A two-sentence segment becomes two cues split at the sentence end, timed first-word-start to last-word-end."""
    srt = _srt_of(tmp_path, [_FakeSegment(text=" Hello there. How are you?", start=0.0, end=20.0, words=[
        _sw(" Hello", 0.5, 0.9), _sw(" there.", 1.0, 1.6),
        _sw(" How", 10.0, 10.2), _sw(" are", 10.2, 10.4), _sw(" you?", 10.4, 11.0),
    ])])

    assert srt == ("1\n00:00:00,500 --> 00:00:01,600\nHello there.\n"
                   "\n"
                   "2\n00:00:10,000 --> 00:00:11,000\nHow are you?\n")


def _w(text, start, end):
    return (text, start, end)


@pytest.mark.parametrize("words, expected", [
    pytest.param(
        [_w(" Hello", 0.0, 0.4), _w(" there.", 0.5, 1.2),
         _w(" How", 1.3, 1.5), _w(" are", 1.5, 1.7), _w(" you?", 1.7, 2.2)],
        [(0.0, 1.2, "Hello there."), (1.3, 2.2, "How are you?")],
        id="splits_at_sentence_end_once_a_second_has_passed"),
    pytest.param(
        [_w("Hi.", 0.0, 0.3), _w("there", 0.4, 0.8), _w("friend.", 0.9, 1.5)],
        [(0.0, 1.5, "Hi. there friend.")],
        id="does_not_split_at_punctuation_before_one_second"),
    pytest.param(
        [_w("x" * 40, 0.0, 0.5), _w("y" * 40, 0.5, 1.0), _w("z" * 40, 1.0, 1.5)],
        [(0.0, 1.0, "x" * 40 + "\n" + "y" * 40), (1.0, 1.5, "z" * 40)],
        id="splits_when_the_text_could_not_be_wrapped_into_two_42_char_lines"),
    pytest.param(
        [_w("hello", 0.0, 9.0)],
        [(0.0, 7.0, "hello")],
        id="clamps_a_single_word_longer_than_7_seconds"),
    pytest.param(
        [_w(" ", 0.0, 0.1), _w("", 0.1, 0.2)], [],
        id="drops_blank_words_and_empty_cues"),
    pytest.param(
        [_w("Jake", i * 0.3, i * 0.3 + 0.3) for i in range(6)]
        + [_w("help", 2.0, 2.3), _w("me.", 2.4, 3.0)],
        [(0.0, 3.0, "Jake Jake help me.")],
        id="collapses_a_hallucination_loop"),
])
def test_build_cues(words, expected):
    assert sv._build_cues(words) == expected


def test_build_cues_force_splits_before_exceeding_7_seconds():
    words = [_w(f"w{i}", float(i), float(i + 1)) for i in range(10)]  # 1 s each, no punctuation
    cues = sv._build_cues(words)
    assert [(start, end) for start, end, _ in cues] == [(0.0, 7.0), (7.0, 10.0)]
    assert [len(text.split()) for _, _, text in cues] == [7, 3]


def test_build_cues_wraps_onto_two_lines_at_a_word_boundary():
    words = "The quick brown fox jumps over the lazy dog again and again today".split()
    cues = sv._build_cues([_w(w, i * 0.1, i * 0.1 + 0.1) for i, w in enumerate(words)])
    assert len(cues) == 1
    lines = cues[0][2].split("\n")
    assert len(lines) == 2
    assert all(len(line) <= 42 for line in lines)
    assert " ".join(lines) == " ".join(words)


@pytest.mark.parametrize("words, expected", [
    pytest.param(["Jake", "Jake", "Jake", "Jake", "Jake", "help", "me"],
                 ["Jake", "Jake", "help", "me"], id="cuts_a_run_of_four_or_more_to_two"),
    pytest.param(["no", "no", "no", "way"], ["no", "no", "no", "way"],
                 id="leaves_runs_shorter_than_four_alone"),
])
def test_collapse_repeats(words, expected):
    out = sv._collapse_repeats([_w(w, 0.0, 0.1) for w in words])
    assert [w[0] for w in out] == expected


def test_detect_language_returns_info_fields_and_does_not_force_a_language(tmp_path):
    """detect_language must not pass language='en' to transcribe(); forcing it would defeat detection."""
    language, probability = "fr", 0.7
    wav = _fake_wav(tmp_path)
    seen = {}

    def _transcribe(path, **kw):
        seen.update(kw)
        return [], SimpleNamespace(language=language, language_probability=probability)

    with _mocked_asr(wav, SimpleNamespace(transcribe=_transcribe)) as (mock_extract, mock_get_model):
        result = sv.detect_language(Path("/video.mkv"), 1, 150.0, 30.0, "small", 4, tmp_path)

    assert result == (language, probability)
    assert "language" not in seen
    mock_get_model.assert_called_once_with("small", 4)
    mock_extract.assert_called_once_with(
        Path("/video.mkv"), 1, 150.0, 30.0, workdir=tmp_path, should_abort=None)
    assert not wav.exists()


@pytest.mark.parametrize("call", [
    pytest.param(lambda: sv.asr_window(Path("/video.mkv"), 1, 300, 30, "small.en", 4, Path("/work")),
                 id="asr_window"),
    pytest.param(lambda: sv.detect_language(Path("/video.mkv"), 1, 0.0, 30.0, "small", 4, Path("/work")),
                 id="detect_language"),
])
def test_wav_is_cleaned_up_even_when_transcription_raises(tmp_path, call):
    wav = _fake_wav(tmp_path)
    model = SimpleNamespace(transcribe=lambda path, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    with _mocked_asr(wav, model):
        with pytest.raises(RuntimeError):
            call()
    assert not wav.exists()


# --- CLI mode: `python3 -m media_stack.subverify transcribe ...` --------

_SRT_HI = "1\n00:00:00,000 --> 00:00:01,000\nHi.\n"
_KEY = {"JELLYFIN_API_KEY": "testkey"}
_BUSY = [{"Id": "a", "NowPlayingItem": {}}]


def _always_interrupted():
    return Mock(side_effect=sv.ExtractionInterrupted("playback"))


def _interrupted_once_then_ok():
    return Mock(side_effect=[sv.ExtractionInterrupted("playback"), _SRT_HI])


def _run_cli(env, transcribe, sessions=None, times=None, jf_cls=None):
    """Run `_cli_transcribe` with the .env and Jellyfin mocked, plus sleep (when `sessions` scripts a wait) and monotonic (when `times` is given); returns the exit code."""
    jf_cls = MagicMock() if jf_cls is None else jf_cls
    jf_cls.return_value.now_playing_sessions = sessions or Mock(return_value=[])
    with ExitStack() as stack:
        stack.enter_context(patch("media_stack.paths.load_env_file", return_value=env))
        stack.enter_context(patch("media_stack.clients.jellyfin.JellyfinClient", jf_cls))
        stack.enter_context(patch.object(sv, "transcribe_to_srt", transcribe))
        if sessions is not None:
            stack.enter_context(patch.object(sv.time, "sleep"))
        if times is not None:
            stack.enter_context(patch.object(sv.time, "monotonic", side_effect=lambda: next(times)))
        return sv._cli_transcribe(["/video.mkv", "1", "medium.en", "6"])


def test_cli_transcribe_success_prints_srt_returns_0_and_writes_a_zero_wait_report(tmp_path, monkeypatch, capsys):
    """Seconds actually spent waiting (zero here) are written to CHILD_PLAYBACK_WAIT_REPORT_FILENAME in TMPDIR on every exit path."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    rc = _run_cli(_KEY, Mock(return_value=_SRT_HI))

    assert rc == 0
    assert "Hi." in capsys.readouterr().out
    report = tmp_path / sv.CHILD_PLAYBACK_WAIT_REPORT_FILENAME
    assert report.exists()
    assert float(report.read_text()) == 0.0


def test_cli_transcribe_retries_after_extraction_interrupted_then_succeeds():
    transcribe = _interrupted_once_then_ok()
    # First now_playing_sessions call (inside the wait loop) says busy, then clears.
    rc = _run_cli(_KEY, transcribe, Mock(side_effect=[_BUSY, []]))

    assert rc == 0
    assert transcribe.call_count == 2


def test_cli_transcribe_wait_exceeding_max_wait_returns_2_and_still_writes_the_wait_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("SUB_FALLBACK_MAX_WAIT_MIN", raising=False)
    # Jump past any max_wait_min; the repeat() tail tolerates extra monotonic() reads.
    times = itertools.chain([0.0, 0.0], itertools.repeat(10_000.0))
    # A key must be configured, or `_cli_transcribe` refuses (rc 3) before reaching the wait loop.
    rc = _run_cli({**_KEY, "SUB_FALLBACK_MAX_WAIT_MIN": "1"}, _always_interrupted(),
                  Mock(return_value=_BUSY), times)

    assert rc == 2
    assert "playback wait exceeded" in capsys.readouterr().err
    report = tmp_path / sv.CHILD_PLAYBACK_WAIT_REPORT_FILENAME
    assert report.exists()
    assert float(report.read_text()) > 0.0


@pytest.mark.parametrize("bad_value", ["nan", "abc"])
def test_cli_transcribe_rejects_invalid_max_wait_values_and_falls_back(bad_value, capsys):
    """Invalid SUB_FALLBACK_MAX_WAIT_MIN falls back to the 30 min default with a warning (`0` is valid, see below).

    An accepted "nan" would never time out (`elapsed >= nan` is always False); the finite
    `times` iterator turns that into a StopIteration failure rather than a hang.
    """
    rc = _run_cli({**_KEY, "SUB_FALLBACK_MAX_WAIT_MIN": bad_value}, _always_interrupted(),
                  Mock(return_value=_BUSY), iter([0.0, 0.0, 10_000.0]))

    assert rc == 2  # the 30-min default, which 10000s exceeds
    assert "SUB_FALLBACK_MAX_WAIT_MIN" in capsys.readouterr().err


def test_cli_transcribe_accepts_zero_as_no_waiting_allowed(capsys):
    """`0` means no waiting allowed (the parent's allowance is exhausted), not "unset" -> 30 min default."""
    rc = _run_cli({**_KEY, "SUB_FALLBACK_MAX_WAIT_MIN": "0"}, _always_interrupted(),
                  Mock(return_value=_BUSY), iter([0.0, 0.0]))

    assert rc == 2
    err = capsys.readouterr().err
    assert "playback wait exceeded" in err
    # Not the "invalid value" warning: 0 was valid and honored as given.
    assert "must be a finite" not in err
    assert "is not a valid number" not in err


def test_cli_transcribe_generic_failure_returns_1(capsys):
    rc = _run_cli(_KEY, Mock(side_effect=RuntimeError("ffmpeg exploded")))

    assert rc == 1
    assert "ffmpeg exploded" in capsys.readouterr().err


@pytest.mark.parametrize("env", [
    pytest.param({}, id="missing"),
    pytest.param({"JELLYFIN_API_KEY": ""}, id="empty_string"),
])
def test_cli_transcribe_no_jellyfin_key_refuses_and_returns_3(env, capsys):
    """A missing or empty JELLYFIN_API_KEY is never "nobody's playing": exit 3, no JellyfinClient, no extraction."""
    jf_cls, transcribe = MagicMock(), Mock()
    rc = _run_cli(env, transcribe, jf_cls=jf_cls)

    assert rc == 3
    assert "JELLYFIN_API_KEY" in capsys.readouterr().err
    jf_cls.assert_not_called()
    transcribe.assert_not_called()


def test_cli_transcribe_writes_cumulative_wait_report_after_a_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    # Three monotonic() reads: t0, the one elapsed check, the post-loop this_wait.
    rc = _run_cli({**_KEY, "SUB_FALLBACK_MAX_WAIT_MIN": "10"}, _interrupted_once_then_ok(),
                  Mock(side_effect=[[]]), iter([0.0, 25.0, 25.0]))

    assert rc == 0
    report = tmp_path / sv.CHILD_PLAYBACK_WAIT_REPORT_FILENAME
    assert float(report.read_text()) == pytest.approx(25.0)


# --- reading a generated track's cues out of the container (generated -> real upgrade check) ---

def _hexdump(data: bytes) -> str:
    """ffprobe `-show_data` rendering: 16 bytes a line, `%08x: `, hex pairs padded to 40 columns, two spaces, ASCII."""
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        pairs = [chunk[i:i + 2].hex() for i in range(0, len(chunk), 2)]
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{off:08x}: {' '.join(pairs):<40}  {ascii_}")
    return "\n" + "\n".join(lines) + "\n"


def _packet(pts, text, codec_data=None):
    data = text.encode("utf-8") if codec_data is None else codec_data
    return {"codec_type": "subtitle", "stream_index": 2, "pts_time": f"{pts:.6f}",
            "data": _hexdump(data)}


def _ffprobe_ok(packets):
    import json
    return SimpleNamespace(returncode=0, stdout=json.dumps({"packets": packets}), stderr="")


def test_decode_ffprobe_data_round_trips_multiline_and_odd_length_dumps():
    for raw in (b"Hello there, friend. How are you?", b"x", b"", "café ♪ ok".encode()):
        assert sv._decode_ffprobe_data(_hexdump(raw)) == raw


def test_packet_text_strips_markup_and_cuts_an_ass_block_to_its_text_field():
    assert sv._packet_text(b"<i>Hello</i> {\\an8}there\r\nfriend", "subrip") == "Hello there friend"
    ass = b"12,0,Default,,0,0,0,,Line one\\NLine {\\i1}two{\\i0}, with comma"
    assert sv._packet_text(ass, "ass") == "Line one Line two, with comma"


def test_embedded_text_window_uses_an_absolute_end_and_the_stream_index():
    with patch.object(sv.subprocess, "run", return_value=_ffprobe_ok([])) as run:
        assert sv.embedded_text_window(Path("/v.mkv"), 7, "subrip", 600.0, 300.0) == ""
    cmd = run.call_args.args[0]
    assert cmd[cmd.index("-read_intervals") + 1] == "600.000%900.000"
    assert cmd[cmd.index("-select_streams") + 1] == "7"
    assert "-show_packets" in cmd and "-show_data" in cmd
    assert run.call_args.kwargs["timeout"] == sv.UPGRADE_FFPROBE_TIMEOUT_S


def test_embedded_text_window_keeps_only_packets_inside_the_window():
    """ffprobe also returns packets before the window start; they must not leak into the text."""
    packets = [_packet(590.0, "before the window"),
               _packet(600.0, "at the start"),
               _packet(750.0, "in the middle"),
               _packet(899.9, "just inside"),
               _packet(900.0, "at the end, which belongs to the next window"),
               _packet(901.0, "after the window")]
    with patch.object(sv.subprocess, "run", return_value=_ffprobe_ok(packets)):
        text = sv.embedded_text_window(Path("/v.mkv"), 2, "subrip", 600.0, 300.0)
    assert text == "at the start in the middle just inside"


@pytest.mark.parametrize("result", [
    SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    subprocess.TimeoutExpired("ffprobe", 60),
])
def test_embedded_text_window_is_none_when_ffprobe_cannot_be_read(result):
    kwargs = ({"side_effect": result} if isinstance(result, BaseException)
              else {"return_value": result})
    with patch.object(sv.subprocess, "run", **kwargs):
        assert sv.embedded_text_window(Path("/v.mkv"), 2, "subrip", 0.0, 10.0) is None


def test_embedded_text_window_does_not_run_ffprobe_for_a_codec_it_cannot_decode():
    with patch.object(sv.subprocess, "run") as run:
        assert sv.embedded_text_window(Path("/v.mkv"), 2, "hdmv_pgs_subtitle", 0.0, 10.0) is None
    run.assert_not_called()


def test_upgrade_windows_are_the_middle_of_each_third_up_to_300_s():
    assert sv.upgrade_windows(2400.0) == [(250.0, 300.0), (1050.0, 300.0), (1850.0, 300.0)]
    # a third of exactly 300 s: the whole thirds
    assert sv.upgrade_windows(900.0) == [(0.0, 300.0), (300.0, 300.0), (600.0, 300.0)]
    # a short video: the windows ARE the three thirds
    short = sv.upgrade_windows(90.0)
    assert short == [(0.0, 30.0), (30.0, 30.0), (60.0, 30.0)]


@pytest.mark.parametrize("duration", [90.0, 2400.0, 7413.7])
def test_upgrade_windows_are_pairwise_disjoint_and_inside_their_own_third(duration):
    windows = sv.upgrade_windows(duration)
    assert len(windows) == 3
    third = duration / 3
    for i, (start, dur) in enumerate(windows):
        assert 0 < dur <= sv.UPGRADE_WINDOW_S
        assert third * i - 1e-9 <= start and start + dur <= third * (i + 1) + 1e-9
        # centred in its third
        assert abs((start - third * i) - (third * (i + 1) - (start + dur))) < 1e-6
    for (s1, d1), (s2, _d2) in zip(windows, windows[1:]):
        assert s1 + d1 <= s2 + 1e-9


@pytest.mark.parametrize("dur", [0, None])
def test_upgrade_windows_are_empty_without_a_usable_duration(dur):
    assert sv.upgrade_windows(dur) == []


def test_generated_track_windows_reads_every_window_of_the_one_stream():
    seen = []

    def _fake(video, index, codec, start, dur, timeout_s=0):
        seen.append((index, codec, start, dur))
        return f"text at {start:.0f}"

    with patch.object(sv, "embedded_text_window", side_effect=_fake):
        out = sv.generated_track_windows(Path("/v.mkv"), {"index": 4, "codec_name": "subrip"}, 2400.0)
    assert out == [(250.0, 300.0, "text at 250"), (1050.0, 300.0, "text at 1050"),
                   (1850.0, 300.0, "text at 1850")]
    assert seen == [(4, "subrip", 250.0, 300.0), (4, "subrip", 1050.0, 300.0),
                    (4, "subrip", 1850.0, 300.0)]


def test_generated_track_windows_is_none_if_any_window_cannot_be_read():
    """A partial read is not evidence: two good windows and one failure is None."""
    texts = iter(["a", None, "c"])
    with patch.object(sv, "embedded_text_window", side_effect=lambda *a, **k: next(texts)):
        assert sv.generated_track_windows(Path("/v.mkv"), {"index": 2, "codec_name": "subrip"},
                                          2400.0) is None


# --- judge_sidecar_against_windows: real overlap math ---

# 150 made-up four-letter words: unrelated episodes rarely share a word pair but share most words.
_VOCAB = [a + b + c + d for a in "bdfgk" for b in "aeiou" for c in "lmnprst" for d in "aeiou"][:150]


def _dialogue(seed, n=40):
    """Deterministic pseudo-dialogue: different seeds share words but not word pairs."""
    import random
    rng = random.Random(seed)
    return [" ".join(rng.choice(_VOCAB) for _ in range(8)) for _ in range(n)]


def _srt(lines, start_s, step=3.0):
    out = []
    for i, line in enumerate(lines, 1):
        t = start_s + i * step
        out.append(f"{i}\n{sv._srt_timestamp(t)} --> {sv._srt_timestamp(t + 2)}\n{line}\n")
    return "\n".join(out)


def _window_for(seed, start_s, n=40):
    lines = _dialogue(seed, n)
    return (start_s, 300.0, " ".join(lines)), _srt(lines, start_s)


def _verdict(windows, srt_text):
    """`(ok, reason)` from `judge_sidecar_against_windows`, without the evidence counts."""
    ok, why, _usable, _evidence = sv.judge_sidecar_against_windows(windows, srt_text)
    return ok, why


def _matching_windows():
    """Three windows plus the sidecar cues that say the same thing."""
    wins, cues = zip(*(_window_for(s, st) for s, st in ((1, 600.0), (2, 1200.0), (3, 1800.0))))
    return list(wins), "\n".join(cues)


def test_score_accepts_a_sidecar_that_says_what_the_generated_track_says():
    wins, cues = _matching_windows()
    ok, why = _verdict(wins, cues)
    assert ok, why
    assert "3 of 3" in why


def test_score_accepts_two_good_windows_and_one_bad_one():
    wins, cues = _matching_windows()
    wins[1] = (1200.0, 300.0, " ".join(_dialogue(99)))  # generated text disagrees here
    ok, why = _verdict(wins, cues)
    assert ok, why
    assert "2 of 3" in why


def test_score_rejects_when_only_one_window_matches():
    wins, cues = _matching_windows()
    wins[1:] = [(1200.0, 300.0, " ".join(_dialogue(98))), (1800.0, 300.0, " ".join(_dialogue(99)))]
    ok, why = _verdict(wins, cues)
    assert not ok
    assert "1 of 3" in why


def test_score_does_not_count_windows_with_too_little_generated_text():
    """Matching cues over a handful of words prove nothing: two windows need MIN_ASR_WORDS distinct words each."""
    few = "garden kitchen mother"
    wins = [(600.0, 300.0, few), (1200.0, 300.0, few), (1800.0, 300.0, few)]
    cues = "\n".join(_srt([few], st - 2) for st in (600.0, 1200.0, 1800.0))
    ok, why = _verdict(wins, cues)
    assert not ok
    assert "0 of 0 usable" in why


# --- the sidecar side is disjoint too ---

def _one_cue(start_s, lines):
    """One SRT cue at `start_s` holding all of `lines`."""
    return (f"1\n{sv._srt_timestamp(start_s)} --> {sv._srt_timestamp(start_s + 1.5)}\n"
            + "\n".join(lines) + "\n")


def test_sidecar_ranges_split_a_modest_gap_in_half():
    windows = [(0.0, 30.0, ""), (70.0, 30.0, "")]  # a 40 s gap: 20 s each side
    assert sv.sidecar_ranges(windows) == [(-30.0, 50.0), (50.0, 130.0)]
    windows = [(0.0, 30.0, ""), (100.0, 30.0, "")]  # a 70 s gap: the full 30 s
    assert sv.sidecar_ranges(windows) == [(-30.0, 60.0), (70.0, 160.0)]


def test_one_sidecar_cue_near_a_boundary_cannot_back_two_windows():
    """One long sidecar cue at 32 s of a 100 s video backs only the window it starts in, not both ('2 of 2')."""
    lines = _dialogue(11, 8)
    first, second = " ".join(lines[:4]), " ".join(lines[4:])
    cue = _one_cue(32.0, lines)
    windows = [(s, d, t) for (s, d), t in zip(sv.upgrade_windows(100.0), (first, second, ""))]
    # overlap() alone (sub-fallback's single-window view) matches both
    for (s, d, t) in windows[:2]:
        w, b, _n = sv.overlap(t, cue, s, d)
        assert w >= sv.MIN_WORD_RATIO and b >= sv.MIN_BIGRAM_RATIO
    ok, why = _verdict(windows, cue)
    assert not ok and "1 of 2 usable" in why


def test_a_long_video_with_large_gaps_still_tolerates_the_full_30_s_of_drift():
    """Windows 500 s apart: a sidecar 29 s early still matches; 50 s early does not."""
    windows_plan = sv.upgrade_windows(2400.0)
    lines = _dialogue(13, 16)

    def _score(shift):
        wins = [(s, d, " ".join(lines) if i == 0 else "") for i, (s, d) in enumerate(windows_plan)]
        wins[1] = (wins[1][0], wins[1][1], " ".join(_dialogue(14, 16)))
        # The sidecar has the passages `shift` s earlier than the windows.
        cues = _srt(lines, windows_plan[0][0] + shift, step=1.0) + "\n\n" + \
            _srt(_dialogue(14, 16), windows_plan[1][0] + shift, step=1.0)
        return _verdict(wins, cues)

    assert _score(-29.0)[0], _score(-29.0)[1]
    assert not _score(-50.0)[0]


def test_score_refuses_when_there_are_no_windows():
    ok, why = _verdict([], "1\n00:00:01,000 --> 00:00:02,000\nhello\n")
    assert not ok and "no verification windows" in why


def _three_windows():
    """Three generated windows 800 s apart (full padding)."""
    wins, cues = zip(*(_window_for(10 + n, 250.0 + 800.0 * n) for n in range(3)))
    return list(wins), list(cues)


def test_judge_counts_evidence_only_where_both_sides_have_text():
    """`usable` = enough generated text; `evidence` = the sidecar's cues in that window also have enough distinct words."""
    wins, cues = _three_windows()
    other = [_window_for(90 + n, 250.0 + 800.0 * n)[1] for n in range(3)]   # unrelated episode

    ok, _why, usable, evidence = sv.judge_sidecar_against_windows(wins, "\n\n".join(cues))
    assert (ok, usable, evidence) == (True, 3, 3)

    # a correct but partial sidecar: only the first window has cues
    ok, why, usable, evidence = sv.judge_sidecar_against_windows(wins, cues[0])
    assert (ok, usable, evidence) == (False, 3, 1)
    assert "1 of 3 usable" in why and "sidecar 0 words" in why

    # cues in two windows, none in the third
    ok, _why, usable, evidence = sv.judge_sidecar_against_windows(wins, "\n\n".join(other[:2]))
    assert (ok, usable, evidence) == (False, 3, 2)

    # an unrelated sidecar with cues everywhere: all three windows are evidence
    ok, _why, usable, evidence = sv.judge_sidecar_against_windows(wins, "\n\n".join(other))
    assert (ok, usable, evidence) == (False, 3, 3)


def test_upgrade_thresholds_are_the_ones_sub_fallback_uses():
    import importlib.util
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("sub_fallback_thresholds", str(root / "sub-fallback.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # the Config dataclass resolves annotations through sys.modules
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    assert (mod.MIN_WORD_RATIO, mod.MIN_BIGRAM_RATIO, mod.MIN_ASR_WORDS) == (
        sv.MIN_WORD_RATIO, sv.MIN_BIGRAM_RATIO, sv.MIN_ASR_WORDS) == (0.60, 0.40, 15)
