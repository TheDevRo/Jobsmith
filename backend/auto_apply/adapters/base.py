"""
auto_apply/adapters/base.py — ATSAdapter protocol (interface).

Every adapter must implement:
  name      str      Human-readable identifier, e.g. "greenhouse"
  matches() bool     True if this adapter can handle the given URL / DOM
  apply()   ApplyResult  Fill the form (and optionally submit)

The orchestrator calls matches() in priority order and invokes apply() on the
first match.  If no adapter matches, GenericAdapter is used as fallback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..browser_controller import BrowserController
    from ..llm_client import LLMClient
    from ..logger import AutoApplyLogger
    from ..models import ApplyMode, ApplyResult, JobApplicationRequest, UserProfile


@runtime_checkable
class ATSAdapter(Protocol):
    """Protocol that all ATS adapters must satisfy."""

    name: str

    def matches(self, url: str, page_text: str) -> bool:
        """
        Return True if this adapter should handle the given application URL.

        *page_text* is a short text excerpt from the page (≤500 chars) that can
        be used for additional heuristics beyond the URL alone.
        """
        ...

    async def apply(
        self,
        ctrl: "BrowserController",
        profile: "UserProfile",
        job: "JobApplicationRequest",
        llm: "LLMClient",
        mode: "ApplyMode",
        log: "AutoApplyLogger",
    ) -> "ApplyResult":
        """
        Fill (and optionally submit) the application form.

        The browser is already at the application URL when this is called.

        Parameters
        ----------
        ctrl   : BrowserController — Playwright wrapper
        profile: UserProfile       — Candidate data
        job    : JobApplicationRequest — The job being applied to
        llm    : LLMClient         — Local LM Studio client (for text gen / field mapping)
        mode   : ApplyMode         — AUTOFILL or SUBMIT
        log    : AutoApplyLogger   — Structured logger for this attempt

        Returns
        -------
        ApplyResult with success, status, fields_filled, etc.
        """
        ...
