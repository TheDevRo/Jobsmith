"""
tests/auto_apply/test_greenhouse_fetcher.py

Offline tests for backend.job_sources.greenhouse.fetch_jobs.
All HTTP calls are mocked — no real network access required.

The fetcher makes exactly ONE request per board: the list endpoint with
?content=true, which returns each job's description and location inline.
"""

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.job_sources.greenhouse import fetch_jobs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session_mock(*payloads, status=200):
    """
    Return a mock aiohttp.ClientSession whose .get() context manager yields
    one response per payload, in call order. Responses expose .text() because
    the fetcher goes through fetch_with_retries, which reads the raw body.
    """
    def _make_resp(payload):
        resp = AsyncMock()
        resp.status = status
        resp.text = AsyncMock(return_value=json.dumps(payload))
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.get = MagicMock(side_effect=[_make_resp(p) for p in payloads])
    return session


_LIST_RESPONSE = {
    "jobs": [
        {
            "id": 42,
            "title": "Security Engineer",
            "company_name": "Acme",
            "content": "&lt;p&gt;We need a &lt;b&gt;security engineer&lt;/b&gt; to protect our systems.&lt;/p&gt;",
            "location": {"name": "Remote"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/42",
            "departments": [{"name": "Engineering"}],
            "updated_at": "2026-03-01T00:00:00Z",
        }
    ]
}

_CONFIG = {
    "search": {
        "greenhouse_boards": ["acme"],
        "lever_companies": [],
        "keywords": ["security engineer"],
        "exclude_keywords": [],
    }
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFetchJobs:

    @pytest.mark.asyncio
    async def test_uses_correct_api_hostname_and_content_param(self):
        """The single request goes to boards-api.greenhouse.io with content=true."""
        session = _make_session_mock(_LIST_RESPONSE)

        with patch("aiohttp.ClientSession", return_value=session):
            await fetch_jobs(_CONFIG)

        first_url = session.get.call_args_list[0][0][0]
        assert first_url.startswith("https://boards-api.greenhouse.io/v1/boards/"), (
            f"Expected boards-api.greenhouse.io, got: {first_url}"
        )
        assert "content=true" in first_url, (
            "List request must ask for inline descriptions (content=true)"
        )

    @pytest.mark.asyncio
    async def test_returns_jobs_from_boards_api(self):
        """Jobs are extracted from response['jobs'] and normalized correctly."""
        session = _make_session_mock(_LIST_RESPONSE)

        with patch("aiohttp.ClientSession", return_value=session):
            jobs = await fetch_jobs(_CONFIG)

        assert len(jobs) == 1
        job = jobs[0]
        assert job["source"] == "greenhouse"
        assert job["title"] == "Security Engineer"
        assert job["company"] == "Acme"
        assert job["external_id"] == "gh-acme-42"
        assert job["url"] == "https://boards.greenhouse.io/acme/jobs/42"
        assert job["location"] == "Remote"
        assert job["is_remote"] is True
        # HTML-escaped content is decoded and tags are stripped
        assert "security engineer" in job["description"].lower()
        assert "<" not in job["description"]
        assert job["tags"] == ["Engineering"]

    @pytest.mark.asyncio
    async def test_single_request_per_board(self):
        """Descriptions come inline — no per-job detail fetches."""
        session = _make_session_mock(_LIST_RESPONSE)

        with patch("aiohttp.ClientSession", return_value=session):
            jobs = await fetch_jobs(_CONFIG)

        assert len(jobs) == 1
        assert session.get.call_count == 1, (
            f"Expected exactly 1 GET per board, got {session.get.call_count}"
        )

    @pytest.mark.asyncio
    async def test_empty_boards_list_logs_info_and_returns_empty(self, caplog):
        """When greenhouse_boards is empty, log INFO and return []."""
        config = {
            "search": {
                "greenhouse_boards": [],
                "lever_companies": [],
                "keywords": ["security"],
                "exclude_keywords": [],
            }
        }

        with patch("aiohttp.ClientSession") as mock_session_cls:
            session = MagicMock()
            session.__aenter__ = AsyncMock(return_value=session)
            session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = session

            with caplog.at_level(logging.INFO, logger="backend.job_sources.greenhouse"):
                jobs = await fetch_jobs(config)

        assert jobs == []
        assert any(
            "No Greenhouse board tokens configured" in r.message
            for r in caplog.records
        ), "Expected INFO about missing board tokens"

    @pytest.mark.asyncio
    async def test_non_200_response_logs_warning_and_skips_board(self, caplog):
        """A non-200 HTTP status logs a WARNING and returns [] for that board."""
        session = _make_session_mock({}, status=404)

        with patch("aiohttp.ClientSession", return_value=session), \
             caplog.at_level(logging.WARNING, logger="backend.job_sources.greenhouse"):
            jobs = await fetch_jobs(_CONFIG)

        assert jobs == []
        assert any(
            ("404" in r.message or "returned" in r.message) and r.levelno == logging.WARNING
            for r in caplog.records
        ), "Expected WARNING about non-200 response"

    @pytest.mark.asyncio
    async def test_zero_matching_jobs_logs_warning(self, caplog):
        """If no jobs survive keyword filtering, a WARNING is emitted."""
        list_resp = {
            "jobs": [
                {
                    "id": 7,
                    "title": "Office Manager",  # won't match "security engineer"
                    "content": "Manage the office.",
                    "location": {"name": "New York"},
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/7",
                    "departments": [],
                    "updated_at": "2026-03-01T00:00:00Z",
                }
            ]
        }

        session = _make_session_mock(list_resp)

        with patch("aiohttp.ClientSession", return_value=session), \
             caplog.at_level(logging.WARNING, logger="backend.job_sources.greenhouse"):
            jobs = await fetch_jobs(_CONFIG)

        assert jobs == []
        assert any(
            "0 jobs" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        ), "Expected WARNING when 0 jobs match filters"

    @pytest.mark.asyncio
    async def test_legacy_greenhouse_companies_key_still_works(self):
        """Backward compat: greenhouse_companies is read if greenhouse_boards absent."""
        config = {
            "search": {
                "greenhouse_companies": ["acme"],  # legacy key
                "lever_companies": [],
                "keywords": ["security engineer"],
                "exclude_keywords": [],
            }
        }
        session = _make_session_mock(_LIST_RESPONSE)

        with patch("aiohttp.ClientSession", return_value=session):
            jobs = await fetch_jobs(config)

        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_example_company_slug_is_skipped(self):
        """'example-company' placeholder tokens are silently ignored."""
        config = {
            "search": {
                "greenhouse_boards": ["example-company"],
                "lever_companies": [],
                "keywords": ["security"],
                "exclude_keywords": [],
            }
        }

        with patch("aiohttp.ClientSession") as mock_session_cls:
            session = MagicMock()
            session.__aenter__ = AsyncMock(return_value=session)
            session.__aexit__ = AsyncMock(return_value=False)
            session.get = MagicMock(side_effect=AssertionError("should not call GET for placeholder"))
            mock_session_cls.return_value = session

            jobs = await fetch_jobs(config)

        assert jobs == []

    @pytest.mark.asyncio
    async def test_exclude_keywords_filter_applied(self):
        """Jobs whose titles contain an exclude keyword are dropped."""
        list_resp = {
            "jobs": [
                {
                    "id": 9,
                    "title": "Senior Staff Security Engineer",
                    "content": "Protect our systems.",
                    "location": {"name": "Remote"},
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/9",
                    "departments": [],
                    "updated_at": "",
                }
            ]
        }
        config = {
            "search": {
                "greenhouse_boards": ["acme"],
                "lever_companies": [],
                "keywords": ["security engineer"],
                "exclude_keywords": ["senior staff"],
            }
        }
        session = _make_session_mock(list_resp)

        with patch("aiohttp.ClientSession", return_value=session):
            jobs = await fetch_jobs(config)

        assert jobs == []

    @pytest.mark.asyncio
    async def test_known_ids_skip_reemission(self):
        """Jobs already in the DB are not re-emitted."""
        session = _make_session_mock(_LIST_RESPONSE)

        with patch("aiohttp.ClientSession", return_value=session):
            jobs = await fetch_jobs(_CONFIG, known_ids={"gh-acme-42"})

        assert jobs == []
