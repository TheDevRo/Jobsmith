"""
linkedin_auth.py — LinkedIn session management for auto-apply.

Extracted from the retired auto_apply_legacy module. Handles the manual
login flow (visible Chromium, user signs in, session persisted), session
presence/validity checks, and logout.

The session lives in a persistent Chromium profile — that preserves cookies
AND localStorage, both of which LinkedIn checks on load. Cookie-only loading
(storage_state.json) causes a redirect to /authwall, so the profile dir is
the source of truth; storage_state.json is an export for consumers that
can't open the profile (validity checks, Browser-Use).
"""

from __future__ import annotations

import logging
from pathlib import Path
from ..paths import project_root

logger = logging.getLogger(__name__)

LINKEDIN_SESSION_DIR = project_root() / "data" / "linkedin_chrome_profile"
_LINKEDIN_SENTINEL = "login_success.json"


def _clear_browser_locks() -> None:
    """Remove stale browser lock files that prevent launching."""
    lock_names = [".parentlock", "lock", "SingletonLock", "SingletonSocket", "SingletonCookie"]
    for name in lock_names:
        lock = LINKEDIN_SESSION_DIR / name
        try:
            if lock.exists():
                lock.unlink()
                logger.info("Removed stale lock file: %s/%s", LINKEDIN_SESSION_DIR.name, name)
        except Exception:
            pass


async def linkedin_login():
    """
    Launch a visible Chromium browser for the user to manually log into LinkedIn.
    Saves the session to LINKEDIN_SESSION_DIR (persistent Chromium profile) for reuse.
    """
    import json as _json
    import datetime

    # Remove any existing sentinel so the status accurately reflects the new
    # login attempt while it's in progress (and if it fails, stays cleared).
    sentinel = LINKEDIN_SESSION_DIR / _LINKEDIN_SENTINEL
    if sentinel.exists():
        sentinel.unlink()

    pw = context = None
    try:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        LINKEDIN_SESSION_DIR.mkdir(parents=True, exist_ok=True)
        _clear_browser_locks()

        context = await pw.chromium.launch_persistent_context(
            str(LINKEDIN_SESSION_DIR),
            headless=False,
            accept_downloads=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30000)

        # Wait for the user to complete login — poll the URL for authenticated pages
        logger.info("Waiting for LinkedIn login (Chromium browser opened)...")
        authenticated_paths = ["/feed", "/mynetwork", "/jobs", "/messaging", "/in/", "/home"]
        login_success = False
        for _ in range(90):  # Poll for up to 3 minutes (90 * 2s)
            await page.wait_for_timeout(2000)
            try:
                url = page.url
                logger.debug("LinkedIn login poll — current URL: %s", url)
                if any(p in url for p in authenticated_paths):
                    login_success = True
                    break
            except Exception:
                break  # Page/context closed

        if not login_success:
            return {"success": False, "message": "Login timed out — please try again"}

        # Export cookies/localStorage to a portable storage-state JSON so the
        # auto-apply browser can load just the session without re-opening the
        # full profile directory (which is fragile and lock-prone).
        state_path = LINKEDIN_SESSION_DIR / "storage_state.json"
        await context.storage_state(path=str(state_path))
        # Chromium exports partitionKey as an object; Playwright's new_context()
        # requires it to be a string or absent.  Sanitize on save so every
        # consumer (Playwright + Browser-Use StorageStateWatchdog) can load it.
        raw = _json.loads(state_path.read_text())
        for cookie in raw.get("cookies", []):
            pk = cookie.get("partitionKey")
            if pk is not None and not isinstance(pk, str):
                del cookie["partitionKey"]
        state_path.write_text(_json.dumps(raw))
        logger.info("LinkedIn session exported to storage_state.json")

        # Write sentinel ONLY after confirmed login so has_linkedin_session() is accurate.
        sentinel.write_text(_json.dumps({"logged_in_at": datetime.datetime.now().isoformat()}))
        logger.info("LinkedIn login successful — session saved")
        return {"success": True, "message": "LinkedIn login successful — session saved"}
    except Exception as e:
        logger.exception("LinkedIn login failed")
        return {"success": False, "message": f"Error: {str(e)}"}
    finally:
        if context:
            try:
                await context.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass


def linkedin_logout():
    """Clear saved LinkedIn session data."""
    import shutil
    if LINKEDIN_SESSION_DIR.exists():
        shutil.rmtree(LINKEDIN_SESSION_DIR)
        logger.info("LinkedIn session cleared")
        return True
    return False


def has_linkedin_session() -> bool:
    """Check if a saved LinkedIn session exists (login actually completed)."""
    return (LINKEDIN_SESSION_DIR / _LINKEDIN_SENTINEL).exists()


async def check_linkedin_session_validity() -> bool:
    """Navigate headlessly to linkedin.com/feed using the saved storage_state.

    Uses the exported storage_state.json (cookies + localStorage) rather than
    the full persistent profile so there are no lock conflicts with a running
    auto-apply browser.

    Returns True  — cookies are still accepted by LinkedIn (/feed loads).
    Returns False — session expired (redirected to /login or /authwall),
                    no sentinel / storage_state.json found, or any error.
    """
    state_path = LINKEDIN_SESSION_DIR / "storage_state.json"
    if not has_linkedin_session() or not state_path.exists():
        logger.debug("LinkedIn session check: sentinel or storage_state.json missing")
        return False

    pw = browser = ctx = None
    try:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(storage_state=str(state_path))
        page = await ctx.new_page()
        await page.goto(
            "https://www.linkedin.com/feed",
            wait_until="domcontentloaded",
            timeout=20_000,
        )
        final_url = page.url
        is_valid = "/login" not in final_url and "/authwall" not in final_url
        logger.info(
            "LinkedIn session validity: %s (url=%s)",
            "valid" if is_valid else "expired",
            final_url,
        )
        return is_valid
    except Exception:
        logger.exception("LinkedIn session validity check failed")
        return False
    finally:
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass


async def install_browsers():
    """Install Playwright Chromium browser if not already present."""
    import subprocess
    try:
        subprocess.run(
            ["playwright", "install", "chromium"],
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info("Playwright Chromium installed successfully")
    except Exception:
        logger.exception("Failed to install Playwright browsers")
