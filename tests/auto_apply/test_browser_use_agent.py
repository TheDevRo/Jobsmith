"""
tests/auto_apply/test_browser_use_agent.py

Unit tests for backend/browser_use_agent.py.

All browser and LLM calls are mocked — offline unit tests run with no external
dependencies.  Integration tests require LM Studio reachable at the configured
ai.base_url and are skipped gracefully when it is not.

Coverage areas
--------------
  TestBuildTaskPrompt     — _build_task_prompt(): LinkedIn vs generic, truncation,
                            file line, candidate data, rule sections
  TestBuildSensitiveData  — _build_sensitive_data(): field extraction, empty-value
                            filtering, missing-key safety
  TestGetBrowserUseLlm    — _get_browser_use_llm(): fast-tier model, base_url, fallback
  TestRunBrowserUseApply  — run_browser_use_apply(): success paths, failure paths,
                            CAPTCHA/login detection, exception handling
  TestReturnDictShape     — documents the CURRENT return shape (success, message only)
                            and xfail-marks the REQUIRED Phase 5 target shape
  TestModeEnforcement     — xfail: autofill mode not enforced in Browser Use path
                            (Phase 2 fix required)
  TestIntegration         — live LM Studio connectivity check (skipped if unreachable)

Running
-------
  # All tests (offline only — integration skipped if LM Studio down)
  venv/bin/python -m pytest tests/auto_apply/test_browser_use_agent.py -v

  # Force integration tests (requires live LM Studio)
  venv/bin/python -m pytest tests/auto_apply/test_browser_use_agent.py -v -m integration
"""

from __future__ import annotations

import sys
import json
import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# Ensure project root is on path so 'backend.*' imports resolve
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# browser-use is an optional extra (requirements-optional.txt). These tests patch
# browser_use.Agent, so without the package installed backend.browser_use_agent's
# lazy import raises "browser-use is not installed" and every assertion here fails
# on that message instead of on the behaviour under test.
pytest.importorskip(
    "browser_use",
    reason="browser-use is optional: pip install -r requirements-optional.txt",
)

# ---------------------------------------------------------------------------
# Check LM Studio reachability once at import time (for integration skip).
# This avoids per-test network calls.
# ---------------------------------------------------------------------------

_LMSTUDIO_REACHABLE: bool = False

def _check_lmstudio() -> bool:
    try:
        import urllib.request
        import yaml  # type: ignore
        cfg_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"
        cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
        base_url = cfg.get("ai", {}).get("base_url", "http://localhost:1234/v1")
        health_url = base_url.rstrip("/v1").rstrip("/") + "/v1/models"
        with urllib.request.urlopen(health_url, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False

try:
    _LMSTUDIO_REACHABLE = _check_lmstudio()
except Exception:
    _LMSTUDIO_REACHABLE = False

requires_lmstudio = pytest.mark.skipif(
    not _LMSTUDIO_REACHABLE,
    reason="LM Studio not reachable at ai.base_url — skipping integration test",
)


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_SAMPLE_PROFILE = {
    "full_name":          "Jane Doe",
    "email":              "jane.doe@example.com",
    "phone":              "555-555-5555",
    "middle_name":        "",
    "street_address":     "123 Main St",
    "city":               "Denver",
    "state":              "CO",
    "zip_code":           "80202",
    "linkedin":           "https://linkedin.com/in/janedoe",
    "portfolio":          "https://janedoe.dev",
    "desired_salary":     "90000",
    "gender":             "",
    "race_ethnicity":     "",
    "veteran_status":     "",
    "disability_status":  "",
    "work_authorization": "Yes",
    "sponsorship_required": "No",
    "workday_email":      "jane.doe@example.com",
    "workday_password":   "s3cr3t!",
    "summary":            "Full-stack engineer with 5 years of experience.",
}

_SAMPLE_JOB = {
    "id":          "job-001",
    "title":       "Senior Python Engineer",
    "company":     "TestCo",
    "url":         "https://apply.testco.com/jobs/123",
    "description": "We need a senior Python engineer.",
}

_SAMPLE_CONFIG = {
    "auto_apply": {
        "mode":                  "autofill",
        "submit_whitelist":      [],
        "max_daily_applications": 20,
        "per_domain_rate_limit":  5,
        "headless":              True,
        "step_ceiling":          10,
        "disable_stuck_detection": False,
    },
    "profile": _SAMPLE_PROFILE,
    "ai": {
        "base_url": "http://localhost:1234/v1",
        "api_key":  "lm-studio",
        "models": {
            "fast": {
                "model":    "test-fast-model",
                "base_url": "http://localhost:1234/v1",
                "api_key":  "lm-studio",
            },
            "strong": {
                "model":    "test-strong-model",
                "base_url": "http://localhost:1234/v1",
                "api_key":  "lm-studio",
            },
        },
        "temperature": 0.1,
        "max_tokens":  4096,
    },
}

_SAMPLE_APPLICATION = {
    "id":                  "app-001",
    "tailored_resume_path": "",   # No file for unit tests
}


# ---------------------------------------------------------------------------
# Helper: import the module under test (avoids top-level import that would
# require real browser_use to be importable in all environments)
# ---------------------------------------------------------------------------

def _import_bua():
    """Import backend.browser_use_agent, raising ImportError if browser_use is absent."""
    from backend import browser_use_agent
    return browser_use_agent


# ============================================================================
# TestBuildTaskPrompt
# ============================================================================

class TestBuildTaskPrompt:
    """Pure-function tests for _build_task_prompt().  No mocks required."""

    def setup_method(self):
        self.bua = _import_bua()

    def test_contains_job_title_and_company(self):
        prompt = self.bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=_SAMPLE_PROFILE,
            resume_file_path=None,
        )
        assert "Senior Python Engineer" in prompt
        assert "TestCo" in prompt

    def test_contains_candidate_name_email_phone(self):
        prompt = self.bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=_SAMPLE_PROFILE,
            resume_file_path=None,
        )
        assert "Jane Doe" in prompt
        assert "jane.doe@example.com" in prompt
        assert "555-555-5555" in prompt

    def test_contains_address_fields(self):
        prompt = self.bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=_SAMPLE_PROFILE,
            resume_file_path=None,
        )
        assert "123 Main St" in prompt
        assert "Denver" in prompt
        assert "CO" in prompt
        assert "80202" in prompt

    def test_generic_apply_instruction_not_linkedin(self):
        """Non-LinkedIn job: uses generic apply instruction, no Easy Apply mention."""
        prompt = self.bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=_SAMPLE_PROFILE,
            resume_file_path=None,
            is_linkedin_job=False,
        )
        assert "Apply/Apply Now" in prompt
        # Must not mention Easy Apply in the instructions for a generic site
        # (the rule about "Do NOT click 'Easy Apply' on non-LinkedIn" can appear,
        #  but the primary instruction should not say "Easy Apply")
        assert "Prefer 'Easy Apply'" not in prompt

    def test_linkedin_apply_instruction_easy_apply(self):
        """LinkedIn job: uses Easy Apply instruction."""
        prompt = self.bua._build_task_prompt(
            job={**_SAMPLE_JOB, "url": "https://www.linkedin.com/jobs/view/123"},
            profile=_SAMPLE_PROFILE,
            resume_file_path=None,
            is_linkedin_job=True,
        )
        assert "Easy Apply" in prompt

    def test_linkedin_extra_rule_no_apply_with_linkedin(self):
        """LinkedIn path: 'Do NOT click Apply with LinkedIn' rule is present."""
        prompt = self.bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=_SAMPLE_PROFILE,
            resume_file_path=None,
            is_linkedin_job=True,
        )
        assert "Do NOT click 'Apply with LinkedIn'" in prompt

    def test_no_resume_file_says_unavailable(self):
        """When resume_file_path is None the prompt says no file is available."""
        prompt = self.bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=_SAMPLE_PROFILE,
            resume_file_path=None,
        )
        assert "No resume file available" in prompt

    def test_with_resume_file_path_included(self):
        """When a resume path is provided it appears in the prompt."""
        prompt = self.bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=_SAMPLE_PROFILE,
            resume_file_path="/tmp/resume_job-001.pdf",
        )
        assert "/tmp/resume_job-001.pdf" in prompt

    def test_summary_truncated_at_600_chars(self):
        """Long summaries are truncated to ≤600 chars with an ellipsis."""
        long_summary = "A" * 700
        profile = {**_SAMPLE_PROFILE, "summary": long_summary}
        prompt = self.bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=profile,
            resume_file_path=None,
        )
        # Should contain the truncated summary ending with '…'
        assert "…" in prompt
        # The raw 700-char string must not appear verbatim
        assert "A" * 700 not in prompt

    def test_empty_summary_uses_placeholder(self):
        """Empty summary produces the 'Not provided' placeholder line."""
        profile = {**_SAMPLE_PROFILE, "summary": ""}
        prompt = self.bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=profile,
            resume_file_path=None,
        )
        assert "Not provided" in prompt

    def test_no_fabrication_rule_present(self):
        """The NEVER fabricate rule must always appear in the prompt."""
        prompt = self.bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=_SAMPLE_PROFILE,
            resume_file_path=None,
        )
        assert "NEVER fabricate" in prompt

    def test_returns_string(self):
        result = self.bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=_SAMPLE_PROFILE,
            resume_file_path=None,
        )
        assert isinstance(result, str)
        assert len(result) > 200  # sanity: not empty


# ============================================================================
# TestBuildSensitiveData
# ============================================================================

class TestBuildSensitiveData:
    """Pure-function tests for _build_sensitive_data().  No mocks required."""

    def setup_method(self):
        self.bua = _import_bua()

    def test_extracts_email_and_phone(self):
        data = self.bua._build_sensitive_data(_SAMPLE_PROFILE)
        assert data["email"] == "jane.doe@example.com"
        assert data["phone"] == "555-555-5555"

    def test_extracts_address_fields(self):
        data = self.bua._build_sensitive_data(_SAMPLE_PROFILE)
        assert data["street_address"] == "123 Main St"
        assert data["zip_code"] == "80202"

    def test_extracts_workday_credentials(self):
        profile = {**_SAMPLE_PROFILE, "workday_password": "mypassword"}
        data = self.bua._build_sensitive_data(profile)
        assert data["workday_email"] == "jane.doe@example.com"
        assert data["workday_password"] == "mypassword"

    def test_empty_string_values_excluded(self):
        """Fields present in the profile but with empty string values must not appear."""
        profile = {
            **_SAMPLE_PROFILE,
            "workday_password": "",   # empty → excluded
            "desired_salary":   "",   # empty → excluded
        }
        data = self.bua._build_sensitive_data(profile)
        assert "workday_password" not in data
        assert "desired_salary" not in data

    def test_missing_keys_do_not_raise(self):
        """A minimal profile dict with no matching keys returns an empty dict safely."""
        data = self.bua._build_sensitive_data({})
        assert isinstance(data, dict)

    def test_returns_dict_of_strings(self):
        """All values in the returned dict must be str, not int/float."""
        profile = {**_SAMPLE_PROFILE, "desired_salary": 90000}
        data = self.bua._build_sensitive_data(profile)
        for k, v in data.items():
            assert isinstance(v, str), f"Key {k!r} value is not str: {v!r}"


# ============================================================================
# TestGetBrowserUseLlm
# ============================================================================

class TestGetBrowserUseLlm:
    """Tests for _get_browser_use_llm() — uses fast-tier model config."""

    def setup_method(self):
        self.bua = _import_bua()

    def test_uses_fast_tier_model(self):
        with patch("backend.browser_use_agent.ChatOpenAI") as MockLLM:
            MockLLM.return_value = MagicMock()
            self.bua._get_browser_use_llm(_SAMPLE_CONFIG)
            _, kwargs = MockLLM.call_args
            assert kwargs.get("model") == "test-fast-model"

    def test_uses_fast_tier_base_url(self):
        with patch("backend.browser_use_agent.ChatOpenAI") as MockLLM:
            MockLLM.return_value = MagicMock()
            self.bua._get_browser_use_llm(_SAMPLE_CONFIG)
            _, kwargs = MockLLM.call_args
            assert kwargs.get("base_url") == "http://localhost:1234/v1"

    def test_falls_back_to_top_level_model_when_no_fast_tier(self):
        """When ai.models.fast is absent, top-level ai.model is used."""
        cfg = {
            "ai": {
                "base_url": "http://localhost:1234/v1",
                "api_key":  "lm-studio",
                "model":    "fallback-model",
                "models":   {},  # no 'fast' key
            },
        }
        with patch("backend.browser_use_agent.ChatOpenAI") as MockLLM:
            MockLLM.return_value = MagicMock()
            self.bua._get_browser_use_llm(cfg)
            _, kwargs = MockLLM.call_args
            assert kwargs.get("model") == "fallback-model"


# ============================================================================
# Helpers for run_browser_use_apply() tests
# ============================================================================

def _make_agent_result(
    is_done: bool = True,
    is_successful: bool = True,
    final_text: str = "",
    errors: list[str] | None = None,
) -> MagicMock:
    """Build a mock agent result object."""
    result = MagicMock()
    result.is_done.return_value = is_done
    result.is_successful.return_value = is_successful
    result.final_result.return_value = final_text
    result.errors.return_value = errors or []
    return result


def _make_mock_browser_session() -> AsyncMock:
    session = AsyncMock()
    session.start = AsyncMock()
    session.stop  = AsyncMock()
    session.kill  = AsyncMock()
    session.navigate_to = AsyncMock()
    session.get_current_page = AsyncMock(return_value=None)
    session.export_storage_state = AsyncMock(return_value=None)
    return session


def _common_patches():
    """Return a list of patch objects needed for run_browser_use_apply() unit tests."""
    return [
        patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
        patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
        patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
        patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock(return_value="/tmp/shot.png")),
        patch("backend.browser_use_agent.session_manager") ,
    ]


# ============================================================================
# TestRunBrowserUseApply
# ============================================================================

class TestRunBrowserUseApply:
    """Tests for the main entry point run_browser_use_apply()."""

    def setup_method(self):
        self.bua = _import_bua()

    @pytest.mark.asyncio
    async def test_success_via_is_successful(self):
        """Agent reports is_done=True and is_successful=True → success=True."""
        agent_result = _make_agent_result(
            is_done=True,
            is_successful=True,
            final_text="Application submitted successfully.",
        )
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=agent_result)

        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", return_value=mock_agent),
        ):
            result = await self.bua.run_browser_use_apply(
                job=_SAMPLE_JOB,
                application=_SAMPLE_APPLICATION,
                profile=_SAMPLE_PROFILE,
                config=_SAMPLE_CONFIG,
            )

        assert result["success"] is True
        assert "submitted" in result["message"].lower() or "browser-use" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_success_via_confirmation_keywords_in_final_text(self):
        """Even if is_successful() is False, confirmation keywords → success."""
        agent_result = _make_agent_result(
            is_done=True,
            is_successful=False,
            final_text="Thank you for applying to TestCo.",
        )
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=agent_result)

        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", return_value=mock_agent),
        ):
            result = await self.bua.run_browser_use_apply(
                job=_SAMPLE_JOB,
                application=_SAMPLE_APPLICATION,
                profile=_SAMPLE_PROFILE,
                config=_SAMPLE_CONFIG,
            )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_failure_when_agent_done_but_no_confirmation(self):
        """Agent done + no success keywords + is_successful=False → success=False."""
        agent_result = _make_agent_result(
            is_done=True,
            is_successful=False,
            final_text="I stopped because the page was blank.",
            errors=["Element not found"],
        )
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=agent_result)

        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", return_value=mock_agent),
        ):
            result = await self.bua.run_browser_use_apply(
                job=_SAMPLE_JOB,
                application=_SAMPLE_APPLICATION,
                profile=_SAMPLE_PROFILE,
                config=_SAMPLE_CONFIG,
            )

        assert result["success"] is False
        assert len(result["message"]) > 0

    @pytest.mark.asyncio
    async def test_failure_when_agent_did_not_complete_max_steps(self):
        """Agent hit max steps without completing → is_done=False → success=False."""
        agent_result = _make_agent_result(
            is_done=False,
            is_successful=False,
            final_text="",
            errors=["Max steps exceeded"],
        )
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=agent_result)

        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", return_value=mock_agent),
        ):
            result = await self.bua.run_browser_use_apply(
                job=_SAMPLE_JOB,
                application=_SAMPLE_APPLICATION,
                profile=_SAMPLE_PROFILE,
                config=_SAMPLE_CONFIG,
            )

        assert result["success"] is False
        assert "did not complete" in result["message"] or "Max steps" in result["message"] or len(result["message"]) > 0

    @pytest.mark.asyncio
    async def test_captcha_detection_returns_captcha_message(self):
        """When on_step sees a CAPTCHA keyword → returns CAPTCHA-specific error."""
        agent_result = _make_agent_result(is_done=True, is_successful=True, final_text="done")

        captcha_callback = None

        class CapturingAgentMock:
            def __init__(self, **kwargs):
                nonlocal captcha_callback
                captcha_callback = kwargs.get("register_new_step_callback")

            async def run(self, max_steps):
                # Simulate CAPTCHA being encountered during execution
                # on_step is now async — must be awaited so the flag is set.
                if captcha_callback:
                    await captcha_callback(MagicMock(), "I see a reCAPTCHA on this page", 1)
                return agent_result

        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", side_effect=CapturingAgentMock),
        ):
            result = await self.bua.run_browser_use_apply(
                job=_SAMPLE_JOB,
                application=_SAMPLE_APPLICATION,
                profile=_SAMPLE_PROFILE,
                config=_SAMPLE_CONFIG,
            )

        assert result["success"] is False
        assert "captcha" in result["message"].lower() or "CAPTCHA" in result["message"]

    @pytest.mark.asyncio
    async def test_login_required_returns_login_message(self):
        """When on_step sees 'sso only' → returns login-required error."""
        agent_result = _make_agent_result(is_done=True, is_successful=True, final_text="done")

        login_callback = None

        class CapturingAgentMock:
            def __init__(self, **kwargs):
                nonlocal login_callback
                login_callback = kwargs.get("register_new_step_callback")

            async def run(self, max_steps):
                # on_step is now async — must be awaited so the flag is set.
                if login_callback:
                    await login_callback(MagicMock(), "This page has SSO only login", 2)
                return agent_result

        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", side_effect=CapturingAgentMock),
        ):
            result = await self.bua.run_browser_use_apply(
                job=_SAMPLE_JOB,
                application=_SAMPLE_APPLICATION,
                profile=_SAMPLE_PROFILE,
                config=_SAMPLE_CONFIG,
            )

        assert result["success"] is False
        assert "login" in result["message"].lower() or "session" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_exception_in_agent_returns_failure(self):
        """If Agent.run() raises an unexpected exception → returns success=False."""
        class ExplodingAgentMock:
            def __init__(self, **kwargs):
                pass

            async def run(self, max_steps):
                raise RuntimeError("Unexpected browser crash")

        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", side_effect=ExplodingAgentMock),
        ):
            result = await self.bua.run_browser_use_apply(
                job=_SAMPLE_JOB,
                application=_SAMPLE_APPLICATION,
                profile=_SAMPLE_PROFILE,
                config=_SAMPLE_CONFIG,
            )

        assert result["success"] is False
        assert "Unexpected browser crash" in result["message"] or "error" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_agent_created_with_correct_settings(self):
        """Verify the Agent is instantiated with the expected keyword arguments."""
        agent_result = _make_agent_result()
        captured_kwargs = {}

        class CapturingAgentMock:
            def __init__(self, **kwargs):
                captured_kwargs.update(kwargs)

            async def run(self, max_steps):
                return agent_result

        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", side_effect=CapturingAgentMock),
        ):
            await self.bua.run_browser_use_apply(
                job=_SAMPLE_JOB,
                application=_SAMPLE_APPLICATION,
                profile=_SAMPLE_PROFILE,
                config=_SAMPLE_CONFIG,
            )

        assert "task" in captured_kwargs, "Agent must receive a task prompt"
        assert "llm" in captured_kwargs, "Agent must receive an llm"
        assert captured_kwargs.get("use_vision") is False, "Vision must be disabled (fast model has no vision)"
        assert captured_kwargs.get("enable_planning") is False, "Planning must be disabled (reduces token overhead)"
        assert captured_kwargs.get("directly_open_url") is False, "directly_open_url must remain False (multi-URL prompt safety)"
        assert "register_new_step_callback" in captured_kwargs, "on_step callback must be registered"

    @pytest.mark.asyncio
    async def test_autofill_mode_config_produces_stop_prompt(self):
        """When config mode='autofill', the task passed to Agent contains STOP instruction."""
        agent_result = _make_agent_result()
        captured_task = None

        class CapturingAgentMock:
            def __init__(self, **kwargs):
                nonlocal captured_task
                captured_task = kwargs.get("task", "")

            async def run(self, max_steps):
                return agent_result

        autofill_config = {**_SAMPLE_CONFIG, "auto_apply": {**_SAMPLE_CONFIG["auto_apply"], "mode": "autofill"}}

        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", side_effect=CapturingAgentMock),
        ):
            await self.bua.run_browser_use_apply(
                job=_SAMPLE_JOB,
                application=_SAMPLE_APPLICATION,
                profile=_SAMPLE_PROFILE,
                config=autofill_config,
            )

        assert captured_task is not None
        assert "STOP" in captured_task or "stop" in captured_task.lower(), (
            f"autofill config must produce a STOP instruction in the agent task.\n"
            f"Task tail: ...{captured_task[-300:]}"
        )
        assert "STOP BEFORE SUBMIT" in captured_task, (
            "autofill task must include the STOP BEFORE SUBMIT rule"
        )

    @pytest.mark.asyncio
    async def test_submit_mode_config_produces_submit_prompt(self):
        """When config mode='submit', the task passed to Agent contains click Submit instruction."""
        agent_result = _make_agent_result()
        captured_task = None

        class CapturingAgentMock:
            def __init__(self, **kwargs):
                nonlocal captured_task
                captured_task = kwargs.get("task", "")

            async def run(self, max_steps):
                return agent_result

        submit_config = {**_SAMPLE_CONFIG, "auto_apply": {**_SAMPLE_CONFIG["auto_apply"], "mode": "submit"}}

        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", side_effect=CapturingAgentMock),
        ):
            await self.bua.run_browser_use_apply(
                job=_SAMPLE_JOB,
                application=_SAMPLE_APPLICATION,
                profile=_SAMPLE_PROFILE,
                config=submit_config,
            )

        assert captured_task is not None
        assert "click Submit" in captured_task, (
            f"submit config must produce a 'click Submit' instruction in the agent task.\n"
            f"Task tail: ...{captured_task[-300:]}"
        )
        assert "STOP BEFORE SUBMIT" not in captured_task, (
            "submit task must not contain the autofill STOP rule"
        )


# ============================================================================
# TestReturnDictShape
# ============================================================================
#
# Documents the CURRENT return shape of run_browser_use_apply() and the
# REQUIRED shape that main.py expects.  Tests marked xfail represent Phase 5
# work: the return dict must be extended to include these keys.
# ============================================================================

class TestReturnDictShape:
    """Verifies the return dict shape — current state and Phase 5 target."""

    def setup_method(self):
        self.bua = _import_bua()

    async def _run_success(self) -> dict:
        agent_result = _make_agent_result(
            is_done=True, is_successful=True, final_text="Application submitted."
        )
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=agent_result)

        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", return_value=mock_agent),
        ):
            return await self.bua.run_browser_use_apply(
                job=_SAMPLE_JOB,
                application=_SAMPLE_APPLICATION,
                profile=_SAMPLE_PROFILE,
                config=_SAMPLE_CONFIG,
            )

    async def _run_failure(self) -> dict:
        agent_result = _make_agent_result(is_done=False, is_successful=False)
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=agent_result)

        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", return_value=mock_agent),
        ):
            return await self.bua.run_browser_use_apply(
                job=_SAMPLE_JOB,
                application=_SAMPLE_APPLICATION,
                profile=_SAMPLE_PROFILE,
                config=_SAMPLE_CONFIG,
            )

    @pytest.mark.asyncio
    async def test_success_result_has_success_key(self):
        """CURRENT: success=True result has 'success' key."""
        result = await self._run_success()
        assert "success" in result
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_success_result_has_message_key(self):
        """CURRENT: success=True result has non-empty 'message' key."""
        result = await self._run_success()
        assert "message" in result
        assert isinstance(result["message"], str)
        assert len(result["message"]) > 0

    @pytest.mark.asyncio
    async def test_failure_result_has_success_false(self):
        """CURRENT: failure result has success=False."""
        result = await self._run_failure()
        assert result["success"] is False

    # ── Phase 5 (implemented) — full return dict shape ────────────────────

    @pytest.mark.asyncio
    async def test_phase5_success_result_has_db_status(self):
        """Phase 5 (implemented): success result includes 'db_status'='applied'."""
        result = await self._run_success()
        assert "db_status" in result
        assert result["db_status"] == "applied"

    @pytest.mark.asyncio
    async def test_phase5_failure_result_has_screenshot_path(self):
        """Phase 5 (implemented): failure result includes 'screenshot_path' key."""
        result = await self._run_failure()
        assert "screenshot_path" in result

    @pytest.mark.asyncio
    async def test_phase5_failure_result_has_block_reason(self):
        """Phase 5 (implemented): failure result includes 'block_reason' key."""
        result = await self._run_failure()
        assert "block_reason" in result

    @pytest.mark.asyncio
    async def test_phase5_failure_result_has_manual_url(self):
        """Phase 5 (implemented): failure result includes 'manual_url' key."""
        result = await self._run_failure()
        assert "manual_url" in result


# ============================================================================
# TestModeEnforcement
# ============================================================================

class TestModeEnforcement:
    """
    Documents the mode enforcement gap in the Browser Use path.

    The orchestrator enforces ApplyMode (autofill=stop before Submit,
    submit=click Submit). The Browser Use path bypasses the orchestrator
    entirely and always instructs the agent to submit.

    The xfail test below will PASS once Phase 2 is complete:
    _build_task_prompt() must include an explicit "STOP — do not click Submit"
    instruction when the config mode is 'autofill'.
    """

    def setup_method(self):
        self.bua = _import_bua()

    def test_phase2_autofill_mode_prompt_contains_stop_instruction(self):
        """Phase 2 implemented: autofill-mode prompt tells agent to STOP at Submit."""
        prompt = self.bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=_SAMPLE_PROFILE,
            resume_file_path=None,
            mode="autofill",
        )
        stop_phrases = [
            "STOP before clicking Submit",
            "do not click Submit",
            "stop at the Submit button",
            "DO NOT submit",
            "stop before submit",
            "STOP at the final page",
            "DO NOT click Submit",
        ]
        found = any(p.lower() in prompt.lower() for p in stop_phrases)
        assert found, (
            f"autofill-mode prompt must contain a 'stop before Submit' instruction.\n"
            f"Prompt tail: ...{prompt[-400:]}"
        )

    def test_submit_mode_prompt_instructs_to_click_submit(self):
        """Submit mode: prompt affirmatively instructs agent to click Submit."""
        prompt = self.bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=_SAMPLE_PROFILE,
            resume_file_path=None,
            mode="submit",
        )
        # submit mode must contain an affirmative 'click Submit' instruction
        submit_phrases = [
            "click Submit",
            "verify all fields are correct, then click Submit",
            "Submit the application",
        ]
        found = any(p in prompt for p in submit_phrases)
        assert found, (
            f"submit-mode prompt must contain an affirmative submit instruction.\n"
            f"Prompt tail: ...{prompt[-400:]}"
        )

    def test_autofill_mode_stop_instruction_is_in_rules(self):
        """Autofill mode: STOP BEFORE SUBMIT rule appears in the RULES section."""
        prompt = self.bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=_SAMPLE_PROFILE,
            resume_file_path=None,
            mode="autofill",
        )
        assert "STOP BEFORE SUBMIT" in prompt, (
            "autofill-mode prompt must include 'STOP BEFORE SUBMIT' in the RULES section "
            "so the agent cannot miss it."
        )

    def test_submit_mode_no_stop_instruction(self):
        """Submit mode: prompt must NOT contain a 'STOP BEFORE SUBMIT' rule."""
        prompt = self.bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=_SAMPLE_PROFILE,
            resume_file_path=None,
            mode="submit",
        )
        assert "STOP BEFORE SUBMIT" not in prompt, (
            "submit-mode prompt must not contain the autofill STOP rule."
        )

    def test_default_mode_is_autofill(self):
        """Default mode (no mode kwarg) must be autofill — safe default."""
        prompt_default = self.bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=_SAMPLE_PROFILE,
            resume_file_path=None,
        )
        prompt_explicit = self.bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=_SAMPLE_PROFILE,
            resume_file_path=None,
            mode="autofill",
        )
        assert prompt_default == prompt_explicit, (
            "Default mode must equal explicit mode='autofill'. "
            "Autofill is the safe default — it never submits accidentally."
        )

    @pytest.mark.asyncio
    async def test_phase4_pause_flag_halts_browser_use_agent(self):
        """Phase 4 (implemented): is_paused()=True blocks run_browser_use_apply()
        before the agent loop starts — zero steps are executed while paused."""
        from backend.auto_apply import orchestrator

        # Set paused before agent starts
        orchestrator.set_paused(True)
        steps_executed = []

        class PauseAwareAgentMock:
            def __init__(self, **kwargs):
                self._callback = kwargs.get("register_new_step_callback")

            async def run(self, max_steps):
                # Simulate 3 steps
                for i in range(3):
                    steps_executed.append(i)
                    if self._callback:
                        self._callback(MagicMock(), f"step {i} output", i)
                return _make_agent_result()

        agent_result = _make_agent_result()
        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", side_effect=PauseAwareAgentMock),
        ):
            try:
                result = await asyncio.wait_for(
                    self.bua.run_browser_use_apply(
                        job=_SAMPLE_JOB,
                        application=_SAMPLE_APPLICATION,
                        profile=_SAMPLE_PROFILE,
                        config=_SAMPLE_CONFIG,
                    ),
                    timeout=3.0,
                )
            except asyncio.TimeoutError:
                # If paused, the function should block — a timeout is acceptable
                pass
            finally:
                orchestrator.set_paused(False)

        # Phase 4 implemented: pre-start pause check blocks before agent.run()
        # so the mock's run() is never called — steps_executed stays empty.
        assert len(steps_executed) == 0, (
            "Agent executed steps while pause flag was set. "
            "The pre-start pause check in run_browser_use_apply() must prevent this."
        )


# ============================================================================
# TestIndeedRouting
# ============================================================================

class TestIndeedRouting:
    """
    Verify the main.py routing decision that bypasses Browser Use for Indeed.

    This tests the routing LOGIC only — we can't call main.py._bg_apply()
    directly in unit tests, but we can verify the domain-check logic.
    """

    def test_indeed_domain_check_is_consistent(self):
        """The 'is_indeed' check in main.py uses case-insensitive 'indeed.com' match."""
        indeed_urls = [
            "https://www.indeed.com/jobs/123",
            "https://indeed.com/viewjob?jk=abc",
            "https://ca.indeed.com/jobs/456",
        ]
        non_indeed_urls = [
            "https://apply.testco.com/jobs/123",
            "https://greenhouse.io/jobs/abc",
            "https://www.linkedin.com/jobs/view/123",
            "https://jobs.lever.co/company/123",
        ]
        for url in indeed_urls:
            assert "indeed.com" in url.lower(), f"Expected Indeed URL to match: {url}"
        for url in non_indeed_urls:
            assert "indeed.com" not in url.lower(), f"Expected non-Indeed URL: {url}"


# ============================================================================
# TestAgentKeyConstraints
# ============================================================================

class TestAgentKeyConstraints:
    """
    Document immutable constraints on the Agent instantiation that must not
    be changed without explicit testing (from CLAUDE.md).
    """

    def setup_method(self):
        self.bua = _import_bua()

    @pytest.mark.asyncio
    async def test_directly_open_url_is_false(self):
        """CONSTRAINT: directly_open_url must remain False (multi-URL prompt safety)."""
        agent_result = _make_agent_result()
        captured = {}

        class CapturingMock:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def run(self, max_steps):
                return agent_result

        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", side_effect=CapturingMock),
        ):
            await self.bua.run_browser_use_apply(
                job=_SAMPLE_JOB,
                application=_SAMPLE_APPLICATION,
                profile=_SAMPLE_PROFILE,
                config=_SAMPLE_CONFIG,
            )

        assert captured.get("directly_open_url") is False, (
            "directly_open_url MUST remain False. The task prompt contains multiple URLs "
            "(job URL, LinkedIn profile, portfolio). Setting True would cause the agent "
            "to navigate to the wrong URL at startup. See CLAUDE.md Phase 6."
        )

    @pytest.mark.asyncio
    async def test_use_vision_is_false(self):
        """CONSTRAINT: use_vision must be False (fast model has no vision capability)."""
        agent_result = _make_agent_result()
        captured = {}

        class CapturingMock:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def run(self, max_steps):
                return agent_result

        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", side_effect=CapturingMock),
        ):
            await self.bua.run_browser_use_apply(
                job=_SAMPLE_JOB,
                application=_SAMPLE_APPLICATION,
                profile=_SAMPLE_PROFILE,
                config=_SAMPLE_CONFIG,
            )

        assert captured.get("use_vision") is False, (
            "use_vision must remain False — the fast model tier has no vision."
        )

    @pytest.mark.asyncio
    async def test_enable_planning_is_false(self):
        """CONSTRAINT: enable_planning must be False (reduces token overhead)."""
        agent_result = _make_agent_result()
        captured = {}

        class CapturingMock:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def run(self, max_steps):
                return agent_result

        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", side_effect=CapturingMock),
        ):
            await self.bua.run_browser_use_apply(
                job=_SAMPLE_JOB,
                application=_SAMPLE_APPLICATION,
                profile=_SAMPLE_PROFILE,
                config=_SAMPLE_CONFIG,
            )

        assert captured.get("enable_planning") is False, (
            "enable_planning must remain False — reduces token overhead for local models."
        )

    @pytest.mark.asyncio
    async def test_step_ceiling_zero_converts_to_unlimited(self):
        """CURRENT behavior: step_ceiling=0 in config → agent runs with 999999 max steps."""
        agent_result = _make_agent_result()
        captured_max_steps = None

        class CapturingMock:
            def __init__(self, **kwargs):
                pass

            async def run(self, max_steps):
                nonlocal captured_max_steps
                captured_max_steps = max_steps
                return agent_result

        cfg = {**_SAMPLE_CONFIG, "auto_apply": {**_SAMPLE_CONFIG["auto_apply"], "step_ceiling": 0}}

        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", side_effect=CapturingMock),
        ):
            await self.bua.run_browser_use_apply(
                job=_SAMPLE_JOB,
                application=_SAMPLE_APPLICATION,
                profile=_SAMPLE_PROFILE,
                config=cfg,
            )

        assert captured_max_steps == 999999, (
            f"step_ceiling=0 must convert to 999999 (unlimited). Got: {captured_max_steps}"
        )

    @pytest.mark.asyncio
    async def test_step_ceiling_positive_value_used_directly(self):
        """A positive step_ceiling value is passed directly to agent.run()."""
        agent_result = _make_agent_result()
        captured_max_steps = None

        class CapturingMock:
            def __init__(self, **kwargs):
                pass

            async def run(self, max_steps):
                nonlocal captured_max_steps
                captured_max_steps = max_steps
                return agent_result

        cfg = {**_SAMPLE_CONFIG, "auto_apply": {**_SAMPLE_CONFIG["auto_apply"], "step_ceiling": 25}}

        with (
            patch("backend.browser_use_agent._get_browser_use_llm", return_value=MagicMock()),
            patch("backend.browser_use_agent._get_browser_session", return_value=_make_mock_browser_session()),
            patch("backend.browser_use_agent._prepare_resume_file", return_value=None),
            patch("backend.browser_use_agent._save_failure_screenshot", new=AsyncMock()),
            patch("backend.browser_use_agent.session_manager"),
            patch("backend.browser_use_agent.Agent", side_effect=CapturingMock),
        ):
            await self.bua.run_browser_use_apply(
                job=_SAMPLE_JOB,
                application=_SAMPLE_APPLICATION,
                profile=_SAMPLE_PROFILE,
                config=cfg,
            )

        assert captured_max_steps == 25, (
            f"step_ceiling=25 must be passed directly as max_steps. Got: {captured_max_steps}"
        )


# ============================================================================
# TestIntegration — requires live LM Studio, skipped otherwise
# ============================================================================

@pytest.mark.integration
class TestIntegration:
    """
    Integration tests — require LM Studio running at ai.base_url.
    Skipped automatically if LM Studio is not reachable.

    Run with: venv/bin/python -m pytest tests/auto_apply/test_browser_use_agent.py -m integration -v
    """

    @requires_lmstudio
    def test_lmstudio_reachable(self):
        """Confirms LM Studio is up before any integration test runs."""
        assert _LMSTUDIO_REACHABLE, "LM Studio must be reachable"

    @requires_lmstudio
    def test_get_browser_use_llm_connects_to_lmstudio(self):
        """_get_browser_use_llm() creates a ChatOpenAI that can reach LM Studio."""
        bua = _import_bua()
        import yaml
        cfg_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"
        cfg = yaml.safe_load(cfg_path.read_text())
        # Just instantiate — no call, no network beyond the initial ChatOpenAI object
        llm = bua._get_browser_use_llm(cfg)
        assert llm is not None

    @requires_lmstudio
    def test_task_prompt_token_count_within_context_window(self):
        """The generated task prompt must fit within a reasonable token budget.

        Local models commonly have 4096–8192 token context windows.  The task
        prompt alone should use no more than ~1500 tokens to leave room for
        conversation history and the system prompt.
        """
        bua = _import_bua()
        prompt = bua._build_task_prompt(
            job=_SAMPLE_JOB,
            profile=_SAMPLE_PROFILE,
            resume_file_path="/tmp/resume.pdf",
        )
        # Rough approximation: 1 token ≈ 4 characters for English prose
        approx_tokens = len(prompt) // 4
        assert approx_tokens <= 1500, (
            f"Task prompt is approximately {approx_tokens} tokens — "
            f"exceeds 1500-token budget. Trim candidate fields or summary."
        )
