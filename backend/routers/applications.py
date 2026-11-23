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
from .. import fact_guard
from .. import section_edit
from .. import facts as facts_mod

logger = logging.getLogger(__name__)

router = APIRouter()


def _apply_in_progress() -> bool:
    """True while an apply run is live.

    The orchestrator drives browser automation through module-level singletons
    (``_active_ctrl``/``_apply_progress``), so only one apply can run at a time;
    a second concurrent run would stomp that shared state. Callers use this to
    reject a duplicate trigger with 409 rather than spawning a stomping task.
    """
    task = state.running_tasks.get("apply")
    return bool(task and not task.done())


class ApplicationContentUpdate(BaseModel):
    resume_content: Optional[str] = None
    cover_letter_content: Optional[str] = None
    # The level an AI edit was made at, so the fact check matches it; defaults to
    # the level the application was tailored at.
    honesty_level: Optional[str] = None
    # The AI instruction behind this change (kept with the version it replaces).
    instruction: Optional[str] = None


class ApplicationStatusUpdate(BaseModel):
    status: str  # "applied" | "manual"


class ApplicationOutcomeUpdate(BaseModel):
    outcome: str  # awaiting | no_response | screening | interview | offer | rejected | withdrawn


class ApplicationScheduleUpdate(BaseModel):
    """Omit a field to leave it alone; send an explicit clear_* to unset it —
    None can't mean "clear" or you couldn't set one date without wiping the other."""
    follow_up_at: Optional[str] = None
    interview_at: Optional[str] = None
    clear_follow_up: bool = False
    clear_interview: bool = False


class ApplicationReviseRequest(BaseModel):
    target: str  # "resume" | "cover_letter"
    instructions: str
    model_tier: Optional[str] = None  # "fast" | "strong"; falls back to global default
    honesty_level: Optional[str] = None  # honest | tailored | embellished | fabricated; falls back to global default
    section: Optional[str] = None  # resume header (e.g. "EXPERIENCE") or "paragraph:N"; omit/"whole" = whole document


class FactBody(BaseModel):
    text: str


def _facts_payload(facts: list[dict]) -> dict:
    return {"facts": facts, "max_facts": facts_mod.MAX_FACTS, "max_chars": facts_mod.MAX_FACT_CHARS}


def _clean_fact_or_400(text: str) -> str:
    try:
        return facts_mod.clean_fact(text)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/applications/{app_id}/facts")
async def list_application_facts(app_id: str):
    """Facts the candidate keeps for this application (sent with every AI write/edit)."""
    if not await db.application_exists(app_id):
        raise HTTPException(404, "Application not found")
    return _facts_payload(await db.get_application_facts(app_id))


@router.post("/api/applications/{app_id}/facts", status_code=201)
async def add_application_fact(app_id: str, body: FactBody):
    text = _clean_fact_or_400(body.text)
    if not await db.application_exists(app_id):
        raise HTTPException(404, "Application not found")
    try:
        fact = await db.add_application_fact(app_id, text)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return fact


@router.patch("/api/applications/{app_id}/facts/{fact_id}")
async def update_application_fact(app_id: str, fact_id: int, body: FactBody):
    text = _clean_fact_or_400(body.text)
    if not await db.update_application_fact(app_id, fact_id, text):
        raise HTTPException(404, "Fact not found")
    return {"id": fact_id, "text": text}


@router.delete("/api/applications/{app_id}/facts/{fact_id}")
async def delete_application_fact(app_id: str, fact_id: int):
    if not await db.delete_application_fact(app_id, fact_id):
        raise HTTPException(404, "Fact not found")
    return {"ok": True}


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
    if _apply_in_progress():
        raise HTTPException(409, "An application is already being applied")
    task = asyncio.create_task(bg._bg_apply(app_id))
    state.running_tasks["apply"] = task
    state.running_tasks[f"apply:{app_id}"] = task
    return {"message": "Apply triggered"}


@router.post("/api/applications/{app_id}/apply/cancel")
async def cancel_single_apply(app_id: str):
    task = state.running_tasks.get(f"apply:{app_id}")
    if task and not task.done():
        task.cancel()
        await db.update_application_status(app_id, "pending_review")
        return {"message": "Apply cancelled, application reset to pending review"}
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


async def _save_content(
    app_id: str, resume: str, cover_letter: str,
    honesty_level: Optional[str] = None, instruction: Optional[str] = None,
) -> dict:
    """Save new resume/cover letter text: record the replaced versions for Undo, regenerate
    the documents (honors the configured PDF/DOCX format) and refresh the fact-check flags."""
    apps_db = await db._get_db()
    try:
        cursor = await apps_db.execute(
            """SELECT a.*, j.id as j_id, j.title, j.company, j.location, j.description
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

    updated = await db.update_application_content(
        app_id, resume_content=resume, cover_letter_content=cover_letter, instruction=instruction,
    )
    if not updated:
        raise HTTPException(404, "Application not found")

    try:
        if resume:
            resume_generator.generate_resume(resume, profile, job_data, cfg)
        if cover_letter:
            resume_generator.generate_cover_letter(cover_letter, profile, job_data, cfg)
    except Exception:
        logger.exception("Failed to regenerate documents after content edit")

    # The stored "Check this" flags describe the text as it is now.
    flags: dict = {}
    try:
        flags = fact_guard.check_documents(
            resume, cover_letter, profile,
            {**job_data, "location": row["location"], "description": row["description"]},
            honesty_level if honesty_level in state.VALID_HONESTY_LEVELS
            else (row["honesty_level"] or "honest"),
            [f["text"] for f in await db.get_application_facts(app_id)],
        )
        await db.set_fact_flags(app_id, flags)
    except Exception:
        logger.exception("Fact check failed after content edit")
    return flags


@router.patch("/api/applications/{app_id}/content")
async def update_application_content(app_id: str, body: ApplicationContentUpdate):
    await _save_content(
        app_id, body.resume_content or "", body.cover_letter_content or "",
        honesty_level=body.honesty_level, instruction=(body.instruction or "").strip() or None,
    )
    return {"message": "Content updated and documents regenerated"}


@router.get("/api/applications/{app_id}/sections")
async def list_sections(app_id: str, target: str = Query("resume")):
    """The parts of the tailored resume (its headers) or cover letter (paragraphs) that an
    AI edit can be limited to."""
    if target not in {"resume", "cover_letter"}:
        raise HTTPException(400, "target must be 'resume' or 'cover_letter'")
    row = await _content_row(app_id)
    return {"sections": section_edit.options(target, row[f"{target}_content"] or "")}


@router.get("/api/applications/{app_id}/revisions")
async def list_revisions(app_id: str, target: str = Query("resume")):
    """Earlier versions of the resume or cover letter, newest first."""
    if target not in db.REVISION_TARGETS:
        raise HTTPException(400, "target must be 'resume' or 'cover_letter'")
    if not await db.application_exists(app_id):
        raise HTTPException(404, "Application not found")
    return {"revisions": await db.get_revisions(app_id, target), "cap": db.REVISION_CAP}


@router.post("/api/applications/{app_id}/revisions/{revision_id}/restore")
async def restore_revision(app_id: str, revision_id: int):
    """Put an earlier version back. The text it replaces is recorded as a revision too, so
    a restore can itself be undone."""
    rev = await db.get_revision(app_id, revision_id)
    if not rev:
        raise HTTPException(404, "Revision not found")
    row = await _content_row(app_id)
    resume, cover = row["resume_content"] or "", row["cover_letter_content"] or ""
    if rev["target"] == "resume":
        resume = rev["content"]
    else:
        cover = rev["content"]
    flags = await _save_content(app_id, resume, cover, instruction="Restored an earlier version")
    return {"target": rev["target"], "content": rev["content"], "fact_flags": flags}


async def _content_row(app_id: str) -> dict:
    apps_db = await db._get_db()
    try:
        cursor = await apps_db.execute(
            "SELECT resume_content, cover_letter_content FROM applications WHERE id = ?", (app_id,)
        )
        row = await cursor.fetchone()
    finally:
        await apps_db.close()
    if not row:
        raise HTTPException(404, "Application not found")
    return dict(row)


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

    app_facts = [f["text"] for f in await db.get_application_facts(app_id)]

    current = row.get(f"{body.target}_content") or ""
    if not current:
        raise HTTPException(400, "No tailored resume exists yet for this application" if body.target == "resume"
                            else "No cover letter exists yet for this application")

    # Optionally limit the edit to one section: only it is rewritten, the rest is
    # read-only context, and the answer is spliced back so the rest stays byte-identical.
    chunks, index, section = None, 0, None
    if (body.section or "").strip().lower() not in ("", section_edit.WHOLE):
        found = section_edit.find(body.target, current, body.section)
        if not found:
            valid = [o["id"] for o in section_edit.options(body.target, current)]
            raise HTTPException(400, f"section must be 'whole' or one of: {valid}")
        chunks, index = found
        section = (chunks[index].label or chunks[index].id, section_edit.read_only_context(chunks, index))
    text_in = chunks[index].text if chunks else current

    revise = ai_engine.revise_tailored_resume if body.target == "resume" else ai_engine.revise_cover_letter
    kwargs = {"app_facts": app_facts, **({"section": section} if section else {})}
    revised = await revise(text_in, instructions, job_data, profile, cfg, tier=tier,
                           honesty_level=honesty_level, **kwargs)
    new_part = revised
    if chunks:
        new_part = section_edit.fit(body.target, chunks[index], revised)
        revised = section_edit.splice(body.target, chunks, index, revised)

    # Only what the edit wrote is checked when just one section changed.
    unverified = fact_guard.check(
        new_part, profile, job_data, honesty_level, app_facts, extra=[instructions]
    )
    return {"revised_content": revised, "model_tier": tier, "honesty_level": honesty_level,
            "unverified": unverified, "section": body.section if chunks else None}


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


@router.get("/api/applications/{application_id}/events")
async def get_application_events(application_id: str):
    """Append-only outcome history — every stage this application reached, and
    whether each transition came from the user, the ghost sweep, or email."""
    return {"events": await db.get_application_events(application_id)}


@router.get("/api/applications/due")
async def get_due_applications():
    """Applications wanting attention: follow-ups now due, upcoming interviews,
    and still-silent applications nearing the ghost threshold.

    The desktop renders this as a queue rather than a push: backend notifications
    are an in-memory deque the frontend polls, so they only fire while the app is
    open — useless for a "you applied 7 days ago" nudge. The phone, which has real
    scheduled notifications, is the push surface.
    """
    cfg = state.load_config()
    days = int(cfg.get("pipeline", {}).get("ghost_after_days", 21))
    return await db.get_due_applications(ghost_after_days=days)


@router.patch("/api/applications/{application_id}/schedule")
async def update_application_schedule(application_id: str, body: ApplicationScheduleUpdate):
    """Set or clear an application's follow-up / interview dates."""
    updated = await db.set_application_schedule(
        application_id,
        follow_up_at=body.follow_up_at,
        interview_at=body.interview_at,
        clear_follow_up=body.clear_follow_up,
        clear_interview=body.clear_interview,
    )
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
