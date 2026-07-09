"""AniDB alt-titles, AniList parsing, and the AniDB-first cascade early-exit."""
import media_lookup
import show_tracker
from media_lookup import MediaResult


def test_anidb_carries_all_synonyms_as_alt_titles(monkeypatch):
    # A synonym match must return the primary title but keep every synonym as
    # an alt so the caller's re-scoring against the primary doesn't reject it.
    monkeypatch.setattr(media_lookup, "_load_anidb_index",
                        lambda: {"kanpekiseijo": [("Long Primary Title", "42")]})
    media_lookup._anidb_titles_by_aid = {"42": ["Long Primary Title", "Kanpekiseijo", "KPS"]}

    results = media_lookup.search_anidb("Kanpekiseijo", media_type="anime")
    assert results and results[0].title == "Long Primary Title"
    assert results[0].media_type == "anime"
    assert "Kanpekiseijo" in results[0].alt_titles
    # Scoring against the alt title recovers a strong match.
    assert media_lookup.best_title_similarity("Kanpekiseijo", results[0]) >= 0.95


def test_anilist_parses_titles_and_airing(monkeypatch):
    raw = [{
        "id": 123, "idMal": 456,
        "title": {"romaji": "Shingeki no Bahamut", "english": "Rage of Bahamut", "native": "神撃"},
        "synonyms": ["SnB"], "seasonYear": 2014, "status": "FINISHED",
        "episodes": 12, "nextAiringEpisode": None,
    }]
    monkeypatch.setattr(media_lookup, "_anilist_search_raw", lambda *a, **k: raw)
    results = media_lookup.search_anilist("Shingeki no Bahamut")
    assert len(results) == 1
    r = results[0]
    assert r.source == "anilist" and r.external_id == "123"
    assert r.title == "Shingeki no Bahamut"
    assert "Rage of Bahamut" in r.alt_titles and "神撃" in r.alt_titles
    assert "mal:456" in r.overview


def test_get_anime_airing_picks_best_and_reads_schedule(monkeypatch):
    raw = [
        {"id": 1, "title": {"romaji": "One Piece Movie", "english": None, "native": None},
         "synonyms": [], "status": "FINISHED", "nextAiringEpisode": None},
        {"id": 21, "title": {"romaji": "One Piece", "english": None, "native": None},
         "synonyms": [], "status": "RELEASING",
         "nextAiringEpisode": {"airingAt": 1783865760, "episode": 1169}},
    ]
    monkeypatch.setattr(media_lookup, "_anilist_search_raw", lambda *a, **k: raw)
    nxt, status = media_lookup.get_anime_airing("One Piece")
    assert status == "Airing"
    assert nxt is not None and nxt.episode == 1169 and nxt.air_date == "2026-07-12"


def test_get_anime_airing_none_when_not_airing(monkeypatch):
    raw = [{"id": 5, "title": {"romaji": "Old Finished Show", "english": None, "native": None},
            "synonyms": [], "status": "FINISHED", "nextAiringEpisode": None}]
    monkeypatch.setattr(media_lookup, "_anilist_search_raw", lambda *a, **k: raw)
    nxt, status = media_lookup.get_anime_airing("Old Finished Show")
    assert nxt is None and status == "Ended"


def test_cascade_short_circuits_on_confident_anidb_match(monkeypatch):
    monkeypatch.setattr(show_tracker, "anime_db_search_results",
                        lambda *a, **k: [])
    # A confident AniDB hit must NOT trigger the slower live sources.
    hit = MediaResult(title="Cowboy Bebop", year=1998, external_id="1",
                      external_url="", media_type="anime", overview="", source="anidb")
    monkeypatch.setattr(show_tracker, "search_anidb", lambda *a, **k: [hit])

    def _boom(*a, **k):
        raise AssertionError("slower source called despite a confident AniDB match")

    monkeypatch.setattr(show_tracker, "search_anilist", _boom)
    monkeypatch.setattr(show_tracker, "search_jikan_anime", _boom)
    monkeypatch.setattr(show_tracker, "search_tmdb_anime", _boom)

    best, score = show_tracker._best_anime_match("Cowboy Bebop", 1998, explicit=False)
    assert best is hit and score >= show_tracker._HIGH_CONFIDENCE


def test_cascade_falls_through_when_anidb_misses(monkeypatch):
    monkeypatch.setattr(show_tracker, "anime_db_search_results",
                        lambda *a, **k: [])
    right = MediaResult(title="Some New Anime", year=2026, external_id="9",
                        external_url="", media_type="anime", overview="", source="anilist")
    monkeypatch.setattr(show_tracker, "search_anidb", lambda *a, **k: [])
    monkeypatch.setattr(show_tracker, "search_anilist", lambda *a, **k: [right])
    # Jikan/TMDB must not be needed once AniList gives a confident match.
    monkeypatch.setattr(show_tracker, "jikan_circuit_open", lambda: False)
    best, score = show_tracker._best_anime_match("Some New Anime", 2026, explicit=False)
    assert best is right


def test_cascade_skips_jikan_when_circuit_open(monkeypatch):
    monkeypatch.setattr(show_tracker, "anime_db_search_results",
                        lambda *a, **k: [])
    monkeypatch.setattr(show_tracker, "search_anidb", lambda *a, **k: [])
    monkeypatch.setattr(show_tracker, "search_anilist", lambda *a, **k: [])
    monkeypatch.setattr(show_tracker, "jikan_circuit_open", lambda: True)

    def _boom(*a, **k):
        raise AssertionError("Jikan called while its circuit breaker is open")

    monkeypatch.setattr(show_tracker, "search_jikan_anime", _boom)
    monkeypatch.setattr(show_tracker, "search_tmdb_anime", lambda *a, **k: [])
    best, _ = show_tracker._best_anime_match("Whatever", None, explicit=False)
    assert best is None
