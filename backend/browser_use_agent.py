"""
browser_use_agent.py — Browser-Use Agent wrapper for autonomous job applications.

Wraps browser-use's Agent class to:
  - Reuse the existing LM Studio LLM configuration
  - Share a persistent browser context across applications
  - Handle resume file upload, multi-step forms, and CAPTCHA/MFA detection
  - Provide structured logging and screenshot-on-failure

This module is only activated when USE_BROWSER_USE=true.
"""

import json
import logging
import os
import signal
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from browser_use import Agent
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession
from browser_use.llm.openai.chat import ChatOpenAI

from . import session_manager
from .paths import project_root

logger = logging.getLogger(__name__)

# Global reference to the active BrowserSession so force_stop() can close it.
_active_browser_session: Optional["BrowserSession"] = None

PROJECT_ROOT = project_root()
FAILED_SCREENSHOTS_DIR = PROJECT_ROOT / os.getenv("FAILED_SCREENSHOTS_DIR", "failed_screenshots")
BROWSER_PROFILE_DIR = PROJECT_ROOT / os.getenv("BROWSER_PROFILE_DIR", ".browser-profile")


def _ensure_dirs():
    FAILED_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)


async def force_stop():
    """Kill the active Browser-Use session immediately (called by force-stop endpoint)."""
    global _active_browser_session
    session = _active_browser_session
    _active_browser_session = None
    if session is None:
        return
    try:
        # kill() terminates the browser process entirely (unlike stop() which keeps it alive)
        await session.kill()
    except Exception:
        try:
            browser = getattr(session, "browser", None)
            if browser:
                await browser.close()
        except Exception:
            pass


def _clear_stale_browser_state():
    """Remove lock files and kill orphaned Chromium processes that block CDP connections.

    When keep_alive=True and a previous session crashes, the profile directory
    retains lock files that prevent a new browser from attaching. This mirrors
    the _clear_browser_locks logic in auto_apply.py.
    """
    lock_names = ["SingletonLock", "SingletonSocket", "SingletonCookie", ".parentlock", "lock"]
    for name in lock_names:
        lock = BROWSER_PROFILE_DIR / name
        try:
            if lock.exists():
                lock.unlink()
                logger.info("Removed stale lock: %s", lock)
        except Exception:
            pass

    # Kill orphaned Chromium processes that reference our profile dir
    try:
        profile_str = str(BROWSER_PROFILE_DIR)
        result = subprocess.run(
            ["pgrep", "-f", profile_str],
            capture_output=True, text=True, timeout=5,
        )
        for pid_str in result.stdout.strip().split("\n"):
            pid_str = pid_str.strip()
            if pid_str and pid_str.isdigit():
                pid = int(pid_str)
                if pid != os.getpid():
                    os.kill(pid, signal.SIGTERM)
                    logger.info("Killed orphaned browser process: %d", pid)
    except Exception as e:
        logger.debug("Could not check for orphaned browser processes: %s", e)


# ---------------------------------------------------------------------------
# LLM setup — reuses existing LM Studio config
# ---------------------------------------------------------------------------

def _get_browser_use_llm(config: dict) -> ChatOpenAI:
    """Create a Browser-Use ChatOpenAI pointed at the same LM Studio endpoint.

    Uses the 'fast' tier model by default (same as ai_navigator), falling back
    to the top-level ai.model config.
    """
    ai_cfg = config.get("ai", {})
    default_url = ai_cfg.get("base_url", "http://localhost:1234/v1")
    default_key = ai_cfg.get("api_key", "lm-studio")

    # Prefer the fast-tier model (same one used by ai_navigator)
    tier_cfg = ai_cfg.get("models", {}).get("fast", {})
    model = tier_cfg.get("model") or ai_cfg.get("model", "local-model")
    base_url = tier_cfg.get("base_url", default_url)
    api_key = tier_cfg.get("api_key", default_key)

    return ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=ai_cfg.get("temperature", 0.7),
        max_completion_tokens=ai_cfg.get("max_tokens", 4096),
    )


# ---------------------------------------------------------------------------
# Browser session — persistent context shared across applications
# ---------------------------------------------------------------------------

def _get_browser_session(
    config: dict,
    domain: str | None = None,
    headless: bool | None = None,
) -> BrowserSession:
    """Create a BrowserSession with persistent profile and optional storageState.

    Parameters
    ----------
    config : dict
        Full app config (from config.yaml).
    domain : str, optional
        If provided and a saved session exists for this domain, loads its
        storageState for cookie/auth reuse.
    headless : bool, optional
        Override headless mode. If None, reads from BROWSER_HEADLESS env var.
    """
    _ensure_dirs()
    _clear_stale_browser_state()

    if headless is None:
        # config.yaml is the source of truth (controlled by the UI toggle).
        # Fall back to BROWSER_HEADLESS env var only when config has no value.
        cfg_val = config.get("auto_apply", {}).get("headless")
        if cfg_val is not None:
            headless = bool(cfg_val)
        else:
            env_val = os.getenv("BROWSER_HEADLESS")
            if env_val is not None:
                headless = env_val.lower() == "true"
            else:
                headless = True

    # Load saved session storageState if available.
    # session_manager is the primary source; fall back to the LinkedIn login
    # flow's storage path when Browser-Use hasn't saved its own linkedin.json yet.
    storage_state = None
    if domain and session_manager.has_session(domain):
        storage_state = str(session_manager.session_path(domain))
        logger.info("Loading saved session for domain: %s", domain)
    elif domain == "linkedin":
        from .auto_apply import LINKEDIN_SESSION_DIR, has_linkedin_session
        if has_linkedin_session():
            fallback = LINKEDIN_SESSION_DIR / "storage_state.json"
            if fallback.exists():
                storage_state = str(fallback)
                logger.info("Loading LinkedIn session from login-flow storage: %s", fallback)

    browser_profile = BrowserProfile(
        headless=headless,
        storage_state=storage_state,  # storage_state only — no user_data_dir (they conflict, cookies silently ignored)
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        keep_alive=True,
        # Workday and other ATS pages are slow to render — give them time
        minimum_wait_page_load_time=2.0,
        wait_for_network_idle_page_load_time=3.0,
        wait_between_actions=0.5,
    )
    return BrowserSession(browser_profile=browser_profile)


# ---------------------------------------------------------------------------
# Resume file handling — DOCX to PDF conversion
# ---------------------------------------------------------------------------

def _convert_docx_to_pdf(docx_path: str) -> str | None:
    """Convert a DOCX file to PDF for upload.

    Tries LibreOffice CLI first, falls back to returning None (caller will
    use DOCX directly).
    """
    if not docx_path or not Path(docx_path).exists():
        return None

    pdf_path = docx_path.rsplit(".", 1)[0] + ".pdf"

    # Try LibreOffice
    try:
        out_dir = str(Path(docx_path).parent)
        result = subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf", "--outdir", out_dir, docx_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and Path(pdf_path).exists():
            logger.info("Converted DOCX to PDF: %s", pdf_path)
            return pdf_path
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    logger.debug("PDF conversion unavailable — will use DOCX directly")
    return None


def _prepare_resume_file(application: dict, job_id: str) -> str | None:
    """Prepare the resume file for upload.

    Tries to convert the tailored DOCX to PDF in /tmp/. Falls back to the
    DOCX path if conversion isn't available.
    """
    docx_path = application.get("tailored_resume_path", "")
    if not docx_path or not Path(docx_path).exists():
        return None

    # Try PDF conversion
    tmp_pdf = f"/tmp/resume_{job_id}.pdf"
    pdf = _convert_docx_to_pdf(docx_path)
    if pdf:
        # Move/copy to standard temp location
        import shutil
        shutil.copy2(pdf, tmp_pdf)
        return tmp_pdf

    # Fall back to DOCX
    return docx_path


# ---------------------------------------------------------------------------
# Agent task prompt construction
# ---------------------------------------------------------------------------

def _build_task_prompt(
    job: dict,
    profile: dict,
    resume_file_path: str | None,
    is_linkedin_job: bool = False,
    mode: str = "autofill",
) -> str:
    """Build a compact navigation prompt for the Browser-Use agent.

    The navigator model handles page classification, button clicking, and
    form filling — it does NOT need the full resume/cover letter text.
    Those are generated by the strong model and uploaded as files.
    Only structured profile fields and a short summary are included.

    Parameters
    ----------
    mode : str
        "autofill" — fill every field, then STOP before clicking Submit.
        "submit"   — fill every field, then click Submit to complete the application.
        Defaults to "autofill" (safe default; explicit submit requires whitelist).
    """
    name = profile.get('full_name', '')
    first = name.split()[0] if name else ''
    last = ' '.join(name.split()[1:]) if name else ''

    file_line = f"Resume file to upload if prompted: {resume_file_path}" if resume_file_path else "No resume file available."

    # One-paragraph professional summary for open-ended text fields.
    # Pull from profile summary if present; otherwise build a minimal stub.
    summary = (profile.get('summary') or '').strip()
    if len(summary) > 600:
        summary = summary[:600].rsplit(' ', 1)[0] + '…'

    if is_linkedin_job:
        apply_instruction = (
            "Click the apply button on this LinkedIn job. "
            "Prefer 'Easy Apply' if present — it opens an in-page modal; fill each step and click Next/Continue, then Submit. "
            "If only a regular 'Apply' button is present, click it. "
            "A dialog may appear ('Share your profile' or 'You are leaving LinkedIn') — click the Continue or primary action button to proceed. "
            "If the application opens in a new tab, switch to that tab and complete the form there."
        )
        extra_rules = "Do NOT click 'Apply with LinkedIn' on external sites. Do NOT click OAuth/SSO buttons."
    elif "myworkdayjobs.com" in (job.get("url") or ""):
        apply_instruction = (
            "The page is already on the Workday application form (or sign-in page). "
            "Do NOT click any Apply/Apply Now button — the form is already open. "
            "If a sign-in form is present, fill in the credentials below and submit. "
            "Then fill every visible application field and advance through each step."
        )
        extra_rules = (
            "CRITICAL: Do NOT click Apply/Apply Now under any circumstances — you are already on the form. "
            "Do NOT click 'Apply with LinkedIn' or any OAuth/SSO buttons."
        )
    else:
        apply_instruction = ("Click the primary Apply/Apply Now button. "
                             "If it opens a dropdown, pick 'Apply' (not 'Apply Manually' or 'Use Last Application'). "
                             "On aggregator pages (Adzuna, Indeed) click the button that goes to the employer's site.")
        extra_rules = "Do NOT click 'Apply with LinkedIn', 'Easy Apply' on non-LinkedIn sites, or any OAuth/SSO buttons."

    # ── Mode-dependent step 8/9 and rule ──────────────────────────────────
    if mode == "autofill":
        mode_step_8 = (
            "8. STOP at the final page — do NOT click Submit, Send Application, or any equivalent button. "
            "Leave the form filled but unsubmitted."
        )
        mode_step_9 = (
            "9. Report exactly: 'AUTOFILL_COMPLETE — all fields filled. Manual submission required.'"
        )
        mode_rule = (
            "STOP BEFORE SUBMIT — your task ends when all visible fields are filled and the Submit "
            "button is visible. DO NOT click Submit under any circumstances."
        )
    else:
        mode_step_8 = "8. Review page: verify all fields are correct, then click Submit."
        mode_step_9 = (
            "9. Done when you see a confirmation (\"Application submitted\", \"Thank you\", etc.)."
        )
        mode_rule = (
            "After filling and verifying all fields, click Submit to complete the application."
        )

    return f"""You are a job application navigator. Complete this application using ONLY the candidate data below.

JOB: {job.get('title', '')} at {job.get('company', '')}

STEPS:
1. Page is already loaded. Wait for it to render fully.
2. {apply_instruction}
3. Follow redirects (LinkedIn → company ATS). Wait for each page to load.
4. Auth: try Sign In with credentials below. If that fails, create an account. Stop only for SSO-only or email-verification walls.
5. Fill every visible field using ONLY the candidate data. Leave unknown fields blank or choose "Decline to answer".
6. Multi-step forms: complete each page then click Next/Continue/Save and Continue.
7. File upload: upload the resume file when prompted (file chooser is handled automatically).
{mode_step_8}
{mode_step_9}

RULES:
- NEVER fabricate data. Use only what's provided.
- Stop and report: CAPTCHA, MFA codes, SSO-only login, email-verification walls.
- {mode_rule}
- {extra_rules}
- Don't click nav links, job alerts, videos, or marketing elements.
- {file_line}

CANDIDATE DATA:
Name: {name}
First: {first} | Last: {last} | Middle: {profile.get('middle_name', '')}
Email: {profile.get('email', '')}
Phone: {profile.get('phone', '')}
Address: {profile.get('street_address', '')}, {profile.get('city', '')}, {profile.get('state', '')} {profile.get('zip_code', '')}
LinkedIn: {profile.get('linkedin', '')}
Portfolio: {profile.get('portfolio', '')}
Work Auth: {profile.get('work_authorization', '')} | Sponsorship: {profile.get('sponsorship_required', '')}
Salary: {profile.get('desired_salary', '')}
Gender: {profile.get('gender', '')} | Race: {profile.get('race_ethnicity', '')}
Veteran: {profile.get('veteran_status', '')} | Disability: {profile.get('disability_status', '')}
Login email: {profile.get('workday_email', profile.get('email', ''))}
Login password: {profile.get('workday_password', '')}

PROFESSIONAL SUMMARY (use for open-ended text boxes about experience/background):
{summary if summary else 'Not provided — leave open-ended fields blank.'}"""


# ---------------------------------------------------------------------------
# Sensitive data mapping for Browser-Use
# ---------------------------------------------------------------------------

def _build_sensitive_data(profile: dict) -> dict[str, str]:
    """Build the sensitive_data dict for Browser-Use Agent.

    Browser-Use redacts these values from logs/screenshots and uses them
    for secure form filling.
    """
    data = {}
    field_map = {
        "email": "email",
        "phone": "phone",
        "street_address": "street_address",
        "zip_code": "zip_code",
        "desired_salary": "desired_salary",
        "workday_email": "workday_email",
        "workday_password": "workday_password",
    }
    for key, profile_key in field_map.items():
        val = profile.get(profile_key, "")
        if val:
            data[key] = str(val)
    return data


# ---------------------------------------------------------------------------
# LinkedIn pre-apply: deterministic Playwright hook before browser-use agent
# ---------------------------------------------------------------------------

async def _linkedin_pre_apply(page) -> str:
    """
    Deterministically handle the LinkedIn Apply button and any interstitial
    dialog (Share your profile / Leave LinkedIn) using Playwright selectors —
    no LLM involved.

    Returns the URL the browser is on after the interaction:
    - Same linkedin.com URL  → Easy Apply modal is open; let the agent handle it
    - External URL           → new tab closed, existing page navigated there; agent fills form
    - Same linkedin.com URL  → button not found or dialog not handled; agent will retry
    """
    import re as _re
    import asyncio as _asyncio

    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass

    # Scope the apply button to the job detail panel to avoid matching a job
    # card in LinkedIn's left-pane list.  The detail panel lives inside
    # .jobs-unified-top-card or .job-view-layout; fall back to page-level only
    # if neither container is present.
    _detail_containers = [
        ".jobs-unified-top-card",
        ".job-view-layout",
        ".jobs-details__main-content",
        "main",
    ]
    apply_btn = None
    for container_sel in _detail_containers:
        try:
            container = page.locator(container_sel).first
            if await container.is_visible(timeout=1_000):
                candidate = container.locator("button[aria-label*='Apply' i]").first
                if await candidate.is_visible(timeout=1_000):
                    apply_btn = candidate
                    logger.info("LinkedIn pre-apply: found apply button inside %r", container_sel)
                    break
        except Exception:
            continue

    # Fallback: page-level search with aria-label filter (never bare .jobs-apply-button
    # which matches leftmost card in the split-pane list)
    if apply_btn is None:
        try:
            apply_btn = page.locator("button[aria-label*='Apply' i]").first
            if not await apply_btn.is_visible(timeout=2_000):
                apply_btn = None
        except Exception:
            apply_btn = None

    if apply_btn is None:
        logger.info("LinkedIn pre-apply: apply button not visible — leaving for agent")
        return page.url

    # Determine if this is Easy Apply (modal) or regular Apply (new tab)
    try:
        btn_label = (await apply_btn.get_attribute("aria-label") or "").lower()
        btn_text  = (await apply_btn.inner_text()).strip().lower()
    except Exception:
        btn_label = btn_text = ""

    if "easy apply" in btn_label or "easy apply" in btn_text:
        # Easy Apply opens an in-page modal — let browser-use handle the full wizard
        logger.info("LinkedIn pre-apply: Easy Apply button detected — deferring to agent")
        return page.url

    # Regular Apply — click it and handle the interstitial
    logger.info("LinkedIn pre-apply: clicking regular Apply button")
    await apply_btn.click()

    # Wait for any modal/dialog to appear before attempting to click it
    try:
        await page.wait_for_selector(
            "[role='dialog'], .artdeco-modal, [data-test-modal]",
            timeout=5_000,
        )
    except Exception:
        pass  # no dialog appeared — may have opened a new tab directly

    # Try to click the Continue / primary action button in the dialog.
    # Use role-based and text-based locators first — they are resilient to
    # LinkedIn's frequent CSS class name changes.  CSS class selectors are
    # kept as a last-resort fallback only.
    _dialog_locators = [
        # Role-based: matches button by accessible name regardless of class
        lambda p: p.get_by_role("button", name=_re.compile(r"continue", _re.IGNORECASE)).first,
        # Text-based: catches "Continue to apply", "Continue to site", etc.
        lambda p: p.get_by_text(_re.compile(r"^continue", _re.IGNORECASE), exact=False).first,
        # Aria-label with case-insensitive CSS attribute selector
        lambda p: p.locator("button[aria-label*='continue' i]").first,
        # CSS class fallbacks (last resort — LinkedIn changes these frequently)
        lambda p: p.locator(".artdeco-modal .artdeco-button--primary").first,
        lambda p: p.locator(".jobs-apply-header__confirm-dialog .artdeco-button--primary").first,
    ]
    dialog_dismissed = False
    for get_locator in _dialog_locators:
        try:
            btn = get_locator(page)
            if await btn.is_visible(timeout=1_500):
                await btn.click()
                logger.info("LinkedIn pre-apply: clicked dialog button via %r", get_locator)
                await page.wait_for_timeout(1_500)
                dialog_dismissed = True
                break
        except Exception:
            continue

    # Check if a new tab/popup opened
    context = page.context
    pages_now = context.pages
    if len(pages_now) > 1:
        new_page = pages_now[-1]

        # Wait only for URL commit — do NOT wait for domcontentloaded, which allows
        # LinkedIn's redirect-page JS to run and can invalidate the shared session.
        try:
            await new_page.wait_for_load_state("commit", timeout=5_000)
        except Exception:
            await _asyncio.sleep(0.5)

        target_url = new_page.url or ""

        # If still on a LinkedIn URL, wait for the redirect to complete so we
        # capture the final external-ATS URL before closing the tab.
        if "linkedin.com" in target_url or not target_url or target_url == "about:blank":
            try:
                await new_page.wait_for_url(
                    lambda u: bool(u) and "linkedin.com" not in u and u != "about:blank",
                    timeout=10_000,
                )
                target_url = new_page.url or target_url
            except Exception:
                target_url = new_page.url or target_url

        # Always close the new tab — prevents concurrent-session detection and
        # stops the agent from seeing two tabs and clicking Apply a second time.
        try:
            await new_page.close()
        except Exception:
            pass

        logger.info("LinkedIn pre-apply: new tab closed, target URL = %s", target_url[:80])
        return target_url

    # If dialog was not dismissed and no new tab opened, capture a screenshot
    # so we can inspect the actual DOM and improve selectors.
    if not dialog_dismissed:
        try:
            import datetime as _dt
            ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            shot_path = str(FAILED_SCREENSHOTS_DIR / f"linkedin_dialog_stuck_{ts}.png")
            FAILED_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=shot_path)
            logger.warning(
                "LinkedIn pre-apply: dialog not dismissed — screenshot saved to %s", shot_path
            )
        except Exception as e:
            logger.debug("LinkedIn pre-apply: could not save dialog screenshot: %s", e)

    return page.url


# ---------------------------------------------------------------------------
# Workday pre-apply: deterministic Apply click before Browser-Use agent starts
# ---------------------------------------------------------------------------

async def _workday_pre_apply(page) -> str:
    """Deterministically handle the Workday Apply button before the agent starts.

    Workday job listing pages have an Apply button that opens the application
    form in a new tab.  Without this hook, the Browser-Use agent keeps clicking
    Apply on the listing page, spawning an endless stream of new tabs.

    Returns the URL of the application form (may be the same URL if already on
    the form, or if no Apply button was found).
    """
    import asyncio as _asyncio

    current_url = page.url

    # Already on the application form — nothing to do
    if "/apply" in current_url:
        logger.info("Workday pre-apply: already on application form")
        return current_url

    try:
        await page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass

    # Workday's primary apply button
    _apply_selectors = [
        "[data-automation-id='jobPostingApply']",
        "a[data-automation-id='jobPostingApply']",
        "button[data-automation-id*='apply' i]",
        "a[href*='/apply']",
    ]

    apply_el = None
    for sel in _apply_selectors:
        try:
            candidate = page.locator(sel).first
            if await candidate.is_visible(timeout=2_000):
                apply_el = candidate
                logger.info("Workday pre-apply: found Apply via %r", sel)
                break
        except Exception:
            continue

    if apply_el is None:
        logger.info("Workday pre-apply: no Apply button found — leaving for agent")
        return current_url

    # Click and wait for a new tab OR same-tab navigation
    form_url = current_url
    try:
        ctx = page.context
        pages_before = set(id(p) for p in ctx.pages)
        await apply_el.click()
        await _asyncio.sleep(2)

        new_pages = [p for p in ctx.pages if id(p) not in pages_before]
        if new_pages:
            new_page = new_pages[-1]
            try:
                await new_page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                await _asyncio.sleep(1)
            form_url = new_page.url or current_url
            await new_page.close()
            if form_url and form_url != current_url:
                await page.goto(form_url, wait_until="domcontentloaded", timeout=15_000)
                logger.info("Workday pre-apply: closed new tab, navigated to form %s", form_url[:80])
        elif page.url != current_url:
            form_url = page.url
            logger.info("Workday pre-apply: same-tab navigation to %s", form_url[:80])
    except Exception as e:
        logger.debug("Workday pre-apply: click handling failed (non-fatal): %s", e)

    return form_url


# ---------------------------------------------------------------------------
# Failure screenshot helper
# ---------------------------------------------------------------------------

async def _save_failure_screenshot(browser_session: BrowserSession, job_id: str) -> str | None:
    """Capture a screenshot on failure and save to failed_screenshots/."""
    _ensure_dirs()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = FAILED_SCREENSHOTS_DIR / f"{job_id}_{timestamp}_failure.png"
    # Try get_current_page() first, then fall back to iterating context pages
    for _attempt in range(2):
        try:
            if _attempt == 0:
                page = await browser_session.get_current_page()
            else:
                # Fallback: grab whichever page is open in the browser context
                ctx = getattr(browser_session, "context", None) or getattr(browser_session, "_context", None)
                pages = ctx.pages if ctx else []
                page = pages[-1] if pages else None
            if page:
                await page.screenshot(path=str(path), full_page=False)
                logger.info("Failure screenshot saved: %s", path)
                return str(path)
        except Exception as e:
            logger.warning("Failure screenshot attempt %d failed: %s", _attempt + 1, e)
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_browser_use_apply(
    job: dict,
    application: dict,
    profile: dict,
    config: dict,
) -> dict:
    """Execute a job application using the Browser-Use Agent.

    This is the main entry point called by auto_apply_job() when
    USE_BROWSER_USE is enabled.

    Parameters
    ----------
    job : dict
        Job data (id, title, company, url, source, description).
    application : dict
        Application data (resume_content, cover_letter_content,
        tailored_resume_path, etc.).
    profile : dict
        Candidate profile from config.yaml.
    config : dict
        Full app config.

    Returns
    -------
    dict
        Keys: success (bool), message (str), db_status (str), screenshot_path
        (str|None), block_reason (str), manual_url (str|None).
        Matches the shape of ApplyResult.to_legacy_dict() from the orchestrator path.
    """
    job_id = job.get("id", "unknown")
    job_title = job.get("title", "Unknown")
    company = job.get("company", "Unknown")
    url = job.get("url", "")

    logger.info(
        "Browser-Use apply starting: %s at %s (job_id=%s, url=%s)",
        job_title, company, job_id, url[:80],
    )

    # Determine domain for session loading
    domain = session_manager._domain_key(url) if url else None
    is_linkedin_job = domain == "linkedin"

    # Prepare resume file
    resume_file_path = _prepare_resume_file(application, job_id)

    # Read mode from config — enforced here so the task prompt matches the UI setting
    apply_mode = config.get("auto_apply", {}).get("mode", "autofill")

    # Build components
    llm = _get_browser_use_llm(config)
    browser_session = _get_browser_session(config, domain=domain)
    task_prompt = _build_task_prompt(
        job=job,
        profile=profile,
        resume_file_path=resume_file_path,
        is_linkedin_job=is_linkedin_job,
        mode=apply_mode,
    )

    sensitive_data = _build_sensitive_data(profile)
    step_ceiling = config.get("auto_apply", {}).get("step_ceiling", None)
    if step_ceiling is not None:
        max_steps = step_ceiling if step_ceiling > 0 else 999999
    else:
        max_steps = int(os.getenv("BROWSER_USE_MAX_STEPS", "50"))

    # Prepare available file paths for resume upload
    available_files = [resume_file_path] if resume_file_path else None

    # CAPTCHA/MFA detection flag
    captcha_detected = False
    login_required = False

    # JavaScript MutationObserver that auto-checks TOS/agreement checkboxes
    # as they appear on the page. Runs entirely in the browser context so it
    # works cross-platform and doesn't need Python async coordination.
    _TOS_AUTOCHECKER_JS = """() => {
        if (window.__tosAutoChecker) return 'already_installed';
        const KEYWORDS = ['terms', 'agree', 'accept', 'consent', 'privacy',
                          'tos', 'policy', 'conditions', 'acknowledge'];
        function checkTOS() {
            document.querySelectorAll(
                'input[type="checkbox"], [role="checkbox"]'
            ).forEach(cb => {
                if (cb.checked || cb.getAttribute('aria-checked') === 'true') return;
                const label = cb.closest('label');
                const linked = cb.id
                    ? document.querySelector('label[for="' + cb.id + '"]') : null;
                const hay = [
                    cb.getAttribute('aria-label') || '',
                    cb.getAttribute('name') || '',
                    cb.id || '',
                    label ? label.innerText : '',
                    linked ? linked.innerText : '',
                    cb.parentElement ? cb.parentElement.innerText : '',
                ].join(' ').toLowerCase().slice(0, 500);
                if (!KEYWORDS.some(kw => hay.includes(kw))) return;
                if (cb.tagName === 'INPUT') {
                    cb.checked = true;
                } else {
                    cb.setAttribute('aria-checked', 'true');
                }
                cb.dispatchEvent(new Event('input', {bubbles: true}));
                cb.dispatchEvent(new Event('change', {bubbles: true}));
                cb.dispatchEvent(new MouseEvent('click', {bubbles: true}));
            });
        }
        // Run once now, then watch for new checkboxes via MutationObserver
        checkTOS();
        new MutationObserver(checkTOS).observe(document.body, {
            childList: true, subtree: true
        });
        window.__tosAutoChecker = true;
        return 'installed';
    }"""

    async def on_step(state, output, step_num):
        """Callback invoked after each agent step for monitoring.

        Phase 4: async so that the pause check can sleep without blocking the
        event loop.  browser_use detects iscoroutinefunction and awaits it.
        """
        nonlocal captcha_detected, login_required
        try:
            # Phase 4 — pause/resume: freeze the agent between steps when the
            # user has paused the application queue via the UI.
            import asyncio as _asyncio
            from backend.auto_apply.orchestrator import is_paused
            while is_paused():
                logger.info(
                    "Browser-Use: PAUSED at step %d — awaiting resume...", step_num
                )
                await _asyncio.sleep(0.5)

            # Check the agent's output text for CAPTCHA/login indicators
            text = str(output) if output else ""
            text_lower = text.lower()
            if any(kw in text_lower for kw in ("captcha", "recaptcha", "hcaptcha", "verify you are human")):
                captcha_detected = True
                logger.warning("Browser-Use agent detected CAPTCHA at step %d", step_num)
            if any(kw in text_lower for kw in ("sso only", "oauth only", "email verification required", "verify your email")):
                login_required = True
                logger.warning("Browser-Use agent detected unresolvable login requirement at step %d", step_num)
        except Exception:
            pass

    global _active_browser_session
    try:
        # Pre-navigate to the job URL so the agent starts on the actual page
        # instead of a blank tab (avoids wasting an LLM round-trip on navigation
        # and fixes intermittent failures where the agent stalls on about:blank).
        if url:
            import asyncio

            # Try to start the browser session, retrying once after clearing
            # stale state if the CDP handshake times out.
            for _start_attempt in range(2):
                try:
                    await browser_session.start()
                    break
                except Exception as e:
                    if _start_attempt == 0 and "timed out" in str(e).lower():
                        logger.warning(
                            "CDP connection timed out — clearing stale browser state and retrying"
                        )
                        _clear_stale_browser_state()
                        await asyncio.sleep(2)
                        # Recreate session with a fresh profile connection
                        browser_session = _get_browser_session(config, domain=domain)
                    else:
                        raise

            # Register globally so force_stop() can close this browser immediately.
            _active_browser_session = browser_session

            nav_success = False
            for _attempt in range(2):
                try:
                    await browser_session.navigate_to(url)
                    logger.info("Pre-navigated to %s", url[:100])
                    nav_success = True
                    break
                except Exception as e:
                    logger.warning("Pre-navigation attempt %d failed: %s — retrying", _attempt + 1, e)
                    if _attempt < 1:
                        await asyncio.sleep(2)
            if not nav_success:
                logger.error("Pre-navigation failed for %s — agent will start on wrong page", url[:100])

        # Check for Cloudflare challenge and solve via FlareSolverr if needed.
        # The Browser-Use agent can't solve these on its own.
        try:
            page = await browser_session.get_current_page()
            if page:
                from .auto_apply import _is_cloudflare_challenge, _solve_cloudflare_with_flaresolverr
                if await _is_cloudflare_challenge(page):
                    logger.info("Cloudflare challenge detected — attempting FlareSolverr solve")
                    solved = await _solve_cloudflare_with_flaresolverr(page, url, config)
                    if solved:
                        logger.info("Cloudflare challenge solved via FlareSolverr")
                    else:
                        logger.warning("FlareSolverr could not solve Cloudflare challenge")
        except Exception as e:
            logger.debug("Cloudflare check failed: %s", e)

        # ── LinkedIn pre-apply: deterministically handle Apply→dialog→external tab ──
        # For regular (non-Easy-Apply) LinkedIn jobs, clicking Apply shows a
        # "Share your profile" or "Leave LinkedIn" dialog then opens the real ATS
        # in a new tab.  Doing this with Playwright before the agent starts saves
        # 2-3 LLM steps and avoids the dialog confusing the model.
        if is_linkedin_job:
            try:
                page = await browser_session.get_current_page()
                if page:
                    external_url = await _linkedin_pre_apply(page)
                    if external_url and "linkedin.com" not in external_url:
                        logger.info("LinkedIn pre-apply: landed on external ATS %s", external_url[:80])
                        # Rebuild task + flags so the agent treats it as a generic form
                        is_linkedin_job = False
                        task_prompt = _build_task_prompt(
                            job=job,
                            profile=profile,
                            resume_file_path=resume_file_path,
                            is_linkedin_job=False,
                            mode=apply_mode,
                        )
                        # _linkedin_pre_apply already closed the new tab and returned the
                        # external URL.  Navigate the existing page there so the agent
                        # starts cleanly on the external ATS with exactly one tab open.
                        await browser_session.navigate_to(external_url)
                        logger.info("LinkedIn pre-apply: navigated existing tab to external ATS")
            except Exception as e:
                logger.debug("LinkedIn pre-apply hook failed (non-fatal): %s", e)

        # ── Workday pre-apply: click Apply and handle new-tab before agent starts ──
        # Workday listing pages open the form in a new tab when Apply is clicked.
        # Doing this deterministically prevents the agent from clicking Apply
        # repeatedly and opening an endless stream of tabs.
        elif "myworkdayjobs.com" in url:
            try:
                page = await browser_session.get_current_page()
                if page:
                    form_url = await _workday_pre_apply(page)
                    if form_url and form_url != url:
                        logger.info("Workday pre-apply: agent will start on form URL %s", form_url[:80])
            except Exception as e:
                logger.debug("Workday pre-apply hook failed (non-fatal): %s", e)

        # Register a filechooser listener so that any native OS file picker
        # dialog is intercepted and satisfied automatically. This prevents the
        # agent from getting stuck on Finder/Explorer/Nautilus dialogs.
        # Works cross-platform because Playwright fires this event before the
        # OS dialog appears.
        async def _handle_filechooser(file_chooser):
            if resume_file_path and Path(resume_file_path).exists():
                try:
                    await file_chooser.set_files(resume_file_path)
                    logger.info("Filechooser intercepted — uploaded %s", Path(resume_file_path).name)
                except Exception as e:
                    logger.warning("Filechooser handler failed: %s", e)

        # Inject TOS auto-checker and re-inject on every navigation/redirect.
        async def _inject_tos_checker(page):
            try:
                result = await page.evaluate(_TOS_AUTOCHECKER_JS)
                logger.debug("TOS auto-checker: %s", result)
            except Exception:
                pass

        async def _on_frame_navigated(frame):
            """Re-inject TOS auto-checker and check for Cloudflare after navigations."""
            if frame.parent_frame is None:  # top-level frame only
                try:
                    await frame.wait_for_load_state("domcontentloaded", timeout=5000)
                    p = frame.page
                    await _inject_tos_checker(p)
                    # Check for Cloudflare on the new page
                    from .auto_apply import _is_cloudflare_challenge, _solve_cloudflare_with_flaresolverr
                    if await _is_cloudflare_challenge(p):
                        logger.info("Cloudflare challenge on navigated page — solving via FlareSolverr")
                        await _solve_cloudflare_with_flaresolverr(p, p.url, config)
                except Exception:
                    pass

        try:
            page = await browser_session.get_current_page()
            if page:
                page.on("filechooser", _handle_filechooser)
                page.on("framenavigated", _on_frame_navigated)
                logger.info("Registered filechooser + TOS listeners")
                await _inject_tos_checker(page)
        except Exception as e:
            logger.debug("Could not register page listeners: %s", e)

        _disable_stuck = config.get("auto_apply", {}).get("disable_stuck_detection", False)
        # Use flash_mode to load the compact 344-word system prompt (~460 tokens)
        # instead of the default 5000-token system prompt, which exceeds most
        # local model context windows before any conversation history is added.
        agent = Agent(
            task=task_prompt,
            llm=llm,
            browser_session=browser_session,
            sensitive_data=sensitive_data,
            available_file_paths=available_files,
            register_new_step_callback=on_step,
            max_actions_per_step=10,
            use_vision=False,
            use_thinking=False,
            flash_mode=True,
            use_judge=False,
            max_failures=5,
            generate_gif=False,
            max_history_items=8,
            enable_planning=False,
            directly_open_url=False,
            loop_detection_enabled=not _disable_stuck,
            loop_detection_window=10,
        )

        # Phase 4 — pre-run pause check: if the queue was paused before this
        # application started, block here (browser is not yet running any steps)
        # rather than spinning up the LLM loop at all.
        import asyncio as _asyncio_pause
        from backend.auto_apply.orchestrator import is_paused
        while is_paused():
            logger.info("Browser-Use: PAUSED before agent run — awaiting resume...")
            await _asyncio_pause.sleep(0.5)

        logger.info("Browser-Use agent created, running up to %d steps...", max_steps)
        result = await agent.run(max_steps=max_steps)

        # Analyze result
        if result and result.is_done():
            final_text = result.final_result() or ""
            final_lower = final_text.lower()

            # Check for success indicators
            success_keywords = [
                "application submitted", "successfully submitted",
                "successfully applied", "thank you for applying",
                "application received", "application has been",
                "we've received", "we have received",
                "thanks for applying", "confirmation",
            ]
            is_success = result.is_successful() or any(kw in final_lower for kw in success_keywords)

            if captcha_detected:
                logger.warning("Application blocked by CAPTCHA: %s at %s", job_title, company)
                shot = await _save_failure_screenshot(browser_session, job_id)
                return {
                    "success": False,
                    "message": f"CAPTCHA detected during application to {job_title} at {company} — manual application required",
                    "db_status":      "manual",
                    "screenshot_path": shot,
                    "block_reason":   "captcha",
                    "manual_url":     url or None,
                }

            if login_required:
                logger.warning("Login required for: %s at %s", job_title, company)
                shot = await _save_failure_screenshot(browser_session, job_id)
                return {
                    "success": False,
                    "message": f"Login required for {job_title} at {company} — set up session first via /api/sessions/{domain}/login",
                    "db_status":      "manual",
                    "screenshot_path": shot,
                    "block_reason":   "login_required",
                    "manual_url":     url or None,
                }

            if is_success:
                logger.info("Browser-Use apply SUCCESS: %s at %s", job_title, company)

                # Save session state after successful apply (capture any new cookies)
                if domain:
                    try:
                        state = await browser_session.export_storage_state()
                        if state:
                            path = session_manager.session_path(domain)
                            with open(path, "w") as f:
                                json.dump(state, f, indent=2)
                            logger.info("Updated session state for %s", domain)
                    except Exception as e:
                        logger.debug("Could not save post-apply session: %s", e)

                # Phase 5: detect autofill-mode completion so db_status reflects the
                # actual outcome rather than always writing "applied".
                is_autofill_complete = (
                    apply_mode == "autofill"
                    and "autofill_complete" in final_lower
                )
                if is_autofill_complete:
                    return {
                        "success": True,
                        "message": f"Autofill complete via Browser-Use: {job_title} at {company} — submit manually",
                        "db_status":      "autofill_complete",
                        "screenshot_path": None,
                        "block_reason":   "",
                        "manual_url":     url or None,
                    }
                return {
                    "success": True,
                    "message": f"Application submitted via Browser-Use: {job_title} at {company}",
                    "db_status":      "applied",
                    "screenshot_path": None,
                    "block_reason":   "",
                    "manual_url":     None,
                }
            else:
                errors = []
                try:
                    errors = [e for e in result.errors() if e]
                except Exception:
                    pass
                error_detail = "; ".join(errors[-3:]) if errors else ""
                fail_msg = final_text[:200] or error_detail or "unknown reason"
                logger.warning(
                    "Browser-Use completed but could not confirm submission: %s at %s. result=%s errors=%s",
                    job_title, company, final_text[:200], error_detail[:200],
                )
                shot = await _save_failure_screenshot(browser_session, job_id)
                return {
                    "success": False,
                    "message": f"Agent completed but could not confirm submission — {fail_msg}",
                    "db_status":      "needs_review",
                    "screenshot_path": shot,
                    "block_reason":   "needs_review",
                    "manual_url":     url or None,
                }
        else:
            # Agent didn't finish (hit max steps or failed).
            # Extract the actual error details from history so the activity log
            # shows WHY it failed instead of a useless generic message.
            errors: list[str] = []
            if result:
                try:
                    errors = [e for e in result.errors() if e]
                except Exception:
                    pass
            error_detail = "; ".join(errors[-3:]) if errors else "hit max_failures (check LM Studio model/context)"
            logger.warning(
                "Browser-Use agent did not complete for %s at %s — %s",
                job_title, company, error_detail,
            )
            shot = await _save_failure_screenshot(browser_session, job_id)
            return {
                "success": False,
                "message": f"Browser-Use agent did not complete: {error_detail}",
                "db_status":      "manual",
                "screenshot_path": shot,
                "block_reason":   "failed",
                "manual_url":     url or None,
            }

    except Exception as e:
        logger.exception("Browser-Use apply failed for %s at %s", job_title, company)
        shot = None
        try:
            shot = await _save_failure_screenshot(browser_session, job_id)
        except Exception:
            pass
        return {
            "success": False,
            "message": f"Browser-Use error: {str(e)}",
            "db_status":      "manual",
            "screenshot_path": shot,
            "block_reason":   "failed",
            "manual_url":     url or None,
        }
    finally:
        _active_browser_session = None
        # Always close the browser on exit — BrowserProfile(keep_alive=True) means
        # the session won't auto-close, so we must do it explicitly here or the
        # browser window stays open after every apply (success AND failure).
        try:
            await browser_session.stop()
        except Exception:
            try:
                await browser_session.kill()
            except Exception:
                pass
        # Clean up temp resume file
        if resume_file_path and resume_file_path.startswith("/tmp/"):
            try:
                Path(resume_file_path).unlink(missing_ok=True)
            except Exception:
                pass
