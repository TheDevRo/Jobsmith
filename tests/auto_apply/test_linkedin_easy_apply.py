"""
Tests for detect_linkedin_easy_apply() in backend/job_sources/linkedin.py.

The function must classify a stored job dict WITHOUT re-fetching any pages.
"""

import pytest
from backend.job_sources.linkedin import detect_linkedin_easy_apply


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job(**kwargs) -> dict:
    """Return a minimal LinkedIn job dict, overridable via kwargs."""
    base = {
        "source": "linkedin",
        "external_id": "li-123456",
        "title": "Software Engineer",
        "company": "Acme Corp",
        "url": "https://www.linkedin.com/jobs/view/software-engineer-123456/",
        "is_easy_apply": False,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Signal 1 — is_easy_apply metadata flag
# ---------------------------------------------------------------------------

class TestIsEasyApplyFlag:
    def test_true_flag_returns_easy_apply(self):
        job = _job(is_easy_apply=True)
        assert detect_linkedin_easy_apply(job) == "easy_apply"

    def test_true_flag_overrides_ambiguous_url(self):
        # Flag takes priority even when URL gives no other signal
        job = _job(
            is_easy_apply=True,
            url="https://www.linkedin.com/jobs/view/something-999/",
        )
        assert detect_linkedin_easy_apply(job) == "easy_apply"

    def test_false_flag_does_not_short_circuit(self):
        # False flag lets the URL checks run
        job = _job(is_easy_apply=False)
        result = detect_linkedin_easy_apply(job)
        assert result in ("easy_apply", "external", "unknown")


# ---------------------------------------------------------------------------
# Signal 2 — /apply path segment on linkedin.com
# ---------------------------------------------------------------------------

class TestApplyUrlPath:
    def test_apply_path_segment_returns_easy_apply(self):
        job = _job(
            is_easy_apply=False,
            url="https://www.linkedin.com/jobs/apply/123456/",
        )
        assert detect_linkedin_easy_apply(job) == "easy_apply"

    def test_apply_path_segment_no_trailing_slash(self):
        job = _job(
            is_easy_apply=False,
            url="https://www.linkedin.com/jobs/apply/123456",
        )
        assert detect_linkedin_easy_apply(job) == "easy_apply"

    def test_apply_path_segment_without_www(self):
        job = _job(
            is_easy_apply=False,
            url="https://linkedin.com/jobs/apply/987654/",
        )
        assert detect_linkedin_easy_apply(job) == "easy_apply"

    def test_view_path_segment_is_not_apply(self):
        # /view is not /apply — should fall through to unknown
        job = _job(
            is_easy_apply=False,
            url="https://www.linkedin.com/jobs/view/some-job-123456/",
        )
        assert detect_linkedin_easy_apply(job) == "unknown"

    def test_apply_as_substring_in_path_part_is_not_matched(self):
        # 'applying' or 'application' should NOT match the '/apply' segment
        job = _job(
            is_easy_apply=False,
            url="https://www.linkedin.com/jobs/application/start/123456/",
        )
        assert detect_linkedin_easy_apply(job) == "unknown"


# ---------------------------------------------------------------------------
# Signal 3 — external (non-LinkedIn) URL
# ---------------------------------------------------------------------------

class TestExternalUrl:
    def test_non_linkedin_hostname_returns_external(self):
        job = _job(
            is_easy_apply=False,
            url="https://careers.example.com/jobs/123",
        )
        assert detect_linkedin_easy_apply(job) == "external"

    def test_greenhouse_url_returns_external(self):
        job = _job(
            is_easy_apply=False,
            url="https://boards.greenhouse.io/acme/jobs/12345",
        )
        assert detect_linkedin_easy_apply(job) == "external"

    def test_workday_url_returns_external(self):
        job = _job(
            is_easy_apply=False,
            url="https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Title/123",
        )
        assert detect_linkedin_easy_apply(job) == "external"

    def test_linkedin_subdomain_is_not_external(self):
        # jobs.linkedin.com is a proper LinkedIn subdomain — NOT external
        job = _job(
            is_easy_apply=False,
            url="https://jobs.linkedin.com/view/123",
        )
        # Not external; no /apply path → unknown
        assert detect_linkedin_easy_apply(job) == "unknown"

    def test_fake_linkedin_domain_is_external(self):
        # A domain that merely contains "linkedin" but is not linkedin.com
        job = _job(
            is_easy_apply=False,
            url="https://notlinkedin.com/jobs/123",
        )
        assert detect_linkedin_easy_apply(job) == "external"


# ---------------------------------------------------------------------------
# Ambiguous / unknown cases
# ---------------------------------------------------------------------------

class TestUnknown:
    def test_empty_url_returns_unknown(self):
        job = _job(is_easy_apply=False, url="")
        assert detect_linkedin_easy_apply(job) == "unknown"

    def test_missing_url_key_returns_unknown(self):
        job = {"source": "linkedin", "is_easy_apply": False}
        assert detect_linkedin_easy_apply(job) == "unknown"

    def test_none_url_returns_unknown(self):
        job = _job(is_easy_apply=False, url=None)
        assert detect_linkedin_easy_apply(job) == "unknown"

    def test_linkedin_view_url_no_flag_returns_unknown(self):
        job = _job(
            is_easy_apply=False,
            url="https://www.linkedin.com/jobs/view/senior-engineer-987654321/",
        )
        assert detect_linkedin_easy_apply(job) == "unknown"

    def test_empty_dict_returns_unknown(self):
        assert detect_linkedin_easy_apply({}) == "unknown"


# ---------------------------------------------------------------------------
# Return type contract
# ---------------------------------------------------------------------------

class TestReturnType:
    @pytest.mark.parametrize("job,expected", [
        (_job(is_easy_apply=True), "easy_apply"),
        (_job(is_easy_apply=False, url="https://www.linkedin.com/jobs/apply/1/"), "easy_apply"),
        (_job(is_easy_apply=False, url="https://boards.greenhouse.io/co/jobs/1"), "external"),
        (_job(is_easy_apply=False, url="https://www.linkedin.com/jobs/view/1/"), "unknown"),
        (_job(is_easy_apply=False, url=""), "unknown"),
    ])
    def test_always_returns_valid_apply_type(self, job, expected):
        result = detect_linkedin_easy_apply(job)
        assert result in ("easy_apply", "quick_apply", "external", "unknown")
        assert result == expected
