"""
auto_apply/adapters/lever.py — Lever ATS adapter.

Handles forms at:
  https://jobs.lever.co/<company>/<id>/apply

Standard Lever application form fields (consistent layout):
  Full name / Email / Phone / Company / Title / Location
  Resume upload / Cover letter upload
  LinkedIn / GitHub / Portfolio
  Custom questions
  Submit
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import ApplyMode, ApplyResult, ApplyStatus
from ..utils.browser_helpers import check_page_errors, wait_if_paused
from ...paths import project_root

if TYPE_CHECKING:
    from ..browser_controller import BrowserController
    from ..llm_client import LLMClient
    from ..logger import AutoApplyLogger
    from ..models import JobApplicationRequest, UserProfile

logger = logging.getLogger(__name__)

_LEVER_HOST = "jobs.lever.co"

_SEL = {
    # Standard fields (Lever uses input[name="..."] reliably)
    "full_name":    "input[name='name']",
    "email":        "input[name='email']",
    "phone":        "input[name='phone']",
    "org":          "input[name='org']",       # Current company
    "title":        "input[name='title']",     # Current title
    "location":     "input[name='location']",
    "resume":       "input[type='file']",
    "linkedin":     "input[name='urls[LinkedIn]']",
    "github":       "input[name='urls[GitHub]']",
    "portfolio":    "input[name='urls[Portfolio]'], input[name='urls[Other]']",
    "cover_letter": "textarea[name='comments']",  # Lever's "Additional information"
    "submit":       "button[type='submit'], input[type='submit']",
}


class LeverAdapter:
    name = "lever"

    def matches(self, url: str, page_text: str) -> bool:
        return _LEVER_HOST in url

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
        log.adapter_chosen("lever")
        filled = 0
        skipped = 0

        # ── Standard fields ────────────────────────────────────────────────
        log.step("standard_fields")

        # Most recent job title + company from experience
        current_title   = profile.experience[0].title   if profile.experience else ""
        current_company = profile.experience[0].company if profile.experience else ""

        field_map = [
            (_SEL["full_name"],  profile.full_name,   "full_name"),
            (_SEL["email"],      profile.email,        "email"),
            (_SEL["phone"],      profile.phone,        "phone"),
            (_SEL["org"],        current_company,      "org"),
            (_SEL["title"],      current_title,        "title"),
            (_SEL["location"],   profile.location,     "location"),
            (_SEL["linkedin"],   profile.linkedin,     "linkedin"),
            (_SEL["github"],     profile.github,       "github"),
            (_SEL["portfolio"],  profile.portfolio,    "portfolio"),
        ]

        for sel, value, label in field_map:
            if not value:
                continue
            ok = await _fill(ctrl, log, sel, value, label)
            filled += int(ok)

        # ── Cover letter text field ────────────────────────────────────────
        from ..answer_bank import get_answer_bank
        bank = get_answer_bank()
        cl_text = bank.get("cover_letter")
        if not cl_text:
            cl_text = await llm.generate_answer(
                "Write a brief cover letter body (2-3 sentences) for this role.",
                profile, job
            )
        if cl_text:
            ok = await _fill(ctrl, log, _SEL["cover_letter"], cl_text, "cover_letter")
            filled += int(ok)

        # ── Resume upload ──────────────────────────────────────────────────
        log.step("file_upload")
        if job.resume_path and Path(job.resume_path).exists():
            try:
                el = ctrl.page.locator(_SEL["resume"]).first
                await el.set_input_files(job.resume_path)
                log.field("resume", job.resume_path, action="upload", source="profile")
                filled += 1
            except Exception as exc:
                log.warning(f"Resume upload failed: {exc}")

        # ── Custom questions ───────────────────────────────────────────────
        log.step("custom_questions")
        filled_c, skipped_c = await _fill_custom(ctrl, log, profile, job, llm)
        filled  += filled_c
        skipped += skipped_c

        # ── Mode gate ─────────────────────────────────────────────────────
        if mode == ApplyMode.AUTOFILL:
            log.result(True, "autofill_complete", filled, skipped, tier=1, page_count=1)
            return ApplyResult(
                success=True,
                status=ApplyStatus.AUTOFILL_COMPLETE,
                message=f"Lever form filled ({filled} fields). Submit manually.",
                adapter_used=self.name,
                fields_filled=filled,
                fields_skipped=skipped,
                log_entries=log.entries,
            )

        # ── Validate before submitting ─────────────────────────────────────
        page_errors = await check_page_errors(ctrl.page)
        if page_errors:
            for err_msg in page_errors:
                log.warning(f"Inline validation error: {err_msg}")
            screenshot = await ctrl.screenshot(_screenshot_path(job.job_id, "errors"))
            log.result(False, "needs_review", filled, skipped, tier=1, page_count=1, screenshot_path=screenshot)
            return ApplyResult(
                success=False,
                status=ApplyStatus.NEEDS_REVIEW,
                message="Validation errors found: " + "; ".join(page_errors[:3]),
                screenshot_path=screenshot,
                manual_url=job.url,
                adapter_used=self.name,
                fields_filled=filled,
                fields_skipped=skipped,
                log_entries=log.entries,
            )

        # SUBMIT
        log.step("submit")
        submitted = await ctrl.click(_SEL["submit"])
        screenshot = ""
        if submitted:
            try:
                await ctrl.page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            page_text = await ctrl.page_text()
            if any(kw in page_text.lower() for kw in ("thank you", "submitted", "received")):
                screenshot = await ctrl.screenshot(_screenshot_path(job.job_id, "submitted"))
                log.result(True, "submitted", filled, skipped, tier=1, page_count=1, screenshot_path=screenshot)
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

        screenshot = await ctrl.screenshot(_screenshot_path(job.job_id, "needs_review"))
        log.result(False, "needs_review", filled, skipped, tier=1, page_count=1, screenshot_path=screenshot)
        return ApplyResult(
            success=False,
            status=ApplyStatus.NEEDS_REVIEW,
            message="Could not confirm submission — manual review required",
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

async def _fill(ctrl, log, selector: str, value: str, label: str) -> bool:
    if not value:
        return False
    try:
        el = ctrl.page.locator(selector).first
        if not await el.is_visible(timeout=3000):
            return False
        await el.click(click_count=3)
        await el.type(value, delay=25)
        log.field(label, value, source="profile", confidence=1.0)
        return True
    except Exception:
        return False


async def _fill_custom(ctrl, log, profile, job, llm) -> tuple[int, int]:
    """Handle custom Lever questions via the generic field-detector."""
    from ..answer_bank import get_answer_bank

    fields = await ctrl.get_dom_snapshot()
    known_names = {"name", "email", "phone", "org", "title", "location",
                   "urls[LinkedIn]", "urls[GitHub]", "urls[Portfolio]", "comments"}
    custom = [
        f for f in fields
        if f.field_type in ("text", "textarea", "number", "select", "radio", "checkbox")
        and f.name not in known_names
    ]
    if not custom:
        return 0, 0

    bank = get_answer_bank()
    bank_dict = {k: v for k, v in bank.all_snippets().items()
                 if not (v.startswith("<") and v.endswith(">"))}

    await wait_if_paused()
    mappings = await llm.map_fields_to_values(profile, job, custom, bank_dict)
    filled = skipped = 0
    for fv in mappings:
        await wait_if_paused()
        if fv.action == "skip" or not fv.value:
            skipped += 1
            continue
        ok = await ctrl.fill_field(fv.field_id, fv.value)
        if ok:
            filled += 1
            log.field(fv.field_id, fv.value, source=fv.source, confidence=fv.confidence)
        else:
            skipped += 1
    return filled, skipped


def _screenshot_path(job_id: str, suffix: str = "") -> str:
    base = project_root() / "failed_screenshots"
    base.mkdir(exist_ok=True)
    return str(base / f"{job_id}_lever{'_' + suffix if suffix else ''}.png")
