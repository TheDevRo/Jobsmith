"""Requirement coverage: the tolerant parser, NLI-derived coverage, the jobs.coverage migration, the API,
the 'Best coverage' sort, the dismiss action and the prompt-override fallback."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from backend import ai_engine, coverage
from backend import database as dbmod
from backend.main import app
from backend.nli import fit

GOOD = {"score": 70, "reasoning": "ok", "matched_skills": ["Python"], "requirements": [
    {"text": "5+ years of Python", "kind": "must", "met": True, "evidence": "Data Engineer at Acme, 2018-present"},
    {"text": "Airflow experience", "kind": "nice", "met": False, "evidence": None},
    {"text": "Bachelor's degree", "kind": "must", "met": False},
]}


# -- parser -----------------------------------------------------------------

def test_parse_requirements_good():
    cov = coverage.parse_requirements(json.dumps(GOOD))
    assert (cov["covered"], cov["total"], cov["missing_must"]) == (1, 3, 1)
    assert cov["requirements"][0] == {"text": "5+ years of Python", "kind": "must", "met": True,
                                      "evidence": "Data Engineer at Acme, 2018-present"}
    assert cov["requirements"][2]["evidence"] is None


def test_parse_requirements_survives_prose_and_code_fences():
    assert coverage.parse_requirements("Here you go:\n```json\n" + json.dumps(GOOD) + "\n```")["total"] == 3


def test_parse_requirements_partial_keeps_usable_items():
    data = {"requirements": [
        {"text": "Good one", "kind": "MUST", "met": True},
        {"text": "No met flag", "kind": "must"},
        {"text": "met is a string", "met": "yes"},
        {"text": "", "met": False},
        "not a dict",
        {"text": "Unknown kind", "kind": "banana", "met": False, "evidence": 5},
    ]}
    cov = coverage.parse_requirements(json.dumps(data))
    assert [(r["text"], r["kind"], r["met"], r["evidence"]) for r in cov["requirements"]] == [
        ("Good one", "must", True, None), ("Unknown kind", "nice", False, None)]


@pytest.mark.parametrize("text", [
    "", "no json at all", "{broken", "[]", "null", '{"score": 50}', '{"requirements": "none"}',
    '{"requirements": []}', '{"requirements": [1, 2]}', '{"requirements": {"a": 1}}',
])
def test_parse_requirements_garbage_is_none(text):
    assert coverage.parse_requirements(text) is None


def test_parse_requirements_never_raises_on_non_strings():
    assert coverage.parse_requirements(None) is None


def test_parse_requirements_caps_the_list():
    data = {"requirements": [{"text": f"req {i}", "met": True} for i in range(50)]}
    assert coverage.parse_requirements(json.dumps(data))["total"] == coverage.MAX_REQUIREMENTS


def test_parse_score_response_is_unchanged_by_requirements():
    score, reasoning, report, method = ai_engine.parse_score_response(json.dumps(GOOD))
    assert (score, reasoning, method) == (70.0, "ok", "json")
    assert "requirements" not in report and "coverage" not in report


# -- NLI-derived coverage ---------------------------------------------------

def test_from_nli_threshold_and_kind():
    lines = ["Must have 5 years of Python", "Experience with Airflow is a plus", "Minimum of a Bachelor's degree",
             "Familiar with dbt and similar tools"]
    cov = coverage.from_nli(lines, [0.5, 0.49, 0.2, 0.9])
    assert [(r["kind"], r["met"]) for r in cov["requirements"]] == [
        ("must", True), ("nice", False), ("must", False), ("nice", True)]
    assert (cov["covered"], cov["total"], cov["missing_must"]) == (2, 4, 1)
    assert coverage.from_nli([], []) is None


def test_fit_score_reports_coverage_from_mocked_probabilities():
    class Fake:
        def entail(self, pairs):
            return [0.9 if "Python" in h else 0.1 for _, h in pairs]
    job = {"title": "Data Engineer", "description": "- 5+ years of experience with Python and SQL\n"
           "- Bachelor's degree in Computer Science or similar\n- Experience with Airflow is a plus\nWe offer snacks."}
    profile = {"summary": "Data engineer.", "skills": ["Python"], "experience": [], "education": []}
    _, _, report = fit.score(job, profile, Fake())
    cov = report["coverage"]
    assert [r["met"] for r in cov["requirements"]] == [True, False, False]
    assert cov["requirements"][0]["kind"] == "must" and cov["requirements"][2]["kind"] == "nice"
    assert cov["covered"] == 1 and cov["total"] == 3


# -- LLM path and prompt overrides -----------------------------------------

def _llm(monkeypatch, text):
    msg = MagicMock(content=text)
    resp = MagicMock(choices=[MagicMock(message=msg)])
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=resp)
    monkeypatch.setattr(ai_engine, "_get_client", lambda *a, **k: client)
    monkeypatch.setattr(ai_engine, "_model", lambda *a, **k: "m")
    return client


JOB = {"title": "Analyst", "company": "Acme", "description": "d"}


def test_default_prompt_asks_for_requirements_and_llm_coverage_is_returned(monkeypatch):
    client = _llm(monkeypatch, json.dumps(GOOD))
    score, _, report = asyncio.run(ai_engine.score_job_fit(JOB, {"full_name": "A"}, {"ai": {}}))
    sent = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert '"requirements"' in sent
    assert score == 70 and report["coverage"]["covered"] == 1


def test_old_prompt_override_without_requirements_still_scores(monkeypatch):
    _llm(monkeypatch, json.dumps({"score": 50, "reasoning": "ok", "matched_skills": ["Python"]}))
    cfg = {"ai": {}, "prompts": {"score_job_fit": "Old override for {job_title}. Return score JSON."}}
    score, reasoning, report = asyncio.run(ai_engine.score_job_fit(JOB, {"full_name": "A"}, cfg))
    assert (score, reasoning) == (50, "ok")
    assert "coverage" not in report


def test_bare_score_override_has_no_report_or_coverage(monkeypatch):
    _llm(monkeypatch, json.dumps({"score": 50, "reasoning": "ok"}))
    cfg = {"ai": {}, "prompts": {"score_job_fit": "CUSTOM {job_title}"}}
    assert asyncio.run(ai_engine.score_job_fit(JOB, {"full_name": "A"}, cfg)) == (50, "ok", None)


def test_requirements_without_skill_lists_still_yield_coverage(monkeypatch):
    _llm(monkeypatch, json.dumps({"score": 40, "reasoning": "r", "requirements": GOOD["requirements"]}))
    _, _, report = asyncio.run(ai_engine.score_job_fit(JOB, {"full_name": "A"}, {"ai": {}}))
    assert set(report) == {"coverage"}


# -- database: migration, storage, sort, API --------------------------------

@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "jobsmith.db"
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    asyncio.run(dbmod.init_db())
    return path


@pytest.fixture
def client():
    return TestClient(app)


def _seed(path, job_id, score, cov=None):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO jobs (id, source, external_id, title, company, url, date_discovered, status, apply_type,"
        " fit_score, coverage) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, "greenhouse", job_id, job_id, "Acme", f"https://x/{job_id}", "2026-07-01T00:00:00Z",
         "discovered", "external", score, json.dumps(cov) if cov else None))
    conn.commit()
    conn.close()


def _cov(met_must, total_must, met_nice, total_nice):
    reqs = ([{"text": f"m{i}", "kind": "must", "met": i < met_must, "evidence": None} for i in range(total_must)]
            + [{"text": f"n{i}", "kind": "nice", "met": i < met_nice, "evidence": None} for i in range(total_nice)])
    return coverage.build(reqs)


def test_migration_adds_coverage_column_to_an_existing_db(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    asyncio.run(dbmod.init_db())
    conn = sqlite3.connect(path)  # rewind to a database from before the migration
    conn.execute("ALTER TABLE jobs DROP COLUMN coverage")
    conn.execute("DELETE FROM schema_version WHERE version >= 30")
    conn.commit()
    conn.close()
    _seed_old = sqlite3.connect(path)
    _seed_old.execute("INSERT INTO jobs (id, source, external_id, title, url, status, apply_type, fit_score)"
                      " VALUES ('j', 's', 'j', 'T', 'u', 'discovered', 'external', 55)")
    _seed_old.commit()
    _seed_old.close()
    asyncio.run(dbmod.init_db())
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT fit_score, coverage FROM jobs WHERE id = 'j'").fetchone() == (55, None)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == dbmod.SCHEMA_MIGRATIONS[-1][0]
    conn.close()


def test_update_job_score_stores_coverage_in_its_own_column(db_path, client):
    _seed(db_path, "a", None)
    cov = _cov(1, 2, 1, 1)
    asyncio.run(dbmod.update_job_score("a", 60, "r", {"matched_skills": ["Python"], "coverage": cov}))
    job = client.get("/api/jobs/a").json()
    assert json.loads(job["coverage"]) == cov
    assert "coverage" not in json.loads(job["match_report"])
    row = client.get("/api/jobs?limit=5").json()["jobs"][0]
    assert json.loads(row["coverage"])["covered"] == 2
    asyncio.run(dbmod.update_job_score("a", 61, "r", None))  # a fresh score without coverage clears the old one
    assert client.get("/api/jobs/a").json()["coverage"] is None


def test_best_coverage_sort_order(db_path, client):
    _seed(db_path, "half", 99, _cov(1, 1, 0, 1))           # 1/2
    _seed(db_path, "full_nice_gap", 10, _cov(2, 2, 1, 2))  # 3/4 = 0.75
    _seed(db_path, "three_quarters_more_missing_must", 50, _cov(1, 2, 2, 2))   # 3/4, 1 missing must
    _seed(db_path, "three_quarters_hi_score", 90, _cov(2, 2, 1, 2))
    _seed(db_path, "all", 5, _cov(1, 1, 1, 1))             # 2/2
    _seed(db_path, "none", 100)                            # no coverage sinks
    ids = [j["id"] for j in client.get("/api/jobs?sort_by=best_coverage").json()["jobs"]]
    assert ids == ["all", "three_quarters_hi_score", "full_nice_gap",
                   "three_quarters_more_missing_must", "half", "none"]
    # the default sort and the score sort are untouched
    assert client.get("/api/jobs?sort_by=fit_score").json()["jobs"][0]["id"] == "none"


def test_dismiss_marks_a_known_gap_and_persists(db_path, client):
    _seed(db_path, "a", 50, _cov(0, 1, 1, 1))
    r = client.post("/api/jobs/a/coverage/dismiss", json={"text": "m0"})
    assert r.status_code == 200
    stored = json.loads(client.get("/api/jobs/a").json()["coverage"])
    assert stored["requirements"][0]["dismissed"] is True
    assert "dismissed" not in stored["requirements"][1]
    assert (stored["covered"], stored["total"], stored["missing_must"]) == (1, 2, 1)  # counts stay honest


def test_dismiss_rejects_met_unknown_and_coverageless(db_path, client):
    _seed(db_path, "a", 50, _cov(0, 1, 1, 1))
    _seed(db_path, "b", 50)
    assert client.post("/api/jobs/a/coverage/dismiss", json={"text": "n0"}).status_code == 404  # already met
    assert client.post("/api/jobs/a/coverage/dismiss", json={"text": "zzz"}).status_code == 404
    assert client.post("/api/jobs/b/coverage/dismiss", json={"text": "m0"}).status_code == 404
    assert client.post("/api/jobs/nope/coverage/dismiss", json={"text": "m0"}).status_code == 404
