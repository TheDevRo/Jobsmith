"""Job-fit score from the local NLI model, used when the scoring LLM is unavailable.

Method (the NLI scoring bench's equal-weight mode): keyword-bearing requirement lines from the
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


# The profile premise is kept to this many tokens so premise + requirement line fits the model's 256-token
# input: 20 model runs per job. (Splitting a long profile into pieces meant ~200 runs per job, over a minute
# on a phone, and the NLI bench that validated this method used one compact premise too.)
PREMISE_TOKENS = 190


def premise(profile: dict, count=None) -> str:
    """One compact premise: roles, education, certifications, then as many skills and summary words as fit."""
    skills = [str(s) for s in profile.get("skills") or []]
    roles = [f"{r.get('title', '')} at {r.get('company', '')}, {r.get('start_date', '')}-{r.get('end_date') or 'present'}"
             for r in profile.get("experience") or [] if r.get("title")]
    edu = ", ".join(f"{e.get('degree', '')} {e.get('school', '')}".strip() for e in profile.get("education") or []
                    if e.get("degree"))
    certs = ", ".join(str(c) for c in profile.get("certifications") or [])
    words = (profile.get("summary") or "").split()
    size = (lambda text: count(text, "")) if count else (lambda text: len(text) // 4)

    def build(n_skills: int, n_words: int) -> str:
        return (f"The candidate's skills: {', '.join(skills[:n_skills])}. Experience: {'; '.join(roles)}. "
                f"Education: {edu}. Certifications: {certs}. {' '.join(words[:n_words])}").strip()

    def most(fits, hi: int) -> int:  # largest n in [0, hi] with fits(n)
        lo = 0
        while lo < hi:
            mid = (lo + hi + 1) // 2
            lo, hi = (mid, hi) if fits(mid) else (lo, mid - 1)
        return lo

    n_skills = len(skills)
    if size(build(n_skills, 0)) > PREMISE_TOKENS:  # even without the summary: trim the skills list
        n_skills = most(lambda n: size(build(n, 0)) <= PREMISE_TOKENS, len(skills))
    return build(n_skills, most(lambda n: size(build(n_skills, n)) <= PREMISE_TOKENS, len(words)))


def score(job: dict, profile: dict, nli) -> tuple[float, str, dict] | None:
    """(score 0-100, reasoning, raw report) or None when the posting has no requirement lines to judge."""
    lines = req_lines(job.get("description") or "")
    if not lines:
        return None
    hyps = [f"The candidate meets this job requirement: {line}" for line in lines]
    prem = premise(profile, getattr(nli, "count_tokens", None))
    met = nli.entail([(prem, h) for h in hyps])
    score = round(100 * sum(met) / len(met), 1)
    yes = [line for line, m in zip(lines, met, strict=True) if m > 0.5]
    no = [line for line, m in zip(lines, met, strict=True) if m <= 0.5]
    reasoning = f"{REASONING}: meets {len(yes)} of {len(lines)} requirement lines."
    return score, reasoning, {"matched_skills": yes, "missing_skills": no, "keywords": []}
