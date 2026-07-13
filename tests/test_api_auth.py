"""
Tests for the dashboard API auth gate (SEC-01), Host-header validation (SEC-05),
and /api/config secret redaction.

These deliberately opt OUT of the autouse JOBSMITH_ALLOW_INSECURE fixture in
conftest.py, because the whole point is to exercise the locked-down default.
"""

import pytest
from fastapi.testclient import TestClient

from backend import extension_api
from backend.main import app
from backend.routers import _auth
from backend.routers.settings import SECRET_MASK


@pytest.fixture(autouse=True)
def _enable_api_auth(monkeypatch):
    """Undo conftest's opt-out: these tests want the gate live."""
    monkeypatch.delenv("JOBSMITH_ALLOW_INSECURE", raising=False)


@pytest.fixture
def client():
    # TestClient's default client host is "testclient" — i.e. NOT loopback, so
    # these requests take the off-machine path through the gate.
    return TestClient(app)


@pytest.fixture
def token():
    return extension_api.get_or_create_token()


class TestDashboardAuthGate:
    def test_off_machine_request_is_rejected(self, client):
        assert client.get("/api/config").status_code == 401

    def test_valid_token_header_is_accepted(self, client, token):
        resp = client.get("/api/config", headers={"X-Jobsmith-Token": token})
        assert resp.status_code == 200

    def test_wrong_token_is_rejected(self, client):
        resp = client.get("/api/config", headers={"X-Jobsmith-Token": "nope"})
        assert resp.status_code == 401

    def test_loopback_request_is_accepted_without_a_token(self, monkeypatch, client):
        monkeypatch.setattr(_auth.state, "is_loopback_request", lambda request: True)
        assert client.get("/api/config").status_code == 200

    def test_insecure_escape_hatch_reopens_the_api(self, monkeypatch, client):
        monkeypatch.setenv("JOBSMITH_ALLOW_INSECURE", "1")
        assert client.get("/api/config").status_code == 200

    def test_health_live_stays_open_for_the_healthcheck(self, client):
        # The container HEALTHCHECK has no token; it must never be gated.
        resp = client.get("/api/health/live")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestCookieExchange:
    """A browser off-machine (e.g. every Docker user) must be able to log in."""

    def test_login_with_bad_token_is_rejected(self, client):
        assert client.post("/api/auth/login", json={"token": "wrong"}).status_code == 401

    def test_login_sets_a_cookie_that_unlocks_the_api(self, client, token):
        assert client.get("/api/config").status_code == 401
        assert client.post("/api/auth/login", json={"token": token}).status_code == 200
        # TestClient persists cookies across requests, like a browser.
        assert client.get("/api/config").status_code == 200

    def test_status_reports_whether_a_token_is_needed(self, client, token):
        assert client.get("/api/auth/status").json() == {
            "authenticated": False,
            "required": True,
        }
        client.post("/api/auth/login", json={"token": token})
        assert client.get("/api/auth/status").json()["authenticated"] is True


class TestConfigRedaction:
    def _cfg(self, client, token):
        return client.get("/api/config", headers={"X-Jobsmith-Token": token}).json()

    def test_secrets_are_masked_for_off_machine_callers(self, client, token, monkeypatch):
        monkeypatch.setattr(_auth.state, "load_config", lambda: {
            "profile": {"workday_password": "hunter2", "ats_login_password": "s3cret"},
            "ai": {"api_key": "sk-live-abc"},
            "api_keys": {"adzuna_app_key": "adz-key", "usajobs_api_key": "usa-key"},
            "salary_estimator": {"bls": {"api_key": "bls-key"}},
        })
        cfg = self._cfg(client, token)
        assert cfg["profile"]["workday_password"] == SECRET_MASK
        assert cfg["profile"]["ats_login_password"] == SECRET_MASK
        assert cfg["ai"]["api_key"] == SECRET_MASK
        assert cfg["api_keys"]["adzuna_app_key"] == SECRET_MASK
        assert cfg["api_keys"]["usajobs_api_key"] == SECRET_MASK
        assert cfg["salary_estimator"]["bls"]["api_key"] == SECRET_MASK
        # And the real values are nowhere in the response body.
        assert "hunter2" not in resp_text(cfg)
        assert "sk-live-abc" not in resp_text(cfg)

    def test_unset_secrets_are_not_masked_into_looking_set(self, client, token, monkeypatch):
        monkeypatch.setattr(_auth.state, "load_config", lambda: {"profile": {}, "ai": {}})
        cfg = self._cfg(client, token)
        assert cfg["profile"]["workday_password"] == ""
        assert cfg["ai"]["api_key"] == ""

    def test_posting_the_mask_back_does_not_overwrite_the_secret(self, monkeypatch, client, token):
        saved = {}
        monkeypatch.setattr(_auth.state, "load_config",
                            lambda: {"profile": {"workday_password": "hunter2"}})
        monkeypatch.setattr("backend.routers.settings.state.save_config",
                            lambda cfg: saved.update(cfg))
        resp = client.post(
            "/api/config",
            headers={"X-Jobsmith-Token": token},
            # what a settings form round-trips when the user never touched the field
            json={"profile": {"workday_password": SECRET_MASK, "full_name": "Ada"}},
        )
        assert resp.status_code == 200
        assert saved["profile"]["workday_password"] == "hunter2"  # not clobbered
        assert saved["profile"]["full_name"] == "Ada"


class TestHostHeaderValidation:
    """SEC-05 — DNS rebinding: a page on attacker.com must not reach the API."""

    def test_rebound_host_header_is_rejected(self):
        with TestClient(app, base_url="http://attacker.com") as c:
            assert c.get("/api/health/live").status_code == 400

    def test_localhost_host_header_is_accepted(self):
        with TestClient(app, base_url="http://localhost") as c:
            assert c.get("/api/health/live").status_code == 200


def resp_text(payload) -> str:
    import json
    return json.dumps(payload)


class TestAssistSetupTokenIsEphemeral:
    """SEC-14 — the setup token is embedded in the launch page DOM, so it must
    not BE the long-lived token that unlocks all of /api/ext/*."""

    def test_launch_page_does_not_embed_the_persistent_token(self):
        """The real lock: assist.py must mint a fresh secret, not reuse the
        long-lived one, when creating a handoff session."""
        import inspect

        from backend.routers import assist

        src = inspect.getsource(assist)
        assert "setup_token = secrets.token_urlsafe" in src
        assert "create_handoff_session(job, setup_token=setup_token)" in src
        # The old bug, in one line: handing get_or_create_token() to the session.
        assert "create_handoff_session(job, setup_token=token)" not in src

    def test_checkin_hands_back_the_persistent_token(self, monkeypatch, client, token):
        import backend.applicant_assist as aa

        monkeypatch.setattr(aa, "get_handoff_session", lambda sid: {
            "id": sid, "setup_token": "ephemeral-xyz", "apply_url": "http://apply",
        })
        monkeypatch.setattr(aa, "mark_handoff_extension_ready", lambda sid: None)

        resp = client.post(
            "/api/ext/assist/checkin",
            json={"session_id": "s1"},
            headers={"X-Jobsmith-Token": "ephemeral-xyz"},  # the DOM token
        )
        assert resp.status_code == 200
        # …and we get the real, persistent token back to store.
        assert resp.json()["token"] == token
        assert resp.json()["token"] != "ephemeral-xyz"
