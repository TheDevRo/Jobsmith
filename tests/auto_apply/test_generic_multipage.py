"""
tests/auto_apply/test_generic_multipage.py

Unit tests for the multi-page loop in GenericAdapter.apply().

All browser and LLM calls are mocked — tests run fully offline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.auto_apply.adapters.generic import GenericAdapter, _MAX_PAGES
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
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def profile() -> UserProfile:
    raw = json.loads((FIXTURES / "sample_profile.json").read_text())
    return UserProfile(**raw)


@pytest.fixture()
def job() -> JobApplicationRequest:
    return JobApplicationRequest(
        job_id="test-multipage-001",
        title="Senior Python Engineer",
        company="TestCo",
        url="https://apply.testco.com/jobs/123",
        description="We need a senior Python engineer.",
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


def _make_mapping(field_id: str, value: str = "test value", confidence: float = 0.95) -> FieldValue:
    return FieldValue(
        field_id=field_id,
        action="fill",
        value=value,
        confidence=confidence,
        source="profile",
    )


def _make_ctrl(
    fields_per_page: list[list[FieldDescriptor]],
    page_errors_sequence: list[list[str]] | None = None,
) -> MagicMock:
    """
    Build a mock BrowserController.

    fields_per_page: each call to get_dom_snapshot() returns the next list.
    page_errors_sequence: unused here (errors come from _check_page_errors mock).
    """
    ctrl = MagicMock()
    ctrl.page = MagicMock()
    ctrl.page.url = "https://apply.testco.com/jobs/123"
    ctrl.page.wait_for_load_state = AsyncMock()
    ctrl.page.wait_for_timeout = AsyncMock()
    ctrl.page.locator = MagicMock(return_value=MagicMock(all=AsyncMock(return_value=[])))

    snapshot_iter = iter(fields_per_page)
    ctrl.dismiss_popups = AsyncMock(return_value=False)
    ctrl.get_dom_snapshot = AsyncMock(side_effect=lambda: next(snapshot_iter, []))
    ctrl.fill_field = AsyncMock(return_value=True)
    ctrl.select_field = AsyncMock(return_value=True)
    ctrl.check_field = AsyncMock(return_value=True)
    ctrl.upload_file = AsyncMock(return_value=False)
    ctrl.click = AsyncMock(return_value=True)
    ctrl.screenshot = AsyncMock(return_value="/tmp/screenshot.png")
    return ctrl


def _make_llm(mappings_per_page: list[list[FieldValue]]) -> MagicMock:
    """Each call to map_fields_to_values() returns the next mapping list."""
    llm = MagicMock()
    mapping_iter = iter(mappings_per_page)
    llm.map_fields_to_values = AsyncMock(side_effect=lambda *_: next(mapping_iter, []))
    return llm


def _make_log() -> MagicMock:
    log = MagicMock()
    log.entries = []
    log.step = MagicMock()
    log.warning = MagicMock()
    log.result = MagicMock()
    log.llm_call = MagicMock()
    log.field = MagicMock()
    log.adapter_chosen = MagicMock()
    return log


def _make_next_btn(page: MagicMock) -> MagicMock:
    btn = MagicMock()
    btn.is_visible = AsyncMock(return_value=True)
    btn.is_enabled = AsyncMock(return_value=True)
    btn.text_content = AsyncMock(return_value="Next")
    btn.click = AsyncMock()
    return btn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSinglePage:
    """Adapter still works correctly for single-page (no next button) forms."""

    @pytest.mark.asyncio
    async def test_autofill_single_page(self, profile, job):
        fields = [_make_field("f1"), _make_field("f2")]
        ctrl = _make_ctrl([[fields[0], fields[1]]])
        llm = _make_llm([[_make_mapping("f1"), _make_mapping("f2")]])
        log = _make_log()

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=[]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(return_value=None),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(ctrl, profile, job, llm, ApplyMode.AUTOFILL, log)

        assert result.status == ApplyStatus.AUTOFILL_COMPLETE
        assert result.fields_filled == 2
        assert "1 page" in result.message

    @pytest.mark.asyncio
    async def test_submit_single_page(self, profile, job):
        fields = [_make_field("f1")]
        ctrl = _make_ctrl([[fields[0]]])
        llm = _make_llm([[_make_mapping("f1")]])
        log = _make_log()

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=[]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(return_value=None),
            ),
            patch(
                "backend.auto_apply.adapters.generic._click_submit",
                AsyncMock(return_value=True),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(ctrl, profile, job, llm, ApplyMode.SUBMIT, log)

        assert result.status == ApplyStatus.SUBMITTED
        assert "1 page" in result.message


class TestMultiPage:
    """Next button causes the loop to advance through multiple pages."""

    @pytest.mark.asyncio
    async def test_two_page_autofill(self, profile, job):
        page1 = [_make_field("p1_f1")]
        page2 = [_make_field("p2_f1"), _make_field("p2_f2")]
        ctrl = _make_ctrl([page1, page2])
        llm = _make_llm([
            [_make_mapping("p1_f1")],
            [_make_mapping("p2_f1"), _make_mapping("p2_f2")],
        ])
        log = _make_log()

        # next button present on page 1, absent on page 2
        next_btn = _make_next_btn(ctrl.page)
        next_button_returns = iter([next_btn, None])

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=[]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(side_effect=lambda _: next(next_button_returns)),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(ctrl, profile, job, llm, ApplyMode.AUTOFILL, log)

        assert result.status == ApplyStatus.AUTOFILL_COMPLETE
        assert result.fields_filled == 3
        assert "2 page" in result.message

    @pytest.mark.asyncio
    async def test_two_page_submit(self, profile, job):
        page1 = [_make_field("p1_f1")]
        page2 = [_make_field("p2_f1")]
        ctrl = _make_ctrl([page1, page2])
        llm = _make_llm([[_make_mapping("p1_f1")], [_make_mapping("p2_f1")]])
        log = _make_log()

        next_btn = _make_next_btn(ctrl.page)
        next_button_returns = iter([next_btn, None])

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=[]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(side_effect=lambda _: next(next_button_returns)),
            ),
            patch(
                "backend.auto_apply.adapters.generic._click_submit",
                AsyncMock(return_value=True),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(ctrl, profile, job, llm, ApplyMode.SUBMIT, log)

        assert result.status == ApplyStatus.SUBMITTED
        assert result.fields_filled == 2

    @pytest.mark.asyncio
    async def test_next_button_clicked_on_each_page(self, profile, job):
        """Verify the next button is actually clicked to advance pages."""
        pages = [[_make_field(f"p{i}_f1")] for i in range(1, 4)]
        ctrl = _make_ctrl(pages)
        llm = _make_llm([[_make_mapping(f"p{i}_f1")] for i in range(1, 4)])
        log = _make_log()

        next_btns = [_make_next_btn(ctrl.page) for _ in range(2)]
        btn_iter = iter([*next_btns, None])

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=[]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(side_effect=lambda _: next(btn_iter)),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(ctrl, profile, job, llm, ApplyMode.AUTOFILL, log)

        assert result.status == ApplyStatus.AUTOFILL_COMPLETE
        assert result.fields_filled == 3
        for btn in next_btns:
            btn.click.assert_called_once()


class TestPageCapGuard:
    """Loop stops at _MAX_PAGES and returns NEEDS_REVIEW."""

    @pytest.mark.asyncio
    async def test_max_pages_exceeded(self, profile, job):
        # Provide _MAX_PAGES + 1 pages worth of fields; next button always present
        pages = [[_make_field(f"p{i}_f1")] for i in range(_MAX_PAGES + 1)]
        ctrl = _make_ctrl(pages)
        llm = _make_llm([[_make_mapping(f"p{i}_f1")] for i in range(_MAX_PAGES + 1)])
        log = _make_log()

        always_next = _make_next_btn(ctrl.page)

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=[]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(return_value=always_next),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(ctrl, profile, job, llm, ApplyMode.SUBMIT, log)

        assert result.status == ApplyStatus.NEEDS_REVIEW
        assert "15 pages" in result.message
        # Warning must have been logged
        warning_messages = [str(call) for call in log.warning.call_args_list]
        assert any("15 pages" in m for m in warning_messages)


class TestPageErrors:
    """Inline validation errors on any page cause immediate NEEDS_REVIEW."""

    @pytest.mark.asyncio
    async def test_error_on_first_page(self, profile, job):
        ctrl = _make_ctrl([[_make_field("f1")]])
        llm = _make_llm([[_make_mapping("f1")]])
        log = _make_log()

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=["Email is required"]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(return_value=None),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(ctrl, profile, job, llm, ApplyMode.SUBMIT, log)

        assert result.status == ApplyStatus.NEEDS_REVIEW
        assert "Email is required" in result.message

    @pytest.mark.asyncio
    async def test_error_on_second_page(self, profile, job):
        pages = [[_make_field("p1_f1")], [_make_field("p2_f1")]]
        ctrl = _make_ctrl(pages)
        llm = _make_llm([[_make_mapping("p1_f1")], [_make_mapping("p2_f1")]])
        log = _make_log()

        next_btn = _make_next_btn(ctrl.page)
        # No errors page 1, one error page 2
        errors_iter = iter([[], ["Phone number invalid"]])

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(side_effect=lambda _: next(errors_iter)),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(side_effect=[next_btn, None]),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(ctrl, profile, job, llm, ApplyMode.SUBMIT, log)

        assert result.status == ApplyStatus.NEEDS_REVIEW
        assert "Phone number invalid" in result.message


class TestNoFieldsOnFirstPage:
    """No fields on the very first page → NEEDS_REVIEW immediately."""

    @pytest.mark.asyncio
    async def test_no_fields_first_page(self, profile, job):
        ctrl = _make_ctrl([[]])  # empty field list
        llm = _make_llm([])
        log = _make_log()

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=[]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(return_value=None),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(ctrl, profile, job, llm, ApplyMode.SUBMIT, log)

        assert result.status == ApplyStatus.NEEDS_REVIEW
        assert "No form fields" in result.message


class TestNoFieldsOnLaterPage:
    """No fields on a page after page 1 → treated as final page, proceed to submit."""

    @pytest.mark.asyncio
    async def test_no_fields_on_page_2_proceeds_to_submit(self, profile, job):
        """Page 2 with zero fields: loop breaks (no next button found), submit is attempted."""
        page1 = [_make_field("p1_f1")]
        ctrl = _make_ctrl([page1, []])  # page 2 has no fields
        llm = _make_llm([[_make_mapping("p1_f1")]])
        log = _make_log()

        next_btn = _make_next_btn(ctrl.page)
        # next present on page 1; page 2 has no fields so next detection is skipped
        next_button_returns = iter([next_btn, None])

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=[]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(side_effect=lambda _: next(next_button_returns)),
            ),
            patch(
                "backend.auto_apply.adapters.generic._click_submit",
                AsyncMock(return_value=True),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(ctrl, profile, job, llm, ApplyMode.SUBMIT, log)

        # Should reach submit (not NEEDS_REVIEW for empty later page)
        assert result.status == ApplyStatus.SUBMITTED
        assert result.fields_filled == 1


class TestSubmitButtonNotFound:
    """Submit button absent on final page → NEEDS_REVIEW."""

    @pytest.mark.asyncio
    async def test_submit_not_found_returns_needs_review(self, profile, job):
        ctrl = _make_ctrl([[_make_field("f1")]])
        llm = _make_llm([[_make_mapping("f1")]])
        log = _make_log()

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=[]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(return_value=None),
            ),
            patch(
                "backend.auto_apply.adapters.generic._click_submit",
                AsyncMock(return_value=False),  # ← no submit button found
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(ctrl, profile, job, llm, ApplyMode.SUBMIT, log)

        assert result.status == ApplyStatus.NEEDS_REVIEW
        assert "Submit button" in result.message or "submit" in result.message.lower()
        assert result.manual_url == job.url


class TestLowConfidence:
    """Low-confidence fields: autofill behaviour depends on severity."""

    @pytest.mark.asyncio
    async def test_low_confidence_autofill_returns_complete(self, profile, job):
        """Autofill: fields between 0.50-0.60 are flagged but still AUTOFILL_COMPLETE."""
        ctrl = _make_ctrl([[_make_field("f1"), _make_field("f2")]])
        # one high-conf, one moderately low-conf (above 0.50 NEEDS_REVIEW threshold)
        llm = _make_llm([[
            _make_mapping("f1", confidence=0.95),
            _make_mapping("f2", confidence=0.55),  # below _LOW_CONF (0.60) but above _NEEDS_REVIEW_CONF (0.50)
        ]])
        log = _make_log()

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=[]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(return_value=None),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(ctrl, profile, job, llm, ApplyMode.AUTOFILL, log)

        assert result.status == ApplyStatus.AUTOFILL_COMPLETE
        assert result.success is True
        # Low-conf field name must appear in the message
        assert "f2" in result.message

    @pytest.mark.asyncio
    async def test_low_confidence_submit_blocked_to_needs_review(self, profile, job):
        """Submit: low-confidence fields → NEEDS_REVIEW (application not submitted)."""
        ctrl = _make_ctrl([[_make_field("f1"), _make_field("f2")]])
        llm = _make_llm([[
            _make_mapping("f1", confidence=0.95),
            _make_mapping("f2", confidence=0.30),  # well below threshold
        ]])
        log = _make_log()

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=[]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(return_value=None),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(ctrl, profile, job, llm, ApplyMode.SUBMIT, log)

        assert result.status == ApplyStatus.NEEDS_REVIEW
        assert result.success is False
        assert "f2" in result.message
        assert result.manual_url == job.url

    @pytest.mark.asyncio
    async def test_confidence_at_threshold_not_flagged(self, profile, job):
        """Confidence exactly at _LOW_CONF (0.60) is NOT treated as low-confidence."""
        ctrl = _make_ctrl([[_make_field("f1")]])
        # Exactly at the threshold — must not be flagged
        llm = _make_llm([[_make_mapping("f1", confidence=0.60)]])
        log = _make_log()

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=[]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(return_value=None),
            ),
            patch(
                "backend.auto_apply.adapters.generic._click_submit",
                AsyncMock(return_value=True),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(ctrl, profile, job, llm, ApplyMode.SUBMIT, log)

        # Exactly at threshold → not low-conf → should proceed to submit
        assert result.status == ApplyStatus.SUBMITTED


class TestFileUpload:
    """File upload fields are handled separately from regular fields."""

    @pytest.mark.asyncio
    async def test_resume_upload_on_resume_field(self, profile, job, tmp_path):
        """A 'file' field with 'resume' in the label triggers upload of resume_path."""
        resume_file = tmp_path / "resume.pdf"
        resume_file.write_text("fake pdf")

        job_with_resume = JobApplicationRequest(
            job_id="upload-test-001",
            title="Engineer",
            company="TestCo",
            url="https://apply.testco.com/jobs/99",
            resume_path=str(resume_file),
        )

        resume_field = _make_field("resume_upload", label="Upload Resume", field_type="file")
        ctrl = _make_ctrl([[resume_field]])
        ctrl.upload_file = AsyncMock(return_value=True)
        llm = _make_llm([])  # no regular-field mappings (upload handled separately)
        log = _make_log()

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=[]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(return_value=None),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(
                ctrl, profile, job_with_resume, llm, ApplyMode.AUTOFILL, log
            )

        assert result.status == ApplyStatus.AUTOFILL_COMPLETE
        # upload_file must have been called with the resume field ID and path
        ctrl.upload_file.assert_called_once_with("resume_upload", str(resume_file))
        assert result.fields_filled == 1

    @pytest.mark.asyncio
    async def test_file_upload_skipped_when_no_resume_path(self, profile, job):
        """File upload field is skipped when job has no resume_path."""
        resume_field = _make_field("resume_upload", label="Upload CV", field_type="file")
        ctrl = _make_ctrl([[resume_field]])
        ctrl.upload_file = AsyncMock(return_value=False)
        llm = _make_llm([])
        log = _make_log()

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=[]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(return_value=None),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(
                ctrl, profile, job, llm, ApplyMode.AUTOFILL, log
            )

        assert result.status == ApplyStatus.AUTOFILL_COMPLETE
        # No upload should have been attempted (job.resume_path is None)
        ctrl.upload_file.assert_not_called()
        assert result.fields_skipped == 1


class TestPopupDismissal:
    """Popups are dismissed before field enumeration."""

    @pytest.mark.asyncio
    async def test_popup_dismissed_before_fill(self, profile, job):
        """When dismiss_popups returns True, a wait is inserted before proceeding."""
        fields = [_make_field("f1")]
        ctrl = _make_ctrl([[fields[0]]])
        ctrl.dismiss_popups = AsyncMock(return_value=True)   # popup was found and dismissed
        ctrl.page.wait_for_timeout = AsyncMock()
        llm = _make_llm([[_make_mapping("f1")]])
        log = _make_log()

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=[]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(return_value=None),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(ctrl, profile, job, llm, ApplyMode.AUTOFILL, log)

        assert result.status == ApplyStatus.AUTOFILL_COMPLETE
        # dismiss_popups called once, and a timeout wait was inserted after
        ctrl.dismiss_popups.assert_called_once()
        ctrl.page.wait_for_timeout.assert_called_once_with(500)

    @pytest.mark.asyncio
    async def test_no_popup_no_wait(self, profile, job):
        """When dismiss_popups returns False, no wait is inserted."""
        ctrl = _make_ctrl([[_make_field("f1")]])
        ctrl.dismiss_popups = AsyncMock(return_value=False)
        ctrl.page.wait_for_timeout = AsyncMock()
        llm = _make_llm([[_make_mapping("f1")]])
        log = _make_log()

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=[]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(return_value=None),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(ctrl, profile, job, llm, ApplyMode.AUTOFILL, log)

        assert result.status == ApplyStatus.AUTOFILL_COMPLETE
        ctrl.page.wait_for_timeout.assert_not_called()


class TestEmptyLlmMappings:
    """LLM returns empty mapping list — all fields are skipped but loop continues."""

    @pytest.mark.asyncio
    async def test_empty_mappings_autofill(self, profile, job):
        """Empty LLM response → 0 filled, 0 skipped from mappings, loop completes."""
        ctrl = _make_ctrl([[_make_field("f1"), _make_field("f2")]])
        llm = _make_llm([[]])   # LLM returns empty list
        log = _make_log()

        with (
            patch(
                "backend.auto_apply.adapters.generic.check_page_errors",
                AsyncMock(return_value=[]),
            ),
            patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                AsyncMock(return_value=None),
            ),
            patch("backend.auto_apply.answer_bank.get_answer_bank", return_value=MagicMock(all_snippets=lambda: {})),
        ):
            result = await GenericAdapter().apply(ctrl, profile, job, llm, ApplyMode.AUTOFILL, log)

        # Should still return AUTOFILL_COMPLETE (empty mappings is not an error)
        assert result.status == ApplyStatus.AUTOFILL_COMPLETE
        assert result.fields_filled == 0
