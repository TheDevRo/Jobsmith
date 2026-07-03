"""
Integration tests for the apply-type detection pipeline.

Covers:
  - detect_all_apply_types() service function (DB mocked)
  - POST /api/detect-apply-types endpoint (202 response)
  - GET  /api/detect-apply-types/status endpoint
  - easy_apply_only filter now matches apply_type IN ('easy_apply', 'quick_apply')
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_job(job_id: str, source: str, url: str, **extra) -> dict:
    return {
        "id": job_id,
        "source": source,
        "title": "Test Job",
        "company": "Test Co",
        "url": url,
        "is_easy_apply": False,
        "apply_type": "unknown",
        **extra,
    }


# ===========================================================================
# Service — detect_all_apply_types()
# ===========================================================================

class TestDetectAllApplyTypesService:
    """Tests for backend.services.apply_type_detector.detect_all_apply_types."""

    @pytest.mark.asyncio
    async def test_returns_correct_summary_shape(self):
        jobs = [
            _make_job("j1", "linkedin", "https://www.linkedin.com/jobs/apply/1/"),
            _make_job("j2", "indeed", "https://www.indeed.com/viewjob?jk=x"),
        ]
        with (
            patch("backend.services.apply_type_detector.db.get_unclassified_jobs", new=AsyncMock(return_value=jobs)),
            patch("backend.services.apply_type_detector.db.update_job_apply_type", new=AsyncMock()),
        ):
            from backend.services.apply_type_detector import detect_all_apply_types
            summary = await detect_all_apply_types()

        assert set(summary.keys()) == {"processed", "easy_apply", "quick_apply", "external", "unknown"}

    @pytest.mark.asyncio
    async def test_linkedin_easy_apply_url_classified(self):
        jobs = [_make_job("j1", "linkedin", "https://www.linkedin.com/jobs/apply/123/")]
        updates = []

        async def _capture_update(job_id, apply_type):
            updates.append((job_id, apply_type))

        with (
            patch("backend.services.apply_type_detector.db.get_unclassified_jobs", new=AsyncMock(return_value=jobs)),
            patch("backend.services.apply_type_detector.db.update_job_apply_type", side_effect=_capture_update),
        ):
            from backend.services.apply_type_detector import detect_all_apply_types
            summary = await detect_all_apply_types()

        assert updates == [("j1", "easy_apply")]
        assert summary["easy_apply"] == 1
        assert summary["processed"] == 1

    @pytest.mark.asyncio
    async def test_indeed_viewjob_url_stays_unknown(self):
        jobs = [_make_job("j2", "indeed", "https://www.indeed.com/viewjob?jk=abc")]
        updates = []

        async def _capture_update(job_id, apply_type):
            updates.append((job_id, apply_type))

        with (
            patch("backend.services.apply_type_detector.db.get_unclassified_jobs", new=AsyncMock(return_value=jobs)),
            patch("backend.services.apply_type_detector.db.update_job_apply_type", side_effect=_capture_update),
        ):
            from backend.services.apply_type_detector import detect_all_apply_types
            summary = await detect_all_apply_types()

        assert updates == [("j2", "unknown")]
        assert summary["unknown"] == 1

    @pytest.mark.asyncio
    async def test_greenhouse_url_classified_easy_apply(self):
        jobs = [_make_job("j3", "greenhouse", "https://boards.greenhouse.io/co/jobs/1")]
        updates = []

        async def _capture(job_id, apply_type):
            updates.append((job_id, apply_type))

        with (
            patch("backend.services.apply_type_detector.db.get_unclassified_jobs", new=AsyncMock(return_value=jobs)),
            patch("backend.services.apply_type_detector.db.update_job_apply_type", side_effect=_capture),
        ):
            from backend.services.apply_type_detector import detect_all_apply_types
            summary = await detect_all_apply_types()

        assert updates == [("j3", "easy_apply")]
        assert summary["easy_apply"] == 1

    @pytest.mark.asyncio
    async def test_lever_url_classified_easy_apply(self):
        jobs = [_make_job("j4", "lever", "https://jobs.lever.co/acme/abc123")]
        updates = []

        async def _capture(job_id, apply_type):
            updates.append((job_id, apply_type))

        with (
            patch("backend.services.apply_type_detector.db.get_unclassified_jobs", new=AsyncMock(return_value=jobs)),
            patch("backend.services.apply_type_detector.db.update_job_apply_type", side_effect=_capture),
        ):
            from backend.services.apply_type_detector import detect_all_apply_types
            summary = await detect_all_apply_types()

        assert updates == [("j4", "easy_apply")]
        assert summary["easy_apply"] == 1

    @pytest.mark.asyncio
    async def test_usajobs_url_classified_easy_apply(self):
        jobs = [_make_job("j5", "usajobs", "https://www.usajobs.gov/job/123456")]
        updates = []

        async def _capture(job_id, apply_type):
            updates.append((job_id, apply_type))

        with (
            patch("backend.services.apply_type_detector.db.get_unclassified_jobs", new=AsyncMock(return_value=jobs)),
            patch("backend.services.apply_type_detector.db.update_job_apply_type", side_effect=_capture),
        ):
            from backend.services.apply_type_detector import detect_all_apply_types
            summary = await detect_all_apply_types()

        assert updates == [("j5", "easy_apply")]
        assert summary["easy_apply"] == 1

    @pytest.mark.asyncio
    async def test_external_url_classified_external(self):
        jobs = [_make_job("j6", "greenhouse", "https://careers.acmecorp.com/jobs/1")]
        updates = []

        async def _capture(job_id, apply_type):
            updates.append((job_id, apply_type))

        with (
            patch("backend.services.apply_type_detector.db.get_unclassified_jobs", new=AsyncMock(return_value=jobs)),
            patch("backend.services.apply_type_detector.db.update_job_apply_type", side_effect=_capture),
        ):
            from backend.services.apply_type_detector import detect_all_apply_types
            summary = await detect_all_apply_types()

        assert updates == [("j6", "external")]
        assert summary["external"] == 1

    @pytest.mark.asyncio
    async def test_unknown_source_stays_unknown(self):
        """A source with no registered detector produces 'unknown'."""
        jobs = [_make_job("j7", "monster", "https://www.monster.com/jobs/1")]
        updates = []

        async def _capture(job_id, apply_type):
            updates.append((job_id, apply_type))

        with (
            patch("backend.services.apply_type_detector.db.get_unclassified_jobs", new=AsyncMock(return_value=jobs)),
            patch("backend.services.apply_type_detector.db.update_job_apply_type", side_effect=_capture),
        ):
            from backend.services.apply_type_detector import detect_all_apply_types
            summary = await detect_all_apply_types()

        assert updates == [("j7", "unknown")]
        assert summary["unknown"] == 1

    @pytest.mark.asyncio
    async def test_empty_job_list_returns_zero_summary(self):
        with (
            patch("backend.services.apply_type_detector.db.get_unclassified_jobs", new=AsyncMock(return_value=[])),
            patch("backend.services.apply_type_detector.db.update_job_apply_type", new=AsyncMock()),
        ):
            from backend.services.apply_type_detector import detect_all_apply_types
            summary = await detect_all_apply_types()

        assert summary["processed"] == 0
        assert summary["easy_apply"] == 0

    @pytest.mark.asyncio
    async def test_cancel_event_stops_processing(self):
        # 10 jobs; cancel after processing first one
        jobs = [_make_job(f"j{i}", "usajobs", f"https://www.usajobs.gov/job/{i}") for i in range(10)]
        processed_ids = []
        cancel = asyncio.Event()

        async def _capture(job_id, apply_type):
            processed_ids.append(job_id)
            # Set cancel after the first update so remaining 9 are skipped
            cancel.set()

        with (
            patch("backend.services.apply_type_detector.db.get_unclassified_jobs", new=AsyncMock(return_value=jobs)),
            patch("backend.services.apply_type_detector.db.update_job_apply_type", side_effect=_capture),
        ):
            from backend.services.apply_type_detector import detect_all_apply_types
            summary = await detect_all_apply_types(cancel_event=cancel)

        assert summary["processed"] < 10

    @pytest.mark.asyncio
    async def test_on_progress_callback_invoked(self):
        jobs = [_make_job("j1", "usajobs", "https://www.usajobs.gov/job/1")]
        progress_calls = []

        with (
            patch("backend.services.apply_type_detector.db.get_unclassified_jobs", new=AsyncMock(return_value=jobs)),
            patch("backend.services.apply_type_detector.db.update_job_apply_type", new=AsyncMock()),
        ):
            from backend.services.apply_type_detector import detect_all_apply_types
            await detect_all_apply_types(on_progress=lambda s: progress_calls.append(dict(s)))

        assert len(progress_calls) == 1
        assert progress_calls[0]["processed"] == 1

    @pytest.mark.asyncio
    async def test_multi_source_batch_summary_counts(self):
        jobs = [
            _make_job("j1", "linkedin", "https://www.linkedin.com/jobs/apply/1/"),
            _make_job("j2", "greenhouse", "https://boards.greenhouse.io/co/jobs/2"),
            _make_job("j3", "greenhouse", "https://careers.custom.com/jobs/3"),
            _make_job("j4", "indeed", "https://www.indeed.com/viewjob?jk=4"),
        ]
        with (
            patch("backend.services.apply_type_detector.db.get_unclassified_jobs", new=AsyncMock(return_value=jobs)),
            patch("backend.services.apply_type_detector.db.update_job_apply_type", new=AsyncMock()),
        ):
            from backend.services.apply_type_detector import detect_all_apply_types
            summary = await detect_all_apply_types()

        assert summary["processed"] == 4
        assert summary["easy_apply"] == 2   # j1, j2
        assert summary["external"] == 1     # j3
        assert summary["unknown"] == 1      # j4


# ===========================================================================
# API endpoint tests
# ===========================================================================

@pytest.fixture()
def api_client():
    from backend.main import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


class TestDetectApplyTypesEndpoint:
    def test_post_returns_202(self, api_client):
        with (
            patch("backend.background_tasks._bg_detect_apply_types", new=AsyncMock()),
        ):
            resp = api_client.post("/api/detect-apply-types")
        assert resp.status_code == 202

    def test_post_returns_message_key(self, api_client):
        with patch("backend.background_tasks._bg_detect_apply_types", new=AsyncMock()):
            resp = api_client.post("/api/detect-apply-types")
        assert "message" in resp.json()

    def test_status_endpoint_returns_200(self, api_client):
        resp = api_client.get("/api/detect-apply-types/status")
        assert resp.status_code == 200

    def test_status_endpoint_returns_expected_keys(self, api_client):
        resp = api_client.get("/api/detect-apply-types/status")
        data = resp.json()
        for key in ("active", "processed", "easy_apply", "quick_apply", "external", "unknown"):
            assert key in data, f"Missing key: {key}"

    def test_cancel_endpoint_returns_200(self, api_client):
        resp = api_client.post("/api/detect-apply-types/cancel")
        assert resp.status_code == 200

    def test_operations_status_includes_detect_apply_types(self, api_client):
        resp = api_client.get("/api/operations/status")
        assert resp.status_code == 200
        assert "detect_apply_types" in resp.json()


# ===========================================================================
# easy_apply_only filter — now covers apply_type as well
# ===========================================================================

class TestEasyApplyOnlyFilter:
    """Verify the database filter SQL includes the new apply_type condition."""

    def test_easy_apply_only_filter_includes_apply_type(self):
        """The easy_apply_only branch must reference apply_type."""
        import inspect
        from backend.database import get_jobs
        source = inspect.getsource(get_jobs)
        # New condition must cover apply_type
        assert "apply_type" in source
        assert "easy_apply" in source
        assert "quick_apply" in source

    def test_easy_apply_only_filter_also_keeps_is_easy_apply(self):
        """Old is_easy_apply flag must still be matched (backward compat)."""
        import inspect
        from backend.database import get_jobs
        source = inspect.getsource(get_jobs)
        assert "is_easy_apply" in source
