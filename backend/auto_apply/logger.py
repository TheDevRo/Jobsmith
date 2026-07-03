"""
auto_apply/logger.py — Structured logging for the auto-apply pipeline.

Writes two outputs:
  1. JSON Lines file  (data/auto_apply_log.jsonl)  — machine-readable audit trail
  2. Python logger   (auto_apply.logger)            — human-readable console output

AutoApplyLogger instances are scoped to a single application attempt and
accumulate entries that become part of ApplyResult.log_entries.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from ..paths import project_root

_JSONL_PATH = project_root() / "data" / "auto_apply_log.jsonl"

# Fields that should be masked in the JSON log to avoid leaking PII
_SENSITIVE_NAMES = frozenset({
    "password", "ssn", "social_security", "dob", "date_of_birth",
    "credit_card", "cvv",
})

logger = logging.getLogger("auto_apply.logger")


class AutoApplyLogger:
    """
    Per-application structured logger.

    Usage::

        log = AutoApplyLogger(job_id="abc", app_id="xyz", site="greenhouse.io")
        log.info("Navigated to application form")
        log.field("input-name", "Jane Doe", source="profile", confidence=1.0)
        log.field("password", "s3cr3t", source="profile")   # will be masked
        log.llm_call(fields_count=12, confidence_avg=0.87)
        log.error("Page timeout on step 3")
        entries = log.entries   # attach to ApplyResult
    """

    def __init__(
        self,
        job_id: str,
        app_id: str,
        site: str = "",
        adapter: str = "",
        mode: str = "",
        jsonl_path: Optional[Path] = None,
    ) -> None:
        self._job_id   = job_id
        self._app_id   = app_id
        self._site     = site
        self._adapter  = adapter
        self._mode     = mode
        self._jsonl    = Path(jsonl_path) if jsonl_path else _JSONL_PATH
        self._entries:  list[dict] = []
        self._start_ts: float = time.monotonic()

    # ------------------------------------------------------------------
    # Public logging methods
    # ------------------------------------------------------------------

    def info(self, message: str, **extra) -> None:
        self._record("info", message, **extra)

    def warning(self, message: str, **extra) -> None:
        self._record("warning", message, **extra)
        logger.warning("[%s/%s] %s", self._app_id[:8], self._adapter, message)

    def error(self, message: str, **extra) -> None:
        self._record("error", message, error=message, **extra)
        logger.error("[%s/%s] %s", self._app_id[:8], self._adapter, message)

    def field(
        self,
        field_id: str,
        value: str,
        *,
        source: str = "profile",
        confidence: float = 1.0,
        action: str = "fill",
    ) -> None:
        """Log a field-fill action (masks sensitive values)."""
        masked = _mask(field_id, value)
        self._record(
            "field",
            f"Field {field_id} ({action}) ← {masked!r} [{source}, conf={confidence:.2f}]",
            field_id=field_id,
            value=masked,
            source=source,
            confidence=confidence,
            action=action,
        )

    def llm_call(
        self,
        fields_count: int,
        confidence_avg: float,
        skipped: int = 0,
    ) -> None:
        self._record(
            "llm_call",
            f"LLM mapped {fields_count} fields "
            f"(avg confidence={confidence_avg:.2f}, skipped={skipped})",
            fields_count=fields_count,
            confidence_avg=round(confidence_avg, 3),
            skipped=skipped,
        )

    def adapter_chosen(self, adapter: str, reason: str = "") -> None:
        self._adapter = adapter
        self._record("adapter", f"Adapter: {adapter}" + (f" — {reason}" if reason else ""))

    def step(self, step_name: str, page_url: str = "") -> None:
        self._record("step", f"Step: {step_name}", page_url=page_url[:120] if page_url else "")

    def result(
        self,
        success: bool,
        status: str,
        fields_filled: int = 0,
        fields_skipped: int = 0,
        *,
        tier: int = 2,
        fields_needs_review: int = 0,
        screenshot_path: Optional[str] = None,
        page_count: int = 1,
        skipped_field_names: Optional[list] = None,
    ) -> None:
        elapsed = round(time.monotonic() - self._start_ts, 1)
        self._record(
            "result",
            f"{'SUCCESS' if success else 'FAILURE'} — {status} "
            f"({fields_filled} filled, {fields_skipped} skipped, {elapsed}s)",
            success=success,
            status=status,
            provider=self._adapter,
            tier=tier,
            fields_attempted=fields_filled + fields_skipped,
            fields_filled=fields_filled,
            fields_skipped=fields_skipped,
            fields_needs_review=fields_needs_review,
            skipped_field_names=skipped_field_names or [],
            final_status=status,
            screenshot_path=screenshot_path or None,
            page_count=page_count,
            elapsed_seconds=elapsed,
        )

    @property
    def entries(self) -> list[dict]:
        return list(self._entries)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _record(self, level: str, message: str, **extra) -> None:
        entry = {
            "ts":      datetime.now(timezone.utc).isoformat(),
            "level":   level,
            "job_id":  self._job_id,
            "app_id":  self._app_id,
            "site":    self._site,
            "adapter": self._adapter,
            "mode":    self._mode,
            "message": message,
            **extra,
        }
        self._entries.append(entry)
        logger.debug("[%s] %s", level.upper(), message)
        self._append_jsonl(entry)

    def _append_jsonl(self, entry: dict) -> None:
        try:
            self._jsonl.parent.mkdir(parents=True, exist_ok=True)
            with open(self._jsonl, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug("AutoApplyLogger: could not write JSONL: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mask(field_id: str, value: str) -> str:
    """Return '***' for sensitive field names, otherwise the raw value."""
    name_lower = field_id.lower()
    if any(s in name_lower for s in _SENSITIVE_NAMES):
        return "***"
    return value
