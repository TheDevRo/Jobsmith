"""
linkedin.py — Fetch jobs from LinkedIn's public job search (no login required).

Uses LinkedIn's public guest job search API with proper filter parameters
for location, remote work, and recency.  After collecting search results,
fetches each job's detail page to extract the full description, salary
information, and criteria tags.
"""

import asyncio
import json
import logging
import random
import re
from pathlib import Path
from urllib.parse import quote_plus

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Seed dict of verified LinkedIn geoIds.
# Only entries confirmed via reverse-lookup against linkedin.com belong here —
# an earlier version of this dict had "denver" mapped to 105072130, which is
# actually *Poland*. That single bad entry caused every "Denver" search to
# silently return Polish job postings. Lesson: do NOT add unverified geoIds.
# The live resolver below is authoritative; this dict is only for offline use.
_SEED_GEO_IDS = {
    "united states": "103644278",
    "us": "103644278",
    "usa": "103644278",
    "remote": "92000001",       # LinkedIn's "Remote anywhere" pseudo-geo
    # Bare "denver" resolves to a different (smaller, likely Denver, NC) geoId
    # on LinkedIn. The disambiguated form lands on Denver, CO. We map both the
    # bare and qualified spellings to the CO geoId because that's the common
    # intent in this tool's primary use case.
    "denver": "103736294",
    "denver co": "103736294",
    "denver colorado": "103736294",
    "colorado": "105763813",
    "co": "105763813",
}

# LinkedIn f_WT values: 1=On-site, 2=Remote, 3=Hybrid
REMOTE_FILTER = "2"

# GeoIds that represent entire states or countries — distance=25 is meaningless
# (or harmful) for these because LinkedIn anchors the radius on an arbitrary
# centroid rather than any city center.
# Only the seed-dict states/countries are listed here; resolver-returned
# geoIds skip the distance=25 hint by default (see _add_distance_param).
_STATE_OR_COUNTRY_GEO_IDS = {
    "103644278",  # United States
    "105763813",  # Colorado (state)
}

# Persistent cache of resolved geoIds.  Populated lazily by _resolve_geo_id.
# Stored on disk so resolution is one-time per location across runs.
_GEO_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "linkedin_geoid_cache.json"
_geo_cache: dict[str, str] | None = None
_geo_cache_lock = asyncio.Lock()
# Hidden form field LinkedIn embeds in /jobs/search?location=... pages
_GEOID_HTML_RE = re.compile(r'geoId"\s*value="(\d+)"')


def _load_geo_cache() -> dict[str, str]:
    global _geo_cache
    if _geo_cache is not None:
        return _geo_cache
    try:
        if _GEO_CACHE_PATH.exists():
            with _GEO_CACHE_PATH.open("r", encoding="utf-8") as f:
                _geo_cache = json.load(f)
                if not isinstance(_geo_cache, dict):
                    _geo_cache = {}
        else:
            _geo_cache = {}
    except Exception:
        logger.exception("Failed to load LinkedIn geoId cache; starting empty")
        _geo_cache = {}
    return _geo_cache


def _save_geo_cache() -> None:
    if _geo_cache is None:
        return
    try:
        _GEO_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _GEO_CACHE_PATH.open("w", encoding="utf-8") as f:
            json.dump(_geo_cache, f, indent=2, sort_keys=True, ensure_ascii=False)
    except Exception:
        logger.exception("Failed to write LinkedIn geoId cache")


async def _resolve_geo_id(
    session: aiohttp.ClientSession,
    location: str,
    headers: dict,
) -> str:
    """Resolve a location string to a LinkedIn geoId.

    Resolution order:
      1. On-disk cache (instant after first run).
      2. Seed dict of common US locations (offline fallback).
      3. Live lookup against LinkedIn's /jobs/search page, which embeds the
         resolved geoId in a hidden form field.

    Returns "" if the location can't be resolved (caller falls back to
    sending text-only location, which is LinkedIn's pre-fix behavior).
    """
    norm = _normalize_location(location)
    if not norm:
        return ""

    cache = _load_geo_cache()
    if norm in cache:
        return cache[norm]
    if norm in _SEED_GEO_IDS:
        return _SEED_GEO_IDS[norm]

    # Live lookup, serialized so concurrent fetches don't all race for the
    # same location at startup.
    async with _geo_cache_lock:
        if norm in cache:  # double-check under lock
            return cache[norm]

        url = f"https://www.linkedin.com/jobs/search?location={quote_plus(location)}"
        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "LinkedIn geoId lookup for '%s' returned %d", location, resp.status,
                    )
                    return ""
                html = await resp.text()
        except Exception:
            logger.exception("LinkedIn geoId lookup failed for '%s'", location)
            return ""

        match = _GEOID_HTML_RE.search(html)
        if not match:
            logger.warning("LinkedIn geoId not found in lookup HTML for '%s'", location)
            return ""

        geo = match.group(1)
        cache[norm] = geo
        _save_geo_cache()
        logger.info("LinkedIn: resolved '%s' -> geoId %s (cached)", location, geo)
        return geo

# Throttle requests to avoid 429s
_SEARCH_DELAY = 1.5  # seconds between search page requests
_DETAIL_DELAY = 2.0  # seconds between detail page requests (sequential fallback)
_DETAIL_CONCURRENCY = 2  # parallel detail-page workers (starts are paced by _DetailThrottle)
_DETAIL_SPACING = 1.2  # min seconds between detail request starts, across all workers
_DETAIL_PHASE_BUDGET = 420.0  # cap (seconds) on the detail-fetch phase as a whole
_MAX_RETRIES = 2
_RETRY_BASE = 5.0  # base seconds for exponential backoff on 429


class _DetailThrottle:
    """Global pacing for detail-page requests.

    Serializes request *starts* a minimum interval apart, and on 429 pushes
    the shared cooldown out so every worker backs off — without this, one
    worker sleeping on a 429 just means the others keep hammering and inherit
    the rate limit.
    """

    def __init__(self, spacing: float = _DETAIL_SPACING):
        self._spacing = spacing
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            delay = self._next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_at = loop.time() + self._spacing + random.uniform(0, 0.3)

    def backoff(self, seconds: float) -> None:
        """Push the next allowed request start at least *seconds* out."""
        loop = asyncio.get_running_loop()
        self._next_at = max(self._next_at, loop.time() + seconds)

# Overall internal budget. The orchestrator cancels this source at 600s and
# a wait_for cancellation discards EVERYTHING collected, so both phases must
# fit under it with margin: search is capped at _SEARCH_PHASE_BUDGET and the
# detail phase gets whatever remains of _TOTAL_BUDGET (at most
# _DETAIL_PHASE_BUDGET).
_TOTAL_BUDGET = 560.0
_SEARCH_PHASE_BUDGET = 240.0

# Keywords are combined into boolean-OR search queries so 11 configured
# keywords cost ~3 searches per location instead of 11.
_KEYWORD_BATCH_SIZE = 4


def _batch_keywords(keywords: list[str], size: int = _KEYWORD_BATCH_SIZE) -> list[str]:
    """Group keywords into boolean-OR queries for the guest search API.

    Multi-word keywords are quoted so OR binds to the whole phrase. A batch
    of one keeps the bare keyword to preserve LinkedIn's fuzzy matching.
    """
    cleaned = [k.strip() for k in keywords if k and k.strip()]
    batches: list[str] = []
    for i in range(0, len(cleaned), size):
        chunk = cleaned[i:i + size]
        if len(chunk) == 1:
            batches.append(chunk[0])
        else:
            batches.append(" OR ".join(f'"{k}"' if " " in k else k for k in chunk))
    return batches


def _normalize_location(loc: str) -> str:
    return loc.strip().lower().replace(",", "").replace("  ", " ")


def _is_location_match(job_location: str, config_locations: list[str]) -> bool:
    """Check if a job's location matches any of the configured locations.

    Returns True when job_location is empty — LinkedIn's guest API often
    omits location from search cards. The detail page fetch may populate it
    later, and filtering on an empty string would silently drop valid jobs.
    """
    if not job_location:
        return True
    loc_lower = job_location.lower()

    for config_loc in config_locations:
        cl = config_loc.strip().lower()
        if not cl:
            continue
        if cl == "remote" and "remote" in loc_lower:
            return True
        if cl in loc_lower:
            return True
        if loc_lower.startswith(cl):
            return True

    return False


def _strip_html(html_text: str) -> str:
    """Delegated to shared sanitizer; kept for callsite compatibility."""
    from . import clean_description
    return clean_description(html_text)


def _parse_salary(text: str) -> tuple[int | None, int | None, str]:
    """
    Extract salary range from a string like '$80,000/yr - $120,000/yr' or
    '$25.00/hr - $30.00/hr'. Returns (min, max, period) where period is one of
    'hourly' | 'annual' | 'unknown'. Hourly values are preserved raw — caller
    must normalize for comparison.
    """
    from . import detect_pay_period

    period = detect_pay_period(text)

    # Capture the k-suffix per amount ("$80k") instead of checking the whole
    # string for the letter k — "401k" elsewhere in the text must not turn an
    # hourly "$25" into "$25,000".
    amounts = re.findall(r"\$\s*([\d,]+(?:\.\d+)?)(?:\s*([kK])\b)?", text)
    if not amounts:
        return None, None, period

    nums: list[int] = []
    for raw, k_suffix in amounts:
        cleaned = raw.replace(",", "")
        try:
            val = float(cleaned)
            if k_suffix:
                val *= 1000
            nums.append(int(val))
        except ValueError:
            continue

    if not nums:
        return None, None, period

    # For annual postings, drop values that are obviously hourly rates that
    # leaked in as standalone numbers. For hourly we keep everything.
    if period != "hourly":
        nums = [n for n in nums if n >= 15000]

    if len(nums) >= 2:
        return min(nums), max(nums), period
    if len(nums) == 1:
        return nums[0], None, period
    return None, None, period


def _parse_search_card(card) -> dict | None:
    """Extract raw fields from one guest-search result card (<li> element).

    Returns None when the card lacks a title or link (ads, spacer nodes).
    Standalone function so fixture tests catch LinkedIn DOM changes before
    they silently zero out the whole source.
    """
    title_el = card.find("h3", class_=re.compile("base-search-card__title"))
    link_el = card.find("a", class_=re.compile("base-card__full-link"))
    if not title_el or not link_el:
        return None

    company_el = card.find("h4", class_=re.compile("base-search-card__subtitle"))
    location_el = card.find("span", class_=re.compile("job-search-card__location"))
    time_el = card.find("time")

    job_url = link_el.get("href", "").split("?")[0]
    job_id_match = re.search(r"/view/[^/]+-(\d+)", job_url)

    return {
        "title": title_el.get_text(strip=True),
        "company": company_el.get_text(strip=True) if company_el else "",
        "location": location_el.get_text(strip=True) if location_el else "",
        "url": job_url,
        "external_id": job_id_match.group(1) if job_id_match else job_url,
        "date_posted": time_el.get("datetime", "") if time_el else "",
    }


def _parse_json_ld(soup: BeautifulSoup) -> dict | None:
    """Extract the JSON-LD JobPosting blob embedded in the page, if present."""
    import json as _json
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = _json.loads(script.string or "")
            # Can be a single object or a list
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") == "JobPosting":
                        return item
            elif isinstance(data, dict):
                if data.get("@type") == "JobPosting":
                    return data
                # Sometimes wrapped in @graph
                for item in data.get("@graph", []):
                    if isinstance(item, dict) and item.get("@type") == "JobPosting":
                        return item
        except (_json.JSONDecodeError, TypeError):
            continue
    return None


def _extract_salary_from_ld(ld: dict) -> tuple[int | None, int | None, str]:
    """
    Pull salary from JSON-LD baseSalary. Schema.org uses `unitText` to specify
    the period: HOUR, DAY, WEEK, MONTH, YEAR. Returns (min, max, period) where
    period is 'hourly' | 'annual' | 'unknown'. Day/week/month not yet handled
    distinctly — fall through to 'unknown'.
    """
    base = ld.get("baseSalary")
    if not base or not isinstance(base, dict):
        return None, None, "unknown"
    value = base.get("value", {})
    if not isinstance(value, dict):
        return None, None, "unknown"

    unit = (value.get("unitText") or "").upper()
    if unit == "HOUR":
        period = "hourly"
    elif unit == "YEAR":
        period = "annual"
    else:
        period = "unknown"

    s_min = value.get("minValue")
    s_max = value.get("maxValue")
    try:
        s_min = int(float(s_min)) if s_min else None
        s_max = int(float(s_max)) if s_max else None
    except (ValueError, TypeError):
        return None, None, period

    # Don't drop low values when LinkedIn explicitly tells us it's hourly.
    if period != "hourly":
        if s_min and s_min < 15000:
            s_min = None
        if s_max and s_max < 15000:
            s_max = None

    # If period unknown but values look hourly-shaped, infer.
    if period == "unknown" and s_min is not None and s_min < 1000:
        period = "hourly"

    return s_min, s_max, period


async def _fetch_job_detail(
    session: aiohttp.ClientSession,
    job: dict,
    headers: dict,
    throttle: _DetailThrottle | None = None,
) -> None:
    """Fetch a single job's detail page and update the dict in-place.

    Uses JSON-LD structured data as the primary extraction method (most
    reliable), then falls back to HTML selectors.  Retries on 429 with
    exponential backoff; when a throttle is provided, the backoff is shared
    across all detail workers.
    """
    job_url = job.get("url", "")
    if not job_url:
        return

    html = None
    for attempt in range(_MAX_RETRIES + 1):
        if throttle is not None:
            await throttle.wait()
        try:
            async with session.get(
                job_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
                allow_redirects=True,
            ) as resp:
                if resp.status == 429:
                    retry_after = float(resp.headers.get("Retry-After", 0))
                    wait = max(retry_after, _RETRY_BASE * (2 ** attempt))
                    if attempt < _MAX_RETRIES:
                        logger.warning(
                            "LinkedIn 429 for %s — retrying in %.0fs (attempt %d/%d)",
                            job_url, wait, attempt + 1, _MAX_RETRIES,
                        )
                        if throttle is not None:
                            throttle.backoff(wait)
                        else:
                            await asyncio.sleep(wait)
                        continue
                    else:
                        logger.warning("LinkedIn 429 for %s — giving up after %d retries", job_url, _MAX_RETRIES)
                        return
                if resp.status != 200:
                    logger.warning("LinkedIn detail page returned %d for %s", resp.status, job_url)
                    return
                html = await resp.text()
                break
        except Exception:
            logger.debug("LinkedIn detail fetch failed for %s", job_url, exc_info=True)
            return

    if not html:
        return

    soup = BeautifulSoup(html, "lxml")

    # --- Primary: JSON-LD structured data ---
    ld = _parse_json_ld(soup)
    if ld:
        desc = ld.get("description", "")
        if desc:
            job["description"] = _strip_html(desc)

        s_min, s_max, s_period = _extract_salary_from_ld(ld)
        if s_min:
            job["salary_min"] = s_min
        if s_max:
            job["salary_max"] = s_max
        if s_period != "unknown" and (s_min or s_max):
            job["salary_period"] = s_period

        # Backfill location from JSON-LD when the search card had none
        if not job.get("location"):
            jl = ld.get("jobLocation")
            entries = jl if isinstance(jl, list) else ([jl] if isinstance(jl, dict) else [])
            for entry in entries:
                addr = entry.get("address", {}) if isinstance(entry, dict) else {}
                city = addr.get("addressLocality", "")
                region = addr.get("addressRegion", "")
                loc_parts = [p for p in [city, region] if p]
                if loc_parts:
                    job["location"] = ", ".join(loc_parts)
                    break

        # Employment type as tag
        emp_type = ld.get("employmentType")
        if emp_type:
            if isinstance(emp_type, list):
                job["tags"] = [str(t) for t in emp_type]
            else:
                job["tags"] = [str(emp_type)]

    # --- Fallback: HTML selectors (if JSON-LD didn't give us a description) ---
    if not job.get("description"):
        desc_el = (
            soup.find("div", class_=re.compile(r"show-more-less-html__markup"))
            or soup.find("div", class_=re.compile(r"description__text"))
            or soup.find("section", class_=re.compile(r"description"))
        )
        if desc_el:
            job["description"] = _strip_html(str(desc_el))

    # --- Fallback: HTML salary ---
    if not job.get("salary_min"):
        salary_el = (
            soup.find("div", class_=re.compile(r"salary-main-rail__data-body"))
            or soup.find("div", class_=re.compile(r"compensation__salary"))
        )
        if not salary_el:
            for li in soup.find_all("li", class_=re.compile(r"description__job-criteria-item")):
                header = li.find("h3")
                if header and "salary" in header.get_text(strip=True).lower():
                    salary_el = li
                    break
        if salary_el:
            s_min, s_max, s_period = _parse_salary(salary_el.get_text(strip=True))
            if s_min:
                job["salary_min"] = s_min
            if s_max:
                job["salary_max"] = s_max
            if s_period != "unknown" and (s_min or s_max):
                job["salary_period"] = s_period

    # --- Fallback: HTML tags from job criteria ---
    if not job.get("tags"):
        criteria_items = soup.find_all("li", class_=re.compile(r"description__job-criteria-item"))
        tags: list[str] = []
        for item in criteria_items:
            val_el = item.find("span", class_=re.compile(r"description__job-criteria-text"))
            if val_el:
                val = val_el.get_text(strip=True)
                if val and val.lower() not in ("other",):
                    tags.append(val)
        if tags:
            job["tags"] = tags

    # --- Easy Apply detection (multi-signal, keep existing value if already True) ---
    is_easy_apply = job.get("is_easy_apply", False)

    # 1. JSON-LD: directApply field (most reliable)
    if not is_easy_apply and ld:
        if ld.get("directApply") is True:
            is_easy_apply = True
        # Some listings use applyMethod or potentialAction
        apply_method = ld.get("applyMethod") or ld.get("potentialAction")
        if isinstance(apply_method, dict):
            if apply_method.get("@type") == "ApplyAction":
                is_easy_apply = True
        elif isinstance(apply_method, list):
            for am in apply_method:
                if isinstance(am, dict) and am.get("@type") == "ApplyAction":
                    is_easy_apply = True
                    break

    # 2. HTML: apply button with Easy Apply specific classes or text
    if not is_easy_apply:
        easy_btn = soup.find(
            attrs={"class": re.compile(
                r"jobs-apply-button--top-card"
                r"|easy-apply"
                r"|easyApply"
                r"|jobs-s-apply"
                r"|jobs-apply-button",
                re.IGNORECASE,
            )}
        )
        if easy_btn:
            btn_text = easy_btn.get_text(separator=" ", strip=True).lower()
            if "easy apply" in btn_text:
                is_easy_apply = True
            # LinkedIn's apply button without "easy apply" text means external
            # so only mark if text explicitly says easy apply

    # 3. HTML: any element with "Easy Apply" text (badges, footers, labels)
    if not is_easy_apply:
        # Look specifically for span/div elements that say "Easy Apply"
        # rather than searching entire page text (avoids false positives
        # from job descriptions mentioning "easy apply")
        for tag in ("span", "div", "li", "button", "a"):
            for el in soup.find_all(tag):
                el_text = el.get_text(strip=True).lower()
                # Must be a short element (badge/label), not a paragraph
                if el_text == "easy apply" or el_text == "be an early applicant · easy apply":
                    is_easy_apply = True
                    break
                if len(el_text) < 40 and "easy apply" in el_text:
                    is_easy_apply = True
                    break
            if is_easy_apply:
                break

    # 4. HTML: data attributes LinkedIn uses for Easy Apply
    if not is_easy_apply:
        ea_data = soup.find(attrs={"data-is-easy-apply": "true"})
        if ea_data:
            is_easy_apply = True
        # Also check for the apply modal trigger attribute
        ea_data2 = soup.find(attrs={"data-job-apply-type": "EASY_APPLY"})
        if ea_data2:
            is_easy_apply = True

    job["is_easy_apply"] = is_easy_apply


def detect_linkedin_easy_apply(job: dict) -> str:
    """Classify a stored LinkedIn job dict as 'easy_apply', 'external', or 'unknown'.

    Works only from data already present in the dict — no network calls are made.

    Signals checked, in order:
      1. ``is_easy_apply`` flag — set by ``_fetch_job_detail`` from JSON-LD,
         HTML buttons, and data attributes.  This is the most reliable signal.
      2. URL path contains ``/apply`` as a standalone segment on linkedin.com —
         some stored apply-action URLs use this path.
      3. URL hostname is not a linkedin.com domain — indicates an external
         redirect to an ATS or third-party site (Applicant Assist flow).

    Returns:
      ``'easy_apply'``  — LinkedIn Easy Apply is available (handled fully in-app).
      ``'external'``    — job URL points to a non-LinkedIn domain.
      ``'unknown'``     — not enough stored information to classify.
    """
    from urllib.parse import urlparse

    # Signal 1: metadata flag already resolved by the detail-page fetch
    if job.get("is_easy_apply"):
        return "easy_apply"

    url = (job.get("url") or "").strip()
    if not url:
        return "unknown"

    parsed = urlparse(url)
    hostname = parsed.netloc.lower()
    # Normalise: strip leading "www." so linkedin.com and www.linkedin.com match
    if hostname.startswith("www."):
        hostname = hostname[4:]

    # Any non-empty hostname that is neither linkedin.com nor a subdomain of it
    # (e.g. media.licdn.com) is an external redirect.
    is_linkedin = hostname == "linkedin.com" or hostname.endswith(".linkedin.com")
    if hostname and not is_linkedin:
        return "external"

    # Signal 2: '/apply' as a standalone path segment on a linkedin.com URL
    path_parts = [p for p in parsed.path.split("/") if p]
    if "apply" in path_parts:
        return "easy_apply"

    return "unknown"


async def fetch_jobs(config: dict, known_ids: set[str] | None = None) -> list[dict]:
    """Fetch jobs from LinkedIn's public job search with strict filtering.

    known_ids — external_ids ("li-...") already in the DB; their detail pages
    are not re-fetched (the stored record already has the description).
    """
    from . import compile_exclude_patterns, matches_exclude

    search_cfg = config.get("search", {})
    keywords = search_cfg.get("keywords", [])
    locations = search_cfg.get("locations", [""])
    exclude_patterns = compile_exclude_patterns(search_cfg.get("exclude_keywords", []))
    max_age_days = search_cfg.get("max_age_days", 7)
    known = known_ids or set()

    if not keywords:
        return []

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # Map max_age_days to LinkedIn's f_TPR parameter
    if max_age_days <= 1:
        time_filter = "r86400"     # Past 24 hours
    elif max_age_days <= 7:
        time_filter = "r604800"    # Past week
    elif max_age_days <= 30:
        time_filter = "r2592000"   # Past month
    else:
        time_filter = ""

    # Check if any configured location is "remote"
    has_remote = any(loc.strip().lower() == "remote" for loc in locations)

    results: list[dict] = []
    seen_ids: set[str] = set()
    _search_request_count = 0
    # Track (query, geo_id) pairs already searched to avoid redundant fetches
    # when multiple configured locations map to the same LinkedIn geoId
    # (e.g. "Colorado" and "CO" both resolve to 105763813).
    _seen_query_geo: set[tuple[str, str]] = set()

    # Batched boolean-OR queries instead of one search per keyword.
    queries = _batch_keywords(keywords)
    logger.info("LinkedIn: %d keywords batched into %d search queries", len(keywords), len(queries))

    loop = asyncio.get_running_loop()
    hard_deadline = loop.time() + _TOTAL_BUDGET
    search_deadline = loop.time() + _SEARCH_PHASE_BUDGET

    async with aiohttp.ClientSession() as session:
        # ----- Phase 1: Collect job cards from search results -----
        for query in queries:
            if loop.time() >= search_deadline:
                logger.warning(
                    "LinkedIn: search-phase budget (%.0fs) exhausted — proceeding to "
                    "detail fetch with %d jobs collected so far",
                    _SEARCH_PHASE_BUDGET, len(results),
                )
                break
            for location in locations:
                if loop.time() >= search_deadline:
                    break
                loc_normalized = _normalize_location(location)
                geo_id = await _resolve_geo_id(session, location, headers)

                # Skip if we've already searched this query+geoId combo
                # (multiple location strings can map to the same geoId, e.g. "Colorado"/"CO")
                combo_key = (query, geo_id or loc_normalized)
                if combo_key in _seen_query_geo:
                    logger.debug("LinkedIn: skipping duplicate search for query=%s geo_id=%s", query, geo_id)
                    continue
                _seen_query_geo.add(combo_key)

                # When we have a geoId, that's the authoritative location signal.
                # Sending the text `&location=` alongside it lets LinkedIn fall back
                # to fuzzy matching the string — which has been observed to pull in
                # Polish/EU jobs even when geoId says US. So: geoId-only when we have one.
                search_url = (
                    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                    f"?keywords={quote_plus(query)}"
                )
                if geo_id:
                    search_url += f"&geoId={geo_id}"
                else:
                    search_url += f"&location={quote_plus(location)}"

                if loc_normalized == "remote":
                    search_url += f"&f_WT={REMOTE_FILTER}"

                if time_filter:
                    search_url += f"&f_TPR={time_filter}"

                if loc_normalized != "remote" and geo_id and geo_id not in _STATE_OR_COUNTRY_GEO_IDS:
                    search_url += "&distance=25"

                # 4 pages per query (was 3 per keyword) — OR-batched queries
                # aggregate several keywords, so go one page deeper.
                for start in range(0, 100, 25):
                    if loop.time() >= search_deadline:
                        break

                    page_url = search_url + f"&start={start}"

                    # Throttle search requests to avoid 429s
                    if _search_request_count > 0:
                        await asyncio.sleep(_SEARCH_DELAY)
                    _search_request_count += 1

                    html = None
                    for attempt in range(_MAX_RETRIES + 1):
                        try:
                            async with session.get(
                                page_url,
                                headers=headers,
                                timeout=aiohttp.ClientTimeout(total=30),
                                allow_redirects=True,
                            ) as resp:
                                if resp.status == 429:
                                    retry_after = float(resp.headers.get("Retry-After", 0))
                                    wait = max(retry_after, _RETRY_BASE * (2 ** attempt))
                                    if attempt < _MAX_RETRIES:
                                        logger.warning(
                                            "LinkedIn 429 on search — retrying in %.0fs (attempt %d/%d)",
                                            wait, attempt + 1, _MAX_RETRIES,
                                        )
                                        await asyncio.sleep(wait)
                                        continue
                                    else:
                                        logger.warning("LinkedIn 429 on search — giving up after %d retries", _MAX_RETRIES)
                                        break
                                if resp.status != 200:
                                    logger.warning(
                                        "LinkedIn returned %d for query=%s location=%s start=%d",
                                        resp.status, query, location, start,
                                    )
                                    break
                                html = await resp.text()
                                break
                        except Exception:
                            logger.exception("LinkedIn request failed for query=%s start=%d", query, start)
                            break

                    if not html:
                        break

                    soup = BeautifulSoup(html, "lxml")
                    job_cards = soup.find_all("li")

                    if not job_cards:
                        logger.info(
                            "LinkedIn: no <li> elements for query=%s location=%s start=%d — "
                            "possible auth redirect or empty results page",
                            query, location, start,
                        )
                        break

                    page_count = 0
                    for card in job_cards:
                        try:
                            parsed = _parse_search_card(card)
                            if parsed is None:
                                continue

                            title = parsed["title"]
                            company = parsed["company"]
                            job_location = parsed["location"]
                            job_url = parsed["url"]
                            date_posted = parsed["date_posted"]
                            ext_id = parsed["external_id"]

                            if ext_id in seen_ids:
                                continue
                            seen_ids.add(ext_id)

                            title_lower = title.lower()
                            if matches_exclude(title, exclude_patterns) or \
                                    matches_exclude(company, exclude_patterns):
                                continue

                            is_remote = "remote" in job_location.lower() or "remote" in title_lower
                            location_ok = is_remote and has_remote
                            if not location_ok:
                                location_ok = _is_location_match(job_location, locations)
                            if not location_ok:
                                logger.info(
                                    "LinkedIn: filtered out '%s' @ %s — location '%s' doesn't match %s",
                                    title, company, job_location, locations,
                                )
                                continue

                            # --- Easy Apply detection from search card ---
                            card_easy_apply = False
                            # LinkedIn search cards often have a footer or
                            # span with "Easy Apply" text
                            card_text = card.get_text(separator=" ").lower()
                            if "easy apply" in card_text:
                                card_easy_apply = True
                            # Check for data attribute or class on the card
                            if not card_easy_apply:
                                ea_el = card.find(
                                    attrs={"class": re.compile(
                                        r"easy-apply|easyApply|job-posting-benefits__text",
                                        re.IGNORECASE,
                                    )}
                                )
                                if ea_el and "easy apply" in (ea_el.get_text(strip=True).lower()):
                                    card_easy_apply = True

                            page_count += 1
                            results.append({
                                "source": "linkedin",
                                "external_id": f"li-{ext_id}",
                                "title": title,
                                "company": company,
                                "location": job_location,
                                "url": job_url,
                                "description": "",
                                "salary_min": None,
                                "salary_max": None,
                                "salary_period": "unknown",
                                "tags": [],
                                "date_posted": date_posted,
                                "is_remote": is_remote,
                                "is_easy_apply": card_easy_apply,
                            })
                        except Exception:
                            continue

                    if page_count == 0:
                        break

        # ----- Phase 2: Fetch detail pages concurrently for descriptions -----
        # Uses a small semaphore to bound parallelism; existing 429 retry
        # handles any backpressure. The whole phase is wrapped in a hard
        # time cap — if we run over, we return the search-phase results
        # we already have (with empty descriptions for stragglers) instead
        # of letting the orchestrator's per-source timeout kill the task
        # and discard EVERYTHING.
        # Jobs already in the DB keep their stored description — re-fetching
        # their detail pages is the slowest, most 429-prone part of this
        # source, so skip them entirely.
        to_fetch = [j for j in results if j.get("external_id") not in known]
        if len(to_fetch) < len(results):
            logger.info(
                "LinkedIn: skipping detail fetch for %d jobs already in DB",
                len(results) - len(to_fetch),
            )
        if to_fetch:
            # Whatever remains of the overall budget, capped at the phase max —
            # search + detail together must land under the orchestrator's
            # 600s per-source timeout or everything collected is discarded.
            detail_budget = max(30.0, min(_DETAIL_PHASE_BUDGET, hard_deadline - loop.time()))
            logger.info(
                "LinkedIn: fetching detail pages for %d jobs (concurrency=%d, budget=%.0fs)",
                len(to_fetch), _DETAIL_CONCURRENCY, detail_budget,
            )
            sem = asyncio.Semaphore(_DETAIL_CONCURRENCY)
            throttle = _DetailThrottle()

            async def _bounded_detail(job: dict) -> None:
                async with sem:
                    await _fetch_job_detail(session, job, headers, throttle)

            tasks = [asyncio.create_task(_bounded_detail(j)) for j in to_fetch]
            try:
                done, pending = await asyncio.wait(
                    tasks, timeout=detail_budget, return_when=asyncio.ALL_COMPLETED,
                )
            except asyncio.CancelledError:
                for t in tasks:
                    t.cancel()
                raise

            if pending:
                logger.warning(
                    "LinkedIn: detail-fetch budget (%.0fs) exhausted with %d/%d jobs still pending — "
                    "returning search-phase results without descriptions for those",
                    detail_budget, len(pending), len(to_fetch),
                )
                for t in pending:
                    t.cancel()
                # Let cancellations settle so aiohttp connections close cleanly
                await asyncio.gather(*pending, return_exceptions=True)

            filled = sum(1 for j in results if j.get("description"))
            logger.info("LinkedIn: fetched descriptions for %d/%d jobs", filled, len(results))

    logger.info("LinkedIn: %d jobs passed all filters", len(results))
    return results
