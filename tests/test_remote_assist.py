from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from backend import applicant_assist, extension_api
from backend.main import app
from backend.routers import assist


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("JOBSMITH_ALLOW_INSECURE", raising=False)
    monkeypatch.setattr(extension_api, "TOKEN_PATH", tmp_path / "token.txt")
    monkeypatch.setenv("JOBSMITH_EXTERNAL_URL", "https://testserver")
    monkeypatch.setattr(assist.state, "load_config", lambda: {})
    return TestClient(app)


def test_remote_launch_returns_configured_https_origin(client, monkeypatch):
    monkeypatch.setattr(assist.db, "get_job", AsyncMock(return_value={
        "id": "job-1", "url": "https://jobs.example/apply",
        "application": {"resume_content": "Sample resume"},
    }))
    monkeypatch.setattr(applicant_assist, "register_active_session", lambda **kw: None)
    monkeypatch.setattr(applicant_assist, "create_handoff_session",
                        lambda job, setup_token: {"id": "session-1"})
    with patch("webbrowser.open") as open_browser:
        response = client.post("/api/assist/launch", json={"job_id": "job-1"},
                               headers={"X-Jobsmith-Token": extension_api.get_or_create_token()})
    assert response.status_code == 200
    assert response.json()["launch_url"] == "https://testserver/assist/launch/session-1"
    assert response.json()["opened"] is False
    open_browser.assert_not_called()


def test_remote_page_requires_dashboard_auth_and_exact_configured_host(client, monkeypatch):
    monkeypatch.setattr(applicant_assist, "get_handoff_session", lambda sid: {
        "id": sid, "setup_token": "ephemeral", "apply_url": "https://jobs.example/apply",
        "job_title": "Engineer", "job_company": "Example",
    })
    assert client.get("/assist/launch/session-1").status_code == 401
    headers = {"X-Jobsmith-Token": extension_api.get_or_create_token()}
    response = client.get("/assist/launch/session-1", headers=headers)
    assert response.status_code == 200
    assert 'data-setup-token="ephemeral"' in response.text
    monkeypatch.setenv("JOBSMITH_EXTERNAL_URL", "https://different.example")
    assert client.get("/assist/launch/session-1", headers=headers).status_code == 403
    # Remote pairing uses the authenticated DOM, not the loopback metadata endpoint.
    assert client.get("/api/assist/session/session-1/handshake-meta", headers=headers).status_code == 403


@pytest.mark.parametrize("value", ["http://jobs.example", "https://user:pw@jobs.example",
                                  "https://jobs.example/api", "https://jobs.example?q=x"])
def test_external_origin_rejects_unsafe_configuration(monkeypatch, value):
    monkeypatch.setenv("JOBSMITH_EXTERNAL_URL", value)
    with pytest.raises(ValueError):
        assist._external_base_url()
