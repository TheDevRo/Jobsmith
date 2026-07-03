"""
conftest.py — shared fixtures for tests/auto_apply/

Automatically patches playwright_stealth.stealth_async so that tests which
exercise backend/job_sources/indeed.py do not require a real browser or the
stealth library to make async calls against a mock Playwright page.
"""

from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _mock_stealth_async(monkeypatch):
    """Replace _stealth_async in indeed.py with a no-op AsyncMock for all tests."""
    monkeypatch.setattr(
        "backend.job_sources.indeed._stealth_async",
        AsyncMock(),
        raising=False,
    )
