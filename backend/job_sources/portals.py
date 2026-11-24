"""
portals.py — Declarative job portals.

A portal is a JSON file (format: docs/portals.md) describing a JSON API, an
RSS/Atom feed, or a page carrying schema.org JobPosting data. The engine only
*reads* the file: nothing in it is evaluated, imported or executed. Every
request must be https to a host the file lists, on every redirect hop, and
must pass the same public-address SSRF guard as manual URL ingestion.

Bundled files live in job_sources/portals/; users may add their own under
<data dir>/portals/ (they can never replace a bundled id or a built-in source).
Portals are opt-in: each is OFF until `search.portals.<id>.enabled` is true.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx

from ..http_client import async_client, send
from ..paths import project_root

logger = logging.getLogger(__name__)

BUNDLED_DIR = Path(__file__).resolve().parent / "portals"

# Conservative hard limits; a portal file may only ask for less.
MAX_FILE_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_PAGES = 3
MAX_QUERIES = 3
MAX_ITEMS = 200
MAX_REDIRECTS = 3
REQUEST_TIMEOUT = 15
RUN_BUDGET = 45  # seconds for one portal run, under the orchestrator's 60s
MIN_DELAY = 1.0
_HEADERS = {"User-Agent": "Jobsmith (+https://github.com/TheDevRo/Jobsmith)"}

KINDS = ("json_api", "feed", "schema_org")
FIELDS = (
    "title", "company", "location", "url", "description", "posted_at",
    "salary_min", "salary_max", "salary_period", "salary_currency",
    "remote", "external_id", "tags",
)
_TOP_KEYS = {
    "id", "name", "version", "homepage", "terms_url", "free", "hosts", "kind",
    "request", "items", "fields", "defaults", "attribution", "min_interval_minutes",
}
_REQUEST_KEYS = {"url", "headers", "max_pages", "max_queries", "delay_seconds"}
_DEFAULT_KEYS = {"location", "remote"}
_PLACEHOLDERS = {"query", "location", "page", "api_key"}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_PATH_RE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")
_TAG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")
_FORBIDDEN_HEADERS = {"host", "cookie", "content-length", "connection", "transfer-encoding"}
_PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")


class PortalError(ValueError):
    """A portal file is malformed or breaks a rule."""


def _check_keys(obj: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise PortalError(f"{where}: unknown key(s) {', '.join(map(repr, unknown))}")


def _placeholders(template: str, where: str) -> set[str]:
    found = set(_PLACEHOLDER_RE.findall(template))
    bad = found - _PLACEHOLDERS
    if bad:
        raise PortalError(f"{where}: unknown placeholder(s) {sorted(bad)}")
    if "{" in _PLACEHOLDER_RE.sub("", template) or "}" in _PLACEHOLDER_RE.sub("", template):
        raise PortalError(f"{where}: unbalanced braces")
    return found


def _check_https(url: object, where: str) -> str:
    if not isinstance(url, str) or not url:
        raise PortalError(f"{where}: must be a non-empty string")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise PortalError(f"{where}: must be an https URL")
    return url


def _check_url_host(url: str, hosts: list[str], where: str) -> None:
    """Scheme/host/userinfo/port rules shared by templates and live URLs."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise PortalError(f"{where}: must be https")
    if parts.username or parts.password or parts.port is not None:
        raise PortalError(f"{where}: credentials and ports are not allowed in the URL")
    if (parts.hostname or "") not in hosts:
        raise PortalError(f"{where}: host {parts.hostname!r} is not in hosts")


def validate_portal(raw: object) -> dict:
    """Return *raw* if it is a valid portal definition, else raise PortalError."""
    if not isinstance(raw, dict):
        raise PortalError("portal file must be a JSON object")
    _check_keys(raw, _TOP_KEYS, "portal")
    for key in ("id", "name", "version", "homepage", "terms_url", "free", "hosts", "kind", "request"):
        if key not in raw:
            raise PortalError(f"portal: missing required key {key!r}")

    if not isinstance(raw["id"], str) or not _ID_RE.match(raw["id"]):
        raise PortalError("id: must be 3-32 chars of a-z, 0-9, '-' or '_'")
    for key in ("name", "version"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise PortalError(f"{key}: must be a non-empty string")
    _check_https(raw["homepage"], "homepage")
    _check_https(raw["terms_url"], "terms_url")
    if not isinstance(raw["free"], bool):
        raise PortalError("free: must be true or false")

    hosts = raw["hosts"]
    if not isinstance(hosts, list) or not hosts or len(hosts) > 5:
        raise PortalError("hosts: must be a list of 1-5 hostnames")
    for host in hosts:
        if (not isinstance(host, str) or not _HOST_RE.match(host)
                or host.rsplit(".", 1)[-1].isdigit()):
            raise PortalError(f"hosts: {host!r} is not a plain lowercase hostname")

    kind = raw["kind"]
    if kind not in KINDS:
        raise PortalError(f"kind: must be one of {', '.join(KINDS)}")

    if "attribution" in raw and not (isinstance(raw["attribution"], str) and raw["attribution"].strip()):
        raise PortalError("attribution: must be a non-empty string")
    interval = raw.get("min_interval_minutes", 0)
    if isinstance(interval, bool) or not isinstance(interval, int) or not 0 <= interval <= 10080:
        raise PortalError("min_interval_minutes: must be an integer from 0 to 10080")

    req = raw["request"]
    if not isinstance(req, dict):
        raise PortalError("request: must be an object")
    _check_keys(req, _REQUEST_KEYS, "request")
    template = _check_https(req.get("url"), "request.url")
    used = _placeholders(template, "request.url")
    if "{" in urlsplit(template).netloc:
        raise PortalError("request.url: the host cannot contain a placeholder")
    _check_url_host(_PLACEHOLDER_RE.sub("x", template), hosts, "request.url")
    if "page" in used and "max_pages" not in req:
        raise PortalError("request.max_pages: required when the URL uses {page}")
    if "api_key" in used:
        raise PortalError("request.url: {api_key} is only allowed in headers")
    for key, cap in (("max_pages", MAX_PAGES), ("max_queries", MAX_QUERIES)):
        if key in req and (isinstance(req[key], bool) or not isinstance(req[key], int)
                           or not 1 <= req[key] <= cap):
            raise PortalError(f"request.{key}: must be an integer from 1 to {cap}")
    if "delay_seconds" in req and (
            isinstance(req["delay_seconds"], bool) or not isinstance(req["delay_seconds"], (int, float))
            or not MIN_DELAY <= req["delay_seconds"] <= 30):
        raise PortalError(f"request.delay_seconds: must be a number from {MIN_DELAY:g} to 30")
    headers = req.get("headers", {})
    if not isinstance(headers, dict) or len(headers) > 5:
        raise PortalError("request.headers: must be an object with at most 5 headers")
    for name, value in headers.items():
        if not _HEADER_NAME_RE.match(name) or name.lower() in _FORBIDDEN_HEADERS:
            raise PortalError(f"request.headers: header name {name!r} is not allowed")
        if not isinstance(value, str) or "\n" in value or "\r" in value:
            raise PortalError(f"request.headers.{name}: must be a single-line string")
        bad = _placeholders(value, f"request.headers.{name}") - {"api_key"}
        if bad:
            raise PortalError(f"request.headers.{name}: only {{api_key}} may be used here")

    items, fields = raw.get("items"), raw.get("fields")
    if kind == "json_api":
        if not isinstance(items, str) or not (items == "$" or _PATH_RE.match(items)):
            raise PortalError("items: required for json_api — a dotted path, or '$' for a top-level array")
        if not isinstance(fields, dict):
            raise PortalError("fields: required for json_api")
        for needed in ("title", "url"):
            if needed not in fields:
                raise PortalError(f"fields: {needed!r} is required")
    elif kind == "feed":
        if items is not None:
            raise PortalError("items: not used by feed portals")
        if fields is not None and not isinstance(fields, dict):
            raise PortalError("fields: must be an object")
    else:
        if items is not None or fields is not None:
            raise PortalError("items/fields: not used by schema_org portals")
    for name, path in (fields or {}).items():
        if name not in FIELDS:
            raise PortalError(f"fields: unknown job field {name!r}")
        pattern = _PATH_RE if kind == "json_api" else _TAG_RE
        if not isinstance(path, str) or not pattern.match(path):
            raise PortalError(f"fields.{name}: invalid {'path' if kind == 'json_api' else 'tag name'}")

    defaults = raw.get("defaults", {})
    if not isinstance(defaults, dict):
        raise PortalError("defaults: must be an object")
    _check_keys(defaults, _DEFAULT_KEYS, "defaults")
    if "remote" in defaults and not isinstance(defaults["remote"], bool):
        raise PortalError("defaults.remote: must be true or false")
    if "location" in defaults and not isinstance(defaults["location"], str):
        raise PortalError("defaults.location: must be a string")
    return raw


def load_portal_file(path: Path) -> dict:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise PortalError(f"{path.name}: file is larger than {MAX_FILE_BYTES} bytes")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PortalError(f"{path.name}: not valid JSON ({exc})") from exc
    return validate_portal(raw)


def user_portals_dir() -> Path:
    return project_root() / "data" / "portals"


def load_portals(
    bundled_dir: Path | None = None, user_dir: Path | None = None, reserved: frozenset | set = frozenset(),
) -> dict[str, dict]:
    """Every valid portal by id. Bad files are logged and skipped, never raised;
    bundled ids win, and nothing may take a built-in source's name (*reserved*)."""
    out: dict[str, dict] = {}
    for directory in (bundled_dir or BUNDLED_DIR, user_dir or user_portals_dir()):
        try:
            files = sorted(directory.glob("*.json"))
        except OSError:
            continue
        for path in files:
            try:
                portal = load_portal_file(path)
            except (PortalError, OSError) as exc:
                logger.warning("Ignoring portal file %s: %s", path.name, exc)
                continue
            if portal["id"] in out or portal["id"] in reserved:
                logger.warning("Ignoring portal file %s: id %r is already taken", path.name, portal["id"])
                continue
            out[portal["id"]] = portal
    return out


# ----- Fetching -----

def _fill(template: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER_RE.sub(lambda m: quote(values.get(m.group(1), ""), safe=""), template)


async def _get(client: httpx.AsyncClient, portal: dict, url: str, headers: dict) -> httpx.Response:
    """GET following redirects by hand: each hop must stay https, on a listed
    host, and resolve to a public address before we connect to it."""
    from .manual import assert_public_http_url

    for _ in range(MAX_REDIRECTS + 1):
        _check_url_host(url, portal["hosts"], "request")
        await asyncio.to_thread(assert_public_http_url, url)
        resp = await send(client, "GET", url, headers=headers, timeout=REQUEST_TIMEOUT,
                          follow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            if not location:
                raise PortalError(f"HTTP {resp.status_code} without a Location")
            url = urljoin(url, location)
            continue
        if len(resp.content) > MAX_RESPONSE_BYTES:
            raise PortalError("response is too large")
        return resp
    raise PortalError(f"more than {MAX_REDIRECTS} redirects")


def _dig(node, path: str):
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(t for t in (_text(v) for v in value) if t)
    if isinstance(value, dict):
        return ""
    return str(value).strip()


def _number(value) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return int(float(str(value).replace(",", "")))
    except ValueError:
        return None


_ANNUAL_FACTOR = {"monthly": 12, "fortnightly": 26, "biweekly": 26, "weekly": 52}


def _salary(values: dict) -> dict:
    """salary_min/max/period from raw mapped values; non-USD figures are dropped
    because the global salary floor compares raw numbers."""
    cur = _text(values.get("salary_currency")).upper()
    lo, hi = _number(values.get("salary_min")), _number(values.get("salary_max"))
    if (cur and cur != "USD") or not (lo or hi):
        return {"salary_min": None, "salary_max": None}
    period = _text(values.get("salary_period")).lower()
    factor = _ANNUAL_FACTOR.get(period)
    if factor:
        lo, hi, period = (lo * factor if lo else lo), (hi * factor if hi else hi), "annual"
    elif period in ("yearly", "annual", "year"):
        period = "annual"
    elif period in ("hourly", "hour"):
        period = "hourly"
    else:
        period = ""
    out = {"salary_min": lo or None, "salary_max": hi or None}
    if period:
        out["salary_period"] = period
    return out


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "remote")
    return bool(value)


def _build_job(portal: dict, values: dict) -> dict | None:
    """Map raw per-field values to Jobsmith's job dict (same shape as the other sources)."""
    from . import clean_description

    title, url = _text(values.get("title")), _text(values.get("url"))
    if not title or urlsplit(url).scheme not in ("http", "https"):
        return None
    defaults = portal.get("defaults", {})
    is_remote = _truthy(values["remote"]) if values.get("remote") is not None else bool(defaults.get("remote", False))
    location = _text(values.get("location")) or defaults.get("location", "")
    if not location and is_remote:
        location = "Remote"
    tags = values.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.strip("[]").replace("'", "").split(",") if t.strip()]
    job = {
        "source": portal["id"],
        "external_id": _text(values.get("external_id")) or url,
        "title": title,
        "company": _text(values.get("company")),
        "location": location,
        "url": url,
        "description": clean_description(_text(values.get("description"))),
        "tags": [str(t) for t in tags] if isinstance(tags, list) else [],
        "date_posted": _text(values.get("posted_at")),
        "is_remote": is_remote or "remote" in location.lower(),
    }
    job.update(_salary(values))
    return job


def _parse_json_api(portal: dict, body: str) -> list[dict]:
    data = json.loads(body)
    items = data if portal["items"] == "$" else _dig(data, portal["items"])
    if not isinstance(items, list):
        return []
    jobs = []
    for item in items[:MAX_ITEMS]:
        if isinstance(item, dict):
            values = {name: _dig(item, path) for name, path in portal["fields"].items()}
            job = _build_job(portal, values)
            if job:
                jobs.append(job)
    return jobs


_FEED_DEFAULTS = {
    "title": "title", "url": "link", "description": "description", "posted_at": "pubDate",
    "company": "author", "external_id": "guid",
}


_ATOM_DEFAULTS = {"url": "link", "description": "summary", "posted_at": "updated", "external_id": "id"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_feed(portal: dict, body: str) -> list[dict]:
    # No DTD/entity expansion in stdlib ElementTree's expat for external entities,
    # but refuse documents declaring any to stay clear of entity-bomb tricks.
    if "<!ENTITY" in body:
        raise PortalError("feed declares entities")
    root = ET.fromstring(body)
    custom = portal.get("fields") or {}
    jobs = []
    for entry in root.iter():
        if _local(entry.tag) not in ("item", "entry"):
            continue
        # First non-empty value per child element name (an Atom <link> carries href).
        children: dict[str, str] = {}
        for child in entry:
            text = (child.text or "").strip() or child.get("href", "")
            if text:
                children.setdefault(_local(child.tag), text)
        values = {}
        for name in FIELDS:
            tag = custom.get(name)
            candidates = [tag] if tag else [_FEED_DEFAULTS.get(name), _ATOM_DEFAULTS.get(name)]
            values[name] = next((children[t] for t in candidates if t in children), None)
        job = _build_job(portal, values)
        if job:
            jobs.append(job)
        if len(jobs) >= MAX_ITEMS:
            break
    return jobs


def _all_jobpostings(node, out: list) -> list:
    if isinstance(node, dict):
        t = node.get("@type")
        if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
            out.append(node)
        for value in node.get("@graph", []) if isinstance(node.get("@graph"), list) else []:
            _all_jobpostings(value, out)
    elif isinstance(node, list):
        for value in node:
            _all_jobpostings(value, out)
    return out


def _parse_schema_org(portal: dict, body: str, page_url: str) -> list[dict]:
    """Every JobPosting JSON-LD node on the page, parsed by the shared _generic parser."""
    from bs4 import BeautifulSoup

    from ._generic import parse_jsonld_jobposting

    soup = BeautifulSoup(body, "lxml")
    nodes: list = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            _all_jobpostings(json.loads(script.string or script.get_text() or ""), nodes)
        except ValueError:
            continue
    jobs = []
    for node in nodes[:MAX_ITEMS]:
        url = node.get("url") if isinstance(node.get("url"), str) else page_url
        # Hand each node to the shared parser as a one-posting page.
        snippet = json.dumps(node).replace("</", "<\\/")
        parsed = parse_jsonld_jobposting(
            f'<script type="application/ld+json">{snippet}</script>', url)
        parsed.setdefault("tags", [])
        values = {
            "title": parsed.get("title"), "company": parsed.get("company"),
            "location": parsed.get("location"), "url": parsed.get("url"),
            "description": parsed.get("description"), "posted_at": parsed.get("date_posted"),
            "salary_min": parsed.get("salary_min"), "salary_max": parsed.get("salary_max"),
            "salary_period": parsed.get("salary_period"), "tags": parsed["tags"],
            "remote": parsed.get("is_remote", False),
            "external_id": node.get("identifier", {}).get("value")
            if isinstance(node.get("identifier"), dict) else None,
        }
        job = _build_job(portal, values)
        if job:
            jobs.append(job)
    return jobs


def _state_path() -> Path:
    return project_root() / "data" / "portal_state.json"


def _read_state() -> dict:
    try:
        data = json.loads(_state_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(portal_id: str) -> None:
    state = _read_state()
    state[portal_id] = time.time()
    try:
        _state_path().parent.mkdir(parents=True, exist_ok=True)
        _state_path().write_text(json.dumps(state, sort_keys=True))
    except OSError:
        logger.debug("Could not persist portal state", exc_info=True)


def is_enabled(config: dict, portal_id: str) -> bool:
    entry = ((config.get("search") or {}).get("portals") or {}).get(portal_id)
    return bool(entry.get("enabled", False)) if isinstance(entry, dict) else False


async def fetch_portal(portal: dict, config: dict) -> list[dict]:
    """Run one portal: one request per keyword (up to MAX_QUERIES) when the URL
    has {query}, otherwise a single request filtered locally by keyword."""
    from . import compile_exclude_patterns, matches_exclude, matches_keywords

    search = config.get("search") or {}
    keywords = [k for k in (search.get("keywords") or []) if isinstance(k, str) and k.strip()]
    if not keywords:
        return []
    req = portal["request"]
    template = req["url"]
    uses_query = "query" in _placeholders(template, "request.url")
    queries = keywords[:min(req.get("max_queries", MAX_QUERIES), MAX_QUERIES)] if uses_query else [""]
    pages = min(req.get("max_pages", 1), MAX_PAGES) if "{page}" in template else 1
    delay = max(float(req.get("delay_seconds", MIN_DELAY)), MIN_DELAY)

    interval = portal.get("min_interval_minutes", 0) * 60
    last = _read_state().get(portal["id"])
    if interval and isinstance(last, (int, float)) and time.time() - last < interval:
        logger.info("Portal %s: skipped, its terms ask for at most one fetch per %d min",
                    portal["id"], portal["min_interval_minutes"])
        return []

    api_key = ""
    keys = config.get("api_keys") or {}
    if isinstance(keys.get(f"{portal['id']}_api_key"), str):
        from . import real_key
        api_key = real_key(keys[f"{portal['id']}_api_key"])
    headers = dict(_HEADERS)
    for name, value in (req.get("headers") or {}).items():
        if "{api_key}" in value and not api_key:
            continue  # optional credential not configured: send without it
        headers[name] = _fill(value, {"api_key": api_key}) if "{api_key}" in value else value
    location = next((l for l in (search.get("locations") or [])
                     if isinstance(l, str) and l.strip() and l.strip().lower() != "remote"), "")

    jobs: list[dict] = []
    seen: set[str] = set()
    started = time.monotonic()
    first = True
    _write_state(portal["id"])
    try:
        async with async_client() as client:
            for query in queries:
                for page in range(1, pages + 1):
                    if time.monotonic() - started > RUN_BUDGET:
                        raise PortalError("run time budget exceeded")
                    if not first:
                        await asyncio.sleep(delay)
                    first = False
                    url = _fill(template, {"query": query, "location": location, "page": str(page)})
                    resp = await _get(client, portal, url, headers)
                    if resp.status_code != 200:
                        logger.warning("Portal %s returned HTTP %d", portal["id"], resp.status_code)
                        break
                    if portal["kind"] == "json_api":
                        batch = _parse_json_api(portal, resp.text)
                    elif portal["kind"] == "feed":
                        batch = _parse_feed(portal, resp.text)
                    else:
                        batch = _parse_schema_org(portal, resp.text, url)
                    fresh = [j for j in batch if j["external_id"] not in seen]
                    seen.update(j["external_id"] for j in fresh)
                    jobs.extend(fresh)
                    if not batch:
                        break
    except (PortalError, httpx.HTTPError, asyncio.TimeoutError, ValueError, ET.ParseError) as exc:
        # Keep what earlier requests returned; the portal just ends early.
        logger.warning("Portal %s stopped early: %s", portal["id"], exc)

    exclude = compile_exclude_patterns(search.get("exclude_keywords", []))
    kept = []
    for job in jobs:
        if matches_exclude(job["title"], exclude):
            continue
        if not uses_query and not matches_keywords(
                f"{job['title']} {job['company']} {' '.join(job['tags'])} {job['description']}", keywords):
            continue
        kept.append(job)
    logger.info("Portal %s: %d jobs matched filters", portal["id"], len(kept))
    return kept


class PortalSource:
    """Adapter giving a portal the same fetch_jobs(config) surface as a source module."""

    def __init__(self, portal: dict):
        self.portal = portal

    async def fetch_jobs(self, config: dict) -> list[dict]:
        return await fetch_portal(self.portal, config)


def portal_sources(reserved: frozenset | set = frozenset()) -> list[tuple[str, PortalSource]]:
    return [(pid, PortalSource(p)) for pid, p in load_portals(reserved=reserved).items()]
