"""plan_for_episode: deterministic routing for tracked episodes."""
import config
import show_tracker
import shows_store


def _make_show(tmp_path, folders, title="Loop Show", ext_id="tt-loop"):
    show_id = shows_store.upsert_show(
        title=title, media_type="tv", source="tvdb", external_id=ext_id,
    )
    for f in folders:
        shows_store.add_show_folder(show_id, str(f))
    return shows_store.get_show(show_id)


def test_season_target_rule_wins(tmp_path):
    show_dir = tmp_path / "Loop Show"
    (show_dir / "Season 01").mkdir(parents=True)
    target = tmp_path / "other_drive" / "Loop Show S2"
    target.mkdir(parents=True)

    show = _make_show(tmp_path, [show_dir], ext_id="tt-loop-a")
    shows_store.set_season_target(show.show_id, 2, str(target))

    plan = show_tracker.plan_for_episode(show, 2, 3)
    assert plan.confident
    assert plan.dest_dir == str(target)          # rule wins over any folder logic
    assert plan.new_filename == "Loop Show - S02E03"

    # Clearing the rule falls back to folder logic.
    shows_store.clear_season_target(show.show_id, 2)
    plan2 = show_tracker.plan_for_episode(show, 2, 3)
    assert plan2.dest_dir != str(target)


def test_routes_to_folder_already_holding_that_season(tmp_path):
    drive_a = tmp_path / "a" / "Split"
    drive_b = tmp_path / "b" / "Split"
    (drive_a / "Season 01").mkdir(parents=True)
    (drive_b / "Season 02").mkdir(parents=True)

    show = _make_show(tmp_path, [drive_a, drive_b], title="Split", ext_id="tt-loop-b")

    # S2 episode must land on drive B (which owns Season 02), not drive A.
    plan = show_tracker.plan_for_episode(show, 2, 5)
    assert plan.confident
    assert str(drive_b) in plan.dest_dir
    # A brand-new season falls back to the first mapped folder.
    plan_s3 = show_tracker.plan_for_episode(show, 3, 1)
    assert str(drive_a) in plan_s3.dest_dir
    assert plan_s3.dest_dir.endswith("Season 03")  # copies drive A's padded style


def test_filename_is_sanitized(tmp_path):
    show_dir = tmp_path / "WeirdShow"
    (show_dir / "Season 01").mkdir(parents=True)
    show_id = shows_store.upsert_show(
        title='What? A "Show": Yes', media_type="tv",
        source="tvdb", external_id="tt-loop-c",
    )
    shows_store.add_show_folder(show_id, str(show_dir))
    show = shows_store.get_show(show_id)

    plan = show_tracker.plan_for_episode(show, 1, 1)
    assert plan.new_filename is not None
    for ch in '<>:"/\\|?*':
        assert ch not in plan.new_filename


def test_no_folders_stays_in_staging(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TORRENT_DOWNLOAD_DIR", str(tmp_path / "staging"))
    show_id = shows_store.upsert_show(
        title="Folderless", media_type="tv", source="tvdb", external_id="tt-loop-d",
    )
    show = shows_store.get_show(show_id)
    plan = show_tracker.plan_for_episode(show, 1, 1)
    assert not plan.confident
    assert plan.dest_dir == str(tmp_path / "staging")


def test_grab_marker_lifecycle(tmp_path):
    show_id = shows_store.upsert_show(
        title="Marker", media_type="tv", source="tvdb", external_id="tt-loop-e",
    )
    shows_store.replace_episodes(
        show_id,
        [type("E", (), {"season": 1, "episode": 1, "title": "x", "air_date": "2020-01-01"})()],
    )
    shows_store.set_episode_grab(show_id, 1, 1, 42)
    ep = shows_store.missing_episodes(show_id)[0]
    assert ep.grab_download_id == 42
    shows_store.set_episode_grab(show_id, 1, 1, None)
    assert shows_store.missing_episodes(show_id)[0].grab_download_id is None
    # Marking the file present removes it from missing entirely.
    shows_store.set_episode_file(show_id, 1, 1, "D:/x/marker.mkv")
    assert shows_store.missing_episodes(show_id) == []
