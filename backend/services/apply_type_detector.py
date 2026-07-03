"""
apply_type_detector.py — Bulk apply-type classification service.

Queries every job in the database whose apply_type is still 'unknown',
calls the appropriate source-specific detector (synchronous, no network),
and writes the result back.  Returns a summary dict when finished.
"""

import asyncio
import logging
from typing import Callable, Optional

from backend import database as db
from backend.job_sources.linkedin import detect_linkedin_easy_apply
from backend.job_sources.indeed import detect_indeed_quick_apply
from backend.job_sources.usajobs import detect_usajobs_apply_type
from backend.job_sources.greenhouse import detect_greenhouse_apply_type, detect_lever_apply_type

logger = logging.getLogger(__name__)

# Maps job.source → detector callable.  Each detector takes a job dict and
# returns one of: 'easy_apply', 'quick_apply', 'external', 'unknown'.
_DETECTORS: dict[str, Callable[[dict], str]] = {
    "linkedin":   detect_linkedin_easy_apply,
    "indeed":     detect_indeed_quick_apply,
    "usajobs":    detect_usajobs_apply_type,
    "greenhouse": detect_greenhouse_apply_type,
    "lever":      detect_lever_apply_type,
}

SummaryDict = dict  # {"processed": int, "easy_apply": int, "quick_apply": int, "external": int, "unknown": int}


async def detect_all_apply_types(
    cancel_event: Optional[asyncio.Event] = None,
    on_progress: Optional[Callable[[SummaryDict], None]] = None,
) -> SummaryDict:
    """Classify the apply_type for every job currently marked 'unknown'.

    Args:
        cancel_event: An asyncio.Event that, when set, stops processing after
            the current job.  The partial summary is still returned.
        on_progress: Optional callback invoked after each job is classified.
            Receives a copy of the running summary dict.

    Returns a summary dict::

        {
            "processed":   <total jobs examined>,
            "easy_apply":  <count classified as easy_apply>,
            "quick_apply": <count classified as quick_apply>,
            "external":    <count classified as external>,
            "unknown":     <count that remained unknown (no detector or no URL)>,
        }
    """
    summary: SummaryDict = {
        "processed": 0,
        "easy_apply": 0,
        "quick_apply": 0,
        "external": 0,
        "unknown": 0,
    }

    jobs = await db.get_unclassified_jobs()
    logger.info("apply_type_detector: %d unclassified jobs to process", len(jobs))

    for job in jobs:
        if cancel_event and cancel_event.is_set():
            logger.info("apply_type_detector: cancelled after %d jobs", summary["processed"])
            break

        source = job.get("source", "")
        detector = _DETECTORS.get(source)
        apply_type = detector(job) if detector else "unknown"

        await db.update_job_apply_type(job["id"], apply_type)

        summary["processed"] += 1
        summary[apply_type] = summary.get(apply_type, 0) + 1

        if on_progress:
            on_progress(dict(summary))

    logger.info(
        "apply_type_detector: done — %d processed, %d easy_apply, %d quick_apply, "
        "%d external, %d unknown",
        summary["processed"], summary["easy_apply"], summary["quick_apply"],
        summary["external"], summary["unknown"],
    )
    return summary
