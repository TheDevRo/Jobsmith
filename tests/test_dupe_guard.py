"""Duplicate-application guard: don't show me a role I already applied to.

The existing dedup only removes duplicates *within a single fetch*, and the DB
only hides the exact job row an application points at. A repost — or the same
role picked up from a second board — carries a different external_id and URL, so
it sails straight back into the inbox looking brand new. This is what catches it.
"""
import pytest

from backend import database as dbmod
from backend.job_sources import _flag_already_applied


async def _setup_temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "test.db")
    await dbmod.init_db()


async def _apply_to(source, external_id, title, company):
    await dbmod.upsert_job({
        "source": source, "external_id": external_id, "title": title,
        "company": company, "location": "Denver, CO", "url": f"https://x/{external_id}",
    })
    jobs = (await dbmod.get_jobs(limit=100))["jobs"]
    job = next(j for j in jobs if j["external_id"] == external_id and j["source"] == source)
    app_id = await dbmod.create_application(
        job_id=job["id"], resume_content="r", cover_letter_content="c")
    await dbmod.update_application_status(app_id, "applied")
    return job["id"]


def test_normalize_identity_ignores_punctuation_and_case():
    assert dbmod.normalize_identity("Senior  Engineer", "Acme, Inc.") == \
           dbmod.normalize_identity("senior engineer", "acme inc")
    # Location is deliberately not part of the key: the same role reposted in
    # another office is still a role you already applied to.
    assert dbmod.normalize_identity("Engineer", "") is None
    assert dbmod.normalize_identity("", "Acme") is None


@pytest.mark.asyncio
async def test_flags_a_repost_from_another_board(tmp_path, monkeypatch):
    await _setup_temp_db(tmp_path, monkeypatch)
    await _apply_to("greenhouse", "1", "Senior Backend Engineer", "Acme Corp")

    fetched = [
        # The same role, reposted on a different board with a new id and URL —
        # invisible to URL dedup and to the "hide the applied job row" rule.
        # Punctuation and spacing differ too, as they do in the wild.
        {"title": "Senior  Backend Engineer", "company": "Acme Corp.",
         "location": "Remote", "url": "https://linkedin/999"},
        {"title": "Platform Engineer", "company": "Globex", "location": "Remote",
         "url": "https://linkedin/1000"},
    ]
    out = await _flag_already_applied(fetched, {})
    assert [j.get("already_applied", False) for j in out] == [True, False]
    assert len(out) == 2  # flagged, not dropped, by default


@pytest.mark.asyncio
async def test_skip_already_applied_drops_them(tmp_path, monkeypatch):
    await _setup_temp_db(tmp_path, monkeypatch)
    await _apply_to("greenhouse", "1", "Senior Backend Engineer", "Acme Corp")

    fetched = [
        {"title": "Senior Backend Engineer", "company": "Acme Corp", "url": "https://x/9"},
        {"title": "Platform Engineer", "company": "Globex", "url": "https://x/10"},
    ]
    out = await _flag_already_applied(fetched, {"pipeline": {"skip_already_applied": True}})
    assert [j["title"] for j in out] == ["Platform Engineer"]


@pytest.mark.asyncio
async def test_get_jobs_badges_reposts_but_not_the_applied_row(tmp_path, monkeypatch):
    await _setup_temp_db(tmp_path, monkeypatch)
    applied_job_id = await _apply_to("greenhouse", "1", "Senior Backend Engineer", "Acme Corp")

    # A repost of the same role lands from another source.
    await dbmod.upsert_job({
        "source": "linkedin", "external_id": "2", "title": "Senior Backend Engineer",
        "company": "Acme Corp", "location": "Remote", "url": "https://linkedin/2",
    })
    # ...and an unrelated job.
    await dbmod.upsert_job({
        "source": "linkedin", "external_id": "3", "title": "Platform Engineer",
        "company": "Globex", "location": "Remote", "url": "https://linkedin/3",
    })

    jobs = {j["id"]: j for j in (await dbmod.get_jobs(limit=100))["jobs"]}
    repost = next(j for j in jobs.values() if j["external_id"] == "2")
    other = next(j for j in jobs.values() if j["external_id"] == "3")

    assert repost["already_applied"] is True
    assert other["already_applied"] is False
    # The application's own job row must not badge itself as a duplicate.
    if applied_job_id in jobs:
        assert jobs[applied_job_id]["already_applied"] is False


@pytest.mark.asyncio
async def test_guard_is_inert_with_no_applications(tmp_path, monkeypatch):
    await _setup_temp_db(tmp_path, monkeypatch)
    fetched = [{"title": "Engineer", "company": "Acme", "url": "https://x/1"}]
    out = await _flag_already_applied(fetched, {})
    assert out[0].get("already_applied") is None
