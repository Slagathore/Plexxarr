# =============================================================================
# tests/test_downloads_store.py
# =============================================================================
# Release audit: download_history is a forever-growing audit trail. These
# tests pin the two fixes:
#   1. prune_download_history() deletes rows older than the retention default
#      (180 days) while keeping recent ones.
#   2. delete_download() cascades to download_history — a hard-deleted
#      download must not leave orphaned history rows behind.
# =============================================================================

import datetime

import db
import downloads_store


def _clean_tables():
    downloads_store.initialize_downloads_db()
    with db.connect() as conn:
        for table in ("downloads", "download_history"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


def _insert_history(download_id: int, action: str, at: str) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO download_history (download_id, action, before_value, "
            "after_value, at) VALUES (?, ?, NULL, NULL, ?)",
            (download_id, action, at))
        conn.commit()
        return int(cur.lastrowid or 0)


def test_prune_download_history_deletes_old_rows_and_keeps_new():
    _clean_tables()
    did = downloads_store.create_download(
        title="Old Movie", magnet="magnet:?xt=urn:btih:" + "b" * 40,
        source="tpb", media_type="movie", request_id=None,
        staging_dir="/tmp", planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=False, auto_move=False)
    # create_download() already wrote one 'grabbed' history row — remove it so
    # this test only reasons about the two rows it plants itself.
    with db.connect() as conn:
        conn.execute("DELETE FROM download_history WHERE download_id = ?",
                     (did,))
        conn.commit()

    old_id = _insert_history(did, "grabbed", "2015-01-01 00:00:00")
    new_id = _insert_history(did, "downloaded",
                             datetime.datetime.now(datetime.timezone.utc)
                             .strftime("%Y-%m-%d %H:%M:%S"))

    deleted = downloads_store.prune_download_history()
    assert deleted >= 1

    remaining = {h.history_id for h in downloads_store.history_for_download(did)}
    assert old_id not in remaining, "history row older than retention must be pruned"
    assert new_id in remaining, "recent history row must survive the prune"


def test_prune_download_history_respects_custom_days_and_now():
    _clean_tables()
    did = downloads_store.create_download(
        title="Custom Window", magnet="magnet:?xt=urn:btih:" + "c" * 40,
        source="tpb", media_type="movie", request_id=None,
        staging_dir="/tmp", planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=False, auto_move=False)
    with db.connect() as conn:
        conn.execute("DELETE FROM download_history WHERE download_id = ?",
                     (did,))
        conn.commit()

    ten_days_ago = _insert_history(did, "rotated", "2026-01-01 00:00:00")
    deleted = downloads_store.prune_download_history(
        days=5, now="2026-01-10T00:00:00+00:00")
    assert deleted == 1
    remaining = {h.history_id for h in downloads_store.history_for_download(did)}
    assert ten_days_ago not in remaining


def test_delete_download_cascades_to_history():
    _clean_tables()
    did = downloads_store.create_download(
        title="Delete Me", magnet="magnet:?xt=urn:btih:" + "d" * 40,
        source="tpb", media_type="movie", request_id=None,
        staging_dir="/tmp", planned_dest=None, planned_name=None,
        route_reason=None, auto_rename=False, auto_move=False)
    downloads_store.add_history(did, "downloaded", before=None, after="ok")
    assert downloads_store.history_for_download(did)  # sanity: rows exist

    downloads_store.delete_download(did)

    assert downloads_store.get_download(did) is None
    assert downloads_store.history_for_download(did) == []
    with db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM download_history WHERE download_id = ?",
            (did,)).fetchone()
    assert row[0] == 0
