"""
tests/job_sources/test_filters.py

Tests for the shared filtering/matching helpers in backend.job_sources —
the layer where jobs get silently dropped, so it's worth pinning down.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.job_sources import (
    compile_exclude_patterns,
    matches_exclude,
    matches_keywords,
    parse_posted_date,
    _identity_key,
    _passes_global_filters,
)
from backend.job_sources.linkedin import _parse_salary


# ---------------------------------------------------------------------------
# Exclude keywords — word-boundary semantics
# ---------------------------------------------------------------------------

class TestExcludeMatching:

    def test_short_acronyms_do_not_match_inside_words(self):
        """The original substring matcher dropped every job whose title or
        company merely contained the letters of a clearance acronym."""
        patterns = compile_exclude_patterns(["SC", "TS", "SCI"])
        assert not matches_exclude("Cisco", patterns)
        assert not matches_exclude("Security Consultants", patterns)
        assert not matches_exclude("Data Scientist", patterns)
        assert not matches_exclude("Products Manager", patterns)

    def test_acronyms_match_as_whole_words(self):
        patterns = compile_exclude_patterns(["TS", "SCI"])
        assert matches_exclude("Requires TS clearance", patterns)
        assert matches_exclude("TS/SCI eligible candidates", patterns)

    def test_phrases_match_case_insensitively(self):
        patterns = compile_exclude_patterns(["senior staff", "Security Clearance"])
        assert matches_exclude("Senior Staff Engineer", patterns)
        assert matches_exclude("Active security clearance required", patterns)
        assert not matches_exclude("Staff Engineer", patterns)

    def test_empty_and_blank_keywords_are_ignored(self):
        patterns = compile_exclude_patterns(["", "  ", "intern"])
        assert matches_exclude("Software Intern", patterns)
        assert not matches_exclude("International Sales", patterns)

    def test_no_keywords_never_matches(self):
        assert not matches_exclude("anything", compile_exclude_patterns([]))
        assert not matches_exclude("anything", compile_exclude_patterns(None))


# ---------------------------------------------------------------------------
# Search keywords — phrase + token fallback
# ---------------------------------------------------------------------------

class TestKeywordMatching:

    def test_phrase_substring_matches(self):
        assert matches_keywords("Senior Security Engineer", ["security engineer"])

    def test_token_fallback_matches_title_variants(self):
        # "cybersecurity analyst" used to miss "Cyber Security Analyst"
        assert matches_keywords("Cyber Security Analyst II", ["cybersecurity analyst"]) is False
        assert matches_keywords("Cybersecurity Operations Analyst", ["cybersecurity analyst"])
        assert matches_keywords("Security Engineer, Cloud", ["cloud security engineer"])

    def test_non_matching_text(self):
        assert not matches_keywords("Office Manager", ["security engineer"])

    def test_empty_inputs(self):
        assert not matches_keywords("", ["security"])
        assert not matches_keywords("Security Engineer", [])


# ---------------------------------------------------------------------------
# Posted-date parsing
# ---------------------------------------------------------------------------

class TestParsePostedDate:

    def test_iso_with_z(self):
        dt = parse_posted_date("2026-06-01T12:00:00Z")
        assert dt == datetime(2026, 6, 1, 12, tzinfo=timezone.utc)

    def test_iso_date_only(self):
        dt = parse_posted_date("2026-06-01")
        assert dt is not None and dt.year == 2026 and dt.tzinfo is not None

    def test_unix_epoch_int_and_string(self):
        epoch = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp())
        assert parse_posted_date(epoch).year == 2026
        assert parse_posted_date(str(epoch)).year == 2026

    def test_rfc822(self):
        dt = parse_posted_date("Mon, 01 Jun 2026 09:30:00 +0000")
        assert dt is not None and dt.month == 6

    def test_unparseable_returns_none(self):
        assert parse_posted_date("Posted 3 days ago") is None
        assert parse_posted_date("") is None
        assert parse_posted_date(None) is None


# ---------------------------------------------------------------------------
# Global filters — max age, min salary, excludes
# ---------------------------------------------------------------------------

def _job(**overrides) -> dict:
    base = {
        "source": "remoteok",
        "title": "Security Engineer",
        "company": "Acme",
        "location": "Remote",
        "is_remote": True,
        "date_posted": "",
        "salary_min": None,
        "salary_max": None,
        "salary_period": "unknown",
        "url": "https://example.com/job/1",
    }
    base.update(overrides)
    return base


_CONFIG = {
    "search": {
        "locations": ["Remote", "Denver, CO"],
        "exclude_keywords": ["SC", "TS", "principal"],
        "max_age_days": 7,
        "min_salary": 85000,
    }
}


class TestGlobalFilters:

    def test_normal_job_passes(self):
        assert _passes_global_filters(_job(), _CONFIG)

    def test_acronym_exclude_does_not_drop_cisco(self):
        assert _passes_global_filters(_job(company="Cisco"), _CONFIG)

    def test_whole_word_exclude_drops(self):
        assert not _passes_global_filters(_job(title="Principal Security Engineer"), _CONFIG)
        assert not _passes_global_filters(_job(title="Engineer (TS clearance)"), _CONFIG)

    def test_old_job_dropped_when_date_parseable(self):
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        assert not _passes_global_filters(_job(date_posted=old), _CONFIG)

    def test_fresh_job_passes_age_filter(self):
        fresh = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        assert _passes_global_filters(_job(date_posted=fresh), _CONFIG)

    def test_unparseable_date_passes_age_filter(self):
        assert _passes_global_filters(_job(date_posted="Posted 30 days ago"), _CONFIG)

    def test_low_stated_salary_dropped(self):
        job = _job(salary_min=50000, salary_max=70000, salary_period="annual")
        assert not _passes_global_filters(job, _CONFIG)

    def test_salary_range_straddling_floor_passes(self):
        # Lenient: the upper bound clears the floor
        job = _job(salary_min=70000, salary_max=95000, salary_period="annual")
        assert _passes_global_filters(job, _CONFIG)

    def test_hourly_salary_normalized_before_comparison(self):
        # $50/hr ≈ $104k/yr — must not be compared against the floor raw
        job = _job(salary_min=45, salary_max=50, salary_period="hourly")
        assert _passes_global_filters(job, _CONFIG)

    def test_no_salary_data_passes(self):
        assert _passes_global_filters(_job(salary_min=None, salary_max=None), _CONFIG)

    def test_location_mismatch_dropped(self):
        job = _job(location="Berlin, Germany", is_remote=False, source="arbeitnow")
        assert not _passes_global_filters(job, _CONFIG)


# ---------------------------------------------------------------------------
# Cross-source identity key
# ---------------------------------------------------------------------------

class TestIdentityKey:

    def test_same_posting_different_sources_collide(self):
        a = {"title": "Security Engineer", "company": "Acme Corp.", "location": "Denver, CO"}
        b = {"title": "Security  Engineer", "company": "ACME Corp", "location": "denver co"}
        assert _identity_key(a) == _identity_key(b)

    def test_different_locations_do_not_collide(self):
        a = {"title": "Security Engineer", "company": "Acme", "location": "Denver, CO"}
        b = {"title": "Security Engineer", "company": "Acme", "location": "New York, NY"}
        assert _identity_key(a) != _identity_key(b)

    def test_missing_company_returns_none(self):
        assert _identity_key({"title": "Security Engineer", "company": ""}) is None


# ---------------------------------------------------------------------------
# LinkedIn salary parsing
# ---------------------------------------------------------------------------

class TestLinkedInParseSalary:

    def test_annual_range(self):
        assert _parse_salary("$80,000/yr - $120,000/yr") == (80000, 120000, "annual")

    def test_hourly_range_preserved_raw(self):
        s_min, s_max, period = _parse_salary("$25.00/hr - $30.00/hr")
        assert (s_min, s_max, period) == (25, 30, "hourly")

    def test_k_suffix_applies_per_amount(self):
        assert _parse_salary("$80k - $100k per year") == (80000, 100000, "annual")

    def test_401k_mention_does_not_inflate_hourly_rate(self):
        # The old parser multiplied any sub-1000 amount by 1000 if the letter
        # "k" appeared anywhere in the string — including in "401k".
        s_min, s_max, period = _parse_salary("$25/hr - $30/hr plus 401k match")
        assert (s_min, s_max, period) == (25, 30, "hourly")

    def test_no_amounts(self):
        assert _parse_salary("Competitive compensation") == (None, None, "unknown")
