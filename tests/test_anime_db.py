"""Local anime metadata DB: build from synthetic dumps, search, mappings."""
import xml.etree.ElementTree as ET

import pytest

import anime_db


@pytest.fixture()
def built_db(tmp_path, monkeypatch):
    monkeypatch.setattr(anime_db, "_db_path", lambda: tmp_path / "anime_meta.sqlite")
    manami = {"data": [
        {
            "sources": ["https://anidb.net/anime/17478",
                        "https://myanimelist.net/anime/52211"],
            "title": "Mashle",
            "type": "TV", "episodes": 12, "status": "FINISHED",
            "animeSeason": {"season": "SPRING", "year": 2023},
            "synonyms": ["Mashle: Magic and Muscles", "マッシュル"],
            "tags": ["comedy"], "score": {"arithmeticMean": 7.5},
        },
        {
            "sources": ["https://anidb.net/anime/9999"],
            "title": "Very Adult Show", "type": "OVA", "episodes": 1,
            "status": "FINISHED", "animeSeason": {"year": 2020},
            "synonyms": [], "tags": ["hentai"], "score": None,
        },
        {
            "sources": ["https://anilist.co/anime/424242"],
            "title": "Anilist Only Show", "type": "TV", "episodes": 24,
            "status": "ONGOING", "animeSeason": {}, "synonyms": ["AOS"],
            "tags": [], "score": {"arithmeticMean": "bad-data"},
        },
    ]}
    fribb = [
        {"anidb_id": 17478, "anilist_id": 151801, "mal_id": 52211,
         "tvdb_id": 421737, "themoviedb_id": {"tv": 204832},
         "imdb_id": ["tt21209804"], "season": {"tvdb": 1}},
        {"anidb_id": 9999, "tvdb_id": 111, "themoviedb_id": {"movie": 222},
         "imdb_id": "tt0000001", "season": {"tvdb": 2}, "episode_offset": 12},
    ]
    xml_root = ET.fromstring(
        '<anime-list><anime anidbid="17478" tvdbid="421737"'
        ' defaulttvdbseason="1"/></anime-list>')
    anime_db._build_database(manami, fribb, xml_root, anime_db._db_path())
    return anime_db


def test_search_titles_and_synonyms(built_db):
    hits = built_db.search("Mashle Magic and Muscles")
    assert hits and hits[0].anidb_id == 17478
    assert hits[0].episodes == 12
    # Synonym search finds the same entry.
    assert built_db.search("マッシュル")[0].anidb_id == 17478
    # Adult flag carried through.
    adult = built_db.search("Very Adult Show")[0]
    assert adult.is_adult


def test_mappings_schema_variants(built_db):
    m = built_db.mapping_for_anidb(17478)
    assert m["tvdb_id"] == 421737 and m["tmdb_id"] == 204832
    assert m["imdb_id"] == "tt21209804"
    assert m["default_tvdb_season"] == "1" and not m["episode_offset"]
    # movie-keyed tmdb dict, list-less imdb, offset entry
    m2 = built_db.mapping_for_anidb(9999)
    assert m2["tmdb_id"] == 222 and m2["imdb_id"] == "tt0000001"
    assert m2["default_tvdb_season"] == "2" and m2["episode_offset"] == 12
    assert built_db.mapping_for_anidb(123456) is None


def test_episode_count_and_status(built_db):
    assert built_db.episode_count_for_anidb(17478) == 12
    assert "3 anime" in built_db.status()


def test_search_results_conversion(built_db, monkeypatch):
    import show_tracker
    monkeypatch.setattr("media_lookup.anidb_english_title", lambda _aid: None)
    results = show_tracker.anime_db_search_results("Mashle", "anime")
    assert results and results[0].source == "anidb" and results[0].external_id == "17478"
    # Hentai excluded from regular anime lookups, included for xanime.
    assert not any("Adult" in r.title
                   for r in show_tracker.anime_db_search_results("Very Adult Show", "anime"))
    xa = show_tracker.anime_db_search_results("Very Adult Show", "xanime")
    assert xa and xa[0].external_id == "9999"
    # Entries without an anidb id fall back to anilist identity.
    al = show_tracker.anime_db_search_results("Anilist Only Show", "anime")
    assert al and al[0].source == "anilist" and al[0].external_id == "424242"
