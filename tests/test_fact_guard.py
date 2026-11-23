"""
tests/test_fact_guard.py

The fact guard flags names, numbers and credentials the candidate's own material
doesn't back. All text here is synthetic.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import database as dbmod
from backend import fact_guard as fg
from backend.main import app

PROFILE = {
    "full_name": "Sam Example",
    "summary": "Backend engineer who ships reliable services.",
    "skills": ["Python", "PostgreSQL", "CI/CD", "AWS"],
    "experience": [
        {"title": "Senior Engineer", "company": "Northwind Labs", "start_date": "2019-03",
         "end_date": "Present", "bullets": ["Cut deploy time by 40%", "Served 4,000 daily users",
                                              "Mentored 3 engineers"]},
        {"title": "Engineer", "company": "Contoso Works", "start_date": "2016-06",
         "end_date": "2019-02", "bullets": ["Built the billing service"]},
    ],
    "education": [{"degree": "B.S. Computer Science", "school": "Example State University", "year": "2016"}],
    "certifications": ["AWS Solutions Architect"],
    "facts": ["Speaks at regional meetups.", "Managed a budget of $2.5M"],
    "workday_password": "hunter2-secret-99",
}
JOB = {"title": "Platform Engineer", "company": "Fabrikam", "location": "Remote",
       "description": "Fabrikam needs a platform engineer. 5+ years of Python. Kubernetes a plus."}


def flags(text, level="honest", **kw):
    return fg.check(text, kw.pop("profile", PROFILE), kw.pop("job", JOB), level, **kw)


# --- numbers ----------------------------------------------------------------

class TestNumbers:
    def test_formatting_variants_are_the_same_number(self):
        for variant in ("4,000", "4000", "4k", "4K", "4 000", "4,000+"):
            assert flags(f"Served {variant} daily users.") == [], variant

    def test_percentages_and_plain_values(self):
        assert flags("Cut deploy time by 40%.") == []
        assert flags("Cut deploy time by 60%.") == ["60%"]

    def test_invented_metric_is_flagged(self):
        assert flags("Served 90,000 daily users.") == ["90,000"]

    def test_scaled_words(self):
        assert flags("Managed a $2.5 million budget.") == []
        assert flags("Managed a $2.5M budget.") == []
        assert flags("Managed a $9 million budget.") == ["9 million"]

    def test_number_words(self):
        assert flags("Mentored three engineers.") == []
        assert flags("Mentored seven engineers.") == ["seven"]
        assert flags("One of the best teams.") == []  # not a claim

    def test_numbers_from_the_posting_are_fine(self):
        assert flags("Over 5 years of Python.") == []

    def test_years_of_experience_the_profile_adds_up_to(self):
        # 2019-03..2026-11 = 7y8m, plus 2016-06..2019-02 = 2y8m -> 10 years
        sources = fg.build_sources(PROFILE, JOB, today=date(2026, 11, 1))
        assert fg.unverified_facts("10 years of experience", sources) == []
        assert fg.unverified_facts("15 years of experience", sources) == ["15"]

    def test_profile_dates_are_backed(self):
        assert flags("Dates: Mar 2019 - Present\nDates: Jun 2016 - Feb 2019") == []
        assert flags("Dates: 2011 - 2014") == ["2011", "2014"]


# --- names and credentials --------------------------------------------------

class TestNames:
    def test_profile_and_posting_names_are_fine(self):
        text = "Worked at Northwind Labs and Contoso Works; excited about Fabrikam."
        assert flags(text) == []

    def test_invented_employer_is_flagged(self):
        assert flags("I worked with engineers at Globex Corporation.") == ["Globex", "Corporation"]

    def test_sentence_starts_are_not_names(self):
        assert flags("Built the billing service. Reliable systems matter. Shipping is a habit.") == []
        assert flags("- Led the work\n- Improved the pipeline") == []

    def test_acronyms_are_checked_even_at_sentence_start(self):
        assert flags("AWS is my main cloud.") == []
        assert flags("PMP lead.") == ["PMP"]

    def test_credential_words(self):
        assert flags("Certified in the AWS Solutions Architect track.") == []  # profile lists a certification
        assert flags("Chartered engineer.") == ["Chartered"]
        assert flags("I am a licensed pilot.") == ["licensed"]
        no_certs = {**PROFILE, "certifications": []}
        assert flags("Certified in cloud.", profile=no_certs) == ["Certified"]

    def test_section_headers_and_labels_are_quiet(self):
        resume = (
            "SUMMARY\nBackend engineer.\n\nTECHNICAL SKILLS\nPython, AWS\n\nEXPERIENCE\n"
            "Title: Senior Engineer\nCompany: Northwind Labs\nDates: Mar 2019 - Present\n"
            "- Built the billing service\n\nEDUCATION\nDegree: B.S. Computer Science\n"
            "School: Example State University\nYear: 2016\n\nCERTIFICATIONS\n- AWS Solutions Architect\n"
        )
        assert flags(resume) == []

    def test_letter_furniture_is_quiet(self):
        letter = "Dear Hiring Manager,\n\nI am excited about Fabrikam.\n\nSincerely,\nSam Example"
        assert flags(letter) == []

    def test_possessive_and_months(self):
        assert flags("In March I joined Fabrikam's mission, and in May I left Northwind's team.") == []

    def test_each_item_reported_once_in_order(self):
        # The first capital starts the sentence, the repeats are one flag each.
        assert flags("Zeta hired 77 people for Zeta and Zeta, then 77 more.") == ["Zeta", "77"]


# --- sources ----------------------------------------------------------------

class TestSources:
    def test_profile_facts_and_application_facts_back_claims(self):
        assert flags("Spoke at regional meetups.") == []
        assert flags("Won the Quartz Award.") == ["Quartz", "Award"]
        assert flags("Won the Quartz Award in 2020.", app_facts=["Won the Quartz Award in 2020"]) == []

    def test_instructions_the_user_typed_count_as_sources(self):
        assert flags("Mentored 12 engineers.", extra=["Mention that I mentored 12 engineers"]) == []

    def test_secrets_never_become_sources(self):
        assert flags("My password is hunter2-secret-99.") != []

    def test_malformed_profile_does_not_raise(self):
        assert flags("Built things at Acme Corp.", profile={"experience": "oops", "facts": None}) == ["Acme", "Corp"]
        assert flags("Hello", profile=None, job=None) == []


# --- levels -----------------------------------------------------------------

@pytest.mark.parametrize("level,expected", [("honest", True), ("tailored", True),
                                            ("embellished", False), ("fabricated", False)])
def test_only_honest_and_tailored_are_checked(level, expected):
    assert bool(flags("Served 90,000 users at Globex.", level)) is expected


def test_unknown_level_is_not_checked_and_blank_text_is_clean():
    assert flags("Served 90,000 users.", "wizard") == []
    assert flags("   ") == []


def test_check_documents_shape():
    out = fg.check_documents("Served 90,000 users.", "I admire Fabrikam.", PROFILE, JOB, "honest")
    assert out == {"resume": ["90,000"], "cover_letter": []}
    assert fg.check_documents("Served 90,000 users.", "x", PROFILE, JOB, "fabricated") == {"resume": [], "cover_letter": []}


def test_a_guard_failure_is_swallowed():
    with patch.object(fg, "build_sources", side_effect=RuntimeError("boom")):
        assert fg.check("Served 90,000 users.", PROFILE, JOB, "honest") == []


# --- wiring: /revise, PATCH /content, stored flags --------------------------

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "test.db")
    asyncio.run(dbmod.init_db())


@pytest.fixture
def cfg(monkeypatch):
    from backend import app_state as state
    monkeypatch.setattr(state, "load_config", lambda: {"profile": PROFILE, "application_honesty": {}})


async def _make_app(honesty="honest"):
    await dbmod.upsert_job({"source": "t", "external_id": "e1", "title": "Platform Engineer",
                            "company": "Fabrikam", "location": "Remote", "url": "https://x/1",
                            "description": JOB["description"]})
    job_id = (await dbmod.get_jobs(limit=5, status=None))["jobs"][0]["id"]
    return await dbmod.create_application(job_id, "SUMMARY\nBuilt the billing service.", "Dear Hiring Manager",
                                          honesty_level=honesty)


def _flags_in_db(app_id):
    import json
    import sqlite3
    conn = sqlite3.connect(dbmod.DB_PATH)
    raw = conn.execute("SELECT fact_flags FROM applications WHERE id = ?", (app_id,)).fetchone()[0]
    conn.close()
    return json.loads(raw) if raw else None


def test_revise_returns_unverified_terms(tmp_db, cfg):
    app_id = asyncio.run(_make_app())

    async def fake(current, instr, job, profile, config, **kw):
        return "Served 90,000 users at Globex."

    with patch("backend.routers.applications.ai_engine.revise_tailored_resume", fake):
        r = TestClient(app).post(f"/api/applications/{app_id}/revise",
                                 json={"target": "resume", "instructions": "tighten"})
    assert r.status_code == 200
    assert r.json()["unverified"] == ["Globex", "90,000"]


def test_revise_at_embellished_level_is_not_flagged(tmp_db, cfg):
    app_id = asyncio.run(_make_app())

    async def fake(current, instr, job, profile, config, **kw):
        return "Served 90,000 users at Globex."

    with patch("backend.routers.applications.ai_engine.revise_tailored_resume", fake):
        r = TestClient(app).post(f"/api/applications/{app_id}/revise",
                                 json={"target": "resume", "instructions": "x", "honesty_level": "embellished"})
    assert r.json()["unverified"] == []


def test_saving_content_refreshes_the_stored_flags(tmp_db, cfg):
    app_id = asyncio.run(_make_app())
    with patch("backend.routers.applications.resume_generator.generate_resume"), \
         patch("backend.routers.applications.resume_generator.generate_cover_letter"):
        r = TestClient(app).patch(f"/api/applications/{app_id}/content", json={
            "resume_content": "Served 90,000 users.", "cover_letter_content": "I admire Fabrikam."})
    assert r.status_code == 200
    assert _flags_in_db(app_id) == {"resume": ["90,000"], "cover_letter": []}


def test_saving_at_embellished_level_stores_no_flags(tmp_db, cfg):
    app_id = asyncio.run(_make_app())
    with patch("backend.routers.applications.resume_generator.generate_resume"), \
         patch("backend.routers.applications.resume_generator.generate_cover_letter"):
        TestClient(app).patch(f"/api/applications/{app_id}/content", json={
            "resume_content": "Served 90,000 users.", "cover_letter_content": "x",
            "honesty_level": "embellished"})
    assert _flags_in_db(app_id) == {"resume": [], "cover_letter": []}


def test_migration_adds_fact_flags_column(tmp_db):
    import sqlite3
    conn = sqlite3.connect(dbmod.DB_PATH)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(applications)")]
    conn.close()
    assert "fact_flags" in cols


def test_tailoring_stores_flags_and_carries_facts_to_the_new_draft(tmp_db, monkeypatch):
    from unittest.mock import AsyncMock
    from backend import app_state as state
    from backend import background_tasks as bg

    monkeypatch.setattr(state, "load_config", lambda: {
        "profile": PROFILE, "application_honesty": {"honesty_level": "honest"},
        "salary_estimator": {"enabled": False}})
    old_app = asyncio.run(_make_app())
    asyncio.run(dbmod.add_application_fact(old_app, "Keep: prefers small teams."))
    job_id = (asyncio.run(dbmod.get_jobs(limit=5, status=None))["jobs"][0]["id"])
    seen = {}

    async def fake_resume(job, profile, cfg, level, match_report=None, app_facts=None):
        seen["facts"] = app_facts
        return "SUMMARY\nServed 90,000 users."

    async def fake_cover(job, profile, cfg, level, app_facts=None):
        return "Dear Hiring Manager"

    monkeypatch.setattr(bg.ai_engine, "score_job_fit", AsyncMock(return_value=(80, "ok", {})))
    monkeypatch.setattr(bg.ai_engine, "generate_tailored_resume", fake_resume)
    monkeypatch.setattr(bg.ai_engine, "generate_cover_letter", fake_cover)
    monkeypatch.setattr(bg.ai_engine, "generate_embellishment_log", AsyncMock(return_value={}))
    monkeypatch.setattr(bg.resume_generator, "generate_resume", lambda *a, **k: None)
    monkeypatch.setattr(bg.resume_generator, "generate_cover_letter", lambda *a, **k: None)

    asyncio.run(bg._bg_tailor_job(job_id))

    assert seen["facts"] == ["Keep: prefers small teams."]
    new_app = asyncio.run(dbmod.get_latest_application_id(job_id))
    assert new_app != old_app
    assert [f["text"] for f in asyncio.run(dbmod.get_application_facts(new_app))] == ["Keep: prefers small teams."]
    assert _flags_in_db(new_app) == {"resume": ["90,000"], "cover_letter": []}
