"""
tests/test_prompt_registry.py

Tests for:
- prompt_registry.render() placeholder substitution semantics
- registry defaults: every declared variable is used, JSON braces survive
- get_template() override resolution from config
- GET/PUT/DELETE /api/prompts endpoints
- rendered prompts actually pick up overrides in ai_engine / resume_parser
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import prompt_registry


# ---------------------------------------------------------------------------
# render()
# ---------------------------------------------------------------------------

class TestRender:
    def test_substitutes_known_placeholders(self):
        out = prompt_registry.render("Hello {name}, apply to {job_title}.",
                                     name="Ada", job_title="Engineer")
        assert out == "Hello Ada, apply to Engineer."

    def test_leaves_unknown_placeholders_intact(self):
        out = prompt_registry.render("Keep {this} but fill {name}.", name="Ada")
        assert out == "Keep {this} but fill Ada."

    def test_json_braces_survive(self):
        tmpl = 'Return {"scores": [{"index": <int>, "score": <0-100>}]} and {value}'
        out = prompt_registry.render(tmpl, value="X")
        assert out == 'Return {"scores": [{"index": <int>, "score": <0-100>}]} and X'

    def test_never_raises_on_user_braces(self):
        # str.format would raise KeyError/IndexError on these
        for tmpl in ("{", "}", "{}", "{0}", "{ unbalanced", '{"json": true}'):
            prompt_registry.render(tmpl, resume="x")

    def test_repeated_placeholder(self):
        out = prompt_registry.render("{a} and {a}", a="1")
        assert out == "1 and 1"


# ---------------------------------------------------------------------------
# Registry defaults
# ---------------------------------------------------------------------------

class TestRegistryDefaults:
    def test_every_prompt_has_required_metadata(self):
        for key, meta in prompt_registry.PROMPTS.items():
            assert meta["label"], key
            assert meta["group"], key
            assert meta["description"], key
            assert isinstance(meta["variables"], dict), key
            assert meta["default"].strip(), key

    def test_default_placeholders_are_all_declared(self):
        """Every {placeholder} in a default template must be a declared variable
        (otherwise it would render as literal text)."""
        for key, meta in prompt_registry.PROMPTS.items():
            used = set(prompt_registry.template_placeholders(meta["default"]))
            declared = set(meta["variables"])
            assert used <= declared, (
                f"{key}: template uses undeclared placeholders {used - declared}"
            )

    def test_declared_variables_all_appear_in_default(self):
        for key, meta in prompt_registry.PROMPTS.items():
            used = set(prompt_registry.template_placeholders(meta["default"]))
            declared = set(meta["variables"])
            assert declared <= used, (
                f"{key}: declares variables never used in default {declared - used}"
            )

    def test_defaults_render_cleanly_with_all_variables(self):
        """Rendering a default with every declared variable leaves no known
        placeholders behind and injects each value."""
        for key, meta in prompt_registry.PROMPTS.items():
            values = {name: f"<{name}-value>" for name in meta["variables"]}
            out = prompt_registry.render(meta["default"], **values)
            for name, val in values.items():
                assert val in out, f"{key}: {name} not injected"
                assert ("{%s}" % name) not in out, f"{key}: {name} left unrendered"

    def test_get_template_returns_default_without_override(self):
        cfg = {}
        assert (prompt_registry.get_template(cfg, "score_job_fit")
                == prompt_registry.PROMPTS["score_job_fit"]["default"])

    def test_get_template_prefers_override(self):
        cfg = {"prompts": {"score_job_fit": "My custom prompt {job_title}"}}
        assert (prompt_registry.get_template(cfg, "score_job_fit")
                == "My custom prompt {job_title}")

    def test_blank_override_falls_back_to_default(self):
        cfg = {"prompts": {"score_job_fit": "   \n"}}
        assert (prompt_registry.get_template(cfg, "score_job_fit")
                == prompt_registry.PROMPTS["score_job_fit"]["default"])


# ---------------------------------------------------------------------------
# Overrides flow through to the actual LLM calls
# ---------------------------------------------------------------------------

def _mock_openai_client(response_text: str):
    client = MagicMock()
    msg = MagicMock()
    msg.content = response_text
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    client.chat.completions.create = AsyncMock(return_value=resp)
    return client


class TestOverridesReachLLM:
    def test_score_job_fit_uses_override(self, monkeypatch):
        from backend import ai_engine

        cfg = {
            "ai": {},
            "prompts": {"score_job_fit": "CUSTOM SCORER for {job_title}"},
        }
        client = _mock_openai_client(json.dumps({"score": 50, "reasoning": "ok"}))
        monkeypatch.setattr(ai_engine, "_get_client", lambda *a, **k: client)
        monkeypatch.setattr(ai_engine, "_model", lambda *a, **k: "m")

        asyncio.run(ai_engine.score_job_fit(
            {"title": "Analyst", "company": "Acme", "description": "d"},
            {"full_name": "A"}, cfg,
        ))
        sent = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        assert sent == "CUSTOM SCORER for Analyst"

    def test_resume_parse_uses_override(self, monkeypatch):
        from backend import resume_parser

        cfg = {
            "ai": {},
            "prompts": {"resume_parse": "CUSTOM PARSER:\n{resume}"},
        }
        client = _mock_openai_client(json.dumps({"full_name": "Jane"}))
        monkeypatch.setattr(resume_parser.ai_engine, "_get_client", lambda *a, **k: client)
        monkeypatch.setattr(resume_parser.ai_engine, "_model", lambda *a, **k: "m")

        asyncio.run(resume_parser.parse_resume("resume text here", cfg))
        sent = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        assert sent == "CUSTOM PARSER:\nresume text here"


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient

import backend.database as db_module


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    asyncio.run(db_module.init_db())
    return db_path


@pytest.fixture()
def tmp_config(tmp_path):
    cfg = {
        "profile": {"full_name": "Test User", "email": "test@example.com"},
        "search": {},
        "auto_apply": {"enabled": False},
        "ai": {"base_url": "http://localhost:1234/v1", "api_key": "none"},
        "server": {"host": "0.0.0.0", "port": 8888},
        "flaresolverr": {"url": ""},
        "linkedin": {"browser": "firefox"},
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(cfg))
    return cfg_path


@pytest.fixture()
def api_client(tmp_config, tmp_db, monkeypatch):
    import backend.main as main_module

    monkeypatch.setattr("backend.app_state.CONFIG_PATH", tmp_config)
    monkeypatch.setattr("backend.app_state.RESUMES_DIR", tmp_config.parent / "resumes")

    with TestClient(main_module.app, raise_server_exceptions=True) as client:
        yield client


class TestPromptsAPI:
    def test_list_prompts(self, api_client):
        resp = api_client.get("/api/prompts")
        assert resp.status_code == 200
        prompts = resp.json()["prompts"]
        assert len(prompts) == len(prompt_registry.PROMPTS)
        by_key = {p["key"]: p for p in prompts}
        p = by_key["tailor_resume"]
        assert p["label"] == "Tailored Resume"
        assert p["override"] is None
        assert p["customized"] is False
        assert "{profile_summary}" in p["default"]

    def test_save_and_reset_override(self, api_client, tmp_config):
        # Save a custom template
        resp = api_client.put("/api/prompts/score_job_fit",
                              json={"template": "Rate {job_title} for me."})
        assert resp.status_code == 200
        assert resp.json()["customized"] is True
        assert resp.json()["unknown_variables"] == []

        # Persisted to config.yaml
        saved = yaml.safe_load(tmp_config.read_text())
        assert saved["prompts"]["score_job_fit"] == "Rate {job_title} for me."

        # Reflected in GET
        prompts = api_client.get("/api/prompts").json()["prompts"]
        p = next(x for x in prompts if x["key"] == "score_job_fit")
        assert p["customized"] is True
        assert p["override"] == "Rate {job_title} for me."

        # Reset removes the override and the (now empty) prompts key
        resp = api_client.delete("/api/prompts/score_job_fit")
        assert resp.status_code == 200
        assert resp.json()["customized"] is False
        saved = yaml.safe_load(tmp_config.read_text())
        assert "prompts" not in saved

    def test_save_default_text_removes_override(self, api_client, tmp_config):
        default = prompt_registry.PROMPTS["cover_letter"]["default"]
        api_client.put("/api/prompts/cover_letter", json={"template": "custom"})
        resp = api_client.put("/api/prompts/cover_letter", json={"template": default})
        assert resp.json()["customized"] is False
        saved = yaml.safe_load(tmp_config.read_text())
        assert "prompts" not in saved

    def test_unknown_placeholder_warning(self, api_client):
        resp = api_client.put(
            "/api/prompts/score_job_fit",
            json={"template": "Use {job_title} and {made_up_thing}."},
        )
        assert resp.status_code == 200
        assert resp.json()["unknown_variables"] == ["made_up_thing"]

    def test_unknown_key_404(self, api_client):
        assert api_client.put("/api/prompts/nope", json={"template": "x"}).status_code == 404
        assert api_client.delete("/api/prompts/nope").status_code == 404
