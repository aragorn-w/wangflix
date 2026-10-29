"""media_stack.loudness: the loudnorm pass-2 ffmpeg argv, with subprocess.run mocked."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.loudness import (
    measure_loudness, normalize_budget_s, pass1_timeout_s, pass2_timeout_s, render_normalized,
)
from media_stack.mux import remux_timeout_s

MEASURED = {
    "input_i": "-21.0", "input_lra": "7.0", "input_tp": "-2.0",
    "input_thresh": "-31.0", "target_offset": "0.0",
}


def _streams(*specs):
    """specs: (index, codec_type, codec_name, attached_pic)"""
    return [{"index": i, "codec_type": t, "codec_name": c,
             "disposition": {"attached_pic": 1} if ap else {}}
            for i, t, c, ap in specs]


def _run_pass2(tmp_path, streams, audio_index=0, channels=6, layout="5.1"):
    """Invoke render_normalized with ffprobe + ffmpeg mocked; return argv."""
    src = tmp_path / "in.mkv"
    src.write_bytes(b"X" * 200_000)
    dst = tmp_path / "out.mkv"
    captured = {}

    def fake_run(cmd, *a, **kw):
        captured["cmd"] = cmd
        dst.write_bytes(b"Y" * 200_000)          # satisfy the size sanity check
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("media_stack.loudness.ffprobe_strict",
               return_value={"streams": streams}), \
         patch("media_stack.loudness.subprocess.run", side_effect=fake_run):
        render_normalized(src, dst, audio_index, channels, MEASURED, layout)
    return captured["cmd"]


def _video_maps(cmd):
    return [cmd[i + 1] for i, a in enumerate(cmd)
            if a == "-map" and cmd[i + 1].startswith("0:")
            and not cmd[i + 1].startswith(("0:a", "0:s", "0:t"))]


@pytest.mark.parametrize("specs, expected_maps", [
    # the regression: `-map 0:v` carried a stray cover-art track over, undoing consolidate's strip
    ([(0, "video", "hevc", False), (1, "video", "mjpeg", False), (2, "audio", "aac", False)], ["0:0"]),
    ([(0, "video", "hevc", False), (1, "video", "mjpeg", False),
      (2, "audio", "aac", False), (3, "audio", "ac3", False)], ["0:0"]),
    ([(0, "video", "hevc", False), (1, "audio", "aac", False)], ["0:0"]),
    ([(0, "video", "hevc", False), (1, "video", "mjpeg", True), (2, "audio", "aac", False)], ["0:0"]),
    ([(0, "video", "hevc", False), (1, "video", "h264", False), (2, "video", "png", False),
      (3, "audio", "aac", False)], ["0:0", "0:1"]),
    ([(0, "video", "mjpeg", False), (1, "video", "hevc", False), (2, "audio", "aac", False)], ["0:1"]),
], ids=["cover-art-dropped", "cover-art-dropped-two-audio", "plain-file", "attached-pic-dropped",
        "both-real-video-kept", "video-index-is-absolute"])
def test_pass2_maps_only_real_video(tmp_path, specs, expected_maps):
    cmd = _run_pass2(tmp_path, _streams(*specs))
    assert "0:v" not in cmd, "bare 0:v re-maps every video stream"
    assert _video_maps(cmd) == expected_maps
    audio = sum(1 for _i, kind, _c, _ap in specs if kind == "audio")
    assert _audio_maps(cmd) == [f"0:a:{i}" for i in range(audio)]   # audio stays relative


@pytest.mark.parametrize("streams, kwargs, match", [
    ([(0, "video", "mjpeg", False), (1, "audio", "aac", False)], {}, "no real video stream"),
    ([(0, "video", "hevc", False), (1, "audio", "aac", False)], {"audio_index": 3}, "audio_index"),
    ([(0, "video", "hevc", False)], {}, "audio_index"),
], ids=["no-real-video", "audio-index-out-of-range", "no-audio"])
def test_pass2_raises(tmp_path, streams, kwargs, match):
    with pytest.raises(RuntimeError, match=match):
        _run_pass2(tmp_path, _streams(*streams), **kwargs)


# --- audio-track preservation: pass 2 once mapped only the primary and destroyed a kept dub ---

def _audio_maps(cmd):
    return [cmd[i + 1] for i, a in enumerate(cmd)
            if a == "-map" and cmd[i + 1].startswith("0:a:")]


def _arg_after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def _dual_audio_streams():
    return _streams(
        (0, "video", "hevc", False),
        (1, "audio", "aac", False),      # primary (original language)
        (2, "audio", "ac3", False),      # English dub
    )


def test_pass2_keeps_every_audio_track_and_reencodes_only_the_primary(tmp_path):
    cmd = _run_pass2(tmp_path, _dual_audio_streams())
    assert _audio_maps(cmd) == ["0:a:0", "0:a:1"]
    assert _arg_after(cmd, "-c:a:0") == "aac"
    assert _arg_after(cmd, "-c:a:1") == "copy"
    assert "-filter:a:0" in cmd, "loudnorm must be applied to the primary"
    assert "-filter:a:1" not in cmd, "loudnorm must not touch a copied track"
    for bad in ("-c:a", "-af", "-ac", "-b:a", "-ar"):   # a bare flag also hits the copied tracks
        assert bad not in cmd, f"{bad} applies to every audio stream"


def test_pass2_honours_which_track_is_primary(tmp_path):
    """audio_index selects which mapped track gets loudnorm, not which is kept."""
    cmd = _run_pass2(tmp_path, _dual_audio_streams(), audio_index=1)
    assert _audio_maps(cmd) == ["0:a:0", "0:a:1"]
    assert _arg_after(cmd, "-c:a:0") == "copy"
    assert _arg_after(cmd, "-c:a:1") == "aac"
    assert "-filter:a:1" in cmd
    assert "-filter:a:0" not in cmd


def test_pass2_single_audio_file_still_reencodes(tmp_path):
    cmd = _run_pass2(tmp_path, _streams(
        (0, "video", "hevc", False),
        (1, "audio", "aac", False),
    ))
    assert _audio_maps(cmd) == ["0:a:0"]
    assert _arg_after(cmd, "-c:a:0") == "aac"


def test_pass2_downmix_targets_only_the_primary(tmp_path):
    cmd = _run_pass2(tmp_path, _streams(
        (0, "video", "hevc", False),
        (1, "audio", "truehd", False),
        (2, "audio", "ac3", False),
    ), channels=8, layout="FL+FR+FC+LFE+SL+SR+TFL+TFR")
    assert _arg_after(cmd, "-ac:a:0") == "6"
    assert "-ac" not in cmd
    assert "-ac:a:1" not in cmd


# --- output sample rate: loudnorm emits 192 kHz and the Shield's AAC decoder tops out at 48 kHz ---

def _with_rates(streams, **rates):
    """Set `sample_rate` on audio streams by index: _with_rates(s, **{"1": "44100"})."""
    for s in streams:
        rate = rates.get(str(s["index"]))
        if rate is not None:
            s["sample_rate"] = rate
    return streams


@pytest.mark.parametrize("src_rate, expected", [
    ("48000", "48000"),
    ("44100", "44100"),
    ("32000", "32000"),
    ("22050", "22050"),
    ("96000", "48000"),
    ("192000", "48000"),
    ("88200", "48000"),
    # Not a rate the AAC encoder accepts: passing it makes ffmpeg fail outright.
    ("44056", "48000"),
    ("47250", "48000"),
])
def test_pass2_sets_output_rate_from_source_capped_at_48k(tmp_path, src_rate, expected):
    cmd = _run_pass2(tmp_path, _with_rates(_streams(
        (0, "video", "hevc", False),
        (1, "audio", "eac3", False),
    ), **{"1": src_rate}))
    assert _arg_after(cmd, "-ar:a:0") == expected


@pytest.mark.parametrize("bad_rate", [None, "", "0", "-44100", "N/A", "garbage"])
def test_pass2_falls_back_to_48k_when_source_rate_is_unusable(tmp_path, bad_rate):
    streams = _streams((0, "video", "hevc", False), (1, "audio", "eac3", False))
    if bad_rate is not None:
        streams[1]["sample_rate"] = bad_rate
    cmd = _run_pass2(tmp_path, streams)
    assert _arg_after(cmd, "-ar:a:0") == "48000"


def test_pass2_output_rate_is_on_the_reencoded_stream_only(tmp_path):
    cmd = _run_pass2(tmp_path, _with_rates(_dual_audio_streams(),
                                           **{"1": "96000", "2": "44100"}))
    assert _arg_after(cmd, "-ar:a:0") == "48000"
    assert "-ar:a:1" not in cmd, "a stream-copied track cannot take a sample rate"
    assert "-ar" not in cmd, "a bare -ar applies to every audio stream"
    assert cmd.count("-ar:a:0") == 1


def test_pass2_output_rate_follows_the_primary_not_the_first_track(tmp_path):
    cmd = _run_pass2(tmp_path, _with_rates(_dual_audio_streams(),
                                           **{"1": "96000", "2": "44100"}),
                     audio_index=1)
    assert _arg_after(cmd, "-ar:a:1") == "44100"
    assert "-ar:a:0" not in cmd
    assert "-ar" not in cmd


# --- timeouts: shared helpers + the outer normalize budget ----------------

GB = 10**9


def test_step_budgets_keep_their_formulas():
    # 1 h floor on both passes; pass 1 ~1 min per 100 MB, pass 2 ~1 s per 5 MB
    assert pass1_timeout_s(0) == pass2_timeout_s(0) == 3600
    assert pass1_timeout_s(30 * GB) == 19800      # 30000 * 0.6 + 1800
    assert pass2_timeout_s(30 * GB) == 7800       # 30000 / 5 + 1800
    assert pass1_timeout_s(80 * GB) == 49800
    assert pass2_timeout_s(80 * GB) == 17800


def test_normalize_budget_is_the_sum_of_the_step_budgets_plus_a_margin():
    for size in (0, 5 * GB, 30 * GB, 35 * GB, 80 * GB):
        assert normalize_budget_s(size) == (
            pass1_timeout_s(size) + pass2_timeout_s(size) + remux_timeout_s(size) + 600)
    assert normalize_budget_s(0) == 3600 + 3600 + 1800 + 600
    assert normalize_budget_s(30 * GB) == 19800 + 7800 + 3200 + 600


def test_normalize_budget_covers_the_films_that_the_old_2h_cap_killed():
    # 27-35 GB were the 2026-10-23 casualties of the old 2 h cap
    budgets = [normalize_budget_s(g * GB) for g in (27, 30, 35)]
    assert all(b > 7200 for b in budgets) and budgets == sorted(budgets)


def _sparse(path: Path, size: int) -> Path:
    with path.open("wb") as fh:
        fh.truncate(size)
    return path


def test_pass1_runs_under_the_shared_size_scaled_timeout(tmp_path):
    src = _sparse(tmp_path / "in.mkv", 30 * GB)
    seen = {}

    def fake_run(cmd, *a, **kw):
        seen["timeout"] = kw["timeout"]
        stderr = ('{"input_i": "-20", "input_tp": "-1", "input_lra": "5", '
                  '"input_thresh": "-30", "target_offset": "0"}')
        return subprocess.CompletedProcess(cmd, 0, "", stderr)

    with patch("media_stack.loudness.subprocess.run", side_effect=fake_run):
        measure_loudness(src, 0)
    assert seen["timeout"] == pass1_timeout_s(30 * GB) == 19800


def test_pass2_runs_under_the_shared_size_scaled_timeout(tmp_path):
    src = _sparse(tmp_path / "in.mkv", 30 * GB)
    dst = tmp_path / "out.mkv"
    seen = {}

    def fake_run(cmd, *a, **kw):
        seen["timeout"] = kw["timeout"]
        dst.write_bytes(b"Y" * 200_000)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("media_stack.loudness.ffprobe_strict",
               return_value={"streams": _streams((0, "video", "hevc", False),
                                                 (1, "audio", "aac", False))}), \
         patch("media_stack.loudness.subprocess.run", side_effect=fake_run):
        render_normalized(src, dst, 0, 2, MEASURED, "stereo")
    assert seen["timeout"] == pass2_timeout_s(30 * GB) == 7800
