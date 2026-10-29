"""Mock-based tests for `_process_locked`'s early returns, `_expose_tag_normalize` and `_normalize_audio_inline`
in consolidate-subs.py (no ffprobe/mkvmerge/real normalize child)."""

import importlib.util
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "consolidate_subs_orch", str(PROJECT_ROOT / "consolidate-subs.py")
)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)


@pytest.fixture(autouse=True)
def _isolate_needs_english_state(tmp_path, monkeypatch):
    """Point NEEDS_ENGLISH_STATE_FILE at `tmp_path` so no test writes the checkout's real tracker."""
    monkeypatch.setattr(cs, "NEEDS_ENGLISH_STATE_FILE", tmp_path / "needs-english.json")


@pytest.fixture(autouse=True)
def _no_real_ffprobe(monkeypatch):
    """The swap helper probes the source and the output for the duration guard: never the real ffprobe
    (a readable file with no duration, so the guard skips)."""
    monkeypatch.setattr(cs, "probe", lambda p: {"streams": []})


def _media(tmp_path: Path) -> Path:
    p = tmp_path / "Movie (2024).mkv"
    p.write_bytes(b"\x1a\x45\xdf\xa3")
    return p


def test_file_gone_after_lock_returns_skip(tmp_path):
    """A peer removed the file between scan and lock acquisition: skip cleanly, no crash."""
    ghost = tmp_path / "vanished.mkv"
    result = cs._process_locked(ghost, str(ghost), dry_run=False)
    assert result["status"] == "SKIP"
    assert "gone" in result["detail"].lower()


def test_state_cache_hit_returns_skip(tmp_path):
    media = _media(tmp_path)
    fake_state = {
        str(media.resolve()): {
            "size": media.stat().st_size,
            "mtime": int(media.stat().st_mtime),   # file_key() stores the int cast
            "v": cs.PIPELINE_VERSION,
            "status": "fixed",
        }
    }
    with patch.object(cs, "load_state", return_value=fake_state):
        result = cs._process_locked(media, str(media), dry_run=False)
    assert result["status"] == "SKIP"
    assert result["detail"] == "state cache"


def test_probe_failure_returns_fail(tmp_path):
    media = _media(tmp_path)
    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", return_value=None):
        result = cs._process_locked(media, str(media), dry_run=False)
    assert result["status"] == "FAIL"
    assert "probe" in result["detail"]


def test_already_processed_returns_skip(tmp_path):
    media = _media(tmp_path)
    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", return_value={"streams": []}), \
         patch.object(cs, "already_processed", return_value=True), \
         patch.object(cs, "update_state_entry"):
        result = cs._process_locked(media, str(media), dry_run=False)
    assert result["status"] == "SKIP"
    assert "tag" in result["detail"]


@pytest.mark.parametrize("lang", ["eng", "en-US"])
def test_dry_run_short_circuits_before_mutation(tmp_path, lang):
    """dry_run=True must not call extract/remux/replace, even with an actionable (regional) English sub."""
    media = _media(tmp_path)
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3",
         "tags": {"language": "eng"}},
        {"index": 2, "codec_type": "subtitle", "codec_name": "subrip",
         "tags": {"language": lang}},
    ]
    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", return_value={"streams": streams}), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[]), \
         patch.object(cs, "extract_track") as mock_extract, \
         patch.object(cs, "remux") as mock_remux:
        result = cs._process_locked(media, str(media), dry_run=True)
    assert result["status"] == "SKIP"
    assert "dry-run" in result["detail"]
    assert "would FIX" in result["detail"] and "embedded=1" in result["detail"]
    assert mock_extract.call_count == 0
    assert mock_remux.call_count == 0


# --- _expose_tag_normalize: destination .mkv lock + tag-return ---

def _mux_and_dest(tmp_path: Path, suffix: str):
    """A muxed temp output + a source path (.mkv: replacement == path; .mp4: a distinct .mkv)."""
    path = tmp_path / f"Movie (2024){suffix}"
    path.write_bytes(b"ORIGINAL")
    out = tmp_path / ".Movie.123.tmp.mkv"
    out.write_bytes(b"MUXED")
    return out, path, path.with_suffix(".mkv")


@pytest.mark.parametrize("suffix", [".mkv", ".mp4"])
def test_expose_tag_normalize_success(tmp_path, suffix):
    out, path, replacement = _mux_and_dest(tmp_path, suffix)   # .mp4: the replacement is a distinct .mkv
    with patch.object(cs, "set_consolidated_tag", return_value=True), \
         patch.object(cs, "_normalize_audio_inline") as norm:
        skip, tag_written = cs._expose_tag_normalize(out, path, replacement, str(path))
    assert skip is None and tag_written is True
    assert replacement.read_bytes() == b"MUXED"             # swapped into place
    assert path.exists() == (replacement == path)           # an original .mp4 is removed
    # normalize must inherit the destination lock, not `path`
    assert norm.call_args.kwargs["locked_path"] == replacement


def test_expose_tag_normalize_normalize_false_skips_normalize(tmp_path):
    """normalize=False (the cleanup's usage, with tag=False) skips normalization; the swap still happens."""
    out, path, replacement = _mux_and_dest(tmp_path, ".mkv")
    with patch.object(cs, "set_consolidated_tag") as tag, \
         patch.object(cs, "_normalize_audio_inline") as norm:
        skip, tag_written = cs._expose_tag_normalize(
            out, path, replacement, str(path), tag=False, normalize=False)
    assert skip is None and tag_written is False
    assert replacement.read_bytes() == b"MUXED"   # swap still happened
    assert tag.call_count == 0                     # tag=False: never attempted
    norm.assert_not_called()                       # normalize=False: never run


def test_expose_tag_normalize_skips_when_dest_locked(tmp_path):
    # a watcher/scan already holding the destination .mkv's lock: back off (SKIP), don't mutate
    from media_stack.locking import acquire_file_lock
    out, path, replacement = _mux_and_dest(tmp_path, ".mp4")
    with acquire_file_lock(replacement) as held:
        assert held
        with patch.object(cs, "set_consolidated_tag") as tag, \
             patch.object(cs, "_normalize_audio_inline") as norm:
            skip, tag_written = cs._expose_tag_normalize(out, path, replacement, str(path))
        assert skip is not None and skip["status"] == "SKIP"
        assert tag.call_count == 0 and norm.call_count == 0  # no mutation
        assert path.exists()                                 # original untouched


@pytest.mark.parametrize("tag_effect", [
    dict(return_value=False),
    dict(side_effect=RuntimeError("mkvpropedit boom")),
], ids=["tag-returns-false", "tag-raises"])
def test_expose_tag_normalize_tag_failure_does_not_roll_back(tmp_path, tag_effect):
    """Unlike normalize-audio, a failed tag write leaves the file consolidated (tag_written=False; state.json
    is the idempotency backstop) and normalization still runs."""
    out, path, replacement = _mux_and_dest(tmp_path, ".mkv")
    with patch.object(cs, "set_consolidated_tag", **tag_effect), \
         patch.object(cs, "_normalize_audio_inline") as norm:
        skip, tag_written = cs._expose_tag_normalize(out, path, replacement, str(path))
    assert skip is None and tag_written is False
    assert replacement.read_bytes() == b"MUXED"     # NOT rolled back
    norm.assert_called_once()


def test_expose_tag_normalize_collision_under_lock_fails(tmp_path):
    # a sibling .mkv that appeared between the pre-lock check and the destination lock: FAIL
    out, path, replacement = _mux_and_dest(tmp_path, ".mp4")  # replacement != path
    replacement.write_bytes(b"SIBLING")              # collision present under lock
    with patch.object(cs, "set_consolidated_tag", return_value=True), \
         patch.object(cs, "_normalize_audio_inline") as norm:
        early, tag_written = cs._expose_tag_normalize(out, path, replacement, str(path))
    assert early is not None and early["status"] == "FAIL"
    assert replacement.read_bytes() == b"SIBLING"    # not overwritten
    assert norm.call_count == 0                       # never reached normalize


# --- duration guard: the swap helper refuses an output shorter than the source ---

def _timed(src_duration, out_duration):
    """A `probe` fake: the temp output (`*.tmp.mkv`) gets `out_duration`, any other file `src_duration`
    (None = ffprobe reported no duration)."""
    def fake(p):
        d = out_duration if Path(p).name.endswith(".tmp.mkv") else src_duration
        return {"streams": [], "format": {} if d is None else {"duration": d}}
    return fake


@pytest.mark.parametrize("suffix", [".mkv", ".mp4"])
def test_expose_tag_normalize_refuses_an_output_shorter_than_the_source(tmp_path, suffix):
    out, path, replacement = _mux_and_dest(tmp_path, suffix)
    with patch.object(cs, "probe", side_effect=_timed("1320.0", "169.0")), \
         patch.object(cs, "set_consolidated_tag") as tag, \
         patch.object(cs, "_normalize_audio_inline") as norm:
        early, tag_written = cs._expose_tag_normalize(out, path, replacement, str(path))
    assert early is not None and early["status"] == "FAIL"
    assert early["detail"].startswith("duration verify: ")
    assert tag_written is False
    assert path.read_bytes() == b"ORIGINAL"                # original untouched
    assert replacement.exists() == (replacement == path)   # no .mkv created beside an .mp4
    assert not out.exists()                                # temp output removed
    assert tag.call_count == 0 and norm.call_count == 0


@pytest.mark.parametrize("src_duration, out_duration", [
    ("1320.0", "1320.0"),    # equal
    ("1320.0", "1307.0"),    # within 1%
    ("100.0", "98.0"),       # exactly the 2 s floor
    (None, "169.0"),         # source duration unknown: skipped, not a block
], ids=["equal", "within-tolerance", "exactly-the-floor", "source-duration-missing"])
def test_expose_tag_normalize_swaps_an_output_within_tolerance_or_unchecked(
        tmp_path, src_duration, out_duration):
    out, path, replacement = _mux_and_dest(tmp_path, ".mp4")
    with patch.object(cs, "probe", side_effect=_timed(src_duration, out_duration)), \
         patch.object(cs, "set_consolidated_tag", return_value=True), \
         patch.object(cs, "_normalize_audio_inline"):
        early, tag_written = cs._expose_tag_normalize(out, path, replacement, str(path))
    assert early is None and tag_written is True
    assert replacement.read_bytes() == b"MUXED"
    assert not path.exists()                                # the .mp4 original is replaced by the .mkv


@pytest.mark.parametrize("failing", ["source", "output"])
def test_expose_tag_normalize_refuses_when_a_file_cannot_be_probed(tmp_path, failing):
    """A transient ffprobe failure at the swap is not a licence to skip the check."""
    out, path, replacement = _mux_and_dest(tmp_path, ".mkv")

    def fake(p):
        unprobeable = out if failing == "output" else path
        return None if Path(p) == unprobeable else {"streams": [], "format": {"duration": "1320.0"}}

    with patch.object(cs, "probe", side_effect=fake), \
         patch.object(cs, "set_consolidated_tag") as tag, \
         patch.object(cs, "_normalize_audio_inline") as norm:
        early, tag_written = cs._expose_tag_normalize(out, path, replacement, str(path))
    assert early is not None and early["status"] == "FAIL"
    assert early["detail"] == f"duration verify: {failing} could not be probed"
    assert path.read_bytes() == b"ORIGINAL" and not out.exists()
    assert tag.call_count == 0 and norm.call_count == 0


_ENGLISH_SRT = ("1\n00:00:01,000 --> 00:00:03,000\nI don't know what you want from me.\n\n"
                "2\n00:00:04,000 --> 00:00:06,000\nWe should get out of here before they come back.\n")


def _full_pass(tmp_path, src_duration, out_duration):
    """The ordinary full pass through `_process_locked` (English sidecar muxed in), every external tool faked;
    the fake remux writes `MUXED` and the probes report the given container durations."""
    media = _media(tmp_path)
    sidecar = tmp_path / "Movie (2024).en.srt"
    sidecar.write_text(_ENGLISH_SRT)
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"},
               {"index": 1, "codec_type": "audio", "codec_name": "ac3", "tags": {"language": "eng"}}]

    def fake_probe(p):
        d = out_duration if Path(p).name.endswith(".tmp.mkv") else src_duration
        return {"streams": streams, "format": {} if d is None else {"duration": d}}

    def fake_remux(src, dst, *a, **k):
        Path(dst).write_bytes(b"MUXED")
        return True, "ok"

    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", side_effect=fake_probe), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "find_sidecar_subs", return_value=[(sidecar, False, False)]), \
         patch.object(cs, "run_ffsubsync", return_value=False), \
         patch.object(cs, "original_language_for", return_value=None), \
         patch.object(cs, "get_audio_lang_pref", return_value=({"eng"}, "eng", False)), \
         patch.object(cs, "select_keep_audio", return_value=[1]), \
         patch.object(cs, "remux", side_effect=fake_remux), \
         patch.object(cs, "set_consolidated_tag", return_value=True) as tag, \
         patch.object(cs, "_normalize_audio_inline") as norm, \
         patch.object(cs, "update_state_entry") as state:
        result = cs._process_locked(media, str(media), dry_run=False)
    return result, media, sidecar, tag, norm, state


def test_full_pass_remux_shorter_than_the_source_fails_and_leaves_the_original(tmp_path):
    """The Family Guy S19E01 case: 22 min in, a 2:49 mux out. The pass FAILs with the file, its sidecar and the
    state cache exactly as they were."""
    result, media, sidecar, tag, norm, state = _full_pass(tmp_path, "1320.0", "169.0")

    assert result["status"] == "FAIL" and result["detail"].startswith("duration verify: ")
    assert media.read_bytes() == b"\x1a\x45\xdf\xa3"           # original untouched
    assert not list(tmp_path.glob("*.tmp.mkv"))                  # tmp output removed
    assert sidecar.exists()                                      # subtitle not consumed
    assert tag.call_count == 0 and norm.call_count == 0
    state.assert_not_called()


@pytest.mark.parametrize("src_duration, out_duration", [
    ("1320.0", "1320.0"), ("1320.0", "1307.0"), (None, "169.0"),
], ids=["equal", "within-one-percent", "source-duration-missing"])
def test_full_pass_remux_as_long_as_the_source_or_unchecked_swaps(tmp_path, src_duration, out_duration):
    result, media, sidecar, tag, norm, _ = _full_pass(tmp_path, src_duration, out_duration)

    assert result["status"] == "FIXED"
    assert media.read_bytes() == b"MUXED"
    assert not sidecar.exists()                                  # consumed as usual
    tag.assert_called_once()


# --- _normalize_audio_inline: size-scaled timeout + process-group kill -------
# A fixed cap killed long runs, and killing only the python child orphaned its ffmpeg grandchildren.

@pytest.fixture
def _norm_log(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "LOG_FILE", tmp_path / "consolidate-subs.log")


def _fake_proc(communicate, returncode=0, pid=4242):
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = returncode
    proc.communicate.side_effect = communicate
    return proc


def _timeout(seconds=1):
    return subprocess.TimeoutExpired("normalize-audio.py", seconds)


def _fake_killpg(group_stays_alive=False):
    """Stand-in for `os.killpg`: the `sig == 0` liveness probe raises ProcessLookupError at once unless
    `group_stays_alive` (a member ignoring SIGTERM); real signals are recorded in `.signals`."""
    signals = []

    def killpg(pgid, sig):
        if sig == 0:
            if group_stays_alive:
                return
            raise ProcessLookupError
        signals.append((pgid, sig))

    killpg.signals = signals
    return killpg


def _fast_clock():
    """`time.monotonic` advancing 5 s per call plus a no-op `sleep`: the 15 s grace passes in three polls."""
    return (patch.object(cs.time, "monotonic", side_effect=iter(range(0, 10**6, 5))),
            patch.object(cs.time, "sleep"))


def test_normalize_inline_timeout_is_computed_from_the_file_size(tmp_path, _norm_log):
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"x" * 1234)
    proc = _fake_proc([("", "")])
    with patch.object(cs.subprocess, "Popen", return_value=proc), \
         patch.object(cs, "normalize_budget_s", return_value=12345) as budget:
        cs._normalize_audio_inline(media)
    budget.assert_called_once_with(1234)
    proc.communicate.assert_called_once_with(timeout=12345)


def test_normalize_inline_uses_the_real_budget_for_a_big_file(tmp_path, _norm_log):
    from media_stack.loudness import normalize_budget_s
    media = tmp_path / "Movie.mkv"
    with media.open("wb") as fh:
        fh.truncate(30 * 10**9)                      # sparse: 30 GB apparent size
    proc = _fake_proc([("", "")])
    with patch.object(cs.subprocess, "Popen", return_value=proc):
        cs._normalize_audio_inline(media)
    (_, kwargs), = proc.communicate.call_args_list
    assert kwargs["timeout"] == normalize_budget_s(30 * 10**9) == 31400   # well past the old 7200


def test_normalize_inline_falls_back_to_7200_when_stat_fails(tmp_path, _norm_log):
    proc = _fake_proc([("", "")])
    with patch.object(cs.subprocess, "Popen", return_value=proc):
        cs._normalize_audio_inline(tmp_path / "does-not-exist.mkv")
    proc.communicate.assert_called_once_with(timeout=7200)


def test_normalize_inline_runs_the_child_in_its_own_session_with_the_lock_env(
        tmp_path, _norm_log, monkeypatch):
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"x")
    monkeypatch.setenv("SOME_INHERITED_VAR", "kept")
    proc = _fake_proc([("", "")])
    with patch.object(cs.subprocess, "Popen", return_value=proc) as popen:
        cs._normalize_audio_inline(media, locked_path=tmp_path / "Movie.mkv")
    (argv,), kwargs = popen.call_args
    assert argv[0] == sys.executable and argv[-1] == str(media)
    assert Path(argv[1]).name == "normalize-audio.py"
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["NORMALIZE_INHERIT_LOCK_PATH"] == str(tmp_path / "Movie.mkv")
    assert kwargs["env"]["SOME_INHERITED_VAR"] == "kept"
    assert kwargs["stdout"] == subprocess.PIPE and kwargs["stderr"] == subprocess.PIPE
    assert kwargs["text"] is True


def test_normalize_inline_without_a_locked_path_sets_no_inherit_env(tmp_path, _norm_log, monkeypatch):
    monkeypatch.delenv("NORMALIZE_INHERIT_LOCK_PATH", raising=False)
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"x")
    with patch.object(cs.subprocess, "Popen", return_value=_fake_proc([("", "")])) as popen:
        cs._normalize_audio_inline(media)
    assert "NORMALIZE_INHERIT_LOCK_PATH" not in popen.call_args.kwargs["env"]


def test_normalize_inline_timeout_kills_the_process_group_with_sigterm(tmp_path, _norm_log, capsys):
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"x")
    # 1st communicate: the budget expires. 2nd: the final drain after the group is gone.
    proc = _fake_proc([_timeout(), ("", "")])
    killpg = _fake_killpg()
    with patch.object(cs.subprocess, "Popen", return_value=proc), \
         patch.object(cs, "normalize_budget_s", return_value=31400), \
         patch.object(cs.os, "killpg", side_effect=killpg):
        cs._normalize_audio_inline(media)                # must not raise
    assert killpg.signals == [(4242, signal.SIGTERM)]    # the group went away: no SIGKILL
    assert proc.communicate.call_args_list[1] == call(timeout=15)
    out = capsys.readouterr().out
    assert "audio-normalize TIMEOUT" in out and "process group killed" in out
    assert "8:43:20 budget" in out                       # the actual budget, not a hardcoded 2h
    assert "(2h)" not in out and str(media) in out
    assert "audio-normalize OK" not in out


@pytest.mark.parametrize("drain", [("", ""), _timeout(15)],
                         ids=["child-drained", "even-the-final-drain-times-out"])
def test_normalize_inline_timeout_sigkills_a_group_that_outlives_sigterm(tmp_path, _norm_log, drain):
    """The leader may exit on SIGTERM while a grandchild ignores it: the group, not the leader, decides
    whether SIGKILL is needed; and whatever the final drain does, nothing raises."""
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"x")
    proc = _fake_proc([_timeout(), drain])
    killpg = _fake_killpg(group_stays_alive=True)
    clock, sleep = _fast_clock()
    with patch.object(cs.subprocess, "Popen", return_value=proc), \
         patch.object(cs.os, "killpg", side_effect=killpg), clock, sleep:
        cs._normalize_audio_inline(media)
    assert killpg.signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    proc.poll.assert_called()                            # the leader is reaped while waiting
    assert proc.communicate.call_count == 2              # drained and reaped after the SIGKILL


def test_normalize_inline_never_raises_when_the_group_is_already_gone(tmp_path, _norm_log):
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"x")
    proc = _fake_proc([_timeout(), ("", "")])
    with patch.object(cs.subprocess, "Popen", return_value=proc), \
         patch.object(cs.os, "killpg", side_effect=ProcessLookupError):
        cs._normalize_audio_inline(media)


def test_normalize_inline_interrupt_kills_the_group_and_propagates(tmp_path, _norm_log):
    """Ctrl-C reaches us, not the child (own session): the group must be stopped and the interrupt not swallowed."""
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"x")
    proc = _fake_proc([KeyboardInterrupt(), ("", "")])
    killpg = _fake_killpg()
    with patch.object(cs.subprocess, "Popen", return_value=proc), \
         patch.object(cs.os, "killpg", side_effect=killpg):
        with pytest.raises(KeyboardInterrupt):
            cs._normalize_audio_inline(media)
    assert killpg.signals == [(4242, signal.SIGTERM)]


@pytest.fixture
def _sigterm_sentinel():
    """A recognisable SIGTERM handler standing in for the one installed before `_normalize_audio_inline`."""
    def sentinel(signum, frame):
        pass

    original = signal.signal(signal.SIGTERM, sentinel)
    yield sentinel
    signal.signal(signal.SIGTERM, original)


def test_normalize_inline_sigterm_during_the_wait_kills_the_group_and_restores_the_handler(
        tmp_path, _norm_log, _sigterm_sentinel):
    """The temporary handler raises, the child's group is killed, and the old handler is restored."""
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"x")
    seen = {}
    calls = []

    def communicate(timeout=None):
        calls.append(timeout)
        if len(calls) == 1:                              # the wait: SIGTERM arrives
            seen["handler"] = signal.getsignal(signal.SIGTERM)
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)                                # not reached: the handler raises
            raise AssertionError("the SIGTERM handler did not interrupt the wait")
        return ("", "")                                  # the drain after the kill

    proc = MagicMock(pid=4242, returncode=None)
    proc.communicate.side_effect = communicate
    killpg = _fake_killpg()
    with patch.object(cs.subprocess, "Popen", return_value=proc), \
         patch.object(cs.os, "killpg", side_effect=killpg):
        with pytest.raises(SystemExit) as exc:
            cs._normalize_audio_inline(media)
    assert exc.value.code == 128 + signal.SIGTERM
    assert seen["handler"] is not _sigterm_sentinel      # a temporary handler was in place
    assert killpg.signals == [(4242, signal.SIGTERM)]    # the child's group was stopped
    assert signal.getsignal(signal.SIGTERM) is _sigterm_sentinel   # ...and the old one restored


@pytest.mark.parametrize("communicate", [
    [("", "")],                                          # normal exit
    [_timeout(), ("", "")],                              # budget expired
])
def test_normalize_inline_restores_the_sigterm_handler_on_every_exit(
        tmp_path, _norm_log, _sigterm_sentinel, communicate):
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"x")
    with patch.object(cs.subprocess, "Popen", return_value=_fake_proc(communicate)), \
         patch.object(cs.os, "killpg", side_effect=_fake_killpg()):
        cs._normalize_audio_inline(media)
    assert signal.getsignal(signal.SIGTERM) is _sigterm_sentinel


def test_normalize_inline_popen_failure_is_logged_not_raised_and_restores_the_sigterm_handler(
        tmp_path, _norm_log, capsys, _sigterm_sentinel):
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"x")
    with patch.object(cs.subprocess, "Popen", side_effect=OSError("no such interpreter")):
        cs._normalize_audio_inline(media)
    assert "audio-normalize ERR" in capsys.readouterr().out
    assert signal.getsignal(signal.SIGTERM) is _sigterm_sentinel


def test_normalize_inline_off_the_main_thread_runs_without_a_sigterm_handler(
        tmp_path, _norm_log, capsys, _sigterm_sentinel):
    """`signal.signal` raises ValueError off the main thread: the handler is skipped and the run goes ahead."""
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"x")
    proc = _fake_proc([("ok\n", "")])
    with patch.object(cs.subprocess, "Popen", return_value=proc):
        worker = threading.Thread(target=cs._normalize_audio_inline, args=(media,))
        worker.start()
        worker.join(10)
    proc.communicate.assert_called_once()
    assert "audio-normalize OK" in capsys.readouterr().out
    assert signal.getsignal(signal.SIGTERM) is _sigterm_sentinel


@pytest.mark.parametrize("returncode, stdout, stderr, expected", [
    (0, "ok\n", "", "audio-normalize OK"),
    (0, "x: locked by other pipeline\n", "", "audio-normalize SKIPPED (lock conflict)"),
    (1, "", "boom\nlast stderr line\n", "audio-normalize FAIL rc=1"),
])
def test_normalize_inline_result_logging_is_unchanged(
        tmp_path, _norm_log, capsys, returncode, stdout, stderr, expected):
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"x")
    proc = _fake_proc([(stdout, stderr)], returncode=returncode)
    with patch.object(cs.subprocess, "Popen", return_value=proc), \
         patch.object(cs.os, "killpg") as killpg:
        cs._normalize_audio_inline(media)
    out = capsys.readouterr().out
    assert expected in out
    if returncode:
        assert "last stderr line" in out
    killpg.assert_not_called()                           # nothing to kill on a normal exit


def _running(pid: int) -> bool:
    try:   # a zombie (killed, not yet reaped by init) is not running
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


# Real (trivial) processes: a child in its own session spawns a sleeping
# grandchild, as normalize-audio.py spawns ffmpeg. The second variant's
# grandchild ignores SIGTERM and has its output redirected (so it does not hold
# the pipes open), which is the case a "leader exited" check would miss.
@pytest.mark.parametrize("script, ignores_term", [
    ("sleep 300 & echo $!; wait", False),
    ("(trap '' TERM; exec sleep 300 >/dev/null 2>&1) & echo $!; wait", True),
], ids=["grandchild-dies-on-sigterm", "grandchild-ignores-sigterm"])
def test_kill_process_group_takes_down_grandchildren(script, ignores_term):
    proc = subprocess.Popen(
        ["bash", "-c", script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    grandchild = int(proc.stdout.readline())
    time.sleep(0.2)                                      # let the subshell install its trap and exec
    try:
        t0 = time.monotonic()
        cs._kill_process_group(proc, grace_s=1)
        elapsed = time.monotonic() - t0

        deadline = time.monotonic() + 5
        while _running(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _running(grandchild), "the grandchild survived the group kill"
        assert proc.returncode is not None               # the child was reaped too
        if not ignores_term:
            assert elapsed < 0.9                         # nothing left to wait out the grace for
    finally:
        try:
            os.kill(grandchild, signal.SIGKILL)
        except ProcessLookupError:
            pass
