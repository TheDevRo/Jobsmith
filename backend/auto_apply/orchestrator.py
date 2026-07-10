"""
auto_apply/orchestrator.py — Central coordinator for the auto-apply pipeline.

Entry point: run_apply(job, application, profile, config) → dict

Responsibilities
----------------
1. Build domain-specific objects (UserProfile, JobApplicationRequest) from
   the raw dicts that main.py passes in.
2. Enforce rate limits (max daily applications, per-domain limits).
3. Choose the right ApplyMode (autofill vs submit) based on config and
   the job's domain whitelist.
4. Select the correct ATS adapter (adapter.matches() check, in priority order).
5. Launch BrowserController and delegate to the adapter.
6. Translate ApplyResult → legacy dict expected by main.py._bg_apply().
7. Return structured result.

Config keys read (all under auto_apply:)
-----------------------------------------
  mode: autofill | submit          (default: autofill)
  submit_whitelist: [domain, ...]  (domains allowed for mode=submit)
  max_daily_applications: N        (default: 20)
  per_domain_rate_limit: N         (default: 5)
  review_required_rules:
    unknown_ats: bool              (flag non-whitelisted sites for review)
    min_confidence: float          (0.0-1.0; unused here, enforced in adapters)
  headless: bool                   (default: true)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Optional

from .adapters import ALL_ADAPTERS
from .browser_controller import BrowserController
from .llm_client import LLMClient
from .logger import AutoApplyLogger
from ..paths import project_root
from .models import (
    ApplyMode,
    ApplyResult,
    ApplyStatus,
    JobApplicationRequest,
    UserProfile,
    validate_config,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level rate-limit counters (in-memory; reset on server restart).
# For persistent limits, the database `activity_log` is the source of truth.
# ---------------------------------------------------------------------------

_daily_count: dict[str, int] = defaultdict(int)   # keyed by ISO date
_domain_count: dict[str, int] = defaultdict(int)  # keyed by "date:domain"

# Pause / force-stop state shared with main.py
# _pause_event: set = running (not paused); clear = paused (workers block on wait())
_pause_event: asyncio.Event = asyncio.Event()
_pause_event.set()  # start unpaused
_active_ctrl: Optional[BrowserController] = None

# Run-ID guard: each run_apply() call gets a unique ID so force_stop() can
# distinguish "stop this run" from "stop a run that already finished".
_run_id: str = ""

# Event set by force_stop() so wait_if_paused() can raise CancelledError
_force_stop_event: asyncio.Event = asyncio.Event()

# Assist handoff timeout: if user leaves browser open too long, auto-close.
_ASSIST_TIMEOUT_SEC: int = 30 * 60  # 30 minutes

# Live progress dict: updated during run_apply(), polled by /api/applications/{id}/apply-progress
_apply_progress: dict = {}


def get_apply_progress() -> dict:
    """Return the current apply progress snapshot (empty dict if idle)."""
    return dict(_apply_progress)


def _set_progress(**kwargs: object) -> None:
    """Update _apply_progress in place with any provided keys."""
    _apply_progress.update(kwargs)


# ---------------------------------------------------------------------------
# Public API (called by main.py and __init__.py)
# ---------------------------------------------------------------------------

async def run_apply(
    job: dict,
    application: dict,
    profile: dict,
    config: dict,
) -> dict:
    """
    Top-level async entry point.

    Parameters match the existing signature of auto_apply.auto_apply_job() so
    main.py requires zero changes.

    Returns
    -------
    Legacy-shaped dict:
      { success, message, screenshot_path, manual_url, block_reason }
    """
    global _run_id, _active_ctrl, _apply_progress
    # Defense-in-depth single-flight guard: this module drives the browser via
    # singletons (_active_ctrl/_apply_progress), so a second concurrent run
    # would stomp the live one. The API layer rejects duplicate triggers with
    # 409, but guard here too in case a caller bypasses it. Do this before
    # touching _run_id/_force_stop_event so we don't disturb the live run.
    if _apply_progress.get("active"):
        logger.warning("run_apply called while an apply is already active — refusing")
        return {
            "success":         False,
            "message":         "An application is already being applied",
            "screenshot_path": None,
            "manual_url":      job.get("url", ""),
            "block_reason":    "already_running",
        }
    _current_run_id = str(uuid.uuid4())
    _run_id = _current_run_id
    _force_stop_event.clear()  # reset for this new apply run
    import sys as _sys
    _IS_TEST = "pytest" in _sys.modules
    _apply_start = time.time()

    # ── Config validation ──────────────────────────────────────────────────
    config_errors = validate_config(config)
    if config_errors:
        msg = "Cannot apply — config errors: " + "; ".join(config_errors)
        logger.error(msg)
        return {
            "success":         False,
            "message":         msg,
            "screenshot_path": None,
            "manual_url":      job.get("url", ""),
            "block_reason":    "config_error",
        }

    aa_cfg = config.get("auto_apply", {})

    # ── Build typed objects ────────────────────────────────────────────────
    user_profile = UserProfile.from_config(config)
    job_req      = _build_job_request(job, application)

    # ── Rate limits ────────────────────────────────────────────────────────
    rate_check = _check_rate_limits(job_req.url, aa_cfg)
    if not rate_check["ok"]:
        logger.warning("Rate limit hit for %s: %s", job_req.url, rate_check["reason"])
        return {
            "success":         False,
            "message":         rate_check["reason"],
            "screenshot_path": None,
            "manual_url":      job_req.url,
            "block_reason":    "rate_limit",
        }

    # ── Mode selection ─────────────────────────────────────────────────────
    mode = _choose_mode(job_req.url, aa_cfg)
    logger.info(
        "Orchestrator: applying to %s at %s (mode=%s)",
        job_req.title, job_req.company, mode.value,
    )
    _apply_progress = {
        "active": True,
        "application_id": job_req.application_id,
        "job_id": job_req.job_id,
        "step": "starting",
        "adapter": "",
        "fields_filled": 0,
        "fields_total": 0,
        "elapsed_seconds": 0,
    }

    # ── Adapter selection ──────────────────────────────────────────────────
    # We peek at the URL to pick the adapter — no browser open yet.
    adapter = _pick_adapter(job_req.url)
    logger.info("Orchestrator: selected adapter=%s", adapter.name)
    _set_progress(adapter=adapter.name, step="navigating")

    # ── Session paths — resolved per adapter ──────────────────────────────
    profile_dir: Optional[Path] = None
    storage_state_path: Optional[Path] = None
    _session_warning: Optional[str] = None

    if adapter.name == "indeed":
        # Use a persistent Chromium profile to avoid Cloudflare challenges.
        # Additionally inject any saved storage_state cookies so the session
        # is active from the first navigation.
        from . import INDEED_CHROME_PROFILE_DIR, INDEED_SESSION_PATH  # local import
        _indeed_sentinel = INDEED_CHROME_PROFILE_DIR / "login_success.json"
        if INDEED_CHROME_PROFILE_DIR.is_dir() and _indeed_sentinel.exists():
            profile_dir = INDEED_CHROME_PROFILE_DIR
            if INDEED_SESSION_PATH.exists():
                storage_state_path = INDEED_SESSION_PATH
        else:
            logger.warning(
                "Indeed auto-apply: no session at %s — user may see login wall",
                INDEED_CHROME_PROFILE_DIR,
            )
    elif adapter.name == "linkedin":
        candidate = _linkedin_profile_dir()
        if candidate and candidate.is_dir():
            sentinel = candidate / "login_success.json"
            if sentinel.exists():
                profile_dir = candidate
            else:
                _session_warning = (
                    f"LinkedIn session directory exists but login was never "
                    f"completed — applying without session ({candidate})"
                )
        else:
            _expected = (
                candidate
                or project_root()
                / "data" / "linkedin_chrome_profile"
            )
            _session_warning = (
                f"LinkedIn session directory not found — applying without "
                f"session, expect authwall (expected: {_expected})"
            )
        if _session_warning:
            logger.warning(_session_warning)

    # ── Auto-apply logger ──────────────────────────────────────────────────
    domain = _extract_domain(job_req.url)
    log = AutoApplyLogger(
        job_id   = job_req.job_id,
        app_id   = job_req.application_id,
        site     = domain,
        adapter  = adapter.name,
        mode     = mode.value,
    )
    _TIER_1_ADAPTERS = {"linkedin", "greenhouse", "lever", "adzuna"}
    tier = 1 if adapter.name in _TIER_1_ADAPTERS else 2
    log.info(
        "apply_start",
        job_url          = job_req.url,
        adapter_selected = adapter.name,
        tier             = tier,
        mode             = mode.value,
    )
    if _session_warning:
        log.warning(_session_warning)

    # ── Launch browser + run adapter ───────────────────────────────────────
    result: ApplyResult
    ctrl = BrowserController(config, profile_dir=profile_dir, storage_state_path=storage_state_path)
    _active_ctrl = ctrl

    try:
        async with ctrl:
            # Block here (browser open) if paused before we even navigate.
            # Do NOT return early — that would close the browser context.
            await _pause_check()
            await _check_cancelled()

            await ctrl.navigate(job_req.url)

            # Let adapter re-check with page text for extra confidence
            page_text_snippet = (await ctrl.page_text())[:500]
            if not adapter.matches(job_req.url, page_text_snippet):
                # URL-based selection should be right, but log any mismatch
                logger.warning(
                    "Adapter %s matches() returned False on page text — proceeding anyway",
                    adapter.name,
                )

            # Second pause point: after navigation, before adapter runs.
            # The browser is now sitting on the job page — a natural point for
            # the user to take over manually if paused.
            await _pause_check()
            await _check_cancelled()

            from .utils.browser_helpers import wait_if_paused
            await wait_if_paused()

            llm = LLMClient(config)

            # Catch adapter-level exceptions here (inside the async with block)
            # so the browser stays open and the assist handoff can still fire.
            _adapter_exc: Optional[Exception] = None
            _set_progress(step="filling_fields", elapsed_seconds=int(time.time() - _apply_start))
            try:
                result = await adapter.apply(ctrl, user_profile, job_req, llm, mode, log)
            except asyncio.CancelledError:
                raise  # let force_stop() propagate normally
            except Exception as _exc:
                _adapter_exc = _exc
                logger.exception("Orchestrator: adapter error: %s", _exc)
                result = ApplyResult(
                    success=False,
                    status=ApplyStatus.FAILED,
                    message=(
                        f"Auto-apply failed in {adapter.name} adapter "
                        f"({job_req.company} — {job_req.title}): {_exc}"
                    ),
                    manual_url=job_req.url,
                    log_entries=log.entries,
                )

            # ── Low-confidence override (submit mode only) ────────────────
            if _adapter_exc is None and mode is ApplyMode.SUBMIT and result.status is ApplyStatus.SUBMITTED:
                low_conf_fields = [
                    e["field_id"]
                    for e in result.log_entries
                    if e.get("level") == "field" and e.get("confidence", 1.0) < 0.60
                ]
                if low_conf_fields:
                    log.warning(
                        "Low-confidence fields detected — overriding SUBMITTED to NEEDS_REVIEW",
                        low_confidence_fields=low_conf_fields,
                    )
                    result.status  = ApplyStatus.NEEDS_REVIEW
                    result.success = False
                    result.message = (
                        "Flagged for review: low-confidence fields: "
                        + ", ".join(low_conf_fields)
                    )
                    result.log_entries = log.entries

            # ── Assist handoff on failure ─────────────────────────────────
            # When auto-apply fails, inject the sidebar into the existing browser
            # window and wait for the user to close it (manual takeover mode).
            # Skipped in tests to avoid hanging on browser close.
            if not result.success and not _IS_TEST:
                try:
                    from .. import applicant_assist as _aa
                    backend_url = f"http://localhost:{config.get('server', {}).get('port', 8888)}"
                    sidebar_script = _aa._build_sidebar_script(backend_url)
                    # Inject into the current page immediately
                    await ctrl.page.evaluate(sidebar_script)
                    # Also register for future navigations in this session
                    if ctrl._ctx is not None:
                        await ctrl._ctx.add_init_script(sidebar_script)
                    # Expose live page + job so Scan/Autofill buttons in the sidebar work
                    _aa._active_page = ctrl.page
                    _aa._active_job = {
                        "id": job_req.job_id,
                        "title": job_req.title,
                        "company": job_req.company,
                        "url": job_req.url,
                        "description": job_req.description,
                    }
                    log.info(
                        "assist_handoff",
                        message="Auto-apply failed — sidebar injected, waiting for user to close browser",
                    )
                    # Block here until the user closes the browser window.
                    # Poll is_connected() rather than wait_for_event("disconnected", timeout=0)
                    # because timeout=0 in Playwright Python means 0ms (not "no timeout").
                    browser = ctrl._browser
                    _assist_start = time.time()
                    if browser is not None:
                        while True:
                            try:
                                if not browser.is_connected():
                                    break
                                elapsed = time.time() - _assist_start
                                if elapsed > _ASSIST_TIMEOUT_SEC:
                                    logger.warning(
                                        "assist_handoff: browser open > 30 min — auto-closing"
                                    )
                                    break
                                await asyncio.sleep(0.5)
                            except asyncio.CancelledError:
                                raise
                            except Exception as _loop_e:
                                log.warning(
                                    "assist_handoff_loop_error", error=str(_loop_e)
                                )
                                break
                    elif ctrl._ctx is not None:
                        while True:
                            try:
                                if not ctrl._ctx.pages:
                                    break
                                elapsed = time.time() - _assist_start
                                if elapsed > _ASSIST_TIMEOUT_SEC:
                                    logger.warning(
                                        "assist_handoff: context open > 30 min — auto-closing"
                                    )
                                    break
                                await asyncio.sleep(0.5)
                            except asyncio.CancelledError:
                                raise
                            except Exception as _loop_e:
                                log.warning(
                                    "assist_handoff_loop_error", error=str(_loop_e)
                                )
                                break
                except asyncio.CancelledError:
                    raise  # let force_stop() propagate normally
                except Exception as _e:
                    log.warning("assist_handoff_error", error=str(_e))
                finally:
                    # Clear page/job state set above so stale references don't linger
                    _aa._active_page = None
                    _aa._active_job  = None

    except asyncio.CancelledError:
        logger.info("Orchestrator: task cancelled")
        return _cancelled_result(job_req, "cancelled")
    except Exception as exc:
        logger.exception("Orchestrator: unhandled error: %s", exc)
        return {
            "success":         False,
            "message":         f"Auto-apply error: {exc}",
            "screenshot_path": None,
            "manual_url":      job_req.url,
            "block_reason":    "error",
        }
    finally:
        _active_ctrl = None
        _apply_progress.clear()  # mark idle

    # ── Update rate-limit counters ─────────────────────────────────────────
    if result.success:
        today = date.today().isoformat()
        _daily_count[today] += 1
        _domain_count[f"{today}:{domain}"] += 1

    return result.to_legacy_dict()


def set_paused(value: bool) -> None:
    if value:
        _pause_event.clear()  # clear = paused; coroutines block on .wait()
        logger.info("Auto-apply PAUSED — browser kept alive, all actions halted")
    else:
        _pause_event.set()    # set = running; .wait() returns immediately
        logger.info("Auto-apply RESUMED — continuing automation")


def is_paused() -> bool:
    return not _pause_event.is_set()


async def _pause_check() -> None:
    """Block here (keeping the browser open) while the pause event is cleared.

    Awaiting _pause_event.wait() suspends the coroutine atomically until
    set_paused(False) calls _pause_event.set().  No polling loop needed.
    """
    await _pause_event.wait()


async def force_stop() -> None:
    """Immediately close the active browser for the current run, if any.

    Uses the run-ID guard: captures the current _run_id before any await so
    that if a new run starts while we're closing, we don't accidentally close
    the new run's browser.
    """
    global _active_ctrl
    captured_run_id = _run_id  # capture synchronously before any await
    _force_stop_event.set()    # unblock any wait_if_paused() waiters
    ctrl = _active_ctrl
    if ctrl is not None and _run_id == captured_run_id:
        _active_ctrl = None    # clear before close to prevent double-close
        try:
            await ctrl.close()
        except Exception as exc:
            logger.debug("force_stop close error (ignored): %s", exc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_job_request(job: dict, application: dict) -> JobApplicationRequest:
    """Assemble a JobApplicationRequest from raw dicts."""
    resume_path: Optional[str] = None
    raw_path = application.get("tailored_resume_path") or application.get("resume_path")
    if raw_path:
        p = Path(raw_path)
        if not p.is_absolute():
            p = project_root() / raw_path
        if p.exists():
            resume_path = str(p)

    cover_path: Optional[str] = None
    raw_cl = application.get("tailored_cover_letter_path")
    if raw_cl:
        p2 = Path(raw_cl)
        if not p2.is_absolute():
            p2 = project_root() / raw_cl
        if p2.exists():
            cover_path = str(p2)

    return JobApplicationRequest(
        job_id         = str(job.get("id", "")),
        application_id = str(application.get("id", "")),
        title          = job.get("title", ""),
        company        = job.get("company", ""),
        url            = job.get("url", ""),
        description    = job.get("description", ""),
        resume_path    = resume_path,
        cover_letter_path = cover_path,
    )


def _check_rate_limits(url: str, aa_cfg: dict) -> dict:
    today  = date.today().isoformat()
    domain = _extract_domain(url)

    daily_max   = int(aa_cfg.get("max_daily_applications", 20))
    domain_max  = int(aa_cfg.get("per_domain_rate_limit", 5))

    daily_used  = _daily_count.get(today, 0)
    domain_used = _domain_count.get(f"{today}:{domain}", 0)

    if daily_used >= daily_max:
        return {
            "ok":     False,
            "reason": f"Daily limit reached ({daily_used}/{daily_max} applications today)",
        }
    if domain_max > 0 and domain_used >= domain_max:
        return {
            "ok":     False,
            "reason": f"Per-domain limit reached for {domain} ({domain_used}/{domain_max})",
        }
    return {"ok": True, "reason": ""}


def _choose_mode(url: str, aa_cfg: dict) -> ApplyMode:
    """Determine whether to autofill-only or auto-submit, based on config."""
    configured_mode = aa_cfg.get("mode", "autofill").lower()
    if configured_mode != "submit":
        return ApplyMode.AUTOFILL

    whitelist: list[str] = aa_cfg.get("submit_whitelist", [])
    domain = _extract_domain(url)
    if any(allowed in domain for allowed in whitelist):
        return ApplyMode.SUBMIT

    # Not on whitelist — fall back to autofill
    logger.info(
        "Domain %s not in submit_whitelist — using autofill mode", domain
    )
    return ApplyMode.AUTOFILL


def _pick_adapter(url: str):
    """Return the first adapter whose matches(url) returns True."""
    for adapter in ALL_ADAPTERS:
        if adapter.matches(url, ""):
            return adapter
    # GenericAdapter.matches() always returns True, so this is unreachable.
    return ALL_ADAPTERS[-1]


def _extract_domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc.lower()
    except Exception:
        return url[:50]


def _linkedin_profile_dir() -> Optional[Path]:
    """Return the Chromium persistent profile directory for LinkedIn."""
    return project_root() / "data" / "linkedin_chrome_profile"


def _cancelled_result(job_req: JobApplicationRequest, reason: str) -> dict:
    return {
        "success":         False,
        "message":         f"Apply {reason} — resume manually at {job_req.url}",
        "screenshot_path": None,
        "manual_url":      job_req.url,
        "block_reason":    reason,
    }


async def _check_cancelled() -> None:
    """Yield control so CancelledError can propagate if the task was cancelled."""
    await asyncio.sleep(0)
