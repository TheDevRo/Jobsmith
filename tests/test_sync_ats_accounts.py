"""The `ats_account` per-tenant registry entity: round-trip through the sync
folder, LWW promotion (a completed sign-in beats a pending create), tombstone
pruning, and the accessor helpers in backend.auto_apply.ats_accounts.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from backend import database as dbmod
from backend.auto_apply import ats_accounts
from backend.sync import SyncEngine


class Clock:
    """Each call returns a strictly-later UTC time (drives deterministic LWW)."""

    def __init__(self):
        self.t = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


async def _init_db(path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    await dbmod.init_db()


def _seed_account(path, host, email="jobs@example.com", status="active",
                  provider="workday", created_at="2026-07-15T08:00:00.000Z"):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """INSERT INTO ats_accounts
                 (tenant_host, provider, email, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?)""",
            (host, provider, email, status, created_at, created_at),
        )
        conn.commit()
    finally:
        conn.close()


def _rows(path, sql, params=()):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_ats_account_round_trip_and_signin_wins(tmp_path, monkeypatch):
    """iOS creates a pending account; the desktop signs in and re-emits it
    active with a newer timestamp, so `active` wins back on iOS."""
    path_a = tmp_path / "phone.db"   # the creator (stands in for iOS)
    path_b = tmp_path / "desktop.db"
    folder = tmp_path / "sync"
    clock = Clock()

    await _init_db(path_a, monkeypatch)
    _seed_account(path_a, "acme.wd5.myworkdayjobs.com", status="pending_verification")
    await _init_db(path_b, monkeypatch)  # DB_PATH now points at the desktop

    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    b = SyncEngine(path_b, "C3D4", now_fn=clock)

    exp = await a.export_changes(folder)
    assert exp.live == 1

    imp = await b.import_changes(folder)
    assert imp.upserts == 1
    got = _rows(path_b, "SELECT * FROM ats_accounts")
    assert len(got) == 1
    assert got[0]["tenant_host"] == "acme.wd5.myworkdayjobs.com"
    assert got[0]["provider"] == "workday"
    assert got[0]["status"] == "pending_verification"

    # Desktop signs in → promotes pending → active with a newer timestamp.
    await ats_accounts.mark_signed_in("acme.wd5.myworkdayjobs.com")
    got = _rows(path_b, "SELECT * FROM ats_accounts")
    assert got[0]["status"] == "active"
    assert got[0]["last_sign_in_at"]

    exp_b = await b.export_changes(folder)
    assert exp_b.live == 1
    await a.import_changes(folder)
    back = _rows(path_a, "SELECT * FROM ats_accounts")
    assert back[0]["status"] == "active"

    # Steady state: neither side re-emits anything.
    assert (await a.export_changes(tmp_path / "s2")).total == 0
    assert (await b.export_changes(tmp_path / "s3")).total == 0


@pytest.mark.asyncio
async def test_ats_account_tombstone_prunes_everywhere(tmp_path, monkeypatch):
    path_a = tmp_path / "phone.db"
    path_b = tmp_path / "desktop.db"
    folder = tmp_path / "sync"
    clock = Clock()

    await _init_db(path_a, monkeypatch)
    _seed_account(path_a, "globex.wd1.myworkdayjobs.com")
    await _init_db(path_b, monkeypatch)

    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    b = SyncEngine(path_b, "C3D4", now_fn=clock)
    await a.export_changes(folder)
    await b.import_changes(folder)
    assert _rows(path_b, "SELECT * FROM ats_accounts")

    # The user removes the saved account; the delete propagates.
    conn = sqlite3.connect(path_a)
    conn.execute("DELETE FROM ats_accounts")
    conn.commit()
    conn.close()
    exp = await a.export_changes(folder)
    assert exp.tombstones == 1
    await b.import_changes(folder)
    assert _rows(path_b, "SELECT * FROM ats_accounts") == []


@pytest.mark.asyncio
async def test_accessor_upsert_get_and_promote(tmp_path, monkeypatch):
    path = tmp_path / "desktop.db"
    await _init_db(path, monkeypatch)

    row = await ats_accounts.upsert(
        "ACME.wd5.myworkdayjobs.com", "me@example.com", status="pending_verification"
    )
    assert row is not None
    # Host is normalized to lowercase.
    assert row["tenant_host"] == "acme.wd5.myworkdayjobs.com"
    assert row["status"] == "pending_verification"
    assert row["created_at"]
    created_at = row["created_at"]

    fetched = await ats_accounts.get("acme.wd5.myworkdayjobs.com")
    assert fetched["email"] == "me@example.com"

    # A sign-in promotes pending → active and preserves created_at.
    promoted = await ats_accounts.mark_signed_in("acme.wd5.myworkdayjobs.com")
    assert promoted["status"] == "active"
    assert promoted["last_sign_in_at"]
    assert promoted["created_at"] == created_at

    # A password is never stored anywhere in the table.
    cols = _rows(path, "PRAGMA table_info(ats_accounts)")
    assert not any("password" in c["name"].lower() for c in cols)

    assert await ats_accounts.get("unknown.wd1.myworkdayjobs.com") is None
    listed = await ats_accounts.all_accounts(provider="workday")
    assert [r["tenant_host"] for r in listed] == ["acme.wd5.myworkdayjobs.com"]
