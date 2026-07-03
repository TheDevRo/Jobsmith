"""
auto_apply/adapters/indeed.py — Indeed Easy Apply adapter.

Flow
----
1. Verify session at indeed.com (session already loaded by BrowserController).
2. Navigate to job URL, dismiss overlays, click the Apply button.
3. Multi-page loop (≤ 15 pages):
   - DOM snapshot → FieldDescriptor list via BrowserController
   - LLM field mapping → fill regular fields
   - File uploads (resume / cover letter)
   - check_page_errors() before advancing
   - _detect_next_button() to advance pages
4. Submit via "Submit" / "Submit your application" button.
5. Verify confirmation text; return SUBMITTED or NEEDS_REVIEW.

URL patterns handled:
  https://www.indeed.com/jobs?vjk=...   (job listing page)
  https://smartapply.indeed.com/...     (post-redirect apply form)

Session loading
---------------
The saved session (data/indeed_session/storage_state.json) is passed to
BrowserController via storage_state_path, which passes it to Playwright's
new_context(storage_state=...) before any navigation.  This follows the
CLAUDE.md pattern exactly:

    browser = await browser_type.launch(**launch_kwargs)
    ctx = await browser.new_context(storage_state=str(path))

This ensures the very first ctrl.navigate(job_req.url) call in the
orchestrator is already authenticated.  _load_indeed_session() now only
checks that the file exists and warns if missing — no cookie injection needed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..models import ApplyMode, ApplyResult, ApplyStatus
from ..utils.browser_helpers import check_page_errors, take_failure_screenshot, wait_if_paused
from ...paths import project_root

if TYPE_CHECKING:
    from ..browser_controller import BrowserController
    from ..llm_client import LLMClient
    from ..logger import AutoApplyLogger
    from ..models import FieldDescriptor, JobApplicationRequest, UserProfile

logger = logging.getLogger(__name__)

_INDEED_HOSTS = ("indeed.com", "smartapply.indeed.com", "m.indeed.com")
_MAX_PAGES = 15

# ---------------------------------------------------------------------------
# Selector constants
# ---------------------------------------------------------------------------

# Tried in order — first visible match is clicked
_APPLY_BTN_SELECTORS = [
    "button[aria-label*='Apply']",
    "a[aria-label*='Apply']",
    "[data-testid='indeedApplyButton']",
    "button:has-text('Apply now')",
    "a:has-text('Apply now')",
    "button:has-text('Continue your application')",
    "a:has-text('Continue your application')",
]

_INDEED_SUBMIT_SELECTORS = [
    "button:has-text('Submit your application')",
    "button[data-testid='submit-button']",
    "button:has-text('Submit')",
    "input[type='submit']",
]

# Positive indicators that the user is signed in to indeed.com
_SIGNED_IN_SELECTORS = [
    "[data-testid='gnav-user-profile']",
    "[data-testid='user-avatar']",
    "[aria-label='Account menu']",
    "[data-gnav-id='user-profile']",
    "a[href*='/profile']",
]

# Page-text fragments that confirm a completed submission
_CONFIRM_PHRASES = (
    "application was submitted",
    "applied to",
    "your application has been",
    "application submitted",
    "successfully applied",
)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class IndeedEasyApplyAdapter:
    name = "indeed"

    def matches(self, url: str, page_text: str) -> bool:
        # "indeed.com" is a substring of both smartapply.indeed.com and
        # m.indeed.com, so a single string check would cover all three.
        # All three are listed explicitly so the intent is self-documenting:
        #   indeed.com            — standard desktop job listings
        #   smartapply.indeed.com — post-redirect apply form
        #   m.indeed.com          — mobile redirects
        return any(h in url for h in _INDEED_HOSTS)

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
        log.adapter_chosen("indeed")
        filled = 0
        skipped = 0

        # ── Session loading + pre-flight verification ──────────────────────
        # Check for a saved Indeed session.  Session verification is a soft
        # warning only — "Apply on company site" jobs redirect to an external
        # ATS and require no Indeed authentication.  We only hard-stop on a
        # missing session if the job turns out to be native Indeed Easy Apply
        # (caught later by the auth-wall check after clicking Apply).
        session_loaded = await _load_indeed_session(ctrl)
        if session_loaded:
            log.step("verify_indeed_session")
            session_ok = await _verify_indeed_session(ctrl)
            if not session_ok:
                log.warning(
                    "Indeed session check failed — proceeding; "
                    "will detect auth wall if Easy Apply requires login"
                )
            # _verify_indeed_session navigated to indeed.com — go back to job URL
            await ctrl.navigate(job.url)
        else:
            log.step("verify_indeed_session_skipped")
            log.warning(
                "No Indeed session found — proceeding without session "
                "(OK for external-ATS jobs; Easy Apply will show auth wall)"
            )

        # ── Dismiss overlays ───────────────────────────────────────────────
        dismissed = await ctrl.dismiss_popups()
        if dismissed:
            log.step("popup_dismissed")
            await ctrl.page.wait_for_timeout(500)

        # ── Detect and click Apply button ──────────────────────────────────
        log.step("click_apply")
        apply_clicked = await _click_apply_button(ctrl)
        if apply_clicked:
            # Brief pause so any new tab or same-tab navigation can begin
            await ctrl.page.wait_for_timeout(1000)

            # ── Redirect detection ─────────────────────────────────────────
            # Indeed sometimes opens external ATS portals in a new tab, or
            # navigates the current tab away from indeed.com.  Mirror the
            # LinkedIn external-redirect pattern exactly.
            dest_url = await _detect_redirect(ctrl)
            if dest_url:
                from . import ALL_ADAPTERS
                ext_adapter = next(
                    (a for a in ALL_ADAPTERS if a.name != "indeed" and a.matches(dest_url, "")),
                    ALL_ADAPTERS[-1],
                )
                log.step("external_apply_redirect", page_url=dest_url)
                logger.info(
                    "Indeed: external apply redirect → %s (adapter=%s)",
                    dest_url[:80], ext_adapter.name,
                )
                return await ext_adapter.apply(ctrl, profile, job, llm, mode, log)

            # No redirect — wait for native Indeed form to load
            try:
                await ctrl.page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            await ctrl.page.wait_for_timeout(500)
        else:
            # smartapply.indeed.com may open directly on the form — check
            fields_check = await ctrl.get_dom_snapshot()
            if not fields_check:
                screenshot = await take_failure_screenshot(ctrl.page, f"{job.job_id}_indeed")
                log.warning("Indeed: no Apply button found and no form fields detected")
                log.result(False, "needs_review", 0, 0, tier=2, page_count=0, screenshot_path=screenshot)
                return ApplyResult(
                    success=False,
                    status=ApplyStatus.NEEDS_REVIEW,
                    message="Indeed: could not locate Apply button",
                    screenshot_path=screenshot,
                    manual_url=job.url,
                    adapter_used=self.name,
                    fields_filled=0,
                    fields_skipped=0,
                    log_entries=log.entries,
                )
            log.step("form_already_open")

        # ── Auth guard after apply click ───────────────────────────────────
        if await _is_auth_wall(ctrl):
            screenshot = await take_failure_screenshot(ctrl.page, f"{job.job_id}_indeed")
            log.warning("Indeed: auth wall after apply click — session may have expired")
            log.result(False, "needs_review", 0, 0, tier=2, page_count=1, screenshot_path=screenshot)
            return ApplyResult(
                success=False,
                status=ApplyStatus.NEEDS_REVIEW,
                message="Indeed session expired — please reconnect via settings",
                screenshot_path=screenshot,
                manual_url=job.url,
                adapter_used=self.name,
                fields_filled=0,
                fields_skipped=0,
                log_entries=log.entries,
            )

        # ── Load answer bank ───────────────────────────────────────────────
        from ..answer_bank import get_answer_bank
        bank = get_answer_bank()
        bank_dict = {
            k: v for k, v in bank.all_snippets().items()
            if not (v.startswith("<") and v.endswith(">"))
        }

        # ── Multi-page loop ────────────────────────────────────────────────
        from .generic import _detect_next_button

        page_num = 1
        skipped_field_labels: list[str] = []  # (field_id, label) for every skipped field
        all_page_errors: list[str] = []        # accumulated validation errors across pages

        while True:
            await wait_if_paused()
            log.step(f"page_{page_num}_dom_snapshot", page_url=ctrl.page.url)

            # 1. Snapshot fields (fresh each page)
            fields: list["FieldDescriptor"] = await ctrl.get_dom_snapshot()

            if not fields:
                if page_num == 1:
                    screenshot = await take_failure_screenshot(ctrl.page, f"{job.job_id}_indeed")
                    log.warning("Indeed: no form fields detected on page 1")
                    log.result(False, "needs_review", 0, 0, tier=2, page_count=page_num, screenshot_path=screenshot)
                    return ApplyResult(
                        success=False,
                        status=ApplyStatus.NEEDS_REVIEW,
                        message="Indeed: no form fields detected — manual review required",
                        screenshot_path=screenshot,
                        manual_url=job.url,
                        adapter_used=self.name,
                        fields_filled=0,
                        fields_skipped=0,
                        log_entries=log.entries,
                    )
                # Later pages with no fields may be confirmation or nav-only — proceed
                log.warning(f"Indeed: page {page_num} has no fields, checking for submit")

            upload_fields  = [f for f in fields if f.field_type == "file"]
            regular_fields = [f for f in fields if f.field_type != "file"]

            # Build id→label map for skip logging
            field_label_by_id = {f.field_id: (f.label or f.name or f.field_id) for f in regular_fields}

            # 2. LLM field mapping
            if regular_fields:
                log.step(f"page_{page_num}_llm_mapping")
                await wait_if_paused()
                mappings = await llm.map_fields_to_values(profile, job, regular_fields, bank_dict)

                # 3. Fill regular fields
                log.step(f"page_{page_num}_fill_fields")
                for fv in mappings:
                    await wait_if_paused()
                    if fv.action == "skip" or not fv.value:
                        skipped += 1
                        label = field_label_by_id.get(fv.field_id, fv.field_id)
                        skipped_field_labels.append(label)
                        log.warning(f"Indeed: skipped field {fv.field_id!r} label={label!r}")
                        continue
                    ok = await _apply_fv(ctrl, fv)
                    if ok:
                        filled += 1
                        log.field(
                            fv.field_id, fv.value,
                            action=fv.action,
                            source=fv.source,
                            confidence=fv.confidence,
                        )
                    else:
                        skipped += 1
                        label = field_label_by_id.get(fv.field_id, fv.field_id)
                        skipped_field_labels.append(label)
                        log.warning(f"Indeed: fill failed for field {fv.field_id!r} label={label!r}")

            # 4. File uploads
            if upload_fields:
                log.step(f"page_{page_num}_file_uploads")
                for uf in upload_fields:
                    uploaded = False
                    label_lower = uf.label.lower()
                    if "cover" in label_lower:
                        if job.cover_letter_path and Path(job.cover_letter_path).exists():
                            uploaded = await ctrl.upload_file(uf.field_id, job.cover_letter_path)
                            if uploaded:
                                log.field(uf.field_id, job.cover_letter_path, action="upload", source="profile")
                                filled += 1
                    else:  # resume / CV / unknown file input
                        if job.resume_path and Path(job.resume_path).exists():
                            uploaded = await ctrl.upload_file(uf.field_id, job.resume_path)
                            if uploaded:
                                log.field(uf.field_id, job.resume_path, action="upload", source="profile")
                                filled += 1
                    if not uploaded:
                        skipped += 1
                        skipped_field_labels.append(uf.label)
                        log.warning(f"File upload skipped for {uf.field_id} ({uf.label!r})")

            # 5. Page error check — collect but do not halt; attempt to advance anyway
            page_errors = await check_page_errors(ctrl.page)
            if page_errors:
                for err_msg in page_errors:
                    log.warning(f"Indeed page {page_num} validation error: {err_msg}")
                    all_page_errors.append(f"p{page_num}: {err_msg}")

            # 6. Page cap
            if page_num >= _MAX_PAGES:
                log.warning("Indeed: multi-page loop exceeded 15 pages")
                screenshot = await take_failure_screenshot(ctrl.page, f"{job.job_id}_indeed")
                log.result(False, "needs_review", filled, skipped, tier=2, page_count=page_num, screenshot_path=screenshot, skipped_field_names=skipped_field_labels)
                return ApplyResult(
                    success=False,
                    status=ApplyStatus.NEEDS_REVIEW,
                    message="Indeed: exceeded 15 pages — manual review required",
                    screenshot_path=screenshot,
                    manual_url=job.url,
                    adapter_used=self.name,
                    fields_filled=filled,
                    fields_skipped=skipped,
                    skipped_field_names=skipped_field_labels,
                    log_entries=log.entries,
                )

            # 7. Check for submit button — if visible, exit loop to submit
            if await _is_submit_visible(ctrl):
                break

            # 8. Next-page button — advance if present
            next_btn = await _detect_next_button(ctrl.page)
            if next_btn is not None:
                log.step(f"page_{page_num}_next_click")
                try:
                    await next_btn.click()
                    await ctrl.page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                page_num += 1
                log.step(f"page_{page_num}_start", page_url=ctrl.page.url)
                continue

            # No next button and no submit — break and attempt submit anyway
            break
        # ── end multi-page loop ────────────────────────────────────────────

        # Log skipped field summary
        if skipped_field_labels:
            log.warning(
                f"Indeed: {len(skipped_field_labels)} field(s) skipped across {page_num} page(s): "
                + ", ".join(skipped_field_labels)
            )

        # ── Mode gate ──────────────────────────────────────────────────────
        if mode == ApplyMode.AUTOFILL:
            skip_note = (
                f" {len(skipped_field_labels)} field(s) skipped: {', '.join(skipped_field_labels)}."
                if skipped_field_labels else ""
            )
            log.result(True, "autofill_complete", filled, skipped, tier=2, page_count=page_num, skipped_field_names=skipped_field_labels)
            return ApplyResult(
                success=True,
                status=ApplyStatus.AUTOFILL_COMPLETE,
                message=f"Indeed form filled ({filled} fields, {page_num} page(s)).{skip_note} Submit manually.",
                adapter_used=self.name,
                fields_filled=filled,
                fields_skipped=skipped,
                skipped_field_names=skipped_field_labels,
                log_entries=log.entries,
            )

        # ── Submit ─────────────────────────────────────────────────────────
        log.step("submit")
        submit_clicked = await _click_indeed_submit(ctrl)
        if not submit_clicked:
            screenshot = await take_failure_screenshot(ctrl.page, f"{job.job_id}_indeed")
            log.warning("Indeed: submit button not found")
            log.result(False, "needs_review", filled, skipped, tier=2, page_count=page_num, screenshot_path=screenshot, skipped_field_names=skipped_field_labels)
            return ApplyResult(
                success=False,
                status=ApplyStatus.NEEDS_REVIEW,
                message="Indeed: could not locate Submit button — manual review required",
                screenshot_path=screenshot,
                manual_url=job.url,
                adapter_used=self.name,
                fields_filled=filled,
                fields_skipped=skipped,
                skipped_field_names=skipped_field_labels,
                log_entries=log.entries,
            )

        try:
            await ctrl.page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        # ── Verify submission ──────────────────────────────────────────────
        page_text = await ctrl.page_text()
        confirmed = any(phrase in page_text.lower() for phrase in _CONFIRM_PHRASES)
        suffix = "submitted" if confirmed else "unconfirmed"
        screenshot = await take_failure_screenshot(ctrl.page, f"{job.job_id}_indeed_{suffix}")

        skip_note = (
            f" ({len(skipped_field_labels)} field(s) skipped: {', '.join(skipped_field_labels)})"
            if skipped_field_labels else ""
        )

        if confirmed:
            log.result(True, "submitted", filled, skipped, tier=2, page_count=page_num, screenshot_path=screenshot, skipped_field_names=skipped_field_labels)
            return ApplyResult(
                success=True,
                status=ApplyStatus.SUBMITTED,
                message=f"Application submitted: {job.title} at {job.company}{skip_note}",
                screenshot_path=screenshot,
                adapter_used=self.name,
                fields_filled=filled,
                fields_skipped=skipped,
                skipped_field_names=skipped_field_labels,
                log_entries=log.entries,
            )

        log.warning("Indeed: submit clicked but confirmation text not found")
        log.result(False, "needs_review", filled, skipped, tier=2, page_count=page_num, screenshot_path=screenshot, skipped_field_names=skipped_field_labels)
        return ApplyResult(
            success=False,
            status=ApplyStatus.NEEDS_REVIEW,
            message=f"Indeed: submit clicked but confirmation not detected — verify manually{skip_note}",
            screenshot_path=screenshot,
            manual_url=job.url,
            adapter_used=self.name,
            fields_filled=filled,
            fields_skipped=skipped,
            skipped_field_names=skipped_field_labels,
            log_entries=log.entries,
        )


# ---------------------------------------------------------------------------
# Field-filling dispatch
# ---------------------------------------------------------------------------

async def _apply_fv(ctrl: "BrowserController", fv) -> bool:
    """Dispatch a FieldValue to the correct BrowserController fill method."""
    if fv.action == "fill":
        return await ctrl.fill_field(fv.field_id, fv.value)
    elif fv.action == "select":
        return await ctrl.select_field(fv.field_id, fv.value)
    elif fv.action == "check":
        return await ctrl.check_field(fv.field_id)
    elif fv.action == "upload":
        return False  # handled separately in the upload_fields block
    return False


# ---------------------------------------------------------------------------
# Button helpers
# ---------------------------------------------------------------------------

async def _click_apply_button(ctrl: "BrowserController") -> bool:
    """
    Find and click the Indeed Apply button.

    Tries selectors in _APPLY_BTN_SELECTORS order: aria-label first
    (most stable), then text-based matches, then data-testid fallback.
    Returns True on first successful click, False if no button found.
    """
    for sel in _APPLY_BTN_SELECTORS:
        try:
            loc = ctrl.page.locator(sel).first
            if await loc.is_visible(timeout=2000):
                await loc.click()
                logger.info("Indeed: apply button clicked via selector %r", sel)
                return True
        except Exception:
            pass
    return False


async def _is_submit_visible(ctrl: "BrowserController") -> bool:
    """Return True if a submit button matching _INDEED_SUBMIT_SELECTORS is visible."""
    for sel in _INDEED_SUBMIT_SELECTORS:
        try:
            if await ctrl.page.locator(sel).first.is_visible(timeout=500):
                return True
        except Exception:
            pass
    return False


async def _click_indeed_submit(ctrl: "BrowserController") -> bool:
    """
    Click the submit button.  Tries _INDEED_SUBMIT_SELECTORS in order:
    "Submit your application" text first, then data-testid, then generic
    "Submit", then input[type='submit'] as final fallback.
    Returns True on first successful click.
    """
    for sel in _INDEED_SUBMIT_SELECTORS:
        try:
            loc = ctrl.page.locator(sel).first
            if await loc.is_visible(timeout=2000):
                await loc.click()
                logger.info("Indeed: submit clicked via selector %r", sel)
                return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# Redirect detection
# ---------------------------------------------------------------------------

async def _detect_redirect(ctrl: "BrowserController") -> str:
    """
    Check whether clicking Apply redirected the browser away from Indeed.

    Mirrors the LinkedIn external-ATS redirect pattern exactly.  Covers two
    cases:

    1. New tab / popup opened — ctrl.switch_to_new_page() detects the new tab,
       closes it, and navigates the existing warm-session page to the external
       URL (same behaviour as LinkedIn's external-apply handler).
    2. Same-tab navigation — the current tab left indeed.com without opening
       a new tab (e.g. a meta-refresh or JS window.location redirect).

    Returns the destination URL if an external redirect is detected (i.e. the
    destination is not an Indeed host), empty string otherwise.  Never raises.
    """
    try:
        # Case 1: new tab opened
        new_url = await ctrl.switch_to_new_page(timeout=3000)
        if new_url and not any(h in new_url for h in _INDEED_HOSTS):
            return new_url

        # Case 2: same-tab navigation away from Indeed
        current = ctrl.page.url
        if current and not any(h in current for h in _INDEED_HOSTS):
            return current
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

async def _load_indeed_session(ctrl: "BrowserController") -> bool:
    """
    Verify that the Indeed persistent profile session exists.

    The session is loaded via launch_persistent_context in BrowserController.launch()
    — orchestrator.py passes profile_dir=INDEED_CHROME_PROFILE_DIR when
    adapter.name == "indeed".  No cookie injection is needed here.

    Returns True if the login sentinel exists (session was loaded),
    False if missing (caller proceeds to verify and may see a login wall).
    """
    from .. import INDEED_CHROME_PROFILE_DIR, _INDEED_SENTINEL
    sentinel = INDEED_CHROME_PROFILE_DIR / _INDEED_SENTINEL
    if not sentinel.exists():
        logger.warning(
            "Indeed auto-apply: no session sentinel at %s — user may see login wall", sentinel
        )
        return False
    logger.info("Indeed: persistent profile session present at %s", INDEED_CHROME_PROFILE_DIR)
    return True


async def _verify_indeed_session(ctrl: "BrowserController") -> bool:
    """
    Navigate to indeed.com and confirm the session is active.

    Checks for:
      1. Absence of sign-in / auth-wall selectors (_is_auth_wall)
      2. Presence of at least one signed-in indicator (_SIGNED_IN_SELECTORS)
         or a non-login URL, as a fallback

    Returns True if the session appears active, False otherwise.
    """
    try:
        await ctrl.navigate("https://www.indeed.com")
        await ctrl.page.wait_for_timeout(1500)

        if await _is_auth_wall(ctrl):
            logger.info("Indeed session check: auth wall detected — session expired or missing")
            return False

        # Check for explicit signed-in DOM indicators
        for sel in _SIGNED_IN_SELECTORS:
            try:
                loc = ctrl.page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible(timeout=500):
                    logger.info("Indeed session check: signed-in indicator found (%s)", sel)
                    return True
            except Exception:
                pass

        # Fallback: no auth wall and URL doesn't look like a login page
        url = ctrl.page.url
        if "login" not in url and "signin" not in url:
            logger.info(
                "Indeed session check: no auth wall detected, assuming active (url=%s)", url[:80]
            )
            return True

        logger.info("Indeed session check: could not confirm session — url=%s", url[:80])
        return False
    except Exception as exc:
        logger.warning("Indeed session verification error: %s", exc)
        return False


async def _is_auth_wall(ctrl: "BrowserController") -> bool:
    """
    Return True if Indeed is showing a sign-in prompt or auth modal.

    Checks DOM selectors first (fast), then falls back to page-text scan.
    Never raises.
    """
    _AUTH_SELECTORS = [
        "[data-testid='signin-form']",
        "[data-testid='login-modal']",
        "form[action*='login']",
        "form[action*='signin']",
        "[class*='SignInModal']",
        "[class*='login-modal']",
    ]
    _AUTH_PHRASES = (
        "sign in to indeed",
        "sign in to apply",
        "log in to apply",
        "create an indeed account",
        "to apply for this job, please sign in",
    )
    try:
        for sel in _AUTH_SELECTORS:
            try:
                loc = ctrl.page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible(timeout=500):
                    return True
            except Exception:
                pass
        try:
            body = ((await ctrl.page.locator("body").text_content()) or "").lower()
            if any(phrase in body for phrase in _AUTH_PHRASES):
                return True
        except Exception:
            pass
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _screenshot_path(job_id: str, suffix: str = "") -> str:
    base = project_root() / "failed_screenshots"
    base.mkdir(exist_ok=True)
    return str(base / f"{job_id}_indeed{'_' + suffix if suffix else ''}.png")
