"""Narrow the fact store to the top-k facts for one field: lexical overlap + an alias table."""
from __future__ import annotations

import math
import re
from collections import Counter

from ..models import FieldDescriptor

from .facts import EXPLICIT, Fact

_STOP = set("""a an the and or of to in on for with at by is are was were be been you your yours do does did have has had
any this that these those what which who whom how when where why can could would will shall should may might must
we our us i me my it its as from if than then there their they them currently current please select choose
candidate candidate's s""".split())

# (question regex, fact regex over "key text"). A hit adds ALIAS_BONUS to that fact's score.
ALIASES: list[tuple[str, str]] = [
    (r"authori[sz]|eligib|legally|right to work|proof .*work|i-9", r"^work_authorization|^sponsorship"),
    (r"sponsor|visa|h-?1b|citizen|permanent resident|green card|authorization status", r"^sponsorship|^work_authorization|visa|citizen"),
    (r"\b18\b|\bage\b", r"^over_18"),
    (r"gender|\bsex\b|\bman\b|\bwoman\b", r"^gender"),
    (r"race|ethnic|hispanic|latin", r"^race"),
    (r"veteran|military|served|armed forces|army|navy", r"^veteran|army|navy|military|veteran"),
    (r"disab", r"^disability"),
    (r"relocat|move to", r"relocat"),
    (r"remote|hybrid|on-?site|office|work arrangement", r"remote|hybrid|on-site|relocat"),
    (r"travel", r"travel"),
    (r"salary|compensation|\bpay\b", r"^salary"),
    (r"\bstart\b|available", r"^available_start"),
    (r"notice", r"^notice_period"),
    (r"locat|\blive\b|based|city|time ?zone|metropolitan|\barea\b|reside", r"^location|^city|^state"),
    (r"employer|company|work(?:s|ing)? (?:currently|today)|currently work|where do you work", r"^current_company"),
    (r"title|position|\brole\b", r"^current_title"),
    (r"school|college|university|institution", r"^edu\d+\.school"),
    (r"degree|education|bachelor|master|field of study|major", r"^edu\d+\.degree"),
    (r"graduat", r"^edu\d+\.year"),
    (r"certif", r"^cert\d+"),
    (r"linkedin", r"^linkedin"),
    (r"github", r"^github"),
    (r"website|portfolio|personal site", r"^portfolio"),
    (r"e-?mail", r"^email"),
    (r"phone|mobile|cell", r"^phone"),
    (r"manag|direct reports|supervis|people|team", r"\b(led|managed|supervised|manage)\b.*\b\d+\b|\bteam of\b"),
    (r"\bgaps?\b|career break", r"^gaps|career break"),
    (r"years|experience", r"^years_total"),
]
ALIAS_BONUS = 3.0
_ALIAS_RE = [(re.compile(q, re.I), re.compile(f, re.I)) for q, f in ALIASES]


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokens(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9+#]+(?:[.-][a-z0-9]+)*", (text or "").lower()) if w not in _STOP]


def question_text(f: FieldDescriptor) -> str:
    return " ".join(filter(None, (f.label, f.extra_context, f.placeholder))) or f.name


def top_facts(field: FieldDescriptor, facts: list[Fact], k: int = 5, sensitive: bool = False) -> list[tuple[Fact, float]]:
    """Top-k (fact, score) with score > 0. Sensitive questions only see explicit profile answers."""
    pool = [f for f in facts if not sensitive or f.category in EXPLICIT]
    return rank(question_text(field), pool, k)


def rank(q: str, pool: list[Fact], k: int = 5) -> list[tuple[Fact, float]]:
    """Top-k (fact, score) with score > 0 for the query text q."""
    if not pool:
        return []
    q_tok = Counter(tokens(q))
    df = Counter(t for f in pool for t in set(tokens(f.text)))
    n = len(pool)
    scored = []
    for f in pool:
        ft = set(tokens(f.text))
        s = sum(math.log(1 + n / df[t]) for t in q_tok if t in ft)
        hay = f"{f.key} {f.text}"
        if any(qr.search(q) and fr.search(hay) for qr, fr in _ALIAS_RE):
            s += ALIAS_BONUS
        if s > 0:
            scored.append((f, s))
    scored.sort(key=lambda x: -x[1])
    return scored[:k]
