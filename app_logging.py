import logging
import threading
from collections import deque
from logging.handlers import RotatingFileHandler

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_BUFFER: deque[str] = deque(maxlen=400)
_LOG_LOCK = threading.Lock()
_LOGGING_CONFIGURED = False
# Monotonic count of every record ever buffered. The Logs tab compares THIS,
# not len(buffer): once the ring is full its length pins at maxlen forever,
# which froze the tab permanently at the first 400 lines.
_LOG_SEQ = 0

# Size-capped crash-trace file: a runaway loop must never fill the disk, and
# unlike the in-memory ring buffer this survives the process dying.
_LOG_FILE_NAME = "sensarr.log"
_LOG_FILE_MAX_BYTES = 2 * 1024 * 1024  # ~2 MB
_LOG_FILE_BACKUP_COUNT = 2


class InMemoryLogHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()

        with _LOG_LOCK:
            global _LOG_SEQ
            _LOG_SEQ += 1
            _LOG_BUFFER.append(message)


def log_sequence() -> int:
    """Total records ever logged (monotonic; survives ring-buffer wraparound)."""
    with _LOG_LOCK:
        return _LOG_SEQ


def configure_logging() -> None:
    global _LOGGING_CONFIGURED
    root_logger = logging.getLogger()
    if _LOGGING_CONFIGURED:
        return

    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter(_LOG_FORMAT)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    memory_handler = InMemoryLogHandler()
    memory_handler.setFormatter(formatter)
    root_logger.addHandler(memory_handler)

    # app_paths is imported lazily here (not at module level) — app_paths
    # itself never imports app_logging, so there's no real cycle today, but
    # every other module in the app treats app_paths as a leaf resolved at
    # point of use, and this keeps that convention. A failure to open the
    # log file (permissions, read-only install, disk full, …) must degrade
    # to the pre-existing stream+memory behavior, never crash startup.
    try:
        import app_paths
        log_path = app_paths.PATHS.data_dir / _LOG_FILE_NAME
        file_handler = RotatingFileHandler(
            str(log_path), maxBytes=_LOG_FILE_MAX_BYTES,
            backupCount=_LOG_FILE_BACKUP_COUNT, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.INFO)
        root_logger.addHandler(file_handler)
    except Exception:
        root_logger.warning(
            "Could not set up the rotating log file (%s) — continuing with "
            "the in-memory/stream logs only.", _LOG_FILE_NAME, exc_info=True)

    _LOGGING_CONFIGURED = True


def get_recent_logs() -> list[str]:
    with _LOG_LOCK:
        return list(_LOG_BUFFER)
