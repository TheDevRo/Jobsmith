"""A job marked applied in Inbox must appear in Pipeline's Applied view."""

import pytest

from backend import database as db
from backend.routers.jobs import StatusUpdate, update_job_status


@pytest.mark.asyncio
async def test_mark_applied_without_tailoring_creates_pipeline_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jobs.db")
    await db.init_db()
    job_id = await db.upsert_job({
        "source": "manual", "external_id": "vanta-test", "title": "Senior Software Engineer",
        "company": "Vanta", "location": "Remote", "url": "https://example.com/vanta",
    })

    await update_job_status(job_id, StatusUpdate(status="manual"))

    job = await db.get_job(job_id)
    applied = await db.get_submitted_applications()
    assert job["status"] == "manual"
    assert [app["job_id"] for app in applied] == [job_id]
    assert applied[0]["status"] == "applied"
    assert applied[0]["applied_at"] is not None

    await update_job_status(job_id, StatusUpdate(status="manual"))
    assert len(await db.get_submitted_applications()) == 1


@pytest.mark.asyncio
async def test_mark_applied_reuses_tailored_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jobs.db")
    await db.init_db()
    job_id = await db.upsert_job({
        "source": "manual", "external_id": "draft-test", "title": "Engineer",
        "company": "Acme", "location": "Remote", "url": "https://example.com/acme",
    })
    app_id = await db.create_application(job_id, "tailored resume", "cover letter")

    await update_job_status(job_id, StatusUpdate(status="manual"))

    applied = await db.get_submitted_applications()
    assert len(applied) == 1
    assert applied[0]["id"] == app_id
    assert applied[0]["resume_content"] == "tailored resume"
