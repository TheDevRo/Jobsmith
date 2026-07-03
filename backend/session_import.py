"""
session_import.py — Persist imported cookies into the right Playwright session.

Shared by two entry points:
  - routers/sessions.py  → file upload (Cookie-Editor JSON / storage_state / cookies.txt)
  - extension_api.py     → one-click sync of chrome.cookies from the browser extension

Both hand us already-normalized Playwright cookies (see cookie_import.normalize_cookies)
and we write them to the session target the apply pipeline reads:
  linkedin → data/linkedin_chrome_profile/storage_state.json + sentinel
  indeed   → data/indeed_session/storage_state.json + indeed profile sentinel
  <domain> → sessions/<domain>.json  (generic Browser-Use session)
"""

import datetime
import json
import re

from . import auto_apply
from . import session_manager


def _sentinel_payload(source: str) -> str:
    return json.dumps({
        "logged_in_at": datetime.datetime.now().isoformat(),
        "source": source,
    })


def persist_session(site: str, cookies: list, *, source: str = "cookie_import") -> dict:
    """Write normalized `cookies` to the session target for `site`.

    Returns {"site", "imported", "target"}. Raises ValueError on a bad site name
    (callers map that to an HTTP 400).
    """
    if not cookies:
        raise ValueError("No usable cookies to import")

    storage_state = {"cookies": cookies, "origins": []}

    if site == "linkedin":
        # browser_controller injects storage_state cookies into the persistent
        # context at launch, so profile dir + cookies + sentinel is the
        # supported route around LinkedIn's cookies-only /authwall problem.
        auto_apply.LINKEDIN_SESSION_DIR.mkdir(parents=True, exist_ok=True)
        target = auto_apply.LINKEDIN_SESSION_DIR / "storage_state.json"
        target.write_text(json.dumps(storage_state))
        (auto_apply.LINKEDIN_SESSION_DIR / auto_apply._LINKEDIN_SENTINEL).write_text(
            _sentinel_payload(source)
        )
    elif site == "indeed":
        auto_apply.INDEED_SESSION_DIR.mkdir(parents=True, exist_ok=True)
        target = auto_apply.INDEED_SESSION_PATH
        target.write_text(json.dumps(storage_state))
        auto_apply.INDEED_CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        (auto_apply.INDEED_CHROME_PROFILE_DIR / auto_apply._INDEED_SENTINEL).write_text(
            _sentinel_payload(source)
        )
    else:
        if not re.fullmatch(r"[a-z0-9.-]{1,100}", site):
            raise ValueError(f"Invalid domain name: {site!r}")
        target = session_manager.session_path(site)
        target.write_text(json.dumps(storage_state))

    return {"site": site, "imported": len(cookies), "target": str(target)}
