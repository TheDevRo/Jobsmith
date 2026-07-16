"""Desktop sync engine (Python) — see spec/FORMAT.md and PLAN.md Step 4.

Two operations against a shared sync folder:

  export_changes(folder)  — diff the live DB (+ config profile) against the last
      thing we emitted and append only genuine local changes to our own
      changes/{device_id}.jsonl. New/changed rows become live records; rows that
      vanished become tombstones. Each carries updated_at = now() (the LWW clock).

  import_changes(folder)  — read every device's log, fold them by the merge
      rules (newest updated_at wins; ties by higher device id), apply the winners
      to the DB and config, then record what we imported so a subsequent export
      re-emits nothing.

Every entity — including a job's `triage` decision — goes through the SAME
generic last-writer-wins path. A delete is not special: it is a `triage` record
whose status is 'deleted', so a delete and a shortlist compete purely on their
timestamps. There is no deletion side table and no re-discovery latch, and thus
no engaged-status override.

(A cycle still runs export_changes before import_changes so a just-made local
decision is stamped `now` and can out-rank an older record already sitting in
the folder. That ordering is inherent to snapshot-diff LWW — local edits have no
timestamp until they're exported — not something specific to deletes.)

State that makes this work without hooking every write path:

  sync_snapshot(entity, id, updated_at, deleted, data_json)
      the canonical payload we last emitted or imported for each record. Export
      diffs against it; import rebuilds it from the winning versions.
  sync_meta(key, value)
      holds last_ts so our emitted timestamps are strictly monotonic per device
      (a same-millisecond re-edit can't tie with its own prior version).

The engine owns its own aiosqlite connection to db_path, so two engines can run
against two databases in one process (used by the round-trip test).
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import aiosqlite

from . import merge as mergelib
from . import profile_map as pm
from . import settings_registry as sr
from .documents import DocumentStore
from .entities import DeferRecord, db_adapters

logger = logging.getLogger(__name__)

RECORD_VERSION = 1

# Bumped when the canonical wire format changes in a way that makes this
# device's previously-emitted records wrong (v2: fold triage into status;
# v3: split job facts from the `triage` decision entity). A device whose
# stored `sync_format` is older force-re-exports once. This is a LOCAL migration
# marker held in sync_meta, distinct from RECORD_VERSION (the per-record wire
# `v`, which stays 1 in lockstep with the Swift ChangeRecord default). Keep in
# sync with SyncEngine.syncFormatVersion on the iOS side.
#
# v4: adds the config-backed `setting` entity (per-key settings sync). A client
# that predates v4 receives `setting` records it has no adapter for and MUST skip
# them on import rather than error (both engines do — see the unknown-entity
# regression tests). Bumping forces a one-shot re-export so each upgraded device
# broadcasts its settings once.
SYNC_FORMAT_VERSION = 4

PROFILE_ENTITY = "profile"
PROFILE_ID = "me"
SETTING_ENTITY = "setting"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def iso_ms(dt: datetime) -> str:
    """RFC3339 UTC with millisecond precision and a 'Z' suffix."""
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _canon(data: dict) -> str:
    """Deterministic serialization for change-detection comparisons."""
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _prev_data(prev: Optional[dict]) -> dict:
    """The payload we last emitted/imported for a record (for overlay), or {}."""
    if not prev or prev["deleted"] or not prev.get("data_json"):
        return {}
    return json.loads(prev["data_json"])


@dataclass
class ExportStats:
    live: int = 0
    tombstones: int = 0

    @property
    def total(self) -> int:
        return self.live + self.tombstones


@dataclass
class ImportStats:
    upserts: int = 0
    deletes: int = 0
    deferred: int = 0
    profile_updated: bool = False
    settings_updated: int = 0
    deferred_keys: list = field(default_factory=list)


class SyncEngine:
    def __init__(
        self,
        db_path: str | Path,
        device_id: str,
        *,
        load_profile: Optional[Callable[[], Optional[dict]]] = None,
        save_profile: Optional[Callable[[dict], None]] = None,
        load_settings: Optional[Callable[[], Optional[dict]]] = None,
        save_settings: Optional[Callable[[dict], None]] = None,
        docs_dir: Optional[str | Path] = None,
        now_fn: Callable[[], datetime] = _now,
    ) -> None:
        self.db_path = str(db_path)
        self.device_id = device_id
        self._load_profile = load_profile
        self._save_profile = save_profile
        # Config-backed `setting` bridge (per-key LWW). load_settings returns the
        # whole config dict; save_settings persists it. Category gating is read
        # from that same dict, so the Profile toggle also gates the profile
        # bridge above (both derive from enabled_categories(cfg)).
        self._load_settings = load_settings
        self._save_settings = save_settings
        self._docs_dir = docs_dir
        self._now = now_fn

    def _enabled_categories(self) -> frozenset[str]:
        if self._load_settings is None:
            # No settings bridge wired: preserve pre-feature behavior — the
            # profile bridge stays unconditionally on.
            return frozenset({"profile"})
        cfg = self._load_settings() or {}
        return sr.enabled_categories(cfg)

    def _adapters(self, folder: Path):
        """Entity adapters for a run against `folder`, wired to its document
        store when a local materialize dir is configured."""
        store = None
        if self._docs_dir is not None:
            store = DocumentStore(folder / "documents", self._docs_dir)
        return db_adapters(store)

    # -- connection / bookkeeping tables -----------------------------------

    async def _connect(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sync_snapshot (
                entity     TEXT NOT NULL,
                id         TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted    INTEGER NOT NULL DEFAULT 0,
                data_json  TEXT,
                PRIMARY KEY (entity, id)
            );
            CREATE TABLE IF NOT EXISTS sync_meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        await conn.commit()
        return conn

    async def _load_snapshot(self, conn, entity: str) -> dict[str, dict]:
        cur = await conn.execute(
            "SELECT id, updated_at, deleted, data_json FROM sync_snapshot WHERE entity = ?",
            (entity,),
        )
        return {r["id"]: dict(r) for r in await cur.fetchall()}

    async def _put_snapshot(self, conn, entity, sid, updated_at, deleted, data_json):
        await conn.execute(
            """INSERT INTO sync_snapshot (entity, id, updated_at, deleted, data_json)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(entity, id) DO UPDATE SET
                 updated_at = excluded.updated_at,
                 deleted    = excluded.deleted,
                 data_json  = excluded.data_json""",
            (entity, sid, updated_at, 1 if deleted else 0, data_json),
        )

    async def _get_meta(self, conn, key) -> Optional[str]:
        cur = await conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def _set_meta(self, conn, key, value) -> None:
        await conn.execute(
            "INSERT INTO sync_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    async def _mark_migration_if_needed(self, conn) -> None:
        """Flag a one-time re-export when the wire format version has advanced
        and this device already holds old-format snapshot rows (a fresh device
        has nothing to migrate, so it must not re-broadcast what it just
        imported). The Swift twin is SyncEngine.markMigrationIfNeeded. Runs at
        the head of both export and import so whichever fires first — reading
        the pre-rebuild snapshot — decides; export consumes the flag."""
        raw = await self._get_meta(conn, "sync_format")
        try:
            stored = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            stored = 0
        if stored >= SYNC_FORMAT_VERSION:
            return
        cur = await conn.execute("SELECT COUNT(*) AS n FROM sync_snapshot")
        row = await cur.fetchone()
        if (row["n"] if row else 0) > 0:
            await self._set_meta(conn, "pending_migration", "1")
        await self._set_meta(conn, "sync_format", str(SYNC_FORMAT_VERSION))

    async def _next_ts(self, conn) -> str:
        """A strictly-increasing per-device timestamp so our own successive
        versions of a record never tie."""
        candidate = self._now().astimezone(timezone.utc)
        last = await self._get_meta(conn, "last_ts")
        if last:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if candidate <= last_dt:
                candidate = last_dt + timedelta(milliseconds=1)
        stamp = iso_ms(candidate)
        await self._set_meta(conn, "last_ts", stamp)
        return stamp

    async def _bump_ts_to(self, conn, stamp: str) -> None:
        """Ensure last_ts is at least `stamp` so future local edits win over
        records we just imported."""
        last = await self._get_meta(conn, "last_ts")
        if last is None or last < stamp:
            await self._set_meta(conn, "last_ts", stamp)

    # -- export ------------------------------------------------------------

    async def export_changes(self, folder: str | Path) -> ExportStats:
        folder = Path(folder)
        conn = await self._connect()
        stats = ExportStats()
        records: list[dict] = []
        try:
            await self._mark_migration_if_needed(conn)
            ts = await self._next_ts(conn)

            # One-time format migration: records this device emitted under the
            # old format carry the wrong canonical shape (a shortlisted job as
            # 'discovered', see entities). When flagged, re-emit every current
            # row once, ignoring the snapshot diff, so fresh correctly-folded
            # records supersede the stale ones for every other device. This also
            # defeats the case where a prior import rebuilt the snapshot to match
            # the DB and would otherwise suppress the fix.
            force = (await self._get_meta(conn, "pending_migration")) == "1"
            if force:
                await self._set_meta(conn, "pending_migration", "0")

            for adapter in self._adapters(folder):
                current = await adapter.snapshot(conn)
                snap = await self._load_snapshot(conn, adapter.entity)
                for sid, data in current.items():
                    prev = snap.get(sid)
                    # Overlay our known fields onto the last-seen payload so keys
                    # only another client models (e.g. iOS `style_preset`) are
                    # preserved verbatim — the spec's write-back invariant.
                    merged = {**_prev_data(prev), **data}
                    cj = _canon(merged)
                    if force or prev is None or prev["deleted"] or prev["data_json"] != cj:
                        records.append(self._live_record(adapter.entity, sid, ts, merged))
                        await self._put_snapshot(conn, adapter.entity, sid, ts, False, cj)
                        stats.live += 1
                for sid, prev in snap.items():
                    if not prev["deleted"] and sid not in current:
                        records.append(self._tombstone_record(adapter.entity, sid, ts))
                        await self._put_snapshot(conn, adapter.entity, sid, ts, True, None)
                        stats.tombstones += 1

            enabled = self._enabled_categories()

            # Profile bridge — GATED by the `profile` category. When off we skip
            # export entirely (and, crucially, never tombstone profile/me), so
            # turning it off just keeps a device-specific profile.
            if self._load_profile is not None and "profile" in enabled:
                prof = self._load_profile()
                if prof is not None:
                    canon_prof = pm.desktop_to_canonical(prof)
                    cj = _canon(canon_prof)
                    snap = await self._load_snapshot(conn, PROFILE_ENTITY)
                    prev = snap.get(PROFILE_ID)
                    if force or prev is None or prev["deleted"] or prev["data_json"] != cj:
                        records.append(
                            self._live_record(PROFILE_ENTITY, PROFILE_ID, ts, canon_prof)
                        )
                        await self._put_snapshot(
                            conn, PROFILE_ENTITY, PROFILE_ID, ts, False, cj
                        )
                        stats.live += 1

            # Settings bridge — one record per enabled canonical path. Export is
            # category-gated inside export_settings; a path only tombstones when
            # its category is still on (a genuine removal), never merely because
            # the category was switched off.
            if self._load_settings is not None:
                cfg = self._load_settings()
                if cfg is not None:
                    current = sr.export_settings(cfg)
                    snap = await self._load_snapshot(conn, SETTING_ENTITY)
                    for path, data in current.items():
                        cj = _canon(data)
                        prev = snap.get(path)
                        if force or prev is None or prev["deleted"] or prev["data_json"] != cj:
                            records.append(self._live_record(SETTING_ENTITY, path, ts, data))
                            await self._put_snapshot(conn, SETTING_ENTITY, path, ts, False, cj)
                            stats.live += 1
                    for path, prev in snap.items():
                        if prev["deleted"] or path in current:
                            continue
                        cat = sr.category_for_path(path)
                        if cat is None or cat not in enabled:
                            continue  # category off / unknown: don't broadcast a delete
                        records.append(self._tombstone_record(SETTING_ENTITY, path, ts))
                        await self._put_snapshot(conn, SETTING_ENTITY, path, ts, True, None)
                        stats.tombstones += 1

            if records:
                self._append_log(folder, records)
            await conn.commit()
        finally:
            await conn.close()
        return stats

    def _live_record(self, entity, sid, ts, data) -> dict:
        return {
            "v": RECORD_VERSION,
            "entity": entity,
            "id": sid,
            "updated_at": ts,
            "device": self.device_id,
            "deleted": False,
            "data": data,
        }

    def _tombstone_record(self, entity, sid, ts) -> dict:
        return {
            "v": RECORD_VERSION,
            "entity": entity,
            "id": sid,
            "updated_at": ts,
            "device": self.device_id,
            "deleted": True,
        }

    def _append_log(self, folder: Path, records: list[dict]) -> None:
        changes = folder / "changes"
        changes.mkdir(parents=True, exist_ok=True)
        log = changes / f"{self.device_id}.jsonl"
        payload = "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in records)
        # Append under an exclusive advisory lock and fsync before releasing, so a
        # concurrent cloud uploader (or another writer) can never capture a torn
        # final line. flock/fsync are POSIX-only — every platform this backend
        # runs on (macOS / Linux / Docker).
        with log.open("a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    # -- import ------------------------------------------------------------

    async def import_changes(self, folder: str | Path) -> ImportStats:
        folder = Path(folder)
        records = mergelib.load_logs(folder) if (folder / "changes").exists() else []
        winners = self._winners(records)

        conn = await self._connect()
        stats = ImportStats()
        adapters = self._adapters(folder)
        adapters_by_entity = {a.entity: a for a in adapters}
        try:
            await self._mark_migration_if_needed(conn)
            # Live upserts, dependency order (job facts -> triage -> answer ->
            # application). A record whose dependency isn't present yet defers
            # and resolves on a later import.
            deferred: set[tuple[str, str]] = set()
            for adapter in adapters:
                for (entity, sid), rec in winners.items():
                    if entity != adapter.entity or rec.get("deleted"):
                        continue
                    try:
                        await adapter.apply_live(conn, sid, rec.get("data", {}))
                        stats.upserts += 1
                    except DeferRecord as e:
                        deferred.add((entity, sid))
                        stats.deferred += 1
                        stats.deferred_keys.append(f"{entity}:{sid}")
                        logger.info("sync import deferred: %s", e)

            # Tombstones, reverse dependency order (children before parents). A
            # job delete is NOT here — it is a live `triage` record whose status
            # is 'deleted', applied by the upsert loop above.
            for adapter in reversed(adapters):
                for (entity, sid), rec in winners.items():
                    if entity == adapter.entity and rec.get("deleted"):
                        await adapter.apply_tombstone(conn, sid)
                        stats.deletes += 1

            enabled = self._enabled_categories()

            # Profile — GATED by the `profile` category (symmetric with export).
            prof_winner = winners.get((PROFILE_ENTITY, PROFILE_ID))
            if (
                prof_winner
                and not prof_winner.get("deleted")
                and self._load_profile is not None
                and self._save_profile is not None
                and "profile" in enabled
            ):
                base = self._load_profile() or {}
                merged = pm.canonical_to_desktop(prof_winner["data"], base=base)
                self._save_profile(merged)
                stats.profile_updated = True

            # Settings — apply each winning `setting/<path>` record whose category
            # is enabled here (import gating). Unknown/disabled paths are skipped;
            # a key absent from the registry is never written (apply_setting no-ops).
            if self._load_settings is not None and self._save_settings is not None:
                setting_winners = {
                    k: r for k, r in winners.items() if k[0] == SETTING_ENTITY
                }
                if setting_winners:
                    cfg = self._load_settings() or {}
                    changed = 0
                    for (_, path), rec in setting_winners.items():
                        cat = sr.category_for_path(path)
                        if cat is None or cat not in enabled:
                            continue
                        if rec.get("deleted"):
                            sr.remove_setting(cfg, path)
                        else:
                            sr.apply_setting(cfg, path, (rec.get("data") or {}).get("value"))
                        changed += 1
                    if changed:
                        self._save_settings(cfg)
                        stats.settings_updated = changed

            await self._rebuild_snapshot(conn, winners, adapters_by_entity, deferred)

            # Keep our clock ahead of everything we've seen.
            if winners:
                await self._bump_ts_to(conn, max(r["updated_at"] for r in winners.values()))

            await conn.commit()
        finally:
            await conn.close()
        return stats

    def _winners(self, records: list[dict]) -> dict[tuple[str, str], dict]:
        winners: dict[tuple[str, str], dict] = {}
        for rec in records:
            key = (rec["entity"], rec["id"])
            if key not in winners or mergelib._wins(rec, winners[key]):
                winners[key] = rec
        return winners

    async def _rebuild_snapshot(self, conn, winners, adapters_by_entity, deferred) -> None:
        """Record what we imported so a subsequent export emits nothing. Live
        payloads are re-read from the DB (not taken from the record) so any
        storage round-trip difference can't masquerade as a local change."""
        reread: dict[str, dict[str, dict]] = {}
        for entity, adapter in adapters_by_entity.items():
            reread[entity] = await adapter.snapshot(conn)

        # Re-read the settings we'd emit next so a subsequent export is a no-op;
        # only records whose category is enabled were actually applied.
        settings_reread: dict[str, dict] = {}
        settings_enabled: frozenset[str] = frozenset()
        if self._load_settings is not None and any(
            e == SETTING_ENTITY for (e, _) in winners
        ):
            cfg = self._load_settings() or {}
            settings_enabled = sr.enabled_categories(cfg)
            settings_reread = sr.export_settings(cfg)

        for (entity, sid), rec in winners.items():
            if rec.get("deleted"):
                if entity == SETTING_ENTITY and (
                    sr.category_for_path(sid) is None
                    or sr.category_for_path(sid) not in settings_enabled
                ):
                    continue  # disabled/unknown: we never touched it, leave snapshot
                await self._put_snapshot(conn, entity, sid, rec["updated_at"], True, None)
                continue
            if (entity, sid) in deferred:
                continue  # not applied; leave it to retry on a later import
            if entity == SETTING_ENTITY:
                # Skip categories we didn't apply so re-enabling later still
                # exports our local value (diff vs an untouched snapshot).
                cat = sr.category_for_path(sid)
                if cat is None or cat not in settings_enabled:
                    continue
                data = settings_reread.get(sid)
                data_json = _canon(data) if data is not None else _canon(rec.get("data", {}))
                await self._put_snapshot(conn, entity, sid, rec["updated_at"], False, data_json)
                continue
            if entity == PROFILE_ENTITY:
                # What we'd emit next is desktop_to_canonical(saved config);
                # store that so an export right after import is a no-op.
                prof = self._load_profile() if self._load_profile else None
                data_json = _canon(pm.desktop_to_canonical(prof)) if prof is not None else _canon(rec["data"])
            else:
                # Overlay our re-read columns onto the winner's full payload so
                # keys we don't model survive, but a storage round-trip of the
                # keys we do model can't masquerade as a local change.
                known = reread.get(entity, {}).get(sid)
                if known is None:
                    continue
                data_json = _canon({**rec.get("data", {}), **known})
            await self._put_snapshot(conn, entity, sid, rec["updated_at"], False, data_json)
