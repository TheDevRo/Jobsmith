"""
auto_apply/adapters/workday.py — Workday ATS adapter.

Workday is a multi-page, heavily JavaScript-driven ATS used by many large
enterprises.  The URL pattern is:
  https://<company>.wd<N>.myworkdayjobs.com/...

This adapter handles:
  1. Account creation / login (email + password from profile)
  2. "My Experience" — work history, education, resume upload
  3. "Application Questions" — standard + custom questions
  4. "Self-Identify" — EEOC / voluntary disclosures
  5. Review + Submit

Due to Workday's complexity, we fall back to the generic LLM field-detector
for most steps, and only hard-code the login and resume-upload flows.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ..models import ApplyMode, ApplyResult, ApplyStatus
from ..utils.browser_helpers import check_page_errors, take_failure_screenshot, wait_if_paused
from ...paths import project_root

from .. import ats_accounts

if TYPE_CHECKING:
    from ..browser_controller import BrowserController
    from ..llm_client import LLMClient
    from ..logger import AutoApplyLogger
    from ..models import JobApplicationRequest, UserProfile

logger = logging.getLogger(__name__)

_WORKDAY_HOST = "myworkdayjobs.com"
_MAX_PAGES = 20

_SEL = {
    "email_input":   "input[data-automation-id='email'], input[type='email']",
    "password_input":"input[data-automation-id='password'], input[type='password']",
    "sign_in_btn":   "[data-automation-id='signInSubmitButton'], button:has-text('Sign In')",
    "create_account":"[data-automation-id='createAccountLink'], a:has-text('Create Account')",
    "save_continue": "[data-automation-id='bottom-navigation-next-button'], button:has-text('Save and Continue')",
    "next_btn":      "[data-automation-id='bottom-navigation-next-button']",
    "submit_btn":    "[data-automation-id='bottom-navigation-next-button']:has-text('Submit'), button:has-text('Submit')",
    "resume_upload": "input[type='file']",
}


class WorkdayAdapter:
    name = "workday"

    def matches(self, url: str, page_text: str) -> bool:
        return _WORKDAY_HOST in url

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
        log.adapter_chosen("workday")
        filled = 0
        skipped = 0
        page_num = 0

        try:
            # ── Login / account check ──────────────────────────────────────────
            log.step("workday_auth")
            auth_ok = await _handle_workday_auth(ctrl, log, profile)
            if not auth_ok:
                screenshot = await take_failure_screenshot(ctrl.page, f"workday_{job.job_id}")
                log.result(False, "needs_review", 0, 0, tier=2, page_count=1, screenshot_path=screenshot)
                if not (profile.workday_email and profile.workday_password):
                    auth_msg = (
                        "Workday login wall detected — add workday_email and "
                        "workday_password to the profile section of config.yaml"
                    )
                else:
                    auth_msg = (
                        "Workday sign-in failed — credentials are set but the "
                        "sign-in attempt did not succeed. Verify your workday_email "
                        "and workday_password in Settings are correct."
                    )
                return ApplyResult(
                    success=False,
                    status=ApplyStatus.NEEDS_REVIEW,
                    message=auth_msg,
                    screenshot_path=screenshot,
                    manual_url=job.url,
                    adapter_used=self.name,
                    fields_filled=0,
                    fields_skipped=0,
                    log_entries=log.entries,
                )

            # ── Multi-page wizard ──────────────────────────────────────────────
            from ..answer_bank import get_answer_bank
            bank = get_answer_bank()
            bank_dict = {k: v for k, v in bank.all_snippets().items()
                         if not (v.startswith("<") and v.endswith(">"))}

            for page_num in range(1, _MAX_PAGES + 1):
                await wait_if_paused()
                log.step(f"workday_page_{page_num}", page_url=await ctrl.current_url())

                # Detect resume-upload page
                file_inputs = ctrl.page.locator(_SEL["resume_upload"])
                if await file_inputs.count() > 0:
                    if job.resume_path and Path(job.resume_path).exists():
                        try:
                            await file_inputs.first.set_input_files(job.resume_path)
                            log.field("resume", job.resume_path, action="upload", source="profile")
                            filled += 1
                        except Exception as exc:
                            log.warning(f"Resume upload failed: {exc}")

                # Generic field detection + LLM mapping for this page
                fields = await ctrl.get_dom_snapshot()
                non_file = [f for f in fields if f.field_type != "file"]
                if non_file:
                    await wait_if_paused()
                    mappings = await llm.map_fields_to_values(profile, job, non_file, bank_dict)
                    for fv in mappings:
                        await wait_if_paused()
                        if fv.action == "skip" or not fv.value:
                            skipped += 1
                            continue
                        ok = await _apply_fv(ctrl, fv)
                        if ok:
                            filled += 1
                            log.field(fv.field_id, fv.value, source=fv.source, confidence=fv.confidence, action=fv.action)
                        else:
                            skipped += 1

                # Check for inline validation errors before navigating.
                page_errors = await check_page_errors(ctrl.page)
                if page_errors:
                    for err_msg in page_errors:
                        log.warning(f"Workday page {page_num} validation error: {err_msg}")
                    screenshot = await take_failure_screenshot(ctrl.page, f"workday_{job.job_id}")
                    log.result(False, "needs_review", filled, skipped, tier=2, page_count=page_num, screenshot_path=screenshot)
                    return ApplyResult(
                        success=False,
                        status=ApplyStatus.NEEDS_REVIEW,
                        message=(
                            f"Validation errors on Workday page {page_num}: "
                            + "; ".join(page_errors[:3])
                        ),
                        screenshot_path=screenshot,
                        manual_url=job.url,
                        adapter_used=self.name,
                        fields_filled=filled,
                        fields_skipped=skipped,
                        log_entries=log.entries,
                    )

                # Distinguish Submit from Save-and-Continue using button text content
                # AND step index — the same data-automation-id is reused by Workday
                # for every navigation button; text content is the only reliable DOM
                # signal, and page_num guards against mis-detection on early steps.
                next_btn = ctrl.page.locator(_SEL["next_btn"])
                if await next_btn.count() == 0 or not await next_btn.first.is_visible(timeout=2000):
                    log.warning(f"No navigation button on Workday page {page_num}")
                    break

                is_submit_step = False
                try:
                    btn_text = ((await next_btn.first.text_content()) or "").lower()
                    if "submit" in btn_text and page_num > 1:
                        is_submit_step = True
                except Exception:
                    pass

                if is_submit_step:
                    if mode == ApplyMode.AUTOFILL:
                        log.result(True, "autofill_complete", filled, skipped, tier=2, page_count=page_num)
                        return ApplyResult(
                            success=True,
                            status=ApplyStatus.AUTOFILL_COMPLETE,
                            message="Workday form filled to final step. Submit manually.",
                            adapter_used=self.name,
                            fields_filled=filled,
                            fields_skipped=skipped,
                            log_entries=log.entries,
                        )
                    log.step(f"submit (step {page_num})")
                    await next_btn.first.click()
                    try:
                        await ctrl.page.wait_for_load_state("networkidle", timeout=20_000)
                    except Exception:
                        pass
                    page_text_check = await ctrl.page_text()
                    if any(kw in page_text_check.lower() for kw in ("thank you", "submitted", "received", "confirmation")):
                        screenshot = await ctrl.screenshot(_screenshot_path(job.job_id, "submitted"))
                        log.result(True, "submitted", filled, skipped, tier=2, page_count=page_num, screenshot_path=screenshot)
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
                else:
                    # Save and Continue
                    log.step(f"save_and_continue (step {page_num})")
                    await next_btn.first.click()
                    try:
                        await ctrl.page.wait_for_load_state("networkidle", timeout=15_000)
                    except Exception:
                        pass
                    await ctrl.page.wait_for_timeout(1000)

        except Exception as exc:
            logger.exception("Workday adapter unhandled error: %s", exc)
            log.warning(f"Unhandled error: {exc}")
            screenshot = await take_failure_screenshot(ctrl.page, f"workday_{job.job_id}")
            log.result(False, "needs_review", filled, skipped, tier=2, page_count=max(page_num, 1), screenshot_path=screenshot)
            return ApplyResult(
                success=False,
                status=ApplyStatus.NEEDS_REVIEW,
                message=f"Workday adapter error: {exc}",
                screenshot_path=screenshot,
                manual_url=job.url,
                adapter_used=self.name,
                fields_filled=filled,
                fields_skipped=skipped,
                log_entries=log.entries,
            )

        screenshot = await ctrl.screenshot(_screenshot_path(job.job_id, "needs_review"))
        log.result(False, "needs_review", filled, skipped, tier=2, page_count=max(page_num, 1), screenshot_path=screenshot)
        return ApplyResult(
            success=False,
            status=ApplyStatus.NEEDS_REVIEW,
            message="Workday: could not complete all steps — manual review required",
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

def _tenant_host(url: str) -> str | None:
    """Lowercased Workday tenant host, or None if the URL isn't a Workday host."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    return host if host.endswith(_WORKDAY_HOST) else None


async def _record_account(ctrl, log, email: str, *, created: bool) -> None:
    """Best-effort: remember this tenant's account in the synced registry.

    Never raises — the automated (Docker) path may run without the desktop DB,
    and an auth success must not be undone by a bookkeeping failure."""
    try:
        host = _tenant_host(await ctrl.current_url())
        if not host:
            return
        if created:
            # A fresh account is 'pending_verification' if Workday shows its
            # email-verification interstitial; otherwise it's immediately active.
            pending = False
            try:
                page_text = (await ctrl.page_text()).lower()
                pending = any(kw in page_text for kw in (
                    "verify your email", "check your email", "verification email",
                    "verification link",
                ))
            except Exception:
                pending = False
            await ats_accounts.upsert(
                host, email, status="pending_verification" if pending else "active"
            )
        else:
            existing = await ats_accounts.get(host)
            if existing is None:
                await ats_accounts.upsert(host, email, status="active")
            else:
                await ats_accounts.mark_signed_in(host)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(f"Workday: could not record account in registry: {exc}")


async def _handle_workday_auth(ctrl, log, profile) -> bool:
    """Sign in to Workday if a login wall is detected.

    Returns True if auth succeeded or no login wall was present.
    Returns False if a login wall was detected but could not be resolved
    (missing credentials or sign-in failed) — caller must return NEEDS_REVIEW.

    Detection uses visible form elements rather than page text, because all
    Workday pages include "Sign In" in their navigation header regardless of
    whether an actual login wall is present.
    """
    try:
        email_field = ctrl.page.locator(_SEL["email_input"]).first
        sign_in_btn = ctrl.page.locator(_SEL["sign_in_btn"]).first
        email_visible   = await email_field.is_visible(timeout=2000)
        sign_in_visible = await sign_in_btn.is_visible(timeout=2000)
    except Exception:
        email_visible   = False
        sign_in_visible = False

    # Detect Workday "Create Account" flow — verifyPassword field + createAccount button
    # are present only on that screen, not on sign-in or regular form pages.
    try:
        verify_field = ctrl.page.locator("input[data-automation-id='verifyPassword']").first
        create_btn   = ctrl.page.locator("[data-automation-id='createAccountSubmitButton']").first
        verify_visible = await verify_field.is_visible(timeout=1000)
        create_visible = await create_btn.is_visible(timeout=1000)
    except Exception:
        verify_visible = create_visible = False

    if verify_visible and create_visible:
        if not (profile.workday_email and profile.workday_password):
            log.warning("Workday create-account detected but no credentials configured in profile")
            return False
        try:
            if email_visible:
                await email_field.click(click_count=3)
                await email_field.type(profile.workday_email, delay=25)
            pw_field = ctrl.page.locator(_SEL["password_input"]).first
            if await pw_field.is_visible(timeout=2000):
                await pw_field.click(click_count=3)
                await pw_field.type(profile.workday_password, delay=25)
            await verify_field.click(click_count=3)
            await verify_field.type(profile.workday_password, delay=25)
            # Check legal/terms checkbox if present
            legal = ctrl.page.locator("input[data-automation-id='createAccountCheckbox']").first
            try:
                if await legal.is_visible(timeout=1000) and not await legal.is_checked():
                    await legal.check()
            except Exception:
                pass
            await create_btn.click()
            await ctrl.page.wait_for_load_state("networkidle", timeout=10_000)
            log.info("Workday: account created successfully")
            await _record_account(ctrl, log, profile.workday_email, created=True)
            return True
        except Exception as exc:
            log.warning(f"Workday create-account attempt failed: {exc}")
            return False

    if not email_visible and not sign_in_visible:
        return True  # No login wall present

    if not (profile.workday_email and profile.workday_password):
        log.warning("Workday login wall detected but no credentials configured in profile")
        return False

    try:
        password_field = ctrl.page.locator(_SEL["password_input"]).first
        if email_visible:
            await email_field.click(click_count=3)
            await email_field.type(profile.workday_email, delay=25)
        if await password_field.is_visible(timeout=3000):
            await password_field.click(click_count=3)
            await password_field.type(profile.workday_password, delay=25)
        if sign_in_visible:
            await sign_in_btn.click()
            await ctrl.page.wait_for_load_state("networkidle", timeout=10_000)
            log.info("Workday: signed in successfully")
            await _record_account(ctrl, log, profile.workday_email, created=False)
            return True
    except Exception as exc:
        log.warning(f"Workday sign-in attempt failed: {exc}")

    return False


async def _apply_fv(ctrl, fv) -> bool:
    from ..models import FieldValue
    if fv.action == "fill":
        return await ctrl.fill_field(fv.field_id, fv.value)
    elif fv.action == "select":
        return await ctrl.select_field(fv.field_id, fv.value)
    elif fv.action == "check":
        return await ctrl.check_field(fv.field_id)
    return False


def _screenshot_path(job_id: str, suffix: str = "") -> str:
    base = project_root() / "failed_screenshots"
    base.mkdir(exist_ok=True)
    return str(base / f"{job_id}_workday{'_' + suffix if suffix else ''}.png")
