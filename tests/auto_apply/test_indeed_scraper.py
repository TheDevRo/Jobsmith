"""
tests/auto_apply/test_indeed_scraper.py

Offline unit tests for backend/job_sources/indeed.py (Playwright scraper).
All Playwright calls are mocked — no browser, network, or running server needed.

Test cases
----------
TestExtractCardsHappyPath
    _extract_cards() with 3 mocked card elements → 3 correctly shaped dicts

TestFetchJobsBotBlock
    fetch_jobs() where page title is "Just a Moment" → returns [] + WARNING log

TestFetchJobsHappyPath
    fetch_jobs() with _extract_cards patched → 3 dicts propagate correctly,
    deduplication by external_id works, exclude_keywords filter applies

TestFetchJobsNoNextPage
    fetch_jobs() stops paginating when no job cards render on the next page

TestFetchJobsMaxPages
    fetch_jobs() respects max_pages=1 and does not attempt a second page
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def _patch_env():
    """Make fetch_jobs() hermetic regardless of local machine state.

    The scraper prefers a persistent Chrome profile / saved session when one
    exists on disk and applies playwright-stealth when installed — all paths
    these mocks don't cover. Pin them off so the plain launch path is used.
    """
    return patch.multiple(
        "backend.job_sources.indeed",
        _stealth_async=None,
        _INDEED_PROFILE_DIR=Path("/nonexistent/indeed_chrome_profile"),
        _INDEED_SESSION_FILE=Path("/nonexistent/indeed_session/storage_state.json"),
    )


# ---------------------------------------------------------------------------
# Mock-building helpers
# ---------------------------------------------------------------------------

def _make_card_element(job_id: str, title: str, company: str, location: str) -> MagicMock:
    """
    Return a mock Playwright ElementHandle-like object that _extract_cards()
    can interact with to produce one job dict.

    Each attribute / text_content call is wired to return the supplied values
    via the first matching selector in _TITLE_SELECTORS / _COMPANY_SELECTORS /
    _LOCATION_SELECTORS.
    """
    card = MagicMock()

    # get_attribute("data-jk") → job_id
    async def _get_attr(attr: str) -> str | None:
        return job_id if attr == "data-jk" else None
    card.get_attribute = _get_attr

    # card.locator(sel).first.text_content(timeout=...) → value by selector family
    def _card_locator(sel: str) -> MagicMock:
        first = MagicMock()

        # Title selectors
        if any(k in sel for k in ("jobTitle", "title", "h2")):
            async def _tc(**kw): return title
        # Company selectors
        elif any(k in sel for k in ("company-name", "companyName")):
            async def _tc(**kw): return company
        # Location selectors
        elif any(k in sel for k in ("text-location", "companyLocation")):
            async def _tc(**kw): return location
        # Date and anything else
        else:
            async def _tc(**kw): return ""

        first.text_content = _tc
        loc = MagicMock()
        loc.first = first
        return loc

    card.locator = _card_locator
    return card


def _make_page_with_cards(cards: list[MagicMock], title: str = "Indeed Jobs") -> MagicMock:
    """
    Return a mock Playwright Page whose job-card locator yields *cards*.

    Suitable for passing directly to _extract_cards() or for wiring into a
    full Playwright stack mock for fetch_jobs() tests.
    """
    page = MagicMock()

    # page.title() → title string
    async def _title() -> str:
        return title
    page.title = _title

    # page.goto / wait helpers — no-ops
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.close = AsyncMock()

    # page.locator(sel) — drives both card-list and captcha / next-btn queries
    def _page_locator(sel: str) -> MagicMock:
        loc = MagicMock()

        if "data-jk" in sel:
            # This is the card-list selector
            async def _count() -> int:
                return len(cards)

            async def _all() -> list:
                return cards

            loc.count = _count
            loc.all = _all
            loc.first = MagicMock()
        elif any(k in sel for k in ("captcha", "pagination", "Next", "next")):
            async def _count() -> int:
                return 0

            first = MagicMock()

            async def _is_visible(timeout: int = 2000) -> bool:
                return False

            first.is_visible = _is_visible
            loc.count = _count
            loc.first = first
        else:
            async def _count() -> int:
                return 0

            loc.count = _count
            first = MagicMock()

            async def _is_visible(timeout: int = 2000) -> bool:
                return False

            first.is_visible = _is_visible
            loc.first = first

        return loc

    page.locator = _page_locator
    return page


def _make_playwright_stack(page: MagicMock) -> MagicMock:
    """
    Return a mock `async_playwright` callable that wires up a full stack
    pointing at *page*, matching the pattern used in test_indeed_session.py.
    """
    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    context.add_cookies = AsyncMock()
    context.close = AsyncMock()

    browser = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()

    pw_instance = MagicMock()
    pw_instance.chromium.launch = AsyncMock(return_value=browser)
    pw_instance.stop = AsyncMock()

    pw_cm = MagicMock()
    pw_cm.start = AsyncMock(return_value=pw_instance)

    mock_async_playwright = MagicMock(return_value=pw_cm)
    return mock_async_playwright


# ---------------------------------------------------------------------------
# TestExtractCardsHappyPath
# ---------------------------------------------------------------------------

class TestExtractCardsHappyPath:
    """_extract_cards() with 3 card elements returns 3 correctly shaped dicts."""

    @pytest.mark.asyncio
    async def test_three_cards_returned(self):
        from backend.job_sources.indeed import _extract_cards

        cards_fixture = [
            _make_card_element("abc001", "Security Engineer", "Acme Corp", "Remote"),
            _make_card_element("abc002", "Penetration Tester", "Beta LLC", "Austin, TX"),
            _make_card_element("abc003", "SOC Analyst", "Gamma Inc", "New York, NY"),
        ]
        page = _make_page_with_cards(cards_fixture)

        result = await _extract_cards(page)

        assert len(result) == 3, f"Expected 3 jobs, got {len(result)}: {result}"

    @pytest.mark.asyncio
    async def test_dict_schema_is_correct(self):
        """Every returned dict must contain all required keys with correct types."""
        from backend.job_sources.indeed import _extract_cards

        required_keys = {
            "source", "external_id", "title", "company", "location",
            "url", "description", "salary_min", "salary_max",
            "job_type", "tags", "date_posted", "is_remote",
        }
        cards_fixture = [
            _make_card_element("id001", "Cloud Engineer", "CloudCo", "Remote"),
        ]
        page = _make_page_with_cards(cards_fixture)

        result = await _extract_cards(page)
        assert result, "Expected at least one job"
        job = result[0]
        assert required_keys.issubset(job.keys()), (
            f"Missing keys: {required_keys - job.keys()}"
        )

    @pytest.mark.asyncio
    async def test_field_values_extracted_correctly(self):
        """title, company, location, external_id, url are populated from card mock."""
        from backend.job_sources.indeed import _extract_cards

        cards_fixture = [
            _make_card_element("xyz789", "Red Team Lead", "SecureCorp", "Washington, DC"),
        ]
        page = _make_page_with_cards(cards_fixture)

        result = await _extract_cards(page)
        assert len(result) == 1
        job = result[0]

        assert job["external_id"] == "xyz789"
        assert job["title"] == "Red Team Lead"
        assert job["company"] == "SecureCorp"
        assert job["location"] == "Washington, DC"
        assert job["url"] == "https://www.indeed.com/viewjob?jk=xyz789"
        assert job["source"] == "indeed"

    @pytest.mark.asyncio
    async def test_is_remote_flag_set_for_remote_jobs(self):
        """is_remote=True when 'remote' appears in title or location."""
        from backend.job_sources.indeed import _extract_cards

        cards_fixture = [
            _make_card_element("r001", "Remote Security Analyst", "Foo Inc", "Anywhere"),
            _make_card_element("r002", "On-Site Engineer", "Bar Co", "Remote"),
            _make_card_element("r003", "On-Site Engineer", "Baz Co", "Chicago, IL"),
        ]
        page = _make_page_with_cards(cards_fixture)
        result = await _extract_cards(page)
        assert len(result) == 3
        assert result[0]["is_remote"] is True   # "remote" in title
        assert result[1]["is_remote"] is True   # "remote" in location
        assert result[2]["is_remote"] is False  # neither


# ---------------------------------------------------------------------------
# TestFetchJobsBotBlock
# ---------------------------------------------------------------------------

class TestFetchJobsBotBlock:
    """fetch_jobs() detects bot-block and returns [] with a WARNING log."""

    @pytest.mark.asyncio
    async def test_bot_blocked_page_returns_empty(self, caplog):
        """A page titled 'Just a Moment' → empty list."""
        from backend.job_sources.indeed import fetch_jobs

        # Page with captcha-style title; no job cards
        bot_page = _make_page_with_cards([], title="Just a Moment")
        mock_pw = _make_playwright_stack(bot_page)

        config = {"search": {"keywords": ["security engineer"], "locations": ["Remote"]}}

        with patch("playwright.async_api.async_playwright", mock_pw), _patch_env():
            with caplog.at_level(logging.WARNING, logger="backend.job_sources.indeed"):
                result = await fetch_jobs(config)

        assert result == [], f"Expected [], got {result}"

    @pytest.mark.asyncio
    async def test_bot_blocked_emits_warning(self, caplog):
        """A bot-block page must emit at least one WARNING-level log entry."""
        from backend.job_sources.indeed import fetch_jobs

        bot_page = _make_page_with_cards([], title="Just a Moment")
        mock_pw = _make_playwright_stack(bot_page)

        config = {"search": {"keywords": ["analyst"], "locations": [""]}}

        with patch("playwright.async_api.async_playwright", mock_pw), _patch_env():
            with caplog.at_level(logging.WARNING, logger="backend.job_sources.indeed"):
                await fetch_jobs(config)

        warning_msgs = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_msgs, "Expected at least one WARNING log when bot-blocked"
        assert any(
            "captcha" in r.message.lower() or "bot" in r.message.lower()
            for r in warning_msgs
        ), f"WARNING should mention captcha/bot, got: {[r.message for r in warning_msgs]}"

    @pytest.mark.asyncio
    async def test_captcha_selector_triggers_block(self, caplog):
        """Presence of div#captcha-box (not just title) also triggers the guard."""
        from backend.job_sources.indeed import fetch_jobs

        # Page with normal title but captcha DOM selector present
        captcha_page = MagicMock()

        async def _normal_title() -> str:
            return "Indeed Jobs"
        captcha_page.title = _normal_title
        captcha_page.goto = AsyncMock()
        captcha_page.wait_for_timeout = AsyncMock()
        captcha_page.wait_for_selector = AsyncMock()
        captcha_page.close = AsyncMock()

        def _captcha_locator(sel: str) -> MagicMock:
            loc = MagicMock()
            # div#captcha-box is present
            if "captcha" in sel:
                async def _count() -> int:
                    return 1
                loc.count = _count
            else:
                async def _count() -> int:
                    return 0
                loc.count = _count
            first = MagicMock()
            async def _is_visible(timeout: int = 2000) -> bool:
                return False
            first.is_visible = _is_visible
            loc.first = first
            return loc

        captcha_page.locator = _captcha_locator
        mock_pw = _make_playwright_stack(captcha_page)

        config = {"search": {"keywords": ["engineer"]}}

        with patch("playwright.async_api.async_playwright", mock_pw), _patch_env():
            with caplog.at_level(logging.WARNING, logger="backend.job_sources.indeed"):
                result = await fetch_jobs(config)

        assert result == []


# ---------------------------------------------------------------------------
# TestFetchJobsHappyPath
# ---------------------------------------------------------------------------

class TestFetchJobsHappyPath:
    """fetch_jobs() integration: _extract_cards patched, 3 jobs propagate."""

    _FAKE_CARDS = [
        {
            "source": "indeed", "external_id": "a1b2c3",
            "title": "Security Engineer", "company": "Acme", "location": "Remote",
            "url": "https://www.indeed.com/viewjob?jk=a1b2c3",
            "description": "", "salary_min": None, "salary_max": None,
            "job_type": None, "tags": [], "date_posted": "", "is_remote": True,
        },
        {
            "source": "indeed", "external_id": "d4e5f6",
            "title": "Penetration Tester", "company": "Beta", "location": "Austin, TX",
            "url": "https://www.indeed.com/viewjob?jk=d4e5f6",
            "description": "", "salary_min": None, "salary_max": None,
            "job_type": None, "tags": [], "date_posted": "", "is_remote": False,
        },
        {
            "source": "indeed", "external_id": "g7h8i9",
            "title": "SOC Analyst", "company": "Gamma", "location": "New York, NY",
            "url": "https://www.indeed.com/viewjob?jk=g7h8i9",
            "description": "", "salary_min": None, "salary_max": None,
            "job_type": None, "tags": [], "date_posted": "", "is_remote": False,
        },
    ]

    @pytest.mark.asyncio
    async def test_three_jobs_returned(self):
        """fetch_jobs() with 3 cards extracted → 3 dicts returned."""
        from backend.job_sources.indeed import fetch_jobs

        page = _make_page_with_cards([], title="Indeed Jobs")
        mock_pw = _make_playwright_stack(page)

        config = {"search": {"keywords": ["security"], "locations": ["Remote"]}}

        with patch("playwright.async_api.async_playwright", mock_pw), _patch_env():
            with patch(
                "backend.job_sources.indeed._extract_cards",
                new=AsyncMock(return_value=self._FAKE_CARDS),
            ):
                result = await fetch_jobs(config)

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_deduplication_by_external_id(self):
        """Duplicate external_ids across calls to _extract_cards are dropped."""
        from backend.job_sources.indeed import fetch_jobs

        duplicate_cards = self._FAKE_CARDS + [self._FAKE_CARDS[0]]  # a1b2c3 appears twice

        page = _make_page_with_cards([], title="Indeed Jobs")
        mock_pw = _make_playwright_stack(page)

        config = {"search": {"keywords": ["security"], "locations": ["Remote"]}}

        with patch("playwright.async_api.async_playwright", mock_pw), _patch_env():
            with patch(
                "backend.job_sources.indeed._extract_cards",
                new=AsyncMock(return_value=duplicate_cards),
            ):
                result = await fetch_jobs(config)

        ids = [j["external_id"] for j in result]
        assert len(ids) == len(set(ids)), "Duplicate external_ids must be removed"
        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_exclude_keywords_filter(self):
        """Jobs whose titles contain an exclude keyword are dropped."""
        from backend.job_sources.indeed import fetch_jobs

        page = _make_page_with_cards([], title="Indeed Jobs")
        mock_pw = _make_playwright_stack(page)

        config = {
            "search": {
                "keywords": ["security"],
                "locations": ["Remote"],
                "exclude_keywords": ["SOC"],  # should drop "SOC Analyst"
            }
        }

        with patch("playwright.async_api.async_playwright", mock_pw), _patch_env():
            with patch(
                "backend.job_sources.indeed._extract_cards",
                new=AsyncMock(return_value=self._FAKE_CARDS),
            ):
                result = await fetch_jobs(config)

        titles = [j["title"] for j in result]
        assert "SOC Analyst" not in titles
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_keywords(self):
        """fetch_jobs() returns [] immediately when no keywords are configured."""
        from backend.job_sources.indeed import fetch_jobs

        config = {"search": {"keywords": []}}
        result = await fetch_jobs(config)
        assert result == []


# ---------------------------------------------------------------------------
# TestFetchJobsNoNextPage
# ---------------------------------------------------------------------------

class TestFetchJobsNoNextPage:
    """Pagination stops when no job cards render on the next page."""

    @pytest.mark.asyncio
    async def test_stops_after_single_page(self):
        from backend.job_sources.indeed import fetch_jobs

        page = _make_page_with_cards([], title="Indeed Jobs")
        # Page 1: first card selector resolves. Page 2: every selector times
        # out (no cards rendered), which must end pagination for the query.
        page.wait_for_selector = AsyncMock(
            side_effect=[None] + [Exception("timeout")] * 10
        )
        mock_pw = _make_playwright_stack(page)

        config = {
            "search": {
                "keywords": ["engineer"],
                "indeed": {"max_pages": 5},
            }
        }
        extract_mock = AsyncMock(return_value=[
            {
                "source": "indeed", "external_id": "p1a",
                "title": "Engineer", "company": "Co", "location": "Remote",
                "url": "https://www.indeed.com/viewjob?jk=p1a",
                "description": "", "salary_min": None, "salary_max": None,
                "job_type": None, "tags": [], "date_posted": "", "is_remote": True,
            }
        ])

        with patch("playwright.async_api.async_playwright", mock_pw), _patch_env():
            with patch("backend.job_sources.indeed._extract_cards", new=extract_mock):
                result = await fetch_jobs(config)

        # Page 2 rendered no cards, so cards were only extracted once
        assert extract_mock.call_count == 1
        assert len(result) == 1


# ---------------------------------------------------------------------------
# TestFetchJobsMaxPages
# ---------------------------------------------------------------------------

class TestFetchJobsMaxPages:
    """fetch_jobs() respects max_pages=1 and never requests a &start= page."""

    @pytest.mark.asyncio
    async def test_max_pages_one_no_second_page(self):
        from backend.job_sources.indeed import fetch_jobs

        page = _make_page_with_cards([], title="Indeed Jobs")
        mock_pw = _make_playwright_stack(page)

        config = {
            "search": {
                "keywords": ["analyst"],
                "indeed": {"max_pages": 1},
            }
        }
        extract_mock = AsyncMock(return_value=[
            {
                "source": "indeed", "external_id": "m1",
                "title": "Analyst", "company": "Corp", "location": "NYC",
                "url": "https://www.indeed.com/viewjob?jk=m1",
                "description": "", "salary_min": None, "salary_max": None,
                "job_type": None, "tags": [], "date_posted": "", "is_remote": False,
            }
        ])

        with patch("playwright.async_api.async_playwright", mock_pw), _patch_env():
            with patch("backend.job_sources.indeed._extract_cards", new=extract_mock):
                result = await fetch_jobs(config)

        assert extract_mock.call_count == 1, "Should only extract cards once when max_pages=1"
        # Pagination is via direct &start=N URLs; with max_pages=1 only the
        # bare search URL may be requested.
        goto_urls = [c.args[0] for c in page.goto.await_args_list]
        assert len(goto_urls) == 1, f"Expected a single navigation, got {goto_urls}"
        assert "start=" not in goto_urls[0]
        assert len(result) == 1
