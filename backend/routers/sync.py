"""
routers/sync.py — Serverless folder sync: status, configuration, run-now.

The sync engine lives in backend/sync/; this router is the dashboard's control
surface over the SyncService singleton.
"""
import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from ..paths import pick_folder
from ..sync.service import default_service

logger = logging.getLogger(__name__)

router = APIRouter()


class SyncConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    folder: Optional[str] = None
    device_label: Optional[str] = None


@router.get("/api/sync/status")
async def sync_status():
    svc = default_service()
    svc.device_id()  # ensure one exists so the UI can show it
    return svc.status()


@router.post("/api/sync/config")
async def sync_config(update: SyncConfigUpdate):
    return default_service().update_config(
        enabled=update.enabled,
        folder=update.folder,
        device_label=update.device_label,
    )


@router.post("/api/sync/run")
async def sync_run():
    return await default_service().sync_once()


# NOTE: plain `def` on purpose — the native folder dialog is modal/blocking,
# so FastAPI runs this in the threadpool and the event loop stays responsive.
@router.post("/api/sync/pick-folder")
def sync_pick_folder():
    """Open the OS folder picker; return {"path": <chosen>} or {"path": null}."""
    return {"path": pick_folder("Choose the shared Jobsmith sync folder")}
