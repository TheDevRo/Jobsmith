"""
ashby.py — Fetch jobs from Ashby public job-board APIs.

Reads board names from config.search.ashby_boards (list of board slugs, the
<board> part of jobs.ashbyhq.com/<board>).

Each board is a single request: the posting API is called with
?includeCompensation=true, which returns every listed job's full description,
location, and compensation inline — no per-job detail fetches are needed.

API: GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true
Response: {"jobs": [{id, title, department, team, location, isRemote, isListed,
publishedAt, jobUrl, applyUrl, descriptionHtml, descriptionPlain,
compensation: {summaryComponents: [{compensationType, interval, minValue, maxValue}]}}]}
"""

import asyncio
import json
import logging

import aiohttp

logger = logging.getLogger(__name__)

# Boards are fetched concurrently; this bounds total parallelism against the
# Ashby API (one request per board).
_BOARD_CONCURRENCY = 4
# Internal budget (seconds) — must finish under the orchestrator's 120s
# per-source timeout so already-collected boards are returned instead of
# everything being discarded by the wait_for cancellation.
_INTERNAL_BUDGET = 100.0


def _strip_html(html_text: str) -> str:
    from . import clean_description
    return clean_description(html_text)


def _matches_keywords(title: str, keywords: list[str]) -> bool:
    from . import matches_keywords
    return matches_keywords(title, keywords)


def _parse_compensation(compensation: dict | None) -> tuple[int | None, int | None, str]:
    """Extract (salary_min, salary_max, salary_period) from Ashby's
    compensation object.

    Ashby reports compensation as a list of typed components; the base salary
    is the one with compensationType == "Salary". Interval "1 YEAR" maps to
    annual, "1 HOUR" to hourly. Equity/bonus components are ignored.
    Missing or malformed compensation returns (None, None, "unknown").
    """
    if not isinstance(compensation, dict):
        return None, None, "unknown"

    components: list[dict] = []
    summary = compensation.get("summaryComponents")
    if isinstance(summary, list):
        components.extend(c for c in summary if isinstance(c, dict))
    for tier in compensation.get("compensationTiers") or []:
        if isinstance(tier, dict):
            components.extend(c for c in tier.get("components") or [] if isinstance(c, dict))

    for comp in components:
        if comp.get("compensationType") != "Salary":
            continue
        min_val, max_val = comp.get("minValue"), comp.get("maxValue")
        if min_val is None and max_val is None:
            continue
        interval = str(comp.get("interval") or "").upper()
        if "HOUR" in interval:
            period = "hourly"
        elif "YEAR" in interval:
            period = "annual"
        else:
            period = "unknown"
        try:
            salary_min = int(min_val) if min_val is not None else None
            salary_max = int(max_val) if max_val is not None else None
        except (TypeError, ValueError):
            continue
        return salary_min, salary_max, period

    return None, None, "unknown"


def detect_ashby_apply_type(job: dict) -> str:
    """Classify a stored Ashby job dict as 'easy_apply', 'external', or 'unknown'.

    Works only from data already present in the dict — no network calls are made.

    Ashby hosts a fully structured native apply flow at jobs.ashbyhq.com that is
    automatable in-app. A stored URL on any ashbyhq.com domain is easy_apply; a
    board that redirects applications to an external ATS/agency site stores a
    non-Ashby URL and is classified external.

    Returns:
      ``'easy_apply'``  — apply form is on ashbyhq.com (handled fully in-app).
      ``'external'``    — URL points to a non-Ashby domain.
      ``'unknown'``     — no URL is stored; cannot classify.
    """
    from urllib.parse import urlparse

    url = (job.get("url") or "").strip()
    if not url:
        return "unknown"

    hostname = urlparse(url).netloc.lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]

    if hostname == "ashbyhq.com" or hostname.endswith(".ashbyhq.com"):
        return "easy_apply"
    return "external" if hostname else "unknown"


async def _fetch_board(
    session: aiohttp.ClientSession,
    board: str,
    keywords: list[str],
    exclude_patterns: tuple,
) -> list[dict]:
    """Fetch all jobs from a single Ashby board in one request."""
    from . import fetch_with_retries, matches_exclude

    results: list[dict] = []
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"

    try:
        status, body = await fetch_with_retries(
            session, url, timeout=aiohttp.ClientTimeout(total=30),
        )
        if status != 200:
            logger.warning("Ashby board %s returned %d", board, status)
            return []
        data = json.loads(body)
    except Exception:
        logger.exception("Failed to fetch Ashby board %s", board)
        return []

    for job in data.get("jobs", []):
        # Unlisted postings are internal-only — the public board hides them.
        if job.get("isListed") is False:
            continue

        title = job.get("title", "")
        if matches_exclude(title, exclude_patterns):
            continue

        # Match keywords against the title only — same semantics as the
        # Greenhouse source. Matching descriptions too floods results with
        # every posting whose boilerplate mentions a keyword.
        if keywords and not _matches_keywords(title, keywords):
            continue

        description = job.get("descriptionPlain") or _strip_html(job.get("descriptionHtml", ""))
        location = job.get("location") or ""
        is_remote = bool(job.get("isRemote")) or "remote" in location.lower()
        salary_min, salary_max, salary_period = _parse_compensation(job.get("compensation"))
        job_id = job.get("id", "")
        url = job.get("jobUrl") or f"https://jobs.ashbyhq.com/{board}/{job_id}"

        results.append({
            "source": "ashby",
            "external_id": f"ashby-{board}-{job_id}",
            "title": title,
            "company": board.replace("-", " ").title(),
            "location": location or ("Remote" if is_remote else ""),
            "url": url,
            "description": description,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_period": salary_period,
            "tags": [t for t in (job.get("department", ""), job.get("team", "")) if t],
            "date_posted": job.get("publishedAt", ""),
            "is_remote": is_remote,
            "is_easy_apply": False,
            "apply_type": detect_ashby_apply_type({"url": url}),
        })

    return results


async def fetch_jobs(config: dict) -> list[dict]:
    """Fetch jobs from all configured Ashby boards.

    Boards run concurrently (bounded by _BOARD_CONCURRENCY) under an internal
    deadline, and results are collected per-board — a slow or hung board costs
    only itself, not every board after it.
    """
    from . import compile_exclude_patterns

    search = config.get("search", {})
    keywords = search.get("keywords", [])
    exclude_patterns = compile_exclude_patterns(search.get("exclude_keywords", []))
    boards = [b for b in search.get("ashby_boards") or [] if b and b != "example-company"]

    if not boards:
        logger.info("No Ashby board names configured — skipping Ashby source")
        return []

    results: list[dict] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _INTERNAL_BUDGET
    sem = asyncio.Semaphore(_BOARD_CONCURRENCY)

    async with aiohttp.ClientSession(
        headers={"User-Agent": "Jobsmith/1.0"}
    ) as session:
        async def _run_board(board: str) -> None:
            async with sem:
                if loop.time() >= deadline:
                    logger.warning("Ashby: budget exhausted before board %s — skipping", board)
                    return
                jobs = await _fetch_board(session, board, keywords, exclude_patterns)
                results.extend(jobs)

        outcomes = await asyncio.gather(
            *(_run_board(b) for b in boards), return_exceptions=True
        )
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.warning("Ashby board task failed: %s", outcome)

    logger.info("Ashby: %d jobs matched filters", len(results))
    return results
