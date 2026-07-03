"""
auto_apply/adapters/ashby.py — Ashby ATS adapter.

Handles forms at:
  https://jobs.ashbyhq.com/<org>/<job-uuid>
  https://jobs.ashbyhq.com/<org>/<job-uuid>/application

Ashby postings are a single-page React app with an "Overview" tab and an
"Application" tab. The application form itself is one page:
  - Name (single full-name field) / Email / Phone
  - Resume upload (usually required; may autofill other fields on upload)
  - Custom questions (varies per company)
  - Submit button

Standard fields use stable "_systemfield_*" ids; everything else goes through
the DOM-snapshot + LLM field-mapping flow, same as the Greenhouse adapter.
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

_ASHBY_URL_FRAGMENTS = ("ashbyhq.com", "jobs.ashby")

# Known Ashby selectors. Standard fields carry stable _systemfield_* ids;
# fallbacks by input type cover boards where the ids differ.
_SEL = {
    "full_name": "input[id='_systemfield_name'], input[name='_systemfield_name'], input[aria-label='Name' i], input[autocomplete='name']",
    "email":     "input[id='_systemfield_email'], input[name='_systemfield_email'], input[type='email']",
    "phone":     "input[id='_systemfield_phone'], input[name='_systemfield_phone'], input[type='tel']",
    "resume":    "input[type='file'][id*='resume'], input[type='file'][name*='resume'], input[type='file']",
    "submit":    "button.ashby-application-form-submit-button, button[type='submit']",
    # The "Application" tab / apply CTA shown when landing on the Overview tab
    "apply_tab": "a[href$='/application'], [role='tab']:has-text('Application'), button:has-text('Apply for this Job'), a:has-text('Apply for this Job')",
}

# System-field names already handled deterministically — the LLM mapping
# pass must not touch these.
_KNOWN_FIELD_NAMES = {"_systemfield_name", "_systemfield_email", "_systemfield_phone"}


class AshbyAdapter:
    name = "ashby"

    def matches(self, url: str, page_text: str) -> bool:
        return any(f in url for f in _ASHBY_URL_FRAGMENTS)

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
        log.adapter_chosen("ashby")
        filled = 0
        skipped = 0

        # ── Reveal the application form ────────────────────────────────────
        # Landing on the job URL shows the Overview tab; the form lives under
        # the "Application" tab (same SPA, /application route).
        await _open_application_tab(ctrl, log, job.url)

        # ── Standard fields ────────────────────────────────────────────────
        log.step("standard_fields")
        filled += await _fill(ctrl, log, _SEL["full_name"], profile.full_name, "full_name")
        filled += await _fill(ctrl, log, _SEL["email"],     profile.email,     "email")
        filled += await _fill(ctrl, log, _SEL["phone"],     profile.phone,     "phone")

        # ── Resume upload ──────────────────────────────────────────────────
        # Uploaded first-ish: Ashby parses the resume and may autofill fields,
        # and _fill skips fields that already hold a value.
        log.step("file_upload")
        if job.resume_path and Path(job.resume_path).exists():
            ok = await _upload(ctrl, log, _SEL["resume"], job.resume_path, "resume")
            filled += int(ok)
            if ok:
                # Give Ashby's resume parser a moment to autofill fields
                try:
                    await ctrl.page.wait_for_timeout(1500)
                except Exception:
                    pass

        # ── Custom questions (DOM snapshot + LLM mapping) ──────────────────
        log.step("custom_questions")
        filled_custom, skipped_custom = await _fill_custom_questions(
            ctrl, log, profile, job, llm
        )
        filled  += filled_custom
        skipped += skipped_custom

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

        # ── Mode gate ──────────────────────────────────────────────────────
        if mode == ApplyMode.AUTOFILL:
            log.result(True, "autofill_complete", filled, skipped, tier=1, page_count=1)
            return ApplyResult(
                success=True,
                status=ApplyStatus.AUTOFILL_COMPLETE,
                message=f"Ashby form filled ({filled} fields). Submit manually.",
                adapter_used=self.name,
                fields_filled=filled,
                fields_skipped=skipped,
                log_entries=log.entries,
            )

        # ── Submit ─────────────────────────────────────────────────────────
        log.step("submit")
        submitted = await ctrl.click(_SEL["submit"])
        screenshot = ""
        if submitted:
            try:
                await ctrl.page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            page_text = await ctrl.page_text()
            if any(kw in page_text.lower() for kw in ("thank you", "submitted", "received", "confirmation")):
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

async def _open_application_tab(ctrl, log, url: str) -> None:
    """Switch the Ashby SPA to the Application tab if the form isn't visible.

    Landing on /application directly renders the form; landing on the job
    overview requires a tab click. Never raises — if the tab can't be found
    the field fills below will simply no-op and the run ends in NEEDS_REVIEW.
    """
    try:
        # Form already rendered? (email field is always present on the form)
        el = ctrl.page.locator(_SEL["email"]).first
        if await el.is_visible(timeout=2000):
            return
    except Exception:
        pass

    try:
        tab = ctrl.page.locator(_SEL["apply_tab"]).first
        if await tab.is_visible(timeout=3000):
            await tab.click()
            log.step("application_tab_opened")
            try:
                await ctrl.page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
    except Exception:
        logger.debug("Ashby: could not open Application tab for %s", url, exc_info=True)


async def _fill(ctrl, log, selector: str, value: str, label: str) -> int:
    """Fill a single field; return 1 on success, 0 otherwise.

    Skips the field if it already contains a non-empty value so we never
    overwrite content Ashby autofilled from the parsed resume.
    """
    if not value:
        return 0
    try:
        el = ctrl.page.locator(selector).first
        if not await el.is_visible(timeout=3000):
            return 0
        try:
            current = ((await el.input_value()) or "").strip()
            if current:
                return 0
        except Exception:
            pass
        await el.click(click_count=3)  # select-all before typing
        await el.type(value, delay=25)
        log.field(label, value, source="profile", confidence=1.0)
        return 1
    except Exception:
        return 0


async def _upload(ctrl, log, selector: str, file_path: str, label: str) -> bool:
    try:
        el = ctrl.page.locator(selector).first
        await el.set_input_files(file_path)
        log.field(label, file_path, action="upload", source="profile", confidence=1.0)
        return True
    except Exception as exc:
        log.warning(f"Upload failed for {label}: {exc}")
        return False


async def _fill_custom_questions(ctrl, log, profile, job, llm) -> tuple[int, int]:
    """
    Use the generic field-detector to handle the custom questions that appear
    below the standard Ashby fields.

    Returns (filled_count, skipped_count).
    """
    fields = await ctrl.get_dom_snapshot()
    custom = [
        f for f in fields
        if f.field_type in ("text", "textarea", "number", "url", "email", "select", "radio", "checkbox")
        and f.name not in _KNOWN_FIELD_NAMES
        and f.field_id not in _KNOWN_FIELD_NAMES
        and "resume" not in f.label.lower()
    ]
    if not custom:
        return 0, 0

    from ..answer_bank import get_answer_bank
    bank = get_answer_bank()
    bank_dict = {
        k: v for k, v in bank.all_snippets().items()
        if not (v.startswith("<") and v.endswith(">"))
    }

    await wait_if_paused()
    mappings = await llm.map_fields_to_values(profile, job, custom, bank_dict)

    # Build id→label map for skip logging
    field_label_by_id = {f.field_id: (f.label or f.name or f.field_id) for f in custom}

    filled = skipped = 0
    for fv in mappings:
        await wait_if_paused()
        if fv.action == "skip" or not fv.value:
            skipped += 1
            label = field_label_by_id.get(fv.field_id, fv.field_id)
            log.warning(f"Ashby: skipped custom field {fv.field_id!r} label={label!r} conf={fv.confidence:.2f}")
            continue
        if fv.action == "select":
            ok = await ctrl.select_field(fv.field_id, fv.value)
        elif fv.action == "check":
            ok = await ctrl.check_field(fv.field_id)
        else:
            ok = await ctrl.fill_field(fv.field_id, fv.value)
        if ok:
            filled += 1
            log.field(fv.field_id, fv.value, source=fv.source, confidence=fv.confidence)
        else:
            skipped += 1
            label = field_label_by_id.get(fv.field_id, fv.field_id)
            log.warning(f"Ashby: fill failed for custom field {fv.field_id!r} label={label!r}")

    return filled, skipped


def _screenshot_path(job_id: str, suffix: str = "") -> str:
    base = project_root() / "failed_screenshots"
    base.mkdir(exist_ok=True)
    return str(base / f"{job_id}_ashby{'_' + suffix if suffix else ''}.png")
