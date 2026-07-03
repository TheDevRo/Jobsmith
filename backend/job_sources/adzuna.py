"""
adzuna.py — Fetch jobs from the Adzuna API.

Requires api_keys.adzuna_app_id and api_keys.adzuna_app_key in config.
If keys are missing the module logs a message and returns an empty list.

Keyword × location combinations run concurrently (bounded by a semaphore);
each combination paginates up to 3 pages sequentially.
"""

import asyncio
import json
import logging

from . import clean_description

import aiohttp

logger = logging.getLogger(__name__)

BASE_URL = "https://api.adzuna.com/v1/api/jobs/us/search"

# Adzuna's free tier rate-limits aggressively — 5 concurrent requests trips
# 429s within seconds. Two lanes plus the retry helper's 429 backoff stays
# under the limit while still beating the old fully-sequential walk.
_CONCURRENCY = 2


async def fetch_jobs(config: dict) -> list[dict]:
    """Fetch jobs from Adzuna for each configured keyword × location."""
    app_id = config.get("api_keys", {}).get("adzuna_app_id", "")
    app_key = config.get("api_keys", {}).get("adzuna_app_key", "")

    if not app_id or not app_key:
        logger.info("Adzuna API keys not configured — skipping source")
        return []

    from . import compile_exclude_patterns, fetch_with_retries, matches_exclude

    keywords = config.get("search", {}).get("keywords", [])
    locations = config.get("search", {}).get("locations", [""])
    exclude_patterns = compile_exclude_patterns(config.get("search", {}).get("exclude_keywords", []))
    max_age = config.get("search", {}).get("max_age_days", 7)

    results: list[dict] = []
    seen_ids: set[str] = set()
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _fetch_combo(session: aiohttp.ClientSession, keyword: str, location: str) -> None:
        # Paginate up to 3 pages for broader results; pages within a combo
        # stay sequential because each page's emptiness ends the walk.
        for page_num in range(1, 4):
            params = {
                "app_id": app_id,
                "app_key": app_key,
                "what": keyword,
                "where": location,
                "max_days_old": max_age,
                "results_per_page": 50,
                "content-type": "application/json",
            }
            try:
                async with sem:
                    status, body = await fetch_with_retries(
                        session, f"{BASE_URL}/{page_num}", params=params,
                        timeout=aiohttp.ClientTimeout(total=30),
                    )
                if status != 200:
                    logger.warning("Adzuna returned %d for keyword=%s page=%d", status, keyword, page_num)
                    break  # Stop pagination on error
                data = json.loads(body)
            except Exception:
                logger.exception("Adzuna request failed for keyword=%s page=%d", keyword, page_num)
                break

            page_results = data.get("results", [])
            if not page_results:
                break  # No more results

            for item in page_results:
                ext_id = str(item.get("id", ""))
                if ext_id in seen_ids:
                    continue
                seen_ids.add(ext_id)

                title = item.get("title", "")
                if matches_exclude(title, exclude_patterns):
                    continue

                company_info = item.get("company", {})
                location_info = item.get("location", {})

                results.append({
                    "source": "adzuna",
                    "external_id": ext_id,
                    "title": title,
                    "company": company_info.get("display_name", ""),
                    "location": location_info.get("display_name", ""),
                    "url": item.get("redirect_url", ""),
                    "description": clean_description(item.get("description", "")),
                    "salary_min": item.get("salary_min"),
                    "salary_max": item.get("salary_max"),
                    "salary_period": "annual",
                    "tags": item.get("category", {}).get("tag", "").split(",") if item.get("category") else [],
                    "date_posted": item.get("created", ""),
                    "is_remote": "remote" in title.lower() or "remote" in location_info.get("display_name", "").lower(),
                })

    async with aiohttp.ClientSession() as session:
        outcomes = await asyncio.gather(
            *[_fetch_combo(session, kw, loc) for kw in keywords for loc in locations],
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.warning("Adzuna combo task failed: %s", outcome)

    logger.info("Adzuna: %d jobs matched filters", len(results))
    return results
