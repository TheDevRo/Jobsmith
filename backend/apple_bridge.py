"""
apple_bridge.py — supervises `jobsmith-apple-ai`, the loopback OpenAI-compatible
server in front of Apple's on-device foundation model (Apple Intelligence).

The bridge is a Swift sidecar (apple-bridge/, shipped as a Tauri externalBin).
It binds 127.0.0.1 on an OS-chosen port and prints one line — `{"port": N}` —
to stdout, which is the handshake this module reads. From then on a tier whose
model is the sentinel `apple-on-device` simply points its OpenAI client at
http://127.0.0.1:<port>/v1, so the AI engine needs no second protocol.

Everything here is lazy: nothing is spawned until a configured tier actually
asks for the sentinel (or the Settings UI asks whether on-device is available on
a machine that could support it). On non-macOS, on Intel Macs, and on macOS
older than 26 the module answers "unsupported" without touching the disk.
"""

import asyncio
import json
import logging
import os
import platform
import sys
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# The model id that means "run this tier on Apple's on-device model". Shared
# verbatim with the iOS app (JobsmithKit AppleOnDeviceEngine) and the Swift
# bridge (OnDeviceModel.modelID) — do not localize or version it.
SENTINEL_MODEL = "apple-on-device"

BINARY_NAME = "jobsmith-apple-ai"
# Tauri stages sidecars as <name>-<target-triple> and strips the triple when it
# copies them into Contents/MacOS, so both spellings are worth looking for.
TRIPLE_BINARY_NAME = f"{BINARY_NAME}-aarch64-apple-darwin"

# Minimum macOS major version with the FoundationModels framework.
MIN_MACOS_MAJOR = 26

# Seconds to wait for the `{"port": N}` line, and for each health probe.
HANDSHAKE_TIMEOUT = 15.0
HEALTH_TIMEOUT = 5.0
STOP_TIMEOUT = 3.0

REASON_UNSUPPORTED = "Apple Intelligence requires macOS 26 on Apple Silicon"
REASON_NOT_INSTALLED = "The Apple Intelligence helper is not installed in this build"


class BridgeUnavailable(Exception):
    """The on-device bridge could not be started or reached.

    Raised instead of quietly falling back to the configured endpoint: an
    on-device tier NEVER silently becomes a cloud/LM Studio call (the iOS
    EngineRouter rule — a silent fallback once sent "private, on-device" work
    to a remote server without the user ever being told).
    """


# --------------------------------------------------------------------------
# Platform + binary discovery
# --------------------------------------------------------------------------
def _macos_major() -> int:
    try:
        return int((platform.mac_ver()[0] or "0").split(".")[0])
    except (ValueError, IndexError):
        return 0


def platform_supported() -> bool:
    """True when this machine could run the on-device model at all."""
    if sys.platform != "darwin":
        return False
    if platform.machine() not in ("arm64", "aarch64"):
        return False
    return _macos_major() >= MIN_MACOS_MAJOR


def _source_root() -> Path:
    # Code assets ship with the app, so they key off __file__ rather than
    # paths.project_root() (which is user state and moves with JOBSMITH_HOME).
    return Path(__file__).resolve().parent.parent


def find_binary() -> Optional[Path]:
    """Locate the bridge executable, or None when this build has no bridge.

    Order: explicit env override → next to the frozen executable (the desktop
    bundle's Contents/MacOS) → the staged Tauri sidecar → the Swift build
    product in a dev checkout.
    """
    env = os.environ.get("JOBSMITH_APPLE_BRIDGE_BIN")
    if env:
        # An explicit override is never second-guessed: if it is wrong the user
        # wants to hear "not installed", not a silent fall-through to some
        # other copy of the binary.
        p = Path(env).expanduser()
        return p if p.is_file() else None

    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates += [exe_dir / BINARY_NAME, exe_dir / TRIPLE_BINARY_NAME]
    root = _source_root()
    candidates.append(root / "src-tauri" / "binaries" / TRIPLE_BINARY_NAME)
    candidates.append(root / "apple-bridge" / ".build" / "release" / BINARY_NAME)

    for c in candidates:
        if c.is_file():
            return c
    return None


# --------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------
def _configured_models(config: dict) -> list[str]:
    ai_cfg = (config or {}).get("ai", {}) or {}
    models = []
    for tier_cfg in (ai_cfg.get("models", {}) or {}).values():
        if isinstance(tier_cfg, dict) and tier_cfg.get("model"):
            models.append(tier_cfg["model"])
    if ai_cfg.get("model"):
        models.append(ai_cfg["model"])
    return models


def uses_sentinel(config: dict) -> bool:
    """True when any configured tier is set to the on-device model."""
    return SENTINEL_MODEL in _configured_models(config)


def only_provider(config: dict) -> bool:
    """True when on-device is the *only* thing configured to answer.

    Used by /api/ai/status: with no endpoint model configured, an unavailable
    bridge means the app has no AI at all, so the status must say so.
    """
    models = _configured_models(config)
    return bool(models) and all(m == SENTINEL_MODEL for m in models)


# --------------------------------------------------------------------------
# The supervisor
# --------------------------------------------------------------------------
class AppleBridge:
    """Spawns the sidecar on demand and keeps one instance alive per process."""

    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._base_url: Optional[str] = None
        self._lock = asyncio.Lock()
        # Bumped on every successful spawn. ai_engine compares it to notice a
        # restart and evict OpenAI clients bound to the dead port.
        self._generation = 0

    # -- introspection (sync, never spawns) --------------------------------
    @property
    def generation(self) -> int:
        return self._generation

    def base_url(self) -> Optional[str]:
        """The running bridge's /v1 URL, or None when it isn't running."""
        if self._proc is not None and self._proc.returncode is None:
            return self._base_url
        return None

    # -- lifecycle ---------------------------------------------------------
    async def ensure_started(self) -> str:
        """Return the bridge's base URL, spawning/restarting it as needed."""
        if not platform_supported():
            raise BridgeUnavailable(REASON_UNSUPPORTED)
        async with self._lock:
            if self._proc is not None and self._proc.returncode is None:
                return self._base_url  # type: ignore[return-value]
            binary = find_binary()
            if binary is None:
                raise BridgeUnavailable(REASON_NOT_INSTALLED)
            # Restart-once: a bridge that died (or a spawn that lost its
            # handshake) gets exactly one more attempt before the caller sees
            # an error, so a single crash doesn't take AI down for the session.
            last: Exception | None = None
            for attempt in (1, 2):
                try:
                    return await self._spawn(binary)
                except BridgeUnavailable as exc:
                    last = exc
                    logger.warning("Apple bridge start attempt %d failed: %s", attempt, exc)
            raise last  # type: ignore[misc]

    async def _spawn(self, binary: Path) -> str:
        await self._terminate_proc()
        try:
            proc = await asyncio.create_subprocess_exec(
                str(binary), "--port", "0",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise BridgeUnavailable(f"Could not start the Apple Intelligence helper: {exc}")

        self._proc = proc
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=HANDSHAKE_TIMEOUT)
        except asyncio.TimeoutError:
            await self._terminate_proc()
            raise BridgeUnavailable("The Apple Intelligence helper did not start in time")
        if not line:
            await self._terminate_proc()
            raise BridgeUnavailable("The Apple Intelligence helper exited on startup")
        try:
            port = int(json.loads(line.decode("utf-8", "replace"))["port"])
        except (ValueError, KeyError, TypeError):
            await self._terminate_proc()
            raise BridgeUnavailable("The Apple Intelligence helper sent an unreadable handshake")

        self._base_url = f"http://127.0.0.1:{port}/v1"
        self._generation += 1
        logger.info("Apple Intelligence bridge listening on 127.0.0.1:%d (pid %s)",
                    port, proc.pid)
        return self._base_url

    async def _terminate_proc(self) -> None:
        proc, self._proc, self._base_url = self._proc, None, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=STOP_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def stop(self) -> None:
        """Terminate the sidecar — called from the app's shutdown hook."""
        async with self._lock:
            await self._terminate_proc()

    # -- health ------------------------------------------------------------
    async def health(self) -> dict:
        """Start (if needed) and probe the bridge: {available, reason}."""
        try:
            base = await self.ensure_started()
        except BridgeUnavailable as exc:
            return {"available": False, "reason": str(exc)}
        root = base[: -len("/v1")]
        try:
            async with httpx.AsyncClient(timeout=HEALTH_TIMEOUT) as client:
                r = await client.get(f"{root}/health")
                r.raise_for_status()
                body = r.json()
        except Exception as exc:  # noqa: BLE001 — any failure is "unavailable"
            return {"available": False,
                    "reason": f"The Apple Intelligence helper is not responding ({exc})"}
        return {
            "available": bool(body.get("available")),
            "reason": body.get("reason"),
        }


_bridge = AppleBridge()


# --------------------------------------------------------------------------
# Module-level API (the surface ai_engine and the routers use)
# --------------------------------------------------------------------------
async def ensure_started() -> str:
    return await _bridge.ensure_started()


def bridge_base_url() -> Optional[str]:
    """Base URL of the running bridge, or None. Sync — never spawns."""
    return _bridge.base_url()


def bridge_generation() -> int:
    return _bridge.generation


async def bridge_status(probe: bool = True) -> dict:
    """`{supported, available, reason}` for /api/ai/status and the UI.

    `supported` means "this machine + this build can offer on-device AI" — it
    gates whether the Settings/wizard controls are rendered at all. `available`
    means it can serve a request right now (Apple Intelligence actually turned
    on, model downloaded). Pass probe=False to answer without spawning.
    """
    if not platform_supported():
        return {"supported": False, "available": False, "reason": REASON_UNSUPPORTED}
    if find_binary() is None:
        return {"supported": False, "available": False, "reason": REASON_NOT_INSTALLED}
    if not probe:
        return {"supported": True, "available": False, "reason": None}
    state = await _bridge.health()
    return {"supported": True, **state}


async def shutdown() -> None:
    await _bridge.stop()
