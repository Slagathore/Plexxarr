"""RTN (rank-torrent-name) integration spike — Task 0 item 8.

Proves the pinned RTN release behaves the way the selection engine (Task B) is
going to lean on it, BEFORE any of that engine is written:

  * it imports (source runs; the PyInstaller specs collect it for the EXE),
  * the committed golden corpus parses to exactly the fields we expect,
  * season / episode fields are LISTS and must be handled as such,
  * the trash / fetch / GarbageTorrent mechanism rejects CAM/TS, and
  * that rejection agrees with the existing BLOCK_CAMS CAM regex.

Naming note captured live 2026-07-13: RTN 1.11.1 does NOT export a `check_trash`
function (the name in the bootstrap). The equivalent trash contract is
`ParsedData.trash` + `check_fetch()` (returns fetchable=False) +
`RTN.rank(remove_trash=True)` (raises GarbageTorrent). This test pins that
contract so a future rename/upgrade is caught.
"""
import hashlib
import json
from importlib import metadata
from pathlib import Path

import pytest

import RTN
from RTN import RTN as RTNClass
from RTN import DefaultRanking, SettingsModel, check_fetch, parse, title_match
from RTN.exceptions import GarbageTorrent

import video_quality

_CORPUS = json.loads(
    (Path(__file__).parent / "data" / "torrent_corpus.json").read_text(encoding="utf-8")
)


def _infohash(seed: str) -> str:
    return hashlib.sha1(seed.encode()).hexdigest()


def test_pinned_version_is_the_live_confirmed_release():
    # Guards against a silent upgrade slipping past the pin in requirements.txt.
    assert metadata.version("rank-torrent-name") == "1.11.1"


def test_imports_available():
    for name in ("parse", "title_match", "RTN", "DefaultRanking", "SettingsModel"):
        assert hasattr(RTN, name), f"RTN.{name} missing"


@pytest.mark.parametrize("case", _CORPUS["cases"], ids=lambda c: c["raw"])
def test_golden_corpus_parses(case):
    p = parse(case["raw"])
    assert p.parsed_title == case["parsed_title"]
    assert p.year == case["year"]
    assert list(p.seasons) == case["seasons"]
    assert list(p.episodes) == case["episodes"]
    assert p.resolution == case["resolution"]
    assert p.country == case["country"]
    assert p.trash is case["trash"]


def test_season_and_episode_fields_are_lists():
    # The whole reason the selection gate must handle types explicitly: a
    # single-episode release still reports seasons/episodes as LISTS.
    p = parse("Married.at.First.Sight.US.S18E05.720p.WEB.h264-GROUP")
    assert isinstance(p.seasons, list) and p.seasons == [18]
    assert isinstance(p.episodes, list) and p.episodes == [5]
    pack = parse("Some.Show.Complete.S01-S03.1080p.WEB.x264")
    assert isinstance(pack.seasons, list) and pack.seasons == [1, 2, 3]
    assert isinstance(p.resolution, str)


def test_title_match_pass_and_fail():
    good = parse("The.Angry.Birds.Movie.2016.1080p.BluRay.x264-AMIABLE")
    assert title_match("The Angry Birds Movie", good.parsed_title) is True


def test_trash_fetch_and_garbage_semantics():
    settings = SettingsModel()
    ranker = RTNClass(settings=settings, ranking_model=DefaultRanking())

    cam = parse("Longlegs.2024.CAM.x264-YIFY")
    assert cam.trash is True
    fetchable, failed = check_fetch(cam, settings)
    assert fetchable is False              # CAM is not fetchable
    assert "trash_quality" in failed       # ...for the trash-quality reason

    # remove_trash=False: returns a Torrent flagged unfetchable, does not raise.
    torrent = ranker.rank(
        "Longlegs.2024.CAM.x264-YIFY", _infohash("cam"), remove_trash=False)
    assert torrent.fetch is False
    assert torrent.data.trash is True

    # remove_trash=True: raises GarbageTorrent (the reject path Task B will use).
    with pytest.raises(GarbageTorrent):
        ranker.rank(
            "Longlegs.2024.CAM.x264-YIFY", _infohash("cam2"), remove_trash=True)

    # A clean release (no site-flagged group) is fetchable and ranks fine.
    good_name = "The.Angry.Birds.Movie.2016.1080p.BluRay.x264-AMIABLE"
    ok_fetch, ok_failed = check_fetch(parse(good_name), settings)
    assert ok_fetch is True and ok_failed == []
    good_torrent = ranker.rank(good_name, _infohash("good"))
    assert good_torrent.fetch is True


def test_cam_ts_rejection_agrees_with_block_cams_regex():
    # RTN's trash flag and the existing CAM_RE gate must concur, so turning on
    # BLOCK_CAMS and turning on RTN trash removal reject the same releases.
    for case in _CORPUS["cases"]:
        parsed = parse(case["raw"])
        cam_by_regex = video_quality.is_cam_release(case["raw"])
        if case["trash"]:
            assert parsed.trash is True
            assert cam_by_regex is True, f"CAM_RE missed {case['raw']}"
        else:
            assert parsed.trash is False
            assert cam_by_regex is False, f"CAM_RE false-positived {case['raw']}"
