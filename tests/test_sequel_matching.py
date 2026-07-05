"""The '<movie> 2 reported as already-in-library because <movie> exists' bug."""
import media_lookup
from media_lookup import _sequel_mismatch, _sequel_signature, check_library_for_title


def test_signature_extracts_digits():
    assert _sequel_signature("some movie 2") == frozenset({2})
    assert _sequel_signature("some movie") == frozenset()


def test_signature_part_words_and_romans():
    assert _sequel_signature("Dune Part Two") == frozenset({2})
    assert _sequel_signature("Rocky III") == frozenset({3})
    assert _sequel_signature("Back to the Future Part II") == frozenset({2})


def test_signature_ignores_years():
    assert _sequel_signature("Blade Runner 2049") == frozenset()
    assert _sequel_signature("1917") == frozenset()


def test_mismatch_detection():
    assert _sequel_mismatch("dune part two", "dune")
    assert _sequel_mismatch("the cleaning lady 2", "the cleaning lady")
    assert not _sequel_mismatch("dune part two", "dune part 2")  # same number
    assert not _sequel_mismatch("severance", "severance")


def test_different_sequel_numbers_never_match():
    # 3 vs 4 vs 10 — all pairwise distinct signatures.
    assert _sequel_mismatch("john wick 3", "john wick 4")
    assert _sequel_mismatch("fast 10", "fast 4")
    assert _sequel_signature("movie 10") == frozenset({10})
    # Roman numeral X = 10 matches digit 10.
    assert not _sequel_mismatch("fast x", "fast 10")


def test_release_group_junk_does_not_pollute_signature():
    # Digits from audio/codec specs must not become phantom sequel numbers.
    assert _sequel_signature("movie.name.2019.ddp5.1.x265-group") == frozenset()
    assert _sequel_signature("show.2020.1080p.web-dl.dd5.1.h264") == frozenset()
    assert _sequel_signature("film 2160p hdr10+ truehd 7.1 atmos") == frozenset()
    # ...but a real sequel number BEFORE the junk still counts.
    assert _sequel_signature("john.wick.3.2019.1080p.ddp5.1") == frozenset({3})
    assert not _sequel_mismatch("john wick 3", "john.wick.3.2019.1080p.ddp5.1")
    assert _sequel_mismatch("john wick", "john.wick.3.2019.1080p.ddp5.1")


def test_junky_library_name_still_matches_plain_request():
    # Junk-only digits on the library side must not block a legit match.
    assert not _sequel_mismatch("movie name", "movie.name.2019.ddp5.1.x265-group")


def test_title_that_is_a_year_survives():
    # "1917" / "2012" are titles, not junk — portion fallback keeps them.
    assert _sequel_signature("1917") == frozenset()
    assert not _sequel_mismatch("2012", "2012")


class _Entry:
    def __init__(self, name):
        self.name = name
        self.path = f"D:/x/{name}.mkv"


def test_library_check_rejects_sequel_false_positive(monkeypatch):
    # Library has "Dune"; user asks for "Dune Part Two" — must NOT match.
    monkeypatch.setattr(
        "library_index.search_library",
        lambda q, limit=10: [_Entry("Dune")],
    )
    found, matches = check_library_for_title("Dune Part Two", "movie")
    assert not found and not matches


def test_library_check_still_matches_same_movie(monkeypatch):
    monkeypatch.setattr(
        "library_index.search_library",
        lambda q, limit=10: [_Entry("Dune Part Two")],
    )
    found, matches = check_library_for_title("Dune Part Two", "movie")
    assert found


def test_library_check_matches_typo(monkeypatch):
    # Fuzzy mode must still tolerate small typos with no sequel numbers.
    monkeypatch.setattr(
        "library_index.search_library",
        lambda q, limit=10: [_Entry("Severance")],
    )
    found, _ = check_library_for_title("Severence", "tv")
    assert found
