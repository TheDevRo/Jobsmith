"""
extension_api.py — HTTP surface for the Jobsmith browser extension.

The extension runs inside the user's real Chrome/Firefox and talks to this API
to: pull profile autofill values, map detected form fields to answers via the
local LLM + answer bank, fetch tailored resume/cover-letter DOCX files, and
read per-job context. None of these endpoints touch Playwright — the user's
own browser is the page context.

All routes (except /health) require an `X-Jobsmith-Token` header. The token is
generated on first use and persisted at `data/extension_token.txt`.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import database as db
from .auto_apply.llm_client import LLMClient
from .paths import project_root
from .auto_apply.models import (
    FieldDescriptor,
    FieldValue,
    JobApplicationRequest,
    UserProfile,
)

logger = logging.getLogger(__name__)

# User state (token, generated documents) must live under project_root() —
# in desktop builds __file__ points into the PyInstaller extraction dir, which
# is recreated on every launch, so a __file__-based token would rotate per run.
PROJECT_ROOT = project_root()
DATA_DIR = PROJECT_ROOT / "data"
RESUMES_DIR = PROJECT_ROOT / "resumes"
TOKEN_PATH = DATA_DIR / "extension_token.txt"


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

def get_or_create_token() -> str:
    """Return the extension auth token, generating + persisting one if missing."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_PATH.exists():
        token = TOKEN_PATH.read_text().strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token)
    TOKEN_PATH.chmod(0o600)
    logger.info("Extension token generated at %s", TOKEN_PATH)
    return token


def rotate_token() -> str:
    """Generate a new token, overwriting the existing one."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.write_text(token)
    TOKEN_PATH.chmod(0o600)
    logger.info("Extension token rotated at %s", TOKEN_PATH)
    return token


def _verify_token(x_jobsmith_token: Optional[str] = Header(default=None)) -> None:
    expected = get_or_create_token()
    if not x_jobsmith_token or not secrets.compare_digest(x_jobsmith_token, expected):
        raise HTTPException(401, "Invalid or missing X-Jobsmith-Token header")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class ExtScanRequest(BaseModel):
    url: str
    job_id: Optional[str] = None
    fields: list[FieldDescriptor] = Field(default_factory=list)


class ExtScanResponse(BaseModel):
    fields: list[FieldValue]
    count: int


class ExtAnswerRequest(BaseModel):
    question: str
    job_id: Optional[str] = None
    field_type: str = "text"


class ExtAnswerResponse(BaseModel):
    value: str
    source: str
    confidence: float


class ExtCookie(BaseModel):
    # Shape of chrome.cookies.Cookie (the fields we care about).
    name: str
    value: str
    domain: str
    path: str = "/"
    secure: bool = False
    httpOnly: bool = False
    sameSite: Optional[str] = None
    expirationDate: Optional[float] = None  # float seconds; absent for session cookies


class ExtSessionImportRequest(BaseModel):
    domain: str  # "linkedin" | "indeed"
    cookies: list[ExtCookie]


class ExtSessionImportResponse(BaseModel):
    ok: bool
    domain: str
    cookie_count: int
    message: str


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def build_router(load_config_fn) -> APIRouter:
    """
    Build the /api/ext router. `load_config_fn` is the same load_config used
    by main.py — injected to avoid a circular import.
    """
    router = APIRouter(prefix="/api/ext", tags=["extension"])

    # ---- Health (unauthenticated) ----------------------------------------
    @router.get("/health")
    async def health():
        return {"ok": True, "service": "jobsmith", "needs_token": True}

    # ---- Applicant Assist handoff checkin --------------------------------
    @router.post("/assist/checkin")
    async def assist_checkin(
        body: dict,
        x_jobsmith_token: Optional[str] = Header(default=None),
    ):
        """Content script on /assist/launch/{id} calls this once it's read
        (and stored, if needed) the setup token from the page DOM.
        Accepts either the persistent extension token or the per-session
        setup_token (same value today, but checked explicitly for clarity).
        """
        from . import applicant_assist

        session_id = body.get("session_id") if isinstance(body, dict) else None
        if not session_id:
            raise HTTPException(400, "session_id required")
        rec = applicant_assist.get_handoff_session(session_id)
        if not rec:
            raise HTTPException(404, "Assist session expired or not found")

        expected_persistent = get_or_create_token()
        expected_setup = rec.get("setup_token", "")
        if not x_jobsmith_token or not (
            secrets.compare_digest(x_jobsmith_token, expected_persistent)
            or secrets.compare_digest(x_jobsmith_token, expected_setup)
        ):
            raise HTTPException(401, "Invalid X-Jobsmith-Token for assist checkin")

        applicant_assist.mark_handoff_extension_ready(session_id)
        return {"ok": True, "apply_url": rec["apply_url"]}

    # ---- Profile ---------------------------------------------------------
    @router.get("/profile", dependencies=[])
    async def get_profile(x_jobsmith_token: Optional[str] = Header(default=None)):
        _verify_token(x_jobsmith_token)
        profile = UserProfile.from_config(load_config_fn())
        return {
            "full_name":      profile.full_name,
            "email":          profile.email,
            "phone":          profile.phone,
            "linkedin":       profile.linkedin,
            "github":         profile.github,
            "portfolio":      profile.portfolio,
            "location":       profile.location,
            "street_address": profile.street_address,
            "city":           profile.city,
            "state":          profile.state,
            "zip_code":       profile.zip_code,
            "desired_salary": profile.desired_salary,
            "work_authorization":  profile.work_authorization,
            "sponsorship_required": profile.sponsorship_required,
            "available_start": profile.available_start,
            "notice_period":   profile.notice_period,
        }

    # ---- Job context -----------------------------------------------------
    @router.get("/job/{job_id}")
    async def get_job(job_id: str, x_jobsmith_token: Optional[str] = Header(default=None)):
        _verify_token(x_jobsmith_token)
        row = await db.get_job(job_id)
        if not row:
            raise HTTPException(404, "Job not found")
        return {
            "id":          row.get("id"),
            "title":       row.get("title", ""),
            "company":     row.get("company", ""),
            "url":         row.get("url", ""),
            "description": row.get("description", ""),
        }

    # ---- Resume / cover-letter ------------------------------------------
    @router.get("/resume/{job_id}")
    async def get_resume(job_id: str, x_jobsmith_token: Optional[str] = Header(default=None)):
        _verify_token(x_jobsmith_token)
        path = RESUMES_DIR / f"{job_id}_resume.docx"
        if not path.exists():
            raise HTTPException(404, "Resume not found — tailor the job first")
        return FileResponse(
            str(path),
            filename=f"{job_id}_resume.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    @router.get("/cover-letter/{job_id}")
    async def get_cover_letter(job_id: str, x_jobsmith_token: Optional[str] = Header(default=None)):
        _verify_token(x_jobsmith_token)
        path = RESUMES_DIR / f"{job_id}_cover_letter.docx"
        if not path.exists():
            raise HTTPException(404, "Cover letter not found — tailor the job first")
        return FileResponse(
            str(path),
            filename=f"{job_id}_cover_letter.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    # ---- Field scan / map ------------------------------------------------
    @router.post("/scan", response_model=ExtScanResponse)
    async def scan_fields(
        body: ExtScanRequest,
        x_jobsmith_token: Optional[str] = Header(default=None),
    ) -> ExtScanResponse:
        _verify_token(x_jobsmith_token)
        cfg = load_config_fn()
        profile = UserProfile.from_config(cfg)

        job_req = await _job_request_for(body.job_id, fallback_url=body.url)

        # Per-job desired-salary override: prefer posting salary, then our
        # estimator's high-end, else leave blank so the LLM skips and the
        # field shows up unfilled in the side panel.
        contextual = await _contextual_desired_salary(body.job_id)
        if contextual is not None:
            profile = profile.model_copy(update={"desired_salary": contextual})

        llm = LLMClient(cfg)

        from .auto_apply.answer_bank import get_answer_bank
        full_bank = get_answer_bank().all_snippets()
        bank_snapshot = _slim_answer_bank(full_bank, body.fields)

        try:
            values = await llm.map_fields_to_values(profile, job_req, body.fields, bank_snapshot)
        except Exception as exc:
            logger.exception("extension scan: map_fields_to_values failed")
            raise HTTPException(500, f"Field mapping failed: {exc}")

        return ExtScanResponse(fields=values, count=len(values))

    # ---- Single-question answer ------------------------------------------
    @router.post("/answer", response_model=ExtAnswerResponse)
    async def answer_question(
        body: ExtAnswerRequest,
        x_jobsmith_token: Optional[str] = Header(default=None),
    ) -> ExtAnswerResponse:
        _verify_token(x_jobsmith_token)
        cfg = load_config_fn()

        # Try answer bank first (free, deterministic)
        from .auto_apply.answer_bank import get_answer_bank
        match = get_answer_bank().find_best_match(body.question)
        if match:
            return ExtAnswerResponse(value=match, source="answer_bank", confidence=1.0)

        profile = UserProfile.from_config(cfg)
        job_req = await _job_request_for(body.job_id, fallback_url="")
        llm = LLMClient(cfg)
        try:
            text = await llm.generate_answer(body.question, profile, job_req)
        except Exception as exc:
            logger.exception("extension answer: generate_answer failed")
            raise HTTPException(500, f"Answer generation failed: {exc}")
        text = (text or "").strip()
        return ExtAnswerResponse(
            value=text,
            source="llm_generated" if text else "skip",
            confidence=0.7 if text else 0.0,
        )

    # ---- Session import from extension cookies ---------------------------
    @router.post("/sessions/import", response_model=ExtSessionImportResponse)
    async def import_session(
        body: ExtSessionImportRequest,
        x_jobsmith_token: Optional[str] = Header(default=None),
    ) -> ExtSessionImportResponse:
        """One-click session sync: the extension reads the user's live
        chrome.cookies for LinkedIn/Indeed and posts them here, seeding the
        same Playwright session the apply pipeline uses — no manual export."""
        _verify_token(x_jobsmith_token)
        from . import cookie_import, session_import

        domain = body.domain.lower().strip()
        if domain not in ("linkedin", "indeed"):
            raise HTTPException(400, f"Unsupported domain: {domain}")

        # chrome.cookies uses expirationDate; normalize_cookies already maps it.
        cookies = cookie_import.normalize_cookies([c.model_dump() for c in body.cookies])
        if not cookies:
            raise HTTPException(400, "No usable cookies provided")

        try:
            result = session_import.persist_session(domain, cookies, source="extension_import")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        await db.log_activity(
            f"{domain}_login",
            f"{domain.capitalize()} session imported from extension ({result['imported']} cookies)",
        )
        return ExtSessionImportResponse(
            ok=True,
            domain=domain,
            cookie_count=result["imported"],
            message=f"Wrote {result['imported']} cookies to {result['target']}",
        )

    return router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slim_answer_bank(
    full_bank: dict[str, str],
    fields: list[FieldDescriptor],
    min_keep: int = 6,
) -> dict[str, str]:
    """Keep only bank entries whose key shares a token with any field label/name.

    Falls back to the full bank if the filter would leave fewer than min_keep
    entries — small banks aren't worth pruning.
    """
    if len(full_bank) <= min_keep:
        return full_bank
    haystack = " ".join(
        f"{f.label or ''} {f.name or ''} {f.placeholder or ''}".lower()
        for f in fields
    )
    if not haystack.strip():
        return full_bank
    kept = {
        k: v for k, v in full_bank.items()
        if any(tok and tok in haystack for tok in k.lower().replace("_", " ").split())
    }
    return kept if len(kept) >= min_keep else full_bank


async def _contextual_desired_salary(job_id: Optional[str]) -> Optional[str]:
    """Compute a per-job desired-salary override.

    Returns:
        Formatted string ($NNN,NNN) if the job posting or our estimator has
        a high-end figure; "" to force blank/skip; None to keep the profile
        default untouched.
    """
    if not job_id:
        return ""  # no job bound → blank, user sees it highlighted
    row = await db.get_job(job_id)
    if not row:
        return ""
    # Prefer the posting's stated max; fall back to estimator's max.
    cand = row.get("salary_max") or row.get("estimated_salary_max")
    if not cand:
        return ""
    try:
        n = int(cand)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return f"${n:,}"


async def _job_request_for(job_id: Optional[str], fallback_url: str) -> JobApplicationRequest:
    """Build a JobApplicationRequest from the DB if we have a job_id, else a stub."""
    if job_id:
        row = await db.get_job(job_id)
        if row:
            return JobApplicationRequest(
                job_id=str(row.get("id", job_id)),
                title=row.get("title", ""),
                company=row.get("company", ""),
                url=row.get("url", fallback_url),
                description=row.get("description", ""),
            )
    return JobApplicationRequest(
        job_id=job_id or "",
        title="",
        company="",
        url=fallback_url,
    )
