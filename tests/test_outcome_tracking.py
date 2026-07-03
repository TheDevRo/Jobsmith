"""
Tests for post-apply outcome tracking + outcome analytics.

Uses a temp SQLite db (monkeypatched DB_PATH) — same pattern as
tests/test_salary_estimator.py::test_include_estimated_filter_matches_estimates.
"""

import pytest

from backend import database as dbmod


async def _setup_temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "test.db")
    await dbmod.init_db()


async def _make_applied_app(dbmod, source, external_id, fit_score, honesty_level):
    """Insert a job + application and mark the application applied."""
    await dbmod.upsert_job({
        "source": source, "external_id": external_id,
        "title": f"Job {external_id}", "company": "Acme",
        "location": "Remote", "url": f"https://x/{external_id}",
    })
    jobs = (await dbmod.get_jobs(limit=100))["jobs"]
    job = next(j for j in jobs if j["external_id"] == external_id and j["source"] == source)
    if fit_score is not None:
        await dbmod.update_job_score(job["id"], fit_score, "test")
    app_id = await dbmod.create_application(
        job_id=job["id"],
        resume_content="resume",
        cover_letter_content="cl",
        honesty_level=honesty_level,
    )
    await dbmod.update_application_status(app_id, "applied")
    return app_id


@pytest.mark.asyncio
async def test_update_outcome_validates_and_stamps(tmp_path, monkeypatch):
    await _setup_temp_db(tmp_path, monkeypatch)
    app_id = await _make_applied_app(dbmod, "test", "1", 80, "honest")

    # Invalid outcome rejected
    with pytest.raises(ValueError):
        await dbmod.update_application_outcome(app_id, "hired")

    # Unknown app id returns False
    assert await dbmod.update_application_outcome("nope", "interview") is False

    # Valid outcome persists and stamps outcome_updated_at
    assert await dbmod.update_application_outcome(app_id, "interview") is True
    apps = await dbmod.get_submitted_applications()
    assert len(apps) == 1
    assert apps[0]["outcome"] == "interview"
    assert apps[0]["outcome_updated_at"] is not None
    assert apps[0]["honesty_level"] == "honest"


@pytest.mark.asyncio
async def test_outcome_analytics_empty(tmp_path, monkeypatch):
    await _setup_temp_db(tmp_path, monkeypatch)
    data = await dbmod.get_outcome_analytics()
    assert data["total_applied"] == 0
    assert data["response_rate"]["overall"] == {"total": 0, "responded": 0, "rate": 0.0}
    assert data["funnel"] == [
        {"stage": "applied", "count": 0},
        {"stage": "screening", "count": 0},
        {"stage": "interview", "count": 0},
        {"stage": "offer", "count": 0},
    ]
    assert data["response_rate"]["by_source"] == []


@pytest.mark.asyncio
async def test_outcome_analytics_aggregates(tmp_path, monkeypatch):
    await _setup_temp_db(tmp_path, monkeypatch)

    # 4 applied apps: linkedin/high-fit/honest -> interview,
    # linkedin/high-fit/honest -> offer, indeed/low-fit/tailored -> no_response,
    # indeed/unscored/None -> awaiting (default)
    a1 = await _make_applied_app(dbmod, "linkedin", "1", 85, "honest")
    a2 = await _make_applied_app(dbmod, "linkedin", "2", 72, "honest")
    a3 = await _make_applied_app(dbmod, "indeed", "3", 30, "tailored")
    await _make_applied_app(dbmod, "indeed", "4", None, None)

    await dbmod.update_application_outcome(a1, "interview")
    await dbmod.update_application_outcome(a2, "offer")
    await dbmod.update_application_outcome(a3, "no_response")

    data = await dbmod.get_outcome_analytics()
    assert data["total_applied"] == 4
    assert data["outcome_counts"]["interview"] == 1
    assert data["outcome_counts"]["offer"] == 1
    assert data["outcome_counts"]["no_response"] == 1
    assert data["outcome_counts"]["awaiting"] == 1

    # Funnel: reached-at-least semantics
    funnel = {f["stage"]: f["count"] for f in data["funnel"]}
    assert funnel == {"applied": 4, "screening": 2, "interview": 2, "offer": 1}

    # Overall response rate: 2 of 4 responded (interview + offer)
    overall = data["response_rate"]["overall"]
    assert overall == {"total": 4, "responded": 2, "rate": 50.0}

    # By source
    by_source = {r["key"]: r for r in data["response_rate"]["by_source"]}
    assert by_source["linkedin"]["rate"] == 100.0
    assert by_source["indeed"]["rate"] == 0.0

    # By fit band (incl. unscored bucket)
    by_band = {r["key"]: r for r in data["response_rate"]["by_fit_band"]}
    assert by_band["70-100"] == {"key": "70-100", "total": 2, "responded": 2, "rate": 100.0}
    assert by_band["0-39"]["responded"] == 0
    assert by_band["unscored"]["total"] == 1

    # By honesty level (None -> 'unknown')
    by_honesty = {r["key"]: r for r in data["response_rate"]["by_honesty"]}
    assert by_honesty["honest"]["rate"] == 100.0
    assert by_honesty["tailored"]["rate"] == 0.0
    assert by_honesty["unknown"]["total"] == 1
