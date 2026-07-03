"""
Shared Playwright helpers used across auto-apply adapters.

Functions here must never raise — all exceptions are caught and suppressed
so callers can use them unconditionally without try/except guards.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from ...paths import project_root as _project_root

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Overlay / cookie-banner selectors tried in order by dismiss_overlays.
# These are intentionally narrow so we never accidentally close an apply modal.
# ---------------------------------------------------------------------------
_OVERLAY_DISMISS_SELECTORS = [
    # OneTrust
    "#onetrust-accept-btn-handler",
    # Cookiebot
    "#CybotCookiebotDialogBodyButtonAccept",
    # TrustArc / Evidon generic
    ".trustarc-agree-btn",
    ".evidon-banner-acceptbutton",
    # Generic "accept cookies" buttons (aria-label based — safer than class)
    "button[aria-label='Accept all cookies']",
    "button[aria-label='Accept cookies']",
    "button[aria-label='I Accept']",
    "button[aria-label='I agree']",
    # Generic cookie-named containers with an accept/close action
    "[id*='cookie-banner'] button[class*='accept']",
    "[id*='cookie-consent'] button[class*='accept']",
    "[class*='cookie-banner'] button[class*='accept']",
    "[class*='cookie-consent'] button[class*='accept']",
    # GDPR overlays
    "[class*='gdpr'] button[class*='accept']",
    "[class*='gdpr'] button[class*='agree']",
    # Generic dismissible notification/toast that is NOT a form modal
    # (data-dismiss is a Bootstrap attribute used for non-modal banners)
    "[data-dismiss='alert']",
]


async def wait_if_paused(poll_interval: float = 0.5, timeout: float = 3600.0) -> None:
    """
    Block execution until the pause flag is cleared.

    Raises asyncio.CancelledError if the force-stop event is set or if
    *timeout* seconds elapse while still paused.  Returns immediately when
    not paused.  Safe to call from any adapter or helper — the import is
    lazy to avoid circular-module issues at load time.
    """
    import asyncio
    from backend.auto_apply import is_paused, _async_force_stop
    elapsed = 0.0
    while is_paused():
        if _async_force_stop.is_set():
            raise asyncio.CancelledError("Force stopped while paused")
        if elapsed >= timeout:
            raise asyncio.CancelledError("Pause timeout exceeded")
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval


async def dismiss_overlays(page) -> None:
    """
    Attempt to dismiss cookie banners, GDPR prompts, and other non-apply
    overlay elements.  Tries each selector once; silently ignores any that
    are absent or fail to interact.  Never raises.
    """
    for sel in _OVERLAY_DISMISS_SELECTORS:
        try:
            locator = page.locator(sel).first
            if await locator.is_visible(timeout=500):
                await locator.click(timeout=1_000)
                # Brief pause so the overlay can animate out before the next action.
                await page.wait_for_timeout(300)
        except Exception:
            pass


async def resilient_click(page, selector: str, timeout: int = 5_000) -> None:
    """
    Click *selector* reliably:
      1. Dismiss any overlays that might intercept the click.
      2. Wait for the element to be visible and enabled within *timeout* ms.
      3. Click.
      4. Wait for any resulting navigation to settle (networkidle, best-effort).

    Raises ``playwright.async_api.TimeoutError`` (or its base ``Exception``)
    if the element is not found within *timeout*.  All other exceptions
    (navigation-related, etc.) are suppressed after the click.
    """
    await dismiss_overlays(page)

    el = page.locator(selector).first
    # wait_for raises TimeoutError if not visible — intentional, let it propagate.
    await el.wait_for(state="visible", timeout=timeout)
    # Best-effort: also wait until not disabled (some buttons enable after JS runs).
    try:
        await page.wait_for_function(
            f"() => {{ const el = document.querySelector({selector!r}); "
            f"return el && !el.disabled && !el.getAttribute('aria-disabled'); }}",
            timeout=min(timeout, 2_000),
        )
    except Exception:
        pass

    await el.click()

    try:
        await page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:
        pass


async def take_failure_screenshot(page, label: str) -> str:
    """
    Save a full-page PNG to ``data/screenshots/{label}_{timestamp}.png``
    relative to the project root.

    Returns the path on success, empty string on failure.  Never raises.
    """
    try:
        # Resolve project root as four levels up from this file:
        # utils/ → auto_apply/ → backend/ → project root
        project_root = _project_root()
        screenshots_dir = project_root / "data" / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = screenshots_dir / f"{label}_{timestamp}.png"
        await page.screenshot(path=str(path), full_page=True)
        logger.debug("failure screenshot saved: %s", path)
        return str(path)
    except Exception as exc:
        logger.debug("take_failure_screenshot failed (%s): %s", label, exc)
        return ""


async def check_page_errors(page) -> list[str]:
    """
    Search the current page for visible inline validation error messages.

    Checks:
      - Elements with aria-invalid='true'
      - Elements with role='alert'
      - Visible elements whose class contains 'error' or 'invalid'
        (text length capped at 300 chars to avoid collecting container text)

    Returns a deduplicated list of error strings.  Returns an empty list on
    any exception — callers must never have to guard against a raise from here.
    """
    errors: list[str] = []
    try:
        seen: set[str] = set()

        async def _collect(sel: str, max_text_len: int = 300) -> None:
            try:
                for el in await page.locator(sel).all():
                    try:
                        if not await el.is_visible():
                            continue
                        text = ((await el.text_content()) or "").strip()
                        if not text or len(text) > max_text_len:
                            continue
                        if text not in seen:
                            seen.add(text)
                            errors.append(text)
                    except Exception:
                        pass
            except Exception:
                pass

        await _collect("[aria-invalid='true']")
        await _collect("[role='alert']")
        await _collect("[class*='error']")
        await _collect("[class*='invalid']")
    except Exception:
        pass
    return errors
