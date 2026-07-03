"""
Tests for detect_usajobs_apply_type() in backend/job_sources/usajobs.py.

The function must classify a stored job dict WITHOUT re-fetching any pages.
"""

import pytest
from backend.job_sources.usajobs import detect_usajobs_apply_type


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job(**kwargs) -> dict:
    """Return a minimal USAJobs job dict, overridable via kwargs."""
    base = {
        "source": "usajobs",
        "external_id": "ABCD-24-123456-DEF",
        "title": "Information Technology Specialist",
        "company": "Dept of Defense",
        "url": "https://www.usajobs.gov/job/123456789",
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Easy apply — usajobs.gov domain
# ---------------------------------------------------------------------------

class TestEasyApply:
    def test_standard_usajobs_url_returns_easy_apply(self):
        job = _job(url="https://www.usajobs.gov/job/123456789")
        assert detect_usajobs_apply_type(job) == "easy_apply"

    def test_usajobs_without_www_returns_easy_apply(self):
        job = _job(url="https://usajobs.gov/job/987654321")
        assert detect_usajobs_apply_type(job) == "easy_apply"

    def test_usajobs_subdomain_returns_easy_apply(self):
        # e.g. apply.usajobs.gov — still in-domain
        job = _job(url="https://apply.usajobs.gov/ViewQuestionnaire/123")
        assert detect_usajobs_apply_type(job) == "easy_apply"

    def test_usajobs_https_and_path_returns_easy_apply(self):
        job = _job(url="https://www.usajobs.gov/GetJob/ViewDetails/123456789")
        assert detect_usajobs_apply_type(job) == "easy_apply"


# ---------------------------------------------------------------------------
# External — non-usajobs.gov domain
# ---------------------------------------------------------------------------

class TestExternal:
    def test_agency_site_returns_external(self):
        # Some delegated vacancies link directly to an agency HR portal
        job = _job(url="https://careers.dod.mil/apply/12345")
        assert detect_usajobs_apply_type(job) == "external"

    def test_monster_url_returns_external(self):
        job = _job(url="https://www.monster.com/jobs/apply/123")
        assert detect_usajobs_apply_type(job) == "external"

    def test_greenhouse_url_returns_external(self):
        job = _job(url="https://boards.greenhouse.io/usdept/jobs/12345")
        assert detect_usajobs_apply_type(job) == "external"

    def test_fake_usajobs_domain_returns_external(self):
        # Domain that merely contains "usajobs" but is not usajobs.gov
        job = _job(url="https://fake-usajobs.com/job/123")
        assert detect_usajobs_apply_type(job) == "external"


# ---------------------------------------------------------------------------
# Unknown — no URL stored
# ---------------------------------------------------------------------------

class TestUnknown:
    def test_empty_url_returns_unknown(self):
        job = _job(url="")
        assert detect_usajobs_apply_type(job) == "unknown"

    def test_missing_url_key_returns_unknown(self):
        job = {"source": "usajobs", "external_id": "X-1"}
        assert detect_usajobs_apply_type(job) == "unknown"

    def test_none_url_returns_unknown(self):
        job = _job(url=None)
        assert detect_usajobs_apply_type(job) == "unknown"

    def test_empty_dict_returns_unknown(self):
        assert detect_usajobs_apply_type({}) == "unknown"


# ---------------------------------------------------------------------------
# Return type contract
# ---------------------------------------------------------------------------

class TestReturnType:
    @pytest.mark.parametrize("job,expected", [
        (_job(url="https://www.usajobs.gov/job/1"), "easy_apply"),
        (_job(url="https://apply.usajobs.gov/ViewQuestionnaire/1"), "easy_apply"),
        (_job(url="https://careers.agency.gov/jobs/1"), "external"),
        (_job(url=""), "unknown"),
        (_job(url=None), "unknown"),
    ])
    def test_always_returns_valid_apply_type(self, job, expected):
        result = detect_usajobs_apply_type(job)
        assert result in ("easy_apply", "quick_apply", "external", "unknown")
        assert result == expected
