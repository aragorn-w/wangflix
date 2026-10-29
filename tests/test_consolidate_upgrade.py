"""consolidate-subs.py: replacing a GENERATED English subtitle ("English (generated)" track) with a real one.

A real, non-forced English sidecar proven to be the episode's subtitle makes the full pass run despite the
CONSOLIDATED_SUBS tag and the state cache. Driven through the real `_process_locked`; faked: ffprobe, the
generated track's cue windows, mkvmerge, ffsubsync, tag writers and the normalize subprocess. Real: sidecar
discovery, `sublang`, the `subverify.overlap` math and window plan, ranking, the state file (in `tmp_path`).
"""
import copy
import importlib.util
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "consolidate_subs_upgrade", str(PROJECT_ROOT / "consolidate-subs.py")
)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)

from media_stack import subverify as sv  # noqa: E402

DURATION = 2400.0
# The middle 300 s of each third of DURATION (800 s thirds): disjoint windows.
WINDOW_STARTS = (250.0, 1050.0, 1850.0)

# 150 made-up words: unrelated episodes share most words but almost never a word PAIR (the bigram leg).
_VOCAB = [a + b + c + d for a in "bdfgk" for b in "aeiou" for c in "lmnprst" for d in "aeiou"][:150]


def _dialogue(seed, n=40):
    """`n` lines of English-looking filler (function words, so `sublang` says English) built from `seed`."""
    rng = random.Random(seed)
    w = lambda: rng.choice(_VOCAB)  # noqa: E731
    return [f"You know the {w()} and {w()} of that {w()} to {w()}" for _ in range(n)]


def _srt_stamp(t):
    return f"{int(t // 3600):02d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d},000"


def _srt(lines_by_start):
    """A full SRT: for each window start, its lines at 3 s spacing."""
    cues = []
    n = 0
    for start, lines in lines_by_start:
        for i, line in enumerate(lines, 1):
            n += 1
            t = start + i * 3.0
            cues.append(f"{n}\n{_srt_stamp(t)} --> {_srt_stamp(t + 2)}\n{line}\n")
    return "\n".join(cues)


def _episode(seed):
    """(sidecar SRT text, {window start: generated ASR text}) for one episode."""
    per_window = {start: _dialogue(seed * 10 + i) for i, start in enumerate(WINDOW_STARTS)}
    srt_text = _srt(list(per_window.items()))
    asr = {start: " ".join(lines) for start, lines in per_window.items()}
    return srt_text, asr


def _info(*, tagged=True, normalized=True, subs=(("English (generated)", "subrip"),)):
    fmt_tags = {}
    if tagged:
        fmt_tags["CONSOLIDATED_SUBS"] = f"v{cs.PIPELINE_VERSION}"
    if normalized:
        fmt_tags["NORMALIZED_AUDIO"] = "v1"
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"},
               {"index": 1, "codec_type": "audio", "codec_name": "aac",
                "tags": {"language": "eng"}, "disposition": {"default": 1}}]
    for i, (title, codec) in enumerate(subs, start=2):
        tags = {"language": "eng"}
        if title:
            tags["title"] = title
        streams.append({"index": i, "codec_type": "subtitle", "codec_name": codec,
                        "tags": tags})
    return {"streams": streams, "format": {"duration": str(DURATION), "tags": fmt_tags}}


class Harness:
    """One consolidated `Movie (2024).mkv` in `tmp_path`, every external tool faked and every call recorded."""

    def __init__(self, tmp_path, monkeypatch, *, sidecars=(), asr=None, info=None,
                 window_failure=False, state_cache=False, stem="Movie (2024)",
                 handoff=False):
        monkeypatch.setattr(cs, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(cs, "NEEDS_ENGLISH_STATE_FILE", tmp_path / "needs-english.json")
        monkeypatch.setattr(cs, "LOG_FILE", tmp_path / "consolidate.log")
        # the late-sidecar sweep may quarantine on these skip paths; send its files to tmp_path
        monkeypatch.setattr(cs, "VAR_QUARANTINE_SUBTITLES", tmp_path / "quarantine" / "subtitles")
        monkeypatch.setattr(cs, "QUARANTINE_LOG", tmp_path / "sub-quarantine.jsonl")
        self._monkeypatch = monkeypatch
        self._real_reasons = cs._upgrade_rejection_reasons
        # By default the sweep neither acts on the upgrade check's rejections (`handoff=True` tests
        # that) nor samples an embedded track's text, so these tests exercise the upgrade check itself.
        if not handoff:
            monkeypatch.setattr(cs, "_upgrade_rejection_reasons", lambda *a, **k: {})
        monkeypatch.setattr(cs, "_embedded_track_is_english", lambda *a, **k: False)
        self.tmp_path = tmp_path
        self.stem = stem
        self.media = tmp_path / f"{stem}.mkv"
        self.media.write_bytes(b"\x1a\x45\xdf\xa3 original")
        for suffix, text in sidecars:
            (tmp_path / f"{stem}{suffix}").write_text(text)
        self.asr = asr if asr is not None else {}
        self.info = info if info is not None else _info()
        self.extract_ok = True  # does extracting an embedded track work
        self.extractions = []   # indices of the embedded tracks the pass tried to extract
        self.window_failure = window_failure
        self.out_edit = None    # edits the remuxed file's probed streams (to fake a bad mux)
        self.out_duration = DURATION  # the remuxed file's probed container duration
        self.last_keep = []     # source subtitle indices the remux was told to keep
        self.on_sync = None     # called inside ffsubsync (before the remux)
        self.on_verify = None   # called when the remux output is probed (the verify)
        self.on_window = None   # called on every window read (make it single-shot)
        self.on_remux = None    # called inside mkvmerge, i.e. after the pass read its inputs
        self.probes = []
        self.window_reads = []
        self.remuxes = []
        self.normalize_calls = []
        self.sync_inputs = []
        if state_cache:
            cur = cs.file_key(self.media)
            cs.update_state_entry(cs.STATE_FILE, str(self.media.resolve()),
                                  {"size": cur[0], "mtime": cur[1], "v": cs.PIPELINE_VERSION,
                                   "status": "fixed"})

    # --- fakes ---------------------------------------------------------------
    def _out_info(self):
        """What ffprobe reports for a faithful upgrade remux: source video and audio, kept subtitles, new English one."""
        streams = [copy.deepcopy(s) for s in self.info["streams"]
                   if s["codec_type"] in ("video", "audio")]
        by_index = {s["index"]: s for s in self.info["streams"]}
        for idx in self.last_keep:  # kept source subtitle tracks come first, as they were
            streams.append(copy.deepcopy(by_index[idx]))
        streams.append({"index": 90, "codec_type": "subtitle", "codec_name": "subrip",
                        "tags": {"language": "eng", "title": "English"},
                        "disposition": {"default": 1, "forced": 0}})
        if self.out_edit:
            self.out_edit(streams)
        return {"streams": streams, "format": {"duration": str(self.out_duration), "tags": {}}}

    def _probe(self, p):
        p = Path(p)
        self.probes.append(p)
        if p.name.endswith(".tmp.mkv"):  # the remux output, probed before the swap
            if self.on_verify:
                self.on_verify()
            return self._out_info()
        return self.info

    def _window(self, video, index, codec, start, dur, **kw):
        self.window_reads.append((index, start, dur))
        if self.on_window:
            self.on_window()
        if self.window_failure:
            return None
        if callable(self.asr):  # asr(start, dur): the cues inside [start, start + dur)
            return self.asr(start, dur)
        return self.asr.get(start, "")

    def _remux(self, src, dst, video_idx, audio_idx, eng_srt, forced_srt,
               keep_subtitle_indices=None, cleanup_flags=None, eng_track_name="English"):
        self.last_keep = list(keep_subtitle_indices or [])
        self.remuxes.append({"eng_track_name": eng_track_name, "audio": list(audio_idx),
                             "video": list(video_idx), "content": Path(eng_srt).read_text(),
                             "forced": forced_srt, "keep": list(keep_subtitle_indices or []),
                             "flags": copy.deepcopy(cleanup_flags)})
        if self.on_remux:
            self.on_remux()
        Path(dst).write_bytes(b"MUXED")
        return True, "ok"

    def _ffsubsync(self, video, srt_in, srt_out):
        self.sync_inputs.append(Path(srt_in).read_text())
        if self.on_sync:
            self.on_sync()
        return False

    def run(self, dry_run=False):
        def _extract(src, idx, codec, dst):
            self.extractions.append(idx)
            Path(dst).write_text(_srt([(0.0, ["You know the generated and embedded cue of that track to hear"])]))
            return self.extract_ok

        with patch.object(cs, "probe", side_effect=self._probe), \
             patch.object(sv, "embedded_text_window", side_effect=self._window), \
             patch.object(cs, "extract_track", side_effect=_extract), \
             patch.object(cs, "run_ffsubsync", side_effect=self._ffsubsync), \
             patch.object(cs, "original_language_for", return_value=None) as orig_lang, \
             patch.object(cs, "get_audio_lang_pref",
                          return_value=({"eng"}, "eng", False)) as audio_pref, \
             patch.object(cs, "select_keep_audio", return_value=[1]) as select_audio, \
             patch.object(cs, "remux", side_effect=self._remux), \
             patch.object(cs, "set_consolidated_tag", return_value=True) as set_tag, \
             patch.object(cs, "_normalize_audio_inline",
                          side_effect=lambda *a, **k: self.normalize_calls.append(a)):
            self.result = cs._process_locked(self.media, str(self.media), dry_run=dry_run)
        self.audio_policy_calls = (orig_lang.call_count, audio_pref.call_count,
                                   select_audio.call_count)
        self.set_tag = set_tag
        return self.result

    def enable_handoff(self):
        """From now on the sweep acts on the upgrade check's rejections."""
        self._monkeypatch.setattr(cs, "_upgrade_rejection_reasons", self._real_reasons)

    def quarantined(self):
        d = self.tmp_path / "quarantine" / "subtitles"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def sidecars_left(self):
        return sorted(p.name for p in self.tmp_path.iterdir()
                      if p.suffix in (".srt", ".ass", ".vtt"))

    def state(self):
        return cs.load_state(cs.STATE_FILE).get(str(self.media.resolve()))


def _upgrade_ready(tmp_path, monkeypatch, *, seed=1, sidecar=".en.srt", **kw):
    """Harness whose sidecar is the right episode for the generated track."""
    srt_text, asr = _episode(seed)
    return Harness(tmp_path, monkeypatch, sidecars=[(sidecar, srt_text)], asr=asr, **kw)


# --- the upgrade itself -----------------------------------------------------------

@pytest.mark.parametrize("sidecar, state_cache", [(".en.srt", False), (".en.srt", True), (".en.sdh.srt", False)],
                         ids=["tagged", "tagged-and-state-cached", "real-sdh-sidecar"])
def test_verified_real_sidecar_replaces_the_generated_track(tmp_path, monkeypatch, capsys, sidecar, state_cache):
    """Real beats generated outright (hearing-impaired or not), through both the tag and the state cache that
    would normally skip the file."""
    h = _upgrade_ready(tmp_path, monkeypatch, sidecar=sidecar, state_cache=state_cache)
    assert (h.state() is not None) is state_cache
    sidecar_text = (tmp_path / f"Movie (2024){sidecar}").read_text()
    result = h.run()

    assert result["status"] == "FIXED"
    assert result["detail"].startswith("UPGRADE generated->real ")
    assert len(h.remuxes) == 1
    assert h.remuxes[0]["eng_track_name"] == "English"          # not "(generated)"
    assert "You know the" in h.remuxes[0]["content"]
    assert h.remuxes[0]["content"].count("-->") == sidecar_text.count("-->")
    assert h.sync_inputs and h.sync_inputs[0].count("-->") == sidecar_text.count("-->")
    assert h.sidecars_left() == []                              # sidecar consumed as usual
    assert f"UPGRADE: generated -> real subtitle (Movie (2024){sidecar})" in capsys.readouterr().out
    state = h.state()
    assert state["status"] == "fixed" and state["upgraded"] is True
    assert "upgrade_rejected" not in state
    h.set_tag.assert_called_once()                              # re-tagged CONSOLIDATED_SUBS


def _stream(index, kind, *, default=0, forced=0, codec=None, **extra):
    return {"index": index, "codec_type": kind,
            "codec_name": codec or {"video": "h264", "audio": "ac3"}[kind],
            "tags": {"language": "eng"} if kind == "audio" else {},
            "disposition": {"default": default, "forced": forced, **extra}}


def _with_av(*av):
    """The default consolidated `_info()` with its video/audio replaced."""
    info = _info()
    subs = [s for s in info["streams"] if s["codec_type"] == "subtitle"]
    info["streams"] = [*av, *subs]
    return info


def test_audio_is_left_exactly_as_consolidated(tmp_path, monkeypatch):
    """An upgrade changes the subtitle only: no audio policy or Arr lookup, every audio stream kept in order."""
    info = _with_av(_stream(0, "video", default=1),
                    _stream(1, "audio", default=0),
                    _stream(5, "audio", default=1))
    h = _upgrade_ready(tmp_path, monkeypatch, info=info)
    assert h.run()["status"] == "FIXED"
    assert h.audio_policy_calls == (0, 0, 0)
    assert h.remuxes[0]["audio"] == [1, 5]
    assert h.remuxes[0]["video"] == [0]
    assert h.remuxes[0]["flags"][1]["default"] is False
    assert h.remuxes[0]["flags"][5]["default"] is True


def test_every_video_stream_survives_an_upgrade(tmp_path, monkeypatch):
    """No single-default reduction or cover-art strip: a second video stream and an attached picture stay."""
    info = _with_av(_stream(0, "video", default=1),
                    _stream(3, "video", default=0),
                    _stream(4, "video", default=0, codec="mjpeg", attached_pic=1),
                    _stream(1, "audio", default=1))
    h = _upgrade_ready(tmp_path, monkeypatch, info=info)
    result = h.run()
    assert result["status"] == "FIXED"
    assert h.remuxes[0]["video"] == [0, 3, 4]
    assert "dropped_video=0" in result["detail"]


def test_video_and_audio_flags_are_the_exact_source_flags(tmp_path, monkeypatch):
    """Explicit default AND forced per track from the probed dispositions, even with no audio default."""
    info = _with_av(_stream(0, "video", default=0),
                    _stream(3, "video", default=1),
                    _stream(1, "audio", default=0),
                    _stream(2, "audio", default=0, forced=1),
                    _stream(4, "audio", default=0))
    h = _upgrade_ready(tmp_path, monkeypatch, info=info)
    assert h.run()["status"] == "FIXED"
    assert h.remuxes[0]["flags"] == {
        0: {"default": False, "forced": False},
        3: {"default": True, "forced": False},
        1: {"default": False, "forced": False},
        2: {"default": False, "forced": True},
        4: {"default": False, "forced": False},
    }


MEDIA_BYTES = b"\x1a\x45\xdf\xa3 original"


@pytest.mark.parametrize("what", ["drops-a-video-stream", "drops-an-audio-stream",
                                  "moves-the-audio-default", "loses-a-forced-flag",
                                  "drops-the-subtitle", "english-not-default"])
def test_a_remux_that_is_not_the_source_plus_the_new_subtitle_fails_and_leaves_the_original(
        tmp_path, monkeypatch, what):
    info = _with_av(_stream(0, "video", default=1),
                    _stream(3, "video", default=0),
                    _stream(1, "audio", default=1),
                    _stream(2, "audio", default=0, forced=1))

    def _edit(streams):
        video = [s for s in streams if s["codec_type"] == "video"]
        audio = [s for s in streams if s["codec_type"] == "audio"]
        subs = [s for s in streams if s["codec_type"] == "subtitle"]
        if what == "drops-a-video-stream":
            streams.remove(video[1])
        elif what == "drops-an-audio-stream":
            streams.remove(audio[1])
        elif what == "moves-the-audio-default":
            audio[0]["disposition"]["default"], audio[1]["disposition"]["default"] = 0, 1
        elif what == "loses-a-forced-flag":
            audio[1]["disposition"]["forced"] = 0
        elif what == "drops-the-subtitle":
            streams.remove(subs[0])
        elif what == "english-not-default":
            subs[0]["disposition"]["default"] = 0

    h = _upgrade_ready(tmp_path, monkeypatch, info=info)
    h.out_edit = _edit
    result = h.run()

    assert result["status"] == "FAIL" and result["detail"].startswith("upgrade verify: ")
    assert h.media.read_bytes() == MEDIA_BYTES                  # original untouched
    assert not list(tmp_path.glob("*.tmp.mkv"))                 # tmp output removed
    assert h.sidecars_left() == ["Movie (2024).en.srt"]         # nothing consumed
    h.set_tag.assert_not_called()
    assert h.normalize_calls == []


def test_an_upgrade_whose_remux_is_shorter_than_the_source_fails_and_leaves_the_original(
        tmp_path, monkeypatch):
    """Right streams and flags but a truncated mux (2400 s in, 400 s out): the duration guard refuses the swap."""
    h = _upgrade_ready(tmp_path, monkeypatch)
    h.out_duration = 400.0
    result = h.run()

    assert result["status"] == "FAIL" and result["detail"].startswith("duration verify: ")
    assert h.media.read_bytes() == MEDIA_BYTES                  # original untouched
    assert not list(tmp_path.glob("*.tmp.mkv"))                 # tmp output removed
    assert h.sidecars_left() == ["Movie (2024).en.srt"]         # nothing consumed
    h.set_tag.assert_not_called()
    assert h.normalize_calls == []


@pytest.mark.parametrize("out_duration", [DURATION, DURATION - 20.0, DURATION + 5.0],
                         ids=["equal", "within-one-percent", "longer"])
def test_an_upgrade_whose_remux_is_as_long_as_the_source_swaps(tmp_path, monkeypatch, out_duration):
    h = _upgrade_ready(tmp_path, monkeypatch)
    h.out_duration = out_duration
    assert h.run()["status"] == "FIXED"
    assert h.media.read_bytes() == b"MUXED"


def test_only_the_verified_sidecar_is_a_candidate(tmp_path, monkeypatch):
    """A longer wrong-episode sidecar would out-score the right one; it is not verified, so not a candidate."""
    right, asr = _episode(1)
    wrong, _ = _episode(2)
    wrong_longer = wrong + "\n" + _srt([(2300.0, _dialogue(77, n=100))])
    h = Harness(tmp_path, monkeypatch, asr=asr,
                sidecars=[(".en.srt", right), (".eng.srt", wrong_longer)])
    assert h.run()["status"] == "FIXED"
    assert h.remuxes[0]["content"].count("-->") == right.count("-->")
    assert h.sidecars_left() == ["Movie (2024).eng.srt"]  # not consumed, so not deleted


# --- loudness normalization ---------------------------------------------------------

@pytest.mark.parametrize("normalized", [True, False])
def test_an_upgrade_never_runs_loudness_normalization(tmp_path, monkeypatch, normalized):
    """Whatever NORMALIZED_AUDIO says, normalization is the regular pass's job: only the tag is written."""
    h = _upgrade_ready(tmp_path, monkeypatch, info=_info(normalized=normalized))
    result = h.run()
    assert result["status"] == "FIXED"
    assert h.normalize_calls == []
    h.set_tag.assert_called_once()
    assert result["detail"].startswith("UPGRADE generated->real ")
    # the gate's probe of the file, the verify's probe of the remux output, then the duration guard's
    # probes of the file and the output right before the swap
    assert [p.name.endswith(".tmp.mkv") for p in h.probes] == [False, True, False, True]
    assert not hasattr(cs, "_ensure_normalized_tag") and not hasattr(cs, "set_normalized_tag")


# --- verification windows are disjoint parts of the video -----------------------------------------------

SHORT = 100.0  # seconds: thirds of 33.3 s


def _short_info():
    info = _info()
    info["format"]["duration"] = str(SHORT)
    return info


def _cues_at(*segments):
    """[(start, lines)] -> (list of (time, line), SRT text). One cue a second."""
    timed = []
    for start, lines in segments:
        timed += [(start + i, line) for i, line in enumerate(lines)]
    srt_text = "\n".join(
        f"{n}\n{_srt_stamp(t)} --> {_srt_stamp(t + 0.9)}\n{line}\n"
        for n, (t, line) in enumerate(timed, 1))
    return timed, srt_text


def _asr_from(timed):
    """What `embedded_text_window` would return: the cues inside [start, start+dur)."""
    return lambda start, dur: " ".join(line for t, line in timed if start <= t < start + dur)


@pytest.mark.parametrize("handoff", [False, True])
def test_one_shared_passage_in_a_short_video_does_not_count_three_times(tmp_path, monkeypatch, capsys, handoff):
    """A 100 s video whose only common passage is at 80-95 s: disjoint thirds put it in one window, not three.
    One usable window of the two needed is no verdict either, so the sweep keeps the sidecar."""
    timed, srt_text = _cues_at((80.0, _dialogue(3, 15)))
    h = Harness(tmp_path, monkeypatch, info=_short_info(), asr=_asr_from(timed), handoff=handoff,
                sidecars=[(".en.srt", srt_text + "\n" + _srt([(5.0, _dialogue(4, 25))]))])
    result = h.run()
    assert result["detail"] == "tag present" and h.remuxes == []
    assert h.sidecars_left() == ["Movie (2024).en.srt"] and h.quarantined() == []
    assert [round(s, 1) for _i, s, _d in h.window_reads] == [0.0, 33.3, 66.7]
    out = capsys.readouterr().out
    assert "UPGRADE-SKIP" in out and "1 of 1 usable" in out


def test_a_genuine_match_in_two_different_thirds_upgrades(tmp_path, monkeypatch):
    timed, srt_text = _cues_at((10.0, _dialogue(5, 16)), (80.0, _dialogue(6, 15)))
    h = Harness(tmp_path, monkeypatch, info=_short_info(), asr=_asr_from(timed),
                sidecars=[(".en.srt", srt_text)])
    assert h.run()["status"] == "FIXED"


def test_a_video_too_short_for_two_usable_windows_does_not_upgrade(tmp_path, monkeypatch):
    """30 s: thirds of 10 s never hold the 15 distinct words a window needs."""
    info = _info()
    info["format"]["duration"] = "30"
    timed, srt_text = _cues_at((1.0, _dialogue(7, 3)), (11.0, _dialogue(8, 3)), (21.0, _dialogue(9, 3)))
    h = Harness(tmp_path, monkeypatch, info=info, asr=_asr_from(timed),
                sidecars=[(".en.srt", srt_text + "\n" + _srt([(40.0, _dialogue(2, 12))]))])
    assert h.run()["detail"] == "tag present" and h.remuxes == []


def test_one_sidecar_cue_near_a_boundary_does_not_back_two_windows(tmp_path, monkeypatch, capsys):
    """A long sidecar cue at 32 s, whose halves are the generated first and second thirds, backs only its own window."""
    lines = _dialogue(21, 8)
    halves = (" ".join(lines[:4]), " ".join(lines[4:]))
    cue = f"1\n{_srt_stamp(32.0)} --> {_srt_stamp(34.0)}\n" + "\n".join(lines) + "\n"
    asr = lambda start, dur: halves[0] if start < 33.0 else (halves[1] if start < 66.0 else "")  # noqa: E731
    h = Harness(tmp_path, monkeypatch, info=_short_info(), asr=asr, sidecars=[(".en.srt", cue)])
    result = h.run()
    assert result["detail"] == "tag present" and h.remuxes == []
    assert h.sidecars_left() == ["Movie (2024).en.srt"]
    assert "1 of 2 usable" in capsys.readouterr().out


def test_two_cues_that_each_back_their_own_window_still_upgrade(tmp_path, monkeypatch):
    lines = _dialogue(21, 8)
    halves = (" ".join(lines[:4]), " ".join(lines[4:]))
    srt_text = (f"1\n{_srt_stamp(32.0)} --> {_srt_stamp(33.0)}\n" + "\n".join(lines[:4]) + "\n\n"
                f"2\n{_srt_stamp(34.0)} --> {_srt_stamp(36.0)}\n" + "\n".join(lines[4:]) + "\n")
    asr = lambda start, dur: halves[0] if start < 33.0 else (halves[1] if start < 66.0 else "")  # noqa: E731
    h = Harness(tmp_path, monkeypatch, info=_short_info(), asr=asr, sidecars=[(".en.srt", srt_text)])
    assert h.run()["status"] == "FIXED"


# --- sidecar suffixes are compared case-insensitively, like discovery -------------------------------------

@pytest.mark.parametrize("suffix", [".en.ASS", ".en.Ass", ".en.SSA"])
def test_an_uppercase_ass_or_ssa_sidecar_is_converted_and_can_upgrade(tmp_path, monkeypatch, suffix):
    right, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(suffix, "[Script Info]")], asr=asr)
    with patch.object(cs, "ass_to_srt", side_effect=_good_ass_to_srt(right)) as conv:
        result = h.run()
    assert conv.call_count == 1           # converted once, at the check; the full pass uses those bytes
    assert result["status"] == "FIXED" and h.remuxes[0]["eng_track_name"] == "English"
    assert h.remuxes[0]["content"].count("-->") == right.count("-->")
    assert h.sidecars_left() == []        # the consumed .ASS is gone


def test_an_uppercase_vtt_sidecar_is_converted_with_ffmpeg_and_can_upgrade(tmp_path, monkeypatch):
    right, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.VTT", "WEBVTT")], asr=asr)
    calls = []

    def _ffmpeg(cmd, **kw):
        calls.append(cmd)
        Path(cmd[-1]).write_text(right)
        return type("R", (), {"returncode": 0})()

    with patch.object(cs.subprocess, "run", side_effect=_ffmpeg):
        result = h.run()
    assert calls and calls[0][0] == "ffmpeg"
    assert result["status"] == "FIXED"


def test_an_uppercase_srt_sidecar_needs_no_conversion(tmp_path, monkeypatch):
    right, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.SRT", right)], asr=asr)
    with patch.object(cs, "ass_to_srt") as conv:
        assert h.run()["status"] == "FIXED"
    conv.assert_not_called()


# --- the sidecar must be the right episode, in English -----------------------------------

def test_wrong_episode_sidecar_is_left_alone_and_not_verified_again(tmp_path, monkeypatch, capsys):
    """Same vocabulary, different word pairs: the bigram leg rejects it, and the verdict is remembered."""
    wrong, _ = _episode(2)
    _, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", wrong)], asr=asr)
    result = h.run()
    assert result["status"] == "SKIP" and result["detail"] == "tag present"
    assert h.remuxes == [] and h.normalize_calls == []
    assert h.sidecars_left() == ["Movie (2024).en.srt"]
    assert h.media.read_bytes() == b"\x1a\x45\xdf\xa3 original"
    out = capsys.readouterr().out
    assert "UPGRADE-SKIP" in out and "not this file's subtitle" in out
    assert "UPGRADE: generated" not in out
    assert len(h.window_reads) == 3
    rejection = h.state()["upgrade_rejected"]
    assert rejection["sidecars"][0][0] == "Movie (2024).en.srt"
    assert rejection["video"] == list(cs._identity(h.media))   # and the video it was judged against

    h.window_reads.clear()
    h.probes.clear()
    second = h.run()
    assert second["detail"] == "state cache"
    assert h.window_reads == [] and h.probes == []


@pytest.mark.parametrize("handoff", [False, True])
def test_foreign_text_sidecar_is_never_used_and_the_sweep_quarantines_it(tmp_path, monkeypatch, capsys, handoff):
    french = _srt([(600.0, ["Je ne sais pas ce que tu veux dire, mais il est tout à fait "
                            "possible que nous soyons en retard pour le dîner de ce soir"] * 40)])
    _, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", french)], asr=asr, handoff=handoff)
    assert cs.classify_candidate(french)[0] is False             # the content gate rejects it
    result = h.run()
    # Not upgraded (the cheap text check ran first, no window was read)...
    assert h.remuxes == []
    assert h.window_reads == []
    assert "language check: fre" in capsys.readouterr().out
    # ...and, being foreign-language, the late sweep quarantines it, by the gate and not the upgrade
    assert result["detail"] == "tag present; quarantined 1 sidecar(s)"
    assert h.sidecars_left() == [] and len(h.quarantined()) == 1
    assert _quarantine_reasons(h) == ["late sidecar beside already-processed file"]


def test_windows_with_too_little_generated_text_do_not_upgrade(tmp_path, monkeypatch, capsys):
    """Generated track nearly silent in the windows (silence, music): a match over a few words proves nothing."""
    srt_text, _ = _episode(1)
    sparse = {start: "you know the garden" for start in WINDOW_STARTS}
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", srt_text)], asr=sparse)
    result = h.run()
    assert result["detail"] == "tag present"
    assert h.remuxes == []
    assert len(h.window_reads) == 3
    assert "0 of 0 usable" in capsys.readouterr().out


@pytest.mark.parametrize("matching_windows, upgrades", [((0,), False), ((0, 2), True)],
                         ids=["one-of-three-is-not-enough", "two-of-three-are-enough"])
def test_two_matching_windows_of_three_are_needed(tmp_path, monkeypatch, matching_windows, upgrades):
    srt_text, asr = _episode(1)
    _, other = _episode(2)
    mixed = {start: (asr if i in matching_windows else other)[start]
             for i, start in enumerate(WINDOW_STARTS)}
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", srt_text)], asr=mixed)
    result = h.run()
    assert (result["status"] == "FIXED") is upgrades
    assert bool(h.remuxes) is upgrades
    assert upgrades or result["detail"] == "tag present"


def test_window_extraction_failure_keeps_the_generated_track_and_retries_later(tmp_path, monkeypatch, capsys):
    srt_text, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", srt_text)], asr=asr,
                window_failure=True)
    result = h.run()
    assert result["detail"] == "tag present"
    assert h.remuxes == [] and h.normalize_calls == []
    assert h.sidecars_left() == ["Movie (2024).en.srt"]
    assert len(h.window_reads) == 1  # gave up at the first unreadable window
    assert "could not read the generated track" in capsys.readouterr().out
    # a read failure may be transient: it must NOT be remembered as a rejection
    assert "upgrade_rejected" not in h.state()


@pytest.mark.parametrize("duration", [None, "0", "0.0", "-5", "N/A", "nan", "inf"])
def test_missing_or_zero_duration_is_a_retryable_failure_not_a_remembered_rejection(
        tmp_path, monkeypatch, capsys, duration):
    """No duration means no window: that says nothing about the sidecar, so nothing is remembered."""
    info = _info()
    if duration is None:
        del info["format"]["duration"]
    else:
        info["format"]["duration"] = duration
    h = _upgrade_ready(tmp_path, monkeypatch, info=info)
    assert h.run()["detail"] == "tag present"
    assert h.remuxes == [] and h.window_reads == []
    assert h.sidecars_left() == ["Movie (2024).en.srt"]
    assert "UPGRADE-SKIP" in capsys.readouterr().out
    assert "upgrade_rejected" not in h.state()

    h.info = _info()  # the duration reads fine now
    assert h.run()["status"] == "FIXED"
    assert h.remuxes[0]["eng_track_name"] == "English"


# --- when the upgrade path does not apply at all -----------------------------------------

@pytest.mark.parametrize("sidecar, info", [
    (".en.srt", _info(subs=(("English", "subrip"),))),
    (".en.srt", _info(subs=(("English (generated)", "subrip"), ("English", "subrip")))),
    (".en.srt", _info(subs=((None, "subrip"),))),
    (".en.generated.srt", None),
    (".en.forced.srt", None),
    (".fr.srt", None),
], ids=["real-embedded-track", "real-track-next-to-generated-one", "untitled-embedded-track-counts-as-real",
        "generated-sidecar", "forced-sidecar", "other-language-sidecar"])
def test_the_upgrade_path_does_not_apply(tmp_path, monkeypatch, sidecar, info):
    h = _upgrade_ready(tmp_path, monkeypatch, sidecar=sidecar, info=info)
    assert h.run()["detail"] == "tag present"
    assert h.remuxes == [] and h.window_reads == []
    assert len(h.probes) == 1  # the one ordinary probe, nothing extra
    assert h.sidecars_left() == [f"Movie (2024){sidecar}"]


@pytest.mark.parametrize("state_cache, detail, probes", [(False, "tag present", 1), (True, "state cache", 0)],
                         ids=["tag-present", "state-cache"])
def test_no_sidecar_means_no_extra_probe_and_no_window_read(tmp_path, monkeypatch, state_cache, detail, probes):
    """The common sweep case stays as cheap as it was: one directory listing."""
    h = Harness(tmp_path, monkeypatch, state_cache=state_cache)
    assert h.run()["detail"] == detail
    assert len(h.probes) == probes and h.window_reads == [] and h.remuxes == []


def test_unprocessed_file_with_a_real_sidecar_takes_the_ordinary_full_pass(tmp_path, monkeypatch):
    """No CONSOLIDATED_SUBS tag: not an upgrade, so the audio policy and normalization run, with no probe
    beyond the first and the duration guard's pair (file, output) before the swap."""
    srt_text, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", srt_text)], asr=asr,
                info=_info(tagged=False, normalized=False, subs=()))
    result = h.run()
    assert result["status"] == "FIXED"
    assert not result["detail"].startswith("UPGRADE")
    assert h.window_reads == []
    assert h.audio_policy_calls == (1, 1, 1)
    assert len(h.normalize_calls) == 1
    assert [p.name.endswith(".tmp.mkv") for p in h.probes] == [False, False, True]
    assert "upgraded" not in h.state()


def test_a_failing_upgrade_check_never_breaks_the_normal_skips(tmp_path, monkeypatch, capsys):
    h = Harness(tmp_path, monkeypatch, state_cache=True)
    with patch.object(cs, "find_sidecar_subs", side_effect=OSError("listing failed")):
        result = h.run()
    assert result["status"] == "SKIP" and result["detail"] == "state cache"
    assert "check failed: OSError" in capsys.readouterr().out


# --- dry run ----------------------------------------------------------------------------

def test_dry_run_reports_the_upgrade_and_changes_nothing(tmp_path, monkeypatch):
    h = _upgrade_ready(tmp_path, monkeypatch)
    result = h.run(dry_run=True)
    assert result["status"] == "SKIP"
    assert result["detail"] == ("dry-run: would UPGRADE generated -> real subtitle "
                                "(Movie (2024).en.srt)")
    assert h.remuxes == [] and h.normalize_calls == []
    assert h.sidecars_left() == ["Movie (2024).en.srt"]
    assert h.state() is None


def test_dry_run_does_not_remember_a_rejection(tmp_path, monkeypatch):
    wrong, _ = _episode(2)
    _, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", wrong)], asr=asr)
    h.run(dry_run=True)
    assert h.state() is None


# --- remembering a rejection ---------------------------------------------------------------

def _rejected_once(tmp_path, monkeypatch):
    """Right-episode sidecar, generated track saying something else: rejected, remembered. Returns (harness, matching ASR)."""
    right, right_asr = _episode(1)
    _other_srt, wrong_asr = _episode(2)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", right)], asr=wrong_asr)
    assert h.run()["detail"] == "tag present"
    assert len(h.window_reads) == 3 and h.remuxes == []
    assert h.state()["upgrade_rejected"]["video"] == list(cs._identity(h.media))
    return h, right_asr


def test_a_same_size_video_rewrite_with_the_mtime_restored_invalidates_the_rejection(
        tmp_path, monkeypatch):
    """Size and mtime look as when rejected but the video was rewritten (ctime): verified again, and now upgraded."""
    h, right_asr = _rejected_once(tmp_path, monkeypatch)
    before = h.state()
    _change_video(h.media, "same-size-mtime-restored")
    now = h.media.stat()
    assert (now.st_size, int(now.st_mtime)) == (before["size"], before["mtime"])  # the old key matched
    h.asr = right_asr   # the rewritten video's generated track matches the sidecar
    h.window_reads.clear()
    result = h.run()
    assert h.window_reads, "the rejection was honoured instead of verifying again"
    assert result["status"] == "FIXED" and h.remuxes[0]["eng_track_name"] == "English"


@pytest.mark.parametrize("change", ["replaced", "rewritten", "touched", "chmod"])
def test_any_change_to_the_video_invalidates_the_rejection(tmp_path, monkeypatch, change):
    """Even metadata only: ctime moves on chmod / a hardlink change too, and the safe side is to look again."""
    h, _right = _rejected_once(tmp_path, monkeypatch)
    _change_video(h.media, change)
    h.window_reads.clear()
    h.run()
    assert len(h.window_reads) == 3  # judged again (still wrong here, so still not upgraded)


def _bare_list(record):
    return record["sidecars"]


def _three_field_video(record):
    return {**record, "video": record["video"][:3]}


def _without_reasons(record):
    return {k: v for k, v in record.items() if k != "reasons"}


@pytest.mark.parametrize("outdated", [_bare_list, _three_field_video, _without_reasons],
                         ids=["old-bare-list-without-video-identity", "legacy-three-field-video-identity",
                              "older-format-without-reasons"])
def test_an_outdated_rejection_record_is_never_honoured_and_is_rewritten(tmp_path, monkeypatch, outdated):
    h, _right = _rejected_once(tmp_path, monkeypatch)
    entry = cs.load_state(cs.STATE_FILE)[str(h.media.resolve())]
    entry["upgrade_rejected"] = outdated(entry["upgrade_rejected"])
    cs.update_state_entry(cs.STATE_FILE, str(h.media.resolve()), entry)
    h.window_reads.clear()
    h.probes.clear()
    h.run()
    assert len(h.window_reads) == 3                                          # verified again
    assert isinstance(h.state()["upgrade_rejected"]["reasons"], dict)       # re-recorded, current format


def test_a_record_with_nothing_to_say_is_still_valid_and_not_reverified(tmp_path, monkeypatch):
    """`reasons == {}` is the current format; only a missing or non-dict `reasons` is invalid."""
    tiny = _srt([(600.0, ["You know that"])])
    _, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", tiny)], asr=asr)
    assert h.run()["detail"] == "tag present"
    assert h.remuxes == [] and h.window_reads == []
    assert h.state()["upgrade_rejected"]["reasons"] == {}
    h.window_reads.clear()
    h.probes.clear()
    assert h.run()["detail"] == "state cache" and h.probes == []


def _snaps_of(*specs):
    """{Path: _Snapshot} for (name, size, digest) specs."""
    return {Path(n): cs._Snapshot((1, size, 0, 0), digest, b"") for n, size, digest in specs}


def test_recorded_verdicts_require_the_reasons_key():
    video, fp = (7, 100, 1000, 2000), [["a.en.srt", 10, "x" * 64]]
    snaps = _snaps_of(("a.en.srt", 10, "x" * 64))
    good = cs._rejection_record(video, fp)
    assert good["reasons"] == {} and cs._recorded_verdicts(good, video, snaps) == {"a.en.srt": None}
    assert cs._recorded_verdicts(cs._rejection_record(video, fp, {"a.en.srt": "why"}), video, snaps) \
        == {"a.en.srt": "why"}
    without = {k: v for k, v in good.items() if k != "reasons"}
    assert cs._recorded_verdicts(without, video, snaps) is None
    assert cs._recorded_verdicts({**good, "reasons": ["a.en.srt"]}, video, snaps) is None
    assert cs._recorded_verdicts({**good, "reasons": None}, video, snaps) is None


def test_recorded_verdicts_need_the_video_and_apply_per_sidecar():
    video, other = (7, 100, 1000, 2000), (7, 100, 1000, 2001)
    fp = [["a.en.srt", 10, "abc"], ["b.en.srt", 20, "def"]]
    record = cs._rejection_record(video, fp, {"a.en.srt": "wrong"})
    both = _snaps_of(("a.en.srt", 10, "abc"), ("b.en.srt", 20, "def"))
    assert cs._recorded_verdicts(record, video, both) == {"a.en.srt": "wrong", "b.en.srt": None}
    assert cs._recorded_verdicts(record, other, both) is None                      # video differs (ctime)
    # ONE sidecar differing (or new, or unreadable) removes only its own entry
    changed_b = _snaps_of(("a.en.srt", 10, "abc"), ("b.en.srt", 20, "XYZ"))
    assert cs._recorded_verdicts(record, video, changed_b) == {"a.en.srt": "wrong"}
    changed_a = _snaps_of(("a.en.srt", 10, "XYZ"), ("b.en.srt", 20, "def"))
    assert cs._recorded_verdicts(record, video, changed_a) == {"b.en.srt": None}
    assert cs._recorded_verdicts(record, video, _snaps_of(("c.en.srt", 5, "q"))) == {}   # a later arrival
    assert cs._recorded_verdicts(record, video, _snaps_of(("b.en.srt", 20, "def"))) == {"b.en.srt": None}
    assert cs._recorded_verdicts(fp, video, both) is None                          # old bare list
    assert cs._recorded_verdicts(None, video, both) is None
    assert cs._recorded_verdicts({"sidecars": fp, "reasons": {}}, video, both) is None   # no video identity
    assert cs._recorded_verdicts({"video": [7, 100, 1000], "sidecars": fp, "reasons": {}},
                                 video, both) is None                              # legacy 3-field
    assert cs._recorded_verdicts({"video": list(video), "reasons": {}}, video, both) == {}   # no sidecars


def test_a_replaced_sidecar_is_verified_again(tmp_path, monkeypatch):
    """The rejection is keyed on the sidecar's name, size and sha256: a different subtitle in its place is checked afresh."""
    wrong, _ = _episode(2)
    right, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", wrong)], asr=asr)
    assert h.run()["detail"] == "tag present"

    (tmp_path / "Movie (2024).en.srt").write_text(right + "\n")
    assert h.run()["status"] == "FIXED"
    assert h.remuxes[0]["eng_track_name"] == "English"


def test_a_new_second_sidecar_is_verified_even_after_a_rejection(tmp_path, monkeypatch):
    wrong, _ = _episode(2)
    right, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", wrong)], asr=asr)
    assert h.run()["detail"] == "tag present"
    (tmp_path / "Movie (2024).eng.srt").write_text(right)
    assert h.run()["status"] == "FIXED"
    assert h.remuxes[0]["content"].count("-->") == right.count("-->")


# --- idempotency ------------------------------------------------------------------------------

def test_after_an_upgrade_the_next_scan_does_nothing(tmp_path, monkeypatch):
    h = _upgrade_ready(tmp_path, monkeypatch)
    assert h.run()["status"] == "FIXED"
    remuxes = len(h.remuxes)
    # the sidecar was consumed; the file now carries a real "English" track
    h.info = _info(subs=(("English", "subrip"),))
    again = h.run()
    assert again["status"] == "SKIP" and again["detail"] == "state cache"
    assert len(h.remuxes) == remuxes


# --- what is verified is what is muxed, and what is deleted ----------------------

def _replace_atomically(path, text):
    """Swap `path` for a new file with `text` (a new inode), as a downloader or `mv` would."""
    tmp = path.with_name(path.name + ".incoming")
    tmp.write_text(text)
    os.replace(tmp, path)


def _once(fn):
    done = []

    def _run():
        if not done:
            done.append(True)
            fn()
    return _run


def test_sidecar_is_read_to_verify_and_once_more_before_it_is_deleted_never_copied(tmp_path, monkeypatch):
    """One open to verify, one more (bounded) to compare with the snapshot before the unlink; never copied."""
    h = _upgrade_ready(tmp_path, monkeypatch)
    sidecar = tmp_path / "Movie (2024).en.srt"
    opens, copies = [], []
    real_open = Path.open

    def _spy_open(self, *a, **k):
        if self == sidecar:
            opens.append(a)
        return real_open(self, *a, **k)

    with patch.object(Path, "open", _spy_open), \
         patch.object(cs.shutil, "copy2", side_effect=lambda s, d, **k: copies.append(s)):
        assert h.run()["status"] == "FIXED"
    assert opens == [("rb",), ("rb",)]
    assert [c for c in copies if Path(c) == sidecar] == []


@pytest.mark.parametrize("when", ["during-verification", "during-the-mux"])
def test_replacement_after_verification_never_gets_muxed_or_deleted(tmp_path, monkeypatch, capsys, when):
    """Swapped after the check read it: the verified bytes are muxed, the replacement neither consumed nor deleted."""
    h = _upgrade_ready(tmp_path, monkeypatch)
    sidecar = tmp_path / "Movie (2024).en.srt"
    verified_text = cs.clean_srt_text(sidecar.read_text())
    replacement = "1\n00:00:01,000 --> 00:00:02,000\nSomething else entirely, unverified.\n"
    swap = _once(lambda: _replace_atomically(sidecar, replacement))
    if when == "during-verification":
        h.on_window = swap
    else:
        h.on_remux = swap
    result = h.run()

    assert result["status"] == "FIXED"
    assert h.remuxes[0]["content"] == verified_text
    assert "unverified" not in h.remuxes[0]["content"]
    assert sidecar.read_text() == replacement
    assert "left Movie (2024).en.srt in place" in capsys.readouterr().out


@pytest.mark.parametrize("rewrite", ["shorter", "same-size-newer-mtime", "same-length-mtime-restored",
                                     "identical-bytes-mtime-restored"])
def test_in_place_rewrite_after_verification_is_left_in_place(tmp_path, monkeypatch, capsys, rewrite):
    """Same inode: size, mtime_ns, ctime and the bytes' sha256 are all that can tell it changed.
    The verified bytes are still what was muxed."""
    h = _upgrade_ready(tmp_path, monkeypatch)
    sidecar = tmp_path / "Movie (2024).en.srt"
    original = sidecar.read_text()
    verified_text = cs.clean_srt_text(original)
    stamp = sidecar.stat()
    after = {"shorter": "X" * 20, "same-size-newer-mtime": "Y" * len(original),
             "same-length-mtime-restored": "X" * len(original),
             "identical-bytes-mtime-restored": original}[rewrite]

    def _rewrite():
        time.sleep(0.02)  # past the filesystem's timestamp granularity
        with sidecar.open("r+") as fh:
            fh.write(after)
            if rewrite == "shorter":
                fh.truncate()
        if rewrite == "same-size-newer-mtime":
            os.utime(sidecar, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 5_000_000_000))
        elif rewrite.endswith("mtime-restored"):
            os.utime(sidecar, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            now = sidecar.stat()
            assert (now.st_ino, now.st_size, now.st_mtime_ns) == (
                stamp.st_ino, stamp.st_size, stamp.st_mtime_ns)
            assert now.st_ctime_ns != stamp.st_ctime_ns   # only the ctime shows the write

    h.on_remux = _once(_rewrite)
    assert h.run()["status"] == "FIXED"
    assert h.remuxes[0]["content"] == verified_text
    assert sidecar.read_text() == after                  # not deleted
    assert "left Movie (2024).en.srt in place" in capsys.readouterr().out


def test_a_sidecar_that_appears_after_the_check_is_left_in_place(tmp_path, monkeypatch):
    h = _upgrade_ready(tmp_path, monkeypatch)
    late = tmp_path / "Movie (2024).en.cc.srt"
    h.on_remux = _once(lambda: late.write_text("arrived late"))
    assert h.run()["status"] == "FIXED"
    assert late.read_text() == "arrived late"
    assert h.sidecars_left() == ["Movie (2024).en.cc.srt"]


def test_only_the_sidecar_that_was_muxed_is_deleted(tmp_path, monkeypatch):
    """Not swept: a generated, forced, foreign or wrong-episode sidecar, nor a second verified one not muxed."""
    srt_text, asr = _episode(1)
    wrong, _ = _episode(2)
    h = Harness(tmp_path, monkeypatch, asr=asr, sidecars=[
        (".en.srt", srt_text), (".en.sdh.srt", srt_text),
        (".en.generated.srt", "old generated"), (".fr.srt", "un sous-titre"),
        (".en.forced.srt", "forced"), (".eng.srt", wrong)])
    result = h.run()
    assert result["status"] == "FIXED" and "dropped_sidecars=1" in result["detail"]
    assert h.sidecars_left() == [
        "Movie (2024).en.forced.srt", "Movie (2024).en.generated.srt",
        "Movie (2024).en.sdh.srt", "Movie (2024).eng.srt", "Movie (2024).fr.srt"]


def _episode_neighbours(tmp_path):
    """Files that belong to Episode 10 and Episode 100, not to Episode 1."""
    neighbours = {
        "Episode 10.en.srt": _episode(1)[0] + "\n" + _srt([(2300.0, _dialogue(5, n=80))]),
        "Episode 10.fr.srt": "un sous-titre",
        "Episode 10.en.generated.srt": "old generated",
        "Episode 100.srt": "another one",
        "Episode 1 (alt).en.srt": "not the same stem either",
    }
    for name, content in neighbours.items():
        (tmp_path / name).write_text(content)
    return neighbours


def test_episode_1_never_touches_episode_10_files_in_an_upgrade(tmp_path, monkeypatch):
    srt_text, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, stem="Episode 1", asr=asr, sidecars=[(".en.srt", srt_text)])
    neighbours = _episode_neighbours(tmp_path)
    result = h.run()
    assert result["status"] == "FIXED"
    assert h.remuxes[0]["content"] == cs.clean_srt_text(srt_text)  # not the longer Episode 10 one
    assert "Episode 1.en.srt" not in h.sidecars_left()               # the consumed one
    for name, content in neighbours.items():
        assert (tmp_path / name).read_text() == content, name


def test_episode_10_sidecar_is_not_a_candidate_for_episode_1(tmp_path, monkeypatch):
    """Only another episode's sidecar is there: no upgrade, no extra work."""
    _, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, stem="Episode 1", asr=asr)
    neighbours = _episode_neighbours(tmp_path)
    result = h.run()
    assert result["detail"] == "tag present"
    assert h.remuxes == [] and h.window_reads == [] and len(h.probes) == 1
    assert all((tmp_path / n).read_text() == c for n, c in neighbours.items())


def test_a_regular_full_pass_on_episode_1_leaves_episode_10_files_alone(tmp_path, monkeypatch):
    srt_text, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, stem="Episode 1", asr=asr, sidecars=[(".en.srt", srt_text)],
                info=_info(tagged=False, subs=()))
    neighbours = _episode_neighbours(tmp_path)
    result = h.run()
    assert result["status"] == "FIXED" and not result["detail"].startswith("UPGRADE")
    assert not (tmp_path / "Episode 1.en.srt").exists()
    assert all((tmp_path / n).read_text() == c for n, c in neighbours.items())


# --- forced English tracks (text or image) are carried over unchanged -----------------------------------

def _sub(index, codec, *, title=None, lang="eng", default=0, forced=1):
    tags = {}
    if lang:
        tags["language"] = lang
    if title:
        tags["title"] = title
    return {"index": index, "codec_type": "subtitle", "codec_name": codec, "tags": tags,
            "disposition": {"default": default, "forced": forced}}


def _info_with(*extra_subs):
    """The usual consolidated file (generated English main at index 2) plus extra source subtitle streams."""
    info = _info()
    info["streams"].extend(extra_subs)
    return info


PGS = "hdmv_pgs_subtitle"


def test_forced_english_pgs_is_kept_unchanged_forced_and_not_default(tmp_path, monkeypatch):
    """The PGS goes through the kept-subtitle path with its exact flags, never extracted: output [PGS, new main]."""
    h = _upgrade_ready(tmp_path, monkeypatch, info=_info_with(_sub(3, PGS, title="Forced")))
    result = h.run()
    assert result["status"] == "FIXED" and result["detail"].startswith("UPGRADE")
    assert h.remuxes[0]["keep"] == [3] and h.remuxes[0]["forced"] is None
    assert h.remuxes[0]["flags"][3] == {"default": False, "forced": True}
    assert h.extractions == []
    out_subs = [s for s in h._out_info()["streams"] if s["codec_type"] == "subtitle"]
    assert [s["codec_name"] for s in out_subs] == [PGS, "subrip"]
    assert (out_subs[0]["disposition"]["forced"], out_subs[0]["disposition"]["default"]) == (1, 0)


@pytest.mark.parametrize("codec", ["subrip", "ass"])
def test_forced_english_text_track_is_kept_as_is_and_never_extracted(tmp_path, monkeypatch, codec):
    """Never extracted, cleaned, converted or re-attached as a second external file (it would duplicate the
    kept track): mkvmerge carries it unchanged even though extracting or converting it would fail."""
    h = _upgrade_ready(tmp_path, monkeypatch, info=_info_with(_sub(3, codec, title="Forced")))
    h.extract_ok = False
    with patch.object(cs, "ass_to_srt", return_value=False):
        assert h.run()["status"] == "FIXED"
    assert h.extractions == []
    assert h.remuxes[0]["keep"] == [3] and h.remuxes[0]["forced"] is None


def test_every_forced_english_track_is_kept_text_and_image_in_source_order(tmp_path, monkeypatch):
    h = _upgrade_ready(tmp_path, monkeypatch, info=_info_with(
        _sub(3, PGS, title="Forced"), _sub(4, "subrip", title="Forced"),
        _sub(5, "dvd_subtitle", lang="en-US", default=0, forced=1)))
    assert h.run()["status"] == "FIXED"
    assert h.remuxes[0]["keep"] == [3, 4, 5]


def test_only_forced_english_tracks_are_kept(tmp_path, monkeypatch):
    """A forced French track, a non-forced English PGS and an untagged forced track are dropped, as in the regular pass."""
    h = _upgrade_ready(tmp_path, monkeypatch, info=_info_with(
        _sub(3, PGS, lang="fre"), _sub(4, PGS, title="English", forced=0),
        _sub(5, "subrip", lang=None), _sub(6, PGS, title="Forced")))
    assert h.run()["status"] == "FIXED"
    assert h.remuxes[0]["keep"] == [6]


def test_a_kept_forced_track_keeps_its_exact_source_flags(tmp_path, monkeypatch):
    """The upgrade does not decide a kept track's flags, default included."""
    h = _upgrade_ready(tmp_path, monkeypatch, info=_info_with(_sub(3, PGS, default=1, forced=1)))
    assert h.run()["status"] == "FIXED"
    assert h.remuxes[0]["flags"][3] == {"default": True, "forced": True}


def test_a_forced_english_track_that_cannot_be_kept_skips_the_upgrade(tmp_path, monkeypatch, capsys):
    """An unrecognised-codec forced English track can't be vouched for: SKIP before any real work, retried later."""
    info = _info_with(_sub(3, "dvb_teletext", title="Forced"))
    h = _upgrade_ready(tmp_path, monkeypatch, info=info)
    result = h.run()
    assert result["status"] == "SKIP"
    assert h.remuxes == [] and h.window_reads == [] and h.sync_inputs == []
    assert h.media.read_bytes() == MEDIA_BYTES
    assert h.sidecars_left() == ["Movie (2024).en.srt"]
    h.set_tag.assert_not_called()
    out = capsys.readouterr().out
    assert "UPGRADE-SKIP" in out and "cannot keep" in out and "dvb_teletext" in out
    assert "upgrade_rejected" not in h.state()  # not a verdict on the sidecar

    h.info = _info()  # the track is gone (or understood) next time
    assert h.run()["status"] == "FIXED"


# --- the subtitle postconditions of the verify ----------------------------------------------------------

@pytest.mark.parametrize("what", [
    "kept-missing", "kept-codec-changed", "kept-untagged", "kept-not-english",
    "kept-forced-lost", "kept-default-changed", "kept-and-main-swapped",
    "main-untagged", "main-not-english", "main-forced", "main-not-default",
    "extra-subtitle"])
def test_the_subtitle_postconditions_are_all_enforced(tmp_path, monkeypatch, what):
    """Exactly the source's forced English track (same codec, English, flags), then the new main one (English, default)."""
    def _edit(streams):
        kept, main = [s for s in streams if s["codec_type"] == "subtitle"]
        if what == "kept-missing":
            streams.remove(kept)
        elif what == "kept-codec-changed":
            kept["codec_name"] = "subrip"
        elif what == "kept-untagged":
            kept["tags"].pop("language")
        elif what == "kept-not-english":
            kept["tags"]["language"] = "fre"
        elif what == "kept-forced-lost":
            kept["disposition"]["forced"] = 0
        elif what == "kept-default-changed":
            kept["disposition"]["default"] = 1
        elif what == "kept-and-main-swapped":
            streams.remove(kept)
            streams.append(kept)
        elif what == "main-untagged":
            main["tags"].pop("language")
        elif what == "main-not-english":
            main["tags"]["language"] = "fre"
        elif what == "main-forced":
            main["disposition"]["forced"] = 1
        elif what == "main-not-default":
            main["disposition"]["default"] = 0
        elif what == "extra-subtitle":
            streams.append({**copy.deepcopy(kept), "index": 99})

    h = _upgrade_ready(tmp_path, monkeypatch, info=_info_with(_sub(3, PGS, title="Forced")))
    h.out_edit = _edit
    result = h.run()
    assert result["status"] == "FAIL" and result["detail"].startswith("upgrade verify: "), result
    assert h.media.read_bytes() == MEDIA_BYTES
    assert not list(tmp_path.glob("*.tmp.mkv"))
    assert h.sidecars_left() == ["Movie (2024).en.srt"]
    h.set_tag.assert_not_called()


def test_verify_computes_the_layout_from_the_source_forced_tracks(tmp_path):
    """The kept-track layout comes from the SOURCE's forced English tracks, not from anything the pass did."""
    video, audio = [_stream(0, "video", default=1)], [_stream(1, "audio", default=1)]
    flags = {0: {"default": True, "forced": False}, 1: {"default": True, "forced": False},
             3: {"default": False, "forced": True}}
    pgs = _sub(3, PGS, title="Forced")
    main = {"index": 90, "codec_type": "subtitle", "codec_name": "subrip",
            "tags": {"language": "eng"}, "disposition": {"default": 1, "forced": 0}}

    def _verify(kept, out_subs):
        with patch.object(cs, "probe", return_value={"streams": [*video, *audio, *out_subs]}):
            return cs._verify_upgrade_output(Path("o"), video, audio, kept, flags)

    assert _verify([pgs], [pgs, main]) is None
    assert "subs=2, got" in _verify([pgs], [main])        # the source had a forced track
    assert "subs=1, got" in _verify([], [pgs, main])      # and here it had none
    assert _verify([], [main]) is None


# --- the upgrade is bound to the video it probed --------------------------------------------------------

def _change_video(media, how):
    st = media.stat()
    if how == "replaced":      # a new file under the same name (new inode)
        new = media.with_name(media.name + ".incoming")
        new.write_bytes(b"\x1a\x45\xdf\xa3 replaced by something else")
        os.replace(new, media)
    elif how == "rewritten":   # same inode, other size
        with media.open("ab") as fh:
            fh.write(b" and some more")
    elif how == "touched":     # same bytes, newer mtime
        os.utime(media, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    elif how == "chmod":       # metadata only
        time.sleep(0.02)
        os.chmod(media, 0o600)
    elif how == "same-size-mtime-restored":
        # Same inode, same size, mtime put back: only the ctime shows it.
        time.sleep(0.02)  # past the filesystem's timestamp granularity
        with media.open("r+b") as fh:
            fh.write(b"\xff" * st.st_size)
        os.utime(media, ns=(st.st_atime_ns, st.st_mtime_ns))
        after = media.stat()
        assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
            st.st_ino, st.st_size, st.st_mtime_ns)


@pytest.mark.parametrize("how", ["replaced", "rewritten", "touched", "same-size-mtime-restored"])
def test_a_video_that_changes_before_the_remux_aborts_the_upgrade(tmp_path, monkeypatch, capsys, how):
    h = _upgrade_ready(tmp_path, monkeypatch)
    h.on_sync = _once(lambda: _change_video(h.media, how))
    result = h.run()
    assert result["status"] == "SKIP"
    assert result["detail"] == "upgrade: the video changed before the remux, original kept"
    assert h.remuxes == [] and h.normalize_calls == []
    assert h.sidecars_left() == ["Movie (2024).en.srt"]
    assert not list(tmp_path.glob("*.tmp.mkv"))
    h.set_tag.assert_not_called()
    assert "UPGRADE-SKIP" in capsys.readouterr().out
    assert h.state() is None  # nothing recorded: retried next scan

    assert h.run()["status"] == "FIXED"  # the changed file is simply the new source


@pytest.mark.parametrize("how", ["replaced", "same-size-mtime-restored"])
@pytest.mark.parametrize("when", ["during-the-remux", "during-the-verify"])
def test_a_video_that_changes_before_the_swap_aborts_and_removes_the_temp_output(
        tmp_path, monkeypatch, capsys, when, how):
    h = _upgrade_ready(tmp_path, monkeypatch)
    changed = {}

    def _change():
        _change_video(h.media, how)
        changed["bytes"] = h.media.read_bytes()

    change = _once(_change)
    if when == "during-the-remux":
        h.on_remux = change
    else:
        h.on_verify = change
    result = h.run()
    assert result["status"] == "SKIP"
    assert result["detail"] == "upgrade: the video changed before the swap, original kept"
    assert h.media.read_bytes() == changed["bytes"] != b"MUXED"   # the new file was never overwritten
    assert not list(tmp_path.glob("*.tmp.mkv"))       # the temp output is gone
    assert h.sidecars_left() == ["Movie (2024).en.srt"]
    h.set_tag.assert_not_called()
    assert h.normalize_calls == []
    assert "the video changed before the swap" in capsys.readouterr().out
    assert h.state() is None


def test_the_video_identity_is_recorded_when_the_gate_probes_it(tmp_path, monkeypatch):
    h = _upgrade_ready(tmp_path, monkeypatch)
    seen = []
    with patch.object(cs, "probe", side_effect=lambda p: seen.append(cs._identity(Path(p))) or h.info), \
         patch.object(sv, "embedded_text_window", side_effect=h._window):
        upgrade, _info_, _rej = cs._generated_upgrade_check(h.media, cs.file_key(h.media), None)
    assert upgrade.video_identity == cs._identity(h.media) == seen[0]


# --- the bytes are compared again before the sidecar is deleted ----------------------------------------

def test_the_snapshot_identity_is_the_four_field_identity_with_ctime(tmp_path):
    p = tmp_path / "Movie.en.srt"
    p.write_text("hello")
    snap = cs._snapshot_sidecar(p)
    st = p.stat()
    assert snap.identity == (st.st_ino, 5, st.st_mtime_ns, st.st_ctime_ns) == cs._identity(p)


class _MidRead:
    """Wraps the real file handle so `mutate()` runs between the first fstat and the read."""
    def __init__(self, fh, mutate):
        self._fh, self._mutate = fh, mutate

    def __enter__(self):
        self._fh.__enter__()
        return self

    def __exit__(self, *exc):
        return self._fh.__exit__(*exc)

    def fileno(self):
        return self._fh.fileno()

    def read(self, n=-1):
        self._mutate()
        return self._fh.read(n)


def _snapshot_with_mutation(path, mutate):
    real_open = Path.open

    def _open(self, *a, **k):
        fh = real_open(self, *a, **k)
        return _MidRead(fh, mutate) if self == path else fh

    with patch.object(Path, "open", _open):
        return cs._snapshot_sidecar(path)


@pytest.mark.parametrize("how", ["truncated", "growing", "rewritten-same-length"])
def test_a_snapshot_taken_while_the_file_changes_is_rejected(tmp_path, how):
    """Complete and stable: as many bytes as the first fstat promised, size/mtime/ctime unchanged across the read."""
    p = tmp_path / "Movie.en.srt"
    p.write_bytes(b"0123456789" * 20)

    def _mutate():
        time.sleep(0.02)
        if how == "truncated":
            os.truncate(p, 50)
        elif how == "growing":
            with open(p, "ab") as fh:  # (builtin open: Path.open is the wrapped one)
                fh.write(b"more " * 10)
        else:
            with open(p, "r+b") as fh:
                fh.write(b"X" * 200)

    assert _snapshot_with_mutation(p, _mutate) is None


def test_a_short_read_with_an_unchanged_stat_is_rejected(tmp_path):
    """Nothing about the file changed, but the read returned fewer bytes than the first fstat promised."""
    p = tmp_path / "Movie.en.srt"
    p.write_bytes(b"0123456789" * 20)
    real_open = Path.open

    class _Short(_MidRead):
        def read(self, n=-1):
            return self._fh.read(n)[:-5]

    def _open(self, *a, **k):
        fh = real_open(self, *a, **k)
        return _Short(fh, lambda: None) if self == p else fh

    with patch.object(Path, "open", _open):
        assert cs._snapshot_sidecar(p) is None


def test_a_stable_complete_file_still_snapshots(tmp_path):
    p = tmp_path / "Movie.en.srt"
    p.write_bytes(b"0123456789" * 20)
    snap = _snapshot_with_mutation(p, lambda: None)
    assert snap is not None and snap.data == b"0123456789" * 20


def test_a_snapshot_that_fails_stability_is_retried_never_remembered(tmp_path, monkeypatch, capsys):
    """A file changing during the first read is unreadable, not a content verdict: the next scan upgrades."""
    h = _upgrade_ready(tmp_path, monkeypatch)
    sidecar = tmp_path / "Movie (2024).en.srt"
    real_fstat = os.fstat
    seen = []

    class _Moved:
        def __init__(self, st):
            self.st_ino, self.st_size = st.st_ino, st.st_size + 1
            self.st_mtime_ns, self.st_ctime_ns = st.st_mtime_ns, st.st_ctime_ns

    def _fstat(fd):
        st = real_fstat(fd)
        if st.st_size == sidecar.stat().st_size and st.st_ino == sidecar.stat().st_ino:
            seen.append(fd)
            if len(seen) == 2:      # the post-read fstat of the first snapshot
                return _Moved(st)
        return st

    with patch.object(cs.os, "fstat", side_effect=_fstat):
        result = h.run()
    assert result["detail"] == "tag present" and h.remuxes == []
    assert "unreadable" in capsys.readouterr().out
    assert "upgrade_rejected" not in h.state()
    assert sidecar.exists()

    assert h.run()["status"] == "FIXED"


def test_a_sidecar_that_cannot_be_read_again_before_the_unlink_is_left_in_place(
        tmp_path, monkeypatch, capsys):
    """Unreadable or over the size bound on the second read: left in place."""
    h = _upgrade_ready(tmp_path, monkeypatch)
    real = cs._snapshot_sidecar
    calls = []

    def _second_read_fails(p):
        calls.append(p)
        return real(p) if len(calls) == 1 else None

    with patch.object(cs, "_snapshot_sidecar", side_effect=_second_read_fails):
        assert h.run()["status"] == "FIXED"
    assert len(calls) == 2
    assert h.sidecars_left() == ["Movie (2024).en.srt"]
    assert "left Movie (2024).en.srt in place" in capsys.readouterr().out


# --- "consolidated" is the tag OR a fixed state entry -------------------------------

def _state_entry(h, **override):
    cur = cs.file_key(h.media)
    entry = {"size": cur[0], "mtime": cur[1], "v": cs.PIPELINE_VERSION, "status": "fixed",
             "synced": False, "tag_written": False}
    entry.update(override)
    cs.update_state_entry(cs.STATE_FILE, str(h.media.resolve()), entry)


def test_a_file_whose_tag_write_failed_but_whose_state_says_fixed_is_upgraded(tmp_path, monkeypatch):
    """A pass can succeed (status=fixed, tag_written=False) without the tag landing; the track is still replaceable."""
    h = _upgrade_ready(tmp_path, monkeypatch, info=_info(tagged=False))
    _state_entry(h)
    result = h.run()
    assert result["status"] == "FIXED" and result["detail"].startswith("UPGRADE")
    assert h.remuxes[0]["eng_track_name"] == "English"
    assert h.window_reads  # it was verified like any other
    assert h.state()["tag_written"] is True  # and this time the tag landed


@pytest.mark.parametrize("override", [{"status": "skip-tag"}, {"status": "needs-bazarr"}])
def test_a_current_state_entry_that_is_not_fixed_does_not_make_a_file_consolidated(tmp_path, monkeypatch, override):
    """The cache still skips it as before, but nothing was verified against the generated track: not an upgrade."""
    h = _upgrade_ready(tmp_path, monkeypatch, info=_info(tagged=False))
    _state_entry(h, **override)
    result = h.run()
    assert result["detail"] == "state cache"
    assert h.window_reads == [] and h.remuxes == []


@pytest.mark.parametrize("override", [{"v": 1}, {"size": 1}, {"mtime": 1}, None],
                         ids=["other-version", "other-size", "other-mtime", "no-entry-at-all"])
def test_a_stale_fixed_state_entry_does_not_make_an_untagged_file_consolidated(tmp_path, monkeypatch, override):
    """Another version or size/mtime (or no entry) is not a record about this file as it is now: ordinary full pass."""
    h = _upgrade_ready(tmp_path, monkeypatch, info=_info(tagged=False))
    if override is not None:
        _state_entry(h, **override)
    result = h.run()
    assert not result["detail"].startswith("UPGRADE")
    assert h.window_reads == []  # never verified against the generated track
    assert h.audio_policy_calls == (1, 1, 1)


# --- a rejection is keyed on the sidecar's content ------------------------------------

def test_same_size_same_second_replacement_with_other_content_is_verified_again(tmp_path, monkeypatch):
    """Same length, mtime put back: size and mtime match the rejected file's, only the content differs."""
    wrong, _ = _episode(2)
    right, asr = _episode(1)
    size = max(len(wrong), len(right))
    wrong, right = wrong.ljust(size), right.ljust(size)
    assert len(wrong) == len(right) and wrong != right
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", wrong)], asr=asr)
    sidecar = tmp_path / "Movie (2024).en.srt"
    stamp = sidecar.stat()
    assert h.run()["detail"] == "tag present"
    rejected = h.state()["upgrade_rejected"]["sidecars"]

    sidecar.write_text(right)
    os.utime(sidecar, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    after = sidecar.stat()
    assert (after.st_size, after.st_mtime_ns) == (stamp.st_size, stamp.st_mtime_ns)

    assert h.run()["status"] == "FIXED"
    assert rejected[0][1] == len(wrong) and len(rejected[0][2]) == 64  # name, size, sha256


def test_the_fingerprint_follows_the_content_not_the_timestamp(tmp_path):
    a = tmp_path / "Movie.en.srt"
    a.write_text("same text")
    first = cs._sidecar_fingerprint({a: cs._snapshot_sidecar(a)})
    os.utime(a, ns=(1_000_000_000, 1_000_000_000))  # new mtime, same bytes
    assert cs._sidecar_fingerprint({a: cs._snapshot_sidecar(a)}) == first
    a.write_text("SAME TEXT")  # same size, other bytes
    assert cs._sidecar_fingerprint({a: cs._snapshot_sidecar(a)}) != first


def test_a_sidecar_over_the_size_bound_is_refused_never_read_as_a_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "UPGRADE_MAX_SIDECAR_BYTES", 16)
    exactly = tmp_path / "Exactly.en.srt"
    exactly.write_bytes(b"0123456789abcdef")
    assert cs._snapshot_sidecar(exactly).data == b"0123456789abcdef"  # at the bound: fine
    big = tmp_path / "Movie.en.srt"
    big.write_bytes(b"0123456789abcdefX")  # one byte over
    assert cs._snapshot_sidecar(big) is None


def test_a_sidecar_that_grew_past_the_bound_after_fstat_is_refused(tmp_path, monkeypatch):
    """fstat said small, the read found more: still no prefix."""
    monkeypatch.setattr(cs, "UPGRADE_MAX_SIDECAR_BYTES", 16)
    grown = tmp_path / "Movie.en.srt"
    grown.write_bytes(b"0123456789abcdef0123456789")
    small = type("St", (), {"st_ino": 1, "st_size": 5, "st_mtime_ns": 1})()
    with patch.object(cs.os, "fstat", return_value=small):
        assert cs._snapshot_sidecar(grown) is None


def test_an_oversized_sidecar_blocks_the_upgrade_and_is_left_untouched(tmp_path, monkeypatch, capsys):
    """The first LIMIT bytes are a perfect subtitle but the file is bigger: never read as a prefix, muxed or deleted."""
    srt_text, asr = _episode(1)
    limit = len(srt_text.encode())
    monkeypatch.setattr(cs, "UPGRADE_MAX_SIDECAR_BYTES", limit)
    oversized = srt_text + "\n\nthis tail is past the bound"
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", oversized)], asr=asr)
    result = h.run()
    assert result["detail"] == "tag present"
    assert h.remuxes == [] and h.window_reads == []
    assert (tmp_path / "Movie (2024).en.srt").read_text() == oversized
    assert "larger than" in capsys.readouterr().out
    assert "upgrade_rejected" not in h.state()


def test_an_oversized_neighbour_neither_blocks_a_good_sidecar_nor_gets_swept(tmp_path, monkeypatch):
    """One sidecar is verified and used; the other was never read (too big), so the sweep must not delete it."""
    srt_text, asr = _episode(1)
    monkeypatch.setattr(cs, "UPGRADE_MAX_SIDECAR_BYTES", len(srt_text.encode()) + 10)
    h = Harness(tmp_path, monkeypatch, asr=asr, sidecars=[
        (".en.srt", srt_text), (".eng.srt", "x" * (len(srt_text) + 100))])
    assert h.run()["status"] == "FIXED"
    assert h.sidecars_left() == ["Movie (2024).eng.srt"]


def test_an_unreadable_sidecar_is_skipped_and_not_remembered(tmp_path, monkeypatch, capsys):
    h = _upgrade_ready(tmp_path, monkeypatch)
    with patch.object(cs, "_snapshot_sidecar", return_value=None):
        result = h.run()
    assert result["detail"] == "tag present"
    assert h.remuxes == [] and h.window_reads == []
    assert "unreadable" in capsys.readouterr().out
    assert "upgrade_rejected" not in h.state()


# --- _sidecar_as_srt given bytes ---------------------------------------------------------------------

def test_sidecar_as_srt_given_bytes_never_reads_the_file(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    ghost = tmp_path / "gone" / "Movie.en.srt"  # does not exist
    out = cs._sidecar_as_srt(ghost, work, b"1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    assert _is_workfile(out, work, "sidecar") and out.read_bytes().startswith(b"1\n00:00")


def test_sidecar_as_srt_given_bytes_converts_ass_from_those_bytes(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    with patch.object(cs, "ass_to_srt", return_value=True) as conv:
        out = cs._sidecar_as_srt(tmp_path / "gone" / "Movie.en.ass", work, b"ASS BYTES")
    assert _is_workfile(out, work, "sidecar-srt")
    assert conv.call_args.args[0].read_bytes() == b"ASS BYTES"


def _is_workfile(p, work, role):
    """A unique, role-prefixed file directly inside `work` (see `_workfile`)."""
    return p.parent == work and p.name.startswith(f"{role}-") and p.is_file()


# --- the candidate set is exactly the verified snapshots ------------------------------------------

def test_verified_sidecar_removed_before_the_mux_is_still_muxed_from_its_bytes(tmp_path, monkeypatch, capsys):
    h = _upgrade_ready(tmp_path, monkeypatch)
    sidecar = tmp_path / "Movie (2024).en.srt"
    verified_text = cs.clean_srt_text(sidecar.read_text())
    late = tmp_path / "Movie (2024).en.cc.srt"
    note = tmp_path / "Movie (2024).nfo"
    note.write_text("not a subtitle")

    def _vanish():
        sidecar.unlink()
        late.write_text("arrived late")

    h.on_window = _once(_vanish)
    result = h.run()

    assert result["status"] == "FIXED" and result["detail"].startswith("UPGRADE generated->real ")
    assert h.remuxes[0]["content"] == verified_text
    assert h.remuxes[0]["eng_track_name"] == "English"
    assert h.state()["upgraded"] is True
    # only the file the pass owns was touched: the late arrival and the unrelated file are still there
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("Movie")) == [
        "Movie (2024).en.cc.srt", "Movie (2024).mkv", "Movie (2024).nfo"]
    assert late.read_text() == "arrived late"
    assert "UPGRADE: generated -> real subtitle (Movie (2024).en.srt)" in capsys.readouterr().out


def test_upgrade_candidates_are_never_rediscovered_after_verification(tmp_path, monkeypatch):
    """A longer, different sidecar turns up after verification; the full pass does not list the directory again."""
    h = _upgrade_ready(tmp_path, monkeypatch)
    verified_text = cs.clean_srt_text((tmp_path / "Movie (2024).en.srt").read_text())
    other, _ = _episode(2)
    h.on_window = _once(lambda: (tmp_path / "Movie (2024).eng.srt").write_text(
        other + "\n" + _srt([(2300.0, _dialogue(77, n=100))])))
    with patch.object(cs, "find_sidecar_subs", wraps=cs.find_sidecar_subs) as listing:
        assert h.run()["status"] == "FIXED"
    assert listing.call_count == 1  # the gate's; none from the full pass
    assert h.remuxes[0]["content"] == verified_text
    assert h.sidecars_left() == ["Movie (2024).eng.srt"]  # arrived late: left alone


def test_the_embedded_generated_track_is_not_a_candidate_in_an_upgrade(tmp_path, monkeypatch):
    h = _upgrade_ready(tmp_path, monkeypatch)
    h.extract_ok = False
    assert h.run()["status"] == "FIXED"
    assert h.extractions == []  # only a forced track is ever extracted, and there is none


def test_no_usable_verified_snapshot_aborts_and_never_muxes_the_generated_track(tmp_path, monkeypatch):
    h = _upgrade_ready(tmp_path, monkeypatch)
    real_write = Path.write_bytes

    def _failing(self, data):
        if self.name.startswith("verified"):
            raise OSError("disk full")
        return real_write(self, data)

    with patch.object(Path, "write_bytes", _failing):
        result = h.run()
    assert result["status"] == "SKIP"
    assert result["detail"].startswith("upgrade: no verified subtitle could be used")
    assert h.remuxes == [] and h.normalize_calls == []
    assert h.media.read_bytes() == MEDIA_BYTES
    assert h.sidecars_left() == ["Movie (2024).en.srt"]
    h.set_tag.assert_not_called()


# --- a conversion failure is a retry, not a verdict ----------------------------------------------------

def _good_ass_to_srt(srt_text):
    def _convert(src, dst):
        Path(dst).write_text(srt_text)
        return True
    return _convert


@pytest.mark.parametrize("failure", ["returns-false", "oserror", "unexpected-error"])
def test_an_ass_conversion_failure_is_retried_never_remembered(tmp_path, monkeypatch, failure):
    right, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.ass", "[Script Info]")], asr=asr)
    effect = {"returns-false": dict(return_value=False),
              "oserror": dict(side_effect=OSError("pysubs2 crashed")),
              "unexpected-error": dict(side_effect=RuntimeError("boom"))}[failure]
    with patch.object(cs, "ass_to_srt", **effect):
        result = h.run()
    assert result["status"] == "SKIP" and h.remuxes == []
    assert h.sidecars_left() == ["Movie (2024).en.ass"]
    assert "upgrade_rejected" not in h.state()

    with patch.object(cs, "ass_to_srt", side_effect=_good_ass_to_srt(right)):
        retry = h.run()  # next scan, the converter works
    assert retry["status"] == "FIXED" and h.remuxes[0]["eng_track_name"] == "English"


@pytest.mark.parametrize("failure", ["ffmpeg-fails", "ffmpeg-times-out"])
def test_a_vtt_conversion_failure_is_retried_never_remembered(tmp_path, monkeypatch, failure):
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.vtt", "WEBVTT")], asr=_episode(1)[1])
    effect = {"ffmpeg-fails": dict(return_value=type("R", (), {"returncode": 1})()),
              "ffmpeg-times-out": dict(side_effect=subprocess.TimeoutExpired("ffmpeg", 60))}[failure]
    with patch.object(cs.subprocess, "run", **effect):
        result = h.run()
    assert result["status"] == "SKIP" and h.remuxes == []
    assert h.sidecars_left() == ["Movie (2024).en.vtt"]
    assert "upgrade_rejected" not in h.state()


# --- _sidecar_as_srt --------------------------------------------------------------------

def test_sidecar_as_srt_copies_an_srt_into_the_workdir(tmp_path):
    src = tmp_path / "Movie.en.srt"
    src.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    work = tmp_path / "work"
    work.mkdir()
    out = cs._sidecar_as_srt(src, work)
    assert _is_workfile(out, work, "sidecar") and out.read_text() == src.read_text()
    assert out.name != src.name


def test_sidecar_as_srt_converts_ass_and_reports_a_failed_conversion(tmp_path):
    src = tmp_path / "Movie.en.ass"
    src.write_text("x")
    work = tmp_path / "work"
    work.mkdir()
    with patch.object(cs, "ass_to_srt", return_value=True) as conv:
        out = cs._sidecar_as_srt(src, work)
    raw, converted = conv.call_args.args
    assert out == converted and _is_workfile(converted, work, "sidecar-srt")
    assert _is_workfile(raw, work, "sidecar") and raw != converted
    with patch.object(cs, "ass_to_srt", return_value=False):
        assert cs._sidecar_as_srt(src, work) is None


@pytest.mark.parametrize("rc, ok", [(0, True), (1, False)])
def test_sidecar_as_srt_converts_vtt_with_ffmpeg(tmp_path, rc, ok):
    src = tmp_path / "Movie.en.vtt"
    src.write_text("WEBVTT")
    work = tmp_path / "work"
    work.mkdir()
    with patch.object(cs.subprocess, "run",
                      return_value=type("R", (), {"returncode": rc})()) as run:
        out = cs._sidecar_as_srt(src, work)
    assert (out is not None and _is_workfile(out, work, "sidecar-srt")) is ok
    assert run.call_args.args[0][0] == "ffmpeg"


# --- _generated_upgrade_check, called directly ------------------------------------------------------

def test_check_hands_back_the_probe_so_the_caller_does_not_probe_twice(tmp_path, monkeypatch):
    h = _upgrade_ready(tmp_path, monkeypatch)
    cur = cs.file_key(h.media)
    with patch.object(cs, "probe", side_effect=h._probe), \
         patch.object(sv, "embedded_text_window", side_effect=h._window):
        upgrade, info, rejected = cs._generated_upgrade_check(h.media, cur, None)
    assert upgrade is not None and len(upgrade.sidecars) == 1
    assert info is h.info and rejected is None
    assert len(h.probes) == 1


def test_check_without_a_real_sidecar_does_no_io_beyond_the_listing(tmp_path, monkeypatch):
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.generated.srt", "x")])
    with patch.object(cs, "probe") as probe, patch.object(sv, "embedded_text_window") as win:
        assert cs._generated_upgrade_check(h.media, (1, 1), None) == (None, None, None)
    probe.assert_not_called()
    win.assert_not_called()


def test_check_reports_a_rejection_fingerprint_only_for_a_judged_sidecar(tmp_path, monkeypatch):
    wrong, _ = _episode(2)
    _, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", wrong)], asr=asr)
    cur = cs.file_key(h.media)
    with patch.object(cs, "probe", side_effect=h._probe), \
         patch.object(sv, "embedded_text_window", side_effect=h._window):
        upgrade, info, rejected = cs._generated_upgrade_check(h.media, cur, None)
    assert upgrade is None and info is h.info
    assert [r[0] for r in rejected["sidecars"]] == ["Movie (2024).en.srt"]
    assert rejected["video"] == list(cs._identity(h.media))
    h.window_failure = True
    with patch.object(cs, "probe", side_effect=h._probe), \
         patch.object(sv, "embedded_text_window", side_effect=h._window):
        assert cs._generated_upgrade_check(h.media, cur, None)[2] is None  # unreadable: not remembered


# --- hand-off to the late-sidecar sweep (tests/test_sub_quarantine.py has the sweep) ---------------
# A sidecar rejected as the wrong episode is quarantined as "rejected upgrade: <why>"; an undecided one is kept.

def _quarantine_reasons(h):
    log = h.tmp_path / "sub-quarantine.jsonl"
    return [json.loads(line)["reason"] for line in log.read_text().splitlines()] if log.exists() else []


def test_a_sidecar_the_upgrade_rejected_as_the_wrong_episode_is_quarantined(tmp_path, monkeypatch):
    wrong, _ = _episode(2)
    _, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", wrong)], asr=asr, handoff=True)
    result = h.run()
    assert result["status"] == "SKIP"
    assert result["detail"] == "tag present; quarantined 1 redundant sidecar(s)"
    assert h.remuxes == [] and h.sidecars_left() == [] and len(h.quarantined()) == 1
    (reason,) = _quarantine_reasons(h)
    assert reason.startswith("rejected upgrade: not this file's subtitle")
    assert h.media.read_bytes() == b"\x1a\x45\xdf\xa3 original"  # the video is never touched


def test_a_remembered_rejection_hands_off_with_no_new_verification_or_probe(tmp_path, monkeypatch):
    wrong, _ = _episode(2)
    _, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", wrong)], asr=asr)  # hand-off off
    assert h.run()["detail"] == "tag present"          # judged wrong, remembered, left (for now)
    assert h.sidecars_left() == ["Movie (2024).en.srt"]
    h.enable_handoff()
    h.window_reads.clear()
    h.probes.clear()

    second = h.run()
    assert second["detail"] == "state cache; quarantined 1 redundant sidecar(s)"
    assert h.window_reads == [] and h.probes == []     # decided from the remembered record alone
    assert h.sidecars_left() == [] and len(h.quarantined()) == 1
    (reason,) = _quarantine_reasons(h)
    assert reason.startswith("rejected upgrade: not this file's subtitle")


def test_a_sidecar_the_content_gate_accepts_is_never_quarantined_for_failing_the_upgrades_language_test(
        tmp_path, monkeypatch):
    """The upgrade's language test is stricter than the content gate: "la la la" and short unlisted-language text are kept."""
    la = _srt([(600.0, ["la la la la la la la la la la"] * 6)])
    filler = " ".join(["habari", "gani", "rafiki", "yangu", "leo", "ni", "siku", "njema"] * 5)
    swahili = _srt([(600.0, [filler])])
    for n, text in enumerate((la, swahili)):
        case = tmp_path / f"case{n}"
        case.mkdir()
        _, asr = _episode(1)
        h = Harness(case, monkeypatch, sidecars=[(".en.srt", text)], asr=asr, handoff=True)
        assert cs.sublang.score(text)["verdict"] != "eng"           # the upgrade declines it...
        assert cs.classify_candidate(text)[0] is True               # ...the gate accepts it
        assert h.run()["detail"] == "tag present"
        assert h.sidecars_left() == ["Movie (2024).en.srt"] and h.quarantined() == []
        assert h.window_reads == []
        assert h.state()["upgrade_rejected"].get("reasons") in (None, {})   # no verdict recorded
        assert h.run()["detail"] == "state cache"                   # remembered, still kept
        assert h.sidecars_left() == ["Movie (2024).en.srt"]


def test_a_sidecar_the_upgrade_could_not_decide_is_kept(tmp_path, monkeypatch, capsys):
    right, asr = _episode(1)
    # transient: the generated track's cues cannot be read -> not remembered, retried
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", right)], asr=asr,
                window_failure=True, handoff=True)
    assert h.run()["detail"] == "tag present"
    assert h.sidecars_left() == ["Movie (2024).en.srt"] and h.quarantined() == []
    assert "kept: possible upgrade for generated subtitle" in capsys.readouterr().out


def test_too_little_text_is_not_a_verdict_and_the_sidecar_stays(tmp_path, monkeypatch):
    tiny = _srt([(600.0, ["You know that"])])
    _, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", tiny)], asr=asr, handoff=True)
    assert h.run()["detail"] == "tag present"
    assert h.state()["upgrade_rejected"]               # remembered...
    assert h.sidecars_left() == ["Movie (2024).en.srt"] and h.quarantined() == []   # ...but undecided
    h.window_reads.clear()
    assert h.run()["detail"] == "state cache" and h.sidecars_left() == ["Movie (2024).en.srt"]


def test_a_correct_but_partial_sidecar_is_undecided_not_the_wrong_episode(tmp_path, monkeypatch):
    """A window with no sidecar text is no evidence against it, and one matching window is no verdict: kept."""
    _, asr = _episode(1)
    partial = _srt([(WINDOW_STARTS[0], _dialogue(10))])         # the first window's own text only
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", partial)], asr=asr, handoff=True)
    assert h.run()["detail"] == "tag present"
    assert h.sidecars_left() == ["Movie (2024).en.srt"] and h.quarantined() == []
    record = h.state()["upgrade_rejected"]
    assert record["reasons"] == {}                              # judged, but no verdict on it
    assert h.run()["detail"] == "state cache"                   # remembered, still kept
    assert h.sidecars_left() == ["Movie (2024).en.srt"] and h.quarantined() == []


def test_a_sidecar_with_text_in_two_windows_that_does_not_match_is_the_wrong_episode(
        tmp_path, monkeypatch):
    """Text on both sides in two windows and no match is a wrong-episode verdict, though the third has no sidecar text."""
    _, asr = _episode(1)
    wrong = _srt([(WINDOW_STARTS[0], _dialogue(20)), (WINDOW_STARTS[1], _dialogue(21))])
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", wrong)], asr=asr, handoff=True)
    assert h.run()["detail"] == "tag present; quarantined 1 redundant sidecar(s)"
    assert h.sidecars_left() == [] and len(h.quarantined()) == 1
    (reason,) = _quarantine_reasons(h)
    assert reason.startswith("rejected upgrade: not this file's subtitle")


def test_a_wrong_sidecar_with_text_in_only_one_window_is_undecided(tmp_path, monkeypatch):
    """One window of evidence cannot make a verdict either way."""
    _, asr = _episode(1)
    wrong = _srt([(WINDOW_STARTS[1], _dialogue(21))])
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", wrong)], asr=asr, handoff=True)
    assert h.run()["detail"] == "tag present"
    assert h.sidecars_left() == ["Movie (2024).en.srt"] and h.quarantined() == []
    assert h.state()["upgrade_rejected"]["reasons"] == {}


def test_a_utf16_sidecar_is_verified_and_muxed_as_the_decoded_utf8_text(tmp_path, monkeypatch):
    """The working copy the pass muxes and syncs is the decoded text as UTF-8, never the UTF-16 bytes."""
    srt_text, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, asr=asr)
    sidecar = tmp_path / "Movie (2024).en.srt"
    sidecar.write_bytes(srt_text.encode("utf-16"))
    result = h.run()
    assert result["status"] == "FIXED" and result["detail"].startswith("UPGRADE generated->real ")
    assert h.remuxes[0]["content"] == srt_text                  # decoded, UTF-8, no BOM, no NULs
    assert "\x00" not in h.sync_inputs[0] and "\ufeff" not in h.sync_inputs[0]
    assert h.sidecars_left() == []                              # the verified original is consumed


def test_a_sidecar_that_is_not_text_is_not_verified_and_stays(tmp_path, monkeypatch):
    _, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, asr=asr)
    sidecar = tmp_path / "Movie (2024).en.srt"
    sidecar.write_bytes(bytes(range(256)) * 20)
    assert h.run()["detail"] == "tag present"
    assert h.remuxes == [] and h.window_reads == []
    assert h.sidecars_left() == ["Movie (2024).en.srt"]
    assert h.state()["upgrade_rejected"].get("reasons") in (None, {})   # no verdict recorded


# --- per-sidecar independence ----------------------------------------------------------
# One sidecar's transient failure (an ASS conversion) must not block another's verdict; it stays retryable.

_B_ASS = ("[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
          "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
          "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,You know the answer to that one\n")
_A = "Movie (2024).en.srt"
_B = "Movie (2024).en.ass"


def _two_sidecars(tmp_path, monkeypatch, **kw):
    wrong, _ = _episode(2)
    _, asr = _episode(1)
    return Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", wrong), (".en.ass", _B_ASS)],
                   asr=asr, **kw)


def test_a_wrong_episode_verdict_survives_another_sidecars_transient_failure(tmp_path, monkeypatch):
    h = _two_sidecars(tmp_path, monkeypatch, handoff=True)
    with patch.object(cs, "ass_to_srt", return_value=False) as conv:
        result = h.run()

    assert result["detail"] == "tag present; quarantined 1 redundant sidecar(s)"
    assert conv.call_count == 1                                  # B was tried and failed
    assert h.sidecars_left() == [_B]                             # B kept (undecided), A quarantined
    (reason,) = _quarantine_reasons(h)
    assert reason.startswith("rejected upgrade: not this file's subtitle")
    record = h.state()["upgrade_rejected"]
    assert [e[0] for e in record["sidecars"]] == [_A]            # the record speaks for A only...
    assert list(record["reasons"]) == [_A]                       # ...B's failure is not remembered


def test_the_failed_sidecar_is_retried_on_the_next_scan_and_the_verdict_is_not_reread(
        tmp_path, monkeypatch):
    h = _two_sidecars(tmp_path, monkeypatch, handoff=False)       # leave A on disk for now
    with patch.object(cs, "ass_to_srt", return_value=False) as conv:
        assert h.run()["detail"] == "tag present"
    assert conv.call_count == 1 and h.sidecars_left() == sorted([_A, _B])
    record = h.state()["upgrade_rejected"]
    assert [e[0] for e in record["sidecars"]] == [_A] and list(record["reasons"]) == [_A]

    h.enable_handoff()
    h.window_reads.clear()
    h.probes.clear()
    with patch.object(cs, "ass_to_srt", return_value=False) as conv:
        second = h.run()

    assert second["detail"] == "state cache; quarantined 1 redundant sidecar(s)"
    assert conv.call_count == 1                                  # B is tried again (still transient)
    assert h.window_reads == []                                  # A's remembered verdict: no window read
    assert len(h.probes) == 1                                    # one probe, for B's retry; the sweep reuses it
    assert h.sidecars_left() == [_B]
    (reason,) = _quarantine_reasons(h)
    assert reason.startswith("rejected upgrade: not this file's subtitle")


def test_once_the_failed_sidecar_converts_it_is_judged_too(tmp_path, monkeypatch):
    h = _two_sidecars(tmp_path, monkeypatch, handoff=False)
    with patch.object(cs, "ass_to_srt", return_value=False):
        h.run()
    assert [e[0] for e in h.state()["upgrade_rejected"]["sidecars"]] == [_A]

    def _converts(ass_path, srt_path):
        Path(srt_path).write_text(_srt([(600.0, ["You know that"])]))   # too little to decide
        return True

    with patch.object(cs, "ass_to_srt", side_effect=_converts):
        h.run()
    record = h.state()["upgrade_rejected"]
    assert sorted(e[0] for e in record["sidecars"]) == sorted([_A, _B])   # now both are on record
    assert list(record["reasons"]) == [_A]                              # B undecided: no verdict
    h.probes.clear()
    h.window_reads.clear()
    assert h.run()["detail"] == "state cache" and h.window_reads == [] and h.probes == []


def test_a_changed_sidecar_loses_only_its_own_remembered_verdict(tmp_path, monkeypatch):
    h = _two_sidecars(tmp_path, monkeypatch, handoff=False)
    with patch.object(cs, "ass_to_srt", return_value=False):
        h.run()
    (tmp_path / _B).write_text(_B_ASS + "\n")                          # B changes; A does not
    h.enable_handoff()
    h.window_reads.clear()
    with patch.object(cs, "ass_to_srt", return_value=False):
        result = h.run()
    assert result["detail"] == "state cache; quarantined 1 redundant sidecar(s)"   # A still quarantined
    assert h.window_reads == [] and h.sidecars_left() == [_B]


def test_when_the_upgrade_applies_the_sweep_never_runs(tmp_path, monkeypatch):
    srt_text, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", srt_text)], asr=asr, handoff=True)
    with patch.object(cs, "_late_sidecar_sweep", wraps=cs._late_sidecar_sweep) as sweep:
        result = h.run()
    assert result["status"] == "FIXED" and "UPGRADE generated->real" in result["detail"]
    sweep.assert_not_called()
    assert h.quarantined() == []


def test_when_the_upgrade_does_not_apply_the_sweep_runs_once(tmp_path, monkeypatch):
    wrong, _ = _episode(2)
    _, asr = _episode(1)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", wrong)], asr=asr, handoff=True)
    with patch.object(cs, "_late_sidecar_sweep", wraps=cs._late_sidecar_sweep) as sweep:
        h.run()
    assert sweep.call_count == 1


def _record_for(h, reasons):
    sidecar = h.tmp_path / "Movie (2024).en.srt"
    snap = cs._read_snapshot(sidecar)
    fp = cs._sidecar_fingerprint({sidecar: snap})
    return sidecar, snap, cs._rejection_record(cs._identity(h.media), fp, reasons)


def test_rejection_reasons_only_apply_while_the_record_still_holds(tmp_path, monkeypatch):
    wrong, _ = _episode(2)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", wrong)], handoff=True)
    sidecar, snap, record = _record_for(h, {"Movie (2024).en.srt": "why"})
    listing = [(sidecar, False, False)]
    reasons = lambda rec, snaps=None: cs._upgrade_rejection_reasons(   # noqa: E731
        h.media, snaps if snaps is not None else {sidecar: snap}, listing, rec)

    assert reasons(record) == {"Movie (2024).en.srt": "why"}
    assert reasons(None) is None and reasons([["a", 1, "x"]]) is None      # no / old-shape record
    assert reasons({**record, "video": record["video"][:3]}) is None       # legacy 3-field identity
    assert reasons(record, {sidecar: None}) == {}                          # that sidecar unreadable: no verdict for it
    no_reasons = {k: v for k, v in record.items() if k != "reasons"}
    assert reasons(no_reasons) is None                                     # older format: invalid
    assert reasons({**record, "reasons": {}}) == {}                        # holds, but no verdict

    other = cs._rejection_record(cs._identity(h.media), [["Movie (2024).en.srt", 1, "0" * 64]],
                                 {"Movie (2024).en.srt": "why"})
    assert reasons(other) == {}                                            # its content differs: no verdict for it
    time.sleep(0.02)                                                       # past ctime granularity
    h.media.write_bytes(h.media.read_bytes())                               # video rewritten (ctime moves)
    assert reasons(record) is None                                         # video identity differs


def test_a_generated_or_forced_sidecar_never_takes_the_rejected_upgrade_path(tmp_path, monkeypatch):
    wrong, _ = _episode(2)
    h = Harness(tmp_path, monkeypatch, sidecars=[(".en.srt", wrong)], handoff=True)
    sidecar, snap, record = _record_for(h, {"Movie (2024).en.srt": "why"})
    gen = h.tmp_path / "Movie (2024).en.generated.srt"
    gen.write_text(wrong)
    forced = h.tmp_path / "Movie (2024).en.forced.srt"
    forced.write_text(wrong)
    listing = [(sidecar, False, False), (gen, False, False), (forced, True, False)]
    # only the real main sidecar is in the set the record covers
    assert cs._upgrade_rejection_reasons(
        h.media, {sidecar: snap, gen: cs._read_snapshot(gen), forced: cs._read_snapshot(forced)},
        listing, record) == {"Movie (2024).en.srt": "why"}
