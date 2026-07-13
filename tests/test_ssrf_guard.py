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
        "http://192.168.1.7:1234/v1/models",
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
