"""Extractive pass 4 for Apply Assist (Local AI model beta).

Ported from the ~/jobsmith-extractive prototype. Fields left after passes 1-3 are
answered from the profile only: a form option, a verbatim profile value, or a
date computation, chosen by an NLI model behind a confidence gate. Essays still
go to the LLM and come back flagged as AI drafts. Anything else is left blank.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import TYPE_CHECKING

from . import handlers as H
from .facts import build_facts
from .retrieve import top_facts

if TYPE_CHECKING:
    from ..llm_client import LLMClient
    from ..models import FieldDescriptor, FieldValue, JobApplicationRequest, UserProfile

log = logging.getLogger(__name__)

# Tuned on the prototype's tuning half (results/tuning.md there): the most conservative
# config within 1 point of the best. Sensitive (legal/EEO) questions use T_STRICT.
T, M, T_STRICT, TOP_K = 0.8, 0.0, 0.99, 5
_EEO_FIELDS = ("gender", "race_ethnicity", "veteran_status", "disability_status")


def decide(f: "FieldDescriptor", profile: "UserProfile", facts, nli, today: date) -> H.Decision:
    """Every non-essay field: choice / text via NLI, years via date math, the rest skipped."""
    kind = H.classify(f)
    if kind == H.SKIP:
        return H.Decision(kind=kind, reason="field type left for the user")
    if kind == H.NUMBER:
        return H.number(f, profile, today)
    sensitive = H.is_sensitive(f)
    t = max(T, T_STRICT) if sensitive else T
    top = [x for x, _ in top_facts(f, facts, TOP_K, sensitive)]
    d = (H.choice if kind == H.CHOICE else H.text)(f, top, nli, t, M)
    if sensitive:
        d.reason = "[strict] " + d.reason
    return d


async def essay(client: "LLMClient", f: "FieldDescriptor", profile: "UserProfile",
                job: "JobApplicationRequest") -> H.Decision:
    d = H.Decision(kind=H.ESSAY)
    if not (client._config.get("ai") or {}).get("base_url"):
        d.reason = "no LLM endpoint configured"
        return d
    try:
        # EEO answers never go into an essay prompt (a 9b model pasted them into "Why us?").
        redacted = profile.model_copy(update={k: "" for k in _EEO_FIELDS})
        raw = await client.generate_answer(f.label or f.name, redacted, job)
    except Exception as exc:  # endpoint down -> leave it for the user
        d.reason = f"essay LLM failed: {exc}"
        return d
    d.value = H.clean_essay(raw)
    if d.value:
        d.origin, d.confidence, d.reason = "essay", 0.5, "LLM draft, needs review"
    else:
        d.reason = f"LLM gave no usable essay: {raw[:120]!r}"
    return d


def to_field_value(f: "FieldDescriptor", d: H.Decision) -> "FieldValue":
    """Existing `source` values only, so the extension and adapters need no change:
    extractive fills are "profile", essays "llm_generated" (the AI-draft marker), the rest "skip"."""
    from ..models import FieldValue
    if not d.filled:
        return FieldValue(field_id=f.field_id, value="", action="skip", confidence=d.confidence, source="skip")
    return FieldValue(field_id=f.field_id, value=d.value, action="select" if f.options else "fill",
                      confidence=d.confidence, source="llm_generated" if d.origin == "essay" else "profile")


async def fill(client: "LLMClient", profile: "UserProfile", job: "JobApplicationRequest",
               fields: "list[FieldDescriptor]", answer_bank: dict[str, str], nli,
               today: date | None = None, trace: list | None = None) -> "list[FieldValue]":
    """Pass 4 for `fields`. NLI runs in a worker thread; essays run concurrently on the LLM."""
    today = today or date.today()
    facts = build_facts(profile, answer_bank, today)
    is_essay = [H.classify(f) == H.ESSAY for f in fields]
    plain = [f for f, e in zip(fields, is_essay, strict=True) if not e]
    essays = [f for f, e in zip(fields, is_essay, strict=True) if e]
    decided, drafts = await asyncio.gather(
        asyncio.to_thread(lambda: [decide(f, profile, facts, nli, today) for f in plain]),
        asyncio.gather(*(essay(client, f, profile, job) for f in essays)),
    )
    by_id = {f.field_id: d for f, d in zip(plain + essays, list(decided) + list(drafts), strict=True)}
    out = []
    for f in fields:
        d = by_id[f.field_id]
        if trace is not None:
            trace.append({"field_id": f.field_id, "label": f.label, "kind": d.kind, "value": d.value,
                          "origin": d.origin, "confidence": round(d.confidence, 4), "reason": d.reason,
                          "facts": d.facts, "scores": d.scores})
        log.debug("extractive %s [%s] %s -> %r (%s)", f.field_id, d.kind, f.label, d.value[:60], d.reason)
        out.append(to_field_value(f, d))
    return out
