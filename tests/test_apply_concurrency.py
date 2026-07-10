"""
Tests for the single-flight guard on the apply endpoints
(backend/routers/applications.py).

The orchestrator drives the browser through module-level singletons, so only
one apply may run at a time; a duplicate POST must be rejected with 409 rather
than spawning a second task that stomps the live one.
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import app_state as state
from backend import background_tasks as bg
from backend.routers import applications as apps_router


class _FakeRunningTask:
    def done(self):
        return False


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(state, "running_tasks", {})

    async def _noop_apply(app_id):
        return None

    monkeypatch.setattr(bg, "_bg_apply", _noop_apply)

    app = FastAPI()
    app.include_router(apps_router.router)
    return TestClient(app)


def test_apply_triggers_when_idle(client):
    r = client.post("/api/applications/abc/apply")
    assert r.status_code == 202
    assert "apply" in state.running_tasks
    assert "apply:abc" in state.running_tasks


def test_duplicate_apply_returns_409(client):
    # Simulate an apply already in flight.
    state.running_tasks["apply"] = _FakeRunningTask()
    r = client.post("/api/applications/abc/apply")
    assert r.status_code == 409


def test_apply_allowed_again_after_task_done(client):
    class _DoneTask:
        def done(self):
            return True

    state.running_tasks["apply"] = _DoneTask()
    r = client.post("/api/applications/abc/apply")
    assert r.status_code == 202
