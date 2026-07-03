"""
job_sources — Aggregates job listings from multiple sources.

Each source module exposes an async fetch_jobs(config) -> list[dict] function
that returns normalized job dictionaries.
"""

import asyncio
import html
import inspect
import json
import logging
import random
import re
import urllib.parse
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class SourceBlockedError(RuntimeError):
    """Raised by a source when the site is bot-blocking us and no results
    are obtainable. fetch_all_jobs reports these sources separately so the
    UI can distinguish 'blocked' from 'genuinely zero new jobs'."""


async def fetch_with_retries(
    session: aiohttp.ClientSession,
    url: str,
    *,
    retries: int = 2,
    backoff_base: float = 1.0,
    **kwargs,
) -> tuple[int, str]:
    """GET *url* and return (status, body_text), retrying transient failures.

    Retries timeouts, connection errors, 5xx, and 429 responses with jittered
    exponential backoff (429 honors Retry-After and backs off harder) so a
    single network blip or rate-limit burst doesn't cost a whole board or
    keyword for the run. Other statuses are returned as-is — semantics like
    403 = blocked stay with the caller. Raises the last exception when all
    attempts fail.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with session.get(url, **kwargs) as resp:
                body = await resp.text()
                if resp.status == 429 and attempt < retries:
                    try:
                        retry_after = float(resp.headers.get("Retry-After", 0))
                    except ValueError:
                        retry_after = 0.0
                    wait = max(retry_after, 2.0 * backoff_base * (2 ** attempt)) + random.uniform(0, 0.5)
                    logger.debug("GET %s returned 429 — retrying in %.1fs", url, wait)
                    await asyncio.sleep(wait)
                    continue
                if resp.status >= 500 and attempt < retries:
                    logger.debug("GET %s returned %d — retrying", url, resp.status)
                    await asyncio.sleep(backoff_base * (2 ** attempt) + random.uniform(0, 0.5))
                    continue
                return resp.status, body
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_exc = exc
            if attempt < retries:
                logger.debug("GET %s failed (%s) — retrying", url, exc)
                await asyncio.sleep(backoff_base * (2 ** attempt) + random.uniform(0, 0.5))
    assert last_exc is not None
    raise last_exc


# ----- Silent-breakage detection -----
# A scraper whose DOM selectors went stale returns 0 jobs and looks exactly
# like "no new postings today". Track consecutive zero-job runs per source
# (only for sources that have returned jobs before) and flag the streak.

_SOURCE_STATS_PATH = Path(__file__).resolve().parents[2] / "data" / "source_stats.json"
_ZERO_STREAK_THRESHOLD = 3


def _record_source_result(name: str, count: int) -> int:
    """Persist a source's run outcome and return its consecutive zero-job
    streak. Sources that have never returned jobs report a streak of 0 —
    an unconfigured source is not a broken one."""
    try:
        stats = json.loads(_SOURCE_STATS_PATH.read_text()) if _SOURCE_STATS_PATH.exists() else {}
        if not isinstance(stats, dict):
            stats = {}
    except Exception:
        stats = {}
    prev = stats.get(name) or {}
    if count > 0:
        entry = {"zero_streak": 0, "ever_nonzero": True}
    else:
        entry = {
            "zero_streak": int(prev.get("zero_streak", 0)) + 1,
            "ever_nonzero": bool(prev.get("ever_nonzero", False)),
        }
    stats[name] = entry
    try:
        _SOURCE_STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SOURCE_STATS_PATH.write_text(json.dumps(stats, indent=2, sort_keys=True))
    except Exception:
        logger.debug("Could not persist source stats", exc_info=True)
    return entry["zero_streak"] if entry["ever_nonzero"] else 0


_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WS_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_PERCENT_ENC_RE = re.compile(r"%[0-9A-Fa-f]{2}")


def clean_description(text: str | None) -> str:
    """
    Shared description sanitizer used across all job sources.

    Steps: HTML-entity decode -> normalize <br> to newlines -> strip remaining
    tags -> percent-decode if the string still looks URL-encoded -> collapse
    whitespace. Idempotent and safe on already-clean text.
    """
    if not text:
        return ""
    s = html.unescape(str(text))
    s = _BR_RE.sub("\n", s)
    s = _TAG_RE.sub(" ", s)
    # Some sources (esp. RSS feeds) double-encode entities; one more pass.
    if "&" in s and ";" in s:
        s = html.unescape(s)
    # Only unquote if the string genuinely looks URL-encoded — avoid mangling
    # legitimate "%" characters in salary or stat phrasing.
    if _PERCENT_ENC_RE.search(s):
        try:
            decoded = urllib.parse.unquote(s)
            # Reject if decode introduced control chars (likely false positive).
            if not any(ord(c) < 9 for c in decoded):
                s = decoded
        except Exception:
            pass
    s = _INLINE_WS_RE.sub(" ", s)
    s = _BLANK_LINES_RE.sub("\n\n", s)
    return s.strip()


# ----- Pay period detection -----
# Used everywhere salary text appears so we can tag postings as hourly vs annual
# without losing the raw rate. Filter/sort code normalizes hourly -> annual via
# ANNUAL_HOURS only at comparison time; storage stays raw.

ANNUAL_HOURS = 2080  # 40h/wk * 52wk

_HOURLY_RE = re.compile(
    r"(?:/\s*(?:hr|hour)\b|\bper\s+hour\b|\bhourly\b|\ban?\s+hour\b)",
    re.IGNORECASE,
)
_ANNUAL_RE = re.compile(
    r"(?:/\s*(?:yr|year|annum)\b|\bper\s+(?:year|annum)\b|\b(?:yearly|annually)\b)",
    re.IGNORECASE,
)


def detect_pay_period(text: str | None) -> str:
    """Return 'hourly' | 'annual' | 'unknown' from free-text salary phrasing."""
    if not text:
        return "unknown"
    if _HOURLY_RE.search(text):
        return "hourly"
    if _ANNUAL_RE.search(text):
        return "annual"
    return "unknown"


def infer_period_from_amount(amount: int | None) -> str:
    """
    Heuristic fallback when no period text is available. A bare numeric value
    of 25 is almost certainly hourly; 80000 is annual. Cutoff at 1000 keeps
    the false-positive rate low for the messy middle range.
    """
    if amount is None:
        return "unknown"
    if amount < 1000:
        return "hourly"
    return "annual"


def normalize_salary_to_annual(amount: int | None, period: str) -> int | None:
    """Convert a raw salary value to its annual equivalent for comparison."""
    if amount is None:
        return None
    if period == "hourly":
        return int(amount * ANNUAL_HOURS)
    return int(amount)


# ----- Shared keyword / exclude matching -----
# All sources route their filtering through these so behavior is uniform.
# Exclude keywords match on *word boundaries*, not substrings — a plain
# substring check made config entries like "SC" or "TS" (clearance acronyms)
# silently drop every job whose title or company merely contained those two
# letters ("Cisco", "Consultants", "Scientist", ...).


@lru_cache(maxsize=64)
def _compile_excludes(exclude: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    patterns = []
    for kw in exclude:
        kw = kw.strip()
        if not kw:
            continue
        # (?<!\w) / (?!\w) instead of \b so keywords that start or end with
        # non-word chars ("TS/SCI", "C++") still anchor correctly.
        patterns.append(re.compile(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", re.IGNORECASE))
    return tuple(patterns)


def compile_exclude_patterns(exclude: list[str] | tuple[str, ...] | None) -> tuple[re.Pattern, ...]:
    """Compile exclude keywords to word-boundary regexes (cached)."""
    return _compile_excludes(tuple(exclude or ()))


def matches_exclude(text: str, patterns: tuple[re.Pattern, ...]) -> bool:
    """True if any compiled exclude pattern matches *text*."""
    if not text:
        return False
    return any(p.search(text) for p in patterns)


def matches_keywords(text: str, keywords: list[str]) -> bool:
    """True if *text* matches any configured search keyword.

    A keyword matches when it appears as a whole phrase, or — for multi-word
    keywords — when every token appears somewhere in the text. The token
    fallback lets "cybersecurity analyst" match "Cyber Security Analyst II"
    style title variants that a strict phrase-substring check missed.
    """
    if not text:
        return False
    t = text.lower()
    for kw in keywords:
        k = kw.lower().strip()
        if not k:
            continue
        if k in t:
            return True
        tokens = k.split()
        if len(tokens) > 1 and all(tok in t for tok in tokens):
            return True
    return False


def parse_posted_date(value) -> datetime | None:
    """Parse the heterogeneous date_posted formats sources emit.

    Handles unix epochs (Arbeitnow), ISO 8601 with or without Z (LinkedIn,
    Adzuna, Greenhouse, USAJobs, RemoteOK), and RFC 822 (WeWorkRemotely RSS).
    Returns a timezone-aware UTC datetime, or None when unparseable (e.g.
    Indeed's "Posted 3 days ago" relative strings) — callers must treat None
    as "unknown, let it through".
    """
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    s = str(value).strip()
    if re.fullmatch(r"\d{9,}", s):
        try:
            return datetime.fromtimestamp(float(s), tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    dt = None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(s)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# Imported after helpers are defined so source modules can pull them in.
from . import remoteok, weworkremotely, adzuna, greenhouse, linkedin, arbeitnow, usajobs, indeed, ashby, workable, recruitee  # noqa: E402

# All source modules with human-readable labels
SOURCES = [
    ("remoteok", remoteok),
    ("weworkremotely", weworkremotely),
    ("adzuna", adzuna),
    ("greenhouse", greenhouse),
    ("linkedin", linkedin),
    ("arbeitnow", arbeitnow),
    ("usajobs", usajobs),
    ("indeed", indeed),
    ("ashby", ashby),
    ("workable", workable),
    ("recruitee", recruitee),
]


def get_source_names() -> list[str]:
    """Return list of all available source names."""
    return [name for name, _ in SOURCES]


def _passes_global_filters(job: dict, config: dict) -> bool:
    """
    Final safety-net filter applied to ALL jobs from ALL sources.
    Catches anything that slipped through individual source filters.
    """
    search = config.get("search", {})
    locations = search.get("locations", [])
    exclude_patterns = compile_exclude_patterns(search.get("exclude_keywords", []))

    title = job.get("title", "").lower()
    job_location = job.get("location", "").lower()

    # Exclude keywords — word-boundary match on title and company
    if matches_exclude(job.get("title", ""), exclude_patterns) or \
            matches_exclude(job.get("company", ""), exclude_patterns):
        logger.debug("Global filter: excluded '%s' at '%s' — matched an exclude keyword",
                     job.get("title"), job.get("company"))
        return False

    # Max-age filter — only when date_posted is parseable; sources that emit
    # relative strings ("Posted 3 days ago") pass through unchecked.
    max_age_days = search.get("max_age_days")
    if max_age_days:
        posted = parse_posted_date(job.get("date_posted"))
        if posted is not None:
            age_days = (datetime.now(timezone.utc) - posted).total_seconds() / 86400
            # +1 day grace for timezone/rounding differences across sources
            if age_days > float(max_age_days) + 1:
                logger.debug("Global filter: excluded '%s' — posted %.0f days ago (max %s)",
                             job.get("title"), age_days, max_age_days)
                return False

    # Min-salary filter — lenient: uses the job's upper bound and only drops
    # jobs that *state* a salary below the floor. No salary data = pass.
    min_salary = search.get("min_salary")
    if min_salary:
        amount = job.get("salary_max") or job.get("salary_min")
        if amount:
            period = job.get("salary_period") or "unknown"
            if period == "unknown":
                period = infer_period_from_amount(amount)
            annual = normalize_salary_to_annual(amount, period)
            if annual is not None and annual < int(min_salary):
                logger.debug("Global filter: excluded '%s' — salary %s/yr below floor %s",
                             job.get("title"), annual, min_salary)
                return False

    # Location filter — only if locations are configured
    if locations:
        # LinkedIn cards sometimes omit location; the detail fetch may backfill
        # it, but if it's still empty let it through — LinkedIn's own filter
        # already approved it.
        if not job_location and job.get("source") == "linkedin":
            return True

        is_remote = job.get("is_remote", False) or "remote" in job_location or "remote" in title
        has_remote_config = any(loc.strip().lower() == "remote" for loc in locations)

        # Remote jobs pass if "Remote" is in configured locations
        if is_remote and has_remote_config:
            return True

        # Check if job location matches any configured location
        for loc in locations:
            loc_clean = loc.strip().lower()
            if not loc_clean or loc_clean == "remote":
                continue
            if loc_clean in job_location:
                return True

        # RemoteOK and WeWorkRemotely are inherently remote — always pass
        if job.get("source") in ("remoteok", "weworkremotely"):
            return True

        # No location match
        logger.debug("Global filter: excluded '%s' — location '%s' doesn't match %s",
                     job.get("title"), job.get("location"), locations)
        return False

    return True


# Per-source timeouts (seconds).
# LinkedIn is slow: sequential detail fetches at 2s/job + 1.5s search delays.
# With 9 keywords × 4 locations × 3 pages that's 150s+ before detail fetches.
_SOURCE_TIMEOUTS: dict[str, int] = {
    "linkedin": 600,
    # Indeed: ~22s Byparr primer + N (kw × loc) Playwright navs + per-card
    # /viewjob enrichment via Byparr (semaphore=4). At full 11 kw × 3 loc ×
    # max_pages this is several minutes.
    "indeed": 600,
    "adzuna": 120,
    "greenhouse": 300,
    "remoteok": 60,
    "weworkremotely": 60,
    "arbeitnow": 60,
    "usajobs": 60,
    "ashby": 120,
    "workable": 120,
    "recruitee": 120,
}
_SOURCE_TIMEOUT = 60  # fallback for any unlisted source

# Sources whose fetch_jobs accepts known_ids map to the DB `source` values
# their jobs are stored under (the greenhouse module emits both greenhouse
# and lever records).
_KNOWN_ID_SOURCES: dict[str, tuple[str, ...]] = {
    "greenhouse": ("greenhouse", "lever"),
    "linkedin": ("linkedin",),
    "indeed": ("indeed",),
}


async def _load_known_ids(source_name: str) -> set[str]:
    """External IDs already in the DB for a source, so scrapers can skip
    re-enriching jobs we have. Returns an empty set on any failure — skipping
    the optimization must never block a fetch."""
    ids: set[str] = set()
    try:
        from backend.database import get_known_external_ids
        for db_source in _KNOWN_ID_SOURCES.get(source_name, (source_name,)):
            ids |= await get_known_external_ids(db_source)
    except Exception:
        logger.debug("Could not load known external IDs for %s", source_name, exc_info=True)
    return ids


def _identity_key(job: dict) -> tuple[str, str, str] | None:
    """Normalized (title, company, location) for cross-source dedup.
    Returns None when title or company is missing — we never merge on
    incomplete identity."""
    def norm(s: str) -> str:
        return re.sub(r"\W+", " ", (s or "").lower()).strip()
    t, c = norm(job.get("title", "")), norm(job.get("company", ""))
    if not t or not c:
        return None
    return (t, c, norm(job.get("location", "")))


async def fetch_all_jobs(
    config: dict,
    sources: list[str] | None = None,
    on_progress=None,
    cancel_event=None,
    _partial_collector: list | None = None,
) -> list[dict]:
    """
    Run all job sources concurrently, each under its own timeout, and report
    progress as they complete. Running concurrently means the slow browser
    sources (LinkedIn, Indeed) no longer starve whoever runs after them, and
    the caller's overall budget only needs to cover the slowest single source.

    If `sources` is provided, only fetch from those sources.
    A single source failing, timing out, or being bot-blocked doesn't affect
    the others; affected source names are reported via on_progress as
    sources_timed_out / sources_blocked / sources_failed.
    Deduplicates by URL, then by normalized (title, company, location), and
    applies global filters before returning.
    If cancel_event is set, pending sources are cancelled and whatever has
    completed so far is returned.
    If _partial_collector is provided, it is extended in-place as each
    source completes so callers can recover partial results on timeout.
    """
    logger.info(f"fetch_all_jobs called with sources={sources} config={config}")
    if sources:
        # Caller explicitly selected sources — respect that list as-is
        requested = {s.lower() for s in sources}
        active_sources = [(n, m) for n, m in SOURCES if n in requested]
        logger.info("Fetching from selected sources: %s", [n for n, _ in active_sources])
    else:
        # Default: all sources, but Indeed is opt-in via config
        indeed_enabled = config.get("search", {}).get("indeed", {}).get("enabled", False)
        active_sources = [(n, m) for n, m in SOURCES if n != "indeed" or indeed_enabled]

    total = len(active_sources)
    if on_progress:
        names = ", ".join(n for n, _ in active_sources)
        on_progress(sources_total=total, sources_done=0,
                    detail=f"Fetching from {total} sources in parallel ({names})...")

    async def _run_source(name, mod):
        source_timeout = _SOURCE_TIMEOUTS.get(name, _SOURCE_TIMEOUT)
        kwargs = {}
        try:
            if "known_ids" in inspect.signature(mod.fetch_jobs).parameters:
                kwargs["known_ids"] = await _load_known_ids(name)
        except (TypeError, ValueError):
            pass
        return await asyncio.wait_for(mod.fetch_jobs(config, **kwargs), timeout=source_timeout)

    task_names: dict[asyncio.Task, str] = {}
    for name, mod in active_sources:
        task = asyncio.create_task(_run_source(name, mod), name=f"fetch-{name}")
        task_names[task] = name

    all_jobs: list[dict] = []
    blocked: list[str] = []
    timed_out: list[str] = []
    failed: list[str] = []
    suspect: list[str] = []
    done_count = 0
    pending: set[asyncio.Task] = set(task_names)

    while pending:
        if cancel_event and cancel_event.is_set():
            logger.info("Fetch cancelled by user with %d/%d sources still running",
                        len(pending), total)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            break
        # Wake every second so cancellation is responsive
        done, pending = await asyncio.wait(pending, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            continue
        for t in done:
            name = task_names[t]
            done_count += 1
            try:
                jobs = t.result()
                logger.info("Source %s returned %d jobs", name, len(jobs))
                streak = _record_source_result(name, len(jobs))
                if streak >= _ZERO_STREAK_THRESHOLD:
                    suspect.append(name)
                    logger.warning(
                        "Job source '%s' has returned 0 jobs for %d consecutive runs — "
                        "its parser or API may have silently broken", name, streak,
                    )
                all_jobs.extend(jobs)
                if _partial_collector is not None:
                    _partial_collector.extend(jobs)
            except asyncio.TimeoutError:
                timed_out.append(name)
                logger.warning("Job source '%s' timed out after %ds",
                               name, _SOURCE_TIMEOUTS.get(name, _SOURCE_TIMEOUT))
            except SourceBlockedError as exc:
                blocked.append(name)
                logger.warning("Job source '%s' is bot-blocked: %s", name, exc)
            except asyncio.CancelledError:
                failed.append(name)
            except Exception as exc:
                failed.append(name)
                logger.warning("Job source '%s' failed: %s", name, exc)
        if on_progress:
            remaining = sorted(task_names[t] for t in pending)
            detail = (f"Waiting on {', '.join(remaining)}... ({done_count}/{total} done)"
                      if remaining else f"All {total} sources finished")
            progress_kwargs: dict = dict(detail=detail, sources_done=done_count, jobs_found=len(all_jobs))
            if blocked:
                progress_kwargs["sources_blocked"] = list(blocked)
            if timed_out:
                progress_kwargs["sources_timed_out"] = list(timed_out)
            if failed:
                progress_kwargs["sources_failed"] = list(failed)
            if suspect:
                progress_kwargs["sources_suspect"] = list(suspect)
            on_progress(**progress_kwargs)

    # Deduplicate by URL
    seen_urls: set[str] = set()
    combined: list[dict] = []
    for job in all_jobs:
        url = job.get("url", "")
        if url and url in seen_urls:
            continue
        seen_urls.add(url)
        combined.append(job)

    # Cross-source dedup: the same posting often appears on LinkedIn, Indeed,
    # and Adzuna with different URLs. Location is part of the key so multi-
    # office postings of the same role survive.
    seen_keys: set[tuple] = set()
    unique: list[dict] = []
    for job in combined:
        key = _identity_key(job)
        if key is not None and key in seen_keys:
            continue
        if key is not None:
            seen_keys.add(key)
        unique.append(job)
    if len(unique) < len(combined):
        logger.info("Cross-source dedup removed %d duplicate postings", len(combined) - len(unique))

    # Apply global filters as a safety net
    before_count = len(unique)
    filtered = [job for job in unique if _passes_global_filters(job, config)]
    removed = before_count - len(filtered)
    if removed > 0:
        logger.info("Global filter removed %d jobs that didn't match filter criteria", removed)

    logger.info("Total unique jobs after filtering: %d", len(filtered))
    return filtered
