"""
tests/auto_apply/test_field_matcher.py

Unit tests for the deterministic profile→field matcher that runs before the
answer bank and the LLM in map_fields_to_values.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.auto_apply.field_matcher import (
    best_option,
    match_profile_fields,
)
from backend.auto_apply.models import FieldDescriptor, UserProfile


def _profile(**overrides) -> UserProfile:
    base = dict(
        full_name="Jane Ann Doe",
        email="jane@example.com",
        phone="+1 555-123-4567",
        middle_name="Ann",
        location="Austin, TX",
        street_address="123 Main St",
        city="Austin",
        state="TX",
        zip_code="78701",
        desired_salary="$150,000",
        linkedin="https://linkedin.com/in/jane",
        github="https://github.com/jane",
        portfolio="https://jane.dev",
        skills=["Python", "React"],
        gender="Female",
        experience=[{"title": "Senior Engineer", "company": "Acme Corp",
                     "start_date": "2020-01", "end_date": "Present"},
                    {"title": "Engineer", "company": "Beta Inc",
                     "start_date": "2017-06", "end_date": "2019-12"}],
        education=[{"degree": "BS Computer Science", "school": "UT Austin", "year": "2015"},
                   {"degree": "MS Computer Science", "school": "Georgia Tech", "year": "2018"}],
    )
    base.update(overrides)
    return UserProfile(**base)


def _match(field: FieldDescriptor, profile: UserProfile | None = None):
    out = match_profile_fields(profile or _profile(), [field])
    return out.get(field.field_id)


def _fd(label: str, ftype: str = "text", **kw) -> FieldDescriptor:
    return FieldDescriptor(field_id="f", label=label, field_type=ftype, **kw)


# ---------------------------------------------------------------------------
# Contact / identity
# ---------------------------------------------------------------------------

class TestContactFields:
    def test_first_name(self):
        assert _match(_fd("First Name")).value == "Jane"

    def test_last_name(self):
        assert _match(_fd("Last Name*")).value == "Doe"

    def test_full_name(self):
        assert _match(_fd("Full Name")).value == "Jane Ann Doe"

    def test_full_name_not_username(self):
        assert _match(_fd("Username")) is None

    def test_middle_initial_shortens(self):
        assert _match(_fd("Middle Initial")).value == "A"

    def test_email(self):
        assert _match(_fd("Email Address", "email")).value == "jane@example.com"

    def test_phone(self):
        assert _match(_fd("Phone Number", "tel")).value == "+1 555-123-4567"

    def test_name_attr_matching_without_label(self):
        fv = _match(_fd("", name="candidate[city]"))
        assert fv.value == "Austin"

    def test_autocomplete_attr_wins(self):
        fv = _match(_fd("Ambiguous label", autocomplete="postal-code"))
        assert fv.value == "78701"


# ---------------------------------------------------------------------------
# Options resolution
# ---------------------------------------------------------------------------

class TestOptionResolution:
    def test_state_abbreviation_expands(self):
        fv = _match(_fd("State", "select", options=["Select...", "California", "Texas"]))
        assert fv.value == "Texas"
        assert fv.action == "select"

    def test_country_alias(self):
        fv = _match(_fd("Country", "select",
                        options=["United States of America", "Canada"]))
        assert fv.value == "United States of America"

    def test_degree_level_bucket(self):
        fv = _match(_fd("Highest Level of Education", "select",
                        options=["High School", "Bachelor's Degree", "Master's Degree"]))
        assert fv.value == "Bachelor's Degree"

    def test_unmatchable_option_falls_through(self):
        # Salary can't resolve to any of these options → leave for the LLM.
        assert _match(_fd("Desired Salary", "select", options=["Band A", "Band B"])) is None


# ---------------------------------------------------------------------------
# Screening questions
# ---------------------------------------------------------------------------

class TestScreening:
    def test_work_authorization_yes(self):
        fv = _match(_fd("Are you legally authorized to work in the United States?",
                        "radio", options=["Yes", "No"]))
        assert fv.value == "Yes"

    def test_sponsorship_no(self):
        fv = _match(_fd("Will you now or in the future require sponsorship?",
                        "radio", options=["Yes", "No"]))
        assert fv.value == "No"

    def test_group_context_does_not_outvote_label(self):
        # Sponsorship field inside a "Work Authorization" fieldset must still
        # answer the sponsorship question, not the work-auth one.
        fv = _match(_fd("Will you require sponsorship?", "radio",
                        options=["Yes", "No"], extra_context="Work Authorization"))
        assert fv.value == "No"

    def test_group_context_used_when_label_is_bare_option(self):
        fv = _match(_fd("Yes", "radio", options=["Yes", "No"],
                        extra_context="Do you require visa sponsorship?"))
        assert fv.value == "No"

    def test_over_18(self):
        fv = _match(_fd("Are you at least 18 years of age?", "radio", options=["Yes", "No"]))
        assert fv.value == "Yes"

    def test_skill_specific_years_left_to_llm(self):
        assert _match(_fd("Years of experience with Python", "number")) is None

    def test_total_years_experience(self):
        fv = _match(_fd("How many years of professional experience do you have?", "number"))
        assert fv is not None and fv.value.isdigit()


# ---------------------------------------------------------------------------
# EEO / demographics
# ---------------------------------------------------------------------------

class TestEEO:
    def test_gender_from_profile(self):
        fv = _match(_fd("Gender", "select", options=["Male", "Female", "Decline to self-identify"]))
        assert fv.value == "Female"

    def test_unset_eeo_declines(self):
        fv = _match(_fd("Disability Status", "select",
                        options=["Yes", "No", "I do not want to answer"]))
        assert fv.value == "I do not want to answer"

    def test_veteran_sentence_maps_to_option(self):
        fv = _match(
            _fd("Veteran Status", "select",
                options=["I am a protected veteran", "I am not a protected veteran"]),
            _profile(veteran_status="I am not a veteran"),
        )
        assert fv.value == "I am not a protected veteran"


# ---------------------------------------------------------------------------
# Repeating work-history / education sections
# ---------------------------------------------------------------------------

class TestRepeatingSections:
    def test_greenhouse_second_education_entry(self):
        fv = _match(_fd("School", name="job_application[educations_attributes][1][school_name]"))
        assert fv.value == "Georgia Tech"

    def test_greenhouse_first_employment_company(self):
        fv = _match(_fd("Company", name="job_application[employments_attributes][0][company_name]"))
        assert fv.value == "Acme Corp"

    def test_greenhouse_employment_start_date(self):
        fv = _match(_fd("Start Date", name="job_application[employments_attributes][0][start_date]"))
        assert fv.value == "2020-01"

    def test_workday_camelcase_second_entry(self):
        # Workday-style separator ids are 1-based: "-2--" → second entry.
        fv = _match(FieldDescriptor(field_id="workExperience-2--companyName",
                                    label="Company", field_type="text"))
        assert fv.value == "Beta Inc"

    def test_employment_start_not_availability(self):
        # An employment start date must never be filled with available_start.
        fv = _match(_fd("Start Date", name="job_application[employments_attributes][1][start_date]"))
        assert fv.value == "2017-06"

    def test_education_start_blocked_not_misfilled(self):
        # No education start year in the profile — must fall to the LLM,
        # not be answered with "Immediately".
        assert _match(_fd("Start Date", name="job_application[educations_attributes][0][start_date]")) is None

    def test_out_of_range_entry_falls_through(self):
        assert _match(_fd("School", name="job_application[educations_attributes][5][school_name]")) is None

    def test_plain_start_date_still_availability(self):
        assert _match(_fd("Earliest Start Date", "date")).value == "Immediately"


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

class TestSafety:
    def test_password_never_filled_when_unset(self):
        assert _match(_fd("Password", "password")) is None

    def test_password_filled_from_credentials(self):
        fv = _match(_fd("Password", "password"), _profile(ats_login_password="hunter2"))
        assert fv.value == "hunter2"

    def test_essay_questions_left_alone(self):
        assert _match(_fd("Why do you want to work here?", "textarea")) is None
        assert _match(_fd("What is your greatest strength?", "textarea")) is None

    def test_consent_checkbox_low_confidence(self):
        fv = _match(_fd("I agree to the terms and conditions", "checkbox"))
        assert fv.value == "Yes"
        assert fv.confidence < 0.60  # flagged for human review


# ---------------------------------------------------------------------------
# best_option scorer
# ---------------------------------------------------------------------------

class TestBestOption:
    def test_no_does_not_match_not_applicable(self):
        assert best_option("No", ["Yes", "Not applicable", "No"]) == "No"

    def test_placeholders_never_picked(self):
        assert best_option("Yes", ["Select one", "Yes", "No"]) == "Yes"

    def test_garbage_returns_none(self):
        assert best_option("garbage-value-xyz", ["Yes", "No"]) is None

    def test_exact_beats_fuzzy(self):
        assert best_option("2 weeks", ["Immediately", "2 weeks", "1 month"]) == "2 weeks"
