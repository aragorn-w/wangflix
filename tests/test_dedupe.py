"""Tests for media_stack/dedupe.py, the movie keeper-selection helpers.

Ranking precedence: not-corrupt > resolution > source tier > processed-tag > size.
"""
import pytest

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


# A combined file reduced to one (season, episode) key could be "deduped" against a
# standalone file, recycling the only copy of the other episode(s): multi-episode names give None.
@pytest.mark.parametrize("name, expected", [
    pytest.param("Rick and Morty - S09E01 - There's Something About Morty WEBDL-1080p.mkv", (9, 1),
                 id="parses-season-episode"),
    pytest.param("Show - Special Feature.mkv", None, id="none-when-unparseable"),
    # "S01E01E02" must not collapse to (1, 1): it would collide with a plain S01E01 file
    pytest.param("Show - S01E01E02 - Title WEBDL-1080p.mkv", None, id="none-multi-episode-e-form"),
    # Sonarr's default multi-episode naming (no second E)
    pytest.param("Show - S01E01-02 - Title WEBDL-1080p.mkv", None, id="none-multi-episode-bare-range"),
    # the bare-range lookaheads reject a digit or p/i suffix, so "-1080p" is not read as "-10"
    pytest.param("Show.S01E01-1080p.mkv", (1, 1), id="resolution-tag-not-mistaken-for-range"),
])
def test_episode_key(name, expected):
    assert dedupe.episode_key(name) == expected


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
