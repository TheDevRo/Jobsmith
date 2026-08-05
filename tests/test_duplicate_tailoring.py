"""One drag of a Pipeline card into Tailoring used to fire POST
/api/jobs/{id}/tailor eleven times, leaving eleven pending_review drafts for the
same job. The frontend half of the fix lives in frontend/tests/test_deck.js;
this covers the three server-side layers that make a duplicate trigger harmless:

  1. the endpoint refuses to spawn a second worker for a job already tailoring,
  2. a re-tailor supersedes the previous un-reviewed draft, and
  3. startup collapses duplicates any earlier build already wrote.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend import app_state as state
from backend import database as dbmod
from backend.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_tasks():
    state.running_tasks.clear()
    yield
    state.running_tasks.clear()


class _Task:
    """Stands in for an asyncio.Task without needing a running loop."""

    def __init__(self, done: bool):
        self._done = done

    def done(self) -> bool:
        return self._done


# ---------------------------------------------------------------------------
# 1. Endpoint guard
# ---------------------------------------------------------------------------

class TestTailorJobGuard:
    def test_second_trigger_while_running_does_not_spawn_a_worker(self, client, monkeypatch):
        job_id = "job-abc"
        monkeypatch.setattr(dbmod, "get_job", _fake_get_job(job_id))
        spawned = _count_spawns(monkeypatch)

        state.running_tasks[f"tailor_job:{job_id}"] = _Task(done=False)
        resp = client.post(f"/api/jobs/{job_id}/tailor")

        # Not an error: the UI toasts failures, and a duplicate trigger is a
        # no-op from the user's point of view.
        assert resp.status_code == 202
        assert resp.json()["message"] == "Tailoring already in progress"
        assert spawned == [], "a second tailor worker was spawned for the same job"

    def test_first_trigger_registers_the_task_under_a_per_job_key(self, client, monkeypatch):
        job_id = "job-abc"
        monkeypatch.setattr(dbmod, "get_job", _fake_get_job(job_id))
        # seen_keys snapshots the registry from inside the worker: that is the
        # window the guard actually covers (after the request returns, the
        # finished task deregisters itself — see the next test).
        seen_keys = []
        spawned = _count_spawns(monkeypatch, seen_keys=seen_keys)

        resp = client.post(f"/api/jobs/{job_id}/tailor")

        assert resp.status_code == 202
        assert resp.json()["message"] == "Tailoring started"
        assert spawned == [job_id]
        # The registration is what makes the guard above work — while the worker
        # is in flight, the key is what a duplicate POST collides with.
        assert f"tailor_job:{job_id}" in seen_keys

    def test_a_finished_run_deregisters_its_key(self, client, monkeypatch):
        """Per-job keys would otherwise accumulate forever in the registry."""
        job_id = "job-abc"
        monkeypatch.setattr(dbmod, "get_job", _fake_get_job(job_id))
        _count_spawns(monkeypatch)   # completes immediately

        client.post(f"/api/jobs/{job_id}/tailor")

        assert f"tailor_job:{job_id}" not in state.running_tasks

    def test_a_different_job_is_not_blocked(self, client, monkeypatch):
        monkeypatch.setattr(dbmod, "get_job", _fake_get_job("anything"))
        spawned = _count_spawns(monkeypatch)

        state.running_tasks["tailor_job:job-abc"] = _Task(done=False)
        resp = client.post("/api/jobs/job-xyz/tailor")

        assert resp.json()["message"] == "Tailoring started"
        assert spawned == ["job-xyz"]

    def test_a_finished_run_does_not_block_a_re_tailor(self, client, monkeypatch):
        job_id = "job-abc"
        monkeypatch.setattr(dbmod, "get_job", _fake_get_job(job_id))
        spawned = _count_spawns(monkeypatch)

        state.running_tasks[f"tailor_job:{job_id}"] = _Task(done=True)
        resp = client.post(f"/api/jobs/{job_id}/tailor")

        assert resp.json()["message"] == "Tailoring started"
        assert spawned == [job_id]

    def test_status_tailoring_with_no_task_still_runs(self, client, monkeypatch):
        """A job left in 'tailoring' by a crash must not be permanently wedged —
        the task registry is the guard, not the job's stored status."""
        job_id = "job-stale"
        monkeypatch.setattr(dbmod, "get_job", _fake_get_job(job_id, status="tailoring"))
        spawned = _count_spawns(monkeypatch)

        resp = client.post(f"/api/jobs/{job_id}/tailor")

        assert resp.json()["message"] == "Tailoring started"
        assert spawned == [job_id]


def _fake_get_job(job_id, status="shortlisted"):
    async def _get_job(requested_id):
        return {"id": requested_id, "title": "Engineer", "company": "Acme", "status": status}
    return _get_job


def _count_spawns(monkeypatch, seen_keys=None):
    """Replace the tailor worker with a recorder; returns the list it fills.

    Pass `seen_keys` to also capture state.running_tasks as it looks while the
    worker is in flight.
    """
    spawned = []

    async def _fake_bg(job_id):
        spawned.append(job_id)
        if seen_keys is not None:
            seen_keys.extend(state.running_tasks)

    from backend import background_tasks as bg
    monkeypatch.setattr(bg, "_bg_tailor_job", _fake_bg)
    return spawned


# ---------------------------------------------------------------------------
# 2. Supersede helper
# ---------------------------------------------------------------------------

async def _temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "test.db")
    await dbmod.init_db()


async def _make_job(external_id):
    await dbmod.upsert_job({
        "source": "greenhouse", "external_id": external_id, "title": f"Role {external_id}",
        "company": "Acme", "location": "Remote", "url": f"https://x/{external_id}",
    })
    jobs = (await dbmod.get_jobs(limit=100, status=None))["jobs"]
    return next(j["id"] for j in jobs if j["external_id"] == external_id)


async def _statuses_for(job_id):
    db = await dbmod._get_db()
    try:
        cur = await db.execute(
            "SELECT status FROM applications WHERE job_id = ? ORDER BY created_at", (job_id,))
        return sorted(r[0] for r in await cur.fetchall())
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_supersede_removes_only_pending_review_for_that_job(tmp_path, monkeypatch):
    await _temp_db(tmp_path, monkeypatch)
    job_a = await _make_job("a")
    job_b = await _make_job("b")

    # job_a: two stale drafts plus history that must survive.
    for status in ("pending_review", "pending_review", "applied", "failed", "paused"):
        app_id = await dbmod.create_application(job_id=job_a, resume_content="r", cover_letter_content="c")
        if status != "pending_review":
            await dbmod.update_application_status(app_id, status)
    # job_b: an unrelated draft.
    await dbmod.create_application(job_id=job_b, resume_content="r", cover_letter_content="c")

    removed = await dbmod.delete_pending_applications_for_job(job_a)

    assert removed == 2
    # 'paused' rows show in the pending-review queue but are user state, not a
    # superseded draft — they stay.
    assert await _statuses_for(job_a) == ["applied", "failed", "paused"]
    assert await _statuses_for(job_b) == ["pending_review"]


@pytest.mark.asyncio
async def test_supersede_on_a_clean_job_is_a_no_op(tmp_path, monkeypatch):
    await _temp_db(tmp_path, monkeypatch)
    job_a = await _make_job("a")
    assert await dbmod.delete_pending_applications_for_job(job_a) == 0


# ---------------------------------------------------------------------------
# 3. Startup dedupe migration
# ---------------------------------------------------------------------------

async def _seed_app(job_id, status, created_at, app_id=None):
    db = await dbmod._get_db()
    try:
        app_id = app_id or str(uuid.uuid4())
        await db.execute(
            "INSERT INTO applications (id, job_id, resume_content, cover_letter_content, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (app_id, job_id, "r", "c", status, created_at),
        )
        await db.commit()
        return app_id
    finally:
        await db.close()


async def _run_dedupe():
    db = await dbmod._get_db()
    try:
        return await dbmod._dedupe_pending_applications(db)
    finally:
        await db.close()


async def _ids_for(job_id):
    db = await dbmod._get_db()
    try:
        cur = await db.execute("SELECT id, status FROM applications WHERE job_id = ?", (job_id,))
        return {r[0]: r[1] for r in await cur.fetchall()}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_keeps_the_newest_pending_draft_per_job(tmp_path, monkeypatch):
    await _temp_db(tmp_path, monkeypatch)
    job_a = await _make_job("a")
    job_b = await _make_job("b")

    base = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    oldest = await _seed_app(job_a, "pending_review", base.isoformat())
    middle = await _seed_app(job_a, "pending_review", (base + timedelta(seconds=20)).isoformat())
    newest = await _seed_app(job_a, "pending_review", (base + timedelta(seconds=45)).isoformat())
    applied = await _seed_app(job_a, "applied", (base - timedelta(days=1)).isoformat())
    other = await _seed_app(job_b, "pending_review", base.isoformat())

    removed = await _run_dedupe()

    assert removed == 2, "should drop the two superseded drafts and nothing else"
    surviving_a = await _ids_for(job_a)
    assert set(surviving_a) == {newest, applied}, "kept the newest draft plus untouched history"
    assert oldest not in surviving_a and middle not in surviving_a
    assert set(await _ids_for(job_b)) == {other}, "another job's single draft is untouched"


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path, monkeypatch):
    await _temp_db(tmp_path, monkeypatch)
    job_a = await _make_job("a")
    base = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    for i in range(3):
        await _seed_app(job_a, "pending_review", (base + timedelta(seconds=i)).isoformat())

    assert await _run_dedupe() == 2
    before = await _ids_for(job_a)
    # Safe to run on every startup: a healthy database matches nothing.
    assert await _run_dedupe() == 0
    assert await _run_dedupe() == 0
    assert await _ids_for(job_a) == before


@pytest.mark.asyncio
async def test_startup_runs_the_dedupe(tmp_path, monkeypatch):
    """The repair has to be wired into init_db, not just callable."""
    await _temp_db(tmp_path, monkeypatch)
    job_a = await _make_job("a")
    base = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    for i in range(4):
        await _seed_app(job_a, "pending_review", (base + timedelta(seconds=i)).isoformat())

    await dbmod.init_db()

    assert len(await _ids_for(job_a)) == 1


@pytest.mark.asyncio
async def test_migration_leaves_a_job_with_only_history_alone(tmp_path, monkeypatch):
    await _temp_db(tmp_path, monkeypatch)
    job_a = await _make_job("a")
    base = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    for status in ("applied", "applied", "failed", "paused", "paused"):
        await _seed_app(job_a, status, base.isoformat())

    assert await _run_dedupe() == 0
    assert len(await _ids_for(job_a)) == 5
