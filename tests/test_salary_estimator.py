"""
tests/test_salary_estimator.py

Unit tests for the salary estimator. Both LLM and HTTP calls are mocked —
no live network or LM Studio required. Verifies that:
  - LLM is only ever asked to canonicalize the role, not to invent salary numbers.
  - The histogram-percentile math produces sensible p25/p50/p75 values.
  - Adzuna and BLS payloads are normalized into the same shape.
  - location_to_msa maps Denver to its MSA code; unknown locations return None.
  - The seniority multiplier shifts ranges in the expected direction.
  - The DB include_estimated filter matches estimated salaries when toggled.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import salary_estimator


@pytest.fixture(autouse=True)
def _reset_cached_clients():
    """Module-level cached clients persist across tests; reset between runs."""
    salary_estimator._adzuna_http_client = None
    salary_estimator._openai_client = None
    salary_estimator._openai_client_key = None
    yield
    salary_estimator._adzuna_http_client = None
    salary_estimator._openai_client = None
    salary_estimator._openai_client_key = None


# ---------------------------------------------------------------------------
# Histogram math
# ---------------------------------------------------------------------------

def test_percentiles_basic_distribution():
    histogram = {
        "40000": 10,
        "60000": 30,
        "80000": 40,
        "100000": 30,
        "120000": 10,
    }
    p25, p50, p75 = salary_estimator._percentiles_from_histogram(histogram)
    assert 50000 < p25 < 80000
    assert 70000 < p50 < 100000
    assert 80000 < p75 < 110000
    assert p25 < p50 < p75


def test_percentiles_empty_histogram():
    assert salary_estimator._percentiles_from_histogram({}) == (None, None, None)


def test_percentiles_invalid_values_dropped():
    # Buckets with zero counts must be ignored, not error
    histogram = {"50000": 0, "60000": 5}
    p25, p50, p75 = salary_estimator._percentiles_from_histogram(histogram)
    assert p25 is not None and p75 is not None


# ---------------------------------------------------------------------------
# Seniority multiplier
# ---------------------------------------------------------------------------

def test_seniority_multiplier_directions():
    assert salary_estimator._seniority_multiplier("intern") < 1.0
    assert salary_estimator._seniority_multiplier("entry") < 1.0
    assert salary_estimator._seniority_multiplier("mid") == 1.0
    assert salary_estimator._seniority_multiplier("senior") > 1.0
    assert salary_estimator._seniority_multiplier("staff") > salary_estimator._seniority_multiplier("senior")
    assert salary_estimator._seniority_multiplier(None) == 1.0
    assert salary_estimator._seniority_multiplier("totally-made-up") == 1.0


# ---------------------------------------------------------------------------
# Fallback canonicalization (no LLM)
# ---------------------------------------------------------------------------

def test_fallback_canonical_strips_seniority():
    assert "senior" not in salary_estimator._fallback_canonical_title("Senior Software Engineer").lower()
    assert "junior" not in salary_estimator._fallback_canonical_title("Junior Data Analyst").lower()


def test_fallback_seniority_detection():
    assert salary_estimator._fallback_seniority("Senior SOC Analyst") == "senior"
    assert salary_estimator._fallback_seniority("Staff Engineer") == "staff"
    assert salary_estimator._fallback_seniority("SOC Analyst") == "mid"


# ---------------------------------------------------------------------------
# MSA mapping
# ---------------------------------------------------------------------------

def test_location_to_msa_denver():
    assert salary_estimator.location_to_msa("Denver, CO") is not None


def test_location_to_msa_unknown():
    assert salary_estimator.location_to_msa("Atlantis, Underwater") is None


def test_location_to_msa_blank():
    assert salary_estimator.location_to_msa("") is None


# ---------------------------------------------------------------------------
# Adzuna histogram lookup (HTTP mocked)
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, *args, **kwargs):
        return self._response


@pytest.mark.asyncio
async def test_adzuna_histogram_normalizes_payload():
    fake_resp = _FakeResponse(200, {
        "histogram": {
            "60000": 20, "80000": 50, "100000": 30, "120000": 10,
        },
    })
    with patch("backend.salary_estimator.httpx.AsyncClient", lambda *a, **k: _FakeAsyncClient(fake_resp)):
        with patch("backend.salary_estimator.db.get_salary_cache", AsyncMock(return_value=None)):
            with patch("backend.salary_estimator.db.set_salary_cache", AsyncMock()):
                result = await salary_estimator.lookup_adzuna_histogram(
                    what="software engineer",
                    where="Denver",
                    country="us",
                    app_id="x",
                    app_key="y",
                )
    assert result["source"] == "adzuna"
    assert result["period"] == "annual"
    assert result["min"] < result["max"]
    assert result["metadata"]["sample_size"] == 110
    assert result["confidence"] == "medium"  # 110 samples -> medium


@pytest.mark.asyncio
async def test_adzuna_histogram_returns_none_on_empty():
    fake_resp = _FakeResponse(200, {"histogram": {}})
    with patch("backend.salary_estimator.httpx.AsyncClient", lambda *a, **k: _FakeAsyncClient(fake_resp)):
        with patch("backend.salary_estimator.db.get_salary_cache", AsyncMock(return_value=None)):
            result = await salary_estimator.lookup_adzuna_histogram(
                what="software engineer",
                where="Denver",
                country="us",
                app_id="x",
                app_key="y",
            )
    assert result is None


@pytest.mark.asyncio
async def test_adzuna_histogram_uses_cache():
    cached_payload = {"min": 1, "max": 2, "source": "adzuna"}
    with patch("backend.salary_estimator.db.get_salary_cache", AsyncMock(return_value=cached_payload)) as g, \
         patch("backend.salary_estimator.httpx.AsyncClient") as http:
        result = await salary_estimator.lookup_adzuna_histogram(
            what="x", where="y", country="us", app_id="a", app_key="b",
        )
    assert result == cached_payload
    g.assert_called_once()
    http.assert_not_called()


# ---------------------------------------------------------------------------
# Top-level estimate_salary — never returns LLM-generated numbers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_estimate_salary_uses_external_data_only():
    """LLM canonicalization runs but salary numbers must come from Adzuna only."""
    job = {
        "id": "j1",
        "title": "Senior Cybersecurity Analyst",
        "description": "Wazuh, SIEM, incident response.",
        "location": "Denver, CO",
    }
    config = {
        "salary_estimator": {"enabled": True},
        "api_keys": {"adzuna_app_id": "x", "adzuna_app_key": "y"},
    }
    classification = {
        "canonical_title": "cybersecurity analyst",
        "seniority": "senior",
        "soc_code": "15-1212",
        "soc_title": "Information Security Analysts",
    }
    adzuna_payload = {
        "min": 80000, "max": 120000, "period": "annual", "source": "adzuna",
        "confidence": "high",
        "metadata": {"p25": 80000, "p50": 100000, "p75": 120000, "sample_size": 250},
    }

    with patch("backend.salary_estimator.classify_job_role", AsyncMock(return_value=classification)), \
         patch("backend.salary_estimator.lookup_adzuna_histogram", AsyncMock(return_value=adzuna_payload)):
        result = await salary_estimator.estimate_salary(job, config)

    # Senior multiplier should boost the range above the raw histogram values.
    assert result["min"] > 80000
    assert result["max"] > 120000
    assert result["source"] == "adzuna"
    assert result["metadata"]["seniority"] == "senior"
    assert result["metadata"]["canonical_title"] == "cybersecurity analyst"


@pytest.mark.asyncio
async def test_estimate_salary_disabled_returns_none():
    job = {"id": "j1", "title": "X", "location": "Denver"}
    config = {"salary_estimator": {"enabled": False}}
    result = await salary_estimator.estimate_salary(job, config)
    assert result is None


@pytest.mark.asyncio
async def test_estimate_salary_no_title_returns_none():
    job = {"id": "j1", "title": "", "location": "Denver"}
    config = {"salary_estimator": {"enabled": True}}
    result = await salary_estimator.estimate_salary(job, config)
    assert result is None


@pytest.mark.asyncio
async def test_estimate_salary_falls_back_to_bls_when_adzuna_unavailable():
    job = {"id": "j1", "title": "Information Security Analyst", "location": "Denver, CO"}
    # No Adzuna keys -> Adzuna path skipped entirely
    config = {
        "salary_estimator": {"enabled": True, "bls": {"api_key": "test"}},
        "api_keys": {},
    }
    classification = {
        "canonical_title": "information security analyst",
        "seniority": "mid",
        "soc_code": "15-1212",
        "soc_title": "Information Security Analysts",
    }
    bls_payload = {
        "min": 90000, "max": 130000, "period": "annual",
        "source": "bls_oews", "confidence": "high",
        "metadata": {"soc_code": "15-1212", "msa_code": "1974000",
                     "scope": "msa:1974000", "p25": 90000, "p50": 110000, "p75": 130000},
    }
    with patch("backend.salary_estimator.classify_job_role", AsyncMock(return_value=classification)), \
         patch("backend.salary_estimator.lookup_bls_oews", return_value=bls_payload):
        result = await salary_estimator.estimate_salary(job, config)
    assert result["source"] == "bls_oews"
    assert result["min"] == 90000


# ---------------------------------------------------------------------------
# DB-level include_estimated filter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_include_estimated_filter_matches_estimates(tmp_path, monkeypatch):
    """Verify list_jobs(min_salary=X, include_estimated=True) returns jobs
    whose estimated_salary fields clear the threshold even when their real
    salary is null."""
    from backend import database as dbmod

    # Point the DB at a temp file
    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "test.db")
    await dbmod.init_db()

    await dbmod.upsert_job({
        "source": "test", "external_id": "1", "title": "Engineer A",
        "company": "Acme", "location": "Denver", "url": "u1",
        "salary_min": None, "salary_max": None, "salary_period": "unknown",
    })
    await dbmod.upsert_job({
        "source": "test", "external_id": "2", "title": "Engineer B",
        "company": "Beta", "location": "Denver", "url": "u2",
        "salary_min": None, "salary_max": None, "salary_period": "unknown",
    })

    # Job A gets a strong estimate; B gets a weak one.
    jobs = (await dbmod.get_jobs(limit=10))["jobs"]
    by_title = {j["title"]: j for j in jobs}
    await dbmod.update_job_estimated_salary(by_title["Engineer A"]["id"], {
        "min": 110000, "max": 140000, "period": "annual",
        "source": "adzuna", "confidence": "high", "metadata": {"p50": 125000},
    })
    await dbmod.update_job_estimated_salary(by_title["Engineer B"]["id"], {
        "min": 50000, "max": 70000, "period": "annual",
        "source": "adzuna", "confidence": "low", "metadata": {"p50": 60000},
    })

    # Without include_estimated, neither matches (both have NULL real salary).
    res = await dbmod.get_jobs(min_salary=100000, include_estimated=False, limit=10)
    assert res["total"] == 0

    # With include_estimated, only Engineer A clears the bar.
    res = await dbmod.get_jobs(min_salary=100000, include_estimated=True, limit=10)
    assert res["total"] == 1
    assert res["jobs"][0]["title"] == "Engineer A"
