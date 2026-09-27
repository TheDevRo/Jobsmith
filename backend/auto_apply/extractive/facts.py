"""Fact store: one atomic, verbatim-valued fact per profile item, plus date math.

A Fact's `text` is what the NLI model reads; its `value` is what gets typed into a
form, unchanged. Computed facts (years) carry the number the date calculator produced.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from ..models import UserProfile, WorkExperience

# Categories. Sensitive (EEO/legal) questions may only read facts from EXPLICIT.
LEGAL, EEO, CONTACT, LOGISTICS, ROLE, EDU, CERT, SKILL, SUMMARY, BANK, COMPUTED = (
    "legal", "eeo", "contact", "logistics", "role", "edu", "cert", "skill", "summary", "bank", "computed")
EXPLICIT = {LEGAL, EEO, SUMMARY}

_DECLINE_RE = re.compile(r"\b(decline|prefer not|don'?t wish|do not wish|not to answer|do not want|don'?t want|not disclose|rather not)\b", re.I)


@dataclass(frozen=True)
class Fact:
    key: str
    text: str
    value: str
    category: str

    @property
    def declines(self) -> bool:
        return self.category == EEO and bool(_DECLINE_RE.search(self.value))


# ---------------------------------------------------------------------------
# Date math
# ---------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_month(s: str, today: date) -> int | None:
    """'2021-04', '2021-04-15', '04/2021', 'April 2021', '2021', 'Present' -> months since year 0 (None if unparseable)."""
    s = (s or "").strip().lower()
    if s in ("present", "current", "now", "today", ""):
        return today.year * 12 + today.month - 1 if s else None
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})(?:-\d{1,2})?", s):
        return int(m[1]) * 12 + int(m[2]) - 1
    if m := re.fullmatch(r"(\d{1,2})/(?:\d{1,2}/)?(\d{4})", s):
        return int(m[2]) * 12 + int(m[1]) - 1
    if m := re.fullmatch(r"([a-z]{3})[a-z]*\.? (\d{4})", s):
        if m[1] in _MONTHS:
            return int(m[2]) * 12 + _MONTHS[m[1]] - 1
    if m := re.fullmatch(r"(\d{4})", s):
        return int(m[1]) * 12  # a bare year counts from January
    return None


def role_span(role: WorkExperience, today: date) -> tuple[int, int] | None:
    """[start, end] month indexes, end inclusive. None if either date is unparseable or reversed."""
    a, b = parse_month(role.start_date, today), parse_month(role.end_date or "Present", today)
    if a is None or b is None or b < a:
        return None
    return a, b


def merged_months(roles: list[WorkExperience], today: date) -> int | None:
    """Total months covered by the roles, overlaps merged, gaps excluded. None if any role's dates are unusable."""
    spans = []
    for r in roles:
        s = role_span(r, today)
        if s is None:
            return None  # refuse to guess a number from a role we can't date
        spans.append(s)
    total, cur = 0, None
    for a, b in sorted(spans):
        if cur and a <= cur[1] + 1:
            cur = (cur[0], max(cur[1], b))
        else:
            if cur:
                total += cur[1] - cur[0] + 1
            cur = (a, b)
    if cur:
        total += cur[1] - cur[0] + 1
    return total


def gaps(roles: list[WorkExperience], today: date) -> list[tuple[int, int]]:
    """Uncovered month ranges between the first start and the last end."""
    spans = sorted(s for r in roles if (s := role_span(r, today)))
    out, end = [], None
    for a, b in spans:
        if end is not None and a > end + 1:
            out.append((end + 1, a - 1))
        end = b if end is None else max(end, b)
    return out


def mentions(role: WorkExperience, term: str) -> bool:
    hay = " ".join([role.title, *role.bullets]).lower()
    return re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])", hay) is not None


def years_with(profile: UserProfile, terms: list[str] | None, today: date) -> float | None:
    """Years of experience (merged, fractional). terms=None -> all roles; else roles mentioning any term.
    None when no role qualifies or dates are unusable: the caller must not fill a number then."""
    roles = profile.experience if terms is None else [r for r in profile.experience if any(mentions(r, t) for t in terms)]
    if not roles:
        return None
    m = merged_months(roles, today)
    return None if m is None else m / 12


# ---------------------------------------------------------------------------
# Fact store
# ---------------------------------------------------------------------------

def _yes_no(v: str) -> bool | None:
    v = (v or "").strip().lower()
    return True if v in ("yes", "y", "true") else False if v in ("no", "n", "false") else None


def _month_name(s: str) -> str:
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", (s or "").strip())
    if not m:
        return s
    return date(int(m[1]), int(m[2]), 1).strftime("%B %Y")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if s.strip()]


def build_facts(profile: UserProfile, bank: dict[str, str] | None = None, today: date | None = None) -> list[Fact]:
    """All facts for one application."""
    today = today or date.today()
    p = profile
    F: list[Fact] = []

    def add(key, text, value, cat):
        if value and str(value).strip():
            F.append(Fact(key, text, str(value).strip(), cat))

    # Contact / links
    add("full_name", f"The candidate's name is {p.full_name}.", p.full_name, CONTACT)
    add("email", f"The candidate's email address is {p.email}.", p.email, CONTACT)
    add("phone", f"The candidate's phone number is {p.phone}.", p.phone, CONTACT)
    add("location", f"The candidate lives in {p.location}.", p.location, CONTACT)
    add("city", f"The candidate's city is {p.city}.", p.city, CONTACT)
    add("state", f"The candidate's state is {p.state}.", p.state, CONTACT)
    add("zip", f"The candidate's zip code is {p.zip_code}.", p.zip_code, CONTACT)
    add("street", f"The candidate's street address is {p.street_address}.", p.street_address, CONTACT)
    add("country", f"The candidate lives in the country {p.country}.", p.country, CONTACT)
    add("linkedin", f"The candidate's LinkedIn profile URL is {p.linkedin}.", p.linkedin, CONTACT)
    add("github", f"The candidate's GitHub URL is {p.github}.", p.github, CONTACT)
    add("portfolio", f"The candidate's personal website and portfolio URL is {p.portfolio}.", p.portfolio, CONTACT)

    # Legal / eligibility — only emitted when the profile value is an unambiguous yes/no (else as-is).
    wa = _yes_no(p.work_authorization)
    if wa is not None:
        add("work_authorization", "The candidate is legally authorized to work in the United States." if wa
            else "The candidate is not authorized to work in the United States.", p.work_authorization, LEGAL)
    else:
        add("work_authorization", f"The candidate's work authorization is: {p.work_authorization}.", p.work_authorization, LEGAL)
    sp = _yes_no(p.sponsorship_required)
    if sp is not None:
        add("sponsorship", "The candidate will require visa sponsorship to work." if sp
            else "The candidate does not require visa sponsorship, now or in the future.", p.sponsorship_required, LEGAL)
    o18 = _yes_no(p.over_18)
    if o18 is not None:
        add("over_18", "The candidate is at least 18 years old." if o18 else "The candidate is under 18 years old.", p.over_18, LEGAL)

    # EEO — only what the profile states. A decline is stated as a decline.
    for key, label, v in (("gender", "gender", p.gender), ("race", "race/ethnicity", p.race_ethnicity),
                          ("veteran", "veteran status", p.veteran_status), ("disability", "disability status", p.disability_status)):
        if v and _DECLINE_RE.search(v):
            add(key, f"The candidate declines to disclose their {label}.", v, EEO)
        else:
            add(key, f"The candidate's {label} is: {v}.", v, EEO)

    # Logistics
    add("salary", f"The candidate's desired salary is {p.desired_salary}.", p.desired_salary, LOGISTICS)
    add("available_start", f"The candidate can start: {p.available_start}.", p.available_start, LOGISTICS)
    add("notice_period", f"The candidate's notice period is {p.notice_period}.", p.notice_period, LOGISTICS)

    # Roles
    for i, r in enumerate(p.experience):
        cur = (r.end_date or "Present").strip().lower() in ("present", "current", "now")
        when = f"from {_month_name(r.start_date)} to {'present' if cur else _month_name(r.end_date)}"
        if i == 0 and cur:
            add("current_company", f"The candidate currently works at {r.company}.", r.company, ROLE)
            add("current_title", f"The candidate's current job title is {r.title}.", r.title, ROLE)
        add(f"role{i}.company", f"The candidate worked at {r.company} as {r.title} {when}.", r.company, ROLE)
        add(f"role{i}.title", f"The candidate held the job title {r.title} at {r.company} {when}.", r.title, ROLE)
        for j, b in enumerate(r.bullets):
            add(f"role{i}.bullet{j}", f"At {r.company}, the candidate: {b}.", b, ROLE)

    # Education / certifications / skills
    for i, e in enumerate(p.education):
        add(f"edu{i}.degree", f"The candidate earned a {e.degree} degree from {e.school} in {e.year}.", e.degree, EDU)
        add(f"edu{i}.school", f"The candidate studied at {e.school}.", e.school, EDU)
        add(f"edu{i}.year", f"The candidate graduated in {e.year}.", e.year, EDU)
    for i, c in enumerate(p.certifications):
        add(f"cert{i}", f"The candidate holds the {c} certification.", c, CERT)
    for i, s in enumerate(p.skills):
        add(f"skill{i}", f"The candidate has experience with {s}.", s, SKILL)

    # Summary, one sentence per fact (keeps premises short for the 512-token limit)
    for i, s in enumerate(_sentences(p.summary)):
        add(f"summary{i}", f"The candidate says: {s}", s, SUMMARY)

    # Computed
    yrs = years_with(p, None, today)
    if yrs is not None:
        add("years_total", f"The candidate has {int(yrs)} years of work experience.", str(int(yrs)), COMPUTED)
    if p.experience:
        long_gaps = [g for g in gaps(p.experience, today) if g[1] - g[0] + 1 > 6]
        add("gaps", "The candidate has had a gap in employment longer than 6 months." if long_gaps
            else "The candidate has had no gaps in employment longer than 6 months since their first listed job.",
            "Yes" if long_gaps else "No", COMPUTED)

    # Answer bank (placeholders never count as answers)
    for k, v in (bank or {}).items():
        if v and not (v.startswith("<") and v.endswith(">")):
            add(f"bank.{k}", f"The candidate's saved answer to '{k.replace('_', ' ')}' is: {v}", v, BANK)
    return F
