"""Content-gate quarantine mechanics in consolidate-subs.py, through the real `_process_locked` with ffmpeg/mkvmerge mocked."""

import errno
import importlib.util
import json
import os
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import media_stack.paths as _paths  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "consolidate_subs_quarantine", str(PROJECT_ROOT / "consolidate-subs.py")
)
cs = importlib.util.module_from_spec(_spec)
# consolidate-subs.py calls `ensure_var_dirs()` at import; stub it so loading the module cannot
# touch this checkout's real var/ tree.
with patch.object(_paths, "ensure_var_dirs"):
    _spec.loader.exec_module(cs)


@pytest.fixture(autouse=True)
def _isolate_var_paths(tmp_path, monkeypatch):
    """Point every var/-backed path at `tmp_path`; the quarantine dir is not pre-created (`_quarantine_sidecar` must create it lazily)."""
    monkeypatch.setattr(cs, "VAR_QUARANTINE_SUBTITLES", tmp_path / "quarantine" / "subtitles")
    monkeypatch.setattr(cs, "QUARANTINE_LOG", tmp_path / "sub-quarantine.jsonl")
    monkeypatch.setattr(cs, "NEEDS_ENGLISH_STATE_FILE", tmp_path / "needs-english.json")
    monkeypatch.setattr(cs, "LOG_FILE", tmp_path / "consolidate-subs.log")
    monkeypatch.setattr(cs, "STATE_FILE", tmp_path / "consolidate-subs.state.json")
    # the late sweep probes the video when an English sidecar exists; never spawn a real ffprobe
    monkeypatch.setattr(cs, "probe", lambda p: None)
    # judging a late English sidecar redundant samples the embedded text; default to English (tests override)
    monkeypatch.setattr(cs.subverify, "embedded_text_window", lambda *a, **k: ENGLISH_TEXT)


def _media(tmp_path: Path, name: str = "Movie (2024).mkv") -> Path:
    # under tmp_path, outside MEDIA_ROOT, so `original_language_for` returns None without a network call
    p = tmp_path / name
    p.write_bytes(b"\x1a\x45\xdf\xa3")
    return p


def _quarantined() -> list[Path]:
    """Files in the (possibly not yet created) quarantine dir."""
    d = cs.VAR_QUARANTINE_SUBTITLES
    return sorted(d.iterdir()) if d.is_dir() else []


# Real dialogue samples (hand-verified against sublang's calibration): enough tokens to clear TOO-LITTLE-TEXT, >= 8 distinct stopwords.
FRENCH_TEXT = (
    "1\n00:00:01,000 --> 00:00:03,000\n"
    "Je ne sais pas ce que tu attends de moi ce soir.\n\n"
    "2\n00:00:03,500 --> 00:00:06,000\n"
    "Dis-moi la verite inspecteur. Je ne cache rien du tout.\n\n"
    "3\n00:00:06,500 --> 00:00:09,000\n"
    "Elle avait promis d'arriver tot mais personne ne l'a vue partir.\n\n"
    "4\n00:00:09,500 --> 00:00:12,000\n"
    "Ecoute cette situation semble impossible on doit partir maintenant.\n"
)
DANISH_TEXT = (
    "1\n00:00:01,000 --> 00:00:03,000\n"
    "Jeg ved ikke hvad du vil have fra mig i aften.\n\n"
    "2\n00:00:03,500 --> 00:00:06,000\n"
    "Sig mig sandheden, detektiv. Jeg skjuler ikke noget.\n\n"
    "3\n00:00:06,500 --> 00:00:09,000\n"
    "Hun sagde hun ville komme tidligt, men ingen saa hende gaa.\n\n"
    "4\n00:00:09,500 --> 00:00:12,000\n"
    "Hoer her, det her er svaert for dig, men vi skal gaa nu.\n"
)
ENGLISH_TEXT = (
    "1\n00:00:01,000 --> 00:00:03,000\n"
    "I don't know what you want from me tonight.\n\n"
    "2\n00:00:03,500 --> 00:00:06,000\n"
    "Just tell the truth, detective. I'm not hiding anything, okay.\n\n"
    "3\n00:00:06,500 --> 00:00:09,000\n"
    "She promised she'd arrive early, but nobody noticed her leaving.\n\n"
    "4\n00:00:09,500 --> 00:00:12,000\n"
    "Look, this situation feels impossible, but we have to move now.\n"
)


def _classify(text: str) -> dict:
    is_eng, info = cs.classify_candidate(text)
    assert is_eng is False
    return info


def _snap(path: Path):
    snap = cs._read_snapshot(path)
    assert snap is not None
    return snap


def _swap_in(sidecar: Path, new_text: str) -> None:
    """Replace `sidecar` with a new file (new inode) holding `new_text`, as Bazarr dropping a fresh sidecar would."""
    incoming = sidecar.parent / "incoming.tmp"
    incoming.write_text(new_text)
    os.replace(incoming, sidecar)


def _swap_after_classify(sidecar: Path, new_text: str = ENGLISH_TEXT):
    """A `classify_candidate` stand-in that swaps `sidecar` for `new_text` right after classifying it."""
    real_classify = cs.classify_candidate

    def _classify_then_swap(text):
        result = real_classify(text)
        _swap_in(sidecar, new_text)
        return result
    return _classify_then_swap


def _write_content(path: Path, content) -> None:
    """Write `content` (text, bytes, or a callable given the path) to `path`."""
    if callable(content):
        content(path)
    elif isinstance(content, str):
        path.write_text(content)
    else:
        path.write_bytes(content)


def _extract_writing(content):
    """`extract_track` fake that writes `content` and succeeds."""
    def _extract(src, idx, codec, dst):
        _write_content(Path(dst), content)
        return True
    return _extract


def _fetch_writing(content):
    """`fetch_via_subliminal` fake that drops `content` into `<workdir>/fetched.en.srt`."""
    def _fetch(video, workdir):
        out = Path(workdir) / "fetched.en.srt"
        _write_content(out, content)
        return out
    return _fetch


@contextmanager
def _full_path(info, sidecars=(), **extra):
    """Patch `_process_locked`'s entry gates (no state cache, not yet tagged, `info` as the probe, `sidecars` as discovered) plus `extra` (`cs` name -> `patch.object` kwargs, overriding a gate). Yields {name: mock}."""
    stubs = {"load_state": {"return_value": {}}, "probe": {"return_value": info},
             "already_processed": {"return_value": False},
             "find_sidecar_subs": {"return_value": list(sidecars)}, **extra}
    with ExitStack() as stack:
        yield {name: stack.enter_context(patch.object(cs, name, **kw)) for name, kw in stubs.items()}


def _spy_cleanup(media: Path):
    """A `_cleanup_no_main_english` stand-in recording its positional args in `seen["args"]` and echoing the status and detail it was given."""
    seen = {}

    def _cleanup(*args):
        seen["args"] = args
        return {"path": str(media), "status": args[2], "detail": args[3]}
    return seen, _cleanup


# --- _read_snapshot (shared with the upgrade's _snapshot_sidecar) ---------------

def test_snapshot_returns_bytes_and_the_four_field_identity_of_the_same_open_file(tmp_path):
    f = tmp_path / "a.en.srt"
    f.write_text(DANISH_TEXT)
    snap = _snap(f)
    assert snap.data == DANISH_TEXT.encode()
    assert snap.identity == cs.file_identity(f)  # (ino, size, mtime_ns, ctime_ns)
    assert len(snap.identity) == 4


def test_snapshot_is_bounded_oversize_file_is_unclassifiable(tmp_path, monkeypatch):
    big = tmp_path / "huge.en.srt"
    big.write_text("a" * 1000)
    monkeypatch.setattr(cs, "UPGRADE_MAX_SIDECAR_BYTES", 64)
    assert cs._read_snapshot(big) is None      # refused, never read as a prefix
    assert cs._snapshot_sidecar(big) is None   # the upgrade's wrapper agrees


def test_one_snapshot_implementation_serves_quarantine_and_upgrade(tmp_path):
    """`_snapshot_sidecar` is `_read_snapshot` with an unreadable file mapped to None; there is no second reader."""
    f = tmp_path / "a.en.srt"
    f.write_text(ENGLISH_TEXT)
    assert cs._snapshot_sidecar(f) == cs._read_snapshot(f)
    with pytest.raises(OSError):
        cs._read_snapshot(tmp_path / "missing.en.srt")
    assert cs._snapshot_sidecar(tmp_path / "missing.en.srt") is None


def _replace_inode(sidecar: Path) -> str:
    _swap_in(sidecar, ENGLISH_TEXT)
    return ENGLISH_TEXT


def _append_in_place(sidecar: Path) -> str:
    appended = "\n99\n00:09:00,000 --> 00:09:01,000\nAppended.\n"
    with open(sidecar, "a") as fh:  # same inode, new size / mtime
        fh.write(appended)
    return DANISH_TEXT + appended


def _rewrite_same_size_keeping_mtime(sidecar: Path) -> str:
    """The identity includes ctime, so a rewrite that restored size and mtime is seen."""
    stamp = sidecar.stat()
    time.sleep(0.02)  # past the filesystem's timestamp granularity (see test_fileid.py)
    rewritten = DANISH_TEXT.replace("Jeg", "Dig")
    assert len(rewritten) == len(DANISH_TEXT)
    sidecar.write_text(rewritten)
    os.utime(sidecar, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    after = sidecar.stat()
    assert (after.st_size, after.st_mtime_ns) == (stamp.st_size, stamp.st_mtime_ns)
    return rewritten


@pytest.mark.parametrize("mutate", [_replace_inode, _append_in_place, _rewrite_same_size_keeping_mtime],
                         ids=["replaced-inode", "modified-in-place", "same-size-rewrite-mtime-put-back"])
def test_quarantine_sidecar_changed_after_classification_is_not_removed(tmp_path, mutate):
    sidecar = tmp_path / "Movie (2024).en.srt"
    sidecar.write_text(DANISH_TEXT)
    snap = _snap(sidecar)
    expected = mutate(sidecar)

    assert cs._quarantine_sidecar(sidecar, snap, _classify(DANISH_TEXT), "r") is False
    assert sidecar.read_text() == expected
    assert _quarantined() == []


# --- _quarantine_sidecar mechanics ----------------------------------------------

def test_quarantine_sidecar_moves_not_deletes_and_creates_dir_lazily(tmp_path):
    sidecar = tmp_path / "Movie (2024).en.srt"
    sidecar.write_text(DANISH_TEXT)
    snap = _snap(sidecar)
    assert not cs.VAR_QUARANTINE_SUBTITLES.exists()

    ok = cs._quarantine_sidecar(sidecar, snap, _classify(DANISH_TEXT), "test reason")

    assert ok is True
    assert not sidecar.exists()  # removed from the library
    (moved,) = _quarantined()
    assert moved.name.endswith("-Movie (2024).en.srt")
    assert moved.read_bytes() == snap.data  # the classified bytes, verbatim


def _fsync_spy(monkeypatch, sidecar):
    """Record, in order, 'fsync-file', ('fsync-dir', <path>) per directory fd and 'unlink-original'; returns the event list."""
    import stat
    events = []
    real_fsync, real_unlink = os.fsync, Path.unlink

    def spy_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            events.append(("fsync-dir", Path(os.readlink(f"/proc/self/fd/{fd}"))))
        else:
            events.append("fsync-file")
        return real_fsync(fd)

    def spy_unlink(self, *a, **k):
        if self == sidecar:
            events.append("unlink-original")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(cs.os, "fsync", spy_fsync)
    monkeypatch.setattr(Path, "unlink", spy_unlink)
    return events


@pytest.mark.parametrize("existing", [False, True], ids=["first-move", "dirs-already-there"])
def test_the_whole_directory_chain_is_fsynced_before_the_original_is_unlinked(tmp_path, monkeypatch, existing):
    """Data fsync, then every directory from the copy's up to `var/` (here `tmp_path`), leaf to root, on every move (existing directories prove nothing about being on disk), and only then the unlink."""
    qdir = tmp_path / "quarantine" / "subtitles"
    if existing:
        qdir.mkdir(parents=True)
    sidecar = tmp_path / "Movie (2024).en.srt"
    sidecar.write_text(DANISH_TEXT)
    snap = _snap(sidecar)
    assert (tmp_path / "quarantine").exists() is existing
    events = _fsync_spy(monkeypatch, sidecar)

    assert cs._quarantine_sidecar(sidecar, snap, _classify(DANISH_TEXT), "r") is True

    assert events == ["fsync-file",
                      ("fsync-dir", qdir), ("fsync-dir", qdir.parent), ("fsync-dir", tmp_path),
                      "unlink-original"]
    assert not sidecar.exists() and len(_quarantined()) == 1


def test_the_chain_stops_at_var_and_never_goes_above_it(tmp_path, monkeypatch):
    var = tmp_path / "deploy" / "var"
    qdir = var / "quarantine" / "subtitles"
    monkeypatch.setattr(cs, "VAR_DIR", var)
    monkeypatch.setattr(cs, "VAR_QUARANTINE_SUBTITLES", qdir)
    sidecar = tmp_path / "Movie (2024).en.srt"
    sidecar.write_text(DANISH_TEXT)
    snap = _snap(sidecar)
    events = _fsync_spy(monkeypatch, sidecar)

    assert cs._quarantine_sidecar(sidecar, snap, _classify(DANISH_TEXT), "r") is True

    assert events == ["fsync-file",
                      ("fsync-dir", qdir), ("fsync-dir", qdir.parent), ("fsync-dir", var),
                      "unlink-original"]                       # `deploy/` (above var/) is not synced


def test_the_chain_unit_follows_var_dir_or_the_default_layout(tmp_path, monkeypatch):
    var = tmp_path / "var"
    monkeypatch.setattr(cs, "VAR_DIR", var)
    q = var / "quarantine" / "subtitles"
    assert cs._quarantine_fsync_chain(q) == [q, var / "quarantine", var]
    # relocated outside var/: the default layout var/quarantine/subtitles is assumed
    elsewhere = tmp_path / "other" / "q" / "s"
    assert cs._quarantine_fsync_chain(elsewhere) == [elsewhere, tmp_path / "other" / "q", tmp_path / "other"]
    # never loops past the filesystem root
    assert cs._quarantine_fsync_chain(Path("/a"))[-1] == Path("/")


@pytest.mark.parametrize("existing", [False, True], ids=["first-move", "dirs-already-there"])
@pytest.mark.parametrize("which", ["leaf", "middle", "var"])
def test_any_failing_directory_fsync_in_the_chain_keeps_the_original(
        tmp_path, monkeypatch, capsys, which, existing):
    import stat
    qdir = tmp_path / "quarantine" / "subtitles"
    if existing:
        qdir.mkdir(parents=True)
    sidecar = tmp_path / "Movie (2024).en.srt"
    sidecar.write_text(DANISH_TEXT)
    snap = _snap(sidecar)
    bad = {"leaf": qdir, "middle": qdir.parent, "var": tmp_path}[which]
    real_fsync = os.fsync

    def failing(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and Path(os.readlink(f"/proc/self/fd/{fd}")) == bad:
            raise OSError(errno.EIO, "Input/output error")
        return real_fsync(fd)

    monkeypatch.setattr(cs.os, "fsync", failing)

    assert cs._quarantine_sidecar(sidecar, snap, _classify(DANISH_TEXT), "r") is False

    assert sidecar.read_text() == DANISH_TEXT and _quarantined() == []   # original kept, copy removed
    assert "quarantine failed" in capsys.readouterr().out and not cs.QUARANTINE_LOG.exists()


def test_a_quarantine_directory_that_cannot_be_opened_keeps_the_original(tmp_path, monkeypatch):
    sidecar = tmp_path / "Movie (2024).en.srt"
    sidecar.write_text(DANISH_TEXT)
    snap = _snap(sidecar)
    monkeypatch.setattr(cs, "_fsync_dir", lambda p: (_ for _ in ()).throw(PermissionError("no")))

    assert cs._quarantine_sidecar(sidecar, snap, _classify(DANISH_TEXT), "r") is False
    assert sidecar.exists() and _quarantined() == []


def test_quarantine_sidecar_writes_jsonl_record(tmp_path):
    sidecar = tmp_path / "Movie (2024).en.srt"
    sidecar.write_text(DANISH_TEXT)
    snap = _snap(sidecar)

    cs._quarantine_sidecar(sidecar, snap, _classify(DANISH_TEXT), "test reason")

    (line,) = cs.QUARANTINE_LOG.read_text().strip().splitlines()
    rec = json.loads(line)
    assert rec["original_path"] == str(sidecar)
    assert rec["quarantine_path"] == str(_quarantined()[0])
    assert rec["verdict"] == "dan"
    assert rec["scores"]["verdict"] == "dan"
    assert rec["reason"] == "test reason"


def test_quarantine_sidecar_never_renames_across_filesystems(tmp_path):
    """Library and var/ can be on different filesystems: no rename/replace is needed (EXDEV must not matter)."""
    sidecar = tmp_path / "Movie (2024).en.srt"
    sidecar.write_text(DANISH_TEXT)
    snap = _snap(sidecar)

    exdev = OSError(errno.EXDEV, "cross-device link")
    with patch("os.rename", side_effect=exdev), patch("os.replace", side_effect=exdev):
        ok = cs._quarantine_sidecar(sidecar, snap, _classify(DANISH_TEXT), "r")

    assert ok is True
    assert not sidecar.exists() and len(_quarantined()) == 1


def test_quarantine_sidecar_write_failure_leaves_original_and_no_partial(tmp_path):
    sidecar = tmp_path / "Movie (2024).en.srt"
    sidecar.write_text(DANISH_TEXT)
    snap = _snap(sidecar)

    with patch("os.fsync", side_effect=OSError(errno.EIO, "disk error")):
        ok = cs._quarantine_sidecar(sidecar, snap, _classify(DANISH_TEXT), "r")

    assert ok is False
    assert sidecar.read_text() == DANISH_TEXT
    assert _quarantined() == []  # our partial copy was removed


def test_quarantine_sidecar_short_write_fails_size_check_before_unlink(tmp_path):
    sidecar = tmp_path / "Movie (2024).en.srt"
    sidecar.write_text(DANISH_TEXT)
    snap = _snap(sidecar)

    # A truncated copy, simulated by truncating the destination at fsync.
    with patch("os.fsync", side_effect=lambda fd: os.ftruncate(fd, 1)):
        ok = cs._quarantine_sidecar(sidecar, snap, _classify(DANISH_TEXT), "r")

    assert ok is False
    assert sidecar.read_text() == DANISH_TEXT
    assert _quarantined() == []


def test_quarantine_sidecar_unique_names_exclusive_create_never_overwrites(tmp_path):
    """Same basename and stamp: the second gets a new suffix; a pre-existing quarantine file is never touched."""
    dir_a, dir_b = tmp_path / "ShowA", tmp_path / "ShowB"
    dir_a.mkdir()
    dir_b.mkdir()
    sidecar_a, sidecar_b = dir_a / "Episode.en.srt", dir_b / "Episode.en.srt"
    sidecar_a.write_text(DANISH_TEXT)
    sidecar_b.write_text(FRENCH_TEXT)
    stamp = "20261013T000000.000000Z"
    cs.VAR_QUARANTINE_SUBTITLES.mkdir(parents=True)
    preexisting = cs.VAR_QUARANTINE_SUBTITLES / f"{stamp}-Episode.en.srt"
    preexisting.write_text("SENTINEL")

    with patch.object(cs, "_quarantine_stamp", return_value=stamp):
        ok_a = cs._quarantine_sidecar(sidecar_a, _snap(sidecar_a),
                                      _classify(DANISH_TEXT), "r1")
        ok_b = cs._quarantine_sidecar(sidecar_b, _snap(sidecar_b),
                                      _classify(FRENCH_TEXT), "r2")

    assert ok_a is True and ok_b is True
    assert preexisting.read_text() == "SENTINEL"
    assert {p.name for p in _quarantined()} == {
        f"{stamp}-Episode.en.srt", f"{stamp}-1-Episode.en.srt", f"{stamp}-2-Episode.en.srt"}
    assert {p.read_text() for p in _quarantined()} == {"SENTINEL", DANISH_TEXT, FRENCH_TEXT}


def test_quarantine_sidecar_vanished_before_unlink_is_a_soft_failure(tmp_path):
    sidecar = tmp_path / "Movie (2024).en.srt"
    sidecar.write_text(DANISH_TEXT)
    snap = _snap(sidecar)
    sidecar.unlink()

    assert cs._quarantine_sidecar(sidecar, snap, _classify(DANISH_TEXT), "r") is False
    assert _quarantined() == []


def test_quarantine_sidecar_unreachable_quarantine_dir_fails_soft(tmp_path, monkeypatch):
    """An uncreatable quarantine dir is a logged failure, never an exception or SystemExit."""
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setattr(cs, "VAR_QUARANTINE_SUBTITLES", blocker / "subtitles")
    sidecar = tmp_path / "Movie (2024).en.srt"
    sidecar.write_text(DANISH_TEXT)
    snap = _snap(sidecar)

    assert cs._quarantine_sidecar(sidecar, snap, _classify(DANISH_TEXT), "r") is False
    assert sidecar.read_text() == DANISH_TEXT


def test_quarantine_sidecar_jsonl_failure_does_not_undo_the_move(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setattr(cs, "QUARANTINE_LOG", blocker / "sub-quarantine.jsonl")
    sidecar = tmp_path / "Movie (2024).en.srt"
    sidecar.write_text(DANISH_TEXT)
    snap = _snap(sidecar)

    assert cs._quarantine_sidecar(sidecar, snap, _classify(DANISH_TEXT), "r") is True
    assert not sidecar.exists() and len(_quarantined()) == 1


def test_quarantine_sidecar_handles_unicode_and_spaces_in_name(tmp_path):
    sidecar = tmp_path / "Amélie - Le Fabuleux Destin (2001).en.srt"
    sidecar.write_text(FRENCH_TEXT, encoding="utf-8")
    snap = _snap(sidecar)

    assert cs._quarantine_sidecar(sidecar, snap, _classify(FRENCH_TEXT), "r") is True
    (moved,) = _quarantined()
    assert moved.name.endswith("-Amélie - Le Fabuleux Destin (2001).en.srt")
    assert moved.read_text(encoding="utf-8") == FRENCH_TEXT


# --- _quarantine_foreign_sidecars (the late-sidecar sweep, standalone) ------------

def test_quarantine_foreign_sidecars_dry_run_moves_nothing(tmp_path):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(DANISH_TEXT)

    with patch.object(cs, "find_sidecar_subs", return_value=[(sidecar, False, False)]):
        counts = cs._quarantine_foreign_sidecars(media, dry_run=True)

    assert counts == (1, 0)
    assert sidecar.read_text() == DANISH_TEXT
    assert _quarantined() == []


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be", "utf-8-sig"])
def test_quarantine_foreign_sidecars_keeps_english_in_any_encoding(tmp_path, encoding):
    """UTF-16 English must be decoded (BOM or BOM-less) before judging, so the file is kept."""
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_bytes(ENGLISH_TEXT.encode(encoding))

    with patch.object(cs, "find_sidecar_subs", return_value=[(sidecar, False, False)]):
        assert cs._quarantine_foreign_sidecars(media, dry_run=False) == (0, 0)

    assert sidecar.exists() and _quarantined() == []


def test_quarantine_foreign_sidecars_replaced_between_classification_and_unlink(tmp_path):
    """Swapped for an English file after the Danish bytes were classified: not removed, nothing quarantined."""
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(DANISH_TEXT)

    with patch.object(cs, "find_sidecar_subs", return_value=[(sidecar, False, False)]), \
         patch.object(cs, "classify_candidate", side_effect=_swap_after_classify(sidecar)):
        counts = cs._quarantine_foreign_sidecars(media, dry_run=False)

    assert counts == (0, 0)
    assert sidecar.read_text() == ENGLISH_TEXT
    assert _quarantined() == []


# --- gate through the real _process_locked --------------------------------------

_VIDEO = {"index": 0, "codec_type": "video", "codec_name": "h264"}
_AUDIO = {"index": 1, "codec_type": "audio", "codec_name": "ac3", "tags": {"language": "eng"}}


def _sub(index: int, codec: str = "subrip") -> dict:
    return {"index": index, "codec_type": "subtitle", "codec_name": codec,
            "tags": {"language": "eng"}}


_NO_SUBS = {"streams": [_VIDEO, _AUDIO]}
_IMAGE_ONLY = {"streams": [_VIDEO, _AUDIO, _sub(2, "hdmv_pgs_subtitle")]}
_CLEAN_VERIFY = {"streams": [{"codec_type": "video", "disposition": {"default": 0}},
                             {"codec_type": "audio", "disposition": {"default": 0}}]}


def _run_cleanup(media, streams, extract, sidecars=(), verify=None):
    """`_process_locked` down a NEEDS_BAZARR cleanup path with the cleanup remux faked: each call is recorded as (video, audio, eng_srt, subs) and writes a tiny output, which `verify` (default: video + audio) describes. Returns `(result, remux_calls, normalize mock)`."""
    remux_calls = []

    def _fake_remux(src, dst, video_idx, audio_idx, eng_srt, forced_srt,
                    keep_subtitle_indices=None, cleanup_flags=None, eng_track_name="English"):
        remux_calls.append({"video": video_idx, "audio": audio_idx, "eng_srt": eng_srt,
                            "subs": keep_subtitle_indices})
        Path(dst).write_bytes(b"CLEANED")
        return True, "ok"

    with _full_path(None, sidecars,
                    probe={"side_effect": lambda p: {"streams": streams} if Path(p) == media
                           else (verify or _CLEAN_VERIFY)},
                    extract_track={"side_effect": extract}, remux={"side_effect": _fake_remux},
                    _normalize_audio_inline={}) as mocks:
        result = cs._process_locked(media, str(media), dry_run=False)
    return result, remux_calls, mocks["_normalize_audio_inline"]


def test_embedded_french_track_tagged_eng_is_dropped_by_cleanup_and_stays_needs_bazarr(tmp_path):
    """A French-text track tagged `eng` is the only candidate: never chosen, NEEDS_BAZARR with a needs-english.json entry, and the cleanup drops it as proven non-English (audio/video untouched, output verified before the swap)."""
    media = _media(tmp_path)
    result, calls, norm = _run_cleanup(media, [_VIDEO, _AUDIO, _sub(2)], _extract_writing(FRENCH_TEXT))

    assert result["status"] == "NEEDS_BAZARR"
    assert "dropped_subs=1" in result["detail"]
    (call,) = calls
    assert call["eng_srt"] is None                  # cleanup mode, nothing "chosen"
    assert call["subs"] == []                       # the French track is gone
    assert call["video"] == [0] and call["audio"] == [1]  # audio/video untouched
    norm.assert_not_called()
    assert str(media.resolve()) in json.loads(cs.NEEDS_ENGLISH_STATE_FILE.read_text())


def test_embedded_rejection_leaves_other_unproven_subtitles_alone(tmp_path):
    """Only the rejected track is dropped; an untagged track the gate never judged is kept."""
    media = _media(tmp_path)
    undetermined = {"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
                    "tags": {"language": "und"}}
    verify = {"streams": [*_CLEAN_VERIFY["streams"], {"codec_type": "subtitle"}]}

    result, calls, _ = _run_cleanup(media, [_VIDEO, _AUDIO, _sub(2), undetermined],
                                    _extract_writing(FRENCH_TEXT), verify=verify)

    assert result["status"] == "NEEDS_BAZARR"
    assert [c["subs"] for c in calls] == [[3]]


def test_sidecar_french_rejected_is_quarantined_and_needs_bazarr(tmp_path):
    """A French-text `.en.srt` is the only candidate: quarantined (never deleted or chosen), file goes NEEDS_BAZARR."""
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(FRENCH_TEXT)

    with _full_path(_NO_SUBS, [(sidecar, False, False)]):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "NEEDS_BAZARR"
    assert not sidecar.exists()
    (moved,) = _quarantined()
    assert moved.read_text() == FRENCH_TEXT
    assert str(media.resolve()) in json.loads(cs.NEEDS_ENGLISH_STATE_FILE.read_text())


def test_sidecar_replaced_after_classification_is_not_quarantined_in_the_full_path(tmp_path):
    """TOCTOU through the real full path: the sidecar is swapped for an English file after the French bytes were classified. The swap survives, nothing is quarantined, the French candidate is still not chosen."""
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(FRENCH_TEXT)

    with _full_path(_NO_SUBS, [(sidecar, False, False)],
                    classify_candidate={"side_effect": _swap_after_classify(sidecar)}):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "NEEDS_BAZARR"
    assert sidecar.read_text() == ENGLISH_TEXT  # the replacement was left alone
    assert _quarantined() == []


def test_english_sidecar_still_used_normally(tmp_path):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(ENGLISH_TEXT)

    with _patched_full_path(media, [(sidecar, False, False)]) as muxed_with:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert "I don't know what you want" in muxed_with[0]
    assert _quarantined() == []


def test_rejected_sidecar_does_not_block_a_genuine_english_sidecar(tmp_path):
    media = _media(tmp_path)
    french = tmp_path / f"{media.stem}.en.sdh.srt"
    english = tmp_path / f"{media.stem}.en.srt"
    french.write_text(FRENCH_TEXT)
    english.write_text(ENGLISH_TEXT)

    with _patched_full_path(media, [(french, False, True), (english, False, False)]) as muxed_with:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert len(muxed_with) == 1 and "I don't know what you want" in muxed_with[0]
    (moved,) = _quarantined()
    assert moved.read_text() == FRENCH_TEXT


class _patched_full_path:
    """Mocks everything the full path needs; a fake remux records the subtitle text it was handed (`muxed_with`, returned by `__enter__`) and the forced text (`forced_with`)."""

    def __init__(self, media: Path, sidecars: list, streams=None, extract=None):
        self.media, self.sidecars = media, sidecars
        self.streams = streams or [_VIDEO, _AUDIO]
        self.extract = extract
        self.muxed_with: list[str] = []
        self.forced_with: list[str | None] = []
        self.kept_source_subs: list = []       # remux(kept_source_subs=...) per call (None when not passed)
        self.muxed_bytes: list[bytes] = []     # the subtitle file exactly as mkvmerge would read it
        self.forced_bytes: list[bytes | None] = []
        self._patches = []

    def __enter__(self):
        def _fake_remux(src, dst, video_idx, audio_idx, eng_srt, forced_srt,
                        keep_subtitle_indices=None, cleanup_flags=None, eng_track_name="English",
                        kept_source_subs=None):
            self.kept_source_subs.append(kept_source_subs)
            self.muxed_with.append(Path(eng_srt).read_text())
            self.forced_with.append(Path(forced_srt).read_text() if forced_srt else None)
            self.muxed_bytes.append(Path(eng_srt).read_bytes())
            self.forced_bytes.append(Path(forced_srt).read_bytes() if forced_srt else None)
            Path(dst).write_bytes(b"MUXED")
            return True, "ok"

        targets = [
            ("load_state", {"return_value": {}}),
            ("probe", {"return_value": {"streams": self.streams}}),
            ("already_processed", {"return_value": False}),
            ("find_sidecar_subs", {"return_value": self.sidecars}),
            ("run_ffsubsync", {"return_value": False}),
            ("original_language_for", {"return_value": None}),
            ("get_audio_lang_pref", {"return_value": ({"eng"}, "eng", False)}),
            ("select_keep_audio", {"return_value": [1]}),
            ("remux", {"side_effect": _fake_remux}),
            ("set_consolidated_tag", {"return_value": True}),
            ("_normalize_audio_inline", {}),
            ("update_state_entry", {}),
        ]
        if self.extract is not None:
            targets.append(("extract_track", {"side_effect": self.extract}))
        for target, kw in targets:
            p = patch.object(cs, target, **kw)
            p.start()
            self._patches.append(p)
        return self.muxed_with

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()


# --- the subliminal download goes through the same gate ---------------------

def test_subliminal_download_with_french_text_is_rejected_needs_bazarr(tmp_path):
    result, ctx = _run_image_only_fetch(tmp_path, FRENCH_TEXT, None)

    assert result["status"] == "NEEDS_BAZARR"
    assert "rejected by content gate" in result["detail"]
    assert ctx.muxed_with == []  # the French download was never muxed in


def test_subliminal_download_with_english_text_is_accepted(tmp_path):
    result, ctx = _run_image_only_fetch(tmp_path, ENGLISH_TEXT, None)

    assert result["status"] == "FIXED"
    assert "I don't know what you want" in ctx.muxed_with[0]


# --- a subliminal download whose last cue starts before SUBLIMINAL_MIN_COVERAGE of the duration (e.g. disc 1 of 2) is rejected like a content-gate rejection ---

def _srt_ending_at(last_cue_start_s: int) -> str:
    """English text (passes the content gate) plus one final cue at `last_cue_start_s`."""
    def ts(t: int) -> str:
        return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d},000"

    return (ENGLISH_TEXT + "\n5\n"
            f"{ts(last_cue_start_s)} --> {ts(last_cue_start_s + 2)}\n"
            "Look, we have to move now, but nobody noticed her leaving.\n")


def _run_image_only_fetch(tmp_path, fetched_text: str, info_format: dict | None):
    """Image-subtitle fallback with `fetched_text` as the subliminal download and `info_format` as the probe's `format` object (None = absent). Returns `(result, ctx)`."""
    media = _media(tmp_path)
    streams = [_VIDEO, _AUDIO, _sub(2, "hdmv_pgs_subtitle")]
    info = {"streams": streams}
    if info_format is not None:
        info["format"] = info_format

    ctx = _patched_full_path(media, [], streams=streams)
    with ctx, patch.object(cs, "fetch_via_subliminal", side_effect=_fetch_writing(fetched_text)), \
         patch.object(cs, "probe", return_value=info):
        return cs._process_locked(media, str(media), dry_run=False), ctx


def test_subliminal_download_covering_42_percent_is_rejected_needs_bazarr(tmp_path, capsys):
    result, ctx = _run_image_only_fetch(
        tmp_path, _srt_ending_at(1 * 3600 + 36 * 60 + 30), {"duration": "13680.0"})  # 3:48:00

    assert result["status"] == "NEEDS_BAZARR"
    assert result["detail"].startswith(
        "only image subs (subliminal result covers only 42% of the film)")
    assert ctx.muxed_with == []                      # never muxed in
    out = capsys.readouterr().out
    assert "coverage REJECT subliminal download for" in out
    assert "last cue at 1:36:30 of 3:48:00 (42%)" in out  # the log line names both times


def test_subliminal_download_covering_the_film_is_still_accepted(tmp_path, capsys):
    result, ctx = _run_image_only_fetch(
        tmp_path, _srt_ending_at(3 * 3600 + 40 * 60), {"duration": "13680.0"})   # 3:40:00 = 96%

    assert result["status"] == "FIXED"
    assert len(ctx.muxed_with) == 1 and "Look, we have to move now" in ctx.muxed_with[0]
    assert "coverage" not in capsys.readouterr().out


@pytest.mark.parametrize("last_cue_s, rejected", [(6999, True), (7000, False)])
def test_coverage_threshold_is_70_percent_of_the_duration(tmp_path, last_cue_s, rejected):
    result, _ = _run_image_only_fetch(
        tmp_path, _srt_ending_at(last_cue_s), {"duration": "10000.0"})

    assert result["status"] == ("NEEDS_BAZARR" if rejected else "FIXED")


@pytest.mark.parametrize("info_format", [
    None, {}, {"duration": "N/A"}, {"duration": "0"}, {"duration": "nan"}, {"duration": None}])
def test_unknown_duration_never_rejects_on_coverage(tmp_path, capsys, info_format):
        # ENGLISH_TEXT's last cue is at 12 s, far short of any film, but with no duration it must be accepted
    result, ctx = _run_image_only_fetch(tmp_path, ENGLISH_TEXT, info_format)

    assert result["status"] == "FIXED"
    assert len(ctx.muxed_with) == 1
    out = capsys.readouterr().out
    assert "coverage check SKIPPED" in out and "duration unknown" in out
    assert "coverage REJECT" not in out


def test_coverage_is_skipped_for_a_download_with_no_timed_cues(tmp_path, capsys):
    # Accepted by the gate (a lone line is TOO-LITTLE-TEXT) but nothing to measure.
    result, _ = _run_image_only_fetch(tmp_path, "just some words\n", {"duration": "13680.0"})

    out = capsys.readouterr().out
    assert "coverage check SKIPPED" in out and "no timed cues" in out
    assert "coverage REJECT" not in out
    assert "covers only" not in result["detail"]


def test_a_coverage_rejection_with_a_failed_extraction_stays_fail(tmp_path):
    """A coverage rejection never hides a tooling failure: an unextractable English track keeps the file FAIL."""
    media = _media(tmp_path)
    streams = [_VIDEO, _AUDIO, _sub(2), _sub(3, "hdmv_pgs_subtitle")]

    with _full_path({"streams": streams, "format": {"duration": "13680.0"}},
                    extract_track={"return_value": False},
                    fetch_via_subliminal={"side_effect": _fetch_writing(_srt_ending_at(600))},
                    remux={}) as mocks:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FAIL"
    assert result["detail"].startswith("all extracts failed")
    assert "1 rejected by content gate" in result["detail"]
    mocks["remux"].assert_not_called()


# --- tooling failures stay visible: FAIL vs NEEDS_BAZARR --------------------

def test_extraction_failure_plus_rejection_stays_fail_and_mentions_both(tmp_path):
    """One candidate failed to extract (tooling), another was content-rejected, none accepted: FAIL (the tooling problem stays visible) and the detail mentions both."""
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(FRENCH_TEXT)
    streams = {"streams": [_VIDEO, _AUDIO, _sub(2)]}  # embedded track 2: extraction will fail

    with _full_path(streams, [(sidecar, False, False)], extract_track={"return_value": False}):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FAIL"
    assert "all extracts failed" in result["detail"]
    assert "rejected by content gate" in result["detail"]
    assert len(_quarantined()) == 1  # the French sidecar is still quarantined
    assert not cs.NEEDS_ENGLISH_STATE_FILE.exists()  # FAIL is not tracked as NEEDS_BAZARR


def test_extraction_failure_alone_is_still_fail(tmp_path):
    media = _media(tmp_path)
    with _full_path({"streams": [_VIDEO, _AUDIO, _sub(2)]}, extract_track={"return_value": False}):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FAIL"
    assert result["detail"].startswith("all extracts failed")
    assert "content gate" not in result["detail"]


def test_only_rejections_no_extraction_failures_is_needs_bazarr(tmp_path):
    """Every non-accepted candidate (an embedded track AND a sidecar) was content-rejected, nothing failed to extract: NEEDS_BAZARR."""
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(DANISH_TEXT)

    result, _, _ = _run_cleanup(media, [_VIDEO, _AUDIO, _sub(2)], _extract_writing(FRENCH_TEXT),
                                [(sidecar, False, False)])

    assert result["status"] == "NEEDS_BAZARR"
    assert len(_quarantined()) == 1 and not sidecar.exists()


# --- late sidecar on an already-processed file ----------------------------------

def _state_cached(media: Path) -> dict:
    return {str(media.resolve()): {
        "size": media.stat().st_size, "mtime": int(media.stat().st_mtime),
        "v": cs.PIPELINE_VERSION, "status": "fixed"}}


@pytest.mark.parametrize("tagged, is_forced, probe_info, detail", [
    (False, False, None, "state cache; quarantined 1 sidecar(s)"),
    (True, False, {"streams": []}, "tag present; quarantined 1 sidecar(s)"),
    (False, True, None, "state cache; quarantined 1 sidecar(s)"),
], ids=["beside-state-cached-file", "beside-tagged-file", "forced-sidecar"])
def test_late_danish_sidecar_is_quarantined_and_the_video_never_touched(
        tmp_path, tagged, is_forced, probe_info, detail):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en{'.forced' if is_forced else ''}.srt"
    sidecar.write_text(DANISH_TEXT)
    original_video_bytes = media.read_bytes()

    result, _ = _late_run(tmp_path, media, [(sidecar, is_forced, False)], probe_info, tagged=tagged)

    assert result["status"] == "SKIP"
    assert result["detail"] == detail
    assert not sidecar.exists() and len(_quarantined()) == 1
    assert media.read_bytes() == original_video_bytes  # video never touched


def test_late_sidecar_dry_run_reports_would_quarantine_and_moves_nothing(tmp_path):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(DANISH_TEXT)

    with patch.object(cs, "load_state", return_value=_state_cached(media)), \
         patch.object(cs, "find_sidecar_subs", return_value=[(sidecar, False, False)]):
        result = cs._process_locked(media, str(media), dry_run=True)

    assert result["status"] == "SKIP"
    assert "dry-run: would quarantine 1 sidecar(s)" in result["detail"]
    assert sidecar.read_text() == DANISH_TEXT and _quarantined() == []


# --- the FIXED path removes only sidecars it read and accepted, while the file is still the one it read ---

def test_fixed_removes_only_the_sidecars_it_consumed(tmp_path):
    media = _media(tmp_path)
    consumed = tmp_path / f"{media.stem}.en.srt"
    consumed.write_text(ENGLISH_TEXT)
    # Files the run never classified: a foreign-named sidecar of this video,
    # and another title's sidecar that merely shares the stem prefix.
    foreign_named = tmp_path / f"{media.stem}.fr.srt"
    other_title = tmp_path / f"{media.stem} Extras.en.srt"
    foreign_named.write_text(FRENCH_TEXT)
    other_title.write_text(ENGLISH_TEXT)

    with _patched_full_path(media, [(consumed, False, False)]):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert "dropped_sidecars=1" in result["detail"]
    assert not consumed.exists()
    assert foreign_named.exists() and other_title.exists()


def test_fixed_does_not_remove_a_sidecar_replaced_after_it_was_read(tmp_path):
    """Bazarr drops a new English sidecar over the one this run read, during ffsubsync: the new file survives FIXED."""
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(ENGLISH_TEXT)
    new_text = ENGLISH_TEXT.replace("tonight", "tomorrow")

    def _swap_during_sync(*args, **kwargs):
        _swap_in(sidecar, new_text)
        return False

    with _patched_full_path(media, [(sidecar, False, False)]) as muxed_with:
        with patch.object(cs, "run_ffsubsync", side_effect=_swap_during_sync):
            result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert "dropped_sidecars=0" in result["detail"]
    assert sidecar.read_text() == new_text  # the replacement was NOT deleted
    assert "tonight" in muxed_with[0]       # what was muxed is what was read


# --- exact stem boundary, seen through the real find_sidecar_subs ---------------

def test_late_sweep_ignores_another_titles_sidecar(tmp_path):
    media = _media(tmp_path, "Movie.mkv")
    other = tmp_path / "Movie2.en.srt"
    other.write_text(DANISH_TEXT)  # would be quarantined if wrongly attributed

    with patch.object(cs, "load_state", return_value=_state_cached(media)):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["detail"] == "state cache"
    assert other.read_text() == DANISH_TEXT and _quarantined() == []


# --- image fallback keeps tooling failures visible ------------------------------

def test_image_fallback_with_a_failed_text_extraction_is_fail_not_needs_bazarr(tmp_path):
    media = _media(tmp_path)
    streams = {"streams": [_VIDEO, _AUDIO, _sub(2), _sub(3, "hdmv_pgs_subtitle")]}

    with _full_path(streams, extract_track={"return_value": False},
                    fetch_via_subliminal={"return_value": None}):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FAIL"
    assert result["detail"].startswith("all extracts failed")


def test_image_fallback_unreadable_subliminal_download_is_fail_with_reason(tmp_path):
    media = _media(tmp_path)
    # a directory in place of the file: open() on it raises IsADirectoryError (an OSError)
    with _full_path(_IMAGE_ONLY, fetch_via_subliminal={"side_effect": _fetch_writing(lambda out: out.mkdir())}):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FAIL"
    assert "subliminal download unreadable" in result["detail"]


def test_image_fallback_with_nothing_fetched_and_no_failures_is_still_needs_bazarr(tmp_path):
    media = _media(tmp_path)
    with _full_path(_IMAGE_ONLY, fetch_via_subliminal={"return_value": None}):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "NEEDS_BAZARR"
    assert "subliminal fetch failed" in result["detail"]


# --- forced English-named sidecars go through the gate too ----------------------

# A short forced file (signs / foreign dialogue only): TOO-LITTLE-TEXT.
SHORT_FORCED_ENGLISH = (
    "1\n00:00:01,000 --> 00:00:02,000\n[speaking French]\n\n"
    "2\n00:00:03,000 --> 00:00:04,000\nWhere is the station?\n"
)


def test_foreign_forced_sidecar_is_quarantined_in_the_full_path(tmp_path):
    media = _media(tmp_path)
    forced = tmp_path / f"{media.stem}.en.forced.srt"
    english = tmp_path / f"{media.stem}.en.srt"
    forced.write_text(DANISH_TEXT)
    english.write_text(ENGLISH_TEXT)

    ctx = _patched_full_path(media, [(forced, True, False), (english, False, False)])
    with ctx:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert ctx.forced_with == [None]  # never embedded as the forced track
    assert not forced.exists()
    (moved,) = _quarantined()
    assert moved.read_text() == DANISH_TEXT


def test_foreign_only_forced_sidecar_is_quarantined_then_no_candidates(tmp_path):
    """The bogus forced file no longer counts as "only forced English": it is quarantined and the file is plain NEEDS_BAZARR (so sub-fallback's FORCED_SIDECAR_PRESENT block cannot be triggered by it)."""
    media = _media(tmp_path)
    forced = tmp_path / f"{media.stem}.en.forced.srt"
    forced.write_text(DANISH_TEXT)

    with _full_path(_NO_SUBS, [(forced, True, False)]):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "NEEDS_BAZARR"
    assert not forced.exists() and len(_quarantined()) == 1


def test_short_english_forced_sidecar_is_kept_in_the_full_path(tmp_path):
    media = _media(tmp_path)
    forced = tmp_path / f"{media.stem}.en.forced.srt"
    forced.write_text(SHORT_FORCED_ENGLISH)

    with _full_path(_NO_SUBS, [(forced, True, False)]):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FAIL" and "only forced English" in result["detail"]
    assert forced.read_text() == SHORT_FORCED_ENGLISH and _quarantined() == []


def test_full_path_dry_run_does_not_gate_or_move_forced_sidecars(tmp_path):
    media = _media(tmp_path)
    forced = tmp_path / f"{media.stem}.en.forced.srt"
    forced.write_text(DANISH_TEXT)

    with _full_path(_NO_SUBS, [(forced, True, False)]):
        cs._process_locked(media, str(media), dry_run=True)

    assert forced.read_text() == DANISH_TEXT and _quarantined() == []


@pytest.mark.parametrize("text, is_forced", [(ENGLISH_TEXT, False), (SHORT_FORCED_ENGLISH, True)],
                         ids=["english", "short-english-forced"])
def test_late_english_sidecar_is_kept_beside_state_cached_file(tmp_path, text, is_forced):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en{'.forced' if is_forced else ''}.srt"
    sidecar.write_text(text)

    result, _ = _late_run(tmp_path, media, [(sidecar, is_forced, False)], None)

    assert result["status"] == "SKIP" and result["detail"] == "state cache"
    assert sidecar.read_text() == text and _quarantined() == []


# --- redundant English late sidecars: beside an embedded English text subtitle, Bazarr's late `.en.srt` / `.en.hi.srt` are quarantined too ---

_REASON = "redundant: embedded English subtitle present"


def _eng_track(index=2, codec="subrip", title=None, forced=False, lang="eng") -> dict:
    t = {"index": index, "codec_type": "subtitle", "codec_name": codec,
         "tags": {"language": lang}, "disposition": {"forced": int(forced)}}
    if title:
        t["tags"]["title"] = title
    return t


def _probe_with(*tracks) -> dict:
    return {"streams": [_VIDEO, _AUDIO, *tracks]}


def _late_run(tmp_path, media, sidecar_entries, probe_info, *, tagged=False, dry_run=False):
    """_process_locked on an already-processed `media`; returns (result, probe mock)."""
    probe_mock = patch.object(cs, "probe", return_value=probe_info)
    with probe_mock as pm, \
         patch.object(cs, "find_sidecar_subs", return_value=sidecar_entries):
        if tagged:
            with patch.object(cs, "load_state", return_value={}), \
                 patch.object(cs, "already_processed", return_value=True), \
                 patch.object(cs, "update_state_entry"):
                return cs._process_locked(media, str(media), dry_run=dry_run), pm
        with patch.object(cs, "load_state", return_value=_state_cached(media)):
            return cs._process_locked(media, str(media), dry_run=dry_run), pm


def test_redundant_hi_and_non_hi_english_sidecars_are_quarantined(tmp_path):
    """Real find_sidecar_subs, real classification."""
    media = _media(tmp_path)
    hi = tmp_path / f"{media.stem}.en.hi.srt"
    plain = tmp_path / f"{media.stem}.en.srt"
    hi.write_text(ENGLISH_TEXT)
    plain.write_text(ENGLISH_TEXT)
    original_video_bytes = media.read_bytes()

    with patch.object(cs, "probe", return_value=_probe_with(_eng_track())) as pm, \
         patch.object(cs, "load_state", return_value=_state_cached(media)):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "SKIP"
    assert result["detail"] == "state cache; quarantined 2 redundant sidecar(s)"
    assert not hi.exists() and not plain.exists()
    assert len(_quarantined()) == 2
    assert pm.call_count == 1                      # probed once for both
    assert media.read_bytes() == original_video_bytes
    reasons = [json.loads(line)["reason"] for line in cs.QUARANTINE_LOG.read_text().splitlines()]
    assert reasons == [_REASON, _REASON]


def test_redundant_english_sidecar_beside_a_tagged_file_reuses_the_existing_probe(tmp_path):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(ENGLISH_TEXT)

    result, pm = _late_run(tmp_path, media, [(sidecar, False, False)],
                           _probe_with(_eng_track()), tagged=True)

    assert result["detail"] == "tag present; quarantined 1 redundant sidecar(s)"
    assert not sidecar.exists()
    assert pm.call_count == 1  # _process_locked's own probe; the sweep did not probe again


def test_english_sidecar_kept_when_the_file_has_no_embedded_english(tmp_path):
    """Embedded French, an English IMAGE track and a FORCED-only English track do not count as embedded English."""
    media = _media(tmp_path)
    for tracks in ([], [_eng_track(lang="fre")],
                   [_eng_track(codec="hdmv_pgs_subtitle")],
                   [_eng_track(forced=True)]):
        sidecar = tmp_path / f"{media.stem}.en.srt"
        sidecar.write_text(ENGLISH_TEXT)
        result, _ = _late_run(tmp_path, media, [(sidecar, False, False)], _probe_with(*tracks))
        assert result["detail"] == "state cache"
        assert sidecar.read_text() == ENGLISH_TEXT and _quarantined() == []


def test_english_sidecar_kept_as_possible_upgrade_for_a_generated_track(tmp_path, capsys):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(ENGLISH_TEXT)

    result, _ = _late_run(tmp_path, media, [(sidecar, False, False)],
                          _probe_with(_eng_track(title="English (generated)")))

    assert result["detail"] == "state cache"
    assert sidecar.read_text() == ENGLISH_TEXT and _quarantined() == []
    assert "kept: possible upgrade for generated subtitle" in capsys.readouterr().out


def test_generated_sidecar_beside_a_generated_track_is_redundant(tmp_path):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.generated.srt"
    sidecar.write_text(ENGLISH_TEXT)

    result, _ = _late_run(tmp_path, media, [(sidecar, False, False)],
                          _probe_with(_eng_track(title="English (generated)")))

    assert result["detail"] == "state cache; quarantined 1 redundant sidecar(s)"
    assert not sidecar.exists()


def test_movie_titled_generated_does_not_make_its_sidecars_look_generated(tmp_path):
    """Only the tags AFTER the video stem count as a ".generated" tag."""
    media = _media(tmp_path, "Generated.mkv")
    sidecar = tmp_path / "Generated.en.srt"
    sidecar.write_text(ENGLISH_TEXT)

    result, _ = _late_run(tmp_path, media, [(sidecar, False, False)],
                          _probe_with(_eng_track(title="English (generated)")))

    assert result["detail"] == "state cache"  # still an "upgrade" candidate: kept
    assert sidecar.exists()


def test_a_real_embedded_track_beside_a_generated_one_makes_the_sidecar_redundant(tmp_path):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(ENGLISH_TEXT)

    result, _ = _late_run(
        tmp_path, media, [(sidecar, False, False)],
        _probe_with(_eng_track(2, title="English (generated)"), _eng_track(3, title="English")))

    assert result["detail"] == "state cache; quarantined 1 redundant sidecar(s)"


def test_probe_failure_keeps_every_english_sidecar(tmp_path):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(ENGLISH_TEXT)

    result, _ = _late_run(tmp_path, media, [(sidecar, False, False)], None)

    assert result["detail"] == "state cache"
    assert sidecar.read_text() == ENGLISH_TEXT and _quarantined() == []


def test_a_failed_probe_is_cached_for_the_whole_sweep(tmp_path):
    media = _media(tmp_path)
    hi = tmp_path / f"{media.stem}.en.hi.srt"
    plain = tmp_path / f"{media.stem}.en.srt"
    hi.write_text(ENGLISH_TEXT)
    plain.write_text(ENGLISH_TEXT)

    with patch.object(cs, "probe", return_value=None) as pm, \
         patch.object(cs, "find_sidecar_subs",
                      return_value=[(hi, False, True), (plain, False, False)]):
        counts = cs._quarantine_foreign_sidecars(media, dry_run=False)

    assert counts == (0, 0) and pm.call_count == 1  # not retried per sidecar
    assert hi.exists() and plain.exists()


def test_foreign_sidecar_is_still_quarantined_when_the_probe_fails(tmp_path):
    media = _media(tmp_path)
    danish = tmp_path / f"{media.stem}.en.srt"
    danish.write_text(DANISH_TEXT)

    result, _ = _late_run(tmp_path, media, [(danish, False, False)], None)
    assert result["detail"] == "state cache; quarantined 1 sidecar(s)"


def test_the_sweep_itself_never_probes_for_a_foreign_sidecar(tmp_path):
    """The sweep alone needs no probe for a foreign verdict (through `_process_locked` the upgrade check may probe first)."""
    media = _media(tmp_path)
    danish = tmp_path / f"{media.stem}.en.srt"
    danish.write_text(DANISH_TEXT)

    with patch.object(cs, "probe", return_value=None) as pm, \
         patch.object(cs, "find_sidecar_subs", return_value=[(danish, False, False)]):
        assert cs._quarantine_foreign_sidecars(media, dry_run=False) == (1, 0)
    pm.assert_not_called()


def test_no_probe_without_an_english_sidecar(tmp_path):
    media = _media(tmp_path)
    _, pm = _late_run(tmp_path, media, [], _probe_with(_eng_track()))
    assert pm.call_count == 0


def test_foreign_and_redundant_counts_are_reported_separately(tmp_path):
    media = _media(tmp_path)
    danish = tmp_path / f"{media.stem}.da.en.srt"
    english = tmp_path / f"{media.stem}.en.hi.srt"
    danish.write_text(DANISH_TEXT)
    english.write_text(ENGLISH_TEXT)

    result, _ = _late_run(tmp_path, media, [(danish, False, False), (english, False, True)],
                          _probe_with(_eng_track()))

    assert result["detail"] == ("state cache; quarantined 1 sidecar(s); "
                                "quarantined 1 redundant sidecar(s)")
    assert {json.loads(l)["reason"] for l in cs.QUARANTINE_LOG.read_text().splitlines()} == {
        _REASON, "late sidecar beside already-processed file"}


def test_forced_english_sidecar_is_redundant_only_beside_an_embedded_forced_track(tmp_path):
    media = _media(tmp_path)
    forced = tmp_path / f"{media.stem}.en.forced.srt"
    forced.write_text(SHORT_FORCED_ENGLISH)

    # no embedded English at all: kept
    kept, _ = _late_run(tmp_path, media, [(forced, True, False)], _probe_with())
    assert kept["detail"] == "state cache" and forced.exists()

    # a verified embedded FORCED English track retires it
    emb_forced = _eng_track(2, title="Forced", forced=True)
    gone, _ = _late_run(tmp_path, media, [(forced, True, False)], _probe_with(emb_forced))
    assert gone["detail"] == "state cache; quarantined 1 redundant sidecar(s)"
    assert not forced.exists()


def test_a_main_english_track_never_retires_a_forced_sidecar(tmp_path):
    """The main track covers the dialogue, not the signs and foreign speech a forced track is for."""
    media = _media(tmp_path)
    forced = tmp_path / f"{media.stem}.en.forced.srt"
    forced.write_text(SHORT_FORCED_ENGLISH)

    with patch.object(cs.subverify, "embedded_text_window") as ex:
        result, _ = _late_run(tmp_path, media, [(forced, True, False)],
                              _probe_with(_eng_track(2)))   # a main track, no forced one

    assert result["detail"] == "state cache"
    assert forced.read_text() == SHORT_FORCED_ENGLISH and _quarantined() == []
    ex.assert_not_called()   # nothing to verify: no forced track exists


def test_a_forced_track_needs_english_or_short_text_but_not_foreign_text(tmp_path):
    """Forced tracks are short, so too little text counts as English evidence for them only; foreign or unlisted text does not."""
    media = _media(tmp_path)
    emb_forced = _eng_track(2, title="Forced", forced=True)
    filler = " ".join(["habari", "gani", "rafiki", "yangu", "leo", "ni", "siku", "njema"] * 5)
    for sample, retired in ((SHORT_FORCED_ENGLISH, True), (ENGLISH_TEXT, True),
                            (FRENCH_TEXT, False), (filler, False)):
        forced = tmp_path / f"{media.stem}.en.forced.srt"
        forced.write_text(SHORT_FORCED_ENGLISH)
        with patch.object(cs.subverify, "embedded_text_window", return_value=sample):
            result, _ = _late_run(tmp_path, media, [(forced, True, False)],
                                  _probe_with(emb_forced))
        assert (result["detail"] != "state cache") is retired, sample[:30]
        assert forced.exists() is (not retired)
        for f in _quarantined():
            f.unlink()


def test_redundant_sidecar_dry_run_reports_and_moves_nothing(tmp_path):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(ENGLISH_TEXT)

    result, _ = _late_run(tmp_path, media, [(sidecar, False, False)],
                          _probe_with(_eng_track()), dry_run=True)

    assert result["detail"] == "state cache; dry-run: would quarantine 1 redundant sidecar(s)"
    assert sidecar.read_text() == ENGLISH_TEXT and _quarantined() == []


def test_redundant_sidecar_replaced_after_classification_is_not_removed(tmp_path):
    """Same identity re-check as every quarantine: a new sidecar swapped in after classification survives."""
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(ENGLISH_TEXT)
    new_text = ENGLISH_TEXT.replace("tonight", "tomorrow")

    with patch.object(cs, "classify_candidate", side_effect=_swap_after_classify(sidecar, new_text)):
        result, _ = _late_run(tmp_path, media, [(sidecar, False, False)], _probe_with(_eng_track()))

    assert result["detail"] == "state cache"
    assert sidecar.read_text() == new_text and _quarantined() == []


# --- a late English sidecar is redundant only if the embedded track's CONTENT is English, not just its tag ---

def test_embedded_french_track_tagged_eng_does_not_make_the_english_sidecar_redundant(
        tmp_path, capsys):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(ENGLISH_TEXT)

    with patch.object(cs.subverify, "embedded_text_window", return_value=FRENCH_TEXT):
        result, _ = _late_run(tmp_path, media, [(sidecar, False, False)],
                              _probe_with(_eng_track()))

    assert result["detail"] == "state cache"
    assert sidecar.read_text() == ENGLISH_TEXT and _quarantined() == []
    out = capsys.readouterr().out
    assert "tagged English but its text is not positive English evidence (language check: fre)" in out
    assert "kept: embedded English subtitle" in out


def test_embedded_english_content_makes_the_sidecar_redundant_and_is_sampled_boundedly(tmp_path):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(ENGLISH_TEXT)

    with patch.object(cs.subverify, "embedded_text_window", return_value=ENGLISH_TEXT) as ex:
        result, _ = _late_run(tmp_path, media, [(sidecar, False, False)],
                              _probe_with(_eng_track(5, codec="ass")))

    assert result["detail"] == "state cache; quarantined 1 redundant sidecar(s)"
    ex.assert_called_once_with(media, 5, "ass", 300.0, 600.0)  # that stream only, one bounded window


@pytest.mark.parametrize("window", [None, "", "   \n", RuntimeError("ffprobe blew up")])
def test_embedded_text_that_cannot_be_read_keeps_the_sidecar(tmp_path, window):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(ENGLISH_TEXT)
    kw = {"side_effect": window} if isinstance(window, Exception) else {"return_value": window}

    with patch.object(cs.subverify, "embedded_text_window", **kw):
        result, _ = _late_run(tmp_path, media, [(sidecar, False, False)],
                              _probe_with(_eng_track()))

    assert result["detail"] == "state cache"
    assert sidecar.read_text() == ENGLISH_TEXT and _quarantined() == []


def test_embedded_content_is_verified_once_per_track_across_sidecars(tmp_path):
    media = _media(tmp_path)
    hi = tmp_path / f"{media.stem}.en.hi.srt"
    plain = tmp_path / f"{media.stem}.en.srt"
    hi.write_text(ENGLISH_TEXT)
    plain.write_text(ENGLISH_TEXT)

    with patch.object(cs.subverify, "embedded_text_window", return_value=ENGLISH_TEXT) as ex:
        result, _ = _late_run(tmp_path, media, [(hi, False, True), (plain, False, False)],
                              _probe_with(_eng_track()))

    assert result["detail"] == "state cache; quarantined 2 redundant sidecar(s)"
    assert ex.call_count == 1


def test_embedded_content_is_not_sampled_when_no_redundancy_decision_is_possible(tmp_path):
    media = _media(tmp_path)
    english = tmp_path / f"{media.stem}.en.srt"
    english.write_text(ENGLISH_TEXT)
    danish = tmp_path / f"{media.stem}.da.en.srt"
    danish.write_text(DANISH_TEXT)

    with patch.object(cs.subverify, "embedded_text_window") as ex:
        _late_run(tmp_path, media, [(english, False, False)], _probe_with())  # no embedded English
        _late_run(tmp_path, media, [(english, False, False)],
                  _probe_with(_eng_track(title="English (generated)")))       # possible upgrade
        _late_run(tmp_path, media, [(danish, False, False)], _probe_with(_eng_track()))  # foreign
    ex.assert_not_called()


def test_a_second_embedded_track_can_verify_but_the_checks_are_capped(tmp_path):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(ENGLISH_TEXT)

    # Track 3 is real English though track 2 is French tagged eng -> redundant.
    def _by_track(src, idx, codec, start, dur):
        return ENGLISH_TEXT if idx == 3 else FRENCH_TEXT

    with patch.object(cs.subverify, "embedded_text_window", side_effect=_by_track):
        result, _ = _late_run(tmp_path, media, [(sidecar, False, False)],
                              _probe_with(_eng_track(2), _eng_track(3)))
    assert result["detail"] == "state cache; quarantined 1 redundant sidecar(s)"

    # Five French tracks tagged eng: only the first three are sampled, all rejected.
    sidecar.write_text(ENGLISH_TEXT)
    with patch.object(cs.subverify, "embedded_text_window", return_value=FRENCH_TEXT) as ex:
        result, _ = _late_run(tmp_path, media, [(sidecar, False, False)],
                              _probe_with(*[_eng_track(i) for i in range(2, 7)]))
    assert result["detail"] == "state cache" and ex.call_count == 3
    assert sidecar.exists()


# --- FIXED must not drop a needed forced sidecar --------------------------------

def test_forced_sidecar_is_embedded_as_the_forced_track_then_removed(tmp_path):
    """No embedded forced track: the accepted forced sidecar becomes the remux's forced subtitle, and only then counts as consumed."""
    media = _media(tmp_path)
    forced = tmp_path / f"{media.stem}.en.forced.srt"
    english = tmp_path / f"{media.stem}.en.srt"
    forced.write_text(SHORT_FORCED_ENGLISH)
    english.write_text(ENGLISH_TEXT)

    ctx = _patched_full_path(media, [(forced, True, False), (english, False, False)])
    with ctx as muxed_with:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert "I don't know what you want" in muxed_with[0]
    assert len(ctx.forced_with) == 1 and "Where is the station?" in ctx.forced_with[0]
    assert "dropped_sidecars=2" in result["detail"]
    assert not forced.exists() and not english.exists()
    assert _quarantined() == []


def test_embedded_forced_track_is_used_and_the_forced_sidecar_is_left_in_place(tmp_path):
    media = _media(tmp_path)
    forced_sidecar = tmp_path / f"{media.stem}.en.forced.srt"
    english = tmp_path / f"{media.stem}.en.srt"
    forced_sidecar.write_text(SHORT_FORCED_ENGLISH)
    english.write_text(ENGLISH_TEXT)
    embedded_forced = _eng_track(2, title="Forced", forced=True)

    embedded_cue = "1\n00:00:01,000 --> 00:00:02,000\nEmbedded forced cue.\n"
    ctx = _patched_full_path(media, [(forced_sidecar, True, False), (english, False, False)],
                             streams=[_VIDEO, _AUDIO, embedded_forced], extract=_extract_writing(embedded_cue))
    with ctx:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert "Embedded forced cue." in ctx.forced_with[0]
    assert "station" not in ctx.forced_with[0]
    assert forced_sidecar.read_text() == SHORT_FORCED_ENGLISH  # not consumed: left in place
    assert not english.exists()
    assert "dropped_sidecars=1" in result["detail"]


# --- never remove a subtitle without POSITIVE proof -----------------------------

_FILLER = " ".join(["habari", "gani", "rafiki", "yangu", "leo", "ni", "siku", "njema"] * 5)


@pytest.mark.parametrize("sample", [
    "Bonjour. Je ne sais pas.",         # too little text, French: the gate accepts it...
    "Shh! Someone's coming.",           # too little text, English: still not enough for a main track
    _FILLER,                            # low-token unlisted language: the gate's benefit of the doubt
])  # (clearly foreign text: test_embedded_french_track_tagged_eng_does_not_make_the_english_sidecar_redundant)
def test_a_main_sidecar_is_redundant_only_on_positive_english_evidence(tmp_path, sample, capsys):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(ENGLISH_TEXT)

    with patch.object(cs.subverify, "embedded_text_window", return_value=sample):
        result, _ = _late_run(tmp_path, media, [(sidecar, False, False)],
                              _probe_with(_eng_track()))

    assert result["detail"] == "state cache"
    assert sidecar.read_text() == ENGLISH_TEXT and _quarantined() == []
    assert "kept: embedded English subtitle" in capsys.readouterr().out


def test_the_embedded_samples_that_pass_the_gate_but_not_the_proof_are_exactly_these():
    """The content gate accepts each of these (benefit of the doubt); only sublang's `eng` verdict is proof."""
    for text in ("Bonjour. Je ne sais pas.", "Shh! Someone's coming.", _FILLER):
        assert cs.classify_candidate(text)[0] is True
        assert cs.sublang.score(text)["verdict"] != "eng"
    assert cs.sublang.score(ENGLISH_TEXT)["verdict"] == "eng"


# --- the video must still be the one that was probed (4-field identity) ------------------------

def _two_redundant_sidecars(tmp_path, media):
    hi = tmp_path / f"{media.stem}.en.hi.srt"
    plain = tmp_path / f"{media.stem}.en.srt"
    hi.write_text(ENGLISH_TEXT)
    plain.write_text(ENGLISH_TEXT)
    return hi, plain, [(hi, False, True), (plain, False, False)]


def _change_video(media, how):
    time.sleep(0.02)  # past the filesystem's timestamp granularity
    if how == "rewrite":
        media.write_bytes(media.read_bytes() + b"x")
    elif how == "metadata":
        os.chmod(media, 0o640 if (media.stat().st_mode & 0o777) != 0o640 else 0o600)
    elif how == "replace":
        other = media.parent / "other.tmp"
        other.write_bytes(b"\x1a\x45\xdf\xa3")
        os.replace(other, media)


@pytest.mark.parametrize("how", ["rewrite", "metadata", "replace"])
def test_the_sweep_stops_when_the_video_changes_after_it_was_probed(tmp_path, how):
    """The probe happens after the identity is taken; a video that changed by the first move keeps everything."""
    media = _media(tmp_path)
    hi, plain, listing = _two_redundant_sidecars(tmp_path, media)

    def _probe_then_change(p):
        _change_video(media, how)
        return _probe_with(_eng_track())

    with patch.object(cs, "probe", side_effect=_probe_then_change), \
         patch.object(cs, "find_sidecar_subs", return_value=listing), \
         patch.object(cs, "load_state", return_value=_state_cached(media)):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "SKIP" and result["detail"] == "state cache"
    assert hi.exists() and plain.exists() and _quarantined() == []


def test_the_sweep_stops_after_a_move_if_the_video_changes_between_moves(tmp_path):
    media = _media(tmp_path)
    hi, plain, listing = _two_redundant_sidecars(tmp_path, media)
    real_quarantine = cs._quarantine_sidecar
    moves = []

    def _quarantine_then_change(*a, **k):
        ok = real_quarantine(*a, **k)
        moves.append(a[0].name)
        _change_video(media, "rewrite")
        return ok

    with patch.object(cs, "_quarantine_sidecar", side_effect=_quarantine_then_change):
        counts = cs._quarantine_foreign_sidecars(
            media, dry_run=False, info=_probe_with(_eng_track()), video_identity=cs.file_identity(media))
    # (find_sidecar_subs is real here: both files are found; whichever is first moves, the rest stays)
    assert len(moves) == 1 and counts == (0, 1)
    assert sum(p.exists() for p in (hi, plain)) == 1 and len(_quarantined()) == 1


def test_an_unchanged_video_lets_the_sweep_finish_and_dry_run_never_checks(tmp_path):
    media = _media(tmp_path)
    hi, plain, listing = _two_redundant_sidecars(tmp_path, media)
    with patch.object(cs, "find_sidecar_subs", return_value=listing):
        assert cs._quarantine_foreign_sidecars(
            media, dry_run=True, info=_probe_with(_eng_track())) == (0, 2)
        assert hi.exists() and plain.exists()
        assert cs._quarantine_foreign_sidecars(
            media, dry_run=False, info=_probe_with(_eng_track())) == (0, 2)
    assert not hi.exists() and not plain.exists() and len(_quarantined()) == 2


def test_a_video_that_cannot_be_stated_keeps_everything(tmp_path):
    media = _media(tmp_path)
    hi, plain, listing = _two_redundant_sidecars(tmp_path, media)
    with patch.object(cs, "find_sidecar_subs", return_value=listing), \
         patch.object(cs, "file_identity", return_value=None):
        assert cs._quarantine_foreign_sidecars(
            media, dry_run=False, info=_probe_with(_eng_track())) == (0, 0)
    assert hi.exists() and plain.exists()


# --- the EMBEDDED forced track goes through the content gate too -----------------

@pytest.mark.parametrize("foreign", [DANISH_TEXT, FRENCH_TEXT], ids=["danish", "french"])
def test_foreign_text_in_an_eng_tagged_forced_track_is_not_used_as_the_forced_track(
        tmp_path, capsys, foreign):
    media = _media(tmp_path)
    english = tmp_path / f"{media.stem}.en.srt"
    english.write_text(ENGLISH_TEXT)
    ctx = _patched_full_path(
        media, [(english, False, False)], streams=[_VIDEO, _AUDIO, _FORCED_TRACK],
        extract=_extract_writing(foreign))
    with ctx:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert ctx.forced_with == [None]       # the foreign "forced" track is not re-attached (and so dropped)
    assert ctx.kept_source_subs == [None]  # nor stream-copied: a rejected track is proven foreign
    assert "content-gate REJECT embedded forced track 2" in capsys.readouterr().out


def test_a_rejected_embedded_forced_track_falls_back_to_an_accepted_forced_sidecar(tmp_path):
    media = _media(tmp_path)
    english = tmp_path / f"{media.stem}.en.srt"
    forced_sidecar = tmp_path / f"{media.stem}.en.forced.srt"
    english.write_text(ENGLISH_TEXT)
    forced_sidecar.write_text(SHORT_FORCED_ENGLISH)
    ctx = _patched_full_path(
        media, [(english, False, False), (forced_sidecar, True, False)],
        streams=[_VIDEO, _AUDIO, _FORCED_TRACK], extract=_extract_writing(DANISH_TEXT))
    with ctx:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert "Where is the station?" in ctx.forced_with[0]      # the sidecar became the forced track
    assert not forced_sidecar.exists()                         # and, being embedded, was consumed


# --- no baseline before the probe: nothing is moved for that call --------------

def _identity_unreadable_once():
    """A `file_identity` whose first call (the baseline taken before probing) fails and works afterwards."""
    real, calls = cs.file_identity, []

    def flaky(p):
        calls.append(p)
        return None if len(calls) == 1 else real(p)
    return flaky


@pytest.mark.parametrize("tagged", [False, True])
def test_an_unreadable_baseline_identity_keeps_everything_even_if_it_reads_fine_later(
        tmp_path, capsys, tagged):
    media = _media(tmp_path)
    hi, plain, listing = _two_redundant_sidecars(tmp_path, media)

    with patch.object(cs, "file_identity", side_effect=_identity_unreadable_once()):
        result, _ = _late_run(tmp_path, media, listing, _probe_with(_eng_track()), tagged=tagged)

    assert result["status"] == "SKIP"
    assert result["detail"] == ("tag present" if tagged else "state cache")
    assert hi.exists() and plain.exists() and _quarantined() == []
    assert "identity could not be read before it was probed" in capsys.readouterr().out


def test_a_foreign_sidecar_is_kept_too_without_a_baseline(tmp_path):
    media = _media(tmp_path)
    danish = tmp_path / f"{media.stem}.en.srt"
    danish.write_text(DANISH_TEXT)
    with patch.object(cs, "find_sidecar_subs", return_value=[(danish, False, False)]):
        assert cs._quarantine_foreign_sidecars(media, dry_run=False, video_identity=None) == (0, 0)
    assert danish.read_text() == DANISH_TEXT and _quarantined() == []


def test_a_caller_that_passes_no_baseline_at_all_still_gets_one_taken_at_the_start(tmp_path):
    media = _media(tmp_path)
    danish = tmp_path / f"{media.stem}.en.srt"
    danish.write_text(DANISH_TEXT)
    with patch.object(cs, "find_sidecar_subs", return_value=[(danish, False, False)]):
        assert cs._quarantine_foreign_sidecars(media, dry_run=False) == (1, 0)


# --- a candidate the gate could not classify (oversize, changing while read, not text) is skipped, never copied, ranked or muxed; with nothing else usable it is FAIL "all extracts failed" ---

_BINARY = bytes(range(256)) * 20          # NULs, invalid UTF-8: not a subtitle in any encoding


def _unclassifiable(kind: str, path: Path) -> None:
    """Make `path` a file the gate cannot classify (`kind`: oversize, binary)."""
    if kind == "oversize":
        with path.open("wb") as fh:                     # sparse: no 8 MiB is written
            fh.truncate(cs.UPGRADE_MAX_SIDECAR_BYTES + 1)
    else:
        path.write_bytes(_BINARY)


@pytest.fixture
def _changing_while_read(monkeypatch):
    """`unstable` kind: `_read_snapshot` gives up on every file named 'unstable', as for a file being written under us."""
    real = cs._read_snapshot
    monkeypatch.setattr(cs, "_read_snapshot",
                        lambda p: None if "unstable" in Path(p).name else real(p))


_KINDS = ["oversize", "binary", "unstable"]


def _make(kind: str, path: Path) -> None:
    if kind == "unstable":
        path.write_text(ENGLISH_TEXT)       # perfectly good text, but never read stably
    else:
        _unclassifiable(kind, path)


def _name(kind: str, media: Path) -> Path:
    return media.with_name(f"{media.stem}.en{'.unstable' if kind == 'unstable' else ''}.srt")


@pytest.mark.parametrize("kind", _KINDS)
def test_an_unclassifiable_sidecar_is_not_a_candidate_and_is_left_in_place(
        tmp_path, kind, _changing_while_read, capsys):
    media = _media(tmp_path)
    sidecar = _name(kind, media)
    _make(kind, sidecar)
    before = sidecar.stat()

    with _full_path(_NO_SUBS, [(sidecar, False, False)], _sidecar_as_srt={}, remux={}) as mocks:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FAIL" and result["detail"].startswith("all extracts failed")
    mocks["_sidecar_as_srt"].assert_not_called()   # never copied into the working directory...
    mocks["remux"].assert_not_called()             # ...nor muxed
    assert sidecar.exists() and sidecar.stat().st_ino == before.st_ino   # left in place
    assert _quarantined() == []                # unclassified is not "foreign"
    assert "content-gate SKIP sidecar" in capsys.readouterr().out
    assert not cs.NEEDS_ENGLISH_STATE_FILE.exists()


@pytest.mark.parametrize("kind", _KINDS)
def test_an_unclassifiable_sidecar_never_blocks_or_rides_along_with_a_good_one(
        tmp_path, kind, _changing_while_read):
    media = _media(tmp_path)
    bad = _name(kind, media)
    good = tmp_path / f"{media.stem}.en.sdh.srt"
    _make(kind, bad)
    good.write_text(ENGLISH_TEXT)

    ctx = _patched_full_path(media, [(bad, False, False), (good, False, True)])
    with ctx:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert len(ctx.muxed_bytes) == 1 and ctx.muxed_bytes[0] == ENGLISH_TEXT.encode()
    assert bad.exists()                        # not consumed: FIXED removes only what it used
    assert not good.exists() and _quarantined() == []


def _unreadable_extract(how: str):
    """`extract_track` fake for an embedded track the gate cannot use: "extract-fails", "oversize" or "binary"."""
    if how == "extract-fails":
        return lambda src, idx, codec, dst: False
    return _extract_writing(lambda dst: _unclassifiable(how, dst))


@pytest.mark.parametrize("kind", ["oversize", "binary"])
def test_an_extracted_track_whose_text_cannot_be_read_is_skipped_as_a_failed_extraction(
        tmp_path, kind, capsys):
    media = _media(tmp_path)
    seen, cleanup = _spy_cleanup(media)

    with _full_path({"streams": [_VIDEO, _AUDIO, _sub(2)]}, extract_track={"side_effect": _unreadable_extract(kind)},
                    _cleanup_no_main_english={"side_effect": cleanup}, remux={}) as mocks:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FAIL" and result["detail"].startswith("all extracts failed")
    mocks["remux"].assert_not_called()
    assert seen["args"][-1] == frozenset()     # unclassified is NOT proven foreign: the track stays
    assert "content-gate SKIP embedded track 2" in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["oversize", "binary"])
def test_an_unreadable_embedded_track_does_not_stop_a_good_sidecar(tmp_path, kind):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_text(ENGLISH_TEXT)
    ctx = _patched_full_path(media, [(sidecar, False, False)],
                             streams=[_VIDEO, _AUDIO, _sub(2)], extract=_unreadable_extract(kind))
    with ctx:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert ctx.muxed_bytes == [ENGLISH_TEXT.encode()]     # the sidecar, not the unreadable track


@pytest.mark.parametrize("kind", ["oversize", "binary"])
def test_a_subliminal_download_that_cannot_be_read_is_skipped_and_the_file_is_fail(
        tmp_path, kind, capsys):
    media = _media(tmp_path)

    with _full_path(_IMAGE_ONLY, remux={},
                    fetch_via_subliminal={"side_effect": _fetch_writing(lambda out: _unclassifiable(kind, out))}
                    ) as mocks:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FAIL"
    assert "subliminal download unreadable" in result["detail"]
    mocks["remux"].assert_not_called()                  # never muxed unclassified
    assert not cs.NEEDS_ENGLISH_STATE_FILE.exists()  # a tooling failure, not NEEDS_BAZARR
    assert "cannot read the subliminal download" in capsys.readouterr().out


# --- what passed the gate flows on as the decoded text, written as UTF-8 into its own `_workfile`, never the original bytes ---

_ACCENTED = ENGLISH_TEXT.replace("detective", "caf\u00e9 detective")
_ENCODINGS = {
    "utf-16": ENGLISH_TEXT.encode("utf-16"),                  # BOM
    "utf-16-le-no-bom": ENGLISH_TEXT.encode("utf-16-le"),
    "utf-16-be-no-bom": ENGLISH_TEXT.encode("utf-16-be"),
    "utf-8-sig": ENGLISH_TEXT.encode("utf-8-sig"),
    "cp1252": _ACCENTED.encode("cp1252"),
}


def _assert_clean_utf8(muxed: bytes, expected_text: str) -> None:
    assert b"\x00" not in muxed
    assert not muxed.startswith(b"\xef\xbb\xbf") and not muxed.startswith((b"\xff\xfe", b"\xfe\xff"))
    text = muxed.decode("utf-8")                              # strict: raises on anything else
    assert "\ufffd" not in text and "\ufeff" not in text
    assert "I don't know what you want" in text
    if "caf\u00e9" in expected_text:
        assert "caf\u00e9 detective" in text


@pytest.mark.parametrize("encoding", sorted(_ENCODINGS))
def test_a_sidecar_reaches_the_mux_as_clean_utf8(tmp_path, encoding):
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en.srt"
    sidecar.write_bytes(_ENCODINGS[encoding])
    ctx = _patched_full_path(media, [(sidecar, False, False)])
    with ctx:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert _quarantined() == []                               # accepted, not rejected as foreign
    _assert_clean_utf8(ctx.muxed_bytes[0], _ACCENTED if encoding == "cp1252" else ENGLISH_TEXT)


@pytest.mark.parametrize("encoding", sorted(_ENCODINGS))
def test_an_extracted_track_reaches_the_mux_as_clean_utf8(tmp_path, encoding):
    media = _media(tmp_path)
    ctx = _patched_full_path(media, [], streams=[_VIDEO, _AUDIO, _sub(2)],
                             extract=_extract_writing(_ENCODINGS[encoding]))
    with ctx:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    _assert_clean_utf8(ctx.muxed_bytes[0], _ACCENTED if encoding == "cp1252" else ENGLISH_TEXT)


@pytest.mark.parametrize("encoding", sorted(_ENCODINGS))
def test_a_subliminal_download_reaches_the_mux_as_clean_utf8(tmp_path, encoding):
    """A UTF-16 download must be cleaned, synced and muxed from its decoded text, not its raw bytes."""
    media = _media(tmp_path)
    ctx = _patched_full_path(media, [], streams=_IMAGE_ONLY["streams"])
    with ctx, patch.object(cs, "fetch_via_subliminal", side_effect=_fetch_writing(_ENCODINGS[encoding])):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    _assert_clean_utf8(ctx.muxed_bytes[0], _ACCENTED if encoding == "cp1252" else ENGLISH_TEXT)


def test_a_utf16_download_is_clean_utf8_when_ffsubsync_and_the_cleaner_see_it(tmp_path):
    """The file ffsubsync is handed is the same clean UTF-8 working copy, not the raw bytes."""
    media = _media(tmp_path)
    synced_inputs = []

    def _sync(video, srt_in, srt_out):
        synced_inputs.append(Path(srt_in).read_bytes())
        return False

    ctx = _patched_full_path(media, [], streams=_IMAGE_ONLY["streams"])
    with ctx, patch.object(cs, "fetch_via_subliminal",
                           side_effect=_fetch_writing(ENGLISH_TEXT.encode("utf-16"))), \
         patch.object(cs, "run_ffsubsync", side_effect=_sync):
        assert cs._process_locked(media, str(media), dry_run=False)["status"] == "FIXED"

    assert len(synced_inputs) == 1
    _assert_clean_utf8(synced_inputs[0], ENGLISH_TEXT)


def test_a_utf16_embedded_forced_track_reaches_the_mux_as_clean_utf8(tmp_path):
    media = _media(tmp_path)
    english = tmp_path / f"{media.stem}.en.srt"
    english.write_text(ENGLISH_TEXT)

    ctx = _patched_full_path(
        media, [(english, False, False)],
        streams=[_VIDEO, _AUDIO, _eng_track(2, title="Forced", forced=True)],
        extract=_extract_writing(SHORT_FORCED_ENGLISH.encode("utf-16")))
    with ctx:
        assert cs._process_locked(media, str(media), dry_run=False)["status"] == "FIXED"

    forced = ctx.forced_bytes[0]
    assert b"\x00" not in forced and b"Where is the station?" in forced
    assert "\ufffd" not in forced.decode("utf-8")


def test_a_utf16_forced_sidecar_embedded_as_the_forced_track_is_clean_utf8(tmp_path):
    media = _media(tmp_path)
    english = tmp_path / f"{media.stem}.en.srt"
    forced_sidecar = tmp_path / f"{media.stem}.en.forced.srt"
    english.write_text(ENGLISH_TEXT)
    forced_sidecar.write_bytes(SHORT_FORCED_ENGLISH.encode("utf-16"))
    ctx = _patched_full_path(media, [(english, False, False), (forced_sidecar, True, False)])
    with ctx:
        assert cs._process_locked(media, str(media), dry_run=False)["status"] == "FIXED"

    forced = ctx.forced_bytes[0]
    assert b"\x00" not in forced and b"Where is the station?" in forced
    forced.decode("utf-8")


def test_the_full_path_never_copies_a_sidecars_original_bytes():
    """Source audit: every `_sidecar_as_srt` call gets the decoded text (`data`); without it the file is copied as-is."""
    import ast
    tree = ast.parse((PROJECT_ROOT / "consolidate-subs.py").read_text())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_sidecar_as_srt"]
    assert len(calls) >= 3
    for call in calls:
        assert len(call.args) >= 3 or any(k.arg == "data" for k in call.keywords), ast.unparse(call)


@pytest.mark.parametrize("is_forced", [False, True])
def test_the_late_sweep_leaves_a_sidecar_that_is_not_text_unclassified(tmp_path, is_forced, capsys):
    """Not decodable is not "foreign" (a garbage decode would score as an unknown language): the sweep moves nothing it could not classify."""
    media = _media(tmp_path)
    sidecar = tmp_path / f"{media.stem}.en{'.forced' if is_forced else ''}.srt"
    sidecar.write_bytes(_BINARY)

    with patch.object(cs, "find_sidecar_subs", return_value=[(sidecar, is_forced, False)]):
        counts = cs._quarantine_foreign_sidecars(media, dry_run=False, info=_probe_with(_eng_track()))

    assert counts == (0, 0) and sidecar.read_bytes() == _BINARY and _quarantined() == []
    assert "not decodable as text; left unclassified" in capsys.readouterr().out


# --- an extracted ASS/SSA track is gated BEFORE conversion; only an accepted one is converted, from its decoded UTF-8 copy ---

def _ass_of(srt_text: str) -> str:
    """The dialogue of an SRT as a small ASS file (header, style, events)."""
    body = [ln for ln in srt_text.splitlines() if ln and not ln.strip().isdigit() and "-->" not in ln]
    head = ("[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize\nStyle: Default,Arial,20\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    return head + "".join(
        f"Dialogue: 0,0:00:{i:02d}.00,0:00:{i:02d}.90,Default,,0,0,0,,{ln}\n"
        for i, ln in enumerate(body, 1))


class _FakeAssToSrt:
    """`ass_to_srt` stand-in: records the ASS bytes it got and, unless `ok=False`, writes a small good SRT (so a conversion that ran would succeed)."""

    def __init__(self, output: str = ENGLISH_TEXT, ok: bool = True):
        self.output, self.ok, self.seen = output, ok, []

    def __call__(self, ass_path, srt_path):
        self.seen.append(Path(ass_path).read_bytes())
        if self.ok:
            Path(srt_path).write_text(self.output)
        return self.ok


@pytest.mark.parametrize("codec", ["ass", "ssa"])
@pytest.mark.parametrize("kind", ["oversize", "binary"])
def test_an_extracted_ass_track_that_fails_the_checks_is_never_converted(tmp_path, kind, codec, capsys):
    media = _media(tmp_path)
    fake = _FakeAssToSrt()                      # its SRT would be small and fine
    seen, cleanup = _spy_cleanup(media)

    with _full_path({"streams": [_VIDEO, _AUDIO, _sub(2, codec)]},
                    extract_track={"side_effect": _extract_writing(lambda dst: _unclassifiable(kind, dst))},
                    ass_to_srt={"side_effect": fake}, _cleanup_no_main_english={"side_effect": cleanup},
                    remux={}) as mocks:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FAIL" and result["detail"].startswith("all extracts failed")
    assert fake.seen == []                      # never converted, so never a candidate
    mocks["remux"].assert_not_called()
    assert seen["args"][-1] == frozenset()      # unclassified is not proven foreign
    assert "content-gate SKIP embedded track 2" in capsys.readouterr().out


@pytest.mark.parametrize("codec", ["ass", "ssa"])
def test_a_foreign_extracted_ass_track_is_rejected_before_it_is_converted(tmp_path, codec):
    media = _media(tmp_path)
    fake = _FakeAssToSrt()
    seen, cleanup = _spy_cleanup(media)

    with _full_path({"streams": [_VIDEO, _AUDIO, _sub(2, codec)]},
                    extract_track={"side_effect": _extract_writing(_ass_of(DANISH_TEXT).encode())},
                    ass_to_srt={"side_effect": fake}, _cleanup_no_main_english={"side_effect": cleanup}):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "NEEDS_BAZARR"
    assert fake.seen == []
    assert seen["args"][-1] == frozenset({2})   # the Danish track is proven foreign


@pytest.mark.parametrize("codec", ["ass", "ssa"])
@pytest.mark.parametrize("encoding", ["utf-16", "utf-8-sig", "utf-8"])
def test_an_accepted_extracted_ass_track_is_converted_from_the_decoded_utf8_text(tmp_path, encoding, codec):
    media = _media(tmp_path)
    fake = _FakeAssToSrt()
    ctx = _patched_full_path(media, [], streams=[_VIDEO, _AUDIO, _sub(2, codec)],
                             extract=_extract_writing(_ass_of(ENGLISH_TEXT).encode(encoding)))
    with ctx, patch.object(cs, "ass_to_srt", side_effect=fake):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    (ass_bytes,) = fake.seen
    assert b"\x00" not in ass_bytes and not ass_bytes.startswith((b"\xef\xbb\xbf", b"\xff\xfe"))
    assert "I don't know what you want" in ass_bytes.decode("utf-8")   # strict UTF-8
    assert ctx.muxed_bytes == [ENGLISH_TEXT.encode()]                  # the converted SRT is what is muxed


def _forced_ass_ctx(media, tmp_path, content, codec="ass"):
    english = tmp_path / f"{media.stem}.en.srt"
    english.write_text(ENGLISH_TEXT)
    return _patched_full_path(
        media, [(english, False, False)],
        streams=[_VIDEO, _AUDIO, _eng_track(2, codec=codec, title="Forced", forced=True)],
        extract=_extract_writing(content))


@pytest.mark.parametrize("codec", ["ass", "ssa"])
@pytest.mark.parametrize("kind", ["oversize", "binary"])
def test_an_extracted_forced_ass_track_that_fails_the_checks_is_never_converted(
        tmp_path, kind, codec, capsys):
    media = _media(tmp_path)
    fake = _FakeAssToSrt(SHORT_FORCED_ENGLISH)
    ctx = _forced_ass_ctx(media, tmp_path, lambda dst: _unclassifiable(kind, dst), codec)
    streams = [_VIDEO, _AUDIO, _eng_track(2, codec=codec, title="Forced", forced=True)]
    out_info = _out_info(kept_codec=codec)       # the unreadable track is kept: the verify sees it
    with ctx, patch.object(cs, "ass_to_srt", side_effect=fake), \
         patch.object(cs, "probe", side_effect=lambda p: {"streams": streams} if Path(p) == media else out_info):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"           # the English sidecar is still muxed
    assert fake.seen == [] and ctx.forced_bytes == [None]
    assert ctx.kept_source_subs == [{2: {"default": False, "forced": True}}]
    assert "content-gate SKIP embedded forced track 2" in capsys.readouterr().out


@pytest.mark.parametrize("codec", ["ass", "ssa"])
def test_a_foreign_extracted_forced_ass_track_is_rejected_before_it_is_converted(tmp_path, codec):
    media = _media(tmp_path)
    fake = _FakeAssToSrt(SHORT_FORCED_ENGLISH)
    ctx = _forced_ass_ctx(media, tmp_path, _ass_of(DANISH_TEXT).encode(), codec)
    with ctx, patch.object(cs, "ass_to_srt", side_effect=fake):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert fake.seen == [] and ctx.forced_bytes == [None]


@pytest.mark.parametrize("codec", ["ass", "ssa"])
def test_an_accepted_extracted_forced_ass_track_is_converted_from_decoded_utf8(tmp_path, codec):
    media = _media(tmp_path)
    fake = _FakeAssToSrt(SHORT_FORCED_ENGLISH)
    ctx = _forced_ass_ctx(media, tmp_path, _ass_of(SHORT_FORCED_ENGLISH).encode("utf-16"), codec)
    with ctx, patch.object(cs, "ass_to_srt", side_effect=fake):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    (ass_bytes,) = fake.seen
    assert b"\x00" not in ass_bytes and "Where is the station?" in ass_bytes.decode("utf-8")
    assert b"Where is the station?" in ctx.forced_bytes[0]


# --- an unreadable forced sidecar is not "forced English": left on disk, out of every decision, counted as an extraction failure ---

_FORCED_KINDS = ["oversize", "binary", "unstable", "unreadable"]


def _forced_path(media: Path, kind: str) -> Path:
    return media.with_name(f"{media.stem}.en.forced{'.unstable' if kind == 'unstable' else ''}.srt")


def _make_forced(kind: str, path: Path) -> None:
    if kind == "unstable":
        path.write_text(SHORT_FORCED_ENGLISH)
    elif kind == "unreadable":
        path.mkdir()          # open() raises IsADirectoryError, an OSError
    else:
        _unclassifiable(kind, path)


def _run_no_main(media, entries):
    with _full_path(_NO_SUBS, entries, remux={}) as mocks:
        result = cs._process_locked(media, str(media), dry_run=False)
    mocks["remux"].assert_not_called()
    return result


@pytest.mark.parametrize("kind", _FORCED_KINDS)
def test_an_unreadable_forced_sidecar_alone_is_an_extraction_failure_not_only_forced_english(
        tmp_path, kind, _changing_while_read, capsys):
    media = _media(tmp_path)
    forced = _forced_path(media, kind)
    _make_forced(kind, forced)
    before = forced.stat()

    result = _run_no_main(media, [(forced, True, False)])
    assert forced.exists()

    assert result["status"] == "FAIL"
    assert result["detail"].startswith("all extracts failed")
    assert "1 forced sidecar(s) unreadable or not text" in result["detail"]
    assert "only forced English" not in result["detail"]
    assert forced.exists() and forced.stat().st_ino == before.st_ino      # untouched
    assert _quarantined() == [] and not cs.NEEDS_ENGLISH_STATE_FILE.exists()
    out = capsys.readouterr().out
    assert str(forced) in out and "not counted as forced English" in out   # logged, either way


def test_an_unreadable_forced_sidecar_beside_a_readable_one_keeps_only_forced_english_and_says_so(
        tmp_path):
    media = _media(tmp_path)
    good = tmp_path / f"{media.stem}.en.forced.srt"
    bad = tmp_path / f"{media.stem}.en.forced.sdh.srt"
    good.write_text(SHORT_FORCED_ENGLISH)
    bad.write_bytes(_BINARY)

    result = _run_no_main(media, [(good, True, False), (bad, True, True)])

    assert result["status"] == "FAIL"
    assert result["detail"].startswith("only forced English (1 forced sidecar(s) unreadable or not text)")
    assert good.exists() and bad.read_bytes() == _BINARY and _quarantined() == []


def test_an_unreadable_forced_sidecar_beside_a_good_main_sidecar_is_ignored(tmp_path):
    media = _media(tmp_path)
    english = tmp_path / f"{media.stem}.en.srt"
    bad = tmp_path / f"{media.stem}.en.forced.srt"
    english.write_text(ENGLISH_TEXT)
    bad.write_bytes(_BINARY)
    ctx = _patched_full_path(media, [(english, False, False), (bad, True, False)])
    with ctx:
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert ctx.forced_bytes == [None]                      # never embedded as the forced track
    assert bad.read_bytes() == _BINARY and not english.exists() and _quarantined() == []


def test_an_unreadable_forced_sidecar_turns_a_content_rejection_into_a_visible_failure(tmp_path):
    """The only main candidate is Danish (quarantined) and the forced sidecar is unreadable: FAIL naming both, not NEEDS_BAZARR."""
    media = _media(tmp_path)
    danish = tmp_path / f"{media.stem}.en.srt"
    bad = tmp_path / f"{media.stem}.en.forced.srt"
    danish.write_text(DANISH_TEXT)
    bad.write_bytes(_BINARY)

    result = _run_no_main(media, [(danish, False, False), (bad, True, False)])

    assert result["status"] == "FAIL"
    assert result["detail"].startswith("all extracts failed")
    assert "1 forced sidecar(s) unreadable or not text" in result["detail"]
    assert "1 rejected by content gate" in result["detail"]
    assert len(_quarantined()) == 1 and bad.read_bytes() == _BINARY


# --- a sidecar the gate REJECTED is out of every decision, moved or not (a failed move leaves it on disk, never embedded or muxed) ---

def test_a_rejected_forced_sidecar_that_could_not_be_quarantined_is_not_forced_english(
        tmp_path, capsys):
    media = _media(tmp_path)
    forced = tmp_path / f"{media.stem}.en.forced.srt"
    forced.write_text(DANISH_TEXT)

    with patch.object(cs, "_quarantine_sidecar", return_value=False):
        result = _run_no_main(media, [(forced, True, False)])

    assert result["status"] == "NEEDS_BAZARR" and "no English candidates" in result["detail"]
    assert "only forced English" not in result["detail"]
    assert forced.read_text() == DANISH_TEXT and _quarantined() == []        # left on disk
    assert "could not be quarantined; left on disk, not counted as forced English" \
        in capsys.readouterr().out


def test_a_rejected_forced_sidecar_that_could_not_be_quarantined_is_never_embedded(tmp_path):
    media = _media(tmp_path)
    english = tmp_path / f"{media.stem}.en.srt"
    forced = tmp_path / f"{media.stem}.en.forced.srt"
    english.write_text(ENGLISH_TEXT)
    forced.write_text(DANISH_TEXT)
    ctx = _patched_full_path(media, [(english, False, False), (forced, True, False)])
    with ctx, patch.object(cs, "_quarantine_sidecar", return_value=False):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert ctx.forced_bytes == [None]                       # not the forced track
    assert forced.read_text() == DANISH_TEXT and not english.exists()   # still there, never consumed


def test_a_rejected_main_sidecar_that_could_not_be_quarantined_is_not_a_candidate(tmp_path):
    media = _media(tmp_path)
    danish = tmp_path / f"{media.stem}.en.srt"
    danish.write_text(DANISH_TEXT)

    with patch.object(cs, "_quarantine_sidecar", return_value=False):
        result = _run_no_main(media, [(danish, False, False)])

    assert result["status"] == "NEEDS_BAZARR"
    assert result["detail"].startswith("no English candidates (rejected by content gate)")
    assert danish.read_text() == DANISH_TEXT and _quarantined() == []


def test_a_rejected_main_sidecar_that_could_not_be_quarantined_is_never_muxed(tmp_path):
    media = _media(tmp_path)
    danish = tmp_path / f"{media.stem}.en.sdh.srt"
    english = tmp_path / f"{media.stem}.en.srt"
    danish.write_text(DANISH_TEXT)
    english.write_text(ENGLISH_TEXT)
    ctx = _patched_full_path(media, [(danish, False, True), (english, False, False)])
    with ctx, patch.object(cs, "_quarantine_sidecar", return_value=False):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert ctx.muxed_bytes == [ENGLISH_TEXT.encode()]       # only the English one
    assert danish.read_text() == DANISH_TEXT and not english.exists()


# --- an accepted forced ASS/SSA track that cannot be converted is never handed to the remux; an accepted forced sidecar may take over ---

_SIDECAR_FORCED = (
    "1\n00:00:01,000 --> 00:00:02,000\n[speaking French]\n\n"
    "2\n00:00:03,000 --> 00:00:04,000\nThe sidecar forced line is here.\n"
)


@pytest.mark.parametrize("codec", ["ass", "ssa"])
def test_a_forced_track_that_cannot_be_converted_gives_way_to_an_accepted_forced_sidecar(
        tmp_path, codec, capsys):
    media = _media(tmp_path)
    english = tmp_path / f"{media.stem}.en.srt"
    forced_sidecar = tmp_path / f"{media.stem}.en.forced.srt"
    english.write_text(ENGLISH_TEXT)
    forced_sidecar.write_text(_SIDECAR_FORCED)
    fail = _FakeAssToSrt(ok=False)
    ctx = _patched_full_path(
        media, [(english, False, False), (forced_sidecar, True, False)],
        streams=[_VIDEO, _AUDIO, _eng_track(2, codec=codec, title="Forced", forced=True)],
        extract=_extract_writing(_ass_of(SHORT_FORCED_ENGLISH).encode()))
    with ctx, patch.object(cs, "ass_to_srt", side_effect=fail):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert len(fail.seen) == 1                                   # the conversion was tried
    forced = ctx.forced_bytes[0]
    assert b"The sidecar forced line is here." in forced         # the sidecar became the forced track
    assert b"[Script Info]" not in forced and b"Dialogue:" not in forced   # never the unconverted ASS
    assert not forced_sidecar.exists()                           # and, being embedded, was consumed
    assert "could not be converted to SRT; not used as the forced track" in capsys.readouterr().out


@pytest.mark.parametrize("codec", ["ass", "ssa"])
def test_a_forced_track_that_cannot_be_converted_and_no_sidecar_means_no_forced_track(
        tmp_path, codec):
    media = _media(tmp_path)
    fail = _FakeAssToSrt(ok=False)
    ctx = _forced_ass_ctx(media, tmp_path, _ass_of(SHORT_FORCED_ENGLISH).encode(), codec)
    with ctx, patch.object(cs, "ass_to_srt", side_effect=fail):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"                           # the main subtitle is unaffected
    assert len(fail.seen) == 1
    assert ctx.forced_bytes == [None]                            # no forced track at all
    assert ctx.muxed_bytes == [ENGLISH_TEXT.encode()]            # and nothing unconverted anywhere


# --- the embedded forced track is gated BEFORE the "no main English candidate" decision (a foreign `eng`-tagged one is not "only forced English") ---

_FORCED_TRACK = _eng_track(2, title="Forced", forced=True)


@pytest.mark.parametrize("codec, foreign", [("subrip", FRENCH_TEXT), ("subrip", DANISH_TEXT),
                                            ("ass", _ass_of(DANISH_TEXT)), ("ssa", _ass_of(DANISH_TEXT))],
                         ids=["french", "danish", "ass", "ssa"])
def test_a_foreign_forced_track_tagged_eng_alone_is_needs_bazarr_tracked_and_dropped(
        tmp_path, codec, foreign):
    media = _media(tmp_path)
    track = _eng_track(2, codec=codec, title="Forced", forced=True)
    result, remux_calls, _ = _run_cleanup(media, [_VIDEO, _AUDIO, track], _extract_writing(foreign))

    assert result["status"] == "NEEDS_BAZARR"
    assert result["detail"].startswith("no English candidates (rejected by content gate)")
    assert "only forced English" not in result["detail"]
    assert "dropped_subs=1" in result["detail"]
    (call,) = remux_calls
    assert call["subs"] == []                                  # the foreign "forced" track is gone
    assert str(media.resolve()) in json.loads(cs.NEEDS_ENGLISH_STATE_FILE.read_text())   # tracked


def test_a_short_english_forced_track_alone_is_still_only_forced_english(tmp_path):
    media = _media(tmp_path)
    result, remux_calls, _ = _run_cleanup(media, [_VIDEO, _AUDIO, _FORCED_TRACK],
                                          _extract_writing(SHORT_FORCED_ENGLISH))

    assert result["status"] == "FAIL"
    assert result["detail"].startswith("only forced English")
    assert "nothing to clean" in result["detail"] and remux_calls == []   # the track is kept
    assert not cs.NEEDS_ENGLISH_STATE_FILE.exists()


@pytest.mark.parametrize("how", ["extract-fails", "oversize", "binary"])
def test_an_unreadable_forced_track_alone_is_an_extraction_failure_and_is_kept(tmp_path, how):
    media = _media(tmp_path)
    result, remux_calls, _ = _run_cleanup(media, [_VIDEO, _AUDIO, _FORCED_TRACK], _unreadable_extract(how))

    assert result["status"] == "FAIL"
    assert result["detail"].startswith("all extracts failed (embedded forced track unreadable or not text)")
    assert "only forced English" not in result["detail"]
    assert remux_calls == []                       # unclassified is not proven foreign: nothing dropped
    assert not cs.NEEDS_ENGLISH_STATE_FILE.exists()


def test_a_rejected_forced_track_is_proven_foreign_for_the_cleanup_after_a_failed_main_candidate(
        tmp_path):
    """The forced track is gated up front, so later cleanup branches drop it too (Danish main sidecar quarantined, forced track French)."""
    media = _media(tmp_path)
    danish = tmp_path / f"{media.stem}.en.srt"
    danish.write_text(DANISH_TEXT)
    seen, cleanup = _spy_cleanup(media)

    with _full_path({"streams": [_VIDEO, _AUDIO, _FORCED_TRACK]}, [(danish, False, False)],
                    extract_track={"side_effect": _extract_writing(FRENCH_TEXT)},
                    _cleanup_no_main_english={"side_effect": cleanup}):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "NEEDS_BAZARR"
    assert seen["args"][-1] == frozenset({2})            # proven foreign
    assert seen["args"][7] is None                       # and not handed over as the forced_text to keep
    assert len(_quarantined()) == 1                      # (the Danish sidecar)


def test_the_forced_track_is_extracted_once_not_again_for_the_fixed_path(tmp_path):
    media = _media(tmp_path)
    english = tmp_path / f"{media.stem}.en.srt"
    english.write_text(ENGLISH_TEXT)
    calls = []

    def _extract(src, idx, codec, dst):
        calls.append(idx)
        Path(dst).write_text(SHORT_FORCED_ENGLISH)
        return True

    ctx = _patched_full_path(media, [(english, False, False)],
                             streams=[_VIDEO, _AUDIO, _FORCED_TRACK], extract=_extract)
    with ctx:
        assert cs._process_locked(media, str(media), dry_run=False)["status"] == "FIXED"

    assert calls == [2] and b"Where is the station?" in ctx.forced_bytes[0]
    assert list(media.parent.glob("consolsub_*")) == []   # no scratch directory left behind


def test_a_dry_run_extracts_nothing_so_the_forced_track_still_counts_as_before(tmp_path):
    media = _media(tmp_path)
    with _full_path({"streams": [_VIDEO, _AUDIO, _FORCED_TRACK]}, extract_track={}) as mocks:
        result = cs._process_locked(media, str(media), dry_run=True)

    mocks["extract_track"].assert_not_called()
    assert result["detail"].startswith("only forced English")


# --- every accepted forced sidecar is tried, in order ---------------------------

def _forced_sidecar_pair(media, tmp_path):
    english = tmp_path / f"{media.stem}.en.srt"
    first = tmp_path / f"{media.stem}.en.forced.ass"       # sorts first; its conversion will fail
    second = tmp_path / f"{media.stem}.en.forced.srt"
    english.write_text(ENGLISH_TEXT)
    first.write_text(_ass_of(SHORT_FORCED_ENGLISH))
    second.write_text(_SIDECAR_FORCED)
    return english, first, second


def test_the_next_accepted_forced_sidecar_is_tried_when_the_first_cannot_be_converted(tmp_path, capsys):
    media = _media(tmp_path)
    english, first, second = _forced_sidecar_pair(media, tmp_path)
    fail = _FakeAssToSrt(ok=False)
    ctx = _patched_full_path(media, [(english, False, False), (first, True, False), (second, True, False)])
    with ctx, patch.object(cs, "ass_to_srt", side_effect=fail):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert len(fail.seen) == 1                                   # the ASS one was tried first
    assert b"The sidecar forced line is here." in ctx.forced_bytes[0]   # the second was embedded
    assert first.read_text() == _ass_of(SHORT_FORCED_ENGLISH)    # the unconvertible one is untouched
    assert not second.exists()                                   # only the embedded one is consumed
    assert "could not be converted to SRT; left in place, trying the next one" in capsys.readouterr().out


def test_no_forced_track_only_when_no_accepted_forced_sidecar_converts(tmp_path):
    media = _media(tmp_path)
    english, first, second = _forced_sidecar_pair(media, tmp_path)
    second.unlink()
    third = tmp_path / f"{media.stem}.en.forced.vtt"
    third.write_text(SHORT_FORCED_ENGLISH)
    fail = _FakeAssToSrt(ok=False)
    ctx = _patched_full_path(media, [(english, False, False), (first, True, False), (third, True, False)])
    with ctx, patch.object(cs, "ass_to_srt", side_effect=fail), \
         patch.object(cs.subprocess, "run", return_value=type("R", (), {"returncode": 1})()):
        result = cs._process_locked(media, str(media), dry_run=False)

    assert result["status"] == "FIXED"
    assert ctx.forced_bytes == [None]                            # none converted: no forced track
    assert first.read_text() == _ass_of(SHORT_FORCED_ENGLISH)    # both left in place, contents unchanged
    assert third.read_text() == SHORT_FORCED_ENGLISH


# --- an UNREADABLE embedded forced track is stream-copied unchanged unless a readable forced subtitle replaces it, and verified before the swap ---

def _out_info(*, kept=True, kept_codec="subrip", kept_forced=1, kept_default=0, main_default=1):
    """What ffprobe says about the remuxed file: video, audio, the kept forced track (maybe), then the new main English subtitle."""
    streams = [dict(_VIDEO), dict(_AUDIO)]
    if kept:
        streams.append({"index": 2, "codec_type": "subtitle", "codec_name": kept_codec,
                        "tags": {"language": "eng"},
                        "disposition": {"forced": kept_forced, "default": kept_default}})
    streams.append({"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
                    "tags": {"language": "eng", "title": "English"},
                    "disposition": {"default": main_default, "forced": 0}})
    return {"streams": streams}


def _run_unreadable_forced(media, tmp_path, out_info, how="extract-fails", sidecars=None):
    english = tmp_path / f"{media.stem}.en.srt"
    english.write_text(ENGLISH_TEXT)
    streams = [_VIDEO, _AUDIO, _FORCED_TRACK]
    ctx = _patched_full_path(media, sidecars or [(english, False, False)], streams=streams,
                             extract=_unreadable_extract(how))
    with ctx, patch.object(
            cs, "probe",
            side_effect=lambda p: {"streams": streams} if Path(p) == media else out_info):
        result = cs._process_locked(media, str(media), dry_run=False)
    return result, ctx


@pytest.mark.parametrize("how", ["extract-fails", "oversize", "binary"])
def test_an_unreadable_embedded_forced_track_is_kept_unchanged_by_the_full_pass(tmp_path, how):
    media = _media(tmp_path)
    result, ctx = _run_unreadable_forced(media, tmp_path, _out_info(), how)

    assert result["status"] == "FIXED"
    assert ctx.kept_source_subs == [{2: {"default": False, "forced": True}}]   # its exact source flags
    assert ctx.muxed_bytes == [ENGLISH_TEXT.encode()]
    assert ctx.forced_bytes == [None]                    # (no NEW forced subtitle was made from it)
    assert "dropped_subs=0" in result["detail"]


@pytest.mark.parametrize("bad, why", [
    (_out_info(kept=False), "subs=2"),
    (_out_info(kept_codec="ass"), "changed codec"),
    (_out_info(kept_forced=0), "flags changed"),
    (_out_info(kept_default=1), "flags changed"),
    (_out_info(main_default=0), "default and not forced"),
])
def test_the_kept_forced_track_is_verified_before_the_swap(tmp_path, bad, why):
    media = _media(tmp_path)
    before = media.read_bytes()
    result, ctx = _run_unreadable_forced(media, tmp_path, bad)

    assert result["status"] == "FAIL" and result["detail"].startswith("forced track verify:")
    assert why in result["detail"]
    assert media.read_bytes() == before                     # the original is untouched
    assert list(tmp_path.glob("*.tmp.mkv")) == []           # and the bad output is removed


def test_an_accepted_forced_sidecar_replaces_an_unreadable_embedded_forced_track(tmp_path):
    media = _media(tmp_path)
    english = tmp_path / f"{media.stem}.en.srt"
    forced = tmp_path / f"{media.stem}.en.forced.srt"
    english.write_text(ENGLISH_TEXT)
    forced.write_text(_SIDECAR_FORCED)
    result, ctx = _run_unreadable_forced(
        media, tmp_path, _out_info(kept=False),
        sidecars=[(english, False, False), (forced, True, False)])

    assert result["status"] == "FIXED"
    assert ctx.kept_source_subs == [None]                      # nothing is stream-copied
    assert b"The sidecar forced line is here." in ctx.forced_bytes[0]


# --- "generated" is irrelevant to a forced sidecar's redundancy -------------------

def test_a_generated_embedded_forced_track_still_retires_a_late_forced_sidecar(tmp_path):
    media = _media(tmp_path)
    forced = tmp_path / f"{media.stem}.en.forced.srt"
    forced.write_text(SHORT_FORCED_ENGLISH)
    generated_forced = _eng_track(2, title="English (generated) Forced", forced=True)

    result, _ = _late_run(tmp_path, media, [(forced, True, False)], _probe_with(generated_forced))

    assert result["detail"] == "state cache; quarantined 1 redundant sidecar(s)"
    assert not forced.exists()

