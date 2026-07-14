"""Task 0 migration gate: idempotence, upgraded-DB boot, the 14 legacy rows
surfacing as needs_identity, and auto-grab skipping them with a logged reason.

All migration runs use TEMP copies. The live plex_reset_button.db is NEVER
touched — the upgraded-DB case copies the verified upgrade-test backup.
"""
import logging
import shutil
import sqlite3
from pathlib import Path

import pytest

import config
import queue_store

_UPGRADE_BACKUP = Path(
    "C:/Users/Cole/CodeStuff/_backups/PlexResetButton-20260713/"
    "plex_reset_button.upgrade-test.db"
)

# The 14 open rows that lacked an external identity on the live DB (bootstrap
# section 0), re-confirmed against the upgrade-test copy.
_EXPECTED_NEEDS_IDENTITY = {1, 2, 3, 4, 5, 6, 7, 8, 10, 42, 51, 54, 85, 94}


def _point_at(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(config, "APP_DB_PATH", str(path))


def _status_counts(path: Path) -> dict:
    con = sqlite3.connect(str(path))
    try:
        return {row[0]: row[1] for row in con.execute(
            "SELECT status, COUNT(*) FROM requests GROUP BY status")}
    finally:
        con.close()


def _needs_identity_ids(path: Path) -> set:
    con = sqlite3.connect(str(path))
    try:
        return {r[0] for r in con.execute(
            "SELECT id FROM requests WHERE status = 'needs_identity'")}
    finally:
        con.close()


# --- fresh DB ---------------------------------------------------------------

def test_fresh_db_boots_and_is_idempotent(monkeypatch, tmp_path):
    db = tmp_path / "fresh.db"
    _point_at(monkeypatch, db)

    queue_store.initialize_queue_db()
    queue_store.initialize_queue_db()  # run twice — must be a no-op the 2nd time

    r = queue_store.add_request("Some Movie", "cole", media_type="movie")
    assert r.status == "open"
    # booting again and listing everything must not raise or lose the row
    queue_store.initialize_queue_db()
    assert len(queue_store.list_requests(status="all", limit=1000)) == 1
    assert queue_store.open_request_count() == 1


# --- upgraded copy of the live DB -------------------------------------------

@pytest.fixture()
def upgraded_db(monkeypatch, tmp_path) -> Path:
    if not _UPGRADE_BACKUP.exists():
        pytest.skip(f"upgrade-test backup not present at {_UPGRADE_BACKUP}")
    db = tmp_path / "upgrade.db"
    shutil.copy(_UPGRADE_BACKUP, db)
    _point_at(monkeypatch, db)
    return db


def test_upgraded_db_surfaces_the_14_legacy_rows(upgraded_db):
    queue_store.initialize_queue_db()

    assert _needs_identity_ids(upgraded_db) == _EXPECTED_NEEDS_IDENTITY

    # done -> fulfilled
    counts = _status_counts(upgraded_db)
    assert counts.get("done", 0) == 0
    assert counts.get("fulfilled", 0) == 1
    assert counts.get("needs_identity", 0) == 14

    # the 14 are excluded from the auto-grab (status='open') query and visible
    # under the needs_identity query
    open_ids = {r.request_id for r in queue_store.list_requests(status="open", limit=1000)}
    assert not (_EXPECTED_NEEDS_IDENTITY & open_ids)
    ni = {r.request_id for r in queue_store.list_requests(
        status="needs_identity", limit=1000)}
    assert ni == _EXPECTED_NEEDS_IDENTITY


def test_upgraded_db_backfills_source_from_url_and_stays_open(upgraded_db):
    queue_store.initialize_queue_db()
    con = sqlite3.connect(str(upgraded_db))
    try:
        # tmdb movie row keeps open + gets a tmdb source
        row = con.execute(
            "SELECT status, identity_source FROM requests WHERE id = 9").fetchone()
        assert row == ("open", "tmdb")
        # tvdb tv row
        assert con.execute(
            "SELECT identity_source FROM requests WHERE id = 33").fetchone()[0] == "tvdb"
        # imdb.com url -> omdb (the source media_lookup uses for imdb ids)
        assert con.execute(
            "SELECT identity_source FROM requests WHERE id = 14").fetchone()[0] == "omdb"
        # MAL anime row
        assert con.execute(
            "SELECT identity_source FROM requests WHERE id = 80").fetchone()[0] == "jikan"
        # item 5: the ASCII search alias is stored on a qualified open row
        import json as _json
        aliases = con.execute(
            "SELECT aliases_json FROM requests WHERE id = 55").fetchone()[0]
        assert _json.loads(aliases) == ["The Angry Birds Movie"]
    finally:
        con.close()


def test_upgraded_db_does_not_repair_poisoned_rows(upgraded_db):
    # 55/86 are poisoned (open + found_in_library, sequel in library). Task 0's
    # migration must NOT repair them — it only normalises identity/state. They
    # stay open (they carry a qualified tmdb identity) with found_in_library=1.
    queue_store.initialize_queue_db()
    con = sqlite3.connect(str(upgraded_db))
    try:
        for rid in (55, 86):
            status, found, src = con.execute(
                "SELECT status, found_in_library, identity_source "
                "FROM requests WHERE id = ?", (rid,)).fetchone()
            assert status == "open"
            assert found == 1
            assert src == "tmdb"
        # 85 is identity-less -> needs_identity, found_in_library left untouched
        status85, found85 = con.execute(
            "SELECT status, found_in_library FROM requests WHERE id = 85").fetchone()
        assert status85 == "needs_identity"
        assert found85 == 1
    finally:
        con.close()


def test_upgraded_db_migration_is_idempotent(upgraded_db):
    queue_store.initialize_queue_db()
    counts_1 = _status_counts(upgraded_db)
    ni_1 = _needs_identity_ids(upgraded_db)

    queue_store.initialize_queue_db()  # second pass
    assert _status_counts(upgraded_db) == counts_1
    assert _needs_identity_ids(upgraded_db) == ni_1

    # boots and lists every request without raising
    everything = queue_store.list_requests(status="all", limit=1000)
    assert len(everything) == 94


# --- auto-grab skips needs_identity with a logged reason --------------------

def test_auto_grab_skips_needs_identity_with_logged_reason(
        upgraded_db, monkeypatch, caplog):
    import download_manager

    queue_store.initialize_queue_db()

    # Keep the manager inert: no session recovery, no real searches, no grabs.
    monkeypatch.setattr(
        download_manager.DownloadManager, "_recover_previous_session",
        lambda self: None)
    monkeypatch.setattr(download_manager, "search_torrents", lambda *a, **k: [])
    # Phase 3 routes every automatic path through search_collect (not
    # search_torrents above) — the upgraded-DB copy carries real open
    # requests with identities, so without this auto_grab_open_requests
    # performs a real YTS/TPB/nyaa search per row.
    from torrent_search import CollectedPool
    monkeypatch.setattr(
        download_manager, "search_collect",
        lambda *a, **k: CollectedPool(results=tuple(), pool_stats={}))
    # And the movie-runtime lookup (real TMDB call) for each open movie row.
    monkeypatch.setattr(
        download_manager, "_request_movie_minutes", lambda *a, **k: None)
    grabbed_request_ids = []
    monkeypatch.setattr(
        download_manager.DownloadManager, "grab",
        lambda self, *a, **k: grabbed_request_ids.append(k.get("request_id")) or 0)
    monkeypatch.setattr(
        download_manager.DownloadManager, "_grab_request_seasonwise",
        lambda self, req: [])
    monkeypatch.setattr(
        download_manager.downloads_store, "request_ids_with_downloads",
        lambda: set())

    mgr = download_manager.DownloadManager()
    with caplog.at_level(logging.INFO, logger=download_manager.logger.name):
        mgr.auto_grab_open_requests()

    text = caplog.text
    assert "need an identity" in text
    # every one of the 14 legacy ids is named in the skip log
    assert all(str(i) in text for i in _EXPECTED_NEEDS_IDENTITY)
    # and none of them was grabbed
    assert not (_EXPECTED_NEEDS_IDENTITY & set(grabbed_request_ids))
