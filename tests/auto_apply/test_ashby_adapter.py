"""
tests/auto_apply/test_ashby_adapter.py

Offline tests for backend.auto_apply.adapters.ashby.AshbyAdapter:
  - matches() URL heuristics
  - adapter registration order (before GenericAdapter)
  - mocked apply() flow for AUTOFILL and SUBMIT modes

No real browser or LLM — BrowserController and LLMClient are mocked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.auto_apply.adapters import ALL_ADAPTERS
from backend.auto_apply.adapters.ashby import AshbyAdapter
from backend.auto_apply.models import (
    ApplyMode,
    ApplyStatus,
    FieldDescriptor,
    FieldValue,
    JobApplicationRequest,
    UserProfile,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def profile() -> UserProfile:
    raw = json.loads((FIXTURES / "sample_profile.json").read_text())
    return UserProfile(**raw)


@pytest.fixture()
def job() -> JobApplicationRequest:
    return JobApplicationRequest(
        job_id="test-ashby-001",
        title="Security Engineer",
        company="Acme",
        url="https://jobs.ashbyhq.com/acme/00207abc-49b7-465c-a219-f7c1140f8047/application",
        description="Security engineer role.",
    )


def _make_ctrl(snapshot_fields: list[FieldDescriptor] | None = None) -> MagicMock:
    """BrowserController mock. page.locator(...).first always resolves to a
    visible, empty element so the deterministic _fill helpers succeed."""
    element = MagicMock()
    element.is_visible = AsyncMock(return_value=True)
    element.input_value = AsyncMock(return_value="")
    element.click = AsyncMock()
    element.type = AsyncMock()
    element.set_input_files = AsyncMock()

    ctrl = MagicMock()
    ctrl.page = MagicMock()
    ctrl.page.url = "https://jobs.ashbyhq.com/acme/00207abc/application"
    ctrl.page.locator = MagicMock(return_value=MagicMock(first=element))
    ctrl.page.wait_for_load_state = AsyncMock()
    ctrl.page.wait_for_timeout = AsyncMock()
    ctrl.get_dom_snapshot = AsyncMock(return_value=snapshot_fields or [])
    ctrl.fill_field = AsyncMock(return_value=True)
    ctrl.select_field = AsyncMock(return_value=True)
    ctrl.check_field = AsyncMock(return_value=True)
    ctrl.click = AsyncMock(return_value=True)
    ctrl.page_text = AsyncMock(return_value="Thank you for applying!")
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


_NO_PAGE_ERRORS = patch(
    "backend.auto_apply.adapters.ashby.check_page_errors",
    new=AsyncMock(return_value=[]),
)


def _empty_answer_bank():
    return patch(
        "backend.auto_apply.answer_bank.get_answer_bank",
        return_value=MagicMock(all_snippets=lambda: {}),
    )


# ---------------------------------------------------------------------------
# matches()
# ---------------------------------------------------------------------------

class TestMatches:

    def test_matches_ashbyhq_job_url(self):
        adapter = AshbyAdapter()
        assert adapter.matches("https://jobs.ashbyhq.com/acme/1234-uuid", "") is True

    def test_matches_ashbyhq_application_url(self):
        adapter = AshbyAdapter()
        assert adapter.matches(
            "https://jobs.ashbyhq.com/acme/1234-uuid/application", ""
        ) is True

    def test_matches_api_host(self):
        adapter = AshbyAdapter()
        assert adapter.matches("https://api.ashbyhq.com/something", "") is True

    def test_matches_jobs_ashby_fragment(self):
        adapter = AshbyAdapter()
        assert adapter.matches("https://jobs.ashby.example.com/acme/123", "") is True

    def test_does_not_match_greenhouse(self):
        adapter = AshbyAdapter()
        assert adapter.matches("https://boards.greenhouse.io/acme/jobs/42", "") is False

    def test_does_not_match_unrelated_url(self):
        adapter = AshbyAdapter()
        assert adapter.matches("https://example.com/careers/security", "") is False

    def test_registered_before_generic_fallback(self):
        names = [a.name for a in ALL_ADAPTERS]
        assert "ashby" in names, "AshbyAdapter must be registered in ALL_ADAPTERS"
        assert names.index("ashby") < names.index("generic"), (
            "AshbyAdapter must come before the GenericAdapter fallback"
        )


# ---------------------------------------------------------------------------
# apply() — mocked flows
# ---------------------------------------------------------------------------

class TestApply:

    @pytest.mark.asyncio
    async def test_autofill_mode_fills_and_stops_before_submit(self, profile, job):
        """AUTOFILL fills the standard fields and never clicks submit."""
        adapter = AshbyAdapter()
        ctrl = _make_ctrl()

        with _NO_PAGE_ERRORS:
            result = await adapter.apply(
                ctrl, profile, job, _make_llm(), ApplyMode.AUTOFILL, _make_log()
            )

        assert result.success is True
        assert result.status == ApplyStatus.AUTOFILL_COMPLETE
        assert result.adapter_used == "ashby"
        # Name / email / phone were filled from the profile
        assert result.fields_filled >= 3
        # Submit must not be clicked in AUTOFILL mode
        ctrl.click.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_submit_mode_clicks_submit_and_confirms(self, profile, job):
        """SUBMIT clicks the submit button and detects the thank-you page."""
        adapter = AshbyAdapter()
        ctrl = _make_ctrl()

        with _NO_PAGE_ERRORS:
            result = await adapter.apply(
                ctrl, profile, job, _make_llm(), ApplyMode.SUBMIT, _make_log()
            )

        assert result.success is True
        assert result.status == ApplyStatus.SUBMITTED
        ctrl.click.assert_awaited_once()
        submit_selector = ctrl.click.await_args[0][0]
        assert "submit" in submit_selector.lower()

    @pytest.mark.asyncio
    async def test_submit_without_confirmation_needs_review(self, profile, job):
        """If no thank-you text appears after submit, flag for manual review."""
        adapter = AshbyAdapter()
        ctrl = _make_ctrl()
        ctrl.page_text = AsyncMock(return_value="Please fix the errors below.")

        with _NO_PAGE_ERRORS:
            result = await adapter.apply(
                ctrl, profile, job, _make_llm(), ApplyMode.SUBMIT, _make_log()
            )

        assert result.success is False
        assert result.status == ApplyStatus.NEEDS_REVIEW
        assert result.manual_url == job.url

    @pytest.mark.asyncio
    async def test_custom_questions_go_through_llm_mapping(self, profile, job):
        """Custom fields from the DOM snapshot are mapped via the LLM and filled."""
        adapter = AshbyAdapter()
        custom_field = FieldDescriptor(
            field_id="q_visa",
            label="Do you require visa sponsorship?",
            field_type="text",
            name="q_visa",
        )
        ctrl = _make_ctrl(snapshot_fields=[custom_field])
        llm = _make_llm([
            FieldValue(field_id="q_visa", value="No", action="fill",
                       confidence=0.95, source="profile"),
        ])

        with _NO_PAGE_ERRORS, _empty_answer_bank():
            result = await adapter.apply(
                ctrl, profile, job, llm, ApplyMode.AUTOFILL, _make_log()
            )

        llm.map_fields_to_values.assert_awaited_once()
        snapshot_arg = llm.map_fields_to_values.await_args[0][2]
        assert [f.field_id for f in snapshot_arg] == ["q_visa"]
        ctrl.fill_field.assert_awaited_once_with("q_visa", "No")
        assert result.status == ApplyStatus.AUTOFILL_COMPLETE

    @pytest.mark.asyncio
    async def test_system_fields_excluded_from_llm_mapping(self, profile, job):
        """_systemfield_* inputs are handled deterministically, never sent to the LLM."""
        adapter = AshbyAdapter()
        fields = [
            FieldDescriptor(field_id="_systemfield_email", label="Email",
                            field_type="email", name="_systemfield_email"),
            FieldDescriptor(field_id="q_start", label="When can you start?",
                            field_type="text", name="q_start"),
        ]
        ctrl = _make_ctrl(snapshot_fields=fields)
        llm = _make_llm([
            FieldValue(field_id="q_start", value="Immediately", action="fill",
                       confidence=0.9, source="profile"),
        ])

        with _NO_PAGE_ERRORS, _empty_answer_bank():
            await adapter.apply(
                ctrl, profile, job, llm, ApplyMode.AUTOFILL, _make_log()
            )

        snapshot_arg = llm.map_fields_to_values.await_args[0][2]
        assert [f.field_id for f in snapshot_arg] == ["q_start"], (
            "System fields must be filtered out before LLM mapping"
        )

    @pytest.mark.asyncio
    async def test_validation_errors_return_needs_review(self, profile, job):
        """Inline validation errors short-circuit to NEEDS_REVIEW before submit."""
        adapter = AshbyAdapter()
        ctrl = _make_ctrl()

        with patch(
            "backend.auto_apply.adapters.ashby.check_page_errors",
            new=AsyncMock(return_value=["Email is required"]),
        ):
            result = await adapter.apply(
                ctrl, profile, job, _make_llm(), ApplyMode.SUBMIT, _make_log()
            )

        assert result.success is False
        assert result.status == ApplyStatus.NEEDS_REVIEW
        assert "Email is required" in result.message
        ctrl.click.assert_not_awaited()
