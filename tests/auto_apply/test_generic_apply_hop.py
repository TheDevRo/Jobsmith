"""
tests/auto_apply/test_generic_apply_hop.py

Regression tests for the multi-hop Apply loop in GenericAdapter (2026-03-30).

Problem: Some employer career portals require multiple "Apply" button clicks
before reaching the actual application form.  The chain looks like:

    Job listing page        (0 fields, Apply button)
        ↓ click Apply
    Company portal page     (0-1 fields, another Apply button)
        ↓ click Apply
    Actual application form (2+ fields — real form)

With a single Apply click the adapter stopped at the portal page, saw
0-1 incidental fields (search box, cookie banner, email subscription) and
either returned NEEDS_REVIEW or tried to fill the wrong fields.

Fix: After each Apply click, count fillable (non-file) fields.
     If len(fillable) < _MIN_FORM_FIELDS (2), try clicking Apply again.
     Stop when a real form (≥ 2 fillable fields) is found, when no more
     Apply buttons exist, or after _MAX_APPLY_HOPS (3) clicks.

Scope: GenericAdapter — the universal fallback adapter.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.auto_apply.adapters.generic import (
    GenericAdapter,
    _MAX_APPLY_HOPS,
    _MIN_FORM_FIELDS,
)
from backend.auto_apply.models import ApplyMode, ApplyStatus, FieldDescriptor, FieldValue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DUMMY_JOB = MagicMock()
_DUMMY_JOB.job_id = "test-hop-001"
_DUMMY_JOB.url = "https://careers.example.com/jobs/engineer"
_DUMMY_JOB.title = "Engineer"
_DUMMY_JOB.company = "Acme"
_DUMMY_JOB.description = "Engineering role"
_DUMMY_JOB.resume_path = None
_DUMMY_JOB.cover_letter_path = None

_DUMMY_PROFILE = MagicMock()
_DUMMY_PROFILE.to_text.return_value = "profile text"


def _make_text_field(fid: str, label: str = "Field") -> FieldDescriptor:
    return FieldDescriptor(
        field_id=fid, field_type="text", label=label, name=fid,
        required=False, options=[], placeholder="", current_value="",
    )


def _real_form_fields() -> list[FieldDescriptor]:
    return [
        _make_text_field("first_name", "First Name"),
        _make_text_field("last_name", "Last Name"),
        _make_text_field("email", "Email"),
    ]


def _make_ctrl(snapshot_sequence: list) -> MagicMock:
    ctrl = MagicMock()
    ctrl.page = MagicMock()
    ctrl.page.url = _DUMMY_JOB.url
    ctrl.page.wait_for_load_state = AsyncMock()
    ctrl.page.wait_for_timeout = AsyncMock()
    ctrl.page.locator = MagicMock(
        return_value=MagicMock(all=AsyncMock(return_value=[]))
    )
    ctrl.page.evaluate = AsyncMock(return_value=None)
    ctrl.dismiss_popups = AsyncMock(return_value=False)
    ctrl.switch_to_new_page = AsyncMock(return_value="")

    it = iter(snapshot_sequence)
    ctrl.get_dom_snapshot = AsyncMock(side_effect=lambda: next(it, []))

    ctrl.fill_field = AsyncMock(return_value=True)
    ctrl.select_field = AsyncMock(return_value=True)
    ctrl.check_field = AsyncMock(return_value=True)
    ctrl.screenshot = AsyncMock(return_value="/tmp/shot.png")
    return ctrl


def _make_llm(field_values: list[FieldValue] | None = None) -> MagicMock:
    llm = MagicMock()
    if field_values is None:
        field_values = [
            FieldValue(field_id=f"field_{i}", value=f"val_{i}",
                       action="fill", source="profile", confidence=0.95)
            for i in range(3)
        ]
    llm.map_fields_to_values = AsyncMock(return_value=field_values)
    return llm


def _make_log() -> MagicMock:
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
# Constant sanity checks
# ---------------------------------------------------------------------------

def test_min_form_fields_constant():
    """_MIN_FORM_FIELDS must be ≥ 2 (enough to distinguish a real form)."""
    assert _MIN_FORM_FIELDS >= 2


def test_max_apply_hops_constant():
    """_MAX_APPLY_HOPS must be ≥ 2 (need at least 2 hops to handle portal pages)."""
    assert _MAX_APPLY_HOPS >= 2


# ---------------------------------------------------------------------------
# Multi-hop tests
# ---------------------------------------------------------------------------

class TestMultiHopApplyLoop:
    """
    Regression: GenericAdapter must keep clicking Apply until it finds a real
    form (≥ _MIN_FORM_FIELDS fillable fields) or runs out of Apply buttons.
    """

    @pytest.mark.asyncio
    async def test_two_hop_chain_reaches_real_form(self):
        """
        REGRESSION: Job listing → portal page (1 field) → real form (3 fields).

        Snapshot sequence (5 calls):
          1. Initial snapshot       → [] (blank SPA)
          2. After networkidle      → [] (job listing rendered, no form)
          3. After hop 1 Apply      → [search_box] (portal page, 1 field — not enough)
          4. After hop 2 Apply      → real_form_fields (3 fields — real form)
        """
        form_fields = _real_form_fields()
        portal_field = [_make_text_field("search", "Search")]  # 1 field — not a real form

        # Apply is clicked twice; second hop returns enough fields
        apply_side_effect = [True, True, False]  # hop1=True, hop2=True, (never reached)
        apply_iter = iter(apply_side_effect)

        ctrl = _make_ctrl([
            [],            # 1: initial blank
            [],            # 2: after networkidle (job listing, no form)
            portal_field,  # 3: after hop 1 click (portal page — 1 field)
            form_fields,   # 4: after hop 2 click (real form — 3 fields)
        ])

        with patch(
            "backend.auto_apply.adapters.generic._click_pre_apply_button",
            new=AsyncMock(side_effect=lambda page: next(apply_iter)),
        ), patch(
            "backend.auto_apply.adapters.generic._detect_generic_redirect",
            new=AsyncMock(return_value=""),
        ), patch(
            "backend.auto_apply.adapters.generic.check_page_errors",
            return_value=[],
        ), patch(
            "backend.auto_apply.adapters.generic._detect_next_button",
            new_callable=AsyncMock, return_value=None,
        ):
            log = _make_log()
            result = await GenericAdapter().apply(
                ctrl, _DUMMY_PROFILE, _DUMMY_JOB, _make_llm(), ApplyMode.AUTOFILL, log
            )

        assert result.status == ApplyStatus.AUTOFILL_COMPLETE, (
            f"Two-hop chain should reach AUTOFILL_COMPLETE. Got: {result.status}"
        )
        step_calls = [c.args[0] for c in log.step.call_args_list]
        assert "page_1_pre_apply_hunt" in step_calls, "hop 1 must fire"
        assert "page_1_apply_hop_2" in step_calls, "hop 2 must fire"

    @pytest.mark.asyncio
    async def test_single_hop_real_form_found_immediately(self):
        """
        If hop 1 lands on a real form (≥ _MIN_FORM_FIELDS), hop 2 must NOT fire.
        """
        form_fields = _real_form_fields()
        apply_call_count = []

        async def _counting_apply(page):
            apply_call_count.append(1)
            return True

        ctrl = _make_ctrl([
            [],          # 1: initial blank
            [],          # 2: after networkidle
            form_fields, # 3: after hop 1 click — real form immediately
        ])

        with patch(
            "backend.auto_apply.adapters.generic._click_pre_apply_button",
            new=_counting_apply,
        ), patch(
            "backend.auto_apply.adapters.generic._detect_generic_redirect",
            new=AsyncMock(return_value=""),
        ), patch(
            "backend.auto_apply.adapters.generic.check_page_errors",
            return_value=[],
        ), patch(
            "backend.auto_apply.adapters.generic._detect_next_button",
            new_callable=AsyncMock, return_value=None,
        ):
            result = await GenericAdapter().apply(
                ctrl, _DUMMY_PROFILE, _DUMMY_JOB, _make_llm(), ApplyMode.AUTOFILL, _make_log()
            )

        assert result.status == ApplyStatus.AUTOFILL_COMPLETE
        assert len(apply_call_count) == 1, (
            f"Apply must only be clicked once when hop 1 finds real form. "
            f"Called {len(apply_call_count)} times."
        )

    @pytest.mark.asyncio
    async def test_hop_stops_when_no_apply_button_found(self):
        """
        If no Apply button is found during a hop, loop must break and use
        the fields available from the previous snapshot (even if < _MIN_FORM_FIELDS).
        """
        single_field = [_make_text_field("email_alert", "Email for job alerts")]

        ctrl = _make_ctrl([
            [],           # 1: initial blank
            single_field, # 2: after networkidle (1 incidental field)
            # No hop 1 snapshot needed — apply_clicked=False breaks the loop
        ])

        with patch(
            "backend.auto_apply.adapters.generic._click_pre_apply_button",
            new=AsyncMock(return_value=False),  # no Apply button found
        ), patch(
            "backend.auto_apply.adapters.generic._detect_generic_redirect",
            new=AsyncMock(return_value=""),
        ):
            log = _make_log()
            result = await GenericAdapter().apply(
                ctrl, _DUMMY_PROFILE, _DUMMY_JOB, _make_llm([]), ApplyMode.AUTOFILL, log
            )

        # With no Apply button and only 1 incidental field, should reach AUTOFILL_COMPLETE
        # using that 1 field (LLM gets a shot at it)
        assert result.status in (ApplyStatus.AUTOFILL_COMPLETE, ApplyStatus.NEEDS_REVIEW), (
            "Should either fill what we have or return NEEDS_REVIEW — not crash"
        )
        step_calls = [c.args[0] for c in log.step.call_args_list]
        assert "page_1_apply_hop_2" not in step_calls, (
            "Hop 2 must not fire when hop 1 finds no Apply button"
        )

    @pytest.mark.asyncio
    async def test_max_hops_cap_prevents_infinite_loop(self):
        """
        REGRESSION: After _MAX_APPLY_HOPS clicks with no real form found,
        the loop must stop and not continue clicking indefinitely.
        """
        apply_call_count = []

        async def _always_finds_apply(page):
            apply_call_count.append(1)
            return True

        # Every snapshot returns 0-1 fields — never reaches _MIN_FORM_FIELDS
        tiny_field = [_make_text_field("x", "x")]
        snapshots = (
            [[]]       # 1: initial
            + [[]]     # 2: after networkidle
            + [tiny_field] * (_MAX_APPLY_HOPS + 2)  # all hops return 1 field
        )
        ctrl = _make_ctrl(snapshots)

        with patch(
            "backend.auto_apply.adapters.generic._click_pre_apply_button",
            new=_always_finds_apply,
        ), patch(
            "backend.auto_apply.adapters.generic._detect_generic_redirect",
            new=AsyncMock(return_value=""),
        ):
            result = await GenericAdapter().apply(
                ctrl, _DUMMY_PROFILE, _DUMMY_JOB, _make_llm([]), ApplyMode.AUTOFILL, _make_log()
            )

        assert len(apply_call_count) <= _MAX_APPLY_HOPS, (
            f"Apply must be clicked at most {_MAX_APPLY_HOPS} times. "
            f"Called {len(apply_call_count)} times."
        )

    @pytest.mark.asyncio
    async def test_known_ats_redirect_on_hop_2_hands_off(self):
        """
        If hop 2 detects a redirect to a known ATS, hand off to that adapter
        rather than continuing to hop.
        """
        greenhouse_url = "https://boards.greenhouse.io/acme/jobs/999"

        hop_count = []

        async def _apply_with_count(page):
            hop_count.append(1)
            return True

        # Hop 1: no redirect, 0 fields. Hop 2: greenhouse redirect.
        redirect_side_effect = ["", greenhouse_url]
        redirect_iter = iter(redirect_side_effect)

        ctrl = _make_ctrl([
            [],  # 1: initial blank
            [],  # 2: after networkidle
            [],  # 3: after hop 1 (no fields — keeps looping)
            # hop 2 returns immediately via redirect before snapshot
        ])

        from backend.auto_apply.adapters.greenhouse import GreenhouseAdapter
        greenhouse_calls = []

        async def _fake_greenhouse(self_inner, *args, **kwargs):
            greenhouse_calls.append(True)
            from backend.auto_apply.models import ApplyResult
            return ApplyResult(
                success=True, status=ApplyStatus.AUTOFILL_COMPLETE,
                message="Greenhouse: 5 fields filled.",
                adapter_used="greenhouse", fields_filled=5, fields_skipped=0,
                log_entries=[],
            )

        with patch(
            "backend.auto_apply.adapters.generic._click_pre_apply_button",
            new=_apply_with_count,
        ), patch(
            "backend.auto_apply.adapters.generic._detect_generic_redirect",
            new=AsyncMock(side_effect=lambda ctrl: next(redirect_iter)),
        ), patch.object(GreenhouseAdapter, "apply", _fake_greenhouse):
            result = await GenericAdapter().apply(
                ctrl, _DUMMY_PROFILE, _DUMMY_JOB, _make_llm(), ApplyMode.AUTOFILL, _make_log()
            )

        assert result.adapter_used == "greenhouse", (
            "Should hand off to GreenhouseAdapter when hop 2 detects redirect"
        )
        assert greenhouse_calls, "GreenhouseAdapter.apply() must have been called"
