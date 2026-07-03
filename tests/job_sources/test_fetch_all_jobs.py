"""
tests/job_sources/test_fetch_all_jobs.py

Tests for the concurrent fetch_all_jobs orchestrator: partial-result
recovery, per-source isolation (timeout / blocked / failed), and dedup.
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import patch

import pytest

from backend.job_sources import SourceBlockedError, fetch_all_jobs


def _src(name: str, fetch):
    """Build a fake source module with an async fetch_jobs."""
    mod = types.SimpleNamespace()
    mod.fetch_jobs = fetch
    return (name, mod)


def _job(ext_id: str, source: str, **overrides) -> dict:
    base = {
        "source": source,
        "external_id": ext_id,
        "title": f"Security Engineer {ext_id}",
        "company": f"Company {ext_id}",
        "location": "Remote",
        "url": f"https://example.com/{source}/{ext_id}",
        "description": "desc",
        "salary_min": None,
        "salary_max": None,
        "salary_period": "unknown",
        "tags": [],
        "date_posted": "",
        "is_remote": True,
    }
    base.update(overrides)
    return base


_CONFIG = {"search": {"keywords": ["security"], "locations": ["Remote"]}}


class TestFetchAllJobs:

    @pytest.mark.asyncio
    async def test_one_source_failing_does_not_affect_others(self):
        async def ok(config):
            return [_job("a", "good")]

        async def boom(config):
            raise RuntimeError("kaput")

        sources = [_src("good", ok), _src("bad", boom)]
        progress: dict = {}

        with patch("backend.job_sources.SOURCES", sources):
            jobs = await fetch_all_jobs(_CONFIG, sources=["good", "bad"],
                                        on_progress=progress.update)

        assert [j["external_id"] for j in jobs] == ["a"]
        assert progress.get("sources_failed") == ["bad"]

    @pytest.mark.asyncio
    async def test_blocked_source_reported_separately(self):
        async def ok(config):
            return [_job("a", "good")]

        async def blocked(config):
            raise SourceBlockedError("403")

        sources = [_src("good", ok), _src("walled", blocked)]
        progress: dict = {}

        with patch("backend.job_sources.SOURCES", sources):
            jobs = await fetch_all_jobs(_CONFIG, sources=["good", "walled"],
                                        on_progress=progress.update)

        assert len(jobs) == 1
        assert progress.get("sources_blocked") == ["walled"]
        assert "sources_failed" not in progress

    @pytest.mark.asyncio
    async def test_timed_out_source_keeps_others_results(self):
        async def ok(config):
            return [_job("a", "fast")]

        async def hang(config):
            await asyncio.sleep(30)
            return [_job("z", "slow")]

        sources = [_src("fast", ok), _src("slow", hang)]
        progress: dict = {}

        with patch("backend.job_sources.SOURCES", sources), \
             patch.dict("backend.job_sources._SOURCE_TIMEOUTS", {"slow": 1, "fast": 5}):
            jobs = await fetch_all_jobs(_CONFIG, sources=["fast", "slow"],
                                        on_progress=progress.update)

        assert [j["external_id"] for j in jobs] == ["a"]
        assert progress.get("sources_timed_out") == ["slow"]

    @pytest.mark.asyncio
    async def test_partial_collector_extended_as_sources_complete(self):
        async def ok(config):
            return [_job("a", "good")]

        async def hang(config):
            await asyncio.sleep(30)
            return []

        collector: list = []
        sources = [_src("good", ok), _src("slow", hang)]

        async def run():
            with patch("backend.job_sources.SOURCES", sources), \
                 patch.dict("backend.job_sources._SOURCE_TIMEOUTS", {"slow": 60, "good": 5}):
                await fetch_all_jobs(_CONFIG, sources=["good", "slow"],
                                     _partial_collector=collector)

        task = asyncio.create_task(run())
        # The fast source's jobs must land in the collector while the slow
        # one is still running — that's what makes outer-timeout recovery work.
        for _ in range(50):
            if collector:
                break
            await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert [j["external_id"] for j in collector] == ["a"]

    @pytest.mark.asyncio
    async def test_url_dedup_across_sources(self):
        job = _job("x", "one")

        async def src_one(config):
            return [dict(job)]

        async def src_two(config):
            return [dict(job)]

        sources = [_src("one", src_one), _src("two", src_two)]

        with patch("backend.job_sources.SOURCES", sources):
            jobs = await fetch_all_jobs(_CONFIG, sources=["one", "two"])

        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_identity_dedup_across_sources(self):
        # Same title+company+location, different URLs (LinkedIn vs Indeed case)
        async def src_li(config):
            return [_job("li-1", "linkedin", title="SOC Analyst", company="Acme",
                         url="https://linkedin.com/jobs/1")]

        async def src_in(config):
            return [_job("in-1", "indeed", title="SOC Analyst", company="Acme",
                         url="https://indeed.com/viewjob?jk=1")]

        sources = [_src("linkedin", src_li), _src("indeed", src_in)]

        with patch("backend.job_sources.SOURCES", sources):
            jobs = await fetch_all_jobs(_CONFIG, sources=["linkedin", "indeed"])

        assert len(jobs) == 1

    @pytest.mark.asyncio
    async def test_cancel_event_returns_completed_sources(self):
        cancel = asyncio.Event()

        async def ok(config):
            return [_job("a", "good")]

        async def hang(config):
            await asyncio.sleep(30)
            return [_job("z", "slow")]

        sources = [_src("good", ok), _src("slow", hang)]

        async def trigger_cancel():
            await asyncio.sleep(1.5)
            cancel.set()

        with patch("backend.job_sources.SOURCES", sources), \
             patch.dict("backend.job_sources._SOURCE_TIMEOUTS", {"slow": 60, "good": 5}):
            trigger = asyncio.create_task(trigger_cancel())
            jobs = await fetch_all_jobs(_CONFIG, sources=["good", "slow"],
                                        cancel_event=cancel)
            await trigger

        assert [j["external_id"] for j in jobs] == ["a"]

    @pytest.mark.asyncio
    async def test_zero_streak_source_reported_as_suspect(self):
        """A source that used to return jobs but has hit 0 for three straight
        runs is flagged via sources_suspect — silent parser breakage must not
        look like 'no new postings'."""
        async def nonempty(config):
            return [_job("a", "flaky")]

        async def empty(config):
            return []

        # One healthy run establishes history...
        with patch("backend.job_sources.SOURCES", [_src("flaky", nonempty)]):
            await fetch_all_jobs(_CONFIG, sources=["flaky"])

        # ...then three consecutive zero-job runs trip the flag.
        progress: dict = {}
        with patch("backend.job_sources.SOURCES", [_src("flaky", empty)]):
            for _ in range(3):
                progress = {}
                await fetch_all_jobs(_CONFIG, sources=["flaky"],
                                     on_progress=progress.update)

        assert progress.get("sources_suspect") == ["flaky"]

    @pytest.mark.asyncio
    async def test_never_productive_source_not_suspect(self):
        """A source with no nonzero history is unconfigured, not broken."""
        async def empty(config):
            return []

        progress: dict = {}
        with patch("backend.job_sources.SOURCES", [_src("barren", empty)]):
            for _ in range(5):
                progress = {}
                await fetch_all_jobs(_CONFIG, sources=["barren"],
                                     on_progress=progress.update)

        assert "sources_suspect" not in progress

    @pytest.mark.asyncio
    async def test_known_ids_passed_to_sources_that_accept_them(self):
        captured: dict = {}

        async def wants_known(config, known_ids=None):
            captured["known_ids"] = known_ids
            return []

        sources = [_src("greenhouse", wants_known)]

        async def fake_known(source):
            return {f"{source}-existing"}

        with patch("backend.job_sources.SOURCES", sources), \
             patch("backend.database.get_known_external_ids", new=fake_known):
            await fetch_all_jobs(_CONFIG, sources=["greenhouse"])

        # greenhouse module covers both greenhouse and lever DB sources
        assert captured["known_ids"] == {"greenhouse-existing", "lever-existing"}
