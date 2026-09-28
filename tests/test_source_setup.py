"""
Sources without setup: per-source setup state (job_sources.source_details and
GET /api/sources), the keyed-source test endpoint, and the wizard's unsaved
profile/search overrides on the company suggester.

Offline: config is a temp YAML file; every outbound call is stubbed.
"""

import asyncio

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import app_state as state
from backend import job_sources
from backend.routers import jobs as jobs_router
from backend.routers.settings import SECRET_MASK


def _by_name(details):
    return {d["name"]: d for d in details}


def test_source_details_defaults_for_a_fresh_install():
    d = _by_name(job_sources.source_details({}))
    assert list(d) == job_sources.get_source_names()
    for name in ("remoteok", "weworkremotely", "arbeitnow", "linkedin"):
        assert d[name]["kind"] == "feed" and d[name]["default_on"]
    for name in ("adzuna", "usajobs"):
        assert d[name]["kind"] == "keyed"
        assert not d[name]["configured"] and not d[name]["default_on"]
    assert d["indeed"]["kind"] == "browser" and not d["indeed"]["default_on"]
    assert "slow" in d["indeed"]["note"].lower() and "slow" in d["linkedin"]["note"].lower()
    assert not d["greenhouse"]["configured"]


def test_source_details_treats_example_placeholders_as_unset():
    cfg = {
        "api_keys": {"adzuna_app_id": "your-app-id", "adzuna_app_key": "your-app-key",
                     "usajobs_email": "me@x.io", "usajobs_api_key": "real"},
        "search": {"greenhouse_boards": ["example-company"], "ashby_boards": ["linear"],
                   "indeed": {"enabled": True}},
    }
    d = _by_name(job_sources.source_details(cfg))
    assert not d["adzuna"]["configured"]
    assert d["usajobs"]["configured"] and d["usajobs"]["default_on"]
    assert not d["greenhouse"]["configured"]
    assert d["ashby"]["configured"]
    assert d["indeed"]["default_on"]


def test_lever_counts_toward_the_greenhouse_source():
    d = _by_name(job_sources.source_details({"search": {"lever_companies": ["zapier"]}}))
    assert d["greenhouse"]["configured"]


def test_adzuna_skips_placeholder_keys_without_a_request(monkeypatch):
    from backend.job_sources import adzuna

    def _boom(*a, **k):
        raise AssertionError("no request expected")

    monkeypatch.setattr(adzuna.aiohttp, "ClientSession", _boom)
    cfg = {"api_keys": {"adzuna_app_id": "your-app-id", "adzuna_app_key": "your-app-key"}}
    assert asyncio.run(adzuna.fetch_jobs(cfg)) == []


# ---- endpoints ----

@pytest.fixture
def config_path(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.dump({
        "profile": {"full_name": "Saved", "summary": "saved summary"},
        "search": {"keywords": ["saved kw"], "lever_companies": ["zapier"]},
        "api_keys": {"adzuna_app_id": "saved-id", "adzuna_app_key": "saved-key"},
    }))
    monkeypatch.setattr(state, "CONFIG_PATH", p)
    return p


@pytest.fixture
def client(config_path):
    app = FastAPI()
    app.include_router(jobs_router.router)
    return TestClient(app)


def test_list_sources_keeps_names_and_adds_details(client):
    body = client.get("/api/sources").json()
    assert body["sources"] == job_sources.get_source_names()
    d = _by_name(body["details"])
    assert d["adzuna"]["configured"] and d["adzuna"]["default_on"]
    assert d["greenhouse"]["configured"]


class _Resp:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """Records GETs and answers with a fixed status."""
    seen: list = []
    status = 200

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, params=None, headers=None):
        _Session.seen.append({"url": url, "params": params or {}, "headers": headers or {}})
        return _Resp(_Session.status)


@pytest.fixture
def fake_http(monkeypatch):
    import aiohttp
    _Session.seen = []
    _Session.status = 200
    monkeypatch.setattr(aiohttp, "ClientSession", _Session)
    return _Session


def test_test_key_uses_form_values(client, fake_http):
    r = client.post("/api/sources/test-key", json={
        "source": "adzuna", "adzuna_app_id": "new-id", "adzuna_app_key": "new-key"}).json()
    assert r["ok"]
    assert fake_http.seen[0]["params"]["app_id"] == "new-id"


def test_test_key_mask_means_saved_value(client, fake_http):
    r = client.post("/api/sources/test-key", json={
        "source": "adzuna", "adzuna_app_id": "saved-id", "adzuna_app_key": SECRET_MASK}).json()
    assert r["ok"]
    assert fake_http.seen[0]["params"]["app_key"] == "saved-key"


def test_test_key_blank_is_not_the_saved_key(client, fake_http):
    r = client.post("/api/sources/test-key", json={"source": "adzuna", "adzuna_app_id": "", "adzuna_app_key": ""}).json()
    assert not r["ok"] and fake_http.seen == []


def test_test_key_reports_rejection(client, fake_http):
    fake_http.status = 401
    r = client.post("/api/sources/test-key", json={
        "source": "usajobs", "usajobs_email": "me@x.io", "usajobs_api_key": "bad"}).json()
    assert not r["ok"] and "Rejected" in r["message"]
    assert fake_http.seen[0]["headers"]["Authorization-Key"] == "bad"


def test_test_key_unknown_source(client, fake_http):
    assert client.post("/api/sources/test-key", json={"source": "linkedin"}).status_code == 400


def test_suggest_companies_uses_wizard_overrides(client, config_path, monkeypatch):
    from backend import ai_engine, database

    seen = {}

    async def _signals(**k):
        return []

    async def _suggest(profile, search_cfg, liked, exclude, cfg):
        seen["profile"], seen["search"] = profile, search_cfg
        return [{"name": "Acme", "why": "fits"}]

    async def _detect(name, session):
        return [{"source": "greenhouse", "slug": "acme", "jobs": 3, "config_key": "greenhouse_boards"}]

    monkeypatch.setattr(database, "get_company_signals", _signals)
    monkeypatch.setattr(ai_engine, "suggest_companies", _suggest)
    monkeypatch.setattr(jobs_router, "_detect_boards_for", _detect)

    r = client.post("/api/sources/suggest-companies", json={
        "exclude": [], "profile": {"summary": "wizard summary"}, "search": {"keywords": ["wizard kw"]}}).json()
    assert [s["name"] for s in r["suggestions"]] == ["Acme"]
    assert seen["profile"]["summary"] == "wizard summary"
    assert seen["profile"]["full_name"] == "Saved"  # merged over the saved profile
    assert seen["search"]["keywords"] == ["wizard kw"]
    assert seen["search"]["lever_companies"] == ["zapier"]  # watched list kept
    # Nothing written: overrides are for this call only.
    assert yaml.safe_load(config_path.read_text())["search"]["keywords"] == ["saved kw"]
