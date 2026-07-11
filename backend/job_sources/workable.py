"""
workable.py — Fetch jobs from Workable public widget APIs.

Reads account names from config.search.workable_accounts (list of account
slugs, the <account> part of apply.workable.com/<account>).

Each account is a single request: the widget endpoint is called with
?details=true, which returns every published job's full description inline —
no per-job detail fetches are needed.

API: GET https://apply.workable.com/api/v1/widget/accounts/{account}?details=true
Response: {"name": ..., "jobs": [{title, shortcode, code, url, shortlink,
application_url, published_on, created_at, telecommuting, city, state,
country, department, employment_type, description, requirements, benefits,
locations: [{city, region, country, ...}]}], "total": N}

NOTE: response shape could not be verified live (apply.workable.com is
DNS-blocked on this network) — parsing follows the documented widget API and
is defensive against missing keys throughout.
"""

import asyncio
import json
import logging

import aiohttp

logger = logging.getLogger(__name__)

# Accounts are fetched concurrently; this bounds total parallelism against
# the Workable API (one request per account).
_ACCOUNT_CONCURRENCY = 4
# Internal budget (seconds) — must finish under the orchestrator's 120s
# per-source timeout so already-collected accounts are returned instead of
# everything being discarded by the wait_for cancellation.
_INTERNAL_BUDGET = 100.0


def _strip_html(html_text: str) -> str:
    from . import clean_description
    return clean_description(html_text)


def _matches_keywords(title: str, keywords: list[str]) -> bool:
    from . import matches_keywords
    return matches_keywords(title, keywords)


def _job_location(job: dict) -> str:
    """Build a display location from top-level city/state/country, falling
    back to the first entry of the locations array."""
    parts = [job.get("city"), job.get("state"), job.get("country")]
    loc = ", ".join(p for p in parts if p)
    if loc:
        return loc
    locations = job.get("locations")
    if isinstance(locations, list) and locations and isinstance(locations[0], dict):
        first = locations[0]
        parts = [first.get("city"), first.get("region"), first.get("country")]
        return ", ".join(p for p in parts if p)
    return ""


def detect_workable_apply_type(job: dict) -> str:
    """Classify a stored Workable job dict as 'easy_apply', 'external', or 'unknown'.

    Works only from data already present in the dict — no network calls are made.

    Workable hosts a structured native apply flow at apply.workable.com that is
    automatable in-app. A stored URL on any workable.com domain is easy_apply;
    an account whose posting links to a custom-domain career site stores a
    non-Workable URL and is classified external.

    Returns:
      ``'easy_apply'``  — apply form is on workable.com (handled fully in-app).
      ``'external'``    — URL points to a non-Workable domain.
      ``'unknown'``     — no URL is stored; cannot classify.
    """
    from urllib.parse import urlparse

    url = (job.get("url") or "").strip()
    if not url:
        return "unknown"

    hostname = urlparse(url).netloc.lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]

    if hostname == "workable.com" or hostname.endswith(".workable.com"):
        return "easy_apply"
    return "external" if hostname else "unknown"


async def _fetch_account(
    session: aiohttp.ClientSession,
    account: str,
    keywords: list[str],
    exclude_patterns: tuple,
) -> list[dict]:
    """Fetch all jobs from a single Workable account in one request."""
    from . import fetch_with_retries, matches_exclude

    results: list[dict] = []
    url = f"https://apply.workable.com/api/v1/widget/accounts/{account}?details=true"

    try:
        status, body = await fetch_with_retries(
            session, url, timeout=aiohttp.ClientTimeout(total=30),
        )
        if status != 200:
            logger.warning("Workable account %s returned %d", account, status)
            return []
        data = json.loads(body)
    except Exception:
        logger.exception("Failed to fetch Workable account %s", account)
        return []

    company = data.get("name") or account.replace("-", " ").title()

    for job in data.get("jobs", []):
        title = job.get("title", "")
        if matches_exclude(title, exclude_patterns):
            continue

        # Match keywords against the title only — same semantics as the
        # Greenhouse source. Matching descriptions too floods results with
        # every posting whose boilerplate mentions a keyword.
        if keywords and not _matches_keywords(title, keywords):
            continue

        description = _strip_html(job.get("description", ""))
        location = _job_location(job)
        is_remote = bool(job.get("telecommuting")) or bool(job.get("remote")) \
            or "remote" in location.lower()
        shortcode = job.get("shortcode") or job.get("code") or job.get("id", "")
        url = job.get("url") or job.get("shortlink") \
            or f"https://apply.workable.com/{account}/j/{shortcode}/"

        results.append({
            "source": "workable",
            "external_id": f"workable-{account}-{shortcode}",
            "title": title,
            "company": company,
            "location": location or ("Remote" if is_remote else ""),
            "url": url,
            "description": description,
            "salary_min": None,
            "salary_max": None,
            "tags": [t for t in (job.get("department", ""), job.get("function", "")) if t],
            "date_posted": job.get("published_on") or job.get("created_at", ""),
            "is_remote": is_remote,
            "is_easy_apply": False,
            "apply_type": detect_workable_apply_type({"url": url}),
        })

    return results


async def fetch_jobs(config: dict) -> list[dict]:
    """Fetch jobs from all configured Workable accounts.

    Accounts run concurrently (bounded by _ACCOUNT_CONCURRENCY) under an
    internal deadline, and results are collected per-account — a slow or hung
    account costs only itself, not every account after it.
    """
    from . import compile_exclude_patterns

    search = config.get("search", {})
    keywords = search.get("keywords", [])
    exclude_patterns = compile_exclude_patterns(search.get("exclude_keywords", []))
    accounts = [a for a in search.get("workable_accounts") or [] if a and a != "example-company"]

    if not accounts:
        logger.info("No Workable accounts configured — skipping Workable source")
        return []

    results: list[dict] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _INTERNAL_BUDGET
    sem = asyncio.Semaphore(_ACCOUNT_CONCURRENCY)

    async with aiohttp.ClientSession(
        headers={"User-Agent": "Jobsmith/1.0"}
    ) as session:
        async def _run_account(account: str) -> None:
            async with sem:
                if loop.time() >= deadline:
                    logger.warning("Workable: budget exhausted before account %s — skipping", account)
                    return
                jobs = await _fetch_account(session, account, keywords, exclude_patterns)
                results.extend(jobs)

        outcomes = await asyncio.gather(
            *(_run_account(a) for a in accounts), return_exceptions=True
        )
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.warning("Workable account task failed: %s", outcome)

    logger.info("Workable: %d jobs matched filters", len(results))
    return results
