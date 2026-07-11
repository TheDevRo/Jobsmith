"""
recruitee.py — Fetch jobs from Recruitee public career-site APIs.

Reads company subdomains from config.search.recruitee_companies (list of
slugs, the <company> part of <company>.recruitee.com).

Each company is a single request: the offers endpoint returns every published
offer with its full description inline — no per-offer detail fetches needed.

API: GET https://{company}.recruitee.com/api/offers/
Response: {"offers": [{id, title, slug, status, careers_url, careers_apply_url,
created_at, published_at ("YYYY-MM-DD HH:MM:SS UTC"), location, city, country,
company_name, department, tags, remote, hybrid, on_site, description,
requirements, salary: {min, max, period, currency}}]}
"""

import asyncio
import json
import logging

import aiohttp

logger = logging.getLogger(__name__)

# Companies are fetched concurrently; this bounds total parallelism against
# the Recruitee API (one request per company).
_COMPANY_CONCURRENCY = 4
# Internal budget (seconds) — must finish under the orchestrator's 120s
# per-source timeout so already-collected companies are returned instead of
# everything being discarded by the wait_for cancellation.
_INTERNAL_BUDGET = 100.0


def _strip_html(html_text: str) -> str:
    from . import clean_description
    return clean_description(html_text)


def _matches_keywords(title: str, keywords: list[str]) -> bool:
    from . import matches_keywords
    return matches_keywords(title, keywords)


def _to_iso(timestamp) -> str:
    """Normalize Recruitee's "2026-06-22 13:37:27 UTC" timestamps to ISO 8601
    so parse_posted_date (and the max_age_days filter) can read them.
    Already-ISO or empty values pass through unchanged."""
    if not timestamp:
        return ""
    s = str(timestamp).strip()
    if s.endswith(" UTC"):
        s = s[:-4].strip().replace(" ", "T") + "+00:00"
    return s


def _parse_salary(salary: dict | None) -> tuple[int | None, int | None, str]:
    """Extract (salary_min, salary_max, salary_period) from Recruitee's salary
    object ({min, max, period, currency} — min/max are strings).

    Only yearly and hourly periods are emitted; monthly figures are dropped
    rather than mislabeled, since the pipeline's salary normalization only
    understands hourly/annual.
    """
    if not isinstance(salary, dict):
        return None, None, "unknown"
    period_raw = str(salary.get("period") or "").lower()
    if period_raw.startswith("hour"):
        period = "hourly"
    elif period_raw.startswith(("year", "annual")):
        period = "annual"
    else:
        return None, None, "unknown"
    try:
        salary_min = int(float(salary["min"])) if salary.get("min") else None
        salary_max = int(float(salary["max"])) if salary.get("max") else None
    except (TypeError, ValueError):
        return None, None, "unknown"
    if salary_min is None and salary_max is None:
        return None, None, "unknown"
    return salary_min, salary_max, period


def detect_recruitee_apply_type(job: dict) -> str:
    """Classify a stored Recruitee job dict as 'easy_apply', 'external', or 'unknown'.

    Works only from data already present in the dict — no network calls are made.

    Recruitee hosts a structured native apply flow on its recruitee.com career
    sites ({company}.recruitee.com) that is automatable in-app. A stored URL on
    any recruitee.com domain is easy_apply; a company using a custom career
    domain stores a non-Recruitee URL and is classified external.

    Returns:
      ``'easy_apply'``  — apply form is on recruitee.com (handled fully in-app).
      ``'external'``    — URL points to a non-Recruitee domain.
      ``'unknown'``     — no URL is stored; cannot classify.
    """
    from urllib.parse import urlparse

    url = (job.get("url") or "").strip()
    if not url:
        return "unknown"

    hostname = urlparse(url).netloc.lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]

    if hostname == "recruitee.com" or hostname.endswith(".recruitee.com"):
        return "easy_apply"
    return "external" if hostname else "unknown"


async def _fetch_company(
    session: aiohttp.ClientSession,
    company: str,
    keywords: list[str],
    exclude_patterns: tuple,
) -> list[dict]:
    """Fetch all offers from a single Recruitee company in one request."""
    from . import fetch_with_retries, matches_exclude

    results: list[dict] = []
    url = f"https://{company}.recruitee.com/api/offers/"

    try:
        status, body = await fetch_with_retries(
            session, url, timeout=aiohttp.ClientTimeout(total=30),
        )
        if status != 200:
            logger.warning("Recruitee company %s returned %d", company, status)
            return []
        data = json.loads(body)
    except Exception:
        logger.exception("Failed to fetch Recruitee company %s", company)
        return []

    for offer in data.get("offers", []):
        # The public endpoint should only return published offers, but be
        # defensive — drafts/closed offers are not applyable.
        if offer.get("status") not in (None, "", "published"):
            continue

        title = offer.get("title", "")
        if matches_exclude(title, exclude_patterns):
            continue

        # Match keywords against the title only — same semantics as the
        # Greenhouse source. Matching descriptions too floods results with
        # every posting whose boilerplate mentions a keyword.
        if keywords and not _matches_keywords(title, keywords):
            continue

        description = _strip_html(offer.get("description", ""))
        location = offer.get("location") or ", ".join(
            p for p in (offer.get("city"), offer.get("country")) if p
        )
        is_remote = bool(offer.get("remote")) or "remote" in location.lower()
        salary_min, salary_max, salary_period = _parse_salary(offer.get("salary"))
        offer_id = offer.get("id", "")
        slug = offer.get("slug", "")

        tags = [t for t in (offer.get("tags") or []) if t]
        department = offer.get("department")
        if department:
            tags.insert(0, department)

        url = offer.get("careers_url") or f"https://{company}.recruitee.com/o/{slug}"

        results.append({
            "source": "recruitee",
            "external_id": f"recruitee-{company}-{offer_id}",
            "title": title,
            "company": offer.get("company_name") or company.replace("-", " ").title(),
            "location": location or ("Remote" if is_remote else ""),
            "url": url,
            "description": description,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_period": salary_period,
            "tags": tags,
            "date_posted": _to_iso(offer.get("published_at") or offer.get("created_at")),
            "is_remote": is_remote,
            "is_easy_apply": False,
            "apply_type": detect_recruitee_apply_type({"url": url}),
        })

    return results


async def fetch_jobs(config: dict) -> list[dict]:
    """Fetch jobs from all configured Recruitee companies.

    Companies run concurrently (bounded by _COMPANY_CONCURRENCY) under an
    internal deadline, and results are collected per-company — a slow or hung
    company costs only itself, not every company after it.
    """
    from . import compile_exclude_patterns

    search = config.get("search", {})
    keywords = search.get("keywords", [])
    exclude_patterns = compile_exclude_patterns(search.get("exclude_keywords", []))
    companies = [c for c in search.get("recruitee_companies") or [] if c and c != "example-company"]

    if not companies:
        logger.info("No Recruitee companies configured — skipping Recruitee source")
        return []

    results: list[dict] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _INTERNAL_BUDGET
    sem = asyncio.Semaphore(_COMPANY_CONCURRENCY)

    async with aiohttp.ClientSession(
        headers={"User-Agent": "Jobsmith/1.0"}
    ) as session:
        async def _run_company(company: str) -> None:
            async with sem:
                if loop.time() >= deadline:
                    logger.warning("Recruitee: budget exhausted before company %s — skipping", company)
                    return
                jobs = await _fetch_company(session, company, keywords, exclude_patterns)
                results.extend(jobs)

        outcomes = await asyncio.gather(
            *(_run_company(c) for c in companies), return_exceptions=True
        )
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.warning("Recruitee company task failed: %s", outcome)

    logger.info("Recruitee: %d jobs matched filters", len(results))
    return results
