"""
tests/test_application_facts.py

Persistent "facts": profile-wide (config.yaml profile.facts) and per
application (application_facts table). CRUD + limits, the migration on an
existing database, and injection into every writing prompt.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import app_state as state
from backend import database as dbmod
from backend import facts as facts_mod
from backend.ai_engine import (
    generate_cover_letter,
    generate_tailored_resume,
    revise_cover_letter,
    revise_tailored_resume,
)
from backend.main import app

JOB = {"title": "Engineer", "company": "Example Co", "description": "Build things."}
PROFILE = {"full_name": "Test Person", "summary": "Synthetic summary.", "skills": ["Python"],
           "experience": [], "education": [], "certifications": []}
CONFIG = {"ai": {"base_url": "http://localhost:1234/v1", "api_key": "k", "model": "m"}}


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "test.db")
    asyncio.run(dbmod.init_db())
    return tmp_path / "test.db"


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"profile": {"full_name": "Test Person"}}))
    monkeypatch.setattr(state, "CONFIG_PATH", path)
    state._config_cache = None
    return path


async def _amake_app(job_ext="j1"):
    await dbmod.upsert_job({"source": "test", "external_id": job_ext, "title": "T",
                            "company": "C", "location": "Remote", "url": "https://x/1"})
    jobs = (await dbmod.get_jobs(limit=10, status=None))["jobs"]
    job_id = next(j["id"] for j in jobs if j["external_id"] == job_ext)
    return job_id, await dbmod.create_application(job_id, "RESUME", "LETTER")


def _make_app(job_ext="j1"):
    return asyncio.run(_amake_app(job_ext))


# --- migration --------------------------------------------------------------

def test_migration_adds_table_to_an_existing_database(tmp_db):
    """A DB at the previous schema version gains application_facts on next init."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.execute("DROP TABLE application_facts")
    conn.execute("DELETE FROM schema_version WHERE version >= 30")
    conn.commit()
    conn.close()

    asyncio.run(dbmod.init_db())

    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT COUNT(*) FROM application_facts").fetchone() == (0,)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 31
    conn.close()


# --- application facts API --------------------------------------------------

class TestApplicationFactsApi:
    def test_crud(self, client, tmp_db):
        _, app_id = _make_app()
        r = client.post(f"/api/applications/{app_id}/facts", json={"text": "  Led a team of five.  "})
        assert r.status_code == 201
        fact = r.json()
        assert fact["text"] == "Led a team of five."

        listed = client.get(f"/api/applications/{app_id}/facts").json()
        assert listed["facts"] == [fact]
        assert listed["max_facts"] == 50 and listed["max_chars"] == 1000

        r = client.patch(f"/api/applications/{app_id}/facts/{fact['id']}", json={"text": "Edited"})
        assert r.status_code == 200
        assert client.get(f"/api/applications/{app_id}/facts").json()["facts"][0]["text"] == "Edited"

        assert client.delete(f"/api/applications/{app_id}/facts/{fact['id']}").status_code == 200
        assert client.get(f"/api/applications/{app_id}/facts").json()["facts"] == []
        assert client.delete(f"/api/applications/{app_id}/facts/{fact['id']}").status_code == 404

    def test_validation_errors_are_400_with_a_reason(self, client, tmp_db):
        _, app_id = _make_app()
        r = client.post(f"/api/applications/{app_id}/facts", json={"text": "   "})
        assert r.status_code == 400 and "empty" in r.json()["detail"]
        r = client.post(f"/api/applications/{app_id}/facts", json={"text": "x" * 1001})
        assert r.status_code == 400 and "1000" in r.json()["detail"]
        ok = client.post(f"/api/applications/{app_id}/facts", json={"text": "x" * 1000})
        assert ok.status_code == 201
        r = client.patch(f"/api/applications/{app_id}/facts/{ok.json()['id']}", json={"text": ""})
        assert r.status_code == 400

    def test_limit_of_50(self, client, tmp_db):
        _, app_id = _make_app()
        for i in range(50):
            assert client.post(f"/api/applications/{app_id}/facts", json={"text": f"f{i}"}).status_code == 201
        r = client.post(f"/api/applications/{app_id}/facts", json={"text": "one too many"})
        assert r.status_code == 400 and "50" in r.json()["detail"]

    def test_unknown_application_is_404(self, client, tmp_db):
        assert client.get("/api/applications/nope/facts").status_code == 404
        assert client.post("/api/applications/nope/facts", json={"text": "x"}).status_code == 404

    def test_facts_are_scoped_to_their_application(self, client, tmp_db):
        _, a = _make_app("j1")
        _, b = _make_app("j2")
        fact = client.post(f"/api/applications/{a}/facts", json={"text": "mine"}).json()
        assert client.get(f"/api/applications/{b}/facts").json()["facts"] == []
        assert client.delete(f"/api/applications/{b}/facts/{fact['id']}").status_code == 404

    def test_retailor_carries_facts_from_the_pending_draft(self, tmp_db):
        job_id, app_id = _make_app()
        asyncio.run(dbmod.add_application_fact(app_id, "keep me"))
        assert asyncio.run(dbmod.get_job_pending_facts(job_id)) == ["keep me"]
        asyncio.run(dbmod.delete_pending_applications_for_job(job_id))
        assert asyncio.run(dbmod.get_application_facts(app_id)) == []  # cascaded away
        assert asyncio.run(dbmod.get_job_pending_facts(job_id)) == []


# --- profile facts API ------------------------------------------------------

class TestProfileFactsApi:
    def test_crud_persists_in_config_under_profile(self, client, tmp_config):
        r = client.post("/api/profile/facts", json={"text": "Prefers remote work."})
        assert r.status_code == 201 and r.json() == {"id": 0, "text": "Prefers remote work."}
        client.post("/api/profile/facts", json={"text": "Second"})
        assert yaml.safe_load(tmp_config.read_text())["profile"]["facts"] == ["Prefers remote work.", "Second"]
        assert yaml.safe_load(tmp_config.read_text())["profile"]["full_name"] == "Test Person"

        assert client.patch("/api/profile/facts/1", json={"text": "Changed"}).status_code == 200
        assert [f["text"] for f in client.get("/api/profile/facts").json()["facts"]] == ["Prefers remote work.", "Changed"]
        assert client.delete("/api/profile/facts/0").status_code == 200
        assert [f["text"] for f in client.get("/api/profile/facts").json()["facts"]] == ["Changed"]
        assert client.delete("/api/profile/facts/5").status_code == 404

    def test_limits(self, client, tmp_config):
        assert client.post("/api/profile/facts", json={"text": ""}).status_code == 400
        assert client.post("/api/profile/facts", json={"text": "x" * 1001}).status_code == 400
        for i in range(50):
            assert client.post("/api/profile/facts", json={"text": f"f{i}"}).status_code == 201
        assert client.post("/api/profile/facts", json={"text": "more"}).status_code == 400

    def test_config_save_validates_facts_and_keeps_them_otherwise(self, client, tmp_config):
        client.post("/api/profile/facts", json={"text": "kept"})
        # A profile save that doesn't mention facts leaves them alone.
        assert client.post("/api/config", json={"profile": {"summary": "New"}}).status_code == 200
        assert yaml.safe_load(tmp_config.read_text())["profile"]["facts"] == ["kept"]
        r = client.post("/api/config", json={"profile": {"facts": ["x" * 1001]}})
        assert r.status_code == 400
        r = client.post("/api/config", json={"profile": {"facts": "not a list"}})
        assert r.status_code == 400

    def test_get_config_returns_facts(self, client, tmp_config):
        client.post("/api/profile/facts", json={"text": "kept"})
        assert client.get("/api/config").json()["profile"]["facts"] == ["kept"]


# --- prompt injection -------------------------------------------------------

def _client_returning(text="OUT"):
    choice = MagicMock()
    choice.message.content = text
    resp = MagicMock()
    resp.choices = [choice]
    c = AsyncMock()
    c.chat.completions.create = AsyncMock(return_value=resp)
    return c


def _prompt(c) -> str:
    return c.chat.completions.create.call_args.kwargs["messages"][0]["content"]


PROFILE_WITH_FACTS = {**PROFILE, "facts": ["Profile fact: speaks at conferences."]}
APP_FACTS = ["App fact: referred by a former colleague."]


CALLS = {
    "tailor": lambda cfg, lvl: generate_tailored_resume(JOB, PROFILE_WITH_FACTS, cfg, lvl, app_facts=APP_FACTS),
    "cover": lambda cfg, lvl: generate_cover_letter(JOB, PROFILE_WITH_FACTS, cfg, lvl, app_facts=APP_FACTS),
    "revise_resume": lambda cfg, lvl: revise_tailored_resume("SUMMARY\nx", "tighten", JOB, PROFILE_WITH_FACTS, cfg, honesty_level=lvl, app_facts=APP_FACTS),
    "revise_cover": lambda cfg, lvl: revise_cover_letter("Dear x", "tighten", JOB, PROFILE_WITH_FACTS, cfg, honesty_level=lvl, app_facts=APP_FACTS),
}
OVERRIDE_KEYS = {"tailor": "tailor_resume", "cover": "cover_letter",
                 "revise_resume": "revise_resume", "revise_cover": "revise_cover_letter"}


@pytest.mark.asyncio
@pytest.mark.parametrize("which", list(CALLS))
async def test_both_fact_lists_reach_every_writing_prompt(which):
    c = _client_returning()
    with patch("backend.ai_engine._get_client", return_value=c):
        await CALLS[which](CONFIG, "honest")
    prompt = _prompt(c)
    assert "Profile fact: speaks at conferences." in prompt
    assert "App fact: referred by a former colleague." in prompt
    assert "they are true" in prompt
    assert "in your own words" in prompt
    assert "may state them even when the profile above doesn't" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("which", list(CALLS))
async def test_facts_survive_a_user_overridden_template(which):
    cfg = {**CONFIG, "prompts": {OVERRIDE_KEYS[which]: "My own short prompt."}}
    c = _client_returning()
    with patch("backend.ai_engine._get_client", return_value=c):
        await CALLS[which](cfg, "honest")
    prompt = _prompt(c)
    assert "My own short prompt." in prompt
    assert "App fact: referred by a former colleague." in prompt
    assert "Profile fact: speaks at conferences." in prompt


@pytest.mark.asyncio
async def test_extends_the_fact_base_only_under_strict_levels():
    for level, expected in (("honest", True), ("tailored", True), ("embellished", False), ("fabricated", False)):
        c = _client_returning()
        with patch("backend.ai_engine._get_client", return_value=c):
            await CALLS["tailor"](CONFIG, level)
        assert ("may state them even when the profile above doesn't" in _prompt(c)) is expected
        assert "App fact:" in _prompt(c)


@pytest.mark.asyncio
async def test_no_facts_adds_nothing():
    c = _client_returning()
    with patch("backend.ai_engine._get_client", return_value=c):
        await generate_cover_letter(JOB, PROFILE, CONFIG, "honest")
    assert "FACTS FROM THE CANDIDATE" not in _prompt(c)


def test_facts_block_ignores_malformed_profile_values():
    assert facts_mod.profile_facts({"facts": "oops"}) == []
    assert facts_mod.profile_facts({"facts": ["ok", "", 3, None, "  also ok "]}) == ["ok", "also ok"]
    assert facts_mod.facts_block([], []) == ""


@pytest.mark.asyncio
async def test_apply_assist_answer_includes_facts(tmp_db):
    from types import SimpleNamespace
    from backend.auto_apply.llm_client import LLMClient

    job_id, app_id = await _amake_app()
    await dbmod.add_application_fact(app_id, "App fact: available from March.")
    seen = {}

    async def fake_complete(self, system, user, **kw):
        seen["user"] = user
        return "answer"

    cfg = {**CONFIG, "profile": {"facts": ["Profile fact: likes small teams."]}}
    job = SimpleNamespace(application_id=app_id, title="T", company="C")
    profile = SimpleNamespace(to_text=lambda: "profile text")
    with patch.object(LLMClient, "complete", fake_complete):
        await LLMClient(cfg).generate_answer("Why us?", profile, job)
    assert "App fact: available from March." in seen["user"]
    assert "Profile fact: likes small teams." in seen["user"]


def test_revise_endpoint_sends_stored_facts(client, tmp_db, tmp_config):
    _, app_id = _make_app()
    asyncio.run(dbmod.add_application_fact(app_id, "App fact: stored in db."))
    captured = {}

    async def fake_revise(current, instr, job, profile, cfg, **kw):
        captured.update(kw)
        return "revised"

    with patch("backend.routers.applications.ai_engine.revise_tailored_resume", fake_revise):
        r = client.post(f"/api/applications/{app_id}/revise",
                        json={"target": "resume", "instructions": "tighten"})
    assert r.status_code == 200
    assert captured["app_facts"] == ["App fact: stored in db."]
