"""
routers/extension.py — Extension token management, packaged-extension
downloads, and the active-job hint the sidepanel polls.

The /api/ext/* endpoints the extension itself calls live in
backend/extension_api.py; this module is the dashboard-facing surface.
"""

import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import app_state as state
from .. import extension_api
from ..paths import reveal_in_file_manager

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


_EXT_DIST_DIR = state.EXT_DIST_DIR
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


# Signed artifacts live in two places: web-ext-artifacts/ (fresh `web-ext
# sign` output, present in desktop builds) and extension/signed/ (committed
# to git so Docker images and source checkouts — which have no dist and no
# AMO credentials — still serve a permanently installable XPI).
_EXT_SIGNED_DIRS = (
    _EXT_DIST_DIR / "firefox" / "web-ext-artifacts",
    _EXT_DIST_DIR.parent / "signed",
)

_XPI_VERSION_RE = re.compile(r"-(\d+(?:\.\d+)*)\.xpi$")


def _xpi_sort_key(path: Path) -> tuple:
    """Highest version wins; mtime breaks ties. Version comes from the
    `...-X.Y.Z.xpi` filename suffix — git checkouts reset mtimes, so mtime
    alone would misrank a committed newer version under a stale local one."""
    m = _XPI_VERSION_RE.search(path.name)
    version = tuple(int(p) for p in m.group(1).split(".")) if m else ()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (version, mtime)


def _latest_signed_xpi() -> Optional[Path]:
    """Best Mozilla-signed .xpi available across all artifact locations."""
    xpis = [p for d in _EXT_SIGNED_DIRS if d.is_dir() for p in d.glob("*.xpi")]
    return max(xpis, key=_xpi_sort_key) if xpis else None


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


def _downloads_dir() -> Path:
    d = Path.home() / "Downloads"
    return d if d.is_dir() else Path.home()


@router.post("/api/extension/save/{browser}")
async def save_extension(browser: str, request: Request):
    """Copy the extension into ~/Downloads and reveal it in the file manager.

    The desktop shell's webview cannot download files, so the backend (a
    local process) writes them to disk instead. Chrome gets the unpacked
    directory ready for Load-unpacked; Firefox gets the Mozilla-signed .xpi
    when one exists, otherwise the unpacked directory for a temporary
    install.
    """
    _require_loopback(request)
    if browser not in _EXT_ARTIFACTS:
        raise HTTPException(404, "Unknown browser; use 'chrome' or 'firefox'")

    dest_root = _downloads_dir()

    if browser == "firefox":
        xpi = _latest_signed_xpi()
        if xpi:
            target = dest_root / "jobsmith-assist.xpi"
            shutil.copyfile(xpi, target)
            return {
                "saved_to": str(target),
                "kind": "xpi",
                "signed": True,
                "revealed": reveal_in_file_manager(target),
            }

    src = _EXT_DIST_DIR / browser
    if not src.is_dir():
        raise HTTPException(
            503,
            "Extension artifact missing. Run extension/scripts/build.sh to generate it.",
        )
    target = dest_root / f"jobsmith-extension-{browser}"
    if target.is_dir():
        shutil.rmtree(target)
    shutil.copytree(src, target, ignore=shutil.ignore_patterns("web-ext-artifacts"))
    return {
        "saved_to": str(target),
        "kind": "unpacked",
        "signed": False,
        "revealed": reveal_in_file_manager(target),
    }


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
