"""
routers/settings.py — Config read/write, per-setting endpoints, first-run
onboarding, and dashboard stats/activity.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

from .. import app_state as state
from .. import database as db
from .. import ai_engine
from .. import resume_parser
from .. import linkedin_profile_import
from ..auto_apply import has_linkedin_session
from . import _auth

logger = logging.getLogger(__name__)

router = APIRouter()

# Shown instead of a stored secret when the caller isn't on this machine. It is
# a *display* value: POST /api/config strips any field still equal to it, so an
# untouched form field round-trips as "leave unchanged" rather than writing the
# mask into config.yaml. Clearing the field still clears the secret.
SECRET_MASK = "•" * 8

# (section, key) pairs never handed to an off-machine caller in the clear.
_SECRET_FIELDS = (
    ("profile", "workday_password"),
    ("profile", "ats_login_password"),
    ("ai", "api_key"),
    ("api_keys", "adzuna_app_key"),
    ("api_keys", "usajobs_api_key"),
)


def _mask_secrets(payload: dict) -> dict:
    """Replace stored secrets with SECRET_MASK (only where one is actually set)."""
    for section, key in _SECRET_FIELDS:
        if payload.get(section, {}).get(key):
            payload[section][key] = SECRET_MASK
    bls = payload.get("salary_estimator", {}).get("bls", {})
    if bls.get("api_key"):
        bls["api_key"] = SECRET_MASK
    return payload


def _strip_masked(section: Optional[dict]) -> Optional[dict]:
    """Drop keys the client echoed back untouched, so the mask is never saved."""
    if not section:
        return section
    return {k: v for k, v in section.items() if v != SECRET_MASK}


class ConfigUpdate(BaseModel):
    profile: Optional[dict] = None
    search: Optional[dict] = None
    auto_apply: Optional[dict] = None
    ai: Optional[dict] = None
    api_keys: Optional[dict] = None
    flaresolverr: Optional[dict] = None
    assist: Optional[dict] = None
    salary_estimator: Optional[dict] = None
    server: Optional[dict] = None


class HonestyLevelUpdate(BaseModel):
    honesty_level: str  # honest | tailored | embellished | fabricated


class ResumeStyleUpdate(BaseModel):
    resume_style: str  # executive | ledger | banner | compact | swiss


class ResumeAccentUpdate(BaseModel):
    resume_accent: str  # default | navy | burgundy | forest | plum | charcoal


class DocumentFormatUpdate(BaseModel):
    document_format: str  # docx | pdf


class AiEditModelTierUpdate(BaseModel):
    model_tier: str  # fast | strong


class MaxResumeExperienceEntriesUpdate(BaseModel):
    # null/None means "include all roles"
    max_resume_experience_entries: Optional[int] = None


class SalaryAutoIngestUpdate(BaseModel):
    auto_on_ingest: bool


class SuggestTitlesRequest(BaseModel):
    answers: dict = {}
    # The wizard passes its in-progress (unsaved) profile; when omitted the
    # saved config profile is used.
    profile: Optional[dict] = None


@router.get("/api/stats")
async def get_stats():
    return await db.get_stats()


@router.get("/api/analytics/outcomes")
async def get_outcome_analytics():
    """Post-apply outcome analytics: funnel counts + response-rate breakdowns."""
    return await db.get_outcome_analytics()


@router.get("/api/digest")
async def get_digest(limit: int = 5):
    """Today's shortlist — the few jobs actually worth applying to right now.

    Weighted by fit, freshness, salary and apply-effort, and by how often each
    source has actually replied to *you* (measured from the outcome history).
    Weights are overridable via config `pipeline.digest_weights`.
    """
    cfg = state.load_config()
    weights = cfg.get("pipeline", {}).get("digest_weights") or {}
    return await db.get_digest(limit=limit, weights=weights)


@router.get("/api/fit-breakdown")
async def get_fit_breakdown():
    return await db.get_fit_breakdown()


@router.get("/api/activity")
async def get_activity(limit: int = Query(20, ge=1, le=100)):
    return await db.get_activity(limit=limit)


@router.get("/api/ai/status")
async def ai_status():
    """Test AI connection and return status."""
    cfg = state.load_config()
    try:
        status = await asyncio.wait_for(ai_engine.test_connection(cfg), timeout=8)
        return {
            "ok": status.get("connected", False),
            "base_url": cfg.get("ai", {}).get("base_url", ""),
            "model": cfg.get("ai", {}).get("model", ""),
            "models": status.get("models", []),
            "error": status.get("error"),
        }
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Connection timed out (>8s)"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/api/config")
async def get_config(
    request: Request,
    x_jobsmith_token: str | None = Header(default=None),
):
    cfg = state.load_config()
    # Callers reaching us from off this machine (LAN / Docker) have already
    # proven they hold the token, but there is still no reason to hand them the
    # user's Workday password and API keys back in the clear — the settings form
    # only ever *writes* these. Loopback (the desktop/local case) is unchanged.
    _local = _auth.auth_disabled() or state.is_loopback_request(request)
    payload = {
        "search": cfg.get("search", {}),
        "auto_apply": cfg.get("auto_apply", {}),
        "ai": {
            "base_url": cfg.get("ai", {}).get("base_url", ""),
            "api_key": cfg.get("ai", {}).get("api_key", ""),
            "model": cfg.get("ai", {}).get("model", ""),
            "models": cfg.get("ai", {}).get("models", {}),
            "scoring_tier": cfg.get("ai", {}).get("scoring_tier", "strong"),
            "context_window": cfg.get("ai", {}).get("context_window", 8192),
        },
        "profile": {
            "full_name": cfg.get("profile", {}).get("full_name", ""),
            "middle_name": cfg.get("profile", {}).get("middle_name", ""),
            "email": cfg.get("profile", {}).get("email", ""),
            "phone": cfg.get("profile", {}).get("phone", ""),
            "location": cfg.get("profile", {}).get("location", ""),
            "street_address": cfg.get("profile", {}).get("street_address", ""),
            "street_address_2": cfg.get("profile", {}).get("street_address_2", ""),
            "city": cfg.get("profile", {}).get("city", ""),
            "state": cfg.get("profile", {}).get("state", ""),
            "zip_code": cfg.get("profile", {}).get("zip_code", ""),
            "desired_salary": cfg.get("profile", {}).get("desired_salary", ""),
            "linkedin": cfg.get("profile", {}).get("linkedin", ""),
            "summary": cfg.get("profile", {}).get("summary", ""),
            "skills": cfg.get("profile", {}).get("skills", []),
            "gender": cfg.get("profile", {}).get("gender", ""),
            "race_ethnicity": cfg.get("profile", {}).get("race_ethnicity", ""),
            "veteran_status": cfg.get("profile", {}).get("veteran_status", ""),
            "disability_status": cfg.get("profile", {}).get("disability_status", ""),
            "work_authorization": cfg.get("profile", {}).get("work_authorization", ""),
            "sponsorship_required": cfg.get("profile", {}).get("sponsorship_required", ""),
            "workday_email": cfg.get("profile", {}).get("workday_email", ""),
            "workday_password": cfg.get("profile", {}).get("workday_password", ""),
            "ats_login_password": cfg.get("profile", {}).get("ats_login_password", ""),
            "experience": cfg.get("profile", {}).get("experience", []),
            "education": cfg.get("profile", {}).get("education", []),
            "certifications": cfg.get("profile", {}).get("certifications", []),
            "references": cfg.get("profile", {}).get("references", []),
        },
        "linkedin": {},
        "api_keys": {
            "adzuna_app_id": cfg.get("api_keys", {}).get("adzuna_app_id", ""),
            "adzuna_app_key": cfg.get("api_keys", {}).get("adzuna_app_key", ""),
            "usajobs_email": cfg.get("api_keys", {}).get("usajobs_email", ""),
            "usajobs_api_key": cfg.get("api_keys", {}).get("usajobs_api_key", ""),
        },
        "flaresolverr": {
            "url": cfg.get("flaresolverr", {}).get("url", ""),
        },
        "assist": {
            "notification_sound": cfg.get("assist", {}).get("notification_sound", True),
        },
        "salary_estimator": {
            "enabled": cfg.get("salary_estimator", {}).get("enabled", True),
            "auto_on_ingest": cfg.get("salary_estimator", {}).get("auto_on_ingest", True),
            "bls": {
                "api_key": cfg.get("salary_estimator", {}).get("bls", {}).get("api_key", ""),
            },
        },
        "server": {
            "host": (cfg.get("server") or {}).get("host", "127.0.0.1"),
            "port": (cfg.get("server") or {}).get("port", 8888),
        },
    }
    return payload if _local else _mask_secrets(payload)


@router.post("/api/config")
async def update_config(body: ConfigUpdate):
    cfg = state.load_config()
    # A masked field means "the client never saw, and never touched, this
    # secret" — drop it so the mask can't overwrite the real value.
    body.profile = _strip_masked(body.profile)
    body.ai = _strip_masked(body.ai)
    body.api_keys = _strip_masked(body.api_keys)
    if body.salary_estimator and isinstance(body.salary_estimator.get("bls"), dict):
        body.salary_estimator["bls"] = _strip_masked(body.salary_estimator["bls"])
    if body.profile:
        cfg["profile"] = {**cfg.get("profile", {}), **body.profile}
    if body.search:
        cfg["search"] = {**cfg.get("search", {}), **body.search}
    if body.auto_apply:
        cfg["auto_apply"] = {**cfg.get("auto_apply", {}), **body.auto_apply}
    if body.ai:
        cfg["ai"] = {**cfg.get("ai", {}), **body.ai}
        # base_url/api_key changes alter the client cache key; drop stale
        # clients so their httpx pools don't leak FDs (see ai_engine).
        ai_engine.clear_clients()
    if body.api_keys:
        cfg["api_keys"] = {**cfg.get("api_keys", {}), **body.api_keys}
    if body.flaresolverr:
        cfg["flaresolverr"] = {**cfg.get("flaresolverr", {}), **body.flaresolverr}
    if body.assist is not None:
        cfg["assist"] = {**cfg.get("assist", {}), **body.assist}
    if body.salary_estimator is not None:
        existing = cfg.get("salary_estimator", {}) or {}
        merged = {**existing, **body.salary_estimator}
        # Deep-merge the nested 'bls' / 'adzuna' subsections so the GUI can
        # update just the API key without clobbering other settings.
        for sub in ("bls", "adzuna"):
            if sub in body.salary_estimator and isinstance(body.salary_estimator[sub], dict):
                merged[sub] = {**(existing.get(sub) or {}), **body.salary_estimator[sub]}
        cfg["salary_estimator"] = merged
    if body.server:
        # Only host/port are recognized; the bind takes effect on next restart
        # (uvicorn binds once at startup).
        allowed = {k: v for k, v in body.server.items() if k in ("host", "port")}
        if allowed:
            cfg["server"] = {**(cfg.get("server") or {}), **allowed}
    state.save_config(cfg)
    return {"message": "Config updated"}


# ---------------------------------------------------------------------------
# First-run onboarding
# ---------------------------------------------------------------------------
_EXAMPLE_NAME = "Jane Doe"
_EXAMPLE_EMAIL = "jane.doe@example.com"


def _needs_onboarding(cfg: dict) -> bool:
    """True when the install still looks fresh / unconfigured.

    Once the user finishes (or explicitly skips) the wizard we set
    `onboarding_complete`, which is the authoritative signal. Before that,
    a still-default profile (example name/email, or empty) also counts as
    needing setup so a bootstrapped config.yaml triggers the gate.
    """
    if cfg.get("onboarding_complete"):
        return False
    profile = cfg.get("profile", {}) or {}
    name = (profile.get("full_name") or "").strip()
    email = (profile.get("email") or "").strip()
    if not name or name == _EXAMPLE_NAME:
        return True
    if not email or email == _EXAMPLE_EMAIL:
        return True
    # Existing install upgraded mid-version: real profile, no flag yet —
    # treat as already onboarded so we don't pester returning users.
    return False


@router.get("/api/onboarding/status")
async def onboarding_status():
    """Whether to show the first-run wizard, plus a snapshot of AI status."""
    cfg = state.load_config()
    try:
        ai = await asyncio.wait_for(ai_engine.test_connection(cfg), timeout=8)
        ai_status = {
            "ok": ai.get("connected", False),
            "models": ai.get("models", []),
            "error": ai.get("error"),
        }
    except Exception as exc:
        ai_status = {"ok": False, "models": [], "error": str(exc)}
    return {
        "needs_onboarding": _needs_onboarding(cfg),
        "tour_complete": bool(cfg.get("tour_complete", False)),
        "ai": ai_status,
    }


@router.post("/api/onboarding/complete")
async def onboarding_complete():
    """Mark setup done so the gate does not reappear (Finish or Skip)."""
    cfg = state.load_config()
    cfg["onboarding_complete"] = True
    state.save_config(cfg)
    await db.log_activity("onboarding", "First-time setup completed")
    return {"onboarding_complete": True}


@router.post("/api/onboarding/tour-complete")
async def onboarding_tour_complete():
    """Mark the post-setup product tour as seen."""
    cfg = state.load_config()
    cfg["tour_complete"] = True
    state.save_config(cfg)
    await db.log_activity("tour", "Product tour completed")
    return {"tour_complete": True}


@router.post("/api/onboarding/tour-reset")
async def onboarding_tour_reset():
    """Reset the tour flag so it can be replayed."""
    cfg = state.load_config()
    cfg["tour_complete"] = False
    state.save_config(cfg)
    return {"tour_complete": False}


@router.post("/api/onboarding/parse-resume")
async def onboarding_parse_resume(
    file: Optional[UploadFile] = File(None),
    text: Optional[str] = Form(None),
):
    """Extract a partial profile from an uploaded résumé OR pasted text.

    Does not persist anything — the wizard shows the result for review.
    """
    resume_text = (text or "").strip()
    if file is not None:
        data = await file.read()
        if not data:
            raise HTTPException(status_code=400, detail="Uploaded file is empty")
        try:
            resume_text = resume_parser.extract_text(file.filename or "", data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if not resume_text:
        raise HTTPException(
            status_code=400,
            detail="Provide a résumé file or paste résumé text.",
        )

    cfg = state.load_config()
    result = await resume_parser.parse_resume(resume_text, cfg)
    return result


@router.post("/api/onboarding/import-linkedin")
async def onboarding_import_linkedin():
    """Scrape the user's own LinkedIn profile (saved session) and extract a
    partial profile with the local LLM.

    Same contract as parse-resume: does not persist anything — the wizard
    shows the result for review.
    """
    if not has_linkedin_session():
        raise HTTPException(
            status_code=409,
            detail="No LinkedIn session — sign in to LinkedIn first.",
        )
    cfg = state.load_config()
    try:
        result = await asyncio.wait_for(
            linkedin_profile_import.import_profile(cfg), timeout=240
        )
    except linkedin_profile_import.LinkedInSessionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except asyncio.TimeoutError:
        raise HTTPException(504, "LinkedIn import timed out — try again.")
    except Exception as exc:
        logger.exception("LinkedIn profile import failed")
        raise HTTPException(502, f"LinkedIn import failed: {exc}")
    await db.log_activity("linkedin_import", "LinkedIn profile imported for review")
    return result


@router.post("/api/settings/suggest-job-titles")
async def suggest_job_titles(body: SuggestTitlesRequest):
    """AI-recommend job titles to search for.

    Uses the saved profile (or the one supplied by the wizard) plus the
    user's answers to the direction questions. Returns
    {"titles": [{"title", "reason"}, ...]}.
    """
    cfg = state.load_config()
    profile = body.profile or cfg.get("profile", {}) or {}
    if not (profile.get("skills") or profile.get("experience") or profile.get("summary")):
        raise HTTPException(
            400,
            "Profile is empty — add a summary, skills, or experience first "
            "(Settings → Profile, or run the setup wizard).",
        )
    try:
        titles = await asyncio.wait_for(
            ai_engine.suggest_job_titles(profile, body.answers or {}, cfg),
            timeout=120,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "The AI took too long to respond — is a model loaded in LM Studio?")
    except Exception as exc:
        logger.exception("suggest_job_titles failed")
        raise HTTPException(502, f"AI request failed: {exc}")
    if not titles:
        raise HTTPException(502, "The AI returned no usable titles — try again")
    return {"titles": titles}


# ---------------------------------------------------------------------------
# Individual settings
# ---------------------------------------------------------------------------

@router.get("/api/settings/salary-estimator-auto-ingest")
async def get_salary_auto_ingest():
    cfg = state.load_config()
    val = cfg.get("salary_estimator", {}).get("auto_on_ingest", True)
    return {"auto_on_ingest": bool(val)}


@router.put("/api/settings/salary-estimator-auto-ingest")
async def set_salary_auto_ingest(body: SalaryAutoIngestUpdate):
    cfg = state.load_config()
    if "salary_estimator" not in cfg:
        cfg["salary_estimator"] = {}
    cfg["salary_estimator"]["auto_on_ingest"] = bool(body.auto_on_ingest)
    state.save_config(cfg)
    return {"auto_on_ingest": bool(body.auto_on_ingest)}


@router.get("/api/settings/honesty-level")
async def get_honesty_level():
    cfg = state.load_config()
    level = cfg.get("application_honesty", {}).get("honesty_level", "honest")
    return {"honesty_level": level}


@router.put("/api/settings/honesty-level")
async def set_honesty_level(body: HonestyLevelUpdate):
    if body.honesty_level not in state.VALID_HONESTY_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"honesty_level must be one of: {sorted(state.VALID_HONESTY_LEVELS)}",
        )
    cfg = state.load_config()
    if "application_honesty" not in cfg:
        cfg["application_honesty"] = {}
    cfg["application_honesty"]["honesty_level"] = body.honesty_level
    state.save_config(cfg)
    return {"honesty_level": body.honesty_level}


@router.get("/api/settings/resume-style")
async def get_resume_style():
    cfg = state.load_config()
    style = str(cfg.get("application_honesty", {}).get("resume_style", "ledger")).lower()
    # Configs written before the current lineup carry retired style names.
    style = state.LEGACY_RESUME_STYLES.get(style, style)
    if style not in state.VALID_RESUME_STYLES:
        style = "ledger"
    return {"resume_style": style}


@router.put("/api/settings/resume-style")
async def set_resume_style(body: ResumeStyleUpdate):
    if body.resume_style not in state.VALID_RESUME_STYLES:
        raise HTTPException(
            status_code=400,
            detail=f"resume_style must be one of: {sorted(state.VALID_RESUME_STYLES)}",
        )
    cfg = state.load_config()
    if "application_honesty" not in cfg:
        cfg["application_honesty"] = {}
    cfg["application_honesty"]["resume_style"] = body.resume_style
    state.save_config(cfg)
    return {"resume_style": body.resume_style}


@router.get("/api/settings/resume-accent")
async def get_resume_accent():
    cfg = state.load_config()
    accent = str(cfg.get("application_honesty", {}).get("resume_accent", "default")).lower()
    if accent not in state.VALID_RESUME_ACCENTS:
        accent = "default"
    return {"resume_accent": accent}


@router.put("/api/settings/resume-accent")
async def set_resume_accent(body: ResumeAccentUpdate):
    if body.resume_accent not in state.VALID_RESUME_ACCENTS:
        raise HTTPException(
            status_code=400,
            detail=f"resume_accent must be one of: {sorted(state.VALID_RESUME_ACCENTS)}",
        )
    cfg = state.load_config()
    if "application_honesty" not in cfg:
        cfg["application_honesty"] = {}
    cfg["application_honesty"]["resume_accent"] = body.resume_accent
    state.save_config(cfg)
    return {"resume_accent": body.resume_accent}


@router.get("/api/settings/document-format")
async def get_document_format():
    cfg = state.load_config()
    fmt = cfg.get("application_honesty", {}).get("document_format", "docx")
    return {"document_format": fmt}


@router.put("/api/settings/document-format")
async def set_document_format(body: DocumentFormatUpdate):
    if body.document_format not in state.VALID_DOC_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"document_format must be one of: {sorted(state.VALID_DOC_FORMATS)}",
        )
    cfg = state.load_config()
    if "application_honesty" not in cfg:
        cfg["application_honesty"] = {}
    cfg["application_honesty"]["document_format"] = body.document_format
    state.save_config(cfg)
    return {"document_format": body.document_format}


@router.get("/api/settings/max-resume-experience-entries")
async def get_max_resume_experience_entries():
    cfg = state.load_config()
    val = cfg.get("application_honesty", {}).get("max_resume_experience_entries")
    return {"max_resume_experience_entries": val}


@router.put("/api/settings/max-resume-experience-entries")
async def set_max_resume_experience_entries(body: MaxResumeExperienceEntriesUpdate):
    val = body.max_resume_experience_entries
    if val is not None and not (1 <= int(val) <= 20):
        raise HTTPException(
            status_code=400,
            detail="max_resume_experience_entries must be null or an integer 1-20",
        )
    cfg = state.load_config()
    if "application_honesty" not in cfg:
        cfg["application_honesty"] = {}
    cfg["application_honesty"]["max_resume_experience_entries"] = (
        int(val) if val is not None else None
    )
    state.save_config(cfg)
    return {"max_resume_experience_entries": cfg["application_honesty"]["max_resume_experience_entries"]}


@router.get("/api/settings/ai-edit-model-tier")
async def get_ai_edit_model_tier():
    cfg = state.load_config()
    tier = cfg.get("application_honesty", {}).get("ai_edit_model_tier", "strong")
    if tier not in state.VALID_AI_EDIT_TIERS:
        tier = "strong"
    return {"model_tier": tier}


@router.put("/api/settings/ai-edit-model-tier")
async def set_ai_edit_model_tier(body: AiEditModelTierUpdate):
    if body.model_tier not in state.VALID_AI_EDIT_TIERS:
        raise HTTPException(
            status_code=400,
            detail=f"model_tier must be one of: {sorted(state.VALID_AI_EDIT_TIERS)}",
        )
    cfg = state.load_config()
    if "application_honesty" not in cfg:
        cfg["application_honesty"] = {}
    cfg["application_honesty"]["ai_edit_model_tier"] = body.model_tier
    state.save_config(cfg)
    return {"model_tier": body.model_tier}
