"""
auto_apply/models.py — Canonical data models for the auto-apply pipeline.

All models use Pydantic v2 for runtime validation and easy JSON serialisation.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import ClassVar, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class HonestyLevel(str, Enum):
    """Controls how much latitude the AI takes when tailoring applications."""
    HONEST      = "honest"       # Only real profile data; no changes
    TAILORED    = "tailored"     # Rephrase and reorder; no fabrication
    EMBELLISHED = "embellished"  # Stretch scope/impact; stay plausible
    FABRICATED  = "fabricated"   # Invent experience or skills as needed


class ApplyMode(str, Enum):
    """Controls how far the orchestrator goes in the application flow."""
    AUTOFILL = "autofill"   # Fill every field, stop *before* final Submit click
    SUBMIT   = "submit"     # Fill every field AND click Submit (whitelist-gated)


class ApplyStatus(str, Enum):
    SUBMITTED         = "submitted"
    NEEDS_REVIEW      = "needs_review"
    FAILED            = "failed"
    AUTOFILL_COMPLETE = "autofill_complete"  # Mode=AUTOFILL succeeded; human must submit
    ALREADY_APPLIED   = "already_applied"    # Job already has an active application
    RATE_LIMITED      = "rate_limited"       # Platform daily limit reached


# ---------------------------------------------------------------------------
# User profile
# ---------------------------------------------------------------------------

class WorkExperience(BaseModel):
    title:      str
    company:    str
    start_date: str
    end_date:   str = "Present"
    bullets:    list[str] = Field(default_factory=list)


class Education(BaseModel):
    degree: str
    school: str
    year:   str


class UserProfile(BaseModel):
    full_name:           str = ""
    email:               str = ""
    phone:               str = ""
    middle_name:         str = ""
    location:            str = ""
    street_address:      str = ""
    street_address_2:    str = ""
    city:                str = ""
    state:               str = ""
    zip_code:            str = ""
    desired_salary:      str = ""
    linkedin:            str = ""
    github:              str = ""
    portfolio:           str = ""
    summary:             str = ""
    skills:              list[str]          = Field(default_factory=list)
    experience:          list[WorkExperience] = Field(default_factory=list)
    education:           list[Education]    = Field(default_factory=list)
    certifications:      list[str]          = Field(default_factory=list)
    gender:              str = ""
    race_ethnicity:      str = ""
    veteran_status:      str = ""
    disability_status:   str = ""
    work_authorization:  str = "Yes"
    sponsorship_required: str = "No"
    # Availability
    notice_period:   str = "2 weeks"
    available_start: str = "Immediately"
    # ATS credentials
    workday_email:      str = ""
    workday_password:   str = ""
    ats_login_password: str = ""   # generic login password for non-Workday ATS portals

    @classmethod
    def from_config(cls, config: dict) -> "UserProfile":
        """Build a UserProfile from the top-level config dict (config.yaml)."""
        raw = config.get("profile", {})
        if not isinstance(raw, dict):
            raw = {}
        # Make a copy FIRST so we never mutate the live config dict
        data = dict(raw)
        exp_raw = data.pop("experience", [])
        edu_raw = data.pop("education", [])
        return cls(
            **{k: v for k, v in data.items() if k in cls.model_fields},
            experience=[WorkExperience(**e) for e in exp_raw],
            education=[Education(**e) for e in edu_raw],
        )

    def years_of_experience(self) -> int:
        """Rough total years across all work history entries."""
        from datetime import datetime

        # Accept multiple date formats common in config.yaml
        _DATE_FORMATS = ("%Y-%m", "%Y-%m-%d", "%m/%d/%Y", "%B %Y", "%b %Y", "%Y")

        def _parse(date_str: str) -> datetime | None:
            s = date_str.strip()
            for fmt in _DATE_FORMATS:
                try:
                    return datetime.strptime(s[:10], fmt)
                except ValueError:
                    pass
            return None

        total = 0
        for exp in self.experience:
            try:
                start = _parse(exp.start_date)
                if start is None:
                    continue
                if exp.end_date.lower() in ("present", "current", "now"):
                    end = datetime.now()
                else:
                    end = _parse(exp.end_date)
                    if end is None:
                        continue
                total += max(0, (end - start).days // 365)
            except Exception:
                pass
        return total

    def to_text(self) -> str:
        """Flat text representation suitable for LLM prompts."""
        lines: list[str] = [
            f"Name: {self.full_name}",
            f"Email: {self.email}",
            f"Phone: {self.phone}",
            f"Location: {self.location}",
        ]
        if self.street_address:
            lines.append(f"Street Address (Address Line 1): {self.street_address}")
        if self.street_address_2:
            lines.append(f"Address Line 2: {self.street_address_2}")
        if self.city:
            lines.append(f"City: {self.city}")
        if self.state:
            lines.append(f"State / Province / Region: {self.state}")
        if self.zip_code:
            lines.append(f"Zip Code (Postal Code): {self.zip_code}")
        if self.linkedin:
            lines.append(f"LinkedIn: {self.linkedin}")
        if self.github:
            lines.append(f"GitHub: {self.github}")
        if self.portfolio:
            lines.append(f"Portfolio: {self.portfolio}")
        lines.append(f"Work Authorization: {self.work_authorization}")
        lines.append(f"Sponsorship Required: {self.sponsorship_required}")
        if self.desired_salary:
            lines.append(f"Desired Salary: {self.desired_salary}")
        if self.skills:
            lines.append(f"Skills: {', '.join(self.skills)}")
        if self.summary:
            lines.append(f"Summary: {self.summary}")
        for exp in self.experience:
            lines.append(
                f"Experience: {exp.title} at {exp.company} "
                f"({exp.start_date} – {exp.end_date})"
            )
            for b in exp.bullets[:3]:
                lines.append(f"  • {b}")
        for edu in self.education:
            lines.append(f"Education: {edu.degree}, {edu.school} ({edu.year})")
        if self.certifications:
            lines.append(f"Certifications: {', '.join(self.certifications)}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Job application request
# ---------------------------------------------------------------------------

class JobApplicationRequest(BaseModel):
    job_id:         str
    application_id: str = ""
    title:          str
    company:        str
    url:            str
    description:    str = ""
    resume_path:         Optional[str] = None
    cover_letter_path:   Optional[str] = None


# ---------------------------------------------------------------------------
# Field-level models (DOM snapshot + LLM mapping)
# ---------------------------------------------------------------------------

class FieldDescriptor(BaseModel):
    """Describes a single form field as detected from the DOM."""
    field_id:      str          # Stable key used to reference this field
    label:         str = ""     # Text from <label>, aria-label, or nearby heading
    placeholder:   str = ""
    field_type:    str = "text" # text|number|email|tel|url|select|textarea|checkbox|radio|file
    name:          str = ""
    options:       Optional[list[str]] = None  # For <select> / radio groups
    required:      bool = False
    extra_context: str = ""     # Nearby text snippet for ambiguous fields


class FieldValue(BaseModel):
    """LLM's answer for a single field."""
    field_id:   str
    value:      str      # The value to set (empty string → skip)
    action:     str = "fill"  # fill|select|check|upload|skip
    confidence: float = 1.0
    source:     str = "profile"  # profile|answer_bank|llm_generated|skip


# ---------------------------------------------------------------------------
# Apply result
# ---------------------------------------------------------------------------

class ApplyResult(BaseModel):
    success:         bool
    status:          ApplyStatus
    message:         str = ""
    screenshot_path: Optional[str] = None
    manual_url:      Optional[str] = None
    adapter_used:    str = ""
    fields_filled:        int = 0
    fields_skipped:       int = 0
    skipped_field_names:  list[str] = Field(default_factory=list)
    log_entries:          list[dict] = Field(default_factory=list)

    # Maps ApplyStatus → the string written to the applications.status DB column.
    _STATUS_TO_DB: ClassVar[dict] = {
        ApplyStatus.SUBMITTED:         "applied",
        ApplyStatus.AUTOFILL_COMPLETE: "autofill_complete",
        ApplyStatus.ALREADY_APPLIED:   "already_applied",
        ApplyStatus.RATE_LIMITED:      "rate_limited",
        ApplyStatus.NEEDS_REVIEW:      "needs_review",
        ApplyStatus.FAILED:            "manual",
    }

    def to_legacy_dict(self) -> dict:
        """Convert to the dict shape expected by main.py._bg_apply()."""
        return {
            "success":         self.success,
            "message":         self.message,
            "screenshot_path": self.screenshot_path,
            "manual_url":      self.manual_url,
            "block_reason":    "" if self.success else self.status.value,
            "db_status":       self._STATUS_TO_DB.get(self.status, "manual"),
        }


# ---------------------------------------------------------------------------
# Embellishment tracking
# ---------------------------------------------------------------------------

def validate_config(config: dict) -> list[str]:
    """Validate essential config fields before attempting an apply.

    Returns a list of human-readable error strings. An empty list means the
    config is valid enough to proceed.
    """
    import re as _re
    errors: list[str] = []
    profile = config.get("profile", {})

    name = (profile.get("full_name") or "").strip()
    if not name:
        errors.append("profile.full_name is required but not set in config.yaml")

    email = (profile.get("email") or "").strip()
    if not email:
        errors.append("profile.email is required but not set in config.yaml")
    elif not _re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        errors.append(f"profile.email '{email}' does not look like a valid email address")

    ai_cfg = config.get("ai", {})
    base_url = (ai_cfg.get("base_url") or "").strip()
    if not base_url:
        errors.append("ai.base_url is required but not set in config.yaml")

    aa_cfg = config.get("auto_apply", {})
    enabled = aa_cfg.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        errors.append(
            f"auto_apply.enabled must be true or false, got: {enabled!r}"
        )

    return errors


class EmbellishmentChange(BaseModel):
    """A single field that was altered from the original profile value."""
    field:    str
    original: str
    modified: str


class EmbellishmentLog(BaseModel):
    """Records what the AI changed (if anything) for one application.

    Stored as JSON in jobs.embellishment_log.  An empty change list means
    the AI stayed within the honesty_level constraint without modifications.
    """
    honesty_level:        HonestyLevel
    resume_changes:       list[EmbellishmentChange] = Field(default_factory=list)
    cover_letter_changes: list[EmbellishmentChange] = Field(default_factory=list)
    generated_at:         datetime
