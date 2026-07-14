"""
Smoke tests for backend/routers/settings.py — the config read/write router.

Offline: the config is backed by a temp YAML file (no live services, no DB).
Covers the /api/config round-trip and that saving AI settings clears the
cached AsyncOpenAI clients (so a rotated key/base_url doesn't leak FDs).
"""

import yaml
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import app_state as state
from backend import ai_engine
from backend.routers import settings as settings_router


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.dump(
            {
                "profile": {"full_name": "Test User", "email": "test@example.com"},
                "search": {"keywords": ["engineer"]},
                "ai": {"base_url": "http://localhost:1234/v1", "api_key": "old-key"},
            }
        )
    )
    monkeypatch.setattr(state, "CONFIG_PATH", p)
    return p


@pytest.fixture
def client(config_path):
    app = FastAPI()
    app.include_router(settings_router.router)
    return TestClient(app)


def test_get_config_returns_curated_shape(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    for key in ("search", "auto_apply", "ai", "profile", "api_keys"):
        assert key in body
    assert body["profile"]["full_name"] == "Test User"
    assert body["ai"]["base_url"] == "http://localhost:1234/v1"


def test_post_config_persists_ai_change(client, config_path):
    r = client.post("/api/config", json={"ai": {"base_url": "http://192.0.2.7:1234/v1"}})
    assert r.status_code == 200
    saved = yaml.safe_load(config_path.read_text())
    assert saved["ai"]["base_url"] == "http://192.0.2.7:1234/v1"
    # existing keys are merged, not clobbered
    assert saved["ai"]["api_key"] == "old-key"


def test_post_ai_config_clears_client_cache(client, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(ai_engine, "clear_clients", lambda: called.__setitem__("n", called["n"] + 1))
    r = client.post("/api/config", json={"ai": {"api_key": "new-key"}})
    assert r.status_code == 200
    assert called["n"] == 1


def test_post_non_ai_config_does_not_clear_cache(client, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(ai_engine, "clear_clients", lambda: called.__setitem__("n", called["n"] + 1))
    r = client.post("/api/config", json={"search": {"keywords": ["designer"]}})
    assert r.status_code == 200
    assert called["n"] == 0
