"""Tests for media_stack.subtitles — scoring, cleaning, classification."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack import subtitles as st
from media_stack.subtitles import (
    clean_srt_text, decode_subtitle_bytes, decode_subtitle_text, find_sidecar_subs,
    is_forced_track, is_generated_sidecar, is_generated_track, is_sdh_track,
    normalize_subtitle_caps, score_text_track,
)


# --- classification ---

@pytest.mark.parametrize("stream, expected", [
    ({"disposition": {"forced": 1}}, True),
    ({"tags": {"title": "Forced Signs"}}, True),
    ({"disposition": {}, "tags": {"title": "English"}}, False),
], ids=["via-disposition", "via-title", "not-forced"])
def test_is_forced_track(stream, expected):
    assert is_forced_track(stream) is expected


def test_sdh_markers_are_matched_as_whole_tokens_case_insensitively():
    for title in ("English SDH", "English (CC)", "English HI", "english (hi)", "English - HI",
                  "Hearing Impaired", "English [Hearing-Impaired]", "Hard of Hearing",
                  "Closed Captions", "closed caption", "SDH", "cc", "English cc"):
        assert is_sdh_track({"tags": {"title": title}}), title


def test_words_that_merely_contain_hi_or_cc_are_not_sdh():
    """"hi"/"cc" inside a longer word is not a hearing-impaired marker."""
    for title in ("English/Chinese", "Chinese", "Hindi", "Philippines", "this", "English Archive",
                  "Accent", "English (Uncut)", "Hearing Aid Documentary", "",
                  "Delhi", "Sushi", "English"):  # "hi" at the END of a word: the left boundary matters too
        assert not is_sdh_track({"tags": {"title": title}}), title
    assert not is_sdh_track({"tags": {}})
    assert not is_sdh_track({})


def test_hearing_impaired_disposition_alone_marks_a_track_sdh():
    assert is_sdh_track({"disposition": {"hearing_impaired": 1}, "tags": {"title": "English"}})
    assert not is_sdh_track({"disposition": {"hearing_impaired": 0}, "tags": {"title": "English"}})


# --- scoring ---

def test_text_score_prefers_subrip():
    s_srt = {"codec_name": "subrip", "disposition": {}}
    s_ass = {"codec_name": "ass", "disposition": {}}
    assert score_text_track(s_srt, 1000) > score_text_track(s_ass, 1000)


def test_text_score_gives_sdh_no_bonus():
    """SDH is ranked down by consolidate-subs.py's candidate sort, never scored up."""
    base = {"codec_name": "subrip", "disposition": {}, "tags": {}}
    sdh = {"codec_name": "subrip", "disposition": {}, "tags": {"title": "SDH"}}
    assert score_text_track(sdh, 1000) == score_text_track(base, 1000)


def test_text_score_penalizes_forced():
    base = {"codec_name": "subrip", "disposition": {}, "tags": {}}
    forced = {"codec_name": "subrip", "disposition": {"forced": 1}, "tags": {}}
    assert score_text_track(base, 1000) > score_text_track(forced, 1000)


def test_text_score_increases_with_line_count():
    s = {"codec_name": "subrip", "disposition": {}, "tags": {}}
    assert score_text_track(s, 100) < score_text_track(s, 1000)


# --- cleaning ---

@pytest.mark.parametrize("cue_text, absent, present", [
    ("{\\b1}Hello{\\b0}", ["{\\b1}", "{\\b0}"], "Hello"),
    ("<font color=red>Hi</font>", ["<font", "</font>"], "Hi"),
    ("Hi\\hthere", ["\\h"], "Hi there"),
], ids=["ass-overrides", "html-font-tags", "literal-backslash-h"])
def test_clean_strips_markup(cue_text, absent, present):
    out = clean_srt_text(f"1\n00:00:01,000 --> 00:00:02,000\n{cue_text}\n")
    for marker in absent:
        assert marker not in out
    assert present in out


def test_clean_strips_bom():
    text = "﻿1\n00:00:01,000 --> 00:00:02,000\nHi\n"
    out = clean_srt_text(text)
    assert not out.startswith("﻿")


# --- ALL-CAPS recasing ---

@pytest.mark.parametrize("text", ["Hello World", "OK", "00:00:01,000 --> 00:00:02,000"],
                         ids=["already-cased", "short-line", "timing-line"])
def test_recase_leaves_line_unchanged(text):
    assert normalize_subtitle_caps(text) == text


def test_recase_lowers_all_caps():
    text = "THIS IS AN ALL-CAPS LINE."
    out = normalize_subtitle_caps(text)
    assert "this" in out.lower()
    assert out[0].isupper()  # first letter capitalized


def test_recase_handles_dialogue_dashes():
    text = "- HELLO THERE FRIEND."
    out = normalize_subtitle_caps(text)
    # First alphabetic char (the H) should be capitalized, rest lower
    assert "Hello there friend." in out


def test_recase_caps_lone_i():
    # Input must be ALL-CAPS to trigger the recase path.
    text = "WHEN I SAW IT EARLIER TODAY."
    out = normalize_subtitle_caps(text)
    # Lone i → I after lowercase+sentence-recase pass
    assert " I " in out
    # Other words ended up lowercase (proves the recase ran)
    assert "saw" in out


@pytest.fixture
def movie(tmp_path):
    video = tmp_path / "Movie (2024).mkv"
    video.touch()
    return video


@pytest.mark.parametrize("sidecar, expected", [
    ("Movie (2024).srt", (False, False)),  # no lang code: accepted as English
    ("Movie (2024).en.sdh.srt", (False, True)),
    ("Movie (2024).en.forced.srt", (True, False)),
    ("Movie (2024).en.generated.srt", (False, False)),  # sub-fallback's Whisper output
    ("Movie (2024).fr.srt", None),
], ids=["bare", "en-sdh", "en-forced", "en-generated", "french-excluded"])
def test_find_sidecar_classifies_a_single_sidecar(tmp_path, movie, sidecar, expected):
    (tmp_path / sidecar).write_text("x")
    assert find_sidecar_subs(movie) == ([] if expected is None else [(tmp_path / sidecar, *expected)])


def test_find_sidecar_hi_cc_and_sdh_tags_are_hearing_impaired(tmp_path, movie):
    names = [f"Movie (2024).{tag}.srt" for tag in ("en.hi", "en.sdh", "en.cc", "en-sdh", "en-cc")]
    for name in names:
        (tmp_path / name).write_text("x")
    found = {p.name: (forced, sdh) for p, forced, sdh in find_sidecar_subs(movie)}
    assert found == {name: (False, True) for name in names}


def test_find_sidecar_generated_alongside_a_real_sidecar_finds_both(tmp_path, movie):
    gen = tmp_path / "Movie (2024).en.generated.srt"
    real = tmp_path / "Movie (2024).en.srt"
    gen.write_text("x")
    real.write_text("y")
    assert sorted(find_sidecar_subs(movie)) == sorted([(gen, False, False), (real, False, False)])


def test_is_generated_sidecar_true_only_for_the_generated_tag(tmp_path):
    movie = tmp_path / "Movie (2024).mkv"
    assert is_generated_sidecar(movie, tmp_path / "Movie (2024).en.generated.srt")
    assert is_generated_sidecar(movie, tmp_path / "Movie (2024).en.sdh.generated.srt")
    assert is_generated_sidecar(movie, tmp_path / "Movie (2024).EN.Generated.SRT")
    assert not is_generated_sidecar(movie, tmp_path / "Movie (2024).en.srt")
    assert not is_generated_sidecar(movie, tmp_path / "Movie (2024).en.sdh.srt")
    assert not is_generated_sidecar(movie, tmp_path / "Movie (2024).srt")


def test_is_generated_track_matches_the_library_title_convention():
    assert is_generated_track({"tags": {"title": "English (generated)"}})
    assert is_generated_track({"tags": {"title": "ENGLISH (Generated)"}})
    assert not is_generated_track({"tags": {"title": "English"}})
    assert not is_generated_track({"tags": {"title": "Auto-generated notes"}})  # no "(generated)"
    assert not is_generated_track({"tags": {}})
    assert not is_generated_track({})


def test_find_sidecar_belongs_to_the_longer_dotted_stem_video(tmp_path):
    """`Movie.mkv` beside `Movie.2020.mkv`: `Movie.2020.en.srt` also starts
    with `Movie.` but belongs to the longer-stemmed video."""
    short, long_ = tmp_path / "Movie.mkv", tmp_path / "Movie.2020.mkv"
    short.touch()
    long_.touch()
    (tmp_path / "Movie.en.srt").write_text("x")
    (tmp_path / "Movie.2020.en.srt").write_text("x")
    assert [p.name for p, _f, _s in find_sidecar_subs(short)] == ["Movie.en.srt"]
    assert [p.name for p, _f, _s in find_sidecar_subs(long_)] == ["Movie.2020.en.srt"]


def test_find_sidecar_same_stem_sibling_video_is_not_a_longer_stem(tmp_path):
    mkv, mp4 = tmp_path / "Movie.mkv", tmp_path / "Movie.mp4"
    mkv.touch()
    mp4.touch()
    (tmp_path / "Movie.en.srt").write_text("x")
    assert len(find_sidecar_subs(mkv)) == 1


def test_find_sidecar_subs_stem_ends_at_a_dot_boundary(tmp_path):
    """`Episode 1.mkv` is never handed `Episode 10.en.srt`, `Episode 100.srt` or `Episode 1 (alt).en.srt`."""
    video = tmp_path / "Episode 1.mkv"
    video.write_bytes(b"x")
    for name in ("Episode 1.srt", "Episode 1.en.srt", "Episode 1.en.sdh.srt",
                 "Episode 10.en.srt", "Episode 10.fr.srt", "Episode 100.srt",
                 "Episode 1 (alt).en.srt", "Episode 1-extra.en.srt"):
        (tmp_path / name).write_text("x")
    found = sorted(p.name for p, _forced, _sdh in find_sidecar_subs(video))
    assert found == ["Episode 1.en.sdh.srt", "Episode 1.en.srt", "Episode 1.srt"]
    other = tmp_path / "Episode 10.mkv"
    other.write_bytes(b"x")
    assert sorted(p.name for p, _f, _s in find_sidecar_subs(other)) == ["Episode 10.en.srt"]


# --- decode_subtitle_bytes (language-classification decoding) ---

_SAMPLE = "1\n00:00:01,000 --> 00:00:02,000\nHello there, café.\n"


@pytest.mark.parametrize("data", [
    _SAMPLE.encode("utf-8-sig"),  # BOM stripped
    _SAMPLE.encode("utf-16"),
    _SAMPLE.encode("cp1252"),  # not replacement garbage
], ids=["utf8-sig", "utf16-bom-le", "cp1252"])
def test_decode_reads_every_supported_encoding(data):
    assert decode_subtitle_bytes(data) == _SAMPLE
    assert decode_subtitle_text(data) == _SAMPLE


def test_decode_never_raises_on_garbage_or_empty():
    assert decode_subtitle_bytes(b"") == ""
    assert decode_subtitle_text(b"") == ""
    assert isinstance(decode_subtitle_bytes(bytes(range(256)) * 4), str)


# --- decode_subtitle_text: text, or None ---

def test_decode_subtitle_text_is_none_for_bytes_that_are_not_text():
    assert decode_subtitle_text(bytes(range(256)) * 20) is None           # NULs and invalid UTF-8
    assert decode_subtitle_text(b"1\n00:00:01,000 --> 00:00:02,000\nHi\x00there\n") is None


# --- extract_track: ASS and SSA come out in their native form ---

def test_extract_track_writes_ass_and_ssa_natively_and_everything_else_as_srt(tmp_path):
    """ASS/SSA is extracted natively so the content gate sees its raw bytes; conversion comes later."""
    src = tmp_path / "Movie.mkv"
    src.write_bytes(b"x")

    def _argv(codec):
        dst = tmp_path / f"out-{codec}"
        dst.write_text("x")                      # "ffmpeg wrote it"
        with patch.object(st.subprocess, "run", return_value=MagicMock(returncode=0)) as run:
            assert st.extract_track(src, 3, codec, dst) is True
        cmd = run.call_args.args[0]
        return cmd[cmd.index("-c:s") + 1], cmd[cmd.index("-map") + 1]

    assert _argv("ass") == ("ass", "0:3")
    assert _argv("ssa") == ("ass", "0:3")
    for codec in ("subrip", "webvtt", "mov_text"):
        assert _argv(codec)[0] == "srt"
    assert st.extract_track(src, 3, "hdmv_pgs_subtitle", tmp_path / "img") is False   # image: not here
