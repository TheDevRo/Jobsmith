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
    default_key = ai_cfg.get("api_key", "lm-studio")

    models_cfg = ai_cfg.get("models", {})
    base_url = default_url
    api_key = default_key
    for t in _tier_chain(tier):
        tier_cfg = models_cfg.get(t, {})
        if tier_cfg.get("base_url") or tier_cfg.get("api_key"):
            base_url = tier_cfg.get("base_url", default_url)
            api_key = tier_cfg.get("api_key", default_key)
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

    prompt = (
        "You are ranking past job roles by relevance to a target job posting.\n"
        "Return ONLY a JSON object: {\"scores\": [{\"index\": <int>, \"score\": <0-100>}, ...]}\n"
        "Score each role 0-100 by how well it prepares the candidate for the target job.\n\n"
        f"TARGET JOB:\nTitle: {job.get('title', '')}\n"
        f"Company: {job.get('company', '')}\n"
        f"Description: {(job.get('description') or '')[:3000]}\n\n"
        "CANDIDATE ROLES TO SCORE:\n" + "\n".join(role_lines) + "\n\n"
        "Return only the JSON object."
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

    prompt = f"""You are a career advisor AI. Evaluate how well this candidate's existing experience fits the job below.
Return ONLY a JSON object with exactly these keys:
- "score": integer 0-100
- "reasoning": string, 2-3 sentences
- "matched_skills": array of hard skills/tools/certifications the job asks for that the candidate HAS (max 12)
- "missing_skills": array of required or strongly-preferred hard skills the candidate LACKS (max 12)
- "matched_soft_skills": array of soft skills the job asks for that the candidate demonstrates (max 8)
- "missing_soft_skills": array of soft skills the job asks for with no evidence in the profile (max 8)
- "title_alignment": one of "strong", "partial", "weak" — how close the candidate's recent titles are to this job's title
- "keywords": array of the most important exact keywords/phrases from the posting that an ATS would scan a resume for (max 15)

Scoring guidelines:
- 80-100: Strong match — most required skills present, directly relevant experience
- 60-79: Good match — several skills overlap, related experience
- 40-59: Partial match — some transferable skills, adjacent experience
- 20-39: Weak match — few relevant skills, mostly unrelated experience
- 0-19: Poor match — no meaningful overlap

Be realistic. Score based on what the candidate has actually done, not aspirational fit.
Use the exact wording from the job posting for skills and keywords (ATS systems match exact terms, not synonyms).
A skill belongs in "matched_skills" ONLY if it appears in (or is clearly evidenced by) the candidate profile.

JOB:
Title: {job.get('title', '')}
Company: {job.get('company', '')}
Description: {job.get('description', '')[:3000]}

CANDIDATE PROFILE:
{_profile_summary(profile)}

Return only the JSON object, no other text."""

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

    prompt = f"""You are a career advisor AI. Recommend job titles this candidate should search for on job boards.

Return ONLY a JSON object: {{"titles": [{{"title": "...", "reason": "..."}}]}}

Rules:
- 8 to 12 titles, ordered most-relevant first.
- Titles must be real, commonly-posted job titles — exactly what employers put in postings — so they work as job-board search keywords. No slashes or parenthetical variants; list variants as separate titles.
- Base them on the candidate's actual experience and skills AND on the candidate's stated preferences below. Preferences win when they conflict with the résumé (e.g. a pivot).
- Do not suggest seniority the candidate hasn't plausibly earned unless their preferences ask for a stretch.
- Each "reason" is one short sentence tying the title to the candidate.

CANDIDATE PREFERENCES:
{answer_lines}

CANDIDATE PROFILE:
{_profile_summary(profile)}

Return only the JSON object, no other text."""

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

    prompt = f"""You are an expert resume writer. Tailor the candidate's resume for the job posting below.

{_honesty_instruction(honesty_level)}

{_keyword_targets_block(match_report)}
Your task: Rephrase and reorder the candidate's experience and skills to best match the job posting.

OUTPUT FORMAT RULES (the document parser requires these exactly):
- Output EXACTLY these section headers in ALL CAPS on their own line, with nothing else on that line:
  SUMMARY
  SKILLS
  EXPERIENCE
  EDUCATION
  CERTIFICATIONS
- Do NOT use any markdown (no **, no ##, no ```, no * bullets). Use plain dashes (-) for bullet points.
- Do NOT include the candidate's name or contact info — that is added separately.
- Target 500-700 words total. Prioritize relevance over length.
- For each experience entry, output EXACTLY in this format on separate lines:
  Title: [exact title from profile]
  Company: [exact company from profile]
  Dates: [exact dates from profile]
  - [bullet point]

EXAMPLE FORMAT:
SUMMARY
Two to three sentences summarizing the candidate for this specific role.

SKILLS
Python, AWS, Docker, Kubernetes, CI/CD

EXPERIENCE
Title: Senior Software Engineer
Company: Acme Corp
Dates: Jan 2022 - Present
- Led migration of monolithic app to microservices, reducing deploy time by 40%
- Built REST APIs serving 10M requests/day using FastAPI and PostgreSQL
- Mentored 4 engineers and established code review standards adopted team-wide

EDUCATION
Degree: B.S. Computer Science
School: State University
Year: 2019

CERTIFICATIONS
- AWS Solutions Architect Associate

Instructions:
1. Reorder and prioritize the candidate's existing skills to match the job description
2. For each of the candidate's real experience entries, rewrite bullets to emphasize relevance to THIS role. Output EXACTLY 3 bullets per entry — no more, no less.
3. Naturally incorporate keywords from the job description into descriptions of the candidate's real experience
4. Use strong action verbs and quantify achievements where possible
5. You may omit less relevant roles, but NEVER add roles that aren't in the candidate's profile
6. Follow the format EXACTLY — the parser depends on "Title:", "Company:", "Dates:" prefixes on their own lines
7. Copy job titles, company names, and dates VERBATIM from the candidate profile — do NOT alter, merge, or round them
8. If the candidate held multiple roles at the same company, keep them as SEPARATE entries with their own dates

JOB POSTING (this is what the candidate is APPLYING TO — do NOT list this as experience):
Title: {job.get('title', '')}
Company: {job.get('company', '')}
Description: {job.get('description', '')[:5000]}

CANDIDATE PROFILE (this is the candidate's ACTUAL background — only use information from here):
{_profile_summary(profile, selected_experiences)}

Write the tailored resume now. Start directly with SUMMARY."""

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

    prompt = f"""You are an expert cover letter writer. Write a tailored cover letter for the candidate applying to the role below.

{_honesty_instruction(honesty_level)}

{_tone_instruction(tone)}

Additional rules:
- Do NOT use placeholder text like [Company Name] or [Your Name] — use actual values from the profile and job.
- Do NOT start with "I am writing to apply for..." — that opener is overused and weak.

Requirements:
1. Address the letter to the hiring team at {job.get('company', 'the company')}
2. Opening paragraph: Express genuine interest in the specific role and company. Reference something specific about the job posting.
3. Body paragraphs (1-2): Connect the candidate's REAL experience and skills to the job requirements. Reference actual requirements from the posting and explain how the candidate's existing background meets them. Be specific with examples, not generic.
4. Closing paragraph: Reiterate enthusiasm and include a clear call to action.
5. Keep it to 3-4 paragraphs total (roughly 250-350 words).

JOB POSTING (this is what the candidate is applying to):
Title: {job.get('title', '')}
Company: {job.get('company', '')}
Description: {job.get('description', '')[:5000]}

CANDIDATE PROFILE (this is the candidate's ACTUAL background — only reference information from here):
{_profile_summary(profile, selected_experiences)}

Write the cover letter now. Start with "Dear Hiring Team," or similar appropriate salutation."""

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

    prompt = f"""You are an expert resume editor performing a SCOPED EDIT.

Your job has two halves, equally important:
1. INSIDE the scope of the user's instruction, make the change FULLY and SUBSTANTIVELY. If they say "rewrite the summary," rewrite the entire summary. If they say "make the bullets stronger," genuinely strengthen every bullet. Do not be timid — a 1–5 word change is a failure when the user asked for a rewrite.
2. OUTSIDE the scope of the instruction, preserve the existing text verbatim. Do not rephrase, reorder, or "polish" sections the user did not mention.

Determine the scope from the instruction itself:
- "rewrite the summary" → summary changes substantially; everything else stays.
- "make it more concise" → entire document is in scope.
- "add more cybersecurity emphasis to the bullets" → all experience bullets are in scope.
- "fix the third bullet under [job]" → only that bullet changes.

When in doubt about scope, lean toward applying the edit broadly enough that the user's intent is clearly satisfied.

{_honesty_instruction(honesty_level)}

{_revise_fabrication_guard(honesty_level)}

OUTPUT FORMAT RULES (the document parser requires these exactly — preserve them):
- Section headers in ALL CAPS on their own line: SUMMARY, SKILLS, EXPERIENCE, EDUCATION, CERTIFICATIONS
- No markdown (no **, no ##, no ```, no * bullets). Plain dashes (-) for bullets.
- Do NOT include the candidate's name or contact info.
- For each experience entry:
  Title: [exact title from profile]
  Company: [exact company from profile]
  Dates: [exact dates from profile]
  - [bullet point]
- Job titles, company names, and dates copied VERBATIM from the candidate profile.

CANDIDATE PROFILE (only use facts from here — never invent):
{_profile_summary(profile, selected_experiences)}

JOB POSTING (target role — maintain relevance to this):
Title: {job.get('title', '')}
Company: {job.get('company', '')}
Description: {job.get('description', '')[:5000]}

USER REVISION INSTRUCTIONS (apply ONLY these changes):
{user_instructions}

CURRENT TAILORED RESUME (this is the source of truth — edit it in place, preserve everything not touched by the instruction):
{current_resume_text}

Output the full revised resume now, starting directly with SUMMARY. Apply the user's instruction substantively within its scope; preserve everything outside its scope."""

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

    prompt = f"""You are an expert cover letter editor performing a SCOPED EDIT.

Your job has two halves, equally important:
1. INSIDE the scope of the user's instruction, make the change FULLY and SUBSTANTIVELY. If they say "rewrite the opening," rewrite the entire opening. If they say "make it more enthusiastic," genuinely shift the tone throughout. Do not be timid — tiny token-level changes when the user asked for a rewrite are a failure.
2. OUTSIDE the scope of the instruction, preserve the existing prose verbatim. Do not rephrase or "polish" paragraphs the user did not mention.

When in doubt about scope, lean toward applying the edit broadly enough that the user's intent is clearly satisfied.

{_honesty_instruction(honesty_level)}

{_tone_instruction(tone)}

{_revise_fabrication_guard(honesty_level)}

Additional rules:
- Do NOT use placeholder text like [Company Name] or [Your Name] — use actual values.
- Do NOT start with "I am writing to apply for..." unless the user explicitly requests it.
- Output the full revised cover letter as plain prose paragraphs. No markdown, no headers.

CANDIDATE PROFILE (only use facts from here):
{_profile_summary(profile, selected_experiences)}

JOB POSTING:
Title: {job.get('title', '')}
Company: {job.get('company', '')}
Description: {job.get('description', '')[:5000]}

USER REVISION INSTRUCTIONS (apply ONLY these changes):
{user_instructions}

CURRENT COVER LETTER (source of truth — edit in place, preserve everything not touched):
{current_letter_text}

Output the full revised cover letter now. Apply the user's instruction substantively within its scope; preserve everything outside its scope."""

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

    prompt = f"""Compare the original profile data below against the two generated documents.
List every addition, change, or embellishment — anything in the documents that was not in, or significantly differs from, the original profile.

ORIGINAL PROFILE:
{_profile_summary(profile)}

GENERATED RESUME:
{resume_text[:3000]}

GENERATED COVER LETTER:
{cover_letter_text[:2000]}

Return a JSON object with exactly two arrays:
{{
  "resume_changes": [{{"field": "...", "original": "...", "modified": "..."}}],
  "cover_letter_changes": [{{"field": "...", "original": "...", "modified": "..."}}]
}}

If nothing was changed in a document, return an empty array for that key.
Return ONLY the JSON object, no other text."""

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

    prompt = f"""You are helping a job candidate answer custom application questions.
Answer each question professionally and concisely based on the candidate's profile.

JOB:
Title: {job.get('title', '')}
Company: {job.get('company', '')}

CANDIDATE PROFILE:
{_profile_summary(profile)}

QUESTIONS:
{questions_text}

Return a JSON object where each key is the exact question text and each value is the answer.
Return only the JSON object, no other text."""

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
