"""
tests/auto_apply/test_generic_pre_apply.py

Regression tests for Phase 7 live validation (2026-03-28).

Bug: GenericAdapter.apply() returned NEEDS_REVIEW("No form fields detected")
     immediately when a job description page was passed in, because it took
     a DOM snapshot before any Apply button was clicked.  Job description
     pages (e.g. dejobs.org, many employer career portals) require clicking
     an Apply CTA before the application form becomes visible.

Fix: When page 1 has no fields, GenericAdapter now attempts to find and click
     a pre-form Apply button (_click_pre_apply_button), then:
       - If a redirect to a known ATS is detected  → hand off to that adapter
       - If a redirect to an unknown host           → re-snapshot on new page
       - If same-page SPA transition / modal        → re-snapshot in place
       - If still no fields after click             → NEEDS_REVIEW (unchanged)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.auto_apply.adapters.generic import GenericAdapter
from backend.auto_apply.models import (
    ApplyMode,
    ApplyResult,
    ApplyStatus,
    FieldDescriptor,
    FieldValue,
    JobApplicationRequest,
    UserProfile,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def profile() -> UserProfile:
    raw = json.loads((FIXTURES / "sample_profile.json").read_text())
    return UserProfile(**raw)


@pytest.fixture()
def job() -> JobApplicationRequest:
    return JobApplicationRequest(
        job_id="test-generic-preapply-001",
        title="Water Treatment Specialist",
        company="Xylem Inc",
        url="https://xyleminc.dejobs.org/denver-co/reuse-and-aop-treatment-specialist/xxx/job/",
        description="Treatment specialist role.",
    )


def _make_field(field_id: str, label: str = "Field", field_type: str = "text") -> FieldDescriptor:
    return FieldDescriptor(
        field_id=field_id,
        label=label,
        field_type=field_type,
        required=False,
        options=[],
        placeholder="",
        current_value="",
    )


def _make_ctrl(
    first_snapshot_fields: list[FieldDescriptor],
    second_snapshot_fields: list[FieldDescriptor] | None = None,
    third_snapshot_fields: list[FieldDescriptor] | None = None,
    page_url: str = "https://xyleminc.dejobs.org/job/",
) -> MagicMock:
    """
    BrowserController mock with multi-phase snapshot support:
      - first call  → first_snapshot_fields  (initial domcontentloaded snapshot)
      - second call → second_snapshot_fields (after networkidle render-wait)
      - third call  → third_snapshot_fields  (after Apply click, optional)
      - further calls → [] (iterator exhausted)

    New flow (page 1 blank start):
      snapshot 1 → [] (blank SPA)
      snapshot 2 → [] or fields (after networkidle)
      snapshot 3 → fields (after Apply click reveals form)
    """
    ctrl = MagicMock()
    ctrl.page = MagicMock()
    ctrl.page.url = page_url
    ctrl.page.wait_for_load_state = AsyncMock()
    ctrl.page.wait_for_timeout = AsyncMock()
    ctrl.page.locator = MagicMock(
        return_value=MagicMock(all=AsyncMock(return_value=[]))
    )
    ctrl.page.evaluate = AsyncMock(return_value=None)
    ctrl.dismiss_popups = AsyncMock(return_value=False)
    ctrl.switch_to_new_page = AsyncMock(return_value="")

    snapshot_list = [first_snapshot_fields, second_snapshot_fields or []]
    if third_snapshot_fields is not None:
        snapshot_list.append(third_snapshot_fields)
    snapshots = iter(snapshot_list)
    ctrl.get_dom_snapshot = AsyncMock(side_effect=lambda: next(snapshots, []))
    ctrl.fill_field = AsyncMock(return_value=True)
    ctrl.select_field = AsyncMock(return_value=True)
    ctrl.check_field = AsyncMock(return_value=True)
    ctrl.upload_file = AsyncMock(return_value=False)
    ctrl.click = AsyncMock(return_value=False)
    ctrl.screenshot = AsyncMock(return_value="/tmp/screenshot.png")
    return ctrl


def _make_llm(mappings: list[FieldValue] | None = None) -> MagicMock:
    llm = MagicMock()
    llm.map_fields_to_values = AsyncMock(return_value=mappings or [])
    return llm


def _make_log() -> MagicMock:
    log = MagicMock()
    log.entries = []
    return log


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGenericPreApply:
    """
    Regression: GenericAdapter must attempt an Apply-button click when page 1
    has no form fields, rather than immediately returning NEEDS_REVIEW.
    """

    @pytest.mark.asyncio
    async def test_no_fields_triggers_pre_apply_hunt(self, profile, job):
        """
        REGRESSION: When page 1 has no fields, GenericAdapter must attempt
        _click_pre_apply_button before returning NEEDS_REVIEW.
        """
        adapter = GenericAdapter()
        ctrl = _make_ctrl(first_snapshot_fields=[])  # no fields on landing page

        pre_apply_called = []

        async def _fake_pre_apply(page):
            pre_apply_called.append(True)
            return False  # button not found

        with patch(
            "backend.auto_apply.adapters.generic._click_pre_apply_button",
            new=_fake_pre_apply,
        ), patch(
            "backend.auto_apply.adapters.generic._detect_generic_redirect",
            new=AsyncMock(return_value=""),
        ):
            result = await adapter.apply(
                ctrl, profile, job, _make_llm(), ApplyMode.AUTOFILL, _make_log()
            )

        assert pre_apply_called, (
            "GenericAdapter must call _click_pre_apply_button when page 1 has no fields"
        )

    @pytest.mark.asyncio
    async def test_pre_apply_not_found_returns_needs_review(self, profile, job):
        """
        If no Apply button is found and no fields appear, result is NEEDS_REVIEW
        (existing behaviour preserved).
        """
        adapter = GenericAdapter()
        ctrl = _make_ctrl(first_snapshot_fields=[], second_snapshot_fields=[])

        with patch(
            "backend.auto_apply.adapters.generic._click_pre_apply_button",
            new=AsyncMock(return_value=False),
        ), patch(
            "backend.auto_apply.adapters.generic._detect_generic_redirect",
            new=AsyncMock(return_value=""),
        ):
            result = await adapter.apply(
                ctrl, profile, job, _make_llm(), ApplyMode.AUTOFILL, _make_log()
            )

        assert result.status == ApplyStatus.NEEDS_REVIEW
        assert "no form fields" in result.message.lower()

    @pytest.mark.asyncio
    async def test_pre_apply_click_reveals_fields_then_fills(self, profile, job):
        """
        After Apply click reveals form fields (SPA modal / same-page transition),
        GenericAdapter must fill them and return AUTOFILL_COMPLETE.

        Snapshot sequence (3 calls):
          1. Initial snapshot → [] (blank SPA / job description page)
          2. After networkidle render-wait → [] (still no form; job desc page)
          3. After Apply click → form_fields (form revealed)
        """
        adapter = GenericAdapter()
        form_fields = [
            _make_field("field-0", "First name"),
            _make_field("field-1", "Last name"),
            _make_field("field-2", "Email"),
        ]
        mappings = [
            FieldValue(field_id="field-0", action="fill", value="Jane",
                       confidence=0.95, source="profile"),
            FieldValue(field_id="field-1", action="fill", value="Doe",
                       confidence=0.95, source="profile"),
            FieldValue(field_id="field-2", action="fill", value="jane@example.com",
                       confidence=0.95, source="profile"),
        ]
        # Three snapshots: blank → blank after render-wait → form after Apply click
        ctrl = _make_ctrl(
            first_snapshot_fields=[],
            second_snapshot_fields=[],
            third_snapshot_fields=form_fields,
        )

        with patch(
            "backend.auto_apply.adapters.generic._click_pre_apply_button",
            new=AsyncMock(return_value=True),   # button found and clicked
        ), patch(
            "backend.auto_apply.adapters.generic._detect_generic_redirect",
            new=AsyncMock(return_value=""),     # no redirect — same-page reveal
        ), patch(
            "backend.auto_apply.utils.browser_helpers.check_page_errors",
            new=AsyncMock(return_value=[]),
        ):
            result = await adapter.apply(
                ctrl, profile, job, _make_llm(mappings), ApplyMode.AUTOFILL, _make_log()
            )

        assert result.status == ApplyStatus.AUTOFILL_COMPLETE, (
            f"After Apply reveals form, should be AUTOFILL_COMPLETE. Got: {result.status}"
        )
        assert result.fields_filled > 0, (
            "Fields must be filled after Apply click reveals form"
        )

    @pytest.mark.asyncio
    async def test_pre_apply_redirect_to_known_ats_hands_off(self, profile, job):
        """
        If clicking Apply redirects to a known ATS (e.g. Greenhouse), GenericAdapter
        must hand off to that adapter, not try to fill the job description page.
        """
        adapter = GenericAdapter()
        greenhouse_url = "https://boards.greenhouse.io/xylem/jobs/12345"

        from backend.auto_apply.adapters.greenhouse import GreenhouseAdapter

        greenhouse_called = []

        async def _fake_greenhouse_apply(self_inner, *args, **kwargs):
            greenhouse_called.append(True)
            return ApplyResult(
                success=True,
                status=ApplyStatus.AUTOFILL_COMPLETE,
                message="Greenhouse: form filled (5 fields). Submit manually.",
                adapter_used="greenhouse",
                fields_filled=5,
                fields_skipped=0,
                log_entries=[],
            )

        ctrl = _make_ctrl(first_snapshot_fields=[])

        with patch(
            "backend.auto_apply.adapters.generic._click_pre_apply_button",
            new=AsyncMock(return_value=True),
        ), patch(
            "backend.auto_apply.adapters.generic._detect_generic_redirect",
            new=AsyncMock(return_value=greenhouse_url),
        ), patch.object(GreenhouseAdapter, "apply", _fake_greenhouse_apply):
            result = await adapter.apply(
                ctrl, profile, job, _make_llm(), ApplyMode.AUTOFILL, _make_log()
            )

        assert result.adapter_used == "greenhouse", (
            f"Redirect to Greenhouse URL should hand off to GreenhouseAdapter. "
            f"Got: {result.adapter_used!r}"
        )
        assert greenhouse_called, "GreenhouseAdapter.apply() was never called"

    @pytest.mark.asyncio
    async def test_page_with_fields_skips_pre_apply_hunt(self, profile, job):
        """
        If page 1 already has form fields, _click_pre_apply_button must NOT
        be called — adapter should go straight to LLM mapping.
        """
        adapter = GenericAdapter()
        form_fields = [_make_field("field-0", "Full name")]
        ctrl = _make_ctrl(first_snapshot_fields=form_fields)

        pre_apply_called = []

        async def _spy_pre_apply(page):
            pre_apply_called.append(True)
            return False

        with patch(
            "backend.auto_apply.adapters.generic._click_pre_apply_button",
            new=_spy_pre_apply,
        ), patch(
            "backend.auto_apply.utils.browser_helpers.check_page_errors",
            new=AsyncMock(return_value=[]),
        ):
            await adapter.apply(
                ctrl, profile, job,
                _make_llm([FieldValue(field_id="field-0", action="fill",
                                      value="Jane Doe", confidence=0.9, source="profile")]),
                ApplyMode.AUTOFILL, _make_log(),
            )

        assert not pre_apply_called, (
            "Pre-apply hunt must NOT run when form fields are already visible on page 1"
        )
