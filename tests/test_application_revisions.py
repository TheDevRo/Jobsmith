"""
tests/test_application_revisions.py

Every saved change to an application's resume or cover letter keeps the previous
version (application_revisions) so it can be undone: record, cap, list, restore,
and a restore that can itself be undone.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import app_state as state
from backend import database as dbmod
from backend.main import app

PROFILE = {"full_name": "Sam Example", "experience": [], "skills": []}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "test.db")
    asyncio.run(dbmod.init_db())
    return tmp_path / "test.db"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(state, "load_config", lambda: {"profile": PROFILE, "application_honesty": {}})
    return TestClient(app)


@pytest.fixture
def gen(monkeypatch):
    """Stand-ins for the DOCX/PDF writers; records what they were asked to render."""
    calls = {"resume": [], "cover_letter": []}
    from backend.routers import applications as router
    monkeypatch.setattr(router.resume_generator, "generate_resume", lambda text, *a, **k: calls["resume"].append(text))
    monkeypatch.setattr(router.resume_generator, "generate_cover_letter", lambda text, *a, **k: calls["cover_letter"].append(text))
    return calls


async def _make_app(resume="R0", letter="L0"):
    await dbmod.upsert_job({"source": "t", "external_id": "e1", "title": "Engineer", "company": "Example Co",
                            "location": "Remote", "url": "https://x/1", "description": "Build things."})
    job_id = (await dbmod.get_jobs(limit=5, status=None))["jobs"][0]["id"]
    return job_id, await dbmod.create_application(job_id, resume, letter, honesty_level="honest")


def _save(client, app_id, resume, letter, **extra):
    return client.patch(f"/api/applications/{app_id}/content",
                        json={"resume_content": resume, "cover_letter_content": letter, **extra})


def _revs(client, app_id, target="resume"):
    return client.get(f"/api/applications/{app_id}/revisions?target={target}").json()["revisions"]


# --- migration --------------------------------------------------------------

def test_migration_adds_the_table_to_an_existing_database(tmp_db):
    conn = sqlite3.connect(tmp_db)
    conn.execute("DROP TABLE application_revisions")
    conn.execute("DELETE FROM schema_version WHERE version >= 33")
    conn.commit()
    conn.close()
    asyncio.run(dbmod.init_db())
    conn = sqlite3.connect(tmp_db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(application_revisions)")]
    conn.close()
    assert cols == ["id", "application_id", "target", "content", "instruction", "created_at"]


# --- recording --------------------------------------------------------------

class TestRecord:
    def test_a_save_keeps_the_previous_version_of_each_changed_document(self, tmp_db, client, gen):
        _, app_id = asyncio.run(_make_app())
        assert _save(client, app_id, "R1", "L0").status_code == 200
        assert [r["preview"] for r in _revs(client, app_id, "resume")] == ["R0"]
        assert _revs(client, app_id, "cover_letter") == []        # unchanged -> nothing recorded

    def test_both_documents_changed_are_recorded_separately(self, tmp_db, client, gen):
        _, app_id = asyncio.run(_make_app())
        _save(client, app_id, "R1", "L1")
        assert [r["preview"] for r in _revs(client, app_id, "resume")] == ["R0"]
        assert [r["preview"] for r in _revs(client, app_id, "cover_letter")] == ["L0"]

    def test_saving_identical_text_records_nothing(self, tmp_db, client, gen):
        _, app_id = asyncio.run(_make_app())
        _save(client, app_id, "R0", "L0")
        assert _revs(client, app_id) == []

    def test_the_ai_instruction_is_kept_with_the_replaced_version(self, tmp_db, client, gen):
        _, app_id = asyncio.run(_make_app())
        _save(client, app_id, "R1", "L0", instruction="  make it shorter ")
        _save(client, app_id, "R2", "L0")                          # a manual edit
        revs = _revs(client, app_id)
        assert [r["instruction"] for r in revs] == [None, "make it shorter"]   # newest first
        assert [r["preview"] for r in revs] == ["R1", "R0"]

    def test_an_empty_previous_text_is_not_an_undo_point(self, tmp_db, client, gen):
        _, app_id = asyncio.run(_make_app(resume="", letter=""))
        _save(client, app_id, "R1", "L1")
        assert _revs(client, app_id) == [] and _revs(client, app_id, "cover_letter") == []

    def test_cap_prunes_the_oldest_per_application_and_target(self, tmp_db, client, gen):
        _, app_id = asyncio.run(_make_app())
        n = dbmod.REVISION_CAP + 5
        for i in range(1, n + 1):
            _save(client, app_id, f"R{i}", "L0")
        revs = _revs(client, app_id)
        assert len(revs) == dbmod.REVISION_CAP == 30
        assert revs[0]["preview"] == f"R{n - 1}"                   # newest kept
        assert revs[-1]["preview"] == f"R{n - 30}"                 # oldest ones pruned: R0..R4 gone
        assert "R0" not in [r["preview"] for r in revs]

    def test_the_cap_is_per_target_and_per_application(self, tmp_db, client, gen):
        _, a = asyncio.run(_make_app())
        for i in range(1, 36):
            _save(client, a, f"R{i}", "L0")
        asyncio.run(dbmod.upsert_job({"source": "t", "external_id": "e2", "title": "T2", "company": "C",
                                      "location": "x", "url": "https://x/2"}))
        jobs = asyncio.run(dbmod.get_jobs(limit=5, status=None))["jobs"]
        j2 = next(j["id"] for j in jobs if j["external_id"] == "e2")
        b = asyncio.run(dbmod.create_application(j2, "B0", "BL0"))
        _save(client, b, "B1", "BL0")
        assert len(_revs(client, a)) == 30 and len(_revs(client, b)) == 1
        _save(client, a, "R35", "L1")
        assert len(_revs(client, a, "cover_letter")) == 1

    def test_listing_validates_and_404s(self, tmp_db, client):
        _, app_id = asyncio.run(_make_app())
        assert client.get(f"/api/applications/{app_id}/revisions?target=bogus").status_code == 400
        assert client.get("/api/applications/nope/revisions?target=resume").status_code == 404


# --- restore / undo ---------------------------------------------------------

class TestRestore:
    def test_restore_puts_the_version_back_and_regenerates_the_documents(self, tmp_db, client, gen):
        _, app_id = asyncio.run(_make_app())
        _save(client, app_id, "R1", "L0")
        gen["resume"].clear()
        rev = _revs(client, app_id)[0]
        r = client.post(f"/api/applications/{app_id}/revisions/{rev['id']}/restore")
        assert r.status_code == 200
        assert r.json()["target"] == "resume" and r.json()["content"] == "R0"
        assert _content(tmp_db, app_id) == ("R0", "L0")
        assert gen["resume"] == ["R0"]                              # DOCX/PDF rebuilt, as PATCH /content does
        assert "fact_flags" in r.json()

    def test_restore_is_itself_recorded_so_it_can_be_undone(self, tmp_db, client, gen):
        _, app_id = asyncio.run(_make_app())
        _save(client, app_id, "R1", "L0")
        undo = _revs(client, app_id)[0]
        client.post(f"/api/applications/{app_id}/revisions/{undo['id']}/restore")      # back to R0
        assert _content(tmp_db, app_id)[0] == "R0"
        revs = _revs(client, app_id)
        assert [r["preview"] for r in revs] == ["R1", "R0"]
        assert revs[0]["instruction"] == "Restored an earlier version"
        client.post(f"/api/applications/{app_id}/revisions/{revs[0]['id']}/restore")   # undo the undo
        assert _content(tmp_db, app_id)[0] == "R1"

    def test_undoing_again_redoes(self, tmp_db, client, gen):
        _, app_id = asyncio.run(_make_app())
        for text in ("R1", "R2", "R3"):
            _save(client, app_id, text, "L0")
        seen = []
        for _ in range(3):
            newest = _revs(client, app_id)[0]
            client.post(f"/api/applications/{app_id}/revisions/{newest['id']}/restore")
            seen.append(_content(tmp_db, app_id)[0])
        # Undo goes back one version; undoing that undo comes forward again.
        assert seen == ["R2", "R3", "R2"]

    def test_restoring_one_document_leaves_the_other_alone(self, tmp_db, client, gen):
        _, app_id = asyncio.run(_make_app())
        _save(client, app_id, "R1", "L1")
        rev = _revs(client, app_id, "cover_letter")[0]
        client.post(f"/api/applications/{app_id}/revisions/{rev['id']}/restore")
        assert _content(tmp_db, app_id) == ("R1", "L0")
        assert gen["cover_letter"][-1] == "L0"

    def test_unknown_or_foreign_revision_is_404(self, tmp_db, client, gen):
        _, a = asyncio.run(_make_app())
        _save(client, a, "R1", "L0")
        rev = _revs(client, a)[0]
        assert client.post(f"/api/applications/{a}/revisions/99999/restore").status_code == 404
        assert client.post(f"/api/applications/other/revisions/{rev['id']}/restore").status_code == 404

    def test_restore_refreshes_the_fact_flags(self, tmp_db, client, gen):
        _, app_id = asyncio.run(_make_app(resume="Served 90,000 users.", letter="L0"))
        _save(client, app_id, "Plain text", "L0")
        rev = _revs(client, app_id)[0]
        r = client.post(f"/api/applications/{app_id}/revisions/{rev['id']}/restore")
        assert r.json()["fact_flags"]["resume"] == ["90,000"]


# --- re-tailor --------------------------------------------------------------

def test_retailor_keeps_the_replaced_draft_as_an_undo_point(tmp_db, monkeypatch):
    from backend import background_tasks as bg
    monkeypatch.setattr(state, "load_config", lambda: {
        "profile": PROFILE, "application_honesty": {"honesty_level": "honest"},
        "salary_estimator": {"enabled": False}})
    job_id, old = asyncio.run(_make_app(resume="OLD RESUME", letter="OLD LETTER"))

    async def fake_resume(*a, **k):
        return "NEW RESUME"

    async def fake_cover(*a, **k):
        return "NEW LETTER"

    monkeypatch.setattr(bg.ai_engine, "score_job_fit", AsyncMock(return_value=(80, "ok", {})))
    monkeypatch.setattr(bg.ai_engine, "generate_tailored_resume", fake_resume)
    monkeypatch.setattr(bg.ai_engine, "generate_cover_letter", fake_cover)
    monkeypatch.setattr(bg.ai_engine, "generate_embellishment_log", AsyncMock(return_value={}))
    monkeypatch.setattr(bg.resume_generator, "generate_resume", lambda *a, **k: None)
    monkeypatch.setattr(bg.resume_generator, "generate_cover_letter", lambda *a, **k: None)

    asyncio.run(bg._bg_tailor_job(job_id))

    new = asyncio.run(dbmod.get_latest_application_id(job_id))
    assert new != old
    resume_revs = asyncio.run(dbmod.get_revisions(new, "resume"))
    assert [(r["preview"], r["instruction"]) for r in resume_revs] == [("OLD RESUME", "Before re-tailoring")]
    assert [r["preview"] for r in asyncio.run(dbmod.get_revisions(new, "cover_letter"))] == ["OLD LETTER"]


def test_deleting_an_application_removes_its_revisions(tmp_db, client, gen):
    job_id, app_id = asyncio.run(_make_app())
    _save(client, app_id, "R1", "L0")
    asyncio.run(dbmod.delete_pending_applications_for_job(job_id))
    conn = sqlite3.connect(tmp_db)
    assert conn.execute("SELECT COUNT(*) FROM application_revisions").fetchone() == (0,)
    conn.close()


def _content(db_path, app_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT resume_content, cover_letter_content FROM applications WHERE id = ?", (app_id,)).fetchone()
    conn.close()
    return row
