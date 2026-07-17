# =============================================================================
# legacy_migration.py — Task H item 2
# =============================================================================
# Moves user state that historically lived beside the executable into the
# app_paths contract locations. This is a LINUX concern: on Windows the
# contract locations ARE the install folder, so the plan is empty by
# definition and nothing ever relocates.
#
# Mirrors the movie_migration discipline:
#   detect -> exact source/destination dry run -> explicit confirmation ->
#   journalled, hash-verified copies -> reopen the copied SQLite DB ->
#   archive the legacy originals only after everything verified.
#
# The journal is a JSON file in DATA_DIR (not the SQLite DB — the DB is one
# of the things being migrated), written after every operation, so an
# interrupted run resumes idempotently: ops whose destination already matches
# the recorded hash are skipped, never re-copied and never clobbered.
#
# WAL safety: before copying a SQLite file the migration checkpoints the WAL
# (TRUNCATE) and READS THE RESULT ROW — sqlite3 does not raise on a blocked
# checkpoint, so (busy, log, checkpointed) is verified explicitly. Any sign
# of a live user (busy/partial checkpoint, a writer holding the lock, a
# non-empty -wal after locking) ABORTS that database's migration with a
# clear "close the app that is using this database first" failure; the
# source is never renamed unless every prior step succeeded. During
# hash+copy the migration holds BEGIN IMMEDIATE on the source so no writer
# can slip rows in between the checkpoint and the copy.
# =============================================================================

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import app_paths

logger = logging.getLogger(__name__)

JOURNAL_FILE = "legacy_migration_journal.json"
_JOURNAL_VERSION = 1
ARCHIVE_SUFFIX = ".migrated"

# Legacy filename -> which contract dir owns it now. Databases and durable
# state go to DATA, .env to CONFIG, expendable/refreshable files to CACHE.
# sensarr.pid (and the pre-rename plexxarr.pid) is runtime state: never
# migrated, just left behind.
_CONFIG_FILES = (".env",)
_CACHE_FILES = (
    "trackers_cache.txt",
    "maintenance_cache.json",
    "library_lowqual.json",
    "watchlist_recs.json",
    "unidentified_folders.json",
    "anidb_titles.dat.gz",
)
# plex_reset_button.db + anime_meta.sqlite + any sibling SQLite file.
_DATA_GLOBS = ("*.db", "*.sqlite")
_SKIP_NAMES = {"sensarr.pid", "plexxarr.pid"}
_SQLITE_SUFFIXES = {".db", ".sqlite"}


@dataclass(frozen=True)
class MigrationItem:
    src: str
    dest: str
    kind: str  # "config" | "data" | "cache"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Plan (dry run — touches nothing)
# ---------------------------------------------------------------------------

def plan_migration(paths: app_paths.AppPaths | None = None,
                   platform: str | None = None,
                   environ=None) -> list[MigrationItem]:
    """Legacy files beside the executable, plus files under the pre-rename
    XDG dirs (~/.config/plexxarr and friends), that belong in the contract
    dirs. Empty on Windows (the dirs are the same place) and on fresh
    installs."""
    platform = platform if platform is not None else sys.platform
    paths = paths or app_paths.PATHS
    if platform == "win32":
        return []
    legacy_dir = paths.install_dir
    items: list[MigrationItem] = []

    def add(src: Path, dest_dir: Path, kind: str) -> None:
        if src.name in _SKIP_NAMES or not src.is_file():
            return
        dest = dest_dir / src.name
        if dest == src:
            return
        items.append(MigrationItem(str(src), str(dest), kind))

    for name in _CONFIG_FILES:
        add(legacy_dir / name, paths.config_dir, "config")
    for pattern in _DATA_GLOBS:
        for f in sorted(legacy_dir.glob(pattern)):
            add(f, paths.data_dir, "data")
    for name in _CACHE_FILES:
        add(legacy_dir / name, paths.cache_dir, "cache")
    # Pre-rename XDG homes: same contract, old folder name. add()'s guards
    # keep this idempotent, and the pid locks are skip-listed like any other.
    for old_config, old_data, old_cache in app_paths.legacy_xdg_dirs(
            platform, environ):
        for name in _CONFIG_FILES:
            add(old_config / name, paths.config_dir, "config")
        for pattern in _DATA_GLOBS:
            for f in sorted(old_data.glob(pattern)):
                add(f, paths.data_dir, "data")
        for name in _CACHE_FILES:
            add(old_cache / name, paths.cache_dir, "cache")
    return items


def format_plan(items: list[MigrationItem]) -> str:
    """The exact source -> destination dry run shown before confirmation."""
    if not items:
        return "Nothing to migrate — no legacy files found beside the app."
    lines = ["The following files will be COPIED (originals are archived "
             f"with a '{ARCHIVE_SUFFIX}' suffix only after every copy "
             "verifies):", ""]
    for it in items:
        lines.append(f"  [{it.kind}] {it.src}")
        lines.append(f"      -> {it.dest}")
    return "\n".join(lines)


def migration_pending(paths: app_paths.AppPaths | None = None,
                      platform: str | None = None,
                      environ=None) -> bool:
    return bool(plan_migration(paths, platform, environ))


# ---------------------------------------------------------------------------
# Journal (JSON in DATA_DIR, written after every op)
# ---------------------------------------------------------------------------

def _journal_path(paths: app_paths.AppPaths) -> Path:
    return paths.data_dir / JOURNAL_FILE


def _load_journal(paths: app_paths.AppPaths) -> dict:
    jp = _journal_path(paths)
    try:
        raw = json.loads(jp.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw.get("version") == _JOURNAL_VERSION:
            return raw
    except (OSError, ValueError):
        pass
    return {"version": _JOURNAL_VERSION, "ops": {}}


def _save_journal(paths: app_paths.AppPaths, journal: dict) -> None:
    jp = _journal_path(paths)
    jp.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    jp.write_text(json.dumps(journal, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# Execution — hash-verified copy, DB reopen, archive after confirm
# ---------------------------------------------------------------------------

_IN_USE_MSG = ("the database is in use — close the app that is using it, "
               "then run the migration again")


def _acquire_quiesced_source(db_path: Path) -> sqlite3.Connection:
    """Quiesce a source SQLite database and hold a write lock for the copy.

    Every step is verified (spec item 2: 'never move an open WAL database'):
      1. PRAGMA wal_checkpoint(TRUNCATE), READING the (busy, log,
         checkpointed) result row — sqlite3 does not raise when a
         reader/writer blocks the checkpoint, so busy != 0 or a partial
         checkpoint (log != checkpointed) aborts here.
      2. BEGIN IMMEDIATE so no writer can add rows between the checkpoint
         and the copy (a writer holding the lock surfaces as 'database is
         locked' and aborts).
      3. With the lock held, the -wal sibling must be absent or empty —
         anything that slipped in between steps aborts.

    Returns the connection HOLDING the lock; the caller must rollback+close
    it after the copy and before any rename (Windows cannot rename an open
    file). Raises RuntimeError with a clear 'close the app first' message
    on any sign of a live user.
    """
    conn = sqlite3.connect(str(db_path), timeout=2)
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        busy, log, checkpointed = row if row else (1, -1, -2)
        if busy != 0 or log != checkpointed:
            raise RuntimeError(
                f"WAL checkpoint blocked (busy={busy}, log={log}, "
                f"checkpointed={checkpointed}); " + _IN_USE_MSG)
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                f"could not lock the database for copying ({exc}); "
                + _IN_USE_MSG) from exc
        wal = db_path.with_name(db_path.name + "-wal")
        if wal.exists() and wal.stat().st_size > 0:
            raise RuntimeError(
                "the WAL sibling gained data after the checkpoint; "
                + _IN_USE_MSG)
        return conn
    except BaseException:
        conn.close()
        raise


def _sqlite_has_no_user_data(path: Path) -> bool:
    """True only when the database opens read-only and every non-internal
    table is empty (a schema-only file from a premature initialize_*_db).
    Any doubt — unreadable, corrupt, a single row anywhere — returns False
    so the anti-clobber guard stays in charge."""
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True,
                               timeout=2)
    except sqlite3.Error:
        return False
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            if conn.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone():
                return False
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _archive_empty_destination(dest: Path) -> None:
    """Self-heal for the fresh-XDG-DB-created-first case: a destination
    SQLite file with zero user rows is archived (never deleted) so the real
    legacy database can take its place."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archived = dest.with_name(dest.name + f".empty-{stamp}")
    dest.rename(archived)
    for sib in (dest.with_name(dest.name + "-wal"),
                dest.with_name(dest.name + "-shm")):
        if sib.exists():
            sib.rename(sib.with_name(sib.name + f".empty-{stamp}"))
    logger.info("Destination %s held no user data — archived as %s so the "
                "legacy database can migrate in.", dest, archived.name)


def _verify_sqlite_opens(db_path: Path) -> None:
    """The copied database must actually open and answer a query."""
    conn = sqlite3.connect(str(db_path), timeout=15)
    try:
        result = conn.execute("PRAGMA quick_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"quick_check failed: {result!r}")
    finally:
        conn.close()


def execute_migration(items: list[MigrationItem],
                      paths: app_paths.AppPaths | None = None,
                      *, archive_legacy: bool = True) -> dict:
    """Run (or RESUME) the confirmed plan. Idempotent: ops journalled as done
    with a matching destination hash are skipped. Originals are renamed to
    `<name>.migrated` only when their copy verified — never deleted."""
    paths = paths or app_paths.PATHS
    journal = _load_journal(paths)
    ops: dict = journal["ops"]
    copied = skipped = failed = 0

    for it in items:
        src, dest = Path(it.src), Path(it.dest)
        rec = ops.get(it.src) or {}
        try:
            if rec.get("state") == "done" and dest.is_file() and \
                    _sha256(dest) == rec.get("sha256"):
                skipped += 1
            else:
                if not src.is_file():
                    if dest.is_file():
                        skipped += 1  # archived by a previous completed run
                        continue
                    raise FileNotFoundError(f"legacy source vanished: {src}")
                is_sqlite = src.suffix in _SQLITE_SUFFIXES
                if dest.is_file() and _sha256(dest) != _sha256(src):
                    # Self-heal: a schema-only DB created at the destination
                    # by a premature initialize_*_db is archived aside; a
                    # destination with ANY user data stays untouchable.
                    if is_sqlite and _sqlite_has_no_user_data(dest):
                        _archive_empty_destination(dest)
                    else:
                        raise FileExistsError(
                            f"different file already at destination: {dest}")
                # Quiesce + hold a write lock on a SQLite source for the
                # whole hash+copy, so an open writer aborts cleanly instead
                # of losing WAL rows. Released before any rename below.
                lock_conn = _acquire_quiesced_source(src) if is_sqlite else None
                try:
                    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    tmp = dest.with_name(dest.name + ".migrating")
                    shutil.copy2(src, tmp)
                    src_hash = _sha256(src)
                    if _sha256(tmp) != src_hash:
                        tmp.unlink(missing_ok=True)
                        raise RuntimeError(f"hash mismatch copying {src}")
                finally:
                    if lock_conn is not None:
                        try:
                            lock_conn.rollback()
                        finally:
                            lock_conn.close()
                tmp.replace(dest)
                if is_sqlite:
                    _verify_sqlite_opens(dest)
                ops[it.src] = {
                    "dest": it.dest, "sha256": src_hash, "state": "done",
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                _save_journal(paths, journal)
                copied += 1
            if archive_legacy and src.is_file():
                src.rename(src.with_name(src.name + ARCHIVE_SUFFIX))
                # WAL/SHM siblings of an archived DB are stale once the
                # checkpointed main file moved — archive them too.
                for sib in (src.with_name(src.name + "-wal"),
                            src.with_name(src.name + "-shm")):
                    if sib.exists():
                        sib.rename(sib.with_name(sib.name + ARCHIVE_SUFFIX))
        except Exception as exc:
            logger.warning("Legacy migration failed for %s: %s", src, exc)
            ops[it.src] = {
                "dest": it.dest, "state": "failed", "detail": str(exc),
                "at": datetime.now(timezone.utc).isoformat(),
            }
            _save_journal(paths, journal)
            failed += 1

    return {"copied": copied, "skipped": skipped, "failed": failed,
            "total": len(items)}
