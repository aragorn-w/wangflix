"""Tests for media_stack.mux.remux — the mkvmerge argv builder.

No real mkvmerge: subprocess.run is mocked. The full consolidate-subs
call site (eng_srt given, cleanup_flags omitted) must build the EXACT
SAME argv it always has — these pin that across every cleanup-mode
addition (eng_srt=None subtitle-keep, then cleanup_flags for explicit
per-track default/forced flags, codex round 4 #2).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.mux import remux


def _ok_result():
    return MagicMock(returncode=0, stdout="", stderr="")


def test_remux_full_path_argv_unchanged(tmp_path):
    """Pins the exact mkvmerge argv for the full consolidate-subs mux
    (external English srt + forced srt) — must stay byte-for-byte
    identical after adding the eng_srt=None cleanup mode."""
    src = tmp_path / "src.mkv"
    src.write_bytes(b"x" * 1000)
    dst = tmp_path / "out.tmp.mkv"
    dst.write_bytes(b"x" * 2_000_000)  # pre-sized so the post-check passes
    eng_srt = tmp_path / "main.srt"
    forced_srt = tmp_path / "forced.srt"

    with patch("media_stack.mux.subprocess.run", return_value=_ok_result()) as run:
        ok, detail = remux(src, dst, [0], [2, 1], eng_srt, forced_srt)

    assert ok is True
    expected = [
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
    assert run.call_args.args[0] == expected


def test_remux_full_path_no_forced_srt_argv_unchanged(tmp_path):
    """Same full path, no forced track (the common case) — no Forced
    block appended."""
    src = tmp_path / "src.mkv"
    src.write_bytes(b"x" * 1000)
    dst = tmp_path / "out.tmp.mkv"
    dst.write_bytes(b"x" * 2_000_000)
    eng_srt = tmp_path / "main.srt"

    with patch("media_stack.mux.subprocess.run", return_value=_ok_result()) as run:
        ok, detail = remux(src, dst, [0], [1], eng_srt, None)

    assert ok is True
    expected = [
        "mkvmerge", "--quiet", "--output", str(dst),
        "--video-tracks", "0", "--default-track", "0:yes",
        "--audio-tracks", "1", "--default-track", "1:yes",
        "--no-subtitles",
        str(src),
        "--language", "0:eng", "--track-name", "0:English",
        "--default-track", "0:yes", "--forced-track", "0:no", str(eng_srt),
    ]
    assert run.call_args.args[0] == expected


def test_remux_cleanup_mode_keeps_source_subtitle_tracks(tmp_path):
    """eng_srt=None (cleanup mode): no external srt attached; the given
    SOURCE subtitle track indices are kept via --subtitle-tracks instead
    of being stripped."""
    src = tmp_path / "src.mkv"
    src.write_bytes(b"x" * 1000)
    dst = tmp_path / "out.tmp.mkv"
    dst.write_bytes(b"x" * 2_000_000)

    with patch("media_stack.mux.subprocess.run", return_value=_ok_result()) as run:
        ok, detail = remux(src, dst, [0], [1], None, None, keep_subtitle_indices=[2, 5])

    assert ok is True
    expected = [
        "mkvmerge", "--quiet", "--output", str(dst),
        "--video-tracks", "0", "--default-track", "0:yes",
        "--audio-tracks", "1", "--default-track", "1:yes",
        "--subtitle-tracks", "2,5",
        str(src),
    ]
    assert run.call_args.args[0] == expected


def test_remux_cleanup_mode_no_subtitles_to_keep_drops_all(tmp_path):
    """eng_srt=None with no keep_subtitle_indices → --no-subtitles, same
    as dropping everything."""
    src = tmp_path / "src.mkv"
    src.write_bytes(b"x" * 1000)
    dst = tmp_path / "out.tmp.mkv"
    dst.write_bytes(b"x" * 2_000_000)

    with patch("media_stack.mux.subprocess.run", return_value=_ok_result()) as run:
        ok, detail = remux(src, dst, [0], [1], None, None)

    assert ok is True
    expected = [
        "mkvmerge", "--quiet", "--output", str(dst),
        "--video-tracks", "0", "--default-track", "0:yes",
        "--audio-tracks", "1", "--default-track", "1:yes",
        "--no-subtitles",
        str(src),
    ]
    assert run.call_args.args[0] == expected


def test_remux_cleanup_mode_ignores_forced_srt(tmp_path):
    """forced_srt is meaningless without an external main srt — the
    external-attach block must not appear at all in cleanup mode even
    if a caller passes forced_srt by mistake."""
    src = tmp_path / "src.mkv"
    src.write_bytes(b"x" * 1000)
    dst = tmp_path / "out.tmp.mkv"
    dst.write_bytes(b"x" * 2_000_000)
    forced_srt = tmp_path / "forced.srt"

    with patch("media_stack.mux.subprocess.run", return_value=_ok_result()) as run:
        remux(src, dst, [0], [1], None, forced_srt, keep_subtitle_indices=[2])

    argv = run.call_args.args[0]
    assert str(forced_srt) not in argv
    assert "--forced-track" not in argv


def test_remux_cleanup_flags_no_defaults_anywhere_emits_explicit_no(tmp_path):
    """Codex round 4 #2: a source with NO default flag anywhere still
    gets an EXPLICIT default=no for every kept video/audio/subtitle
    track. mkvmerge auto-picks a default per TYPE when the source has
    none at all for it, so omitting the override would NOT mean
    "unchanged" -- every kept track must be stated explicitly."""
    src = tmp_path / "src.mkv"
    src.write_bytes(b"x" * 1000)
    dst = tmp_path / "out.tmp.mkv"
    dst.write_bytes(b"x" * 2_000_000)
    cleanup_flags = {
        0: {"default": False},                    # video
        1: {"default": False},                    # audio
        2: {"default": False},                     # audio
        3: {"default": False, "forced": False},   # subtitle
    }

    with patch("media_stack.mux.subprocess.run", return_value=_ok_result()) as run:
        ok, detail = remux(
            src, dst, [0], [1, 2], None, None,
            keep_subtitle_indices=[3], cleanup_flags=cleanup_flags,
        )

    assert ok is True
    expected = [
        "mkvmerge", "--quiet", "--output", str(dst),
        "--video-tracks", "0", "--default-track", "0:no",
        "--audio-tracks", "1,2",
        "--default-track", "1:no", "--default-track", "2:no",
        "--subtitle-tracks", "3",
        "--default-track", "3:no", "--forced-track", "3:no",
        str(src),
    ]
    assert run.call_args.args[0] == expected


def test_remux_cleanup_flags_confident_pick_changes_only_audio_defaults(tmp_path):
    """A confident audio pick (id=2) changes ONLY the audio defaults --
    video's own source default (preserved as True here) and the
    subtitle's own source forced/default flags are re-emitted exactly
    as given, untouched by the audio decision."""
    src = tmp_path / "src.mkv"
    src.write_bytes(b"x" * 1000)
    dst = tmp_path / "out.tmp.mkv"
    dst.write_bytes(b"x" * 2_000_000)
    cleanup_flags = {
        0: {"default": True},                      # video: source default=yes, preserved
        1: {"default": False},                     # audio: was wrongly default -- now no
        2: {"default": True},                       # audio: the confident pick -- now yes
        3: {"default": False, "forced": True},      # subtitle: forced, not default (source)
    }

    with patch("media_stack.mux.subprocess.run", return_value=_ok_result()) as run:
        remux(src, dst, [0], [1, 2], None, None,
              keep_subtitle_indices=[3], cleanup_flags=cleanup_flags)

    expected = [
        "mkvmerge", "--quiet", "--output", str(dst),
        "--video-tracks", "0", "--default-track", "0:yes",
        "--audio-tracks", "1,2",
        "--default-track", "1:no", "--default-track", "2:yes",
        "--subtitle-tracks", "3",
        "--default-track", "3:no", "--forced-track", "3:yes",
        str(src),
    ]
    assert run.call_args.args[0] == expected


def test_remux_cleanup_flags_subtitle_forced_flags_match_source(tmp_path):
    """Subtitle forced flags are re-emitted exactly as in the source for
    every kept subtitle, independent of the audio/video decisions --
    here track 4 (forced) and track 5 (not forced) are both kept."""
    src = tmp_path / "src.mkv"
    src.write_bytes(b"x" * 1000)
    dst = tmp_path / "out.tmp.mkv"
    dst.write_bytes(b"x" * 2_000_000)
    cleanup_flags = {
        0: {"default": True},
        1: {"default": True},
        4: {"default": False, "forced": True},
        5: {"default": False, "forced": False},
    }

    with patch("media_stack.mux.subprocess.run", return_value=_ok_result()) as run:
        remux(src, dst, [0], [1], None, None,
              keep_subtitle_indices=[4, 5], cleanup_flags=cleanup_flags)

    argv = run.call_args.args[0]
    i = argv.index("--subtitle-tracks")
    assert argv[i:i + 10] == [
        "--subtitle-tracks", "4,5",
        "--default-track", "4:no", "--forced-track", "4:yes",
        "--default-track", "5:no", "--forced-track", "5:no",
    ]


def test_remux_cleanup_flags_missing_entry_defaults_to_no(tmp_path):
    """A kept id with no entry in cleanup_flags (a caller bug) is
    treated conservatively as default=no / forced=no -- never "yes" by
    accident."""
    src = tmp_path / "src.mkv"
    src.write_bytes(b"x" * 1000)
    dst = tmp_path / "out.tmp.mkv"
    dst.write_bytes(b"x" * 2_000_000)

    with patch("media_stack.mux.subprocess.run", return_value=_ok_result()) as run:
        remux(src, dst, [0], [1], None, None, keep_subtitle_indices=[2], cleanup_flags={})

    argv = run.call_args.args[0]
    assert argv[argv.index("--video-tracks"):argv.index("--video-tracks") + 4] == [
        "--video-tracks", "0", "--default-track", "0:no",
    ]
    assert "1:no" in argv  # audio
    assert "2:no" in argv  # subtitle default and forced, both no
