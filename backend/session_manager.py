"""
session_manager.py — Per-domain browser session persistence for Browser-Use.

Manages storageState JSON files (cookies, localStorage) in a sessions/
directory, one file per job board domain. On first run for a domain, launches
a visible browser for manual login, then saves the state. On subsequent runs,
loads the saved state automatically.
"""

import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse
from .paths import project_root

logger = logging.getLogger(__name__)

PROJECT_ROOT = project_root()
SESSIONS_DIR = PROJECT_ROOT / os.getenv("SESSIONS_DIR", "sessions")


def _domain_key(url: str) -> str:
    """Extract a safe filename-friendly domain key from a URL.

    Examples:
        https://www.linkedin.com/jobs/123 -> linkedin
        https://myworkday.com/foo         -> myworkday
        https://boards.greenhouse.io/x    -> greenhouse
    """
    host = urlparse(url).hostname or ""
    # Strip common prefixes
    for prefix in ("www.", "boards.", "jobs.", "apply."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    # Use the second-level domain (e.g. "linkedin" from "linkedin.com")
    parts = host.split(".")
    return parts[0] if parts else "unknown"


def _domain_from_name(name: str) -> str:
    """Map a friendly domain name to a login URL."""
    domain_urls = {
        "linkedin": "https://www.linkedin.com/login",
        "indeed": "https://secure.indeed.com/auth",
        "greenhouse": "https://www.greenhouse.io/sign-in",
        "lever": "https://www.lever.co/",
        "workday": "https://www.myworkday.com/",
        "glassdoor": "https://www.glassdoor.com/profile/login_input.htm",
    }
    return domain_urls.get(name.lower(), f"https://{name}.com/login")


def sessions_dir() -> Path:
    """Return the sessions directory, creating it if needed."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR


def session_path(domain: str) -> Path:
    """Return the storageState JSON path for a domain."""
    return sessions_dir() / f"{domain}.json"


def has_session(domain: str) -> bool:
    """Check if a saved session exists for a domain."""
    path = session_path(domain)
    return path.exists() and path.stat().st_size > 10


def list_sessions() -> list[dict]:
    """List all saved sessions with metadata."""
    result = []
    sdir = sessions_dir()
    for f in sorted(sdir.glob("*.json")):
        try:
            stat = f.stat()
            result.append({
                "domain": f.stem,
                "file": f.name,
                "size_bytes": stat.st_size,
                "modified": stat.st_mtime,
            })
        except Exception:
            continue
    return result


def load_storage_state(domain: str) -> dict | None:
    """Load storageState dict for a domain, or None if not found."""
    path = session_path(domain)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load session for %s: %s", domain, e)
        return None


def delete_session(domain: str) -> bool:
    """Delete a saved session file."""
    path = session_path(domain)
    if path.exists():
        path.unlink()
        logger.info("Deleted session for %s", domain)
        return True
    return False


async def create_session_interactive(domain: str, timeout_seconds: int = 180) -> dict:
    """Launch a visible browser for manual login, then save the storageState.

    This opens a non-headless Chromium window pointed at the domain's login
    page. The user signs in manually. Once an authenticated page is detected
    (or the timeout is reached), the storageState is saved.

    Returns {"success": bool, "message": str}.
    """
    from playwright.async_api import async_playwright

    login_url = _domain_from_name(domain)
    pw = None
    browser = None

    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)

        logger.info("Session setup: waiting for manual login at %s (timeout %ds)",
                     login_url, timeout_seconds)

        # Poll for navigation away from login page (indicates successful auth)
        login_path = urlparse(login_url).path
        polls = timeout_seconds // 2
        logged_in = False

        for _ in range(polls):
            await page.wait_for_timeout(2000)
            try:
                current = page.url
                current_path = urlparse(current).path
                # If we've navigated away from the login page, assume success
                if current_path != login_path and "/login" not in current_path.lower():
                    logged_in = True
                    break
            except Exception:
                break  # Browser was closed

        if not logged_in:
            return {"success": False, "message": f"Login timed out for {domain}"}

        # Save storageState
        state = await context.storage_state()
        path = session_path(domain)
        with open(path, "w") as f:
            json.dump(state, f, indent=2)

        logger.info("Session saved for %s at %s", domain, path)
        return {"success": True, "message": f"Session saved for {domain}"}

    except Exception as e:
        logger.exception("Session creation failed for %s", domain)
        return {"success": False, "message": f"Error: {str(e)}"}
    finally:
        if browser:
            await browser.close()
        if pw:
            await pw.stop()
