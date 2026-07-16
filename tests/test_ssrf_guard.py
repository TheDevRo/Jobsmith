"""
SEC-02 — /api/jobs/ingest-url fetches a user-supplied URL server-side, so it must
refuse to reach anything that isn't a public address (cloud metadata, the Docker
bridge, localhost admin panels).
"""

import pytest

from backend.job_sources.manual import assert_public_http_url


class TestSsrfGuard:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/",
        "http://127.0.0.1:8888/api/config",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",   # AWS/GCP/Azure metadata
        "http://[::1]/",
        "http://10.0.0.5/",
        "http://192.168.1.100:1234/v1/models",
        "http://172.17.0.1/",                          # docker bridge gateway
        "http://0.0.0.0/",
    ])
    def test_internal_addresses_are_refused(self, url):
        with pytest.raises(ValueError, match="private/internal"):
            assert_public_http_url(url)

    @pytest.mark.parametrize("url", [
        "ftp://example.com/job",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "",
        "   ",
    ])
    def test_non_http_schemes_are_refused(self, url):
        with pytest.raises(ValueError):
            assert_public_http_url(url)

    def test_a_hostname_resolving_to_loopback_is_refused(self, monkeypatch):
        """The guard must resolve, not just pattern-match: a public *name* can
        point at 127.0.0.1 (classic DNS-based SSRF bypass)."""
        import backend.job_sources.manual as manual

        monkeypatch.setattr(manual.socket, "getaddrinfo", lambda *a, **k: [
            (2, 1, 6, "", ("127.0.0.1", 80)),
        ])
        with pytest.raises(ValueError, match="private/internal"):
            assert_public_http_url("http://totally-legit-jobs.example/posting/1")

    def test_a_public_url_is_allowed(self, monkeypatch):
        import backend.job_sources.manual as manual

        monkeypatch.setattr(manual.socket, "getaddrinfo", lambda *a, **k: [
            (2, 1, 6, "", ("93.184.216.34", 443)),
        ])
        url = assert_public_http_url("  https://example.com/jobs/1  ")
        assert url == "https://example.com/jobs/1"  # also trims

    def test_every_resolved_address_must_be_public(self, monkeypatch):
        """A host resolving to one public and one internal IP must still be refused."""
        import backend.job_sources.manual as manual

        monkeypatch.setattr(manual.socket, "getaddrinfo", lambda *a, **k: [
            (2, 1, 6, "", ("93.184.216.34", 80)),
            (2, 1, 6, "", ("169.254.169.254", 80)),
        ])
        with pytest.raises(ValueError, match="private/internal"):
            assert_public_http_url("http://dual.example/")


class _FakeResp:
    """Minimal aiohttp-response stand-in for the redirect-guard tests."""
    def __init__(self, status, headers=None, body=""):
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._body


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requested = []

    def get(self, url, **kwargs):
        # The guard must disable aiohttp's own redirect following.
        assert kwargs.get("allow_redirects") is False
        self.requested.append(url)
        return self._responses.pop(0)


class TestRedirectGuard:
    """SEC — the ingest fetch must re-validate every redirect hop, or a public
    URL can 302 the server into an internal address (SSRF-via-redirect / TOCTOU)."""

    def test_redirect_to_internal_address_is_refused(self):
        import asyncio
        from backend.job_sources import manual

        session = _FakeSession([
            _FakeResp(302, {"Location": "http://169.254.169.254/latest/meta-data/"}),
        ])
        with pytest.raises(ValueError, match="private/internal"):
            asyncio.run(manual._get_following_redirects(session, "https://jobs.example/1"))
        assert session.requested == ["https://jobs.example/1"]  # never reached the metadata host

    def test_redirect_to_public_address_is_followed(self):
        import asyncio
        from backend.job_sources import manual

        session = _FakeSession([
            _FakeResp(301, {"Location": "http://93.184.216.34/final"}),
            _FakeResp(200, body="<html>ok</html>"),
        ])
        body, final = asyncio.run(
            manual._get_following_redirects(session, "http://93.184.216.34/start")
        )
        assert body == "<html>ok</html>"
        assert final == "http://93.184.216.34/final"

    def test_redirect_chain_is_bounded(self):
        import asyncio
        from backend.job_sources import manual

        # Every hop is a public 302 — the guard must still stop after max_hops.
        session = _FakeSession(
            [_FakeResp(302, {"Location": f"http://93.184.216.34/{i}"}) for i in range(10)]
        )
        with pytest.raises(ValueError, match="Too many redirects"):
            asyncio.run(manual._get_following_redirects(session, "http://93.184.216.34/start"))
