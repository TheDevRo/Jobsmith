"""Tests for backend/cookie_import.py and the /api/sessions/import-cookies endpoint."""

import json

import pytest

from backend import cookie_import


COOKIE_EDITOR_EXPORT = json.dumps([
    {
        "name": "li_at", "value": "AQEDAxyz", "domain": ".linkedin.com",
        "path": "/", "expirationDate": 1790000000.5, "hostOnly": False,
        "httpOnly": True, "secure": True, "session": False,
        "sameSite": "no_restriction", "storeId": "0",
    },
    {
        "name": "JSESSIONID", "value": "ajax:123", "domain": ".www.linkedin.com",
        "path": "/", "hostOnly": False, "httpOnly": False, "secure": True,
        "session": True, "sameSite": "lax", "storeId": "0",
    },
])

STORAGE_STATE_EXPORT = json.dumps({
    "cookies": [
        {
            "name": "CTK", "value": "abc", "domain": ".indeed.com", "path": "/",
            "expires": 1790000000, "httpOnly": True, "secure": True,
            "sameSite": "Lax", "priority": "Medium", "sourceScheme": "Secure",
            "sourcePort": 443, "size": 40, "partitionKey": {"topLevelSite": "https://indeed.com"},
        },
    ],
    "origins": [{"origin": "https://indeed.com", "localStorage": []}],
})

NETSCAPE_EXPORT = """\
# Netscape HTTP Cookie File
# This is a generated file! Do not edit.

.glassdoor.com\tTRUE\t/\tTRUE\t1790000000\tGSESSIONID\tsess-value
#HttpOnly_.glassdoor.com\tTRUE\t/\tTRUE\t1790000000\tat\ttoken-value
.glassdoor.com\tTRUE\t/\tFALSE\t0\ttrs\ttracking
"""


class TestDetectFormat:
    def test_cookie_editor_array(self):
        assert cookie_import.detect_format(COOKIE_EDITOR_EXPORT) == "cookie_editor"

    def test_storage_state_object(self):
        assert cookie_import.detect_format(STORAGE_STATE_EXPORT) == "storage_state"

    def test_netscape(self):
        assert cookie_import.detect_format(NETSCAPE_EXPORT) == "netscape"

    def test_bytes_input(self):
        assert cookie_import.detect_format(NETSCAPE_EXPORT.encode()) == "netscape"

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            cookie_import.detect_format("hello world\nnot cookies")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            cookie_import.detect_format("   ")

    def test_wrong_json_shape_raises(self):
        with pytest.raises(ValueError):
            cookie_import.detect_format('{"foo": "bar"}')


class TestParseNetscape:
    def test_parses_fields(self):
        cookies = cookie_import.parse_netscape(NETSCAPE_EXPORT)
        assert len(cookies) == 3
        first = cookies[0]
        assert first["name"] == "GSESSIONID"
        assert first["value"] == "sess-value"
        assert first["domain"] == ".glassdoor.com"
        assert first["secure"] is True
        assert first["expires"] == 1790000000

    def test_httponly_prefix(self):
        cookies = cookie_import.parse_netscape(NETSCAPE_EXPORT)
        at = next(c for c in cookies if c["name"] == "at")
        assert at["httpOnly"] is True

    def test_comments_skipped(self):
        cookies = cookie_import.parse_netscape("# just a comment\n\n")
        assert cookies == []


class TestNormalizeCookies:
    def test_chromium_fields_stripped(self):
        cookies = cookie_import.parse_upload(STORAGE_STATE_EXPORT)
        assert len(cookies) == 1
        c = cookies[0]
        for field in ("priority", "sourceScheme", "sourcePort", "size", "partitionKey"):
            assert field not in c

    def test_samesite_mapping(self):
        raw = [
            {"name": "a", "value": "1", "domain": ".x.com", "sameSite": "no_restriction"},
            {"name": "b", "value": "2", "domain": ".x.com", "sameSite": "lax"},
            {"name": "c", "value": "3", "domain": ".x.com", "sameSite": "strict"},
            {"name": "d", "value": "4", "domain": ".x.com", "sameSite": "unspecified"},
            {"name": "e", "value": "5", "domain": ".x.com"},
        ]
        out = cookie_import.normalize_cookies(raw)
        by_name = {c["name"]: c for c in out}
        assert by_name["a"]["sameSite"] == "None"
        assert by_name["b"]["sameSite"] == "Lax"
        assert by_name["c"]["sameSite"] == "Strict"
        assert "sameSite" not in by_name["d"]
        assert "sameSite" not in by_name["e"]

    def test_session_cookie_expiry(self):
        cookies = cookie_import.parse_upload(COOKIE_EDITOR_EXPORT)
        jsession = next(c for c in cookies if c["name"] == "JSESSIONID")
        assert jsession["expires"] == -1

    def test_expiration_date_alias(self):
        cookies = cookie_import.parse_upload(COOKIE_EDITOR_EXPORT)
        li_at = next(c for c in cookies if c["name"] == "li_at")
        assert li_at["expires"] == 1790000000.5

    def test_missing_domain_dropped(self):
        out = cookie_import.normalize_cookies([{"name": "x", "value": "y"}])
        assert out == []

    def test_non_dict_dropped(self):
        out = cookie_import.normalize_cookies(["nope", None, 42])
        assert out == []


class TestDetectSite:
    def test_linkedin(self):
        cookies = cookie_import.parse_upload(COOKIE_EDITOR_EXPORT)
        assert cookie_import.detect_site(cookies) == "linkedin"

    def test_indeed(self):
        cookies = cookie_import.parse_upload(STORAGE_STATE_EXPORT)
        assert cookie_import.detect_site(cookies) == "indeed"

    def test_generic_registrable_domain(self):
        cookies = cookie_import.parse_upload(NETSCAPE_EXPORT)
        assert cookie_import.detect_site(cookies) == "glassdoor.com"

    def test_empty(self):
        assert cookie_import.detect_site([]) is None

    def test_majority_vote(self):
        cookies = [
            {"name": "a", "value": "1", "domain": ".linkedin.com"},
            {"name": "b", "value": "2", "domain": ".linkedin.com"},
            {"name": "c", "value": "3", "domain": ".ads.tracker.net"},
        ]
        assert cookie_import.detect_site(cookie_import.normalize_cookies(cookies)) == "linkedin"


class TestImportEndpoint:
    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from backend import auto_apply, session_manager
        from backend.main import app

        monkeypatch.setattr(auto_apply, "LINKEDIN_SESSION_DIR", tmp_path / "linkedin_profile")
        monkeypatch.setattr(auto_apply, "INDEED_SESSION_DIR", tmp_path / "indeed_session")
        monkeypatch.setattr(auto_apply, "INDEED_SESSION_PATH", tmp_path / "indeed_session" / "storage_state.json")
        monkeypatch.setattr(auto_apply, "INDEED_CHROME_PROFILE_DIR", tmp_path / "indeed_profile")
        monkeypatch.setattr(session_manager, "SESSIONS_DIR", tmp_path / "sessions")

        async def fake_check():
            return True
        monkeypatch.setattr(auto_apply, "check_linkedin_session_validity", fake_check)

        with TestClient(app) as c:
            yield c, tmp_path

    def test_linkedin_import_writes_state_and_sentinel(self, client):
        c, tmp_path = client
        resp = c.post(
            "/api/sessions/import-cookies",
            files={"file": ("cookies.json", COOKIE_EDITOR_EXPORT, "application/json")},
            data={"site": "auto"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["site"] == "linkedin"
        assert body["imported"] == 2
        state_file = tmp_path / "linkedin_profile" / "storage_state.json"
        sentinel = tmp_path / "linkedin_profile" / "login_success.json"
        assert state_file.exists() and sentinel.exists()
        state = json.loads(state_file.read_text())
        assert {c_["name"] for c_ in state["cookies"]} == {"li_at", "JSESSIONID"}
        assert json.loads(sentinel.read_text())["source"] == "cookie_import"

    def test_indeed_import(self, client):
        c, tmp_path = client
        resp = c.post(
            "/api/sessions/import-cookies",
            files={"file": ("state.json", STORAGE_STATE_EXPORT, "application/json")},
            data={"site": "indeed"},
        )
        assert resp.status_code == 200, resp.text
        assert (tmp_path / "indeed_session" / "storage_state.json").exists()
        assert (tmp_path / "indeed_profile" / "login_success.json").exists()

    def test_generic_domain_import(self, client):
        c, tmp_path = client
        resp = c.post(
            "/api/sessions/import-cookies",
            files={"file": ("cookies.txt", NETSCAPE_EXPORT, "text/plain")},
            data={"site": "auto"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["site"] == "glassdoor.com"
        session_file = tmp_path / "sessions" / "glassdoor.com.json"
        assert session_file.exists()
        state = json.loads(session_file.read_text())
        assert len(state["cookies"]) == 3

    def test_bad_file_400(self, client):
        c, _ = client
        resp = c.post(
            "/api/sessions/import-cookies",
            files={"file": ("junk.txt", "not cookies at all", "text/plain")},
            data={"site": "auto"},
        )
        assert resp.status_code == 400

    def test_bad_domain_400(self, client):
        c, _ = client
        resp = c.post(
            "/api/sessions/import-cookies",
            files={"file": ("cookies.txt", NETSCAPE_EXPORT, "text/plain")},
            data={"site": "../evil"},
        )
        assert resp.status_code == 400
