"""
routers/extension.py — Extension token management, packaged-extension
downloads, and the active-job hint the sidepanel polls.

The /api/ext/* endpoints the extension itself calls live in
backend/extension_api.py; this module is the dashboard-facing surface.
"""

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import app_state as state
from .. import extension_api

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_loopback(request: Request) -> None:
    """Reject callers that aren't on this machine, even when the server is
    deliberately bound to a LAN interface."""
    if not state.is_loopback_request(request):
        raise HTTPException(403, "Only served to localhost")


@router.get("/api/extension/token")
async def get_extension_token(request: Request):
    """Return the extension auth token. Loopback clients only."""
    _require_loopback(request)
    return {"token": extension_api.get_or_create_token()}


@router.post("/api/extension/token/rotate")
async def rotate_extension_token(request: Request):
    """Generate a fresh extension token, invalidating any previously-issued one."""
    _require_loopback(request)
    return {"token": extension_api.rotate_token()}


_EXT_DIST_DIR = state.PROJECT_ROOT / "extension" / "dist"
_EXT_ARTIFACTS = {
    "chrome": "jobsmith-chrome.zip",
    "firefox": "jobsmith-firefox.zip",
}


@router.get("/api/extension/download/{browser}")
async def download_extension(browser: str):
    """Serve the packaged Apply Assist extension zip for Chrome or Firefox."""
    fname = _EXT_ARTIFACTS.get(browser)
    if not fname:
        raise HTTPException(404, "Unknown browser; use 'chrome' or 'firefox'")
    path = _EXT_DIST_DIR / fname
    if not path.exists():
        raise HTTPException(
            503,
            "Extension artifact missing. Run extension/scripts/build.sh to generate it.",
        )
    return FileResponse(str(path), media_type="application/zip", filename=fname)


def _latest_signed_xpi() -> Optional[Path]:
    """Newest Mozilla-signed .xpi produced by `web-ext sign`, if any."""
    artifacts_dir = _EXT_DIST_DIR / "firefox" / "web-ext-artifacts"
    if not artifacts_dir.is_dir():
        return None
    xpis = sorted(artifacts_dir.glob("*.xpi"), key=lambda p: p.stat().st_mtime, reverse=True)
    return xpis[0] if xpis else None


@router.get("/api/extension/firefox-xpi")
async def download_firefox_xpi():
    """Serve the Mozilla-signed .xpi for a permanent Firefox install.

    Served with the application/x-xpinstall content type so clicking the link
    in Firefox triggers the native install prompt directly. Produced by
    `cd extension/dist/firefox && web-ext sign --channel=unlisted ...`.
    """
    path = _latest_signed_xpi()
    if not path:
        raise HTTPException(
            503,
            "No signed .xpi found. Run web-ext sign in extension/dist/firefox "
            "to produce one (see extension/README.md).",
        )
    return FileResponse(
        str(path),
        media_type="application/x-xpinstall",
        filename="jobsmith-assist.xpi",
    )


# In-memory hint: the most recent job whose "Open Job URL" was clicked in the
# Jobsmith UI. The extension polls /api/extension/active-job on focus and
# auto-binds. Single slot, latest click wins, no expiry.
_ext_active_job_id: Optional[str] = None


class ActiveJobRequest(BaseModel):
    job_id: str


@router.post("/api/extension/active-job")
async def set_extension_active_job(body: ActiveJobRequest):
    """Record which job the user just opened from the Jobsmith UI."""
    global _ext_active_job_id
    _ext_active_job_id = body.job_id
    return {"job_id": _ext_active_job_id}


@router.get("/api/extension/active-job")
async def get_extension_active_job():
    """Return the most recently signalled active job (or null)."""
    return {"job_id": _ext_active_job_id}


@router.delete("/api/extension/active-job")
async def clear_extension_active_job():
    """Clear the active-job hint."""
    global _ext_active_job_id
    _ext_active_job_id = None
    return {"job_id": None}
