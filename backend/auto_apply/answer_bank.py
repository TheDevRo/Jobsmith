"""
auto_apply/answer_bank.py — Persistent library of reusable free-text answer snippets.

Snippets are stored in data/answer_bank.json as two dicts:
    {
      "snippets": { "question_key": "answer text", ... },   ← 8 seed keys
      "custom":   [ { "key": "...", "keywords": [...], "value": "..." }, ... ]
    }

Question keys are short human-readable slugs (e.g. "tell_us_about_yourself",
"why_this_role", "challenging_project").  The LLM uses these when mapping
open-ended form fields so it doesn't have to re-generate common answers.

Matching uses a weighted scoring algorithm:
    100 — exact phrase match
     80 — all keywords present
     60 — >70% of keywords present
     40 — any keyword present
  Minimum threshold of 60 required to return a match.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional
from ..paths import project_root

logger = logging.getLogger(__name__)

# Default location — can be overridden by passing a path to AnswerBank()
_DEFAULT_PATH = project_root() / "data" / "answer_bank.json"

# Seed snippets shipped with the project.  Users fill in the <…> placeholders.
_SEED: dict[str, str] = {
    "tell_us_about_yourself": (
        "<Replace with your professional summary — 2-3 sentences covering your "
        "background, core skills, and what you're looking for.>"
    ),
    "why_this_role": (
        "<Replace with why this specific role interests you — mention the company "
        "mission, the technology, or the team focus.>"
    ),
    "challenging_project": (
        "<Replace with a STAR-format story: Situation, Task, Action, Result — "
        "a concrete project where you overcame a real technical or organisational challenge.>"
    ),
    "greatest_strength": (
        "<Replace with one or two genuine strengths supported by a brief example.>"
    ),
    "greatest_weakness": (
        "<Replace with an honest weakness plus the concrete steps you are taking to improve.>"
    ),
    "career_goal": (
        "<Replace with your 3-5 year career goal, connected to the role you are applying for.>"
    ),
    "salary_expectation": (
        "<Replace with your salary expectation or a range, or leave blank to pull "
        "from profile.desired_salary.>"
    ),
    "cover_letter": (
        "<Replace with a reusable cover-letter body.  The orchestrator will prepend "
        "a personalised opening and close.>"
    ),
}

# Weighted keyword map for the 8 built-in keys.
# Each value is a dict with:
#   "exact"    — phrases that earn score 100 if ANY found verbatim
#   "keywords" — words used for partial scoring (80/60/40 depending on coverage)
_KEY_PATTERNS: dict[str, dict[str, list[str]]] = {
    "tell_us_about_yourself": {
        "exact":    ["tell us about yourself", "tell me about yourself", "introduce yourself",
                     "about yourself", "tell us about you", "describe yourself",
                     "who are you", "professional background", "brief introduction"],
        "keywords": ["about", "yourself", "introduce", "background", "yourself"],
    },
    "why_this_role": {
        "exact":    ["why this role", "why this position", "why do you want", "why are you interested",
                     "interest in this", "what attracts you", "motivation for applying",
                     "why apply", "why our company", "why do you want to work here",
                     "why are you a good fit"],
        "keywords": ["why", "interest", "motivation", "attract", "role", "position"],
    },
    "challenging_project": {
        "exact":    ["challenging project", "difficult project", "overcome a challenge",
                     "challenging situation", "describe a challenge", "tell us about a challenge",
                     "obstacle you faced", "difficult situation"],
        "keywords": ["challenge", "challenging", "difficult", "obstacle", "overcome"],
    },
    "greatest_strength": {
        "exact":    ["greatest strength", "top strength", "strongest skill", "best quality",
                     "key strength", "what is your strength", "describe your strengths",
                     "what are your strengths"],
        "keywords": ["strength", "strengths", "strongest", "best quality"],
    },
    "greatest_weakness": {
        "exact":    ["greatest weakness", "area for improvement", "biggest weakness",
                     "development area", "something to improve", "what is your weakness",
                     "describe your weakness", "what are your weaknesses"],
        "keywords": ["weakness", "weaknesses", "improve", "development area"],
    },
    "career_goal": {
        "exact":    ["career goal", "where do you see yourself", "5 years", "five years",
                     "career aspiration", "long-term goal", "career objective",
                     "professional goal", "future plans"],
        "keywords": ["career", "goal", "aspiration", "future", "years", "objective"],
    },
    "salary_expectation": {
        "exact":    ["salary expectation", "salary requirement", "desired salary",
                     "compensation expectation", "pay expectation", "expected salary",
                     "what salary", "salary range", "compensation range"],
        "keywords": ["salary", "compensation", "pay", "expected", "range"],
    },
    "cover_letter": {
        "exact":    ["cover letter", "why should we hire", "why should we choose you",
                     "why should we select you"],
        "keywords": ["cover", "letter", "hire", "choose"],
    },
}

# Minimum score required to return a match (0-100 scale)
_MIN_MATCH_SCORE = 60


class AnswerBank:
    """
    In-memory cache backed by a JSON file on disk.

    Thread-safe for the single-process FastAPI server (async, no threads).

    Storage format (v2):
        {
          "snippets": { key: value, ... },
          "custom":   [ { "key": str, "label": str, "keywords": [str], "value": str }, ... ]
        }

    The old flat-dict format is auto-migrated on load.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else _DEFAULT_PATH
        self._data: dict[str, str] = {}
        self._custom: list[dict] = []
        self._load()

    # ------------------------------------------------------------------
    # Public API — built-in keys
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[str]:
        """Return the snippet for *key*, or None if not set / still a placeholder."""
        value = self._data.get(key, "")
        if value.startswith("<") and value.endswith(">"):
            return None   # Placeholder — treat as unset
        return value or None

    def set(self, key: str, value: str) -> None:
        """Persist a snippet."""
        self._data[key] = value
        self._save()

    def delete(self, key: str) -> bool:
        if key in self._data:
            del self._data[key]
            self._save()
            return True
        return False

    def all_snippets(self) -> dict[str, str]:
        """Return all snippets (built-in + custom) as a flat key→value dict."""
        result = dict(self._data)
        for entry in self._custom:
            k = entry.get("key", "")
            v = entry.get("value", "")
            if k and v and not (v.startswith("<") and v.endswith(">")):
                result[k] = v
        return result

    # ------------------------------------------------------------------
    # Public API — custom keys
    # ------------------------------------------------------------------

    def get_custom_answers(self) -> list[dict]:
        """Return list of custom answer dicts: {key, label, keywords, value}."""
        return list(self._custom)

    def set_custom_answer(self, key: str, label: str, keywords: list[str], value: str) -> None:
        """Add or update a custom answer entry."""
        for entry in self._custom:
            if entry["key"] == key:
                entry["label"] = label
                entry["keywords"] = keywords
                entry["value"] = value
                self._save()
                return
        self._custom.append({"key": key, "label": label, "keywords": keywords, "value": value})
        self._save()

    def delete_custom_answer(self, key: str) -> bool:
        """Delete a custom answer entry. Returns True if found and removed."""
        before = len(self._custom)
        self._custom = [e for e in self._custom if e["key"] != key]
        if len(self._custom) < before:
            self._save()
            return True
        return False

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def find_best_match(self, question_text: str) -> Optional[str]:
        """
        Weighted keyword lookup.  Returns the snippet value of the best match
        if score >= _MIN_MATCH_SCORE, else None.

        Score rubric (0-100):
          100 — any exact phrase found verbatim in the question
           80 — all keywords present
           60 — >70% of keywords present
           40 — any keyword present (below threshold — returns None)

        Placeholders (values wrapped in <...>) are never returned.
        """
        q_lower = question_text.lower()
        best_score = 0
        best_key: Optional[str] = None
        best_value: Optional[str] = None

        # --- built-in keys ---
        for key, patterns in _KEY_PATTERNS.items():
            score = _score_question(q_lower, patterns["exact"], patterns["keywords"])
            if score > best_score:
                best_score = score
                best_key = key
                best_value = self.get(key)

        # --- custom keys ---
        for entry in self._custom:
            kws = [k.lower() for k in entry.get("keywords", [])]
            exact: list[str] = []
            if kws:
                score = _score_question(q_lower, exact, kws)
                if score > best_score:
                    best_score = score
                    best_key = entry["key"]
                    v = entry.get("value", "")
                    best_value = v if v and not (v.startswith("<") and v.endswith(">")) else None

        if best_score < _MIN_MATCH_SCORE:
            logger.debug(
                "find_best_match: best score %d < threshold %d for %r — no match",
                best_score, _MIN_MATCH_SCORE, question_text[:80],
            )
            return None

        if best_value is None:
            logger.debug(
                "find_best_match: key %r matched (score %d) but value is placeholder/empty",
                best_key, best_score,
            )
            return None

        logger.debug(
            "find_best_match: key %r matched with score %d", best_key, best_score
        )
        return best_value

    def score_question(self, question_text: str) -> dict:
        """
        Public helper: returns the best match info for a question.
        Used by the /api/answer-bank/test-match endpoint.

        Returns:
            {"matched_key": str|None, "score": int, "value": str|None}
        """
        q_lower = question_text.lower()
        best_score = 0
        best_key: Optional[str] = None
        best_value: Optional[str] = None

        for key, patterns in _KEY_PATTERNS.items():
            score = _score_question(q_lower, patterns["exact"], patterns["keywords"])
            if score > best_score:
                best_score = score
                best_key = key
                best_value = self.get(key)

        for entry in self._custom:
            kws = [k.lower() for k in entry.get("keywords", [])]
            score = _score_question(q_lower, [], kws)
            if score > best_score:
                best_score = score
                best_key = entry["key"]
                v = entry.get("value", "")
                best_value = v if v and not (v.startswith("<") and v.endswith(">")) else None

        if best_score < _MIN_MATCH_SCORE:
            return {"matched_key": None, "score": best_score, "value": None}

        return {"matched_key": best_key, "score": best_score, "value": best_value}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path, encoding="utf-8") as f:
                    raw = json.load(f)

                # Migrate old flat-dict format → new v2 format
                if isinstance(raw, dict) and "snippets" not in raw and "custom" not in raw:
                    logger.info("AnswerBank: migrating old flat-dict format to v2")
                    self._data = raw
                    self._custom = []
                    self._save()
                else:
                    self._data = raw.get("snippets", dict(_SEED))
                    self._custom = raw.get("custom", [])

                # Ensure all seed keys exist (non-destructively)
                for k, v in _SEED.items():
                    if k not in self._data:
                        self._data[k] = v

                logger.debug(
                    "AnswerBank: loaded %d snippets + %d custom from %s",
                    len(self._data), len(self._custom), self._path,
                )
            except Exception as exc:
                logger.warning("AnswerBank: could not load %s — %s", self._path, exc)
                self._data = dict(_SEED)
                self._custom = []
        else:
            self._data = dict(_SEED)
            self._custom = []
            self._save()
            logger.info("AnswerBank: created seed file at %s", self._path)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"snippets": self._data, "custom": self._custom}
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.error("AnswerBank: could not save %s — %s", self._path, exc)


# ---------------------------------------------------------------------------
# Scoring helper
# ---------------------------------------------------------------------------

def _score_question(q_lower: str, exact_phrases: list[str], keywords: list[str]) -> int:
    """
    Score how well *q_lower* matches a set of exact phrases and keywords.

    Returns an integer 0-100.
    """
    # Exact phrase: score 100
    for phrase in exact_phrases:
        if phrase in q_lower:
            return 100

    if not keywords:
        return 0

    matched = sum(1 for kw in keywords if kw in q_lower)
    total = len(keywords)
    ratio = matched / total

    if ratio >= 1.0:
        return 80
    elif ratio >= 0.6:
        return 60
    elif matched > 0:
        return 40
    return 0


# Module-level singleton — created lazily
_instance: Optional[AnswerBank] = None


def get_answer_bank(path: Optional[Path] = None) -> AnswerBank:
    """Return the module-level singleton, creating it if necessary."""
    global _instance
    if _instance is None:
        _instance = AnswerBank(path)
    return _instance
