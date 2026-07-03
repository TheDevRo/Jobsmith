"""
tests/auto_apply/test_pre_apply_button_phases.py

Regression tests for the three-phase _click_pre_apply_button fix (2026-03-28).

Bug: _click_pre_apply_button only used Playwright :has-text() CSS selectors.
     On job boards like dejobs.org the Apply button has only Tailwind utility
     classes (e.g. "w-[250px] rounded-md bg-button ...") and no "apply" keyword
     in its class list or href.  Playwright's :has-text() also requires the
     element to pass a visibility check before scanning text, so buttons below
     the fold (or with unusual geometry) were silently skipped.

Fix: _click_pre_apply_button now searches in three phases:
     Phase 1 — CSS selectors (_PRE_APPLY_SELECTORS)           [unchanged]
     Phase 2 — text-content scan of all a[href] and button    [new]
               elements; scroll-into-view before click
     Phase 3 — JavaScript DOM evaluation via page.evaluate()  [new]
               uses offsetParent visibility (bypasses Playwright
               geometry checks)

Scope: GenericAdapter — the universal fallback adapter.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.auto_apply.adapters.generic import _click_pre_apply_button


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_page(
    *,
    css_visible: bool = False,
    text_scan_elements: list[dict] | None = None,
    js_eval_result=None,
) -> MagicMock:
    """
    Build a minimal Playwright Page mock that exercises the three phases:

    css_visible          — whether .locator(sel).first.is_visible() returns True
    text_scan_elements   — list of dicts with 'visible', 'text', 'scrollable'
                           used to build the page.locator("a[href], button").all() list
    js_eval_result       — return value of page.evaluate() (str → clicked, None → not found)
    """
    page = MagicMock()

    # ── Phase 1 mock: per-selector CSS locator ──────────────────────────
    css_loc = MagicMock()
    css_loc.first = MagicMock()
    css_loc.first.is_visible = AsyncMock(return_value=css_visible)
    css_loc.first.click = AsyncMock()

    # ── Phase 2 mock: bulk locator for text scan ─────────────────────────
    element_mocks = []
    for spec in (text_scan_elements or []):
        el = MagicMock()
        el.is_visible = AsyncMock(return_value=spec.get("visible", True))
        el.text_content = AsyncMock(return_value=spec.get("text", ""))
        el.scroll_into_view_if_needed = AsyncMock(
            side_effect=None if spec.get("scrollable", True) else Exception("no scroll")
        )
        el.click = AsyncMock()
        element_mocks.append(el)

    bulk_loc = MagicMock()
    bulk_loc.all = AsyncMock(return_value=element_mocks)

    # page.locator() returns css_loc for specific selectors OR bulk_loc
    # for the "a[href], button" scan query.
    def _locator_dispatch(selector):
        if selector == "a[href], button":
            return bulk_loc
        return css_loc

    page.locator = MagicMock(side_effect=_locator_dispatch)

    # ── Phase 3 mock: JavaScript evaluation ──────────────────────────────
    page.evaluate = AsyncMock(return_value=js_eval_result)

    return page, css_loc, element_mocks


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestClickPreApplyButtonPhases:
    """
    Regression: _click_pre_apply_button must try CSS → text scan → JS eval
    before returning False on job boards with unusual button markup.
    """

    @pytest.mark.asyncio
    async def test_phase1_css_selector_hits_returns_true(self):
        """Phase 1 CSS selector match: returns True immediately."""
        page, css_loc, _ = _make_page(css_visible=True)

        result = await _click_pre_apply_button(page)

        assert result is True, "Phase 1 CSS hit should return True"
        css_loc.first.click.assert_called_once()

    @pytest.mark.asyncio
    async def test_phase2_text_scan_apply_now_hits(self):
        """
        REGRESSION: Phase 2 text scan finds button with text 'Apply Now'
        even when CSS selector phase fails.
        """
        page, css_loc, elements = _make_page(
            css_visible=False,
            text_scan_elements=[
                {"visible": True, "text": "Apply Now", "scrollable": True},
            ],
            js_eval_result=None,
        )

        result = await _click_pre_apply_button(page)

        assert result is True, "Phase 2 text scan should find 'Apply Now' button"
        elements[0].click.assert_called_once()

    @pytest.mark.asyncio
    async def test_phase2_text_scan_apply_for_this_job_hits(self):
        """Phase 2 finds 'Apply for this job' variant."""
        page, _, elements = _make_page(
            css_visible=False,
            text_scan_elements=[
                {"visible": True, "text": "Apply for this job", "scrollable": True},
            ],
        )

        result = await _click_pre_apply_button(page)

        assert result is True
        elements[0].click.assert_called_once()

    @pytest.mark.asyncio
    async def test_phase2_invisible_element_skipped(self):
        """
        Phase 2 must skip invisible elements and fall through to Phase 3.
        """
        page, _, elements = _make_page(
            css_visible=False,
            text_scan_elements=[
                {"visible": False, "text": "Apply Now"},  # invisible — skip
            ],
            js_eval_result="apply now",  # Phase 3 succeeds
        )

        result = await _click_pre_apply_button(page)

        assert result is True, "Phase 3 JS eval should succeed after Phase 2 skip"
        elements[0].click.assert_not_called()  # Phase 2 element must NOT be clicked

    @pytest.mark.asyncio
    async def test_phase2_scroll_error_does_not_abort(self):
        """scroll_into_view_if_needed raising must not abort Phase 2."""
        page, _, elements = _make_page(
            css_visible=False,
            text_scan_elements=[
                {"visible": True, "text": "Apply Now", "scrollable": False},
            ],
        )

        result = await _click_pre_apply_button(page)

        # click should still be attempted even if scroll raised
        assert result is True
        elements[0].click.assert_called_once()

    @pytest.mark.asyncio
    async def test_phase3_js_eval_hits_tailwind_button(self):
        """
        REGRESSION: Phase 3 JS eval clicks button when CSS and text scan both
        fail — simulates dejobs.org Tailwind-only button (no 'apply' in class).
        """
        page, _, _ = _make_page(
            css_visible=False,
            text_scan_elements=[],  # no elements in text scan
            js_eval_result="apply now",  # JS evaluation found it
        )

        result = await _click_pre_apply_button(page)

        assert result is True, "Phase 3 JS eval should click Tailwind-only Apply button"
        page.evaluate.assert_called_once()

    @pytest.mark.asyncio
    async def test_phase3_js_eval_returns_none_means_not_found(self):
        """Phase 3 returning None means no Apply button — overall returns False."""
        page, _, _ = _make_page(
            css_visible=False,
            text_scan_elements=[],
            js_eval_result=None,
        )

        result = await _click_pre_apply_button(page)

        assert result is False, "None from JS eval means button not found"

    @pytest.mark.asyncio
    async def test_all_phases_fail_returns_false(self):
        """When all three phases fail, returns False without raising."""
        page, _, _ = _make_page(
            css_visible=False,
            text_scan_elements=[
                {"visible": True, "text": "View Details"},  # not an Apply button
                {"visible": True, "text": "Share"},
            ],
            js_eval_result=None,
        )

        result = await _click_pre_apply_button(page)

        assert result is False

    @pytest.mark.asyncio
    async def test_phase3_js_eval_exception_returns_false_not_raise(self):
        """page.evaluate() raising must not propagate — returns False."""
        page, _, _ = _make_page(css_visible=False, text_scan_elements=[])
        page.evaluate = AsyncMock(side_effect=Exception("JS context destroyed"))

        result = await _click_pre_apply_button(page)

        assert result is False, "Exception in JS eval must be swallowed"

    @pytest.mark.asyncio
    async def test_phase2_does_not_match_unrelated_text(self):
        """
        Buttons whose text does not contain an Apply keyword must not be clicked.
        """
        page, _, elements = _make_page(
            css_visible=False,
            text_scan_elements=[
                {"visible": True, "text": "Save Job"},
                {"visible": True, "text": "Learn More"},
                {"visible": True, "text": "View Company"},
            ],
            js_eval_result=None,
        )

        result = await _click_pre_apply_button(page)

        assert result is False
        for el in elements:
            el.click.assert_not_called()
