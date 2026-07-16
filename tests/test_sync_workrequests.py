"""The `work_request` hand-off entity: round-trip through the sync folder,
done-beats-pending under LWW, tombstone pruning, and the opt-in fulfillment
hook (off by default — importing a request must never by itself spend tokens).
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from backend import background_tasks as bg
from backend import database as dbmod
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


def _seed_request(path, req_id="req-1", status="pending", params=None,
                  kind="score_all", requested_at="2026-07-15T08:00:00.000Z"):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """INSERT INTO work_requests
                 (id, kind, status, requested_by, requested_at, params)
               VALUES (?,?,?,?,?,?)""",
            (req_id, kind, status, "PHONE01", requested_at,
             json.dumps(params if params is not None else {"cap": 25, "pool": "inbox"})),
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
async def test_work_request_round_trip_and_completion(tmp_path, monkeypatch):
    """Requester's pending record lands on the fulfiller; the fulfiller's
    `done` re-emit (newer timestamp) wins back on the requester."""
    path_a = tmp_path / "phone.db"   # the requester (stands in for iOS)
    path_b = tmp_path / "desktop.db"
    folder = tmp_path / "sync"
    clock = Clock()

    await _init_db(path_a, monkeypatch)
    _seed_request(path_a)
    await _init_db(path_b, monkeypatch)  # DB_PATH now points at the desktop

    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    b = SyncEngine(path_b, "C3D4", now_fn=clock)

    exp = await a.export_changes(folder)
    assert exp.live == 1
    lines = [json.loads(l) for l in (folder / "changes" / "A1B2.jsonl").read_text().splitlines()]
    assert lines[0]["entity"] == "work_request"
    assert lines[0]["data"]["status"] == "pending"
    assert lines[0]["data"]["params"] == {"cap": 25, "pool": "inbox"}

    imp = await b.import_changes(folder)
    assert imp.upserts == 1
    got = _rows(path_b, "SELECT * FROM work_requests")
    assert len(got) == 1
    assert got[0]["id"] == "req-1"
    assert got[0]["status"] == "pending"
    assert got[0]["requested_by"] == "PHONE01"
    assert json.loads(got[0]["params"]) == {"cap": 25, "pool": "inbox"}

    # Desktop fulfills and retires it (DB_PATH already points at the desktop).
    await dbmod.complete_work_request("req-1", "C3D4")
    got = _rows(path_b, "SELECT * FROM work_requests")
    assert got[0]["status"] == "done" and got[0]["completed_by"] == "C3D4"

    # The re-export carries `done` with a newer timestamp, so it wins on A.
    exp_b = await b.export_changes(folder)
    assert exp_b.live == 1
    await a.import_changes(folder)
    back = _rows(path_a, "SELECT * FROM work_requests")
    assert back[0]["status"] == "done"
    assert back[0]["completed_by"] == "C3D4"

    # Steady state: neither side re-emits anything.
    assert (await a.export_changes(tmp_path / "s2")).total == 0
    assert (await b.export_changes(tmp_path / "s3")).total == 0


@pytest.mark.asyncio
async def test_work_request_tombstone_prunes_everywhere(tmp_path, monkeypatch):
    path_a = tmp_path / "phone.db"
    path_b = tmp_path / "desktop.db"
    folder = tmp_path / "sync"
    clock = Clock()

    await _init_db(path_a, monkeypatch)
    _seed_request(path_a, status="done")
    await _init_db(path_b, monkeypatch)

    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    b = SyncEngine(path_b, "C3D4", now_fn=clock)
    await a.export_changes(folder)
    await b.import_changes(folder)
    assert _rows(path_b, "SELECT * FROM work_requests")

    # The requester prunes the retired row; the delete propagates.
    conn = sqlite3.connect(path_a)
    conn.execute("DELETE FROM work_requests")
    conn.commit()
    conn.close()
    exp = await a.export_changes(folder)
    assert exp.tombstones == 1
    await b.import_changes(folder)
    assert _rows(path_b, "SELECT * FROM work_requests") == []


@pytest.mark.asyncio
async def test_fulfillment_is_off_by_default(tmp_path, monkeypatch):
    """No `sync.fulfill_work_requests` ⇒ the after-import hook schedules nothing,
    even with a pending request sitting in the database."""
    path = tmp_path / "desktop.db"
    await _init_db(path, monkeypatch)
    _seed_request(path)

    monkeypatch.setattr(bg.state, "load_config", lambda: {"sync": {"enabled": True}})
    created = []
    monkeypatch.setattr(bg.asyncio, "create_task", lambda coro: created.append(coro))

    bg.schedule_work_request_fulfillment()
    assert created == []


@pytest.fixture(autouse=True)
def _clean_mutex_state():
    """Each fulfillment test starts with no batch registered and no stale cancel
    flag — this module shares app_state's process-wide singletons with the rest
    of the suite."""
    bg.state.running_tasks.pop("score_batch", None)
    bg.state.running_tasks.pop("work_request", None)
    bg.state.cancel_score.clear()
    yield
    bg.state.running_tasks.pop("score_batch", None)
    bg.state.running_tasks.pop("work_request", None)
    bg.state.cancel_score.clear()


@pytest.mark.asyncio
async def test_fulfillment_scores_each_pool_and_retires_requests(tmp_path, monkeypatch):
    """Opted in: each pool gets its own batch (inbox=discovered, pipeline=
    shortlisted) drawing the largest cap in the bucket, then every request whose
    pool ran to completion is marked done with this device's id."""
    path = tmp_path / "desktop.db"
    await _init_db(path, monkeypatch)
    _seed_request(path, "req-1", params={"cap": 25, "pool": "inbox"},
                  requested_at="2026-07-15T08:00:00.000Z")
    _seed_request(path, "req-2", params={"cap": 10, "pool": "pipeline"},
                  requested_at="2026-07-15T08:00:01.000Z")

    monkeypatch.setattr(
        bg.state, "load_config",
        lambda: {"sync": {"enabled": True, "fulfill_work_requests": True,
                          "device_id": "C3D4"}})
    batches = []

    async def fake_batch(limit=None, rescore=False, status="discovered"):
        bg.state.cancel_score.clear()  # the real batch clears this on entry
        batches.append((limit, status))

    monkeypatch.setattr(bg, "_bg_score_batch", fake_batch)
    logged = []

    async def fake_log(action, details, job_id=None):
        logged.append(action)

    monkeypatch.setattr(bg.db, "log_activity", fake_log)

    await bg._bg_fulfill_work_requests()

    # One batch per pool, each against its own worklist and cap.
    assert batches == [(25, "discovered"), (10, "shortlisted")]
    rows = _rows(path, "SELECT * FROM work_requests ORDER BY id")
    assert [r["status"] for r in rows] == ["done", "done"]
    assert all(r["completed_by"] == "C3D4" for r in rows)
    assert "handoff" in logged
    # The mutex is released once fulfillment ends.
    assert not bg.state.task_running("score_batch")


@pytest.mark.asyncio
async def test_fulfillment_retires_only_completed_pools(tmp_path, monkeypatch):
    """A pool whose batch is cancelled midway leaves its request pending; the
    other pool's completed batch still retires its own request. Covers both
    pool isolation and retire-only-on-completion."""
    path = tmp_path / "desktop.db"
    await _init_db(path, monkeypatch)
    _seed_request(path, "req-inbox", params={"cap": 25, "pool": "inbox"},
                  requested_at="2026-07-15T08:00:00.000Z")
    _seed_request(path, "req-pipe", params={"cap": 10, "pool": "pipeline"},
                  requested_at="2026-07-15T08:00:01.000Z")

    monkeypatch.setattr(
        bg.state, "load_config",
        lambda: {"sync": {"fulfill_work_requests": True, "device_id": "C3D4"}})

    async def fake_batch(limit=None, rescore=False, status="discovered"):
        # Inbox completes cleanly; the pipeline run is cancelled midway.
        if status == "shortlisted":
            bg.state.cancel_score.set()
        else:
            bg.state.cancel_score.clear()

    monkeypatch.setattr(bg, "_bg_score_batch", fake_batch)
    monkeypatch.setattr(bg.db, "log_activity", _noop_log())

    await bg._bg_fulfill_work_requests()

    by_id = {r["id"]: r for r in _rows(path, "SELECT * FROM work_requests")}
    assert by_id["req-inbox"]["status"] == "done"        # completed → retired
    assert by_id["req-pipe"]["status"] == "pending"      # cancelled → left pending


@pytest.mark.asyncio
async def test_fulfillment_defers_while_a_batch_is_running(tmp_path, monkeypatch):
    """A batch already registered under the score-batch mutex wins; the request
    stays pending and the next sync tick re-checks."""
    path = tmp_path / "desktop.db"
    await _init_db(path, monkeypatch)
    _seed_request(path)

    monkeypatch.setattr(
        bg.state, "load_config",
        lambda: {"sync": {"fulfill_work_requests": True, "device_id": "C3D4"}})

    class _Running:  # stands in for a live score-batch task
        def done(self):
            return False

    bg.state.running_tasks["score_batch"] = _Running()

    async def boom(limit=None, rescore=False, status="discovered"):
        raise AssertionError("a second batch must not start")

    monkeypatch.setattr(bg, "_bg_score_batch", boom)
    await bg._bg_fulfill_work_requests()
    assert _rows(path, "SELECT status FROM work_requests")[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_unknown_kind_is_dropped_on_import(tmp_path, monkeypatch):
    """A work_request of a kind this build can't fulfill is dropped on import
    rather than stored — otherwise it would sit pending forever and re-sync
    every cycle."""
    path_a = tmp_path / "phone.db"
    path_b = tmp_path / "desktop.db"
    folder = tmp_path / "sync"
    clock = Clock()

    await _init_db(path_a, monkeypatch)
    _seed_request(path_a, "req-known", kind="score_all")
    _seed_request(path_a, "req-alien", kind="reindex_universe")
    await _init_db(path_b, monkeypatch)

    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    b = SyncEngine(path_b, "C3D4", now_fn=clock)
    await a.export_changes(folder)
    await b.import_changes(folder)

    got = _rows(path_b, "SELECT id FROM work_requests")
    assert [r["id"] for r in got] == ["req-known"]  # the alien kind never landed


def _noop_log():
    async def _log(action, details, job_id=None):
        return None
    return _log
