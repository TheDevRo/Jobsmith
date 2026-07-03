"""
auto_apply/adapters/linkedin.py — LinkedIn Easy Apply adapter.

LinkedIn Easy Apply is a multi-step modal wizard injected over the job listing.
The wizard varies by company configuration but generally has these steps:
  1. Contact info (pre-filled from LinkedIn profile)
  2. Resume selection / upload
  3. Screening questions (Yes/No, dropdowns, short text)
  4. Additional questions (salary, start date, custom)
  5. Review + Submit

Button / navigation strategy (informed by GodsScion/Auto_job_applier_linkedIn):
  - Easy Apply button: filter by aria-label containing "Easy Apply" so we never
    accidentally click the external "Apply" button.  Multiple fallback selectors.
  - Scroll into view + wait for enabled before every button click.
  - Navigation (Next / Review / Submit): aria-label first, span-text second.
    Span-text matching (GodsScion's approach) survives LinkedIn DOM class renames.

Field-filling strategy:
  1. LinkedIn-specific DOM queries on the modal ([data-test-form-element] pattern)
  2. Per-type handlers: select, radio, text, textarea, checkbox
  3. Comprehensive heuristic label→value map (~25+ field types)
  4. LLM batch call ONLY for fields the heuristic could not match
  5. Autocomplete handling for city/location (ArrowDown + Enter)

NOTE: Requires an active LinkedIn session (logged-in browser context with
storage_state).  The orchestrator is responsible for passing storage_state_path.
"""

from __future__ import annotations

import logging
import random
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import ApplyMode, ApplyResult, ApplyStatus, FieldDescriptor
from ..utils.browser_helpers import check_page_errors, wait_if_paused
from ...paths import project_root

if TYPE_CHECKING:
    from ..browser_controller import BrowserController
    from ..llm_client import LLMClient
    from ..logger import AutoApplyLogger
    from ..models import JobApplicationRequest, UserProfile

logger = logging.getLogger(__name__)

_LINKEDIN_HOST = "linkedin.com"
_MAX_STEPS = 15  # Safety limit — LinkedIn forms rarely exceed 8 steps

# ---------------------------------------------------------------------------
# Easy Apply button — ordered from most-specific to most-generic.
# We MUST land on an Easy Apply button, not the regular "Apply" button.
# GodsScion filters by aria-label containing "Easy"; we mirror that.
# ---------------------------------------------------------------------------
_EASY_APPLY_SELECTORS = [
    "button[aria-label*='Easy Apply']",           # full-page job detail view
    "a[aria-label*='Easy Apply']",                # compact card / feed view uses <a>
    "[aria-label*='Easy Apply']",                 # any element type — widest net
    "button[aria-label*='easy apply']",
    "a[aria-label*='easy apply']",
    ".jobs-apply-button",                         # class-only last resort
]

# ---------------------------------------------------------------------------
# Modal — try a few selectors in case LinkedIn renames the class
# ---------------------------------------------------------------------------
_MODAL_SELECTORS = [
    ".jobs-easy-apply-modal",
    "[data-test-modal-id='easy-apply-modal']",
    "div.artdeco-modal[role='dialog']",
    # Dynamic fallback: any visible dialog containing a form is the Easy Apply modal.
    # Catches LinkedIn DOM renames without code changes.
    "div[role='dialog']:has(form)",
    "div[role='dialog']:has(input)",
]

# ---------------------------------------------------------------------------
# Navigation buttons.
#
# Strategy (scoped to modal, GodsScion pattern):
#   1. aria-label CSS selector   — fastest, works when LinkedIn keeps the label
#   2. XPath normalize-space()   — matches "Next" regardless of whitespace in the span
#      GodsScion: `.//button[contains(span, "Next")]`
#      Playwright XPath equivalent: `.//button[.//span[normalize-space()='Next']]`
#
# All searches MUST be scoped to the modal element, not ctrl.page.
# LinkedIn's main navigation also contains buttons — page-level search finds the
# wrong element or wastes seconds burning through timeouts on the wrong scope.
#
# Note: get_by_role("button", name="Next") does NOT work — LinkedIn's accessible
# name is "Continue to next step", not "Next".  We removed that path entirely.
# ---------------------------------------------------------------------------
_NAV = {
    "next": {
        "aria": [
            "button[aria-label='Continue to next step']",
            "button[aria-label*='next step']",
            "button[aria-label*='Next step']",
        ],
        # XPath normalize-space strips the whitespace LinkedIn bakes into spans
        "xpath": [
            ".//button[.//span[normalize-space()='Next']]",
            ".//button[.//span[normalize-space()='Continue']]",
        ],
    },
    "review": {
        "aria": [
            "button[aria-label='Review your application']",
            "button[aria-label*='Review']",
        ],
        "xpath": [
            ".//button[.//span[normalize-space()='Review your application']]",
            ".//button[.//span[normalize-space()='Review']]",
        ],
    },
    "submit": {
        "aria": [
            "button[aria-label='Submit application']",
            "button[aria-label*='Submit']",
        ],
        "xpath": [
            ".//button[.//span[normalize-space()='Submit application']]",
            ".//button[.//span[normalize-space()='Submit']]",
        ],
    },
}

# Modal sub-selectors for field types (GodsScion patterns)
_LI_SELECT         = "select"
_LI_RADIO_FIELDSET = "fieldset[data-test-form-builder-radio-button-form-component='true']"
# Radio group title — try stable selectors first.
# The data-test attribute used previously does not exist in LinkedIn's current DOM.
# GodsScion uses the CSS class substring approach; <legend> is the HTML standard.
_LI_RADIO_TITLE_SELECTORS = [
    "legend",                                         # standard HTML — most stable
    "span[class*='fb-dash-form-element__label']",     # GodsScion's actual selector
    "span[class*='form-element__label']",             # common LinkedIn variant
    "[data-test-form-builder-radio-button-form-component__title]",  # last resort
]
_LI_TEXT_INPUTS    = "input[type='text'], input[type='number'], input[type='email'], input[type='tel']"
_LI_TEXTAREA       = "textarea"
_LI_CHECKBOX       = "input[type='checkbox']"
_LI_FILE_SELECTORS = ["input[name='file']", "input[type='file']"]


class LinkedInEasyApplyAdapter:
    name = "linkedin"

    def matches(self, url: str, page_text: str) -> bool:
        return _LINKEDIN_HOST in url

    async def apply(
        self,
        ctrl: "BrowserController",
        profile: "UserProfile",
        job: "JobApplicationRequest",
        llm: "LLMClient",
        mode: ApplyMode,
        log: "AutoApplyLogger",
    ) -> ApplyResult:
        await wait_if_paused()
        log.adapter_chosen("linkedin")
        filled  = 0
        skipped = 0

        # ── Open Easy Apply modal ──────────────────────────────────────────
        log.step("open_easy_apply")

        # ── Already-applied checks (GodsScion: two detection points) ─────────
        # Check 1 — footer badge on the job card
        already_badge = ctrl.page.locator(".job-card-container__footer-job-state")
        try:
            if await already_badge.count() > 0:
                badge_text = ((await already_badge.first.text_content()) or "").strip()
                if badge_text.lower() == "applied":
                    log.warning("Already applied (footer badge)")
                    return ApplyResult(
                        success=False,
                        status=ApplyStatus.ALREADY_APPLIED,
                        message="Already applied to this job (footer badge)",
                        manual_url=job.url,
                        adapter_used=self.name,
                    )
        except Exception:
            pass

        # Check 2 — presence of the application-link element means "Applied" top-card
        already_link = ctrl.page.locator(".jobs-s-apply__application-link")
        try:
            if await already_link.count() > 0:
                log.warning("Already applied (application link present)")
                return ApplyResult(
                    success=False,
                    status=ApplyStatus.ALREADY_APPLIED,
                    message="Already applied to this job (application link present)",
                    manual_url=job.url,
                    adapter_used=self.name,
                )
        except Exception:
            pass

        btn_sel = await _find_easy_apply_button(ctrl)
        if not btn_sel:
            log.warning("Easy Apply button not found — may not be an Easy Apply listing")
            screenshot = await ctrl.screenshot(_screenshot_path(job.job_id, "no_button"))
            return ApplyResult(
                success=False,
                status=ApplyStatus.NEEDS_REVIEW,
                message="LinkedIn Easy Apply button not found — apply manually",
                screenshot_path=screenshot,
                manual_url=job.url,
                adapter_used=self.name,
            )

        # Scroll into view + click (GodsScion: always scroll before clicking)
        await _scroll_and_click(ctrl, btn_sel)

        # Let LinkedIn react; take diagnostic screenshot
        await ctrl.page.wait_for_timeout(2000)
        await ctrl.screenshot(_screenshot_path(job.job_id, "after_apply_click"))

        # ── Daily limit check (GodsScion: artdeco-inline-feedback__message) ──
        # LinkedIn shows this inline error when the daily Easy Apply quota is hit.
        try:
            limit_el = ctrl.page.locator(".artdeco-inline-feedback__message")
            if await limit_el.count() > 0:
                msg = ((await limit_el.first.text_content()) or "").lower()
                if "exceeded" in msg or "limit" in msg or "daily" in msg:
                    log.warning("LinkedIn daily Easy Apply limit reached")
                    return ApplyResult(
                        success=False,
                        status=ApplyStatus.RATE_LIMITED,
                        message="LinkedIn daily Easy Apply limit reached — try again tomorrow",
                        manual_url=job.url,
                        adapter_used=self.name,
                    )
        except Exception:
            pass

        # Handle interstitial dialogs that can appear before the modal
        if not await _find_modal(ctrl, timeout=1_000):
            _interstitial_selectors = [
                ".jobs-apply-header__confirm-dialog .artdeco-button--primary",
                ".artdeco-modal .artdeco-button--primary",
                "button[aria-label='Continue']",
                "button[aria-label*='Continue']",
                "button[aria-label*='continue']",
            ]
            for sel in _interstitial_selectors:
                if await ctrl.wait_for_selector(sel, timeout=1_500):
                    await _scroll_and_click(ctrl, sel)
                    logger.info("LinkedIn: dismissed interstitial via %r", sel)
                    await ctrl.page.wait_for_timeout(1000)
                    break

        if not await _find_modal(ctrl, timeout=4_000):
            # Check for external ATS redirect (LinkedIn opens new tab)
            new_url = await ctrl.switch_to_new_page(timeout=4_000)
            if new_url and "linkedin.com" not in new_url:
                from . import ALL_ADAPTERS
                ext_adapter = next(
                    (a for a in ALL_ADAPTERS if a.name != "linkedin" and a.matches(new_url, "")),
                    ALL_ADAPTERS[-1],
                )
                log.step("external_apply_redirect", page_url=new_url)
                logger.info(
                    "LinkedIn: external apply redirect → %s (adapter=%s)",
                    new_url[:80], ext_adapter.name,
                )
                return await ext_adapter.apply(ctrl, profile, job, llm, mode, log)

            screenshot = await ctrl.screenshot(_screenshot_path(job.job_id, "no_modal"))
            return ApplyResult(
                success=False,
                status=ApplyStatus.NEEDS_REVIEW,
                message="Easy Apply modal did not open and no external redirect detected",
                screenshot_path=screenshot,
                manual_url=job.url,
                adapter_used=self.name,
            )

        # ── Step loop ──────────────────────────────────────────────────────
        from ..answer_bank import get_answer_bank
        bank = get_answer_bank()
        bank_dict = {
            k: v for k, v in bank.all_snippets().items()
            if not (v.startswith("<") and v.endswith(">"))
        }

        # DOM version detection — log which structural markers are present.
        # Helps diagnose breakages when LinkedIn updates their DOM.
        try:
            dom_version = "unknown"
            if await ctrl.page.locator(".jobs-easy-apply-modal").count() > 0:
                dom_version = "artdeco-modal"
            elif await ctrl.page.locator("[data-test-modal-id]").count() > 0:
                dom_version = "data-test-modal"
            elif await ctrl.page.locator("div[role='dialog']").count() > 0:
                dom_version = "role-dialog-dynamic"
            logger.info("LinkedIn: detected DOM version %r", dom_version)
            log.step("dom_version_detected", page_url=dom_version)
        except Exception:
            pass

        resume_uploaded = False  # Upload only once per application (GodsScion pattern)
        missing_required: list[str] = []  # Required fields LLM couldn't fill confidently

        for step_num in range(1, _MAX_STEPS + 1):
            await wait_if_paused()
            log.step(f"wizard_step_{step_num}", page_url=await ctrl.current_url())

            # Detect navigation buttons — aria-label first, span-text fallback
            has_submit = await _find_nav_button(ctrl, "submit")
            has_review = await _find_nav_button(ctrl, "review") if not has_submit else False
            has_next   = await _find_nav_button(ctrl, "next")   if not has_submit and not has_review else False

            # Fill fields on this step — with error recovery: take screenshot and
            # return NEEDS_REVIEW if an unrecoverable exception escapes the filler.
            try:
                step_filled, step_skipped, step_missing = await _fill_step_native(
                    ctrl, log, profile, job, llm, bank_dict
                )
            except Exception as step_exc:
                logger.error("LinkedIn: _fill_step_native raised on step %d: %s", step_num, step_exc)
                screenshot = await ctrl.screenshot(_screenshot_path(job.job_id, f"error_step{step_num}"))
                log.result(False, "needs_review", filled, skipped, tier=1, page_count=step_num, screenshot_path=screenshot)
                return ApplyResult(
                    success=False,
                    status=ApplyStatus.NEEDS_REVIEW,
                    message=f"LinkedIn adapter error on step {step_num}: {step_exc}",
                    screenshot_path=screenshot,
                    manual_url=job.url,
                    adapter_used=self.name,
                    fields_filled=filled,
                    fields_skipped=skipped,
                    log_entries=log.entries,
                )
            filled  += step_filled
            skipped += step_skipped
            missing_required.extend(step_missing)

            # Resume upload — once per application
            if not resume_uploaded and job.resume_path and Path(job.resume_path).exists():
                if await _upload_resume(ctrl, log, job.resume_path):
                    filled += 1
                    resume_uploaded = True
                    await ctrl.page.wait_for_timeout(1_500)  # allow upload processing

            # Allow LinkedIn's async field-validation JS to run before we try
            # to click Next.  Without this, the button can be visible but still
            # disabled (grayed out) immediately after the last input event fires.
            await ctrl.page.wait_for_timeout(800)

            # Check for inline validation errors before advancing or submitting.
            page_errors = await check_page_errors(ctrl.page)
            if page_errors:
                for err_msg in page_errors:
                    log.warning(f"Inline validation error on step {step_num}: {err_msg}")
                screenshot = await ctrl.screenshot(
                    _screenshot_path(job.job_id, f"errors_step{step_num}")
                )
                log.result(False, "needs_review", filled, skipped, tier=1, page_count=step_num, screenshot_path=screenshot)
                return ApplyResult(
                    success=False,
                    status=ApplyStatus.NEEDS_REVIEW,
                    message=(
                        f"Validation errors on step {step_num}: "
                        + "; ".join(page_errors[:3])
                    ),
                    screenshot_path=screenshot,
                    manual_url=job.url,
                    adapter_used=self.name,
                    fields_filled=filled,
                    fields_skipped=skipped,
                    log_entries=log.entries,
                )

            # Navigate forward
            if has_submit:
                # Flag missing required fields before handing off or submitting
                if missing_required:
                    log.warning(
                        f"Required fields not filled confidently: {', '.join(missing_required[:5])}"
                    )

                if mode == ApplyMode.AUTOFILL:
                    msg = "LinkedIn Easy Apply filled to final step. Submit manually."
                    if missing_required:
                        msg += f" WARNING: {len(missing_required)} required field(s) may be empty: " + ", ".join(missing_required[:3])
                    log.result(True, "autofill_complete", filled, skipped, tier=1, page_count=step_num)
                    return ApplyResult(
                        success=True,
                        status=ApplyStatus.AUTOFILL_COMPLETE,
                        message=msg,
                        adapter_used=self.name,
                        fields_filled=filled,
                        fields_skipped=skipped,
                        log_entries=log.entries,
                    )
                log.step("submit")
                await _click_nav_button(ctrl, "submit")
                try:
                    await ctrl.page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                screenshot = await ctrl.screenshot(_screenshot_path(job.job_id, "submitted"))
                confirmed = await _check_submit_confirmation(ctrl.page)
                if not confirmed:
                    log.warning("Submit confirmation not detected — flagging as NEEDS_REVIEW")
                    log.result(False, "needs_review", filled, skipped, tier=1, page_count=step_num, screenshot_path=screenshot)
                    return ApplyResult(
                        success=False,
                        status=ApplyStatus.NEEDS_REVIEW,
                        message=(
                            f"Submit clicked but confirmation not detected: "
                            f"{job.title} at {job.company}"
                        ),
                        screenshot_path=screenshot,
                        manual_url=job.url,
                        adapter_used=self.name,
                        fields_filled=filled,
                        fields_skipped=skipped,
                        log_entries=log.entries,
                    )
                log.result(True, "submitted", filled, skipped, tier=1, page_count=step_num, screenshot_path=screenshot)
                return ApplyResult(
                    success=True,
                    status=ApplyStatus.SUBMITTED,
                    message=f"Application submitted: {job.title} at {job.company}",
                    screenshot_path=screenshot,
                    adapter_used=self.name,
                    fields_filled=filled,
                    fields_skipped=skipped,
                    log_entries=log.entries,
                )

            elif has_review:
                await _click_nav_button(ctrl, "review")
                await ctrl.page.wait_for_timeout(1500)

            elif has_next:
                clicked = await _click_nav_button(ctrl, "next")
                if not clicked:
                    # Click was intercepted (e.g. LinkedIn's "About Company" photo
                    # overlay — GodsScion catches ElementClickInterceptedException here).
                    # Treat as wizard end and fall through to the post-loop Review attempt.
                    log.warning(
                        f"Next click intercepted on step {step_num} — treating as wizard end"
                    )
                    break
                await ctrl.page.wait_for_timeout(1500)

            else:
                log.warning(f"No navigation button found on step {step_num}")
                break

        # ── Post-loop Review attempt (GodsScion safety net) ───────────────
        # If the loop exited early (break on intercept / no nav button), we may
        # still be mid-wizard.  Try reaching Review unconditionally before giving up.
        if await _find_nav_button(ctrl, "review"):
            log.step("post_loop_review_attempt")
            await _click_nav_button(ctrl, "review")
            await ctrl.page.wait_for_timeout(1500)
            # Re-check for Submit now that we're on the review screen
            if await _find_nav_button(ctrl, "submit"):
                if missing_required:
                    log.warning(f"Required fields may be empty: {', '.join(missing_required[:5])}")
                if mode == ApplyMode.AUTOFILL:
                    msg = "LinkedIn Easy Apply filled to final step. Submit manually."
                    if missing_required:
                        msg += f" WARNING: {len(missing_required)} required field(s) may be empty."
                    log.result(True, "autofill_complete", filled, skipped, tier=1, page_count=step_num)
                    return ApplyResult(
                        success=True,
                        status=ApplyStatus.AUTOFILL_COMPLETE,
                        message=msg,
                        adapter_used=self.name,
                        fields_filled=filled,
                        fields_skipped=skipped,
                        log_entries=log.entries,
                    )
                log.step("submit_post_loop")
                await _click_nav_button(ctrl, "submit")
                try:
                    await ctrl.page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                screenshot = await ctrl.screenshot(_screenshot_path(job.job_id, "submitted"))
                confirmed = await _check_submit_confirmation(ctrl.page)
                if not confirmed:
                    log.warning("Submit confirmation not detected (post-loop) — flagging as NEEDS_REVIEW")
                    log.result(False, "needs_review", filled, skipped, tier=1, page_count=step_num, screenshot_path=screenshot)
                    return ApplyResult(
                        success=False,
                        status=ApplyStatus.NEEDS_REVIEW,
                        message=(
                            f"Submit clicked but confirmation not detected: "
                            f"{job.title} at {job.company}"
                        ),
                        screenshot_path=screenshot,
                        manual_url=job.url,
                        adapter_used=self.name,
                        fields_filled=filled,
                        fields_skipped=skipped,
                        log_entries=log.entries,
                    )
                log.result(True, "submitted", filled, skipped, tier=1, page_count=step_num, screenshot_path=screenshot)
                return ApplyResult(
                    success=True,
                    status=ApplyStatus.SUBMITTED,
                    message=f"Application submitted: {job.title} at {job.company}",
                    screenshot_path=screenshot,
                    adapter_used=self.name,
                    fields_filled=filled,
                    fields_skipped=skipped,
                    log_entries=log.entries,
                )

        screenshot = await ctrl.screenshot(_screenshot_path(job.job_id, "step_limit"))
        log.result(False, "needs_review", filled, skipped, tier=1, page_count=step_num, screenshot_path=screenshot)
        return ApplyResult(
            success=False,
            status=ApplyStatus.NEEDS_REVIEW,
            message=f"Exceeded {_MAX_STEPS}-step limit — manual review required",
            screenshot_path=screenshot,
            manual_url=job.url,
            adapter_used=self.name,
            fields_filled=filled,
            fields_skipped=skipped,
            log_entries=log.entries,
        )


# ---------------------------------------------------------------------------
# Button / modal helpers
# ---------------------------------------------------------------------------

async def _find_easy_apply_button(ctrl: "BrowserController") -> str | None:
    """
    Find the Easy Apply button. Returns the first matching selector, or None.

    LinkedIn shows the button in two different layouts depending on how the job
    URL was opened:
      - Full-page /jobs/view/{id}  → <button aria-label*='Easy Apply'>
      - Compact card / feed view   → <a aria-label*='Easy Apply'>

    Strategy: do one combined wait (fast-path) for any Easy Apply element, then
    identify which specific selector matched.  This avoids the old approach of
    iterating selectors with multi-second timeouts each (which added 18+ s of
    delay when selectors were wrong).
    """
    # Single combined wait — fires as soon as ANY variant appears (up to 10 s).
    combined = ", ".join(_EASY_APPLY_SELECTORS)
    try:
        await ctrl.page.wait_for_selector(combined, state="visible", timeout=10_000)
    except Exception:
        return None  # nothing found within timeout

    # Identify which selector actually matched so callers can use it for clicking.
    for sel in _EASY_APPLY_SELECTORS:
        try:
            if await ctrl.page.locator(sel).first.is_visible():
                logger.info("LinkedIn: found Easy Apply button via %r", sel)
                return sel
        except Exception:
            continue
    return None


async def _scroll_and_click(ctrl: "BrowserController", selector: str) -> bool:
    """
    Scroll selector into view, wait for visible+enabled, click.

    Falls back to a JS click if Playwright's synthesised click is intercepted
    (LinkedIn sometimes puts invisible overlay elements over the button).
    After the JS fallback we explicitly wait for a network/DOM event so the
    page has time to react (a raw el.click() does not trigger Playwright waits).
    """
    try:
        locator = ctrl.page.locator(selector).first
        await locator.scroll_into_view_if_needed(timeout=5_000)
        await locator.wait_for(state="visible", timeout=5_000)
        # "enabled" state is only valid for form elements (button/input).
        # <a> tags don't support it — skip for non-button selectors.
        tag = await locator.evaluate("el => el.tagName.toLowerCase()")
        if tag == "button":
            await locator.wait_for(state="enabled", timeout=5_000)
        try:
            await locator.click(timeout=8_000)
        except Exception as click_err:
            logger.warning("_scroll_and_click regular click failed (%s), trying JS click", click_err)
            el = await locator.element_handle()
            if el:
                await ctrl.page.evaluate("el => el.click()", el)
                # JS click doesn't trigger Playwright's auto-wait — settle manually
                await ctrl.page.wait_for_timeout(500)
            else:
                raise
        return True
    except Exception as exc:
        logger.warning("_scroll_and_click %r failed: %s", selector, exc)
        return False


async def _find_modal(ctrl: "BrowserController", timeout: int = 4_000) -> bool:
    """Check whether the Easy Apply modal is visible using multiple fallback selectors."""
    combined = ", ".join(_MODAL_SELECTORS)
    try:
        await ctrl.page.wait_for_selector(combined, state="visible", timeout=timeout)
        return True
    except Exception:
        return False




async def _get_modal_locator(ctrl: "BrowserController"):
    """Return the first visible modal locator."""
    for sel in _MODAL_SELECTORS:
        try:
            # wait_for_selector actually waits; is_visible() does not
            await ctrl.page.wait_for_selector(sel, state="visible", timeout=1_000)
            return ctrl.page.locator(sel).first
        except Exception:
            continue
    # Fallback: return primary selector even if not confirmed visible
    return ctrl.page.locator(_MODAL_SELECTORS[0]).first


async def _find_nav_button(ctrl: "BrowserController", nav_key: str) -> bool:
    """
    Check whether a navigation button (next/review/submit) is visible.

    Scoped to the Easy Apply modal so we never match LinkedIn's main-nav buttons.

    Priority:
      1. aria-label CSS selectors — faster when LinkedIn preserves the attribute
      2. XPath normalize-space() — GodsScion's span-text approach, handles whitespace
         in spans (the old `button:has(span:text-is(...))` broke on whitespace and
         `get_by_role(name=...)` never matched because accessible name ≠ visible text)
    """
    config = _NAV[nav_key]
    modal  = await _get_modal_locator(ctrl)  # use whichever selector matched the real modal

    # 1. aria-label selectors — scoped to modal to avoid matching LinkedIn's main-nav
    for sel in config["aria"]:
        try:
            loc = modal.locator(sel).first
            await loc.wait_for(state="visible", timeout=300)
            return True
        except Exception:
            pass

    # 2. XPath normalize-space() scoped to modal (GodsScion pattern)
    for xp in config["xpath"]:
        try:
            loc = modal.locator(f"xpath={xp}").first
            await loc.wait_for(state="visible", timeout=300)
            return True
        except Exception:
            pass

    return False


async def _click_nav_button(ctrl: "BrowserController", nav_key: str) -> bool:
    """
    Click a navigation button, scoped to the modal, with enabled-state wait.

    Scoping to the modal prevents accidentally clicking LinkedIn's main-nav buttons.
    Waiting for `enabled` (not just `visible`) prevents clicking while LinkedIn's
    async form-validation still has the button disabled.
    """
    config = _NAV[nav_key]
    modal  = await _get_modal_locator(ctrl)  # use whichever selector matched the real modal

    # 1. aria-label selectors — scoped to modal
    for sel in config["aria"]:
        try:
            loc = modal.locator(sel).first
            await loc.wait_for(state="visible", timeout=1_000)
            await loc.wait_for(state="enabled", timeout=3_000)  # wait for validation
            await loc.scroll_into_view_if_needed(timeout=3_000)
            await loc.click(timeout=5_000)
            logger.info("LinkedIn: clicked %s nav via aria-label %r", nav_key, sel)
            return True
        except Exception:
            pass

    # 2. XPath normalize-space() scoped to modal
    for xp in config["xpath"]:
        try:
            loc = modal.locator(f"xpath={xp}").first
            await loc.wait_for(state="visible", timeout=1_000)
            await loc.wait_for(state="enabled", timeout=3_000)  # wait for validation
            await loc.scroll_into_view_if_needed(timeout=3_000)
            await loc.click(timeout=5_000)
            logger.info("LinkedIn: clicked %s nav via xpath %r", nav_key, xp)
            return True
        except Exception:
            pass

    logger.warning("LinkedIn: could not click nav button %r", nav_key)
    return False


# ---------------------------------------------------------------------------
# Native LinkedIn field filling (GodsScion approach)
# ---------------------------------------------------------------------------

async def _fill_step_native(ctrl, log, profile, job, llm, bank_dict) -> tuple[int, int, list[str]]:
    """
    Fill all form fields on the current wizard step.

    Returns (filled, skipped, missing_required) where missing_required is a list
    of labels for required fields that could not be filled confidently.

    Strategy:
      1. Select dropdowns  — heuristic → fuzzy option match → LLM queue
      2. Radio buttons     — heuristic → label text match → LLM queue
      3. Text inputs       — heuristic → LLM queue; city gets autocomplete handling
      4. Textareas         — profile summary / answer bank → LLM queue
      5. Checkboxes        — always check if unchecked (agreements/follow toggles)
      6. LLM batch         — fields not matched by heuristic
    """
    filled = skipped = 0
    missing_required: list[str] = []
    modal = await _get_modal_locator(ctrl)

    # Queues for LLM batch: (fid, element, ftype, label, options, is_required)
    llm_queue: list[tuple[str, object, str, str, list[str] | None, bool]] = []

    # ── 1. Select dropdowns ───────────────────────────────────────────────
    selects = await modal.locator(_LI_SELECT).all()
    for idx, sel_el in enumerate(selects):
        label = await _label_for(ctrl.page, sel_el)
        is_required = await _is_required(sel_el)
        # Skip phone-country-code dropdowns (GodsScion: keep default)
        if re.search(r"phone.*country|country.*code", label.lower()):
            continue

        options = await sel_el.locator("option").all_text_contents()
        options = [
            o.strip() for o in options
            if o.strip() and o.strip().lower() not in ("", "select an option", "please select")
        ]

        value = _heuristic_value(label, profile)
        if value is not None:
            chosen = _best_option(value, options)
            if chosen:
                try:
                    await sel_el.scroll_into_view_if_needed()
                    await sel_el.select_option(label=chosen)
                    filled += 1
                    log.field(label, chosen, action="select", source="heuristic")
                except Exception as e:
                    logger.debug("LinkedIn select failed for %r: %s", label, e)
                    if is_required:
                        missing_required.append(label)
                    skipped += 1
            else:
                if is_required:
                    missing_required.append(label)
                skipped += 1
        else:
            llm_queue.append((f"select_{idx}", sel_el, "select", label, options, is_required))

    # ── 2. Radio buttons ──────────────────────────────────────────────────
    radio_fieldsets = await modal.locator(_LI_RADIO_FIELDSET).all()
    for idx, fieldset in enumerate(radio_fieldsets):
        # Try each title selector in stability order (GodsScion fix: the old
        # data-test attribute does not exist in LinkedIn's current DOM)
        label = ""
        for title_sel in _LI_RADIO_TITLE_SELECTORS:
            try:
                title_el = fieldset.locator(title_sel)
                if await title_el.count() > 0:
                    label = ((await title_el.first.text_content()) or "").strip()
                    if label:
                        break
            except Exception:
                continue
        if not label:
            label = await _label_for(ctrl.page, fieldset)

        # Build option list using the for-id label relationship (GodsScion pattern)
        # This is more reliable than .all_text_contents() on label elements
        radio_inputs = await fieldset.locator("input[type='radio']").all()
        radio_pairs: list[tuple[object, str]] = []  # (input_locator, label_text)
        for radio_input in radio_inputs:
            r_id = await radio_input.get_attribute("id")
            r_text = ""
            if r_id:
                lbl = fieldset.locator(f"label[for='{r_id}']")
                if await lbl.count() > 0:
                    r_text = ((await lbl.first.text_content()) or "").strip()
            if not r_text:
                # Fallback: sibling label text
                r_text = await _label_for(ctrl.page, radio_input)
            if r_text:
                radio_pairs.append((radio_input, r_text))

        options = [t for _, t in radio_pairs]

        value = _heuristic_value(label, profile)
        if value is not None:
            best_text = _best_option(value, options)
            matched = False
            if best_text:
                for radio_input, r_text in radio_pairs:
                    if r_text == best_text:
                        try:
                            await radio_input.scroll_into_view_if_needed()
                            await radio_input.check()
                            filled += 1
                            log.field(label, r_text, action="click_radio", source="heuristic")
                            matched = True
                            break
                        except Exception as e:
                            logger.debug("LinkedIn radio click failed for %r: %s", label, e)
            if not matched:
                # EEO fallback — find a "decline / prefer not" option
                declined_input = await _fuzzy_decline_radio_inputs(radio_pairs)
                if declined_input:
                    try:
                        await declined_input.scroll_into_view_if_needed()
                        await declined_input.check()
                        filled += 1
                        log.field(label, "decline_fuzzy", action="click_radio", source="heuristic_fuzzy")
                    except Exception:
                        skipped += 1
                else:
                    skipped += 1
        else:
            is_req = await _is_required(fieldset)
            llm_queue.append((f"radio_{idx}", fieldset, "radio", label, options, is_req))

    # ── 3. Text inputs ────────────────────────────────────────────────────
    text_inputs = await modal.locator(_LI_TEXT_INPUTS).all()
    for idx, inp in enumerate(text_inputs):
        if not await inp.is_visible():
            continue
        label = await _label_for(ctrl.page, inp)
        is_required = await _is_required(inp)
        value = _heuristic_value(label, profile)
        is_city = bool(
            re.search(r"\b(city|town)\b", label.lower()) or
            (re.search(r"\blocation\b", label.lower()) and "job" not in label.lower())
        )

        if value is not None:
            try:
                await inp.scroll_into_view_if_needed()
                await inp.click(click_count=3)  # select-all before typing
                await inp.type(str(value), delay=random.randint(25, 95))
                if is_city:
                    # LinkedIn location autocomplete — press ArrowDown + Enter
                    # to select the first suggestion (GodsScion pattern)
                    await ctrl.page.wait_for_timeout(600)
                    await inp.press("ArrowDown")
                    await inp.press("Enter")
                filled += 1
                log.field(label, str(value), action="fill", source="heuristic")
            except Exception as e:
                logger.debug("LinkedIn text fill failed for %r: %s", label, e)
                if is_required:
                    missing_required.append(label)
                skipped += 1
        else:
            if is_required:
                # Required field — defer to LLM but track it
                llm_queue.append((f"text_{idx}", inp, "text", label, None, True))
            else:
                llm_queue.append((f"text_{idx}", inp, "text", label, None, False))

    # ── 4. Textareas ──────────────────────────────────────────────────────
    textareas = await modal.locator(_LI_TEXTAREA).all()
    for idx, ta in enumerate(textareas):
        if not await ta.is_visible():
            continue
        label = await _label_for(ctrl.page, ta)
        is_required = await _is_required(ta)
        l = label.lower()
        value = None
        if re.search(r"\bsummary\b", l) or re.search(r"\babout\s*(yourself|you)\b", l):
            value = profile.summary or None
        elif re.search(r"\bcover\s*letter\b", l):
            value = bank_dict.get("cover_letter") or bank_dict.get("cover") or None
        elif re.search(r"\bheadline\b", l):
            if profile.experience:
                value = f"{profile.experience[0].title} | {profile.experience[0].company}"

        if value:
            try:
                await ta.scroll_into_view_if_needed()
                await ta.click(click_count=3)  # select-all before typing
                await ta.type(str(value), delay=random.randint(25, 75))
                filled += 1
                log.field(
                    label,
                    str(value)[:60] + ("..." if len(str(value)) > 60 else ""),
                    action="fill", source="profile",
                )
            except Exception as e:
                logger.debug("LinkedIn textarea fill failed for %r: %s", label, e)
                if is_required:
                    missing_required.append(label)
                skipped += 1
        else:
            llm_queue.append((f"textarea_{idx}", ta, "textarea", label, None, is_required))

    # ── 5. Checkboxes ─────────────────────────────────────────────────────
    # LinkedIn checkboxes are agreements, follow toggles, etc. — always check.
    checkboxes = await modal.locator(_LI_CHECKBOX).all()
    for cb in checkboxes:
        try:
            if await cb.is_visible() and not await cb.is_checked():
                await cb.scroll_into_view_if_needed()
                await cb.check()
                filled += 1
        except Exception:
            pass

    # ── 6. LLM fallback for unmatched fields ──────────────────────────────
    if llm_queue:
        f, s, llm_missing = await _llm_fill_queue(ctrl, log, profile, job, llm, bank_dict, llm_queue)
        filled  += f
        skipped += s
        missing_required.extend(llm_missing)

    return filled, skipped, missing_required


_CONF_REQUIRED_MIN = 0.50  # Below this confidence, required fields are flagged as missing


async def _llm_fill_queue(ctrl, log, profile, job, llm, bank_dict, queue) -> tuple[int, int, list[str]]:
    """Send unmatched fields to the LLM in a single batch and apply results.

    Returns (filled, skipped, missing_required) where missing_required is a list
    of labels for required fields the LLM could not fill with confidence >= 0.50.
    """
    filled = skipped = 0
    missing_required: list[str] = []

    descriptors = []
    handle_map: dict[str, tuple] = {}

    for fid, el, ftype, label, options, is_required in queue:
        desc = FieldDescriptor(
            field_id=fid,
            label=label,
            field_type=ftype if ftype in ("text", "textarea", "select", "radio") else "text",
            options=options,
        )
        descriptors.append(desc)
        handle_map[fid] = (el, ftype, label, is_required)

    try:
        mappings = await llm.map_fields_to_values(profile, job, descriptors, bank_dict)
    except Exception as e:
        logger.warning("LinkedIn LLM batch failed: %s", e)
        # All required fields in the queue are now missing
        for _, _, label, is_req in handle_map.values():
            if is_req:
                missing_required.append(label)
        return 0, len(queue), missing_required

    for fv in mappings:
        item = handle_map.get(fv.field_id)
        if not item:
            skipped += 1
            continue
        el, ftype, label, is_required = item

        if fv.action == "skip" or not fv.value:
            skipped += 1
            if is_required:
                missing_required.append(label)
            continue

        # Flag required fields the LLM couldn't map confidently
        if is_required and fv.confidence < _CONF_REQUIRED_MIN:
            logger.warning(
                "LinkedIn: required field %r has confidence %.2f < %.2f — flagging",
                label, fv.confidence, _CONF_REQUIRED_MIN,
            )
            missing_required.append(label)

        try:
            if ftype == "select":
                options = await el.locator("option").all_text_contents()
                chosen = _best_option(fv.value, [o.strip() for o in options])
                if chosen:
                    await el.scroll_into_view_if_needed()
                    await el.select_option(label=chosen)
                    filled += 1
                    log.field(fv.field_id, chosen, action="select",
                              source=fv.source, confidence=fv.confidence)
                else:
                    skipped += 1
            elif ftype == "radio":
                # Rebuild for-id pairs from the fieldset (same approach as heuristic path)
                radio_inputs = await el.locator("input[type='radio']").all()
                llm_pairs: list[tuple[object, str]] = []
                for radio_input in radio_inputs:
                    r_id = await radio_input.get_attribute("id")
                    r_text = ""
                    if r_id:
                        lbl = el.locator(f"label[for='{r_id}']")
                        if await lbl.count() > 0:
                            r_text = ((await lbl.first.text_content()) or "").strip()
                    if r_text:
                        llm_pairs.append((radio_input, r_text))

                llm_opts = [t for _, t in llm_pairs]
                best_text = _best_option(fv.value, llm_opts)
                matched = False
                if best_text:
                    for radio_input, r_text in llm_pairs:
                        if r_text == best_text:
                            await radio_input.scroll_into_view_if_needed()
                            await radio_input.check()
                            filled += 1
                            log.field(fv.field_id, r_text, action="click_radio",
                                      source=fv.source, confidence=fv.confidence)
                            matched = True
                            break
                if not matched:
                    skipped += 1
            else:
                await el.scroll_into_view_if_needed()
                await el.click(click_count=3)  # select-all before typing
                await el.type(fv.value, delay=random.randint(25, 95))
                filled += 1
                log.field(fv.field_id, fv.value, action="fill",
                          source=fv.source, confidence=fv.confidence)
        except Exception as e:
            logger.debug("LinkedIn LLM field apply failed for %r: %s", fv.field_id, e)
            if is_required:
                missing_required.append(label)
            skipped += 1

    return filled, skipped, missing_required


# ---------------------------------------------------------------------------
# Resume upload
# ---------------------------------------------------------------------------

async def _upload_resume(ctrl, log, resume_path: str) -> bool:
    """
    Upload resume into the LinkedIn Easy Apply modal.
    Tries input[name='file'] first (GodsScion pattern), then input[type='file'].
    """
    modal = await _get_modal_locator(ctrl)
    for sel in _LI_FILE_SELECTORS:
        file_input = modal.locator(sel)
        try:
            if await file_input.count() > 0:
                await file_input.first.set_input_files(resume_path)
                log.field("resume_upload", resume_path, action="upload", source="profile")
                logger.info("LinkedIn: uploaded resume via %r", sel)
                return True
        except Exception as e:
            logger.warning("LinkedIn: resume upload via %r failed: %s", sel, e)
    return False


# ---------------------------------------------------------------------------
# Label extraction
# ---------------------------------------------------------------------------

async def _is_required(element) -> bool:
    """Return True if the element has required/aria-required attributes."""
    try:
        if await element.get_attribute("required") is not None:
            return True
        aria = (await element.get_attribute("aria-required") or "").lower()
        if aria == "true":
            return True
    except Exception:
        pass
    return False


async def _label_for(page, element) -> str:
    """
    Extract the human-readable label for a form element.

    Priority (GodsScion-informed):
      1. <label for="id"> → .visually-hidden span text (LinkedIn's pattern)
      2. <label for="id"> → full text content
      3. aria-label attribute
      4. placeholder attribute
      5. name attribute (slugified)
    """
    try:
        el_id = await element.get_attribute("id")
        if el_id:
            label_loc = page.locator(f"label[for='{el_id}']")
            if await label_loc.count() > 0:
                vis = label_loc.first.locator(".visually-hidden")
                if await vis.count() > 0:
                    text = ((await vis.text_content()) or "").strip()
                    if text:
                        return text
                text = ((await label_loc.first.text_content()) or "").strip()
                if text:
                    return text
    except Exception:
        pass

    try:
        aria = await element.get_attribute("aria-label")
        if aria and aria.strip():
            return aria.strip()
    except Exception:
        pass

    try:
        ph = await element.get_attribute("placeholder")
        if ph and ph.strip():
            return ph.strip()
    except Exception:
        pass

    try:
        name = (await element.get_attribute("name")) or ""
        return name.replace("-", " ").replace("_", " ").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Heuristic label→value mapping  (GodsScion keyword patterns)
# ---------------------------------------------------------------------------

def _heuristic_value(label: str, profile: "UserProfile") -> str | None:
    """
    Map a field label to a profile value without calling the LLM.
    Returns None when no confident match — those fields go to the LLM queue.
    """
    if not label:
        return None
    l = label.lower()

    # ── Contact ──────────────────────────────────────────────────────────
    if any(k in l for k in ("email", "e-mail")):
        return profile.email or None
    if any(k in l for k in ("phone", "mobile", "cell")):
        return profile.phone or None

    # ── Name ─────────────────────────────────────────────────────────────
    if re.search(r"\bfirst\s*name\b", l):
        parts = (profile.full_name or "").split()
        return parts[0] if parts else None
    if re.search(r"\b(last\s*name|surname|family\s*name)\b", l):
        parts = (profile.full_name or "").split()
        return parts[-1] if len(parts) > 1 else (parts[0] if parts else None)
    if re.search(r"\bmiddle\s*(name|initial)\b", l):
        return profile.middle_name or None
    if re.search(r"\b(full\s*name|your\s*name)\b", l):
        return profile.full_name or None
    if l.strip() == "name":
        return profile.full_name or None
    if re.search(r"\bsignature\b", l):
        return profile.full_name or None

    # ── Location ─────────────────────────────────────────────────────────
    if re.search(r"\b(street|address)\b", l) and "email" not in l:
        return profile.street_address or None
    if re.search(r"\b(city|town)\b", l):
        return profile.city or None
    if re.search(r"\blocation\b", l) and "job" not in l and "work" not in l:
        return profile.city or profile.location or None
    if re.search(r"\b(state|province|region)\b", l):
        return profile.state or None
    if re.search(r"\b(zip|postal)\b", l):
        return profile.zip_code or None
    if re.search(r"\bcountry\b", l):
        return "United States"

    # ── Experience ───────────────────────────────────────────────────────
    if re.search(r"\b(year|years)\b", l) and re.search(r"\b(experience|exp)\b", l):
        return str(profile.years_of_experience())
    if re.search(r"\bhow\s+many\s+years\b", l):
        return str(profile.years_of_experience())
    if re.search(r"\byears?\s+of\s+(relevant|total|professional)\b", l):
        return str(profile.years_of_experience())

    # ── Compensation ─────────────────────────────────────────────────────
    if re.search(r"\b(salary|compensation|pay|wage|ctc)\b", l):
        return profile.desired_salary or None

    # ── Online presence ──────────────────────────────────────────────────
    if re.search(r"\blinkedin\b", l):
        return profile.linkedin or None
    if re.search(r"\bgit\s*hub\b", l) or l.strip() == "github":
        return profile.github or None
    if re.search(r"\b(website|portfolio|personal\s*url|blog)\b", l):
        return profile.portfolio or profile.github or None

    # ── Work authorization ───────────────────────────────────────────────
    if re.search(r"\b(authorized|authorization|legally\s*work|eligible\s*to\s*work)\b", l):
        return profile.work_authorization or "Yes"
    if re.search(r"\b(require.*visa|visa.*require|need.*sponsor)\b", l):
        return profile.sponsorship_required or "No"
    if re.search(r"\bsponsor\b", l) and re.search(r"\b(require|need|necessary)\b", l):
        return profile.sponsorship_required or "No"

    # ── EEO / Compliance (safe defaults — GodsScion: "Decline" strategy) ─
    if re.search(r"\b(gender|sex)\b", l) and "bisex" not in l:
        return profile.gender or "Prefer not to answer"
    if re.search(r"\b(race|ethnicity|ethnic\s*origin)\b", l):
        return profile.race_ethnicity or "Prefer not to answer"
    if re.search(r"\b(veteran|protected\s*veteran)\b", l):
        return profile.veteran_status or "I am not a protected veteran"
    if re.search(r"\b(disability|disabled|handicap)\b", l):
        return profile.disability_status or "I do not have a disability"

    # ── Notice / Availability ─────────────────────────────────────────────
    if re.search(r"\bnotice\s*period\b", l):
        return getattr(profile, "notice_period", None) or "2 weeks"
    if re.search(r"\b(start\s*date|available\s*to\s*start|earliest.*start)\b", l):
        return getattr(profile, "available_start", None) or "Immediately"

    return None  # → falls through to LLM queue


# ---------------------------------------------------------------------------
# Option matching helpers
# ---------------------------------------------------------------------------

def _best_option(target: str, options: list[str]) -> str | None:
    """
    Find the best matching option for a target value.

    Match priority (AIHawk pattern — avoids unsafe substring matches):
      1. Exact match (case-insensitive)
      2. Prefix/suffix containment — target is a prefix of option or vice versa
         (handles "Yes" → "Yes (confirmed)" but not "No, yes I'm sure")
      3. Highest SequenceMatcher ratio — requires >= 0.55 to avoid garbage matches

    This replaces the old `target in option` substring approach which incorrectly
    matched "Yes" to "No, yes I'm sure" and "1" to "10-20".
    """
    if not options:
        return None
    t = target.lower().strip()

    # 1. Exact
    for o in options:
        if o.lower().strip() == t:
            return o

    # 2. Prefix/suffix containment
    for o in options:
        ol = o.lower().strip()
        if ol.startswith(t) or t.startswith(ol):
            return o

    # 3. Edit-distance similarity
    scored = [
        (o, SequenceMatcher(None, t, o.lower().strip()).ratio())
        for o in options
    ]
    best_o, best_score = max(scored, key=lambda x: x[1])
    return best_o if best_score >= 0.55 else None


async def _fuzzy_decline_radio_inputs(
    radio_pairs: list[tuple[object, str]],
) -> object | None:
    """
    Return the input locator for the first radio option that looks like
    'decline / prefer not to answer'.  Used as EEO fallback.

    Accepts the (input_locator, label_text) pairs built by the heuristic path,
    which uses the stable for-id relationship (GodsScion pattern).
    """
    decline_re = re.compile(
        r"(decline|prefer not|not wish|choose not|do not wish|opt out)", re.I
    )
    for radio_input, text in radio_pairs:
        if decline_re.search(text):
            return radio_input
    return None


# ---------------------------------------------------------------------------
# Inline error detection
# ---------------------------------------------------------------------------

async def _check_submit_confirmation(page) -> bool:
    """
    Poll the page for up to ~5 seconds looking for a post-submit confirmation.

    Signals checked on each iteration (first match returns True):
      1. Presence of LinkedIn's post-apply share card or modal element
      2. Presence of .jobs-s-apply__application-link (already-applied indicator)
      3. Absence of the Easy Apply modal (.jobs-easy-apply-modal)
      4. Page body containing confirmation phrases

    Returns False if none appear within the polling window.  Never raises.
    """
    _CONFIRM_CSS = [
        ".jobs-post-apply-share-to-feed-card",
        "[data-test-modal='post-apply']",
        ".jobs-s-apply__application-link",
    ]
    _CONFIRM_TEXTS = [
        "application was sent",
        "application submitted",
        "thank you for applying",
        "successfully submitted",
        "your application was",
        "applied to",
        "applied",
    ]
    try:
        # 10 checks × 500 ms ≈ 5 seconds total
        for _ in range(10):
            # Signal 1 & 2: presence of confirmation / already-applied elements
            for sel in _CONFIRM_CSS:
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0 and await loc.first.is_visible():
                        return True
                except Exception:
                    pass

            # Signal 3: Easy Apply modal has disappeared
            try:
                modal = page.locator(".jobs-easy-apply-modal")
                if await modal.count() == 0 or not await modal.first.is_visible():
                    return True
            except Exception:
                pass

            # Signal 4: confirmation text in page body
            try:
                body = ((await page.locator("body").text_content()) or "").lower()
                if any(t in body for t in _CONFIRM_TEXTS):
                    return True
            except Exception:
                pass

            await page.wait_for_timeout(500)
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Screenshot path helper
# ---------------------------------------------------------------------------

def _screenshot_path(job_id: str, suffix: str = "") -> str:
    base = project_root() / "failed_screenshots"
    base.mkdir(exist_ok=True)
    return str(base / f"{job_id}_linkedin{'_' + suffix if suffix else ''}.png")
