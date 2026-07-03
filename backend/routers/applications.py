"""
routers/applications.py — Application review queue, apply triggers, live
apply control (pause/resume/force-stop), and content editing/revision.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from .. import app_state as state
from .. import background_tasks as bg
from .. import database as db
from .. import ai_engine
from .. import resume_generator
from .. import auto_apply

logger = logging.getLogger(__name__)

router = APIRouter()


class ApplicationContentUpdate(BaseModel):
    resume_content: Optional[str] = None
    cover_letter_content: Optional[str] = None


class ApplicationStatusUpdate(BaseModel):
    status: str  # "applied" | "manual"


class ApplicationOutcomeUpdate(BaseModel):
    outcome: str  # awaiting | no_response | screening | interview | offer | rejected | withdrawn


class ApplicationReviseRequest(BaseModel):
    target: str  # "resume" | "cover_letter"
    instructions: str
    model_tier: Optional[str] = None  # "fast" | "strong"; falls back to global default
    honesty_level: Optional[str] = None  # honest | tailored | embellished | fabricated; falls back to global default


@router.get("/api/applications/pending")
async def pending_reviews(limit: int = Query(20, ge=1, le=100)):
    return await db.get_pending_reviews(limit=limit)


@router.get("/api/applications/submitted")
async def submitted_applications(limit: int = Query(50, ge=1, le=200)):
    return await db.get_submitted_applications(limit=limit)


@router.get("/api/applications/failed")
async def failed_applications(limit: int = Query(50, ge=1, le=200)):
    return await db.get_failed_applications(limit=limit)


@router.get("/api/applications/in-progress")
async def applications_in_progress():
    """Return live (currently applying) and needs-attention applications."""
    import json as _json

    # ── Category 1: LIVE ────────────────────────────────────────────────────
    in_progress: list[dict] = []

    # Prefer per-app keys ("apply:{app_id}") set by the manual-trigger path.
    active_app_ids = [
        key[len("apply:"):]
        for key, task in state.running_tasks.items()
        if key.startswith("apply:") and not task.done()
    ]

    if active_app_ids:
        _db = await db._get_db()
        try:
            placeholders = ",".join("?" * len(active_app_ids))
            cursor = await _db.execute(
                f"""SELECT a.id, a.job_id, j.title, j.company
                    FROM applications a JOIN jobs j ON j.id = a.job_id
                    WHERE a.id IN ({placeholders})""",
                active_app_ids,
            )
            rows = await cursor.fetchall()
        finally:
            await _db.close()
        for row in rows:
            in_progress.append({
                "job_id": row["job_id"], "job_title": row["title"],
                "company": row["company"], "state": "applying", "live": True,
                "adapter": None, "tier": None,
                "fields_filled": None, "fields_skipped": None, "screenshot_path": None,
            })
    else:
        # Fall back: generic "apply" key (approve-path) — find DB row with status=applying or paused.
        apply_task = state.running_tasks.get("apply")
        if apply_task and not apply_task.done():
            _db = await db._get_db()
            try:
                cursor = await _db.execute(
                    """SELECT a.id, a.job_id, a.status, j.title, j.company
                       FROM applications a JOIN jobs j ON j.id = a.job_id
                       WHERE a.status IN ('applying', 'paused')
                       ORDER BY a.created_at DESC LIMIT 1""",
                )
                rows = await cursor.fetchall()
            finally:
                await _db.close()
            for row in rows:
                in_progress.append({
                    "id": row["id"],
                    "job_id": row["job_id"], "job_title": row["title"],
                    "company": row["company"], "state": row["status"], "live": True,
                    "paused": row["status"] == "paused",
                    "adapter": None, "tier": None,
                    "fields_filled": None, "fields_skipped": None, "screenshot_path": None,
                })

    # ── Category 2: NEEDS ATTENTION ─────────────────────────────────────────
    needs_attention: list[dict] = []

    _db2 = await db._get_db()
    try:
        cursor = await _db2.execute(
            """SELECT a.id, a.job_id, a.status, j.title, j.company
               FROM applications a JOIN jobs j ON j.id = a.job_id
               WHERE a.status IN ('autofill_complete', 'needs_review', 'rate_limited')
               ORDER BY a.created_at DESC""",
        )
        attention_rows = await cursor.fetchall()
    finally:
        await _db2.close()

    if attention_rows:
        job_id_set = {row["job_id"] for row in attention_rows}

        # Read JSONL once; keep the last "result"-level entry per job_id.
        result_by_job: dict[str, dict] = {}
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
                        jid = obj.get("job_id")
                        if jid in job_id_set and obj.get("level") == "result":
                            result_by_job[jid] = obj  # last entry wins
        except Exception:
            logger.exception("in-progress: failed reading JSONL for enrichment")

        for row in attention_rows:
            jid = row["job_id"]
            meta = result_by_job.get(jid, {})
            needs_attention.append({
                "id": row["id"],
                "job_id": jid, "job_title": row["title"],
                "company": row["company"], "state": row["status"], "live": False,
                "adapter": meta.get("provider"),
                "tier": meta.get("tier"),
                "fields_filled": meta.get("fields_filled"),
                "fields_skipped": meta.get("fields_skipped"),
                "skipped_field_names": meta.get("skipped_field_names", []),
                "screenshot_path": meta.get("screenshot_path"),
            })

    return {"in_progress": in_progress, "needs_attention": needs_attention}


@router.get("/api/applications/{app_id}/apply-progress")
async def application_apply_progress(app_id: str):
    """Return live apply progress for a specific application (empty if not active)."""
    progress = auto_apply.orchestrator.get_apply_progress()
    if progress.get("application_id") == app_id:
        return {**progress, "elapsed_seconds": int(progress.get("elapsed_seconds", 0))}
    return {"active": False}


@router.post("/api/applications/{app_id}/approve")
async def approve_application(app_id: str):
    await db.update_application_status(app_id, "approved")

    cfg = state.load_config()
    if cfg.get("auto_apply", {}).get("enabled", False):
        task = asyncio.create_task(bg._bg_apply(app_id))
        state.running_tasks["apply"] = task
        return {"message": "Application approved — auto-apply triggered"}
    return {"message": "Application approved"}


@router.post("/api/applications/{app_id}/reject")
async def reject_application(app_id: str):
    await db.update_application_status(app_id, "rejected")
    return {"message": "Application rejected"}


@router.post("/api/applications/{app_id}/requeue")
async def requeue_application(app_id: str):
    await db.update_application_status(app_id, "pending_review")
    return {"message": "Application requeued for review"}


@router.post("/api/applications/{app_id}/apply", status_code=202)
async def apply_application(app_id: str, request: Request):
    task = asyncio.create_task(bg._bg_apply(app_id))
    state.running_tasks["apply"] = task
    state.running_tasks[f"apply:{app_id}"] = task
    return {"message": "Apply triggered"}


@router.post("/api/applications/{app_id}/apply/cancel")
async def cancel_single_apply(app_id: str):
    task = state.running_tasks.get(f"apply:{app_id}")
    if task and not task.done():
        task.cancel()
        await db.update_application_status(app_id, "approved")
        return {"message": "Apply cancelled, application reset to approved"}
    return {"message": "No active apply task for this application"}


@router.post("/api/applications/apply/cancel")
async def cancel_apply():
    state.cancel_apply.set()
    task = state.running_tasks.get("apply")
    if task and not task.done():
        task.cancel()
    return {"message": "Apply cancel requested"}


@router.post("/api/applications/apply/force-stop")
async def force_stop_apply():
    """Force stop: cancel task AND close the browser immediately."""
    state.cancel_apply.set()
    auto_apply.set_paused(False)
    # Cancel all active apply tasks
    for key, task in list(state.running_tasks.items()):
        if key.startswith("apply") and not task.done():
            task.cancel()
    # Force-close the browser (Playwright path)
    await auto_apply.force_stop()
    # Force-close Browser-Use session if active
    try:
        from .. import browser_use_agent
        await browser_use_agent.force_stop()
    except Exception:
        pass
    return {"message": "Force stop — browser closed"}


@router.post("/api/applications/apply/pause")
async def pause_apply():
    """Pause: freeze automation in place without closing the browser.

    The running task is NOT cancelled — it blocks in _pause_check() while the
    Playwright browser stays open so the user can interact manually.
    Call /resume to unfreeze.
    """
    auto_apply.set_paused(True)
    # Update DB status so the UI shows "paused" immediately.
    if state.current_apply_app_id:
        await db.update_application_status(
            state.current_apply_app_id, "paused", error_message="Paused by user"
        )
        state.push_notification(
            "apply", "Apply Paused",
            "Automation paused — browser is open for manual interaction",
            "info",
        )
    return {"message": "Paused — automation frozen, browser left open"}


@router.post("/api/applications/apply/resume")
async def resume_apply():
    """Resume: unfreeze the paused automation.

    Clears the pause flag so the _pause_check() loop in the orchestrator exits
    and the adapter continues from where it stopped.

    Returns {"live": true} if a blocked task was unpaused, {"live": false} if
    there was no live task (caller should restart the apply instead).
    """
    live = auto_apply.is_paused() and bool(state.current_apply_app_id)
    auto_apply.set_paused(False)
    if state.current_apply_app_id:
        await db.update_application_status(state.current_apply_app_id, "applying")
        state.push_notification(
            "apply", "Apply Resumed",
            "Automation resuming...",
            "info",
        )
    return {"message": "Resumed — automation continuing", "live": live}


@router.patch("/api/applications/{app_id}/content")
async def update_application_content(app_id: str, body: ApplicationContentUpdate):
    # Get the application + job data for DOCX regeneration
    apps_db = await db._get_db()
    try:
        cursor = await apps_db.execute(
            """SELECT a.*, j.id as j_id, j.title, j.company
               FROM applications a JOIN jobs j ON j.id = a.job_id
               WHERE a.id = ?""",
            (app_id,),
        )
        row = await cursor.fetchone()
    finally:
        await apps_db.close()

    if not row:
        raise HTTPException(404, "Application not found")

    row = dict(row)
    cfg = state.load_config()
    profile = cfg.get("profile", {})
    job_data = {"id": row["j_id"], "title": row["title"], "company": row["company"]}

    # Update text content in database
    updated = await db.update_application_content(
        app_id,
        resume_content=body.resume_content or "",
        cover_letter_content=body.cover_letter_content or "",
    )
    if not updated:
        raise HTTPException(404, "Application not found")

    # Regenerate documents so downloads reflect the edits (honors the
    # configured PDF/DOCX format).
    try:
        if body.resume_content:
            resume_generator.generate_resume(body.resume_content, profile, job_data, cfg)
        if body.cover_letter_content:
            resume_generator.generate_cover_letter(body.cover_letter_content, profile, job_data, cfg)
    except Exception:
        logger.exception("Failed to regenerate documents after content edit")

    return {"message": "Content updated and documents regenerated"}


@router.post("/api/applications/{app_id}/revise")
async def revise_application_content(app_id: str, body: ApplicationReviseRequest):
    """AI-assisted revision of a tailored resume or cover letter.

    Returns the revised text only — does NOT persist. The frontend should
    PATCH /api/applications/{app_id}/content to save if the user accepts.
    """
    if body.target not in {"resume", "cover_letter"}:
        raise HTTPException(400, "target must be 'resume' or 'cover_letter'")
    instructions = (body.instructions or "").strip()
    if not instructions:
        raise HTTPException(400, "instructions must not be empty")

    apps_db = await db._get_db()
    try:
        cursor = await apps_db.execute(
            """SELECT a.resume_content, a.cover_letter_content,
                      j.id as j_id, j.title, j.company, j.description
               FROM applications a JOIN jobs j ON j.id = a.job_id
               WHERE a.id = ?""",
            (app_id,),
        )
        row = await cursor.fetchone()
    finally:
        await apps_db.close()

    if not row:
        raise HTTPException(404, "Application not found")

    row = dict(row)
    cfg = state.load_config()
    profile = cfg.get("profile", {})
    job_data = {
        "id": row["j_id"],
        "title": row["title"],
        "company": row["company"],
        "description": row.get("description") or "",
    }

    # Resolve effective model tier: per-request override → global default → "strong"
    if body.model_tier is not None:
        if body.model_tier not in state.VALID_AI_EDIT_TIERS:
            raise HTTPException(400, f"model_tier must be one of: {sorted(state.VALID_AI_EDIT_TIERS)}")
        tier = body.model_tier
    else:
        tier = cfg.get("application_honesty", {}).get("ai_edit_model_tier", "strong")
        if tier not in state.VALID_AI_EDIT_TIERS:
            tier = "strong"

    # Resolve effective honesty level: per-request override → global default → "honest"
    if body.honesty_level is not None:
        if body.honesty_level not in state.VALID_HONESTY_LEVELS:
            raise HTTPException(400, f"honesty_level must be one of: {sorted(state.VALID_HONESTY_LEVELS)}")
        honesty_level = body.honesty_level
    else:
        honesty_level = cfg.get("application_honesty", {}).get("honesty_level", "honest")
        if honesty_level not in state.VALID_HONESTY_LEVELS:
            honesty_level = "honest"

    if body.target == "resume":
        current = row.get("resume_content") or ""
        if not current:
            raise HTTPException(400, "No tailored resume exists yet for this application")
        revised = await ai_engine.revise_tailored_resume(current, instructions, job_data, profile, cfg, tier=tier, honesty_level=honesty_level)
    else:
        current = row.get("cover_letter_content") or ""
        if not current:
            raise HTTPException(400, "No cover letter exists yet for this application")
        revised = await ai_engine.revise_cover_letter(current, instructions, job_data, profile, cfg, tier=tier, honesty_level=honesty_level)

    return {"revised_content": revised, "model_tier": tier, "honesty_level": honesty_level}


@router.patch("/api/applications/{application_id}/outcome")
async def update_application_outcome(application_id: str, body: ApplicationOutcomeUpdate):
    """Update the post-apply outcome (orthogonal to status — status drives
    the apply orchestrator, outcome tracks what happened after submission)."""
    if body.outcome not in db.VALID_OUTCOMES:
        raise HTTPException(400, f"outcome must be one of: {sorted(db.VALID_OUTCOMES)}")
    updated = await db.update_application_outcome(application_id, body.outcome)
    if not updated:
        raise HTTPException(404, "Application not found")
    return {"ok": True}


_ALLOWED_MANUAL_STATUSES = {"applied", "manual"}


@router.patch("/api/applications/{application_id}/status")
async def update_application_status_manual(application_id: str, body: ApplicationStatusUpdate):
    """Human-initiated status override (Mark as Applied / Dismiss).

    NOTE: db.update_application_status() guards against the auto-apply pipeline
    overwriting 'needs_review' rows — that guard also fires here.  For
    'autofill_complete' and 'rate_limited' the transition works correctly.
    Fixing the guard to be context-aware (auto vs human) is tracked as a
    follow-up task.
    """
    if body.status not in _ALLOWED_MANUAL_STATUSES:
        raise HTTPException(400, f"status must be one of: {sorted(_ALLOWED_MANUAL_STATUSES)}")

    # Fetch job_id + labels for the activity log entry.
    _adb = await db._get_db()
    try:
        cursor = await _adb.execute(
            """SELECT a.job_id, j.title, j.company
               FROM applications a JOIN jobs j ON j.id = a.job_id
               WHERE a.id = ?""",
            (application_id,),
        )
        row = await cursor.fetchone()
    finally:
        await _adb.close()

    if not row:
        raise HTTPException(404, "Application not found")

    job_id, title, company = row["job_id"], row["title"], row["company"]

    await db.update_application_status(application_id, body.status)
    action = "applied" if body.status == "applied" else "manual_apply_needed"
    detail = f"Manually {'marked applied' if body.status == 'applied' else 'dismissed'}: {title} at {company}"
    await db.log_activity(action, detail, job_id)
    return {"ok": True}
