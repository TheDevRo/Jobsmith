"""
auto_apply/browser_controller.py — Playwright-based browser automation layer.

Wraps Playwright's async API with:
  - Persistent browser profiles (so LinkedIn/Indeed sessions survive)
  - Reliable DOM snapshotting for field detection
  - Deterministic field-filling helpers (no LLM clicks)
  - Human-like timing / stealth to reduce bot detection
  - Structured error returns

Only free OSS: Playwright (Apache-2.0), already in requirements.txt.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page, Playwright

from .models import FieldDescriptor

logger = logging.getLogger(__name__)

# How long to wait (ms) for page-load / network idle after navigation
_NAV_TIMEOUT     = 30_000
_ACTION_TIMEOUT  = 10_000
_UPLOAD_TIMEOUT  = 15_000

# Maximum fields we snapshot at once (prevents enormous LLM prompts)
_MAX_FIELDS = 60

# ---------------------------------------------------------------------------
# User-agent rotation pool
# ---------------------------------------------------------------------------
# Biased toward newer versions; each entry is (weight, ua_string).
_UA_POOL = [
    # Chrome 132 (latest stable as of early 2026)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    # Chrome 131
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 130
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Chrome 129
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]

# Stealth init script — injected before every page load via add_init_script().
# Overrides common bot-detection signals used by Cloudflare, PerimeterX, etc.
_STEALTH_JS = """
() => {
  // 1. Hide webdriver flag
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

  // 2. Fake plugins array (real Chrome always has these)
  Object.defineProperty(navigator, 'plugins', {
    get: () => {
      const makePlugin = (name, filename, desc, mimeType) => {
        const mime = { type: mimeType, suffixes: 'pdf', description: desc, enabledPlugin: null };
        const plugin = { name, filename, description: desc, length: 1, item: (i) => i === 0 ? mime : null, namedItem: (n) => n === mimeType ? mime : null };
        mime.enabledPlugin = plugin;
        return plugin;
      };
      const plugins = [
        makePlugin('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', 'application/pdf'),
        makePlugin('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', 'application/x-google-chrome-pdf'),
        makePlugin('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', 'application/x-chromium-pdf'),
        makePlugin('Microsoft Edge PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format', 'application/x-edge-pdf'),
        makePlugin('WebKit built-in PDF', 'internal-pdf-viewer', 'Portable Document Format', 'application/x-webkit-pdf'),
      ];
      plugins.length = plugins.length;
      plugins.item = (i) => plugins[i] || null;
      plugins.namedItem = (n) => plugins.find(p => p.name === n) || null;
      plugins.refresh = () => {};
      return plugins;
    }
  });

  // 3. Languages
  Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

  // 4. Hardware concurrency (real machine value)
  Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });

  // 5. Remove chrome automation extension fingerprint
  if (window.chrome) {
    const originalChrome = window.chrome;
    const keys = Object.keys(originalChrome).filter(k => k.startsWith('cdc_'));
    keys.forEach(k => { try { delete originalChrome[k]; } catch(e) {} });
  }

  // 6. Prevent permission query from revealing automation
  if (navigator.permissions && navigator.permissions.query) {
    const originalQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (parameters) => (
      parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission, onchange: null })
        : originalQuery(parameters)
    );
  }
}
"""


class BrowserController:
    """
    Manages a single Playwright browser context for one application attempt.

    Usage:
        async with BrowserController(config) as ctrl:
            await ctrl.navigate(url)
            fields = await ctrl.get_dom_snapshot()
            await ctrl.fill_field("field-0", "Jane Doe")
            ...
            await ctrl.screenshot("/tmp/result.png")
    """

    def __init__(
        self,
        config: dict,
        profile_dir: Optional[Path] = None,
        storage_state_path: Optional[Path] = None,
    ) -> None:
        aa = config.get("auto_apply", {})
        self._headless: bool = aa.get("headless", True)
        self._profile_dir: Optional[Path] = profile_dir
        self._storage_state_path: Optional[Path] = storage_state_path

        self._pw:      Optional["Playwright"]      = None
        self._browser: Optional["Browser"]         = None
        self._ctx:     Optional["BrowserContext"]  = None
        self._page:    Optional["Page"]            = None

        # field_id → CSS selector mapping, built during get_dom_snapshot()
        self._field_map: dict[str, str] = {}
        # field_id → element type
        self._field_types: dict[str, str] = {}

        # Pick a random UA once per instance so it's consistent within a session
        self._user_agent: str = random.choice(_UA_POOL)

        # Randomize viewport slightly — avoid the dead-giveaway 1280×720 default
        self._viewport_width:  int = random.randint(1280, 1920)
        self._viewport_height: int = random.randint(720, 1080)

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    @classmethod
    def from_existing_page(cls, page: "Page", config: dict) -> "BrowserController":
        """
        Wrap an already-open Playwright page without owning the browser lifecycle.

        close() will be a no-op so the caller's browser/context stays open.
        Use this for Applicant Assist autofill where launch_assist() owns the browser.
        """
        ctrl = cls.__new__(cls)
        ctrl._headless = False
        ctrl._profile_dir = None
        ctrl._storage_state_path = None
        ctrl._pw = None
        ctrl._browser = None
        ctrl._ctx = page.context
        ctrl._page = page
        ctrl._field_map = {}
        ctrl._field_types = {}
        ctrl._user_agent = random.choice(_UA_POOL)
        ctrl._viewport_width = 1280
        ctrl._viewport_height = 900
        ctrl._external = True  # signals close() to skip teardown
        page.set_default_timeout(_ACTION_TIMEOUT)
        return ctrl

    async def __aenter__(self) -> "BrowserController":
        await self.launch()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def launch(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()

        # Always use Chromium — LinkedIn requires a persistent profile that
        # preserves both cookies AND localStorage (cookie-only loading causes
        # /authwall redirects).  All other adapters also use Chromium for
        # consistency and UA fingerprint correctness.
        browser_type = self._pw.chromium
        _chromium_args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=site-per-process",
        ]

        if self._profile_dir is not None and self._profile_dir.exists():
            # Persistent profile path (LinkedIn): reuse the full Chromium profile
            # saved during login.  Chromium leaves a SingletonLock on unclean exit —
            # remove it so the next launch doesn't refuse to start.
            self._cleanup_lock_files()

            # launch_persistent_context returns a BrowserContext directly —
            # no separate Browser object.  Leave self._browser as None so
            # close() skips the browser.close() call.
            self._ctx = await browser_type.launch_persistent_context(
                str(self._profile_dir),
                headless=self._headless,
                accept_downloads=True,
                args=_chromium_args,
                user_agent=self._user_agent,
                viewport={"width": self._viewport_width, "height": self._viewport_height},
            )
            self._browser = None
            logger.info(
                "BrowserController: using persistent Chromium profile at %s (UA: ...%s)",
                self._profile_dir, self._user_agent[-30:],
            )
            self._page = (
                self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
            )
            # Inject stealth script into persistent context
            await self._ctx.add_init_script(_STEALTH_JS)

            # Inject session cookies from storage_state_path into the persistent
            # context so saved Indeed (or other) session cookies are active while
            # still benefiting from the persistent profile's Cloudflare bypass.
            if self._storage_state_path is not None and self._storage_state_path.exists():
                import json as _json
                state = _json.loads(self._storage_state_path.read_text())
                cookies = state.get("cookies", [])
                if cookies:
                    await self._ctx.add_cookies(cookies)
                    logger.info(
                        "BrowserController: injected %d cookies from storage_state into persistent context",
                        len(cookies),
                    )
        else:
            # Non-session path (Greenhouse, Lever, generic, etc.)
            # Pass storage_state when provided (e.g. Indeed saved session) so
            # the very first navigation is authenticated.
            self._browser = await browser_type.launch(
                headless=self._headless,
                args=_chromium_args,
            )
            _storage_state: Optional[str] = None
            if (
                self._storage_state_path is not None
                and self._storage_state_path.exists()
            ):
                _storage_state = str(self._storage_state_path)
                logger.info(
                    "BrowserController: loading session from %s", self._storage_state_path
                )
            self._ctx = await self._browser.new_context(
                accept_downloads=True,
                user_agent=self._user_agent,
                storage_state=_storage_state,
                viewport={"width": self._viewport_width, "height": self._viewport_height},
            )
            # Inject stealth script
            await self._ctx.add_init_script(_STEALTH_JS)
            self._page = await self._ctx.new_page()

        logger.debug(
            "BrowserController: viewport %dx%d, UA …%s",
            self._viewport_width, self._viewport_height, self._user_agent[-40:],
        )
        self._page.set_default_timeout(_ACTION_TIMEOUT)

    def _cleanup_lock_files(self) -> None:
        """Remove stale Chromium lock files from the persistent profile dir."""
        if not self._profile_dir:
            return
        for lock_name in ("SingletonLock", ".parentlock", "lock"):
            lf = self._profile_dir / lock_name
            try:
                if lf.exists():
                    lf.unlink()
                    logger.info("BrowserController: removed stale lock %s", lf.name)
            except Exception as exc:
                logger.warning(
                    "BrowserController: could not remove lock file %s: %s — retrying", lf, exc
                )
                # One retry after a brief wait
                try:
                    import time as _t
                    _t.sleep(2)
                    lf.unlink()
                    logger.info("BrowserController: removed stale lock %s (retry succeeded)", lf.name)
                except Exception as exc2:
                    raise RuntimeError(
                        f"Browser profile is locked by another process — "
                        f"close any open Chrome instances using {self._profile_dir}. "
                        f"Error: {exc2}"
                    ) from exc2

    async def close(self) -> None:
        if getattr(self, "_external", False):
            # Browser is owned by an external caller (e.g. launch_assist).
            # Just clear local state — don't touch the browser/context/page.
            self._pw = self._browser = self._ctx = self._page = None
            self._field_map.clear()
            self._field_types.clear()
            return
        try:
            if self._ctx:
                await self._ctx.close()
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception as exc:
            logger.debug("BrowserController.close error (ignored): %s", exc)
        finally:
            self._pw = self._browser = self._ctx = self._page = None
            self._field_map.clear()
            self._field_types.clear()

    @property
    def page(self) -> "Page":
        if not self._page:
            raise RuntimeError("BrowserController: browser not launched")
        return self._page

    # ------------------------------------------------------------------
    # Human-like timing
    # ------------------------------------------------------------------

    async def _human_delay(self, min_ms: int = 300, max_ms: int = 1200) -> None:
        """Sleep a random duration to simulate human pacing between actions."""
        await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate(self, url: str, timeout: int = _NAV_TIMEOUT) -> None:
        """
        Navigate to url and wait for the DOM to be ready.

        Uses "domcontentloaded" rather than "networkidle": SPAs like LinkedIn
        maintain persistent WebSocket / polling connections that prevent the
        network from ever going fully idle, which causes networkidle to always
        time out on those sites.
        """
        logger.info("BrowserController: navigating to %s", url[:80])
        await self.page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        # Brief human-like pause after navigation settles
        await self._human_delay(400, 1000)

    async def switch_to_new_page(self, timeout: int = 5000) -> str:
        """
        Handle a newly opened tab/popup (e.g. LinkedIn external apply) and return its
        final destination URL.  If no new page opens within *timeout* ms, returns "".

        Checks already-open pages first so we don't miss a tab that opened before
        this method was called.

        Session-preservation strategy: close the new tab before LinkedIn's redirect-page
        JavaScript can fully execute (which would detect automation and invalidate the
        shared persistent-context session), then navigate the existing warm-session tab
        to the final destination.  This mirrors the legacy _check_new_tab pattern.
        """
        try:
            pages = self._ctx.pages if self._ctx else []
            if len(pages) > 1:
                new_page = pages[-1]
            else:
                new_page = await self._ctx.wait_for_event("page", timeout=timeout)

            # Wait only for the initial URL commit — enough to know where we're headed
            # but before LinkedIn's redirect-page JS runs and can invalidate the session.
            try:
                await new_page.wait_for_load_state("commit", timeout=5_000)
            except Exception:
                await asyncio.sleep(0.5)  # fallback: brief wait for URL to be set

            initial_url = new_page.url or ""

            # If the tab landed on a LinkedIn redirect, wait for it to leave LinkedIn
            # so we capture the real external-ATS URL before closing.
            target_url = initial_url
            if "linkedin.com" in initial_url or not initial_url or initial_url == "about:blank":
                try:
                    await new_page.wait_for_url(
                        lambda u: bool(u) and "linkedin.com" not in u and u != "about:blank",
                        timeout=10_000,
                    )
                    target_url = new_page.url or ""
                except Exception:
                    target_url = new_page.url or initial_url

            # Close the new tab now — keep only one tab live to avoid concurrent-session
            # detection and prevent any further LinkedIn JS from running in that tab.
            try:
                await new_page.close()
            except Exception:
                pass

            if not target_url or target_url == "about:blank":
                logger.debug("switch_to_new_page: new tab had no usable URL")
                return ""

            # Navigate the existing warm-session tab to the external destination.
            logger.info("BrowserController: new tab closed, navigating to %s", target_url[:80])
            await self._page.goto(target_url, wait_until="domcontentloaded", timeout=15_000)
            self._page.set_default_timeout(_ACTION_TIMEOUT)
            self._field_map.clear()
            self._field_types.clear()
            return self._page.url

        except Exception as exc:
            logger.debug("switch_to_new_page: no new page within %dms (%s)", timeout, exc)
            return ""

    async def wait_for_selector(self, selector: str, timeout: int = _ACTION_TIMEOUT) -> bool:
        """Return True if the selector appears within timeout, False otherwise."""
        try:
            await self.page.wait_for_selector(selector, timeout=timeout)
            return True
        except Exception:
            return False

    async def current_url(self) -> str:
        return self.page.url

    async def page_text(self) -> str:
        return await self.page.inner_text("body")

    # ------------------------------------------------------------------
    # DOM snapshot — field detection
    # ------------------------------------------------------------------

    async def get_dom_snapshot(self) -> list[FieldDescriptor]:
        """
        Enumerate all visible, interactive form fields on the current page.

        Returns a list of FieldDescriptor objects, each with a stable field_id
        that can be passed back to fill_field() / select_field() / etc.

        Also rebuilds the internal field_map (field_id → CSS selector).
        """
        self._field_map.clear()
        self._field_types.clear()

        # Inject a helper script that collects field metadata
        raw: list[dict] = await self.page.evaluate(_SNAPSHOT_JS)

        descriptors: list[FieldDescriptor] = []
        for idx, item in enumerate(raw[:_MAX_FIELDS]):
            fid = f"field-{idx}"
            selector = item.get("selector", "")
            if not selector:
                continue

            self._field_map[fid]   = selector
            self._field_types[fid] = item.get("type", "text")

            descriptors.append(
                FieldDescriptor(
                    field_id      = fid,
                    label         = item.get("label", ""),
                    placeholder   = item.get("placeholder", ""),
                    field_type    = item.get("type", "text"),
                    name          = item.get("name", ""),
                    options       = item.get("options") or None,
                    required      = bool(item.get("required", False)),
                    extra_context = item.get("extra_context", ""),
                )
            )

        logger.debug("BrowserController: snapshot found %d fields", len(descriptors))
        return descriptors

    # ------------------------------------------------------------------
    # Field-filling helpers
    # ------------------------------------------------------------------

    async def fill_field(self, field_id: str, value: str) -> bool:
        """Type *value* into a text/textarea/number/email/tel field."""
        selector = self._field_map.get(field_id)
        if not selector:
            logger.warning("fill_field: unknown field_id %s", field_id)
            return False
        try:
            await self._human_delay(200, 600)
            locator = self.page.locator(selector).first
            await locator.scroll_into_view_if_needed()
            await locator.click(click_count=3)  # select-all before typing
            # Randomized per-character keystroke delay simulates natural typing variance
            await locator.type(value, delay=random.randint(25, 95))
            logger.debug("fill_field: %s ← %r", field_id, value[:60])
            return True
        except Exception as exc:
            logger.warning("fill_field %s failed: %s", field_id, exc)
            return False

    async def select_field(self, field_id: str, value: str) -> bool:
        """
        Select an option from a <select> element.

        Tries exact text match first, then partial/case-insensitive match.
        """
        selector = self._field_map.get(field_id)
        if not selector:
            return False
        try:
            await self._human_delay(200, 800)
            locator = self.page.locator(selector).first
            # Try value attribute match
            try:
                await locator.select_option(value=value, timeout=3000)
                return True
            except Exception:
                pass
            # Try label text match
            try:
                await locator.select_option(label=value, timeout=3000)
                return True
            except Exception:
                pass
            # Case-insensitive partial match against available options
            options: list[str] = await locator.evaluate(
                "el => Array.from(el.options).map(o => o.text)"
            )
            target = value.lower()
            match = next(
                (o for o in options if target in o.lower()), None
            )
            if match:
                await locator.select_option(label=match, timeout=3000)
                return True

            logger.warning("select_field %s: no option matching %r", field_id, value)
            return False
        except Exception as exc:
            logger.warning("select_field %s failed: %s", field_id, exc)
            return False

    async def check_field(self, field_id: str) -> bool:
        """Check a checkbox (no-op if already checked)."""
        selector = self._field_map.get(field_id)
        if not selector:
            return False
        try:
            locator = self.page.locator(selector).first
            if not await locator.is_checked():
                await locator.check()
            return True
        except Exception as exc:
            logger.warning("check_field %s failed: %s", field_id, exc)
            return False

    async def click_radio(self, field_id: str, value: str) -> bool:
        """Click the radio button whose label/value matches *value*."""
        # Radio buttons share a name attribute; look for the one matching value
        selector = self._field_map.get(field_id)
        if not selector:
            return False
        try:
            await self._human_delay(150, 500)
            # Try to find a radio with value=value inside the same group
            group_selector = f"input[type='radio'][name='{await self._get_name(field_id)}']"
            radios = self.page.locator(group_selector)
            count = await radios.count()
            for i in range(count):
                radio = radios.nth(i)
                radio_value = await radio.get_attribute("value") or ""
                # Also check its label
                label = await self._get_radio_label(radio)
                if (
                    radio_value.lower() == value.lower()
                    or value.lower() in label.lower()
                ):
                    await radio.check()
                    return True
            logger.warning("click_radio %s: no radio matching %r", field_id, value)
            return False
        except Exception as exc:
            logger.warning("click_radio %s failed: %s", field_id, exc)
            return False

    async def upload_file(self, field_id: str, file_path: str) -> bool:
        """Set a file on a <input type='file'> element."""
        selector = self._field_map.get(field_id)
        if not selector:
            return False
        path = Path(file_path)
        if not path.exists():
            logger.warning("upload_file: file not found %s", file_path)
            return False
        try:
            await self._human_delay(300, 800)
            locator = self.page.locator(selector).first
            async with self.page.expect_file_chooser(timeout=_UPLOAD_TIMEOUT) as fc_info:
                await locator.click()
            file_chooser = await fc_info.value
            await file_chooser.set_files(str(path))
            logger.info("upload_file: uploaded %s to %s", path.name, field_id)
            return True
        except Exception:
            # Fallback: set_input_files directly
            try:
                await self.page.locator(selector).first.set_input_files(str(path))
                return True
            except Exception as exc2:
                logger.warning("upload_file %s failed: %s", field_id, exc2)
                return False

    async def click(self, selector: str, timeout: int = _ACTION_TIMEOUT) -> bool:
        """Click an element by CSS selector."""
        try:
            await self.page.locator(selector).first.click(timeout=timeout)
            return True
        except Exception as exc:
            logger.warning("click %r failed: %s", selector, exc)
            return False

    async def click_text(self, text: str, exact: bool = False) -> bool:
        """Click a visible element containing *text*."""
        try:
            locator = self.page.get_by_text(text, exact=exact)
            await locator.first.click(timeout=_ACTION_TIMEOUT)
            return True
        except Exception as exc:
            logger.warning("click_text %r failed: %s", text, exc)
            return False

    async def dismiss_popups(self) -> bool:
        """
        Dismiss common interstitial popups (cookie banners, newsletter nags, etc.)
        that appear before the actual application form.

        Tries a ranked list of dismiss patterns and clicks the first match.
        Returns True if something was dismissed.
        """
        # Ordered: prefer explicit declines over generic closes
        candidates = [
            # Text-based — most reliable across sites
            "No Thanks",
            "No thanks",
            "No thank you",
            "Not Now",
            "Not now",
            "Maybe Later",
            "Skip",
            "Decline",
            "Reject All",
            "Reject",
            # Aria-label patterns
            "button[aria-label='Close']",
            "button[aria-label='Dismiss']",
            "button[aria-label='No thanks']",
            "button[aria-label='close']",
            # Common class patterns
            "[class*='cookie'] button[class*='decline']",
            "[class*='cookie'] button[class*='reject']",
            "[class*='modal'] button[class*='close']",
            "[class*='popup'] button[class*='close']",
            "[class*='banner'] button[class*='close']",
        ]
        for candidate in candidates:
            # Distinguish CSS selectors (contain [ or . or #) from plain text
            is_selector = any(c in candidate for c in "[.#>:")
            try:
                if is_selector:
                    el = self.page.locator(candidate).first
                else:
                    el = self.page.get_by_text(candidate, exact=False).first
                if await el.is_visible(timeout=500):
                    await el.click(timeout=2000)
                    logger.info("dismiss_popups: dismissed popup via %r", candidate)
                    await self.page.wait_for_timeout(500)
                    return True
            except Exception:
                continue
        return False

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    async def screenshot(self, path: str) -> str:
        """Take a full-page screenshot and return the path."""
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=path, full_page=True)
            return path
        except Exception as exc:
            logger.warning("screenshot failed: %s", exc)
            return ""

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _get_name(self, field_id: str) -> str:
        selector = self._field_map.get(field_id, "")
        if not selector:
            return ""
        try:
            return await self.page.locator(selector).first.get_attribute("name") or ""
        except Exception:
            return ""

    async def _get_radio_label(self, locator) -> str:
        try:
            el_id = await locator.get_attribute("id")
            if el_id:
                label = await self.page.locator(f"label[for='{el_id}']").inner_text(timeout=2000)
                return label
        except Exception:
            pass
        return ""


# ---------------------------------------------------------------------------
# JavaScript injected into the page for field snapshotting
# ---------------------------------------------------------------------------

_SNAPSHOT_JS = """
() => {
  function isVisible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    // Check ancestors too (up to 4 levels)
    let parent = el.parentElement;
    let depth = 0;
    while (parent && depth < 4) {
      const ps = window.getComputedStyle(parent);
      if (ps.display === 'none' || ps.visibility === 'hidden') return false;
      if (parent.getAttribute('aria-hidden') === 'true') return false;
      parent = parent.parentElement;
      depth++;
    }
    return true;
  }

  function getLabel(el) {
    // 1. <label for="id">
    if (el.id) {
      const lbl = document.querySelector('label[for="' + el.id + '"]');
      if (lbl) return lbl.innerText.trim();
    }
    // 2. Wrapping <label>
    const parent = el.closest('label');
    if (parent) return parent.innerText.replace(el.value || '', '').trim();
    // 3. aria-label (direct)
    const ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel) return ariaLabel.trim();
    // 4. aria-labelledby
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const ref = document.getElementById(labelledBy);
      if (ref) return ref.innerText.trim();
    }
    // 5. Nearest preceding <label> or heading sibling
    let prev = el.previousElementSibling;
    while (prev) {
      if (['LABEL','LEGEND','H1','H2','H3','H4','SPAN','P'].includes(prev.tagName)) {
        const t = prev.innerText.trim();
        if (t) return t;
      }
      prev = prev.previousElementSibling;
    }
    return el.name || el.placeholder || '';
  }

  function getHelperText(el) {
    // aria-describedby — often contains helper/hint text
    const describedBy = el.getAttribute('aria-describedby');
    if (describedBy) {
      const parts = describedBy.trim().split(' ').filter(function(s){ return s.length > 0; });
      const texts = parts.map(id => {
        const ref = document.getElementById(id);
        return ref ? ref.innerText.trim() : '';
      }).filter(Boolean);
      if (texts.length) return texts.join(' ');
    }
    return '';
  }

  function getExtraContext(el) {
    try {
      const section = el.closest('fieldset, [role="group"], .form-group, .field, section') || el.parentElement;
      if (!section) return '';
      return section.innerText.slice(0, 200).trim();
    } catch(e) { return ''; }
  }

  function uniqueSelector(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    // Build a path from tag + nth-of-type
    let path = '';
    let node = el;
    while (node && node.tagName && node !== document.body) {
      let idx = 1;
      let sib = node.previousElementSibling;
      while (sib) { if (sib.tagName === node.tagName) idx++; sib = sib.previousElementSibling; }
      path = node.tagName.toLowerCase() + ':nth-of-type(' + idx + ')' + (path ? ' > ' + path : '');
      node = node.parentElement;
    }
    return 'body > ' + path;
  }

  const seen = new Set();
  const fields = [];

  // Collect inputs (exclude hidden/submit/button/image/reset)
  const excludeTypes = new Set(['hidden','submit','button','image','reset','search']);
  document.querySelectorAll('input, textarea, select').forEach(el => {
    const type = (el.type || el.tagName.toLowerCase()).toLowerCase();
    if (excludeTypes.has(type)) return;
    // Skip display:none / aria-hidden / visibility:hidden fields
    if (!isVisible(el)) return;
    if (!el.offsetParent && el.type !== 'file') return;
    const sel = uniqueSelector(el);
    if (seen.has(sel)) return;
    seen.add(sel);

    const helperText = getHelperText(el);
    const extraCtx = getExtraContext(el);

    const entry = {
      selector: sel,
      type: el.tagName === 'SELECT' ? 'select' : (el.tagName === 'TEXTAREA' ? 'textarea' : type),
      name: el.name || '',
      placeholder: el.placeholder || '',
      required: el.required || el.getAttribute('aria-required') === 'true',
      label: getLabel(el),
      autocomplete: el.getAttribute('autocomplete') || '',
      helper_text: helperText,
      extra_context: helperText ? helperText + ' | ' + extraCtx : extraCtx,
    };

    if (el.tagName === 'SELECT') {
      entry.options = Array.from(el.options)
        .slice(1)  // skip first (usually "Select...")
        .map(o => o.text.trim())
        .filter(Boolean);
    }

    fields.push(entry);
  });

  return fields;
}
"""
