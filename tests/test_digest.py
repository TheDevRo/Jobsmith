"""Today's shortlist: a ranked handful of jobs actually worth applying to.

The part that matters is the conversion term — each source is weighted by how
often it has actually replied to *you*, measured from the outcome event history.
That is what makes the digest a strategy tool rather than another sort order.
"""
from datetime import datetime, timedelta, timezone

import pytest

from backend import database as dbmod


async def _setup_temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "test.db")
    await dbmod.init_db()


async def _add_job(source, external_id, *, score=None, easy=False, salary=None,
                   posted=None, title=None):
    await dbmod.upsert_job({
        "source": source, "external_id": external_id,
        "title": title or f"Engineer {external_id}", "company": f"Co{external_id}",
        "location": "Remote", "url": f"https://x/{source}/{external_id}",
        "is_easy_apply": easy, "salary_max": salary,
        "date_posted": posted or datetime.now(timezone.utc).isoformat(),
    })
    jobs = (await dbmod.get_jobs(limit=200))["jobs"]
    job = next(j for j in jobs if j["source"] == source and j["external_id"] == external_id)
    if score is not None:
        await dbmod.update_job_score(job["id"], score, "test")
    return job["id"]


async def _apply_with_outcome(source, external_id, outcome):
    job_id = await _add_job(source, external_id, score=50)
    app_id = await dbmod.create_application(
        job_id=job_id, resume_content="r", cover_letter_content="c")
    await dbmod.update_application_status(app_id, "applied")
    if outcome:
        await dbmod.update_application_outcome(app_id, outcome)


@pytest.mark.asyncio
async def test_digest_excludes_unscored_and_already_applied(tmp_path, monkeypatch):
    await _setup_temp_db(tmp_path, monkeypatch)
    good = await _add_job("greenhouse", "1", score=90)
    await _add_job("greenhouse", "2")  # unscored — not a candidate
    await _apply_with_outcome("greenhouse", "3", None)  # already applied

    digest = await dbmod.get_digest(limit=5)
    assert [j["id"] for j in digest["jobs"]] == [good]


@pytest.mark.asyncio
async def test_digest_ranks_fit_freshness_salary_and_effort(tmp_path, monkeypatch):
    await _setup_temp_db(tmp_path, monkeypatch)
    old = (datetime.now(timezone.utc) - timedelta(days=25)).isoformat()

    strong = await _add_job("greenhouse", "1", score=95, easy=True, salary=200000)
    weak = await _add_job("greenhouse", "2", score=40, salary=80000, posted=old)

    digest = await dbmod.get_digest(limit=5)
    assert [j["id"] for j in digest["jobs"]] == [strong, weak]
    top = digest["jobs"][0]
    assert top["components"]["fit"] == 0.95
    assert top["components"]["effort"] == 1.0
    assert top["components"]["salary"] == 1.0        # best-paying in the set
    assert top["components"]["freshness"] > 0.9      # posted today
    # The ranking is interrogable — every term is reported, not just the total.
    assert set(top["components"]) == {"fit", "freshness", "salary", "effort", "conversion"}


@pytest.mark.asyncio
async def test_a_source_that_never_replies_gets_buried(tmp_path, monkeypatch):
    """The whole point: a board that has never once responded to you stops
    outranking one that actually converts, even at equal fit."""
    await _setup_temp_db(tmp_path, monkeypatch)

    # linkedin: 3 applications, no response. greenhouse: 3 applications, all replied.
    for i in range(3):
        await _apply_with_outcome("linkedin", f"dead{i}", "no_response")
        await _apply_with_outcome("greenhouse", f"live{i}", "interview")

    # Two identical candidate jobs, one from each source.
    dead = await _add_job("linkedin", "cand", score=80)
    live = await _add_job("greenhouse", "cand", score=80)

    digest = await dbmod.get_digest(limit=5)
    assert digest["conversion_by_source"] == {"linkedin": 0.0, "greenhouse": 1.0}
    assert [j["id"] for j in digest["jobs"]] == [live, dead]

    picks = {j["id"]: j for j in digest["jobs"]}
    assert picks[live]["components"]["conversion"] == 1.0
    assert picks[dead]["components"]["conversion"] == 0.0
    assert picks[live]["score"] > picks[dead]["score"]


@pytest.mark.asyncio
async def test_thin_history_stays_neutral(tmp_path, monkeypatch):
    """One silent application is not evidence a board is bad. Below the sample
    threshold the conversion term sits neutral rather than confidently wrong."""
    await _setup_temp_db(tmp_path, monkeypatch)
    await _apply_with_outcome("linkedin", "one", "no_response")

    candidate = await _add_job("linkedin", "cand", score=80)
    digest = await dbmod.get_digest(limit=5)

    assert digest["conversion_by_source"] == {}  # too thin to judge
    pick = next(j for j in digest["jobs"] if j["id"] == candidate)
    assert pick["components"]["conversion"] == 0.5


@pytest.mark.asyncio
async def test_weights_are_overridable(tmp_path, monkeypatch):
    await _setup_temp_db(tmp_path, monkeypatch)
    easy_low_fit = await _add_job("greenhouse", "1", score=50, easy=True)
    hard_high_fit = await _add_job("greenhouse", "2", score=90)

    # Default weighting favours fit.
    assert (await dbmod.get_digest())["jobs"][0]["id"] == hard_high_fit
    # Crank apply-effort and the cheap shot on goal wins instead.
    weighted = await dbmod.get_digest(weights={"fit": 0.1, "effort": 5.0})
    assert weighted["jobs"][0]["id"] == easy_low_fit
