"""
tests/job_sources/test_portals.py

Declarative JSON portals (backend/job_sources/portals.py): strict format
validation, the host allowlist on every hop (redirects included), mapping of the
three bundled portals from recorded fixtures, the feed and schema_org kinds,
off-by-default wiring into fetch_all_jobs, and failure isolation.

Offline: HTTP goes through httpx.MockTransport; nothing touches the network.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from backend import job_sources
from backend.job_sources import portals

FIXTURES = Path(__file__).parent / "fixtures"
KEYWORDS = {"search": {"keywords": ["engineer"]}}


def good(**over) -> dict:
    base = {
        "id": "demo", "name": "Demo", "version": "1.0.0",
        "homepage": "https://demo.example/", "terms_url": "https://demo.example/terms",
        "free": True, "hosts": ["api.demo.example"], "kind": "json_api",
        "request": {"url": "https://api.demo.example/jobs?q={query}&page={page}", "max_pages": 2},
        "items": "data.jobs",
        "fields": {"title": "title", "url": "link", "company": "org.name"},
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """User data under tmp, no sleeping, and the public-address guard stubbed
    (its own behaviour is covered in tests/test_ssrf_guard.py)."""
    monkeypatch.setenv("JOBSMITH_HOME", str(tmp_path))
    monkeypatch.setattr("backend.job_sources._SOURCE_STATS_PATH", tmp_path / "stats.json")

    async def no_sleep(_):
        return None

    monkeypatch.setattr(portals.asyncio, "sleep", no_sleep)
    monkeypatch.setattr("backend.job_sources.manual.assert_public_http_url", lambda url: url)


def serve(monkeypatch, handler):
    """Route the engine's HTTP client through *handler(request) -> Response*."""
    seen: list[str] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return handler(request)

    monkeypatch.setattr(
        portals, "async_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(wrapped), follow_redirects=True),
    )
    return seen


def run(coro):
    import asyncio
    return asyncio.run(coro)


# ----- format validation -----

class TestValidation:
    def test_good_file_is_accepted(self):
        assert portals.validate_portal(good())["id"] == "demo"

    def test_bundled_portals_are_valid_and_https_only(self):
        loaded = portals.load_portals(user_dir=Path("/nonexistent"))
        assert set(loaded) == {"remotive", "himalayas", "jobicy"}
        for p in loaded.values():
            assert p["free"] is True and p["terms_url"].startswith("https://")
            assert p["request"]["url"].startswith("https://")

    @pytest.mark.parametrize("mutate", [
        lambda p: p.update(extra=1),
        lambda p: p["request"].update(method="POST"),
        lambda p: p["defaults"].update(company="x") if "defaults" in p else p.update(defaults={"company": "x"}),
    ])
    def test_unknown_keys_are_rejected(self, mutate):
        p = good()
        mutate(p)
        with pytest.raises(portals.PortalError, match="unknown key"):
            portals.validate_portal(p)

    def test_unknown_job_field_is_rejected(self):
        with pytest.raises(portals.PortalError, match="unknown job field"):
            portals.validate_portal(good(fields={"title": "t", "url": "u", "salary": "s"}))

    @pytest.mark.parametrize("url", [
        "http://api.demo.example/jobs",
        "ftp://api.demo.example/jobs",
        "//api.demo.example/jobs",
    ])
    def test_non_https_request_is_rejected(self, url):
        with pytest.raises(portals.PortalError, match="https"):
            portals.validate_portal(good(request={"url": url}))

    def test_non_https_homepage_or_terms_is_rejected(self):
        with pytest.raises(portals.PortalError, match="homepage"):
            portals.validate_portal(good(homepage="http://demo.example/"))
        with pytest.raises(portals.PortalError, match="terms_url"):
            portals.validate_portal(good(terms_url="javascript:alert(1)"))

    def test_host_outside_the_allowlist_is_rejected(self):
        with pytest.raises(portals.PortalError, match="not in hosts"):
            portals.validate_portal(good(request={"url": "https://evil.example/jobs"}))

    @pytest.mark.parametrize("url", [
        "https://user:pw@api.demo.example/jobs",
        "https://api.demo.example:8443/jobs",
        "https://{query}.demo.example/jobs",
        "https://api.demo.example/jobs?x={unknown}",
    ])
    def test_sneaky_urls_are_rejected(self, url):
        with pytest.raises(portals.PortalError):
            portals.validate_portal(good(request={"url": url}))

    @pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.5", "localhost", "*.demo.example", "Demo.example"])
    def test_ip_wildcard_and_bare_hosts_are_rejected(self, host):
        with pytest.raises(portals.PortalError, match="hosts"):
            portals.validate_portal(good(hosts=[host]))

    def test_kind_rules(self):
        with pytest.raises(portals.PortalError, match="kind"):
            portals.validate_portal(good(kind="graphql"))
        no_items = good()
        del no_items["items"]
        with pytest.raises(portals.PortalError, match="items"):
            portals.validate_portal(no_items)
        with pytest.raises(portals.PortalError, match="not used by feed"):
            portals.validate_portal(good(kind="feed"))
        with pytest.raises(portals.PortalError, match="not used by schema_org"):
            portals.validate_portal(good(kind="schema_org"))

    def test_limits_are_capped(self):
        with pytest.raises(portals.PortalError, match="max_pages"):
            portals.validate_portal(good(request={"url": "https://api.demo.example/j?p={page}", "max_pages": 50}))
        with pytest.raises(portals.PortalError, match="delay_seconds"):
            portals.validate_portal(good(request={"url": "https://api.demo.example/j", "delay_seconds": 0.1}))

    def test_headers_are_restricted(self):
        ok = {"url": "https://api.demo.example/j", "headers": {"Authorization": "Bearer {api_key}"}}
        portals.validate_portal(good(request=ok))
        for headers in ({"Host": "x"}, {"X-A": "{query}"}, {"X-A": "a\nb"}):
            with pytest.raises(portals.PortalError):
                portals.validate_portal(good(request={"url": "https://api.demo.example/j", "headers": headers}))
        with pytest.raises(portals.PortalError, match="only allowed in headers"):
            portals.validate_portal(good(request={"url": "https://api.demo.example/j?k={api_key}"}))

    def test_bad_files_are_skipped_not_raised(self, tmp_path):
        user = tmp_path / "portals"
        user.mkdir()
        (user / "broken.json").write_text("{not json")
        (user / "extra.json").write_text(json.dumps(good(id="extra", surprise=True)))
        (user / "fine.json").write_text(json.dumps(good(id="fine")))
        (user / "remotive.json").write_text(json.dumps(good(id="remotive")))  # cannot replace a bundled id
        (user / "remoteok.json").write_text(json.dumps(good(id="remoteok")))  # nor a built-in source
        loaded = portals.load_portals(user_dir=user, reserved={"remoteok"})
        assert "fine" in loaded and "extra" not in loaded
        assert loaded["remotive"]["homepage"] == "https://remotive.com/"
        assert "remoteok" not in loaded

    def test_oversized_file_is_skipped(self, tmp_path):
        big = tmp_path / "big.json"
        big.write_text(json.dumps(good(name="x" * (portals.MAX_FILE_BYTES + 1))))
        with pytest.raises(portals.PortalError, match="larger than"):
            portals.load_portal_file(big)


# ----- request safety -----

class TestRequestSafety:
    def test_redirect_to_a_foreign_host_is_blocked(self, monkeypatch):
        def handler(request):
            if request.url.host == "api.demo.example":
                return httpx.Response(302, headers={"Location": "https://evil.example/steal"})
            return httpx.Response(200, json={"data": {"jobs": []}})

        seen = serve(monkeypatch, handler)
        assert run(portals.fetch_portal(good(), KEYWORDS)) == []
        assert seen == ["https://api.demo.example/jobs?q=engineer&page=1"]  # never reached evil.example

    def test_redirect_to_http_is_blocked(self, monkeypatch):
        seen = serve(monkeypatch, lambda r: httpx.Response(
            301, headers={"Location": "http://api.demo.example/jobs"}))
        assert run(portals.fetch_portal(good(), KEYWORDS)) == []
        assert len(seen) == 1

    def test_redirect_within_the_allowlist_is_followed(self, monkeypatch):
        body = {"data": {"jobs": [{"title": "Engineer", "link": "https://x.example/1", "org": {"name": "Acme"}}]}}

        def handler(request):
            if request.url.path == "/jobs":
                return httpx.Response(301, headers={"Location": "/v2/jobs?q=engineer"})
            return httpx.Response(200, json=body)

        serve(monkeypatch, handler)
        p = good(request={"url": "https://api.demo.example/jobs?q={query}"})
        assert [j["title"] for j in run(portals.fetch_portal(p, KEYWORDS))] == ["Engineer"]

    def test_redirect_loop_is_cut_off(self, monkeypatch):
        seen = serve(monkeypatch, lambda r: httpx.Response(302, headers={"Location": str(r.url)}))
        assert run(portals.fetch_portal(good(), KEYWORDS)) == []
        assert len(seen) == portals.MAX_REDIRECTS + 1

    def test_private_resolution_is_refused_before_connecting(self, monkeypatch):
        from backend.job_sources import manual
        monkeypatch.undo()  # drop the guard stub
        monkeypatch.setattr(manual.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
        monkeypatch.setattr(portals.asyncio, "sleep", lambda _: None)
        seen = serve(monkeypatch, lambda r: httpx.Response(200, json={}))
        assert run(portals.fetch_portal(good(), KEYWORDS)) == []
        assert seen == []

    def test_request_budget_is_conservative(self, monkeypatch):
        seen = serve(monkeypatch, lambda r: httpx.Response(200, json={"data": {"jobs": [
            {"title": f"Engineer {r.url}", "link": f"https://x.example/{len(str(r.url))}{r.url.query!s}"}]}}))
        cfg = {"search": {"keywords": ["a", "b", "c", "d", "e"]}}
        run(portals.fetch_portal(good(request={"url": "https://api.demo.example/j?q={query}&p={page}", "max_pages": 3}), cfg))
        assert len(seen) == portals.MAX_QUERIES * 3
        assert not any("q=d" in u or "q=e" in u for u in seen)

    def test_http_error_keeps_earlier_results(self, monkeypatch):
        def handler(request):
            if "page=2" in str(request.url):
                return httpx.Response(500)
            return httpx.Response(200, json={"data": {"jobs": [{"title": "Engineer", "link": "https://x.example/1"}]}})

        serve(monkeypatch, handler)
        assert len(run(portals.fetch_portal(good(), KEYWORDS))) == 1

    def test_min_interval_is_honoured(self, monkeypatch):
        seen = serve(monkeypatch, lambda r: httpx.Response(200, json={"data": {"jobs": []}}))
        p = good(min_interval_minutes=60)
        run(portals.fetch_portal(p, KEYWORDS))
        n = len(seen)
        run(portals.fetch_portal(p, KEYWORDS))
        assert len(seen) == n  # second run inside the interval makes no request
        state = portals._read_state()
        state["demo"] = time.time() - 3700
        portals._state_path().write_text(json.dumps(state))
        run(portals.fetch_portal(p, KEYWORDS))
        assert len(seen) > n

    def test_api_key_header_only_when_configured(self, monkeypatch):
        got = []
        serve(monkeypatch, lambda r: (got.append(r.headers.get("authorization")),
                                      httpx.Response(200, json={"data": {"jobs": []}}))[1])
        p = good(request={"url": "https://api.demo.example/j", "headers": {"Authorization": "Bearer {api_key}"}})
        run(portals.fetch_portal(p, KEYWORDS))
        run(portals.fetch_portal(p, {**KEYWORDS, "api_keys": {"demo_api_key": "sekret"}}))
        assert got == [None, "Bearer sekret"]


# ----- bundled portals, mapped from recorded fixtures -----

def _fixture_portal(monkeypatch, pid, fixture):
    body = (FIXTURES / fixture).read_text()
    seen = serve(monkeypatch, lambda r: httpx.Response(200, text=body, headers={"content-type": "application/json"}))
    portal = portals.load_portals(user_dir=Path("/nonexistent"))[pid]
    return portal, seen


class TestBundledPortals:
    def test_remotive(self, monkeypatch):
        portal, seen = _fixture_portal(monkeypatch, "remotive", "remotive.json")
        jobs = run(portals.fetch_portal(portal, {"search": {"keywords": ["content", "rails"]}}))
        assert seen == ["https://remotive.com/api/remote-jobs?limit=100"]
        assert [j["title"] for j in jobs] == ["Content Reviewer - United States", "Tech Lead Full-Stack Rails Engineer"]
        first = jobs[0]
        assert first["source"] == "remotive" and first["external_id"] == "2091177"
        assert first["company"] == "TELUS Digital" and first["location"] == "USA"
        assert first["url"].startswith("https://remotive.com/remote-jobs/")
        assert first["date_posted"] == "2026-11-19T14:58:42" and first["is_remote"] is True
        assert first["tags"][:2] == ["android", "ios"]
        assert set(first) >= {"source", "external_id", "title", "company", "location", "url", "description",
                              "salary_min", "salary_max", "tags", "date_posted", "is_remote"}

    def test_remotive_filters_by_keyword_locally(self, monkeypatch):
        portal, _ = _fixture_portal(monkeypatch, "remotive", "remotive.json")
        jobs = run(portals.fetch_portal(portal, {"search": {"keywords": ["rails"]}}))
        assert [j["external_id"] for j in jobs] == ["2069746"]

    def test_himalayas(self, monkeypatch):
        portal, seen = _fixture_portal(monkeypatch, "himalayas", "himalayas.json")
        jobs = run(portals.fetch_portal(portal, {"search": {"keywords": ["python"]}}))
        assert seen[0] == "https://himalayas.app/jobs/api/search?q=python&sort=recent&page=1"
        assert len(seen) == 2  # max_pages
        assert len(jobs) == 2  # the same two postings on both pages dedupe
        first, second = jobs
        assert first["title"] == "Senior Software Engineer" and first["company"] == "Alien Intellect"
        assert first["url"].endswith("?utm_source=api")  # applicationLink, unchanged
        assert first["external_id"].startswith("https://himalayas.app/companies/alien-intellect/")
        assert first["date_posted"] == "1793013430"
        # USD 2000-3000 monthly -> annual
        assert (first["salary_min"], first["salary_max"], first["salary_period"]) == (24000, 36000, "annual")
        assert first["location"] == "Remote"
        assert second["location"] == "Canada, United Kingdom, United States"
        assert second["salary_min"] is None

    def test_jobicy(self, monkeypatch):
        portal, seen = _fixture_portal(monkeypatch, "jobicy", "jobicy.json")
        jobs = run(portals.fetch_portal(portal, {"search": {"keywords": ["incident"]}}))
        assert seen == ["https://jobicy.com/api/jobs?count=50&tag=incident"]
        assert len(jobs) == 2
        first = jobs[0]
        assert (first["source"], first["external_id"], first["title"]) == ("jobicy", "163952", "NOC Incident Manager")
        assert first["url"] == "https://jobicy.com/jobs/163952-noc-incident-manager"
        assert first["company"] == "Airalo" and first["location"] == "Spain" and first["is_remote"] is True
        assert first["salary_min"] is None  # EUR figures are not mixed into the USD salary floor
        assert "<" not in first["description"]


# ----- other kinds -----

RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Platform Engineer</title><link>https://feed.example/jobs/1</link>
<description>&lt;p&gt;Build things&lt;/p&gt;</description><pubDate>Mon, 23 Nov 2026 10:00:00 GMT</pubDate>
<author>Acme</author></item>
<item><title>Chef</title><link>https://feed.example/jobs/2</link><description>Cook</description></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>SRE Engineer</title><link href="https://feed.example/a/1"/><id>tag:feed.example,1</id>
<summary>Keep it up</summary><updated>2026-11-20T00:00:00Z</updated></entry></feed>"""

PAGE = """<html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
{"@type":"JobPosting","title":"Data Engineer","url":"https://pg.example/j/1",
 "hiringOrganization":{"@type":"Organization","name":"Acme"},
 "description":"<p>Pipelines</p>","datePosted":"2026-11-20","jobLocationType":"TELECOMMUTE",
 "baseSalary":{"value":{"minValue":100000,"maxValue":120000,"unitText":"YEAR"}}},
{"@type":"JobPosting","title":"Cook","hiringOrganization":"Diner"}]}</script>
<script type="application/ld+json">not json</script>
<script type="application/ld+json">{"@type":"JobPosting","title":"Engineer II","url":"https://pg.example/j/3"}</script>
</head></html>"""


class TestOtherKinds:
    def test_feed(self, monkeypatch):
        serve(monkeypatch, lambda r: httpx.Response(200, text=RSS))
        p = good(kind="feed", hosts=["feed.example"], request={"url": "https://feed.example/rss"},
                 defaults={"remote": True})
        del p["items"], p["fields"]
        jobs = run(portals.fetch_portal(p, KEYWORDS))
        assert [(j["title"], j["company"], j["url"], j["date_posted"]) for j in jobs] == [
            ("Platform Engineer", "Acme", "https://feed.example/jobs/1", "Mon, 23 Nov 2026 10:00:00 GMT")]
        assert jobs[0]["description"] == "Build things" and jobs[0]["is_remote"] is True

    def test_feed_field_mapping_and_atom(self, monkeypatch):
        serve(monkeypatch, lambda r: httpx.Response(200, text=ATOM))
        p = good(kind="feed", hosts=["feed.example"], request={"url": "https://feed.example/atom"})
        del p["items"], p["fields"]
        jobs = run(portals.fetch_portal(p, KEYWORDS))
        assert [(j["title"], j["url"], j["external_id"]) for j in jobs] == [
            ("SRE Engineer", "https://feed.example/a/1", "tag:feed.example,1")]

    def test_feed_with_entities_is_refused(self, monkeypatch):
        serve(monkeypatch, lambda r: httpx.Response(200, text='<!DOCTYPE x [<!ENTITY a "b">]><rss/>'))
        p = good(kind="feed", hosts=["feed.example"], request={"url": "https://feed.example/rss"})
        del p["items"], p["fields"]
        assert run(portals.fetch_portal(p, KEYWORDS)) == []

    def test_schema_org(self, monkeypatch):
        serve(monkeypatch, lambda r: httpx.Response(200, text=PAGE))
        p = good(kind="schema_org", hosts=["pg.example"], request={"url": "https://pg.example/careers"})
        del p["items"], p["fields"]
        jobs = run(portals.fetch_portal(p, KEYWORDS))
        assert [j["title"] for j in jobs] == ["Data Engineer", "Engineer II"]
        first = jobs[0]
        assert (first["company"], first["url"], first["date_posted"]) == ("Acme", "https://pg.example/j/1", "2026-11-20")
        assert (first["salary_min"], first["salary_max"], first["salary_period"]) == (100000, 120000, "annual")
        assert first["description"] == "Pipelines" and first["is_remote"] is True
        assert jobs[1]["url"] == "https://pg.example/j/3"

    def test_javascript_urls_are_dropped(self, monkeypatch):
        serve(monkeypatch, lambda r: httpx.Response(200, json={"data": {"jobs": [
            {"title": "Engineer", "link": "javascript:alert(1)"}, {"title": "Engineer", "link": "https://x.example/ok"}]}}))
        assert [j["url"] for j in run(portals.fetch_portal(good(), KEYWORDS))] == ["https://x.example/ok"]

    def test_exclude_keywords_apply(self, monkeypatch):
        serve(monkeypatch, lambda r: httpx.Response(200, json={"data": {"jobs": [
            {"title": "Staff Engineer", "link": "https://x.example/1"}, {"title": "Engineer", "link": "https://x.example/2"}]}}))
        cfg = {"search": {"keywords": ["engineer"], "exclude_keywords": ["staff"]}}
        assert [j["url"] for j in run(portals.fetch_portal(good(), cfg))] == ["https://x.example/2"]

    def test_no_keywords_means_no_request(self, monkeypatch):
        seen = serve(monkeypatch, lambda r: httpx.Response(200, json={}))
        assert run(portals.fetch_portal(good(), {"search": {}})) == []
        assert seen == []


# ----- wiring: opt-in config, setup state, orchestrator isolation -----

class TestWiring:
    def test_portals_are_listed_and_off_by_default(self):
        d = {x["name"]: x for x in job_sources.source_details({})}
        for pid in ("remotive", "himalayas", "jobicy"):
            assert pid in job_sources.get_source_names()
            assert d[pid]["kind"] == "portal" and not d[pid]["default_on"]
            assert d[pid]["label"] and d[pid]["note"]

    def test_config_example_ships_them_disabled(self):
        import yaml
        root = Path(__file__).resolve().parents[2]
        cfg = yaml.safe_load((root / "config.example.yaml").read_text())
        assert cfg["search"]["portals"] == {pid: {"enabled": False} for pid in ("remotive", "himalayas", "jobicy")}

    def test_enabling_turns_the_default_on(self):
        cfg = {"search": {"portals": {"jobicy": {"enabled": True}, "remotive": {"enabled": False}}}}
        d = {x["name"]: x for x in job_sources.source_details(cfg)}
        assert d["jobicy"]["default_on"] and not d["remotive"]["default_on"]

    @pytest.mark.asyncio
    async def test_default_run_skips_disabled_portals(self):
        called = []

        async def fake(portal, config):
            called.append(portal["id"])
            return []

        cfg = {"search": {"keywords": ["x"], "portals": {"jobicy": {"enabled": True}}}}
        with patch("backend.job_sources.SOURCES", []), patch.object(portals, "fetch_portal", fake):
            await job_sources.fetch_all_jobs(cfg)
            assert called == ["jobicy"]
            called.clear()
            await job_sources.fetch_all_jobs({"search": {"keywords": ["x"]}})
            assert called == []  # nothing enabled -> nothing fetched
            await job_sources.fetch_all_jobs(cfg, sources=["remotive"])
            assert called == ["remotive"]  # an explicit pick is honoured

    @pytest.mark.asyncio
    async def test_a_failing_portal_does_not_break_the_run(self):
        async def fake(portal, config):
            if portal["id"] == "himalayas":
                raise RuntimeError("kaput")
            return [{
                "source": portal["id"], "external_id": "1", "title": f"Security Engineer {portal['id']}", "company": "Acme",
                "location": "Remote", "url": f"https://{portal['id']}.example/1", "description": "",
                "salary_min": None, "salary_max": None, "tags": [], "date_posted": "", "is_remote": True,
            }]

        cfg = {"search": {"keywords": ["security"], "locations": ["Remote"],
                          "portals": {p: {"enabled": True} for p in ("remotive", "himalayas", "jobicy")}}}
        progress: dict = {}
        with patch("backend.job_sources.SOURCES", []), patch.object(portals, "fetch_portal", fake):
            jobs = await job_sources.fetch_all_jobs(cfg, on_progress=progress.update)
        assert sorted(j["source"] for j in jobs) == ["jobicy", "remotive"]
        assert progress.get("sources_failed") == ["himalayas"]

    def test_user_portal_appears_as_a_source(self, tmp_path):
        user = tmp_path / "data" / "portals"
        user.mkdir(parents=True)
        (user / "mine.json").write_text(json.dumps(good(id="mine")))
        assert "mine" in job_sources.get_source_names()
