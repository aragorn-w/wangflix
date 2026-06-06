"""Tests for media_stack.subtitles — scoring, cleaning, classification."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.subtitles import (
    clean_srt_text, is_forced_track, is_sdh_track, normalize_subtitle_caps,
    score_image_track, score_text_track,
)


# --- classification ---

def test_forced_via_disposition():
    assert is_forced_track({"disposition": {"forced": 1}})


def test_forced_via_title():
    assert is_forced_track({"tags": {"title": "Forced Signs"}})


def test_not_forced():
    assert not is_forced_track({"disposition": {}, "tags": {"title": "English"}})


def test_sdh_via_title():
    assert is_sdh_track({"tags": {"title": "English SDH"}})
    assert is_sdh_track({"tags": {"title": "English (CC)"}})
    assert is_sdh_track({"tags": {"title": "English HI"}})


def test_not_sdh():
    assert not is_sdh_track({"tags": {"title": "English"}})


# --- scoring ---

def test_text_score_prefers_subrip():
    s_srt = {"codec_name": "subrip", "disposition": {}}
    s_ass = {"codec_name": "ass", "disposition": {}}
    assert score_text_track(s_srt, 1000) > score_text_track(s_ass, 1000)


def test_text_score_prefers_sdh():
    base = {"codec_name": "subrip", "disposition": {}, "tags": {}}
    sdh = {"codec_name": "subrip", "disposition": {}, "tags": {"title": "SDH"}}
    assert score_text_track(sdh, 1000) > score_text_track(base, 1000)


def test_text_score_penalizes_forced():
    base = {"codec_name": "subrip", "disposition": {}, "tags": {}}
    forced = {"codec_name": "subrip", "disposition": {"forced": 1}, "tags": {}}
    assert score_text_track(base, 1000) > score_text_track(forced, 1000)


def test_text_beats_image():
    text = {"codec_name": "subrip", "disposition": {}, "tags": {}}
    image = {"codec_name": "hdmv_pgs_subtitle", "disposition": {}, "tags": {}}
    assert score_text_track(text, 100) > score_image_track(image)


def test_text_score_increases_with_line_count():
    s = {"codec_name": "subrip", "disposition": {}, "tags": {}}
    assert score_text_track(s, 100) < score_text_track(s, 1000)


# --- cleaning ---

def test_clean_strips_ass_overrides():
    text = "1\n00:00:01,000 --> 00:00:02,000\n{\\b1}Hello{\\b0}\n"
    out = clean_srt_text(text)
    assert "{\\b1}" not in out
    assert "{\\b0}" not in out
    assert "Hello" in out


def test_clean_strips_html_font_tags():
    text = "1\n00:00:01,000 --> 00:00:02,000\n<font color=red>Hi</font>\n"
    out = clean_srt_text(text)
    assert "<font" not in out
    assert "</font>" not in out
    assert "Hi" in out


def test_clean_replaces_literal_nh():
    text = "1\n00:00:01,000 --> 00:00:02,000\nHi\\hthere\n"
    out = clean_srt_text(text)
    assert "\\h" not in out
    assert "Hi there" in out


def test_clean_strips_bom():
    text = "﻿1\n00:00:01,000 --> 00:00:02,000\nHi\n"
    out = clean_srt_text(text)
    assert not out.startswith("﻿")


def test_clean_ensures_trailing_newline():
    text = "1\n00:00:01,000 --> 00:00:02,000\nHi"
    out = clean_srt_text(text)
    assert out.endswith("\n")


# --- ALL-CAPS recasing ---

def test_recase_skips_already_cased_lines():
    text = "Hello World"
    assert normalize_subtitle_caps(text) == "Hello World"


def test_recase_lowers_all_caps():
    text = "THIS IS AN ALL-CAPS LINE."
    out = normalize_subtitle_caps(text)
    assert "this" in out.lower()
    assert out[0].isupper()  # first letter capitalized


def test_recase_preserves_short_lines():
    text = "OK"
    assert normalize_subtitle_caps(text) == "OK"


def test_recase_preserves_timing_lines():
    text = "00:00:01,000 --> 00:00:02,000"
    assert normalize_subtitle_caps(text) == text


def test_recase_preserves_index_lines():
    text = "42"
    assert normalize_subtitle_caps(text) == "42"


def test_recase_handles_dialogue_dashes():
    text = "- HELLO THERE FRIEND."
    out = normalize_subtitle_caps(text)
    # First alphabetic char (the H) should be capitalized, rest lower
    assert "Hello there friend." in out


def test_recase_caps_lone_i():
    # Input must be ALL-CAPS to trigger the recase path; the function
    # then lowercases, then the lone-i regex bumps standalone `i` back
    # to `I`.
    text = "WHEN I SAW IT EARLIER TODAY."
    out = normalize_subtitle_caps(text)
    # Lone i → I after lowercase+sentence-recase pass
    assert " I " in out
    # Other words ended up lowercase (proves the recase ran)
    assert "saw" in out


def test_find_sidecar_bare_srt(tmp_path):
    """Codex round-module-split-2 #4: `Movie.srt` (no lang code) must
    be accepted as an English sidecar."""
    from media_stack.subtitles import find_sidecar_subs
    movie = tmp_path / "Movie (2024).mkv"
    movie.touch()
    bare = tmp_path / "Movie (2024).srt"
    bare.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    found = find_sidecar_subs(movie)
    assert len(found) == 1
    assert found[0] == (bare, False, False)


def test_find_sidecar_en_srt(tmp_path):
    from media_stack.subtitles import find_sidecar_subs
    movie = tmp_path / "Movie (2024).mkv"
    movie.touch()
    en = tmp_path / "Movie (2024).en.srt"
    en.write_text("x")
    found = find_sidecar_subs(movie)
    assert len(found) == 1
    assert found[0][0] == en
    assert found[0][1] is False  # not forced
    assert found[0][2] is False  # not SDH


def test_find_sidecar_en_sdh_srt(tmp_path):
    from media_stack.subtitles import find_sidecar_subs
    movie = tmp_path / "Movie (2024).mkv"
    movie.touch()
    sdh = tmp_path / "Movie (2024).en.sdh.srt"
    sdh.write_text("x")
    found = find_sidecar_subs(movie)
    assert len(found) == 1
    assert found[0][2] is True  # SDH


def test_find_sidecar_en_forced_srt(tmp_path):
    from media_stack.subtitles import find_sidecar_subs
    movie = tmp_path / "Movie (2024).mkv"
    movie.touch()
    forced = tmp_path / "Movie (2024).en.forced.srt"
    forced.write_text("x")
    found = find_sidecar_subs(movie)
    assert len(found) == 1
    assert found[0][1] is True  # forced


def test_find_sidecar_french_excluded(tmp_path):
    from media_stack.subtitles import find_sidecar_subs
    movie = tmp_path / "Movie (2024).mkv"
    movie.touch()
    fre = tmp_path / "Movie (2024).fr.srt"
    fre.write_text("x")
    found = find_sidecar_subs(movie)
    assert len(found) == 0


def test_recase_caps_i_contractions():
    text = "BUT I'M SURE I'LL FIND IT SOMEWHERE."
    out = normalize_subtitle_caps(text)
    assert "I'm" in out
    assert "I'll" in out
