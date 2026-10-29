"""`consolidate-watch.sh` sidecar-arrival block (run under bash, `log`/`trigger` stubbed): sidecar -> video mapping.

Up to 3 trailing lang/forced/sdh/generated tags are peeled off the stem; a video named exactly like the
remaining stem wins, but only if `find_sidecar_subs` would give it the sidecar.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from media_stack.subtitles import find_sidecar_subs  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "consolidate-watch.sh"


def _sidecar_block() -> str:
    text = SCRIPT.read_text()
    m = re.search(r'^ +dir=\$\(dirname "\$path"\)\n.*?^ {12}fi\n', text, re.S | re.M)
    assert m, "sidecar-arrival block not found in consolidate-watch.sh"
    return m.group(0)


def _triggered(tmp_path: Path, sidecar: str, videos: list[str]) -> list[str]:
    """Names of the videos the watcher would trigger for `sidecar` arriving
    in a directory that contains `videos` (empty list = no trigger)."""
    for v in videos:
        (tmp_path / v).write_bytes(b"\x1a\x45\xdf\xa3")
    script = ('log() { :; }\n'
              'trigger() { printf "%s\\n" "$1"; }\n'
              'path="$1"; event=close_write\n' + _sidecar_block())
    r = subprocess.run(["bash", "-c", script, "_", str(tmp_path / sidecar)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return [Path(line).name for line in r.stdout.splitlines()]


def test_existing_tag_peeling_is_unchanged(tmp_path):
    for sidecar in ("Show.S01E01.en.srt", "Show.S01E01.en.cc.srt", "Show.S01E01.en.forced.srt",
                    "Show.S01E01.srt", "Show.S01E01.en-us.srt", "Show.S01E01.eng.sdh.srt",
                    "Show.S01E01.en-sdh.srt", "Show.S01E01.en.hi.srt"):
        assert _triggered(tmp_path, sidecar, ["Show.S01E01.mkv"]) == ["Show.S01E01.mkv"], sidecar


def test_a_sidecar_with_tags_but_no_english_one_triggers_nothing(tmp_path):
    """`find_sidecar_subs` ignores these for `Show.mkv` (no English tag), so the watcher must not fire."""
    for sidecar in ("Show.hi.srt", "Show.forced.srt", "Show.cc.srt", "Show.sdh.srt",
                    "Show.generated.srt", "Show.forced.generated.srt"):
        assert _triggered(tmp_path, sidecar, ["Show.mkv"]) == [], sidecar


def test_peeling_stops_after_three_tags(tmp_path):
    """Four trailing tags is one too many: the video is not reached."""
    assert _triggered(tmp_path, "Show.en.sdh.cc.hi.srt", ["Show.mkv"]) == []
    assert _triggered(tmp_path, "Show.en.sdh.cc.srt", ["Show.mkv"]) == ["Show.mkv"]


# Every case below is also checked against `find_sidecar_subs`'s own acceptance rule.
CROSS_CHECK_CASES = [
    # (sidecar, videos present, expected video or None)
    ("Show.S01E01.en.generated.srt", ["Show.S01E01.mkv"], "Show.S01E01.mkv"),
    ("Show.S01E01.en.sdh.generated.srt", ["Show.S01E01.mkv"], "Show.S01E01.mkv"),
    ("Show.en.generated.srt", ["Show.mkv"], "Show.mkv"),
    ("Show.S01E01.en.srt", ["Show.S01E01.mp4"], "Show.S01E01.mp4"),
    ("Show.S01E01.en.srt", ["Other.mkv"], None),
    ("Say.Hi.en.srt", ["Say.Hi.mp4"], "Say.Hi.mp4"),
    ("Show.Hi.srt", ["Show.mkv", "Show.Hi.mkv"], "Show.Hi.mkv"),
    ("The.Generated.Show.S01E01.en.generated.srt", ["The.Generated.Show.S01E01.mkv"],
     "The.Generated.Show.S01E01.mkv"),
    ("Show.en.generated.srt", ["Show.mkv", "Show.en.mkv"], "Show.mkv"),
    ("Show.en.generated.srt", ["Show.en.mkv"], None),
    ("Show.en.en.srt", ["Show.en.mkv"], "Show.en.mkv"),
    ("Show.en.en.srt", ["Show.en.mkv", "Show.mkv"], "Show.en.mkv"),
    ("Show.en.sdh.srt", ["Show.en.mkv", "Show.mkv"], "Show.mkv"),
    ("The.Generated.srt", ["The.Generated.mkv"], "The.Generated.mkv"),
    ("The.Generated.en.generated.srt", ["The.Generated.mkv", "The.mkv"], "The.Generated.mkv"),
    ("Show.hi.srt", ["Show.mkv"], None),
    ("Show.forced.srt", ["Show.mkv"], None),
    ("Show.generated.srt", ["Show.mkv"], None),
    ("Show.en.forced.srt", ["Show.mkv"], "Show.mkv"),
    ("Show.en.cc.generated.srt", ["Show.mkv"], "Show.mkv"),
    ("Show.S01E01.en-us.srt", ["Show.S01E01.mkv", "Show.S01E01.mp4"], "Show.S01E01.mkv"),
    ("Show.srt", ["Show.mkv", "Show.mp4"], "Show.mkv"),
    ("Show.EN.Generated.SRT", ["Show.mkv"], "Show.mkv"),
    # Regional and spelled-out English: find_sidecar_subs takes ANY tag that
    # starts with "en", so the watcher must peel and accept the same ones.
    ("Show.en-ca.srt", ["Show.mkv"], "Show.mkv"),
    ("Show.en-au.srt", ["Show.mkv"], "Show.mkv"),
    ("Show.EN-NZ.srt", ["Show.mkv"], "Show.mkv"),
    ("Show.english.srt", ["Show.mkv"], "Show.mkv"),
    ("Show.English.SDH.srt", ["Show.mkv"], "Show.mkv"),
    ("Show.en-ca.cc.srt", ["Show.mkv"], "Show.mkv"),
    ("Show.en-ca.generated.srt", ["Show.mkv"], "Show.mkv"),
    ("Show.english.forced.srt", ["Show.mkv"], "Show.mkv"),
    ("Show.en-ca.srt", ["Show.mkv", "Show.en-ca.mkv"], "Show.en-ca.mkv"),
    ("Show.en-ca.srt", ["Show.en-ca.mp4"], "Show.en-ca.mp4"),
    ("Show.en-ca.srt", ["Show.en.mkv"], None),
    ("Show.en-ca.en-au.srt", ["Show.mkv"], "Show.mkv"),
    ("Show.en-ca.en-au.srt", ["Show.en-ca.mkv", "Show.mkv"], "Show.en-ca.mkv"),
    ("Show.fr.srt", ["Show.mkv"], None),
    ("Show.fr-ca.srt", ["Show.mkv"], None),
]


@pytest.mark.parametrize("sidecar, videos, expected", CROSS_CHECK_CASES)
def test_the_watchers_choice_agrees_with_find_sidecar_subs(tmp_path, sidecar, videos, expected):
    """The triggered video must be one `find_sidecar_subs` hands this sidecar (longest stem wins),
    and nothing is triggered when none would use it."""
    (tmp_path / sidecar).write_text("x")
    chosen = _triggered(tmp_path, sidecar, videos)
    accepting = [v for v in videos
                 if (tmp_path / sidecar) in [p for p, _, _ in find_sidecar_subs(tmp_path / v)]]
    assert chosen == ([expected] if expected else [])
    if accepting:
        # longest stem first; for the same stem the watcher tries mkv before mp4
        best = max(accepting, key=lambda v: (len(Path(v).stem), v.endswith(".mkv")))
        assert chosen == [best]
    else:
        assert chosen == []
