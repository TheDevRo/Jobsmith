"""
manual.py — Ingest a single job posting from a user-supplied URL.

When the URL matches a known board (LinkedIn so far), we reuse that source's
detail parser to populate the rich fields (salary, easy-apply, tags). For any
other URL, we fall back to a generic JSON-LD JobPosting parser. In all cases,
the resulting job is stamped with `source="manual"` so it stays separable from
scraped jobs and won't collide on the (source, external_id) UNIQUE constraint.
"""

from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

from . import linkedin as _linkedin
from . import clean_description
from ._generic import parse_jsonld_jobposting

logger = logging.getLogger(__name__)


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _manual_external_id(url: str) -> str:
    return "manual:" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def _extract_greenhouse_slug_id(url: str) -> tuple[str, str] | None:
    """Match boards.greenhouse.io and job-boards.greenhouse.io URLs."""
    m = re.search(r"greenhouse\.io/([^/]+)/jobs/(\d+)", url)
    if m:
        return m.group(1), m.group(2)
    return None


async def _fetch_via_greenhouse(url: str) -> dict:
    """Use Greenhouse's public board API for clean structured data."""
    pair = _extract_greenhouse_slug_id(url)
    if not pair:
        raise ValueError("Could not parse Greenhouse slug/job id from URL")
    slug, job_id = pair
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(
            api_url,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                raise ValueError(f"Greenhouse API returned HTTP {resp.status}")
            detail = await resp.json()

    location_name = ""
    if isinstance(detail.get("location"), dict):
        location_name = detail["location"].get("name", "") or ""

    return {
        "title": detail.get("title", ""),
        "company": slug.replace("-", " ").title(),
        "location": location_name,
        "url": detail.get("absolute_url") or url,
        "description": clean_description(detail.get("content", "")),
        "tags": [d.get("name", "") for d in detail.get("departments", []) if d.get("name")],
        "date_posted": detail.get("updated_at", ""),
        "is_remote": "remote" in location_name.lower(),
    }


def _extract_linkedin_job_id(url: str) -> str | None:
    m = re.search(r"/jobs/view/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"currentJobId=(\d+)", url)
    if m:
        return m.group(1)
    return None


async def _fetch_linkedin_topcard(
    session: aiohttp.ClientSession, job_id: str, job: dict
) -> None:
    """Populate title/company/location from LinkedIn's public guest endpoint.

    The search-flow detail parser assumes title/company already came from the
    search card. For manual ingestion we have neither, so hit the lightweight
    `jobs-guest/jobs/api/jobPosting/{id}` fragment which is reliably reachable
    without auth and contains the topcard fields.
    """
    api_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    try:
        async with session.get(
            api_url,
            headers=_HEADERS,
            timeout=aiohttp.ClientTimeout(total=20),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                logger.warning("LinkedIn guest endpoint returned %d for job %s", resp.status, job_id)
                return
            html = await resp.text()
    except Exception:
        logger.debug("LinkedIn guest fetch failed for %s", job_id, exc_info=True)
        return

    soup = BeautifulSoup(html, "lxml")

    title_el = soup.find("h2", class_=re.compile(r"topcard__title|top-card-layout__title"))
    if title_el:
        job["title"] = title_el.get_text(strip=True)

    company_el = soup.find("a", class_=re.compile(r"topcard__org-name-link"))
    if not company_el:
        company_el = soup.find(class_=re.compile(r"topcard__flavor--black-link"))
    if company_el:
        job["company"] = company_el.get_text(strip=True)

    # Location is the bulleted topcard flavor span, sibling of the company link.
    loc_el = soup.find("span", class_=re.compile(r"topcard__flavor--bullet"))
    if loc_el:
        loc_text = loc_el.get_text(strip=True)
        if loc_text:
            job["location"] = loc_text
            job["is_remote"] = "remote" in loc_text.lower()


async def _fetch_via_linkedin(url: str) -> dict:
    """Use LinkedIn's detail parser, then stamp source=manual."""
    job: dict = {"url": url}
    job_id = _extract_linkedin_job_id(url)
    if job_id:
        # Use the canonical guest-view URL for best parser hit-rate
        job["url"] = f"https://www.linkedin.com/jobs/view/{job_id}"

    async with aiohttp.ClientSession() as session:
        # Manual ingestion has no search card, so fetch title/company/location
        # from the public guest topcard endpoint before enrichment.
        if job_id:
            await _fetch_linkedin_topcard(session, job_id, job)
        await _linkedin._fetch_job_detail(session, job, _HEADERS)
    # Restore the user's original URL after the fetch (so the UI links back to what they pasted)
    job["url"] = url
    return job


async def _fetch_generic(url: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            headers=_HEADERS,
            timeout=aiohttp.ClientTimeout(total=30),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                raise ValueError(f"URL returned HTTP {resp.status}")
            html = await resp.text()
    return parse_jsonld_jobposting(html, url)


async def fetch_job_from_url(url: str) -> dict:
    """Fetch and parse a single job URL into a job dict ready for upsert_job().

    Always sets source="manual" and a deterministic external_id so re-pasting
    the same URL is idempotent.
    """
    if not url or not url.strip():
        raise ValueError("URL is required")
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("URL must be an http(s) link")

    host = (parsed.hostname or "").lower()

    if "linkedin.com" in host:
        job = await _fetch_via_linkedin(url)
    elif "greenhouse.io" in host and _extract_greenhouse_slug_id(url):
        job = await _fetch_via_greenhouse(url)
    else:
        job = await _fetch_generic(url)

    job["source"] = "manual"
    job["external_id"] = _manual_external_id(url)
    job.setdefault("url", url)
    if not job.get("title"):
        # Without a title the row is useless — surface a clear error
        raise ValueError("Could not extract a job title from that URL")
    job.setdefault("company", "")
    job.setdefault("location", "")
    job.setdefault("description", "")
    return job
