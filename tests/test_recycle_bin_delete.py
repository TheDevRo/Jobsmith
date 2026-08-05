"""Recently Deleted — deleting a posting that is already soft-deleted.

Delete is a soft delete (status -> 'deleted'), so a second delete of the same
posting updates zero rows. That used to surface as a 404 "Job not found", which
the dashboard rendered as "Failed to delete job" — postings sitting in Recently
Deleted looked undeletable. DELETE is now idempotent: 404 is reserved for a row
that is genuinely gone, and erasing for good goes through purge-deleted.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend import database as dbmod
from backend.main import app


def _seed_job(path, job_id="job-1", status="discovered"):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """INSERT INTO jobs (id, source, external_id, title, company, url,
                                 date_discovered, status, apply_type)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (job_id, "greenhouse", job_id, "Engineer", "Acme",
             f"https://x/{job_id}", "2026-07-01T00:00:00Z", status, "external"),
        )
        conn.commit()
    finally:
        conn.close()


def _status(path, job_id):
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "jobsmith.db"
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    import asyncio

    asyncio.run(dbmod.init_db())
    return path


@pytest.fixture
def client():
    return TestClient(app)


def _seed_application(path, app_id, job_id, status):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO applications (id, job_id, status) VALUES (?,?,?)",
            (app_id, job_id, status),
        )
        conn.commit()
    finally:
        conn.close()


def test_first_delete_soft_deletes(client, db_path):
    _seed_job(db_path)
    resp = client.delete("/api/jobs/job-1")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1
    assert _status(db_path, "job-1") == "deleted"


def test_delete_reports_the_previous_status_for_undo(client, db_path):
    # The Pipeline's one-click delete PATCHes this back so the card returns to
    # the column it came from rather than to the Inbox.
    _seed_job(db_path, status="shortlisted")
    resp = client.delete("/api/jobs/job-1")
    assert resp.json()["previous_status"] == "shortlisted"

    undo = client.patch("/api/jobs/job-1/status", json={"status": "shortlisted"})
    assert undo.status_code == 200
    assert _status(db_path, "job-1") == "shortlisted"


def test_deleting_an_already_deleted_job_is_a_no_op_not_404(client, db_path):
    # The case behind the bug: the posting is in Recently Deleted and the user
    # hits Delete again (stale view, or the detail pane's Delete button).
    _seed_job(db_path, status="deleted")
    resp = client.delete("/api/jobs/job-1")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 0
    assert _status(db_path, "job-1") == "deleted"


def test_delete_of_a_missing_job_still_404s(client, db_path):
    resp = client.delete("/api/jobs/nope")
    assert resp.status_code == 404


def test_purge_deleted_erases_one_posting_by_id(client, db_path):
    # Erase Permanently / the recycle bin's per-row Erase button.
    _seed_job(db_path, job_id="gone", status="deleted")
    _seed_job(db_path, job_id="stays", status="deleted")
    resp = client.post("/api/jobs/purge-deleted", json={"job_ids": ["gone"]})
    assert resp.status_code == 200
    assert resp.json()["purged"] == 1
    assert _status(db_path, "gone") is None
    assert _status(db_path, "stays") == "deleted"


def test_purge_deleted_ignores_live_jobs(client, db_path):
    # Erase only ever applies to the recycle bin — a live posting passed by id
    # must survive.
    _seed_job(db_path, job_id="live", status="discovered")
    resp = client.post("/api/jobs/purge-deleted", json={"job_ids": ["live"]})
    assert resp.status_code == 200
    assert resp.json()["purged"] == 0
    assert _status(db_path, "live") == "discovered"


# ---------------------------------------------------------------------------
# Pipeline columns — an application whose job is deleted must leave the board.
# Without this the card stayed put and the posting looked undeletable.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "app_status,endpoint",
    [
        ("pending_review", "/api/applications/pending"),
        ("applied", "/api/applications/submitted"),
        ("failed", "/api/applications/failed"),
    ],
)
def test_deleting_a_job_clears_its_pipeline_card(client, db_path, app_status, endpoint):
    _seed_job(db_path, job_id="job-a", status="shortlisted")
    _seed_application(db_path, "app-a", "job-a", app_status)
    assert len(client.get(endpoint).json()) == 1

    client.delete("/api/jobs/job-a")
    assert client.get(endpoint).json() == []

    # Undo puts the card back where it was.
    client.patch("/api/jobs/job-a/status", json={"status": "shortlisted"})
    assert len(client.get(endpoint).json()) == 1
