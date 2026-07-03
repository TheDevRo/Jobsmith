"""
tests/test_honesty_level.py

Tests for:
- HonestyLevel enum and EmbellishmentLog Pydantic model
- GET /api/settings/honesty-level
- PUT /api/settings/honesty-level
- jobs.embellishment_log DB helpers (get/set)
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

from backend.auto_apply.models import (
    EmbellishmentChange,
    EmbellishmentLog,
    HonestyLevel,
)


class TestHonestyLevel:
    def test_valid_values(self):
        assert HonestyLevel.HONEST.value == "honest"
        assert HonestyLevel.TAILORED.value == "tailored"
        assert HonestyLevel.EMBELLISHED.value == "embellished"
        assert HonestyLevel.FABRICATED.value == "fabricated"

    def test_str_coercion(self):
        assert HonestyLevel("honest") is HonestyLevel.HONEST
        assert HonestyLevel("fabricated") is HonestyLevel.FABRICATED

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            HonestyLevel("made_up")


class TestEmbellishmentLog:
    def _make_log(self, level: str = "honest") -> EmbellishmentLog:
        return EmbellishmentLog(
            honesty_level=HonestyLevel(level),
            resume_changes=[
                EmbellishmentChange(
                    field="summary",
                    original="Led a team.",
                    modified="Led a cross-functional team of 12 engineers.",
                )
            ],
            cover_letter_changes=[],
            generated_at=datetime(2026, 3, 31, 12, 0, 0, tzinfo=timezone.utc),
        )

    def test_round_trip_json(self):
        log = self._make_log("embellished")
        data = json.loads(log.model_dump_json())
        restored = EmbellishmentLog.model_validate(data)
        assert restored.honesty_level == HonestyLevel.EMBELLISHED
        assert len(restored.resume_changes) == 1
        assert restored.resume_changes[0].field == "summary"

    def test_empty_changes(self):
        log = EmbellishmentLog(
            honesty_level=HonestyLevel.HONEST,
            generated_at=datetime.now(timezone.utc),
        )
        assert log.resume_changes == []
        assert log.cover_letter_changes == []

    def test_generated_at_required(self):
        with pytest.raises(Exception):
            EmbellishmentLog(honesty_level=HonestyLevel.HONEST)


# ---------------------------------------------------------------------------
# Database helper tests
# ---------------------------------------------------------------------------

import aiosqlite
import backend.database as db_module


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Point the database module at a fresh temp file for each test."""
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    asyncio.run(db_module.init_db())
    return db_path


def _run(coro):
    return asyncio.run(coro)


def _insert_job(db_path: Path, job_id: str) -> None:
    async def _do():
        conn = await aiosqlite.connect(str(db_path))
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute(
            "INSERT INTO jobs (id, source, external_id, title) VALUES (?, ?, ?, ?)",
            (job_id, "test", job_id, "Test Job"),
        )
        await conn.commit()
        await conn.close()

    _run(_do())


class TestEmbellishmentLogDB:
    def test_get_returns_none_when_unset(self, tmp_db):
        _insert_job(tmp_db, "job-1")
        result = _run(db_module.get_embellishment_log("job-1"))
        assert result is None

    def test_get_returns_none_for_missing_job(self, tmp_db):
        result = _run(db_module.get_embellishment_log("nonexistent"))
        assert result is None

    def test_set_and_get_round_trip(self, tmp_db):
        _insert_job(tmp_db, "job-2")
        payload = {
            "honesty_level": "tailored",
            "resume_changes": [{"field": "title", "original": "Dev", "modified": "Senior Dev"}],
            "cover_letter_changes": [],
            "generated_at": "2026-03-31T12:00:00+00:00",
        }
        updated = _run(db_module.set_embellishment_log("job-2", payload))
        assert updated is True

        result = _run(db_module.get_embellishment_log("job-2"))
        assert result is not None
        assert result["honesty_level"] == "tailored"
        assert result["resume_changes"][0]["field"] == "title"

    def test_set_returns_false_for_missing_job(self, tmp_db):
        updated = _run(db_module.set_embellishment_log("ghost", {"honesty_level": "honest"}))
        assert updated is False

    def test_overwrite(self, tmp_db):
        _insert_job(tmp_db, "job-3")
        _run(db_module.set_embellishment_log("job-3", {"honesty_level": "honest", "resume_changes": [], "cover_letter_changes": [], "generated_at": "2026-03-31T00:00:00Z"}))
        _run(db_module.set_embellishment_log("job-3", {"honesty_level": "fabricated", "resume_changes": [], "cover_letter_changes": [], "generated_at": "2026-03-31T01:00:00Z"}))
        result = _run(db_module.get_embellishment_log("job-3"))
        assert result["honesty_level"] == "fabricated"


# ---------------------------------------------------------------------------
# API endpoint tests (FastAPI TestClient)
# ---------------------------------------------------------------------------

import yaml
from fastapi.testclient import TestClient


@pytest.fixture()
def tmp_config(tmp_path):
    """Write a minimal config.yaml to a temp dir and patch the module."""
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

    # Avoid touching the real filesystem for resumes dir
    monkeypatch.setattr("backend.app_state.RESUMES_DIR", tmp_config.parent / "resumes")

    with TestClient(main_module.app, raise_server_exceptions=True) as client:
        yield client


class TestHonestyLevelAPI:
    def test_get_default(self, api_client):
        resp = api_client.get("/api/settings/honesty-level")
        assert resp.status_code == 200
        assert resp.json()["honesty_level"] == "honest"

    def test_put_valid(self, api_client):
        resp = api_client.put(
            "/api/settings/honesty-level", json={"honesty_level": "tailored"}
        )
        assert resp.status_code == 200
        assert resp.json()["honesty_level"] == "tailored"

    def test_put_persists(self, api_client):
        api_client.put("/api/settings/honesty-level", json={"honesty_level": "embellished"})
        resp = api_client.get("/api/settings/honesty-level")
        assert resp.json()["honesty_level"] == "embellished"

    def test_put_all_valid_levels(self, api_client):
        for level in ("honest", "tailored", "embellished", "fabricated"):
            resp = api_client.put(
                "/api/settings/honesty-level", json={"honesty_level": level}
            )
            assert resp.status_code == 200, f"Failed for level: {level}"

    def test_put_invalid_level(self, api_client):
        resp = api_client.put(
            "/api/settings/honesty-level", json={"honesty_level": "lying"}
        )
        assert resp.status_code == 400
