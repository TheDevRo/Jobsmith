"""
tests/test_honesty_prompts.py

Unit tests for honesty-level prompt injection and embellishment log generation.
All LLM calls are mocked — no LM Studio required.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.ai_engine import (
    _honesty_instruction,
    generate_cover_letter,
    generate_embellishment_log,
    generate_tailored_resume,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_CONFIG = {
    "ai": {
        "base_url": "http://localhost:1234/v1",
        "api_key":  "test",
        "temperature": 0.7,
        "max_tokens":  2000,
    }
}

SAMPLE_JOB = {
    "title":       "Senior Software Engineer",
    "company":     "Acme Corp",
    "description": "We need a senior engineer with Python and AWS skills.",
}

SAMPLE_PROFILE = {
    "full_name": "Jane Doe",
    "summary":   "Software engineer with 5 years of Python experience.",
    "skills":    ["Python", "AWS", "Docker"],
    "experience": [
        {
            "title":      "Software Engineer",
            "company":    "Old Corp",
            "start_date": "2020-01",
            "end_date":   "Present",
            "bullets":    ["Built REST APIs", "Managed CI/CD pipelines"],
        }
    ],
    "education":      [{"degree": "B.S. CS", "school": "State U", "year": "2019"}],
    "certifications": ["AWS SAA"],
}


def _mock_completion(text: str):
    """Return an AsyncMock that mimics an OpenAI chat completion response."""
    choice   = MagicMock()
    choice.message.content = text
    response = MagicMock()
    response.choices = [choice]
    client   = AsyncMock()
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


# ---------------------------------------------------------------------------
# _honesty_instruction — four distinct directives
# ---------------------------------------------------------------------------

class TestHonestyInstruction:
    def test_honest_contains_key_phrase(self):
        inst = _honesty_instruction("honest")
        assert "Do not add, invent, or exaggerate" in inst

    def test_tailored_contains_key_phrase(self):
        inst = _honesty_instruction("tailored")
        assert "most favorable light" in inst

    def test_embellished_contains_key_phrase(self):
        inst = _honesty_instruction("embellished")
        assert "upgrade job titles slightly" in inst

    def test_fabricated_contains_key_phrase(self):
        inst = _honesty_instruction("fabricated")
        assert "invent specific achievements" in inst

    def test_unknown_falls_back_to_honest(self):
        """Unrecognised level should silently fall back to 'honest'."""
        inst = _honesty_instruction("wizard_mode")
        assert "Do not add, invent, or exaggerate" in inst

    def test_all_levels_are_non_empty(self):
        for level in ("honest", "tailored", "embellished", "fabricated"):
            assert _honesty_instruction(level).strip()

    def test_each_level_is_distinct(self):
        instructions = [_honesty_instruction(l) for l in ("honest", "tailored", "embellished", "fabricated")]
        assert len(set(instructions)) == 4, "Each level must produce a unique instruction"


# ---------------------------------------------------------------------------
# generate_tailored_resume — correct system prompt per level
# ---------------------------------------------------------------------------

class TestGenerateTailoredResume:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", ["honest", "tailored", "embellished", "fabricated"])
    async def test_prompt_contains_honesty_instruction(self, level):
        """The LLM receives the expected honesty-level instruction."""
        client = _mock_completion("SUMMARY\nA great engineer.\n\nSKILLS\nPython")
        expected_fragment = _honesty_instruction(level)

        with patch("backend.ai_engine._get_client", return_value=client):
            await generate_tailored_resume(SAMPLE_JOB, SAMPLE_PROFILE, MINIMAL_CONFIG, level)

        call_args = client.chat.completions.create.call_args
        prompt = call_args.kwargs["messages"][0]["content"]
        assert expected_fragment in prompt, (
            f"[{level}] Honesty instruction not found in prompt.\n"
            f"Expected:\n{expected_fragment}\n\nGot (first 500 chars):\n{prompt[:500]}"
        )

    @pytest.mark.asyncio
    async def test_honest_prompt_does_not_contain_fabricated_phrase(self):
        """The 'honest' prompt must not contain the fabricated-mode directive."""
        client = _mock_completion("SUMMARY\nSome text\n\nSKILLS\nPython")
        with patch("backend.ai_engine._get_client", return_value=client):
            await generate_tailored_resume(SAMPLE_JOB, SAMPLE_PROFILE, MINIMAL_CONFIG, "honest")

        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        assert "invent specific achievements" not in prompt

    @pytest.mark.asyncio
    async def test_default_level_is_honest(self):
        """Calling without honesty_level defaults to 'honest'."""
        client = _mock_completion("SUMMARY\nText\n\nSKILLS\nPython")
        with patch("backend.ai_engine._get_client", return_value=client):
            await generate_tailored_resume(SAMPLE_JOB, SAMPLE_PROFILE, MINIMAL_CONFIG)

        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        assert _honesty_instruction("honest") in prompt

    @pytest.mark.asyncio
    async def test_returns_llm_text(self):
        expected = "SUMMARY\nSenior engineer.\n\nSKILLS\nPython, AWS"
        client = _mock_completion(expected)
        with patch("backend.ai_engine._get_client", return_value=client):
            result = await generate_tailored_resume(SAMPLE_JOB, SAMPLE_PROFILE, MINIMAL_CONFIG, "tailored")
        assert result == expected


# ---------------------------------------------------------------------------
# generate_cover_letter — correct system prompt per level
# ---------------------------------------------------------------------------

class TestGenerateCoverLetter:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("level", ["honest", "tailored", "embellished", "fabricated"])
    async def test_prompt_contains_honesty_instruction(self, level):
        client = _mock_completion("Dear Hiring Team,\n\nI am excited...")
        expected_fragment = _honesty_instruction(level)

        with patch("backend.ai_engine._get_client", return_value=client):
            await generate_cover_letter(SAMPLE_JOB, SAMPLE_PROFILE, MINIMAL_CONFIG, level)

        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        assert expected_fragment in prompt, (
            f"[{level}] Honesty instruction not found in cover letter prompt."
        )

    @pytest.mark.asyncio
    async def test_default_level_is_honest(self):
        client = _mock_completion("Dear Hiring Team,\n\nSome text.")
        with patch("backend.ai_engine._get_client", return_value=client):
            await generate_cover_letter(SAMPLE_JOB, SAMPLE_PROFILE, MINIMAL_CONFIG)

        prompt = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        assert _honesty_instruction("honest") in prompt

    @pytest.mark.asyncio
    async def test_returns_llm_text(self):
        expected = "Dear Hiring Manager,\n\nI bring relevant experience."
        client = _mock_completion(expected)
        with patch("backend.ai_engine._get_client", return_value=client):
            result = await generate_cover_letter(SAMPLE_JOB, SAMPLE_PROFILE, MINIMAL_CONFIG, "embellished")
        assert result == expected


# ---------------------------------------------------------------------------
# generate_embellishment_log — structure, WARNING field, empty-change handling
# ---------------------------------------------------------------------------

_VALID_LOG_RESPONSE = json.dumps({
    "resume_changes": [
        {"field": "summary", "original": "5 years", "modified": "7 years"},
    ],
    "cover_letter_changes": [],
})

_EMPTY_LOG_RESPONSE = json.dumps({
    "resume_changes": [],
    "cover_letter_changes": [],
})


class TestGenerateEmbellishmentLog:
    @pytest.mark.asyncio
    async def test_log_has_required_keys(self):
        client = _mock_completion(_VALID_LOG_RESPONSE)
        with patch("backend.ai_engine._get_client", return_value=client):
            log = await generate_embellishment_log(
                SAMPLE_PROFILE, "resume text", "cover letter text", "tailored", MINIMAL_CONFIG
            )
        assert "honesty_level" in log
        assert "resume_changes" in log
        assert "cover_letter_changes" in log
        assert "generated_at" in log

    @pytest.mark.asyncio
    async def test_log_captures_changes(self):
        client = _mock_completion(_VALID_LOG_RESPONSE)
        with patch("backend.ai_engine._get_client", return_value=client):
            log = await generate_embellishment_log(
                SAMPLE_PROFILE, "resume", "cover", "embellished", MINIMAL_CONFIG
            )
        assert len(log["resume_changes"]) == 1
        assert log["resume_changes"][0]["field"] == "summary"
        assert log["cover_letter_changes"] == []

    @pytest.mark.asyncio
    async def test_honesty_level_stored(self):
        client = _mock_completion(_EMPTY_LOG_RESPONSE)
        with patch("backend.ai_engine._get_client", return_value=client):
            log = await generate_embellishment_log(
                SAMPLE_PROFILE, "r", "c", "honest", MINIMAL_CONFIG
            )
        assert log["honesty_level"] == "honest"

    @pytest.mark.asyncio
    async def test_fabricated_adds_warning(self):
        client = _mock_completion(_EMPTY_LOG_RESPONSE)
        with patch("backend.ai_engine._get_client", return_value=client):
            log = await generate_embellishment_log(
                SAMPLE_PROFILE, "r", "c", "fabricated", MINIMAL_CONFIG
            )
        assert "WARNING" in log
        assert "fabricated" in log["WARNING"].lower()

    @pytest.mark.asyncio
    async def test_non_fabricated_has_no_warning(self):
        for level in ("honest", "tailored", "embellished"):
            client = _mock_completion(_EMPTY_LOG_RESPONSE)
            with patch("backend.ai_engine._get_client", return_value=client):
                log = await generate_embellishment_log(
                    SAMPLE_PROFILE, "r", "c", level, MINIMAL_CONFIG
                )
            assert "WARNING" not in log, f"Unexpected WARNING for level '{level}'"

    @pytest.mark.asyncio
    async def test_llm_failure_returns_empty_log(self):
        """If the LLM call throws, the function returns a valid empty log rather than raising."""
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(side_effect=RuntimeError("network error"))
        with patch("backend.ai_engine._get_client", return_value=client):
            log = await generate_embellishment_log(
                SAMPLE_PROFILE, "r", "c", "honest", MINIMAL_CONFIG
            )
        assert log["resume_changes"] == []
        assert log["cover_letter_changes"] == []
        assert log["honesty_level"] == "honest"

    @pytest.mark.asyncio
    async def test_malformed_json_falls_back_gracefully(self):
        """Partial/malformed JSON from LLM should not crash the function."""
        client = _mock_completion("Sure! Here are the changes: [not valid json]")
        with patch("backend.ai_engine._get_client", return_value=client):
            log = await generate_embellishment_log(
                SAMPLE_PROFILE, "r", "c", "tailored", MINIMAL_CONFIG
            )
        assert isinstance(log["resume_changes"], list)
        assert isinstance(log["cover_letter_changes"], list)

    @pytest.mark.asyncio
    async def test_malformed_entries_are_dropped(self):
        """Change entries missing required keys are silently dropped."""
        bad_response = json.dumps({
            "resume_changes": [
                {"field": "x"},          # missing original/modified
                {"field": "y", "original": "a", "modified": "b"},  # valid
            ],
            "cover_letter_changes": [],
        })
        client = _mock_completion(bad_response)
        with patch("backend.ai_engine._get_client", return_value=client):
            log = await generate_embellishment_log(
                SAMPLE_PROFILE, "r", "c", "embellished", MINIMAL_CONFIG
            )
        assert len(log["resume_changes"]) == 1
        assert log["resume_changes"][0]["field"] == "y"

    @pytest.mark.asyncio
    async def test_fabricated_warning_logged(self, caplog):
        """FABRICATED generation must emit a WARNING-level log entry."""
        import logging
        client = _mock_completion(_EMPTY_LOG_RESPONSE)
        with patch("backend.ai_engine._get_client", return_value=client):
            with caplog.at_level(logging.WARNING, logger="backend.ai_engine"):
                await generate_embellishment_log(
                    SAMPLE_PROFILE, "r", "c", "fabricated", MINIMAL_CONFIG
                )
        assert any("FABRICATED" in r.message for r in caplog.records)
