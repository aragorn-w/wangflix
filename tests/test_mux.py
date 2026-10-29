"""Tests for media_stack.mux.remux, the mkvmerge argv builder.

No real mkvmerge: subprocess.run is mocked. The full consolidate-subs call
(eng_srt given, cleanup_flags omitted) must keep building the exact same argv.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.mux import mkvmerge_remux_simple, remux, remux_timeout_s


def _ok_result():
    return MagicMock(returncode=0, stdout="", stderr="")


@pytest.fixture
def files(tmp_path):
    """(src, dst): dst is pre-sized so remux's post-check passes."""
    src = tmp_path / "src.mkv"
    src.write_bytes(b"x" * 1000)
    dst = tmp_path / "out.tmp.mkv"
    dst.write_bytes(b"x" * 2_000_000)
    return src, dst


def _argv(files, *args, **kwargs):
    """The mkvmerge argv `remux(src, dst, *args, **kwargs)` runs; asserts it reports success."""
    with patch("media_stack.mux.subprocess.run", return_value=_ok_result()) as run:
        ok, _ = remux(*files, *args, **kwargs)
    assert ok is True
    return run.call_args.args[0]


def test_remux_full_path_argv_unchanged(files, tmp_path):
    """Pins the exact mkvmerge argv for the full mux (external English srt + forced srt)."""
    src, dst = files
    eng_srt = tmp_path / "main.srt"
    forced_srt = tmp_path / "forced.srt"

    assert _argv(files, [0], [2, 1], eng_srt, forced_srt) == [
        "mkvmerge", "--quiet", "--output", str(dst),
        "--video-tracks", "0", "--default-track", "0:yes",
        "--audio-tracks", "2,1",
        "--default-track", "2:yes", "--default-track", "1:no",
        "--no-subtitles",
        str(src),
        "--language", "0:eng", "--track-name", "0:English",
        "--default-track", "0:yes", "--forced-track", "0:no", str(eng_srt),
        "--language", "0:eng", "--track-name", "0:Forced",
        "--default-track", "0:no", "--forced-track", "0:yes", str(forced_srt),
    ]


def test_remux_full_path_no_forced_srt_argv_unchanged(files, tmp_path):
    """Same full path without a forced track: no Forced block appended."""
    src, dst = files
    eng_srt = tmp_path / "main.srt"

    assert _argv(files, [0], [1], eng_srt, None) == [
        "mkvmerge", "--quiet", "--output", str(dst),
        "--video-tracks", "0", "--default-track", "0:yes",
        "--audio-tracks", "1", "--default-track", "1:yes",
        "--no-subtitles",
        str(src),
        "--language", "0:eng", "--track-name", "0:English",
        "--default-track", "0:yes", "--forced-track", "0:no", str(eng_srt),
    ]


def test_remux_kept_source_subs_stream_copies_them_ahead_of_the_external_srt(files, tmp_path):
    """The full path can keep a source subtitle (an embedded forced track whose
    text could not be read) unchanged, with explicit flags; video and audio are
    exactly the full path's, and without it the argv is the pinned one."""
    src, dst = files
    eng_srt = tmp_path / "main.srt"

    assert _argv(files, [0], [1], eng_srt, None,
                 kept_source_subs={3: {"default": False, "forced": True}}) == [
        "mkvmerge", "--quiet", "--output", str(dst),
        "--video-tracks", "0", "--default-track", "0:yes",
        "--audio-tracks", "1", "--default-track", "1:yes",
        "--subtitle-tracks", "3", "--default-track", "3:no", "--forced-track", "3:yes",
        str(src),
        "--language", "0:eng", "--track-name", "0:English",
        "--default-track", "0:yes", "--forced-track", "0:no", str(eng_srt),
    ]


def test_remux_kept_source_subs_does_not_change_the_argv_when_absent_or_empty(files, tmp_path):
    eng_srt = tmp_path / "main.srt"
    argvs = [_argv(files, [0], [1], eng_srt, None, **kwargs)
             for kwargs in ({}, {"kept_source_subs": None}, {"kept_source_subs": {}})]
    assert argvs[0] == argvs[1] == argvs[2] and "--no-subtitles" in argvs[0]


def test_remux_eng_track_name_defaults_to_english_and_can_be_overridden(files, tmp_path):
    """`eng_track_name` only renames the re-attached English subtitle; the default argv is unchanged."""
    eng_srt = tmp_path / "main.srt"

    default_argv = _argv(files, [0], [1], eng_srt, None)
    generated_argv = _argv(files, [0], [1], eng_srt, None, eng_track_name="English (generated)")

    assert default_argv[default_argv.index("--track-name") + 1] == "0:English"
    assert generated_argv[generated_argv.index("--track-name") + 1] == "0:English (generated)"
    renamed = [a if a != "0:English (generated)" else "0:English" for a in generated_argv]
    assert renamed == default_argv


def test_remux_generated_name_applies_to_the_main_track_not_the_forced_one(files, tmp_path):
    argv = _argv(files, [0], [1], tmp_path / "main.srt", tmp_path / "forced.srt",
                 eng_track_name="English (generated)")
    names = [argv[i + 1] for i, a in enumerate(argv) if a == "--track-name"]
    assert names == ["0:English (generated)", "0:Forced"]


@pytest.mark.parametrize("kwargs, subtitle_args", [
    pytest.param({"keep_subtitle_indices": [2, 5]}, ["--subtitle-tracks", "2,5"], id="keeps-source-subtitle-tracks"),
    pytest.param({}, ["--no-subtitles"], id="nothing-to-keep-drops-all"),
])
def test_remux_cleanup_mode_subtitle_selection(files, kwargs, subtitle_args):
    """eng_srt=None (cleanup mode): kept source subtitle indices go via --subtitle-tracks, else --no-subtitles."""
    src, dst = files
    assert _argv(files, [0], [1], None, None, **kwargs) == [
        "mkvmerge", "--quiet", "--output", str(dst),
        "--video-tracks", "0", "--default-track", "0:yes",
        "--audio-tracks", "1", "--default-track", "1:yes",
        *subtitle_args,
        str(src),
    ]


def test_remux_cleanup_mode_ignores_forced_srt(files, tmp_path):
    """forced_srt is ignored in cleanup mode: no external-attach block."""
    forced_srt = tmp_path / "forced.srt"
    argv = _argv(files, [0], [1], None, forced_srt, keep_subtitle_indices=[2])
    assert str(forced_srt) not in argv
    assert "--forced-track" not in argv


def test_remux_cleanup_flags_no_defaults_anywhere_emits_explicit_no(files):
    """A source with no default flag anywhere still gets an explicit default=no per kept track
    (mkvmerge auto-picks a default per type otherwise)."""
    src, dst = files
    cleanup_flags = {
        0: {"default": False},                    # video
        1: {"default": False},                    # audio
        2: {"default": False},                     # audio
        3: {"default": False, "forced": False},   # subtitle
    }

    assert _argv(files, [0], [1, 2], None, None,
                 keep_subtitle_indices=[3], cleanup_flags=cleanup_flags) == [
        "mkvmerge", "--quiet", "--output", str(dst),
        "--video-tracks", "0", "--default-track", "0:no",
        "--audio-tracks", "1,2",
        "--default-track", "1:no", "--default-track", "2:no",
        "--subtitle-tracks", "3",
        "--default-track", "3:no", "--forced-track", "3:no",
        str(src),
    ]


def test_remux_cleanup_flags_confident_pick_changes_only_audio_defaults(files):
    """A confident audio pick (id=2) changes only the audio defaults; video and subtitle flags mirror the source."""
    src, dst = files
    cleanup_flags = {
        0: {"default": True},                      # video: source default=yes, preserved
        1: {"default": False},                     # audio: was wrongly default -- now no
        2: {"default": True},                       # audio: the confident pick -- now yes
        3: {"default": False, "forced": True},      # subtitle: forced, not default (source)
    }

    assert _argv(files, [0], [1, 2], None, None,
                 keep_subtitle_indices=[3], cleanup_flags=cleanup_flags) == [
        "mkvmerge", "--quiet", "--output", str(dst),
        "--video-tracks", "0", "--default-track", "0:yes",
        "--audio-tracks", "1,2",
        "--default-track", "1:no", "--default-track", "2:yes",
        "--subtitle-tracks", "3",
        "--default-track", "3:no", "--forced-track", "3:yes",
        str(src),
    ]


def test_remux_cleanup_flags_subtitle_forced_flags_match_source(files):
    """Kept subtitles re-emit their source forced flags (track 4 forced, track 5 not)."""
    cleanup_flags = {
        0: {"default": True},
        1: {"default": True},
        4: {"default": False, "forced": True},
        5: {"default": False, "forced": False},
    }

    argv = _argv(files, [0], [1], None, None,
                 keep_subtitle_indices=[4, 5], cleanup_flags=cleanup_flags)
    i = argv.index("--subtitle-tracks")
    assert argv[i:i + 10] == [
        "--subtitle-tracks", "4,5",
        "--default-track", "4:no", "--forced-track", "4:yes",
        "--default-track", "5:no", "--forced-track", "5:no",
    ]


def test_remux_cleanup_flags_missing_entry_defaults_to_no(files):
    """A kept id missing from cleanup_flags defaults to default=no / forced=no."""
    argv = _argv(files, [0], [1], None, None, keep_subtitle_indices=[2], cleanup_flags={})
    assert argv[argv.index("--video-tracks"):argv.index("--video-tracks") + 4] == [
        "--video-tracks", "0", "--default-track", "0:no",
    ]
    assert "1:no" in argv  # audio
    assert "2:no" in argv  # subtitle default and forced, both no


def test_remux_upgrade_mode_explicit_av_flags_with_an_external_english_srt(files, tmp_path):
    """The generated -> real upgrade passes an external English srt and cleanup_flags:
    video/audio carry explicit default and forced, source subtitles are dropped, the new srt is default."""
    src, dst = files
    eng, forced = tmp_path / "eng.srt", tmp_path / "forced.srt"
    flags = {
        0: {"default": True, "forced": False},   # video
        3: {"default": False, "forced": False},  # second video
        1: {"default": False, "forced": False},  # audio, no default anywhere
        2: {"default": False, "forced": True},   # audio flagged forced
    }

    assert _argv(files, [0, 3], [1, 2], eng, forced, cleanup_flags=flags) == [
        "mkvmerge", "--quiet", "--output", str(dst),
        "--video-tracks", "0,3",
        "--default-track", "0:yes", "--forced-track", "0:no",
        "--default-track", "3:no", "--forced-track", "3:no",
        "--audio-tracks", "1,2",
        "--default-track", "1:no", "--forced-track", "1:no",
        "--default-track", "2:no", "--forced-track", "2:yes",
        "--no-subtitles", str(src),
        "--language", "0:eng", "--track-name", "0:English",
        "--default-track", "0:yes", "--forced-track", "0:no", str(eng),
        "--language", "0:eng", "--track-name", "0:Forced",
        "--default-track", "0:no", "--forced-track", "0:yes", str(forced),
    ]


def test_remux_cleanup_flags_without_a_forced_key_emit_no_forced_track_for_av(files):
    """Cleanup video/audio entries carry only "default": no --forced-track."""
    argv = _argv(files, [0], [1], None, None, keep_subtitle_indices=[2],
                 cleanup_flags={0: {"default": True}, 1: {"default": True},
                                2: {"default": False, "forced": True}})
    assert argv.count("--forced-track") == 1 and "2:yes" in argv


def test_remux_upgrade_mode_keeps_source_subtitles_then_appends_the_new_main(files, tmp_path):
    """The upgrade keeps source forced English tracks via --subtitle-tracks with their exact
    flags; mkvmerge writes them first and the external srt after (checked against real mkvmerge)."""
    src, dst = files
    eng = tmp_path / "eng.srt"
    flags = {
        0: {"default": True, "forced": False},
        1: {"default": True, "forced": False},
        3: {"default": False, "forced": True},   # a forced PGS
        4: {"default": True, "forced": True},    # another, with its own (odd) flags
    }

    assert _argv(files, [0], [1], eng, None, keep_subtitle_indices=[3, 4], cleanup_flags=flags) == [
        "mkvmerge", "--quiet", "--output", str(dst),
        "--video-tracks", "0", "--default-track", "0:yes", "--forced-track", "0:no",
        "--audio-tracks", "1", "--default-track", "1:yes", "--forced-track", "1:no",
        "--subtitle-tracks", "3,4",
        "--default-track", "3:no", "--forced-track", "3:yes",
        "--default-track", "4:yes", "--forced-track", "4:yes",
        str(src),
        "--language", "0:eng", "--track-name", "0:English",
        "--default-track", "0:yes", "--forced-track", "0:no", str(eng),
    ]


def test_remux_full_path_still_drops_every_source_subtitle_when_keep_comes_without_flags(files, tmp_path):
    """An external srt plus kept indices but no explicit flags still drops all source subtitles."""
    argv = _argv(files, [0], [1], tmp_path / "eng.srt", None, keep_subtitle_indices=[3])
    assert "--no-subtitles" in argv and "--subtitle-tracks" not in argv


def test_remux_timeout_formula():
    # 30-min floor, then 1 s per 15 MB on top of 20 min.
    assert remux_timeout_s(0) == 1800
    assert remux_timeout_s(30 * 10**9) == 3200
    assert remux_timeout_s(70 * 10**9) == 5866


def test_both_remux_paths_use_the_shared_timeout(tmp_path):
    src = tmp_path / "src.mkv"
    with src.open("wb") as fh:
        fh.truncate(30 * 10**9)                        # sparse: 30 GB apparent size
    dst = tmp_path / "out.mkv"
    seen = []

    def fake_run(cmd, *a, **kw):
        seen.append(kw["timeout"])
        dst.write_bytes(b"x" * 2_000_000)
        return _ok_result()

    with patch("media_stack.mux.subprocess.run", side_effect=fake_run):
        mkvmerge_remux_simple(src, dst)
        remux(src, dst, [0], [1], None, None)
    assert seen == [remux_timeout_s(30 * 10**9)] * 2 == [3200, 3200]
