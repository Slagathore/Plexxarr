# =============================================================================
# tests/test_app_logging.py
# =============================================================================
# Release audit: configure_logging() must add a size-capped RotatingFileHandler
# (sensarr.log in app_paths.PATHS.data_dir) so a crash leaves a trace on disk
# instead of only the in-memory ring buffer, which dies with the process. A
# failure to open the log file must degrade gracefully, never crash startup.
# =============================================================================

import logging

import pytest

import app_logging
import app_paths


@pytest.fixture()
def _isolated_root_logger():
    """Snapshot + restore the root logger's handlers around a
    configure_logging() call. configure_logging() is a run-once-per-process
    singleton guarded by _LOGGING_CONFIGURED, so tests force it to re-run
    against a redirected app_paths.PATHS — this fixture makes sure the
    tmp_path-pointed handlers it adds never leak into whatever else shares
    this pytest process."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield root
    for h in root.handlers:
        if h not in saved_handlers:
            try:
                h.close()
            except Exception:
                pass
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def _fake_paths(tmp_path, *, data_dir=None):
    return app_paths.AppPaths(
        bundle_dir=tmp_path, install_dir=tmp_path, config_dir=tmp_path,
        data_dir=data_dir if data_dir is not None else tmp_path,
        cache_dir=tmp_path, runtime_dir=tmp_path, download_dir=tmp_path)


def test_configure_logging_writes_rotating_file(monkeypatch, tmp_path,
                                                _isolated_root_logger):
    # Force past the run-once guard regardless of whether some earlier test
    # in this process already called configure_logging() for real.
    monkeypatch.setattr(app_logging, "_LOGGING_CONFIGURED", False)
    monkeypatch.setattr(app_paths, "PATHS", _fake_paths(tmp_path))

    app_logging.configure_logging()
    logging.getLogger("test_app_logging_marker").info(
        "marker line for the rotating file handler test")

    log_path = tmp_path / app_logging._LOG_FILE_NAME
    assert log_path.is_file()
    assert "marker line for the rotating file handler test" in \
        log_path.read_text(encoding="utf-8")


def test_log_file_setup_failure_degrades_gracefully(monkeypatch, tmp_path,
                                                     _isolated_root_logger):
    """A failure to open the log file (here: data_dir is a plain file, not a
    directory, so RotatingFileHandler's open() must fail) must not crash
    configure_logging() — it degrades to the stream+memory handlers only."""
    monkeypatch.setattr(app_logging, "_LOGGING_CONFIGURED", False)
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(app_paths, "PATHS", _fake_paths(tmp_path, data_dir=blocker))

    app_logging.configure_logging()  # must not raise

    assert app_logging._LOGGING_CONFIGURED is True
    logging.getLogger("test_app_logging_marker2").info("still logs in-memory")
    assert any("still logs in-memory" in line
              for line in app_logging.get_recent_logs())
