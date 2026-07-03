"""
remoteok.py — Fetch jobs from the RemoteOK JSON API.

RemoteOK aggressively blocks automated requests. This module uses browser-like
headers and handles their common anti-bot responses (403, HTML instead of JSON).
"""

import logging
import re

import aiohttp

logger = logging.getLogger(__name__)

API_URL = "https://remoteok.com/api"


def _strip_html(html_text: str) -> str:
    from . import clean_description
    return clean_description(html_text)


def _matches_keywords(job: dict, keywords: list[str]) -> bool:
    """Check if a job matches any of the configured search keywords (case-insensitive)."""
    from . import matches_keywords
    text = f"{job.get('position', '')} {job.get('company', '')} {' '.join(job.get('tags', []))} {job.get('description', '')}"
    return matches_keywords(text, keywords)


async def fetch_jobs(config: dict) -> list[dict]:
    """Fetch and filter jobs from RemoteOK.

    Raises SourceBlockedError when RemoteOK is bot-blocking us, so the
    orchestrator can surface 'blocked' instead of a silent zero-job run.
    """
    from . import SourceBlockedError, compile_exclude_patterns, fetch_with_retries, matches_exclude

    keywords = config.get("search", {}).get("keywords", [])
    exclude_patterns = compile_exclude_patterns(config.get("search", {}).get("exclude_keywords", []))

    if not keywords:
        return []

    # RemoteOK requires browser-like headers to avoid 403s
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://remoteok.com/",
        "Origin": "https://remoteok.com",
    }

    try:
        async with aiohttp.ClientSession() as session:
            status, raw_text = await fetch_with_retries(
                session,
                API_URL,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=True,
            )
        if status == 403:
            raise SourceBlockedError("RemoteOK returned 403 — blocking automated requests")
        if status != 200:
            logger.warning("RemoteOK returned status %d", status)
            return []

        # RemoteOK sometimes returns HTML instead of JSON when blocking
        if "<html" in raw_text[:200].lower():
            raise SourceBlockedError("RemoteOK returned HTML instead of JSON — likely blocked")

        try:
            import json
            data = json.loads(raw_text)
        except Exception:
            logger.warning("RemoteOK response was not valid JSON")
            return []
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning("RemoteOK request failed: %s", str(e))
        return []

    # First element is metadata — skip it
    jobs_raw = data[1:] if isinstance(data, list) and len(data) > 1 else []
    results: list[dict] = []

    for item in jobs_raw:
        if not isinstance(item, dict):
            continue
        if not _matches_keywords(item, keywords):
            continue
        title = item.get("position", "")
        if matches_exclude(title, exclude_patterns):
            continue

        # Build proper URL
        slug = item.get("slug", item.get("id", ""))
        url = item.get("url", "")
        if not url or url == "":
            url = f"https://remoteok.com/remote-jobs/{slug}" if slug else ""

        # Extract apply URL if available (some jobs have direct links)
        apply_url = item.get("apply_url", url)

        results.append({
            "source": "remoteok",
            "external_id": str(item.get("id", "")),
            "title": title,
            "company": item.get("company", ""),
            "location": item.get("location", "Remote"),
            "url": apply_url if apply_url else url,
            "description": _strip_html(item.get("description", "")),
            "salary_min": item.get("salary_min"),
            "salary_max": item.get("salary_max"),
            "salary_period": "annual",
            "tags": item.get("tags", []),
            "date_posted": item.get("date", ""),
            "is_remote": True,
        })

    logger.info("RemoteOK: %d jobs matched filters", len(results))
    return results
