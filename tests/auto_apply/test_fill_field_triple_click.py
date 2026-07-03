"""
tests/auto_apply/test_fill_field_triple_click.py

Regression test for Phase 7 live validation (2026-03-28).

Bug: BrowserController.fill_field() called locator.triple_click() which does
     not exist in the Playwright Python API.  This caused every text-field fill
     to raise AttributeError and fall into the except branch, resulting in
     0 fields filled / all fields skipped across every adapter.

Fix: Replace locator.triple_click() with locator.click(click_count=3), the
     correct Playwright idiom for triple-clicking to select-all before typing.

Scope: BrowserController.fill_field is shared infrastructure; this bug affected
       every adapter (Generic, Greenhouse, Lever, LinkedIn, Workday).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_browser_controller_with_field(
    field_id: str = "field-0",
    selector: str = "input#name",
) -> MagicMock:
    """
    Return a BrowserController-like object whose _field_map maps field_id →
    selector, and whose page.locator() returns a controllable mock locator.
    """
    # Build mock locator
    locator_mock = MagicMock()
    locator_mock.scroll_into_view_if_needed = AsyncMock()
    locator_mock.click = AsyncMock()
    locator_mock.type = AsyncMock()

    # Simulate the .first attribute returning itself (Playwright pattern)
    locator_mock.first = locator_mock

    # Page mock
    page_mock = MagicMock()
    page_mock.locator = MagicMock(return_value=locator_mock)

    # BrowserController stub — only the parts fill_field uses
    ctrl = MagicMock()
    ctrl.page = page_mock
    ctrl._field_map = {field_id: selector}
    # fill_field now calls _human_delay for natural typing pacing
    ctrl._human_delay = AsyncMock()

    return ctrl, locator_mock


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestFillFieldTripleClick:
    """
    Regression: fill_field must use locator.click(click_count=3), not
    locator.triple_click() (which does not exist in Playwright Python).
    """

    @pytest.mark.asyncio
    async def test_fill_field_uses_click_count_3_not_triple_click(self):
        """
        REGRESSION: fill_field must call locator.click(click_count=3).
        It must never call locator.triple_click() — that attribute does not
        exist on Playwright Locator objects and raises AttributeError.
        """
        from backend.auto_apply.browser_controller import BrowserController

        ctrl, locator_mock = _make_browser_controller_with_field()

        # Temporarily bind the real fill_field to our stub ctrl
        # by calling it as an unbound method with ctrl as self
        result = await BrowserController.fill_field(ctrl, "field-0", "Jane Doe")

        assert result is True, "fill_field should return True on success"

        # Must have called click(click_count=3) — this is the correct Playwright
        # idiom and implicitly confirms triple_click() was not used (which would
        # have raised AttributeError before reaching this point)
        locator_mock.click.assert_called_once_with(click_count=3)

    @pytest.mark.asyncio
    async def test_fill_field_does_not_call_triple_click_attribute(self):
        """
        Explicit guard: verify that the real fill_field implementation does
        NOT reference .triple_click on the locator.  We do this by setting up
        a locator that raises AttributeError on triple_click and confirming
        fill_field succeeds anyway.
        """
        from backend.auto_apply.browser_controller import BrowserController

        ctrl, locator_mock = _make_browser_controller_with_field()

        # Make triple_click raise AttributeError (mirrors Playwright reality)
        def _raise(*args, **kwargs):
            raise AttributeError("'Locator' object has no attribute 'triple_click'")

        locator_mock.triple_click = _raise

        result = await BrowserController.fill_field(ctrl, "field-0", "Jane Doe")

        assert result is True, (
            "fill_field must succeed even when triple_click raises AttributeError — "
            "it should use click(click_count=3) instead"
        )

    @pytest.mark.asyncio
    async def test_fill_field_types_value_after_select_all(self):
        """
        After the select-all click, fill_field must call locator.type(value)
        so the existing field content is replaced by the new value.
        """
        from backend.auto_apply.browser_controller import BrowserController

        ctrl, locator_mock = _make_browser_controller_with_field()

        await BrowserController.fill_field(ctrl, "field-0", "test@example.com")

        # type() must be called with the target value
        locator_mock.type.assert_called_once()
        args, kwargs = locator_mock.type.call_args
        assert args[0] == "test@example.com", (
            f"fill_field must type the target value. Got: {args[0]!r}"
        )

    @pytest.mark.asyncio
    async def test_fill_field_returns_false_for_unknown_field_id(self):
        """fill_field must return False (not raise) for an unmapped field_id."""
        from backend.auto_apply.browser_controller import BrowserController

        ctrl, _ = _make_browser_controller_with_field()

        result = await BrowserController.fill_field(ctrl, "nonexistent-field", "value")

        assert result is False, (
            "fill_field must return False for unknown field_id, not raise"
        )
