"""
Shared pytest fixtures.

Starlette's TestClient presents a client host of "testclient", which the
dashboard auth gate (backend/routers/_auth.py) correctly classifies as an
off-machine caller — so without this every API test in the suite would 401.
Rather than paper over that in the gate (a real caller from off-machine *should*
be challenged), the suite opts out explicitly and the gate gets exercised
head-on in tests/test_api_auth.py.
"""

import pytest


@pytest.fixture(autouse=True)
def _disable_api_auth(monkeypatch):
    """Let business-logic tests talk to the API without minting a token."""
    monkeypatch.setenv("JOBSMITH_ALLOW_INSECURE", "1")
