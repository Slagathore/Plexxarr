# =============================================================================
# Failure (c) regression — the cancelled-wrong-grab re-download loop.
#
# Bootstrap section 0(c): a cancelled wrong grab is excluded from the
# has-a-download skip set (request_ids_with_downloads drops cancelled rows) and
# is never recorded as a failure, so the next auto-grab pass re-grabs the SAME
# magnet forever. The subject-scoped blocklist (Task C) closes this: the
# wrong-grab action blocks the release for the request's identity and reopens
# the request, so the next pass picks a DIFFERENT release (or defers).
#
# STAGE 1 (this file, committed first) carries the assertion under
# @pytest.mark.xfail(strict=True) so the suite stays green while PROVING the
# loop exists at HEAD. At HEAD there is no wrong-grab/block action, so the test
# falls back to a plain cancel and the second pass re-grabs the same infohash —
# the xfail fires. STAGE 2 removes the xfail marker once mark_wrong_grab +
# blocklist land and the second pass genuinely picks a different release.
# =============================================================================

import pytest

import db
import downloads_store
import queue_store
import request_intake
from download_manager import DownloadManager
from media_lookup import MediaResult
from torrent_search import CollectedPool, TorrentResult

_MB = 1024 ** 2
_GB = 1024 ** 3


def _clean_db():
    queue_store.initialize_queue_db()
    downloads_store.initialize_downloads_db()
    with db.connect() as conn:
        for table in ("requests", "downloads", "download_history",
                      "selection_runs", "candidate_decisions", "failed_grabs",
                      "grab_deferrals", "blocklist", "download_files",
                      "request_downloads"):
            try:
                conn.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        conn.commit()


@pytest.fixture(autouse=True)
def _fresh():
    _clean_db()
    yield
    _clean_db()


def _hash(seed: str) -> str:
    import hashlib
    return hashlib.sha1(seed.encode()).hexdigest()


def _res(title, seed, *, size=1400 * _MB, seeders=50, media_type="movie",
         source="tpb") -> TorrentResult:
    return TorrentResult(
        title=title, magnet=f"magnet:?xt=urn:btih:{_hash(seed)}&dn=x",
        size_bytes=size, seeders=seeders, source=source, media_type=media_type)


def _pool(results):
    return CollectedPool(results=tuple(results),
                         pool_stats={"per_source": {"tpb": len(results)},
                                     "collected": len(results),
                                     "deduped": len(results),
                                     "duplicates_removed": 0})


def _movie(title="Inception", year=2010, ext="27205"):
    return MediaResult(
        title=title, year=year, external_id=ext,
        external_url=f"https://www.themoviedb.org/movie/{ext}",
        media_type="movie", overview="", source="tmdb", origin_countries=())


def _dm(monkeypatch, results):
    dm = DownloadManager()
    monkeypatch.setattr("download_manager.search_collect",
                        lambda *a, **k: _pool(results))
    monkeypatch.setattr("download_manager._request_movie_minutes",
                        lambda *a, **k: None)
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    return dm


def test_wrong_grab_does_not_regrab_the_same_release(monkeypatch):
    """grab -> wrong-grab/cancel -> next pass must pick a DIFFERENT release."""
    req = request_intake.add_matched_request(
        "Inception", "cole", media_type="movie", match=_movie())
    # Two gate-clean releases; A out-seeds B, so deterministic selection takes A
    # on every pass unless something remembers A was wrong.
    a = _res("Inception.2010.1080p.BluRay.x264-AAA", "A", seeders=100)
    b = _res("Inception.2010.1080p.WEB.x264-BBB", "B", seeders=10)
    dm = _dm(monkeypatch, [a, b])

    first = dm.auto_grab_open_requests()
    assert len(first) == 1
    dl1 = downloads_store.get_download(first[0])
    assert dl1.magnet == a.magnet  # A was taken first

    # The user marks it a wrong pick. Post-fix this blocks A for the identity and
    # reopens the request; at HEAD there is no such action, so a plain cancel is
    # the closest primitive — and it leaves nothing to stop the re-grab.
    if hasattr(dm, "mark_wrong_grab"):
        dm.mark_wrong_grab(first[0], recycle=False)
    else:
        dm.cancel(first[0])

    second = dm.auto_grab_open_requests()
    # Either a different release is grabbed, or the request visibly defers /
    # is not re-grabbed with the same magnet.
    if second:
        dl2 = downloads_store.get_download(second[0])
        assert dl2.magnet != a.magnet, (
            "re-grabbed the identical wrong release — the loop is open")
    else:
        # Deferred / no re-grab is also acceptable (visible, not a silent loop).
        req_now = queue_store.get_request(req.request_id)
        assert req_now.status in (
            queue_store.STATUS_DEFERRED, queue_store.STATUS_NEEDS_ATTENTION)
