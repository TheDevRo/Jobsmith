"""
tests/auto_apply/test_orchestrator_linkedin_session.py

Tests that the orchestrator emits a warning (including the expected
directory path) when a LinkedIn session is missing or incomplete.
All browser and LLM calls are mocked — runs fully offline.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.auto_apply.models import ApplyResult, ApplyStatus


_LINKEDIN_CONFIG = {
    "auto_apply": {
        "mode":                   "autofill",
        "submit_whitelist":       [],
        "max_daily_applications": 100,
        "per_domain_rate_limit":  50,
        "headless":               True,
    },
    "profile": {"full_name": "Test User", "email": "test@example.com"},
    "ai": {"base_url": "http://localhost:1234", "api_key": "lm-studio"},
}

_LINKEDIN_JOB = {
    "id":          "li-1",
    "title":       "Engineer",
    "company":     "Acme",
    "url":         "https://www.linkedin.com/jobs/view/12345",
    "description": "",
}

_MINIMAL_APP = {"id": "app-1"}


def _fake_linkedin_adapter():
    adapter = MagicMock()
    adapter.name = "linkedin"
    adapter.matches.return_value = True
    adapter.apply = AsyncMock(
        return_value=ApplyResult(
            success=True,
            status=ApplyStatus.AUTOFILL_COMPLETE,
            message="done",
            log_entries=[],
        )
    )
    return adapter


def _fake_ctrl():
    ctrl = AsyncMock()
    ctrl.__aenter__ = AsyncMock(return_value=ctrl)
    ctrl.__aexit__ = AsyncMock(return_value=False)
    ctrl.page_text = AsyncMock(return_value="")
    ctrl.navigate = AsyncMock()
    return ctrl


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warning_when_session_dir_missing(caplog, tmp_path):
    """logger.warning fires and includes the expected path when the dir is absent."""
    missing_dir = tmp_path / "linkedin_chrome_profile"  # does not exist

    with (
        patch("backend.auto_apply.orchestrator.BrowserController") as MockCtrl,
        patch("backend.auto_apply.orchestrator.LLMClient"),
        patch("backend.auto_apply.orchestrator._pick_adapter", return_value=_fake_linkedin_adapter()),
        patch("backend.auto_apply.orchestrator._linkedin_profile_dir", return_value=missing_dir),
    ):
        MockCtrl.return_value = _fake_ctrl()

        from backend.auto_apply import orchestrator

        with caplog.at_level(logging.WARNING, logger="backend.auto_apply.orchestrator"):
            await orchestrator.run_apply(
                _LINKEDIN_JOB, _MINIMAL_APP, {}, _LINKEDIN_CONFIG
            )

    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("not found" in m for m in warning_messages), (
        f"Expected 'not found' in warning; got: {warning_messages}"
    )
    assert any(str(missing_dir) in m for m in warning_messages), (
        f"Expected directory path in warning; got: {warning_messages}"
    )


@pytest.mark.asyncio
async def test_warning_when_sentinel_missing(caplog, tmp_path):
    """logger.warning fires and includes the dir path when sentinel file is absent."""
    session_dir = tmp_path / "linkedin_chrome_profile"
    session_dir.mkdir()
    # login_success.json intentionally NOT created

    with (
        patch("backend.auto_apply.orchestrator.BrowserController") as MockCtrl,
        patch("backend.auto_apply.orchestrator.LLMClient"),
        patch("backend.auto_apply.orchestrator._pick_adapter", return_value=_fake_linkedin_adapter()),
        patch("backend.auto_apply.orchestrator._linkedin_profile_dir", return_value=session_dir),
    ):
        MockCtrl.return_value = _fake_ctrl()

        from backend.auto_apply import orchestrator

        with caplog.at_level(logging.WARNING, logger="backend.auto_apply.orchestrator"):
            await orchestrator.run_apply(
                _LINKEDIN_JOB, _MINIMAL_APP, {}, _LINKEDIN_CONFIG
            )

    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("login was never completed" in m for m in warning_messages), (
        f"Expected 'login was never completed' in warning; got: {warning_messages}"
    )
    assert any(str(session_dir) in m for m in warning_messages), (
        f"Expected directory path in warning; got: {warning_messages}"
    )


@pytest.mark.asyncio
async def test_no_warning_when_session_valid(caplog, tmp_path):
    """No LinkedIn session warning when dir and sentinel both exist."""
    session_dir = tmp_path / "linkedin_chrome_profile"
    session_dir.mkdir()
    (session_dir / "login_success.json").write_text("{}")

    with (
        patch("backend.auto_apply.orchestrator.BrowserController") as MockCtrl,
        patch("backend.auto_apply.orchestrator.LLMClient"),
        patch("backend.auto_apply.orchestrator._pick_adapter", return_value=_fake_linkedin_adapter()),
        patch("backend.auto_apply.orchestrator._linkedin_profile_dir", return_value=session_dir),
    ):
        MockCtrl.return_value = _fake_ctrl()

        from backend.auto_apply import orchestrator

        with caplog.at_level(logging.WARNING, logger="backend.auto_apply.orchestrator"):
            await orchestrator.run_apply(
                _LINKEDIN_JOB, _MINIMAL_APP, {}, _LINKEDIN_CONFIG
            )

    session_warnings = [
        r.message for r in caplog.records
        if r.levelno == logging.WARNING and "session" in r.message.lower()
    ]
    assert not session_warnings, (
        f"Expected no session warning but got: {session_warnings}"
    )
