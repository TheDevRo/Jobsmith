"""
Setup Assistant (first-run step 0) backend: provider presets, the 1-token
chat ping + plain-English error mapper, onboarding status/save endpoints,
the empty-model guard and the registry rows. Fully offline: the OpenAI client,
the Apple bridge status and the model downloads are all faked.
"""

import json

import httpx
import openai
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import ai_engine, apple_bridge
from backend import app_state as state
from backend.routers import settings as settings_router
from backend.sync import settings_registry as sr


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def config_path(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump({
        "profile": {"full_name": "Test User", "email": "test@example.com"},
        "ai": {"base_url": "http://localhost:1234/v1", "api_key": "old-key",
               "models": {"strong": {"model": "old", "base_url": "http://tier/v1"}}},
    }))
    monkeypatch.setattr(state, "CONFIG_PATH", p)
    return p


@pytest.fixture
def installs(monkeypatch):
    """Record (never run) the NLI / Quick match downloads."""
    from backend.nli import model, triage_model
    calls = []
    monkeypatch.setattr(model, "install", lambda: calls.append("nli"))
    monkeypatch.setattr(triage_model, "install", lambda: calls.append("triage") or {})
    return calls


@pytest.fixture
def client(config_path, monkeypatch):
    async def fake_conn(cfg):
        return {"connected": False, "error": "offline"}

    async def fake_on_device(cfg):
        return {"supported": True, "available": False, "reason": "Apple Intelligence is turned off"}

    monkeypatch.setattr(ai_engine, "test_connection", fake_conn)
    monkeypatch.setattr(settings_router, "_on_device_status", fake_on_device)
    app = FastAPI()
    app.include_router(settings_router.router)
    return TestClient(app)


def _saved(config_path):
    return yaml.safe_load(config_path.read_text())


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
def test_providers_list(client):
    rows = client.get("/api/ai/providers").json()
    assert [r["name"] for r in rows] == [
        "OpenAI", "Anthropic", "Google Gemini", "xAI (Grok)", "Mistral", "Groq",
        "DeepSeek", "Together AI", "Fireworks", "Cerebras", "NVIDIA NIM", "OpenRouter",
    ]
    for r in rows:
        assert set(r) == {"name", "base_url", "key_url"}
        assert r["base_url"].startswith("https://") and not r["base_url"].endswith("/")
        assert r["key_url"].startswith("https://")
    assert rows[-1]["base_url"] == "https://openrouter.ai/api/v1"


# ---------------------------------------------------------------------------
# ping_chat + error mapper
# ---------------------------------------------------------------------------
_REQ = httpx.Request("POST", "https://api.example.com/v1/chat/completions")


def _status_error(cls, code, body=None):
    return cls(f"Error code: {code} - {body or {}}", response=httpx.Response(code, request=_REQ), body=body)


class _FakeClient:
    raise_exc = None
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.chat = self
        self.completions = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def create(self, **kwargs):
        _FakeClient.calls.append((self.kwargs, kwargs))
        if _FakeClient.raise_exc:
            raise _FakeClient.raise_exc
        return object()


@pytest.fixture
def fake_openai(monkeypatch):
    _FakeClient.raise_exc = None
    _FakeClient.calls = []
    monkeypatch.setattr(ai_engine, "AsyncOpenAI", _FakeClient)
    return _FakeClient


@pytest.mark.asyncio
async def test_ping_ok_sends_one_token(fake_openai):
    r = await ai_engine.ping_chat("https://api.example.com/v1", "k", "m1")
    assert r == {"ok": True, "code": "ok", "message": "", "detail": ""}
    client_kw, create_kw = fake_openai.calls[0]
    assert client_kw["base_url"] == "https://api.example.com/v1"
    assert client_kw["timeout"] == 20.0 and client_kw["max_retries"] == 0
    assert create_kw == {"model": "m1", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}


@pytest.mark.asyncio
async def test_ping_blank_key_uses_placeholder(fake_openai):
    await ai_engine.ping_chat("http://rig.local/v1", "", "m1")
    assert fake_openai.calls[0][0]["api_key"] == "lm-studio"


@pytest.mark.parametrize("exc, code, message", [
    (_status_error(openai.AuthenticationError, 401), "auth", "That API key was rejected"),
    (_status_error(openai.PermissionDeniedError, 403), "auth", "That API key was rejected"),
    (_status_error(openai.NotFoundError, 404), "model", "That model is not available on this account"),
    (_status_error(openai.BadRequestError, 400, {"error": {"code": "model_not_found"}}), "model",
     "That model is not available on this account"),
    (_status_error(openai.APIStatusError, 402), "credit", "Your provider account has no credit"),
    (_status_error(openai.RateLimitError, 429, {"error": {"code": "insufficient_quota"}}), "credit",
     "Your provider account has no credit"),
    (_status_error(openai.RateLimitError, 429), "rate_limit",
     "The provider is rate-limiting; try again in a minute"),
    (openai.APIConnectionError(request=_REQ), "unreachable", "Could not reach the server at api.example.com"),
    (openai.APITimeoutError(request=_REQ), "unreachable", "Could not reach the server at api.example.com"),
])
@pytest.mark.asyncio
async def test_ping_maps_errors(fake_openai, exc, code, message):
    fake_openai.raise_exc = exc
    r = await ai_engine.ping_chat("https://api.example.com/v1", "k", "m1")
    assert (r["ok"], r["code"], r["message"]) == (False, code, message)
    assert r["detail"]  # raw error for "Show details"


@pytest.mark.asyncio
async def test_ping_without_model_does_not_call(fake_openai):
    r = await ai_engine.ping_chat("https://api.example.com/v1", "k", "  ")
    assert r["ok"] is False and r["code"] == "no_model"
    assert fake_openai.calls == []


@pytest.mark.asyncio
async def test_ping_without_url_does_not_call(fake_openai):
    r = await ai_engine.ping_chat("  ", "k", "m1")
    assert r["ok"] is False and r["code"] == "no_url"
    assert fake_openai.calls == []


def test_list_models_endpoint_uses_request_values(client, config_path, monkeypatch):
    seen = {}

    async def conn(cfg):
        seen.update(cfg["ai"])
        return {"connected": True, "models": ["a", "text-embedding-3"]}
    monkeypatch.setattr(ai_engine, "test_connection", conn)
    before = config_path.read_text()
    r = client.post("/api/ai/models", json={"base_url": "https://x/v1", "api_key": ""}).json()
    assert r == {"ok": True, "models": ["a", "text-embedding-3"], "message": "", "detail": ""}
    assert seen == {"base_url": "https://x/v1", "api_key": ""}
    assert config_path.read_text() == before
    assert client.post("/api/ai/models", json={"base_url": ""}).json()["ok"] is False


@pytest.mark.asyncio
async def test_ping_apple_unavailable_reports_reason(fake_openai, monkeypatch):
    async def status(probe=True):
        return {"supported": True, "available": False, "reason": "Apple Intelligence is turned off"}
    monkeypatch.setattr(apple_bridge, "bridge_status", status)
    r = await ai_engine.ping_chat("", "", apple_bridge.SENTINEL_MODEL)
    assert (r["ok"], r["code"], r["message"]) == (False, "unavailable", "Apple Intelligence is turned off")
    assert fake_openai.calls == []


@pytest.mark.asyncio
async def test_ping_apple_routes_through_bridge(fake_openai, monkeypatch):
    async def status(probe=True):
        return {"supported": True, "available": True, "reason": None}

    async def started():
        return "http://127.0.0.1:5555/v1"
    monkeypatch.setattr(apple_bridge, "bridge_status", status)
    monkeypatch.setattr(apple_bridge, "ensure_started", started)
    monkeypatch.setattr(apple_bridge, "bridge_base_url", lambda: "http://127.0.0.1:5555/v1")
    r = await ai_engine.ping_chat("https://ignored/v1", "", apple_bridge.SENTINEL_MODEL)
    assert r["ok"] is True
    assert fake_openai.calls[0][0]["base_url"] == "http://127.0.0.1:5555/v1"


@pytest.mark.asyncio
async def test_test_connection_uses_plain_english(monkeypatch):
    class Boom:
        class models:
            @staticmethod
            async def list():
                raise _status_error(openai.AuthenticationError, 401)

    async def gc(cfg, tier="strong"):
        return Boom
    monkeypatch.setattr(ai_engine, "get_client", gc)
    r = await ai_engine.test_connection({"ai": {"base_url": "https://x/v1"}})
    assert r["connected"] is False and r["error"] == "That API key was rejected"
    assert "401" in r["detail"]


def test_test_chat_endpoint_passes_values_and_writes_nothing(client, config_path, monkeypatch):
    seen = {}

    async def ping(base_url, api_key, model):
        seen.update(base_url=base_url, api_key=api_key, model=model)
        return {"ok": True, "code": "ok", "message": "", "detail": ""}
    monkeypatch.setattr(ai_engine, "ping_chat", ping)
    before = config_path.read_text()
    r = client.post("/api/ai/test-chat", json={"base_url": " https://openrouter.ai/api/v1 ",
                                               "api_key": settings_router.SECRET_MASK, "model": "x/y:free"})
    assert r.json()["ok"] is True
    assert seen == {"base_url": "https://openrouter.ai/api/v1", "api_key": "old-key", "model": "x/y:free"}
    assert config_path.read_text() == before


# ---------------------------------------------------------------------------
# Onboarding status + save
# ---------------------------------------------------------------------------
def test_onboarding_status_has_on_device(client):
    body = client.get("/api/onboarding/status").json()
    assert body["on_device"] == {"supported": True, "available": False,
                                 "reason": "Apple Intelligence is turned off"}
    assert body["setup_mode"] == "" and body["provider"] == ""


def test_save_local_mode(client, config_path, installs):
    s = "apple-on-device"
    r = client.post("/api/onboarding/ai", json={
        "mode": "local", "models": {"strong": s, "fast": s, "utility": s},
        "scoring_tier": "local-match-model", "nli": True, "triage": True})
    assert r.status_code == 200
    cfg = _saved(config_path)
    assert cfg["setup_mode"] == "local" and cfg["ai_verified"] is True
    assert {t: cfg["ai"]["models"][t]["model"] for t in ("strong", "fast", "utility")} == \
        {"strong": s, "fast": s, "utility": s}
    # base-overlay: the per-tier override survives
    assert cfg["ai"]["models"]["strong"]["base_url"] == "http://tier/v1"
    assert cfg["ai"]["base_url"] == "http://localhost:1234/v1"  # Local leaves the endpoint alone
    assert cfg["ai"]["scoring_tier"] == "local-match-model"
    assert cfg["ai"]["nli_beta"]["enabled"] is True
    assert installs == ["nli", "triage"]


def test_save_cloud_mode(client, config_path, installs):
    r = client.post("/api/onboarding/ai", json={
        "mode": "cloud", "provider": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-or-1", "models": {"strong": "a/b:free", "fast": "a/b:free", "utility": "a/b:free"},
        "scoring_tier": "strong", "triage": False})
    assert r.status_code == 200
    cfg = _saved(config_path)
    assert cfg["setup_mode"] == "cloud"
    assert cfg["ai"]["provider"] == "OpenRouter"
    assert cfg["ai"]["base_url"] == "https://openrouter.ai/api/v1"
    assert cfg["ai"]["api_key"] == "sk-or-1"
    assert cfg["ai"]["models"]["fast"]["model"] == "a/b:free"
    assert "nli_beta" not in cfg["ai"]  # untouched when not sent
    assert installs == []


def test_save_custom_empty_key_unverified(client, config_path, installs):
    r = client.post("/api/onboarding/ai", json={
        "mode": "cloud", "provider": "custom", "base_url": "https://rig.example/v1", "api_key": "",
        "models": {"strong": "qwen"}, "verified": False})
    assert r.status_code == 200
    cfg = _saved(config_path)
    assert cfg["ai"]["api_key"] == "" and cfg["ai"]["provider"] == "custom"
    assert cfg["ai_verified"] is False


def test_save_advanced_mode_keeps_masked_key(client, config_path, installs):
    r = client.post("/api/onboarding/ai", json={
        "mode": "advanced", "provider": "custom", "base_url": "http://192.0.2.9:1234/v1",
        "api_key": settings_router.SECRET_MASK,
        "models": {"strong": "big", "fast": "small", "utility": "small"}, "scoring_tier": "fast", "nli": False})
    assert r.status_code == 200
    cfg = _saved(config_path)
    assert cfg["setup_mode"] == "advanced"
    assert cfg["ai"]["api_key"] == "old-key"
    assert cfg["ai"]["scoring_tier"] == "fast"
    assert cfg["ai"]["nli_beta"]["enabled"] is False


@pytest.mark.parametrize("payload", [{"mode": "later"}, {"mode": "cloud", "scoring_tier": "bogus"}])
def test_save_rejects_bad_values(client, config_path, installs, payload):
    before = config_path.read_text()
    assert client.post("/api/onboarding/ai", json=payload).status_code == 400
    assert config_path.read_text() == before


# ---------------------------------------------------------------------------
# Empty model guard + registry
# ---------------------------------------------------------------------------
def test_model_raises_clear_error_when_empty():
    with pytest.raises(ai_engine.AINotConfigured, match="No AI model is set up — open Settings → AI"):
        ai_engine._model({"ai": {"base_url": "http://x/v1", "models": {"strong": {"model": ""}}}})
    assert ai_engine._model({"ai": {"models": {"fast": {"model": "f"}}}}, "utility") == "f"


def test_resolve_endpoint_works_without_a_model():
    # Listing models on a fresh install must not need a model id.
    assert ai_engine._resolve_endpoint({"ai": {"base_url": "http://x/v1"}}, "strong") == ("http://x/v1", "lm-studio")


def test_registry_rows():
    rows = {s.canonical: s for s in sr.REGISTRY}
    assert rows["ai.provider"].cls == sr.Cls.SYNC and rows["ai.provider"].category == "ai_connection"
    assert rows["ai.provider"].ios == "ai.provider"
    assert rows["setup_mode"].cls == sr.Cls.LOCAL
    assert rows["ai_verified"].cls == sr.Cls.LOCAL
    ids = sr.syncable_canonical_ids()
    assert "ai.provider" in ids and "setup_mode" not in ids


def test_example_config_ships_no_models_or_placeholders():
    from backend.app_state import EXAMPLE_CONFIG_PATH
    cfg = yaml.safe_load(EXAMPLE_CONFIG_PATH.read_text())
    assert all(not t["model"] for t in cfg["ai"]["models"].values())
    assert cfg["ai"]["provider"] == ""
    assert not any(cfg["api_keys"].values())
    assert json.dumps(cfg).find("mistral-7b") == -1


# ---------------------------------------------------------------------------
# Résumé chunking for Apple's 8,000-character input cap
# ---------------------------------------------------------------------------
def _long_resume() -> str:
    roles = []
    for i in range(12):
        bullets = "\n".join(f"- Delivered outcome {i}.{j} " + "x" * 160 for j in range(9))
        roles.append(f"Engineer {i} at Company{i}\n2010 - 2012\n{bullets}\n")
    text = ("Jane Real\njane@real.dev\n\nSUMMARY\nBuilds things.\n\nEXPERIENCE\n" + "\n".join(roles)
            + "\nEDUCATION\nBSc Computer Science, State University, 2009\n\nSKILLS\nPython, Go, SQL\n")
    assert len(text) >= 20000
    return text


@pytest.mark.asyncio
async def test_resume_chunked_for_apple_8k_engine(monkeypatch):
    from backend import resume_parser
    seen = []

    class Fake8k:
        class chat:
            class completions:
                @staticmethod
                async def create(model, messages, **kw):
                    prompt = messages[0]["content"]
                    seen.append(len(prompt))
                    if len(prompt) > 8000:
                        raise openai.BadRequestError("input too long", response=httpx.Response(400, request=_REQ), body=None)
                    out = {"experience": [], "education": [], "skills": []}
                    if "jane@real.dev" in prompt:
                        out.update(full_name="Jane Real", email="jane@real.dev")
                    for i in range(12):
                        if f"Engineer {i} at Company{i}" in prompt:
                            out["experience"].append({"title": f"Engineer {i}", "company": f"Company{i}"})
                    if "State University" in prompt:
                        out["education"].append({"degree": "BSc Computer Science", "school": "State University"})
                    if "Python, Go" in prompt:
                        out["skills"] = ["Python", "Go", "python"]
                    resp = type("R", (), {})()
                    resp.choices = [type("C", (), {"message": type("M", (), {"content": json.dumps(out)})()})()]
                    return resp

    async def gc(cfg, tier="strong"):
        return Fake8k
    monkeypatch.setattr(ai_engine, "get_client", gc)
    cfg = {"ai": {"models": {"strong": {"model": apple_bridge.SENTINEL_MODEL}}}}
    res = await resume_parser.parse_resume(_long_resume(), cfg)
    assert len(seen) >= 3 and max(seen) <= 8000
    p = res["profile"]
    assert p["full_name"] == "Jane Real" and p["email"] == "jane@real.dev"
    assert [e["title"] for e in p["experience"]] == [f"Engineer {i}" for i in range(12)]
    assert p["education"][0]["school"] == "State University"
    assert p["skills"] == ["Python", "Go"]  # de-duplicated case-insensitively
    assert not any("only the first part" in w for w in res["warnings"])


def test_chunk_resume_splits_on_sections_under_limit():
    from backend import resume_parser
    chunks = resume_parser.chunk_resume(_long_resume(), 7000)
    assert all(len(c) <= 7000 for c in chunks)
    assert any(c.startswith("EDUCATION") or "\nEDUCATION" in c for c in chunks)
    assert "".join(chunks).replace("\n", "") .count("Engineer 11 at Company11") == 1


def test_merge_profiles_first_scalar_wins_and_lists_dedupe():
    from backend import resume_parser
    a = resume_parser._sanitize({"full_name": "A", "experience": [{"title": "T", "company": "C", "bullets": ["x"]}]})
    b = resume_parser._sanitize({"full_name": "B", "email": "e@x", "experience": [{"title": "t", "company": "c", "bullets": ["x", "y"]}]})
    m = resume_parser.merge_profiles([a, b])
    assert m["full_name"] == "A" and m["email"] == "e@x"
    assert len(m["experience"]) == 1 and m["experience"][0]["bullets"] == ["x", "y"]


@pytest.mark.asyncio
async def test_resume_parse_failure_returns_real_error(monkeypatch):
    from backend import resume_parser

    async def gc(cfg, tier="strong"):
        raise apple_bridge.BridgeUnavailable("Apple Intelligence is turned off")
    monkeypatch.setattr(ai_engine, "get_client", gc)
    res = await resume_parser.parse_resume("Jane\njane@x.dev", {"ai": {"models": {"strong": {"model": "m"}}}})
    assert "Apple Intelligence is turned off" in res["warnings"][0]


# ---------------------------------------------------------------------------
# Deleting Quick match resets the scoring tier
# ---------------------------------------------------------------------------
def test_delete_quick_match_resets_scoring_tier(client, config_path, monkeypatch):
    from backend.nli import triage_model
    monkeypatch.setattr(triage_model, "delete", lambda: {"state": "not_installed"})
    cfg = _saved(config_path)
    cfg["ai"]["scoring_tier"] = "local-match-model"
    config_path.write_text(yaml.dump(cfg))
    r = client.delete("/api/ai/triage/model").json()
    assert r["scoring_tier_reset"] is True
    assert _saved(config_path)["ai"]["scoring_tier"] == "strong"
    r = client.delete("/api/ai/triage/model").json()
    assert r["scoring_tier_reset"] is False


def test_server_label_names_provider():
    assert ai_engine.server_label({"ai": {"provider": "OpenRouter"}}) == "OpenRouter"
    assert ai_engine.server_label({"ai": {"provider": "custom"}}) == "your AI server"
    assert ai_engine.server_label({}) == "your AI server"
