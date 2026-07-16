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
from .settings_registry import CATEGORIES, CATEGORY_KEYS, category_defaults
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
        after_import: Optional[Callable[[], None]] = None,
    ):
        self._load_config = load_config
        self._save_config = save_config
        self._db_path_fn = db_path_fn
        self._docs_dir = Path(docs_dir)
        self._platform = platform
        # Fired after each cycle that imported changes — the seam where the app
        # reacts to what sync brought in (today: work-request fulfillment).
        # Synchronous and must not block; spawn a task for real work.
        self._after_import = after_import
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

    @property
    def interval_seconds(self) -> int:
        """Poll cadence for the background loop; 0 disables auto-sync (manual
        only). Defaults to 60s. Clamped to a sane floor so a bad value can't
        busy-loop."""
        raw = self._sync_cfg(self._load_config()).get("interval_seconds", 60)
        try:
            secs = int(raw)
        except (TypeError, ValueError):
            return 60
        if secs <= 0:
            return 0
        return max(10, secs)

    def update_config(self, *, enabled: Optional[bool] = None,
                      folder: Optional[str] = None,
                      device_label: Optional[str] = None,
                      interval_seconds: Optional[int] = None,
                      settings: Optional[dict] = None,
                      fulfill_work_requests: Optional[bool] = None) -> dict:
        cfg = self._load_config()
        section = self._sync_cfg(cfg)
        if enabled is not None:
            section["enabled"] = enabled
        if fulfill_work_requests is not None:
            section["fulfill_work_requests"] = bool(fulfill_work_requests)
        if folder is not None:
            section["folder"] = folder
        if device_label is not None:
            section["device_label"] = device_label
        if interval_seconds is not None:
            section["interval_seconds"] = interval_seconds
        if settings is not None:
            # Per-category sync toggles (sync.settings.<key>), validated against
            # the registry's category keys so an unknown key can't be persisted.
            current = section.get("settings")
            current = dict(current) if isinstance(current, dict) else {}
            for key, val in settings.items():
                if key in CATEGORY_KEYS:
                    current[key] = bool(val)
            section["settings"] = current
        cfg["sync"] = section
        self._save_config(cfg)
        return self.status()

    def settings_state(self, cfg: dict) -> dict:
        """Per-category toggle state, seeded from category_defaults() so an absent
        key reads as its default (profile ON, everything else OFF) rather than
        false."""
        section = self._sync_cfg(cfg)
        stored = section.get("settings")
        stored = stored if isinstance(stored, dict) else {}
        out = category_defaults()
        for key in CATEGORY_KEYS:
            if key in stored:
                out[key] = bool(stored[key])
        return out

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
            # Serve other devices' hand-off requests (score_all). Off by
            # default: a synced file must not spend LLM tokens uninvited.
            "fulfill_work_requests": bool(section.get("fulfill_work_requests")),
            "interval_seconds": self.interval_seconds,
            "known_devices": devices,
            "settings": self.settings_state(cfg),
            "settings_categories": [
                {"key": c.key, "label": c.label, "default": c.default}
                for c in CATEGORIES
            ],
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

        # Config-backed `setting` bridge: the engine reads the whole config to
        # export enabled paths and writes it back after applying imports. Category
        # gating (incl. the Profile toggle) is derived from this same dict.
        def load_settings():
            return self._load_config()

        def save_settings(cfg):
            self._save_config(cfg)

        self._docs_dir.mkdir(parents=True, exist_ok=True)
        return SyncEngine(
            self._db_path_fn(), device_id,
            load_profile=load_profile, save_profile=save_profile,
            load_settings=load_settings, save_settings=save_settings,
            docs_dir=self._docs_dir,
        )

    # -- the cycle ---------------------------------------------------------

    async def sync_once(self) -> dict:
        """Import remote changes, export local ones, compact. No-op (returns a
        'skipped' result) when disabled or unconfigured."""
        if not self.enabled or not self.folder:
            return {"skipped": True, "reason": "disabled" if not self.enabled else "no folder"}

        imported_changes = False
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
                # Export BEFORE import: a just-made local decision (shortlist or
                # delete) has no timestamp until it's exported, so we stamp it
                # `now` first — then import lets it out-rank any older record
                # already in the folder under plain last-writer-wins. See engine.py.
                exp = await engine.export_changes(folder)
                imp = await engine.import_changes(folder)
                dropped = sf.compact_own_log(device_id)
                result = {
                    "skipped": False,
                    "imported": {"upserts": imp.upserts, "deletes": imp.deletes,
                                 "deferred": imp.deferred, "profile": imp.profile_updated,
                                 "settings": imp.settings_updated},
                    "exported": {"live": exp.live, "tombstones": exp.tombstones},
                    "compacted": dropped,
                }
                self.last_result = result
                self.last_error = None
                # A hand-off can only arrive on a cycle that actually imported
                # something, so only bother the hook then — not on every idle tick.
                imported_changes = bool(imp.upserts or imp.deletes)
            except Exception as e:  # sync must never crash the app
                logger.exception("sync_once failed")
                self.last_error = str(e)
                return {"skipped": False, "error": str(e)}

        # Fire the after-import hook OUTSIDE the lock — it may kick off a long
        # fulfillment batch, which must not block the next cycle from acquiring
        # the lock. Isolated: the hook must never break sync itself.
        if imported_changes and self._after_import is not None:
            try:
                self._after_import()
            except Exception:
                logger.exception("after-import hook failed")
        return result

    async def run_periodic(self, interval_seconds: Optional[int] = None) -> None:
        """Background loop; reloads config each tick so it's safe to always
        start (self-gates on `sync.enabled`) and picks up interval changes live.
        Pass `interval_seconds` to override the configured cadence (tests)."""
        while True:
            try:
                if self.enabled and self.folder:
                    await self.sync_once()
            except Exception:
                logger.exception("sync periodic tick failed")
            # Re-read each tick so a Settings change takes effect without a
            # restart; 0 means "manual only" — idle at a slow heartbeat.
            configured = self.interval_seconds if interval_seconds is None else interval_seconds
            await asyncio.sleep(configured if configured > 0 else 60)


_default: Optional[SyncService] = None


def default_service() -> SyncService:
    """The app-wired singleton (config via app_state, DB via database.DB_PATH)."""
    global _default
    if _default is None:
        from .. import app_state as state
        from .. import background_tasks
        from .. import database
        from ..paths import project_root

        _default = SyncService(
            load_config=state.load_config,
            save_config=state.save_config,
            db_path_fn=lambda: str(database.DB_PATH),
            docs_dir=project_root() / "data" / "sync-docs",
            after_import=background_tasks.schedule_work_request_fulfillment,
        )
    return _default
