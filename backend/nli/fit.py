"""Job-fit score from the local NLI model, used when the scoring LLM is unavailable.

Method (laya-bench `profile_eval.py flat`): keyword-bearing requirement lines from the
posting, each judged "is it met?" against the profile, equal weights; score = mean P(met).
"""
from __future__ import annotations

import re

MAX_LINES = 20
REASONING = "Scored by the local model (beta)"
KEYWORDS = re.compile(r"\b(experience|years|degree|bachelor|master|proficien|knowledge|skill|familiar|certif|ability|"
                      r"required|must|prefer|expertise|background)", re.I)
_BULLET = re.compile(r"^[\s•\-\*·▪◦●]+")


def req_lines(desc: str) -> list[str]:
    """Candidate requirement lines: split on newlines and sentence ends, 25-300 chars, keyword-bearing."""
    parts = re.split(r"\n+|(?<=[.!?;])\s+(?=[A-Z•\-\*])", desc or "")
    lines = [p for p in (_BULLET.sub("", p).strip() for p in parts) if 25 <= len(p) <= 300]
    return [line for line in lines if KEYWORDS.search(line)][:MAX_LINES]


def profile_pieces(profile: dict) -> list[str]:
    """[whole profile] plus the pieces it splits into when it is too long for one premise."""
    skills = ", ".join(profile.get("skills") or [])
    roles = [f"{r.get('title', '')} at {r.get('company', '')}, {r.get('start_date', '')}-{r.get('end_date') or 'present'}"
             for r in profile.get("experience") or [] if r.get("title")]
    edu = ", ".join(f"{e.get('degree', '')} {e.get('school', '')}".strip() for e in profile.get("education") or []
                    if e.get("degree"))
    certs = ", ".join(profile.get("certifications") or [])
    summary = (profile.get("summary") or "").strip()
    whole = (f"The candidate's skills: {skills}. Experience: {'; '.join(roles)}. Education: {edu}. "
             f"Certifications: {certs}. {summary}")
    pieces = [summary, f"The candidate's skills: {skills}.", f"Education: {edu}."]
    pieces += [f"The candidate worked as {r}." for r in roles]
    if certs:
        pieces.append(f"Certifications: {certs}.")
    return [whole] + [p for p in pieces if p.strip(" .:")]


def score(job: dict, profile: dict, nli) -> tuple[float, str, dict] | None:
    """(score 0-100, reasoning, raw report) or None when the posting has no requirement lines to judge."""
    lines = req_lines(job.get("description") or "")
    if not lines:
        return None
    hyps = [f"The candidate meets this job requirement: {line}" for line in lines]
    whole, *pieces = profile_pieces(profile)
    count = getattr(nli, "count_tokens", None)
    premises = [whole]
    if count and max(count(whole, h) for h in hyps) > 512:
        premises = pieces  # too long for one pass: judge each piece, best piece per line
    per = [nli.entail([(p, h) for h in hyps]) for p in premises]
    met = [max(col) for col in zip(*per, strict=True)]
    score = round(100 * sum(met) / len(met), 1)
    yes = [line for line, m in zip(lines, met, strict=True) if m > 0.5]
    no = [line for line, m in zip(lines, met, strict=True) if m <= 0.5]
    reasoning = f"{REASONING}: meets {len(yes)} of {len(lines)} requirement lines."
    return score, reasoning, {"matched_skills": yes, "missing_skills": no, "keywords": []}
