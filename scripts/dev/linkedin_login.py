#!/usr/bin/env python3
"""
LinkedIn Login — run this script to sign into LinkedIn.
Your session will be saved and reused by the auto-apply bot.

Usage:
    .venv/bin/python3 linkedin_login.py [firefox|chromium|webkit]
"""

import asyncio
import sys
from pathlib import Path

SESSION_DIR = Path(__file__).resolve().parent / "data" / "linkedin_session"

BROWSER_ARGS = {
    "viewport": {"width": 1280, "height": 900},
    "user_agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


async def main():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright is not installed. Run: pip install playwright && playwright install")
        sys.exit(1)

    browser_name = sys.argv[1] if len(sys.argv) > 1 else "firefox"
    if browser_name not in ("firefox", "chromium", "webkit"):
        print(f"Unknown browser: {browser_name}. Use firefox, chromium, or webkit.")
        sys.exit(1)

    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Opening {browser_name} to LinkedIn login page...")
    print("Please sign in. The window will close automatically when done.\n")

    pw = await async_playwright().start()
    try:
        browser_type = getattr(pw, browser_name)
        context = await browser_type.launch_persistent_context(
            str(SESSION_DIR),
            headless=False,
            **BROWSER_ARGS,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://www.linkedin.com/login", wait_until="networkidle")

        print("Waiting for you to log in (up to 3 minutes)...")
        try:
            await page.wait_for_url("**/feed**", timeout=180000)
        except Exception:
            url = page.url
            if not any(p in url for p in ["/feed", "/mynetwork", "/jobs", "/messaging"]):
                print("\nLogin timed out. Please try again.")
                await context.close()
                await pw.stop()
                sys.exit(1)

        print("\nLinkedIn login successful! Session saved.")
        print("Auto-apply will use your session for LinkedIn jobs.")

        await context.close()
    finally:
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
