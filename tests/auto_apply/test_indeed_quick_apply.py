"""
Tests for detect_indeed_quick_apply() in backend/job_sources/indeed.py.

The function must classify a stored job dict WITHOUT re-fetching any pages.
"""

import pytest
from backend.job_sources.indeed import detect_indeed_quick_apply


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job(**kwargs) -> dict:
    """Return a minimal Indeed job dict, overridable via kwargs."""
    base = {
        "source": "indeed",
        "external_id": "abc123def456",
        "title": "Software Engineer",
        "company": "Acme Corp",
        "url": "https://www.indeed.com/viewjob?jk=abc123def456",
        "is_quick_apply": False,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Signal 1 — metadata flags
# ---------------------------------------------------------------------------

class TestMetadataFlags:
    def test_is_quick_apply_flag_returns_quick_apply(self):
        job = _job(is_quick_apply=True)
        assert detect_indeed_quick_apply(job) == "quick_apply"

    def test_indeed_apply_enabled_flag_returns_quick_apply(self):
        job = _job(is_quick_apply=False, indeedApplyEnabled=True)
        assert detect_indeed_quick_apply(job) == "quick_apply"

    def test_is_indeed_apply_flag_returns_quick_apply(self):
        job = _job(is_quick_apply=False, isIndeedApply=True)
        assert detect_indeed_quick_apply(job) == "quick_apply"

    def test_flag_takes_priority_over_ambiguous_url(self):
        # Flag short-circuits before URL checks run
        job = _job(
            is_quick_apply=True,
            url="https://www.indeed.com/viewjob?jk=xyz",
        )
        assert detect_indeed_quick_apply(job) == "quick_apply"

    def test_all_flags_false_does_not_short_circuit(self):
        job = _job(is_quick_apply=False, indeedApplyEnabled=False, isIndeedApply=False)
        result = detect_indeed_quick_apply(job)
        assert result in ("quick_apply", "external", "unknown")

    def test_missing_flags_does_not_raise(self):
        job = {"source": "indeed", "url": "https://www.indeed.com/viewjob?jk=x"}
        result = detect_indeed_quick_apply(job)
        assert result in ("quick_apply", "external", "unknown")


# ---------------------------------------------------------------------------
# Signal 2 — smartapply.indeed.com hostname
# ---------------------------------------------------------------------------

class TestSmartApplyHostname:
    def test_smartapply_subdomain_returns_quick_apply(self):
        job = _job(
            is_quick_apply=False,
            url="https://smartapply.indeed.com/beta/indeedapply/form?jobId=xyz",
        )
        assert detect_indeed_quick_apply(job) == "quick_apply"

    def test_smartapply_path_any_returns_quick_apply(self):
        job = _job(
            is_quick_apply=False,
            url="https://smartapply.indeed.com/indeedapply/start?jk=abc",
        )
        assert detect_indeed_quick_apply(job) == "quick_apply"


# ---------------------------------------------------------------------------
# Signal 3 — /apply or /applystart path segment on indeed.com
# ---------------------------------------------------------------------------

class TestApplyPathSegment:
    def test_apply_path_segment_returns_quick_apply(self):
        job = _job(
            is_quick_apply=False,
            url="https://www.indeed.com/apply/abc123",
        )
        assert detect_indeed_quick_apply(job) == "quick_apply"

    def test_applystart_path_segment_returns_quick_apply(self):
        job = _job(
            is_quick_apply=False,
            url="https://www.indeed.com/applystart/jk/abc123/desktop/xyz",
        )
        assert detect_indeed_quick_apply(job) == "quick_apply"

    def test_apply_segment_no_trailing_slash(self):
        job = _job(
            is_quick_apply=False,
            url="https://www.indeed.com/apply/abc123",
        )
        assert detect_indeed_quick_apply(job) == "quick_apply"

    def test_apply_segment_without_www(self):
        job = _job(
            is_quick_apply=False,
            url="https://indeed.com/apply/abc123",
        )
        assert detect_indeed_quick_apply(job) == "quick_apply"

    def test_viewjob_path_is_not_apply(self):
        # The standard scraper URL — no Quick Apply signal
        job = _job(
            is_quick_apply=False,
            url="https://www.indeed.com/viewjob?jk=abc123",
        )
        assert detect_indeed_quick_apply(job) == "unknown"

    def test_application_substring_not_matched(self):
        # "application" contains "apply" but is not the exact segment "apply"
        job = _job(
            is_quick_apply=False,
            url="https://www.indeed.com/application/status/abc",
        )
        assert detect_indeed_quick_apply(job) == "unknown"

    def test_applying_substring_not_matched(self):
        # "applying" is not the exact segment "apply" or "applystart"
        job = _job(
            is_quick_apply=False,
            url="https://www.indeed.com/jobs/applying/abc",
        )
        assert detect_indeed_quick_apply(job) == "unknown"


# ---------------------------------------------------------------------------
# Signal 4 — external (non-Indeed) URL
# ---------------------------------------------------------------------------

class TestExternalUrl:
    def test_non_indeed_hostname_returns_external(self):
        job = _job(
            is_quick_apply=False,
            url="https://careers.example.com/jobs/123",
        )
        assert detect_indeed_quick_apply(job) == "external"

    def test_greenhouse_url_returns_external(self):
        job = _job(
            is_quick_apply=False,
            url="https://boards.greenhouse.io/acme/jobs/12345",
        )
        assert detect_indeed_quick_apply(job) == "external"

    def test_workday_url_returns_external(self):
        job = _job(
            is_quick_apply=False,
            url="https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Title/123",
        )
        assert detect_indeed_quick_apply(job) == "external"

    def test_linkedin_url_returns_external(self):
        job = _job(
            is_quick_apply=False,
            url="https://www.linkedin.com/jobs/view/123456",
        )
        assert detect_indeed_quick_apply(job) == "external"

    def test_indeed_subdomain_is_not_external(self):
        # Any *.indeed.com subdomain stays in-domain
        job = _job(
            is_quick_apply=False,
            url="https://resumes.indeed.com/jobs/view/123",
        )
        # Not external; no apply path → unknown
        assert detect_indeed_quick_apply(job) != "external"

    def test_fake_indeed_domain_is_external(self):
        # A domain that merely contains "indeed" but is not indeed.com
        job = _job(
            is_quick_apply=False,
            url="https://notindeed.com/jobs/123",
        )
        assert detect_indeed_quick_apply(job) == "external"


# ---------------------------------------------------------------------------
# Ambiguous / unknown cases
# ---------------------------------------------------------------------------

class TestUnknown:
    def test_empty_url_returns_unknown(self):
        job = _job(is_quick_apply=False, url="")
        assert detect_indeed_quick_apply(job) == "unknown"

    def test_missing_url_key_returns_unknown(self):
        job = {"source": "indeed", "is_quick_apply": False}
        assert detect_indeed_quick_apply(job) == "unknown"

    def test_none_url_returns_unknown(self):
        job = _job(is_quick_apply=False, url=None)
        assert detect_indeed_quick_apply(job) == "unknown"

    def test_standard_viewjob_url_returns_unknown(self):
        # The URL every card gets from the scraper — no Quick Apply evidence yet
        job = _job(
            is_quick_apply=False,
            url="https://www.indeed.com/viewjob?jk=abc123def456",
        )
        assert detect_indeed_quick_apply(job) == "unknown"

    def test_empty_dict_returns_unknown(self):
        assert detect_indeed_quick_apply({}) == "unknown"


# ---------------------------------------------------------------------------
# Return type contract
# ---------------------------------------------------------------------------

class TestReturnType:
    @pytest.mark.parametrize("job,expected", [
        (_job(is_quick_apply=True), "quick_apply"),
        (_job(indeedApplyEnabled=True), "quick_apply"),
        (_job(url="https://smartapply.indeed.com/beta/form?jobId=x"), "quick_apply"),
        (_job(url="https://www.indeed.com/apply/abc123"), "quick_apply"),
        (_job(url="https://www.indeed.com/applystart/jk/abc"), "quick_apply"),
        (_job(url="https://boards.greenhouse.io/co/jobs/1"), "external"),
        (_job(url="https://www.indeed.com/viewjob?jk=abc123"), "unknown"),
        (_job(url=""), "unknown"),
    ])
    def test_always_returns_valid_apply_type(self, job, expected):
        result = detect_indeed_quick_apply(job)
        assert result in ("easy_apply", "quick_apply", "external", "unknown")
        assert result == expected
