"""SyncService — ties config, engine, transport, and documents together.

Owns the desktop's sync lifecycle: a stable device id, the user-chosen folder,
and one `sync_once()` that imports remote changes, exports local ones, and
compacts this device's log. Everything the service touches is injected
(load_config/save_config, db_path, docs_dir) so it is testable without a running
server; `default_service()` wires the real app.

Config lives under a `sync:` section:

    sync:
      enabled: true
      folder: /Users/me/Library/Mobile Documents/iCloud~app~jobsmith/Documents
      device_id: 9F3A2B7C          # generated once, never shown to the user
      device_label: "MacBook Pro"  # display only
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Callable, Optional

from .engine import SyncEngine
from .transport import SyncFolder

logger = logging.getLogger(__name__)


class SyncService:
    def __init__(
        self,
        load_config: Callable[[], dict],
        save_config: Callable[[dict], None],
        db_path_fn: Callable[[], str],
        docs_dir: str | Path,
        *,
        platform: str = "macos",
    ):
        self._load_config = load_config
        self._save_config = save_config
        self._db_path_fn = db_path_fn
        self._docs_dir = Path(docs_dir)
        self._platform = platform
        self._lock = asyncio.Lock()
        self.last_result: Optional[dict] = None
        self.last_error: Optional[str] = None

    # -- config ------------------------------------------------------------

    def _sync_cfg(self, cfg: dict) -> dict:
        section = cfg.get("sync")
        return section if isinstance(section, dict) else {}

    def device_id(self) -> str:
        """The stable per-install id, generated and persisted on first use."""
        cfg = self._load_config()
        section = self._sync_cfg(cfg)
        did = section.get("device_id")
        if not did:
            did = uuid.uuid4().hex[:8].upper()
            section["device_id"] = did
            cfg["sync"] = section
            self._save_config(cfg)
        return did

    @property
    def enabled(self) -> bool:
        return bool(self._sync_cfg(self._load_config()).get("enabled"))

    @property
    def folder(self) -> Optional[str]:
        return self._sync_cfg(self._load_config()).get("folder") or None

    def update_config(self, *, enabled: Optional[bool] = None,
                      folder: Optional[str] = None,
                      device_label: Optional[str] = None) -> dict:
        cfg = self._load_config()
        section = self._sync_cfg(cfg)
        if enabled is not None:
            section["enabled"] = enabled
        if folder is not None:
            section["folder"] = folder
        if device_label is not None:
            section["device_label"] = device_label
        cfg["sync"] = section
        self._save_config(cfg)
        return self.status()

    def status(self) -> dict:
        cfg = self._load_config()
        section = self._sync_cfg(cfg)
        folder = section.get("folder") or None
        devices = []
        if folder and Path(folder).exists():
            devices = sorted(SyncFolder(folder).log_device_ids())
        return {
            "enabled": bool(section.get("enabled")),
            "folder": folder,
            "device_id": section.get("device_id"),
            "device_label": section.get("device_label"),
            "known_devices": devices,
            "last_result": self.last_result,
            "last_error": self.last_error,
        }

    # -- profile bridge ----------------------------------------------------

    def _make_engine(self, folder: Path, device_id: str) -> SyncEngine:
        def load_profile():
            return self._load_config().get("profile")

        def save_profile(p):
            cfg = self._load_config()
            cfg["profile"] = p
            self._save_config(cfg)

        self._docs_dir.mkdir(parents=True, exist_ok=True)
        return SyncEngine(
            self._db_path_fn(), device_id,
            load_profile=load_profile, save_profile=save_profile,
            docs_dir=self._docs_dir,
        )

    # -- the cycle ---------------------------------------------------------

    async def sync_once(self) -> dict:
        """Import remote changes, export local ones, compact. No-op (returns a
        'skipped' result) when disabled or unconfigured."""
        if not self.enabled or not self.folder:
            return {"skipped": True, "reason": "disabled" if not self.enabled else "no folder"}

        async with self._lock:
            folder = Path(self.folder).expanduser()
            device_id = self.device_id()
            try:
                sf = SyncFolder(folder)
                sf.ensure_dirs()
                sf.register_device(
                    device_id,
                    label=self._sync_cfg(self._load_config()).get("device_label"),
                    platform=self._platform,
                )
                engine = self._make_engine(folder, device_id)
                imp = await engine.import_changes(folder)
                exp = await engine.export_changes(folder)
                dropped = sf.compact_own_log(device_id)
                result = {
                    "skipped": False,
                    "imported": {"upserts": imp.upserts, "deletes": imp.deletes,
                                 "deferred": imp.deferred, "profile": imp.profile_updated},
                    "exported": {"live": exp.live, "tombstones": exp.tombstones},
                    "compacted": dropped,
                }
                self.last_result = result
                self.last_error = None
                return result
            except Exception as e:  # sync must never crash the app
                logger.exception("sync_once failed")
                self.last_error = str(e)
                return {"skipped": False, "error": str(e)}

    async def run_periodic(self, interval_seconds: int = 60) -> None:
        """Background loop; reloads config each tick so it's safe to always
        start (self-gates on `sync.enabled`)."""
        while True:
            try:
                if self.enabled and self.folder:
                    await self.sync_once()
            except Exception:
                logger.exception("sync periodic tick failed")
            await asyncio.sleep(interval_seconds)


_default: Optional[SyncService] = None


def default_service() -> SyncService:
    """The app-wired singleton (config via app_state, DB via database.DB_PATH)."""
    global _default
    if _default is None:
        from .. import app_state as state
        from .. import database
        from ..paths import project_root

        _default = SyncService(
            load_config=state.load_config,
            save_config=state.save_config,
            db_path_fn=lambda: str(database.DB_PATH),
            docs_dir=project_root() / "data" / "sync-docs",
        )
    return _default
