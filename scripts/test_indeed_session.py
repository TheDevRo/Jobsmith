"""
Standalone script to test whether the Indeed session file can produce
an authenticated Playwright browser.

Does NOT import anything from backend/.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Step 1 — Locate INDEED_SESSION_PATH by parsing __init__.py as text
# ---------------------------------------------------------------------------
INIT_FILE = Path(__file__).resolve().parent.parent / "backend" / "auto_apply" / "__init__.py"

init_text = INIT_FILE.read_text()

# Extract the parent-chain count (number of .parent calls on __file__)
# Line pattern: INDEED_SESSION_DIR: _Path = _Path(__file__).resolve().parent.parent.parent / "data" / "indeed_session"
dir_match = re.search(
    r'INDEED_SESSION_DIR\s*(?::\s*\S+)?\s*=\s*_Path\(__file__\)\.resolve\(\)((?:\.parent)+)\s*/\s*"([^"]+)"\s*/\s*"([^"]+)"',
    init_text,
)
path_match = re.search(
    r'INDEED_SESSION_PATH\s*(?::\s*\S+)?\s*=\s*INDEED_SESSION_DIR\s*/\s*"([^"]+)"',
    init_text,
)

if not dir_match or not path_match:
    print("ERROR: Could not extract INDEED_SESSION_PATH from __init__.py via regex.")
    sys.exit(1)

parent_chain = dir_match.group(1)  # e.g. ".parent.parent.parent"
parent_depth = parent_chain.count(".parent")
dir_parts = [dir_match.group(2), dir_match.group(3)]  # ["data", "indeed_session"]
filename = path_match.group(1)  # "storage_state.json"

# Resolve relative to __init__.py location
base = INIT_FILE.resolve()
for _ in range(parent_depth):
    base = base.parent
INDEED_SESSION_PATH = base.joinpath(*dir_parts, filename)

print(f"Resolved INDEED_SESSION_PATH: {INDEED_SESSION_PATH}")

# ---------------------------------------------------------------------------
# Step 2 — File existence and size
# ---------------------------------------------------------------------------
exists = INDEED_SESSION_PATH.exists()
size = INDEED_SESSION_PATH.stat().st_size if exists else 0
print(f"File exists: {exists}")
print(f"File size: {size} bytes")

if not exists or size == 0:
    print("\nFAIL — session file missing or empty, cannot proceed.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Step 3 — Parse JSON and inspect cookies
# ---------------------------------------------------------------------------
with open(INDEED_SESSION_PATH) as f:
    session_data = json.load(f)

cookies: list[dict] = session_data.get("cookies", [])
cookie_names = [c.get("name", "") for c in cookies]

auth_keywords = {"auth", "token", "session", "login"}
auth_cookies = [n for n in cookie_names if any(kw in n.lower() for kw in auth_keywords)]
has_cf_clearance = "cf_clearance" in cookie_names
has_jsessionid = any(n.upper() == "JSESSIONID" for n in cookie_names)

print(f"\nTotal cookie count: {len(cookies)}")
print(f"Cookie names: {cookie_names}")
print(f"cf_clearance present: {has_cf_clearance}")
print(f"JSESSIONID present: {has_jsessionid}")
print(f"Auth/token cookies: {auth_cookies if auth_cookies else 'none'}")


# ---------------------------------------------------------------------------
# Steps 4–10 — Playwright session test
# ---------------------------------------------------------------------------
async def run_test() -> bool:
    from playwright.async_api import async_playwright

    user_data_dir = tempfile.mkdtemp(prefix="indeed_session_test_")
    print(f"\nTemp user_data_dir: {user_data_dir}")

    pw = None
    context = None
    passed = False

    try:
        pw = await async_playwright().start()

        # Step 4 — launch with persistent context, no storage_state
        context = await pw.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )

        page = context.pages[0] if context.pages else await context.new_page()

        # Step 5 — inject cookies from session file
        # Playwright's add_cookies() requires 'url' or 'domain'+'path' on each cookie.
        injectable = []
        for c in cookies:
            entry = {k: v for k, v in c.items() if v is not None}
            # Ensure required field presence
            if "domain" not in entry and "url" not in entry:
                entry["url"] = "https://www.indeed.com"
            injectable.append(entry)

        await context.add_cookies(injectable)
        print("Cookies injected.")

        # Step 6 — navigate to indeed.com
        print("Navigating to https://www.indeed.com …")
        await page.goto("https://www.indeed.com", wait_until="load", timeout=45000)

        # Step 7 — print URL and title
        final_url = page.url
        title = await page.title()
        print(f"Final URL: {final_url}")
        print(f"Page title: {title}")

        # Step 8 — check for auth signs
        html = await page.content()
        has_user_menu = await page.query_selector('[data-testid="header-user-menu"]') is not None
        has_sign_in_text = "Sign in" in html

        if has_user_menu:
            print("Auth check: [data-testid='header-user-menu'] found → session appears AUTHENTICATED")
            passed = True
        elif not has_sign_in_text:
            print("Auth check: 'Sign in' not found in HTML → session appears AUTHENTICATED")
            passed = True
        else:
            print("Auth check: 'Sign in' found and no user-menu element → session appears NOT authenticated")

        # Step 9 — screenshot
        screenshot_path = "/tmp/indeed_session_test.png"
        await page.screenshot(path=screenshot_path, full_page=False)
        print(f"Screenshot saved to {screenshot_path}")

        # Step 10 — wait 3 seconds then close
        await asyncio.sleep(3)

    except Exception as exc:
        print(f"\nERROR during Playwright test: {exc}")
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

    return passed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
loop_passed = asyncio.run(run_test())

print()
if loop_passed:
    print("PASS — Indeed session loaded successfully and page appears authenticated.")
else:
    print("FAIL — Session loaded but authentication could not be confirmed.")
