"""
arbeitnow.py — Fetch jobs from the Arbeitnow API.

Arbeitnow is a free job board API with no authentication required.
Focuses on remote and tech jobs. Returns paginated JSON.
"""

import json
import logging

import aiohttp

from . import clean_description

logger = logging.getLogger(__name__)

API_URL = "https://www.arbeitnow.com/api/job-board-api"


def _matches_keywords(job: dict, keywords: list[str]) -> bool:
    """Check if a job matches any configured search keywords."""
    from . import matches_keywords
    text = f"{job.get('title', '')} {job.get('company_name', '')} {job.get('description', '')}"
    return matches_keywords(text, keywords)


async def fetch_jobs(config: dict) -> list[dict]:
    """Fetch and filter jobs from Arbeitnow."""
    from . import compile_exclude_patterns, fetch_with_retries, matches_exclude

    keywords = config.get("search", {}).get("keywords", [])
    exclude_patterns = compile_exclude_patterns(config.get("search", {}).get("exclude_keywords", []))

    if not keywords:
        return []

    results: list[dict] = []
    seen_slugs: set[str] = set()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            # Fetch up to 3 pages
            for page in range(1, 4):
                status, body = await fetch_with_retries(
                    session,
                    API_URL,
                    params={"page": page},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                )
                if status != 200:
                    logger.warning("Arbeitnow returned %d on page %d", status, page)
                    break
                data = json.loads(body)

                jobs_list = data.get("data", [])
                if not jobs_list:
                    break

                for item in jobs_list:
                    slug = item.get("slug", "")
                    if slug in seen_slugs:
                        continue
                    seen_slugs.add(slug)

                    if not _matches_keywords(item, keywords):
                        continue

                    title = item.get("title", "")
                    if matches_exclude(title, exclude_patterns):
                        continue

                    location = item.get("location", "")
                    is_remote = item.get("remote", False) or "remote" in location.lower()

                    tags = item.get("tags", [])
                    if isinstance(tags, str):
                        tags = [t.strip() for t in tags.split(",") if t.strip()]

                    results.append({
                        "source": "arbeitnow",
                        "external_id": slug,
                        "title": title,
                        "company": item.get("company_name", ""),
                        "location": location or ("Remote" if is_remote else ""),
                        "url": item.get("url", f"https://www.arbeitnow.com/view/{slug}"),
                        "description": clean_description(item.get("description", "")),
                        "salary_min": None,
                        "salary_max": None,
                        "tags": tags if isinstance(tags, list) else [],
                        "date_posted": item.get("created_at", ""),
                        "is_remote": is_remote,
                    })

                # Stop if no more pages
                if not data.get("links", {}).get("next"):
                    break

    except aiohttp.ClientError as e:
        logger.warning("Arbeitnow request failed: %s", str(e))
        return []

    logger.info("Arbeitnow: %d jobs matched filters", len(results))
    return results
