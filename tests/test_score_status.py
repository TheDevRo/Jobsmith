"""
Batch-scoring progress feed — GET /api/jobs/score-batch/status mirrors the
fetch-status pattern so the frontend can show live progress (header chip +
Score All progress card) instead of inferring state from button disablement.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from backend import app_state as state
from backend import background_tasks as bg
from backend.main import app

IDLE = {"status": "idle", "done": 0, "total": 0, "current": "", "detail": "", "started_at": None, "finished_at": None}


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_state():
    state.running_tasks.clear()
    state.score_status = dict(IDLE)
    yield
    state.running_tasks.clear()
    state.score_status = dict(IDLE)


class TestScoreStatusEndpoint:
    def test_endpoint_returns_idle_shape(self, client):
        resp = client.get("/api/jobs/score-batch/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "idle"
        for key in ("status", "done", "total", "current", "detail", "started_at", "finished_at"):
            assert key in data, f"score status is missing '{key}'"

    def test_post_seeds_scoring_status_synchronously(self, client, monkeypatch):
        # The worker itself is stubbed out — this asserts the *endpoint* flips
        # the feed to "scoring" before any poll can observe a stale state.
        async def _noop(**kwargs):
            pass

        monkeypatch.setattr(bg, "_bg_score_batch", _noop)
        resp = client.post("/api/jobs/score-batch")
        assert resp.status_code == 202
        data = client.get("/api/jobs/score-batch/status").json()
        assert data["status"] == "scoring"
        assert data["started_at"] is not None
        assert data["finished_at"] is None


class TestScoreBatchLifecycle:
    def _patch_worker(self, monkeypatch, jobs, score_side_effect=None):
        served = {"page": False}

        async def fake_get_jobs(**kwargs):
            # First unscored page returns the batch; subsequent pages are empty
            # (jobs "drop out" once scored). limit=1 probe still reports total.
            if kwargs.get("limit") == 1:
                return {"jobs": jobs[:1], "total": len(jobs)}
            if served["page"]:
                return {"jobs": [], "total": 0}
            served["page"] = True
            return {"jobs": list(jobs), "total": len(jobs)}

        async def fake_score(job, profile, cfg):
            if score_side_effect:
                score_side_effect(job)
            return 77.0, "looks good", {}

        async def _anoop(*args, **kwargs):
            pass

        monkeypatch.setattr(bg.db, "get_jobs", fake_get_jobs)
        monkeypatch.setattr(bg.db, "update_job_score", _anoop)
        monkeypatch.setattr(bg.db, "log_activity", _anoop)
        monkeypatch.setattr(bg.ai_engine, "score_job_fit", fake_score)
        monkeypatch.setattr(state, "load_config", lambda: {"salary_estimator": {"auto_on_ingest": False}})

    def test_run_reports_progress_and_finishes_done(self, monkeypatch):
        jobs = [
            {"id": "1", "title": "Engineer", "company": "Acme"},
            {"id": "2", "title": "Analyst", "company": "Globex"},
        ]
        currents = []
        self._patch_worker(monkeypatch, jobs, score_side_effect=lambda j: currents.append(state.score_status["current"]))

        asyncio.run(bg._bg_score_batch())

        s = state.score_status
        assert s["status"] == "done"
        assert s["done"] == 2
        assert s["total"] == 2
        assert s["current"] == ""
        assert s["finished_at"] is not None
        assert currents == ["Engineer · Acme", "Analyst · Globex"]

    def test_cancel_marks_status_cancelled(self, monkeypatch):
        jobs = [{"id": "1", "title": "Engineer", "company": "Acme"}]
        # Cancel while the first job is mid-score: the loop's next iteration
        # (or its post-loop check) must land on "cancelled", not "done".
        self._patch_worker(monkeypatch, jobs, score_side_effect=lambda j: state.cancel_score.set())

        asyncio.run(bg._bg_score_batch())

        assert state.score_status["status"] == "cancelled"
        assert state.score_status["finished_at"] is not None
