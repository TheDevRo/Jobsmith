"""
facts.py — short true statements the candidate wants the AI to know.

Two scopes: profile-wide ("Facts about me", a list in config.yaml under
profile.facts) and per-application (the application_facts table). Both are
injected into every writing prompt by `facts_block`, which ai_engine appends
in code, so a user-overridden prompt template can't lose them.
"""

from __future__ import annotations

MAX_FACTS = 50
MAX_FACT_CHARS = 1000


def clean_fact(text) -> str:
    """Return the trimmed fact text, or raise ValueError with a user-facing reason."""
    if not isinstance(text, str):
        raise ValueError("A fact must be text")
    text = text.strip()
    if not text:
        raise ValueError("A fact must not be empty")
    if len(text) > MAX_FACT_CHARS:
        raise ValueError(f"A fact must be at most {MAX_FACT_CHARS} characters")
    return text


def check_new_count(current: int) -> None:
    if current >= MAX_FACTS:
        raise ValueError(f"You can keep at most {MAX_FACTS} facts here; remove one first")


def clean_fact_list(items) -> list[str]:
    """Validate a whole list (used when a profile save carries `facts`)."""
    if not isinstance(items, list):
        raise ValueError("facts must be a list")
    if len(items) > MAX_FACTS:
        raise ValueError(f"You can keep at most {MAX_FACTS} facts here")
    return [clean_fact(i) for i in items]


def profile_facts(profile: dict | None) -> list[str]:
    """The profile-wide facts, tolerant of a missing or malformed value."""
    raw = (profile or {}).get("facts")
    if not isinstance(raw, list):
        return []
    return [f.strip() for f in raw if isinstance(f, str) and f.strip()]


def facts_block(
    about_me: list[str] | None,
    about_application: list[str] | None,
    honesty_level: str = "honest",
) -> str:
    """The prompt section for both fact lists ("" when there are none)."""
    about_me = [f for f in (about_me or []) if f]
    about_application = [f for f in (about_application or []) if f]
    if not about_me and not about_application:
        return ""
    lines = [
        "\nFACTS FROM THE CANDIDATE",
        "The candidate wrote these themselves, so they are true. Use the ones "
        "that help this document, in your own words, rather than listing them. "
        "Where a fact says what to emphasise or how to come across, do that, "
        "staying within the facts.",
    ]
    if honesty_level in ("honest", "tailored"):
        lines.append(
            "They count as part of the candidate's real background: you may "
            "state them even when the profile above doesn't mention them."
        )
    for title, items in (
        ("About the candidate (every application):", about_me),
        ("About this application:", about_application),
    ):
        if items:
            lines.append(title)
            lines.extend(f"- {f}" for f in items)
    return "\n".join(lines) + "\n"
