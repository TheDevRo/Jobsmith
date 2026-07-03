"""
tests/auto_apply/test_linkedin_session_check.py

Unit tests for check_linkedin_session_validity() in backend/auto_apply/linkedin_auth.py.
All Playwright, filesystem, and network calls are mocked — no browser or network
required.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# Patch target: async_playwright is imported inline inside the function so we
# must patch the canonical module path, not the caller's module namespace.
_PW_PATCH = "playwright.async_api.async_playwright"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pw_stack(final_url: str) -> MagicMock:
    """
    Return a mock `async_playwright` callable for check_linkedin_session_validity().

    The function uses:
      pw.chromium.launch(headless=True, ...) → browser
      browser.new_context(storage_state=...) → ctx
      ctx.new_page() → page
      page.goto(...)
      page.url → final_url
    """
    page = MagicMock()
    page.url = final_url
    page.goto = AsyncMock()

    ctx = AsyncMock()
    ctx.new_page = AsyncMock(return_value=page)
    ctx.close = AsyncMock()

    browser = AsyncMock()
    browser.new_context = AsyncMock(return_value=ctx)
    browser.close = AsyncMock()

    pw_instance = MagicMock()
    pw_instance.chromium.launch = AsyncMock(return_value=browser)
    pw_instance.stop = AsyncMock()

    pw_cm = MagicMock()
    pw_cm.start = AsyncMock(return_value=pw_instance)

    mock_async_playwright = MagicMock(return_value=pw_cm)
    return mock_async_playwright


def _make_broken_pw_stack() -> MagicMock:
    """Return a mock async_playwright whose .start() raises RuntimeError."""
    pw_cm = MagicMock()
    pw_cm.start = AsyncMock(side_effect=RuntimeError("browser crashed"))
    return MagicMock(return_value=pw_cm)


# ---------------------------------------------------------------------------
# TestCheckLinkedInSessionValidity — core behaviour
# ---------------------------------------------------------------------------

class TestCheckLinkedInSessionValidity:
    """Tests for check_linkedin_session_validity()."""

    @pytest.mark.asyncio
    async def test_returns_true_when_feed_url(self, tmp_path):
        """Returns True when the browser lands on /feed (valid session)."""
        from backend.auto_apply import linkedin_auth as leg

        sentinel = tmp_path / "login_success.json"
        sentinel.write_text('{"logged_in_at": "2026-01-01T00:00:00"}')
        (tmp_path / "storage_state.json").write_text('{}')

        with (
            patch.object(leg, "LINKEDIN_SESSION_DIR", tmp_path),
            patch.object(leg, "has_linkedin_session", return_value=True),
            patch(_PW_PATCH, _make_pw_stack("https://www.linkedin.com/feed/")),
        ):
            result = await leg.check_linkedin_session_validity()

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_redirected_to_login(self, tmp_path):
        """Returns False when the browser is redirected to /login."""
        from backend.auto_apply import linkedin_auth as leg

        sentinel = tmp_path / "login_success.json"
        sentinel.write_text('{"logged_in_at": "2026-01-01T00:00:00"}')
        (tmp_path / "storage_state.json").write_text('{}')

        with (
            patch.object(leg, "LINKEDIN_SESSION_DIR", tmp_path),
            patch.object(leg, "has_linkedin_session", return_value=True),
            patch(_PW_PATCH, _make_pw_stack("https://www.linkedin.com/login?session_redirect=abc")),
        ):
            result = await leg.check_linkedin_session_validity()

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_redirected_to_authwall(self, tmp_path):
        """Returns False when the browser is redirected to /authwall."""
        from backend.auto_apply import linkedin_auth as leg

        sentinel = tmp_path / "login_success.json"
        sentinel.write_text('{"logged_in_at": "2026-01-01T00:00:00"}')
        (tmp_path / "storage_state.json").write_text('{}')

        with (
            patch.object(leg, "LINKEDIN_SESSION_DIR", tmp_path),
            patch.object(leg, "has_linkedin_session", return_value=True),
            patch(_PW_PATCH, _make_pw_stack("https://www.linkedin.com/authwall?trk=xyz")),
        ):
            result = await leg.check_linkedin_session_validity()

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_sentinel(self, tmp_path):
        """Returns False immediately when has_linkedin_session() returns False."""
        from backend.auto_apply import linkedin_auth as leg

        with (
            patch.object(leg, "LINKEDIN_SESSION_DIR", tmp_path),
            patch.object(leg, "has_linkedin_session", return_value=False),
        ):
            result = await leg.check_linkedin_session_validity()

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_storage_state_missing(self, tmp_path):
        """Returns False when storage_state.json does not exist (no browser launch)."""
        from backend.auto_apply import linkedin_auth as leg

        sentinel = tmp_path / "login_success.json"
        sentinel.write_text('{"logged_in_at": "2026-01-01T00:00:00"}')
        # storage_state.json intentionally absent

        with (
            patch.object(leg, "LINKEDIN_SESSION_DIR", tmp_path),
            patch.object(leg, "has_linkedin_session", return_value=True),
        ):
            result = await leg.check_linkedin_session_validity()

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_playwright_exception(self, tmp_path):
        """Returns False (no crash) when Playwright raises an exception."""
        from backend.auto_apply import linkedin_auth as leg

        sentinel = tmp_path / "login_success.json"
        sentinel.write_text('{"logged_in_at": "2026-01-01T00:00:00"}')
        (tmp_path / "storage_state.json").write_text('{}')

        with (
            patch.object(leg, "LINKEDIN_SESSION_DIR", tmp_path),
            patch.object(leg, "has_linkedin_session", return_value=True),
            patch(_PW_PATCH, _make_broken_pw_stack()),
        ):
            result = await leg.check_linkedin_session_validity()

        assert result is False

    @pytest.mark.asyncio
    async def test_browser_cleanup_called_on_success(self, tmp_path):
        """ctx.close(), browser.close(), and pw.stop() are all called."""
        from backend.auto_apply import linkedin_auth as leg

        sentinel = tmp_path / "login_success.json"
        sentinel.write_text('{"logged_in_at": "2026-01-01T00:00:00"}')
        (tmp_path / "storage_state.json").write_text('{}')

        page = MagicMock()
        page.url = "https://www.linkedin.com/feed/"
        page.goto = AsyncMock()
        ctx = AsyncMock()
        ctx.new_page = AsyncMock(return_value=page)
        ctx.close = AsyncMock()
        browser = AsyncMock()
        browser.new_context = AsyncMock(return_value=ctx)
        browser.close = AsyncMock()
        pw_instance = MagicMock()
        pw_instance.chromium.launch = AsyncMock(return_value=browser)
        pw_instance.stop = AsyncMock()
        pw_cm = MagicMock()
        pw_cm.start = AsyncMock(return_value=pw_instance)
        mock_pw = MagicMock(return_value=pw_cm)

        with (
            patch.object(leg, "LINKEDIN_SESSION_DIR", tmp_path),
            patch.object(leg, "has_linkedin_session", return_value=True),
            patch(_PW_PATCH, mock_pw),
        ):
            result = await leg.check_linkedin_session_validity()

        assert result is True
        ctx.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        pw_instance.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_browser_cleanup_called_on_navigation_failure(self, tmp_path):
        """Cleanup runs even when page.goto() raises (exception path)."""
        from backend.auto_apply import linkedin_auth as leg

        sentinel = tmp_path / "login_success.json"
        sentinel.write_text('{"logged_in_at": "2026-01-01T00:00:00"}')
        (tmp_path / "storage_state.json").write_text('{}')

        page = MagicMock()
        page.goto = AsyncMock(side_effect=Exception("navigation timeout"))
        ctx = AsyncMock()
        ctx.new_page = AsyncMock(return_value=page)
        ctx.close = AsyncMock()
        browser = AsyncMock()
        browser.new_context = AsyncMock(return_value=ctx)
        browser.close = AsyncMock()
        pw_instance = MagicMock()
        pw_instance.chromium.launch = AsyncMock(return_value=browser)
        pw_instance.stop = AsyncMock()
        pw_cm = MagicMock()
        pw_cm.start = AsyncMock(return_value=pw_instance)
        mock_pw = MagicMock(return_value=pw_cm)

        with (
            patch.object(leg, "LINKEDIN_SESSION_DIR", tmp_path),
            patch.object(leg, "has_linkedin_session", return_value=True),
            patch(_PW_PATCH, mock_pw),
        ):
            result = await leg.check_linkedin_session_validity()

        assert result is False
        ctx.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        pw_instance.stop.assert_awaited_once()
