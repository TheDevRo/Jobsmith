"""
greenhouse.py — Fetch jobs from Greenhouse and Lever public job board APIs.

Reads board tokens from config.search.greenhouse_boards (list of company slugs/tokens).
Falls back to the legacy config.search.greenhouse_companies key for backward compat.
Lever boards are read from config.search.lever_companies.

Each board is a single request: the Greenhouse list endpoint is called with
?content=true, which returns every job's full description and location inline,
so no per-job detail fetches are needed.

# GUI CHANGE NEEDED: expose greenhouse_boards as an editable list in the Settings tab
# so users can add/remove Greenhouse board tokens without editing config.yaml directly.
"""

import asyncio
import json
import logging

import aiohttp

logger = logging.getLogger(__name__)

# Boards are fetched concurrently; this bounds total parallelism against the
# Greenhouse/Lever APIs (one request per board).
_BOARD_CONCURRENCY = 4
# Internal budget (seconds) — must finish under the orchestrator's 300s
# per-source timeout so already-collected boards are returned instead of
# everything being discarded by the wait_for cancellation.
_INTERNAL_BUDGET = 240.0


def _strip_html(html_text: str) -> str:
    from . import clean_description
    return clean_description(html_text)


def _matches_keywords(title: str, description: str, keywords: list[str]) -> bool:
    from . import matches_keywords
    return matches_keywords(f"{title} {description}", keywords)


async def _fetch_greenhouse_company(
    session: aiohttp.ClientSession,
    slug: str,
    keywords: list[str],
    exclude_patterns: tuple,
    known_ids: set[str],
) -> list[dict]:
    """Fetch all jobs from a single Greenhouse board in one request.

    ?content=true returns every job with its full description, location, and
    departments inline — no per-job detail requests, no rate-limit sleeps.
    """
    from . import fetch_with_retries, matches_exclude

    results: list[dict] = []
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"

    try:
        # content=true payloads for large boards run to several MB — allow
        # more than the usual 30s.
        status, body = await fetch_with_retries(
            session, url, timeout=aiohttp.ClientTimeout(total=45),
        )
        if status != 200:
            logger.warning("Greenhouse board %s returned %d", slug, status)
            return []
        data = json.loads(body)
    except Exception:
        logger.exception("Failed to list Greenhouse board %s", slug)
        return []

    skipped_known = 0
    for job in data.get("jobs", []):
        job_id = job.get("id")
        title = job.get("title", "")

        if matches_exclude(title, exclude_patterns):
            continue

        # Already in the DB — the stored record has the description and
        # upsert would be a no-op anyway.
        if f"gh-{slug}-{job_id}" in known_ids:
            skipped_known += 1
            continue

        # Match keywords against the title only — same effective semantics as
        # the old title pre-filter. Matching descriptions too floods results:
        # every posting on a security company's board mentions "SOC"/"analyst"
        # somewhere in its boilerplate.
        if keywords and not _matches_keywords(title, "", keywords):
            continue

        description = _strip_html(job.get("content", ""))
        location_name = (job.get("location") or {}).get("name", "")

        results.append({
            "source": "greenhouse",
            "external_id": f"gh-{slug}-{job_id}",
            "title": title,
            "company": job.get("company_name") or slug.replace("-", " ").title(),
            "location": location_name,
            "url": job.get("absolute_url", f"https://boards.greenhouse.io/{slug}/jobs/{job_id}"),
            "description": description,
            "salary_min": None,
            "salary_max": None,
            "tags": [dept.get("name", "") for dept in job.get("departments") or []],
            "date_posted": job.get("updated_at", ""),
            "is_remote": "remote" in location_name.lower(),
        })

    if not results and not skipped_known:
        logger.warning("Greenhouse board %s: 0 jobs matched filters (board may be empty or keywords too narrow)", slug)
    elif not results:
        logger.info("Greenhouse board %s: no new jobs (%d already in DB)", slug, skipped_known)

    return results


async def _fetch_lever_company(
    session: aiohttp.ClientSession,
    slug: str,
    keywords: list[str],
    exclude_patterns: tuple,
    known_ids: set[str],
) -> list[dict]:
    """Fetch all jobs from a single Lever posting board (one HTTP request)."""
    from . import fetch_with_retries, matches_exclude

    results: list[dict] = []
    url = f"https://api.lever.co/v0/postings/{slug}"

    try:
        status, body = await fetch_with_retries(
            session, url, timeout=aiohttp.ClientTimeout(total=30),
        )
        if status != 200:
            logger.warning("Lever board %s returned %d", slug, status)
            return []
        data = json.loads(body)
    except Exception:
        logger.exception("Failed to fetch Lever board %s", slug)
        return []

    for posting in data:
        title = posting.get("text", "")
        if matches_exclude(title, exclude_patterns):
            continue

        external_id = f"lv-{slug}-{posting.get('id', '')}"
        if external_id in known_ids:
            continue

        description = _strip_html(posting.get("descriptionPlain", posting.get("description", "")))
        categories = posting.get("categories", {})
        location_name = categories.get("location", "")

        if not _matches_keywords(title, description, keywords):
            continue

        results.append({
            "source": "lever",
            "external_id": external_id,
            "title": title,
            "company": slug.replace("-", " ").title(),
            "location": location_name,
            "url": posting.get("hostedUrl", f"https://jobs.lever.co/{slug}/{posting.get('id', '')}"),
            "description": description,
            "salary_min": None,
            "salary_max": None,
            "tags": [categories.get("team", ""), categories.get("department", "")],
            "date_posted": "",
            "is_remote": "remote" in location_name.lower(),
        })

    return results


def detect_greenhouse_apply_type(job: dict) -> str:
    """Classify a stored Greenhouse job dict as 'easy_apply', 'external', or 'unknown'.

    Works only from data already present in the dict — no network calls are made.

    Greenhouse embed boards (boards.greenhouse.io) host a fully structured apply
    form that is automatable in-app.  If the stored URL resolves to any greenhouse.io
    domain (the boards API, the embed widget host, etc.) the job is classified as
    easy_apply.  A non-greenhouse.io URL indicates the absolute_url field pointed
    to an external ATS or agency site.

    Returns:
      ``'easy_apply'``  — apply form is on greenhouse.io (handled fully in-app).
      ``'external'``    — URL points to a non-Greenhouse domain.
      ``'unknown'``     — no URL is stored; cannot classify.
    """
    from urllib.parse import urlparse

    url = (job.get("url") or "").strip()
    if not url:
        return "unknown"

    parsed = urlparse(url)
    hostname = parsed.netloc.lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]

    is_greenhouse = hostname == "greenhouse.io" or hostname.endswith(".greenhouse.io")
    if is_greenhouse:
        return "easy_apply"

    if hostname:
        return "external"

    return "unknown"


def detect_lever_apply_type(job: dict) -> str:
    """Classify a stored Lever job dict as 'easy_apply', 'external', or 'unknown'.

    Works only from data already present in the dict — no network calls are made.

    Lever's canonical job board is jobs.lever.co and the apply flow is fully
    structured and automatable in-app.  If the hostedUrl stored in the job points
    to a lever.co subdomain it is classified as easy_apply.  Custom-domain Lever
    boards (e.g. jobs.company.com backed by Lever) cannot be identified from the
    stored URL alone and are classified as external — the Applicant Assist flow
    will handle them.

    Returns:
      ``'easy_apply'``  — apply form is on lever.co (handled fully in-app).
      ``'external'``    — URL points to a non-Lever domain.
      ``'unknown'``     — no URL is stored; cannot classify.
    """
    from urllib.parse import urlparse

    url = (job.get("url") or "").strip()
    if not url:
        return "unknown"

    parsed = urlparse(url)
    hostname = parsed.netloc.lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]

    is_lever = hostname == "lever.co" or hostname.endswith(".lever.co")
    if is_lever:
        return "easy_apply"

    if hostname:
        return "external"

    return "unknown"


async def fetch_jobs(config: dict, known_ids: set[str] | None = None) -> list[dict]:
    """Fetch jobs from all configured Greenhouse and Lever company boards.

    Boards run concurrently (bounded by _BOARD_CONCURRENCY) under an internal
    deadline, and results are collected per-board — a slow or hung board costs
    only itself, not every board after it.
    """
    from . import compile_exclude_patterns

    search = config.get("search", {})
    keywords = search.get("keywords", [])
    exclude_patterns = compile_exclude_patterns(search.get("exclude_keywords", []))
    # greenhouse_boards is the canonical key; fall back to legacy greenhouse_companies
    gh_boards = [s for s in (search.get("greenhouse_boards") or search.get("greenhouse_companies", []))
                 if s != "example-company"]
    lv_companies = [s for s in search.get("lever_companies", []) if s != "example-company"]
    known = known_ids or set()

    results: list[dict] = []

    if not gh_boards:
        logger.info("No Greenhouse board tokens configured — skipping Greenhouse source")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _INTERNAL_BUDGET
    sem = asyncio.Semaphore(_BOARD_CONCURRENCY)

    async with aiohttp.ClientSession(
        headers={"User-Agent": "Jobsmith/1.0"}
    ) as session:
        async def _run_board(fetcher, slug: str) -> None:
            async with sem:
                if loop.time() >= deadline:
                    logger.warning("Greenhouse/Lever: budget exhausted before board %s — skipping", slug)
                    return
                jobs = await fetcher(session, slug, keywords, exclude_patterns, known)
                results.extend(jobs)

        board_tasks = [_run_board(_fetch_greenhouse_company, slug) for slug in gh_boards]
        board_tasks += [_run_board(_fetch_lever_company, slug) for slug in lv_companies]
        if board_tasks:
            outcomes = await asyncio.gather(*board_tasks, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, Exception):
                    logger.warning("Greenhouse/Lever board task failed: %s", outcome)

    logger.info("Greenhouse/Lever: %d jobs matched filters", len(results))
    return results
