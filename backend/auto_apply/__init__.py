"""
auto_apply/__init__.py — Public API for the auto-apply package.

Exposes the same interface as the old auto_apply.py so main.py requires
zero changes.

LinkedIn session helpers (linkedin_auth.py):
  linkedin_login(), linkedin_logout(), has_linkedin_session()
  LINKEDIN_SESSION_DIR, _LINKEDIN_SENTINEL
  install_browsers()

New (orchestrator.py):
  auto_apply_job()   ← replaces the Stagehand microservice call
  set_paused()
  is_paused()
  _async_force_stop  ← asyncio.Event; .set() = force-stop signal
  force_stop()       ← async coroutine; closes browser + sets the event

Indeed session helpers (defined here):
  indeed_login(), indeed_logout(), has_indeed_session()
  INDEED_SESSION_DIR, _INDEED_SENTINEL

"""

from __future__ import annotations

from pathlib import Path as _Path
import logging as _logging

_log = _logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# New orchestrator — main entry point
# ---------------------------------------------------------------------------
from .orchestrator import (
    run_apply           as auto_apply_job,   # aliased for drop-in replacement
    set_paused,
    is_paused,
    force_stop,                              # async coroutine — closes browser
    _force_stop_event   as _async_force_stop, # asyncio.Event — checked by wait_if_paused()
    get_apply_progress,
)
from . import orchestrator

# ---------------------------------------------------------------------------
# LinkedIn session helpers
# ---------------------------------------------------------------------------
from ..paths import project_root as _project_root
from .linkedin_auth import (      # noqa: F401
    linkedin_login,
    linkedin_logout,
    has_linkedin_session,
    check_linkedin_session_validity,
    LINKEDIN_SESSION_DIR,
    _LINKEDIN_SENTINEL,
    install_browsers,
)


# ---------------------------------------------------------------------------
# Indeed session helpers — mirror the LinkedIn pattern
# ---------------------------------------------------------------------------
# Persistent Chromium profile dir (same approach as LinkedIn).
INDEED_CHROME_PROFILE_DIR: _Path = _project_root() / "data" / "indeed_chrome_profile"
_INDEED_SENTINEL: str = "login_success.json"
# Legacy cookie-based session paths — kept for exports/backward compat.
INDEED_SESSION_DIR: _Path = _project_root() / "data" / "indeed_session"
INDEED_SESSION_PATH: _Path = INDEED_SESSION_DIR / "storage_state.json"


def _is_indeed_authenticated(url: str) -> bool:
    """
    Return True if *url* indicates the user is logged in to Indeed.

    Exclusion-based: the user is authenticated when BOTH conditions hold:
      1. The hostname is www.indeed.com, indeed.com, OR secure.indeed.com
      2. The path does NOT start with any known auth/login prefix

    This handles Google OAuth redirects (accounts.google.com → NOT indeed.com)
    and intermediate secure.indeed.com/auth hops without false-positives.
    """
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    indeed_hosts = {"www.indeed.com", "indeed.com", "secure.indeed.com"}
    if host not in indeed_hosts:
        return False

    auth_prefixes = ("/account/login", "/account/register", "/auth", "/oauth")
    return not any(path.startswith(p) for p in auth_prefixes)


async def indeed_login() -> dict:
    """
    Launch a visible Chromium browser for the user to manually log into Indeed.
    Uses a persistent Chromium profile (same pattern as LinkedIn) so cookies,
    localStorage, and fingerprint are preserved across apply sessions.
    """
    import datetime
    import json as _json

    sentinel = INDEED_CHROME_PROFILE_DIR / _INDEED_SENTINEL
    if sentinel.exists():
        sentinel.unlink()

    pw = context = None
    try:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        INDEED_CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        # Remove stale Chromium lock files so launch doesn't fail after a
        # previous unclean exit.
        for lock_name in ("SingletonLock", ".parentlock", "lock"):
            lf = INDEED_CHROME_PROFILE_DIR / lock_name
            try:
                if lf.exists():
                    lf.unlink()
            except Exception:
                pass

        context = await pw.chromium.launch_persistent_context(
            str(INDEED_CHROME_PROFILE_DIR),
            headless=False,
            accept_downloads=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto("https://www.indeed.com/account/login", wait_until="domcontentloaded", timeout=30000)

        _log.info("Waiting for Indeed login (Chromium browser opened)...")
        login_success = False
        for _ in range(150):  # Poll for up to 5 minutes (150 × 2 s)
            try:
                await page.wait_for_timeout(2000)
            except Exception:
                break  # Browser/page was closed; stop polling
            try:
                url = page.url
                _log.debug("Indeed login poll — current URL: %s", url)
                if _is_indeed_authenticated(url):
                    login_success = True
                    break
            except Exception:
                pass  # URL temporarily unavailable during navigation; keep polling

        if not login_success:
            _log.warning("Indeed login timed out — no authenticated state detected after 5 minutes")
            return {"success": False, "error": "Login timeout — 5 minutes exceeded"}

        # Write sentinel only after confirmed login so has_indeed_session() is accurate.
        sentinel.write_text(_json.dumps({"logged_in_at": datetime.datetime.now().isoformat()}))
        _log.info("Indeed login successful — persistent profile saved to %s", INDEED_CHROME_PROFILE_DIR)
        return {"success": True, "message": "Indeed login successful — session saved"}
    except Exception as e:
        _log.exception("Indeed login failed")
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


def indeed_logout() -> bool:
    """Clear saved Indeed session data (persistent profile dir)."""
    import shutil
    if INDEED_CHROME_PROFILE_DIR.exists():
        shutil.rmtree(INDEED_CHROME_PROFILE_DIR)
        _log.info("Indeed session cleared")
        return True
    return False


def has_indeed_session() -> bool:
    """Check if a saved Indeed session exists (login sentinel present)."""
    return (INDEED_CHROME_PROFILE_DIR / _INDEED_SENTINEL).exists()


# ---------------------------------------------------------------------------
# Re-export the new orchestrator's run_apply under its real name too
# (useful for direct callers / tests that want the typed signature)
# ---------------------------------------------------------------------------
from .orchestrator import run_apply  # noqa: F401
from .models import (                # noqa: F401
    ApplyMode,
    ApplyResult,
    ApplyStatus,
    JobApplicationRequest,
    UserProfile,
)
from .answer_bank import get_answer_bank  # noqa: F401
from .llm_client  import LLMClient        # noqa: F401

__all__ = [
    # Legacy-compatible names
    "auto_apply_job",
    "set_paused",
    "is_paused",
    "_async_force_stop",  # asyncio.Event — the force-stop signal object
    "force_stop",         # async coroutine — closes browser + sets the event
    "linkedin_login",
    "linkedin_logout",
    "has_linkedin_session",
    "check_linkedin_session_validity",
    "LINKEDIN_SESSION_DIR",
    "_LINKEDIN_SENTINEL",
    "install_browsers",
    # Indeed session
    "INDEED_CHROME_PROFILE_DIR",
    "INDEED_SESSION_DIR",
    "_INDEED_SENTINEL",
    "INDEED_SESSION_PATH",
    "indeed_login",
    "indeed_logout",
    "has_indeed_session",
    # New names
    "run_apply",
    "ApplyMode",
    "ApplyResult",
    "ApplyStatus",
    "JobApplicationRequest",
    "UserProfile",
    "get_answer_bank",
    "LLMClient",
]
