"""
Standalone script to test whether the persistent Indeed Chrome profile
produces an authenticated Playwright browser session.

Does NOT import anything from backend/.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Step 1 — Profile directory
# ---------------------------------------------------------------------------
PROFILE_DIR = Path.home() / "jobsmith/data/indeed_chrome_profile"

print(f"Profile path: {PROFILE_DIR}")
print(f"Exists: {PROFILE_DIR.exists()}")

if PROFILE_DIR.exists():
    entries = sorted(PROFILE_DIR.iterdir())
    print(f"Contents ({len(entries)} entries):")
    for e in entries:
        kind = "DIR " if e.is_dir() else "FILE"
        size = f"  {e.stat().st_size} bytes" if e.is_file() else ""
        print(f"  [{kind}] {e.name}{size}")
else:
    print("  (directory does not exist)")


async def run_test() -> bool:
    from playwright.async_api import async_playwright

    pw = None
    context = None
    passed = False

    try:
        pw = await async_playwright().start()

        # Step 2 — launch with persistent profile, no storage_state injection
        print(f"\nLaunching Chromium with persistent profile at {PROFILE_DIR} …")
        context = await pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )

        page = context.pages[0] if context.pages else await context.new_page()

        # Step 3 — navigate to indeed.com
        print("Navigating to https://www.indeed.com …")
        await page.goto("https://www.indeed.com", wait_until="load", timeout=45000)

        # Step 4 — URL and title
        final_url = page.url
        title = await page.title()
        print(f"Final URL:  {final_url}")
        print(f"Page title: {title}")

        # Step 5 — auth check
        user_menu = await page.query_selector('[data-testid="header-user-menu"]')
        html = await page.content()
        has_sign_in = "Sign in" in html

        if user_menu:
            print("Auth check: [data-testid='header-user-menu'] found → AUTHENTICATED")
            passed = True
        elif not has_sign_in:
            print("Auth check: 'Sign in' absent from HTML → AUTHENTICATED")
            passed = True
        else:
            print("Auth check: 'Sign in' present, user-menu absent → NOT authenticated")

        # Step 6 — screenshot
        screenshot_path = "/tmp/indeed_profile_test.png"
        await page.screenshot(path=screenshot_path, full_page=False)
        print(f"Screenshot saved to {screenshot_path}")

        # Step 7 — wait then close
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


passed = asyncio.run(run_test())

print()
if passed:
    print("PASS — persistent profile loaded and page appears authenticated.")
else:
    print("FAIL — persistent profile loaded but authentication could not be confirmed.")
