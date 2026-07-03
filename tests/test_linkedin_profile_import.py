"""
Tests for backend/linkedin_profile_import.py — section assembly, URL
resolution, and the LLM mapping glue. All offline; browser + LLM mocked.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend import linkedin_profile_import as li
from backend import resume_parser


# ---------------------------------------------------------------------------
# combine_sections / _clean_text / _profile_url_from
# ---------------------------------------------------------------------------
def test_clean_text_collapses_blank_runs():
    raw = "Line one\n\n\n\n   \nLine two   \n"
    assert li._clean_text(raw) == "Line one\n\nLine two"


def test_combine_sections_labels_and_caps():
    long_exp = "x" * 10_000
    text = li.combine_sections([
        ("PROFILE OVERVIEW", "Jane Doe\nSecurity Engineer"),
        ("ALL EXPERIENCE", long_exp),
        ("SKILLS", ""),  # empty sections dropped
    ])
    assert "=== PROFILE OVERVIEW ===" in text
    assert "Jane Doe" in text
    assert "=== SKILLS ===" not in text
    # Experience capped at its configured budget (6500)
    exp_part = text.split("=== ALL EXPERIENCE ===\n")[1]
    assert len(exp_part) == 6500


@pytest.mark.parametrize("url,expected", [
    ("https://www.linkedin.com/in/jane-doe-123/", "https://www.linkedin.com/in/jane-doe-123/"),
    ("https://www.linkedin.com/in/jane-doe-123/details/experience/", "https://www.linkedin.com/in/jane-doe-123/"),
    ("https://www.linkedin.com/in/me/", ""),  # unresolved redirect
    ("https://www.linkedin.com/feed/", ""),
])
def test_profile_url_from(url, expected):
    assert li._profile_url_from(url) == expected


# ---------------------------------------------------------------------------
# fetch_profile_text — session guard
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_profile_text_requires_session():
    with patch.object(li, "has_linkedin_session", return_value=False):
        with pytest.raises(li.LinkedInSessionError):
            await li.fetch_profile_text()


# ---------------------------------------------------------------------------
# import_profile — scrape mocked, LLM mocked (same pattern as resume tests)
# ---------------------------------------------------------------------------
def _mock_client(content: str):
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )
    client = SimpleNamespace()
    client.chat = SimpleNamespace()
    client.chat.completions = SimpleNamespace(create=AsyncMock(return_value=response))
    return client


@pytest.fixture
def cfg():
    return {"ai": {"base_url": "http://x", "models": {"strong": {"model": "m"}}}}


@pytest.mark.asyncio
async def test_import_profile_fills_linkedin_url_from_navigation(cfg):
    payload = {
        "full_name": "Jane Doe",
        "summary": "Security engineer.",
        "skills": ["SIEM"],
        "experience": [{"title": "Analyst", "company": "Acme",
                        "start_date": "2021", "end_date": "Present", "bullets": ["Did SOC work"]}],
        "education": [],
        "certifications": ["Security+"],
        "linkedin": "",  # model found no URL in the scraped text
    }
    client = _mock_client(json.dumps(payload))
    with patch.object(li, "fetch_profile_text",
                      AsyncMock(return_value=("=== PROFILE OVERVIEW ===\nJane Doe",
                                              "https://www.linkedin.com/in/jane-doe/"))), \
         patch.object(resume_parser.ai_engine, "_get_client", return_value=client), \
         patch.object(resume_parser.ai_engine, "_model", return_value="m"):
        out = await li.import_profile(cfg)

    p = out["profile"]
    assert p["full_name"] == "Jane Doe"
    assert p["linkedin"] == "https://www.linkedin.com/in/jane-doe/"
    assert out["linkedin_url"] == "https://www.linkedin.com/in/jane-doe/"
    assert p["experience"][0]["company"] == "Acme"
    assert p["certifications"] == ["Security+"]
    # The LinkedIn prompt template formats cleanly (its JSON braces are escaped)
    sent = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "LINKEDIN PROFILE TEXT" in sent
    assert "Jane Doe" in sent


@pytest.mark.asyncio
async def test_import_profile_empty_scrape_returns_warning(cfg):
    with patch.object(li, "fetch_profile_text", AsyncMock(return_value=("", ""))):
        out = await li.import_profile(cfg)
    assert out["profile"]["full_name"] == ""
    assert out["warnings"]


@pytest.mark.asyncio
async def test_import_profile_never_fabricates_on_empty_model_output(cfg):
    # Honest model facing thin input returns empties — they must survive as-is.
    client = _mock_client(json.dumps({
        "full_name": "", "email": "", "summary": "",
        "skills": [], "experience": [], "education": [], "certifications": [],
    }))
    with patch.object(li, "fetch_profile_text",
                      AsyncMock(return_value=("=== PROFILE OVERVIEW ===\n(sparse)", ""))), \
         patch.object(resume_parser.ai_engine, "_get_client", return_value=client), \
         patch.object(resume_parser.ai_engine, "_model", return_value="m"):
        out = await li.import_profile(cfg)
    p = out["profile"]
    assert p["full_name"] == ""
    assert p["linkedin"] == ""  # no URL resolved, none invented
    assert p["experience"] == []
