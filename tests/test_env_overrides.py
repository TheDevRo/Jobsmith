"""Tests for the JOBSMITH_* env-var config overrides in backend/app_state.py."""

from backend.app_state import _apply_env_overrides


def test_no_env_leaves_config_untouched(monkeypatch):
    for var in ("JOBSMITH_AI_BASE_URL", "JOBSMITH_AI_API_KEY", "JOBSMITH_FLARESOLVERR_URL"):
        monkeypatch.delenv(var, raising=False)
    cfg = {"ai": {"base_url": "http://localhost:1234/v1", "api_key": "lm-studio"}}
    out = _apply_env_overrides(dict(cfg))
    assert out == cfg


def test_ai_overrides_applied(monkeypatch):
    monkeypatch.setenv("JOBSMITH_AI_BASE_URL", "http://host.docker.internal:1234/v1")
    monkeypatch.setenv("JOBSMITH_AI_API_KEY", "override-key")
    cfg = {"ai": {"base_url": "http://localhost:1234/v1", "api_key": "lm-studio", "model": "m"}}
    out = _apply_env_overrides(cfg)
    assert out["ai"]["base_url"] == "http://host.docker.internal:1234/v1"
    assert out["ai"]["api_key"] == "override-key"
    assert out["ai"]["model"] == "m"  # untouched keys survive


def test_missing_section_created(monkeypatch):
    monkeypatch.setenv("JOBSMITH_FLARESOLVERR_URL", "http://byparr:8191")
    out = _apply_env_overrides({})
    assert out["flaresolverr"]["url"] == "http://byparr:8191"


def test_none_section_replaced(monkeypatch):
    # YAML "flaresolverr:" with nothing under it loads as None
    monkeypatch.setenv("JOBSMITH_FLARESOLVERR_URL", "http://byparr:8191")
    out = _apply_env_overrides({"flaresolverr": None})
    assert out["flaresolverr"]["url"] == "http://byparr:8191"


def test_empty_env_ignored(monkeypatch):
    monkeypatch.setenv("JOBSMITH_AI_BASE_URL", "")
    cfg = {"ai": {"base_url": "http://localhost:1234/v1"}}
    out = _apply_env_overrides(cfg)
    assert out["ai"]["base_url"] == "http://localhost:1234/v1"
