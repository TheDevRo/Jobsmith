"""
auto_apply/adapters/adzuna.py — Adzuna job aggregator adapter.

Flow
----
1. Navigate to job URL, wait for any meta-refresh/JS redirects to settle.
2. Dismiss overlays (cookie consent, job-alert subscribe modals).
3. Detect immediate external-ATS redirect — dispatch to sub-adapter.
4. If still on Adzuna: find and click the Apply/External Apply button.
5. Re-detect external redirect after click.
6. If no external ATS detected: fall through to GenericAdapter.

Adzuna is a job aggregator — most job pages redirect to an employer's
ATS (Greenhouse, Lever, Workday, etc.). The adapter runs unauthenticated;
no Adzuna account login is required.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..models import ApplyMode, ApplyResult, ApplyStatus
from ..utils.browser_helpers import wait_if_paused

if TYPE_CHECKING:
    from ..browser_controller import BrowserController
    from ..llm_client import LLMClient
    from ..logger import AutoApplyLogger
    from ..models import JobApplicationRequest, UserProfile

logger = logging.getLogger(__name__)

_ADZUNA_HOSTS = ("adzuna.com",)
_REDIRECT_SETTLE_MS = 3_000

# ---------------------------------------------------------------------------
# Selector constants
# ---------------------------------------------------------------------------

_APPLY_BTN_SELECTORS = [
    "a[data-cy='apply-button']",
    "button[data-cy='apply-button']",
    "a[class*='apply']",
    "button[class*='apply']",
    "a:has-text('Apply now')",
    "a:has-text('Apply for this job')",
    "a:has-text('Apply externally')",
    "button:has-text('Apply now')",
    "button:has-text('Apply externally')",
]

# Tried in order — Adzuna-specific overlays before generic cookie selectors
_OVERLAY_SELECTORS = [
    "[data-cy='modal-close']",
    "button[aria-label='Close']",
    "button[aria-label='close']",
    ".modal-close",
    "[class*='modal'] button[class*='close']",
    "#onetrust-accept-btn-handler",
    "#CybotCookiebotDialogBodyButtonAccept",
    "button[aria-label='Accept all cookies']",
    "button[aria-label='Accept cookies']",
]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class AdzunaAdapter:
    name = "adzuna"

    def matches(self, url: str, page_text: str) -> bool:
        return any(h in url for h in _ADZUNA_HOSTS)

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
        log.adapter_chosen("adzuna")

        # ── Navigate to job URL ────────────────────────────────────────────
        await ctrl.navigate(job.url)

        # ── Wait for meta-refresh / JS redirects ──────────────────────────
        try:
            await ctrl.page.wait_for_load_state("networkidle", timeout=_REDIRECT_SETTLE_MS)
        except Exception:
            pass

        # ── Dismiss overlays ───────────────────────────────────────────────
        await _dismiss_adzuna_overlays(ctrl)
        await ctrl.page.wait_for_timeout(500)

        # ── Immediate external redirect? ───────────────────────────────────
        log.step("check_initial_redirect", page_url=ctrl.page.url)
        if _is_external(ctrl.page.url):
            return await _dispatch_external(ctrl.page.url, ctrl, profile, job, llm, mode, log)

        # ── Find and click Apply button ────────────────────────────────────
        log.step("click_apply")
        clicked = await _click_apply_button(ctrl)
        if not clicked:
            logger.info("Adzuna: no Apply button found — using generic adapter")
            log.step("no_apply_button_generic_fallback")
            from .generic import GenericAdapter
            return await GenericAdapter().apply(ctrl, profile, job, llm, mode, log)

        # Brief pause for any navigation triggered by the click
        await ctrl.page.wait_for_timeout(1_500)

        # ── Check for new-tab redirect after click ─────────────────────────
        try:
            new_url = await ctrl.switch_to_new_page(timeout=2_000)
            if new_url and _is_external(new_url):
                log.step("external_redirect_new_tab", page_url=new_url)
                return await _dispatch_external(new_url, ctrl, profile, job, llm, mode, log)
        except Exception:
            pass

        # ── Same-tab redirect? ─────────────────────────────────────────────
        if _is_external(ctrl.page.url):
            log.step("external_redirect_same_tab", page_url=ctrl.page.url)
            return await _dispatch_external(
                ctrl.page.url, ctrl, profile, job, llm, mode, log
            )

        # ── Still on Adzuna — generic fallback ────────────────────────────
        logger.info("Adzuna: still on adzuna.com after apply click — using generic adapter")
        log.step("generic_fallback")
        from .generic import GenericAdapter
        return await GenericAdapter().apply(ctrl, profile, job, llm, mode, log)


# ---------------------------------------------------------------------------
# Overlay dismissal
# ---------------------------------------------------------------------------

async def _dismiss_adzuna_overlays(ctrl: "BrowserController") -> None:
    """Dismiss Adzuna-specific overlays (job-alert subscribe, cookie banners)."""
    for sel in _OVERLAY_SELECTORS:
        try:
            loc = ctrl.page.locator(sel).first
            if await loc.is_visible(timeout=500):
                await loc.click(timeout=1_000)
                await ctrl.page.wait_for_timeout(300)
                logger.debug("Adzuna: dismissed overlay via %r", sel)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Button helpers
# ---------------------------------------------------------------------------

async def _click_apply_button(ctrl: "BrowserController") -> bool:
    for sel in _APPLY_BTN_SELECTORS:
        try:
            loc = ctrl.page.locator(sel).first
            if await loc.is_visible(timeout=2_000):
                await loc.click()
                logger.info("Adzuna: apply button clicked via %r", sel)
                return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# Redirect detection
# ---------------------------------------------------------------------------

def _is_external(url: str) -> bool:
    """Return True if url is NOT on any adzuna.com host."""
    return bool(url) and not any(h in url for h in _ADZUNA_HOSTS)


async def _dispatch_external(
    dest_url: str,
    ctrl: "BrowserController",
    profile: "UserProfile",
    job: "JobApplicationRequest",
    llm: "LLMClient",
    mode: ApplyMode,
    log: "AutoApplyLogger",
) -> ApplyResult:
    """Pick a sub-adapter for dest_url and delegate."""
    from . import ALL_ADAPTERS
    sub = next(
        (a for a in ALL_ADAPTERS if a.name != "adzuna" and a.matches(dest_url, "")),
        ALL_ADAPTERS[-1],  # GenericAdapter is always last
    )
    logger.info("Adzuna: external redirect → %s (adapter=%s)", dest_url[:80], sub.name)
    log.step("external_apply_redirect", page_url=dest_url, sub_adapter=sub.name)

    # Navigate to dest if not already there (new-tab case lands there via switch_to_new_page)
    if ctrl.page.url != dest_url:
        await ctrl.navigate(dest_url)

    return await sub.apply(ctrl, profile, job, llm, mode, log)
