"""
tests/test_posting_quality.py

Unit tests for the ghost-job / posting-quality heuristics. Pure functions —
no network, no LLM, no database required. Verifies:
  - A pristine posting scores 100 with no fired signals.
  - Reposted + stale postings accumulate the expected penalties.
  - Missing descriptions take the heavy penalty; short ones the mild one.
  - Salary transparency: penalty without any salary data, bonus (capped at
    100) with a real posted salary, no signal when only an estimate exists.
  - Title red flags and buzzword density fire as mild penalties.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services import posting_quality as pq


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def _pristine_job(**overrides) -> dict:
    job = {
        "title": "Senior Security Engineer",
        "company": "Acme Corp",
        "description": "x" * 500,
        "salary_min": None,
        "salary_max": None,
        "estimated_salary_min": 120000,
        "estimated_salary_max": 150000,
        "date_posted": _days_ago(3),
        "times_seen": 1,
    }
    job.update(overrides)
    return job


def _signal_names(report: dict) -> set[str]:
    return {s["signal"] for s in report["signals"]}


class TestPristineJob:
    def test_score_100_no_signals(self):
        report = pq.compute_quality_report(_pristine_job())
        assert report["score"] == 100
        assert report["signals"] == []

    def test_report_shape(self):
        report = pq.compute_quality_report(_pristine_job(times_seen=2))
        assert set(report.keys()) == {"score", "signals"}
        for sig in report["signals"]:
            assert set(sig.keys()) == {"signal", "impact", "detail"}
            assert isinstance(sig["impact"], int)


class TestRepostAndStaleness:
    def test_mild_repost(self):
        report = pq.compute_quality_report(_pristine_job(times_seen=2))
        assert report["score"] == 100 + pq.REPOST_MILD_PENALTY
        assert _signal_names(report) == {"repost"}

    def test_heavy_repost(self):
        report = pq.compute_quality_report(_pristine_job(times_seen=5))
        assert report["score"] == 100 + pq.REPOST_HEAVY_PENALTY

    def test_stale_30(self):
        report = pq.compute_quality_report(_pristine_job(date_posted=_days_ago(45)))
        assert report["score"] == 100 + pq.STALE_30_PENALTY
        assert _signal_names(report) == {"stale"}

    def test_stale_60(self):
        report = pq.compute_quality_report(_pristine_job(date_posted=_days_ago(90)))
        assert report["score"] == 100 + pq.STALE_60_PENALTY

    def test_reposted_stale_job_stacks_penalties(self):
        report = pq.compute_quality_report(
            _pristine_job(times_seen=4, date_posted=_days_ago(90))
        )
        assert report["score"] == 100 + pq.REPOST_HEAVY_PENALTY + pq.STALE_60_PENALTY
        assert _signal_names(report) == {"repost", "stale"}

    def test_unparseable_date_skips_staleness(self):
        report = pq.compute_quality_report(
            _pristine_job(date_posted="Posted 3 days ago")
        )
        assert report["score"] == 100
        assert "stale" not in _signal_names(report)

    def test_missing_times_seen_treated_as_one(self):
        job = _pristine_job()
        del job["times_seen"]
        report = pq.compute_quality_report(job)
        assert "repost" not in _signal_names(report)


class TestDescription:
    def test_missing_description_heavy_penalty(self):
        report = pq.compute_quality_report(_pristine_job(description=""))
        assert report["score"] == 100 + pq.MISSING_DESC_PENALTY
        assert _signal_names(report) == {"missing_description"}

    def test_short_description_mild_penalty(self):
        report = pq.compute_quality_report(_pristine_job(description="Great job!"))
        assert report["score"] == 100 + pq.SHORT_DESC_PENALTY
        assert _signal_names(report) == {"short_description"}

    def test_buzzword_density(self):
        desc = (
            "We need a rockstar ninja to join our fast-paced environment. "
            "You will wear many hats. " + "x" * 300
        )
        report = pq.compute_quality_report(_pristine_job(description=desc))
        assert "buzzwords" in _signal_names(report)
        buzz = next(s for s in report["signals"] if s["signal"] == "buzzwords")
        # 4 distinct buzzwords, but the penalty is capped
        assert buzz["impact"] == pq.BUZZWORD_PENALTY_CAP

    def test_score_floor_zero(self):
        report = pq.compute_quality_report(
            {
                "title": "URGENT!!! Apply now ⚡",
                "description": "",
                "date_posted": _days_ago(120),
                "times_seen": 10,
            }
        )
        assert 0 <= report["score"] <= 100


class TestSalaryTransparency:
    def test_no_salary_no_estimate_penalty(self):
        report = pq.compute_quality_report(
            _pristine_job(estimated_salary_min=None, estimated_salary_max=None)
        )
        assert report["score"] == 100 + pq.NO_SALARY_PENALTY
        assert _signal_names(report) == {"no_salary"}

    def test_estimate_only_no_signal(self):
        report = pq.compute_quality_report(_pristine_job())
        assert "no_salary" not in _signal_names(report)
        assert "salary_posted" not in _signal_names(report)

    def test_posted_salary_bonus_capped_at_100(self):
        report = pq.compute_quality_report(
            _pristine_job(salary_min=110000, salary_max=140000)
        )
        assert report["score"] == 100  # bonus fired but score is capped
        assert _signal_names(report) == {"salary_posted"}

    def test_posted_salary_bonus_offsets_penalty(self):
        report = pq.compute_quality_report(
            _pristine_job(salary_min=110000, times_seen=2)
        )
        assert report["score"] == min(
            100, 100 + pq.REPOST_MILD_PENALTY + pq.POSTED_SALARY_BONUS
        )


class TestTitleRedFlags:
    def test_urgency_terms(self):
        report = pq.compute_quality_report(
            _pristine_job(title="URGENT: Security Analyst - Immediate Start")
        )
        assert "title_urgency" in _signal_names(report)

    def test_emoji_noise(self):
        report = pq.compute_quality_report(
            _pristine_job(title="Security Engineer 🚀🔥")
        )
        assert "title_noise" in _signal_names(report)

    def test_excessive_punctuation(self):
        report = pq.compute_quality_report(
            _pristine_job(title="Amazing opportunity!!! Apply today???")
        )
        assert "title_noise" in _signal_names(report)

    def test_clean_title_no_flags(self):
        report = pq.compute_quality_report(_pristine_job())
        assert not _signal_names(report) & {"title_urgency", "title_noise"}
