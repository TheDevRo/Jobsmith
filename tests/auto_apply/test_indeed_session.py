"""
tests/auto_apply/test_indeed_session.py

Offline unit tests for indeed_login() in backend/auto_apply/__init__.py.
All Playwright and filesystem calls are mocked — no browser, network, or
running server required.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pw_stack(page: MagicMock) -> MagicMock:
    """
    Return a mock `async_playwright` callable that wires up a persistent-
    context Playwright stack pointing at *page*.

    Mirrors the new indeed_login() flow:
      pw.chromium.launch_persistent_context(...) → ctx
      ctx.pages  → [] (so ctx.new_page() is called)
      ctx.new_page() → page
    """
    ctx = AsyncMock()
    ctx.pages = []                          # empty → falls through to new_page()
    ctx.new_page = AsyncMock(return_value=page)
    ctx.close = AsyncMock()

    pw_instance = MagicMock()
    pw_instance.chromium.launch_persistent_context = AsyncMock(return_value=ctx)
    pw_instance.stop = AsyncMock()

    pw_cm = MagicMock()
    pw_cm.start = AsyncMock(return_value=pw_instance)

    mock_async_playwright = MagicMock(return_value=pw_cm)
    return mock_async_playwright


# ---------------------------------------------------------------------------
# Test: login completes when URL matches authenticated_paths
# ---------------------------------------------------------------------------

class TestIndeedLoginSuccess:
    @pytest.mark.asyncio
    async def test_success_on_myjobs_url(self, tmp_path):
        """State = success when URL transitions to /myjobs."""
        import backend.auto_apply as aa

        original_dir = aa.INDEED_CHROME_PROFILE_DIR
        aa.INDEED_CHROME_PROFILE_DIR = tmp_path / "indeed_chrome_profile"
        try:
            page = MagicMock()
            page.goto = AsyncMock()
            page.wait_for_timeout = AsyncMock()

            # First poll: still on login page.  Second poll: authenticated.
            urls = iter([
                "https://www.indeed.com/account/login",
                "https://www.indeed.com/myjobs",
            ])
            type(page).url = PropertyMock(side_effect=lambda: next(urls))

            mock_pw = _make_pw_stack(page)

            with patch("playwright.async_api.async_playwright", mock_pw):
                result = await aa.indeed_login()

            assert result.get("success") is True, result
            sentinel = aa.INDEED_CHROME_PROFILE_DIR / "login_success.json"
            assert sentinel.exists(), "login_success.json sentinel must be written on success"
        finally:
            aa.INDEED_CHROME_PROFILE_DIR = original_dir

    @pytest.mark.asyncio
    async def test_success_on_jobs_url(self, tmp_path):
        """State = success when URL transitions to /jobs."""
        import backend.auto_apply as aa

        original_dir = aa.INDEED_CHROME_PROFILE_DIR
        aa.INDEED_CHROME_PROFILE_DIR = tmp_path / "indeed_chrome_profile"
        try:
            page = MagicMock()
            page.goto = AsyncMock()
            page.wait_for_timeout = AsyncMock()

            urls = iter([
                "https://www.indeed.com/account/login",
                "https://www.indeed.com/jobs?q=engineer",
            ])
            type(page).url = PropertyMock(side_effect=lambda: next(urls))

            mock_pw = _make_pw_stack(page)

            with patch("playwright.async_api.async_playwright", mock_pw):
                result = await aa.indeed_login()

            assert result.get("success") is True, result
        finally:
            aa.INDEED_CHROME_PROFILE_DIR = original_dir


# ---------------------------------------------------------------------------
# Test: timeout after 5 minutes with no authenticated URL → failure
# ---------------------------------------------------------------------------

class TestIndeedLoginTimeout:
    @pytest.mark.asyncio
    async def test_timeout_returns_failure(self, tmp_path):
        """
        If the URL never matches authenticated_paths after 150 polls,
        indeed_login() returns success=False with a timeout message.
        """
        import backend.auto_apply as aa

        original_dir = aa.INDEED_CHROME_PROFILE_DIR
        aa.INDEED_CHROME_PROFILE_DIR = tmp_path / "indeed_chrome_profile"
        try:
            page = MagicMock()
            page.goto = AsyncMock()
            page.wait_for_timeout = AsyncMock()
            # URL never leaves the login page
            page.url = "https://www.indeed.com/account/login"

            mock_pw = _make_pw_stack(page)

            with patch("playwright.async_api.async_playwright", mock_pw):
                result = await aa.indeed_login()

            assert result.get("success") is not True, result
            msg = result.get("error", result.get("message", ""))
            assert "timeout" in msg.lower() or "5 minute" in msg.lower(), (
                f"Expected timeout message, got: {msg}"
            )
            sentinel = aa.INDEED_CHROME_PROFILE_DIR / "login_success.json"
            assert not sentinel.exists(), "sentinel must NOT be written on timeout"
        finally:
            aa.INDEED_CHROME_PROFILE_DIR = original_dir


# ---------------------------------------------------------------------------
# Test: redirect URL does NOT trigger early close (Condition B removed)
# ---------------------------------------------------------------------------

class TestIndeedLoginNoConditionB:
    @pytest.mark.asyncio
    async def test_redirect_url_does_not_break_loop(self, tmp_path):
        """
        A redirect URL that is neither the login page nor an authenticated
        path must NOT cause the loop to exit early.
        """
        import backend.auto_apply as aa

        original_dir = aa.INDEED_CHROME_PROFILE_DIR
        aa.INDEED_CHROME_PROFILE_DIR = tmp_path / "indeed_chrome_profile"
        try:
            page = MagicMock()
            page.goto = AsyncMock()
            page.wait_for_timeout = AsyncMock()

            urls = iter([
                "https://www.indeed.com/account/login",
                "https://secure.indeed.com/auth?redirect=%2Fmyjobs",  # intermediate
                "https://www.indeed.com/myjobs",                      # authenticated
            ])
            type(page).url = PropertyMock(side_effect=lambda: next(urls))

            mock_pw = _make_pw_stack(page)

            with patch("playwright.async_api.async_playwright", mock_pw):
                result = await aa.indeed_login()

            assert result.get("success") is True, (
                "Redirect URL should not break the loop — loop must reach "
                "the authenticated URL."
            )
        finally:
            aa.INDEED_CHROME_PROFILE_DIR = original_dir

    @pytest.mark.asyncio
    async def test_multiple_redirects_before_auth(self, tmp_path):
        """Multiple consecutive redirect/intermediate URLs must be tolerated."""
        import backend.auto_apply as aa

        original_dir = aa.INDEED_CHROME_PROFILE_DIR
        aa.INDEED_CHROME_PROFILE_DIR = tmp_path / "indeed_chrome_profile"
        try:
            page = MagicMock()
            page.goto = AsyncMock()
            page.wait_for_timeout = AsyncMock()

            urls = iter([
                "https://www.indeed.com/account/login",
                "https://secure.indeed.com/auth",
                "https://www.indeed.com/account/login?error=retry",
                "https://www.indeed.com/account/login",
                "https://www.indeed.com/dashboard",  # authenticated
            ])
            type(page).url = PropertyMock(side_effect=lambda: next(urls))

            mock_pw = _make_pw_stack(page)

            with patch("playwright.async_api.async_playwright", mock_pw):
                result = await aa.indeed_login()

            assert result.get("success") is True, result
        finally:
            aa.INDEED_CHROME_PROFILE_DIR = original_dir


# ---------------------------------------------------------------------------
# Test: _is_indeed_authenticated helper — exclusion-based URL detection
# ---------------------------------------------------------------------------

class TestIsIndeedAuthenticated:
    """Unit tests for the _is_indeed_authenticated() URL classifier."""

    def setup_method(self):
        import backend.auto_apply as aa
        self.fn = aa._is_indeed_authenticated

    def test_indeed_jobs_url_is_authenticated(self):
        assert self.fn("https://www.indeed.com/jobs") is True

    def test_indeed_jobs_with_query_is_authenticated(self):
        assert self.fn("https://www.indeed.com/jobs?q=engineer") is True

    def test_secure_indeed_login_is_not_authenticated(self):
        assert self.fn("https://secure.indeed.com/account/login") is False

    def test_www_indeed_login_is_not_authenticated(self):
        assert self.fn("https://www.indeed.com/account/login") is False

    def test_google_oauth_url_is_not_authenticated(self):
        assert self.fn("https://accounts.google.com/o/oauth2/v2/auth?client_id=indeed") is False

    def test_secure_indeed_auth_redirect_is_not_authenticated(self):
        assert self.fn("https://secure.indeed.com/auth?redirect=%2Fmyjobs") is False

    def test_secure_indeed_oauth_is_not_authenticated(self):
        assert self.fn("https://secure.indeed.com/oauth/authorize") is False

    def test_secure_indeed_root_after_google_auth_is_authenticated(self):
        assert self.fn("https://secure.indeed.com/") is True

    def test_myjobs_is_authenticated(self):
        assert self.fn("https://www.indeed.com/myjobs") is True

    def test_account_register_is_not_authenticated(self):
        assert self.fn("https://www.indeed.com/account/register") is False


# ---------------------------------------------------------------------------
# Test: indeed_login() integration — exclusion-based detection
# ---------------------------------------------------------------------------

class TestIndeedLoginExclusionLogic:
    @pytest.mark.asyncio
    async def test_www_indeed_jobs_triggers_success(self, tmp_path):
        """https://www.indeed.com/jobs must trigger login success."""
        import backend.auto_apply as aa

        original_dir = aa.INDEED_CHROME_PROFILE_DIR
        aa.INDEED_CHROME_PROFILE_DIR = tmp_path / "indeed_chrome_profile"
        try:
            page = MagicMock()
            page.goto = AsyncMock()
            page.wait_for_timeout = AsyncMock()

            urls = iter([
                "https://www.indeed.com/account/login",
                "https://accounts.google.com/o/oauth2/v2/auth",  # Google OAuth
                "https://www.indeed.com/jobs",                    # success
            ])
            type(page).url = PropertyMock(side_effect=lambda: next(urls))

            mock_pw = _make_pw_stack(page)

            with patch("playwright.async_api.async_playwright", mock_pw):
                result = await aa.indeed_login()

            assert result.get("success") is True, result
            sentinel = aa.INDEED_CHROME_PROFILE_DIR / "login_success.json"
            assert sentinel.exists(), "sentinel must be saved on success"
        finally:
            aa.INDEED_CHROME_PROFILE_DIR = original_dir

    @pytest.mark.asyncio
    async def test_secure_indeed_login_does_not_trigger_success(self, tmp_path):
        """https://secure.indeed.com/account/login must NOT trigger success."""
        import backend.auto_apply as aa

        original_dir = aa.INDEED_CHROME_PROFILE_DIR
        aa.INDEED_CHROME_PROFILE_DIR = tmp_path / "indeed_chrome_profile"
        try:
            page = MagicMock()
            page.goto = AsyncMock()
            page.wait_for_timeout = AsyncMock()
            page.url = "https://secure.indeed.com/account/login"

            mock_pw = _make_pw_stack(page)

            with patch("playwright.async_api.async_playwright", mock_pw):
                result = await aa.indeed_login()

            assert result.get("success") is not True, result
            sentinel = aa.INDEED_CHROME_PROFILE_DIR / "login_success.json"
            assert not sentinel.exists(), "sentinel must NOT be saved"
        finally:
            aa.INDEED_CHROME_PROFILE_DIR = original_dir

    @pytest.mark.asyncio
    async def test_google_oauth_url_does_not_trigger_success(self, tmp_path):
        """https://accounts.google.com/... must NOT trigger success."""
        import backend.auto_apply as aa

        original_dir = aa.INDEED_CHROME_PROFILE_DIR
        aa.INDEED_CHROME_PROFILE_DIR = tmp_path / "indeed_chrome_profile"
        try:
            page = MagicMock()
            page.goto = AsyncMock()
            page.wait_for_timeout = AsyncMock()
            page.url = "https://accounts.google.com/o/oauth2/v2/auth?client_id=indeed"

            mock_pw = _make_pw_stack(page)

            with patch("playwright.async_api.async_playwright", mock_pw):
                result = await aa.indeed_login()

            assert result.get("success") is not True, result
            sentinel = aa.INDEED_CHROME_PROFILE_DIR / "login_success.json"
            assert not sentinel.exists(), "sentinel must NOT be saved"
        finally:
            aa.INDEED_CHROME_PROFILE_DIR = original_dir
