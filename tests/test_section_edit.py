"""
tests/test_section_edit.py

Section-targeted AI edits: the document is split losslessly, only the chosen
section goes to the model as the part to rewrite (the rest is read-only
context), and the answer is spliced back so every other byte is unchanged.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import database as dbmod
from backend import section_edit as se
from backend.ai_engine import revise_cover_letter, revise_tailored_resume
from backend.main import app

RESUME = """SUMMARY
Backend engineer who ships reliable services.

SKILLS
Python, PostgreSQL, AWS

EXPERIENCE
Title: Senior Engineer
Company: Example Co
Dates: Mar 2019 - Present
- Cut deploy time by 40%
- Mentored 3 engineers

EDUCATION
Degree: B.S. Computer Science
School: Example State University
Year: 2016
"""

LETTER = "Dear Hiring Team,\n\nFirst paragraph about the role.\nIt has two lines.\n\n\nSecond paragraph about me.\n\nSincerely,\nSam Example\n"


# --- splitting --------------------------------------------------------------

class TestSplit:
    def test_resume_sections_in_order(self):
        assert [o["id"] for o in se.options("resume", RESUME)] == ["SUMMARY", "SKILLS", "EXPERIENCE", "EDUCATION"]

    def test_split_is_lossless(self):
        for target, text in (("resume", RESUME), ("cover_letter", LETTER),
                             ("resume", "no headers at all\njust text"), ("resume", ""),
                             ("cover_letter", "one paragraph"), ("resume", "x\r\nSKILLS\r\nPython\r\n")):
            assert "".join(c.text for c in se.split(target, text)) == text

    def test_header_variants_and_markdown_prefix(self):
        text = "Intro line\n## Professional Experience\n- did things\nTECHNICAL SKILLS:\nGo\n"
        assert [o["id"] for o in se.options("resume", text)] == ["PROFESSIONAL EXPERIENCE", "TECHNICAL SKILLS"]

    def test_text_before_the_first_header_is_not_offered(self):
        chunks = se.split("resume", "Preamble\nSKILLS\nGo\n")
        assert chunks[0].id == "preamble" and not chunks[0].editable

    def test_cover_letter_paragraphs(self):
        opts = se.options("cover_letter", LETTER)
        assert [o["id"] for o in opts] == ["paragraph:1", "paragraph:2", "paragraph:3", "paragraph:4"]
        assert opts[1]["label"].startswith("Paragraph 2: First paragraph")

    def test_find(self):
        assert se.find("resume", RESUME, "skills")[1] == 1   # case-insensitive; SUMMARY is chunk 0
        assert se.find("resume", RESUME, "NOPE") is None
        assert se.find("cover_letter", LETTER, "paragraph:9") is None


# --- splicing: everything else stays byte-identical -------------------------

class TestSplice:
    def _others(self, chunks, index):
        return [c.text for i, c in enumerate(chunks) if i != index]

    def test_other_sections_are_byte_identical(self):
        chunks, i = se.find("resume", RESUME, "SKILLS")
        out = se.splice("resume", chunks, i, "SKILLS\nPython, Go, Rust")
        again = se.split("resume", out)
        assert [c.text for j, c in enumerate(again) if j != i] == self._others(chunks, i)
        assert out.startswith("SUMMARY\nBackend engineer who ships reliable services.\n\nSKILLS\nPython, Go, Rust\n\nEXPERIENCE\n")
        assert out.endswith("Year: 2016\n")

    def test_trailing_blank_lines_are_kept(self):
        chunks, i = se.find("resume", RESUME, "SUMMARY")
        out = se.splice("resume", chunks, i, "SUMMARY\nNew summary.\n")
        assert out.startswith("SUMMARY\nNew summary.\n\nSKILLS")

    def test_missing_header_is_restored(self):
        chunks, i = se.find("resume", RESUME, "SKILLS")
        assert se.splice("resume", chunks, i, "Python, Go").splitlines()[3:5] == ["SKILLS", "Python, Go"]

    def test_model_that_wanders_into_the_next_section_is_cut_off(self):
        chunks, i = se.find("resume", RESUME, "SKILLS")
        out = se.splice("resume", chunks, i, "SKILLS\nGo\n\nEXPERIENCE\nTitle: Invented\nCompany: Nowhere")
        assert "Invented" not in out
        assert out.count("EXPERIENCE") == 1

    def test_a_different_leading_header_is_replaced_by_the_right_one(self):
        chunks, i = se.find("resume", RESUME, "SKILLS")
        out = se.splice("resume", chunks, i, "TECHNICAL SKILLS\nGo")
        assert out.count("SKILLS") == 1 and "TECHNICAL" not in out and "\nGo\n" in out

    def test_cover_letter_paragraph(self):
        chunks, i = se.find("cover_letter", LETTER, "paragraph:3")
        out = se.splice("cover_letter", chunks, i, "Rewritten second paragraph.")
        assert out == LETTER.replace("Second paragraph about me.", "Rewritten second paragraph.")

    def test_read_only_context_marks_the_section(self):
        chunks, i = se.find("resume", RESUME, "SKILLS")
        ctx = se.read_only_context(chunks, i)
        assert "[... the section being edited ...]" in ctx and "Python, PostgreSQL" not in ctx
        assert "Backend engineer" in ctx and "Example State University" in ctx

    def test_every_preview_style_header_is_recognised(self):
        from backend.resume_generator import PREVIEW_CONTENT
        ids = [o["id"] for o in se.options("resume", PREVIEW_CONTENT)]
        assert ids == ["SUMMARY", "TECHNICAL SKILLS", "PROFESSIONAL EXPERIENCE", "EDUCATION", "CERTIFICATIONS"]


# --- the prompt -------------------------------------------------------------

def _client(text="OUT"):
    choice = MagicMock()
    choice.message.content = text
    resp = MagicMock()
    resp.choices = [choice]
    c = AsyncMock()
    c.chat.completions.create = AsyncMock(return_value=resp)
    return c


CONFIG = {"ai": {"base_url": "http://localhost:1234/v1", "api_key": "k", "model": "m"}}
JOB = {"title": "Engineer", "company": "Example Co", "description": "Build things."}
PROFILE = {"full_name": "Sam Example", "experience": [], "skills": []}


@pytest.mark.asyncio
async def test_resume_prompt_sends_only_the_section_plus_read_only_context():
    c = _client()
    chunks, i = se.find("resume", RESUME, "SKILLS")
    section = ("SKILLS", se.read_only_context(chunks, i))
    with patch("backend.ai_engine._get_client", return_value=c):
        await revise_tailored_resume(chunks[i].text, "add Go", JOB, PROFILE, CONFIG, section=section)
    prompt = c.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "SECTION EDIT" in prompt and "READ-ONLY CONTEXT" in prompt
    assert prompt.count("Python, PostgreSQL, AWS") == 1          # the section appears once, as the part to rewrite
    assert "Backend engineer who ships" in prompt                  # the rest is there as context
    assert prompt.rindex("SECTION EDIT") > prompt.index("starting directly with SUMMARY")  # last word wins


@pytest.mark.asyncio
async def test_section_block_survives_a_user_overridden_template_and_cover_letter():
    c = _client()
    cfg = {**CONFIG, "prompts": {"revise_cover_letter": "My short prompt {current_letter}"}}
    chunks, i = se.find("cover_letter", LETTER, "paragraph:3")
    with patch("backend.ai_engine._get_client", return_value=c):
        await revise_cover_letter(chunks[i].text, "shorter", JOB, PROFILE, cfg,
                                  section=("Paragraph 3", se.read_only_context(chunks, i)))
    prompt = c.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "My short prompt Second paragraph about me." in prompt
    assert "SECTION EDIT" in prompt and "First paragraph about the role." in prompt


@pytest.mark.asyncio
async def test_without_a_section_nothing_changes():
    c = _client()
    with patch("backend.ai_engine._get_client", return_value=c):
        await revise_tailored_resume(RESUME, "x", JOB, PROFILE, CONFIG)
    assert "SECTION EDIT" not in c.chat.completions.create.call_args.kwargs["messages"][0]["content"]


# --- the endpoint -----------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "test.db")
    asyncio.run(dbmod.init_db())


@pytest.fixture
def cfg(monkeypatch):
    from backend import app_state as state
    monkeypatch.setattr(state, "load_config", lambda: {"profile": PROFILE, "application_honesty": {}})


async def _make_app():
    await dbmod.upsert_job({"source": "t", "external_id": "e1", "title": "Engineer", "company": "Example Co",
                            "location": "Remote", "url": "https://x/1", "description": "Build things."})
    job_id = (await dbmod.get_jobs(limit=5, status=None))["jobs"][0]["id"]
    return await dbmod.create_application(job_id, RESUME, LETTER, honesty_level="honest")


def _revise(app_id, section, answer, target="resume"):
    seen = {}

    async def fake(text_in, instr, job, profile, config, **kw):
        seen["text_in"], seen["kw"] = text_in, kw
        return answer

    fn = "revise_tailored_resume" if target == "resume" else "revise_cover_letter"
    body = {"target": target, "instructions": "do it", **({"section": section} if section else {})}
    with patch(f"backend.routers.applications.ai_engine.{fn}", fake):
        r = TestClient(app).post(f"/api/applications/{app_id}/revise", json=body)
    return r, seen


def test_sections_endpoint(tmp_db, cfg):
    app_id = asyncio.run(_make_app())
    c = TestClient(app)
    ids = [s["id"] for s in c.get(f"/api/applications/{app_id}/sections?target=resume").json()["sections"]]
    assert ids == ["SUMMARY", "SKILLS", "EXPERIENCE", "EDUCATION"]
    ids = [s["id"] for s in c.get(f"/api/applications/{app_id}/sections?target=cover_letter").json()["sections"]]
    assert ids[0] == "paragraph:1" and len(ids) == 4
    assert c.get(f"/api/applications/{app_id}/sections?target=bogus").status_code == 400
    assert c.get("/api/applications/nope/sections").status_code == 404


def test_revise_section_returns_the_whole_document_with_only_that_section_changed(tmp_db, cfg):
    app_id = asyncio.run(_make_app())
    r, seen = _revise(app_id, "SKILLS", "SKILLS\nPython, Go")
    assert r.status_code == 200
    out = r.json()["revised_content"]
    assert out == RESUME.replace("SKILLS\nPython, PostgreSQL, AWS\n", "SKILLS\nPython, Go\n")
    assert seen["text_in"] == "SKILLS\nPython, PostgreSQL, AWS\n\n"      # only the section is the text to rewrite
    label, context = seen["kw"]["section"]
    assert label == "SKILLS" and "Backend engineer" in context and "PostgreSQL" not in context
    assert r.json()["section"] == "SKILLS"


def test_revise_cover_letter_paragraph(tmp_db, cfg):
    app_id = asyncio.run(_make_app())
    r, _ = _revise(app_id, "paragraph:3", "Rewritten.", target="cover_letter")
    assert r.json()["revised_content"] == LETTER.replace("Second paragraph about me.", "Rewritten.")


def test_whole_and_omitted_section_send_the_whole_document(tmp_db, cfg):
    app_id = asyncio.run(_make_app())
    for section in (None, "", "whole", "WHOLE"):
        r, seen = _revise(app_id, section, "Entire new text")
        assert r.json()["revised_content"] == "Entire new text"
        assert seen["text_in"] == RESUME and "section" not in seen["kw"] and r.json()["section"] is None


def test_unknown_section_is_a_400_that_lists_the_valid_ones(tmp_db, cfg):
    app_id = asyncio.run(_make_app())
    r, _ = _revise(app_id, "HOBBIES", "x")
    assert r.status_code == 400 and "SKILLS" in r.json()["detail"]
