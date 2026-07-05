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
