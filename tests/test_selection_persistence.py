"""Task B item 4 / RESOLVED DECISION 12: the normalized selection_runs +
candidate_decisions persistence. Phase 1 builds the tables + one transactional
writer + a read API; nothing writes them automatically yet, so these tests call
the writer directly to prove the round-trip.
"""
import json

import downloads_store
import torrent_select as ts
from media_identity import MediaIdentity
from torrent_select import Candidate, SelectWant, select_torrent

_MB = 1024 ** 2


def _decision():
    want = SelectWant(
        identity=MediaIdentity(media_type="movie", identity_source="tmdb",
                               external_id="1",
                               canonical_title="The Angry Birds Movie",
                               canonical_year=2016, origin_countries=("US",)),
        size_pref_mb_min=10.0, fallback_minutes=120.0)
    cands = [
        Candidate("The.Angry.Birds.Movie.2.2019.1080p.BluRay.x264-GROUP",
                  "a" * 40, 2 * 1024 ** 3, 900),
        Candidate("The.Angry.Birds.Movie.2016.1080p.BluRay.x264-AMIABLE",
                  "b" * 40, 1400 * _MB, 30),
    ]
    return select_torrent(cands, want, pool_stats={"per_source": {"tpb": 2}})


def test_record_and_read_selection_run():
    decision = _decision()
    run_id = downloads_store.record_selection_run(
        decision, request_id=42, download_id=7)
    assert run_id > 0

    run = downloads_store.get_selection_run(run_id)
    assert run is not None
    assert run.mode == ts.MODE_AUTOMATIC_SINGLE
    assert run.profile == "plexxarr-v1"
    assert run.rtn_version == "1.11.1"
    assert run.request_id == 42 and run.download_id == 7
    assert run.chosen_title == "The.Angry.Birds.Movie.2016.1080p.BluRay.x264-AMIABLE"
    assert json.loads(run.pool_stats_json)["per_source"]["tpb"] == 2

    decisions = downloads_store.list_candidate_decisions(run_id)
    # Every candidate is persisted — both the survivor and the rejected sequel.
    assert len(decisions) == 2
    by_title = {d.title: d for d in decisions}
    sequel = by_title["The.Angry.Birds.Movie.2.2019.1080p.BluRay.x264-GROUP"]
    winner = by_title["The.Angry.Birds.Movie.2016.1080p.BluRay.x264-AMIABLE"]
    assert sequel.passed is False and sequel.reason_code == "sequel_mismatch"
    assert sequel.rank_position is None      # rejected: no score row
    assert winner.passed is True and winner.reason_code == "ok"
    assert winner.rank_position == 1
    assert winner.score_total is not None
    comps = json.loads(winner.score_components_json)
    assert "rtn_quality" in comps and "size_closeness" in comps


def test_get_selection_run_for_download_returns_latest():
    decision = _decision()
    first = downloads_store.record_selection_run(decision, download_id=99)
    second = downloads_store.record_selection_run(decision, download_id=99)
    latest = downloads_store.get_selection_run_for_download(99)
    assert latest is not None
    assert latest.selection_run_id == max(first, second)


def test_writer_is_transactional_all_candidates_land_together():
    decision = _decision()
    run_id = downloads_store.record_selection_run(decision)
    # One run row and exactly len(verdicts) candidate rows — the whole decision
    # landed atomically.
    assert downloads_store.get_selection_run(run_id) is not None
    assert len(downloads_store.list_candidate_decisions(run_id)) == len(
        decision.verdicts)
