"""
tests/auto_apply/test_orchestrator_confidence.py

Unit tests for the low-confidence field override logic in orchestrator.run_apply().

All browser and LLM calls are mocked — tests run fully offline.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.auto_apply.models import (
    ApplyMode,
    ApplyResult,
    ApplyStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(status: ApplyStatus, log_entries: list[dict]) -> ApplyResult:
    return ApplyResult(
        success=status is ApplyStatus.SUBMITTED,
        status=status,
        message="ok",
        log_entries=log_entries,
    )


def _field_entry(field_id: str, confidence: float) -> dict:
    return {
        "level":      "field",
        "field_id":   field_id,
        "confidence": confidence,
        "message":    f"Field {field_id}",
    }


_MINIMAL_CONFIG = {
    "auto_apply": {
        "mode":                  "submit",
        "submit_whitelist":      ["example.com"],
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_low_confidence_submit_overrides_to_needs_review():
    """SUBMITTED + submit mode + low-confidence field → NEEDS_REVIEW."""
    entries = [
        _field_entry("cover_letter", 0.90),
        _field_entry("years_experience", 0.45),   # below threshold
    ]
    adapter_result = _make_result(ApplyStatus.SUBMITTED, entries)

    with (
        patch("backend.auto_apply.orchestrator.BrowserController") as MockCtrl,
        patch("backend.auto_apply.orchestrator.LLMClient"),
        patch("backend.auto_apply.orchestrator._pick_adapter") as mock_pick,
    ):
        # Set up a fake adapter that returns our result
        fake_adapter          = MagicMock()
        fake_adapter.name     = "greenhouse"
        fake_adapter.matches.return_value = True
        fake_adapter.apply    = AsyncMock(return_value=adapter_result)
        mock_pick.return_value = fake_adapter

        # Set up a fake BrowserController context manager
        fake_ctrl             = AsyncMock()
        fake_ctrl.__aenter__  = AsyncMock(return_value=fake_ctrl)
        fake_ctrl.__aexit__   = AsyncMock(return_value=False)
        fake_ctrl.page_text   = AsyncMock(return_value="")
        fake_ctrl.navigate    = AsyncMock()
        MockCtrl.return_value  = fake_ctrl

        from backend.auto_apply import orchestrator
        legacy = await orchestrator.run_apply(
            _MINIMAL_JOB, _MINIMAL_APP, {}, _MINIMAL_CONFIG
        )

    assert legacy["success"] is False
    assert legacy["block_reason"] == "needs_review"
    assert "years_experience" in legacy["message"]


@pytest.mark.asyncio
async def test_all_high_confidence_submit_stays_submitted():
    """SUBMITTED + submit mode + all high-confidence → stays SUBMITTED."""
    entries = [
        _field_entry("name",  0.95),
        _field_entry("email", 0.80),
        _field_entry("phone", 0.75),
    ]
    adapter_result = _make_result(ApplyStatus.SUBMITTED, entries)

    with (
        patch("backend.auto_apply.orchestrator.BrowserController") as MockCtrl,
        patch("backend.auto_apply.orchestrator.LLMClient"),
        patch("backend.auto_apply.orchestrator._pick_adapter") as mock_pick,
    ):
        fake_adapter          = MagicMock()
        fake_adapter.name     = "greenhouse"
        fake_adapter.matches.return_value = True
        fake_adapter.apply    = AsyncMock(return_value=adapter_result)
        mock_pick.return_value = fake_adapter

        fake_ctrl             = AsyncMock()
        fake_ctrl.__aenter__  = AsyncMock(return_value=fake_ctrl)
        fake_ctrl.__aexit__   = AsyncMock(return_value=False)
        fake_ctrl.page_text   = AsyncMock(return_value="")
        fake_ctrl.navigate    = AsyncMock()
        MockCtrl.return_value  = fake_ctrl

        from backend.auto_apply import orchestrator
        legacy = await orchestrator.run_apply(
            _MINIMAL_JOB, _MINIMAL_APP, {}, _MINIMAL_CONFIG
        )

    assert legacy["success"] is True
    assert legacy["block_reason"] == ""


@pytest.mark.asyncio
async def test_low_confidence_autofill_not_overridden():
    """AUTOFILL_COMPLETE + autofill mode → never overridden, even with low confidence."""
    entries = [
        _field_entry("cover_letter",    0.30),
        _field_entry("years_experience", 0.10),
    ]
    adapter_result = ApplyResult(
        success=True,
        status=ApplyStatus.AUTOFILL_COMPLETE,
        message="autofill done",
        log_entries=entries,
    )

    autofill_config = dict(_MINIMAL_CONFIG)
    autofill_config["auto_apply"] = dict(_MINIMAL_CONFIG["auto_apply"])
    autofill_config["auto_apply"]["mode"] = "autofill"

    with (
        patch("backend.auto_apply.orchestrator.BrowserController") as MockCtrl,
        patch("backend.auto_apply.orchestrator.LLMClient"),
        patch("backend.auto_apply.orchestrator._pick_adapter") as mock_pick,
    ):
        fake_adapter          = MagicMock()
        fake_adapter.name     = "greenhouse"
        fake_adapter.matches.return_value = True
        fake_adapter.apply    = AsyncMock(return_value=adapter_result)
        mock_pick.return_value = fake_adapter

        fake_ctrl             = AsyncMock()
        fake_ctrl.__aenter__  = AsyncMock(return_value=fake_ctrl)
        fake_ctrl.__aexit__   = AsyncMock(return_value=False)
        fake_ctrl.page_text   = AsyncMock(return_value="")
        fake_ctrl.navigate    = AsyncMock()
        MockCtrl.return_value  = fake_ctrl

        from backend.auto_apply import orchestrator
        legacy = await orchestrator.run_apply(
            _MINIMAL_JOB, _MINIMAL_APP, {}, autofill_config
        )

    assert legacy["success"] is True
    assert legacy["block_reason"] == ""


@pytest.mark.asyncio
async def test_exactly_at_threshold_not_flagged():
    """confidence == 0.60 is not below threshold — should not trigger override."""
    entries = [_field_entry("salary", 0.60)]
    adapter_result = _make_result(ApplyStatus.SUBMITTED, entries)

    with (
        patch("backend.auto_apply.orchestrator.BrowserController") as MockCtrl,
        patch("backend.auto_apply.orchestrator.LLMClient"),
        patch("backend.auto_apply.orchestrator._pick_adapter") as mock_pick,
    ):
        fake_adapter          = MagicMock()
        fake_adapter.name     = "greenhouse"
        fake_adapter.matches.return_value = True
        fake_adapter.apply    = AsyncMock(return_value=adapter_result)
        mock_pick.return_value = fake_adapter

        fake_ctrl             = AsyncMock()
        fake_ctrl.__aenter__  = AsyncMock(return_value=fake_ctrl)
        fake_ctrl.__aexit__   = AsyncMock(return_value=False)
        fake_ctrl.page_text   = AsyncMock(return_value="")
        fake_ctrl.navigate    = AsyncMock()
        MockCtrl.return_value  = fake_ctrl

        from backend.auto_apply import orchestrator
        legacy = await orchestrator.run_apply(
            _MINIMAL_JOB, _MINIMAL_APP, {}, _MINIMAL_CONFIG
        )

    assert legacy["success"] is True


@pytest.mark.asyncio
async def test_non_submitted_status_not_overridden():
    """FAILED status in submit mode is never overridden to NEEDS_REVIEW."""
    entries = [_field_entry("name", 0.10)]
    adapter_result = ApplyResult(
        success=False,
        status=ApplyStatus.FAILED,
        message="form error",
        log_entries=entries,
    )

    with (
        patch("backend.auto_apply.orchestrator.BrowserController") as MockCtrl,
        patch("backend.auto_apply.orchestrator.LLMClient"),
        patch("backend.auto_apply.orchestrator._pick_adapter") as mock_pick,
    ):
        fake_adapter          = MagicMock()
        fake_adapter.name     = "greenhouse"
        fake_adapter.matches.return_value = True
        fake_adapter.apply    = AsyncMock(return_value=adapter_result)
        mock_pick.return_value = fake_adapter

        fake_ctrl             = AsyncMock()
        fake_ctrl.__aenter__  = AsyncMock(return_value=fake_ctrl)
        fake_ctrl.__aexit__   = AsyncMock(return_value=False)
        fake_ctrl.page_text   = AsyncMock(return_value="")
        fake_ctrl.navigate    = AsyncMock()
        MockCtrl.return_value  = fake_ctrl

        from backend.auto_apply import orchestrator
        legacy = await orchestrator.run_apply(
            _MINIMAL_JOB, _MINIMAL_APP, {}, _MINIMAL_CONFIG
        )

    assert legacy["block_reason"] == "failed"
