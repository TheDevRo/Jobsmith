"""
Tests for post-apply outcome tracking + outcome analytics.

Uses a temp SQLite db (monkeypatched DB_PATH) — same pattern as
tests/test_salary_estimator.py::test_include_estimated_filter_matches_estimates.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend import database as dbmod


async def _setup_temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "test.db")
    await dbmod.init_db()


async def _backdate_applied_at(app_id: str, days_ago: int) -> None:
    """Pretend the application was submitted `days_ago` days ago."""
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    db = await dbmod._get_db()
    try:
        await db.execute("UPDATE applications SET applied_at = ? WHERE id = ?", (when, app_id))
        await db.commit()
    finally:
        await db.close()


async def _backdate_event(app_id: str, to_outcome: str, days_ago: int) -> None:
    when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    db = await dbmod._get_db()
    try:
        await db.execute(
            "UPDATE application_events SET occurred_at = ? WHERE application_id = ? AND to_outcome = ?",
            (when, app_id, to_outcome),
        )
        await db.commit()
    finally:
        await db.close()


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


@pytest.mark.asyncio
async def test_outcome_transitions_are_recorded_as_events(tmp_path, monkeypatch):
    await _setup_temp_db(tmp_path, monkeypatch)
    app_id = await _make_applied_app(dbmod, "test", "1", 80, "honest")

    await dbmod.update_application_outcome(app_id, "screening")
    await dbmod.update_application_outcome(app_id, "interview")
    # Re-selecting the same outcome must not pad the history.
    await dbmod.update_application_outcome(app_id, "interview")

    events = await dbmod.get_application_events(app_id)
    assert [(e["from_outcome"], e["to_outcome"]) for e in events] == [
        ("awaiting", "screening"),
        ("screening", "interview"),
    ]
    assert all(e["source"] == "user" for e in events)


@pytest.mark.asyncio
async def test_funnel_keeps_stages_a_rejected_application_reached(tmp_path, monkeypatch):
    """The bug the event log fixes: an application that interviewed and was then
    rejected used to count only toward 'applied', understating every stage."""
    await _setup_temp_db(tmp_path, monkeypatch)
    app_id = await _make_applied_app(dbmod, "test", "1", 80, "honest")

    await dbmod.update_application_outcome(app_id, "screening")
    await dbmod.update_application_outcome(app_id, "interview")
    await dbmod.update_application_outcome(app_id, "rejected")

    data = await dbmod.get_outcome_analytics()
    funnel = {f["stage"]: f["count"] for f in data["funnel"]}
    assert funnel == {"applied": 1, "screening": 1, "interview": 1, "offer": 0}
    # Current outcome is still the denormalized truth, and a rejection is a response.
    assert data["outcome_counts"]["rejected"] == 1
    assert data["response_rate"]["overall"]["responded"] == 1


@pytest.mark.asyncio
async def test_funnel_implies_earlier_stages_when_user_skips_ahead(tmp_path, monkeypatch):
    await _setup_temp_db(tmp_path, monkeypatch)
    app_id = await _make_applied_app(dbmod, "test", "1", 80, "honest")

    # Straight to offer — no screening/interview ever recorded.
    await dbmod.update_application_outcome(app_id, "offer")

    funnel = {f["stage"]: f["count"] for f in (await dbmod.get_outcome_analytics())["funnel"]}
    assert funnel == {"applied": 1, "screening": 1, "interview": 1, "offer": 1}


@pytest.mark.asyncio
async def test_ghost_sweep_retires_silent_applications(tmp_path, monkeypatch):
    await _setup_temp_db(tmp_path, monkeypatch)
    stale = await _make_applied_app(dbmod, "test", "1", 80, "honest")
    fresh = await _make_applied_app(dbmod, "test", "2", 80, "honest")
    answered = await _make_applied_app(dbmod, "test", "3", 80, "honest")

    await _backdate_applied_at(stale, days_ago=30)
    await _backdate_applied_at(answered, days_ago=30)
    await dbmod.update_application_outcome(answered, "screening")

    # Only the silent, old one is retired — not the fresh one, not the answered one.
    assert await dbmod.mark_ghosted_applications(21) == [stale]

    apps = {a["id"]: a for a in await dbmod.get_submitted_applications()}
    assert apps[stale]["outcome"] == "no_response"
    assert apps[fresh]["outcome"] == "awaiting"
    assert apps[answered]["outcome"] == "screening"

    # The transition is attributed to the rule, not to the user.
    (event,) = await dbmod.get_application_events(stale)
    assert (event["from_outcome"], event["to_outcome"], event["source"]) == (
        "awaiting", "no_response", "rule",
    )

    # Idempotent: a second sweep has nothing left to do.
    assert await dbmod.mark_ghosted_applications(21) == []
    # 0 disables the sweep entirely.
    assert await dbmod.mark_ghosted_applications(0) == []


@pytest.mark.asyncio
async def test_stage_durations(tmp_path, monkeypatch):
    await _setup_temp_db(tmp_path, monkeypatch)
    a1 = await _make_applied_app(dbmod, "test", "1", 80, "honest")
    a2 = await _make_applied_app(dbmod, "test", "2", 80, "honest")

    # a1: applied 20d ago, screening 10d ago (10 days to respond)
    # a2: applied 20d ago, screening 14d ago (6 days to respond) -> median 8.0
    for app_id in (a1, a2):
        await _backdate_applied_at(app_id, days_ago=20)
        await dbmod.update_application_outcome(app_id, "screening")
    await _backdate_event(a1, "screening", days_ago=10)
    await _backdate_event(a2, "screening", days_ago=14)

    hops = {(h["from"], h["to"]): h for h in (await dbmod.get_outcome_analytics())["stage_durations"]}
    assert hops[("applied", "screening")]["samples"] == 2
    assert hops[("applied", "screening")]["median_days"] == 8.0
    # No interviews yet — reported as an empty sample, not a fabricated zero.
    assert hops[("screening", "interview")] == {
        "from": "screening", "to": "interview", "samples": 0, "median_days": None,
    }
