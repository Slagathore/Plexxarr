"""Task 0 item 4: the immutable want snapshot on downloads, and the collapse of
the three historical request_title derivations into one snapshot read.
"""
import json

import download_manager
import downloads_store
import queue_store


class _FakeResult:
    """Minimal TorrentResult stand-in for the snapshot builder."""
    def __init__(self, title, media_type="movie"):
        self.title = title
        self.media_type = media_type
        self.magnet = "magnet:?xt=urn:btih:0000000000000000000000000000000000000000"
        self.source = "tpb"
        self.size_bytes = 0
        self.seeders = 1


def _make_download(**overrides):
    base = dict(
        title="whatever.torrent", magnet="magnet:?xt=urn:btih:cafe",
        source="tpb", media_type="movie", request_id=None, staging_dir="/tmp",
        planned_dest=None, planned_name=None, route_reason=None,
        auto_rename=False, auto_move=False,
    )
    base.update(overrides)
    return downloads_store.create_download(**base)


def test_build_want_snapshot_freezes_identity():
    req = queue_store.add_request(
        "angry birds", "cole", media_type="movie",
        resolved_title="The Angry Birds Movie", external_id="153518",
        external_url="https://www.themoviedb.org/movie/153518",
        identity_source="tmdb", canonical_year=2016,
        origin_countries=["US"], aliases=["The Angry Birds Movie"])

    want = download_manager._build_want_snapshot(
        _FakeResult("The.Angry.Birds.Movie.2016.1080p"), req,
        request_title="The Angry Birds Movie",
        show_id=None, season=None, episode=None, minutes=None)

    assert want["identity_source"] == "tmdb"
    assert want["external_id"] == "153518"
    assert want["canonical_title"] == "The Angry Birds Movie"
    assert want["canonical_year"] == 2016
    assert want["origin_countries"] == ["US"]
    assert want["search_alias"] == "The Angry Birds Movie"
    assert "size_pref_mb_min" in want and "size_max_rate" in want


def test_request_title_reads_snapshot_not_torrent_title():
    # The restart regression: a sequel-named torrent title must NOT re-drive
    # routing. With a snapshot present, the snapshot's alias wins.
    did = _make_download(
        title="The.Angry.Birds.Movie.2.2019.1080p.BluRay",
        want_json=json.dumps({
            "schema": 1,
            "search_alias": "The Angry Birds Movie",
            "canonical_title": "The Angry Birds Movie",
        }))
    row = downloads_store.get_download(did)
    assert download_manager._request_title_from_row(row) == "The Angry Birds Movie"


def test_request_title_falls_back_to_ascii_request_when_no_snapshot():
    # Legacy rows without a snapshot reconstruct from the request using the
    # ascii-preferring logic (never the native-script canonical).
    req = queue_store.add_request(
        "pursuit of jade text", "cole", media_type="tv", resolved_title="追撃")
    did = _make_download(media_type="tv", request_id=req.request_id)
    row = downloads_store.get_download(did)
    assert download_manager._request_title_from_row(row) == "pursuit of jade text"


def test_want_json_roundtrip_and_absence():
    did = _make_download(want_json=json.dumps({"schema": 1, "canonical_title": "X"}))
    assert downloads_store.get_want(did) == {"schema": 1, "canonical_title": "X"}

    legacy = _make_download()  # no want_json
    assert downloads_store.get_want(legacy) is None
    assert downloads_store.get_download(legacy).want_json is None
