"""`sub-fallback.py`, the hourly ASR fallback: Popen, the model plumbing and `JellyfinClient` are mocked and every var/ path is under `tmp_path`."""

import fcntl
import importlib.util
import itertools
import json
import multiprocessing
import os
import stat
import sys
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from media_stack.subverify import ExtractionInterrupted

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "sub_fallback", str(PROJECT_ROOT / "sub-fallback.py")
)
sf = importlib.util.module_from_spec(_spec)
# Config is a dataclass under `from __future__ import annotations` (resolved via sys.modules), so register the module before exec_module.
sys.modules[_spec.name] = sf
_spec.loader.exec_module(sf)

# Captured before the autouse `_assume_revalidation_passes` fixture stubs it; the revalidation tests restore it.
_REAL_REVALIDATE_CANDIDATE = sf._revalidate_candidate


_MKV = b"\x1a\x45\xdf\xa3"
_PROBED = {"streams": [], "format": {}}
_DURATION_INFO = {"streams": [], "format": {"duration": "1200"}}
_BUSY = [{"Id": "a", "NowPlayingItem": {}}]
_HI_SRT = "1\n00:00:00,000 --> 00:00:02,000\nHi.\n"
_SHORT_INFO = {"streams": [], "format": {"duration": "60"}}
_ENG_SCORE = {"verdict": "eng", "eng": 0.45}
_PLACED = {"_place_sidecar": {"return_value": None}, "wait_for_pipeline": {"return_value": (True, 12)}}
_NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


def _media(tmp_path: Path, name: str = "Episode S01E01.mkv") -> Path:
    p = tmp_path / name
    p.write_bytes(_MKV)
    return p


@pytest.fixture(autouse=True)
def _isolate_runtime_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(sf, "NEEDS_ENGLISH_STATE_FILE", tmp_path / "needs-english.json")
    monkeypatch.setattr(sf, "LOCK_FILE", tmp_path / "sub-fallback.lock")
    monkeypatch.setattr(sf, "JSONL_FILE", tmp_path / "sub-fallback.jsonl")
    monkeypatch.setattr(sf, "WORKDIR_ROOT", tmp_path / "workdir-root")
    monkeypatch.setattr(sf, "PIPELINE_POLL_S", 0)


@pytest.fixture(autouse=True)
def _assume_revalidation_passes(monkeypatch):
    """Default `_revalidate_candidate` to "still valid" so unrelated `process_candidate` tests need no tracker entry; the revalidation tests restore `_REAL_REVALIDATE_CANDIDATE`."""
    monkeypatch.setattr(sf, "_revalidate_candidate", lambda video, cfg: None)


def _cfg(**overrides) -> "sf.Config":
    base = dict(
        jellyfin_url="http://jf:8096", jellyfin_key="realkey",
        age_hours=24.0, budget_min=50.0, pipeline_timeout_min=60.0,
        max_wait_min=30.0, asr_timeout_min=90.0,
        whisper_model="medium.en", whisper_verify_model="small.en",
        whisper_lid_model="small", whisper_threads=6,
    )
    base.update(overrides)
    return sf.Config(**base)


def _budget(max_wait_min: float = 30.0) -> "sf.PlaybackWaitBudget":
    """A fresh per-candidate playback-wait budget, as `process_candidate` creates."""
    return sf.PlaybackWaitBudget(max_wait_min)


class _FakeJellyfin:
    """Scripted now_playing_sessions: each call returns the next of list / [] / None."""

    def __init__(self, sequence):
        self._seq = list(sequence)

    def now_playing_sessions(self, key):
        if not self._seq:
            raise AssertionError("now_playing_sessions called more times than scripted")
        return self._seq.pop(0)


class _FakeClock:
    """`monotonic()`/`sleep()` pair where sleep(s) advances the clock by exactly s, independent of how many times the code reads the clock per poll."""

    def __init__(self, start: float = 0.0):
        self.t = start

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


# --- _parse_iso: both timestamp formats ----------------------------------

# `_utc_now_iso()` writes microseconds with `+00:00` (Python 3.10's `fromisoformat` rejects `Z`); old entries use `Z`.
@pytest.mark.parametrize("text, expected", [
    ("2026-10-13T00:00:00Z", datetime(2026, 10, 13, tzinfo=timezone.utc)),
    ("2026-10-13T17:22:05.123456+00:00", datetime(2026, 10, 13, 17, 22, 5, 123456, tzinfo=timezone.utc)),
    ("not-a-date", None),
], ids=["old-whole-second-z", "new-microsecond-offset", "garbage"])
def test_parse_iso_accepts_both_formats_and_rejects_garbage(text, expected):
    assert sf._parse_iso(text) == expected


# --- select_candidates: age gate + stale dropping ------

def _select(state, *, exists=True, consolidated=False):
    return sf.select_candidates(
        state, _NOW, age_hours=24.0,
        file_exists=lambda p: exists, is_consolidated=lambda p: consolidated)


def test_select_candidates_age_gate_excludes_recent_entries():
    candidates, stale = _select({
        "/media/old.mkv": {"first_seen": "2026-10-10T00:00:00Z"},
        "/media/new.mkv": {"first_seen": "2026-10-13T00:00:00Z"},
    })
    assert candidates == [Path("/media/old.mkv")]
    assert stale == []


@pytest.mark.parametrize("state, kwargs, expected_stale", [
    ({"/media/gone.mkv": {"first_seen": "2026-10-01T00:00:00Z"}}, {"exists": False}, ["/media/gone.mkv"]),
    ({"/media/done.mkv": {"first_seen": "2026-10-01T00:00:00Z"}}, {"consolidated": True}, ["/media/done.mkv"]),
    ({"/media/bad.mkv": {}, "/media/bad2.mkv": {"first_seen": "not-a-date"}}, {},
     ["/media/bad.mkv", "/media/bad2.mkv"]),
], ids=["file-gone", "already-consolidated", "malformed-first-seen"])
def test_select_candidates_drops_stale_entries(state, kwargs, expected_stale):
    candidates, stale = _select(state, **kwargs)
    assert candidates == []
    assert sorted(stale) == expected_stale


def test_select_candidates_sorts_oldest_first():
    candidates, _ = _select({
        "/media/b.mkv": {"first_seen": "2026-10-02T00:00:00Z"},
        "/media/a.mkv": {"first_seen": "2026-10-01T00:00:00Z"},
    })
    assert candidates == [Path("/media/a.mkv"), Path("/media/b.mkv")]


# --- load_needs_english: missing / empty / corrupt -----------

@pytest.mark.parametrize("content, expected", [
    (None, {}),
    (json.dumps({"/a.mkv": {"first_seen": "x", "last_seen": "y"}}),
     {"/a.mkv": {"first_seen": "x", "last_seen": "y"}}),
    ("{not valid json", None),
], ids=["missing-file", "valid", "corrupt"])
def test_load_needs_english(tmp_path, content, expected):
    p = tmp_path / "needs.json"
    if content is not None:
        p.write_text(content)
    assert sf.load_needs_english(p) == expected


# --- drop_stale_entries: conditional removal against the live tracker ----

def test_drop_stale_entries_removes_only_named_keys():
    state_file = sf.NEEDS_ENGLISH_STATE_FILE
    state_file.write_text(json.dumps({
        "/a.mkv": {"first_seen": "x"}, "/b.mkv": {"first_seen": "y"},
    }))
    removed = sf.drop_stale_entries({"/a.mkv": {"first_seen": "x"}})
    assert removed == 1
    assert json.loads(state_file.read_text()) == {"/b.mkv": {"first_seen": "y"}}


def test_drop_stale_entries_empty_dict_is_noop():
    assert sf.drop_stale_entries({}) == 0
    assert not sf.NEEDS_ENGLISH_STATE_FILE.exists()


def test_drop_stale_entries_keeps_an_entry_re_recorded_since_the_snapshot():
    """A fresh entry re-recorded after the stale snapshot is kept: removal compares against what is on disk."""
    state_file = sf.NEEDS_ENGLISH_STATE_FILE
    snapshot_entry = {"first_seen": "2026-10-01T00:00:00Z", "last_seen": "2026-10-01T00:00:00Z"}
    fresh_entry = {"first_seen": "2026-10-13T12:00:00Z", "last_seen": "2026-10-13T12:00:00Z"}
    # The snapshot said /a.mkv was stale, but consolidate-subs.py re-recorded it before removal.
    state_file.write_text(json.dumps({
        "/a.mkv": fresh_entry, "/b.mkv": {"first_seen": "y"},
    }))
    removed = sf.drop_stale_entries({"/a.mkv": snapshot_entry, "/b.mkv": {"first_seen": "y"}})
    assert removed == 1  # only /b.mkv -- /a.mkv's current entry didn't match
    assert json.loads(state_file.read_text()) == {"/a.mkv": fresh_entry}


# --- playback gate: the logic lives in media_stack.playback (tests/test_playback.py); here only the adapters' wiring ---

def test_playback_adapters_bind_cfg_and_module_constants_into_the_shared_helpers():
    jf, cfg, budget = _FakeJellyfin([]), _cfg(jellyfin_key="k", max_wait_min=7.0), _budget(7.0)
    with patch.object(sf, "_shared_wait_while_playing") as shared_wait:
        sf.wait_while_playing(jf, cfg, budget)
    shared_wait.assert_called_once_with(jf, "k", 7.0, budget, poll_s=sf.PLAYBACK_POLL_S, log=sf.log)

    def fn(*args, should_abort, **kwargs):
        raise AssertionError("never called: the shared helper is mocked")
    with patch.object(sf, "call_with_playback_pause", return_value="ok") as shared_call:
        assert sf._call_with_playback_pause(fn, jf, cfg, budget, "a", kw=1) == "ok"
    shared_call.assert_called_once_with(
        fn, jf, "k", 7.0, budget, "a", poll_s=sf.PLAYBACK_POLL_S, log=sf.log, kw=1)


def test_call_with_playback_pause_retries_share_cumulative_budget(monkeypatch):
    """Through the adapter: retries within one call each clear under the allowance but exhaust the shared budget on the third (180 s = 3 x 60 s `PLAYBACK_POLL_S`)."""
    cfg = _cfg(max_wait_min=3.0)  # 180s total allowance
    budget = _budget(cfg.max_wait_min)
    clock = _FakeClock()
    monkeypatch.setattr(sf.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(sf.time, "sleep", clock.sleep)

    jf = _FakeJellyfin([_BUSY, [], _BUSY, [], _BUSY, []])

    attempts = {"n": 0}

    def _fn(*a, should_abort):
        attempts["n"] += 1
        raise ExtractionInterrupted("playback")

    with pytest.raises(sf.PlaybackWaitTimeout):
        sf._call_with_playback_pause(_fn, jf, cfg, budget)
    assert attempts["n"] == 3  # the 3rd retry's wait is what finally exhausts it


# --- wait_for_pipeline: recognizes an mp4->mkv replacement ---------------

def test_wait_for_pipeline_succeeds_on_original_path(tmp_path):
    video = _media(tmp_path, "Episode.mkv")
    with patch.object(sf, "probe", return_value={"streams": [], "format": {}}), \
         patch.object(sf, "already_processed", return_value=True):
        reached, _waited = sf.wait_for_pipeline(video, timeout_s=5.0)
    assert reached is True


def test_wait_for_pipeline_succeeds_when_mp4_was_replaced_by_mkv(tmp_path):
    """An .mp4 replaced by a same-stem .mkv is credited via the .mkv once the original is really gone (`video.exists()`, not `probe`)."""
    video = _media(tmp_path, "Episode.mp4")
    mkv_alt = video.with_suffix(".mkv")
    video.unlink()  # simulates the real mp4->mkv swap's unlink
    mkv_alt.write_bytes(b"\x1a\x45\xdf\xa3")

    def _fake_probe(p):
        if Path(p) == video:
            return None
        if Path(p) == mkv_alt:
            return {"streams": [], "format": {}}
        raise AssertionError(f"unexpected probe target: {p}")

    with patch.object(sf, "probe", side_effect=_fake_probe), \
         patch.object(sf, "already_processed", return_value=True):
        reached, _waited = sf.wait_for_pipeline(video, timeout_s=5.0)
    assert reached is True


def test_wait_for_pipeline_does_not_credit_mkv_sibling_while_mp4_still_exists(tmp_path):
    """A separate `<stem>.mkv` beside a still-present `<stem>.mp4` is a sibling collision; its tag is never credited to the .mp4."""
    video = _media(tmp_path, "Episode.mp4")
    mkv_alt = video.with_suffix(".mkv")
    mkv_alt.write_bytes(b"\x1a\x45\xdf\xa3")  # the .mp4 is NOT removed

    def _fake_probe(p):
        if Path(p) == video:
            return {"tag": "mp4-untagged"}
        if Path(p) == mkv_alt:
            return {"tag": "mkv-tagged"}
        raise AssertionError(f"unexpected probe target: {p}")

    def _fake_already_processed(info):
        return info.get("tag") == "mkv-tagged"

    with patch.object(sf, "probe", side_effect=_fake_probe), \
         patch.object(sf, "already_processed", side_effect=_fake_already_processed):
        reached, _waited = sf.wait_for_pipeline(video, timeout_s=0.05)
    assert reached is False


def test_wait_for_pipeline_times_out_when_neither_path_is_tagged(tmp_path):
    video = _media(tmp_path, "Episode.mp4")
    with patch.object(sf, "probe", return_value=None):
        reached, _waited = sf.wait_for_pipeline(video, timeout_s=0.05)
    assert reached is False


def _consolidate_lock_holder(media_path_str: str, hold_seconds: float, ready_event) -> None:
    """Hold the real per-file lock on `media_path_str`, as consolidate-subs.py's inline normalization does after writing the tag."""
    import time as _time
    from media_stack.locking import acquire_file_lock as afl
    media = Path(media_path_str)
    with afl(media) as acquired:
        if acquired:
            ready_event.set()
            _time.sleep(hold_seconds)


def test_wait_for_pipeline_keeps_waiting_while_lock_is_held(tmp_path):
    """The tag lands before inline normalization, which holds the per-file lock: a tagged file is not credited while the lock is held."""
    video = _media(tmp_path, "Episode.mkv")
    ready = multiprocessing.Event()
    p = multiprocessing.Process(target=_consolidate_lock_holder, args=(str(video), 2, ready))
    p.start()
    try:
        assert ready.wait(timeout=3), "holder didn't acquire the lock"
        with patch.object(sf, "probe", return_value={"streams": [], "format": {}}), \
             patch.object(sf, "already_processed", return_value=True):
            reached, _waited = sf.wait_for_pipeline(video, timeout_s=0.2)
        assert reached is False
    finally:
        p.join(timeout=5)


# --- _place_sidecar: no-clobber placement --------------------------------

def _leftover_sfb_temps(dest: Path) -> list[Path]:
    """Every `_place_sidecar` temp file next to `dest` (names are unique per attempt, so glob)."""
    return sorted(dest.parent.glob(sf._sidecar_temp_glob(dest)))


def _place(tmp_path, video, dest, text="text", *, identity=None, rec=None,
           probe=_PROBED, processed=False, sidecars=(), extra=()):
    """Run `_place_sidecar` with probe, already_processed and discovery stubbed (`sidecars=None` keeps real discovery); `extra` are further patchers."""
    with ExitStack() as stack:
        stack.enter_context(patch.object(sf, "probe", return_value=probe))
        stack.enter_context(patch.object(sf, "already_processed", return_value=processed))
        if sidecars is not None:
            stack.enter_context(patch.object(sf, "find_sidecar_subs", return_value=list(sidecars)))
        for patcher in extra:
            stack.enter_context(patcher)
        return sf._place_sidecar(
            video, dest, text,
            video_identity=sf._video_identity(video) if identity is None else identity,
            pipeline_timeout_s=60.0, rec={} if rec is None else rec, workdir=tmp_path)


def _open_for_event_fails(dest):
    """`os.open` side effect failing only the O_APPEND open of `dest` that generates the post-link close_write event."""
    real_open = os.open

    def _fake_open(path, flags, *a, **kw):
        if Path(path) == dest and (flags & os.O_APPEND):
            raise OSError("transient I/O error")
        return real_open(path, flags, *a, **kw)
    return _fake_open


_CANDIDATE_BASES = {
    "asr": {  # audio explicitly English: straight to the subliminal / whisper steps
        "probe": {"return_value": _DURATION_INFO}, "already_processed": {"return_value": False},
        "english_audio_stream": {"return_value": 2}, "is_audio_explicitly_english": {"return_value": True},
        "fetch_via_subliminal": {"return_value": None}, "wait_while_playing": {},
    },
    "sidecar": {  # every action that must not run when a sidecar blocks or short-circuits the candidate
        "probe": {"return_value": _PROBED}, "already_processed": {"return_value": False},
        "_run_consolidate_subs_directly": {}, "fetch_via_subliminal": {},
        "_transcribe_in_subprocess": {}, "wait_while_playing": {},
    },
    "bare": {"wait_while_playing": {}},
}


def _candidate(tmp_path, *, video=None, base="asr", deadline_s=3000.0, cfg=None, extra=(), **patches):
    """Run `process_candidate` with the `sf` names in `_CANDIDATE_BASES[base]` plus `patches` stubbed (name -> `patch.object` kwargs; None drops a base entry); `extra` are further patchers. Returns `(rec, {name: mock})`."""
    video = video or _media(tmp_path)
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    stubs = {**_CANDIDATE_BASES[base], **patches}
    with ExitStack() as stack:
        mocks = {name: stack.enter_context(patch.object(sf, name, **kw))
                 for name, kw in stubs.items() if kw is not None}
        for patcher in extra:
            stack.enter_context(patcher)
        rec = sf.process_candidate(video, cfg or _cfg(), _FakeJellyfin([]), workdir,
                                   sf.time.monotonic() + deadline_s)
    return rec, mocks


def _assert_nothing_attempted(mocks):
    for name in ("_run_consolidate_subs_directly", "fetch_via_subliminal", "_transcribe_in_subprocess"):
        mocks[name].assert_not_called()


def test_place_sidecar_creates_file_with_complete_content(tmp_path):
    video = _media(tmp_path)
    dest = video.with_name(video.stem + ".en.srt")
    assert _place(tmp_path, video, dest, "subtitle content") is None
    assert dest.read_text() == "subtitle content"
    assert _leftover_sfb_temps(dest) == []


def test_place_sidecar_published_content_is_complete_from_first_moment(tmp_path):
    """The hard link publishes a temp file already fully written, fsynced and closed: `dest` never has partial content."""
    video = _media(tmp_path)
    dest = video.with_name(video.stem + ".en.srt")
    seen_tmp_content = {}
    real_link = os.link

    def _spy_link(src, dst):
        seen_tmp_content["content"] = Path(src).read_text()
        return real_link(src, dst)

    result = _place(tmp_path, video, dest, "COMPLETE TEXT",
                    extra=[patch.object(sf.os, "link", side_effect=_spy_link)])
    assert result is None
    assert seen_tmp_content["content"] == "COMPLETE TEXT"


def _add_mkv_sibling(video, dest):
    video.with_suffix(".mkv").write_bytes(_MKV)
    return {}


@pytest.mark.parametrize("expected, scenario", [
    ("ALREADY_CONSOLIDATED", lambda video, dest: {"processed": True}),
    ("VIDEO_CHANGED", lambda video, dest: {"identity": (999, 999, 1, 1)}),  # not the video's real identity
    ("SIBLING_COLLISION", _add_mkv_sibling),
], ids=["already-consolidated", "video-changed", "sibling-collision"])
def test_place_sidecar_blocking_result_places_nothing(tmp_path, expected, scenario):
    video = _media(tmp_path, "Episode.mp4")
    dest = video.with_name(video.stem + ".en.srt")
    assert _place(tmp_path, video, dest, **scenario(video, dest)) == expected
    assert not dest.exists()
    assert _leftover_sfb_temps(dest) == []


def test_place_sidecar_never_overwrites_existing_file_eexist(tmp_path):
    """os.link()'s EEXIST is the no-clobber guarantee, even when find_sidecar_subs missed the existing file."""
    video = _media(tmp_path)
    dest = video.with_name(video.stem + ".en.srt")
    dest.write_text("ORIGINAL CONTENT")
    assert _place(tmp_path, video, dest, "NEW CONTENT") == "SIDECAR_APPEARED"
    assert dest.read_text() == "ORIGINAL CONTENT"  # untouched
    assert _leftover_sfb_temps(dest) == []


def test_place_sidecar_place_failed_on_other_oserror_never_renames(tmp_path):
    """A non-EEXIST OSError from os.link() (e.g. EXDEV) is PLACE_FAILED, never a clobbering rename."""
    video = _media(tmp_path)
    dest = video.with_name(video.stem + ".en.srt")
    link_fails = patch.object(sf.os, "link", side_effect=OSError(18, "Invalid cross-device link"))
    assert _place(tmp_path, video, dest, extra=[link_fails]) == "PLACE_FAILED"
    assert not dest.exists()
    assert _leftover_sfb_temps(dest) == []


def test_place_sidecar_cleans_up_temp_on_write_failure(tmp_path):
    video = _media(tmp_path)
    dest = video.with_name(video.stem + ".en.srt")
    with pytest.raises(OSError):
        _place(tmp_path, video, dest, extra=[patch("os.fdopen", side_effect=OSError("disk full"))])
    assert not dest.exists()
    assert _leftover_sfb_temps(dest) == []


# --- _place_sidecar: unique-per-attempt temp name (never a fixed name) --


def test_place_sidecar_sweeps_unrelated_stale_temp_leftover_before_writing(tmp_path):
    """A stale unlinked temp from an earlier crash is swept and a normal placement proceeds."""
    video = _media(tmp_path)
    dest = video.with_name(video.stem + ".en.srt")
    stale = dest.with_name("." + dest.name + ".DEADATTEMPT" + sf._SIDECAR_TEMP_SUFFIX)
    stale.write_text("abandoned partial write")

    assert _place(tmp_path, video, dest) is None
    assert dest.read_text() == "text"
    assert not stale.exists()
    assert _leftover_sfb_temps(dest) == []


# --- _sibling_video_collision / SIBLING_COLLISION ------------------------

def test_sibling_video_collision_detects_mp4_mkv_pair(tmp_path):
    mp4 = _media(tmp_path, "Episode.mp4")
    mkv = tmp_path / "Episode.mkv"
    mkv.write_bytes(b"\x1a\x45\xdf\xa3")
    assert sf._sibling_video_collision(mp4) == mkv
    assert sf._sibling_video_collision(mkv) == mp4


@pytest.mark.parametrize("video_name, sibling_name", [
    ("Episode.mkv", None), ("Episode.mkv", "Episode.avi"), ("Episode.ts", "Episode.mkv"),
], ids=["no-sibling", "avi-is-outside-the-watcher-set", "video-outside-the-watcher-set"])
def test_sibling_video_collision_is_none(tmp_path, video_name, sibling_name):
    """consolidate-watch.sh only stem-matches `mkv mp4`, so anything else is not a collision."""
    video = _media(tmp_path, video_name)
    if sibling_name:
        (tmp_path / sibling_name).write_bytes(_MKV)
    assert sf._sibling_video_collision(video) is None


def test_process_candidate_skips_sibling_collision_before_anything_else(tmp_path):
    """Checked first, before even the playback wait."""
    video = _media(tmp_path, "Episode.mp4")
    (tmp_path / "Episode.mkv").write_bytes(_MKV)
    rec, mocks = _candidate(tmp_path, video=video, base="bare", probe={}, fetch_via_subliminal={},
                            _transcribe_in_subprocess={})
    assert rec["result"] == "SIBLING_COLLISION"
    for name in ("probe", "wait_while_playing", "fetch_via_subliminal", "_transcribe_in_subprocess"):
        mocks[name].assert_not_called()


# --- _place_sidecar: post-publication close_write event is recoverable ---


def test_event_open_failure_after_publish_resolves_to_fixed_not_error_through_process_candidate(tmp_path):
    """End to end: an event-open failure after a successful publish still resolves to FIXED once the tag lands."""
    video = _media(tmp_path)
    dest = video.with_name(video.stem + ".en.generated.srt")  # whisper source -> generated name
    rec, _ = _candidate(
        tmp_path, video=video, _transcribe_in_subprocess={"return_value": _HI_SRT},
        _run_consolidate_subs_directly={"return_value": (True, "ok")}, wait_for_pipeline={"return_value": (True, 5)},
        extra=[patch.object(sf.os, "open", side_effect=_open_for_event_fails(dest))])

    assert rec["result"] == "FIXED"
    assert rec.get("event_fallback_used") is True


# --- _video_identity -------------------------------------------------------


def test_video_identity_none_when_file_is_gone(tmp_path):
    video = _media(tmp_path)
    video.unlink()
    assert sf._video_identity(video) is None


def _rewrite_same_size_keeping_mtime(path):
    st = path.stat()
    time.sleep(0.02)
    with path.open("r+b") as fh:
        fh.write(b"\xff" * st.st_size)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    return st


def test_video_identity_includes_the_ctime_and_catches_a_same_size_rewrite(tmp_path):
    """Same size, mtime put back: dev/ino/size/mtime all look untouched, the ctime does not."""
    video = _media(tmp_path)
    before = sf._video_identity(video)
    assert len(before) == 5 and before[-1] == video.stat().st_ctime_ns
    _rewrite_same_size_keeping_mtime(video)
    after = sf._video_identity(video)
    assert before[:4] == after[:4] and before != after


# --- _revalidate_candidate / REVALIDATION_SKIP: process_candidate re-checks the current tracker entry right before processing ---

def _seed_tracker_entry(video: Path, *, hours_old: float = 30.0, identity=...) -> None:
    """Write a needs-english.json entry for `video`, first seen `hours_old` hours ago. `identity` defaults to the file's current one; a callable is given the video; None omits it."""
    first_seen = (datetime.now(timezone.utc) - timedelta(hours=hours_old)
                  ).isoformat(timespec="microseconds")
    if identity is ...:
        identity = sf._video_identity_sans_dev(video)
    elif callable(identity):
        identity = identity(video)
    entry = {"first_seen": first_seen, "last_seen": first_seen}
    if identity is not None:
        entry["identity"] = identity
    sf.NEEDS_ENGLISH_STATE_FILE.write_text(json.dumps({str(video.resolve()): entry}))


def _tracker_identity(video: Path, *, fields: int) -> list:
    """The video's tracker-style identity (3 fields: ino, size, mtime_ns; 4 adds ctime_ns)."""
    return list(sf._video_identity_sans_dev(video))[:fields]


def _revalidate(monkeypatch, video: Path, *, consolidated: bool = False):
    monkeypatch.setattr(sf, "_revalidate_candidate", _REAL_REVALIDATE_CANDIDATE)
    with patch.object(sf, "_has_consolidated_tag", return_value=consolidated):
        return sf._revalidate_candidate(video, _cfg(age_hours=24.0))


# The tracker identity is [ino, size, mtime_ns] (ctime would reset a wait on a hardlink change), compared on the shared fields.
@pytest.mark.parametrize("identity", [..., lambda v: _tracker_identity(v, fields=3), None],
                         ids=["current-four-field", "three-field", "no-identity-old-format"])
def test_revalidate_candidate_passes_for_a_valid_unchanged_entry(tmp_path, monkeypatch, identity):
    video = _media(tmp_path)
    _seed_tracker_entry(video, identity=identity)
    assert _revalidate(monkeypatch, video) is None


@pytest.mark.parametrize("seed, consolidated, reason", [
    ({"hours_old": 1.0}, False, "grace period"),  # first_seen was reset after selection (file replaced)
    ({"identity": [999999, 999999, 999999]}, False, "identity"),
    ({}, True, "consolidated"),
], ids=["first-seen-reset", "identity-mismatch", "already-consolidated"])
def test_revalidate_candidate_skips(tmp_path, monkeypatch, seed, consolidated, reason):
    video = _media(tmp_path)
    _seed_tracker_entry(video, **seed)
    result = _revalidate(monkeypatch, video, consolidated=consolidated)
    assert result is not None and reason in result


def test_revalidate_candidate_skips_when_entry_removed(tmp_path, monkeypatch):
    """An entry removed after selection (FIXED or dropped as stale) is skipped."""
    video = _media(tmp_path)
    sf.NEEDS_ENGLISH_STATE_FILE.write_text(json.dumps({}))
    result = _revalidate(monkeypatch, video)
    assert result is not None and "no longer present" in result


def test_revalidate_candidate_skips_when_file_gone(tmp_path, monkeypatch):
    video = _media(tmp_path)
    _seed_tracker_entry(video)
    video.unlink()
    result = _revalidate(monkeypatch, video)
    assert result is not None and "no longer exists" in result


def test_revalidate_candidate_tracker_unreadable_is_skipped_not_raised(tmp_path, monkeypatch):
    video = _media(tmp_path)
    monkeypatch.setattr(sf, "_revalidate_candidate", _REAL_REVALIDATE_CANDIDATE)
    with patch.object(sf, "load_state_locked", side_effect=OSError("corrupt")):
        result = sf._revalidate_candidate(video, _cfg(age_hours=24.0))
    assert result is not None and "unreadable" in result


def test_process_candidate_revalidation_skip_before_anything_else(tmp_path, monkeypatch):
    """process_candidate surfaces REVALIDATION_SKIP with a `reason`, before the sibling-collision check or anything else."""
    monkeypatch.setattr(sf, "_revalidate_candidate", _REAL_REVALIDATE_CANDIDATE)
    sf.NEEDS_ENGLISH_STATE_FILE.write_text(json.dumps({}))  # removed/never recorded
    rec, mocks = _candidate(tmp_path, base="bare", _sibling_video_collision={})
    assert rec["result"] == "REVALIDATION_SKIP"
    assert "no longer present" in rec["reason"]
    mocks["_sibling_video_collision"].assert_not_called()


# --- _verify_untagged_audio_english: LID retry logic ----------------------

def _lid(workdir=Path("/work"), **lid_kw):
    """Run the LID gate with `detect_language` stubbed by `lid_kw`; returns `(result, detect_language mock, rec)`."""
    rec = {}
    with patch.object(sf, "detect_language", **lid_kw) as mock_lid:
        result = sf._verify_untagged_audio_english(
            Path("/v.mkv"), 1, 1200.0, _cfg(), _FakeJellyfin([]), _budget(), workdir, rec)
    return result, mock_lid, rec


def test_verify_untagged_audio_english_passes_on_first_window():
    workdir = Path("/candidate-workdir")
    result, mock_lid, rec = _lid(workdir, return_value=("en", 0.95))
    assert result is True
    assert mock_lid.call_count == 1
    assert rec["audio_lid_language"] == "en"
    assert rec["audio_lid_probability"] == 0.95
    assert mock_lid.call_args[0][-1] == workdir  # the candidate's workdir is forwarded


def test_verify_untagged_audio_english_retries_low_confidence_then_passes():
    result, mock_lid, _ = _lid(side_effect=[("en", 0.3), ("en", 0.9)])
    assert result is True
    assert mock_lid.call_count == 2
    # second call used the 50% fraction window
    assert mock_lid.call_args_list[1][0][2] == 1200.0 * 0.50


@pytest.mark.parametrize("lid_kw, calls", [
    ({"side_effect": [("en", 0.2), ("en", 0.3), ("en", 0.1)]}, 3),
    ({"return_value": ("fr", 0.92)}, 1),
], ids=["three-low-confidence-windows", "confidently-foreign-stops-retrying"])
def test_verify_untagged_audio_english_fails(lid_kw, calls):
    result, mock_lid, _ = _lid(**lid_kw)
    assert result is False
    assert mock_lid.call_count == calls


# --- process_candidate: short-circuits -------------------------------------

def test_no_english_audio_gives_result_without_transcription(tmp_path):
    rec, mocks = _candidate(tmp_path, base="sidecar", english_audio_stream={"return_value": None})
    assert rec["result"] == "NO_ENGLISH_AUDIO"
    _assert_nothing_attempted(mocks)


_ENGLISH_SIDECAR_TEXT = (
    "1\n00:00:01,000 --> 00:00:03,000\n"
    "I don't know what you want from me tonight.\n\n"
    "2\n00:00:03,500 --> 00:00:06,000\n"
    "Just tell the truth, detective. I'm not hiding anything, okay.\n\n"
    "3\n00:00:06,500 --> 00:00:09,000\n"
    "She promised she'd arrive early, but nobody noticed her leaving.\n\n"
    "4\n00:00:09,500 --> 00:00:12,000\n"
    "Look, this situation feels impossible, but we have to move now.\n"
)

@pytest.mark.parametrize("names", [
    (".en.srt",), (".en.generated.srt",), (".en.srt", ".en.generated.srt"),
], ids=["plain", "generated", "two-main-sidecars"])
def test_an_existing_main_sidecar_is_always_skipped(tmp_path, names):
    """Whoever placed it (Bazarr, a user, an earlier run of this script), a video with a main sidecar gets SIDECAR_PRESENT and NO action, not even a consolidate nudge: the watcher and the 05:00 scan own it."""
    video = _media(tmp_path)
    sidecars = [video.with_name(video.stem + name) for name in names]
    for sidecar in sidecars:
        sidecar.write_text(_ENGLISH_SIDECAR_TEXT)
    rec, mocks = _candidate(tmp_path, video=video, base="sidecar",
                            find_sidecar_subs={"return_value": [(sc, False, False) for sc in sidecars]},
                            wait_for_pipeline={})
    assert rec["result"] == "SIDECAR_PRESENT"
    _assert_nothing_attempted(mocks)
    mocks["wait_for_pipeline"].assert_not_called()


def test_already_tagged_file_short_circuits_before_the_sidecar_check(tmp_path):
    """A tagged file short-circuits at ALREADY_CONSOLIDATED before the sidecar-exists check."""
    rec, mocks = _candidate(tmp_path, base="sidecar", already_processed={"return_value": True},
                            find_sidecar_subs={})
    assert rec["result"] == "ALREADY_CONSOLIDATED"
    mocks["find_sidecar_subs"].assert_not_called()
    mocks["_run_consolidate_subs_directly"].assert_not_called()


# --- _main_sidecars -----------------------------------------

@pytest.mark.parametrize("kinds, has_forced", [
    (("forced", "main"), True), (("forced",), True), (("main",), False),
], ids=["forced-then-main", "forced-only", "main-only"])
def test_main_sidecars_excludes_forced_entries_and_has_forced_sidecar_sees_any(tmp_path, kinds, has_forced):
    video = _media(tmp_path)
    available = {"forced": (video.with_name(video.stem + ".en.forced.srt"), True, False),
                 "main": (video.with_name(video.stem + ".en.srt"), False, False)}
    entries = [available[k] for k in kinds]
    with patch.object(sf, "find_sidecar_subs", return_value=entries):
        assert sf._main_sidecars(video) == [e for e in entries if not e[1]]
        assert sf._has_forced_sidecar(video) is has_forced


def test_process_candidate_forced_sidecar_present_blocks_everything(tmp_path):
    """ANY forced English sidecar gives FORCED_SIDECAR_PRESENT with nothing fetched, transcribed or placed (consolidate-subs.py handles forced-pattern sidecars itself)."""
    video = _media(tmp_path)
    forced = video.with_name(video.stem + ".en.forced.srt")
    forced.write_text(_ENGLISH_SIDECAR_TEXT)
    rec, mocks = _candidate(tmp_path, video=video, base="sidecar", probe={"return_value": _DURATION_INFO},
                            find_sidecar_subs={"return_value": [(forced, True, False)]})

    assert rec["result"] == "FORCED_SIDECAR_PRESENT"
    _assert_nothing_attempted(mocks)
    assert forced.read_text() == _ENGLISH_SIDECAR_TEXT  # untouched


def test_probe_failure_is_recorded(tmp_path):
    rec, _ = _candidate(tmp_path, base="bare", probe={"return_value": None})
    assert rec["result"] == "PROBE_FAILED"


# --- process_candidate: untagged audio LID gate ---------------------------

def test_explicitly_tagged_audio_skips_lid_entirely(tmp_path):
    rec, mocks = _candidate(
        tmp_path, probe={"return_value": _SHORT_INFO}, english_audio_stream={"return_value": 1},
        detect_language={}, _transcribe_in_subprocess={"return_value": _HI_SRT},
        _place_sidecar={"return_value": None}, wait_for_pipeline={"return_value": (True, 1)})
    mocks["detect_language"].assert_not_called()
    assert rec["result"] == "FIXED"


def test_untagged_audio_unverified_gives_result_without_subliminal_or_whisper(tmp_path):
    rec, mocks = _candidate(
        tmp_path, probe={"return_value": _SHORT_INFO}, english_audio_stream={"return_value": 1},
        is_audio_explicitly_english={"return_value": False}, detect_language={"return_value": ("da", 0.9)},
        fetch_via_subliminal={}, _transcribe_in_subprocess={})
    assert rec["result"] == "AUDIO_LANGUAGE_UNVERIFIED"
    assert rec["audio_lid_language"] == "da"
    mocks["fetch_via_subliminal"].assert_not_called()
    mocks["_transcribe_in_subprocess"].assert_not_called()


def test_untagged_audio_verified_english_proceeds_to_subliminal(tmp_path):
    rec, mocks = _candidate(
        tmp_path, english_audio_stream={"return_value": 1}, is_audio_explicitly_english={"return_value": False},
        detect_language={"return_value": ("en", 0.9)}, fetch_via_subliminal={"return_value": None},
        _transcribe_in_subprocess={"return_value": ""})
    mocks["fetch_via_subliminal"].assert_called_once()
    mocks["_transcribe_in_subprocess"].assert_called_once()
    assert rec["result"] == "TOO_FEW_CUES"


# --- process_candidate: subliminal verification + whisper fallback -------


def test_whisper_result_is_published_as_a_generated_sidecar(tmp_path):
    """A Whisper transcript goes to `<stem>.en.generated.srt` so a real subtitle can be preferred over it later."""
    video = _media(tmp_path)
    rec, mocks = _candidate(tmp_path, video=video, _transcribe_in_subprocess={"return_value": _HI_SRT}, **_PLACED)
    assert rec["result"] == "FIXED"
    assert rec["source"] == "whisper"
    assert mocks["_place_sidecar"].call_args[0][1].name == video.stem + ".en.generated.srt"


def test_place_sidecar_publishes_a_generated_sidecar_that_discovery_sees_as_a_plain_main_one(tmp_path):
    """The generated name publishes and is found as one non-forced main sidecar, like any other."""
    video = _media(tmp_path)
    dest = sf._sidecar_dest(video, "whisper")
    assert _place(tmp_path, video, dest, "subtitle content", sidecars=None) is None
    assert dest.read_text() == "subtitle content"
    assert sf._main_sidecars(video) == [(dest, False, False)]
    assert sf._has_forced_sidecar(video) is False


def test_place_sidecar_blocks_when_a_real_sidecar_exists_under_the_other_name(tmp_path):
    """No-clobber holds across BOTH names: a Bazarr `.en.srt` already next to the video stops a generated publish (SIDECAR_APPEARED), and the reverse."""
    video = _media(tmp_path)
    real = video.with_name(video.stem + ".en.srt")
    real.write_text("real subtitle")
    gen = sf._sidecar_dest(video, "whisper")
    assert _place(tmp_path, video, gen, "generated", sidecars=None) == "SIDECAR_APPEARED"
    assert not gen.exists()
    assert real.read_text() == "real subtitle"


def test_place_sidecar_published_sidecar_is_world_readable_0644(tmp_path):
    """mkstemp creates the temp 0600 and the link keeps it; fchmod(0o644) before linking makes the sidecar readable regardless of umask."""
    video = _media(tmp_path)
    dest = sf._sidecar_dest(video, "whisper")
    old_umask = os.umask(0o077)  # the mode must not depend on the umask
    try:
        assert _place(tmp_path, video, dest, "subtitle content", sidecars=None) is None
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(dest.stat().st_mode) == 0o644


def _fetched_srt(tmp_path, text):
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    fetched = workdir / "fetched.en.srt"
    fetched.write_text(text)
    return fetched


def test_verified_subliminal_candidate_placed_through_place_sidecar(tmp_path):
    video = _media(tmp_path)
    fetched = _fetched_srt(tmp_path, "English subtitle content here")
    rec, mocks = _candidate(
        tmp_path, video=video, fetch_via_subliminal={"return_value": fetched},
        sublang_score={"return_value": _ENG_SCORE}, asr_window={"return_value": "asr text matching the subtitle window"},
        overlap={"return_value": (0.9, 0.8, 20)}, _transcribe_in_subprocess={}, **_PLACED)

    assert rec["result"] == "FIXED"
    assert rec["source"] == "subliminal"
    mocks["_transcribe_in_subprocess"].assert_not_called()
    mocks["_place_sidecar"].assert_called_once()
    assert mocks["_place_sidecar"].call_args[0][1].name == video.stem + ".en.srt"
    assert not fetched.exists()  # workdir copy cleaned up


def test_wrong_episode_subliminal_rejected_on_bigram_falls_back_to_whisper(tmp_path):
    fetched = _fetched_srt(tmp_path, "English subtitle content for the wrong episode")
    rec, mocks = _candidate(
        tmp_path, fetch_via_subliminal={"return_value": fetched}, sublang_score={"return_value": _ENG_SCORE},
        asr_window={"return_value": "shares words but not order"}, overlap={"return_value": (0.9, 0.05, 20)},
        _transcribe_in_subprocess={"return_value": _HI_SRT}, **_PLACED)

    assert rec["result"] == "FIXED"
    assert rec["source"] == "whisper"
    assert rec["subliminal_reject_reason"] == "wrong episode (low overlap)"
    mocks["_transcribe_in_subprocess"].assert_called_once()


def test_non_english_subliminal_rejected_without_any_asr_call(tmp_path):
    fetched = _fetched_srt(tmp_path, "Dette er faktisk dansk tekst, ikke engelsk overhovedet")
    rec, mocks = _candidate(
        tmp_path, fetch_via_subliminal={"return_value": fetched},
        sublang_score={"return_value": {"verdict": "dan", "eng": 0.02}}, asr_window={}, overlap={},
        _transcribe_in_subprocess={"return_value": _HI_SRT}, **_PLACED)

    assert rec["source"] == "whisper"
    mocks["asr_window"].assert_not_called()
    mocks["overlap"].assert_not_called()


def test_too_little_speech_rejects_subliminal_tries_second_window_then_whisper(tmp_path):
    fetched = _fetched_srt(tmp_path, "English subtitle content here")
    rec, mocks = _candidate(
        tmp_path, fetch_via_subliminal={"return_value": fetched}, sublang_score={"return_value": _ENG_SCORE},
        asr_window={"side_effect": ["", ""]}, overlap={"side_effect": [(0.0, 0.0, 3), (0.0, 0.0, 5)]},
        _transcribe_in_subprocess={"return_value": _HI_SRT}, **_PLACED)

    assert mocks["asr_window"].call_count == 2
    assert mocks["overlap"].call_count == 2
    assert rec["subliminal_reject_reason"] == "too little speech to verify"
    assert rec["source"] == "whisper"


# --- process_candidate: whisper fallback edge cases -----------------------

def test_whisper_too_few_cues_rejects_without_placing_sidecar(tmp_path):
    rec, mocks = _candidate(tmp_path, _transcribe_in_subprocess={"return_value": ""}, _place_sidecar={},
                            wait_for_pipeline={})
    assert rec["result"] == "TOO_FEW_CUES"
    mocks["_place_sidecar"].assert_not_called()
    mocks["wait_for_pipeline"].assert_not_called()


@pytest.mark.parametrize("exc, result", [
    (sf.ASRTimeout("boom"), "ASR_TIMEOUT"), (sf.MissingJellyfinKey("boom"), "MISSING_JELLYFIN_KEY"),
], ids=["asr-timeout", "missing-jellyfin-key"])
def test_transcription_failure_gets_its_own_result_never_the_generic_error(tmp_path, exc, result):
    rec, _ = _candidate(tmp_path, _transcribe_in_subprocess={"side_effect": exc})
    assert rec["result"] == result


@pytest.mark.parametrize("deadline_s, result, transcribes", [
    (300.0, "DEFERRED_BUDGET", False),   # under 10 min left: skipped, tracker untouched
    (601.0, "TOO_FEW_CUES", True),       # just over 10 min: still starts
], ids=["under-ten-minutes", "just-over-ten-minutes"])
def test_transcription_is_deferred_only_when_under_ten_minutes_of_budget_remain(
        tmp_path, deadline_s, result, transcribes):
    rec, mocks = _candidate(tmp_path, deadline_s=deadline_s, _transcribe_in_subprocess={"return_value": ""})
    assert rec["result"] == result
    assert mocks["_transcribe_in_subprocess"].call_count == int(transcribes)


def test_unexpected_exception_is_caught_as_error_result(tmp_path):
    rec, _ = _candidate(tmp_path, base="bare", probe={"side_effect": RuntimeError("ffprobe exploded")})
    assert rec["result"] == "ERROR"
    assert "ffprobe exploded" in rec["error"]


def test_playback_wait_timeout_propagates_not_caught_as_error(tmp_path):
    with pytest.raises(sf.PlaybackWaitTimeout):
        _candidate(tmp_path, base="bare", wait_while_playing={"side_effect": sf.PlaybackWaitTimeout("x")})


def test_pipeline_timeout_is_recorded(tmp_path):
    rec, _ = _candidate(tmp_path, _transcribe_in_subprocess={"return_value": _HI_SRT},
                        _place_sidecar={"return_value": None}, wait_for_pipeline={"return_value": (False, 3600)})
    assert rec["result"] == "PIPELINE_TIMEOUT"


# --- _transcribe_in_subprocess / _run_consolidate_subs_directly: output goes to files and the wait is unreaped (os.waitid WNOWAIT), so the fake Popen needs only .pid/.wait()/.poll() ---

def _fake_proc(returncode=0, pid=4321):
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = returncode
    proc.wait.return_value = returncode
    proc.poll.return_value = returncode
    return proc


def _popen_side_effect(proc, stdout_text="", stderr_text=""):
    """Popen side_effect that writes the simulated child's output into the stdout=/stderr= handles it is given, then returns `proc`."""
    def _side_effect(*args, **kwargs):
        out_fh = kwargs.get("stdout")
        err_fh = kwargs.get("stderr")
        if out_fh is not None and stdout_text:
            out_fh.write(stdout_text.encode())
            out_fh.flush()
        if err_fh is not None and stderr_text:
            err_fh.write(stderr_text.encode())
            err_fh.flush()
        return proc
    return _side_effect


def _timeout_monotonic_sequence():
    """time.monotonic() side_effect that times out `_wait_for_exit_unreaped` quickly: 0.0, 0.0, then a huge value forever."""
    return itertools.chain([0.0, 0.0], itertools.repeat(10_000.0))


@contextmanager
def _fake_child(proc, stdout_text="", stderr_text="", *, wait="exits", killpg=None):
    """Patch Popen, waitid and killpg so a run sees `proc` finish as `wait` says: "exits", "times-out" or "interrupted". Yields `(Popen mock, killpg mock)`."""
    waitid = {"exits": {"return_value": True}, "times-out": {"return_value": None},
              "interrupted": {"side_effect": KeyboardInterrupt}}[wait]
    times = _timeout_monotonic_sequence()
    kill = killpg if killpg is not None else MagicMock()
    with ExitStack() as stack:
        popen = stack.enter_context(patch.object(
            sf.subprocess, "Popen", side_effect=_popen_side_effect(proc, stdout_text, stderr_text)))
        stack.enter_context(patch.object(sf.os, "waitid", **waitid))
        stack.enter_context(patch.object(sf.os, "killpg", kill))
        if wait == "times-out":
            stack.enter_context(patch.object(sf.time, "sleep"))
            stack.enter_context(patch.object(sf.time, "monotonic", side_effect=lambda: next(times)))
        yield popen, kill


def _transcribe(tmp_path, cfg=None, budget=None):
    return sf._transcribe_in_subprocess(
        Path("/v.mkv"), 1, cfg or _cfg(), _budget() if budget is None else budget, tmp_path, 60.0)


_RUN_CHILD = {
    "transcribe": _transcribe,
    "consolidate": lambda tmp_path: sf._run_consolidate_subs_directly(Path("/v.mkv"), 60.0, tmp_path),
}


def _outcome(run, tmp_path):
    """What `run(tmp_path)` returned, or the exception class it raised."""
    try:
        return run(tmp_path)
    except BaseException as exc:
        return type(exc)


def test_transcribe_in_subprocess_success_returns_stdout(tmp_path):
    with _fake_child(_fake_proc(), stdout_text="SRT OUTPUT") as (popen, _):
        result = _transcribe(tmp_path)
    assert result == "SRT OUTPUT"
    cmd = popen.call_args[0][0]
    assert cmd[1:4] == ["-m", "media_stack.subverify", "transcribe"]
    env = popen.call_args[1]["env"]
    assert env["TMPDIR"] == str(tmp_path)
    assert popen.call_args[1]["start_new_session"] is True


@pytest.mark.parametrize("runner, wait", [
    ("transcribe", "exits"), ("transcribe", "times-out"), ("transcribe", "interrupted"),
    ("consolidate", "exits"), ("consolidate", "times-out"), ("consolidate", "interrupted"),
], ids=["transcribe-exits", "transcribe-times-out", "transcribe-interrupted",
        "consolidate-exits", "consolidate-times-out", "consolidate-interrupted"])
def test_the_child_process_group_is_killed_on_every_exit_path(tmp_path, runner, wait):
    """The child exiting does not prove its process group is empty, so killpg(pid) runs once on every path."""
    proc = _fake_proc(returncode=0)
    with _fake_child(proc, stdout_text="OUT", wait=wait) as (_, killpg):
        outcome = _outcome(_RUN_CHILD[runner], tmp_path)

    killpg.assert_called_once_with(proc.pid, sf.signal.SIGKILL)
    if (runner, wait) == ("consolidate", "times-out"):
        assert outcome[0] is False and "timed out" in outcome[1]
    else:
        assert outcome == {
            ("transcribe", "exits"): "OUT", ("transcribe", "times-out"): sf.ASRTimeout,
            ("consolidate", "exits"): (True, "OUT"),
        }.get((runner, wait), KeyboardInterrupt)


@pytest.mark.parametrize("runner", ["transcribe", "consolidate"])
def test_killpg_process_lookup_error_is_swallowed_on_the_success_path(tmp_path, runner):
    """ProcessLookupError from the always-attempted killpg (group already empty) must not break the success path."""
    with _fake_child(_fake_proc(), stdout_text="OUT", killpg=MagicMock(side_effect=ProcessLookupError)):
        outcome = _RUN_CHILD[runner](tmp_path)
    assert outcome == ("OUT" if runner == "transcribe" else (True, "OUT"))


@pytest.mark.parametrize("returncode, stderr, exc, match", [
    (2, "playback wait exceeded", sf.PlaybackWaitTimeout, None),
    (3, "JELLYFIN_API_KEY is not set", sf.MissingJellyfinKey, None),
    (1, "boom", RuntimeError, "boom"),  # stderr is read back from its file (output capture is off pipes)
], ids=["exit-2-playback-wait-timeout", "exit-3-missing-jellyfin-key", "other-failure-raises-runtime-error"])
def test_transcribe_in_subprocess_maps_the_childs_exit_status(tmp_path, returncode, stderr, exc, match):
    with _fake_child(_fake_proc(returncode=returncode), stderr_text=stderr):
        with pytest.raises(exc, match=match):
            _transcribe(tmp_path)


def test_transcribe_in_subprocess_passes_jellyfin_key_and_url_explicitly(tmp_path, monkeypatch):
    """The child's env carries this run's `cfg.jellyfin_key`/`cfg.jellyfin_url`, overriding a different value in `os.environ`."""
    monkeypatch.setenv("JELLYFIN_API_KEY", "stale-inherited-key")
    monkeypatch.setenv("JELLYFIN_URL", "http://stale-inherited:8096")
    cfg = _cfg(jellyfin_key="realkey", jellyfin_url="http://jf:8096")
    with _fake_child(_fake_proc(), stdout_text="SRT OUTPUT") as (popen, _):
        _transcribe(tmp_path, cfg)
    env = popen.call_args[1]["env"]
    assert env["JELLYFIN_API_KEY"] == "realkey"
    assert env["JELLYFIN_URL"] == "http://jf:8096"


def test_transcribe_in_subprocess_passes_the_remaining_wait_budget(tmp_path):
    """SUB_FALLBACK_MAX_WAIT_MIN handed to the child is the budget's current remaining allowance, not the full configured value."""
    budget = _budget(10.0)
    budget.spend(420.0)
    with _fake_child(_fake_proc(), stdout_text="SRT OUTPUT") as (popen, _):
        _transcribe(tmp_path, _cfg(max_wait_min=10.0), budget)
    env = popen.call_args[1]["env"]
    assert float(env["SUB_FALLBACK_MAX_WAIT_MIN"]) == pytest.approx(3.0)


@pytest.mark.parametrize("report, wait, expected_spent_s", [
    ("185.500", "exits", 185.5), ("42.0", "times-out", 42.0), (None, "exits", 0.0),
], ids=["reported-wait-deducted", "deducted-even-on-asr-timeout", "missing-report-deducts-zero"])
def test_transcribe_in_subprocess_deducts_the_childs_reported_wait_from_the_budget(
        tmp_path, report, wait, expected_spent_s):
    """The child reports the seconds it actually waited in a file in `workdir`; the parent deducts it from the shared budget on every exit path (no report is not an error)."""
    budget = _budget(10.0)  # 600 s allowance
    if report is not None:
        (tmp_path / sf.CHILD_PLAYBACK_WAIT_REPORT_FILENAME).write_text(report)
    with _fake_child(_fake_proc(), stdout_text="SRT OUTPUT", wait=wait):
        outcome = _outcome(lambda p: _transcribe(p, budget=budget), tmp_path)
    assert outcome == ("SRT OUTPUT" if wait == "exits" else sf.ASRTimeout)
    assert budget.remaining_s == pytest.approx(600.0 - expected_spent_s)


# --- _run_consolidate_subs_directly ----------------------------------------

def test_run_consolidate_subs_directly_success_returns_true_and_detail(tmp_path):
    with _fake_child(_fake_proc(), stdout_text="consolidated ok") as (popen, _):
        ok, detail = sf._run_consolidate_subs_directly(Path("/v.mkv"), 60.0, tmp_path)
    assert ok is True
    assert detail == "consolidated ok"
    assert popen.call_args[1]["start_new_session"] is True


def test_run_consolidate_subs_directly_nonzero_exit_returns_false(tmp_path):
    """Stderr is read back from its file."""
    with _fake_child(_fake_proc(returncode=1), stderr_text="consolidate-subs.py failed"):
        ok, detail = sf._run_consolidate_subs_directly(Path("/v.mkv"), 60.0, tmp_path)
    assert ok is False
    assert "failed" in detail


# --- main(): one-file-at-a-time, budget, dry-run, singleton lock ----------


def _seed_needs_english(path: Path, entries: dict) -> None:
    path.write_text(json.dumps(entries))


def _run_main(*argv, cfg=None, **patches):
    """Run `sf.main()` with `load_config` stubbed and the `sf` names in `patches` (name -> `patch.object` kwargs) replaced. Returns `(rc, {name: mock})`."""
    patches = {"load_config": {"return_value": cfg or _cfg()}, **patches}
    with ExitStack() as stack:
        mocks = {name: stack.enter_context(patch.object(sf, name, **kw)) for name, kw in patches.items()}
        stack.enter_context(patch.object(sys, "argv", ["sub-fallback.py", *argv]))
        return sf.main(), mocks


def _two_waiting_videos(tmp_path):
    v1, v2 = _media(tmp_path, "a.mkv"), _media(tmp_path, "b.mkv")
    _seed_needs_english(sf.NEEDS_ENGLISH_STATE_FILE, {
        str(v1.resolve()): {"first_seen": "2026-10-01T00:00:00Z"},
        str(v2.resolve()): {"first_seen": "2026-10-02T00:00:00Z"},
    })
    return v1, v2


def test_main_rejects_english_only_lid_model_on_real_run():
    rc, mocks = _run_main(cfg=_cfg(whisper_lid_model="small.en"), load_needs_english={})
    assert rc == 1
    mocks["load_needs_english"].assert_not_called()  # caught before ever touching the tracker


@pytest.mark.parametrize("argv, key, rc", [
    ((), "", 1), (("--dry-run",), "", 0), ((), "realkey", 0),
], ids=["real-run-needs-the-key", "dry-run-works-without-it", "nothing-to-do"])
def test_main_exit_code_with_and_without_a_jellyfin_key(argv, key, rc):
    assert _run_main(*argv, cfg=_cfg(jellyfin_key=key))[0] == rc


def test_main_drops_stale_tracker_entries(tmp_path):
    """An entry whose file is gone is removed from needs-english.json so the tracker does not leak."""
    gone_key = str((tmp_path / "gone.mkv").resolve())  # never actually created on disk
    _seed_needs_english(sf.NEEDS_ENGLISH_STATE_FILE, {
        gone_key: {"first_seen": "2026-10-01T00:00:00Z", "last_seen": "2026-10-01T00:00:00Z"},
    })

    rc, _ = _run_main(_has_consolidated_tag={"return_value": False})

    assert rc == 0
    assert json.loads(sf.NEEDS_ENGLISH_STATE_FILE.read_text()) == {}


@pytest.mark.parametrize("cfg_overrides, pause_s, raises, started", [
    ({}, 0, None, 2),
    ({"budget_min": 0.0005}, 0.05, None, 1),  # the budget stops STARTING new files
    ({}, 0, sf.PlaybackWaitTimeout("playback wait exceeded 30 min"), 1),  # ends the run cleanly, exit 0
], ids=["one-at-a-time-oldest-first", "budget-stops-starting-new-files", "playback-wait-timeout-ends-the-run"])
def test_main_processes_one_candidate_at_a_time_until_budget_or_playback_stops_it(
        tmp_path, cfg_overrides, pause_s, raises, started):
    v1, v2 = _two_waiting_videos(tmp_path)
    calls = []

    def _fake_process(video, cfg, jellyfin, workdir, remaining_budget_s):
        calls.append(video)
        time.sleep(pause_s)
        if raises:
            raise raises
        return {"path": str(video), "result": "FIXED"}

    rc, _ = _run_main(cfg=_cfg(**cfg_overrides), _has_consolidated_tag={"return_value": False},
                      process_candidate={"side_effect": _fake_process})

    assert rc == 0
    assert calls == [v1, v2][:started]


def test_main_dry_run_mutates_nothing(tmp_path):
    state_file = sf.NEEDS_ENGLISH_STATE_FILE
    v1 = _media(tmp_path, "a.mkv")
    _seed_needs_english(state_file, {
        str(v1.resolve()): {"first_seen": "2026-10-01T00:00:00Z"},
        str((tmp_path / "gone.mkv").resolve()): {"first_seen": "2026-10-01T00:00:00Z"},
    })
    before = state_file.read_text()

    rc, mocks = _run_main("--dry-run", _has_consolidated_tag={"return_value": False}, process_candidate={})

    assert rc == 0
    mocks["process_candidate"].assert_not_called()
    assert not sf.JSONL_FILE.exists()
    assert state_file.read_text() == before


def test_main_exits_quietly_when_another_instance_holds_the_lock():
    with open(sf.LOCK_FILE, "w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rc, mocks = _run_main(load_needs_english={})
    assert rc == 0
    mocks["load_needs_english"].assert_not_called()


def test_main_blinded_by_unreadable_state_returns_1():
    sf.NEEDS_ENGLISH_STATE_FILE.write_text("{not valid json")
    assert _run_main()[0] == 1


# --- Config / env parsing ---------------------------------------------------

def test_load_config_defaults_with_empty_env():
    # whisper_threads defaults to min(6, os.cpu_count()); pin cpu_count so this is deterministic on any host.
    with patch.object(sf.os, "cpu_count", return_value=8):
        cfg = sf.load_config({})
    assert cfg.age_hours == 24.0
    assert cfg.budget_min == 50.0
    assert cfg.pipeline_timeout_min == 60.0
    assert cfg.max_wait_min == 30.0
    assert cfg.asr_timeout_min == 90.0
    assert cfg.whisper_model == "medium.en"
    assert cfg.whisper_verify_model == "small.en"
    assert cfg.whisper_lid_model == "small"
    assert cfg.whisper_threads == 6
    assert cfg.jellyfin_key == ""
    assert cfg.jellyfin_url.endswith(":8096")


def test_load_config_honors_overrides():
    env = {
        "SUB_FALLBACK_AGE_HOURS": "12", "SUB_FALLBACK_BUDGET_MIN": "30",
        "SUB_FALLBACK_PIPELINE_TIMEOUT_MIN": "90", "SUB_FALLBACK_MAX_WAIT_MIN": "15",
        "SUB_FALLBACK_ASR_TIMEOUT_MIN": "45",
        "WHISPER_MODEL": "large-v3", "WHISPER_VERIFY_MODEL": "tiny.en",
        "WHISPER_LID_MODEL": "medium", "WHISPER_THREADS": "2",
        "JELLYFIN_URL": "http://jf.example:9999", "JELLYFIN_API_KEY": "secret",
    }
    # WHISPER_THREADS="2" is in-bound only with >= 2 CPUs; pin cpu_count.
    with patch.object(sf.os, "cpu_count", return_value=8):
        cfg = sf.load_config(env)
    assert cfg.age_hours == 12.0
    assert cfg.budget_min == 30.0
    assert cfg.pipeline_timeout_min == 90.0
    assert cfg.max_wait_min == 15.0
    assert cfg.asr_timeout_min == 45.0
    assert cfg.whisper_model == "large-v3"
    assert cfg.whisper_verify_model == "tiny.en"
    assert cfg.whisper_lid_model == "medium"
    assert cfg.whisper_threads == 2
    assert cfg.jellyfin_url == "http://jf.example:9999"
    assert cfg.jellyfin_key == "secret"


# --- load_config: finite, positive, BOUNDED validation of numeric settings ----

_NUMERIC_SETTINGS = [  # (env var, Config attribute, upper bound)
    ("SUB_FALLBACK_AGE_HOURS", "age_hours", 8760),
    ("SUB_FALLBACK_BUDGET_MIN", "budget_min", 1440),
    ("SUB_FALLBACK_PIPELINE_TIMEOUT_MIN", "pipeline_timeout_min", 1440),
    ("SUB_FALLBACK_MAX_WAIT_MIN", "max_wait_min", 1440),
    ("SUB_FALLBACK_ASR_TIMEOUT_MIN", "asr_timeout_min", 1440),
]


def test_load_config_rejects_bad_values_for_every_numeric_setting():
    """Every numeric setting rejects a negative or unparseable value, falling back to its default."""
    defaults = sf.load_config({})
    for bad in ["-5", "abc"]:
        for key, attr, _ in _NUMERIC_SETTINGS:
            assert getattr(sf.load_config({key: bad}), attr) == getattr(defaults, attr), f"{key}={bad!r}"
        cfg = sf.load_config({"WHISPER_THREADS": bad})
        assert cfg.whisper_threads == defaults.whisper_threads, f"WHISPER_THREADS={bad!r}"


def test_load_config_accepts_the_exact_limit_and_rejects_just_over_it():
    defaults = sf.load_config({})
    for key, attr, limit in _NUMERIC_SETTINGS:
        assert getattr(sf.load_config({key: str(limit + 0.5)}), attr) == getattr(defaults, attr), f"{key}={limit + 0.5}"
        assert getattr(sf.load_config({key: str(limit)}), attr) == limit, f"{key}={limit}"


def test_load_config_rejects_whisper_threads_over_cpu_count():
    """WHISPER_THREADS <= os.cpu_count() (64 if unknown), and the default respects that bound too (`min(6, bound)`): on 4 CPUs a rejected oversized override falls back to 4, not 6."""
    with patch.object(sf.os, "cpu_count", return_value=4):
        cfg_ok = sf.load_config({"WHISPER_THREADS": "4"})
        cfg_over = sf.load_config({"WHISPER_THREADS": "5"})
        cfg_default = sf.load_config({})
    assert cfg_ok.whisper_threads == 4
    assert cfg_over.whisper_threads == 4  # fell back to min(6, cpu_count) = 4
    assert cfg_default.whisper_threads == 4


# --- SIGTERM handling --------------------------------------------------------

def test_handle_sigterm_raises_system_exit():
    """Must raise SystemExit so `_transcribe_in_subprocess`'s `finally` runs and kills the child's process group."""
    with pytest.raises(SystemExit):
        sf._handle_sigterm(sf.signal.SIGTERM, None)


def test_main_installs_sigterm_handler():
    with patch.object(sf.signal, "signal") as mock_signal:
        _run_main()
    mock_signal.assert_called_once_with(sf.signal.SIGTERM, sf._handle_sigterm)
