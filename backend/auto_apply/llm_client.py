"""
auto_apply/llm_client.py — Local LM Studio HTTP client.

NEVER calls external AI providers.  All requests go to the host:port
configured under ai.base_url in config.yaml (default http://localhost:1234/v1),
which is where LM Studio listens with its OpenAI-compatible API.

Public helpers
--------------
map_fields_to_values(profile, job, fields, answer_bank) → list[FieldValue]
    Ask the LLM to map each detected form field to a concrete value.

generate_answer(question, profile, job) → str
    Generate a short free-text answer for a specific question.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

import aiohttp

from .. import prompt_registry

if TYPE_CHECKING:
    from .models import FieldDescriptor, FieldValue, JobApplicationRequest, UserProfile

logger = logging.getLogger(__name__)

# Upload fields that clearly want something other than a resume/cover letter —
# defaulting the resume into these would be wrong. Mirror of
# FieldMapper.nonResumeUploadTokens in the iOS JobsmithKit port.
_NON_RESUME_UPLOAD_TOKENS = (
    "photo", "picture", "image", "avatar", "headshot",
    "portfolio", "transcript", "certific", "license",
    "passport", "visa", "sample",
)


class LLMClient:
    """
    Thin async wrapper around the LM Studio OpenAI-compatible chat/completions
    endpoint.  Reads all settings from the project config dict.
    """

    def __init__(self, config: dict) -> None:
        self._config = config  # kept for prompt_registry override lookups
        ai = config.get("ai", {})
        self.base_url   = ai.get("base_url", "http://localhost:1234/v1").rstrip("/")
        self.api_key    = ai.get("api_key") or "lm-studio"
        self.model      = (
            ai.get("models", {}).get("fast", {}).get("model", "local-model")
        )
        if not self.model:
            logger.warning(
                "LLMClient: ai.models.fast.model is empty — no fast model configured. "
                "LM Studio will use whatever model is currently loaded."
            )
        self.temperature = float(ai.get("temperature", 0.3))
        self.max_tokens  = int(ai.get("max_tokens", 4096))

    # ------------------------------------------------------------------
    # Low-level completions
    # ------------------------------------------------------------------

    async def complete(
        self,
        system: str,
        user: str,
        max_retries: int = 3,
        override_max_tokens: int | None = None,
    ) -> str:
        """Return the assistant's raw text response from one chat turn."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens":  override_max_tokens or self.max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type":  "application/json",
        }

        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{self.base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=90),
                    ) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
                        return data["choices"][0]["message"]["content"]
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries:
                    # Exponential backoff: 1 s, 2 s, 4 s — prevents hammering a
                    # slow LM Studio instance (pattern from AIHawk's rate-limit handler)
                    wait = 2 ** (attempt - 1)
                    logger.warning(
                        "LLMClient.complete attempt %d/%d failed, retrying in %ds: %s",
                        attempt, max_retries, wait, exc,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        "LLMClient.complete failed after %d attempts: %s",
                        max_retries, exc,
                    )

        raise RuntimeError(
            f"LM Studio call failed after {max_retries} attempts: {last_exc}"
        )

    async def complete_json(
        self,
        system: str,
        user: str,
        max_retries: int = 3,
    ) -> list | dict:
        """
        Call LM Studio and parse the response as JSON.

        Strips markdown code fences if present and retries on parse failure.
        """
        last_text = ""
        for attempt in range(1, max_retries + 1):
            text = await self.complete(system, user)
            last_text = text
            try:
                return _extract_json(text)
            except (json.JSONDecodeError, ValueError):
                logger.warning(
                    "LLMClient JSON parse failed (attempt %d/%d): %.200s",
                    attempt, max_retries, text,
                )
                logger.debug(
                    "LLMClient raw LLM response (attempt %d/%d, full): %s",
                    attempt, max_retries, text,
                )
                # Append a reminder on the next attempt by tweaking the user prompt
                user = user + "\n\n[IMPORTANT: Return ONLY valid JSON. No markdown, no extra text.]"

        raise ValueError(
            f"LM Studio returned invalid JSON after {max_retries} attempts. "
            f"Last response: {last_text[:300]}"
        )

    # ------------------------------------------------------------------
    # High-level helpers
    # ------------------------------------------------------------------

    async def map_fields_to_values(
        self,
        profile: "UserProfile",
        job: "JobApplicationRequest",
        fields: "list[FieldDescriptor]",
        answer_bank: dict[str, str],
        chunk_size: int = 25,
    ) -> "list[FieldValue]":
        """
        Ask the LLM to produce a FieldValue for every FieldDescriptor.

        Fields are first checked against the AnswerBank; only unresolved
        fields are sent to the LLM.  If all fields are resolved by the bank,
        the LLM call is skipped entirely.

        The LLM must only use facts present in the profile or answer bank.
        It returns a JSON array that we validate into FieldValue objects.
        """
        from .answer_bank import get_answer_bank
        from .field_matcher import match_profile_fields
        from .models import FieldValue  # local import to avoid circular deps

        if not fields:
            return []

        # --- Phase 0: deterministic mapping for file inputs ---
        # File fields are matched by keyword (resume/cv vs cover letter) and
        # emitted as action="upload" with a kind token in `value`. The browser
        # extension swaps the token for the actual file bytes at fill time.
        # File inputs never reach the LLM — it can't attach files, so a
        # fall-through would come back "skip" and the upload silently
        # degrades to manual.
        file_resolved: dict[str, FieldValue] = {}
        remaining_fields: list["FieldDescriptor"] = []
        for f in fields:
            if (f.field_type or "").lower() == "file":
                # Greenhouse-style file inputs label themselves "Attach" and
                # carry no `name` attribute — the only meaningful signal is
                # `id="resume"` / `id="cover_letter"`, which we send as
                # field_id. Workday-style inputs have no signal at all (a
                # generated field_id and a "Select files" button); the drop
                # zone's group text, when present, arrives as extra_context.
                hay = (
                    f"{f.label or ''} {f.name or ''} {f.placeholder or ''} "
                    f"{f.field_id or ''} {f.extra_context or ''}"
                ).lower()
                if "cover" in hay:
                    kind = "cover_letter"
                elif any(tok in hay for tok in ("resume", "cv", "curriculum")):
                    kind = "resume"
                elif any(tok in hay for tok in _NON_RESUME_UPLOAD_TOKENS):
                    # A different kind of document — nothing sensible to
                    # attach, and the LLM can't help; leave it manual.
                    file_resolved[f.field_id] = FieldValue(
                        field_id=f.field_id,
                        value="",
                        action="skip",
                        confidence=0.0,
                        source="skip",
                    )
                    continue
                else:
                    kind = "resume"  # unlabeled uploader — default to resume
                file_resolved[f.field_id] = FieldValue(
                    field_id=f.field_id,
                    value=kind,
                    action="upload",
                    confidence=0.95,
                    source="profile",
                )
                continue
            remaining_fields.append(f)

        # --- Phase 0.5: deterministic profile matching ---
        # Contact info, address, links, salary, work auth, EEO, education,
        # availability — matched by autocomplete attr + label/name regex rules
        # so the common fields never depend on LLM output.
        det_resolved = match_profile_fields(profile, remaining_fields)
        if det_resolved:
            logger.debug(
                "map_fields_to_values: %d field(s) resolved deterministically",
                len(det_resolved),
            )
        _after_det: list["FieldDescriptor"] = []
        for f in remaining_fields:
            if f.field_id in det_resolved:
                continue
            # Password fields must never reach the LLM — if the matcher had a
            # credential it already used it; otherwise skip outright.
            if (f.field_type or "").lower() == "password":
                det_resolved[f.field_id] = FieldValue(
                    field_id=f.field_id, value="", action="skip",
                    confidence=0.0, source="skip",
                )
                continue
            _after_det.append(f)
        remaining_fields = _after_det

        # --- Phase 1: resolve fields from the answer bank ---
        bank = get_answer_bank()
        bank_resolved: dict[str, FieldValue] = {}
        llm_fields: list["FieldDescriptor"] = []

        for f in remaining_fields:
            # Combine label + group context (fieldset legend etc.) so bank
            # keyword matching sees the actual question, not just "Yes".
            question_text = " ".join(
                filter(None, (f.label, f.extra_context))
            ) or f.name
            match = bank.find_best_match(question_text) if question_text else None
            if match:
                bank_resolved[f.field_id] = FieldValue(
                    field_id=f.field_id,
                    value=match,
                    action="fill",
                    confidence=1.0,
                    source="answer_bank",
                )
                logger.debug(
                    "map_fields_to_values: field %r resolved from answer_bank", f.field_id
                )
            else:
                llm_fields.append(f)

        # --- Phase 2: LLM call(s) for remaining fields (chunked, skipped if none) ---
        llm_results: list[FieldValue] = []
        if llm_fields:
            system = prompt_registry.get_template(self._config, "auto_apply_field_map")
            chunks = [
                llm_fields[i:i + chunk_size]
                for i in range(0, len(llm_fields), chunk_size)
            ]

            async def _call_chunk(chunk: "list[FieldDescriptor]"):
                user = _build_field_map_user(profile, job, chunk, answer_bank)
                try:
                    return await self.complete_json(system, user)
                except Exception as exc:
                    logger.error(
                        "map_fields_to_values chunk (%d fields) failed: %s",
                        len(chunk), exc,
                    )
                    return []

            chunk_raws = await asyncio.gather(*(_call_chunk(c) for c in chunks))

            mapped_ids: set[str] = set()
            skipped_malformed: list[str] = []
            for raw in chunk_raws:
                raw_list = raw if isinstance(raw, list) else []
                for item in raw_list:
                    if not isinstance(item, dict):
                        skipped_malformed.append(repr(item)[:80])
                        continue
                    try:
                        fv = FieldValue(**item)
                        llm_results.append(fv)
                        mapped_ids.add(fv.field_id)
                    except Exception as _parse_exc:
                        skipped_malformed.append(f"{item.get('field_id', '?')}: {_parse_exc}")

            if skipped_malformed:
                logger.warning(
                    "map_fields_to_values: %d malformed LLM item(s) skipped: %s",
                    len(skipped_malformed), skipped_malformed[:5],
                )

            # Fill gaps for any llm_fields the LLM omitted
            gap_filled: list[str] = []
            for f in llm_fields:
                if f.field_id not in mapped_ids:
                    gap_filled.append(f.field_id)
                    llm_results.append(
                        FieldValue(field_id=f.field_id, value="", action="skip",
                                   confidence=0.0, source="skip")
                    )
            if gap_filled:
                logger.warning(
                    "map_fields_to_values: %d field(s) gap-filled as skip (LLM omitted them): %s",
                    len(gap_filled), gap_filled,
                )

            logger.debug(
                "map_fields_to_values: %d/%d LLM fields parsed across %d chunk(s)",
                len(mapped_ids), len(llm_fields), len(chunks),
            )
        else:
            logger.debug("map_fields_to_values: all %d field(s) resolved from answer_bank; skipping LLM call", len(fields))

        # --- Phase 3: merge in original field order ---
        llm_by_id = {fv.field_id: fv for fv in llm_results}
        out: list[FieldValue] = []
        for f in fields:
            if f.field_id in file_resolved:
                out.append(file_resolved[f.field_id])
            elif f.field_id in det_resolved:
                out.append(det_resolved[f.field_id])
            elif f.field_id in bank_resolved:
                out.append(bank_resolved[f.field_id])
            elif f.field_id in llm_by_id:
                out.append(llm_by_id[f.field_id])
        return out

    async def generate_answer(
        self,
        question: str,
        profile: "UserProfile",
        job: "JobApplicationRequest",
        max_words: int = 80,
    ) -> str:
        """
        Generate a concise free-text answer for a single question.

        Uses only facts from the profile — never invents employers, dates, etc.
        """
        system = prompt_registry.render_prompt(
            self._config, "auto_apply_answer", max_words=max_words
        )
        user = (
            f"CANDIDATE PROFILE:\n{profile.to_text()}\n\n"
            f"JOB: {job.title} at {job.company}\n\n"
            f"QUESTION: {question}\n\n"
            "ANSWER:"
        )
        return await self.complete(system, user, override_max_tokens=512)


# ---------------------------------------------------------------------------
# Prompt templates — the system prompts live in prompt_registry (keys
# "auto_apply_field_map" and "auto_apply_answer") so they can be edited
# from Settings → Prompts.
# ---------------------------------------------------------------------------


def _build_field_map_user(
    profile: "UserProfile",
    job: "JobApplicationRequest",
    fields: "list[FieldDescriptor]",
    answer_bank: dict[str, str],
) -> str:
    bank_str = json.dumps(
        {k: v for k, v in answer_bank.items()
         if not (v.startswith("<") and v.endswith(">"))},  # omit placeholders
        indent=2, ensure_ascii=False,
    )
    fields_str = json.dumps(
        [f.model_dump() for f in fields],
        indent=2, ensure_ascii=False,
    )
    return (
        f"CANDIDATE PROFILE:\n{profile.to_text()}\n\n"
        f"JOB:\nTitle: {job.title}\nCompany: {job.company}\n"
        f"Description (first 300 chars):\n{job.description[:300]}\n\n"
        f"ANSWER BANK:\n{bank_str}\n\n"
        f"FORM FIELDS TO MAP:\n{fields_str}\n\n"
        "Return the JSON array now."
    )


# ---------------------------------------------------------------------------
# JSON extraction helper
# ---------------------------------------------------------------------------

# A real answer-bank/field-map payload is a few KB; anything past this is either
# a runaway generation or an attempt to blow the repair pass's stack.
_MAX_JSON_REPAIR_CHARS = 200_000


def _extract_json(text: str) -> list | dict:
    """
    Strip markdown fences and parse JSON from LLM output.

    Fallback chain (handles quirks of local LLMs):
      1. json.loads             — standard JSON
      2. json.loads(_normalize) — Python-style single quotes / trailing commas,
                                   repaired to strict JSON WITHOUT evaluating the
                                   model output as code

    Leading prose before the first [ or { is trimmed.
    Trailing prose after the matching closing ] or } is trimmed using a
    bracket-depth counter so models that append explanations don't break
    parsing.

    Pattern from AIHawk: local models frequently return single-quoted dicts
    or prepend/append commentary around the JSON payload. We repair those
    quirks textually rather than handing the string to any Python evaluator.
    """
    text = text.strip()

    # Strip markdown code fences
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()

    # Trim any leading non-JSON prose before the first [ or {
    # Only trim if the text does not already start with a valid JSON opener —
    # searching for { inside [{"key":...}] would incorrectly truncate to {"key":...}].
    if text and text[0] not in ("[", "{"):
        for start_char in ("[", "{"):
            idx = text.find(start_char)
            if idx >= 0:
                text = text[idx:]
                break

    # Trim trailing prose after the closing ] or } using a bracket-depth
    # counter.  This handles models that append an explanation after the JSON.
    text = _trim_trailing_prose(text)

    # Attempt 1: standard JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Attempt 2: repair Python-style near-JSON (single-quoted strings/keys,
    # trailing commas) into strict JSON and parse it with json.loads.
    #
    # This deliberately does NOT use ast.literal_eval or any other evaluator:
    # the model output is only ever transformed textually and handed to the
    # strict JSON parser, so malformed or hostile output cannot execute code.
    # Cap the input so the char-by-char repair pass can't be turned into a
    # resource-exhaustion vector by a runaway generation.
    if len(text) <= _MAX_JSON_REPAIR_CHARS:
        try:
            result = json.loads(_normalize_json_like(text))
            if isinstance(result, (list, dict)):
                return result
        except json.JSONDecodeError:
            pass

    # Give up — caller will retry with a stricter prompt
    raise json.JSONDecodeError("Cannot parse LLM output as JSON or Python literal", text, 0)


def _normalize_json_like(text: str) -> str:
    """
    Best-effort repair of near-JSON emitted by local models so it parses as
    strict JSON, without evaluating it as code.

    Two conservative, string-aware transforms:
      * unambiguous single-quoted strings/keys → double-quoted
      * trailing commas before a closing ] or } removed

    Both passes track string state so commas or quotes *inside* string values
    are left untouched. Genuinely ambiguous input (e.g. an apostrophe inside a
    single-quoted value) simply won't parse and falls through to the caller's
    error path — same outcome as before, with no code evaluation.
    """
    return _strip_trailing_commas(_single_to_double_quotes(text))


def _single_to_double_quotes(text: str) -> str:
    """Convert single-quoted strings/keys to double-quoted, skipping over any
    already-double-quoted spans so their contents are preserved verbatim."""
    out: list[str] = []
    i, n = 0, len(text)
    in_double = False   # inside a "…" span (leave verbatim)
    in_single = False   # inside a '…' span we are rewriting to "…"
    while i < n:
        ch = text[i]
        if in_double:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if ch == '"':
                in_double = False
            i += 1; continue
        if in_single:
            if ch == "\\" and i + 1 < n:
                out.append(ch); out.append(text[i + 1]); i += 2; continue
            if ch == '"':
                out.append('\\"'); i += 1; continue   # escape bare " inside
            if ch == "'":
                out.append('"'); in_single = False; i += 1; continue
            out.append(ch); i += 1; continue
        # Outside any string
        if ch == '"':
            in_double = True; out.append(ch); i += 1; continue
        if ch == "'":
            in_single = True; out.append('"'); i += 1; continue
        out.append(ch); i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    """Drop commas that immediately precede a closing ] or } (ignoring
    whitespace), skipping over double-quoted strings."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if ch == '"':
                in_string = False
            i += 1; continue
        if ch == '"':
            in_string = True; out.append(ch); i += 1; continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "]}":
                i += 1; continue   # drop this trailing comma
        out.append(ch); i += 1
    return "".join(out)


def _trim_trailing_prose(text: str) -> str:
    """
    Return the shortest prefix of *text* that contains a complete top-level
    JSON array or object, discarding any characters that follow the matching
    closing bracket.

    Uses a bracket-depth counter that also skips over string literals so that
    brackets inside quoted values do not affect the depth count.
    """
    if not text:
        return text

    opener = text[0]
    if opener == "[":
        closer = "]"
    elif opener == "{":
        closer = "}"
    else:
        return text  # No JSON opener found — return as-is

    depth = 0
    in_string = False
    escape_next = False

    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[: i + 1]

    # Unbalanced — return original so the caller's parser gives a clear error
    return text
