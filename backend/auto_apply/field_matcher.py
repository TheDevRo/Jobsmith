"""
auto_apply/field_matcher.py — Deterministic profile→field matching.

Runs BEFORE the answer bank and the LLM in map_fields_to_values. Matches the
common ~90% of application-form fields (contact info, address, links, salary,
work authorization, EEO, education, availability) against the user profile
with regex/keyword rules, so filling them never depends on LLM output.

Matching signals, in priority order:
  1. The HTML `autocomplete` attribute (exact token — highest precision).
  2. Ordered regex rules over a normalized haystack built from the field's
     label, name, placeholder, id, autocomplete and extra_context.

When a field carries an options list (select / radio), the profile value is
resolved to the best-matching option text so the extension clicks an option
that actually exists. US state abbreviations and common country aliases are
expanded during option resolution. EEO fields fall back to the
"Prefer not to answer"-style option when the profile has no value.

Fields that don't match any rule (or whose profile value is empty) are left
for the answer bank / LLM.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field as dc_field
from typing import Callable, Optional

from .models import FieldDescriptor, FieldValue, UserProfile

logger = logging.getLogger(__name__)

# Types a rule may apply to. "select" covers native selects AND combobox
# widgets (the snapshot maps both to "select").
_TEXTY = ("text", "email", "tel", "url", "number", "textarea", "select", "date", "password")
_CHOICE = ("select", "radio", "checkbox", "text")

# Options that mean "decline to answer" on EEO widgets.
_DECLINE_HINTS = (
    "prefer not", "decline", "do not wish", "don't wish", "dont wish",
    "not wish", "choose not", "rather not", "no answer", "not to say",
    "not specified", "don't want", "do not want", "not disclose",
)

# Placeholder options that must never be picked during fuzzy fallback.
_PLACEHOLDER_RE = re.compile(r"^(select|choose|please|pick)\b|^[-–—.\s]*$")

_US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
    "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah",
    "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}
_US_STATES_REV = {v: k for k, v in _US_STATES.items()}

_COUNTRY_ALIASES = {
    "united states": ["usa", "us", "united states of america", "america", "u s a", "u s"],
    "united kingdom": ["uk", "great britain", "england"],
    "canada": ["ca"],
}

# Degree strings ("BS Computer Science") → the education-level buckets ATS
# dropdowns actually offer ("Bachelor's Degree").
_DEGREE_LEVELS: list[tuple[str, list[str]]] = [
    (r"\b(ph\.?d|doctor)", ["phd", "doctorate", "doctoral degree"]),
    (r"\bmba\b", ["mba", "master's degree", "masters"]),
    (r"\b(ms|m\.?s\.?c?|ma|m\.a|master)\b", ["master's degree", "masters", "master"]),
    (r"\b(bs|b\.?s\.?c?|ba|b\.a|bachelor)\b", ["bachelor's degree", "bachelors", "bachelor"]),
    (r"\b(associate|a\.?a\.?s?)\b", ["associate's degree", "associate degree", "associate"]),
]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#.\s]", " ", (s or "").lower())).strip()


def _decamel(s: str) -> str:
    """Split camelCase so Workday-style ids ("workExperience-1--startDate")
    become rule-matchable words. Applied to haystacks only — option labels
    like "PhD" must not be split."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s or "")


def _tokens(s: str) -> set[str]:
    return set(_norm(s).split())


def _expand_candidates(value: str) -> list[str]:
    """Return [value, alias1, ...] for state/country style values."""
    v = _norm(value)
    out = [value]
    if v in _US_STATES:
        out.append(_US_STATES[v])
    elif v in _US_STATES_REV:
        out.append(_US_STATES_REV[v])
    for canonical, aliases in _COUNTRY_ALIASES.items():
        if v == canonical:
            out.extend(aliases)
        elif v in aliases:
            out.append(canonical)
    for pattern, expansions in _DEGREE_LEVELS:
        if re.search(pattern, v):
            out.extend(expansions)
            break
    return out


def _option_score(want: str, opt: str) -> int:
    """0-100 similarity between a desired value and an option label."""
    w, t = _norm(want), _norm(opt)
    if not w or not t:
        return 0
    if w == t:
        return 100
    wt, tt = _tokens(w), _tokens(t)
    if w in ("yes", "no"):
        if w == "yes":
            if re.match(r"^y(es)?\b", t):
                return 90
            return 80 if "yes" in tt else 0
        if re.match(r"^n(o)?\b", t):
            return 90
        return 75 if (tt & {"no", "not", "none", "never"}) else 0
    if wt <= tt and tt <= wt:
        return 95
    if wt <= tt:
        return max(60, 88 - (len(tt) - len(wt)))
    if tt <= wt:
        return max(60, 80 - (len(wt) - len(tt)))
    inter = len(wt & tt)
    if inter:
        jac = inter / len(wt | tt)
        bonus = 25 if (w in t or t in w) else 0
        return round(40 * jac) + bonus
    return 0


def best_option(value: str, options: list[str], threshold: int = 55) -> Optional[str]:
    """Pick the option text that best matches *value*, or None."""
    if not value or not options:
        return None
    best, best_score = None, 0
    for opt in options:
        if _PLACEHOLDER_RE.match(_norm(opt) or ""):
            continue
        score = max(_option_score(cand, opt) for cand in _expand_candidates(value))
        # Prefer shorter options on ties (avoids "No" → "Not applicable"-style grabs).
        if score > best_score or (score == best_score and best and len(opt) < len(best)):
            best, best_score = opt, score
    return best if best_score >= threshold else None


def _decline_option(options: list[str]) -> Optional[str]:
    for opt in options or []:
        t = _norm(opt)
        if any(h in t for h in _DECLINE_HINTS):
            return opt
    return None


# ---------------------------------------------------------------------------
# Repeating-section entry index
# ---------------------------------------------------------------------------

def _entry_index(f: FieldDescriptor) -> int:
    """Which work-history / education entry a field belongs to.

    Greenhouse/Rails array names ("...[educations_attributes][1][school]")
    are 0-based; Workday-style separator ids ("workExperience-2--company")
    are 1-based. Fields with no index belong to entry 0.
    """
    for src in (f.name or "", f.field_id or ""):
        m = re.search(r"\[(\d{1,2})\]", src)
        if m:
            return int(m.group(1))
        m = re.search(r"(?:^|[._\-])(\d{1,2})(?:[._\-]|$)", src)
        if m:
            return max(0, int(m.group(1)) - 1)
    return 0


def _exp_at(p: UserProfile, f: FieldDescriptor):
    idx = _entry_index(f)
    return p.experience[idx] if 0 <= idx < len(p.experience) else None


def _edu_at(p: UserProfile, f: FieldDescriptor):
    idx = _entry_index(f)
    return p.education[idx] if 0 <= idx < len(p.education) else None


# ---------------------------------------------------------------------------
# Value getters
# ---------------------------------------------------------------------------

def _name_parts(p: UserProfile) -> list[str]:
    return (p.full_name or "").split()


def _first_name(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    parts = _name_parts(p)
    return parts[0] if parts else ""


def _last_name(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    parts = _name_parts(p)
    return parts[-1] if len(parts) > 1 else ""


def _middle_name(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    mid = p.middle_name or ""
    if mid and "initial" in hay:
        return mid[0]
    return mid


def _phone_country_code(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    m = re.match(r"^\s*(\+\d{1,3})", p.phone or "")
    if m:
        return m.group(1)
    return "+1" if _norm(p.country) in ("united states", "usa", "us") else ""


def _city(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    if p.city:
        return p.city
    return (p.location or "").split(",")[0].strip()


def _state(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    if p.state:
        return p.state
    parts = (p.location or "").split(",")
    return parts[1].strip() if len(parts) > 1 else ""


def _location(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    if p.location:
        return p.location
    if p.city and p.state:
        return f"{p.city}, {p.state}"
    return ""


def _years_experience(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    return str(p.years_of_experience()) if p.experience else ""


def _current_company(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    e = _exp_at(p, f)
    return e.company if e else ""


def _current_title(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    e = _exp_at(p, f)
    return e.title if e else ""


def _exp_start(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    e = _exp_at(p, f)
    return e.start_date if e else ""


def _exp_end(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    e = _exp_at(p, f)
    return e.end_date if e else ""


def _exp_description(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    e = _exp_at(p, f)
    return "\n".join(f"• {b}" for b in e.bullets) if e and e.bullets else ""


def _school(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    e = _edu_at(p, f)
    return e.school if e else ""


def _degree(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    e = _edu_at(p, f)
    return e.degree if e else ""


def _grad_year(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    e = _edu_at(p, f)
    return e.year if e else ""


def _skills(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    return ", ".join(p.skills) if p.skills else ""


def _hispanic(p: UserProfile, f: FieldDescriptor, hay: str) -> str:
    r = _norm(p.race_ethnicity)
    if not r:
        return ""  # EEO decline fallback kicks in
    return "Yes" if ("hispanic" in r or "latin" in r) else "No"


def _attr(name: str) -> Callable:
    return lambda p, f, hay: getattr(p, name, "") or ""


def _const(value: str) -> Callable:
    return lambda p, f, hay: value


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

@dataclass
class _Rule:
    key: str
    pattern: str                       # regex over the normalized haystack
    getter: Callable                   # (profile, field, hay) -> str
    negative: str = ""                 # regex that vetoes the match
    types: tuple = _TEXTY
    eeo: bool = False                  # decline-option fallback when value empty
    options_only: bool = False         # only apply when the field has options
    confidence: float = 0.95
    _re: re.Pattern = dc_field(init=False, repr=False, default=None)
    _neg_re: Optional[re.Pattern] = dc_field(init=False, repr=False, default=None)

    def __post_init__(self):
        self._re = re.compile(self.pattern)
        self._neg_re = re.compile(self.negative) if self.negative else None


# Order matters — first matching rule with a non-empty resolvable value wins.
_RULES: list[_Rule] = [
    # --- Names (specific before generic "name") ---
    _Rule("first_name", r"\b(first name|given name|fname|forename)\b", _first_name),
    _Rule("last_name", r"\b(last name|family name|surname|lname)\b", _last_name),
    _Rule("middle_name", r"\bmiddle (name|initial)\b", _middle_name),
    _Rule("preferred_name", r"\b(preferred name|nickname|goes by|known as)\b", _first_name),
    _Rule("full_name", r"\b(full name|legal name|your name|candidate name|applicant name|complete name)\b|^name$",
          _attr("full_name"),
          negative=r"\b(user ?name|company|employer|school|university|referr|recruiter|reference|manager|contact person|father|mother|emergency)\b"),

    # --- Contact ---
    _Rule("email", r"\be ?mail\b", _attr("email")),
    _Rule("phone_country_code", r"\b(country code|phone code|dial code)\b", _phone_country_code),
    _Rule("phone_type", r"\b(phone (device )?type|device type)\b", _const("Mobile"), confidence=0.8),
    _Rule("phone", r"\b(phone|mobile|cell|telephone|contact number)\b", _attr("phone"),
          negative=r"\bext(ension)?\b"),

    # --- Address ---
    _Rule("address_line2", r"\b(address line ?2|line ?2|apt|apartment|suite|unit number|unit)\b",
          _attr("street_address_2")),
    _Rule("street_address", r"\b(street address|address line ?1|home address|mailing address|street|address)\b",
          _attr("street_address"), negative=r"\b(email|line ?2|country|city|state|zip|postal|web)\b"),
    _Rule("city", r"\b(city|town|municipality)\b", _city),
    _Rule("zip", r"\b(zip|postal( code)?|postcode)\b", _attr("zip_code")),
    _Rule("state", r"\b(state|province|county)\b", _state,
          negative=r"\b(united states|statement)\b"),
    _Rule("country", r"\bcountry\b", _attr("country"), negative=r"\bcountry code\b"),
    _Rule("location", r"\b(location|city and state|where (are you|do you) (located|based|live|reside))\b",
          _location),

    # --- Links ---
    _Rule("linkedin", r"\blinked ?in\b", _attr("linkedin")),
    _Rule("github", r"\bgit ?hub\b", _attr("github")),
    _Rule("portfolio", r"\b(portfolio|personal (web ?site|site|url)|website|web ?site url|other url)\b",
          _attr("portfolio"), negative=r"\b(company|employer) (web ?site|url)\b"),

    # --- Repeating work-history / education entries ---
    # Must precede the availability rules: an employment/education "Start
    # Date" must never be answered with available_start. First-match-wins
    # means an empty getter value also acts as a guard (falls to the LLM
    # instead of a wrong deterministic fill).
    _Rule("exp_company",
          r"(?=.*\b(employments?|work ?experience|work ?history|previous ?employer)\b)(?=.*\b(company|employer)\b)",
          _current_company),
    _Rule("exp_title",
          r"(?=.*\b(employments?|work ?experience|work ?history)\b)(?=.*\b(title|role|position)\b)",
          _current_title),
    _Rule("exp_start",
          r"(?=.*\b(employments?|work ?experience|work ?history)\b)(?=.*\b(start|from)\b)",
          _exp_start),
    _Rule("exp_end",
          r"(?=.*\b(employments?|work ?experience|work ?history)\b)(?=.*\b(end|until|to date)\b)",
          _exp_end),
    _Rule("exp_description",
          r"(?=.*\b(employments?|work ?experience|work ?history|position)\b)(?=.*\b(description|duties|responsibilities)\b)",
          _exp_description),
    _Rule("edu_end",
          r"(?=.*\b(educations?|school|university|degree)\b)(?=.*\b(end|graduation|completion|to date)\b)",
          _grad_year),
    _Rule("edu_start",  # no start-year in the profile — block availability misfill
          r"(?=.*\b(educations?|school|university|degree)\b)(?=.*\bstart\b)",
          _const("")),

    # --- Compensation / availability ---
    _Rule("salary", r"\b(salary|compensation|desired pay|expected pay|pay (rate|expectation|requirement)|rate of pay|hourly rate)\b",
          _attr("desired_salary")),
    _Rule("notice_period", r"\b(notice period|notice required|weeks? (of )?notice|current notice)\b",
          _attr("notice_period")),
    _Rule("start_date", r"\b(start date|earliest (possible )?(start|date)|available to start|availability date|date available|when (can|could) you start)\b",
          _attr("available_start")),

    # --- Work authorization / screening ---
    _Rule("work_auth", r"\b(work authorization|authoriz(ed|ation) to work|legally (authorized|eligible|able|permitted)|eligible to work|right to work|work permit|lawfully (work|employed)|work eligibility)\b",
          _attr("work_authorization"), types=_CHOICE),
    _Rule("sponsorship", r"\bsponsor", _attr("sponsorship_required"), types=_CHOICE),
    _Rule("over_18", r"\b(at least 18|18 (years|or older)|over 18|minimum age|legal age|age requirement)\b",
          _attr("over_18"), types=_CHOICE),
    _Rule("agree_terms", r"\b(i (agree|certify|acknowledge|consent|confirm|accept)|terms (and|&) conditions|privacy (policy|notice)|certify that|acknowledge)\b",
          _const("Yes"), types=("checkbox",), confidence=0.55),

    # --- EEO / demographics ---
    _Rule("hispanic", r"\b(hispanic|latino|latinx)\b", _hispanic, types=_CHOICE, eeo=True),
    _Rule("gender", r"\bgender\b|\bsex\b", _attr("gender"),
          negative=r"\b(orientation|sexual)\b", types=_CHOICE, eeo=True),
    _Rule("race", r"\b(race|ethnic)", _attr("race_ethnicity"), types=_CHOICE, eeo=True),
    _Rule("veteran", r"\b(veteran|military|armed forces|uniformed service)\b",
          _attr("veteran_status"), types=_CHOICE, eeo=True),
    _Rule("disability", r"\bdisab", _attr("disability_status"), types=_CHOICE, eeo=True),
    _Rule("eeo_decline_only", r"\b(sexual orientation|lgbtq|transgender|pronoun)\b",
          _const(""), types=_CHOICE, eeo=True, options_only=True),

    # --- Experience / education ---
    _Rule("years_experience", r"\b(years of (\w+ )?experience|experience in years|how many years)\b",
          _years_experience,
          # Skill-specific ("years of experience with Python") must not be
          # answered with total career years — leave those to the LLM.
          negative=r"\b(with|using)\b|experience in (?!years)\w"),
    _Rule("current_company", r"\b(current (employer|company)|most recent (employer|company)|present employer|company name|employer name)\b",
          _current_company),
    _Rule("current_title", r"\b((current|most recent|present) (job )?(title|role|position)|job title)\b",
          _current_title),
    _Rule("school", r"\b(school|university|college|alma mater|institution)\b", _school),
    _Rule("degree", r"\b(degree|education level|highest (level of )?education|qualification)\b", _degree),
    _Rule("grad_year", r"\bgraduat", _grad_year),
    _Rule("skills", r"\bskills?\b", _skills, types=("text", "textarea"),
          negative=r"\b(why|describe|how|what makes)\b"),

    # --- Credentials (never let the LLM near these) ---
    _Rule("password", r"\bpassword\b", _attr("ats_login_password"), types=("password", "text")),
]

# autocomplete attribute → rule key (exact token match, checked first)
_AUTOCOMPLETE_MAP: dict[str, str] = {
    "given-name": "first_name",
    "additional-name": "middle_name",
    "family-name": "last_name",
    "name": "full_name",
    "email": "email",
    "tel": "phone",
    "tel-national": "phone",
    "tel-country-code": "phone_country_code",
    "street-address": "street_address",
    "address-line1": "street_address",
    "address-line2": "address_line2",
    "address-level2": "city",
    "address-level1": "state",
    "postal-code": "zip",
    "country": "country",
    "country-name": "country",
    "organization": "current_company",
    "organization-title": "current_title",
    "url": "portfolio",
    "new-password": "password",
    "current-password": "password",
}

_RULES_BY_KEY = {r.key: r for r in _RULES}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def match_profile_fields(
    profile: UserProfile,
    fields: list[FieldDescriptor],
) -> dict[str, FieldValue]:
    """
    Deterministically resolve as many fields as possible from the profile.

    Returns {field_id: FieldValue} for resolved fields only. Unresolved
    fields are simply absent — the caller sends them on to the answer bank
    and the LLM.
    """
    out: dict[str, FieldValue] = {}
    for f in fields:
        try:
            fv = _match_one(profile, f)
        except Exception:
            logger.exception("field_matcher: rule evaluation failed for %r", f.field_id)
            fv = None
        if fv is not None:
            out[f.field_id] = fv
    if out:
        logger.debug(
            "field_matcher: deterministically resolved %d/%d field(s): %s",
            len(out), len(fields), sorted(out.keys()),
        )
    return out


def _find_rule(ftype: str, f: FieldDescriptor, hay: str) -> Optional[_Rule]:
    for r in _RULES:
        if ftype not in r.types:
            continue
        if r.options_only and not f.options:
            continue
        if not r._re.search(hay):
            continue
        if r._neg_re and r._neg_re.search(hay):
            continue
        return r
    return None


def _match_one(profile: UserProfile, f: FieldDescriptor) -> Optional[FieldValue]:
    ftype = (f.field_type or "text").lower()
    if ftype == "file":
        return None  # handled by the dedicated file phase

    # Two-pass haystack: the field's own label/name/placeholder first;
    # extra_context (fieldset legend / section heading) only as a fallback —
    # group context is shared across sibling fields and must not outvote a
    # field's own label (e.g. a "Sponsorship?" field inside a
    # "Work Authorization" section).
    hay_own = _norm(_decamel(" ".join(filter(None, (
        f.label, f.name, f.placeholder, f.field_id, f.autocomplete,
    )))))
    hay_full = _norm(" ".join(filter(None, (hay_own, _decamel(f.extra_context)))))
    if not hay_full:
        return None

    rule = None
    hay = hay_own
    ac = (f.autocomplete or "").strip().lower()
    if ac in _AUTOCOMPLETE_MAP:
        rule = _RULES_BY_KEY[_AUTOCOMPLETE_MAP[ac]]
        if ftype not in rule.types and ftype != "select":
            rule = None

    if rule is None and hay_own:
        rule = _find_rule(ftype, f, hay_own)
    if rule is None and hay_full != hay_own:
        rule = _find_rule(ftype, f, hay_full)
        hay = hay_full

    if rule is None:
        return None

    value = (rule.getter(profile, f, hay) or "").strip()

    # Resolve against the field's options so we always emit a clickable choice.
    if f.options:
        resolved = best_option(value, f.options) if value else None
        if resolved is None and rule.eeo:
            resolved = _decline_option(f.options)
        if resolved is None:
            return None  # nothing safe to click — let the LLM try
        value = resolved
    elif not value:
        if rule.eeo and ftype in ("text", "textarea"):
            value = "Prefer not to answer"
        else:
            return None

    action = "select" if f.options else ("check" if ftype == "checkbox" else "fill")
    return FieldValue(
        field_id=f.field_id,
        value=value,
        action=action,
        confidence=rule.confidence,
        source="profile",
    )
