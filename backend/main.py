"""
main.py — FastAPI application assembly for Jobsmith.

The routes live in backend/routers/ (one module per functional area), the
long-running workers in backend/background_tasks.py, and shared runtime
state in backend/app_state.py. This module wires them together: lifespan,
middleware, CORS, router registration, and the static frontend mount.

Run with: uvicorn backend.main:app --port 8888  (see start_server.sh)
"""

import asyncio
import logging
import os
import shutil
import socket
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import app_state as state
from .app_state import load_config, save_config  # re-exported for callers/tests
from . import database as db
from . import ai_engine
from . import auto_apply
from . import extension_api
from . import routers
from .routers import _auth
from .routers.sessions import _bg_check_linkedin_session
from .version import APP_VERSION

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# Also log to a rotating file: the desktop app's stdout is invisible, and
# Settings → Logs tails this file (served by /api/logs/tail).
state.LOGS_DIR.mkdir(parents=True, exist_ok=True)


class _OwnerOnlyRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that keeps the log (and its backups) mode 0600.

    The log can carry tokens, session details and PII, and the default 0644
    makes it readable by every local user. chmod on each open covers both the
    initial file and every post-rotation file.
    """

    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:  # pragma: no cover - best effort (e.g. odd filesystems)
            pass
        return stream


_file_handler = _OwnerOnlyRotatingFileHandler(
    state.LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
)
logging.getLogger().addHandler(_file_handler)

logger = logging.getLogger(__name__)

# Path aliases kept at module level for tests that monkeypatch them.
PROJECT_ROOT = state.PROJECT_ROOT
CONFIG_PATH = state.CONFIG_PATH
EXAMPLE_CONFIG_PATH = state.EXAMPLE_CONFIG_PATH
FRONTEND_DIR = state.FRONTEND_DIR
RESUMES_DIR = state.RESUMES_DIR


GHOST_SWEEP_INTERVAL_SECONDS = 6 * 3600


async def _bg_ghost_sweep() -> None:
    """Periodically retire applications the employer never answered.

    Applications default to outcome='awaiting' and stay there unless the user
    hand-edits them, so the outcome funnel reported a 0% response rate for anyone
    who skipped the data entry. This ages silent applications out to
    'no_response' automatically. Re-reads config each tick so a Settings change
    takes effect without a restart; 0 disables the sweep.
    """
    while True:
        try:
            days = int(state.load_config().get("pipeline", {}).get("ghost_after_days", 21))
            if days > 0 and (ghosted := await db.mark_ghosted_applications(days)):
                logger.info(
                    "Ghost sweep: %d application(s) marked no_response after %d days",
                    len(ghosted), days,
                )
                state.push_notification(
                    "outcome",
                    "Applications aged out",
                    f"{len(ghosted)} application(s) had no response after {days} days",
                )
        except Exception:  # a bad config value must not kill the loop
            logger.exception("ghost sweep tick failed")
        await asyncio.sleep(GHOST_SWEEP_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await db.init_db()
    state.RESUMES_DIR.mkdir(parents=True, exist_ok=True)

    # Fresh checkout: bootstrap config.yaml from the example so the API works
    # and the onboarding wizard can take over.
    if not state.CONFIG_PATH.exists() and state.EXAMPLE_CONFIG_PATH.exists():
        shutil.copyfile(state.EXAMPLE_CONFIG_PATH, state.CONFIG_PATH)
        logger.info("No config.yaml found — created one from config.example.yaml. "
                    "Open the app to run first-time setup.")

    cfg = state.load_config()
    try:
        ai_status = await asyncio.wait_for(ai_engine.test_connection(cfg), timeout=5)
        if ai_status["connected"]:
            logger.info("LM Studio connected — models: %s", ai_status["models"])
        else:
            logger.warning("LM Studio not reachable: %s", ai_status.get("error"))
    except asyncio.TimeoutError:
        logger.warning("LM Studio connection timed out — server starting without AI")

    reset_count = await db.reset_stuck_applications()
    if reset_count:
        logger.warning("Reset %d application(s) stuck in 'applying' state from previous session", reset_count)

    # Rebuild today's rate-limit counters from the DB — otherwise a restart
    # silently resets the applications-per-day cap to 0.
    await auto_apply.hydrate_rate_limits()

    # Kick off a background LinkedIn session validity check on startup (non-blocking).
    if auto_apply.has_linkedin_session():
        asyncio.create_task(_bg_check_linkedin_session())

    # Ensure the token file exists, but never log the value: this log is tailed
    # by /api/logs/tail and kept on disk (with backups), so logging the token
    # would hand over the whole /api/ext/* surface to anyone who can read it.
    extension_api.get_or_create_token()
    logger.info("Browser extension token ready (Settings → Integrations, or %s)",
                extension_api.TOKEN_PATH)

    # Folder-sync poller — self-gates on config.sync.enabled + a chosen folder,
    # reloads config each tick, and never raises, so it's safe to always start.
    from .sync.service import default_service as _sync_service
    asyncio.create_task(_sync_service().run_periodic())

    asyncio.create_task(_bg_ghost_sweep())

    await db.log_activity("system_start", "Jobsmith server started")
    yield
    # Shutdown
    logger.info("Server shutting down")


app = FastAPI(title="Jobsmith", version=APP_VERSION, lifespan=lifespan)


class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    """Prevent browsers from caching static assets (HTML, JS, CSS)."""
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.endswith(('.html', '.js', '.css')) or path == '/':
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
        return response


app.add_middleware(NoCacheStaticMiddleware)


def _configured_bind() -> tuple[str, int]:
    """Resolve the host/port uvicorn will actually listen on.

    Mirrors start_server.sh and packaging/desktop_entry.py: JOBSMITH_PORT wins
    (the desktop shell picks a free port at launch), then config.yaml, then the
    8888 default. Never raises — a fresh checkout has no config.yaml yet.
    """
    host, port = "127.0.0.1", 8888
    try:
        server = state.load_config().get("server") or {}
        host = server.get("host") or host
        port = int(server.get("port") or port)
    except Exception:  # missing/corrupt config — fall back to the defaults
        pass
    host = os.environ.get("JOBSMITH_HOST") or host
    try:
        port = int(os.environ.get("JOBSMITH_PORT") or port)
    except ValueError:
        pass
    return host, port


_BIND_HOST, _BIND_PORT = _configured_bind()
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", ""}

# --- Host-header validation (DNS rebinding) --------------------------------
# Without this, a page on attacker.com can re-resolve its own domain to
# 127.0.0.1 and issue *same-origin* requests to attacker.com:8888 — CORS never
# applies and the client IP looks like loopback, so the "local == trusted"
# rule below would hand it the whole API.
#
# We only pin the Host list when we're actually bound to loopback (the desktop
# case, which is the one that trusts loopback). When the operator has bound to
# a LAN address / 0.0.0.0 (Docker), the browser legitimately connects by an IP
# or hostname we cannot enumerate here — that deployment is protected instead
# by the token requirement in _auth (a non-loopback caller must authenticate),
# which is exactly what rebinding cannot satisfy.
if _BIND_HOST in _LOOPBACK_HOSTS:
    app.add_middleware(
        TrustedHostMiddleware,
        # "testserver" is Starlette's TestClient default Host. Allowing it is
        # not a rebinding hole: a browser always sends the real hostname it
        # dialled, so an attacker page cannot forge this value.
        allowed_hosts=["localhost", "127.0.0.1", "::1", "[::1]", "testserver"],
    )

# --- CORS ------------------------------------------------------------------
# Browser callers are limited to the local dashboard (same-origin, so CORS is
# not even consulted) and the Apply Assist extension pages, whose origins are
# chrome-extension:// / moz-extension://.
#
# allow_credentials is deliberately off: the dashboard's session cookie is
# same-origin and SameSite=strict, and the extension authenticates with the
# X-Jobsmith-Token header. Leaving credentials on while matching any localhost
# port let *any* other app on the machine make credentialed calls to us.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://localhost:{_BIND_PORT}",
        f"http://127.0.0.1:{_BIND_PORT}",
    ],
    allow_origin_regex=r"^(chrome|moz|safari-web)-extension://.*$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# /api/ext/* — the surface the extension itself talks to (token-gated)
app.include_router(extension_api.build_router(state.load_config))

# /api/auth/* — the token->cookie exchange. Mounted WITHOUT the auth dependency
# below, or a locked-out browser could never authenticate itself.
app.include_router(_auth.router)

# Dashboard API routes — every one of these is now gated: loopback callers pass,
# anyone else must present the token (header or cookie). See routers/_auth.py.
for _router in routers.ALL_ROUTERS:
    app.include_router(_router, dependencies=[Depends(_auth.require_local_or_token)])

# ---------------------------------------------------------------------------
# Serve frontend static files
# ---------------------------------------------------------------------------
if state.FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(state.FRONTEND_DIR), html=True), name="frontend")
