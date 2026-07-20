# =============================================================================
# tests/test_task_j.py  — Library identity backbone (Fix-sprint Task J)
# =============================================================================
# Covers: the library_identity table + CRUD, the row lifecycle (prune with the
# index / remove_from_index), each backfill source (episode / show_folder /
# download / batch lookup), and the consumer flips (find_duplicates
# identity-first + split-on-differing-identity + unidentified marking,
# request_present_in_library and plex_api.check_item_in_library identity-join).
#
# No network: the batch-lookup phase's only outbound seam
# (media_lookup.search_tmdb_movies) is monkeypatched; conftest's socket guard
# would fail the test instantly if a real call slipped through.
# =============================================================================
from pathlib import Path

import pytest

import config
import db
import library_identity as li


def _clear() -> None:
    li.initialize_library_identity_db()
    with db.connect() as conn:
        conn.execute("DELETE FROM library_identity")
        try:
            conn.execute("DELETE FROM library_files")
        except Exception:
            pass
        conn.commit()


def _index(paths_names: list[tuple[str, str]]) -> None:
    """Seed the library_files index directly (root/search_name unused here)."""
    import library_index
    library_index.initialize_library_index_db()
    with db.connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO library_files "
            "(path, name, root_path, search_name, size_bytes, modified_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(p, n, "root", n.casefold(), 1, 0.0) for p, n in paths_names])
        conn.commit()


# ---------------------------------------------------------------------------
# Table + CRUD
# ---------------------------------------------------------------------------

def test_set_get_roundtrip():
    _clear()
    li.set_identity("/lib/Dune (2021).mkv", media_type="movie",
                    identity_source="tmdb", external_id="438631",
                    resolved_by=li.RESOLVED_DOWNLOAD,
                    canonical_title="Dune", canonical_year=2021)
    row = li.get_identity("/lib/Dune (2021).mkv")
    assert row is not None
    assert row.is_qualified
    assert row.external_id == "438631"
    assert row.canonical_year == 2021
    assert row.group_key == ("id", "movie", "tmdb", "438631", None, None)


def test_only_if_stronger_preserves_trust_order():
    _clear()
    # A live placement (download) then a weaker inherited show_folder pass.
    li.set_identity("/lib/x.mkv", media_type="movie", identity_source="tmdb",
                    external_id="1", resolved_by=li.RESOLVED_DOWNLOAD)
    wrote = li.set_identity("/lib/x.mkv", media_type="tv",
                            identity_source="tvdb", external_id="999",
                            resolved_by=li.RESOLVED_SHOW_FOLDER,
                            only_if_stronger=True)
    assert wrote is False
    assert li.get_identity("/lib/x.mkv").external_id == "1"
    # An episode-exact pass DOES upgrade it.
    wrote = li.set_identity("/lib/x.mkv", media_type="tv",
                            identity_source="tvdb", external_id="7",
                            resolved_by=li.RESOLVED_EPISODE,
                            only_if_stronger=True)
    assert wrote is True
    assert li.get_identity("/lib/x.mkv").resolved_by == li.RESOLVED_EPISODE


def test_remove_identities():
    _clear()
    li.set_identity("/lib/a.mkv", media_type="movie", identity_source="tmdb",
                    external_id="1", resolved_by=li.RESOLVED_DOWNLOAD)
    assert li.remove_identities(["/lib/a.mkv"]) == 1
    assert li.get_identity("/lib/a.mkv") is None


def test_prune_orphans_respects_index():
    _clear()
    _index([("/lib/keep.mkv", "keep.mkv")])
    li.set_identity("/lib/keep.mkv", media_type="movie", identity_source="tmdb",
                    external_id="1", resolved_by=li.RESOLVED_DOWNLOAD)
    li.set_identity("/lib/gone.mkv", media_type="movie", identity_source="tmdb",
                    external_id="2", resolved_by=li.RESOLVED_DOWNLOAD)
    pruned = li.prune_orphans()
    assert pruned == 1
    assert li.get_identity("/lib/keep.mkv") is not None
    assert li.get_identity("/lib/gone.mkv") is None


def test_remove_from_index_drops_identity(tmp_path, monkeypatch):
    """Lifecycle: dropping a file from the index removes its identity row."""
    _clear()
    import library_index
    _index([("/lib/z.mkv", "z.mkv")])
    li.set_identity("/lib/z.mkv", media_type="movie", identity_source="tmdb",
                    external_id="5", resolved_by=li.RESOLVED_DOWNLOAD)
    library_index.remove_from_index(["/lib/z.mkv"])
    assert li.get_identity("/lib/z.mkv") is None


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------

def test_identity_present_joins_index():
    _clear()
    _index([("/lib/Dune.mkv", "Dune.mkv")])
    li.set_identity("/lib/Dune.mkv", media_type="movie", identity_source="tmdb",
                    external_id="438631", resolved_by=li.RESOLVED_DOWNLOAD)
    assert li.identity_present_in_library("tmdb", "438631", season=None)
    # A resolved-but-not-indexed file is NOT present (index is truth on disk).
    li.set_identity("/lib/ghost.mkv", media_type="movie", identity_source="tmdb",
                    external_id="777", resolved_by=li.RESOLVED_DOWNLOAD)
    assert not li.identity_present_in_library("tmdb", "777", season=None)


def test_identity_present_season_scoping():
    _clear()
    _index([("/lib/S01E01.mkv", "S01E01.mkv")])
    li.set_identity("/lib/S01E01.mkv", media_type="tv", identity_source="tvdb",
                    external_id="42", season=1, episode=1,
                    resolved_by=li.RESOLVED_EPISODE)
    assert li.identity_present_in_library("tvdb", "42", season=1)
    assert not li.identity_present_in_library("tvdb", "42", season=2)
    assert li.identity_present_in_library("tvdb", "42", season_any=True)


# ---------------------------------------------------------------------------
# Backfill sources
# ---------------------------------------------------------------------------

def test_backfill_episodes_and_show_folder(tmp_path, monkeypatch):
    _clear()
    import shows_store
    show_id = shows_store.upsert_show(
        title="Sekirei", media_type="anime", source="jikan",
        external_id="513", year=2008)
    # One tracked episode with a file on disk (exact), and one OTHER file under
    # the mapped show folder (inherited).
    ep_path = str(tmp_path / "Sekirei" / "Season 1" / "Sekirei - S01E01.mkv")
    other_path = str(tmp_path / "Sekirei" / "Season 2" / "Sekirei Ep 05.mkv")
    shows_store.add_show_folder(show_id, str(tmp_path / "Sekirei"))
    shows_store.set_episode_file(show_id, 1, 1, ep_path)
    _index([(ep_path, Path(ep_path).name), (other_path, Path(other_path).name)])

    summary = li.backfill_identities(allow_network=False)
    assert summary["episodes"] >= 1
    assert summary["show_folder"] >= 1

    exact = li.get_identity(ep_path)
    assert exact.resolved_by == li.RESOLVED_EPISODE
    assert exact.season == 1 and exact.episode == 1
    assert exact.external_id == "513"

    inherited = li.get_identity(other_path)
    assert inherited.resolved_by == li.RESOLVED_SHOW_FOLDER
    assert inherited.external_id == "513"
    # Season parsed from the "Season 2" ancestor, episode from the "Ep 05" token.
    assert inherited.season == 2 and inherited.episode == 5


def test_backfill_download_movies(monkeypatch):
    _clear()
    import queue_store
    import downloads_store as ds

    class _Req:
        request_id = 1
        media_type = "movie"
        identity_source = "tmdb"
        external_id = "603"
        resolved_title = "The Matrix"
        content = "matrix"
        canonical_year = 1999

    class _DL:
        download_id = 11

    class _File:
        verification_state = "verified"
        final_path = "/lib/The Matrix (1999).mkv"
        parsed_season = None
        parsed_episode = None

    monkeypatch.setattr(queue_store, "list_requests", lambda **k: [_Req()])
    monkeypatch.setattr(ds, "downloads_for_request", lambda rid: [_DL()])
    monkeypatch.setattr(ds, "list_download_files", lambda did: [_File()])
    # Index the placed file so the end-of-backfill prune keeps its identity.
    _index([("/lib/The Matrix (1999).mkv", "The Matrix (1999).mkv")])

    summary = li.backfill_identities(allow_network=False)
    assert summary["download"] >= 1
    row = li.get_identity("/lib/The Matrix (1999).mkv")
    assert row.resolved_by == li.RESOLVED_DOWNLOAD
    assert row.external_id == "603"


def test_backfill_batch_lookup_mocked(tmp_path, monkeypatch):
    _clear()
    root = tmp_path / "movies"
    root.mkdir()
    movie = root / "Inception 2010 1080p BluRay.mkv"
    movie.write_bytes(b"0")
    monkeypatch.setattr(
        config, "MEDIA_LIBRARY_PATHS",
        [config.MediaLibraryPath(path=str(root), media_type="movie")])
    monkeypatch.setattr(config, "PLEX_LIBRARY_PATHS", [str(root)])
    _index([(str(movie), movie.name)])

    import media_lookup

    class _Match:
        title = "Inception"
        year = 2010
        external_id = "27205"
        external_url = ""
        media_type = "movie"
        source = "tmdb"
        alt_titles = ()

    calls = {"n": 0}

    def _fake_search(title, year=None, *, limit=5):
        calls["n"] += 1
        return [_Match()]

    monkeypatch.setattr(media_lookup, "search_tmdb_movies", _fake_search)

    summary = li.backfill_identities(allow_network=True, throttle=0)
    assert calls["n"] >= 1
    assert summary["batch_lookup"] >= 1
    row = li.get_identity(str(movie))
    assert row.resolved_by == li.RESOLVED_BATCH
    assert row.external_id == "27205"

    # Resumable: a second run does NOT re-query the already-resolved file.
    before = calls["n"]
    li.backfill_identities(allow_network=True, throttle=0)
    assert calls["n"] == before


# ---------------------------------------------------------------------------
# find_duplicates identity-first
# ---------------------------------------------------------------------------

def _dupe_fixture(tmp_path, monkeypatch, files):
    root = tmp_path / "movies"
    for rel in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"0" * 1024)
    monkeypatch.setattr(
        config, "MEDIA_LIBRARY_PATHS",
        [config.MediaLibraryPath(path=str(root), media_type="movie")])
    monkeypatch.setattr(config, "PLEX_LIBRARY_PATHS", [str(root)])
    return root


def test_find_duplicates_identity_first_hit(tmp_path, monkeypatch):
    """Two differently-named files that RESOLVE to the same id group as one
    identity-keyed duplicate, marked identified with provenance."""
    _clear()
    from maintenance import find_duplicates
    root = _dupe_fixture(tmp_path, monkeypatch, [
        "Blade Runner 1080p.mkv",
        "BR.Final.Cut.720p.mkv",
    ])
    for name in ("Blade Runner 1080p.mkv", "BR.Final.Cut.720p.mkv"):
        li.set_identity(str(root / name), media_type="movie",
                        identity_source="tmdb", external_id="78",
                        resolved_by=li.RESOLVED_EPISODE,
                        canonical_title="Blade Runner", canonical_year=1982)
    groups = find_duplicates()
    assert len(groups) == 1
    assert len(groups[0].candidates) == 2
    assert groups[0].unidentified is False
    assert groups[0].resolved_by == li.RESOLVED_EPISODE
    assert groups[0].identity_source == "tmdb"


def test_find_duplicates_split_on_differing_identity(tmp_path, monkeypatch):
    """Same parsed filename, DIFFERENT resolved ids (a reboot) must NOT group —
    the identity split guarantee, even when the string key would collide."""
    _clear()
    from maintenance import find_duplicates
    root = _dupe_fixture(tmp_path, monkeypatch, [
        "Goosebumps/A/Goosebumps S01E01.mkv",
        "Goosebumps/B/Goosebumps S01E01.mkv",
    ])
    li.set_identity(str(root / "Goosebumps" / "A" / "Goosebumps S01E01.mkv"),
                    media_type="tv", identity_source="tvdb", external_id="1995",
                    season=1, episode=1, resolved_by=li.RESOLVED_EPISODE,
                    canonical_title="Goosebumps", canonical_year=1995)
    li.set_identity(str(root / "Goosebumps" / "B" / "Goosebumps S01E01.mkv"),
                    media_type="tv", identity_source="tvdb", external_id="2023",
                    season=1, episode=1, resolved_by=li.RESOLVED_EPISODE,
                    canonical_title="Goosebumps", canonical_year=2023)
    # Same season/episode, DIFFERENT show id -> different identity keys -> the
    # string key would have collided, the identity split keeps them apart.
    assert find_duplicates() == []


def test_find_duplicates_string_fallback_marks_unidentified(tmp_path, monkeypatch):
    """No identity rows -> string key -> group is flagged unidentified so the
    UI can offer Resolve (existing behavior preserved for unresolved files)."""
    _clear()
    from maintenance import find_duplicates
    _dupe_fixture(tmp_path, monkeypatch, [
        "ShowX/ShowX S01E02 720p.mkv",
        "ShowX/ShowX.S01E02.1080p.mkv",
    ])
    groups = find_duplicates()
    assert len(groups) == 1
    assert groups[0].unidentified is True


def test_find_duplicates_identified_and_unidentified_do_not_merge(tmp_path, monkeypatch):
    """An identified file and a same-string unidentified file stay apart (the
    identified one keys by id, the other by string) — resolving the second is
    how they'd merge, per the Task J design."""
    _clear()
    from maintenance import find_duplicates
    root = _dupe_fixture(tmp_path, monkeypatch, [
        "Movie 1080p.mkv",
        "Movie 720p.mkv",
    ])
    li.set_identity(str(root / "Movie 1080p.mkv"), media_type="movie",
                    identity_source="tmdb", external_id="9",
                    resolved_by=li.RESOLVED_BATCH, canonical_title="Movie",
                    canonical_year=2000)
    # Only one file identified -> no group of >=2 shares a key.
    assert find_duplicates() == []


# ---------------------------------------------------------------------------
# Consumer: request_present_in_library
# ---------------------------------------------------------------------------

def test_request_present_identity_join(monkeypatch):
    _clear()
    _index([("/lib/Arrival.mkv", "Arrival.mkv")])
    li.set_identity("/lib/Arrival.mkv", media_type="movie",
                    identity_source="tmdb", external_id="329865",
                    resolved_by=li.RESOLVED_DOWNLOAD, canonical_title="Arrival",
                    canonical_year=2016)
    import maintenance

    class _Req:
        request_id = 1
        media_type = "movie"
        identity_source = "tmdb"
        external_id = "329865"
        resolved_title = "Arrival"
        content = "arrival"
        canonical_year = 2016
        season = None

    # If the identity join fires, we never reach search_library.
    def _boom(*a, **k):
        raise AssertionError("string fallback should not run when id matches")
    monkeypatch.setattr("library_index.search_library", _boom)
    assert maintenance.request_present_in_library(_Req()) is True


# ---------------------------------------------------------------------------
# Consumer: plex_api.check_item_in_library
# ---------------------------------------------------------------------------

def test_check_item_in_library_identity_join():
    _clear()
    _index([("/lib/Show/S01E01.mkv", "S01E01.mkv")])
    li.set_identity("/lib/Show/S01E01.mkv", media_type="tv",
                    identity_source="tvdb", external_id="121361", season=1,
                    episode=1, resolved_by=li.RESOLVED_EPISODE)
    import plex_api
    assert plex_api.check_item_in_library(
        "", "tv", tvdb_id="121361") is True
    assert plex_api.check_item_in_library(
        "", "tv", tvdb_id="999999") is False


# ---------------------------------------------------------------------------
# Placement hook
# ---------------------------------------------------------------------------

def test_record_placement(monkeypatch):
    _clear()
    import downloads_store as ds
    import queue_store

    class _Req:
        request_id = 3
        media_type = "movie"
        identity_source = "tmdb"
        external_id = "550"
        resolved_title = "Fight Club"
        content = "fight club"
        canonical_year = 1999

    class _Row:
        request_id = 3
        download_id = 30
        season = None
        episode = None
        show_id = None

    monkeypatch.setattr(queue_store, "get_request", lambda rid: _Req())
    monkeypatch.setattr(ds, "list_download_files", lambda did: [])
    n = li.record_placement(_Row(), ["/lib/Fight Club (1999).mkv"])
    assert n == 1
    row = li.get_identity("/lib/Fight Club (1999).mkv")
    assert row.external_id == "550"
    assert row.resolved_by == li.RESOLVED_DOWNLOAD


# ---------------------------------------------------------------------------
# Finding 1 — movie/show provider-id namespace isolation
# ---------------------------------------------------------------------------

def test_namespace_movie_query_excludes_show_row():
    """TMDB reuses integer ids across its movie and TV namespaces. A movie
    presence query for id 550 must NOT match a SHOW row that also carries 550."""
    _clear()
    _index([("/lib/s/S01E01.mkv", "S01E01.mkv")])
    li.set_identity("/lib/s/S01E01.mkv", media_type="tv", identity_source="tmdb",
                    external_id="550", season=1, episode=1,
                    resolved_by=li.RESOLVED_EPISODE)
    assert not li.identity_present_in_library("tmdb", "550", season=None,
                                              movie=True)
    assert not li.identity_present_in_library("tmdb", "550", season_any=True,
                                              movie=True)
    # the episodic query DOES match it
    assert li.identity_present_in_library("tmdb", "550", season=1, movie=False)


def test_namespace_show_query_excludes_movie_row():
    _clear()
    _index([("/lib/m.mkv", "m.mkv")])
    li.set_identity("/lib/m.mkv", media_type="movie", identity_source="tmdb",
                    external_id="550", resolved_by=li.RESOLVED_DOWNLOAD)
    assert not li.identity_present_in_library("tmdb", "550", season_any=True,
                                              movie=False)
    assert li.identity_present_in_library("tmdb", "550", season=None, movie=True)


def test_namespace_group_key_separates_movie_and_show():
    """The dupes identity key carries the namespace, so a movie and a show
    with the same source+id never share a group key."""
    _clear()
    mv = li.LibraryIdentityRow(
        path="/m.mkv", media_type="movie", identity_source="tmdb",
        external_id="550", show_id=None, season=None, episode=None,
        canonical_title="x", canonical_year=None, resolved_by="download",
        resolved_at="")
    tv = li.LibraryIdentityRow(
        path="/t.mkv", media_type="tv", identity_source="tmdb",
        external_id="550", show_id=None, season=None, episode=None,
        canonical_title="x", canonical_year=None, resolved_by="episode",
        resolved_at="")
    assert mv.group_key != tv.group_key
    assert mv.namespace == "movie" and tv.namespace == "ep"


# ---------------------------------------------------------------------------
# Finding 2 — episodic identity with no episode number stays string-keyed
# ---------------------------------------------------------------------------

def test_find_duplicates_episodic_without_episode_not_grouped(tmp_path, monkeypatch):
    """Two differently-named files under one show folder, neither with a
    parseable episode number, must NOT collapse into one identity dupe group
    (they'd all key on the same show/season/None). They fall back to string
    keys, so distinct episodes stay distinct."""
    _clear()
    from maintenance import find_duplicates
    root = _dupe_fixture(tmp_path, monkeypatch, [
        "Show/Alpha Special.mkv",
        "Show/Beta Special.mkv",
    ])
    for name in ("Alpha Special.mkv", "Beta Special.mkv"):
        li.set_identity(str(root / "Show" / name), media_type="tv",
                        identity_source="tvdb", external_id="7", season=1,
                        episode=None, resolved_by=li.RESOLVED_SHOW_FOLDER,
                        canonical_title="Show", canonical_year=2000)
    assert find_duplicates() == []


def test_find_duplicates_episodic_with_episode_still_groups(tmp_path, monkeypatch):
    """Sanity: episodic rows WITH an episode number still identity-group."""
    _clear()
    from maintenance import find_duplicates
    root = _dupe_fixture(tmp_path, monkeypatch, [
        "Show/copyA 720p.mkv",
        "Show/copyB 1080p.mkv",
    ])
    for name in ("copyA 720p.mkv", "copyB 1080p.mkv"):
        li.set_identity(str(root / "Show" / name), media_type="tv",
                        identity_source="tvdb", external_id="7", season=1,
                        episode=3, resolved_by=li.RESOLVED_EPISODE,
                        canonical_title="Show", canonical_year=2000)
    groups = find_duplicates()
    assert len(groups) == 1
    assert groups[0].unidentified is False


# ---------------------------------------------------------------------------
# Finding 3 — backfill prunes identities whose file isn't indexed
# ---------------------------------------------------------------------------

def test_backfill_prunes_stale_episode_path(monkeypatch):
    """An episode row written for a file_path that isn't in the index gets
    pruned at the end of the backfill (the episode phase doesn't validate the
    index itself)."""
    _clear()
    import shows_store
    show_id = shows_store.upsert_show(
        title="Ghost", media_type="tv", source="tvdb", external_id="900")
    # file_path points at a file NOT present in library_files.
    shows_store.set_episode_file(show_id, 1, 1, "/lib/gone/Ghost S01E01.mkv")
    _index([])  # empty index
    summary = li.backfill_identities(allow_network=False)
    assert summary["pruned"] >= 1
    assert li.get_identity("/lib/gone/Ghost S01E01.mkv") is None


# ---------------------------------------------------------------------------
# Finding 5 — batch lookup miss / low-similarity / cancel
# ---------------------------------------------------------------------------

def _one_movie_fixture(tmp_path, monkeypatch, name):
    root = tmp_path / "movies"
    root.mkdir()
    movie = root / name
    movie.write_bytes(b"0")
    monkeypatch.setattr(
        config, "MEDIA_LIBRARY_PATHS",
        [config.MediaLibraryPath(path=str(root), media_type="movie")])
    monkeypatch.setattr(config, "PLEX_LIBRARY_PATHS", [str(root)])
    _index([(str(movie), movie.name)])
    return movie


def test_backfill_batch_writes_nothing_on_miss(tmp_path, monkeypatch):
    _clear()
    movie = _one_movie_fixture(tmp_path, monkeypatch, "Obscure 1999.mkv")
    import media_lookup
    monkeypatch.setattr(media_lookup, "search_tmdb_movies",
                        lambda *a, **k: [])
    summary = li.backfill_identities(allow_network=True, throttle=0)
    assert summary["batch_lookup"] == 0
    assert li.get_identity(str(movie)) is None


def test_backfill_batch_rejects_low_similarity(tmp_path, monkeypatch):
    _clear()
    movie = _one_movie_fixture(tmp_path, monkeypatch, "Inception 2010.mkv")
    import media_lookup

    class _Bad:
        title = "Completely Unrelated Documentary"
        year = 1975
        external_id = "1"
        source = "tmdb"
        alt_titles = ()

    monkeypatch.setattr(media_lookup, "search_tmdb_movies",
                        lambda *a, **k: [_Bad()])
    summary = li.backfill_identities(allow_network=True, throttle=0)
    assert summary["batch_lookup"] == 0
    assert li.get_identity(str(movie)) is None


def test_backfill_cancel_raises():
    _clear()
    from maint_jobs import JobCancelled
    with pytest.raises(JobCancelled):
        li.backfill_identities(allow_network=False, cancel_check=lambda: True)


def test_backfill_batch_cancel_raises(tmp_path, monkeypatch):
    _clear()
    movie = _one_movie_fixture(tmp_path, monkeypatch, "A Movie 2001.mkv")
    from maint_jobs import JobCancelled
    with pytest.raises(JobCancelled):
        li._backfill_batch_lookup(
            lambda **k: None, lambda: True,
            [(str(movie), movie.name)], batch_limit=None, throttle=0)


def test_show_folder_inheritance_nulls_suffixed_episode(tmp_path):
    """Live-audit false positive: 'S07E05sp The Snowmen' (a special) inherited
    episode=5 and identity-grouped with the real S07E05. A letter-suffixed
    episode can't be represented by the integer column, so inheritance must
    leave episode unset — the row still serves presence, and find_duplicates
    keeps the pair apart via its suffix-aware string path."""
    _clear()
    import shows_store
    show_id = shows_store.upsert_show(
        title="Doctor Who", media_type="tv", source="tvdb",
        external_id="76107", year=1963)
    real = str(tmp_path / "Doctor Who" / "Season 07"
               / "Doctor Who S07E05 The Angels Take Manhattan.mp4")
    special = str(tmp_path / "Doctor Who" / "Season 07"
                  / "Doctor Who S07E05sp The Snowmen.mp4")
    shows_store.add_show_folder(show_id, str(tmp_path / "Doctor Who"))
    _index([(real, Path(real).name), (special, Path(special).name)])

    li.backfill_identities(allow_network=False)

    real_row = li.get_identity(real)
    assert real_row.episode == 5
    special_row = li.get_identity(special)
    assert special_row is not None, "presence row must still exist"
    assert special_row.episode is None, "suffixed episode must not claim E05"
    # And the two must not share an identity group key.
    assert real_row.group_key != special_row.group_key
