"""Tests for media_stack/dedupe.py — movie keeper-selection helpers.

These cover the ranking precedence that movie-dedupe.py relies on to pick
which of two+ video files in a folder to keep: not-corrupt > resolution >
source tier > processed-tag > size.  The corrupt-demotion and the .mk3d
extension are the two cases that bit the first manual dedup pass.
"""
from media_stack import dedupe


def test_is_video_extensions():
    assert dedupe.is_video("Movie (2024) Bluray-2160p.mkv")
    assert dedupe.is_video("Movie (2001) WEBDL-2160p.mk3d")   # 3D MKV (missed first pass)
    assert dedupe.is_video("Movie (1999) WEBDL-480p.avi")
    assert not dedupe.is_video("Movie.nfo")
    assert not dedupe.is_video("poster.jpg")
    assert not dedupe.is_video(".Movie.consol.123.tmp.mkv")   # in-progress mux


def test_resolution_rank_orders_by_height():
    assert (dedupe.resolution_rank("X 2160p.mkv")
            > dedupe.resolution_rank("X 1080p.mkv")
            > dedupe.resolution_rank("X 720p.mkv")
            > dedupe.resolution_rank("X noisetag.mkv"))


def test_source_rank_remux_beats_bluray_beats_webdl():
    assert (dedupe.source_rank("X Remux-1080p.mkv")
            > dedupe.source_rank("X Bluray-1080p.mkv")
            > dedupe.source_rank("X WEBDL-1080p.mkv")
            > dedupe.source_rank("X WEBRip-1080p.mkv"))


def test_is_corrupt_threshold():
    assert dedupe.is_corrupt(25 * 1024 * 1024)         # 25MB Zootopia 2 case
    assert not dedupe.is_corrupt(2 * 1024 ** 3)        # 2GB real file


def test_choose_keeper_prefers_higher_resolution():
    vids = [
        {"name": "M Bluray-2160p.mkv", "size": 6 * 1024**3, "processed": True},
        {"name": "M Remux-1080p.mkv", "size": 28 * 1024**3, "processed": False},
    ]
    keeper, extras = dedupe.choose_keeper(vids)
    assert keeper["name"] == "M Bluray-2160p.mkv"          # 2160p beats 1080p-remux
    assert [e["name"] for e in extras] == ["M Remux-1080p.mkv"]


def test_choose_keeper_corrupt_never_wins_despite_better_name():
    vids = [
        {"name": "M Bluray-2160p.mkv", "size": 25 * 1024 * 1024, "processed": True},  # corrupt
        {"name": "M WEBDL-2160p.mkv", "size": 19 * 1024**3, "processed": False},
    ]
    keeper, extras = dedupe.choose_keeper(vids)
    assert keeper["name"] == "M WEBDL-2160p.mkv"           # real file beats 25MB "Bluray"
    assert extras[0]["name"] == "M Bluray-2160p.mkv"


def test_choose_keeper_same_res_source_prefers_processed():
    vids = [
        {"name": "M Bluray-1080p.mkv", "size": 2 * 1024**3, "processed": True},
        {"name": "M Bluray-1080p.mp4", "size": 2 * 1024**3, "processed": False},
    ]
    keeper, _ = dedupe.choose_keeper(vids)
    assert keeper["name"] == "M Bluray-1080p.mkv"          # processed .mkv wins tiebreak


def test_choose_keeper_single_video_no_extras():
    keeper, extras = dedupe.choose_keeper([{"name": "M.mkv", "size": 1, "processed": False}])
    assert keeper["name"] == "M.mkv" and extras == []


def test_choose_keeper_empty_raises():
    import pytest
    with pytest.raises(ValueError):
        dedupe.choose_keeper([])


def test_episode_key_parses_season_episode():
    name = "Rick and Morty - S09E01 - There's Something About Morty WEBDL-1080p.mkv"
    assert dedupe.episode_key(name) == (9, 1)


def test_episode_key_case_insensitive():
    assert dedupe.episode_key("Show - s01e02 - Title.mkv") == (1, 2)


def test_episode_key_none_when_unparseable():
    assert dedupe.episode_key("Show - Special Feature.mkv") is None


def test_episode_key_none_for_multi_episode_e_form():
    # codex review #1 — "S01E01E02" must NOT collapse to (1, 1); that would
    # let it collide with a plain S01E01 file and get deduped away, losing
    # the only copy of E02.
    assert dedupe.episode_key("Show - S01E01E02 - Title WEBDL-1080p.mkv") is None


def test_episode_key_none_for_multi_episode_hyphen_e_form():
    assert dedupe.episode_key("Show - S01E01-E02 - Title WEBDL-1080p.mkv") is None


def test_episode_key_single_episode_with_hyphenated_quality_still_parses():
    # A hyphen right after the episode number is common for quality tags
    # (source-resolution) and must NOT be mistaken for a second episode
    # marker — only a literal E/e right after (optionally hyphenated)
    # signals a multi-episode release.
    assert dedupe.episode_key("Show.S01E01-WEBDL.1080p.mkv") == (1, 1)


def test_group_by_episode_groups_matching_keys():
    vids = [
        {"name": "Show - S01E01 - A WEBDL-1080p.mkv", "size": 1, "processed": False},
        {"name": "Show - S01E01 - A WEBRip-1080p.mkv", "size": 1, "processed": False},
        {"name": "Show - S01E02 - B WEBDL-1080p.mkv", "size": 1, "processed": False},
    ]
    groups = dedupe.group_by_episode(vids)
    assert set(groups.keys()) == {(1, 1), (1, 2)}
    assert len(groups[(1, 1)]) == 2
    assert len(groups[(1, 2)]) == 1


def test_group_by_episode_excludes_unparseable_names():
    vids = [
        {"name": "Show - S01E01 - A WEBDL-1080p.mkv", "size": 1, "processed": False},
        {"name": "Show - Special Feature.mkv", "size": 1, "processed": False},
    ]
    groups = dedupe.group_by_episode(vids)
    assert list(groups.keys()) == [(1, 1)]
    all_names = [v["name"] for names in groups.values() for v in names]
    assert "Show - Special Feature.mkv" not in all_names
