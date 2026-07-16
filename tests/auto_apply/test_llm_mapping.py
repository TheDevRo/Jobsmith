"""
tests/auto_apply/test_llm_mapping.py

Unit tests for the LLM field-mapping pipeline.

All LLM calls are mocked so tests run offline, without LM Studio.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Make backend importable without installing
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.auto_apply.llm_client import LLMClient, _extract_json
from backend.auto_apply.models import (
    FieldDescriptor,
    FieldValue,
    JobApplicationRequest,
    UserProfile,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def profile() -> UserProfile:
    raw = json.loads((FIXTURES / "sample_profile.json").read_text())
    return UserProfile(**raw)


@pytest.fixture()
def job() -> JobApplicationRequest:
    return JobApplicationRequest(
        job_id="test-job-001",
        title="Senior Python Engineer",
        company="TestCo",
        url="https://boards.greenhouse.io/testco/jobs/123",
        description="We are looking for a senior Python engineer with 5+ years of experience.",
    )


@pytest.fixture()
def fields() -> list[FieldDescriptor]:
    raw = json.loads((FIXTURES / "sample_field_descriptors.json").read_text())
    return [FieldDescriptor(**f) for f in raw]


@pytest.fixture()
def config() -> dict:
    return {
        "ai": {
            "base_url":    "http://localhost:1234/v1",
            "api_key":     "lm-studio",
            "temperature": 0.3,
            "max_tokens":  4096,
            "models": {
                "fast": {"model": "test-model"},
            },
        }
    }


@pytest.fixture()
def llm(config) -> LLMClient:
    return LLMClient(config)


# ---------------------------------------------------------------------------
# _extract_json helper
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_plain_json_array(self):
        text = '[{"a": 1}]'
        assert _extract_json(text) == [{"a": 1}]

    def test_json_with_markdown_fence(self):
        text = '```json\n[{"a": 1}]\n```'
        assert _extract_json(text) == [{"a": 1}]

    def test_json_with_plain_fence(self):
        text = '```\n{"key": "value"}\n```'
        assert _extract_json(text) == {"key": "value"}

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _extract_json("not json at all")

    def test_nested_json(self):
        text = '[{"field_id": "f-0", "value": "Jane", "action": "fill", "confidence": 0.99, "source": "profile"}]'
        result = _extract_json(text)
        assert result[0]["field_id"] == "f-0"

    def test_single_quotes_and_trailing_commas_repaired(self):
        """The Attempt-2 fallback repairs Python-style single quotes and
        trailing commas into strict JSON — without evaluating the text."""
        text = (
            "[{'field_id': 'f-0', 'value': 'Jane', 'action': 'fill',},]"
        )
        result = _extract_json(text)
        assert result == [
            {"field_id": "f-0", "value": "Jane", "action": "fill"}
        ]

    def test_commas_inside_string_values_survive_repair(self):
        """Trailing-comma stripping is string-aware: a literal ',}' inside a
        quoted value must not be mangled."""
        text = "{'value': 'a, b, c', 'note': 'ends here',}"
        result = _extract_json(text)
        assert result == {"value": "a, b, c", "note": "ends here"}


# ---------------------------------------------------------------------------
# map_fields_to_values — mocked LLM
# ---------------------------------------------------------------------------

MOCK_LLM_RESPONSE = json.dumps([
    {"field_id": "field-0", "value": "Jane",              "action": "fill",   "confidence": 1.0,  "source": "profile"},
    {"field_id": "field-1", "value": "Doe",               "action": "fill",   "confidence": 1.0,  "source": "profile"},
    {"field_id": "field-2", "value": "jane.doe@example.com", "action": "fill","confidence": 1.0,  "source": "profile"},
    {"field_id": "field-3", "value": "555-555-5555",       "action": "fill",  "confidence": 1.0,  "source": "profile"},
    {"field_id": "field-4", "value": "5",                  "action": "fill",  "confidence": 0.80, "source": "profile"},
    {"field_id": "field-5", "value": "Yes",                "action": "select","confidence": 1.0,  "source": "profile"},
    {"field_id": "field-6", "value": "No",                 "action": "select","confidence": 1.0,  "source": "profile"},
    {"field_id": "field-7", "value": "I'm a full-stack engineer with 5 years...", "action": "fill", "confidence": 0.85, "source": "llm_generated"},
    {"field_id": "field-8", "value": "90000",              "action": "fill",  "confidence": 1.0,  "source": "profile"},
    {"field_id": "field-9", "value": "https://linkedin.com/in/janedoe", "action": "fill", "confidence": 1.0, "source": "profile"},
])


@pytest.mark.asyncio
async def test_map_fields_returns_field_values(llm, profile, job, fields):
    with patch.object(llm, "complete", new=AsyncMock(return_value=MOCK_LLM_RESPONSE)):
        results = await llm.map_fields_to_values(profile, job, fields, answer_bank={})

    assert len(results) == len(fields)
    assert all(isinstance(r, FieldValue) for r in results)


@pytest.mark.asyncio
async def test_map_fields_correct_values(llm, profile, job, fields):
    with patch.object(llm, "complete", new=AsyncMock(return_value=MOCK_LLM_RESPONSE)):
        results = await llm.map_fields_to_values(profile, job, fields, answer_bank={})

    mapping = {r.field_id: r for r in results}
    assert mapping["field-0"].value == "Jane"
    assert mapping["field-1"].value == "Doe"
    assert mapping["field-2"].value == "jane.doe@example.com"
    assert mapping["field-5"].action == "select"
    assert mapping["field-5"].value == "Yes"
    assert mapping["field-6"].value == "No"


@pytest.mark.asyncio
async def test_file_inputs_never_reach_the_llm(llm, profile, job):
    """Phase 0 resolves every file input deterministically — Workday-style
    anonymous inputs default to resume, drop-zone context is honored, and
    clearly-other uploads (photo, portfolio, …) are skipped, all without an
    LLM call."""
    fields = [
        # Workday shape: generated field_id, no label/name keyword match.
        FieldDescriptor(field_id="field_23", label="Select files", field_type="file"),
        # The drop zone's text arrives only as extra_context.
        FieldDescriptor(field_id="field_9", field_type="file",
                        extra_context="Upload your Cover Letter here"),
        # Not a resume slot — deterministic skip.
        FieldDescriptor(field_id="field_4", label="Profile photo", field_type="file"),
    ]
    with patch.object(llm, "complete", new=AsyncMock(side_effect=AssertionError("LLM must not be called"))):
        results = await llm.map_fields_to_values(profile, job, fields, answer_bank={})

    mapping = {r.field_id: r for r in results}
    assert (mapping["field_23"].action, mapping["field_23"].value) == ("upload", "resume")
    assert (mapping["field_9"].action, mapping["field_9"].value) == ("upload", "cover_letter")
    assert mapping["field_4"].action == "skip"


# Fixture fields that can't be resolved deterministically from the profile and
# therefore reach the LLM: field-4 is skill-specific ("years of experience with
# Python") and field-7 is an open-ended textarea.
LLM_BOUND = {"field-4", "field-7"}


@pytest.mark.asyncio
async def test_map_fields_fills_gaps_with_skip(llm, profile, job, fields):
    """If the LLM omits an LLM-bound field, a skip entry is inserted for it."""
    # Respond with an unrelated field only, so both LLM-bound fields are omitted
    partial_response = json.dumps([
        {"field_id": "field-0", "value": "Jane", "action": "fill", "confidence": 1.0, "source": "profile"},
    ])
    with patch.object(llm, "complete", new=AsyncMock(return_value=partial_response)):
        results = await llm.map_fields_to_values(profile, job, fields, answer_bank={})

    assert len(results) == len(fields)
    skipped = {r.field_id for r in results if r.action == "skip"}
    assert skipped == LLM_BOUND


@pytest.mark.asyncio
async def test_map_fields_on_llm_error_keeps_deterministic(llm, profile, job, fields):
    """If the LLM call fails entirely, only LLM-bound fields are skipped —
    deterministically matched profile fields still come back filled."""
    with patch.object(llm, "complete", new=AsyncMock(side_effect=RuntimeError("LM Studio down"))):
        results = await llm.map_fields_to_values(profile, job, fields, answer_bank={})

    assert len(results) == len(fields)
    by_id = {r.field_id: r for r in results}
    for fid in LLM_BOUND:
        assert by_id[fid].action == "skip"
        assert by_id[fid].confidence == 0.0
    for fid in set(by_id) - LLM_BOUND:
        assert by_id[fid].action != "skip"
        assert by_id[fid].value


@pytest.mark.asyncio
async def test_map_fields_retries_on_bad_json(llm, profile, job, fields):
    """
    If LLM returns invalid JSON, complete_json should retry up to max_retries,
    eventually raising; LLM-bound fields degrade to skip without raising.
    """
    with patch.object(llm, "complete", new=AsyncMock(return_value="not valid json")):
        results = await llm.map_fields_to_values(profile, job, fields, answer_bank={})

    # Should gracefully degrade, not raise
    assert len(results) == len(fields)
    assert {r.field_id for r in results if r.action == "skip"} == LLM_BOUND


# ---------------------------------------------------------------------------
# generate_answer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_answer_returns_string(llm, profile, job):
    mock_answer = "I am a passionate engineer with 5 years of experience in Python."
    with patch.object(llm, "complete", new=AsyncMock(return_value=mock_answer)):
        result = await llm.generate_answer("Tell us about yourself", profile, job)

    assert isinstance(result, str)
    assert len(result) > 0
    assert result == mock_answer


@pytest.mark.asyncio
async def test_generate_answer_passes_profile_context(llm, profile, job):
    """Verify the profile text is included in the LLM prompt."""
    captured_user_prompt: list[str] = []

    async def mock_complete(system, user, **kwargs):
        captured_user_prompt.append(user)
        return "mock answer"

    with patch.object(llm, "complete", new=mock_complete):
        await llm.generate_answer("Tell us about yourself", profile, job)

    assert captured_user_prompt, "complete() was never called"
    prompt = captured_user_prompt[0]
    assert profile.full_name in prompt
    assert profile.email in prompt


# ---------------------------------------------------------------------------
# LLMClient config loading
# ---------------------------------------------------------------------------

def test_llm_client_reads_config(config):
    llm = LLMClient(config)
    assert llm.base_url == "http://localhost:1234/v1"
    assert llm.model == "test-model"
    assert llm.api_key == "lm-studio"


def test_llm_client_defaults_on_missing_config():
    llm = LLMClient({})
    assert "localhost" in llm.base_url
    assert llm.model  # some fallback string


# ---------------------------------------------------------------------------
# complete() — model ID and max_tokens reach LM Studio unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_sends_configured_fast_model_and_max_tokens():
    """
    The model ID in the HTTP payload must be exactly what is in
    ai.models.fast.model, regardless of any max_tokens setting.
    The max_tokens in the payload must come from ai.max_tokens, not a
    hardcoded constant.
    """
    cfg = {
        "ai": {
            "base_url":    "http://localhost:1234/v1",
            "api_key":     "lm-studio",
            "temperature": 0.1,
            "max_tokens":  8192,
            "models": {
                "fast": {"model": "mistral-7b"},
            },
        }
    }
    llm = LLMClient(cfg)

    # Capture the JSON payload sent to LM Studio without making a real HTTP call.
    captured_payload: list[dict] = []

    class _MockResponse:
        status = 200
        async def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        def raise_for_status(self): pass

    class _MockSession:
        def post(self, url, json=None, headers=None, timeout=None):
            captured_payload.append(json or {})
            return _MockResponse()
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    with patch("backend.auto_apply.llm_client.aiohttp.ClientSession", return_value=_MockSession()):
        await llm.complete("sys", "user")

    assert captured_payload, "No HTTP call was made"
    payload = captured_payload[0]

    # Model must be the configured fast model, not "qwen2.5" or any other hardcoded value.
    assert payload["model"] == "mistral-7b", (
        f"Expected model 'mistral-7b' but got '{payload['model']}'"
    )

    # max_tokens must reflect ai.max_tokens from config, not a hardcoded constant.
    assert payload["max_tokens"] == 8192, (
        f"Expected max_tokens 8192 (from config) but got {payload['max_tokens']}"
    )


@pytest.mark.asyncio
async def test_complete_override_max_tokens_takes_priority():
    """Callers can still pass override_max_tokens to cap a specific call."""
    cfg = {
        "ai": {
            "base_url":    "http://localhost:1234/v1",
            "api_key":     "lm-studio",
            "temperature": 0.1,
            "max_tokens":  8192,
            "models": {"fast": {"model": "mistral-7b"}},
        }
    }
    llm = LLMClient(cfg)
    captured_payload: list[dict] = []

    class _MockResponse:
        status = 200
        async def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        def raise_for_status(self): pass

    class _MockSession:
        def post(self, url, json=None, headers=None, timeout=None):
            captured_payload.append(json or {})
            return _MockResponse()
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass

    with patch("backend.auto_apply.llm_client.aiohttp.ClientSession", return_value=_MockSession()):
        await llm.complete("sys", "user", override_max_tokens=512)

    assert captured_payload[0]["max_tokens"] == 512
