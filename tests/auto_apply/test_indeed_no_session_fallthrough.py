"""
tests/auto_apply/test_indeed_no_session_fallthrough.py

Regression tests for Phase 7 live validation (2026-03-28).

Bug: IndeedEasyApplyAdapter.apply() hard-stopped with "Indeed session expired"
     when no session sentinel existed, even for "Apply on company site" jobs
     that don't require an Indeed session.

Fix: Session failure is now a soft warning. The adapter proceeds to click the
     Apply button and only hard-stops if:
       (a) an auth wall appears after clicking (Easy Apply w/ no session), or
       (b) no Apply button AND no form fields are found.

For external-ATS jobs (redirect detected), the adapter hands off to the
appropriate downstream adapter (GenericAdapter or other) without requiring
any Indeed session.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.auto_apply.adapters.indeed import IndeedEasyApplyAdapter
from backend.auto_apply.models import (
    ApplyMode,
    ApplyResult,
    ApplyStatus,
    JobApplicationRequest,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_job(url: str = "https://www.indeed.com/viewjob?jk=0fe4e5185f7b387a") -> JobApplicationRequest:
    return JobApplicationRequest(
        job_id="test-no-session-001",
        title="Security Analyst",
        company="Acme Corp",
        url=url,
        description="Cybersecurity role.",
    )


def _make_ctrl(apply_btn_visible: bool = False, page_url: str = "https://www.indeed.com/viewjob?jk=abc") -> MagicMock:
    """Minimal BrowserController mock."""
    ctrl = MagicMock()
    ctrl.page = MagicMock()
    ctrl.page.url = page_url
    ctrl.page.wait_for_timeout = AsyncMock()
    ctrl.page.wait_for_load_state = AsyncMock()
    ctrl.navigate = AsyncMock()
    ctrl.dismiss_popups = AsyncMock(return_value=False)
    ctrl.get_dom_snapshot = AsyncMock(return_value=[])
    ctrl.screenshot = AsyncMock(return_value="/tmp/test_screenshot.png")
    ctrl.switch_to_new_page = AsyncMock(return_value="")
    ctrl.page_text = AsyncMock(return_value="")

    # Apply-button locator
    mock_loc = MagicMock()
    mock_loc.first = MagicMock()
    mock_loc.first.is_visible = AsyncMock(return_value=apply_btn_visible)
    mock_loc.first.click = AsyncMock()
    mock_loc.count = AsyncMock(return_value=0)
    ctrl.page.locator = MagicMock(return_value=mock_loc)
    return ctrl


def _make_log() -> MagicMock:
    log = MagicMock()
    log.entries = []
    return log


def _make_llm() -> MagicMock:
    llm = MagicMock()
    llm.map_fields_to_values = AsyncMock(return_value=[])
    return llm


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestIndeedNoSessionFallthrough:
    """
    Regression: missing Indeed session must NOT hard-stop the adapter.

    Pre-fix behaviour: adapter returned NEEDS_REVIEW("Indeed session expired")
                       before even navigating to the job page.

    Post-fix behaviour: adapter proceeds; session failure is a warning only.
    """

    @pytest.mark.asyncio
    async def test_no_session_does_not_return_session_expired(self):
        """
        REGRESSION: With no session sentinel, adapter must NOT return a
        NEEDS_REVIEW result containing 'session expired' before clicking Apply.

        Previously, the adapter hard-stopped here.  Now it must attempt
        Apply and fall through to the normal failure path (no apply button
        found / no fields detected).
        """
        adapter = IndeedEasyApplyAdapter()
        ctrl = _make_ctrl(apply_btn_visible=False)
        job = _make_job()
        log = _make_log()

        with patch(
            "backend.auto_apply.adapters.indeed._load_indeed_session",
            new=AsyncMock(return_value=False),          # no sentinel
        ), patch(
            "backend.auto_apply.adapters.indeed._is_auth_wall",
            new=AsyncMock(return_value=False),
        ):
            result = await adapter.apply(ctrl, MagicMock(), job, _make_llm(), ApplyMode.AUTOFILL, log)

        # Must NOT be the old hard-stop message
        assert "session expired" not in result.message.lower(), (
            f"Adapter must not hard-stop on missing session. Got: {result.message!r}"
        )

    @pytest.mark.asyncio
    async def test_no_session_navigate_called_on_job_url(self):
        """
        When no session, BrowserController.navigate() must NOT be called with
        indeed.com (session verification round-trip must be skipped).
        The adapter should stay on the job URL.
        """
        adapter = IndeedEasyApplyAdapter()
        ctrl = _make_ctrl(apply_btn_visible=False)
        job = _make_job(url="https://www.indeed.com/viewjob?jk=abc123")
        log = _make_log()

        with patch(
            "backend.auto_apply.adapters.indeed._load_indeed_session",
            new=AsyncMock(return_value=False),
        ), patch(
            "backend.auto_apply.adapters.indeed._is_auth_wall",
            new=AsyncMock(return_value=False),
        ):
            await adapter.apply(ctrl, MagicMock(), job, _make_llm(), ApplyMode.AUTOFILL, log)

        # Verify we did NOT navigate to indeed.com for session check
        navigate_calls = [str(c) for c in ctrl.navigate.call_args_list]
        indeed_home_calls = [c for c in navigate_calls if "https://www.indeed.com\"" in c
                             or "('https://www.indeed.com')" in c]
        assert not indeed_home_calls, (
            "Adapter navigated to indeed.com homepage for session check despite no sentinel. "
            f"navigate() calls: {navigate_calls}"
        )

    @pytest.mark.asyncio
    async def test_no_session_external_ats_redirect_hands_off(self):
        """
        REGRESSION: When Apply click redirects to an external ATS URL,
        adapter must hand off to the appropriate downstream adapter
        (not return NEEDS_REVIEW with 'session expired').
        """
        adapter = IndeedEasyApplyAdapter()
        ctrl = _make_ctrl(apply_btn_visible=True)
        job = _make_job()
        log = _make_log()

        # External URL that no specific adapter owns → falls to GenericAdapter
        external_url = "https://apply.externalats.com/jobs/99999/apply"

        generic_apply_calls: list[str] = []

        async def _fake_generic_apply(self_inner, ctrl_, profile_, job_, llm_, mode_, log_):
            generic_apply_calls.append(job_.url)
            return ApplyResult(
                success=True,
                status=ApplyStatus.AUTOFILL_COMPLETE,
                message="Generic: form filled (0 fields, 1 page(s)). Manual submit required.",
                adapter_used="generic",
                fields_filled=0,
                fields_skipped=0,
                log_entries=[],
            )

        with patch(
            "backend.auto_apply.adapters.indeed._load_indeed_session",
            new=AsyncMock(return_value=False),
        ), patch(
            "backend.auto_apply.adapters.indeed._click_apply_button",
            new=AsyncMock(return_value=True),
        ), patch(
            "backend.auto_apply.adapters.indeed._detect_redirect",
            new=AsyncMock(return_value=external_url),
        ), patch(
            "backend.auto_apply.adapters.generic.GenericAdapter.apply",
            new=_fake_generic_apply,
        ):
            result = await adapter.apply(
                ctrl, MagicMock(), job, _make_llm(), ApplyMode.AUTOFILL, log
            )

        assert result.adapter_used == "generic", (
            f"External ATS redirect should hand off to GenericAdapter. "
            f"Got adapter_used={result.adapter_used!r}, message={result.message!r}"
        )
        assert generic_apply_calls, (
            "GenericAdapter.apply() was never invoked after external-ATS redirect"
        )

    @pytest.mark.asyncio
    async def test_auth_wall_after_apply_click_still_returns_needs_review(self):
        """
        If an auth wall appears AFTER clicking Apply (i.e. Easy Apply requires
        login), the adapter must still return NEEDS_REVIEW with a session-expired
        message.  This ensures the old behavior is preserved for the correct case.
        """
        adapter = IndeedEasyApplyAdapter()
        ctrl = _make_ctrl(apply_btn_visible=True)
        job = _make_job()
        log = _make_log()

        with patch(
            "backend.auto_apply.adapters.indeed._load_indeed_session",
            new=AsyncMock(return_value=False),
        ), patch(
            "backend.auto_apply.adapters.indeed._click_apply_button",
            new=AsyncMock(return_value=True),
        ), patch(
            "backend.auto_apply.adapters.indeed._detect_redirect",
            new=AsyncMock(return_value=""),          # no redirect → native Easy Apply
        ), patch(
            "backend.auto_apply.adapters.indeed._is_auth_wall",
            new=AsyncMock(return_value=True),        # auth wall appeared
        ):
            result = await adapter.apply(
                ctrl, MagicMock(), job, _make_llm(), ApplyMode.AUTOFILL, log
            )

        assert result.status == ApplyStatus.NEEDS_REVIEW, (
            f"Auth wall after Apply click must yield NEEDS_REVIEW. Got: {result.status}"
        )
        assert "session" in result.message.lower(), (
            f"NEEDS_REVIEW message should mention session. Got: {result.message!r}"
        )
