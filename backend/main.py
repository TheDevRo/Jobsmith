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
import shutil
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from . import app_state as state
from .app_state import load_config, save_config  # re-exported for callers/tests
from . import database as db
from . import ai_engine
from . import auto_apply
from . import extension_api
from . import routers
from .routers.sessions import _bg_check_linkedin_session

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Path aliases kept at module level for tests that monkeypatch them.
PROJECT_ROOT = state.PROJECT_ROOT
CONFIG_PATH = state.CONFIG_PATH
EXAMPLE_CONFIG_PATH = state.EXAMPLE_CONFIG_PATH
FRONTEND_DIR = state.FRONTEND_DIR
RESUMES_DIR = state.RESUMES_DIR


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

    # Kick off a background LinkedIn session validity check on startup (non-blocking).
    if auto_apply.has_linkedin_session():
        asyncio.create_task(_bg_check_linkedin_session())

    token = extension_api.get_or_create_token()
    logger.info("Browser extension token: %s  (paste into extension popup)", token)

    await db.log_activity("system_start", "Jobsmith server started")
    yield
    # Shutdown
    logger.info("Server shutting down")


app = FastAPI(title="Jobsmith", version="1.0.0", lifespan=lifespan)


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

# Browser callers are limited to the local dashboard and the Apply Assist
# extension (whose pages have chrome-extension:// / moz-extension:// origins;
# its only content script runs on this server's own origin, so same-origin).
# The extension API is additionally gated by the X-Jobsmith-Token header.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=(
        r"^(https?://(localhost|127\.0\.0\.1)(:\d+)?"
        r"|(chrome|moz|safari-web)-extension://.*)$"
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# /api/ext/* — the surface the extension itself talks to (token-gated)
app.include_router(extension_api.build_router(state.load_config))

# Dashboard API routes
for _router in routers.ALL_ROUTERS:
    app.include_router(_router)

# ---------------------------------------------------------------------------
# Serve frontend static files
# ---------------------------------------------------------------------------
if state.FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(state.FRONTEND_DIR), html=True), name="frontend")
