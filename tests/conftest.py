import os
import sys
import tempfile
from pathlib import Path

# IMPORTANT: this runs at conftest IMPORT time, before pytest imports any test
# module. config.py resolves APP_DB_PATH the moment it is first imported, so
# the env var must be set here at module level — a session fixture is too late
# and the tests would write into the real application database.
_TEST_DB_DIR = tempfile.mkdtemp(prefix="prb-tests-")
os.environ["APP_DB_PATH"] = str(Path(_TEST_DB_DIR) / "test_app.db")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-not-real")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture(autouse=True)
def _no_airing_network(monkeypatch):
    """Stop show sync from hitting the network for airing/episode fallbacks.

    sync_show() resolves a TMDB id + next-episode-to-air and can fall back to
    TMDB episode lists or AniList airing — all over the network. Tests that
    care override the specific call they exercise.
    """
    try:
        import show_tracker
    except Exception:
        return
    for name in ("resolve_tmdb_tv_id", "get_tmdb_next_air", "get_anilist_next_air"):
        monkeypatch.setattr(show_tracker, name, lambda *a, **k: None, raising=False)
    monkeypatch.setattr(show_tracker, "get_tmdb_tv_episodes", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(show_tracker, "get_anilist_status", lambda *a, **k: "", raising=False)
