# =============================================================================
# The torrent runner bootstraps itself.
#
# A fresh install ships download.mjs WITHOUT node_modules (they are
# platform-specific native builds, so no build can bundle them for every OS).
# Before this, the first grab died with a raw ERR_MODULE_NOT_FOUND surfaced as
# "runner exit code 1" — proven on a real packaged Linux artifact. Now the grab
# path seeds the writable runner dir and runs npm install once, and every
# failure mode says something a human can act on.
# =============================================================================

import subprocess
from pathlib import Path

import pytest

import download_manager
import downloads_store
import health


@pytest.fixture(autouse=True)
def _reset_ready_cache():
    download_manager._RUNNER_READY = None
    yield
    download_manager._RUNNER_READY = None


def _runner_dir(tmp_path: Path, *, with_script=True, with_modules=False) -> Path:
    d = tmp_path / "torrent_runner"
    d.mkdir(parents=True, exist_ok=True)
    if with_script:
        (d / "download.mjs").write_text("// runner", encoding="utf-8")
        (d / "package.json").write_text('{"name":"r"}', encoding="utf-8")
    if with_modules:
        (d / "node_modules" / "webtorrent").mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def test_missing_deps_detected(tmp_path):
    assert download_manager.runner_missing_deps(
        _runner_dir(tmp_path, with_modules=False)) is True


def test_ready_runner_not_flagged(tmp_path):
    assert download_manager.runner_missing_deps(
        _runner_dir(tmp_path, with_modules=True)) is False


def test_absent_script_counts_as_missing(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    assert download_manager.runner_missing_deps(empty) is True


# ---------------------------------------------------------------------------
# Seeding out of a read-only bundle (the packaged-install shape)
# ---------------------------------------------------------------------------

def test_seed_copies_scripts_from_the_bundle(tmp_path, monkeypatch):
    bundle = tmp_path / "app" / "_internal" / "torrent_runner"
    bundle.mkdir(parents=True)
    for name in ("download.mjs", "diag.mjs", "package.json", "package-lock.json"):
        (bundle / name).write_text("x", encoding="utf-8")
    data_runner = tmp_path / "data" / "torrent_runner"

    import dataclasses

    import app_paths
    # PATHS is a frozen dataclass — swap the whole object, not its fields.
    monkeypatch.setattr(
        app_paths, "PATHS",
        dataclasses.replace(app_paths.PATHS, install_dir=tmp_path / "app",
                            bundle_dir=tmp_path / "app"))

    out = download_manager.seed_runner_dir(data_runner)

    assert out == data_runner
    assert (data_runner / "download.mjs").is_file()
    assert (data_runner / "package.json").is_file()


# ---------------------------------------------------------------------------
# ensure_runner_ready: installs once, is honest when it can't
# ---------------------------------------------------------------------------

def test_ensure_runs_npm_install_once_then_caches(tmp_path, monkeypatch):
    d = _runner_dir(tmp_path, with_modules=False)
    monkeypatch.setattr(download_manager, "runner_install_dir", lambda: d)
    monkeypatch.setattr(download_manager.shutil, "which", lambda _n: "/usr/bin/npm")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        (d / "node_modules" / "webtorrent").mkdir(parents=True)  # npm "worked"
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(download_manager.subprocess, "run", fake_run)

    ok, why = download_manager.ensure_runner_ready()
    assert ok, why
    assert len(calls) == 1 and calls[0][1] == "install"

    # Cached: a second grab must not shell out to npm again.
    ok2, _ = download_manager.ensure_runner_ready()
    assert ok2 and len(calls) == 1


def test_ready_runner_never_calls_npm(tmp_path, monkeypatch):
    d = _runner_dir(tmp_path, with_modules=True)
    monkeypatch.setattr(download_manager, "runner_install_dir", lambda: d)

    def boom(*a, **k):
        raise AssertionError("npm must not run when the runner is already ready")

    monkeypatch.setattr(download_manager.subprocess, "run", boom)
    ok, _ = download_manager.ensure_runner_ready()
    assert ok


def test_missing_npm_gives_actionable_message(tmp_path, monkeypatch):
    d = _runner_dir(tmp_path, with_modules=False)
    monkeypatch.setattr(download_manager, "runner_install_dir", lambda: d)
    monkeypatch.setattr(download_manager.shutil, "which", lambda _n: None)

    ok, why = download_manager.ensure_runner_ready()

    assert ok is False
    assert "Node.js" in why and "npm" in why
    assert "exit code" not in why           # never the old opaque error


def test_failed_npm_reports_its_own_output(tmp_path, monkeypatch):
    d = _runner_dir(tmp_path, with_modules=False)
    monkeypatch.setattr(download_manager, "runner_install_dir", lambda: d)
    monkeypatch.setattr(download_manager.shutil, "which", lambda _n: "/usr/bin/npm")
    monkeypatch.setattr(
        download_manager.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "EACCES denied"))

    ok, why = download_manager.ensure_runner_ready()

    assert ok is False
    assert "npm install failed" in why and "EACCES" in why


def test_npm_timeout_is_reported(tmp_path, monkeypatch):
    d = _runner_dir(tmp_path, with_modules=False)
    monkeypatch.setattr(download_manager, "runner_install_dir", lambda: d)
    monkeypatch.setattr(download_manager.shutil, "which", lambda _n: "/usr/bin/npm")

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("npm", 600)

    monkeypatch.setattr(download_manager.subprocess, "run", timeout)
    ok, why = download_manager.ensure_runner_ready()
    assert ok is False and "timed out" in why


# ---------------------------------------------------------------------------
# The grab path self-heals instead of dying with "runner exit code 1"
# ---------------------------------------------------------------------------

def test_grab_errors_helpfully_when_runner_cannot_be_readied(tmp_path, monkeypatch):
    """The regression this closes: a fresh install's first download used to fail
    with an opaque exit code. Now the row explains what to do."""
    monkeypatch.setattr(
        download_manager, "ensure_runner_ready",
        lambda **k: (False, "Node.js 20+ is required for downloads but npm was "
                            "not found on PATH."))
    spawned = []
    monkeypatch.setattr(
        download_manager.subprocess, "Popen",
        lambda *a, **k: spawned.append(a) or pytest.fail("must not spawn node"))

    did = downloads_store.create_download(
        title="X", magnet="magnet:?xt=urn:btih:" + "a" * 40, source="tpb",
        media_type="movie", request_id=None, staging_dir=str(tmp_path),
        planned_dest=None, planned_name=None, route_reason=None,
        auto_rename=False, auto_move=False)

    dm = download_manager.DownloadManager()
    monkeypatch.setattr(dm, "_maybe_start_next", lambda: None)
    dm._run_download_node(did, "magnet:?x", str(tmp_path), None)

    row = downloads_store.get_download(did)
    assert row.status == "error"
    assert "Node.js" in (row.error or "")
    assert not spawned


def test_health_check_reports_the_bootstrap_path(tmp_path, monkeypatch):
    monkeypatch.setattr(download_manager, "runner_missing_deps", lambda *a: True)
    report = health.format_health_report()
    assert "Torrent runner" in report
    assert "automatically" in report or "npm install" in report
