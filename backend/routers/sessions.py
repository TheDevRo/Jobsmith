"""
routers/sessions.py — LinkedIn / Indeed / generic Browser-Use session
management: interactive login flows, session status, and logout.
"""

import asyncio
import logging
import os
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from .. import cookie_import
from .. import session_import

from .. import app_state as state
from .. import database as db
from .. import auto_apply
from .. import session_manager

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_display():
    """Interactive logins need a visible browser. In the Docker image that
    only exists when headed mode is on (Xvfb behind noVNC); otherwise
    Playwright dies with a cryptic "Missing X server" — fail friendly instead.
    """
    if os.environ.get("JOBSMITH_IN_DOCKER") and os.environ.get("BROWSER_HEADLESS", "true").lower() != "false":
        raise HTTPException(
            409,
            "Interactive login needs a visible browser. Set BROWSER_HEADLESS=false "
            "in .env and restart the container (then open http://<host>:6080/vnc.html), "
            "or use the cookie importer in Settings → Integrations instead.",
        )


class LinkedInLoginRequest(BaseModel):
    pass


class IndeedLoginRequest(BaseModel):
    pass


class SessionLoginRequest(BaseModel):
    timeout: int = 180


# Track login state so the frontend can poll
_linkedin_login_state: dict = {"status": "idle", "message": ""}

# Cached result of the last headless session-validity check
_linkedin_session_check: dict = {"valid": None, "checked_at": None}

_indeed_login_state: dict = {"status": "idle", "message": ""}


async def _bg_check_linkedin_session():
    """Background task: headlessly verify LinkedIn session validity and cache result."""
    global _linkedin_session_check
    import datetime
    try:
        valid = await auto_apply.check_linkedin_session_validity()
        _linkedin_session_check = {"valid": valid, "checked_at": datetime.datetime.now().isoformat()}
        logger.info("LinkedIn session check complete: valid=%s", valid)
    except Exception:
        logger.exception("LinkedIn session background check failed")
        _linkedin_session_check = {"valid": False, "checked_at": datetime.datetime.now().isoformat()}


async def _bg_linkedin_login():
    """Background task: open Chromium browser for LinkedIn login."""
    global _linkedin_login_state, _linkedin_session_check
    _linkedin_login_state = {"status": "waiting", "message": "Browser window opened — please sign in"}
    try:
        result = await auto_apply.linkedin_login()
        if result["success"]:
            import datetime
            _linkedin_login_state = {"status": "success", "message": result["message"]}
            _linkedin_session_check = {"valid": True, "checked_at": datetime.datetime.now().isoformat()}
            await db.log_activity("linkedin_login", "LinkedIn session saved")
        else:
            _linkedin_login_state = {"status": "failed", "message": result["message"]}
    except asyncio.CancelledError:
        _linkedin_login_state = {"status": "failed", "message": "Login cancelled"}
        # Force-close the browser since the task was cancelled externally
        await auto_apply.force_stop()
    except Exception as e:
        _linkedin_login_state = {"status": "failed", "message": str(e)}
    finally:
        state.running_tasks.pop("linkedin_login", None)


@router.post("/api/linkedin/login", status_code=202)
async def linkedin_login(body: LinkedInLoginRequest):
    """Start LinkedIn login flow — opens a browser window in the background."""
    _require_display()
    # Cancel any existing login task before starting a new one
    existing = state.running_tasks.get("linkedin_login")
    if existing and not existing.done():
        existing.cancel()

    task = asyncio.create_task(_bg_linkedin_login())
    state.running_tasks["linkedin_login"] = task
    return {"message": "Opening browser — please sign in to LinkedIn in the window that appears"}


@router.post("/api/linkedin/login/cancel")
async def cancel_linkedin_login():
    """Cancel an in-progress LinkedIn login and close the browser."""
    global _linkedin_login_state
    task = state.running_tasks.get("linkedin_login")
    if task and not task.done():
        task.cancel()
    else:
        # No active task — still force-close any leftover browser
        await auto_apply.force_stop()
    _linkedin_login_state = {"status": "failed", "message": "Login cancelled"}
    return {"message": "LinkedIn login cancelled"}


@router.get("/api/linkedin/session")
async def linkedin_session_status():
    """Check if a LinkedIn session exists and current login state."""
    import json as _json
    has_session = auto_apply.has_linkedin_session()
    logged_in_at = None
    if has_session:
        sentinel = auto_apply.LINKEDIN_SESSION_DIR / auto_apply._LINKEDIN_SENTINEL
        try:
            data = _json.loads(sentinel.read_text())
            logged_in_at = data.get("logged_in_at")
        except Exception:
            pass
    return {
        "has_session": has_session,
        "logged_in_at": logged_in_at,
        "login_state": _linkedin_login_state,
        "session_check": _linkedin_session_check,
    }


@router.post("/api/linkedin/check-session")
async def check_linkedin_session():
    """Run a headless session validity check and return the result."""
    global _linkedin_session_check
    import datetime
    try:
        valid = await asyncio.wait_for(
            auto_apply.check_linkedin_session_validity(),
            timeout=25.0,
        )
    except asyncio.TimeoutError:
        valid = False
        logger.warning("LinkedIn session check timed out")
    except Exception:
        valid = False
        logger.exception("LinkedIn session check endpoint failed")
    _linkedin_session_check = {"valid": valid, "checked_at": datetime.datetime.now().isoformat()}
    return _linkedin_session_check


@router.post("/api/linkedin/logout")
async def linkedin_logout():
    """Clear saved LinkedIn session."""
    global _linkedin_login_state, _linkedin_session_check
    auto_apply.linkedin_logout()
    _linkedin_login_state = {"status": "idle", "message": ""}
    _linkedin_session_check = {"valid": None, "checked_at": None}
    return {"message": "LinkedIn session cleared"}


# ---------------------------------------------------------------------------
# Indeed session management
# ---------------------------------------------------------------------------

async def _bg_indeed_login():
    """Background task: open Chromium browser for Indeed login."""
    global _indeed_login_state
    _indeed_login_state = {"status": "waiting", "message": "Browser window opened — please sign in"}
    try:
        result = await auto_apply.indeed_login()
        if result["success"]:
            _indeed_login_state = {"status": "success", "message": result["message"]}
            await db.log_activity("indeed_login", "Indeed session saved")
        else:
            _indeed_login_state = {"status": "failed", "message": result["message"]}
    except asyncio.CancelledError:
        _indeed_login_state = {"status": "failed", "message": "Login cancelled"}
        await auto_apply.force_stop()
    except Exception as e:
        _indeed_login_state = {"status": "failed", "message": str(e)}
    finally:
        state.running_tasks.pop("indeed_login", None)


@router.post("/api/indeed/login", status_code=202)
async def indeed_login(body: IndeedLoginRequest):
    """Start Indeed login flow — opens a browser window in the background."""
    _require_display()
    existing = state.running_tasks.get("indeed_login")
    if existing and not existing.done():
        existing.cancel()

    task = asyncio.create_task(_bg_indeed_login())
    state.running_tasks["indeed_login"] = task
    return {"message": "Opening browser — please sign in to Indeed in the window that appears"}


@router.get("/api/indeed/session")
async def indeed_session_status():
    """Check if an Indeed session exists and current login state."""
    return {
        "has_session": auto_apply.has_indeed_session(),
        "login_state": _indeed_login_state,
    }


@router.post("/api/indeed/logout")
async def indeed_logout():
    """Clear saved Indeed session."""
    global _indeed_login_state
    auto_apply.indeed_logout()
    _indeed_login_state = {"status": "idle", "message": ""}
    return {"message": "Indeed session cleared"}


# ---------------------------------------------------------------------------
# Browser-Use session management endpoints
# ---------------------------------------------------------------------------

@router.get("/api/sessions")
async def list_sessions():
    """List all saved Browser-Use sessions."""
    return {"sessions": session_manager.list_sessions()}


@router.post("/api/sessions/{domain}/login", status_code=202)
async def create_session(domain: str, body: Optional[SessionLoginRequest] = None):
    """Launch a visible browser for manual login to save a session for a domain."""
    _require_display()
    timeout = body.timeout if body else 180

    async def _bg_session_login():
        result = await session_manager.create_session_interactive(domain, timeout_seconds=timeout)
        if result["success"]:
            await db.log_activity("session_login", f"Session saved for {domain}")
            state.push_notification("session", "Session Saved", f"Session saved for {domain}", "success")
        else:
            state.push_notification("session", "Session Login Failed", result["message"], "error")

    asyncio.create_task(_bg_session_login())
    return {"message": f"Opening browser for {domain} login — please sign in"}


@router.delete("/api/sessions/{domain}")
async def delete_session(domain: str):
    """Delete a saved session."""
    deleted = session_manager.delete_session(domain)
    if not deleted:
        raise HTTPException(404, f"No session found for {domain}")
    return {"message": f"Session for {domain} deleted"}


# ---------------------------------------------------------------------------
# Cookie import — seed sessions from a browser cookie export
# ---------------------------------------------------------------------------

@router.post("/api/sessions/import-cookies")
async def import_cookies(
    file: UploadFile = File(...),
    site: str = Form("auto"),
):
    """Import cookies exported from the user's normal browser (Cookie-Editor
    JSON, Playwright storage_state JSON, or Netscape cookies.txt) and seed the
    matching Playwright session so interactive login is unnecessary."""
    global _linkedin_session_check
    raw = await file.read()
    try:
        cookies = cookie_import.parse_upload(raw)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not cookies:
        raise HTTPException(400, "No usable cookies found in the file")

    target = site.strip().lower()
    if target in ("", "auto"):
        detected = cookie_import.detect_site(cookies)
        if not detected:
            raise HTTPException(400, "Could not detect the target site — pick one explicitly")
        target = detected

    try:
        result = session_import.persist_session(target, cookies)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if target == "linkedin":
        _linkedin_session_check = {"valid": None, "checked_at": None}
        asyncio.create_task(_bg_check_linkedin_session())
        result["validity_check"] = "started"
    await db.log_activity("cookie_import", f"{target} session imported ({len(cookies)} cookies)")

    return result
