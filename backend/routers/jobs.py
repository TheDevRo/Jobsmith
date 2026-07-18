"""
routers/jobs.py — Job listing/CRUD, fetch orchestration, description refetch,
manual URL ingest, screenshots, and per-job logs.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import app_state as state
from .. import background_tasks as bg
from .. import database as db
from .. import salary_estimator

logger = logging.getLogger(__name__)

router = APIRouter()


class StatusUpdate(BaseModel):
    status: str


@router.get("/api/jobs")
async def list_jobs(
    status: Optional[str] = None,
    source: Optional[str] = None,
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
    unscored_only: bool = False,
    search: Optional[str] = None,
    location: Optional[str] = None,
    company: Optional[str] = None,
    remote_only: bool = False,
    easy_apply_only: bool = False,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    min_salary: Optional[int] = None,
    include_estimated: bool = False,
    pay_floor: Optional[int] = None,
    require_stated_pay: bool = False,
    sort_by: str = "date_discovered",
    sort_dir: str = "desc",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return await db.get_jobs(
        status=status, source=source, min_score=min_score, max_score=max_score,
        unscored_only=unscored_only, search=search,
        location=location, company=company, remote_only=remote_only,
        easy_apply_only=easy_apply_only,
        date_from=date_from, date_to=date_to, min_salary=min_salary,
        include_estimated=include_estimated,
        pay_floor=pay_floor, require_stated_pay=require_stated_pay,
        sort_by=sort_by, sort_dir=sort_dir, limit=limit, offset=offset,
    )


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.get("/api/jobs/{job_id}/screenshots")
async def list_job_screenshots(job_id: str):
    """Return list of screenshot filenames for a given job."""
    if not state.SCREENSHOTS_DIR.exists():
        return {"screenshots": []}
    files = sorted(
        f.name for f in state.SCREENSHOTS_DIR.glob(f"{job_id}_*.png")
    )
    if not files:
        logger.warning("list_job_screenshots: no screenshots found for job_id=%s in %s", job_id, state.SCREENSHOTS_DIR)
    return {"screenshots": files}


@router.get("/api/screenshots/{filename}")
async def get_screenshot(filename: str):
    """Serve a screenshot image."""
    # Sanitize filename to prevent path traversal
    safe = Path(filename).name
    path = state.SCREENSHOTS_DIR / safe
    if not path.exists() or not path.suffix == ".png":
        raise HTTPException(404, "Screenshot not found")
    return FileResponse(str(path), media_type="image/png")


@router.post("/api/jobs/{job_id}/estimate-salary")
async def estimate_job_salary(job_id: str):
    """Re-run the external salary estimator for a single job, on demand.

    Returns 200 in all non-error cases — including when no external source
    has data for this title/location. The response `status` field tells the
    client whether to render an estimate or surface a "no data" message.
    """
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    cfg = state.load_config()
    try:
        payload = await salary_estimator.estimate_salary(job, cfg)
    except salary_estimator.QuotaExceeded as e:
        return {
            "status": "quota_exceeded",
            "message": (
                "External API quota reached for today. "
                "Try again tomorrow when it resets."
            ),
            "detail": str(e),
        }
    except salary_estimator.ResourceExhausted as e:
        return {
            "status": "resource_exhausted",
            "message": (
                "Server is out of file descriptors. Please restart the "
                "server process to recover."
            ),
            "detail": str(e),
        }
    if not payload:
        return {
            "status": "no_data",
            "message": (
                "No salary data is available for this title/location combination. "
                "Adzuna and BLS both returned 0 matches — the title may be too "
                "specific, or the location string may not resolve to a known market."
            ),
        }
    await db.update_job_estimated_salary(job_id, payload)
    return {"status": "ok", "estimate": payload}


@router.get("/api/jobs/{job_id}/embellishment-log")
async def get_embellishment_log_endpoint(job_id: str):
    """Return the embellishment log stored on a job record."""
    log = await db.get_embellishment_log(job_id)
    return {"embellishment_log": log}


@router.get("/api/jobs/{job_id}/apply-log")
async def get_apply_log(job_id: str):
    """Return the step-by-step apply log for a job, if one exists."""
    log_path = state.SCREENSHOTS_DIR / f"{job_id}_apply_log.json"
    if not log_path.exists():
        return {"log": None}
    import json as _json
    with open(log_path) as f:
        return {"log": _json.load(f)}


@router.get("/api/jobs/{job_id}/apply-log-v2")
async def get_apply_log_v2(job_id: str):
    """Return JSONL apply-log entries for a specific job_id."""
    entries: list[dict] = []
    import json as _json
    try:
        if state.JSONL_LOG_PATH.exists():
            with open(state.JSONL_LOG_PATH, encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = _json.loads(raw)
                    except Exception:
                        continue
                    if obj.get("job_id") != job_id:
                        continue
                    entries.append({k: obj.get(k) for k in state.APPLY_LOG_V2_FIELDS})
    except Exception:
        logger.exception("apply-log-v2: failed reading %s", state.JSONL_LOG_PATH)
    return {"job_id": job_id, "entries": entries}


class DeleteJobsRequest(BaseModel):
    job_ids: list[str] | None = None   # Specific IDs to delete
    all: bool = False                   # Delete all jobs
    status: str | None = None           # Delete by status filter
    source: str | None = None           # Delete by source filter


@router.post("/api/jobs/delete")
async def delete_jobs(body: DeleteJobsRequest):
    """Delete jobs by IDs, filters, or all."""
    if body.all:
        count = await db.delete_all_jobs()
        await db.log_activity("delete", f"Deleted all {count} jobs")
        return {"deleted": count, "message": f"Deleted all {count} jobs"}
    elif body.job_ids:
        count = await db.delete_jobs(body.job_ids)
        await db.log_activity("delete", f"Deleted {count} selected jobs")
        return {"deleted": count, "message": f"Deleted {count} jobs"}
    elif body.status or body.source:
        count = await db.delete_jobs_filtered(status=body.status, source=body.source)
        await db.log_activity("delete", f"Deleted {count} jobs by filter")
        return {"deleted": count, "message": f"Deleted {count} jobs"}
    else:
        raise HTTPException(400, "Specify job_ids, filters, or all=true")


@router.delete("/api/jobs/{job_id}")
async def delete_single_job(job_id: str):
    """Delete a single job by ID."""
    count = await db.delete_jobs([job_id])
    if count == 0:
        raise HTTPException(404, "Job not found")
    await db.log_activity("delete", f"Deleted job {job_id}")
    return {"deleted": 1, "message": "Job deleted"}


class FetchJobsRequest(BaseModel):
    sources: list[str] | None = None  # None = all sources


@router.post("/api/jobs/fetch", status_code=202)
async def fetch_jobs(body: Optional[FetchJobsRequest] = None):
    if state.task_running("fetch"):
        raise HTTPException(409, "A job fetch is already running")
    sources = body.sources if body and body.sources else None
    task = asyncio.create_task(bg._bg_fetch_jobs(sources))
    state.running_tasks["fetch"] = task
    label = ", ".join(sources) if sources else "all sources"
    return {"message": f"Job fetch started from {label}"}


class IngestUrlRequest(BaseModel):
    url: str


@router.post("/api/jobs/ingest-url")
async def ingest_url(body: IngestUrlRequest):
    """Ingest a single job from a user-supplied URL into the jobs table."""
    from ..job_sources import manual as _manual

    try:
        job = await _manual.fetch_job_from_url(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("ingest-url failed for %s", body.url)
        raise HTTPException(502, f"Failed to fetch URL: {e}")

    existing = await db.get_job_by_source_external(job["source"], job["external_id"])
    if existing and not (existing.get("title") or "").strip():
        # Prior manual ingest landed with empty title (e.g. parser didn't exist yet).
        # Refill title/company/location/description directly so the user doesn't
        # have to delete the row.
        await db.refill_manual_job(existing["id"], job)
        return {
            "status": "refilled",
            "job_id": existing["id"],
            "title": job.get("title"),
            "company": job.get("company"),
        }

    job_id = await db.upsert_job(job)
    if job_id is None:
        if existing:
            return {
                "status": "exists",
                "job_id": existing["id"],
                "title": existing.get("title"),
                "company": existing.get("company"),
            }
        raise HTTPException(500, "upsert returned None but job not found")

    # Optional: auto-estimate salary at ingest time for jobs without disclosed comp.
    cfg = state.load_config()
    sal_cfg = cfg.get("salary_estimator", {}) or {}
    if (
        sal_cfg.get("enabled", True)
        and sal_cfg.get("auto_on_ingest", True)
        and not (job.get("salary_min") or job.get("salary_max"))
    ):
        ingest_job = {**job, "id": job_id}
        asyncio.create_task(bg._maybe_estimate_salary(ingest_job, cfg))

    await bg._refresh_job_quality(job_id, job)

    await db.log_activity("job_added", f"Added '{job.get('title')}' at {job.get('company') or 'unknown'} via URL")
    return {
        "status": "added",
        "job_id": job_id,
        "title": job.get("title"),
        "company": job.get("company"),
    }


@router.post("/api/jobs/fetch/cancel")
async def cancel_fetch():
    state.cancel_fetch.set()
    task = state.running_tasks.get("fetch")
    if task and not task.done():
        task.cancel()
    return {"message": "Fetch cancel requested"}


@router.post("/api/jobs/fetch/finish")
async def finish_fetch():
    """Stop kicking off new sources, but let the task save what it has so far."""
    state.fetch_keep_partial = True
    state.cancel_fetch.set()
    return {"message": "Finishing fetch with partial results"}


@router.get("/api/jobs/fetch/status")
async def fetch_jobs_status():
    return state.fetch_status


@router.get("/api/operations/status")
async def operations_status():
    """Return running state of all background operations."""
    def _is_active(key: str) -> bool:
        task = state.running_tasks.get(key)
        return task is not None and not task.done()
    return {
        "fetch": state.fetch_status.get("active", False),
        "score_batch": _is_active("score_batch"),
        "tailor_batch": _is_active("tailor_batch"),
        "apply": _is_active("apply"),
        "detect_apply_types": _is_active("detect_apply_types"),
        "estimate_salaries": _is_active("estimate_salaries"),
    }


@router.get("/api/sources")
async def list_sources():
    """Return available job source names."""
    from ..job_sources import get_source_names
    return {"sources": get_source_names()}


# ---------------------------------------------------------------------------
# ATS board detection — type a company name, find its board slug(s)
# ---------------------------------------------------------------------------

class DetectBoardsRequest(BaseModel):
    company: str


# Public, unauthenticated board APIs. probe(slug) -> (url, extract_job_count)
_ATS_PROBES = {
    "greenhouse": {
        "url": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "count": lambda d: len(d.get("jobs", [])) if isinstance(d, dict) else None,
        "board_url": "https://boards.greenhouse.io/{slug}",
        "config_key": "greenhouse_boards",
    },
    "lever": {
        "url": "https://api.lever.co/v0/postings/{slug}?mode=json",
        "count": lambda d: len(d) if isinstance(d, list) else None,
        "board_url": "https://jobs.lever.co/{slug}",
        "config_key": "lever_companies",
    },
    "ashby": {
        "url": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "count": lambda d: len(d.get("jobs", [])) if isinstance(d, dict) else None,
        "board_url": "https://jobs.ashbyhq.com/{slug}",
        "config_key": "ashby_boards",
    },
    "workable": {
        "url": "https://apply.workable.com/api/v1/widget/accounts/{slug}",
        "count": lambda d: len(d.get("jobs", [])) if isinstance(d, dict) else None,
        "name": lambda d: d.get("name"),
        "board_url": "https://apply.workable.com/{slug}",
        "config_key": "workable_accounts",
    },
    "recruitee": {
        "url": "https://{slug}.recruitee.com/api/offers/",
        "count": lambda d: len(d.get("offers", [])) if isinstance(d, dict) else None,
        "name": lambda d: (d.get("offers") or [{}])[0].get("company_name"),
        "board_url": "https://{slug}.recruitee.com",
        "config_key": "recruitee_companies",
    },
}


def _slug_candidates(company: str) -> list[str]:
    """Turn a company name into likely board slugs: 'Notion Labs' →
    ['notionlabs', 'notion-labs', 'notion']."""
    import re

    base = re.sub(r"[^a-z0-9 ]", "", company.lower()).strip()
    if not base:
        return []
    words = base.split()
    candidates = ["".join(words), "-".join(words)]
    # Common legal/branding suffixes rarely appear in slugs.
    if len(words) > 1 and words[-1] in {"inc", "labs", "hq", "io", "co", "ai", "gmbh", "ltd", "llc"}:
        candidates.append("".join(words[:-1]))
        candidates.append("-".join(words[:-1]))
    seen, out = set(), []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


async def _probe_ats(source: str, spec: dict, slug: str, session) -> Optional[dict]:
    """One (ATS, slug) probe: 200 + parseable board payload → match dict."""
    import json

    import aiohttp

    url = spec["url"].format(slug=slug)
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return None
            data = json.loads(await resp.text())
    except Exception:
        return None
    count = spec["count"](data)
    if count is None:
        return None
    name = None
    if "name" in spec:
        try:
            name = spec["name"](data)
        except Exception:
            name = None
    return {
        "source": source,
        "slug": slug,
        "jobs": count,
        "company_name": name,
        "board_url": spec["board_url"].format(slug=slug),
        "config_key": spec["config_key"],
    }


async def _detect_boards_for(company: str, session) -> list[dict]:
    """Probe every ATS with every slug guess for one company; return the best
    hit (most jobs) per ATS, sorted by job count."""
    slugs = _slug_candidates(company)
    if not slugs:
        return []
    results = await asyncio.gather(*(
        _probe_ats(source, spec, slug, session)
        for source, spec in _ATS_PROBES.items()
        for slug in slugs
    ))
    best: dict = {}
    for r in results:
        if r and (r["source"] not in best or r["jobs"] > best[r["source"]]["jobs"]):
            best[r["source"]] = r
    return sorted(best.values(), key=lambda r: -r["jobs"])


@router.post("/api/sources/detect-boards")
async def detect_boards(body: DetectBoardsRequest):
    """Probe the public APIs of all supported ATSes with slug guesses derived
    from a company name. Returns every (source, slug) that answers with a
    live board so the user never has to know slugs up front."""
    import aiohttp

    slugs = _slug_candidates(body.company)
    if not slugs:
        raise HTTPException(400, "Give me a company name to look for")

    async with aiohttp.ClientSession(headers={"User-Agent": "Jobsmith/1.0"}) as session:
        matches = await _detect_boards_for(body.company, session)
    return {"matches": matches, "tried_slugs": slugs}


class SuggestCompaniesRequest(BaseModel):
    exclude: list[str] = []


def _norm_company(name: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]", "", name.lower())


@router.post("/api/sources/suggest-companies")
async def suggest_companies(body: SuggestCompaniesRequest):
    """AI company recommender: merge zero-hallucination candidates mined from
    the user's own feed (companies that scored well / were applied to) with
    LLM suggestions from their profile, then validate every candidate against
    the live ATS board probes. Only companies with a reachable board are
    returned, so a hallucinated suggestion costs nothing."""
    import aiohttp

    from .. import ai_engine

    cfg = state.load_config()
    search_cfg = cfg.get("search", {})

    # Companies already watched (config holds slugs) or already shown.
    watched = {
        _norm_company(s)
        for key in ("greenhouse_boards", "greenhouse_companies", "lever_companies",
                    "ashby_boards", "workable_accounts", "recruitee_companies")
        for s in (search_cfg.get(key) or [])
    }
    shown = {_norm_company(n) for n in body.exclude}

    def _fresh(name: str) -> bool:
        n = _norm_company(name)
        return bool(n) and n not in watched and n not in shown

    # 1. Zero-hallucination pool: the user's own feed.
    signals = await db.get_company_signals(min_score=70.0, limit=15)
    candidates: list[dict] = []
    for s in signals:
        if not _fresh(s["company"]):
            continue
        why = f"{s['matched']} job{'s' if s['matched'] != 1 else ''} scored ≥70% fit in your feed (best {int(s['best_score'])}%)"
        if s["applied"]:
            why += f"; you applied to {s['applied']} of them"
        candidates.append({"name": s["company"], "why": why, "origin": "history"})

    # 2. LLM pool. AI being down shouldn't kill the history-mined results.
    ai_error = None
    try:
        liked = [s["company"] for s in signals]
        exclude_for_ai = body.exclude + liked + [c["name"] for c in candidates]
        ai_suggestions = await ai_engine.suggest_companies(
            cfg.get("profile", {}), search_cfg, liked, exclude_for_ai, cfg,
        )
    except Exception as e:
        logger.warning("suggest-companies: AI call failed: %s", e)
        ai_suggestions, ai_error = [], str(e)

    seen = {_norm_company(c["name"]) for c in candidates}
    for s in ai_suggestions:
        if _fresh(s["name"]) and _norm_company(s["name"]) not in seen:
            seen.add(_norm_company(s["name"]))
            candidates.append({"name": s["name"], "why": s["why"], "origin": "ai"})

    candidates = candidates[:12]

    # 3. Validate every candidate against the live board probes (bounded).
    sem = asyncio.Semaphore(4)
    async with aiohttp.ClientSession(headers={"User-Agent": "Jobsmith/1.0"}) as session:
        async def _validate(cand: dict) -> Optional[dict]:
            async with sem:
                boards = await _detect_boards_for(cand["name"], session)
            live = [b for b in boards if b["jobs"] > 0]
            return {**cand, "boards": live} if live else None

        validated = await asyncio.gather(*(_validate(c) for c in candidates))

    suggestions = [v for v in validated if v]
    return {
        "suggestions": suggestions,
        "considered": len(candidates),
        "ai_error": ai_error,
    }


@router.post("/api/jobs/refetch-descriptions", status_code=202)
async def refetch_descriptions():
    """Re-fetch LinkedIn job detail pages for any rows with empty descriptions."""
    if state.refetch_status.get("active"):
        raise HTTPException(409, "Refetch already running")
    task = asyncio.create_task(bg._bg_refetch_descriptions())
    state.running_tasks["refetch_descriptions"] = task
    return {"message": "Refetch started"}


@router.get("/api/jobs/refetch-descriptions/status")
async def refetch_descriptions_status():
    return state.refetch_status


@router.post("/api/jobs/refetch-descriptions/cancel")
async def cancel_refetch_descriptions():
    state.cancel_refetch.set()
    task = state.running_tasks.get("refetch_descriptions")
    if task and not task.done():
        task.cancel()
    return {"message": "Refetch cancel requested"}


@router.post("/api/linkedin/resolve-locations")
async def resolve_linkedin_locations(body: dict):
    """Resolve location strings to LinkedIn geoIds.

    Body: {"locations": ["Denver", "Remote", ...]}
    Returns: {"results": [{"location": "Denver", "geo_id": "105072130", "source": "seed|cache|live", "ok": true}, ...]}

    Lets the user verify their search.locations entries before kicking off a fetch.
    """
    import aiohttp
    from ..job_sources.linkedin import _resolve_geo_id, _SEED_GEO_IDS, _load_geo_cache, _normalize_location

    locations = body.get("locations") or []
    if not isinstance(locations, list):
        raise HTTPException(400, "locations must be a list")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    results = []
    cache_before = dict(_load_geo_cache())  # snapshot to label cache vs live
    async with aiohttp.ClientSession() as session:
        for loc in locations:
            if not isinstance(loc, str) or not loc.strip():
                continue
            norm = _normalize_location(loc)
            if norm in cache_before:
                src = "cache"
            elif norm in _SEED_GEO_IDS:
                src = "seed"
            else:
                src = "live"
            geo = await _resolve_geo_id(session, loc, headers)
            results.append({
                "location": loc,
                "geo_id": geo or None,
                "source": src,
                "ok": bool(geo),
            })
    return {"results": results}


@router.patch("/api/jobs/{job_id}/status")
async def update_job_status(job_id: str, body: StatusUpdate):
    updated = await db.update_job_status(job_id, body.status)
    if not updated:
        raise HTTPException(404, "Job not found")
    if body.status in ("applied", "manual"):
        job = await db.get_job(job_id)
        title = job.get("title", "Unknown") if job else "Unknown"
        company = job.get("company", "") if job else ""
        await db.log_activity("manual_applied", f"Manually marked as applied: {title} at {company}", job_id)
    return {"message": "Status updated"}
