"""
tests/job_sources/test_parsers.py

Fixture-based tests for the HTML parsers — the highest-churn surface of the
scraper sources. When LinkedIn or Indeed restructure their markup, these
tests go red instead of the source silently returning 0 jobs in production.
Also pins the boolean-OR keyword batching used to cut search request counts.
"""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from backend.job_sources.indeed import _batch_keywords as indeed_batch
from backend.job_sources.indeed import _parse_viewjob_html
from backend.job_sources.linkedin import _batch_keywords as linkedin_batch
from backend.job_sources.linkedin import _parse_search_card

FIXTURES = Path(__file__).parent / "fixtures"


def _linkedin_cards() -> list:
    html = (FIXTURES / "linkedin_search.html").read_text()
    return BeautifulSoup(html, "lxml").find_all("li")


# ---------------------------------------------------------------------------
# LinkedIn guest-search card parsing
# ---------------------------------------------------------------------------

class TestLinkedInSearchCardParsing:

    def test_full_card_extracts_all_fields(self):
        parsed = _parse_search_card(_linkedin_cards()[0])
        assert parsed == {
            "title": "Security Engineer",
            "company": "Acme Corp",
            "location": "Denver, CO",
            "url": "https://www.linkedin.com/jobs/view/security-engineer-at-acme-corp-4012345678",
            "external_id": "4012345678",
            "date_posted": "2026-06-28",
        }

    def test_query_string_stripped_from_url(self):
        parsed = _parse_search_card(_linkedin_cards()[1])
        assert "?" not in parsed["url"]
        assert parsed["external_id"] == "3999888777"

    def test_card_without_link_returns_none(self):
        # Ad/spacer nodes have no title or full-link anchor
        assert _parse_search_card(_linkedin_cards()[2]) is None

    def test_all_real_cards_parse(self):
        cards = _linkedin_cards()
        parsed = [_parse_search_card(c) for c in cards]
        assert sum(1 for p in parsed if p is not None) == 2


# ---------------------------------------------------------------------------
# Indeed /viewjob page parsing
# ---------------------------------------------------------------------------

class TestIndeedViewjobParsing:

    def test_description_extracted_and_cleaned(self):
        html = (FIXTURES / "indeed_viewjob.html").read_text()
        out = _parse_viewjob_html(html)
        assert "Monitor SIEM alerts and triage incidents" in out["description"]
        assert "Respond to security events" in out["description"]
        assert "<" not in out["description"]  # tags stripped

    def test_salary_and_period_from_json_ld(self):
        html = (FIXTURES / "indeed_viewjob.html").read_text()
        out = _parse_viewjob_html(html)
        assert out["salary_min"] == 32
        assert out["salary_max"] == 40
        assert out["salary_period"] == "hourly"
        assert out["job_type"] == "FULL_TIME"

    def test_empty_html_returns_defaults(self):
        out = _parse_viewjob_html("")
        assert out["description"] == ""
        assert out["salary_min"] is None
        assert out["salary_period"] == "unknown"


# ---------------------------------------------------------------------------
# Boolean-OR keyword batching
# ---------------------------------------------------------------------------

class TestKeywordBatching:

    def test_linkedin_phrases_quoted_and_joined_with_or(self):
        batches = linkedin_batch(
            ["cybersecurity analyst", "security engineer", "SOC analyst", "sysadmin"]
        )
        assert batches == [
            '"cybersecurity analyst" OR "security engineer" OR "SOC analyst" OR sysadmin'
        ]

    def test_linkedin_overflow_starts_new_batch(self):
        batches = linkedin_batch(["a b", "c d", "e f", "g h", "solo"])
        assert len(batches) == 2
        assert batches[1] == "solo"  # a batch of one stays bare

    def test_indeed_uses_lowercase_or(self):
        batches = indeed_batch(["cybersecurity analyst", "security engineer"])
        assert batches == ['"cybersecurity analyst" or "security engineer"']

    def test_single_keyword_stays_bare(self):
        # No quoting — preserves the engines' fuzzy matching for the
        # single-keyword config that existed before batching.
        assert linkedin_batch(["cybersecurity analyst"]) == ["cybersecurity analyst"]
        assert indeed_batch(["cybersecurity analyst"]) == ["cybersecurity analyst"]

    def test_blank_keywords_dropped(self):
        assert linkedin_batch(["", "  ", "security"]) == ["security"]
        assert indeed_batch([]) == []
