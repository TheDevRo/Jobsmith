"""
auto_apply/adapters/greenhouse.py — Greenhouse ATS adapter.

Handles forms at:
  https://boards.greenhouse.io/<company>/jobs/<id>
  https://job-boards.greenhouse.io/<company>/jobs/<id>

Form structure (consistent across all Greenhouse boards):
  - First name / Last name / Email / Phone
  - Resume upload (required)
  - Cover letter upload (optional)
  - LinkedIn / website / other links
  - Custom questions (varies per company)
  - EEOC section (optional, company-controlled)
  - Submit button
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

_GREENHOUSE_HOSTS = ("boards.greenhouse.io", "job-boards.greenhouse.io")

# Known Greenhouse CSS selectors (stable across all boards as of 2025)
_SEL = {
    "first_name":    "#first_name",
    "last_name":     "#last_name",
    "email":         "#email",
    "phone":         "#phone",
    "resume":        "input[type='file'][id*='resume'], input[type='file'][name*='resume']",
    "cover_letter":  "input[type='file'][id*='cover_letter'], input[type='file'][name*='cover']",
    "linkedin":      "input[id*='linkedin'], input[name*='linkedin']",
    "website":       "input[id*='website'], input[name*='website'], input[id*='portfolio']",
    "github":        "input[id*='github'], input[name*='github']",
    "submit":        "#submit_app, input[type='submit'], button[type='submit']",
    # EEOC selectors
    "eeoc_gender":   "select[id*='gender']",
    "eeoc_race":     "select[id*='race']",
    "eeoc_veteran":  "select[id*='veteran']",
    "eeoc_disabled": "select[id*='disab']",
}


class GreenhouseAdapter:
    name = "greenhouse"

    def matches(self, url: str, page_text: str) -> bool:
        return any(h in url for h in _GREENHOUSE_HOSTS)

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
        log.adapter_chosen("greenhouse")
        filled = 0
        skipped = 0

        # ── Standard fields ────────────────────────────────────────────────
        log.step("standard_fields")

        # Split full_name into first / last
        name_parts = profile.full_name.strip().split(" ", 1)
        first = name_parts[0]
        last  = name_parts[1] if len(name_parts) > 1 else ""

        filled += await _fill(ctrl, log, _SEL["first_name"], first,   "first_name",  profile)
        filled += await _fill(ctrl, log, _SEL["last_name"],  last,    "last_name",   profile)
        filled += await _fill(ctrl, log, _SEL["email"],      profile.email, "email", profile)
        filled += await _fill(ctrl, log, _SEL["phone"],      profile.phone, "phone", profile)
        filled += await _fill(ctrl, log, _SEL["linkedin"],   profile.linkedin, "linkedin", profile)
        filled += await _fill(ctrl, log, _SEL["website"],    profile.portfolio or profile.github, "website", profile)
        filled += await _fill(ctrl, log, _SEL["github"],     profile.github, "github", profile)

        # ── File uploads ───────────────────────────────────────────────────
        log.step("file_uploads")
        if job.resume_path and Path(job.resume_path).exists():
            ok = await _upload(ctrl, log, _SEL["resume"], job.resume_path, "resume")
            filled += int(ok)
        if job.cover_letter_path and Path(job.cover_letter_path).exists():
            ok = await _upload(ctrl, log, _SEL["cover_letter"], job.cover_letter_path, "cover_letter")
            filled += int(ok)

        # ── EEOC / demographic fields ──────────────────────────────────────
        log.step("eeoc_fields")
        filled += await _select_if_exists(ctrl, log, _SEL["eeoc_gender"],   profile.gender or "Decline to self identify")
        filled += await _select_if_exists(ctrl, log, _SEL["eeoc_race"],     profile.race_ethnicity or "Decline to self identify")
        filled += await _select_if_exists(ctrl, log, _SEL["eeoc_veteran"],  profile.veteran_status or "I am not a protected veteran")
        filled += await _select_if_exists(ctrl, log, _SEL["eeoc_disabled"], profile.disability_status or "I don't wish to answer")

        # ── Custom questions (use LLM + generic field-detector) ───────────
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

        # ── Submit ─────────────────────────────────────────────────────────
        if mode == ApplyMode.AUTOFILL:
            log.result(True, "autofill_complete", filled, skipped, tier=1, page_count=1)
            return ApplyResult(
                success=True,
                status=ApplyStatus.AUTOFILL_COMPLETE,
                message=f"Greenhouse form filled ({filled} fields). Submit manually.",
                adapter_used=self.name,
                fields_filled=filled,
                fields_skipped=skipped,
                log_entries=log.entries,
            )

        log.step("submit")
        submitted = await ctrl.click(_SEL["submit"])
        screenshot = ""
        if submitted:
            try:
                await ctrl.page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            # Verify we got a confirmation page
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

async def _fill(ctrl, log, selector: str, value: str, label: str, profile) -> int:
    """Fill a single field; return 1 on success, 0 otherwise.

    Skips the field if it already contains a non-empty value to avoid
    overwriting pre-populated content (e.g. values carried in from a saved
    profile on the ATS side).
    """
    if not value:
        return 0
    try:
        el = ctrl.page.locator(selector).first
        if not await el.is_visible(timeout=3000):
            return 0
        # Skip if already filled — do not overwrite pre-populated values
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


async def _select_if_exists(ctrl, log, selector: str, value: str) -> int:
    """Select a value from a <select>; return 1 if element exists and we selected, 0 otherwise."""
    try:
        el = ctrl.page.locator(selector).first
        if not await el.is_visible(timeout=2000):
            return 0
        try:
            await el.select_option(label=value, timeout=3000)
        except Exception:
            # Try partial match
            options = await el.evaluate("el => Array.from(el.options).map(o => o.text)")
            v_lower = value.lower()
            match = next((o for o in options if v_lower in o.lower()), None)
            if match:
                await el.select_option(label=match, timeout=3000)
            else:
                return 0
        log.field(selector.split("[")[0], value, source="profile", confidence=1.0, action="select")
        return 1
    except Exception:
        return 0


async def _fill_custom_questions(ctrl, log, profile, job, llm) -> tuple[int, int]:
    """
    Use the generic field-detector to handle any custom questions that appear
    below the standard Greenhouse fields.

    Returns (filled_count, skipped_count).
    """
    from ..answer_bank import get_answer_bank

    fields = await ctrl.get_dom_snapshot()
    # Filter to only unfilled text/textarea fields (skip known-filled selectors)
    known_ids = {"first_name", "last_name", "email", "phone"}
    custom = [
        f for f in fields
        if f.field_type in ("text", "textarea", "number", "url", "email")
        and f.name not in known_ids
        and "resume" not in f.label.lower()
    ]
    if not custom:
        return 0, 0

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
            log.warning(f"Greenhouse: skipped custom field {fv.field_id!r} label={label!r} conf={fv.confidence:.2f}")
            continue
        ok = await ctrl.fill_field(fv.field_id, fv.value)
        if ok:
            filled += 1
            log.field(fv.field_id, fv.value, source=fv.source, confidence=fv.confidence)
        else:
            skipped += 1
            label = field_label_by_id.get(fv.field_id, fv.field_id)
            log.warning(f"Greenhouse: fill failed for custom field {fv.field_id!r} label={label!r}")

    return filled, skipped


def _screenshot_path(job_id: str, suffix: str = "") -> str:
    base = project_root() / "failed_screenshots"
    base.mkdir(exist_ok=True)
    return str(base / f"{job_id}_greenhouse{'_' + suffix if suffix else ''}.png")
