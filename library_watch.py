# =============================================================================
# library_watch.py  (Fix-sprint Task K — folder watcher + downtime caching)
# =============================================================================
# Plex-style pickup of manual library additions. Watches every configured
# library root recursively (watchdog); a changed subtree is processed only once
# it has been quiet AND size-stable for LIBRARY_WATCH_SETTLE_SECONDS — a
# multi-GB copy grows for minutes, and acting mid-copy would index a truncated
# file and reconcile a request against a file that isn't finished. When a batch
# settles, one scoped pipeline runs: a scoped index delta (guard computed
# against the changed subtree, never the whole index), offline identity for the
# files now present under it, and request reconciliation through the daily
# check's own close-if-satisfied rule — so an admin's manual grab auto-closes
# the matching request within minutes instead of waiting for the nightly pass.
#
# Degrades gracefully: no watchdog (a source checkout that hasn't reinstalled)
# or an unwatchable root logs once and falls back to the nightly delta — the
# feature turns itself off, nothing crashes. App-initiated placements produce
# events too; the pipeline is idempotent, so re-processing them is harmless.
#
# Module scope stays import-light (config + a guarded watchdog import) so it is
# importable under the CI pytest subset; every heavier collaborator
# (library_index, library_identity, maintenance) is imported lazily.
# =============================================================================

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable

import config

logger = logging.getLogger(__name__)

# watchdog is an optional dependency (pinned in requirements.txt / the tests.yml
# CI list, collected in the PyInstaller specs). A checkout that hasn't
# reinstalled simply runs without the real-time watcher.
try:
    from watchdog.events import FileSystemEventHandler as _HANDLER_BASE
    from watchdog.observers import Observer as _OBSERVER_CLS
    _WATCHDOG_IMPORT_ERROR: Exception | None = None
except Exception as _exc:  # pragma: no cover - exercised only where absent
    _HANDLER_BASE = object  # type: ignore[assignment,misc]
    _OBSERVER_CLS = None
    _WATCHDOG_IMPORT_ERROR = _exc

_missing_logged = False


def watchdog_available() -> bool:
    """True when the watchdog observer backend imported cleanly."""
    return _OBSERVER_CLS is not None


def _log_watchdog_missing() -> None:
    """Log the missing-watchdog notice exactly once per process."""
    global _missing_logged
    if _missing_logged:
        return
    _missing_logged = True
    logger.info(
        "Folder watcher: the 'watchdog' package isn't installed (%s), so manual "
        "library additions are picked up by the nightly delta instead of in "
        "real time. `pip install watchdog` to enable live pickup.",
        _WATCHDOG_IMPORT_ERROR)


def _safe_size(path: str) -> int | None:
    """Current size in bytes, or None for a directory / vanished / unreadable
    path (which read as 'stable' — nothing more to wait for)."""
    try:
        return os.path.getsize(path)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Debouncer (pure, no watchdog / no disk dependency when its probes are
# injected) — the piece the unit tests drive directly.
# ---------------------------------------------------------------------------

class SettleDebouncer:
    """Accumulates changed paths and reports a batch READY only when two things
    hold at once: the subtree has been quiet for `settle_seconds` (no new event),
    and every pending file is size-stable across two probes (a still-growing
    copy resets the window). clock and size_probe are injectable so the settle
    logic is testable without real sleeps or real files."""

    def __init__(self, settle_seconds: float, *,
                 clock: Callable[[], float] = time.monotonic,
                 size_probe: Callable[[str], int | None] | None = None) -> None:
        self._settle = float(settle_seconds)
        self._clock = clock
        self._size_probe = size_probe or _safe_size
        self._paths: dict[str, int | None] = {}
        self._last_event: float | None = None

    def note(self, path: str) -> None:
        """Record a filesystem event for `path`, sampling its size and (re)opening
        the quiet window."""
        self._paths[path] = self._size_probe(path)
        self._last_event = self._clock()

    def pending(self) -> set[str]:
        return set(self._paths)

    def ready(self) -> bool:
        """True when the batch is quiet AND size-stable. A file whose size moved
        since its last sample is still being written: its new size is stored and
        the quiet window is re-opened, so ready() only trips once the copy has
        genuinely finished."""
        if not self._paths or self._last_event is None:
            return False
        now = self._clock()
        if now - self._last_event < self._settle:
            return False
        stable = True
        for path in list(self._paths):
            current = self._size_probe(path)
            if current != self._paths[path]:
                self._paths[path] = current
                stable = False
        if not stable:
            self._last_event = now
            return False
        return True

    def drain(self) -> set[str]:
        """Return the settled batch and clear pending state for the next one."""
        batch = set(self._paths)
        self._paths.clear()
        self._last_event = None
        return batch


# ---------------------------------------------------------------------------
# Event handler — funnels watchdog events into a single note() callback.
# ---------------------------------------------------------------------------

class _MediaEventHandler(_HANDLER_BASE):  # type: ignore[misc,valid-type]
    def __init__(self, on_event: Callable[[str], None]) -> None:
        super().__init__()
        self._on_event = on_event

    def on_created(self, event) -> None:
        self._on_event(getattr(event, "src_path", "") or "")

    def on_modified(self, event) -> None:
        # Directory-modified events are pure noise (they fire for every child
        # change); the child file events carry the signal.
        if not getattr(event, "is_directory", False):
            self._on_event(getattr(event, "src_path", "") or "")

    def on_moved(self, event) -> None:
        self._on_event(getattr(event, "dest_path", "") or "")
        self._on_event(getattr(event, "src_path", "") or "")

    def on_deleted(self, event) -> None:
        self._on_event(getattr(event, "src_path", "") or "")


# ---------------------------------------------------------------------------
# Watcher — observer + settle poller. Calls on_batch(sorted_paths) per batch.
# ---------------------------------------------------------------------------

class LibraryWatcher:
    """Recursive watcher over the configured library roots. Filesystem events
    feed a SettleDebouncer; a background poller drains settled batches and hands
    each to on_batch. Fully degradable: no watchdog, no roots, or no watchable
    root all return False from start() (feature off) instead of raising."""

    def __init__(self, on_batch: Callable[[list[str]], None], *,
                 roots: list[str] | None = None,
                 settle_seconds: float | None = None,
                 poll_interval: float = 5.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._on_batch = on_batch
        self._roots = list(roots) if roots is not None else list(config.PLEX_LIBRARY_PATHS)
        self._settle = (float(settle_seconds) if settle_seconds is not None
                        else float(config.LIBRARY_WATCH_SETTLE_SECONDS))
        self._poll_interval = poll_interval
        self._deb = SettleDebouncer(self._settle, clock=clock)
        self._lock = threading.Lock()
        self._observer = None
        self._poller: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = False

    def start(self) -> bool:
        """Begin watching. Returns True when at least one root is under watch,
        False (feature off) on any degradation path."""
        if self._started:
            return True
        if not watchdog_available():
            _log_watchdog_missing()
            return False
        if not self._roots:
            logger.info("Folder watcher: no library paths configured — watcher off.")
            return False
        observer = _OBSERVER_CLS()  # type: ignore[misc]
        handler = _MediaEventHandler(self._note)
        watched = 0
        for root in self._roots:
            if not os.path.isdir(root):
                logger.info("Folder watcher: skipping unavailable root %s "
                            "(nightly delta still covers it).", root)
                continue
            try:
                observer.schedule(handler, root, recursive=True)
                watched += 1
            except Exception:
                logger.warning("Folder watcher: could not watch %s — the nightly "
                               "delta still covers it.", root, exc_info=True)
        if watched == 0:
            logger.info("Folder watcher: no watchable roots — falling back to the "
                        "nightly delta.")
            return False
        try:
            observer.daemon = True
            observer.start()
        except Exception:
            logger.warning("Folder watcher: observer failed to start — nightly "
                           "delta still covers the library.", exc_info=True)
            return False
        self._observer = observer
        self._stop.clear()
        self._poller = threading.Thread(target=self._poll_loop,
                                        name="library-watch", daemon=True)
        self._poller.start()
        self._started = True
        logger.info("Folder watcher: watching %d root(s), %.0fs settle window.",
                    watched, self._settle)
        return True

    def _note(self, path: str) -> None:
        if not path:
            return
        with self._lock:
            self._deb.note(path)

    def _poll_loop(self) -> None:
        while not self._stop.wait(self._poll_interval):
            batch: set[str] | None = None
            with self._lock:
                if self._deb.ready():
                    batch = self._deb.drain()
            if batch:
                try:
                    self._on_batch(sorted(batch))
                except Exception:
                    logger.exception("Folder watcher: settled-batch handler failed.")

    def stop(self) -> None:
        """Stop the observer and poller. Safe to call when never started."""
        self._stop.set()
        observer = self._observer
        if observer is not None:
            try:
                observer.stop()
                observer.join(timeout=5)
            except Exception:
                logger.debug("Folder watcher: observer stop failed.", exc_info=True)
        poller = self._poller
        if poller is not None and poller.is_alive():
            poller.join(timeout=5)
        self._observer = None
        self._poller = None
        self._started = False


# ---------------------------------------------------------------------------
# Settled-batch pipeline — scoped index delta -> scoped identity -> reconcile.
# Importable and callable directly (tests / a manual invocation) as well as
# from the watcher's registry job.
# ---------------------------------------------------------------------------

def process_settled_paths(paths, *, progress=None, force: bool = False) -> dict:
    """Run the per-batch pipeline for a settled set of changed paths:

      1. scoped index delta over just the changed subtree (the collapse guard
         is judged against that subtree, so a small legitimate change can't trip
         it and a large vanish still does);
      2. offline identity (episode / show_folder / download) for the files now
         present under the scope — no network provider lookup here;
      3. request reconciliation: any open / grabbing / needs_attention /
         deferred request whose identity is now present closes through the daily
         check's own rule.

    Returns a summary dict. On a refused (guarded) index delta it stops after
    step 1 and reports the refusal — it never forces past the guard."""
    import library_index

    scope = sorted({str(p) for p in paths if p})
    summary: dict = {"scope": len(scope)}
    if not scope:
        return summary

    if progress is not None:
        progress(phase="Indexing changed files…")
    refreshed = library_index.refresh_library_index(scope=scope, force=force)
    summary["index"] = {
        "added": refreshed.added, "removed": refreshed.removed,
        "updated": refreshed.updated, "aborted": refreshed.aborted_reason}
    if refreshed.aborted_reason:
        logger.warning("Folder watcher: scoped index delta refused (%s).",
                       refreshed.aborted_reason)
        return summary

    present = library_index.indexed_paths_under(scope)
    if present:
        import library_identity
        if progress is not None:
            progress(phase="Resolving identities for new files…")
        summary["identity"] = library_identity.backfill_paths(present)

    import maintenance
    if progress is not None:
        progress(phase="Closing requests now satisfied…")
    summary["reconcile"] = maintenance.reconcile_requests_with_library()
    return summary
