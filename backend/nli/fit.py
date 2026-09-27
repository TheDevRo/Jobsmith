"""Job-fit score from the local NLI model, used when the scoring LLM is unavailable.

Method (the NLI scoring bench's equal-weight mode): keyword-bearing requirement lines from the
posting, each judged "is it met?" against a premise built for that line (line_premises), equal
weights; score = mean P(met).
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


# One premise per requirement line (one model run per line): roles, education and certifications, then the
# skills, summary sentences and role bullets ordered by the words they share with the line (the Apply Assist
# lexical retrieval), as many as fit so premise + hypothesis stay within MAX_PAIR_TOKENS, the fixed input length
# of the on-device model. A long profile no longer has to be cut to one compact premise that loses the detail a
# line needs (long-profile NLI bench, 2026-09-27).
MAX_PAIR_TOKENS = 256
_UNIT_SPLIT = re.compile(r"(?<=[.!?])\s+|\s*[\n•]+\s*")


def hypothesis(line: str) -> str:
    return f"The candidate meets this job requirement: {line}"


def _split_relevant(line: str, items: list[str]) -> tuple[list[str], list[str]]:
    """(items that share words with line, best first; the rest in profile order)."""
    from ..auto_apply.extractive.facts import Fact
    from ..auto_apply.extractive.retrieve import rank
    hits = [f.value for f, _ in rank(line, [Fact(str(i), t, t, "") for i, t in enumerate(items)], len(items))]
    return hits, [t for t in items if t not in hits]


def _most(fits, hi: int) -> int:
    """Largest n in [0, hi] with fits(n) (fits is monotone)."""
    lo = 0
    while lo < hi:
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if fits(mid) else (lo, mid - 1)
    return lo


def line_premises(profile: dict, lines: list[str], count=None) -> list[str]:
    """One premise per requirement line: roles, education and certifications always; then the skills and the
    summary sentences / role bullets that match the line; then the other skills and sentences, while they fit."""
    skills = [str(s) for s in profile.get("skills") or []]
    roles = [f"{r.get('title', '')} at {r.get('company', '')}, {r.get('start_date', '')}-{r.get('end_date') or 'present'}"
             for r in profile.get("experience") or [] if r.get("title")]
    edu = ", ".join(f"{e.get('degree', '')} {e.get('school', '')}".strip() for e in profile.get("education") or []
                    if e.get("degree"))
    certs = ", ".join(str(c) for c in profile.get("certifications") or [])
    units = [u.strip() for u in _UNIT_SPLIT.split(profile.get("summary") or "") if len(u.strip()) > 2]
    units += [f"At {r.get('company', '')}: {b.rstrip('.')}." for r in profile.get("experience") or [] for b in r.get("bullets") or [] if b]
    size = count or (lambda a, b: (len(a) + len(b)) // 4 + 3)
    out = []
    for line in lines:
        hyp = hypothesis(line)
        (sk_hit, sk_rest), (un_hit, un_rest) = _split_relevant(line, skills), _split_relevant(line, units)
        n = [0, 0, 0, 0]  # matching skills, matching sentences, other skills, other sentences

        def build(n) -> str:
            sk, un = sk_hit[:n[0]] + sk_rest[:n[2]], un_hit[:n[1]] + un_rest[:n[3]]
            return (f"The candidate's skills: {', '.join(sk)}. Experience: {'; '.join(roles)}. "
                    f"Education: {edu}. Certifications: {certs}. {' '.join(un)}").strip()

        for i, hi in enumerate((len(sk_hit), len(un_hit), len(sk_rest), len(un_rest))):
            n[i] = _most(lambda k: size(build(n[:i] + [k] + n[i + 1:]), hyp) <= MAX_PAIR_TOKENS, hi)
        out.append(build(n))
    return out


def score(job: dict, profile: dict, nli) -> tuple[float, str, dict] | None:
    """(score 0-100, reasoning, raw report) or None when the posting has no requirement lines to judge."""
    lines = req_lines(job.get("description") or "")
    if not lines:
        return None
    prems = line_premises(profile, lines, getattr(nli, "count_tokens", None))
    met = nli.entail([(p, hypothesis(line)) for p, line in zip(prems, lines, strict=True)])
    score = round(100 * sum(met) / len(met), 1)
    yes = [line for line, m in zip(lines, met, strict=True) if m > 0.5]
    no = [line for line, m in zip(lines, met, strict=True) if m <= 0.5]
    reasoning = f"{REASONING}: meets {len(yes)} of {len(lines)} requirement lines."
    return score, reasoning, {"matched_skills": yes, "missing_skills": no, "keywords": []}
