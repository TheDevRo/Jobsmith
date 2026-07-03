"""
tests/auto_apply/test_generic_spa_render_wait.py

Regression tests for the SPA render-wait fix (2026-03-28).

Bug: GenericAdapter.apply() called get_dom_snapshot() immediately after
     BrowserController.navigate(), which uses wait_until="domcontentloaded".
     For React/Vue SPAs like dejobs.org, domcontentloaded fires as soon as the
     HTML skeleton is parsed — the JS bundle hasn't executed yet, so the page
     is a blank white canvas.  The DOM snapshot found 0 fields, and even the
     three-phase _click_pre_apply_button search found nothing because there
     were literally no DOM elements to iterate.

Fix: When page 1 returns 0 fields, GenericAdapter now:
     1. Fires "page_1_wait_for_render" and awaits wait_for_load_state("networkidle",
        timeout=8_000) — giving the JS bundle time to hydrate.
     2. Re-snapshots the DOM.  If fields are now present, it proceeds normally
        (short-circuits: no pre-apply hunt needed).
     3. Only if still no fields does it fire the pre-apply hunt.

     The timeout is intentionally short (8 s) so LinkedIn-style sites with
     persistent WebSocket connections don't hang here.

Scope: GenericAdapter — the universal fallback adapter.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.auto_apply.adapters.generic import GenericAdapter
from backend.auto_apply.models import ApplyMode, ApplyStatus, FieldDescriptor, FieldValue


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_DUMMY_JOB = MagicMock()
_DUMMY_JOB.job_id = "test-spa-001"
_DUMMY_JOB.url = "https://example.dejobs.org/jobs/engineer/ABC123/job/"
_DUMMY_JOB.title = "Engineer"
_DUMMY_JOB.company = "Acme"
_DUMMY_JOB.description = "Test job"
_DUMMY_JOB.resume_path = None
_DUMMY_JOB.cover_letter_path = None

_DUMMY_PROFILE = MagicMock()
_DUMMY_PROFILE.to_text.return_value = "profile text"

_TEXT_FIELD = FieldDescriptor(
    field_id="full_name",
    field_type="text",
    label="Full Name",
    name="full_name",
)


def _make_ctrl(
    *,
    snapshot_sequence: list,
    networkidle_raises: bool = False,
):
    """
    Build a BrowserController mock that returns snapshot_sequence values
    on successive calls to get_dom_snapshot().

    snapshot_sequence: list of lists-of-FieldDescriptor (one entry per call).
    networkidle_raises: whether wait_for_load_state raises (simulates timeout).
    """
    ctrl = MagicMock()
    ctrl.page = MagicMock()
    ctrl.page.url = _DUMMY_JOB.url

    # Successive DOM snapshots
    ctrl.get_dom_snapshot = AsyncMock(side_effect=snapshot_sequence)
    ctrl.dismiss_popups = AsyncMock(return_value=False)
    ctrl.page.wait_for_timeout = AsyncMock()

    if networkidle_raises:
        ctrl.page.wait_for_load_state = AsyncMock(
            side_effect=Exception("networkidle timeout")
        )
    else:
        ctrl.page.wait_for_load_state = AsyncMock()

    ctrl.fill_field = AsyncMock(return_value=True)
    ctrl.select_field = AsyncMock(return_value=True)
    ctrl.check_field = AsyncMock(return_value=True)
    ctrl.screenshot = AsyncMock(return_value="/tmp/shot.png")
    ctrl.switch_to_new_page = AsyncMock(return_value="")

    return ctrl


def _make_llm(field_values: list[FieldValue] | None = None):
    llm = MagicMock()
    if field_values is None:
        field_values = [
            FieldValue(
                field_id="full_name",
                value="Jane Smith",
                action="fill",
                source="profile",
                confidence=0.95,
            )
        ]
    llm.map_fields_to_values = AsyncMock(return_value=field_values)
    return llm


def _make_log():
    log = MagicMock()
    log.entries = []
    log.step = MagicMock()
    log.warning = MagicMock()
    log.field = MagicMock()
    log.llm_call = MagicMock()
    log.result = MagicMock()
    log.adapter_chosen = MagicMock()
    return log


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSpaRenderWait:
    """
    Regression: GenericAdapter must wait for networkidle before pre-apply hunt
    so that JS-rendered pages (React/Vue SPAs) have time to paint their DOM.
    """

    @pytest.mark.asyncio
    async def test_wait_for_render_step_fires_on_empty_page1(self):
        """
        REGRESSION: when page 1 returns 0 fields, 'page_1_wait_for_render'
        fires, then 'page_1_pre_apply_hunt' always fires (even if the
        re-snapshot found incidental fields), but no click occurs because
        no Apply button is present.  Fields from the re-snapshot are used.
        """
        with patch("backend.auto_apply.adapters.generic.check_page_errors", return_value=[]):
            ctrl = _make_ctrl(
                snapshot_sequence=[
                    [],              # first snapshot: blank SPA
                    [_TEXT_FIELD],   # after networkidle: fields appear
                    # (no more snapshots needed — apply_clicked=False)
                ],
            )
            with patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                new_callable=AsyncMock, return_value=None,
            ):
                log = _make_log()
                result = await GenericAdapter().apply(
                    ctrl, _DUMMY_PROFILE, _DUMMY_JOB, _make_llm(), ApplyMode.AUTOFILL, log
                )

        step_calls = [c.args[0] for c in log.step.call_args_list]
        assert "page_1_wait_for_render" in step_calls, (
            "page_1_wait_for_render must fire when page 1 is initially empty"
        )
        # Pre-apply hunt always fires on page 1 blank start, even when
        # re-snapshot found incidental fields (handles job desc pages with
        # search boxes / cookie banners that shouldn't be treated as a form).
        assert "page_1_pre_apply_hunt" in step_calls, (
            "page_1_pre_apply_hunt must always fire when page 1 initial snapshot was empty"
        )
        # No Apply button in mock → should NOT have clicked anything
        assert "page_1_pre_apply_clicked" not in step_calls, (
            "page_1_pre_apply_clicked must only appear when a button is actually clicked"
        )

    @pytest.mark.asyncio
    async def test_fields_appear_after_networkidle_autofill_complete(self):
        """
        When the SPA renders fields during the networkidle wait,
        the adapter must fill them and return AUTOFILL_COMPLETE.
        """
        with patch("backend.auto_apply.adapters.generic.check_page_errors", return_value=[]):
            ctrl = _make_ctrl(
                snapshot_sequence=[[], [_TEXT_FIELD]],
            )
            with patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                new_callable=AsyncMock, return_value=None,
            ):
                result = await GenericAdapter().apply(
                    ctrl, _DUMMY_PROFILE, _DUMMY_JOB,
                    _make_llm(), ApplyMode.AUTOFILL, _make_log()
                )

        assert result.status == ApplyStatus.AUTOFILL_COMPLETE
        assert result.fields_filled == 1

    @pytest.mark.asyncio
    async def test_networkidle_timeout_does_not_abort(self):
        """
        REGRESSION: if wait_for_load_state("networkidle") times out,
        the adapter must NOT raise — it proceeds with whatever is in the DOM.
        """
        with patch("backend.auto_apply.adapters.generic.check_page_errors", return_value=[]):
            ctrl = _make_ctrl(
                snapshot_sequence=[
                    [],             # blank page
                    [_TEXT_FIELD],  # re-snapshot still finds fields (timeout didn't block)
                ],
                networkidle_raises=True,  # networkidle times out
            )
            with patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                new_callable=AsyncMock, return_value=None,
            ):
                result = await GenericAdapter().apply(
                    ctrl, _DUMMY_PROFILE, _DUMMY_JOB,
                    _make_llm(), ApplyMode.AUTOFILL, _make_log()
                )

        # Despite the timeout, it should still proceed and fill the field
        assert result.status == ApplyStatus.AUTOFILL_COMPLETE

    @pytest.mark.asyncio
    async def test_pre_apply_hunt_fires_after_render_wait_if_still_no_fields(self):
        """
        If the page is a job description page (not a blank SPA), the re-snapshot
        after networkidle wait still finds 0 fields, and then page_1_pre_apply_hunt
        must fire.
        """
        with patch("backend.auto_apply.adapters.generic.check_page_errors", return_value=[]):
            with patch(
                "backend.auto_apply.adapters.generic._click_pre_apply_button",
                new_callable=AsyncMock, return_value=False,
            ) as mock_hunt:
                ctrl = _make_ctrl(
                    snapshot_sequence=[[], []],  # both snapshots empty (job desc page)
                )
                log = _make_log()
                result = await GenericAdapter().apply(
                    ctrl, _DUMMY_PROFILE, _DUMMY_JOB,
                    _make_llm(), ApplyMode.AUTOFILL, log
                )

        step_calls = [c.args[0] for c in log.step.call_args_list]
        assert "page_1_wait_for_render" in step_calls
        assert "page_1_pre_apply_hunt" in step_calls
        mock_hunt.assert_called_once()
        assert result.status == ApplyStatus.NEEDS_REVIEW

    @pytest.mark.asyncio
    async def test_render_wait_only_on_page_1(self):
        """
        The networkidle render-wait must only trigger on page 1.
        On page 2+, 0 fields means we've hit the end of the form — proceed
        to submit detection without any extra wait.
        """
        with patch("backend.auto_apply.adapters.generic.check_page_errors", return_value=[]):
            with patch(
                "backend.auto_apply.adapters.generic._detect_next_button",
                new_callable=AsyncMock, return_value=None,
            ):
                # Page 1 has fields; page 2 is empty (end of form)
                ctrl = _make_ctrl(
                    snapshot_sequence=[[_TEXT_FIELD], []],
                )
                # Navigate page 1 → next_button click → page 2
                next_btn_mock = MagicMock()
                next_btn_mock.click = AsyncMock()
                call_count = 0

                async def _next_btn_side_effect(page):
                    nonlocal call_count
                    call_count += 1
                    return next_btn_mock if call_count == 1 else None

                with patch(
                    "backend.auto_apply.adapters.generic._detect_next_button",
                    side_effect=_next_btn_side_effect,
                ):
                    log = _make_log()
                    result = await GenericAdapter().apply(
                        ctrl, _DUMMY_PROFILE, _DUMMY_JOB,
                        _make_llm(), ApplyMode.AUTOFILL, log
                    )

        # page_1_wait_for_render must NOT appear — page 1 had fields immediately
        step_calls = [c.args[0] for c in log.step.call_args_list]
        assert "page_1_wait_for_render" not in step_calls, (
            "render-wait must be skipped when page 1 finds fields on first snapshot"
        )
