"""Unit tests for the pure media_identity comparator module (Task 0 item 1)."""
from dataclasses import dataclass

import media_identity as mi
import media_lookup


# --- the moved sequel logic still lives, and media_lookup still delegates ----

def test_sequel_logic_moved_and_delegated():
    assert mi.sequel_signature("some movie 2") == frozenset({2})
    assert mi.sequel_mismatch("dune part two", "dune")
    # media_lookup's aliases point at the moved functions
    assert media_lookup._sequel_signature is mi.sequel_signature
    assert media_lookup._sequel_mismatch is mi.sequel_mismatch


def test_normalize_title():
    assert mi.normalize_title("The Angry Birds Movie 2!") == "the angry birds movie 2"
    assert mi.normalize_title("  Multiple   Spaces ") == "multiple spaces"
    assert mi.normalize_title(None) == ""


def test_numeric_title_mismatch_is_directional():
    # candidate carries a number the canonical doesn't -> mismatch
    assert mi.numeric_title_mismatch("Angry Birds Movie", "Angry Birds Movie 2")
    # same number -> no mismatch
    assert not mi.numeric_title_mismatch("Angry Birds Movie 2", "Angry Birds Movie 2")
    # candidate has no extra number -> not a numeric mismatch (other gates judge)
    assert not mi.numeric_title_mismatch("Angry Birds Movie 2", "Angry Birds Movie")


def test_country_edition_mismatch_contradiction_vs_absence():
    assert mi.country_edition_mismatch(["US"], "AU")          # contradiction
    assert not mi.country_edition_mismatch(["US"], "US")      # agreement
    assert not mi.country_edition_mismatch(["US"], None)      # absence is never a mismatch
    assert not mi.country_edition_mismatch([], "AU")          # no want, no mismatch
    assert not mi.country_edition_mismatch(["US"], "USA")     # synonym agreement
    assert mi.country_edition_mismatch(["US"], ["AU", "NZ"])  # disjoint sets


def test_search_alias_prefers_ascii():
    assert mi.search_alias("Pursuit of Jade", "pursuit of jade") == "Pursuit of Jade"
    # native-script canonical is skipped in favour of the ascii raw text
    assert mi.search_alias("追撃", "Pursuit of Jade") == "Pursuit of Jade"


# --- compare_media_identity against a duck-typed parsed object ---------------

@dataclass
class _Parsed:
    parsed_title: str = ""
    year: int | None = None
    seasons: list | None = None
    episodes: list | None = None
    country: str | None = None


def test_compare_rejects_sequel():
    want = mi.MediaIdentity(media_type="movie", canonical_title="The Angry Birds Movie",
                            canonical_year=2016)
    v = mi.compare_media_identity(want, _Parsed(parsed_title="The Angry Birds Movie 2", year=2019))
    assert not v.ok and v.reason_code in ("sequel_mismatch", "numeric_title_mismatch")


def test_compare_accepts_correct_original():
    want = mi.MediaIdentity(media_type="movie", canonical_title="The Angry Birds Movie",
                            canonical_year=2016)
    v = mi.compare_media_identity(want, _Parsed(parsed_title="The Angry Birds Movie", year=2016))
    assert v.ok and v.reason_code == "ok"


def test_compare_rejects_country_contradiction():
    want = mi.MediaIdentity(media_type="tv", canonical_title="Married at First Sight",
                            origin_countries=("US",), season=18)
    v = mi.compare_media_identity(
        want, _Parsed(parsed_title="Married At First Sight", seasons=[13], country="AU"))
    assert not v.ok and v.reason_code == "country_edition_contradiction"


def test_compare_rejects_year_and_season():
    want_movie = mi.MediaIdentity(media_type="movie", canonical_title="Tremors",
                                  canonical_year=1990)
    assert mi.compare_media_identity(
        want_movie, _Parsed(parsed_title="Tremors", year=1996)).reason_code == "year_mismatch"

    want_tv = mi.MediaIdentity(media_type="tv", canonical_title="Severance", season=2)
    assert mi.compare_media_identity(
        want_tv, _Parsed(parsed_title="Severance", seasons=[3], episodes=[1])
    ).reason_code == "season_contradiction"
    # a pack containing the wanted season passes
    assert mi.compare_media_identity(
        want_tv, _Parsed(parsed_title="Severance", seasons=[1, 2, 3])).ok


def test_identity_subject_key_and_qualified():
    unq = mi.MediaIdentity(media_type="movie", canonical_title="X")
    assert not unq.is_qualified and unq.subject_key is None
    mv = mi.MediaIdentity(media_type="movie", identity_source="tmdb", external_id="153518")
    assert mv.is_qualified and mv.subject_key == "tmdb:153518"
    tv = mi.MediaIdentity(media_type="tv", identity_source="tvdb", external_id="75692", season=19)
    assert tv.subject_key == "tvdb:75692:s19"
