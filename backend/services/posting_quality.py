"""
posting_quality.py — Ghost-job / posting-quality heuristics.

Pure-Python signal scoring computed at fetch time. NO LLM calls, NO network.
Each signal subtracts from (or, for posted salary, adds to) a 0-100
hiring-likelihood score that starts at 100. Only signals that actually
fired are included in the report.

Report shape:
    {"score": int 0-100,
     "signals": [{"signal": str, "impact": int, "detail": str}, ...]}
"""

import re
from datetime import datetime, timezone

from ..job_sources import parse_posted_date

# ---- Signal weights (impact applied to the score; negative = penalty) ----
REPOST_HEAVY_PENALTY = -25   # times_seen >= 3
REPOST_MILD_PENALTY = -10    # times_seen == 2
STALE_60_PENALTY = -20       # posted > 60 days ago
STALE_30_PENALTY = -10       # posted > 30 days ago
NO_SALARY_PENALTY = -8       # no posted salary AND no estimate
POSTED_SALARY_BONUS = 5      # real disclosed salary (score still capped at 100)
MISSING_DESC_PENALTY = -25   # no description at all
SHORT_DESC_PENALTY = -12     # description under SHORT_DESC_CHARS
SHORT_DESC_CHARS = 300
BUZZWORD_PENALTY_EACH = -4   # per distinct buzzword found
BUZZWORD_PENALTY_CAP = -12
TITLE_URGENCY_PENALTY = -8   # "urgent" / "immediate start" style titles
TITLE_NOISE_PENALTY = -5     # excessive punctuation / emoji in title

BUZZWORDS = (
    "rockstar",
    "rock star",
    "ninja",
    "guru",
    "wear many hats",
    "fast-paced environment",
    "fast paced environment",
    "work hard play hard",
    "work hard, play hard",
    "unlimited pto",
)

TITLE_URGENCY_TERMS = ("urgent", "immediate start", "hiring now", "apply now", "asap")

# Punctuation that legitimately appears in titles is fine in small doses;
# 3+ of ! ? * or any emoji-range char reads as a spammy listing.
_TITLE_NOISE_RE = re.compile(r"[!?*]")
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF⭐❗✅❌️]"
)


def compute_quality_report(job: dict) -> dict:
    """Score how likely a posting reflects a real, active hiring effort.

    `job` is a job dict/row (as stored in the DB or as normalized by the
    fetchers). Missing keys are treated as absent data. Returns a report
    dict; only signals that fired are listed.
    """
    score = 100
    signals: list[dict] = []

    def fire(name: str, impact: int, detail: str):
        nonlocal score
        score += impact
        signals.append({"signal": name, "impact": impact, "detail": detail})

    # -- Repost signal --------------------------------------------------
    try:
        times_seen = int(job.get("times_seen") or 1)
    except (TypeError, ValueError):
        times_seen = 1
    if times_seen >= 3:
        fire("repost", REPOST_HEAVY_PENALTY,
             f"Seen in {times_seen} separate fetches — likely reposted repeatedly")
    elif times_seen == 2:
        fire("repost", REPOST_MILD_PENALTY, "Re-appeared in a later fetch")

    # -- Staleness (skip when date unparseable) -------------------------
    posted = parse_posted_date(job.get("date_posted"))
    if posted is not None:
        age_days = (datetime.now(timezone.utc) - posted).days
        if age_days > 60:
            fire("stale", STALE_60_PENALTY, f"Posted {age_days} days ago")
        elif age_days > 30:
            fire("stale", STALE_30_PENALTY, f"Posted {age_days} days ago")

    # -- Salary transparency --------------------------------------------
    has_posted_salary = bool(job.get("salary_min") or job.get("salary_max"))
    has_estimate = bool(
        job.get("estimated_salary_min") or job.get("estimated_salary_max")
    )
    if has_posted_salary:
        fire("salary_posted", POSTED_SALARY_BONUS, "Employer discloses compensation")
    elif not has_estimate:
        fire("no_salary", NO_SALARY_PENALTY,
             "No posted salary and no market estimate available")

    # -- Description quality ---------------------------------------------
    description = (job.get("description") or "").strip()
    if not description:
        fire("missing_description", MISSING_DESC_PENALTY, "No job description")
    else:
        if len(description) < SHORT_DESC_CHARS:
            fire("short_description", SHORT_DESC_PENALTY,
                 f"Description is only {len(description)} characters")
        desc_lower = description.lower()
        found = [b for b in BUZZWORDS if b in desc_lower]
        # Collapse spelling variants so e.g. "rockstar"+"rock star" counts once.
        distinct = {b.replace("-", " ").replace(",", "") for b in found}
        if distinct:
            impact = max(BUZZWORD_PENALTY_EACH * len(distinct), BUZZWORD_PENALTY_CAP)
            fire("buzzwords", impact, "Buzzwords: " + ", ".join(sorted(distinct)))

    # -- Title red flags ---------------------------------------------------
    title = (job.get("title") or "").strip()
    if title:
        title_lower = title.lower()
        urgency = [t for t in TITLE_URGENCY_TERMS if t in title_lower]
        if urgency:
            fire("title_urgency", TITLE_URGENCY_PENALTY,
                 "Urgency language in title: " + ", ".join(urgency))
        noise = len(_TITLE_NOISE_RE.findall(title)) + len(_EMOJI_RE.findall(title))
        if noise >= 3 or _EMOJI_RE.search(title):
            fire("title_noise", TITLE_NOISE_PENALTY,
                 "Excessive punctuation or emoji in title")

    return {"score": max(0, min(100, score)), "signals": signals}
