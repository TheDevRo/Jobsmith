"""
tests/auto_apply/test_force_stop.py

Force-stop semantics in orchestrator.run_apply().

force_stop() interrupts an in-flight adapter by closing the browser out from
under it, which surfaces inside run_apply as a plain Playwright exception —
NOT as asyncio.CancelledError. The orchestrator must recognize that case as a
user-initiated stop (block_reason="stopped"), not record it as a failed apply,
and must not fall through to the assist handoff.

All browser and LLM calls are mocked — tests run fully offline.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


_MINIMAL_CONFIG = {
    "auto_apply": {
        "mode":                  "autofill",
        "max_daily_applications": 100,
        "per_domain_rate_limit":  50,
        "headless":              True,
    },
    "profile": {"full_name": "Test User", "email": "test@example.com"},
    "ai": {"base_url": "http://localhost:1234", "api_key": "lm-studio"},
}

_MINIMAL_JOB = {
    "id":          "job-1",
    "title":       "Engineer",
    "company":     "Acme",
    "url":         "https://example.com/jobs/1",
    "description": "",
}

_MINIMAL_APP = {"id": "app-1"}


def _mock_ctrl(MockCtrl):
    fake_ctrl            = AsyncMock()
    fake_ctrl.__aenter__ = AsyncMock(return_value=fake_ctrl)
    fake_ctrl.__aexit__  = AsyncMock(return_value=False)
    fake_ctrl.page_text  = AsyncMock(return_value="")
    fake_ctrl.navigate   = AsyncMock()
    MockCtrl.return_value = fake_ctrl
    return fake_ctrl


@pytest.mark.asyncio
async def test_adapter_error_after_force_stop_reports_stopped():
    """Browser torn down by force_stop mid-apply → 'stopped', not a failure."""
    from backend.auto_apply import orchestrator

    async def _apply_then_die(*a, **k):
        # Simulate force_stop() arriving while the adapter is mid-flight:
        # the event gets set, then the closed browser kills the in-flight call.
        orchestrator._force_stop_event.set()
        raise RuntimeError("Target page, context or browser has been closed")

    with (
        patch("backend.auto_apply.orchestrator.BrowserController") as MockCtrl,
        patch("backend.auto_apply.orchestrator.LLMClient"),
        patch("backend.auto_apply.orchestrator._pick_adapter") as mock_pick,
    ):
        fake_adapter        = MagicMock()
        fake_adapter.name   = "generic"
        fake_adapter.matches.return_value = True
        fake_adapter.apply  = AsyncMock(side_effect=_apply_then_die)
        mock_pick.return_value = fake_adapter
        _mock_ctrl(MockCtrl)

        legacy = await orchestrator.run_apply(
            _MINIMAL_JOB, _MINIMAL_APP, {}, _MINIMAL_CONFIG
        )

    assert legacy["success"] is False
    assert legacy["block_reason"] == "stopped"
    # A user stop must not read like an apply failure.
    assert "error" not in legacy["message"].lower()


@pytest.mark.asyncio
async def test_adapter_error_without_force_stop_still_fails_normally():
    """Same exception with NO force stop → normal failure path is preserved."""
    from backend.auto_apply import orchestrator

    with (
        patch("backend.auto_apply.orchestrator.BrowserController") as MockCtrl,
        patch("backend.auto_apply.orchestrator.LLMClient"),
        patch("backend.auto_apply.orchestrator._pick_adapter") as mock_pick,
    ):
        fake_adapter        = MagicMock()
        fake_adapter.name   = "generic"
        fake_adapter.matches.return_value = True
        fake_adapter.apply  = AsyncMock(side_effect=RuntimeError("selector timeout"))
        mock_pick.return_value = fake_adapter
        _mock_ctrl(MockCtrl)

        legacy = await orchestrator.run_apply(
            _MINIMAL_JOB, _MINIMAL_APP, {}, _MINIMAL_CONFIG
        )

    assert legacy["success"] is False
    assert legacy["block_reason"] != "stopped"
    assert "selector timeout" in legacy["message"]
