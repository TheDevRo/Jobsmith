"""
tests/auto_apply/test_ashby_fetcher.py

Offline tests for backend.job_sources.ashby.fetch_jobs.
All HTTP calls are mocked — no real network access required.

The fetcher makes exactly ONE request per board: the posting API with
?includeCompensation=true, which returns each job's description, location,
and compensation inline.
"""

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.job_sources.ashby import fetch_jobs


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


# Mirrors the real posting-api response shape (verified against
# api.ashbyhq.com/posting-api/job-board/openai on 2026-07-02).
_LIST_RESPONSE = {
    "apiVersion": "1",
    "jobs": [
        {
            "id": "00207abc-49b7-465c-a219-f7c1140f8047",
            "title": "Security Engineer",
            "department": "Security",
            "team": "Detection",
            "employmentType": "FullTime",
            "location": "San Francisco",
            "secondaryLocations": [],
            "publishedAt": "2026-06-15T01:25:39.198+00:00",
            "isListed": True,
            "isRemote": True,
            "jobUrl": "https://jobs.ashbyhq.com/acme/00207abc-49b7-465c-a219-f7c1140f8047",
            "applyUrl": "https://jobs.ashbyhq.com/acme/00207abc-49b7-465c-a219-f7c1140f8047/application",
            "descriptionHtml": "<p>We need a <b>security engineer</b> to protect our systems.</p>",
            "descriptionPlain": "We need a security engineer to protect our systems.",
            "compensation": {
                "compensationTierSummary": "$185K – $325K • Offers Equity",
                "summaryComponents": [
                    {
                        "compensationType": "EquityCashValue",
                        "interval": "1 YEAR",
                        "currencyCode": "USD",
                        "minValue": None,
                        "maxValue": None,
                    },
                    {
                        "compensationType": "Salary",
                        "interval": "1 YEAR",
                        "currencyCode": "USD",
                        "minValue": 185000,
                        "maxValue": 325000,
                    },
                ],
            },
        }
    ],
}

_CONFIG = {
    "search": {
        "ashby_boards": ["acme"],
        "keywords": ["security engineer"],
        "exclude_keywords": [],
    }
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFetchJobs:

    @pytest.mark.asyncio
    async def test_uses_posting_api_with_compensation_param(self):
        """The single request goes to api.ashbyhq.com with includeCompensation=true."""
        session = _make_session_mock(_LIST_RESPONSE)

        with patch("aiohttp.ClientSession", return_value=session):
            await fetch_jobs(_CONFIG)

        first_url = session.get.call_args_list[0][0][0]
        assert first_url.startswith("https://api.ashbyhq.com/posting-api/job-board/acme"), (
            f"Expected api.ashbyhq.com posting API, got: {first_url}"
        )
        assert "includeCompensation=true" in first_url, (
            "List request must ask for inline compensation (includeCompensation=true)"
        )

    @pytest.mark.asyncio
    async def test_returns_normalized_job_dicts(self):
        """Jobs are extracted from response['jobs'] and normalized correctly."""
        session = _make_session_mock(_LIST_RESPONSE)

        with patch("aiohttp.ClientSession", return_value=session):
            jobs = await fetch_jobs(_CONFIG)

        assert len(jobs) == 1
        job = jobs[0]
        assert job["source"] == "ashby"
        assert job["title"] == "Security Engineer"
        assert job["company"] == "Acme"
        assert job["external_id"] == "ashby-acme-00207abc-49b7-465c-a219-f7c1140f8047"
        assert job["url"] == "https://jobs.ashbyhq.com/acme/00207abc-49b7-465c-a219-f7c1140f8047"
        assert job["location"] == "San Francisco"
        assert job["is_remote"] is True
        assert job["description"] == "We need a security engineer to protect our systems."
        assert job["tags"] == ["Security", "Detection"]
        assert job["date_posted"] == "2026-06-15T01:25:39.198+00:00"
        assert job["apply_type"] == "external"
        assert job["is_easy_apply"] is False
        # Salary comes from the "Salary" compensation component, not equity
        assert job["salary_min"] == 185000
        assert job["salary_max"] == 325000
        assert job["salary_period"] == "annual"

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
        """When ashby_boards is empty (or missing), log INFO and return []."""
        config = {
            "search": {
                "ashby_boards": [],
                "keywords": ["security"],
                "exclude_keywords": [],
            }
        }

        with caplog.at_level(logging.INFO, logger="backend.job_sources.ashby"):
            jobs = await fetch_jobs(config)

        assert jobs == []
        assert any(
            "No Ashby board names configured" in r.message
            for r in caplog.records
        ), "Expected INFO about missing board names"

    @pytest.mark.asyncio
    async def test_non_200_response_logs_warning_and_skips_board(self, caplog):
        """A non-200 HTTP status logs a WARNING and returns [] for that board."""
        session = _make_session_mock({}, status=404)

        with patch("aiohttp.ClientSession", return_value=session), \
             caplog.at_level(logging.WARNING, logger="backend.job_sources.ashby"):
            jobs = await fetch_jobs(_CONFIG)

        assert jobs == []
        assert any(
            ("404" in r.message or "returned" in r.message) and r.levelno == logging.WARNING
            for r in caplog.records
        ), "Expected WARNING about non-200 response"

    @pytest.mark.asyncio
    async def test_unlisted_jobs_are_skipped(self):
        """Postings with isListed=False are internal-only and must be dropped."""
        payload = json.loads(json.dumps(_LIST_RESPONSE))
        payload["jobs"][0]["isListed"] = False
        session = _make_session_mock(payload)

        with patch("aiohttp.ClientSession", return_value=session):
            jobs = await fetch_jobs(_CONFIG)

        assert jobs == []

    @pytest.mark.asyncio
    async def test_keyword_filter_matches_title_only(self):
        """A title that doesn't match any keyword is dropped."""
        payload = json.loads(json.dumps(_LIST_RESPONSE))
        payload["jobs"][0]["title"] = "Office Manager"
        session = _make_session_mock(payload)

        with patch("aiohttp.ClientSession", return_value=session):
            jobs = await fetch_jobs(_CONFIG)

        assert jobs == []

    @pytest.mark.asyncio
    async def test_exclude_keywords_filter_applied(self):
        """Jobs whose titles contain an exclude keyword are dropped."""
        payload = json.loads(json.dumps(_LIST_RESPONSE))
        payload["jobs"][0]["title"] = "Senior Staff Security Engineer"
        config = {
            "search": {
                "ashby_boards": ["acme"],
                "keywords": ["security engineer"],
                "exclude_keywords": ["senior staff"],
            }
        }
        session = _make_session_mock(payload)

        with patch("aiohttp.ClientSession", return_value=session):
            jobs = await fetch_jobs(config)

        assert jobs == []

    @pytest.mark.asyncio
    async def test_missing_compensation_and_optional_keys(self):
        """Defensive parsing: minimal job dicts don't crash the fetcher."""
        payload = {
            "jobs": [
                {
                    "id": "abc-123",
                    "title": "Security Engineer",
                }
            ]
        }
        session = _make_session_mock(payload)

        with patch("aiohttp.ClientSession", return_value=session):
            jobs = await fetch_jobs(_CONFIG)

        assert len(jobs) == 1
        job = jobs[0]
        assert job["salary_min"] is None
        assert job["salary_max"] is None
        assert job["salary_period"] == "unknown"
        assert job["is_remote"] is False
        assert job["url"] == "https://jobs.ashbyhq.com/acme/abc-123"
        assert job["description"] == ""

    @pytest.mark.asyncio
    async def test_hourly_compensation_interval(self):
        """A '1 HOUR' salary interval maps to salary_period='hourly'."""
        payload = json.loads(json.dumps(_LIST_RESPONSE))
        payload["jobs"][0]["compensation"] = {
            "summaryComponents": [
                {
                    "compensationType": "Salary",
                    "interval": "1 HOUR",
                    "currencyCode": "USD",
                    "minValue": 40,
                    "maxValue": 60,
                }
            ]
        }
        session = _make_session_mock(payload)

        with patch("aiohttp.ClientSession", return_value=session):
            jobs = await fetch_jobs(_CONFIG)

        assert jobs[0]["salary_min"] == 40
        assert jobs[0]["salary_max"] == 60
        assert jobs[0]["salary_period"] == "hourly"
