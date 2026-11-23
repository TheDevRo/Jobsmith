"""
tests/job_sources/test_weworkremotely.py

Fixture-based test for the WeWorkRemotely RSS parser (stdlib ElementTree,
which replaced feedparser). Pins the job dicts produced from a saved feed so a
parser swap or feed-format change shows up as a diff, not as 0 jobs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from backend.job_sources import weworkremotely as wwr

FEED = (Path(__file__).parent / "fixtures" / "weworkremotely.rss").read_text()

_CONFIG = {"search": {"keywords": ["python", "security"], "exclude_keywords": ["sales"]}}


def _fetch(body: str) -> list[dict]:
    async def fake_fetch(session, url, **kwargs):
        return (200, body) if url == wwr.FEEDS[0] else (404, "")

    with patch("backend.job_sources.fetch_with_retries", fake_fetch):
        return asyncio.run(wwr.fetch_jobs(_CONFIG))


def _job(link: str, title: str, company: str, description: str, date_posted: str) -> dict:
    return {
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
        "date_posted": date_posted,
        "is_remote": True,
    }


EXPECTED = [
    _job(
        "https://weworkremotely.com/remote-jobs/acme-corp-senior-security-engineer",
        "Senior Security Engineer", "Acme Corp",
        "Headquarters: Denver, CO We are hiring a security engineer to run our SIEM.\n"
        "Python & AWS required.",
        "Mon, 05 Oct 2026 14:12:03 +0000",
    ),
    _job(
        "https://weworkremotely.com/remote-jobs/smith-jones-llc-python-developer",
        "Python Developer", "Smith & Jones LLC",
        "Build Python services. Salary: $120k",
        "Sun, 04 Oct 2026 09:00:00 +0000",
    ),
    _job(
        "https://weworkremotely.com/remote-jobs/untitled-role",
        "Untitled Role Without Company", "",
        "Python generalist.",
        "",
    ),
]


def test_feed_fixture_parses_to_expected_jobs():
    # Covers CDATA and entity-escaped HTML descriptions, entities in titles,
    # whitespace around <link>, missing pubDate, duplicate links across items,
    # and the keyword / exclude filters.
    assert _fetch(FEED) == EXPECTED


def test_bare_ampersand_still_parses():
    malformed = FEED.replace("Smith &amp; Jones", "Smith & Jones")
    assert malformed != FEED
    assert _fetch(malformed) == EXPECTED


def test_unparseable_feed_is_skipped():
    assert _fetch("<rss><channel><item><title>broken") == []
