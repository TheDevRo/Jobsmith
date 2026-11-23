"""Requirement coverage: "this job covers N of M requirements".

A coverage dict is stored as JSON in jobs.coverage next to the 0-100 fit score:
    {"requirements": [{"text", "kind": "must"|"nice", "met": bool, "evidence": str|None, "dismissed"?: True}],
     "covered": int, "total": int, "missing_must": int}
It comes from the scoring LLM (parse_requirements) or from the local model's per-line P(met) (from_nli).
Kept apart from ai_engine.parse_score_response, which is pinned by the iOS twin's fixtures.
"""
from __future__ import annotations

import json
import re
from typing import Optional

MAX_REQUIREMENTS = 20
MET_THRESHOLD = 0.5
_MUST = re.compile(r"\b(required|requires?|must|minimum|mandatory|essential|at least)\b|\d\+?\s*years", re.I)


def build(requirements: list[dict]) -> Optional[dict]:
    """The coverage dict with its derived counts; None for an empty list."""
    if not requirements:
        return None
    return {
        "requirements": requirements,
        "covered": sum(r["met"] for r in requirements),
        "total": len(requirements),
        "missing_must": sum(r["kind"] == "must" and not r["met"] for r in requirements),
    }


def parse_requirements(text: str) -> Optional[dict]:
    """Coverage from an LLM scoring response's "requirements" array. Tolerant: None for anything malformed
    or empty, never raises. Items that are not usable are skipped, the rest is kept (partial input works)."""
    try:
        match = re.search(r"\{.*\}", text or "", re.DOTALL)
        data = json.loads(match.group(0)) if match else None
        raw = data.get("requirements") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return None
        out = []
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("met"), bool):
                continue
            req = item.get("text")
            if not isinstance(req, str) or not req.strip():
                continue
            evidence = item.get("evidence")
            out.append({
                "text": req.strip()[:200],
                "kind": "must" if str(item.get("kind", "")).lower() in ("must", "must-have", "required") else "nice",
                "met": item["met"],
                "evidence": evidence.strip()[:200] if isinstance(evidence, str) and evidence.strip() else None,
            })
            if len(out) >= MAX_REQUIREMENTS:
                break
        return build(out)
    except Exception:  # noqa: BLE001 — coverage is optional; a bad response just means none
        return None


def from_nli(lines: list[str], probs: list[float]) -> Optional[dict]:
    """Coverage from the local model: line met when P(met) >= 0.5; 'must' when the line says required/must/minimum."""
    return build([
        {"text": line, "kind": "must" if _MUST.search(line) else "nice", "met": p >= MET_THRESHOLD,
         "evidence": f"Local match: {round(100 * p)}% likely met"}
        for line, p in zip(lines, probs, strict=True)
    ])


def dismiss(coverage: dict, text: str) -> bool:
    """Mark the unmet requirement with this text as a known gap ("I don't have this"). True if one changed.
    Counts are unchanged: the gap is acknowledged, not covered."""
    for req in coverage.get("requirements") or []:
        if req.get("text") == text and not req.get("met"):
            req["dismissed"] = True
            return True
    return False


def sort_key_sql(alias: str = "j") -> str:
    """ORDER BY terms for 'Best coverage': share covered, then fewer missing must-haves, then score.
    Jobs without coverage sink below those with it."""
    c = f"{alias}.coverage"
    return (f"({c} IS NULL) ASC, "
            f"CAST(json_extract({c}, '$.covered') AS REAL) / NULLIF(json_extract({c}, '$.total'), 0) DESC, "
            f"json_extract({c}, '$.missing_must') ASC, COALESCE({alias}.fit_score, -1) DESC")
