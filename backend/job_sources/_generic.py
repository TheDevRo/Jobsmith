"""
_generic.py — Source-agnostic JobPosting parser.

Used by manual.py when a pasted URL doesn't match a known board. Looks for
schema.org JobPosting JSON-LD first (covered by Lever, Workable, Ashby, and
many ATS platforms), then falls back to <title> / <meta> tags.
"""

from __future__ import annotations

import json
import logging
import re
from html import unescape

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def _strip_html(html_text: str) -> str:
    if not html_text:
        return ""
    soup = BeautifulSoup(html_text, "lxml")
    text = soup.get_text(separator="\n")
    text = unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _find_jobposting(node):
    """Recurse a JSON-LD node looking for an object with @type == JobPosting."""
    if isinstance(node, dict):
        t = node.get("@type")
        if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
            return node
        graph = node.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                hit = _find_jobposting(item)
                if hit:
                    return hit
    elif isinstance(node, list):
        for item in node:
            hit = _find_jobposting(item)
            if hit:
                return hit
    return None


def _parse_salary(ld: dict) -> tuple[int | None, int | None, str]:
    base = ld.get("baseSalary")
    if not isinstance(base, dict):
        return None, None, "unknown"
    val = base.get("value")
    if not isinstance(val, dict):
        return None, None, "unknown"
    s_min = val.get("minValue") or val.get("value")
    s_max = val.get("maxValue") or val.get("value")
    unit = (val.get("unitText") or "").upper()
    period = "unknown"
    if unit == "HOUR":
        period = "hourly"
    elif unit in ("YEAR", "ANNUAL"):
        period = "annual"
    try:
        s_min = int(float(s_min)) if s_min is not None else None
        s_max = int(float(s_max)) if s_max is not None else None
    except (TypeError, ValueError):
        s_min, s_max = None, None
    if period == "unknown" and s_min is not None and s_min < 1000:
        period = "hourly"
    return s_min, s_max, period


def parse_jsonld_jobposting(html: str, url: str) -> dict:
    """Parse a job posting page into a job dict.

    Returns a dict with whatever could be extracted; missing fields are simply
    omitted. Caller is responsible for setting `source` and `external_id`.
    """
    soup = BeautifulSoup(html, "lxml")
    job: dict = {"url": url}

    ld = None
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        ld = _find_jobposting(data)
        if ld:
            break

    if ld:
        title = ld.get("title")
        if title:
            job["title"] = str(title).strip()

        org = ld.get("hiringOrganization")
        if isinstance(org, dict):
            name = org.get("name")
            if name:
                job["company"] = str(name).strip()
        elif isinstance(org, str):
            job["company"] = org.strip()

        desc = ld.get("description")
        if desc:
            job["description"] = _strip_html(str(desc))

        s_min, s_max, period = _parse_salary(ld)
        if s_min:
            job["salary_min"] = s_min
        if s_max:
            job["salary_max"] = s_max
        if period != "unknown" and (s_min or s_max):
            job["salary_period"] = period

        jl = ld.get("jobLocation")
        entries = jl if isinstance(jl, list) else ([jl] if isinstance(jl, dict) else [])
        for entry in entries:
            addr = entry.get("address", {}) if isinstance(entry, dict) else {}
            if isinstance(addr, dict):
                city = addr.get("addressLocality", "")
                region = addr.get("addressRegion", "")
                parts = [p for p in [city, region] if p]
                if parts:
                    job["location"] = ", ".join(parts)
                    break

        date_posted = ld.get("datePosted")
        if date_posted:
            job["date_posted"] = str(date_posted)

        emp_type = ld.get("employmentType")
        if emp_type:
            job["tags"] = emp_type if isinstance(emp_type, list) else [str(emp_type)]

        if ld.get("jobLocationType") == "TELECOMMUTE":
            job["is_remote"] = True

    # Fallback title: <meta property="og:title"> or <title>
    if not job.get("title"):
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            job["title"] = og_title["content"].strip()
        elif soup.title and soup.title.string:
            job["title"] = soup.title.string.strip()

    # Fallback company: <meta property="og:site_name">
    if not job.get("company"):
        og_site = soup.find("meta", attrs={"property": "og:site_name"})
        if og_site and og_site.get("content"):
            job["company"] = og_site["content"].strip()

    # Fallback description: <meta name="description">
    if not job.get("description"):
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            job["description"] = meta_desc["content"].strip()

    return job
