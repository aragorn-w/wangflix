"""consolidate-subs.py and the "English (dub)" subtitle of a dual-audio (Japanese + English) file.

Through the real `_process_locked` (and `_add_late_dub` via its skip paths). Real: audio policy and `is_anime`
(the media sits under an `/anime/` folder, which outside `tv/` is the keyword rule), sidecar discovery, content gate, cleaning, hearing-impaired stripping,
ranking, the state file (in `tmp_path`). Faked: ffprobe, track extraction, ffsubsync, mkvmerge, tag writers.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "consolidate_subs_dub", str(PROJECT_ROOT / "consolidate-subs.py")
)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)

STEM = "Show S01E01"
ORIGINAL = b"\x1a\x45\xdf\xa3 original"
MAIN = "You know what we have to do."
DUB = "We have to go now, you know."
FORCED = "Signs"
HI = "[door slams]\n(sighs) " + DUB
FRENCH = " ".join(["je suis tout a fait sur que ce nous le la les et est un une vous de des qui pas ne en du il"] * 4)


def _srt(*texts):
    return "\n".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},500\n{t}\n" for i, t in enumerate(texts, 1))


def _audio(lang, index):
    return {"index": index, "codec_type": "audio", "codec_name": "ac3",
            "tags": {"language": lang}, "disposition": {}}


def _sub(index, title, lang="eng", **disp):
    return {"index": index, "codec_type": "subtitle", "codec_name": "subrip",
            "tags": {"language": lang, "title": title}, "disposition": disp}


def _tagged(streams):
    return {"streams": streams, "format": {"tags": {"CONSOLIDATED_SUBS": f"v{cs.PIPELINE_VERSION}"}}}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(cs, "NEEDS_ENGLISH_STATE_FILE", tmp_path / "needs-english.json")
    monkeypatch.setattr(cs, "LOG_FILE", tmp_path / "consolidate.log")
    monkeypatch.setattr(cs, "VAR_QUARANTINE_SUBTITLES", tmp_path / "quarantine")
    monkeypatch.setattr(cs, "QUARANTINE_LOG", tmp_path / "sub-quarantine.jsonl")


def _media(tmp_path, sidecars=(), folder="anime", stem=STEM):
    directory = tmp_path / folder
    directory.mkdir(parents=True, exist_ok=True)
    media = directory / f"{stem}.mkv"
    media.write_bytes(ORIGINAL)
    for suffix, text in sidecars:
        (directory / f"{stem}{suffix}").write_text(text)
    return media


def _left(media):
    return sorted(p.name.removeprefix(media.stem) for p in media.parent.iterdir() if p.suffix == ".srt")


def _full_pass(tmp_path, *, audio=("jpn", "eng"), embedded=(), sidecars=(), folder="anime", stem=STEM,
               anime=None, original=None):
    """Run the full pass on an unprocessed file. `embedded` is `(title, text)` per English text track
    (stream indexes follow the audio), `sidecars` `(suffix, text)`. `anime` overrides `is_anime`,
    `original` is the Arr original language. Returns `(result, run, media)`."""
    media = _media(tmp_path, sidecars, folder, stem)
    streams = [{"index": 0, "codec_type": "video", "codec_name": "h264"},
               *[_audio(lang, i) for i, lang in enumerate(audio, 1)]]
    texts = {}
    for idx, (title, text) in enumerate(embedded, len(audio) + 1):
        streams.append(_sub(idx, title))
        texts[idx] = text
    run = SimpleNamespace(remuxes=[], syncs=[], cleanups=[])

    def _extract(src, idx, codec, dst):
        Path(dst).write_text(texts[idx])
        return True

    def _sync(video, srt_in, srt_out):
        run.syncs.append(Path(srt_in).read_text())
        return False

    def _remux(src, dst, video_idx, audio_idx, eng_srt, forced_srt, keep_subtitle_indices=None,
               cleanup_flags=None, eng_track_name="English", kept_source_subs=None, dub_srt=None):
        run.remuxes.append({"audio": list(audio_idx), "main": Path(eng_srt).read_text(),
                            "dub": Path(dub_srt).read_text() if dub_srt else None,
                            "forced": Path(forced_srt).read_text() if forced_srt else None})
        Path(dst).write_bytes(b"MUXED")
        return True, "ok"

    def _cleanup(path, path_str, status, base_detail, *a, **k):
        run.cleanups.append(base_detail)
        return {"path": path_str, "status": status, "detail": base_detail}

    with patch.object(cs, "probe", return_value={"streams": streams, "format": {}}), \
         patch.object(cs, "extract_track", side_effect=_extract), \
         patch.object(cs, "run_ffsubsync", side_effect=_sync), \
         patch.object(cs, "original_language_for", return_value=original), \
         patch.object(cs, "is_anime", new=cs.is_anime if anime is None else (lambda p: anime)), \
         patch.object(cs, "remux", side_effect=_remux), \
         patch.object(cs, "set_consolidated_tag", return_value=True), \
         patch.object(cs, "_normalize_audio_inline"), \
         patch.object(cs, "_cleanup_no_main_english", side_effect=_cleanup):
        result = cs._process_locked(media, str(media), dry_run=False)
    return result, run, media


def test_dual_file_gets_main_dub_and_forced_tracks_and_only_the_main_is_synced(tmp_path):
    result, run, media = _full_pass(tmp_path, sidecars=[
        (".en.srt", _srt(MAIN)), (".en.dub.srt", _srt(HI)), (".en.forced.srt", _srt(FORCED))])

    assert result["status"] == "FIXED"
    assert "dub_subtitle=1" in result["detail"] and "dropped_sidecars=3" in result["detail"]
    [remux] = run.remuxes
    assert remux["audio"] == [1, 2]                       # the anime rule keeps Japanese AND the English dub
    assert MAIN in remux["main"] and DUB not in remux["main"]
    assert DUB in remux["dub"] and "door slams" not in remux["dub"] and "sighs" not in remux["dub"]
    assert remux["dub"].count("-->") == 1                 # the sound-tag-only cue is gone
    assert FORCED in remux["forced"]
    assert len(run.syncs) == 1 and MAIN in run.syncs[0]   # the dub is never ffsubsync'd
    assert _left(media) == []


def test_embedded_hearing_impaired_track_is_the_dub_not_a_main_candidate(tmp_path):
    result, run, _ = _full_pass(tmp_path, embedded=[
        ("English", _srt(MAIN)), ("English SDH", _srt(HI, "(laughs)", "[music]", "Extra"))])

    assert result["status"] == "FIXED"
    [remux] = run.remuxes
    assert MAIN in remux["main"] and "Extra" not in remux["main"]
    assert DUB in remux["dub"] and "sighs" not in remux["dub"]


def test_a_dub_sidecar_is_preferred_over_an_embedded_dub_track(tmp_path):
    _, run, media = _full_pass(
        tmp_path, embedded=[("English", _srt(MAIN)), ("English (dub)", _srt("From the embedded track."))],
        sidecars=[(".en.dub.srt", _srt(DUB))])

    assert DUB in run.remuxes[0]["dub"] and "embedded track" not in run.remuxes[0]["dub"]
    assert _left(media) == []


def test_a_foreign_dub_sidecar_is_quarantined_and_the_embedded_dub_track_is_used(tmp_path):
    _, run, media = _full_pass(
        tmp_path, embedded=[("English", _srt(MAIN)), ("English CC", _srt(DUB))],
        sidecars=[(".en.dub.srt", _srt(FRENCH))])

    assert DUB in run.remuxes[0]["dub"]
    assert _left(media) == []
    assert [p.name for p in (tmp_path / "quarantine").iterdir()][0].endswith(f"{STEM}.en.dub.srt")


def test_dual_file_without_a_dub_candidate_gets_no_dub_track(tmp_path):
    _, run, _ = _full_pass(tmp_path, sidecars=[(".en.srt", _srt(MAIN))])
    assert run.remuxes[0]["dub"] is None


def test_a_sole_hearing_impaired_track_stays_the_main_with_no_separate_dub_track(tmp_path):
    """Dub-like tracks leave the main ranking only when something else can be the main: with a sole
    CC/SDH track (or one beside a plain track the content gate rejects) the file behaves as before."""
    for embedded in ([("English SDH", _srt(HI))],
                     [("English", _srt(FRENCH)), ("English SDH", _srt(HI))]):
        result, run, _ = _full_pass(tmp_path / str(len(embedded)), embedded=embedded)
        assert result["status"] == "FIXED" and "dub_subtitle" not in result["detail"]
        [remux] = run.remuxes
        assert "sighs" in remux["main"] and remux["dub"] is None   # not stripped: it is the main
        assert run.cleanups == []


def test_a_dub_sidecar_still_becomes_the_dub_track_beside_a_sole_hearing_impaired_main(tmp_path):
    _, run, media = _full_pass(tmp_path, embedded=[("English CC", _srt(HI))], sidecars=[(".en.dub.srt", _srt(DUB))])
    [remux] = run.remuxes
    assert "sighs" in remux["main"] and "sighs" not in remux["dub"] and DUB in remux["dub"]
    assert _left(media) == []


def test_a_dub_sidecar_beside_a_single_audio_file_is_not_a_main_candidate_and_stays(tmp_path, capsys):
    result, run, media = _full_pass(
        tmp_path, audio=("eng",), sidecars=[(".en.srt", _srt(MAIN)), (".en.dub.srt", _srt(DUB))])

    assert result["status"] == "FIXED"
    [remux] = run.remuxes
    assert MAIN in remux["main"] and remux["dub"] is None
    assert _left(media) == [".en.dub.srt"]
    assert "left in place" in capsys.readouterr().out


def test_a_dub_sidecar_alone_beside_a_single_audio_file_does_not_make_a_main(tmp_path):
    result, run, media = _full_pass(tmp_path, audio=("eng",), sidecars=[(".en.dub.srt", _srt(DUB))])
    assert (result["status"], run.cleanups, run.remuxes) == ("NEEDS_BAZARR", ["no English candidates"], [])
    assert _left(media) == [".en.dub.srt"]


GODZILLA = "Godzilla Minus One (2023)"   # kept dual by DUAL_AUDIO_KEYWORDS, but not anime


def test_a_live_action_dual_audio_file_is_unchanged(tmp_path):
    """Live-action Japanese film (the tmp dir name must not hold the keyword `anime`) with Japanese + English audio: a sole CC track is the main, an extra
    SDH track is just a loser in the ranking, and a dub sidecar is no main candidate and stays."""
    result, run, _ = _full_pass(tmp_path / "a", folder="films", stem=GODZILLA, embedded=[("English CC", _srt(HI))])
    assert result["status"] == "FIXED" and run.remuxes[0]["audio"] == [1, 2]
    assert "sighs" in run.remuxes[0]["main"] and run.remuxes[0]["dub"] is None and run.cleanups == []

    _, run, _ = _full_pass(tmp_path / "b", folder="films", stem=GODZILLA,
                           embedded=[("English", _srt(MAIN)), ("English SDH", _srt(HI, "Extra"))])
    assert MAIN in run.remuxes[0]["main"] and run.remuxes[0]["dub"] is None

    result, run, media = _full_pass(tmp_path / "c", folder="films", stem=GODZILLA,
                                    sidecars=[(".en.srt", _srt(MAIN)), (".en.dub.srt", _srt(DUB))])
    assert MAIN in run.remuxes[0]["main"] and run.remuxes[0]["dub"] is None
    assert _left(media) == [".en.dub.srt"]


@pytest.mark.parametrize("anime", [True, False])
def test_the_dub_split_follows_is_anime(tmp_path, anime):
    """A Sonarr `seriesType` (here faked) decides, with no keyword in the path: Japanese original per Arr."""
    result, run, _ = _full_pass(
        tmp_path, folder="tv", stem="Some Show S01E01", anime=anime, original="jpn",
        embedded=[("English", _srt(MAIN)), ("English CC", _srt(HI))])
    assert result["status"] == "FIXED" and run.remuxes[0]["audio"] == [1, 2]
    assert (run.remuxes[0]["dub"] is not None) is anime


# --- an already-processed dual file takes a new .en.dub.srt without being reprocessed ---

MAIN_SUB = _sub(3, "English", default=1)
DUB_SUB = _sub(4, "English (dub)")


def _late_dub(tmp_path, *, cached, audio=("jpn", "eng"), subs=(MAIN_SUB,), after=(MAIN_SUB, DUB_SUB),
              sidecar=_srt(HI), dry_run=False, mux_edit=None, real_sweep=False, anime=True):
    """Run `_process_locked` on a processed file. `after` are the subtitles the faked mux leaves in the output."""
    media = _media(tmp_path, [(".en.dub.srt", sidecar)])
    base = [{"index": 0, "codec_type": "video", "codec_name": "h264", "disposition": {"default": 1}},
            *[{**_audio(lang, i), "disposition": {"default": int(i == 1)}} for i, lang in enumerate(audio, 1)]]
    before_info = _tagged([*base, *subs])
    after_info = _tagged([*base, *after])
    run = SimpleNamespace(added=[], sweeps=[])

    def _probe(p):
        return after_info if Path(p).name.endswith(".tmp.mkv") else before_info

    def _add(src, dst, srt):
        run.added.append(Path(srt).read_text())
        Path(dst).write_bytes(b"MUXED")
        if mux_edit:
            mux_edit(src)
        return True, "ok"

    if cached:
        size, mtime = cs.file_key(media)
        cs.update_state_entry(cs.STATE_FILE, str(media.resolve()), {
            "size": size, "mtime": mtime, "v": cs.PIPELINE_VERSION, "status": "fixed", "synced": True})
    sweep = cs._late_sidecar_sweep if real_sweep else lambda *a, **k: run.sweeps.append(a) or (0, 0)
    with patch.object(cs, "probe", side_effect=_probe), \
         patch.object(cs, "is_anime", return_value=anime), \
         patch.object(cs, "add_dub_subtitle", side_effect=_add), \
         patch.object(cs, "_late_sidecar_sweep", side_effect=sweep):
        result = cs._process_locked(media, str(media), dry_run=dry_run)
    return result, run, media


@pytest.mark.parametrize("cached", [True, False], ids=["state-cache-hit", "tag-present"])
def test_a_new_dub_sidecar_is_added_to_a_processed_dual_file(tmp_path, cached):
    result, run, media = _late_dub(tmp_path, cached=cached)

    assert (result["status"], result["detail"]) == ("FIXED", f"dub subtitle added from {STEM}.en.dub.srt")
    assert media.read_bytes() == b"MUXED"                 # the mkvmerge copy replaced the file
    assert _left(media) == []                             # sidecar consumed, not quarantined
    assert not list(media.parent.glob("*.tmp.mkv")) and not (tmp_path / "quarantine").exists()
    assert len(run.added) == 1 and "sighs" not in run.added[0] and DUB in run.added[0]
    assert run.sweeps == []
    entry = cs.load_state(cs.STATE_FILE)[str(media.resolve())]
    assert (entry["size"], entry["mtime"]) == cs.file_key(media) and entry["v"] == cs.PIPELINE_VERSION


@pytest.mark.parametrize("kwargs, why", [
    ({"audio": ("eng",), "after": ()}, "not dual"),
    ({"anime": False}, "dual but not anime"),
    ({"subs": (MAIN_SUB, DUB_SUB)}, "already has a dub track"),
    ({"subs": (MAIN_SUB, _sub(4, "English CC"))}, "already has a CC track, which follows the dub"),
])
def test_the_dub_sidecar_is_left_alone_when_it_has_no_job(tmp_path, kwargs, why):
    result, run, media = _late_dub(tmp_path, cached=True, **kwargs)

    assert result["status"] == "SKIP", why
    assert run.added == [] and media.read_bytes() == ORIGINAL
    assert _left(media) == [".en.dub.srt"] and not (tmp_path / "quarantine").exists()


def test_the_late_sidecar_sweep_never_quarantines_a_dub_sidecar_it_has_no_job_for(tmp_path):
    result, _, media = _late_dub(tmp_path, cached=True, audio=("eng",), after=(), real_sweep=True)
    assert result["status"] == "SKIP" and _left(media) == [".en.dub.srt"]
    assert not (tmp_path / "quarantine").exists()


def test_a_dub_track_does_not_count_as_the_embedded_english_that_retires_a_main_sidecar(tmp_path):
    """Beside a generated main + a dub track, a real main sidecar is still the generated -> real upgrade."""
    info = _tagged([_sub(3, "English (generated)"), DUB_SUB])
    tracks = cs._embedded_english_text_tracks(info)
    assert [t["index"] for t in tracks] == [3]
    media = tmp_path / f"{STEM}.mkv"
    assert cs._late_english_sidecar_verdict(media, media.with_suffix(".en.srt"), False, tracks) == "upgrade"


def test_a_dub_add_whose_output_lacks_the_new_track_fails_and_changes_nothing(tmp_path):
    result, _, media = _late_dub(tmp_path, cached=True, after=(MAIN_SUB,))

    assert result["status"] == "FAIL" and result["detail"].startswith("dub verify: ")
    assert media.read_bytes() == ORIGINAL and _left(media) == [".en.dub.srt"]
    assert not list(media.parent.glob("*.tmp.mkv"))


def test_a_dub_add_that_changes_an_existing_flag_fails(tmp_path):
    moved = _sub(3, "English")                            # the main lost its default flag
    result, _, media = _late_dub(tmp_path, cached=True, after=(moved, DUB_SUB))
    assert result["status"] == "FAIL" and "existing subtitles changed" in result["detail"]
    assert media.read_bytes() == ORIGINAL


def test_a_video_replaced_during_the_dub_add_is_not_overwritten(tmp_path):
    result, _, media = _late_dub(
        tmp_path, cached=True, mux_edit=lambda src: Path(src).write_bytes(b"A NEW RELEASE"))

    assert result["status"] == "SKIP" and "changed" in result["detail"]
    assert media.read_bytes() == b"A NEW RELEASE" and _left(media) == [".en.dub.srt"]
    assert not list(media.parent.glob("*.tmp.mkv"))


def test_dry_run_reports_the_dub_add_and_touches_nothing(tmp_path):
    result, run, media = _late_dub(tmp_path, cached=True, dry_run=True)
    assert result["status"] == "SKIP" and result["detail"].startswith("dry-run: would add dub subtitle")
    assert run.added == [] and media.read_bytes() == ORIGINAL and _left(media) == [".en.dub.srt"]


# --- verification of a full pass that stream-copied an unreadable forced track ---

def _verify_kept(tmp_path, out_subs, dub):
    kept = {5: {"default": False, "forced": True}}
    src = {"index": 5, "codec_name": "subrip"}
    info = {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}, *out_subs]}
    with patch.object(cs, "probe", return_value=info):
        return cs._verify_kept_source_subs(tmp_path / "out.mkv", 1, 1, src, kept, dub=dub)


def _out_sub(title, **disp):
    return {"codec_type": "subtitle", "codec_name": "subrip",
            "tags": {"language": "eng", "title": title}, "disposition": disp}


def test_kept_source_subs_verification_expects_the_dub_after_the_main(tmp_path):
    forced, main = _out_sub("Forced", forced=1), _out_sub("English", default=1)
    assert _verify_kept(tmp_path, [forced, main, _out_sub("English (dub)")], dub=True) is None
    assert _verify_kept(tmp_path, [forced, main], dub=False) is None
    assert "subs=3" in _verify_kept(tmp_path, [forced, main], dub=True)
    assert "dub subtitle" in _verify_kept(tmp_path, [forced, main, _out_sub("English (dub)", default=1)], dub=True)
