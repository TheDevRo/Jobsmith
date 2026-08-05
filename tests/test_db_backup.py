"""Tests for backend/db_backup.py — daily startup snapshots + rotation.

The module contract that matters:
- a real, openable SQLite copy is produced (not a torn file copy)
- at most one snapshot per calendar day
- rotation keeps the newest KEEP_DAYS files
- nothing raises out of maybe_backup_daily(), ever (boot must not be blocked)
"""

import sqlite3

import pytest

from backend import db_backup


def _make_db(path, rows=3):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT)")
    conn.executemany(
        "INSERT INTO jobs (title) VALUES (?)", [(f"job {i}",) for i in range(rows)]
    )
    conn.commit()
    conn.close()


@pytest.fixture
def dbenv(tmp_path, monkeypatch):
    """Point the module at a throwaway db + backups dir."""
    db_path = tmp_path / "jobsmith.db"
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(db_backup, "DB_PATH", db_path)
    monkeypatch.setattr(db_backup, "BACKUP_DIR", backup_dir)
    return db_path, backup_dir


@pytest.mark.asyncio
async def test_snapshot_created_and_openable(dbenv):
    db_path, backup_dir = dbenv
    _make_db(db_path)

    dest = await db_backup.maybe_backup_daily()

    assert dest is not None and dest.exists()
    assert dest.parent == backup_dir
    # The snapshot is a valid database with the same content, not a byte-soup copy.
    conn = sqlite3.connect(str(dest))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
    finally:
        conn.close()
    # No .tmp turd left behind.
    assert list(backup_dir.glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_second_call_same_day_skips(dbenv):
    db_path, backup_dir = dbenv
    _make_db(db_path)

    first = await db_backup.maybe_backup_daily()
    assert first is not None
    mtime = first.stat().st_mtime_ns

    assert await db_backup.maybe_backup_daily() is None
    assert first.stat().st_mtime_ns == mtime  # untouched
    assert len(list(backup_dir.glob("jobsmith-*.db"))) == 1


@pytest.mark.asyncio
async def test_missing_or_empty_db_is_skipped(dbenv):
    db_path, backup_dir = dbenv

    assert await db_backup.maybe_backup_daily() is None  # no file at all

    db_path.touch()  # zero bytes — first boot creates-then-populates
    assert await db_backup.maybe_backup_daily() is None
    assert not backup_dir.exists()


@pytest.mark.asyncio
async def test_rotation_keeps_newest(dbenv, monkeypatch):
    db_path, backup_dir = dbenv
    _make_db(db_path)
    backup_dir.mkdir(parents=True)
    # Seed 9 fake older snapshots; names sort chronologically by design.
    for day in range(1, 10):
        (backup_dir / f"jobsmith-202601{day:02d}.db").write_bytes(b"old")

    dest = await db_backup.maybe_backup_daily()

    assert dest is not None
    snaps = sorted(p.name for p in backup_dir.glob("jobsmith-*.db"))
    assert len(snaps) == db_backup.KEEP_DAYS
    assert dest.name in snaps  # today's survives
    assert snaps[0] > "jobsmith-20260102.db"  # oldest seeds were pruned


@pytest.mark.asyncio
async def test_backup_failure_never_raises(dbenv, monkeypatch):
    db_path, _ = dbenv
    _make_db(db_path)

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(db_backup, "_snapshot_sync", _boom)
    # Contract: boot-path call swallows the failure and reports None.
    assert await db_backup.maybe_backup_daily() is None
