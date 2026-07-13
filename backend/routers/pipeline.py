"""
routers/pipeline.py — Batch pipeline triggers: scoring, tailoring, salary
estimation, apply-type detection, plus the n8n webhooks that drive them.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .. import app_state as state
from .. import background_tasks as bg
from .. import database as db

logger = logging.getLogger(__name__)

router = APIRouter()


class TailorBatchRequest(BaseModel):
    min_score: float = 50.0


class ApproveBatchRequest(BaseModel):
    application_ids: list[str]


@router.post("/api/jobs/{job_id}/score", status_code=202)
async def score_job(job_id: str):
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    asyncio.create_task(bg._bg_score_job(job_id))
    return {"message": "Scoring started"}


@router.post("/api/jobs/score-batch", status_code=202)
async def score_batch(
    limit: Optional[int] = Query(None, ge=1, description="Max jobs to score; omit to score all"),
    rescore: bool = Query(False, description="Also re-score jobs that already have a score"),
):
    if state.task_running("score_batch"):
        raise HTTPException(409, "Batch scoring is already running")
    task = asyncio.create_task(bg._bg_score_batch(limit=limit, rescore=rescore))
    state.running_tasks["score_batch"] = task
    return {"message": "Batch rescoring started" if rescore else "Batch scoring started"}


@router.post("/api/jobs/score-batch/cancel")
async def cancel_score_batch():
    state.cancel_score.set()
    task = state.running_tasks.get("score_batch")
    if task and not task.done():
        task.cancel()
    return {"message": "Batch scoring cancel requested"}


@router.post("/api/jobs/estimate-salaries", status_code=202)
async def estimate_salaries_batch(
    limit: Optional[int] = Query(None, ge=1, description="Max jobs to estimate; omit for all"),
):
    """Start a background batch to pull market salaries for jobs missing one."""
    if state.task_running("estimate_salaries"):
        raise HTTPException(409, "Salary estimation is already running")
    task = asyncio.create_task(bg._bg_estimate_salaries_batch(limit=limit))
    state.running_tasks["estimate_salaries"] = task
    return {"message": "Salary estimation started"}


@router.post("/api/jobs/estimate-salaries/cancel")
async def cancel_estimate_salaries():
    state.cancel_estimate_salaries.set()
    task = state.running_tasks.get("estimate_salaries")
    if task and not task.done():
        task.cancel()
    return {"message": "Salary estimation cancel requested"}


@router.post("/api/detect-apply-types", status_code=202)
async def detect_apply_types():
    """Start background apply-type classification for all unclassified jobs.

    Returns 202 immediately; poll ``GET /api/detect-apply-types/status`` for
    progress, or wait for a ``detect_apply_types`` notification event.
    """
    if state.task_running("detect_apply_types"):
        raise HTTPException(409, "Apply-type detection is already running")
    task = asyncio.create_task(bg._bg_detect_apply_types())
    state.running_tasks["detect_apply_types"] = task
    return {"message": "Apply type detection started"}


@router.get("/api/detect-apply-types/status")
async def detect_apply_types_status():
    """Return the current state of the apply-type detection run."""
    return state.detect_types_status


@router.post("/api/detect-apply-types/cancel")
async def cancel_detect_apply_types():
    state.cancel_detect_types.set()
    task = state.running_tasks.get("detect_apply_types")
    if task and not task.done():
        task.cancel()
    return {"message": "Apply type detection cancel requested"}


@router.post("/api/jobs/{job_id}/tailor", status_code=202)
async def tailor_job(job_id: str):
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    asyncio.create_task(bg._bg_tailor_job(job_id))
    return {"message": "Tailoring started"}


@router.post("/api/jobs/tailor-batch", status_code=202)
async def tailor_batch(body: TailorBatchRequest):
    if state.task_running("tailor_batch"):
        raise HTTPException(409, "Batch tailoring is already running")
    task = asyncio.create_task(bg._bg_tailor_batch(body.min_score))
    state.running_tasks["tailor_batch"] = task
    return {"message": "Batch tailoring started"}


@router.post("/api/jobs/tailor-batch/cancel")
async def cancel_tailor_batch():
    state.cancel_tailor.set()
    task = state.running_tasks.get("tailor_batch")
    if task and not task.done():
        task.cancel()
    return {"message": "Batch tailoring cancel requested"}


@router.post("/api/jobs/approve-batch")
async def approve_batch(body: ApproveBatchRequest):
    cfg = state.load_config()
    auto_apply_enabled = cfg.get("auto_apply", {}).get("enabled", False)
    for app_id in body.application_ids:
        await db.update_application_status(app_id, "approved")
        if auto_apply_enabled:
            asyncio.create_task(bg._bg_apply(app_id))
    return {"message": f"Approved {len(body.application_ids)} applications"}


# ---------------------------------------------------------------------------
# Webhook endpoints (for n8n)
# ---------------------------------------------------------------------------

@router.post("/api/webhooks/jobs-fetched")
async def webhook_jobs_fetched():
    await db.log_activity("webhook", "n8n: jobs-fetched webhook received")
    return {"message": "Acknowledged"}


@router.post("/api/webhooks/trigger-tailor", status_code=202)
async def webhook_trigger_tailor():
    # Shares the "tailor_batch" slot with POST /api/jobs/tailor-batch, so an n8n
    # retry storm can't stack workers on top of a run already in flight.
    if state.task_running("tailor_batch"):
        raise HTTPException(409, "Batch tailoring is already running")
    task = asyncio.create_task(bg._bg_tailor_batch(50.0))
    state.running_tasks["tailor_batch"] = task
    await db.log_activity("webhook", "n8n: trigger-tailor webhook received")
    return {"message": "Tailoring triggered"}
