"""
Apple Intelligence (on-device) support: the sidecar supervisor
(backend/apple_bridge.py), the sentinel routing in ai_engine, and the
/api/ai/status merge.

The Swift sidecar is stood in for by tests/fixtures/fake_apple_bridge.py — a
real child process speaking the real handshake — so spawn/crash/restart/
terminate are exercised end to end on any platform, not mocked away.
"""

from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import ai_engine
from backend import app_state as state
from backend import apple_bridge
from backend.routers import settings as settings_router

FAKE_BRIDGE = Path(__file__).parent / "fixtures" / "fake_apple_bridge.py"
SENTINEL = apple_bridge.SENTINEL_MODEL


@pytest.fixture
def fake_bridge_env(monkeypatch):
    """Point discovery at the fake sidecar and pretend the platform qualifies."""
    monkeypatch.setenv("JOBSMITH_APPLE_BRIDGE_BIN", str(FAKE_BRIDGE))
    monkeypatch.setattr(apple_bridge, "platform_supported", lambda: True)
    monkeypatch.setenv("FAKE_BRIDGE_MODE", "ok")
    monkeypatch.setenv("FAKE_BRIDGE_AVAILABLE", "1")


@pytest.fixture
def bridge(fake_bridge_env):
    b = apple_bridge.AppleBridge()
    yield b
    # Sync teardown: the test's event loop is already gone, so reap the child
    # directly rather than awaiting stop() on a loop it isn't attached to.
    proc = b._proc
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _cfg(**tiers):
    return {"ai": {"base_url": "http://endpoint.invalid:1234/v1", "api_key": "k",
                   "models": {t: {"model": m} for t, m in tiers.items()}}}


# ---------------------------------------------------------------- discovery
class TestBinaryDiscovery:
    def test_env_override_wins(self, monkeypatch, tmp_path):
        exe = tmp_path / "jobsmith-apple-ai"
        exe.write_text("#!/bin/sh\n")
        monkeypatch.setenv("JOBSMITH_APPLE_BRIDGE_BIN", str(exe))
        assert apple_bridge.find_binary() == exe

    def test_missing_env_override_is_not_installed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JOBSMITH_APPLE_BRIDGE_BIN", str(tmp_path / "nope"))
        assert apple_bridge.find_binary() is None

    def test_falls_back_to_checkout_paths(self, monkeypatch, tmp_path):
        monkeypatch.delenv("JOBSMITH_APPLE_BRIDGE_BIN", raising=False)
        monkeypatch.setattr(apple_bridge, "_source_root", lambda: tmp_path)
        assert apple_bridge.find_binary() is None
        staged = tmp_path / "src-tauri" / "binaries" / apple_bridge.TRIPLE_BINARY_NAME
        staged.parent.mkdir(parents=True)
        staged.write_text("x")
        assert apple_bridge.find_binary() == staged

    def test_dev_build_product_is_last_resort(self, monkeypatch, tmp_path):
        monkeypatch.delenv("JOBSMITH_APPLE_BRIDGE_BIN", raising=False)
        monkeypatch.setattr(apple_bridge, "_source_root", lambda: tmp_path)
        dev = tmp_path / "apple-bridge" / ".build" / "release" / apple_bridge.BINARY_NAME
        dev.parent.mkdir(parents=True)
        dev.write_text("x")
        assert apple_bridge.find_binary() == dev


# ------------------------------------------------------------------ spawn
class TestSupervisor:
    @pytest.mark.asyncio
    async def test_spawn_handshake_and_health(self, bridge):
        url = await bridge.ensure_started()
        assert url.startswith("http://127.0.0.1:") and url.endswith("/v1")
        assert bridge.base_url() == url
        assert bridge.generation == 1
        assert await bridge.health() == {"available": True, "reason": None}
        # The URL really is a live server speaking the bridge contract.
        async with httpx.AsyncClient(timeout=5) as c:
            body = (await c.get(url + "/models")).json()
        assert [m["id"] for m in body["data"]] == [SENTINEL]

    @pytest.mark.asyncio
    async def test_second_call_reuses_the_same_process(self, bridge):
        first = await bridge.ensure_started()
        assert await bridge.ensure_started() == first
        assert bridge.generation == 1

    @pytest.mark.asyncio
    async def test_restart_once_after_a_crash(self, bridge):
        first = await bridge.ensure_started()
        bridge._proc.kill()
        await bridge._proc.wait()
        assert bridge.base_url() is None  # crash is visible, not papered over
        second = await bridge.ensure_started()
        assert second != first
        assert bridge.generation == 2
        assert (await bridge.health())["available"] is True

    @pytest.mark.asyncio
    async def test_terminate_on_shutdown(self, bridge):
        await bridge.ensure_started()
        proc = bridge._proc
        await bridge.stop()
        assert proc.returncode is not None
        assert bridge.base_url() is None

    @pytest.mark.asyncio
    async def test_unavailable_reason_is_reported_not_invented(self, bridge, monkeypatch):
        monkeypatch.setenv("FAKE_BRIDGE_AVAILABLE", "0")
        state_ = await bridge.health()
        assert state_["available"] is False
        assert "not turned on" in state_["reason"]

    @pytest.mark.asyncio
    async def test_helper_that_dies_on_startup_raises(self, bridge, monkeypatch):
        monkeypatch.setenv("FAKE_BRIDGE_MODE", "exit")
        with pytest.raises(apple_bridge.BridgeUnavailable):
            await bridge.ensure_started()

    @pytest.mark.asyncio
    async def test_unreadable_handshake_raises(self, bridge, monkeypatch):
        monkeypatch.setenv("FAKE_BRIDGE_MODE", "garbage")
        with pytest.raises(apple_bridge.BridgeUnavailable, match="handshake"):
            await bridge.ensure_started()

    @pytest.mark.asyncio
    async def test_silent_helper_times_out_and_is_killed(self, bridge, monkeypatch):
        monkeypatch.setenv("FAKE_BRIDGE_MODE", "silent")
        monkeypatch.setattr(apple_bridge, "HANDSHAKE_TIMEOUT", 0.5)
        with pytest.raises(apple_bridge.BridgeUnavailable, match="in time"):
            await bridge.ensure_started()
        assert bridge.base_url() is None

    @pytest.mark.asyncio
    async def test_unsupported_platform_never_spawns(self, bridge, monkeypatch):
        monkeypatch.setattr(apple_bridge, "platform_supported", lambda: False)
        with pytest.raises(apple_bridge.BridgeUnavailable, match="macOS 26"):
            await bridge.ensure_started()

    @pytest.mark.asyncio
    async def test_missing_binary_says_not_installed(self, bridge, monkeypatch, tmp_path):
        monkeypatch.setenv("JOBSMITH_APPLE_BRIDGE_BIN", str(tmp_path / "gone"))
        with pytest.raises(apple_bridge.BridgeUnavailable, match="not installed"):
            await bridge.ensure_started()


class TestBridgeStatus:
    @pytest.mark.asyncio
    async def test_unsupported_platform(self, monkeypatch):
        monkeypatch.setattr(apple_bridge, "platform_supported", lambda: False)
        assert await apple_bridge.bridge_status() == {
            "supported": False, "available": False,
            "reason": apple_bridge.REASON_UNSUPPORTED,
        }

    @pytest.mark.asyncio
    async def test_missing_binary(self, monkeypatch, tmp_path):
        monkeypatch.setattr(apple_bridge, "platform_supported", lambda: True)
        monkeypatch.setenv("JOBSMITH_APPLE_BRIDGE_BIN", str(tmp_path / "gone"))
        s = await apple_bridge.bridge_status()
        assert s == {"supported": False, "available": False,
                     "reason": apple_bridge.REASON_NOT_INSTALLED}

    @pytest.mark.asyncio
    async def test_probe_false_does_not_spawn(self, fake_bridge_env):
        s = await apple_bridge.bridge_status(probe=False)
        assert s == {"supported": True, "available": False, "reason": None}
        assert apple_bridge.bridge_base_url() is None


class TestConfigHelpers:
    def test_uses_sentinel(self):
        assert apple_bridge.uses_sentinel(_cfg(fast=SENTINEL, strong="big"))
        assert not apple_bridge.uses_sentinel(_cfg(fast="small", strong="big"))
        assert apple_bridge.uses_sentinel({"ai": {"model": SENTINEL}})

    def test_only_provider(self):
        assert apple_bridge.only_provider(_cfg(fast=SENTINEL, utility=SENTINEL))
        assert not apple_bridge.only_provider(_cfg(fast=SENTINEL, strong="big"))
        assert not apple_bridge.only_provider(_cfg())


# ----------------------------------------------------------------- routing
class TestSentinelRouting:
    @pytest.fixture(autouse=True)
    def _clean_clients(self):
        ai_engine.clear_clients()
        yield
        ai_engine.clear_clients()

    @pytest.mark.asyncio
    async def test_sentinel_tier_points_at_the_bridge(self, fake_bridge_env, monkeypatch):
        b = apple_bridge.AppleBridge()
        monkeypatch.setattr(apple_bridge, "_bridge", b)
        try:
            cfg = _cfg(fast=SENTINEL, strong="big-model")
            client = await ai_engine.get_client(cfg, "fast")
            assert str(client.base_url).rstrip("/") == b.base_url()
            # The endpoint tier is untouched by any of this.
            other = await ai_engine.get_client(cfg, "strong")
            assert "endpoint.invalid" in str(other.base_url)
        finally:
            await b.stop()

    @pytest.mark.asyncio
    async def test_utility_inherits_fast_sentinel(self, fake_bridge_env, monkeypatch):
        b = apple_bridge.AppleBridge()
        monkeypatch.setattr(apple_bridge, "_bridge", b)
        try:
            cfg = _cfg(fast=SENTINEL)
            client = await ai_engine.get_client(cfg, "utility")
            assert str(client.base_url).rstrip("/") == b.base_url()
        finally:
            await b.stop()

    def test_strict_no_fallback_when_bridge_is_down(self, monkeypatch):
        # The rule from iOS EngineRouter: an on-device tier must fail loudly
        # rather than quietly become a network call to the configured endpoint.
        monkeypatch.setattr(apple_bridge, "bridge_base_url", lambda: None)
        with pytest.raises(apple_bridge.BridgeUnavailable):
            ai_engine._get_client(_cfg(fast=SENTINEL), "fast")

    @pytest.mark.asyncio
    async def test_failed_bridge_start_does_not_reach_the_endpoint(self, fake_bridge_env, monkeypatch, tmp_path):
        monkeypatch.setenv("JOBSMITH_APPLE_BRIDGE_BIN", str(tmp_path / "gone"))
        with pytest.raises(apple_bridge.BridgeUnavailable):
            await ai_engine.get_client(_cfg(fast=SENTINEL), "fast")

    def test_restart_evicts_the_stale_client(self, monkeypatch):
        # Same port after a restart is legal (the OS may hand it straight back),
        # so the cache must key off the bridge generation, not just the URL.
        url = "http://127.0.0.1:51234/v1"
        gen = {"n": 1}
        monkeypatch.setattr(apple_bridge, "bridge_base_url", lambda: url)
        monkeypatch.setattr(apple_bridge, "bridge_generation", lambda: gen["n"])
        cfg = _cfg(fast=SENTINEL)
        first = ai_engine._get_client(cfg, "fast")
        assert ai_engine._get_client(cfg, "fast") is first  # still one process
        gen["n"] = 2
        second = ai_engine._get_client(cfg, "fast")
        assert second is not first

    def test_endpoint_clients_survive_a_bridge_restart(self, monkeypatch):
        gen = {"n": 1}
        monkeypatch.setattr(apple_bridge, "bridge_generation", lambda: gen["n"])
        cfg = _cfg(strong="big-model")
        first = ai_engine._get_client(cfg, "strong")
        gen["n"] = 2
        assert ai_engine._get_client(cfg, "strong") is first

    @pytest.mark.asyncio
    async def test_completion_round_trip_through_the_bridge(self, fake_bridge_env, monkeypatch):
        b = apple_bridge.AppleBridge()
        monkeypatch.setattr(apple_bridge, "_bridge", b)
        try:
            cfg = _cfg(fast=SENTINEL)
            client = await ai_engine.get_client(cfg, "fast")
            resp = await client.chat.completions.create(
                model=ai_engine._model(cfg, "fast"),
                messages=[{"role": "user", "content": "hi"}],
            )
            assert resp.choices[0].message.content == "fake on-device reply"
        finally:
            await b.stop()


# ------------------------------------------------------------ status merge
@pytest.fixture
def status_client(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    monkeypatch.setattr(state, "CONFIG_PATH", p)
    app = FastAPI()
    app.include_router(settings_router.router)

    def write(cfg):
        p.write_text(yaml.dump(cfg))
    return TestClient(app), write


class TestAiStatusMerge:
    @pytest.fixture(autouse=True)
    def _no_real_endpoint(self, monkeypatch):
        self.connection = {"connected": False, "error": "Connection refused"}

        async def fake_test_connection(cfg):
            return dict(self.connection)
        monkeypatch.setattr(ai_engine, "test_connection", fake_test_connection)

    def test_pure_endpoint_config_skips_the_bridge(self, status_client, monkeypatch):
        client, write = status_client
        write(_cfg(strong="big-model"))
        monkeypatch.setattr(apple_bridge, "platform_supported", lambda: False)

        async def boom(*a, **k):  # a spawn here would be the bug
            raise AssertionError("bridge_status() must not run for a pure-endpoint config")
        monkeypatch.setattr(apple_bridge, "bridge_status", boom)

        self.connection = {"connected": True, "models": ["big-model"]}
        body = client.get("/api/ai/status").json()
        assert body["ok"] is True
        assert body["models"] == ["big-model"]
        assert body["on_device"] == {"supported": False, "available": False,
                                     "reason": apple_bridge.REASON_UNSUPPORTED}

    def test_available_bridge_appends_the_sentinel(self, status_client, monkeypatch):
        client, write = status_client
        write(_cfg(strong="big-model", fast=SENTINEL))
        monkeypatch.setattr(apple_bridge, "bridge_status",
                            _async({"supported": True, "available": True, "reason": None}))
        self.connection = {"connected": True, "models": ["big-model"]}
        body = client.get("/api/ai/status").json()
        assert body["ok"] is True
        assert body["models"] == ["big-model", SENTINEL]
        assert body["on_device"]["available"] is True

    def test_on_device_only_and_unavailable_is_not_ok(self, status_client, monkeypatch):
        client, write = status_client
        write(_cfg(fast=SENTINEL, utility=SENTINEL))
        monkeypatch.setattr(apple_bridge, "bridge_status",
                            _async({"supported": True, "available": False,
                                    "reason": "Apple Intelligence is not turned on"}))
        body = client.get("/api/ai/status").json()
        assert body["ok"] is False
        assert body["error"] == "Apple Intelligence is not turned on"
        assert SENTINEL not in body["models"]

    def test_on_device_only_and_available_is_ok_despite_dead_endpoint(self, status_client, monkeypatch):
        client, write = status_client
        write(_cfg(fast=SENTINEL, utility=SENTINEL))
        monkeypatch.setattr(apple_bridge, "bridge_status",
                            _async({"supported": True, "available": True, "reason": None}))
        body = client.get("/api/ai/status").json()
        assert body["ok"] is True
        assert body["error"] is None
        assert body["models"] == [SENTINEL]

    def test_endpoint_still_configured_keeps_its_own_verdict(self, status_client, monkeypatch):
        client, write = status_client
        write(_cfg(strong="big-model", fast=SENTINEL))
        monkeypatch.setattr(apple_bridge, "bridge_status",
                            _async({"supported": True, "available": False, "reason": "off"}))
        body = client.get("/api/ai/status").json()
        assert body["ok"] is False  # the endpoint really is down
        assert body["error"] == "Connection refused"  # and that's still the reason
        assert body["on_device"] == {"supported": True, "available": False, "reason": "off"}

    def test_bridge_failure_never_500s_the_endpoint(self, status_client, monkeypatch):
        client, write = status_client
        write(_cfg(fast=SENTINEL))

        async def broken(*a, **k):
            raise RuntimeError("bridge exploded")
        monkeypatch.setattr(apple_bridge, "bridge_status", broken)
        r = client.get("/api/ai/status")
        assert r.status_code == 200
        assert r.json()["on_device"]["supported"] is False


def _async(value):
    async def _f(*a, **k):
        return value
    return _f
