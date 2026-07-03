"""
Tests for detect_greenhouse_apply_type() and detect_lever_apply_type()
in backend/job_sources/greenhouse.py.

Both functions must classify a stored job dict WITHOUT re-fetching any pages.
"""

import pytest
from backend.job_sources.greenhouse import (
    detect_greenhouse_apply_type,
    detect_lever_apply_type,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gh_job(**kwargs) -> dict:
    """Return a minimal Greenhouse job dict, overridable via kwargs."""
    base = {
        "source": "greenhouse",
        "external_id": "gh-acmecorp-123456",
        "title": "Software Engineer",
        "company": "Acme Corp",
        "url": "https://boards.greenhouse.io/acmecorp/jobs/123456",
    }
    base.update(kwargs)
    return base


def _lv_job(**kwargs) -> dict:
    """Return a minimal Lever job dict, overridable via kwargs."""
    base = {
        "source": "lever",
        "external_id": "lv-acmecorp-abc123",
        "title": "Software Engineer",
        "company": "Acme Corp",
        "url": "https://jobs.lever.co/acmecorp/abc123-def456",
    }
    base.update(kwargs)
    return base


# ===========================================================================
# detect_greenhouse_apply_type
# ===========================================================================

class TestGreenhouseEasyApply:
    def test_boards_greenhouse_url_returns_easy_apply(self):
        job = _gh_job(url="https://boards.greenhouse.io/acmecorp/jobs/123456")
        assert detect_greenhouse_apply_type(job) == "easy_apply"

    def test_greenhouse_without_boards_subdomain_returns_easy_apply(self):
        # Other greenhouse.io subdomains are still in-domain
        job = _gh_job(url="https://app.greenhouse.io/embed/job_app?token=abc")
        assert detect_greenhouse_apply_type(job) == "easy_apply"

    def test_greenhouse_io_root_returns_easy_apply(self):
        job = _gh_job(url="https://greenhouse.io/jobs/123")
        assert detect_greenhouse_apply_type(job) == "easy_apply"

    def test_boards_api_hostname_returns_easy_apply(self):
        # boards-api.greenhouse.io is the API hostname used internally
        job = _gh_job(url="https://boards-api.greenhouse.io/v1/boards/acmecorp/jobs/123")
        assert detect_greenhouse_apply_type(job) == "easy_apply"


class TestGreenhouseExternal:
    def test_custom_domain_returns_external(self):
        # Some companies configure absolute_url with their own careers page
        job = _gh_job(url="https://careers.acmecorp.com/jobs/123456")
        assert detect_greenhouse_apply_type(job) == "external"

    def test_lever_url_returns_external(self):
        job = _gh_job(url="https://jobs.lever.co/acmecorp/abc123")
        assert detect_greenhouse_apply_type(job) == "external"

    def test_workday_url_returns_external(self):
        job = _gh_job(url="https://acmecorp.wd5.myworkdayjobs.com/en-US/Careers/job/1")
        assert detect_greenhouse_apply_type(job) == "external"

    def test_fake_greenhouse_domain_returns_external(self):
        job = _gh_job(url="https://notgreenhouse.io/jobs/123")
        assert detect_greenhouse_apply_type(job) == "external"


class TestGreenhouseUnknown:
    def test_empty_url_returns_unknown(self):
        job = _gh_job(url="")
        assert detect_greenhouse_apply_type(job) == "unknown"

    def test_missing_url_key_returns_unknown(self):
        job = {"source": "greenhouse", "external_id": "gh-co-1"}
        assert detect_greenhouse_apply_type(job) == "unknown"

    def test_none_url_returns_unknown(self):
        job = _gh_job(url=None)
        assert detect_greenhouse_apply_type(job) == "unknown"

    def test_empty_dict_returns_unknown(self):
        assert detect_greenhouse_apply_type({}) == "unknown"


class TestGreenhouseReturnType:
    @pytest.mark.parametrize("job,expected", [
        (_gh_job(url="https://boards.greenhouse.io/acmecorp/jobs/1"), "easy_apply"),
        (_gh_job(url="https://app.greenhouse.io/embed/job_app?token=x"), "easy_apply"),
        (_gh_job(url="https://careers.acmecorp.com/jobs/1"), "external"),
        (_gh_job(url=""), "unknown"),
        (_gh_job(url=None), "unknown"),
    ])
    def test_always_returns_valid_apply_type(self, job, expected):
        result = detect_greenhouse_apply_type(job)
        assert result in ("easy_apply", "quick_apply", "external", "unknown")
        assert result == expected


# ===========================================================================
# detect_lever_apply_type
# ===========================================================================

class TestLeverEasyApply:
    def test_jobs_lever_co_returns_easy_apply(self):
        job = _lv_job(url="https://jobs.lever.co/acmecorp/abc123-def456")
        assert detect_lever_apply_type(job) == "easy_apply"

    def test_lever_co_without_subdomain_returns_easy_apply(self):
        job = _lv_job(url="https://lever.co/acmecorp/apply/abc123")
        assert detect_lever_apply_type(job) == "easy_apply"

    def test_other_lever_subdomain_returns_easy_apply(self):
        # Any *.lever.co host is still in-domain
        job = _lv_job(url="https://hire.lever.co/acmecorp/apply/abc123")
        assert detect_lever_apply_type(job) == "easy_apply"

    def test_lever_url_with_query_params_returns_easy_apply(self):
        job = _lv_job(url="https://jobs.lever.co/acmecorp/abc123?lever-source=linkedin")
        assert detect_lever_apply_type(job) == "easy_apply"


class TestLeverExternal:
    def test_custom_domain_returns_external(self):
        # hostedUrl pointed to a custom careers page backed by Lever —
        # cannot be identified as Lever from the domain alone
        job = _lv_job(url="https://careers.acmecorp.com/jobs/abc123")
        assert detect_lever_apply_type(job) == "external"

    def test_greenhouse_url_returns_external(self):
        job = _lv_job(url="https://boards.greenhouse.io/acmecorp/jobs/123")
        assert detect_lever_apply_type(job) == "external"

    def test_workday_url_returns_external(self):
        job = _lv_job(url="https://acmecorp.wd5.myworkdayjobs.com/en-US/Careers/job/1")
        assert detect_lever_apply_type(job) == "external"

    def test_fake_lever_domain_returns_external(self):
        job = _lv_job(url="https://notlever.co/jobs/abc123")
        assert detect_lever_apply_type(job) == "external"

    def test_linkedin_url_returns_external(self):
        job = _lv_job(url="https://www.linkedin.com/jobs/view/123456")
        assert detect_lever_apply_type(job) == "external"


class TestLeverUnknown:
    def test_empty_url_returns_unknown(self):
        job = _lv_job(url="")
        assert detect_lever_apply_type(job) == "unknown"

    def test_missing_url_key_returns_unknown(self):
        job = {"source": "lever", "external_id": "lv-co-1"}
        assert detect_lever_apply_type(job) == "unknown"

    def test_none_url_returns_unknown(self):
        job = _lv_job(url=None)
        assert detect_lever_apply_type(job) == "unknown"

    def test_empty_dict_returns_unknown(self):
        assert detect_lever_apply_type({}) == "unknown"


class TestLeverReturnType:
    @pytest.mark.parametrize("job,expected", [
        (_lv_job(url="https://jobs.lever.co/acmecorp/abc123"), "easy_apply"),
        (_lv_job(url="https://lever.co/acmecorp/apply/abc123"), "easy_apply"),
        (_lv_job(url="https://careers.acmecorp.com/jobs/1"), "external"),
        (_lv_job(url="https://boards.greenhouse.io/co/jobs/1"), "external"),
        (_lv_job(url=""), "unknown"),
        (_lv_job(url=None), "unknown"),
    ])
    def test_always_returns_valid_apply_type(self, job, expected):
        result = detect_lever_apply_type(job)
        assert result in ("easy_apply", "quick_apply", "external", "unknown")
        assert result == expected
