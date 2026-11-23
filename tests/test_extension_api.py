"""
Tests for backend/extension_api.py — token auth, profile endpoint, scan endpoint.
All offline: LLM and DB are mocked.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import extension_api
from backend.auto_apply.models import FieldValue


@pytest.fixture
def token_path(tmp_path, monkeypatch):
    p = tmp_path / "extension_token.txt"
    monkeypatch.setattr(extension_api, "DATA_DIR", tmp_path)
    monkeypatch.setattr(extension_api, "TOKEN_PATH", p)
    return p


@pytest.fixture
def fake_config():
    return {
        "profile": {
            "full_name": "Test User",
            "email": "test@example.com",
            "phone": "555-0100",
            "linkedin": "linkedin.com/in/test",
            "location": "Remote",
        },
        "ai": {"base_url": "http://localhost:1234/v1", "model": "fake"},
    }


@pytest.fixture
def client(token_path, fake_config):
    app = FastAPI()
    app.include_router(extension_api.build_router(lambda: fake_config))
    return TestClient(app)


def test_health_no_auth(client):
    r = client.get("/api/ext/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_profile_requires_token(client):
    r = client.get("/api/ext/profile")
    assert r.status_code == 401


def test_profile_with_token(client, token_path):
    token = extension_api.get_or_create_token()
    r = client.get("/api/ext/profile", headers={"X-Jobsmith-Token": token})
    assert r.status_code == 200
    body = r.json()
    assert body["full_name"] == "Test User"
    assert body["email"] == "test@example.com"


def test_profile_wrong_token(client):
    extension_api.get_or_create_token()
    r = client.get("/api/ext/profile", headers={"X-Jobsmith-Token": "wrong"})
    assert r.status_code == 401


def test_token_persisted(token_path):
    t1 = extension_api.get_or_create_token()
    t2 = extension_api.get_or_create_token()
    assert t1 == t2
    assert token_path.read_text().strip() == t1


def test_token_file_owner_only(token_path):
    extension_api.get_or_create_token()
    assert token_path.stat().st_mode & 0o777 == 0o600
    extension_api.rotate_token()
    assert token_path.stat().st_mode & 0o777 == 0o600


def test_scan_endpoint_uses_llm(client, token_path):
    token = extension_api.get_or_create_token()

    fake_values = [
        FieldValue(field_id="email", value="test@example.com",
                   action="fill", confidence=1.0, source="profile"),
    ]
    with patch.object(
        extension_api.LLMClient, "map_fields_to_values",
        new=AsyncMock(return_value=fake_values),
    ), patch("backend.extension_api.db.get_job", new=AsyncMock(return_value=None)):
        r = client.post(
            "/api/ext/scan",
            headers={"X-Jobsmith-Token": token},
            json={
                "url": "https://example.com/jobs/1",
                "fields": [{"field_id": "email", "label": "Email", "field_type": "email"}],
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["fields"][0]["value"] == "test@example.com"


def test_answer_from_bank(client, token_path):
    token = extension_api.get_or_create_token()
    from backend.auto_apply import answer_bank as ab_mod
    fake_bank = type("FB", (), {"find_best_match": lambda self, q: "Yes, authorized"})()
    with patch.object(ab_mod, "get_answer_bank", return_value=fake_bank):
        r = client.post(
            "/api/ext/answer",
            headers={"X-Jobsmith-Token": token},
            json={"question": "Are you authorized to work in the US?"},
        )
    assert r.status_code == 200
    assert r.json() == {"value": "Yes, authorized", "source": "answer_bank", "confidence": 1.0}


def test_resume_404_when_missing(client, token_path, tmp_path, monkeypatch):
    monkeypatch.setattr(extension_api, "RESUMES_DIR", tmp_path / "nope")
    token = extension_api.get_or_create_token()
    r = client.get("/api/ext/resume/job-123", headers={"X-Jobsmith-Token": token})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Resume / cover-letter downloads: ?format=docx|pdf, PDF rendered on demand
# ---------------------------------------------------------------------------

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
KINDS = [("resume", "resume"), ("cover-letter", "cover_letter")]
SAMPLE_JOB = {
    "id": "job-1", "title": "Engineer", "company": "Example Co",
    "application": {
        "resume_content": "SUMMARY\nBuilds reliable software.\n\nSKILLS\nPython, SQL",
        "cover_letter_content": "Dear Hiring Team,\n\nI would like to apply.\n",
    },
}


@pytest.fixture
def docs_dir(tmp_path, monkeypatch):
    """Point both the endpoint and the renderers at one scratch directory."""
    from backend import resume_generator
    monkeypatch.setattr(extension_api, "RESUMES_DIR", tmp_path)
    monkeypatch.setattr(resume_generator, "RESUMES_DIR", tmp_path)
    return tmp_path


def _get(client, kind, fmt=None):
    token = extension_api.get_or_create_token()
    url = f"/api/ext/{kind}/job-1" + (f"?format={fmt}" if fmt else "")
    with patch.object(extension_api.db, "get_job", new=AsyncMock(return_value=SAMPLE_JOB)):
        return client.get(url, headers={"X-Jobsmith-Token": token})


@pytest.mark.parametrize("kind,stem", KINDS)
def test_format_pdf_renders_on_demand(client, token_path, docs_dir, kind, stem):
    (docs_dir / f"job-1_{stem}.docx").write_bytes(b"docx")
    r = _get(client, kind, "pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert f'job-1_{stem}.pdf' in r.headers["content-disposition"]
    assert r.content.startswith(b"%PDF")
    assert (docs_dir / f"job-1_{stem}.pdf").exists()


@pytest.mark.parametrize("kind,stem", KINDS)
def test_format_pdf_serves_existing_pdf_without_rerender(client, token_path, docs_dir, kind, stem):
    (docs_dir / f"job-1_{stem}.pdf").write_bytes(b"%PDF-existing")
    r = _get(client, kind, "pdf")
    assert r.status_code == 200
    assert r.content == b"%PDF-existing"


@pytest.mark.parametrize("kind,stem", KINDS)
def test_format_docx_serves_docx_even_when_pdf_exists(client, token_path, docs_dir, kind, stem):
    (docs_dir / f"job-1_{stem}.docx").write_bytes(b"docx")
    (docs_dir / f"job-1_{stem}.pdf").write_bytes(b"%PDF-existing")
    r = _get(client, kind, "docx")
    assert r.status_code == 200
    assert r.headers["content-type"] == DOCX_MIME
    assert f"job-1_{stem}.docx" in r.headers["content-disposition"]
    assert r.content == b"docx"


@pytest.mark.parametrize("kind,stem", KINDS)
def test_no_format_param_follows_config(client, token_path, docs_dir, fake_config, kind, stem):
    (docs_dir / f"job-1_{stem}.docx").write_bytes(b"docx")
    fake_config["application_honesty"] = {"document_format": "docx"}
    r = _get(client, kind)
    assert r.headers["content-type"] == DOCX_MIME
    fake_config["application_honesty"] = {"document_format": "pdf"}
    r = _get(client, kind)
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


@pytest.mark.parametrize("kind,stem", KINDS)
def test_pdf_render_failure_falls_back_to_docx(client, token_path, docs_dir, caplog, kind, stem):
    from backend import resume_generator
    (docs_dir / f"job-1_{stem}.docx").write_bytes(b"docx")
    with patch.object(resume_generator, "_render_resume_pdf", side_effect=RuntimeError("boom")), \
         patch.object(resume_generator, "_render_cover_letter_pdf", side_effect=RuntimeError("boom")), \
         caplog.at_level("WARNING", logger=extension_api.logger.name):
        r = _get(client, kind, "pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == DOCX_MIME
    assert r.content == b"docx"
    assert "PDF rendering failed" in caplog.text


@pytest.mark.parametrize("kind,stem", KINDS)
def test_format_pdf_without_content_or_files_is_404(client, token_path, docs_dir, kind, stem):
    token = extension_api.get_or_create_token()
    with patch.object(extension_api.db, "get_job", new=AsyncMock(return_value=None)):
        r = client.get(f"/api/ext/{kind}/job-1?format=pdf", headers={"X-Jobsmith-Token": token})
    assert r.status_code == 404


def test_invalid_format_rejected(client, token_path, docs_dir):
    assert _get(client, "resume", "odt").status_code == 422


# ---------------------------------------------------------------------------
# Extension one-click session import (chrome.cookies → Playwright session)
# ---------------------------------------------------------------------------

CHROME_COOKIES = [
    {
        "name": "li_at", "value": "AQEDxyz", "domain": ".linkedin.com",
        "path": "/", "secure": True, "httpOnly": True,
        "sameSite": "no_restriction", "expirationDate": 1790000000.5,
        "hostOnly": False, "session": False,
    },
    {
        "name": "JSESSIONID", "value": "ajax:1", "domain": ".www.linkedin.com",
        "path": "/", "secure": True, "httpOnly": False,
        "sameSite": "unspecified",  # chrome value with no Playwright equivalent
    },
]


@pytest.fixture
def import_dirs(tmp_path, monkeypatch):
    from backend import auto_apply
    monkeypatch.setattr(auto_apply, "LINKEDIN_SESSION_DIR", tmp_path / "linkedin_profile")
    monkeypatch.setattr(auto_apply, "INDEED_SESSION_DIR", tmp_path / "indeed_session")
    monkeypatch.setattr(auto_apply, "INDEED_SESSION_PATH", tmp_path / "indeed_session" / "storage_state.json")
    monkeypatch.setattr(auto_apply, "INDEED_CHROME_PROFILE_DIR", tmp_path / "indeed_profile")
    return tmp_path


def test_extension_import_linkedin(client, token_path, import_dirs):
    import json as _json
    token = extension_api.get_or_create_token()
    with patch("backend.extension_api.db.log_activity", new=AsyncMock(return_value=None)):
        r = client.post(
            "/api/ext/sessions/import",
            headers={"X-Jobsmith-Token": token},
            json={"domain": "linkedin", "cookies": CHROME_COOKIES},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["cookie_count"] == 2
    state_file = import_dirs / "linkedin_profile" / "storage_state.json"
    sentinel = import_dirs / "linkedin_profile" / "login_success.json"
    assert state_file.exists() and sentinel.exists()
    state = _json.loads(state_file.read_text())
    li_at = next(c for c in state["cookies"] if c["name"] == "li_at")
    assert li_at["sameSite"] == "None"          # no_restriction mapped
    assert li_at["expires"] == 1790000000.5      # expirationDate carried
    assert _json.loads(sentinel.read_text())["source"] == "extension_import"


def test_extension_import_requires_token(client, import_dirs):
    r = client.post("/api/ext/sessions/import",
                    json={"domain": "linkedin", "cookies": CHROME_COOKIES})
    assert r.status_code == 401


def test_extension_import_rejects_unknown_domain(client, token_path, import_dirs):
    token = extension_api.get_or_create_token()
    r = client.post("/api/ext/sessions/import", headers={"X-Jobsmith-Token": token},
                    json={"domain": "glassdoor", "cookies": CHROME_COOKIES})
    assert r.status_code == 400


def test_extension_import_rejects_empty_cookies(client, token_path, import_dirs):
    token = extension_api.get_or_create_token()
    r = client.post("/api/ext/sessions/import", headers={"X-Jobsmith-Token": token},
                    json={"domain": "indeed", "cookies": []})
    assert r.status_code == 400


# ---- Workday one-tap auth (credentials + tenant registry) -----------------

@pytest.fixture
def workday_client(token_path):
    cfg = {
        "profile": {
            "full_name": "Test User", "email": "test@example.com",
            "workday_email": "jobs@example.com", "workday_password": "s3cret",
        },
        "ai": {"base_url": "http://localhost:1234/v1", "model": "fake"},
    }
    app = FastAPI()
    app.include_router(extension_api.build_router(lambda: cfg))
    return TestClient(app)


def test_workday_credentials_requires_token(workday_client):
    assert workday_client.get("/api/ext/workday_credentials").status_code == 401


def test_workday_credentials_returns_configured(workday_client, token_path):
    token = extension_api.get_or_create_token()
    r = workday_client.get("/api/ext/workday_credentials", headers={"X-Jobsmith-Token": token})
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["email"] == "jobs@example.com"
    assert body["password"] == "s3cret"


def test_workday_credentials_unconfigured(token_path):
    app = FastAPI()
    app.include_router(extension_api.build_router(lambda: {"profile": {}}))
    client = TestClient(app)
    token = extension_api.get_or_create_token()
    r = client.get("/api/ext/workday_credentials", headers={"X-Jobsmith-Token": token})
    assert r.status_code == 200
    assert r.json()["configured"] is False


def test_workday_account_report_and_read(workday_client, token_path, tmp_path, monkeypatch):
    import asyncio
    from backend import database as dbmod

    monkeypatch.setattr(dbmod, "DB_PATH", tmp_path / "reg.db")
    asyncio.run(dbmod.init_db())
    token = extension_api.get_or_create_token()
    hdr = {"X-Jobsmith-Token": token}
    host = "acme.wd5.myworkdayjobs.com"

    # Unknown tenant → not found.
    r = workday_client.get(f"/api/ext/workday_account?host={host}", headers=hdr)
    assert r.status_code == 200 and r.json()["found"] is False

    # Report an account creation that is pending verification.
    r = workday_client.post("/api/ext/workday_account", headers=hdr,
                            json={"tenant_host": host, "action": "account_created",
                                  "email": "jobs@example.com", "pending": True})
    assert r.status_code == 200
    assert r.json()["account"]["status"] == "pending_verification"

    # It is now known.
    r = workday_client.get(f"/api/ext/workday_account?host={host}", headers=hdr)
    assert r.json()["found"] is True

    # A later sign-in promotes it to active.
    r = workday_client.post("/api/ext/workday_account", headers=hdr,
                            json={"tenant_host": host, "action": "signed_in"})
    assert r.json()["account"]["status"] == "active"


# ---- A3: assist checkin persists extension_paired -------------------------

@pytest.fixture
def paired_config(monkeypatch):
    """Capture what assist_checkin writes back to config.yaml."""
    from backend import app_state

    saved = {"cfg": {"profile": {}}, "writes": 0}

    def _save(cfg):
        saved["cfg"] = cfg
        saved["writes"] += 1

    monkeypatch.setattr(app_state, "load_config", lambda: dict(saved["cfg"]))
    monkeypatch.setattr(app_state, "save_config", _save)
    return saved


def _start_handoff():
    """Register a handoff session and return (session_id, setup_token)."""
    from backend import applicant_assist

    setup_token = "setup-tok"
    rec = applicant_assist.create_handoff_session(
        {"id": "job1", "title": "T", "company": "C", "url": "https://example.com/apply"},
        setup_token,
    )
    return rec["id"], setup_token


def test_assist_checkin_marks_extension_paired(client, token_path, paired_config):
    session_id, setup_token = _start_handoff()
    r = client.post("/api/ext/assist/checkin",
                    headers={"X-Jobsmith-Token": setup_token},
                    json={"session_id": session_id})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert paired_config["cfg"]["extension_paired"] is True
    assert paired_config["writes"] == 1

    # Already true → no second write (no config churn per checkin).
    session_id2, setup_token2 = _start_handoff()
    r = client.post("/api/ext/assist/checkin",
                    headers={"X-Jobsmith-Token": setup_token2},
                    json={"session_id": session_id2})
    assert r.status_code == 200
    assert paired_config["writes"] == 1


def test_assist_checkin_rejects_bad_token(client, token_path, paired_config):
    session_id, _ = _start_handoff()
    r = client.post("/api/ext/assist/checkin",
                    headers={"X-Jobsmith-Token": "nope"},
                    json={"session_id": session_id})
    assert r.status_code == 401
    assert paired_config["writes"] == 0
