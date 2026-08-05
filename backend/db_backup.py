"""
db_backup.py — startup safety snapshots of the SQLite database.

The database *is* the user's job search: postings, applications, activity
history, answer bank. A corrupted disk, an interrupted schema migration, or a
bad manual edit would otherwise be unrecoverable. This module takes a snapshot
at most once per calendar day, right before init_db() runs migrations, and
keeps a short rolling history.

Design notes:
- Uses the sqlite3 online backup API (`Connection.backup`), which takes a
  consistent snapshot of a live WAL database — cp on a WAL db can tear the
  file, so this is the only correct copy method while the app may be running.
- Runs in a worker thread (`asyncio.to_thread`) because the stdlib backup API
  is synchronous; the event loop is never blocked.
- Best-effort by contract: a failed backup logs loudly but never stops boot.
  A server that can't snapshot should still serve.
- At most one snapshot per day (KEEP_DAYS files kept). Restarting the server
  many times in one day therefore doesn't churn the whole history away — the
  oldest snapshot is always ~KEEP_DAYS calendar days old.

Restoring: stop the server, then

    cp data/backups/jobsmith-YYYYMMDD.db data/jobsmith.db
    rm -f data/jobsmith.db-wal data/jobsmith.db-shm

and start the server again (migrations re-run forward automatically).
"""

import asyncio
import logging
import sqlite3
from datetime import date
from pathlib import Path

from .database import DB_PATH

logger = logging.getLogger(__name__)

BACKUP_DIR = DB_PATH.parent / "backups"
KEEP_DAYS = 7  # rolling daily snapshots to retain

_PREFIX = "jobsmith-"
_SUFFIX = ".db"


def _backup_path_for_today() -> Path:
    return BACKUP_DIR / f"{_PREFIX}{date.today().strftime('%Y%m%d')}{_SUFFIX}"


def _snapshot_sync(src: Path, dest: Path) -> None:
    """Consistent point-in-time copy of a (possibly live) SQLite db."""
    tmp = dest.with_name(dest.name + ".tmp")
    src_conn = sqlite3.connect(str(src))
    try:
        dest_conn = sqlite3.connect(str(tmp))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()
    # Atomic publish so a crash mid-backup never leaves a torn .db in the
    # backups dir masquerading as a good snapshot.
    tmp.replace(dest)


def _prune_sync(keep: int) -> list[str]:
    """Delete all but the newest `keep` snapshots. Returns deleted names."""
    snaps = sorted(
        p for p in BACKUP_DIR.glob(f"{_PREFIX}*{_SUFFIX}")
        if p.is_file()
    )
    doomed = snaps[:-keep] if keep > 0 else snaps
    deleted = []
    for p in doomed:
        try:
            p.unlink()
            deleted.append(p.name)
        except OSError:
            logger.warning("Could not prune old backup %s", p, exc_info=True)
    return deleted


async def maybe_backup_daily() -> Path | None:
    """Snapshot the database unless one already exists for today.

    Returns the path of the snapshot taken (or None if skipped/failed).
    Never raises: called from the boot sequence, and failing to back up must
    not prevent the app from starting.
    """
    try:
        if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
            return None  # first boot — nothing to protect yet

        dest = _backup_path_for_today()
        if dest.exists():
            return None  # already snapped today

        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_snapshot_sync, DB_PATH, dest)
        deleted = await asyncio.to_thread(_prune_sync, KEEP_DAYS)
        size_mb = dest.stat().st_size / 1_048_576
        logger.info(
            "Database snapshot: %s (%.1f MB)%s",
            dest.name, size_mb,
            f"; pruned {len(deleted)} old snapshot(s)" if deleted else "",
        )
        return dest
    except Exception:
        logger.exception(
            "Database backup failed — continuing startup WITHOUT today's snapshot"
        )
        return None
