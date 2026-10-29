"""consolidate-subs.py ranking and naming of GENERATED (Whisper) subtitles, plus workdir name collisions.

Through the real `_process_locked`; only external tools are faked, sidecar discovery is real. The muxed track
is "English (generated)" for a `.generated` sidecar or a "(generated)"-titled embedded track, else "English".
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "consolidate_subs_generated", str(PROJECT_ROOT / "consolidate-subs.py")
)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)

GENERATED = "English (generated)"


def _run_gate(tmp_path, monkeypatch, *, embedded=(), sidecars=()):
    """Run `_process_locked` on `Movie (2024).mkv`; return (result, one {eng_track_name, content} per remux).

    embedded: (index, title_or_None, content, line_count) tracks; sidecars: (suffix, content, line_count).
    """
    monkeypatch.setattr(cs, "NEEDS_ENGLISH_STATE_FILE", tmp_path / "needs-english.json")
    media = tmp_path / "Movie (2024).mkv"
    media.write_bytes(b"\x1a\x45\xdf\xa3")
    lines_by_content = {}
    content_by_index = {}
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "h264"},
        {"index": 1, "codec_type": "audio", "codec_name": "ac3", "tags": {"language": "eng"}},
    ]
    for idx, title, content, lines in embedded:
        tags = {"language": "eng"}
        if title:
            tags["title"] = title
        streams.append({"index": idx, "codec_type": "subtitle", "codec_name": "subrip",
                        "tags": tags})
        content_by_index[idx] = content
        lines_by_content[content] = lines
    for suffix, content, lines in sidecars:
        (tmp_path / f"Movie (2024){suffix}").write_text(content)
        lines_by_content[content] = lines

    def _extract(src, idx, codec, dst):
        Path(dst).write_text(content_by_index[idx])
        return True

    calls = []

    def _fake_remux(src, dst, video_idx, audio_idx, eng_srt, forced_srt,
                    keep_subtitle_indices=None, cleanup_flags=None,
                    eng_track_name="English"):
        calls.append({"eng_track_name": eng_track_name,
                      "content": Path(eng_srt).read_text()})
        Path(dst).write_bytes(b"MUXED")
        return True, "ok"

    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", return_value={"streams": streams}), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "extract_track", side_effect=_extract), \
         patch.object(cs, "count_subtitle_lines",
                      side_effect=lambda p: lines_by_content[Path(p).read_text()]), \
         patch.object(cs, "clean_srt_text", side_effect=lambda t: t), \
         patch.object(cs, "run_ffsubsync", return_value=False), \
         patch.object(cs, "original_language_for", return_value=None), \
         patch.object(cs, "get_audio_lang_pref", return_value=({"eng"}, "eng", False)), \
         patch.object(cs, "select_keep_audio", return_value=[1]), \
         patch.object(cs, "remux", side_effect=_fake_remux), \
         patch.object(cs, "set_consolidated_tag", return_value=True), \
         patch.object(cs, "_normalize_audio_inline"), \
         patch.object(cs, "update_state_entry"):
        result = cs._process_locked(media, str(media), dry_run=False)
    return result, calls


# candidates -> the one track muxed and its name. Real beats generated whatever the line counts or base
# score; within a class non-HI beats HI; an HI candidate is still used when alone.
_P = pytest.param
RANKING_CASES = [
    _P(dict(sidecars=[(".en.generated.srt", "GEN", 5)]), GENERATED, "GEN", id="generated-sidecar"),
    _P(dict(sidecars=[(".en.srt", "REAL", 5)]), "English", "REAL", id="plain-sidecar"),
    _P(dict(sidecars=[(".en.sdh.generated.srt", "GEN-SDH", 2000), (".en.srt", "REAL", 5)]),
       "English", "REAL", id="real-sidecar-beats-400x-longer-generated-sidecar"),
    _P(dict(embedded=[(2, "English (generated)", "GEN-EMBEDDED", 2000)],
            sidecars=[(".en.srt", "REAL-SIDECAR", 5)]),
       "English", "REAL-SIDECAR", id="real-sidecar-beats-higher-scored-generated-embedded"),
    _P(dict(embedded=[(2, "English (generated)", "GEN", 2000), (3, "English", "REAL", 5)]),
       "English", "REAL", id="real-embedded-beats-generated-embedded"),
    _P(dict(sidecars=[(".en.sdh.srt", "REAL-SDH", 2000), (".en.srt", "REAL-PLAIN", 5)]),
       "English", "REAL-PLAIN", id="non-hi-sidecar-beats-longer-sdh-sidecar"),
    _P(dict(sidecars=[(".en.sdh.srt", "REAL-SDH", 5)]), "English", "REAL-SDH", id="hi-sidecar-alone"),
    _P(dict(sidecars=[(".en.sdh.srt", "REAL-SDH", 5), (".en.generated.srt", "GEN-PLAIN", 2000)]),
       "English", "REAL-SDH", id="real-hi-sidecar-beats-generated-non-hi"),
]


@pytest.mark.parametrize("candidates, track_name, content", RANKING_CASES)
def test_the_best_candidate_is_muxed_under_the_right_track_name(
        tmp_path, monkeypatch, candidates, track_name, content):
    result, calls = _run_gate(tmp_path, monkeypatch, **candidates)
    assert result["status"] == "FIXED"
    assert calls == [{"eng_track_name": track_name, "content": content}]


# --- workdir names: working files are unique and role-prefixed, never named after the video or a sidecar ---

class _Collision:
    def __init__(self):
        self.sync_calls = []      # (input path, output path, input text when called)
        self.remuxes = []         # {"main": text, "forced": text | None}
        self.created = {}         # path -> text it was created with
        self.clobbered = []       # created files whose text changed before the sync


def _run_collision(tmp_path, monkeypatch, *, stem, embedded=(), sidecars=(), forced=None,
                   converted_ass=None):
    """Run the regular full pass on `<stem>.mkv`.

    embedded: (index, text, line_count); forced: (index, text); sidecars: (suffix, text, line_count),
    `.ass` ones being "converted" to `converted_ass`.
    """
    monkeypatch.setattr(cs, "NEEDS_ENGLISH_STATE_FILE", tmp_path / "needs-english.json")
    media = tmp_path / f"{stem}.mkv"
    media.write_bytes(b"\x1a\x45\xdf\xa3")
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"},
               {"index": 1, "codec_type": "audio", "codec_name": "ac3", "tags": {"language": "eng"}}]
    text_by_index, lines_by_text = {}, {}
    for idx, text, lines in embedded:
        streams.append({"index": idx, "codec_type": "subtitle", "codec_name": "subrip",
                        "tags": {"language": "eng"}})
        text_by_index[idx] = text
        lines_by_text[text] = lines
    if forced:
        streams.append({"index": forced[0], "codec_type": "subtitle", "codec_name": "subrip",
                        "tags": {"language": "eng", "title": "Forced"}, "disposition": {"forced": 1}})
        text_by_index[forced[0]] = forced[1]
    for suffix, text, lines in sidecars:
        (tmp_path / f"{stem}{suffix}").write_text(text)
        lines_by_text[text] = lines
    if converted_ass:
        lines_by_text[converted_ass[0]] = converted_ass[1]

    run = _Collision()

    def _remember(path):
        run.created[Path(path)] = Path(path).read_text()

    def _extract(src, idx, codec, dst):
        Path(dst).write_text(text_by_index[idx])
        _remember(dst)
        return True

    def _convert(src, dst):
        Path(dst).write_text(converted_ass[0])
        _remember(dst)
        return True

    def _count(p):
        text = Path(p).read_text()
        _remember(p)  # a candidate, as first created
        return lines_by_text[text]

    def _sync(video, srt_in, srt_out):
        run.sync_calls.append((Path(srt_in), Path(srt_out), Path(srt_in).read_text()))
        for p, text in run.created.items():
            if p != Path(srt_in) and p.exists() and p.read_text() != text:
                run.clobbered.append(p)
        Path(srt_out).write_text("SYNCED:" + Path(srt_in).read_text())
        return True

    def _remux(src, dst, video_idx, audio_idx, eng_srt, forced_srt, **kw):
        run.remuxes.append({"main": Path(eng_srt).read_text(),
                            "forced": Path(forced_srt).read_text() if forced_srt else None})
        Path(dst).write_bytes(b"MUXED")
        return True, "ok"

    with patch.object(cs, "load_state", return_value={}), \
         patch.object(cs, "probe", return_value={"streams": streams}), \
         patch.object(cs, "already_processed", return_value=False), \
         patch.object(cs, "extract_track", side_effect=_extract), \
         patch.object(cs, "ass_to_srt", side_effect=_convert), \
         patch.object(cs, "count_subtitle_lines", side_effect=_count), \
         patch.object(cs, "clean_srt_text", side_effect=lambda t: t), \
         patch.object(cs, "run_ffsubsync", side_effect=_sync), \
         patch.object(cs, "original_language_for", return_value=None), \
         patch.object(cs, "get_audio_lang_pref", return_value=({"eng"}, "eng", False)), \
         patch.object(cs, "select_keep_audio", return_value=[1]), \
         patch.object(cs, "remux", side_effect=_remux), \
         patch.object(cs, "set_consolidated_tag", return_value=True), \
         patch.object(cs, "_normalize_audio_inline"), \
         patch.object(cs, "update_state_entry"):
        run.result = cs._process_locked(media, str(media), dry_run=False)
    return run


def test_two_sidecars_that_convert_to_the_same_name_stay_separate(tmp_path, monkeypatch):
    """`X.en.srt` and `X.en.ass` must not convert onto the same name."""
    run = _run_collision(tmp_path, monkeypatch, stem="Movie",
                         sidecars=[(".en.srt", "SRT-ONE", 5), (".en.ass", "ASS-RAW", 1)],
                         converted_ass=("CONVERTED-ASS", 3000))
    assert run.remuxes == [{"main": "SYNCED:CONVERTED-ASS", "forced": None}]  # more lines: wins
    assert run.clobbered == []
    assert "SRT-ONE" in run.created.values() and "CONVERTED-ASS" in run.created.values()
    assert len({p for p in run.created}) == len(run.created) >= 2


@pytest.mark.parametrize("stem", ["forced", "track2"])
def test_no_working_file_is_ever_overwritten_whatever_the_video_is_called(tmp_path, monkeypatch, stem):
    run = _run_collision(
        tmp_path, monkeypatch, stem=stem, embedded=[(2, "EMBEDDED-MAIN", 10)],
        sidecars=[(".srt", "SIDECAR-MAIN", 2000), (".en.srt", "SIDECAR-EN", 20)],
        forced=(3, "FORCED-TEXT"))
    assert run.result["status"] == "FIXED"
    assert run.clobbered == []
    assert run.remuxes[0]["forced"] == "FORCED-TEXT"
    assert run.remuxes[0]["main"] == "SYNCED:SIDECAR-MAIN"   # 2000 lines beats the rest
    (srt_in, srt_out, _text), = run.sync_calls
    assert srt_in != srt_out


def test_workfile_names_are_unique_role_prefixed_and_inside_the_workdir(tmp_path):
    made = [cs._workfile(tmp_path, role, ".srt") for role in
            ("forced", "forced", "synced", "main", "embedded2", "embedded2-srt")]
    assert len(set(made)) == len(made)
    for p, role in zip(made, ("forced", "forced", "synced", "main", "embedded2", "embedded2-srt")):
        assert p.parent == tmp_path and p.name.startswith(f"{role}-") and p.suffix == ".srt"
        assert p.is_file() and p.stat().st_size == 0

