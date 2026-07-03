"""
Shared fixtures for job_sources tests.
"""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_source_stats(tmp_path):
    """Keep the zero-streak stats file out of the real data/ directory —
    orchestrator tests run fake sources whose outcomes must not pollute
    (or be polluted by) the user's runtime stats."""
    with patch("backend.job_sources._SOURCE_STATS_PATH", tmp_path / "source_stats.json"):
        yield
