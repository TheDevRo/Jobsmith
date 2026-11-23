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


@pytest.mark.asyncio
async def test_new_entry_has_no_null_text_fields(tmp_path, monkeypatch):
    # The iOS app's applications table declares these NOT NULL; a synced NULL
    # would fail its whole import.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jobs.db")
    await db.init_db()
    job_id = await db.upsert_job({
        "source": "manual", "external_id": "null-test", "title": "Engineer",
        "company": "Acme", "location": "Remote", "url": "https://example.com/n",
    })

    await update_job_status(job_id, StatusUpdate(status="manual"))

    (app,) = await db.get_submitted_applications()
    assert app["resume_content"] == ""
    assert app["cover_letter_content"] == ""
    assert app["honesty_level"] == "honest"


@pytest.mark.asyncio
async def test_already_applied_job_is_not_duplicated_by_a_newer_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jobs.db")
    await db.init_db()
    job_id = await db.upsert_job({
        "source": "manual", "external_id": "dup-test", "title": "Engineer",
        "company": "Acme", "location": "Remote", "url": "https://example.com/d",
    })
    first = await db.create_application(job_id, "resume v1", "cover v1")
    await db.update_application_status(first, "applied")
    await db.create_application(job_id, "resume v2", "cover v2")

    await update_job_status(job_id, StatusUpdate(status="manual"))

    applied = await db.get_submitted_applications()
    assert [app["id"] for app in applied] == [first]


@pytest.mark.asyncio
async def test_application_mid_auto_apply_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jobs.db")
    await db.init_db()
    job_id = await db.upsert_job({
        "source": "manual", "external_id": "applying-test", "title": "Engineer",
        "company": "Acme", "location": "Remote", "url": "https://example.com/a",
    })
    app_id = await db.create_application(job_id, "resume", "cover")
    await db.update_application_status(app_id, "applying")

    await update_job_status(job_id, StatusUpdate(status="manual"))

    assert (await db.get_job(job_id))["status"] == "manual"
    assert await db.get_submitted_applications() == []
