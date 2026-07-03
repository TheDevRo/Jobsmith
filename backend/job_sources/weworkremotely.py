"""
weworkremotely.py — Fetch jobs from WeWorkRemotely RSS feeds.

Covers all major job categories, not just devops/sysadmin.
"""

import asyncio
import logging
import re

import aiohttp
import feedparser

logger = logging.getLogger(__name__)

_CONCURRENCY = 5

# All available WeWorkRemotely RSS feed categories, per
# weworkremotely.com/remote-job-categories as of 2026-07. Retired slugs
# (remote-programming-jobs, remote-sales-jobs, remote-marketing-jobs,
# remote-hr-jobs, remote-finance-and-legal-jobs) now 301 with no Location.
# The main remote-jobs.rss feed is a superset; dedup handles the overlap.
FEEDS = [
    "https://weworkremotely.com/remote-jobs.rss",
    "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-design-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    "https://weworkremotely.com/categories/remote-customer-support-jobs.rss",
    "https://weworkremotely.com/categories/remote-product-jobs.rss",
    "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
    "https://weworkremotely.com/categories/remote-management-and-finance-jobs.rss",
    "https://weworkremotely.com/categories/all-other-remote-jobs.rss",
]


def _strip_html(html_text: str) -> str:
    from . import clean_description
    return clean_description(html_text)


def _parse_title_company(raw_title: str) -> tuple[str, str]:
    """
    WeWorkRemotely titles are often 'Company: Job Title'.
    Split on the first colon to extract company and title.
    """
    if ":" in raw_title:
        parts = raw_title.split(":", 1)
        return parts[1].strip(), parts[0].strip()
    return raw_title, ""


def _matches_keywords(title: str, description: str, keywords: list[str]) -> bool:
    from . import matches_keywords
    return matches_keywords(f"{title} {description}", keywords)


async def fetch_jobs(config: dict) -> list[dict]:
    """Fetch and filter jobs from WeWorkRemotely RSS feeds (fetched concurrently)."""
    from . import compile_exclude_patterns, fetch_with_retries, matches_exclude

    keywords = config.get("search", {}).get("keywords", [])
    exclude_patterns = compile_exclude_patterns(config.get("search", {}).get("exclude_keywords", []))

    if not keywords:
        return []

    results: list[dict] = []
    seen_links: set[str] = set()
    sem = asyncio.Semaphore(_CONCURRENCY)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }

    async def _fetch_feed(session: aiohttp.ClientSession, feed_url: str) -> None:
        try:
            async with sem:
                status, body = await fetch_with_retries(
                    session, feed_url, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                    allow_redirects=True,
                )
            if status != 200:
                logger.warning("WeWorkRemotely feed %s returned %d", feed_url, status)
                return
        except Exception:
            logger.exception("Failed to fetch feed %s", feed_url)
            return

        feed = feedparser.parse(body)
        for entry in feed.entries:
            link = entry.get("link", "")
            if link in seen_links:
                continue
            seen_links.add(link)

            raw_title = entry.get("title", "")
            title, company = _parse_title_company(raw_title)
            description = _strip_html(entry.get("summary", entry.get("description", "")))

            if not _matches_keywords(title, description, keywords):
                continue
            if matches_exclude(title, exclude_patterns):
                continue

            results.append({
                "source": "weworkremotely",
                "external_id": link,
                "title": title,
                "company": company,
                "location": "Remote",
                "url": link,
                "description": description,
                "salary_min": None,
                "salary_max": None,
                "tags": [],
                "date_posted": entry.get("published", ""),
                "is_remote": True,
            })

    async with aiohttp.ClientSession() as session:
        outcomes = await asyncio.gather(
            *[_fetch_feed(session, url) for url in FEEDS],
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, Exception):
                logger.warning("WeWorkRemotely feed task failed: %s", outcome)

    logger.info("WeWorkRemotely: %d jobs matched filters", len(results))
    return results
