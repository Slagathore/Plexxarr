# =============================================================================
# tests/test_json_cache.py
# =============================================================================
# Release audit: save_json_cache() must write atomically — a temp file in the
# same directory, then os.replace() over the real path — so a crash mid-write
# can never leave a truncated/corrupt cache file for the next load to choke on.
# =============================================================================

import json_cache


def test_atomic_save_writes_valid_json_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "cache.json"
    ok = json_cache.save_json_cache(path, {"a": 1, "b": [1, 2, 3]}, version=1)
    assert ok is True
    assert path.is_file()

    # Only the target file should exist — the temp file used for the atomic
    # replace must not survive a successful save.
    leftovers = [p.name for p in tmp_path.iterdir() if p != path]
    assert leftovers == []

    loaded = json_cache.load_json_cache(path, version=1)
    assert loaded == {"a": 1, "b": [1, 2, 3]}


def test_atomic_save_does_not_clobber_existing_file_on_encode_failure(tmp_path):
    path = tmp_path / "cache.json"
    assert json_cache.save_json_cache(path, {"good": True}, version=1) is True

    class Unencodable:
        pass

    ok = json_cache.save_json_cache(path, Unencodable(), version=1)
    assert ok is False

    # A failed encode must never partially overwrite the existing cache —
    # exactly the hazard the temp-file-then-replace approach avoids.
    loaded = json_cache.load_json_cache(path, version=1)
    assert loaded == {"good": True}
    leftovers = [p.name for p in tmp_path.iterdir() if p != path]
    assert leftovers == []


def test_atomic_save_cleans_up_temp_file_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    assert json_cache.save_json_cache(path, {"good": True}, version=1) is True

    def _boom(*_a, **_k):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(json_cache.os, "replace", _boom)
    ok = json_cache.save_json_cache(path, {"new": "data"}, version=1)
    assert ok is False

    # The original file survives the failed replace, and the temp file it
    # would have replaced with is cleaned up rather than left behind.
    loaded = json_cache.load_json_cache(path, version=1)
    assert loaded == {"good": True}
    leftovers = [p.name for p in tmp_path.iterdir() if p != path]
    assert leftovers == []
