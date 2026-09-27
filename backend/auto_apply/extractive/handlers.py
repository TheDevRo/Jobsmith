"""Per-kind handlers and the confidence gate. Each returns a Decision; only essays touch an LLM.

Source guarantee per handler:
  choice -> value is one of field.options          (origin "option")
  text   -> value equals a Fact.value verbatim      (origin "fact:<key>")
  number -> value computed from role dates          (origin "computed")
  essay  -> LLM text, flagged for review            (origin "essay")
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date

from ..models import FieldDescriptor, UserProfile

from .facts import Fact, years_with
from .retrieve import question_text

CHOICE, TEXT, NUMBER, ESSAY, SKIP = "choice", "text", "number", "essay", "skip"

_ESSAY_RE = re.compile(r"^(why|describe|tell us|tell me|explain|what makes|share|is there anything|anything else)\b|cover letter", re.I)
_COUNT_RE = re.compile(r"\bhow many\b", re.I)
_YEARS_RE = re.compile(r"\byears?\b.*\bexperience\b|\bexperience\b.*\byears?\b", re.I)
_SENSITIVE_RE = re.compile(
    r"sponsor|visa|h-?1b|citizen|permanent resident|green card|authori[sz]|eligib|legally|i-9|criminal|convict|felony|"
    r"arrest|disab|veteran|military|served|gender|\bsex\b|race|ethnic|hispanic|latin|orientation|pronoun|clearance|"
    r"background check|drug|non-?compete|non-?solicit|terminat|relative|at least 18|\bage\b", re.I)
_PLACEHOLDER_RE = re.compile(r"^\s*(select|choose|please|pick|--)|^[-–—.\s]*$", re.I)
_DECLINE_RE = re.compile(r"decline|prefer not|don'?t wish|do not wish|not to answer|do not want|rather not|not to say|not disclose", re.I)
# Words that make a years question generic (all roles) rather than about one skill.
_TOTAL_TERMS = {"professional", "work", "total", "overall", "full-time", "full time", "paid", ""}


@dataclass
class Decision:
    value: str = ""
    kind: str = SKIP
    origin: str = "none"            # option | fact:<key> | computed | essay | none
    confidence: float = 0.0
    reason: str = ""
    scores: dict = field(default_factory=dict)
    facts: list = field(default_factory=list)

    @property
    def filled(self) -> bool:
        return bool(self.value)


def classify(f: FieldDescriptor) -> str:
    t = (f.field_type or "text").lower()
    label = f.label or ""
    if t in ("file", "password", "date", "hidden"):
        return SKIP
    if _YEARS_RE.search(label) or _COUNT_RE.search(label):
        return NUMBER
    if f.options:
        return CHOICE
    if t == "textarea" or _ESSAY_RE.search(label.strip()):
        return ESSAY
    if t == "checkbox":
        return SKIP  # a bare consent/attestation box: always the user's call
    return TEXT


def is_sensitive(f: FieldDescriptor) -> bool:
    return bool(_SENSITIVE_RE.search(question_text(f)))


def gate(scores: list[tuple[str, float]], T: float, M: float) -> tuple[str | None, str]:
    """Fill with the top candidate iff its score >= T and it beats the runner-up by >= M."""
    if not scores:
        return None, "no candidates"
    ranked = sorted(scores, key=lambda x: -x[1])
    best, p1 = ranked[0]
    p2 = ranked[1][1] if len(ranked) > 1 else 0.0
    if p1 < T:
        return None, f"top {p1:.2f} < T {T:.2f}"
    if p1 - p2 < M or p1 == p2:  # a tie never "beats" the runner-up, even with M = 0
        return None, f"margin {p1 - p2:.2f} < M {M:.2f}" if p1 != p2 else "tie with runner-up"
    return best, f"top {p1:.2f}, margin {p1 - p2:.2f}"


def _hyp(q: str, answer: str) -> str:
    return f"The answer to '{q}' is '{answer}'."


def choice(f: FieldDescriptor, facts: list[Fact], nli, T: float, M: float) -> Decision:
    """Score every option against one premise made of all retrieved facts; the gate decides."""
    q = f.label or question_text(f)
    declined = any(x.declines for x in facts)
    cands = [o for o in f.options if not _PLACEHOLDER_RE.search(o) and (declined or not _DECLINE_RE.search(o))]
    d = Decision(kind=CHOICE, facts=[x.key for x in facts])
    if not facts or not cands:
        d.reason = "no facts retrieved" if not facts else "no answerable options"
        return d
    premise = " ".join(x.text for x in facts)
    d.scores = dict(zip(cands, nli.entail([(premise, _hyp(q, o)) for o in cands]), strict=True))
    best, d.reason = gate(list(d.scores.items()), T, M)
    if best is not None:
        d.value, d.origin, d.confidence = best, "option", d.scores[best]
    return d


_FORMAT = {"email": lambda v: "@" in v, "url": lambda v: v.startswith("http"),
           "tel": lambda v: sum(c.isdigit() for c in v) >= 7}


def text(f: FieldDescriptor, facts: list[Fact], nli, T: float, M: float) -> Decision:
    """Pick the fact whose value answers the question (each judged against its own fact); entered verbatim."""
    q = f.label or question_text(f)
    ok = _FORMAT.get((f.field_type or "").lower(), lambda v: True)
    cands = [x for x in facts if len(x.value) <= 80 and ok(x.value)]  # a sentence is not a short-text answer
    d = Decision(kind=TEXT, facts=[x.key for x in facts])
    if not cands:
        d.reason = "no short fact values"
        return d
    probs = nli.entail([(x.text, _hyp(q, x.value)) for x in cands])
    best_by_value: dict[str, tuple[float, Fact]] = {}
    for x, p in zip(cands, probs, strict=True):
        if p > best_by_value.get(x.value, (-1, None))[0]:
            best_by_value[x.value] = (p, x)
    d.scores = {v: p for v, (p, _) in best_by_value.items()}
    best, d.reason = gate(list(d.scores.items()), T, M)
    if best is not None:
        p, x = best_by_value[best]
        d.value, d.origin, d.confidence = x.value, f"fact:{x.key}", p
    return d


def skill_terms(label: str) -> list[str] | None:
    """Skill a years question asks about; None = all roles. 'Years of experience with JavaScript/TypeScript' -> [...]"""
    m = re.search(r"\b(?:with|in|using)\s+(.+?)\s*\??$", label, re.I) or \
        re.search(r"years of (.+?) experience", label, re.I)
    term = (m[1] if m else "").strip().lower()
    if term in _TOTAL_TERMS:
        return None
    return [t.strip() for t in re.split(r"/|,| or | and ", term) if t.strip()]


def _bucket(opt: str) -> tuple[float, float] | None:
    """Option text -> [lo, hi) range in years. None when the option isn't a numeric range."""
    o = opt.lower().replace("–", "-")
    if re.fullmatch(r"\s*(none|0|no experience)\s*(years?)?\s*", o):
        return (0, 0)  # zero years: never chosen, since we abstain when no role qualifies
    if m := re.search(r"less than (\d+)", o):
        return (0, int(m[1]))
    if m := re.search(r"(?:more than|over) (\d+)", o):
        return (int(m[1]) + 1e-9, math.inf)
    if m := re.search(r"(\d+)\s*\+", o):
        return (int(m[1]), math.inf)
    if m := re.search(r"(\d+)\s*-\s*(\d+)", o):
        return (int(m[1]), int(m[2]) + 1)  # "3-5 years" covers 3.0 up to (not incl.) 6.0 when floored
    return None


def number(f: FieldDescriptor, profile: UserProfile, today: date) -> Decision:
    d = Decision(kind=NUMBER)
    if not _YEARS_RE.search(f.label or ""):
        d.reason = "only years can be computed; other counts are left for the user (NLI can't compare numbers)"
        return d
    terms = skill_terms(f.label or "")
    yrs = years_with(profile, terms, today)
    if yrs is None:
        d.reason = f"no dated role mentions {terms}" if terms else "no dated roles"
        return d
    n = math.floor(yrs)
    d.scores = {"years": round(yrs, 2), "terms": terms}
    if not f.options:
        d.value, d.origin, d.confidence, d.reason = str(n), "computed", 1.0, f"{yrs:.2f} years -> {n}"
        return d
    hits = [o for o in f.options if (b := _bucket(o)) and b != (0, 0) and b[0] <= n < b[1]]
    if len(hits) == 1:
        d.value, d.origin, d.confidence, d.reason = hits[0], "computed", 1.0, f"{yrs:.2f} years -> {hits[0]}"
    else:
        d.reason = f"{yrs:.2f} years matches {len(hits)} options"
    return d


_REFUSAL_RE = re.compile(r"\b(cannot|can't|unable to|not able to|am not able to) (answer|provide|generate)|no (answer|information)"
                         r" (can|is|regarding)|not (explicitly )?stated in the (candidate )?profile|must return an empty",
                         re.I)


def clean_essay(raw: str) -> str:
    """Plain essay text from generate_answer, or "" when the model returned nothing usable.

    the auto_apply_answer prompt asks for a JSON array, so unwrap ["..."] / {"answer": ...};
    a refusal ("I cannot answer this based on the profile") is not an essay and goes back to the user."""
    s = (raw or "").strip()
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.S).strip()
    if s.startswith("```"):
        s = s.strip("`").removeprefix("json").strip()
    if s[:1] in "[{":
        try:
            v = json.loads(s)
            items = v if isinstance(v, list) else [v]
            s = " ".join(str(x.get("answer", "") if isinstance(x, dict) else x) for x in items).strip()
        except json.JSONDecodeError:
            pass
    return "" if _REFUSAL_RE.search(s) else s
