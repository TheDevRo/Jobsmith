"""
tests/auto_apply/test_profile_mapping.py

Unit tests for UserProfile loading, serialisation, and field-mapping logic.
No browser, no LLM calls.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.auto_apply.models import (
    ApplyMode,
    ApplyResult,
    ApplyStatus,
    Education,
    FieldDescriptor,
    FieldValue,
    JobApplicationRequest,
    UserProfile,
    WorkExperience,
)
from backend.auto_apply.answer_bank import AnswerBank

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# UserProfile
# ---------------------------------------------------------------------------

class TestUserProfile:
    def test_load_from_json(self):
        raw = json.loads((FIXTURES / "sample_profile.json").read_text())
        profile = UserProfile(**raw)
        assert profile.full_name == "Jane Doe"
        assert profile.email == "jane.doe@example.com"
        assert len(profile.skills) > 0
        assert len(profile.experience) == 2
        assert len(profile.education) == 1

    def test_from_config(self):
        config = {
            "profile": {
                "full_name": "John Smith",
                "email": "john@smith.com",
                "phone": "555-000-1111",
                "work_authorization": "Yes",
                "sponsorship_required": "No",
                "experience": [
                    {"title": "SWE", "company": "ACME", "start_date": "2020-01-01", "end_date": "Present"}
                ],
                "education": [
                    {"degree": "BS CS", "school": "MIT", "year": "2019"}
                ],
            }
        }
        profile = UserProfile.from_config(config)
        assert profile.full_name == "John Smith"
        assert len(profile.experience) == 1
        assert profile.experience[0].title == "SWE"

    def test_from_config_missing_profile(self):
        """Should not raise when profile key is absent."""
        profile = UserProfile.from_config({})
        assert profile.full_name == ""

    def test_to_text_contains_key_fields(self):
        raw = json.loads((FIXTURES / "sample_profile.json").read_text())
        profile = UserProfile(**raw)
        text = profile.to_text()
        assert "Jane Doe" in text
        assert "jane.doe@example.com" in text
        assert "Python" in text
        assert "Acme Corp" in text

    def test_years_of_experience(self):
        profile = UserProfile(
            experience=[
                WorkExperience(
                    title="SWE", company="A",
                    start_date="2020-01-01", end_date="Present"
                ),
                WorkExperience(
                    title="Intern", company="B",
                    start_date="2019-06-01", end_date="2019-12-31"
                ),
            ]
        )
        years = profile.years_of_experience()
        assert years >= 5   # 5+ years total

    def test_work_experience_defaults(self):
        exp = WorkExperience(title="Dev", company="Co", start_date="2022-01-01")
        assert exp.end_date == "Present"
        assert exp.bullets == []

    def test_education_model(self):
        edu = Education(degree="BS CS", school="MIT", year="2019")
        assert edu.degree == "BS CS"


# ---------------------------------------------------------------------------
# FieldDescriptor
# ---------------------------------------------------------------------------

class TestFieldDescriptor:
    def test_load_from_fixture(self):
        raw = json.loads((FIXTURES / "sample_field_descriptors.json").read_text())
        fields = [FieldDescriptor(**f) for f in raw]
        assert len(fields) == 10
        first = fields[0]
        assert first.field_id == "field-0"
        assert first.label == "First Name"
        assert first.required is True

    def test_defaults(self):
        f = FieldDescriptor(field_id="x")
        assert f.label == ""
        assert f.field_type == "text"
        assert f.options is None
        assert f.required is False


# ---------------------------------------------------------------------------
# FieldValue
# ---------------------------------------------------------------------------

class TestFieldValue:
    def test_defaults(self):
        fv = FieldValue(field_id="f-0", value="hello")
        assert fv.action == "fill"
        assert fv.confidence == 1.0
        assert fv.source == "profile"

    def test_skip_action(self):
        fv = FieldValue(field_id="f-0", value="", action="skip", confidence=0.0, source="skip")
        assert fv.action == "skip"


# ---------------------------------------------------------------------------
# ApplyResult
# ---------------------------------------------------------------------------

class TestApplyResult:
    def test_to_legacy_dict_success(self):
        result = ApplyResult(
            success=True,
            status=ApplyStatus.SUBMITTED,
            message="Application submitted",
            fields_filled=10,
        )
        legacy = result.to_legacy_dict()
        assert legacy["success"] is True
        assert legacy["message"] == "Application submitted"
        assert legacy["manual_url"] is None
        assert "block_reason" in legacy

    def test_to_legacy_dict_failure(self):
        result = ApplyResult(
            success=False,
            status=ApplyStatus.NEEDS_REVIEW,
            message="Human review required",
            manual_url="https://example.com/apply",
        )
        legacy = result.to_legacy_dict()
        assert legacy["success"] is False
        assert legacy["manual_url"] == "https://example.com/apply"
        assert legacy["block_reason"] == "needs_review"


# ---------------------------------------------------------------------------
# JobApplicationRequest
# ---------------------------------------------------------------------------

class TestJobApplicationRequest:
    def test_basic(self):
        req = JobApplicationRequest(
            job_id="job-1",
            title="Engineer",
            company="ACME",
            url="https://boards.greenhouse.io/acme/jobs/1",
        )
        assert req.job_id == "job-1"
        assert req.resume_path is None

    def test_with_resume(self):
        req = JobApplicationRequest(
            job_id="job-2",
            title="Analyst",
            company="Beta",
            url="https://jobs.lever.co/beta/apply",
            resume_path="/tmp/resume.pdf",
        )
        assert req.resume_path == "/tmp/resume.pdf"


# ---------------------------------------------------------------------------
# ApplyMode
# ---------------------------------------------------------------------------

class TestApplyMode:
    def test_enum_values(self):
        assert ApplyMode.AUTOFILL == "autofill"
        assert ApplyMode.SUBMIT   == "submit"

    def test_from_string(self):
        assert ApplyMode("autofill") == ApplyMode.AUTOFILL
        assert ApplyMode("submit")   == ApplyMode.SUBMIT


# ---------------------------------------------------------------------------
# AnswerBank
# ---------------------------------------------------------------------------

class TestAnswerBank:
    def test_seed_on_new_file(self, tmp_path):
        bank = AnswerBank(path=tmp_path / "bank.json")
        # All seed keys should exist (as placeholders)
        assert "tell_us_about_yourself" in bank.all_snippets()

    def test_set_and_get(self, tmp_path):
        bank = AnswerBank(path=tmp_path / "bank.json")
        bank.set("custom_key", "My custom answer")
        assert bank.get("custom_key") == "My custom answer"

    def test_placeholder_returns_none(self, tmp_path):
        bank = AnswerBank(path=tmp_path / "bank.json")
        # Default seed values are placeholders — get() should return None
        assert bank.get("tell_us_about_yourself") is None

    def test_delete(self, tmp_path):
        bank = AnswerBank(path=tmp_path / "bank.json")
        bank.set("foo", "bar")
        assert bank.delete("foo") is True
        assert bank.get("foo") is None
        assert bank.delete("foo") is False   # already gone

    def test_persistence(self, tmp_path):
        path = tmp_path / "bank.json"
        bank1 = AnswerBank(path=path)
        bank1.set("greeting", "Hello world")

        bank2 = AnswerBank(path=path)
        assert bank2.get("greeting") == "Hello world"

    def test_find_best_match_about_yourself(self, tmp_path):
        bank = AnswerBank(path=tmp_path / "bank.json")
        bank.set("tell_us_about_yourself", "I am a software engineer.")
        match = bank.find_best_match("Please tell us about yourself")
        assert match == "I am a software engineer."

    def test_find_best_match_no_match(self, tmp_path):
        bank = AnswerBank(path=tmp_path / "bank.json")
        match = bank.find_best_match("How many fingers do you have?")
        assert match is None
