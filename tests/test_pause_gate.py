"""
tests/test_pause_gate.py — Offline tests for the pause-gate mechanism.

All browser and LLM interactions are mocked.  No network, no Playwright,
no LM Studio required.

Test cases
----------
a) wait_if_paused() returns immediately when not paused
b) wait_if_paused() blocks then unblocks when set_paused(False) is called
   from a concurrent coroutine after ~0.1 s
c) wait_if_paused() raises CancelledError when _force_stop_event is set
   while the caller is blocked
d) A mock adapter coroutine that calls wait_if_paused() does NOT proceed
   until set_paused(False) is called (confirm via side-effect flag)
"""

from __future__ import annotations

import asyncio
import time

import pytest

# ---------------------------------------------------------------------------
# Helpers — patch the orchestrator module state between tests
# ---------------------------------------------------------------------------

import backend.auto_apply.orchestrator as _orc


def _reset():
    """Restore clean state between tests."""
    _orc._paused = False
    _orc._force_stop_event.clear()


# Make sure every test starts clean
@pytest.fixture(autouse=True)
def clean_state():
    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# Import the function under test after patch fixtures are in place
# ---------------------------------------------------------------------------

from backend.auto_apply.utils.browser_helpers import wait_if_paused  # noqa: E402


# ---------------------------------------------------------------------------
# Test a: returns immediately when not paused
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_returns_immediately_when_not_paused():
    """wait_if_paused() must return without blocking when is_paused() is False."""
    assert not _orc.is_paused()
    t0 = time.monotonic()
    await wait_if_paused()
    elapsed = time.monotonic() - t0
    # Should return in well under 50 ms — no sleep involved
    assert elapsed < 0.1


# ---------------------------------------------------------------------------
# Test b: blocks then unblocks when resumed from a concurrent coroutine
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_blocks_then_unblocks_on_resume():
    """wait_if_paused() should block while paused, then return when resumed."""
    _orc.set_paused(True)
    resumed_at: list[float] = []

    async def _resume_after(delay: float) -> None:
        await asyncio.sleep(delay)
        _orc.set_paused(False)

    t0 = time.monotonic()
    resume_task = asyncio.create_task(_resume_after(0.1))
    await wait_if_paused(poll_interval=0.05)
    elapsed = time.monotonic() - t0

    # Should have blocked for at least 0.1 s (the resume delay)
    assert elapsed >= 0.09, f"Unblocked too early: {elapsed:.3f}s"
    # Should not have waited more than 1 s total
    assert elapsed < 1.0, f"Took too long to unblock: {elapsed:.3f}s"

    await resume_task  # clean up


# ---------------------------------------------------------------------------
# Test c: raises CancelledError when _force_stop_event is set while paused
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_raises_cancelled_on_force_stop():
    """wait_if_paused() must raise CancelledError when force-stop is signalled."""
    _orc.set_paused(True)

    async def _force_after(delay: float) -> None:
        await asyncio.sleep(delay)
        _orc._force_stop_event.set()

    force_task = asyncio.create_task(_force_after(0.1))

    with pytest.raises(asyncio.CancelledError, match="Force stopped"):
        await wait_if_paused(poll_interval=0.05)

    await force_task  # clean up


# ---------------------------------------------------------------------------
# Test d: mock adapter does NOT proceed until resumed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mock_adapter_halts_until_resumed():
    """
    A coroutine that calls wait_if_paused() at its entry point must not
    advance past that point until set_paused(False) is called.
    """
    proceeded = False

    async def mock_adapter() -> str:
        await wait_if_paused(poll_interval=0.05)
        # This line must not be reached while still paused
        nonlocal proceeded
        proceeded = True
        return "done"

    _orc.set_paused(True)

    async def _resume_and_check():
        # Let the adapter get stuck on wait_if_paused first
        await asyncio.sleep(0.1)
        assert not proceeded, "Adapter proceeded before resume!"
        _orc.set_paused(False)

    checker = asyncio.create_task(_resume_and_check())
    result = await mock_adapter()

    assert proceeded, "Adapter never proceeded after resume"
    assert result == "done"
    await checker
