"""
REL-07 — endpoints that kick off a background worker used to overwrite
state.running_tasks[key] unconditionally, so a double-POST spawned a second
racing worker and orphaned the first handle (which could then never be
cancelled). They now return 409 while a run is in flight.
"""

import pytest
from fastapi.testclient import TestClient

from backend import app_state as state
from backend.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_tasks():
    state.running_tasks.clear()
    yield
    state.running_tasks.clear()


# (endpoint, running_tasks key, json body)
GUARDED = [
    ("/api/jobs/fetch", "fetch", None),
    ("/api/jobs/score-batch", "score_batch", None),
    ("/api/jobs/estimate-salaries", "estimate_salaries", None),
    ("/api/detect-apply-types", "detect_apply_types", None),
    ("/api/jobs/tailor-batch", "tailor_batch", {"min_score": 50.0}),
    ("/api/webhooks/trigger-tailor", "tailor_batch", None),
]


class _Task:
    """Stands in for an asyncio.Task without needing a running loop."""

    def __init__(self, done: bool):
        self._done = done

    def done(self) -> bool:
        return self._done


class TestTaskMutex:
    @pytest.mark.parametrize("endpoint,key,body", GUARDED)
    def test_second_call_while_running_is_409(self, client, endpoint, key, body):
        state.running_tasks[key] = _Task(done=False)
        resp = client.post(endpoint, json=body) if body else client.post(endpoint)
        assert resp.status_code == 409, f"{endpoint} did not guard against a double-POST"
        assert "already running" in resp.json()["detail"].lower()

    @pytest.mark.parametrize("endpoint,key,body", GUARDED)
    def test_a_finished_task_does_not_block_the_next_run(self, client, endpoint, key, body):
        state.running_tasks[key] = _Task(done=True)
        # Must be let through (the guard is "in flight", not "ever ran").
        resp = client.post(endpoint, json=body) if body else client.post(endpoint)
        assert resp.status_code != 409, f"{endpoint} blocks even after the run finished"


class TestTaskRunningHelper:
    def test_missing_key_is_not_running(self):
        assert state.task_running("nope") is False

    def test_finished_task_is_not_running(self):
        state.running_tasks["k"] = _Task(done=True)
        assert state.task_running("k") is False

    def test_in_flight_task_is_running(self):
        state.running_tasks["k"] = _Task(done=False)
        assert state.task_running("k") is True
