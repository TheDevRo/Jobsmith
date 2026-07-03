"""
indeed.py — Fetch jobs from Indeed using Playwright (headless browser).

Navigates to https://www.indeed.com/jobs?q=...&l=...&sort=date, extracts job
cards, and paginates via the "Next" button up to a configurable max_pages
(default 5, configurable at search.indeed.max_pages in config.yaml).

If a saved Indeed session exists (data/indeed_session/storage_state.json),
cookies are injected into the browser context before the first navigation so
the session carries over to the search page.

CAPTCHA / bot-block detection: if div#captcha-box is present on the page, or
the page <title> contains "just a moment", "captcha", "access denied", or
"are you human", a WARNING is logged and an empty list is returned immediately
rather than crashing.

Results are deduplicated by job_id (the data-jk attribute on each job card)
before returning.

Returned dicts share the same schema as every other job source so fetch_all_jobs()
requires no changes:
  source, external_id, title, company, location, url, description,
  salary_min, salary_max, job_type, tags, date_posted, is_remote
"""

import asyncio
import json
import logging
import random
from urllib.parse import urlencode, quote_plus

import aiohttp

try:
    from playwright_stealth import stealth_async as _stealth_async
except ImportError:
    _stealth_async = None

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://www.indeed.com/jobs"

# Overall internal budget. The orchestrator cancels this source at 600s and a
# wait_for cancellation discards everything collected, so Byparr primer +
# search + enrichment must all fit under _TOTAL_BUDGET. The primer runs first
# (up to ~180s worst case), search gets at most _SEARCH_BUDGET of what's left,
# and enrichment gets the remainder capped at _ENRICH_BUDGET.
_TOTAL_BUDGET = 560.0
_SEARCH_BUDGET = 280.0
_ENRICH_BUDGET = 300.0

# Keywords are combined into boolean-OR queries so each search navigation
# covers several keywords at once (Indeed q= syntax: quoted phrases joined
# by "or").
_KEYWORD_BATCH_SIZE = 4


def _batch_keywords(keywords: list[str]) -> list[str]:
    """Group keywords into boolean-OR q= queries. Multi-word keywords are
    quoted so "or" binds to the whole phrase; a batch of one keeps the bare
    keyword to preserve Indeed's fuzzy matching."""
    cleaned = [k.strip() for k in keywords if k and k.strip()]
    batches: list[str] = []
    for i in range(0, len(cleaned), _KEYWORD_BATCH_SIZE):
        chunk = cleaned[i:i + _KEYWORD_BATCH_SIZE]
        if len(chunk) == 1:
            batches.append(chunk[0])
        else:
            batches.append(" or ".join(f'"{k}"' if " " in k else k for k in chunk))
    return batches

# Single source of truth for the Indeed session paths.
from backend.auto_apply import (
    INDEED_SESSION_PATH as _INDEED_SESSION_FILE,
    INDEED_CHROME_PROFILE_DIR as _INDEED_PROFILE_DIR,
)

_INDEED_PROFILE_SENTINEL = "login_success.json"


def _cleanup_profile_locks(profile_dir) -> None:
    """Remove stale Chromium SingletonLock files from a persistent profile dir.

    Chromium leaves these on unclean exit and refuses to relaunch until cleared.
    Mirrors BrowserController._cleanup_lock_files.
    """
    for lock_name in ("SingletonLock", ".parentlock", "lock"):
        lf = profile_dir / lock_name
        try:
            if lf.exists():
                lf.unlink()
        except Exception as exc:
            logger.warning("Indeed scraper: could not remove %s: %s", lf, exc)

# ---------------------------------------------------------------------------
# Selector constants — all collected here so they're easy to update
# ---------------------------------------------------------------------------

# Tried in order; first selector that returns > 0 elements wins.
# .job_seen_beacon is the current Indeed card container (2024+).
# Fallbacks retain compatibility if Indeed restructures.
_CARD_SELECTORS = [
    ".job_seen_beacon",
    "div[data-jk]",
    "li[data-jk]",
    "a[data-jk]",
]

# Tried in order inside each card element
_TITLE_SELECTORS = [
    "a[data-jk] span",
    "a[data-jk]",
    "[class*='jobTitle']",
    "h2[class*='title']",
    ".jobTitle",
    "h2 a",
    "h2",
]
_COMPANY_SELECTORS = [
    "[data-testid='company-name']",
    "[class*='companyName']",
    ".companyName",
]
_LOCATION_SELECTORS = [
    "[data-testid='text-location']",
    "[class*='companyLocation']",
    ".companyLocation",
]
_DATE_SELECTORS = [
    "[data-testid='myJobsStateDate']",
    "span[class*='date']",
    "[class*='date']",
]

# Bot-block detection
_CAPTCHA_SELECTORS = ["div#captcha-box", "#captcha", "div[id*='captcha']"]
_BOT_TITLE_FRAGMENTS = ("just a moment", "captcha", "blocked", "access denied", "are you human", "unusual traffic")


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------

async def _inject_session(context) -> None:
    """
    Inject saved Indeed session cookies into an already-open browser context.

    Mirrors the pattern in auto_apply/adapters/indeed.py: Indeed uses cookies
    (not localStorage) for session state, so add_cookies() is sufficient.
    Never raises — a missing or corrupt session file is silently skipped.
    """
    if not _INDEED_SESSION_FILE.exists():
        return
    try:
        state = json.loads(_INDEED_SESSION_FILE.read_text())
        cookies = state.get("cookies", [])
        if cookies:
            await context.add_cookies(cookies)
            logger.debug("Indeed scraper: injected %d session cookies", len(cookies))
    except Exception as exc:
        logger.warning("Indeed scraper: failed to inject session cookies: %s", exc)


# ---------------------------------------------------------------------------
# Bot-block detection
# ---------------------------------------------------------------------------

async def _is_bot_blocked(page) -> bool:
    """
    Return True if Indeed is showing a CAPTCHA or bot-block page.

    Checks page title first (fast string op), then DOM selectors.
    Never raises.
    """
    try:
        title = (await page.title()).lower()
        if any(frag in title for frag in _BOT_TITLE_FRAGMENTS):
            return True
    except Exception:
        pass
    for sel in _CAPTCHA_SELECTORS:
        try:
            if await page.locator(sel).count() > 0:
                return True
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# Card extraction
# ---------------------------------------------------------------------------

async def _first_text(el, selectors: list[str]) -> str:
    """
    Try each selector against *el* and return the first non-empty text_content.
    Returns empty string if none match.  Never raises.
    """
    for sel in selectors:
        try:
            text = await el.locator(sel).first.text_content(timeout=500)
            if text and text.strip():
                return text.strip()
        except Exception:
            pass
    return ""


async def _extract_cards(page) -> list[dict]:
    """
    Extract job data from all visible job cards on the current page.

    Returns a list of raw dicts — deduplication and exclude-keyword filtering
    happen in fetch_jobs().  Never raises; individual card errors are skipped.
    """
    cards: list[dict] = []

    # Find the right card selector
    card_locator = None
    for sel in _CARD_SELECTORS:
        try:
            count = await page.locator(sel).count()
            if count > 0:
                card_locator = page.locator(sel)
                logger.debug("Indeed scraper: %d cards via %r", count, sel)
                break
        except Exception:
            pass

    if card_locator is None:
        return cards

    for el in await card_locator.all():
        try:
            # data-jk may be on the card element itself (old DOM) or on the
            # title anchor inside the card (.job_seen_beacon / new DOM).
            job_id = (await el.get_attribute("data-jk")) or ""
            if not job_id:
                try:
                    job_id = (
                        await el.locator("a[data-jk]").first.get_attribute(
                            "data-jk", timeout=500
                        )
                    ) or ""
                except Exception:
                    pass
            if not job_id:
                continue

            title = await _first_text(el, _TITLE_SELECTORS)
            if not title:
                continue

            company = await _first_text(el, _COMPANY_SELECTORS)
            location = await _first_text(el, _LOCATION_SELECTORS)
            date_posted = await _first_text(el, _DATE_SELECTORS)

            url = f"https://www.indeed.com/viewjob?jk={job_id}"
            is_remote = "remote" in title.lower() or "remote" in location.lower()

            cards.append({
                "source": "indeed",
                "external_id": job_id,
                "title": title,
                "company": company,
                "location": location,
                "url": url,
                "description": "",
                "salary_min": None,
                "salary_max": None,
                "salary_period": "unknown",
                "job_type": None,
                "tags": [],
                "date_posted": date_posted,
                "is_remote": is_remote,
            })
        except Exception as exc:
            logger.debug("Indeed scraper: error extracting card: %s", exc)

    return cards


# ---------------------------------------------------------------------------
# Byparr (FlareSolverr-compatible) helpers
# ---------------------------------------------------------------------------

async def _byparr_solve(byparr_url: str, url: str) -> dict | None:
    """Solve a URL via Byparr; return {'cookies': [...], 'userAgent': str} or None.

    cf_clearance is bound to both the egress IP and the User-Agent that solved
    it, so callers must propagate the returned UA to whatever client replays
    the cookies — otherwise Cloudflare rejects the cookie on the next request.
    """
    if not byparr_url:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            payload = {"cmd": "request.get", "url": url, "max_timeout": 120}
            async with session.post(byparr_url, json=payload) as resp:
                if resp.status != 200:
                    logger.warning("Indeed scraper: Byparr HTTP %d", resp.status)
                    return None
                data = await resp.json()
        if data.get("status") != "ok":
            logger.warning("Indeed scraper: Byparr error: %s", data.get("message"))
            return None
        sol = data.get("solution") or {}
        cookies = sol.get("cookies") or []
        ua = sol.get("userAgent") or ""
        if not cookies:
            return None
        return {"cookies": cookies, "userAgent": ua}
    except Exception as exc:
        logger.warning("Indeed scraper: Byparr solve failed: %s", exc)
        return None


def _byparr_cookies_to_playwright(cookies: list[dict]) -> list[dict]:
    """Normalize Byparr/FlareSolverr cookies to the Playwright add_cookies shape."""
    out: list[dict] = []
    for c in cookies:
        ck = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain") or ".indeed.com",
            "path": c.get("path") or "/",
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": bool(c.get("secure", False)),
        }
        ss = c.get("sameSite")
        if ss and ss.lower() in ("lax", "strict", "none"):
            ck["sameSite"] = ss.capitalize()
        exp = c.get("expires", -1)
        if isinstance(exp, (int, float)) and exp > 0:
            ck["expires"] = exp
        out.append(ck)
    return out


async def _byparr_get_html(
    session: aiohttp.ClientSession, byparr_url: str, url: str, max_timeout: int = 60
) -> str | None:
    """Fallback fetch through Byparr when direct GET hits a CF challenge or
    rate-limit. Slower (~3-15s per call) but reliably bypasses Cloudflare.
    """
    if not byparr_url:
        return None
    try:
        payload = {"cmd": "request.get", "url": url, "max_timeout": max_timeout}
        async with session.post(
            byparr_url, json=payload,
            timeout=aiohttp.ClientTimeout(total=max_timeout + 30),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        if data.get("status") != "ok":
            return None
        return (data.get("solution") or {}).get("response")
    except Exception as exc:
        logger.debug("Indeed scraper: Byparr fetch failed for %s: %s", url[:80], exc)
        return None


async def _direct_get_html(
    session: aiohttp.ClientSession, url: str, cookies: dict[str, str], ua: str
) -> str | None:
    """Fetch a URL directly via aiohttp with the cf_clearance cookies + UA we
    already solved. Same egress IP + same UA + same cookies = Cloudflare lets
    us through without re-solving. Drastically faster than calling Byparr per
    page (each Byparr call would otherwise re-run a CF challenge).
    """
    try:
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        async with session.get(
            url, cookies=cookies, headers=headers, timeout=aiohttp.ClientTimeout(total=20),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                logger.debug("Indeed scraper: direct GET %s -> HTTP %d", url[:80], resp.status)
                return None
            text = await resp.text()
            # Sanity check: a Cloudflare challenge page is short and contains
            # "Just a moment" or similar. Real /viewjob pages are 100kB+.
            if len(text) < 5_000 and "just a moment" in text.lower():
                return None
            return text
    except Exception as exc:
        logger.debug("Indeed scraper: direct fetch failed for %s: %s", url[:80], exc)
        return None


def _parse_viewjob_html(html_text: str) -> dict:
    """Extract description, salary, and job_type from an Indeed /viewjob page.

    Description comes from the #jobDescriptionText container; salary + period
    come from the JobPosting JSON-LD when present. Never raises.
    """
    out: dict = {
        "description": "",
        "salary_min": None,
        "salary_max": None,
        "salary_period": "unknown",
        "job_type": None,
    }
    if not html_text:
        return out
    try:
        from bs4 import BeautifulSoup
        from . import clean_description as _clean

        soup = BeautifulSoup(html_text, "html.parser")
        desc_el = soup.select_one("#jobDescriptionText")
        if desc_el:
            out["description"] = _clean(desc_el.decode_contents())

        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "")
            except Exception:
                continue
            postings = data if isinstance(data, list) else [data]
            for posting in postings:
                if not isinstance(posting, dict) or posting.get("@type") != "JobPosting":
                    continue
                bs_field = posting.get("baseSalary") or {}
                val = bs_field.get("value") or {}
                unit = (val.get("unitText") or "").lower()
                if unit == "hour":
                    out["salary_period"] = "hourly"
                elif unit == "year":
                    out["salary_period"] = "annual"
                mn = val.get("minValue") if isinstance(val, dict) else None
                mx = val.get("maxValue") if isinstance(val, dict) else None
                v = val.get("value") if isinstance(val, dict) else None
                if isinstance(mn, (int, float)):
                    out["salary_min"] = int(mn)
                elif isinstance(v, (int, float)):
                    out["salary_min"] = int(v)
                if isinstance(mx, (int, float)):
                    out["salary_max"] = int(mx)
                elif isinstance(v, (int, float)):
                    out["salary_max"] = int(v)
                et = posting.get("employmentType")
                if isinstance(et, list) and et:
                    out["job_type"] = str(et[0])
                elif isinstance(et, str):
                    out["job_type"] = et
                return out
    except Exception as exc:
        logger.debug("Indeed scraper: viewjob parse error: %s", exc)
    return out


async def _enrich_card(
    session: aiohttp.ClientSession,
    card: dict,
    cookies: dict[str, str],
    ua: str,
    byparr_url: str,
    direct_sem: asyncio.Semaphore,
    byparr_sem: asyncio.Semaphore,
) -> None:
    """Fetch /viewjob and merge description/salary/job_type into card.

    Strategy: try plain aiohttp first (fast, ~1-3s per call when CF cookies
    are warm). If it fails or returns a CF challenge, fall back to Byparr
    (slow but reliable). Two semaphores so a slow Byparr fallback queue
    doesn't starve the fast direct path.
    """
    async with direct_sem:
        html_text = await _direct_get_html(session, card["url"], cookies, ua)
    if not html_text and byparr_url:
        async with byparr_sem:
            html_text = await _byparr_get_html(session, byparr_url, card["url"])
    if not html_text:
        return
    parsed = _parse_viewjob_html(html_text)
    if parsed["description"]:
        card["description"] = parsed["description"]
    if parsed["salary_min"] is not None:
        card["salary_min"] = parsed["salary_min"]
    if parsed["salary_max"] is not None:
        card["salary_max"] = parsed["salary_max"]
    if parsed["salary_period"] != "unknown":
        card["salary_period"] = parsed["salary_period"]
    if parsed["job_type"]:
        card["job_type"] = parsed["job_type"]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def detect_indeed_quick_apply(job: dict) -> str:
    """Classify a stored Indeed job dict as 'quick_apply', 'external', or 'unknown'.

    Works only from data already present in the dict — no network calls are made.

    Signals checked, in order:
      1. Metadata flags — ``is_quick_apply``, ``indeedApplyEnabled``, or
         ``isIndeedApply``.  These are set when a richer scraper or JSON-LD
         enrichment step has already resolved the apply type.
      2. URL hostname is ``smartapply.indeed.com`` — Indeed's dedicated Quick
         Apply subdomain; always means Quick Apply regardless of path.
      3. URL path contains ``apply`` or ``applystart`` as a standalone segment
         on an indeed.com domain — matches ``/apply/``, ``/applystart/``, etc.
      4. URL hostname is not an indeed.com domain — indicates an external
         redirect to an ATS or third-party site (Applicant Assist flow).

    Returns:
      ``'quick_apply'``  — Indeed Quick Apply is available (handled fully in-app).
      ``'external'``     — job URL points to a non-Indeed domain.
      ``'unknown'``      — not enough stored information to classify.
    """
    from urllib.parse import urlparse

    # Signal 1: metadata flags already resolved by a scraper / enrichment step
    if job.get("is_quick_apply") or job.get("indeedApplyEnabled") or job.get("isIndeedApply"):
        return "quick_apply"

    url = (job.get("url") or "").strip()
    if not url:
        return "unknown"

    parsed = urlparse(url)
    hostname = parsed.netloc.lower()
    # Normalise: strip leading "www." so indeed.com and www.indeed.com both match
    if hostname.startswith("www."):
        hostname = hostname[4:]

    # Signal 2: smartapply.indeed.com is Indeed's dedicated Quick Apply host
    if hostname == "smartapply.indeed.com":
        return "quick_apply"

    # Determine whether the URL is on the indeed.com domain at all
    is_indeed = hostname == "indeed.com" or hostname.endswith(".indeed.com")

    # Signal 4: any non-empty, non-Indeed hostname is an external redirect
    if hostname and not is_indeed:
        return "external"

    # Signal 3: apply-related path segment on an indeed.com URL
    # Matches /apply/ and /applystart/ (Indeed Quick Apply entry points)
    path_parts = [p for p in parsed.path.split("/") if p]
    if any(p in ("apply", "applystart") for p in path_parts):
        return "quick_apply"

    return "unknown"


async def fetch_jobs(config: dict, known_ids: set[str] | None = None) -> list[dict]:
    """
    Fetch jobs from Indeed using Playwright for each OR-batched keyword query × location.

    Config keys read:
      search.keywords           — list of query strings (required)
      search.locations          — list of location strings (default: [""])
      search.exclude_keywords   — titles containing these are dropped
      search.min_salary         — passed as &salary= query param
                                  (legacy alias: search.salary_floor)
      search.max_age_days       — passed as &fromage= query param
      search.indeed.max_pages   — max pagination depth per query (default 5)

    known_ids — external_ids already in the DB; those cards skip the /viewjob
    enrichment fetch (the stored record already has the description).
    """
    from . import compile_exclude_patterns, matches_exclude

    search = config.get("search", {})
    keywords: list[str] = search.get("keywords", [])
    locations: list[str] = search.get("locations", [""])
    exclude_patterns = compile_exclude_patterns(search.get("exclude_keywords", []))
    salary_floor: int | None = search.get("min_salary") or search.get("salary_floor")
    max_age_days = search.get("max_age_days")
    max_pages: int = search.get("indeed", {}).get("max_pages", 5)
    byparr_url: str = (config.get("flaresolverr") or {}).get("url", "")
    known = known_ids or set()

    if not keywords:
        return []

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Indeed scraper: playwright not installed — skipping")
        return []

    results: list[dict] = []
    seen_ids: set[str] = set()

    # The clock starts before the Byparr primer: a slow solve (up to ~180s)
    # must eat into the search/enrichment budgets, not push the total past
    # the orchestrator's per-source timeout.
    loop = asyncio.get_event_loop()
    hard_deadline = loop.time() + _TOTAL_BUDGET

    queries = _batch_keywords(keywords)
    logger.info("Indeed scraper: %d keywords batched into %d queries", len(keywords), len(queries))

    # Solve Cloudflare via Byparr first so we know what UA Byparr used. The
    # cf_clearance cookie is bound to that UA, so we must launch the browser
    # with the same one or Cloudflare will reject the cookie on replay. The
    # solution covers any indeed.com path, so a single solve is enough for
    # all query × location combinations on this run.
    primer_url = (
        f"{_SEARCH_URL}?{urlencode({'q': queries[0], 'sort': 'date', 'l': (locations[0] or '').strip()}, quote_via=quote_plus)}"
        if locations else f"{_SEARCH_URL}?{urlencode({'q': queries[0], 'sort': 'date'}, quote_via=quote_plus)}"
    )
    byparr_solution: dict | None = None
    if byparr_url:
        byparr_solution = await _byparr_solve(byparr_url, primer_url)
        if byparr_solution:
            logger.info(
                "Indeed scraper: Byparr primed (cookies=%d, ua=%r)",
                len(byparr_solution["cookies"]), byparr_solution["userAgent"][:60],
            )
        else:
            logger.warning("Indeed scraper: Byparr unavailable; falling back to direct nav")

    pw = browser = None
    try:
        pw = await async_playwright().start()
        chromium_args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=site-per-process",
        ]
        # If Byparr primed us, replay its UA so cf_clearance binds. Otherwise
        # use a generic Chrome UA (legacy path; only useful when the persistent
        # profile already has fresh CF cookies).
        ua = (byparr_solution or {}).get("userAgent") or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        )

        # When Byparr primed us, an ephemeral context is the cleanest path:
        # the persistent profile carries stale cf_clearance / __cf_bm cookies
        # that conflict with the freshly-solved ones, and Byparr's solution
        # is sufficient by itself. Only fall back to the persistent profile
        # when Byparr is unavailable.
        sentinel = _INDEED_PROFILE_DIR / _INDEED_PROFILE_SENTINEL
        use_persistent = (
            byparr_solution is None
            and _INDEED_PROFILE_DIR.is_dir()
            and sentinel.exists()
        )

        if use_persistent:
            _cleanup_profile_locks(_INDEED_PROFILE_DIR)
            context = await pw.chromium.launch_persistent_context(
                str(_INDEED_PROFILE_DIR),
                headless=True,
                args=chromium_args,
                user_agent=ua,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                accept_downloads=False,
            )
            browser = None
            logger.info("Indeed scraper: using persistent profile at %s", _INDEED_PROFILE_DIR)
        else:
            try:
                browser = await pw.chromium.launch(
                    headless=True, channel="chrome", args=chromium_args,
                )
            except Exception:
                browser = await pw.chromium.launch(headless=True, args=chromium_args)
            ctx_kwargs: dict = {
                "user_agent": ua,
                "viewport": {"width": 1280, "height": 800},
                "locale": "en-US",
            }
            # Skip the stale storage_state when Byparr provides fresh cookies —
            # the old session bag would just contend with them.
            if byparr_solution is None and _INDEED_SESSION_FILE.exists():
                ctx_kwargs["storage_state"] = str(_INDEED_SESSION_FILE)
            elif byparr_solution is None:
                logger.warning(
                    "Indeed scraper: no persistent profile or session — results may be "
                    "blocked. Run Settings → Connect Indeed Account first.",
                )
            context = await browser.new_context(**ctx_kwargs)

        # Inject Byparr's solved cookies into the context so the first goto
        # arrives already past Cloudflare. Same egress IP + same UA + these
        # cookies = clean 200 from Indeed.
        if byparr_solution:
            try:
                await context.add_cookies(
                    _byparr_cookies_to_playwright(byparr_solution["cookies"])
                )
            except Exception as exc:
                logger.warning("Indeed scraper: cookie inject failed: %s", exc)

        page = await context.new_page()
        if _stealth_async is not None:
            await _stealth_async(page)

        # Internal search budget so we always have time to return collected
        # results + run enrichment before the orchestrator's wait_for kicks in.
        # Bounded both relatively (_SEARCH_BUDGET from now) and absolutely
        # (leave at least 120s of the total for enrichment, even after a slow
        # Byparr primer).
        search_deadline = min(loop.time() + _SEARCH_BUDGET, hard_deadline - 120.0)

        for query in queries:
            if loop.time() >= search_deadline:
                logger.warning(
                    "Indeed scraper: search budget hit, abandoning remaining "
                    "queries; %d jobs collected so far", len(results),
                )
                break
            for location in locations:
                if loop.time() >= search_deadline:
                    break
                loc_str = location.strip() if location else ""
                params: dict = {"q": query, "sort": "date"}
                if loc_str:
                    params["l"] = loc_str
                if salary_floor:
                    params["salary"] = str(salary_floor)
                if max_age_days:
                    params["fromage"] = str(int(max_age_days))

                base_qs = urlencode(params, quote_via=quote_plus)

                for page_num in range(1, max_pages + 1):
                    # Direct URL navigation per page using &start=N — Indeed's
                    # JS Next-button flow tends to trip a fresh CF challenge,
                    # while a clean GET with the same cf_clearance + UA does
                    # not. start=0 is page 1, start=10 page 2, etc.
                    page_url = (
                        f"{_SEARCH_URL}?{base_qs}"
                        if page_num == 1
                        else f"{_SEARCH_URL}?{base_qs}&start={(page_num - 1) * 10}"
                    )
                    try:
                        await page.goto(
                            page_url, wait_until="domcontentloaded", timeout=30_000
                        )
                        # Skip networkidle — Indeed's analytics/tracking calls
                        # keep the network busy long after the cards are
                        # visible. wait_for_selector below is the real signal.
                        await page.wait_for_timeout(500 + random.randint(0, 500))
                    except Exception as exc:
                        logger.warning(
                            "Indeed scraper: navigation failed for query=%r page=%d: %s",
                            query, page_num, exc,
                        )
                        break

                    # Bot-block check — stop pagination for this keyword/location
                    # but keep any jobs already collected.
                    if await _is_bot_blocked(page):
                        logger.warning(
                            "Indeed scraper: CAPTCHA/bot-block detected "
                            "(query=%r location=%r page=%d) — stopping pagination, "
                            "returning %d jobs collected so far",
                            query, loc_str, page_num, len(results),
                        )
                        break

                    # Wait for at least one card selector to appear. 3s per
                    # selector (was 8s) — pages that haven't rendered cards by
                    # then aren't going to.
                    cards_appeared = False
                    for sel in _CARD_SELECTORS:
                        try:
                            await page.wait_for_selector(sel, timeout=3_000)
                            cards_appeared = True
                            break
                        except Exception:
                            pass

                    if not cards_appeared:
                        logger.debug(
                            "Indeed scraper: no cards on page %d for query=%r",
                            page_num, query,
                        )
                        break

                    cards = await _extract_cards(page)
                    for card in cards:
                        ext_id = card.get("external_id", "")
                        if not ext_id or ext_id in seen_ids:
                            continue
                        if matches_exclude(card.get("title", ""), exclude_patterns):
                            continue
                        seen_ids.add(ext_id)
                        results.append(card)

                    if page_num >= max_pages:
                        break

                    # Brief random pause before the next page request to spread
                    # out load and reduce rate-limit triggers.
                    await page.wait_for_timeout(1_500 + random.randint(0, 1_000))

        await page.close()
        await context.close()

    except Exception as exc:
        logger.warning("Indeed scraper: unexpected error: %s", exc)
    finally:
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

    # Enrich each card with description, salary, and job_type from /viewjob.
    # Direct aiohttp call with the cf_clearance + UA already solved — much
    # faster than re-solving via Byparr per page. Bounded by both an enrichment
    # budget (so we never blow the orchestrator's per-source wait_for) and the
    # remaining time after search/extract finished.
    to_enrich = [c for c in results if c.get("external_id") not in known]
    if len(to_enrich) < len(results):
        logger.info(
            "Indeed scraper: skipping enrichment for %d cards already in DB",
            len(results) - len(to_enrich),
        )
    if to_enrich and byparr_solution:
        cf_cookies = {c["name"]: c["value"] for c in byparr_solution["cookies"]}
        ua_for_enrich = byparr_solution["userAgent"]
        # Direct path can run wide; Byparr path serializes through a single
        # browser internally, so a small concurrency there avoids queue
        # build-up. Total budget unchanged.
        direct_sem = asyncio.Semaphore(8)
        byparr_sem = asyncio.Semaphore(2)
        # Whatever remains of the overall budget, capped at the phase max.
        enrich_timeout = max(30.0, min(_ENRICH_BUDGET, hard_deadline - loop.time()))
        try:
            connector = aiohttp.TCPConnector(limit=10)
            async with aiohttp.ClientSession(connector=connector) as session:
                await asyncio.wait_for(
                    asyncio.gather(
                        *[
                            _enrich_card(
                                session, card, cf_cookies, ua_for_enrich,
                                byparr_url, direct_sem, byparr_sem,
                            )
                            for card in to_enrich
                        ],
                        return_exceptions=True,
                    ),
                    timeout=enrich_timeout,
                )
        except asyncio.TimeoutError:
            logger.warning(
                "Indeed scraper: enrichment budget hit; returning %d cards "
                "(some may have empty descriptions)",
                len(results),
            )
        enriched = sum(1 for c in to_enrich if c.get("description"))
        logger.info(
            "Indeed scraper: enriched %d/%d cards with viewjob details",
            enriched, len(to_enrich),
        )

    logger.info("Indeed scraper: %d jobs collected", len(results))
    return results
