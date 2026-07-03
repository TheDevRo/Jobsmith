"""
usajobs.py — Fetch jobs from the USAJobs API.

USAJobs is the official U.S. government job board. The API requires an
API key (free — register at developer.usajobs.gov with your email).
Configure api_keys.usajobs_email and api_keys.usajobs_api_key in config.yaml.
"""

import asyncio
import json
import logging

import aiohttp

from . import clean_description

logger = logging.getLogger(__name__)

BASE_URL = "https://data.usajobs.gov/api/Search"

# Keyword × location searches run concurrently; the API is keyed and
# rate-limited generously enough for modest parallelism.
_CONCURRENCY = 5


def detect_usajobs_apply_type(job: dict) -> str:
    """Classify a stored USAJobs job dict as 'easy_apply', 'external', or 'unknown'.

    Works only from data already present in the dict — no network calls are made.

    USAJobs hosts its own structured apply flow at usajobs.gov, so any job whose
    stored URL is on that domain is treated as fully automatable in-app.  If the
    PositionURI somehow points to an external agency site (rare but possible for
    agency-specific delegated vacancy announcements), the job is classified as
    external.

    Returns:
      ``'easy_apply'``  — apply flow is on usajobs.gov (handled fully in-app).
      ``'external'``    — URL points to a non-USAJobs domain.
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

    is_usajobs = hostname == "usajobs.gov" or hostname.endswith(".usajobs.gov")
    if is_usajobs:
        return "easy_apply"

    if hostname:
        return "external"

    return "unknown"


async def fetch_jobs(config: dict) -> list[dict]:
    """Fetch jobs from USAJobs for each configured keyword."""
    email = config.get("api_keys", {}).get("usajobs_email", "")
    api_key = config.get("api_keys", {}).get("usajobs_api_key", "")

    if not email or not api_key:
        logger.info("USAJobs credentials not configured — skipping source")
        return []

    from . import compile_exclude_patterns, fetch_with_retries, matches_exclude

    keywords = config.get("search", {}).get("keywords", [])
    locations = config.get("search", {}).get("locations", [""])
    exclude_patterns = compile_exclude_patterns(config.get("search", {}).get("exclude_keywords", []))
    max_age = config.get("search", {}).get("max_age_days", 7)

    results: list[dict] = []
    seen_ids: set[str] = set()
    sem = asyncio.Semaphore(_CONCURRENCY)

    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": email,
        "Authorization-Key": api_key,
    }

    async def _fetch_combo(session: aiohttp.ClientSession, keyword: str, location: str) -> None:
        params = {
            "Keyword": keyword,
            "LocationName": location if location.lower() != "remote" else "",
            "ResultsPerPage": 50,
            "DatePosted": max_age,
        }
        if location.lower() == "remote":
            params["RemoteIndicator"] = "True"

        try:
            async with sem:
                status, body = await fetch_with_retries(
                    session, BASE_URL, params=params, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                )
            if status != 200:
                logger.warning("USAJobs returned %d for keyword=%s", status, keyword)
                return
            data = json.loads(body)
        except Exception:
            logger.exception("USAJobs request failed for keyword=%s", keyword)
            return

        items = (
            data.get("SearchResult", {})
            .get("SearchResultItems", [])
        )

        for item in items:
            match = item.get("MatchedObjectDescriptor", {})
            ext_id = match.get("PositionID", "")
            if ext_id in seen_ids:
                continue
            seen_ids.add(ext_id)

            title = match.get("PositionTitle", "")
            if matches_exclude(title, exclude_patterns):
                continue

            org = match.get("OrganizationName", "")
            dept = match.get("DepartmentName", "")
            company = f"{org} ({dept})" if dept and dept != org else org

            # Location
            locs = match.get("PositionLocation", [])
            loc_str = ", ".join(
                loc.get("LocationName", "") for loc in locs[:3]
            ) if locs else ""

            # Salary
            remuneration = match.get("PositionRemuneration", [])
            salary_min = None
            salary_max = None
            if remuneration:
                try:
                    salary_min = int(float(remuneration[0].get("MinimumRange", 0)))
                    salary_max = int(float(remuneration[0].get("MaximumRange", 0)))
                except (ValueError, TypeError):
                    pass

            url = match.get("PositionURI", "")
            description = match.get("UserArea", {}).get("Details", {}).get("MajorDuties", "")
            if isinstance(description, list):
                description = " ".join(description)
            if not description:
                description = match.get("QualificationSummary", "")

            results.append({
                "source": "usajobs",
                "external_id": ext_id,
                "title": title,
                "company": company,
                "location": loc_str,
                "url": url,
                "description": clean_description(description),
                "salary_min": salary_min if salary_min else None,
                "salary_max": salary_max if salary_max else None,
                "salary_period": "annual",
                "tags": ["government", "federal"],
                "date_posted": match.get("PublicationStartDate", ""),
                "is_remote": any(
                    "remote" in loc.get("LocationName", "").lower()
                    or "negotiable" in loc.get("LocationName", "").lower()
                    for loc in locs
                ),
            })

    async with aiohttp.ClientSession() as session:
        outcomes = await asyncio.gather(
            *[_fetch_combo(session, kw, loc) for kw in keywords for loc in locations],
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.warning("USAJobs combo task failed: %s", outcome)

    logger.info("USAJobs: %d jobs matched filters", len(results))
    return results
