"""
background_tasks.py — Long-running workers spawned by the API routes.

Each worker reads/writes shared runtime state through backend.app_state
(cancel events, status dicts, the notification queue). Routes create these
as asyncio tasks and register them in state.running_tasks so the matching
/cancel endpoints can stop them.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from . import app_state as state
from . import database as db
from . import ai_engine
from . import salary_estimator
from . import resume_generator
from . import auto_apply
from . import applicant_assist
from .job_sources import fetch_all_jobs
from .services import posting_quality

logger = logging.getLogger(__name__)


def _update_fetch_status(**kwargs):
    state.fetch_status.update(kwargs)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finish_score_status(status: str, detail: str):
    """Terminal transition for the batch-scoring feed (done/cancelled/error)."""
    state.score_status.update(status=status, current="", detail=detail, finished_at=_iso_now())


async def _bg_fetch_jobs(sources: list[str] | None = None):
    """Fetch jobs from selected sources (or all) and upsert into the database."""
    state.cancel_fetch.clear()
    state.fetch_keep_partial = False
    src_label = ", ".join(sources) if sources else "all sources"
    state.fetch_status = {"active": True, "phase": "fetching", "detail": f"Searching {src_label}...", "sources_done": 0, "sources_total": 0, "jobs_found": 0, "jobs_inserted": 0}
    try:
        cfg = state.load_config()
        partial_collector: list[dict] = []
        timed_out = False
        try:
            jobs = await asyncio.wait_for(
                fetch_all_jobs(cfg, sources=sources, on_progress=_update_fetch_status, cancel_event=state.cancel_fetch, _partial_collector=partial_collector),
                timeout=900,
            )
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning("Overall job fetch timed out after 900s; using %d partial results", len(partial_collector))
            # Deduplicate partial results by URL
            seen: set[str] = set()
            jobs = []
            for _j in partial_collector:
                _url = _j.get("url", "")
                if _url and _url in seen:
                    continue
                seen.add(_url)
                jobs.append(_j)
            state.fetch_status.update(partial=True, timed_out=True)
        stopped_early = False
        if state.cancel_fetch.is_set() and not timed_out:
            if state.fetch_keep_partial:
                stopped_early = True
                state.fetch_status.update(partial=True, stopped_early=True)
            else:
                state.fetch_status.update(phase="done", detail="Fetch cancelled", active=False)
                state.push_notification("fetch", "Job Fetch Cancelled", "Job fetch was stopped by user", "info")
                return
        state.fetch_status.update(phase="saving", detail=f"Saving {len(jobs)} jobs to database...", jobs_found=len(jobs))
        inserted = 0
        for i, job in enumerate(jobs):
            # Honor cancel during save only for hard-cancel; soft-stop wants every
            # already-collected job to be persisted.
            if state.cancel_fetch.is_set() and not state.fetch_keep_partial:
                state.fetch_status.update(phase="done", detail=f"Cancelled — saved {inserted} of {len(jobs)} jobs", jobs_found=len(jobs), jobs_inserted=inserted, active=False)
                state.push_notification("fetch", "Job Fetch Cancelled", f"Stopped after saving {inserted} jobs", "info")
                return
            job_id = await db.upsert_job(job)
            if job_id:
                inserted += 1
            await _refresh_job_quality(job_id, job)
            if (i + 1) % 10 == 0:
                state.fetch_status.update(detail=f"Saving jobs... {i + 1}/{len(jobs)}", jobs_inserted=inserted)
        if stopped_early:
            done_label = "Stopped early"
        elif timed_out:
            done_label = "Partial results (timed out)"
        else:
            done_label = "Done"
        # Surface degraded sources so "0 new jobs" is distinguishable from
        # "a source was bot-blocked or timed out".
        issues = []
        for status_key, label in (("sources_blocked", "blocked"),
                                  ("sources_timed_out", "timed out"),
                                  ("sources_failed", "failed"),
                                  ("sources_suspect", "no jobs repeatedly, may be broken")):
            names = state.fetch_status.get(status_key)
            if names:
                issues.append(f"{label}: {', '.join(names)}")
        issue_suffix = f" ({'; '.join(issues)})" if issues else ""
        state.fetch_status.update(phase="done", detail=f"{done_label} — {len(jobs)} jobs found, {inserted} new{issue_suffix}", jobs_found=len(jobs), jobs_inserted=inserted, active=False)
        await db.log_activity("jobs_fetched", f"Fetched {len(jobs)} jobs ({inserted} new) from {src_label}")
        logger.info("Job fetch complete: %d total, %d new from %s (timed_out=%s)", len(jobs), inserted, src_label, timed_out)
        # Maintenance: reclaim space from long-deleted jobs (keeps the row +
        # 'deleted' status so the deletion stays durable and syncs). Non-fatal.
        try:
            compacted = await db.gc_deleted_jobs()
            if compacted:
                logger.info("GC: compacted %d long-deleted jobs", compacted)
        except Exception:
            logger.exception("GC of deleted jobs failed (non-fatal)")
        state.push_notification("fetch", "Job Fetch Complete", f"Found {len(jobs)} jobs ({inserted} new) from {src_label}{issue_suffix}", "success")
    except asyncio.CancelledError:
        state.fetch_status.update(phase="done", detail="Fetch cancelled", active=False)
        state.push_notification("fetch", "Job Fetch Cancelled", "Job fetch was stopped by user", "info")
    except Exception:
        logger.exception("Background job fetch failed")
        state.fetch_status.update(phase="error", detail="Fetch failed — check server logs", active=False)
        state.push_notification("fetch", "Job Fetch Failed", "An error occurred while fetching jobs", "error")


async def _refresh_job_quality(job_id: Optional[str], job: dict) -> None:
    """Compute and store the posting-quality report for a just-upserted job.

    `job_id` is upsert_job's return value (the new id, or None on duplicate).
    For duplicates the stored row is looked up so the report reflects the
    incremented times_seen and any backfilled fields. Cheap pure-Python
    heuristics — failures never block the fetch pipeline.
    """
    try:
        row = None
        if job_id:
            row = await db.get_job(job_id)
        else:
            row = await db.get_job_by_source_external(
                job.get("source", ""), job.get("external_id", "")
            )
        if not row:
            return
        report = posting_quality.compute_quality_report(row)
        await db.set_job_quality_report(row["id"], report)
    except Exception:
        logger.exception("quality report failed for job %s", job.get("title"))


async def _maybe_estimate_salary(job: dict, cfg: dict) -> None:
    """Generate and persist a market salary estimate for a job.

    Used by both the scoring pipeline (market-comparison + gap-fill) and the
    ingestion pipeline (gap-fill only when configured). Failures are swallowed
    — a missing estimate must never block scoring or ingestion.
    """
    sal_cfg = cfg.get("salary_estimator", {}) or {}
    if not sal_cfg.get("enabled", True):
        return
    try:
        payload = await salary_estimator.estimate_salary(job, cfg)
        if payload:
            await db.update_job_estimated_salary(job["id"], payload)
    except Exception:
        logger.exception("salary estimate failed for job %s", job.get("id"))


async def _bg_tailor_job(job_id: str):
    """Score and generate tailored resume + cover letter for a single job."""
    try:
        cfg = state.load_config()
        profile = cfg.get("profile", {})
        honesty_level = cfg.get("application_honesty", {}).get("honesty_level", "honest")
        job = await db.get_job(job_id)
        if not job:
            return

        await db.update_job_status(job_id, "tailoring")
        await db.log_activity("tailoring_started", f"Tailoring {job['title']}", job_id)

        # Score (and concurrently produce a market salary estimate so the
        # vs-market badge / gap-fill is ready by the time tailoring finishes).
        score, reasoning, match_report = await ai_engine.score_job_fit(job, profile, cfg)
        await db.update_job_score(job_id, score, reasoning, match_report)

        if cfg.get("salary_estimator", {}).get("auto_on_ingest", True) and cfg.get("salary_estimator", {}).get("market_compare_on_score", True):
            await _maybe_estimate_salary(job, cfg)

        if state.cancel_tailor.is_set():
            await db.update_job_status(job_id, "discovered")
            return

        # Generate resume (required). Cover letter is attempted separately so a
        # CL failure doesn't prevent the resume from being saved.
        resume_text = await ai_engine.generate_tailored_resume(
            job, profile, cfg, honesty_level, match_report=match_report
        )

        cover_letter_text = ""
        try:
            cover_letter_text = await ai_engine.generate_cover_letter(job, profile, cfg, honesty_level)
        except Exception:
            logger.exception("Cover letter generation failed for job %s — saving resume only", job_id)

        # Generate DOCX files — failures are logged but do not abort the tailor.
        resume_path = None
        try:
            resume_path = resume_generator.generate_resume(resume_text, profile, job, cfg)
        except Exception:
            logger.exception("Resume document generation failed for job %s", job_id)

        cl_path = None
        if cover_letter_text:
            try:
                cl_path = resume_generator.generate_cover_letter(cover_letter_text, profile, job, cfg)
            except Exception:
                logger.exception("Cover letter DOCX generation failed for job %s", job_id)

        # Check auto-approve
        auto_approve = cfg.get("auto_apply", {}).get("auto_approve", False)

        await db.create_application(
            job_id=job_id,
            resume_content=resume_text,
            cover_letter_content=cover_letter_text,
            resume_path=resume_path,
            cover_letter_path=cl_path,
            auto_approved=auto_approve,
            honesty_level=honesty_level,
        )

        # Always generate embellishment log — even if CL is empty (e.g. generation failed).
        try:
            emb_log = await ai_engine.generate_embellishment_log(
                profile, resume_text, cover_letter_text, honesty_level, cfg
            )
            await db.set_embellishment_log(job_id, emb_log)
        except Exception:
            logger.exception("Embellishment log generation failed for job %s — continuing", job_id)

        await db.log_activity(
            "tailoring_complete",
            f"Score: {score:.0f} — {job['title']} at {job.get('company', '')} [{honesty_level}]",
            job_id,
        )
        state.push_notification("tailor", "Tailoring Complete", f"{job['title']} at {job.get('company', '')} — Score: {score:.0f}", "success")
    except asyncio.CancelledError:
        await db.update_job_status(job_id, "discovered")
    except Exception:
        logger.exception("Background tailoring failed for job %s", job_id)
        await db.update_job_status(job_id, "discovered")
        state.push_notification("tailor", "Tailoring Failed", f"Failed to tailor job {job_id}", "error")


async def _bg_tailor_batch(min_score: float):
    """Score all discovered jobs; generate materials for those above threshold."""
    state.cancel_tailor.clear()
    BATCH_SIZE = 50
    try:
        cfg = state.load_config()
        profile = cfg.get("profile", {})
        honesty_level = cfg.get("application_honesty", {}).get("honesty_level", "honest")
        processed = 0

        while True:
            if state.cancel_tailor.is_set():
                break
            result = await db.get_jobs(status="discovered", limit=BATCH_SIZE)
            jobs = result["jobs"]
            if not jobs:
                break
            logger.info("Batch tailoring %d discovered jobs (processed so far: %d, threshold: %.0f)", len(jobs), processed, min_score)

            # Phase 1 — score every job in the batch using the configured
            # scoring tier. Keeping scoring contiguous means LM Studio only
            # loads the scoring model once per batch instead of once per job.
            scored: list[tuple[dict, float, Optional[dict]]] = []
            for job in jobs:
                if state.cancel_tailor.is_set():
                    logger.info("Batch tailoring cancelled after %d jobs", processed)
                    state.push_notification("tailor", "Batch Tailoring Stopped", f"Stopped after processing {processed} jobs", "info")
                    await db.log_activity("batch_tailor_complete", f"Processed {processed} jobs (cancelled)")
                    return
                job_id = job["id"]
                await db.update_job_status(job_id, "tailoring")
                try:
                    score, reasoning, match_report = await ai_engine.score_job_fit(job, profile, cfg)
                except Exception:
                    logger.exception("Scoring failed for %s", job.get("title"))
                    await db.update_job_status(job_id, "discovered")
                    continue
                await db.update_job_score(job_id, score, reasoning, match_report)
                scored.append((job, score, match_report))

            # Phase 2 — salary estimates for the whole batch. Uses the Utility
            # tier; doing them back-to-back avoids ping-ponging the model
            # between scoring and salary calls.
            sal_cfg = cfg.get("salary_estimator", {})
            do_salary = sal_cfg.get("auto_on_ingest", True) and sal_cfg.get("market_compare_on_score", True)
            if do_salary:
                for job, _score, _report in scored:
                    if state.cancel_tailor.is_set():
                        break
                    await _maybe_estimate_salary(job, cfg)

            if state.cancel_tailor.is_set():
                logger.info("Batch tailoring cancelled after %d jobs", processed)
                state.push_notification("tailor", "Batch Tailoring Stopped", f"Stopped after processing {processed} jobs", "info")
                await db.log_activity("batch_tailor_complete", f"Processed {processed} jobs (cancelled)")
                return

            # Phase 3 — generate resume + cover letter for jobs that cleared
            # the threshold. All Content-tier work in a single contiguous run.
            for job, score, match_report in scored:
                if state.cancel_tailor.is_set():
                    logger.info("Batch tailoring cancelled after %d jobs", processed)
                    state.push_notification("tailor", "Batch Tailoring Stopped", f"Stopped after processing {processed} jobs", "info")
                    await db.log_activity("batch_tailor_complete", f"Processed {processed} jobs (cancelled)")
                    return
                job_id = job["id"]
                if score >= min_score:
                    try:
                        resume_text = await ai_engine.generate_tailored_resume(
                            job, profile, cfg, honesty_level, match_report=match_report
                        )
                        cl_text = await ai_engine.generate_cover_letter(job, profile, cfg, honesty_level)
                        resume_path = resume_generator.generate_resume(resume_text, profile, job, cfg)
                        cl_path = resume_generator.generate_cover_letter(cl_text, profile, job, cfg)

                        auto_approve = cfg.get("auto_apply", {}).get("auto_approve", False)
                        await db.create_application(
                            job_id=job_id,
                            resume_content=resume_text,
                            cover_letter_content=cl_text,
                            resume_path=resume_path,
                            cover_letter_path=cl_path,
                            auto_approved=auto_approve,
                            honesty_level=honesty_level,
                        )

                        emb_log = await ai_engine.generate_embellishment_log(
                            profile, resume_text, cl_text, honesty_level, cfg
                        )
                        await db.set_embellishment_log(job_id, emb_log)
                    except Exception:
                        logger.exception("Failed to generate materials for %s", job.get("title"))
                        await db.update_job_status(job_id, "discovered")
                else:
                    await db.update_job_status(job_id, "discovered")
                processed += 1

        await db.log_activity("batch_tailor_complete", f"Processed {processed} jobs")
        state.push_notification("tailor", "Batch Tailoring Complete", f"Processed {processed} jobs", "success")
    except asyncio.CancelledError:
        state.push_notification("tailor", "Batch Tailoring Stopped", "Batch tailoring was stopped by user", "info")
    except Exception:
        logger.exception("Batch tailoring failed")
        state.push_notification("tailor", "Batch Tailoring Failed", "An error occurred during batch tailoring", "error")


async def _bg_score_job(job_id: str):
    """Score a single job without generating tailored materials."""
    try:
        cfg = state.load_config()
        profile = cfg.get("profile", {})
        job = await db.get_job(job_id)
        if not job:
            return

        score, reasoning, match_report = await ai_engine.score_job_fit(job, profile, cfg)
        await db.update_job_score(job_id, score, reasoning, match_report)

        if cfg.get("salary_estimator", {}).get("auto_on_ingest", True) and cfg.get("salary_estimator", {}).get("market_compare_on_score", True):
            await _maybe_estimate_salary(job, cfg)

        await db.log_activity(
            "scored",
            f"Score: {score:.0f} — {job['title']} at {job.get('company', '')}",
            job_id,
        )
        state.push_notification("score", "Scoring Complete", f"{job['title']} — Score: {score:.0f}", "success")
    except Exception:
        logger.exception("Scoring failed for job %s", job_id)
        state.push_notification("score", "Scoring Failed", f"Failed to score job {job_id}", "error")


async def _bg_score_batch(limit: Optional[int] = None, rescore: bool = False):
    """Score discovered jobs that don't have a score yet.

    Args:
        limit: Maximum number of jobs to score. None means score all.
        rescore: Also re-score jobs that already have a score (e.g. after a
            profile change). Default scores unscored jobs only.
    """
    state.cancel_score.clear()
    BATCH_SIZE = 50
    verb = "Rescored" if rescore else "Scored"
    state.score_status = {"status": "scoring", "done": 0, "total": 0, "current": "", "detail": "Starting batch scoring...", "started_at": _iso_now(), "finished_at": None}
    try:
        cfg = state.load_config()
        profile = cfg.get("profile", {})
        scored = 0
        processed = 0

        # Rescored jobs stay in the query's result set (unlike unscored jobs,
        # which drop out once scored), so snapshot the target IDs up front and
        # walk that list instead of re-reading page 0 forever.
        pending_ids: list[str] = []
        if rescore:
            offset = 0
            while True:
                page = (await db.get_jobs(status="discovered", limit=500, offset=offset))["jobs"]
                if not page:
                    break
                pending_ids.extend(j["id"] for j in page)
                offset += len(page)

        # Progress feed: how many jobs this run intends to score.
        if rescore:
            total = len(pending_ids)
        else:
            total = (await db.get_jobs(status="discovered", unscored_only=True, limit=1))["total"]
        if limit is not None:
            total = min(total, limit)
        state.score_status.update(total=total, detail=f"Scoring {total} jobs...")

        while True:
            if state.cancel_score.is_set():
                break
            if limit is not None and scored >= limit:
                break

            fetch_size = BATCH_SIZE if limit is None else min(BATCH_SIZE, limit - scored)
            if rescore:
                batch_ids, pending_ids = pending_ids[:fetch_size], pending_ids[fetch_size:]
                if not batch_ids:
                    break
                # Re-fetch each job so we score fresh data and skip any deleted mid-run
                jobs = [j for jid in batch_ids if (j := await db.get_job(jid))]
                if not jobs:
                    continue
            else:
                result = await db.get_jobs(status="discovered", unscored_only=True, limit=fetch_size)
                jobs = result["jobs"]
                if not jobs:
                    break

            logger.info("Scoring batch of %d jobs (scored so far: %d, rescore=%s)", len(jobs), scored, rescore)

            for job in jobs:
                if state.cancel_score.is_set():
                    logger.info("Batch scoring cancelled after %d jobs", scored)
                    _finish_score_status("cancelled", f"Stopped after scoring {scored} jobs")
                    state.push_notification("score", "Batch Scoring Stopped", f"Stopped after scoring {scored} jobs", "info")
                    await db.log_activity("batch_score_complete", f"{verb} {scored} jobs (cancelled)")
                    return
                if limit is not None and scored >= limit:
                    break
                current = f"{job.get('title') or 'Untitled'} · {job.get('company') or ''}".rstrip(" ·")
                progress = f"Scoring job {min(processed + 1, total)} of {total}..." if total else f"Scoring job {processed + 1}..."
                state.score_status.update(current=current, detail=progress)
                try:
                    score, reasoning, match_report = await ai_engine.score_job_fit(job, profile, cfg)
                    await db.update_job_score(job["id"], score, reasoning, match_report)
                    if cfg.get("salary_estimator", {}).get("auto_on_ingest", True) and cfg.get("salary_estimator", {}).get("market_compare_on_score", True):
                        await _maybe_estimate_salary(job, cfg)
                    scored += 1
                except Exception:
                    logger.exception("Failed to score %s", job.get("title"))
                processed += 1
                state.score_status.update(done=processed)

        if state.cancel_score.is_set():
            _finish_score_status("cancelled", f"Stopped after scoring {scored} jobs")
        else:
            _finish_score_status("done", f"{verb} {scored} jobs")
        await db.log_activity("batch_score_complete", f"{verb} {scored} jobs")
        state.push_notification("score", "Batch Scoring Complete", f"{verb} {scored} jobs", "success")
    except asyncio.CancelledError:
        _finish_score_status("cancelled", "Batch scoring was stopped by user")
        state.push_notification("score", "Batch Scoring Stopped", "Batch scoring was stopped by user", "info")
    except Exception:
        logger.exception("Batch scoring failed")
        _finish_score_status("error", "Batch scoring failed — check server logs")
        state.push_notification("score", "Batch Scoring Failed", "An error occurred during batch scoring", "error")


async def _bg_estimate_salaries_batch(limit: Optional[int] = None):
    """Pull market salary estimates for jobs that don't have one yet.

    Mirrors the Score Jobs batch UX — same limit selector + stop button.
    Skips jobs that already have an estimate so re-running is cheap and
    idempotent. Each lookup is gated by salary_estimator.enabled.
    """
    state.cancel_estimate_salaries.clear()
    estimated = 0
    skipped = 0
    try:
        cfg = state.load_config()
        if not cfg.get("salary_estimator", {}).get("enabled", True):
            state.push_notification(
                "estimate_salaries", "Salary Estimator Disabled",
                "Enable salary_estimator in config or via Settings to run this.",
                "warning",
            )
            return

        jobs = await db.get_jobs_missing_salary_estimate(limit=limit)
        if not jobs:
            state.push_notification(
                "estimate_salaries", "No Jobs to Estimate",
                "Every job already has a market salary estimate.",
                "info",
            )
            return

        total = len(jobs)
        logger.info("Estimate-salaries batch: %d job(s) targeted (limit=%s)", total, limit)

        # Graceful-failure thresholds: stop if the same error happens many
        # times in a row (likely a config / network / quota issue we can't
        # recover from inside this run).
        MAX_CONSECUTIVE_FAILURES = 10
        consecutive_failures = 0
        first_failure_logged = False
        quota_exhausted = False

        for i, job in enumerate(jobs):
            if state.cancel_estimate_salaries.is_set():
                logger.info("Estimate-salaries batch cancelled after %d jobs", estimated)
                state.push_notification(
                    "estimate_salaries", "Salary Estimate Stopped",
                    f"Stopped after estimating {estimated}/{total} jobs ({skipped} skipped)",
                    "info",
                )
                await db.log_activity(
                    "estimate_salaries_complete",
                    f"Estimated {estimated}/{total} jobs (cancelled, {skipped} skipped)",
                )
                return
            try:
                payload = await salary_estimator.estimate_salary(job, cfg)
                if payload:
                    await db.update_job_estimated_salary(job["id"], payload)
                    estimated += 1
                else:
                    skipped += 1
                consecutive_failures = 0
            except salary_estimator.QuotaExceeded as e:
                logger.warning(
                    "Estimate-salaries: external quota exhausted at %d/%d (%s) — stopping",
                    i, total, e,
                )
                quota_exhausted = True
                break
            except salary_estimator.ResourceExhausted as e:
                logger.error(
                    "Estimate-salaries: process FDs exhausted at %d/%d (%s) — stopping",
                    i, total, e,
                )
                state.push_notification(
                    "estimate_salaries", "Salary Estimate Stopped — Server Needs Restart",
                    f"The server process ran out of file descriptors at {estimated}/{total}. "
                    f"Estimates already saved are intact. Please restart the server to recover.",
                    "error",
                )
                # Don't bother trying to log to the DB — that's also broken
                # right now. Bail immediately.
                return
            except Exception as e:
                consecutive_failures += 1
                skipped += 1
                if not first_failure_logged:
                    # Full traceback once so the underlying cause is visible,
                    # then a single line per subsequent failure.
                    logger.exception("Estimate-salaries: first failure at job %r", job.get("title"))
                    first_failure_logged = True
                else:
                    logger.warning(
                        "Estimate-salaries: failure %d at job %r — %s",
                        consecutive_failures, job.get("title"), e,
                    )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.error(
                        "Estimate-salaries: %d consecutive failures — stopping at %d/%d",
                        consecutive_failures, i + 1, total,
                    )
                    break

        # Pick the right summary based on how the loop exited.
        if quota_exhausted:
            title_msg = "Salary Estimate Stopped — Quota Reached"
            detail = (
                f"External API quota reached. Estimated {estimated}/{total} jobs "
                f"({skipped} skipped). Try again tomorrow when the quota resets."
            )
            level = "warning"
        elif consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            title_msg = "Salary Estimate Stopped — Persistent Errors"
            detail = (
                f"Stopped after {consecutive_failures} consecutive failures. "
                f"Estimated {estimated}/{total} jobs ({skipped} skipped). "
                f"Check server logs for the underlying cause."
            )
            level = "error"
        else:
            title_msg = "Salary Estimate Complete"
            detail = f"Estimated {estimated}/{total} jobs ({skipped} skipped)"
            level = "success"

        await db.log_activity("estimate_salaries_complete", detail)
        state.push_notification("estimate_salaries", title_msg, detail, level)
    except asyncio.CancelledError:
        state.push_notification(
            "estimate_salaries", "Salary Estimate Stopped",
            "Stopped by user", "info",
        )
    except Exception:
        logger.exception("Estimate-salaries batch failed")
        state.push_notification(
            "estimate_salaries", "Salary Estimate Failed",
            "An error occurred — check server logs", "error",
        )


async def _bg_detect_apply_types():
    """Classify apply_type for all 'unknown' jobs in the database."""
    state.cancel_detect_types.clear()
    state.detect_types_status = {"active": True, "processed": 0, "easy_apply": 0, "quick_apply": 0, "external": 0, "unknown": 0, "detail": "Scanning unclassified jobs..."}

    from backend.services.apply_type_detector import detect_all_apply_types

    def _on_progress(summary: dict) -> None:
        state.detect_types_status.update(summary, detail=f"Classified {summary['processed']} jobs...")

    try:
        summary = await detect_all_apply_types(
            cancel_event=state.cancel_detect_types,
            on_progress=_on_progress,
        )
        cancelled = state.cancel_detect_types.is_set()
        detail = (
            f"Stopped after {summary['processed']} jobs"
            if cancelled
            else f"Done — {summary['processed']} jobs classified"
        )
        state.detect_types_status.update(summary, active=False, detail=detail)
        await db.log_activity(
            "detect_apply_types",
            f"Classified {summary['processed']} jobs: "
            f"{summary['easy_apply']} easy_apply, {summary['quick_apply']} quick_apply, "
            f"{summary['external']} external, {summary['unknown']} unknown",
        )
        status_word = "info" if cancelled else "success"
        state.push_notification(
            "detect_apply_types",
            "Apply Type Detection " + ("Stopped" if cancelled else "Complete"),
            detail,
            status_word,
        )
    except asyncio.CancelledError:
        state.detect_types_status.update(active=False, detail="Cancelled")
        state.push_notification("detect_apply_types", "Apply Type Detection Stopped", "Stopped by user", "info")
    except Exception:
        logger.exception("Background apply-type detection failed")
        state.detect_types_status.update(active=False, detail="Failed — check server logs")
        state.push_notification("detect_apply_types", "Apply Type Detection Failed", "An error occurred", "error")


async def _bg_apply(app_id: str):
    """Execute auto-apply for a single approved application."""
    state.cancel_apply.clear()
    auto_apply.set_paused(False)
    state.current_apply_app_id = app_id
    job_title = app_id  # fallback for notifications before row is fetched
    job_company = ""
    try:
        if state.cancel_apply.is_set():
            return
        cfg = state.load_config()
        profile = cfg.get("profile", {})

        # Get application and its job
        apps_db = await db._get_db()
        try:
            cursor = await apps_db.execute(
                """SELECT a.*, j.id as j_id, j.title, j.company, j.url, j.source, j.description
                   FROM applications a JOIN jobs j ON j.id = a.job_id
                   WHERE a.id = ?""",
                (app_id,),
            )
            row = await cursor.fetchone()
        finally:
            await apps_db.close()

        if not row:
            logger.warning("Application %s not found", app_id)
            return

        row = dict(row)
        job_title = row["title"]
        job_company = row["company"]
        job_data = {
            "id": row["j_id"],
            "title": row["title"],
            "company": row["company"],
            "url": row["url"],
            "source": row["source"],
            "description": row.get("description", ""),
        }
        app_data = dict(row)

        # Set active session early so /api/assist/content can serve content
        # even during the auto-apply attempt (sidebar page may load before failure).
        _resume_text_early = app_data.get("resume_content") or ""
        _cl_text_early = app_data.get("cover_letter_content") or ""
        applicant_assist._active_session = {
            "job_id": row["j_id"],
            "resume_path": state.RESUMES_DIR / f"{row['j_id']}_resume.docx",
            "cover_letter_path": state.RESUMES_DIR / f"{row['j_id']}_cover_letter.docx",
            "resume_text": _resume_text_early,
            "cover_letter_text": _cl_text_early,
        }

        # ── Attempt counter guard ──────────────────────────────────────────
        _MAX_AUTO_APPLY_ATTEMPTS = 3
        _current_attempts = await db.get_apply_attempts(app_id)
        if _current_attempts >= _MAX_AUTO_APPLY_ATTEMPTS:
            skip_msg = (
                f"Auto-apply has failed {_current_attempts} time(s) for this job. "
                f"Skipping auto-apply — please apply manually."
            )
            logger.warning("Skipping auto-apply for app %s: %s", app_id, skip_msg)
            await db.update_application_status(app_id, "manual", error_message=skip_msg)
            await db.update_job_status(row["j_id"], "manual")
            await db.log_activity("manual_apply_needed", skip_msg, row["j_id"])
            state.push_notification(
                "apply", "Manual Apply Required",
                f"{row['title']} at {row['company']} — auto-apply failed {_current_attempts}x, please apply manually",
                "warning",
            )
            return

        await db.update_application_status(app_id, "applying")
        await db.log_activity("applying", f"Applying to {row['title']} at {row['company']}", row["j_id"])

        use_browser_use = cfg.get("auto_apply", {}).get("use_browser_use", False)
        # Indeed must always use orchestrator (persistent profile required for auth)
        job_url = job_data.get("url", "")
        is_indeed = "indeed.com" in job_url.lower()

        if use_browser_use and not is_indeed:
            from . import browser_use_agent as _bua
            result = await _bua.run_browser_use_apply(
                job=job_data, application=app_data, profile=profile, config=cfg
            )
        else:
            result = await auto_apply.auto_apply_job(job_data, app_data, profile, config=cfg)

        if result["success"]:
            await db.update_application_status(app_id, result.get("db_status", "applied"), error_message=result.get("message", "Application submitted successfully"))
            await db.update_job_status(row["j_id"], "applied")
            await db.log_activity("applied", f"Applied to {row['title']} at {row['company']}", row["j_id"])
            state.push_notification("apply", "Application Submitted", f"Applied to {row['title']} at {row['company']}", "success")
        else:
            # Increment attempt counter on any failure (not on user-cancelled runs)
            _new_attempt_count = await db.increment_apply_attempts(app_id)
            error_msg = result.get("message", "Auto-apply failed")
            _attempt_suffix = f" (attempt {_new_attempt_count}/{_MAX_AUTO_APPLY_ATTEMPTS})"
            await db.update_application_status(app_id, result.get("db_status", "failed"), error_message=error_msg + _attempt_suffix)
            await db.update_job_status(row["j_id"], "manual")
            await db.log_activity("manual_apply_needed", f"{error_msg} — apply manually: {row['url']}", row["j_id"])
            state.push_notification(
                "apply", "Manual Apply Needed",
                f"{row['title']} at {row['company']} — {error_msg[:120]}",
                "info",
            )

    except asyncio.CancelledError:
        # Only force-stop reaches here now (pause no longer cancels the task).
        await db.update_application_status(app_id, "failed", error_message="Force stopped by user")
        state.push_notification("apply", "Apply Stopped", f"Force stopped: {job_title} at {job_company}", "warning")
        raise  # re-raise so asyncio marks the task as cancelled
    except Exception:
        logger.exception("Auto-apply failed for application %s", app_id)
        await db.update_application_status(app_id, "manual", error_message="Unexpected error — apply manually")
        state.push_notification("apply", "Manual Apply Needed", f"Auto-apply failed for {app_id} — apply manually", "info")
    finally:
        state.current_apply_app_id = None


async def _bg_refetch_descriptions():
    """Re-run LinkedIn detail fetch against jobs whose description is empty."""
    import aiohttp
    from .job_sources.linkedin import _fetch_job_detail

    state.cancel_refetch.clear()
    try:
        rows = await db.get_jobs_missing_descriptions(source="linkedin")
        state.refetch_status = {
            "active": True,
            "processed": 0,
            "total": len(rows),
            "updated": 0,
            "failed": 0,
            "detail": f"Refetching {len(rows)} LinkedIn descriptions...",
        }
        if not rows:
            state.refetch_status.update(active=False, detail="No LinkedIn jobs with empty descriptions")
            return

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

        sem = asyncio.Semaphore(4)
        updated = 0
        failed = 0
        processed = 0

        async with aiohttp.ClientSession() as session:
            async def worker(row):
                nonlocal updated, failed, processed
                if state.cancel_refetch.is_set():
                    return
                async with sem:
                    if state.cancel_refetch.is_set():
                        return
                    job_dict = {"url": row["url"]}
                    try:
                        await _fetch_job_detail(session, job_dict, headers)
                        if job_dict.get("description"):
                            ok = await db.backfill_job_detail(row["id"], job_dict)
                            if ok:
                                updated += 1
                            else:
                                failed += 1
                        else:
                            failed += 1
                    except Exception:
                        logger.exception("Refetch failed for job %s", row["id"])
                        failed += 1
                    finally:
                        processed += 1
                        state.refetch_status.update(
                            processed=processed,
                            updated=updated,
                            failed=failed,
                            detail=f"Refetched {processed}/{len(rows)} (updated {updated}, failed {failed})",
                        )

            await asyncio.gather(*(worker(r) for r in rows))

        cancelled = state.cancel_refetch.is_set()
        state.refetch_status.update(
            active=False,
            detail=("Cancelled — " if cancelled else "") + f"updated {updated}, failed {failed} of {len(rows)}",
        )
    except Exception:
        logger.exception("Refetch descriptions failed")
        state.refetch_status.update(active=False, detail="Refetch failed — check server logs")
