"""
fact_guard.py — a warning-only check on AI-written documents.

After a resume or cover letter is generated or revised, the names, numbers,
years, percentages and credential words in it are looked up in everything the
candidate has said: the profile, the posting, and their facts. What can't be
found is returned so the UI can say "Check this: …". It never blocks anything.

Ported in spirit from AienTech/dossier (AGPL-3.0) src/lib/ai/fact-guard.ts. It
is a safety net for the strict levels and is tuned to stay quiet about what is
legitimately there (profile dates, the company from the posting, capitals at
the start of a sentence, section headers).

Only "honest" and "tailored" are checked: "embellished" and "fabricated" are
allowed to go beyond the profile, so flagging would only be noise.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger(__name__)

GUARDED_LEVELS = frozenset({"honest", "tailored"})

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100,
}

_SKIP_WORDS = frozenset({
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug",
    "sep", "sept", "oct", "nov", "dec",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    # Capitalised mid-sentence, but not names.
    "i", "i'm", "i’m", "i've", "i’ve", "i'd", "i’d", "i'll", "i’ll", "ok",
    # Resume and letter furniture that legitimately isn't in the sources.
    "present", "current", "dear", "hiring", "manager", "team", "sincerely", "regards",
    "best", "thank", "thanks", "respectfully", "recruiter", "recruiting", "committee",
    "title", "company", "dates", "degree", "school", "year",
})

# Section headers the document parser accepts (resume_generator._parse_resume_sections).
_HEADERS = frozenset({
    "SUMMARY", "PROFESSIONAL SUMMARY", "PROFILE SUMMARY", "PROFILE", "ABOUT ME", "SKILLS",
    "TECHNICAL SKILLS", "CORE COMPETENCIES", "KEY SKILLS", "COMPETENCIES", "EXPERIENCE",
    "PROFESSIONAL EXPERIENCE", "WORK EXPERIENCE", "EMPLOYMENT HISTORY", "CAREER HISTORY",
    "EDUCATION", "EDUCATIONAL BACKGROUND", "ACADEMIC BACKGROUND", "CERTIFICATIONS",
    "CERTIFICATES", "LICENSES", "LICENSES & CERTIFICATIONS", "PROJECTS", "AWARDS",
    "ACHIEVEMENTS",
})

# Credentials said in words rather than as an acronym.
_CREDENTIAL = re.compile(
    r"\b(certified|certification|certificate|licensed|license|accredited|chartered)\b", re.I
)

# A number as people write it: "4,000+", "4 000", "120k", "30%", "2.5", "12", "10 million".
_NUMBER = re.compile(
    r"(?<![^\W_])(\d{1,3}(?:[,.\u00a0\u202f ]\d{3})+|\d+(?:[.,]\d+)?)"
    r"\s*(?:(k|m|b)(?![^\W_])|\s(thousand|million|billion)(?![^\W_]))?%?(?![^\W_])",
    re.I,
)
_THOUSANDS = re.compile(r"^\d{1,3}(?:[,.\u00a0\u202f ]\d{3})+$")
_SCALE = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000,
          "thousand": 1_000, "million": 1_000_000, "billion": 1_000_000_000}
_WORD = re.compile(r"[^\W_][\w&'’.+#-]*")
_CAPITALISED = re.compile(r"[^\W\d_][\w&'’+#-]*")
_SECRET_KEYS = frozenset({"workday_password", "ats_login_password", "workday_email", "password"})


def _number_value(digits: str, suffix: str | None) -> float:
    if _THOUSANDS.match(digits):
        value = float(re.sub(r"\D", "", digits))
    else:
        value = float(digits.replace(",", "."))
    return value * _SCALE.get((suffix or "").lower(), 1)


def _numbers_in(text: str) -> list[tuple[str, float]]:
    found = [
        (m.group(0).strip(), _number_value(m.group(1), m.group(2) or m.group(3)))
        for m in _NUMBER.finditer(text)
    ]
    for m in re.finditer(r"\b([a-z]+)\b", text, re.I):
        value = _NUMBER_WORDS.get(m.group(1).lower())
        if value is not None:
            found.append((m.group(1), float(value)))
    return found


def _trim_end(word: str, marks: str) -> str:
    return word.rstrip(marks)


@dataclass
class FactSources:
    text: str = ""
    words: set[str] = field(default_factory=set)
    numbers: set[float] = field(default_factory=set)


def fact_sources(parts: list[str]) -> FactSources:
    text = "\n".join(p for p in parts if p).lower()
    return FactSources(
        text=text,
        words={_trim_end(w, ".'’") for w in _WORD.findall(text)},
        numbers={v for _, v in _numbers_in(text)},
    )


def _flatten(value) -> list[str]:
    """Every string/number inside a profile-shaped value, secrets excluded."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(value)]
    if isinstance(value, dict):
        return [s for k, v in value.items() if k not in _SECRET_KEYS for s in _flatten(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _flatten(v)]
    return []


_YM = re.compile(r"(\d{4})(?:[-/](\d{1,2}))?")


def _experience_years(profile: dict, today: date | None = None) -> int | None:
    """Whole years the profile's roles add up to (the model likes "8 years of experience")."""
    today = today or date.today()
    months = 0
    for role in (profile or {}).get("experience") or []:
        if not isinstance(role, dict):
            continue
        start = _YM.search(str(role.get("start_date") or ""))
        if not start:
            continue
        end_raw = str(role.get("end_date") or "")
        end = _YM.search(end_raw)
        sy, sm = int(start.group(1)), int(start.group(2) or 1)
        ey, em = (int(end.group(1)), int(end.group(2) or 12)) if end else (today.year, today.month)
        months += max(0, (ey - sy) * 12 + (em - sm))
    return months // 12 if months else None


def build_sources(
    profile: dict | None,
    job: dict | None = None,
    app_facts: list[str] | None = None,
    extra: list[str] | None = None,
    today: date | None = None,
) -> FactSources:
    """Everything a claim may be backed by: profile (with its facts), posting, application facts."""
    profile = profile or {}
    job = job or {}
    years = _experience_years(profile, today)
    # A listed certification backs saying "certified" (the credential words are checked by stem).
    certified = "certified certification" if profile.get("certifications") else ""
    parts = [
        certified,
        *_flatten(profile),
        job.get("title") or "", job.get("company") or "",
        job.get("location") or "", job.get("description") or "",
        *(app_facts or []),
        *(extra or []),
        "" if years is None else str(years),
    ]
    return fact_sources([str(p) for p in parts])


def _sentence_starts(text: str) -> set[int]:
    starts = {re.match(r"\s*(?:[-*•·]\s+)?", text).end()}
    for m in re.finditer(r"(?:[.!?:;][\"”’)]?\s+|\n)\s*(?:[-*•·]\s+)?(?=\S)", text.rstrip()):
        starts.add(m.end())
    return starts


def _strip_headers(text: str) -> str:
    """Blank out section-header lines (SUMMARY, EXPERIENCE…) so they aren't read as acronyms."""
    return "\n".join(
        "" if re.sub(r"^[#*_`]+\s*", "", ln.strip()).upper().rstrip(": ") in _HEADERS else ln
        for ln in text.split("\n")
    )


def unverified_facts(text: str, sources: FactSources) -> list[str]:
    """What in `text` the sources don't back, in order, once each."""
    flags: list[str] = []

    def flag(value: str) -> None:
        if value not in flags:
            flags.append(value)

    text = _strip_headers(text)

    # Names: capitalised words that don't start a sentence, and acronyms anywhere.
    starts = _sentence_starts(text)
    for m in _CAPITALISED.finditer(text):
        if not m.group(0)[0].isupper():
            continue
        word = _trim_end(re.sub(r"['’]s$", "", m.group(0)), "-'’")
        lower = word.lower()
        # Two capitals or more (PhD, MBA, UX) mark an acronym or credential, even at a sentence start.
        acronym = sum(c.isupper() for c in word) >= 2
        if m.start() in starts and not acronym:
            continue
        if lower in _SKIP_WORDS or lower in sources.words or lower in sources.text:
            continue
        flag(word)

    # Numbers, years and percentages, however they're formatted.
    for raw, value in _numbers_in(text):
        if value in sources.numbers:
            continue
        # "one of the…", "a single…" aren't claims.
        if raw.isalpha() and value <= 2:
            continue
        flag(raw)

    # Credentials in words: "certified", "licensed"…
    for m in _CREDENTIAL.finditer(text):
        if m.group(1).lower()[:7] not in sources.text:
            flag(m.group(1))
    return flags


def check(
    text: str,
    profile: dict | None,
    job: dict | None,
    honesty_level: str,
    app_facts: list[str] | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    """Flags for one document, or [] when the level opts out or the text is empty."""
    if honesty_level not in GUARDED_LEVELS or not (text or "").strip():
        return []
    try:
        return unverified_facts(text, build_sources(profile, job, app_facts, extra))
    except Exception:  # a warning-only check must never break a generation
        logger.exception("fact guard failed")
        return []


def check_documents(
    resume: str,
    cover_letter: str,
    profile: dict | None,
    job: dict | None,
    honesty_level: str,
    app_facts: list[str] | None = None,
) -> dict[str, list[str]]:
    """Flags for both documents of an application: {"resume": [...], "cover_letter": [...]}."""
    return {
        "resume": check(resume, profile, job, honesty_level, app_facts),
        "cover_letter": check(cover_letter, profile, job, honesty_level, app_facts),
    }
