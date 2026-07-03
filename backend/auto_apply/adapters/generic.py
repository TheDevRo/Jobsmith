"""
auto_apply/adapters/generic.py — Universal field-detector adapter.

Used as the fallback when no ATS-specific adapter matches.

Algorithm
---------
For each page in a multi-page loop (max 15 pages):
  1. Take a DOM snapshot (list[FieldDescriptor]) via BrowserController.
  2. Send descriptors + candidate profile + job description to the LLM.
  3. LLM returns a list[FieldValue] — concrete values for each field.
  4. Deterministically fill each field using BrowserController helpers.
  5. Handle file uploads (resume, cover letter) separately.
  6. Post-fill validation: check fill success rate; NEEDS_REVIEW if >30% failed.
  7. Check for inline validation errors — bail to NEEDS_REVIEW if found.
  8. Check for "done" state (thank-you page, confirmation URL).
  9. If a "next page" button is present: click it and continue the loop.
 10. Otherwise: attempt to find and click the submit button.

The LLM is ONLY used for semantic mapping and text generation — it never
clicks, navigates, or makes browser decisions.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import ApplyMode, ApplyResult, ApplyStatus, FieldDescriptor, FieldValue
from ..utils.browser_helpers import check_page_errors, wait_if_paused
from ...paths import project_root

if TYPE_CHECKING:
    from ..browser_controller import BrowserController
    from ..llm_client import LLMClient
    from ..logger import AutoApplyLogger
    from ..models import JobApplicationRequest, UserProfile

logger = logging.getLogger(__name__)

# Confidence threshold: fields below this are logged as low-confidence
_LOW_CONF = 0.60
# Confidence threshold below which AUTOFILL mode returns NEEDS_REVIEW (not success)
_NEEDS_REVIEW_CONF = 0.50

# Maximum number of form pages to traverse before giving up
_MAX_PAGES = 15

# Minimum number of fillable fields that signals a real application form.
_MIN_FORM_FIELDS = 2

# Maximum number of consecutive Apply-button clicks before giving up.
_MAX_APPLY_HOPS = 5

# Fill failure rate above which we flag the result as NEEDS_REVIEW
_MAX_FAIL_RATE = 0.30

# Selectors tried in order to find the final submit button.
# More specific / explicit patterns first to avoid false positives.
_SUBMIT_SELECTORS = [
    # Explicit submit by type
    "button[type='submit']",
    "input[type='submit']",
    # ARIA role buttons with submit/apply label
    "[role='button'][aria-label*='submit' i]",
    "[role='button'][aria-label*='apply' i]",
    # Text-based (longest/most specific first)
    "button:has-text('Submit Application')",
    "button:has-text('Send Application')",
    "button:has-text('Apply Now')",
    "button:has-text('Complete Application')",
    "button:has-text('Complete')",
    "button:has-text('Submit')",
    "button:has-text('Apply')",
    # ATS-specific
    "[data-automation-id='bottom-navigation-next-button']",
]

# Text patterns that identify a "next page" button (checked against lowercased text)
_NEXT_PATTERNS = [
    "next step",
    "next page",
    "save and continue",
    "save & continue",
    "save and next",
    "go to next",
    "proceed",
    "continue",
    "next",
    "forward",
    ">",
]

# Selectors tried in order to find a pre-form "Apply" CTA on job description pages.
_PRE_APPLY_SELECTORS = [
    "a:has-text('Apply Now')",
    "button:has-text('Apply Now')",
    "a:has-text('Apply for this job')",
    "button:has-text('Apply for this job')",
    "a:has-text('Apply for this position')",
    "[data-apply-button]",
    "[id*='apply'][href]",
    "a.apply-button",
    "a[class*='apply']",
    "button[class*='apply']",
    "a:has-text('Apply')",
    "button:has-text('Apply')",
]

# Text/URL fragments that indicate a "thank you / submitted" page
_SUCCESS_TEXT = [
    "thank you", "thanks for applying", "application received",
    "successfully submitted", "application submitted", "application complete",
    "we have received", "you have applied", "confirmation",
]
_SUCCESS_URL_FRAGMENTS = [
    "success", "confirmation", "thank-you", "thankyou", "complete", "submitted",
]


class GenericAdapter:
    """Universal adapter — detects all form fields via DOM snapshot + LLM mapping."""

    name = "generic"

    def matches(self, url: str, page_text: str) -> bool:
        # Always matches — used as fallback
        return True

    async def apply(
        self,
        ctrl: "BrowserController",
        profile: "UserProfile",
        job: "JobApplicationRequest",
        llm: "LLMClient",
        mode: ApplyMode,
        log: "AutoApplyLogger",
    ) -> ApplyResult:
        from ..answer_bank import get_answer_bank

        await wait_if_paused()
        log.adapter_chosen("generic", "universal fallback")
        answer_bank = get_answer_bank()
        bank_dict = {
            k: v for k, v in answer_bank.all_snippets().items()
            if not (v.startswith("<") and v.endswith(">"))
        }

        # ── Step 0: Dismiss popups that might block the form ──────────────
        dismissed = await ctrl.dismiss_popups()
        if dismissed:
            log.step("popup_dismissed")
            await ctrl.page.wait_for_timeout(500)

        # ── Multi-page loop ────────────────────────────────────────────────
        filled          = 0
        skipped         = 0
        fill_attempts   = 0
        fill_failures   = 0
        low_conf_fields: list[str] = []
        very_low_conf_fields: list[str] = []
        page_num        = 1

        while True:
            await wait_if_paused()
            log.step(f"page_{page_num}_dom_snapshot")

            # ── 1: Snapshot fields (fresh each page) ──────────────────────
            fields: list[FieldDescriptor] = await ctrl.get_dom_snapshot()
            if not fields:
                if page_num == 1:
                    # ── Step A: wait for SPA to paint ─────────────────────
                    log.step("page_1_wait_for_render")
                    try:
                        await ctrl.page.wait_for_load_state("networkidle", timeout=8_000)
                    except Exception:
                        pass

                    fields = await ctrl.get_dom_snapshot()

                    # ── Step B: multi-hop Apply loop ───────────────────────
                    # Tracks URLs we've actually navigated TO (not the start URL).
                    # If _detect_generic_redirect returns a URL we already tried,
                    # we stop to prevent circular Apply→portal→Apply chains.
                    seen_redirect_urls: set[str] = set()
                    for _hop in range(_MAX_APPLY_HOPS):
                        hop_label = "page_1_pre_apply_hunt" if _hop == 0 else f"page_1_apply_hop_{_hop + 1}"
                        log.step(hop_label)

                        apply_clicked = await _click_pre_apply_button(ctrl.page)
                        if not apply_clicked:
                            break

                        clicked_label = "page_1_pre_apply_clicked" if _hop == 0 else f"page_1_apply_hop_{_hop + 1}_clicked"
                        log.step(clicked_label)
                        await ctrl.page.wait_for_timeout(1500)

                        redirect_url = await _detect_generic_redirect(ctrl)
                        if redirect_url:
                            # Detect circular redirect chain
                            if redirect_url in seen_redirect_urls:
                                logger.info(
                                    "Generic: Apply hop %d revisited redirect URL — stopping", _hop + 1
                                )
                                break
                            seen_redirect_urls.add(redirect_url)

                            from . import ALL_ADAPTERS
                            ext_adapter = next(
                                (a for a in ALL_ADAPTERS
                                 if a.name != "generic" and a.matches(redirect_url, "")),
                                None,
                            )
                            if ext_adapter:
                                log.step("pre_apply_redirect", page_url=redirect_url)
                                logger.info(
                                    "Generic: pre-apply redirect → %s (adapter=%s)",
                                    redirect_url[:80], ext_adapter.name,
                                )
                                return await ext_adapter.apply(ctrl, profile, job, llm, mode, log)
                            log.step("pre_apply_redirect_generic", page_url=redirect_url)
                            logger.info(
                                "Generic: pre-apply navigated to %s, re-snapshotting",
                                redirect_url[:80],
                            )
                        else:
                            try:
                                await ctrl.page.wait_for_load_state("networkidle", timeout=5_000)
                            except Exception:
                                pass

                        fields = await ctrl.get_dom_snapshot()
                        fillable = [f for f in fields if f.field_type != "file"]

                        if len(fillable) >= _MIN_FORM_FIELDS:
                            logger.info(
                                "Generic: Apply hop %d found %d fillable fields — stopping",
                                _hop + 1, len(fillable),
                            )
                            break

                    if not fields:
                        log.warning("No interactive fields detected on page")
                        screenshot = await ctrl.screenshot(_screenshot_path(job.job_id))
                        return ApplyResult(
                            success=False,
                            status=ApplyStatus.NEEDS_REVIEW,
                            message="No form fields detected — manual review required",
                            screenshot_path=screenshot,
                            adapter_used=self.name,
                        )
                else:
                    log.warning(f"Page {page_num}: no fields detected, proceeding to submit")

            # Separate file-upload fields from regular fields
            upload_fields  = [f for f in fields if f.field_type == "file"]
            regular_fields = [f for f in fields if f.field_type != "file"]

            # ── Pre-pass: deterministic credential fill (bypasses LLM) ───
            if any(f.field_type == "password" for f in regular_fields):
                log.step(f"page_{page_num}_credential_fill")
                regular_fields, cred_filled, cred_skipped, cred_attempts, cred_failures = \
                    await _resolve_login_fields(regular_fields, profile, ctrl, log, page_num)
                filled        += cred_filled
                skipped       += cred_skipped
                fill_attempts += cred_attempts
                fill_failures += cred_failures

            # ── 2: LLM field mapping ───────────────────────────────────────
            if regular_fields:
                log.step(f"page_{page_num}_llm_field_mapping")
                await wait_if_paused()
                mappings: list[FieldValue] = await llm.map_fields_to_values(
                    profile, job, regular_fields, bank_dict
                )

                if mappings:
                    avg_conf = sum(m.confidence for m in mappings) / len(mappings)
                    n_skipped = sum(1 for m in mappings if m.action == "skip")
                    log.llm_call(
                        fields_count=len(regular_fields),
                        confidence_avg=avg_conf,
                        skipped=n_skipped,
                    )
                    page_low_conf = [
                        fv.field_id for fv in mappings
                        if fv.action != "skip" and fv.value and fv.confidence < _LOW_CONF
                    ]
                    page_very_low = [
                        fv.field_id for fv in mappings
                        if fv.action != "skip" and fv.value and fv.confidence < _NEEDS_REVIEW_CONF
                    ]
                    if page_low_conf:
                        log.warning(
                            f"Page {page_num}: low-confidence LLM mappings "
                            f"({len(page_low_conf)}): " + ", ".join(page_low_conf)
                        )
                        low_conf_fields.extend(page_low_conf)
                    if page_very_low:
                        very_low_conf_fields.extend(page_very_low)
                else:
                    mappings = []

                # ── 3: Fill regular fields ─────────────────────────────────
                log.step(f"page_{page_num}_fill_fields")
                for fv in mappings:
                    await wait_if_paused()
                    if fv.action == "skip" or not fv.value:
                        skipped += 1
                        log.field(fv.field_id, "", action="skip", source="skip", confidence=fv.confidence)
                        continue

                    fill_attempts += 1
                    success = await _apply_field_value(ctrl, fv)
                    if success:
                        filled += 1
                        log.field(
                            fv.field_id, fv.value,
                            action=fv.action,
                            source=fv.source,
                            confidence=fv.confidence,
                        )
                    else:
                        fill_failures += 1
                        skipped += 1
                        log.warning(f"Could not fill field {fv.field_id}")

            # ── 4: Handle file uploads ─────────────────────────────────────
            if upload_fields:
                log.step(f"page_{page_num}_file_uploads")
                for uf in upload_fields:
                    uploaded = False
                    label_lower = uf.label.lower()
                    if "resume" in label_lower or "cv" in label_lower:
                        if job.resume_path and Path(job.resume_path).exists():
                            uploaded = await ctrl.upload_file(uf.field_id, job.resume_path)
                            if uploaded:
                                log.field(uf.field_id, job.resume_path, action="upload", source="profile")
                                filled += 1
                    elif "cover" in label_lower:
                        if job.cover_letter_path and Path(job.cover_letter_path).exists():
                            uploaded = await ctrl.upload_file(uf.field_id, job.cover_letter_path)
                            if uploaded:
                                log.field(uf.field_id, job.cover_letter_path, action="upload", source="profile")
                                filled += 1
                    else:
                        if job.resume_path and Path(job.resume_path).exists():
                            uploaded = await ctrl.upload_file(uf.field_id, job.resume_path)
                            if uploaded:
                                filled += 1

                    if not uploaded:
                        skipped += 1
                        log.warning(f"File upload skipped for {uf.field_id} ({uf.label!r})")

            # ── 5: Post-fill validation ────────────────────────────────────
            if fill_attempts > 0:
                fail_rate = fill_failures / fill_attempts
                if fail_rate > _MAX_FAIL_RATE:
                    log.warning(
                        f"Page {page_num}: {fill_failures}/{fill_attempts} fills failed "
                        f"({fail_rate:.0%}) — flagging for review"
                    )
                    screenshot = await ctrl.screenshot(_screenshot_path(job.job_id))
                    log.result(False, "needs_review", filled, skipped, tier=2, page_count=page_num, screenshot_path=screenshot)
                    return ApplyResult(
                        success=False,
                        status=ApplyStatus.NEEDS_REVIEW,
                        message=(
                            f"Page {page_num}: {fill_failures}/{fill_attempts} fields could not be filled — "
                            "manual review required"
                        ),
                        screenshot_path=screenshot,
                        manual_url=job.url,
                        adapter_used=self.name,
                        fields_filled=filled,
                        fields_skipped=skipped,
                        log_entries=log.entries,
                    )

            # ── 6: Page error check ────────────────────────────────────────
            page_errors = await check_page_errors(ctrl.page)
            if page_errors:
                for err_msg in page_errors:
                    log.warning(f"Page {page_num} inline validation error: {err_msg}")
                screenshot = await ctrl.screenshot(_screenshot_path(job.job_id))
                log.result(False, "needs_review", filled, skipped, tier=2, page_count=page_num, screenshot_path=screenshot)
                return ApplyResult(
                    success=False,
                    status=ApplyStatus.NEEDS_REVIEW,
                    message=f"Validation errors on page {page_num}: " + "; ".join(page_errors[:3]),
                    screenshot_path=screenshot,
                    manual_url=job.url,
                    adapter_used=self.name,
                    fields_filled=filled,
                    fields_skipped=skipped,
                    log_entries=log.entries,
                )

            # ── 7: Page-cap guard ──────────────────────────────────────────
            if page_num >= _MAX_PAGES:
                log.warning("Multi-page loop exceeded 15 pages")
                screenshot = await ctrl.screenshot(_screenshot_path(job.job_id))
                log.result(False, "needs_review", filled, skipped, tier=2, page_count=page_num, screenshot_path=screenshot)
                return ApplyResult(
                    success=False,
                    status=ApplyStatus.NEEDS_REVIEW,
                    message="Multi-page loop exceeded 15 pages — manual review required",
                    screenshot_path=screenshot,
                    manual_url=job.url,
                    adapter_used=self.name,
                    fields_filled=filled,
                    fields_skipped=skipped,
                    log_entries=log.entries,
                )

            # ── 8: Navigate — next page or submit ─────────────────────────
            next_btn = await _detect_next_button(ctrl.page)
            if next_btn is not None:
                log.step(f"page_{page_num}_next_click")
                url_before = ctrl.page.url
                try:
                    await next_btn.click()
                    await ctrl.page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass

                # Check for "done" state immediately after the click
                if await _check_success_state(ctrl.page):
                    log.step("success_page_detected")
                    screenshot = await ctrl.screenshot(_screenshot_path(job.job_id))
                    log.result(True, "submitted", filled, skipped, tier=2, page_count=page_num, screenshot_path=screenshot)
                    return ApplyResult(
                        success=True,
                        status=ApplyStatus.SUBMITTED,
                        message=f"Application submitted — confirmation page detected ({page_num} page(s))",
                        screenshot_path=screenshot,
                        adapter_used=self.name,
                        fields_filled=filled,
                        fields_skipped=skipped,
                        log_entries=log.entries,
                    )

                # Verify the page actually changed after clicking "next"
                if ctrl.page.url == url_before:
                    try:
                        await ctrl.page.wait_for_url(
                            lambda u: u != url_before, timeout=3_000
                        )
                    except Exception:
                        pass  # URL didn't change — may be SPA with in-page step advance

                page_num += 1
                log.step(f"page_{page_num}_start", page_url=ctrl.page.url)
                continue

            # No next button → final page
            break
        # ── end multi-page loop ───────────────────────────────────────────

        # ── 9: Very-low-confidence gate (NEEDS_REVIEW even in AUTOFILL) ───
        if very_low_conf_fields:
            very_low_msg = (
                f"{len(very_low_conf_fields)} field(s) had confidence < 50% — review: "
                + ", ".join(very_low_conf_fields)
            )
            screenshot = await ctrl.screenshot(_screenshot_path(job.job_id))
            log.result(False, "needs_review", filled, skipped, tier=2, page_count=page_num,
                       fields_needs_review=len(very_low_conf_fields), screenshot_path=screenshot)
            return ApplyResult(
                success=False,
                status=ApplyStatus.NEEDS_REVIEW,
                message=f"Needs review: {very_low_msg}",
                screenshot_path=screenshot,
                manual_url=job.url,
                adapter_used=self.name,
                fields_filled=filled,
                fields_skipped=skipped,
                log_entries=log.entries,
            )

        # ── 10: Moderate low-confidence message (informational) ───────────
        low_conf_msg = ""
        if low_conf_fields:
            low_conf_msg = (
                f" {len(low_conf_fields)} low-confidence field(s): "
                + ", ".join(low_conf_fields)
            )

        # ── 11: Mode gate — submit or stop ────────────────────────────────
        if mode == ApplyMode.AUTOFILL:
            log.result(True, "autofill_complete", filled, skipped, tier=2, page_count=page_num,
                       fields_needs_review=len(low_conf_fields))
            return ApplyResult(
                success=True,
                status=ApplyStatus.AUTOFILL_COMPLETE,
                message=f"Form filled ({filled} fields, {page_num} page(s)). Manual submit required.{low_conf_msg}",
                adapter_used=self.name,
                fields_filled=filled,
                fields_skipped=skipped,
                log_entries=log.entries,
            )

        # SUBMIT mode: find and click the submit button
        log.step("submit")
        submitted = await _click_submit(ctrl)
        screenshot = await ctrl.screenshot(_screenshot_path(job.job_id))

        if submitted:
            # Check for confirmation page after submit
            is_confirmed = await _check_success_state(ctrl.page)
            log.result(True, "submitted", filled, skipped, tier=2, page_count=page_num, screenshot_path=screenshot)
            return ApplyResult(
                success=True,
                status=ApplyStatus.SUBMITTED,
                message=(
                    f"Application submitted ({page_num} page(s)): {job.title} at {job.company}"
                    + (" — confirmation detected" if is_confirmed else "")
                    + low_conf_msg
                ),
                screenshot_path=screenshot,
                adapter_used=self.name,
                fields_filled=filled,
                fields_skipped=skipped,
                log_entries=log.entries,
            )
        else:
            log.result(False, "needs_review", filled, skipped, tier=2, page_count=page_num, screenshot_path=screenshot)
            return ApplyResult(
                success=False,
                status=ApplyStatus.NEEDS_REVIEW,
                message="Could not locate Submit button — manual review required",
                screenshot_path=screenshot,
                manual_url=job.url,
                adapter_used=self.name,
                fields_filled=filled,
                fields_skipped=skipped,
                log_entries=log.entries,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _apply_field_value(ctrl: "BrowserController", fv: FieldValue) -> bool:
    """Dispatch to the correct BrowserController method based on action."""
    if fv.action == "fill":
        return await ctrl.fill_field(fv.field_id, fv.value)
    elif fv.action == "select":
        return await ctrl.select_field(fv.field_id, fv.value)
    elif fv.action == "check":
        return await ctrl.check_field(fv.field_id)
    elif fv.action == "upload":
        # Upload is handled separately — skip here
        return False
    return False


async def _click_submit(ctrl: "BrowserController") -> bool:
    """
    Try each submit selector in order; click and wait for navigation.

    Returns True on first successful click. Never raises.
    """
    for sel in _SUBMIT_SELECTORS:
        try:
            locator = ctrl.page.locator(sel).first
            if not await locator.is_visible(timeout=2000):
                continue
            await locator.click()
            try:
                await ctrl.page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            return True
        except Exception:
            pass
    return False


async def _detect_next_button(page) -> object | None:
    """
    Search for a visible, clickable "next page" button on the current page.

    Matches by text content, aria-label, and icon-button patterns.
    Never raises.
    """
    try:
        # Collect all candidate interactive elements
        locator = page.locator(
            "button, input[type='button'], input[type='submit'], "
            "a[role='button'], [role='button']"
        )
        buttons = await locator.all()
        for btn in buttons:
            try:
                if not await btn.is_visible():
                    continue
                if not await btn.is_enabled():
                    continue

                # Check visible text
                text = ((await btn.text_content()) or "").strip().lower()
                if not text:
                    text = ((await btn.get_attribute("value")) or "").strip().lower()

                # Check aria-label (icon buttons often skip visible text)
                aria_label = ((await btn.get_attribute("aria-label")) or "").strip().lower()
                check_text = text or aria_label

                for pattern in _NEXT_PATTERNS:
                    if pattern in check_text:
                        # Don't match "next" inside a submit button label to avoid
                        # accidentally clicking Submit when we want only Next.
                        if any(s in check_text for s in ["submit", "apply", "send"]):
                            continue
                        return btn
            except Exception:
                pass
    except Exception:
        pass
    return None


async def _check_success_state(page) -> bool:
    """
    Return True if the current page looks like a post-submission confirmation.

    Checks both page text and URL for common success indicators.
    Never raises.
    """
    try:
        url_lower = (page.url or "").lower()
        for fragment in _SUCCESS_URL_FRAGMENTS:
            if fragment in url_lower:
                logger.info("Generic: success URL fragment %r detected in %s", fragment, url_lower[:80])
                return True

        try:
            body_text = (await page.inner_text("body", timeout=3000)).lower()
            for phrase in _SUCCESS_TEXT:
                if phrase in body_text:
                    logger.info("Generic: success phrase %r detected on page", phrase)
                    return True
        except Exception:
            pass
    except Exception:
        pass
    return False


async def _click_pre_apply_button(page) -> bool:
    """
    Search for a visible "Apply" CTA on a job description page and click it.

    Three-phase search to handle job boards with unusual markup:

    Phase 1 — CSS selectors (_PRE_APPLY_SELECTORS)
    Phase 2 — text-content scan of all <a href> and <button> elements
    Phase 3 — JavaScript DOM evaluation (bypasses Playwright visibility)

    Returns True on first successful click, False if no button found.
    Never raises.
    """
    _APPLY_KEYWORDS = [
        "apply now",
        "apply for this job",
        "apply for this position",
        "apply for this role",
        "apply online",
        "apply here",
        "easy apply",
        "quick apply",
        "apply to this",
    ]

    # ── Phase 1: specific CSS selectors ───────────────────────────────────
    for sel in _PRE_APPLY_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                await loc.click()
                logger.info("Generic: pre-apply button clicked via selector %r", sel)
                return True
        except Exception:
            pass

    # ── Phase 2: text-content scan (handles non-standard class names) ─────
    try:
        candidates = await page.locator("a[href], button").all()
        for el in candidates:
            try:
                if not await el.is_visible():
                    continue
                text = ((await el.text_content()) or "").strip().lower()
                for kw in _APPLY_KEYWORDS:
                    if kw in text:
                        try:
                            await el.scroll_into_view_if_needed()
                        except Exception:
                            pass
                        await el.click()
                        logger.info(
                            "Generic: pre-apply button clicked via text scan "
                            "(kw=%r, text=%r)", kw, text[:60]
                        )
                        return True
            except Exception:
                pass
    except Exception:
        pass

    # ── Phase 3: JavaScript DOM evaluation (bypasses Playwright visibility) ─
    _JS_CLICK_APPLY = """
        () => {
            const phrases = [
                'apply now', 'apply for this job', 'apply for this position',
                'apply for this role', 'apply online', 'apply here',
                'easy apply', 'quick apply', 'apply to this',
            ];
            const els = [...document.querySelectorAll('a[href], button')];

            // First pass: specific multi-word phrases
            for (const el of els) {
                const text = (el.textContent || '').trim().toLowerCase();
                if (phrases.some(p => text.includes(p)) && el.offsetParent !== null) {
                    el.click();
                    return text.substring(0, 60);
                }
            }
            // Second pass: short elements whose full text is/contains 'apply'
            for (const el of els) {
                const text = (el.textContent || '').trim().toLowerCase();
                if (text.length <= 60 && text.includes('apply') && el.offsetParent !== null) {
                    el.click();
                    return text.substring(0, 60);
                }
            }
            return null;
        }
    """
    try:
        clicked_text = await page.evaluate(_JS_CLICK_APPLY)
        if clicked_text is not None:
            logger.info(
                "Generic: pre-apply button clicked via JS DOM eval (text=%r)",
                clicked_text,
            )
            return True
    except Exception:
        pass

    return False


async def _detect_generic_redirect(ctrl: "BrowserController") -> str:
    """
    After clicking a pre-apply button, check whether the browser navigated
    away from the original host (new tab or same-tab redirect).

    Returns the destination URL if an off-host navigation occurred,
    empty string otherwise.  Never raises.
    """
    try:
        from urllib.parse import urlparse as _urlparse
        original_host = _urlparse(ctrl.page.url).netloc if ctrl.page.url else ""

        # Case 1: new tab opened
        new_url = await ctrl.switch_to_new_page(timeout=3000)
        if new_url:
            new_host = _urlparse(new_url).netloc if new_url else ""
            if new_host and new_host != original_host:
                return new_url

        # Case 2: same-tab navigation to a different host
        current = ctrl.page.url
        current_host = _urlparse(current).netloc if current else ""
        if current_host and current_host != original_host:
            return current
    except Exception:
        pass
    return ""


async def _resolve_login_fields(
    fields: list["FieldDescriptor"],
    profile: "UserProfile",
    ctrl: "BrowserController",
    log: "AutoApplyLogger",
    page_num: int,
) -> "tuple[list[FieldDescriptor], int, int, int, int]":
    """
    Detect a login form by the presence of a type='password' field, then fill
    credentials deterministically without calling the LLM.

    Password fields never go to the LLM — they're filled directly from:
      ats_login_password  (preferred)
      workday_password    (fallback)

    The companion email/username field is filled from profile.email.

    Returns (remaining_fields, filled, skipped, attempts, failures).
    """
    from ..models import FieldValue

    password_fields = [f for f in fields if f.field_type == "password"]
    if not password_fields:
        return fields, 0, 0, 0, 0

    login_email    = profile.email
    login_password = profile.ats_login_password or profile.workday_password

    _LOGIN_KW = ("email", "username", "user name", "login", "e-mail")
    email_fields = [
        f for f in fields
        if f.field_type in ("email", "text")
        and any(kw in (f.label + " " + f.name).lower() for kw in _LOGIN_KW)
    ]

    credential_ids: set[str] = (
        {f.field_id for f in password_fields} | {f.field_id for f in email_fields}
    )

    filled = skipped = attempts = failures = 0

    for f in email_fields:
        attempts += 1
        if login_email:
            fv = FieldValue(
                field_id=f.field_id, value=login_email,
                action="fill", confidence=1.0, source="profile",
            )
            ok = await _apply_field_value(ctrl, fv)
            if ok:
                filled += 1
                log.field(f.field_id, login_email, action="fill", source="profile", confidence=1.0)
            else:
                failures += 1
                skipped  += 1
                log.warning(f"Page {page_num}: could not fill login email field {f.field_id}")
        else:
            skipped += 1
            log.warning(f"Page {page_num}: login email field found but profile.email is empty")

    for f in password_fields:
        attempts += 1
        if login_password:
            fv = FieldValue(
                field_id=f.field_id, value=login_password,
                action="fill", confidence=1.0, source="profile",
            )
            ok = await _apply_field_value(ctrl, fv)
            if ok:
                filled += 1
                log.field(f.field_id, "[REDACTED]", action="fill", source="profile", confidence=1.0)
            else:
                failures += 1
                skipped  += 1
                log.warning(f"Page {page_num}: could not fill password field {f.field_id}")
        else:
            skipped += 1
            logger.warning(
                "Generic: password field on page %d but no ats_login_password or "
                "workday_password configured — skipping", page_num
            )

    remaining = [f for f in fields if f.field_id not in credential_ids]
    return remaining, filled, skipped, attempts, failures


def _screenshot_path(job_id: str) -> str:
    from pathlib import Path
    base = project_root() / "failed_screenshots"
    base.mkdir(exist_ok=True)
    return str(base / f"{job_id}_generic.png")
