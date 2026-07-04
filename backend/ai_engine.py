"""
ai_engine.py — LM Studio integration for job scoring, resume tailoring, and cover letters.

Uses the OpenAI-compatible API exposed by LM Studio.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from openai import AsyncOpenAI

from . import prompt_registry

logger = logging.getLogger(__name__)


# Tier fallback chain: utility falls back to fast if not explicitly configured,
# so existing two-tier configs keep working when new code asks for "utility".
_TIER_FALLBACKS = {
    "utility": ("utility", "fast"),
    "fast": ("fast",),
    "strong": ("strong",),
}


def _tier_chain(tier: str) -> tuple[str, ...]:
    return _TIER_FALLBACKS.get(tier, (tier,))


_client_cache: dict[tuple[str, str], AsyncOpenAI] = {}


def _get_client(config: dict, tier: str = "strong") -> AsyncOpenAI:
    """Return a cached AsyncOpenAI client pointed at LM Studio.

    Supports tiered model config (ai.models.utility / fast / strong).
    Each tier can override base_url and api_key, otherwise inherits from
    the top-level ai section. Falls back to legacy single-model config.

    Clients are cached per (base_url, api_key) for the lifetime of the
    process. Constructing a fresh AsyncOpenAI per call leaks the underlying
    httpx connection pool + SSL context until the process hits Errno 24
    ("Too many open files") — and on macOS the per-process FD ceiling is
    only 256, so a batch of a few hundred scoring calls is enough to crash
    the server.
    """
    ai_cfg = config.get("ai", {})
    default_url = ai_cfg.get("base_url", "http://localhost:1234/v1")
    # Blank key (e.g. cleared in Settings) falls back to the LM Studio
    # placeholder — the OpenAI SDK requires a non-empty string.
    default_key = ai_cfg.get("api_key") or "lm-studio"

    models_cfg = ai_cfg.get("models", {})
    base_url = default_url
    api_key = default_key
    for t in _tier_chain(tier):
        tier_cfg = models_cfg.get(t, {})
        if tier_cfg.get("base_url") or tier_cfg.get("api_key"):
            base_url = tier_cfg.get("base_url") or default_url
            api_key = tier_cfg.get("api_key") or default_key
            break

    key = (base_url, api_key)
    client = _client_cache.get(key)
    if client is None:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=90.0)
        _client_cache[key] = client
    return client


def _model(config: dict, tier: str = "strong") -> str:
    """Return the model name for the given tier.

    Walks the tier fallback chain (e.g. utility → fast), then falls back
    to the legacy top-level ai.model key.
    """
    ai_cfg = config.get("ai", {})
    models_cfg = ai_cfg.get("models", {})
    for t in _tier_chain(tier):
        tier_model = models_cfg.get(t, {}).get("model")
        if tier_model:
            return tier_model
    return ai_cfg.get("model", "local-model")


def _profile_summary(profile: dict, experiences: Optional[list] = None) -> str:
    """Build a structured profile description for prompts.

    Uses the same Title:/Company:/Dates: format as the resume output so the
    model can copy dates and titles verbatim instead of guessing.

    Pass `experiences` to override the role list (e.g. after relevance
    filtering). Defaults to the full profile.experience list.
    """
    parts = [
        f"Name: {profile.get('full_name', '')}",
        f"Summary: {profile.get('summary', '')}",
        f"Skills: {', '.join(profile.get('skills', []))}",
        "",
    ]

    # Number each role so the model treats them as distinct entries
    if experiences is None:
        experiences = profile.get("experience", [])
    if experiences:
        parts.append(f"EXPERIENCE ({len(experiences)} separate roles — each has its own dates, do NOT merge them):")
        for i, exp in enumerate(experiences, 1):
            if not exp.get("title"):
                continue
            parts.append(f"  Role {i}:")
            parts.append(f"    Title: {exp['title']}")
            parts.append(f"    Company: {exp.get('company', '')}")
            parts.append(f"    Dates: {exp.get('start_date', '')} - {exp.get('end_date', '')}")
            for bullet in exp.get("bullets", []):
                if bullet:
                    parts.append(f"    - {bullet}")
        parts.append("")

    for edu in profile.get("education", []):
        if edu.get("degree"):
            parts.append(f"Education: {edu['degree']} from {edu.get('school', '')} ({edu.get('year', '')})")
    certs = profile.get("certifications", [])
    if certs:
        parts.append(f"Certifications: {', '.join(certs)}")
    return "\n".join(parts)


def _honesty_instruction(honesty_level: str) -> str:
    """Return the tailoring directive inserted into resume/cover-letter prompts.

    Four stops on the honesty dial — each replaces the old hard-coded
    CRITICAL RULES block so the LLM knows exactly how much latitude it has.
    """
    instructions = {
        "honest": (
            "TAILORING DIRECTIVE:\n"
            "Tailor the resume to highlight genuinely relevant experience. "
            "Do not add, invent, or exaggerate anything. Reorder and reword only.\n"
            "- You may ONLY use experience, education, and certifications from the CANDIDATE PROFILE below.\n"
            "- NEVER invent, fabricate, or add jobs, companies, degrees, or certifications that are not in the candidate's profile.\n"
            "- NEVER add the target job/company to the experience section — the candidate is APPLYING there.\n"
            "- NEVER change, merge, or consolidate dates. Copy them EXACTLY as given."
        ),
        "tailored": (
            "TAILORING DIRECTIVE:\n"
            "Tailor the resume to best match this job. You may rephrase experience to use the job's exact keywords "
            "and terminology. Do not invent experience that doesn't exist, but present existing experience in its "
            "most favorable light.\n"
            "- Do not add jobs, companies, or degrees that are not in the candidate's profile.\n"
            "- NEVER add the target job/company to the experience section — the candidate is APPLYING there.\n"
            "- Copy dates VERBATIM — do not merge or round them."
        ),
        "embellished": (
            "TAILORING DIRECTIVE:\n"
            "Tailor the resume aggressively. You may expand on vague experience, upgrade job titles slightly "
            "(e.g., 'helped with' → 'led'), add reasonable skills that the candidate plausibly has but didn't "
            "list, and frame all experience to sound maximally relevant. Keep it believable and consistent.\n"
            "- Do not contradict the core timeline (companies and rough date ranges).\n"
            "- NEVER add the target job/company to the experience section — the candidate is APPLYING there.\n"
            "- Copy dates VERBATIM — do not merge or round them."
        ),
        "fabricated": (
            "TAILORING DIRECTIVE:\n"
            "Create the most competitive version of this resume for the role. You may invent specific achievements "
            "with plausible metrics, add missing skills or tools, upgrade responsibilities, and fill experience gaps. "
            "Everything must remain internally consistent and believable — nothing should contradict the core timeline "
            "or be obviously unverifiable. This is for personal use only.\n"
            "- NEVER add the target job/company to the experience section — the candidate is APPLYING there.\n"
            "- Preserve the existing timeline structure (companies and rough date ranges)."
        ),
    }
    return instructions.get(honesty_level, instructions["honest"])


def _revise_fabrication_guard(honesty_level: str) -> str:
    """Honesty-aware fabrication guard for the revise prompts.

    Pairs with _honesty_instruction() so revisions match the user's chosen
    tolerance for invention. Strict for honest/tailored, permissive for
    embellished/fabricated.
    """
    guards = {
        "honest": (
            "Critical: Only use facts present in the candidate profile. If the user's instructions ask you to add "
            "experience, skills, certifications, or accomplishments that are NOT in the profile, IGNORE that part "
            "of the instruction — do not invent or fabricate. Apply only the instructions you can satisfy from the "
            "candidate's real background."
        ),
        "tailored": (
            "Use facts from the candidate profile. You may rephrase and reframe to better match the job, but do not "
            "invent jobs, companies, degrees, or certifications. If the user asks for something that would require "
            "fabricating those, ignore that part."
        ),
        "embellished": (
            "You may apply the user's instruction with reasonable enhancement: expand on vague experience, upgrade "
            "phrasing, and add plausible adjacent skills the candidate likely has. Do not invent entire jobs, "
            "companies, or degrees that aren't in the profile, and do not contradict the timeline. Within those "
            "limits, satisfy the instruction fully."
        ),
        "fabricated": (
            "Apply the user's instruction aggressively. You may invent specific achievements with plausible metrics, "
            "add skills/tools, and upgrade responsibilities to satisfy the request. Keep everything internally "
            "consistent and believable; preserve the existing timeline (companies and rough date ranges)."
        ),
    }
    return guards.get(honesty_level, guards["honest"])


def _experience_sort_key(exp: dict) -> str:
    """Sort key for descending chronological order — Present roles float to top."""
    end = (exp.get("end_date") or "").strip()
    if end.lower() in ("present", "current", ""):
        return "9999-99-99"
    return end


async def _select_resume_experiences(
    experiences: list, job: dict, max_entries, config: dict
) -> list:
    """Pick which experience entries belong on the tailored resume.

    Pinned roles are always included (even if they exceed max_entries).
    The remaining slots are filled by asking the local LLM to score each
    unpinned role 0-100 against the job description; top scorers win.
    Falls back to original order on any LLM/parse failure.

    The returned list is sorted by end_date descending so the resume
    timeline reads chronologically.
    """
    if not experiences:
        return experiences
    try:
        cap = int(max_entries) if max_entries is not None else 0
    except (TypeError, ValueError):
        cap = 0
    if cap <= 0 or len(experiences) <= cap:
        return experiences

    pinned = [e for e in experiences if e.get("pinned")]
    unpinned = [e for e in experiences if not e.get("pinned")]

    if len(pinned) >= cap:
        if len(pinned) > cap:
            logger.warning(
                "Pinned roles (%d) exceed max_resume_experience_entries (%d); "
                "including all pinned roles anyway.", len(pinned), cap,
            )
        return sorted(pinned, key=_experience_sort_key, reverse=True)

    slots_remaining = cap - len(pinned)
    if not unpinned or slots_remaining <= 0:
        return sorted(pinned, key=_experience_sort_key, reverse=True)

    # Build a numbered description of unpinned roles for the LLM to score
    role_lines = []
    for i, exp in enumerate(unpinned):
        bullets = "; ".join(b for b in exp.get("bullets", []) if b)
        role_lines.append(
            f"Role {i}: {exp.get('title', '')} at {exp.get('company', '')} "
            f"({exp.get('start_date', '')} - {exp.get('end_date', '')}). "
            f"Highlights: {bullets[:500]}"
        )

    prompt = prompt_registry.render_prompt(
        config, "select_resume_experiences",
        job_title=job.get("title", ""),
        job_company=job.get("company", ""),
        job_description=(job.get("description") or "")[:3000],
        role_lines="\n".join(role_lines),
    )

    selected_unpinned: list = []
    try:
        client = _get_client(config, "utility")
        response = await client.chat.completions.create(
            model=_model(config, "utility"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=400,
        )
        text = response.choices[0].message.content.strip()
        try:
            data = json.loads(text)
            scored = data["scores"]
        except (json.JSONDecodeError, KeyError):
            # Salvage scores via regex
            scored = [
                {"index": int(m.group(1)), "score": float(m.group(2))}
                for m in re.finditer(
                    r'"index"\s*:\s*(\d+)\s*,\s*"score"\s*:\s*(\d+)', text
                )
            ]
            if not scored:
                raise ValueError(f"Could not parse role scores: {text[:200]}")
            logger.warning("_select_resume_experiences: used regex fallback")

        scored.sort(key=lambda r: float(r.get("score", 0)), reverse=True)
        seen: set = set()
        for row in scored:
            idx = int(row.get("index", -1))
            if 0 <= idx < len(unpinned) and idx not in seen:
                seen.add(idx)
                selected_unpinned.append(unpinned[idx])
                if len(selected_unpinned) >= slots_remaining:
                    break
    except Exception:
        logger.exception(
            "Relevance scoring failed; falling back to original order for "
            "resume experience selection."
        )
        selected_unpinned = unpinned[:slots_remaining]

    # If the LLM returned fewer scores than slots, top up from original order
    if len(selected_unpinned) < slots_remaining:
        for exp in unpinned:
            if exp not in selected_unpinned:
                selected_unpinned.append(exp)
                if len(selected_unpinned) >= slots_remaining:
                    break

    chosen = pinned + selected_unpinned
    return sorted(chosen, key=_experience_sort_key, reverse=True)


_TITLE_ALIGNMENTS = {"strong", "partial", "weak"}


def _sanitize_match_report(data: dict) -> Optional[dict]:
    """Coerce the LLM's structured match output into a clean report dict.

    Returns None if nothing usable survives — callers treat that as
    "score/reasoning only", exactly like the old two-field response.
    """
    def str_list(key: str, cap: int) -> list[str]:
        raw = data.get(key)
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                out.append(item.strip()[:80])
            if len(out) >= cap:
                break
        return out

    report = {
        "matched_skills": str_list("matched_skills", 12),
        "missing_skills": str_list("missing_skills", 12),
        "matched_soft_skills": str_list("matched_soft_skills", 8),
        "missing_soft_skills": str_list("missing_soft_skills", 8),
        "keywords": str_list("keywords", 15),
    }
    alignment = data.get("title_alignment")
    report["title_alignment"] = alignment if alignment in _TITLE_ALIGNMENTS else None

    if not any(report[k] for k in ("matched_skills", "missing_skills", "keywords")):
        return None
    return report


async def score_job_fit(
    job: dict, profile: dict, config: dict
) -> tuple[float, str, Optional[dict]]:
    """
    Score how well a job matches the candidate's profile (0-100).
    Returns (score, reasoning, match_report). match_report is a structured
    skill/keyword gap breakdown, or None when the model output couldn't be
    parsed beyond a bare score.
    """
    ai_cfg = config.get("ai", {})
    tier = ai_cfg.get("scoring_tier", "strong")
    client = _get_client(config, tier)

    prompt = prompt_registry.render_prompt(
        config, "score_job_fit",
        job_title=job.get("title", ""),
        job_company=job.get("company", ""),
        job_description=job.get("description", "")[:3000],
        profile_summary=_profile_summary(profile),
    )

    try:
        response = await client.chat.completions.create(
            model=_model(config, tier),
            messages=[{"role": "user", "content": prompt}],
            temperature=ai_cfg.get("temperature", 0.7),
            max_tokens=1200,
        )
        text = response.choices[0].message.content.strip()
        logger.debug("Score response: %s", text)

        # Try parsing as JSON first
        try:
            data = json.loads(text)
            return (
                float(data["score"]),
                data.get("reasoning", ""),
                _sanitize_match_report(data),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            # Salvage attempt — models often wrap JSON in prose or code fences
            obj_match = re.search(r"\{.*\}", text, re.DOTALL)
            if obj_match:
                try:
                    data = json.loads(obj_match.group(0))
                    return (
                        float(data["score"]),
                        data.get("reasoning", ""),
                        _sanitize_match_report(data),
                    )
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    pass
            # Regex fallback 1 — look for a number after "score"
            score_match = re.search(r'"score"\s*:\s*(\d+)', text)
            reasoning_match = re.search(r'"reasoning"\s*:\s*"([^"]+)"', text)
            if score_match:
                logger.warning(
                    "score_job_fit: JSON parse failed, used regex fallback. "
                    "Response snippet: %s", text[:200]
                )
                score = float(score_match.group(1))
                reasoning = reasoning_match.group(1) if reasoning_match else text
                return score, reasoning, None
            # Regex fallback 2 — scan for any integer 0-100 in the text
            any_number = re.search(r'\b([0-9]{1,2}|100)\b', text)
            if any_number:
                score = float(any_number.group(1))
                logger.warning(
                    "score_job_fit: JSON + regex both failed, used number-scan "
                    "fallback (score=%s). Response snippet: %s", score, text[:200]
                )
                return score, f"(Score parsed from raw response) {text[:300]}", None
            logger.error(
                "score_job_fit: Could not parse score from AI response: %s", text[:200]
            )
            return 0.0, f"ERROR: Could not parse score from LLM response. Raw: {text[:200]}", None

    except Exception as e:
        logger.exception("AI scoring failed for job %s", job.get("title", ""))
        # Retry once
        try:
            response = await client.chat.completions.create(
                model=_model(config),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1200,
            )
            text = response.choices[0].message.content.strip()
            data = json.loads(text)
            return (
                float(data["score"]),
                data.get("reasoning", ""),
                _sanitize_match_report(data),
            )
        except Exception:
            logger.exception("AI scoring retry also failed")
            return 0.0, f"AI error: {str(e)}", None


async def suggest_job_titles(profile: dict, answers: dict, config: dict) -> list[dict]:
    """Recommend job titles to search for, based on the candidate's profile
    and their answers to a few direction questions.

    Returns a list of {"title": str, "reason": str} dicts (may be empty if
    the model output was unusable).
    """
    ai_cfg = config.get("ai", {})
    client = _get_client(config, "strong")

    answer_lines = "\n".join(
        f"- {k.replace('_', ' ').capitalize()}: {str(v).strip()}"
        for k, v in (answers or {}).items()
        if v and str(v).strip()
    ) or "- (no preferences given)"

    prompt = prompt_registry.render_prompt(
        config, "suggest_job_titles",
        answer_lines=answer_lines,
        profile_summary=_profile_summary(profile),
    )

    response = await client.chat.completions.create(
        model=_model(config, "strong"),
        messages=[{"role": "user", "content": prompt}],
        temperature=ai_cfg.get("temperature", 0.7),
        max_tokens=1500,
    )
    text = (response.choices[0].message.content or "").strip()

    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    if not isinstance(data, dict):
        logger.warning("suggest_job_titles: unparseable response: %s", text[:300])
        return []

    titles: list[dict] = []
    seen: set[str] = set()
    for item in data.get("titles", []):
        if isinstance(item, str):
            item = {"title": item, "reason": ""}
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        titles.append({"title": title, "reason": str(item.get("reason", "")).strip()})
    return titles


async def suggest_companies(
    profile: dict,
    search_cfg: dict,
    liked_companies: list[str],
    exclude: list[str],
    config: dict,
) -> list[dict]:
    """Recommend companies whose job boards the candidate should watch.

    Returns a list of {"name": str, "why": str} dicts (may be empty if the
    model output was unusable). Suggestions are candidates only — the caller
    must validate each against the live ATS board probes before showing it.
    """
    ai_cfg = config.get("ai", {})
    client = _get_client(config, "strong")

    keywords = ", ".join(search_cfg.get("keywords", [])) or "(none set)"
    liked = ", ".join(liked_companies[:15]) or "(no history yet)"
    excluded = ", ".join(exclude[:60]) or "(none)"

    prompt = prompt_registry.render_prompt(
        config, "suggest_companies",
        profile_summary=_profile_summary(profile),
        keywords=keywords,
        liked=liked,
        excluded=excluded,
    )

    response = await client.chat.completions.create(
        model=_model(config, "strong"),
        messages=[{"role": "user", "content": prompt}],
        temperature=ai_cfg.get("temperature", 0.7),
        max_tokens=1500,
    )
    text = (response.choices[0].message.content or "").strip()

    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    if not isinstance(data, dict):
        logger.warning("suggest_companies: unparseable response: %s", text[:300])
        return []

    companies: list[dict] = []
    seen: set[str] = set()
    for item in data.get("companies", []):
        if isinstance(item, str):
            item = {"name": item, "why": ""}
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        companies.append({"name": name, "why": str(item.get("why", "")).strip()})
    return companies


def _keyword_targets_block(match_report: Optional[dict]) -> str:
    """Build the ATS keyword-targeting section injected into the resume prompt.

    Derived from the structured match report produced at scoring time. The
    honesty directive still governs latitude: matched skills are safe to
    emphasize verbatim; missing skills may only be referenced to the extent
    the directive allows (at 'honest'/'tailored' that means only where the
    candidate's real experience genuinely supports adjacent wording).
    """
    if not match_report:
        return ""
    lines = ["KEYWORD TARGETS (from ATS gap analysis of this posting):"]
    matched = match_report.get("matched_skills") or []
    keywords = match_report.get("keywords") or []
    missing = match_report.get("missing_skills") or []
    if matched:
        lines.append(
            f"- Candidate HAS these skills the job requires — feature them prominently, "
            f"using this exact wording: {', '.join(matched)}"
        )
    if keywords:
        lines.append(
            f"- ATS scan keywords — weave these exact phrases into the summary and bullets "
            f"wherever the candidate's real experience supports them: {', '.join(keywords)}"
        )
    if missing:
        lines.append(
            f"- Candidate LACKS these required skills: {', '.join(missing)}. "
            f"Follow the TAILORING DIRECTIVE above for how much latitude you have; "
            f"do not claim them beyond what it permits."
        )
    return "\n".join(lines) + "\n"


async def generate_tailored_resume(
    job: dict,
    profile: dict,
    config: dict,
    honesty_level: str = "honest",
    match_report: Optional[dict] = None,
) -> str:
    """Generate a tailored resume text for a specific job.

    honesty_level controls how much latitude the AI has when tailoring:
      honest | tailored | embellished | fabricated

    match_report (optional) is the structured skill/keyword gap breakdown from
    score_job_fit — when present, its keywords are targeted explicitly.
    """
    client = _get_client(config)
    ai_cfg = config.get("ai", {})

    max_entries = config.get("application_honesty", {}).get("max_resume_experience_entries")
    selected_experiences = await _select_resume_experiences(
        profile.get("experience", []), job, max_entries, config
    )

    prompt = prompt_registry.render_prompt(
        config, "tailor_resume",
        honesty_instruction=_honesty_instruction(honesty_level),
        keyword_targets=_keyword_targets_block(match_report),
        job_title=job.get("title", ""),
        job_company=job.get("company", ""),
        job_description=job.get("description", "")[:5000],
        profile_summary=_profile_summary(profile, selected_experiences),
    )

    try:
        response = await client.chat.completions.create(
            model=_model(config),
            messages=[{"role": "user", "content": prompt}],
            temperature=ai_cfg.get("temperature", 0.7),
            max_tokens=ai_cfg.get("max_tokens", 2000),
        )
        content = response.choices[0].message.content.strip()
        logger.info("Generated tailored resume [%s] for: %s at %s", honesty_level, job.get("title"), job.get("company"))
        return content
    except Exception as e:
        logger.exception("Resume generation failed")
        try:
            response = await client.chat.completions.create(
                model=_model(config),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5,
                max_tokens=ai_cfg.get("max_tokens", 2000),
            )
            return response.choices[0].message.content.strip()
        except Exception:
            logger.exception("Resume generation retry failed")
            raise


def _tone_instruction(tone: str) -> str:
    """Return a tone directive for cover letter generation."""
    tones = {
        "professional": (
            "TONE: Write in a formal, corporate tone. Use complete sentences, no contractions "
            "(use 'I am' not 'I'm'). Maintain professional distance while still being engaging."
        ),
        "conversational": (
            "TONE: Write naturally, as if speaking directly to a peer or colleague. "
            "Contractions are fine ('I'm', 'I've', 'you'll'). Warm and approachable without being casual."
        ),
        "enthusiastic": (
            "TONE: Show genuine excitement and energy about this opportunity. Use active, "
            "energetic language. Lead with passion for the role and company. Keep it professional "
            "but let enthusiasm come through clearly."
        ),
    }
    return tones.get(tone, tones["professional"])


async def generate_cover_letter(
    job: dict, profile: dict, config: dict, honesty_level: str = "honest"
) -> str:
    """Generate a tailored cover letter for a specific job.

    honesty_level controls how much latitude the AI has when tailoring:
      honest | tailored | embellished | fabricated

    cover_letter_tone (from config.application_honesty.cover_letter_tone):
      professional | conversational | enthusiastic  (default: professional)
    """
    client = _get_client(config)
    ai_cfg = config.get("ai", {})
    tone = config.get("application_honesty", {}).get("cover_letter_tone", "professional")

    max_entries = config.get("application_honesty", {}).get("max_resume_experience_entries")
    selected_experiences = await _select_resume_experiences(
        profile.get("experience", []), job, max_entries, config
    )

    prompt = prompt_registry.render_prompt(
        config, "cover_letter",
        honesty_instruction=_honesty_instruction(honesty_level),
        tone_instruction=_tone_instruction(tone),
        job_title=job.get("title", ""),
        job_company=job.get("company", "") or "the company",
        job_description=job.get("description", "")[:5000],
        profile_summary=_profile_summary(profile, selected_experiences),
    )

    try:
        response = await client.chat.completions.create(
            model=_model(config),
            messages=[{"role": "user", "content": prompt}],
            temperature=ai_cfg.get("temperature", 0.7),
            max_tokens=ai_cfg.get("max_tokens", 2000),
        )
        content = response.choices[0].message.content.strip()
        logger.info("Generated cover letter [%s] for: %s at %s", honesty_level, job.get("title"), job.get("company"))
        return content
    except Exception as e:
        logger.exception("Cover letter generation failed")
        try:
            response = await client.chat.completions.create(
                model=_model(config),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5,
                max_tokens=ai_cfg.get("max_tokens", 2000),
            )
            return response.choices[0].message.content.strip()
        except Exception:
            logger.exception("Cover letter retry failed")
            raise


async def revise_tailored_resume(
    current_resume_text: str,
    user_instructions: str,
    job: dict,
    profile: dict,
    config: dict,
    tier: str = "strong",
    honesty_level: str = "honest",
) -> str:
    """Revise an already-tailored resume according to user instructions.

    honesty_level controls how much latitude the AI has when applying edits
    (honest | tailored | embellished | fabricated). Output format must remain
    parser-compatible (same headers/prefixes as generate_tailored_resume).
    """
    client = _get_client(config, tier)
    ai_cfg = config.get("ai", {})

    max_entries = config.get("application_honesty", {}).get("max_resume_experience_entries")
    selected_experiences = await _select_resume_experiences(
        profile.get("experience", []), job, max_entries, config
    )

    prompt = prompt_registry.render_prompt(
        config, "revise_resume",
        honesty_instruction=_honesty_instruction(honesty_level),
        fabrication_guard=_revise_fabrication_guard(honesty_level),
        profile_summary=_profile_summary(profile, selected_experiences),
        job_title=job.get("title", ""),
        job_company=job.get("company", ""),
        job_description=job.get("description", "")[:5000],
        user_instructions=user_instructions,
        current_resume=current_resume_text,
    )

    try:
        response = await client.chat.completions.create(
            model=_model(config, tier),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=max(ai_cfg.get("max_tokens", 2000), 3000),
        )
        content = response.choices[0].message.content.strip()
        logger.info("Revised resume (tier=%s, model=%s, honesty=%s) for: %s at %s", tier, _model(config, tier), honesty_level, job.get("title"), job.get("company"))
        return content
    except Exception:
        logger.exception("Resume revision failed")
        raise


async def revise_cover_letter(
    current_letter_text: str,
    user_instructions: str,
    job: dict,
    profile: dict,
    config: dict,
    tier: str = "strong",
    honesty_level: str = "honest",
) -> str:
    """Revise an already-generated cover letter according to user instructions.

    honesty_level controls how much latitude the AI has when applying edits.
    """
    client = _get_client(config, tier)
    ai_cfg = config.get("ai", {})
    tone = config.get("application_honesty", {}).get("cover_letter_tone", "professional")

    max_entries = config.get("application_honesty", {}).get("max_resume_experience_entries")
    selected_experiences = await _select_resume_experiences(
        profile.get("experience", []), job, max_entries, config
    )

    prompt = prompt_registry.render_prompt(
        config, "revise_cover_letter",
        honesty_instruction=_honesty_instruction(honesty_level),
        tone_instruction=_tone_instruction(tone),
        fabrication_guard=_revise_fabrication_guard(honesty_level),
        profile_summary=_profile_summary(profile, selected_experiences),
        job_title=job.get("title", ""),
        job_company=job.get("company", ""),
        job_description=job.get("description", "")[:5000],
        user_instructions=user_instructions,
        current_letter=current_letter_text,
    )

    try:
        response = await client.chat.completions.create(
            model=_model(config, tier),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=max(ai_cfg.get("max_tokens", 2000), 2500),
        )
        content = response.choices[0].message.content.strip()
        logger.info("Revised cover letter (tier=%s, model=%s, honesty=%s) for: %s at %s", tier, _model(config, tier), honesty_level, job.get("title"), job.get("company"))
        return content
    except Exception:
        logger.exception("Cover letter revision failed")
        raise


async def generate_embellishment_log(
    profile: dict,
    resume_text: str,
    cover_letter_text: str,
    honesty_level: str,
    config: dict,
) -> dict:
    """Make a second LLM call to diff the generated documents against the original profile.

    Returns a dict in the shape of EmbellishmentLog (ready for db.set_embellishment_log).
    On any failure the log is still returned with empty change lists so the caller
    always gets a valid record.
    """
    client = _get_client(config)
    ai_cfg = config.get("ai", {})

    prompt = prompt_registry.render_prompt(
        config, "embellishment_log",
        profile_summary=_profile_summary(profile),
        resume_text=resume_text[:3000],
        cover_letter_text=cover_letter_text[:2000],
    )

    resume_changes: list[dict] = []
    cover_letter_changes: list[dict] = []

    try:
        response = await client.chat.completions.create(
            model=_model(config),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=ai_cfg.get("max_tokens", 2000),
        )
        text = response.choices[0].message.content.strip()

        # Parse the JSON response
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r'\{[\s\S]*\}', text)
            data = json.loads(match.group()) if match else {}

        resume_changes = data.get("resume_changes", [])
        cover_letter_changes = data.get("cover_letter_changes", [])

        # Ensure each entry has the expected keys; drop malformed ones
        def _clean(entries: list) -> list[dict]:
            out = []
            for e in entries:
                if isinstance(e, dict) and all(k in e for k in ("field", "original", "modified")):
                    out.append({
                        "field":    str(e["field"]),
                        "original": str(e["original"]),
                        "modified": str(e["modified"]),
                    })
            return out

        resume_changes = _clean(resume_changes)
        cover_letter_changes = _clean(cover_letter_changes)
        logger.info(
            "Embellishment log [%s]: %d resume change(s), %d cover letter change(s)",
            honesty_level, len(resume_changes), len(cover_letter_changes),
        )
    except Exception:
        logger.exception("Embellishment log generation failed — storing empty log")

    log: dict = {
        "honesty_level":        honesty_level,
        "resume_changes":       resume_changes,
        "cover_letter_changes": cover_letter_changes,
        "generated_at":         datetime.now(timezone.utc).isoformat(),
    }

    if honesty_level == "fabricated":
        log["WARNING"] = (
            "This application contains fabricated content. Review before interviews."
        )
        logger.warning(
            "FABRICATED application generated for job — embellishment log saved. "
            "Review before any interview. resume_changes=%d cover_letter_changes=%d",
            len(resume_changes), len(cover_letter_changes),
        )

    return log


async def generate_custom_answers(
    job: dict, profile: dict, questions: list[str], config: dict
) -> dict:
    """Generate answers to custom application questions (Greenhouse/Lever forms)."""
    client = _get_client(config)
    ai_cfg = config.get("ai", {})

    questions_text = "\n".join(f"- {q}" for q in questions)

    prompt = prompt_registry.render_prompt(
        config, "custom_answers",
        job_title=job.get("title", ""),
        job_company=job.get("company", ""),
        profile_summary=_profile_summary(profile),
        questions=questions_text,
    )

    try:
        response = await client.chat.completions.create(
            model=_model(config),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=ai_cfg.get("max_tokens", 2000),
        )
        text = response.choices[0].message.content.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON from response
            json_match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            logger.warning("Could not parse custom answers JSON")
            return {q: "" for q in questions}
    except Exception:
        logger.exception("Custom answer generation failed")
        return {q: "" for q in questions}


async def batch_process_jobs(
    jobs: list[dict], profile: dict, config: dict, score_threshold: float = 60.0
) -> list[dict]:
    """
    Score all jobs. For those above the threshold, also generate resume + cover letter.
    Returns a list of result dicts with keys: job_id, score, reasoning, resume, cover_letter.
    """
    results = []

    for i, job in enumerate(jobs):
        logger.info("Processing job %d/%d: %s", i + 1, len(jobs), job.get("title", ""))

        score, reasoning, match_report = await score_job_fit(job, profile, config)
        result = {
            "job": job,
            "score": score,
            "reasoning": reasoning,
            "match_report": match_report,
            "resume": None,
            "cover_letter": None,
        }

        if score >= score_threshold:
            try:
                result["resume"] = await generate_tailored_resume(
                    job, profile, config, match_report=match_report
                )
                result["cover_letter"] = await generate_cover_letter(job, profile, config)
            except Exception:
                logger.exception("Failed to generate materials for job %s", job.get("title"))

        results.append(result)

    return results


async def test_connection(config: dict) -> dict:
    """Test connectivity to LM Studio. Returns status dict."""
    client = _get_client(config)
    try:
        models = await client.models.list()
        model_ids = [m.id for m in models.data]
        return {"connected": True, "models": model_ids}
    except Exception as e:
        return {"connected": False, "error": str(e)}
