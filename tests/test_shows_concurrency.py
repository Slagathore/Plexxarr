"""The concurrency guard: only one scan/sync/grab runs at a time."""
import threading
import time

import pytest

import show_tracker
from show_tracker import ShowsBusyError, run_exclusive


def test_second_concurrent_operation_is_rejected():
    started = threading.Event()
    release = threading.Event()
    outcomes: dict[str, object] = {}

    def slow():
        started.set()
        release.wait(timeout=5)
        return "done"

    def hold():
        outcomes["first"] = run_exclusive("first", slow)

    t = threading.Thread(target=hold)
    t.start()
    assert started.wait(timeout=5)  # first op is now holding the lock

    # A second op from THIS thread must be rejected immediately, not queued.
    with pytest.raises(ShowsBusyError):
        run_exclusive("second", lambda: "should not run")

    release.set()
    t.join(timeout=5)
    assert outcomes["first"] == "done"

    # Lock is free again afterwards.
    assert run_exclusive("third", lambda: "ok") == "ok"


def test_same_thread_reentrancy_allows_nested_calls():
    # sync_all() calls sync_show() while holding the lock — must not deadlock.
    def outer():
        return run_exclusive("inner", lambda: "inner-ran")
    assert run_exclusive("outer", outer) == "inner-ran"


def test_auto_grab_skips_when_busy(monkeypatch):
    import download_manager

    dm = download_manager.DownloadManager()
    # Pretend a scan is holding the lock on another thread.
    held = threading.Event()
    done = threading.Event()

    def holder():
        run_exclusive("scan", lambda: (held.set(), done.wait(timeout=5)))

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(timeout=5)

    # Auto-grab must bail out cleanly (empty list), not pile onto the APIs.
    assert dm.auto_grab_missing_episodes() == []

    done.set()
    t.join(timeout=5)
