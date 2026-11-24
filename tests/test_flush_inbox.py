"""Inbox flush: hard-deletes only undecided, untouched Inbox jobs."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from backend import database as dbmod
from backend.sync import SyncEngine

OLD_SQLITE = "2020-01-01 00:00:00"                      # CURRENT_TIMESTAMP style
OLD_ISO = "2020-01-01T00:00:00.123456+00:00"            # Python isoformat style


def _recent_iso():
    return (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()


def _recent_sqlite():
    return (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")


def _insert(conn, jid, status="discovered", discovered=OLD_SQLITE, coverage=None):
    conn.execute(
        """INSERT INTO jobs (id, source, external_id, title, company, url,
             description, date_discovered, status, coverage)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (jid, "greenhouse", jid, "Test Role", "Company A", f"https://x/{jid}",
         "desc", discovered, status, coverage),
    )


def _app(conn, jid):
    conn.execute(
        "INSERT INTO applications (id, job_id, status) VALUES (?,?,?)",
        (f"app-{jid}", jid, "pending_review"),
    )


def _ids(path):
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT id FROM jobs")}
    finally:
        conn.close()


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    await dbmod.init_db()
    return path


def _seed_mixed(path):
    conn = sqlite3.connect(path)
    cov_dismissed = json.dumps({"requirements": [
        {"text": "Go", "kind": "must", "met": False, "dismissed": True}]})
    cov_open = json.dumps({"requirements": [
        {"text": "Go", "kind": "must", "met": False}]})
    _insert(conn, "flush-sqlite")
    _insert(conn, "flush-iso", discovered=OLD_ISO)
    _insert(conn, "flush-cov", coverage=cov_open)
    _insert(conn, "keep-short", status="shortlisted")
    _insert(conn, "keep-passed", status="passed")
    _insert(conn, "keep-applied", status="applied")
    _insert(conn, "keep-deleted", status="deleted")
    _insert(conn, "keep-app")
    _app(conn, "keep-app")
    _insert(conn, "keep-gap", coverage=cov_dismissed)
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_flush_removes_only_untouched_inbox(db):
    _seed_mixed(db)
    assert await dbmod.count_flushable(None) == 3
    assert await dbmod.flush_inbox(None) == 3
    assert _ids(db) == {"keep-short", "keep-passed", "keep-applied",
                        "keep-deleted", "keep-app", "keep-gap"}
    assert await dbmod.flush_inbox(None) == 0


@pytest.mark.asyncio
async def test_age_filter_handles_both_timestamp_formats(db):
    conn = sqlite3.connect(db)
    _insert(conn, "old-sqlite", discovered=OLD_SQLITE)
    _insert(conn, "old-iso", discovered=OLD_ISO)
    _insert(conn, "new-sqlite", discovered=_recent_sqlite())
    _insert(conn, "new-iso", discovered=_recent_iso())
    conn.commit()
    conn.close()

    preview = await dbmod.count_flushable(14)
    assert preview == 2
    assert await dbmod.flush_inbox(14) == preview
    assert _ids(db) == {"new-sqlite", "new-iso"}
    assert await dbmod.count_flushable(None) == 2


@pytest.mark.asyncio
async def test_flush_clears_children_and_rediscovery_inserts_fresh(db):
    conn = sqlite3.connect(db)
    _insert(conn, "j1")
    conn.commit()
    conn.close()
    old_id = "j1"
    assert await dbmod.flush_inbox(None) == 1
    new_id = await dbmod.upsert_job(
        {"source": "greenhouse", "external_id": "j1", "title": "Test Role",
         "company": "Company A", "url": "https://x/j1", "description": "d"})
    assert new_id is not None and new_id != old_id


@pytest.mark.asyncio
async def test_flush_emits_job_tombstone(tmp_path, monkeypatch):
    path = tmp_path / "a.db"
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    await dbmod.init_db()
    conn = sqlite3.connect(path)
    _insert(conn, "j1")
    conn.commit()
    conn.close()

    t = [datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)]

    def clock():
        t[0] += timedelta(seconds=1)
        return t[0]

    engine = SyncEngine(path, "A1B2", now_fn=clock)
    folder = tmp_path / "sync"
    first = await engine.export_changes(folder)
    assert first.live >= 1

    assert await dbmod.flush_inbox(None) == 1
    second = await engine.export_changes(folder)
    assert second.tombstones >= 1


@pytest.mark.asyncio
async def test_flush_routes(db):
    from fastapi.testclient import TestClient

    from backend.main import app

    _seed_mixed(db)
    client = TestClient(app)
    assert client.get("/api/jobs/flush-preview").json() == {"count": 3}
    assert client.get("/api/jobs/flush-preview?older_than_days=14").json() == {"count": 3}
    assert client.get("/api/jobs/flush-preview?older_than_days=-1").status_code == 422
    resp = client.post("/api/jobs/flush", json={"older_than_days": None})
    assert resp.status_code == 200 and resp.json()["flushed"] == 3
    assert client.get("/api/jobs/flush-preview").json() == {"count": 0}
