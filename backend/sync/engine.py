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

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import aiosqlite

from . import merge as mergelib
from . import profile_map as pm
from .documents import DocumentStore
from .entities import DeferRecord, db_adapters

logger = logging.getLogger(__name__)

RECORD_VERSION = 1
PROFILE_ENTITY = "profile"
PROFILE_ID = "me"

# Canonical job statuses that do NOT count as the user keeping/advancing a job:
# a fresh re-discovery and a dismiss. A deletion is resurrected only by a newer
# live record whose status is anything else (shortlisted or a pipeline stage).
_UNENGAGED_STATUSES = frozenset({"discovered", "passed"})


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
    deferred_keys: list = field(default_factory=list)


class SyncEngine:
    def __init__(
        self,
        db_path: str | Path,
        device_id: str,
        *,
        load_profile: Optional[Callable[[], Optional[dict]]] = None,
        save_profile: Optional[Callable[[dict], None]] = None,
        docs_dir: Optional[str | Path] = None,
        now_fn: Callable[[], datetime] = _now,
    ) -> None:
        self.db_path = str(db_path)
        self.device_id = device_id
        self._load_profile = load_profile
        self._save_profile = save_profile
        self._docs_dir = docs_dir
        self._now = now_fn

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
            CREATE TABLE IF NOT EXISTS deleted_jobs (
                sync_id    TEXT PRIMARY KEY,
                deleted_at TEXT NOT NULL
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

    async def _load_deletions(self, conn) -> dict[str, str]:
        """{sync_id: deleted_at} — durable job tombstones (see database.py).
        The `job` entity's sync id is exactly this table's sync_id."""
        cur = await conn.execute("SELECT sync_id, deleted_at FROM deleted_jobs")
        return {r["sync_id"]: r["deleted_at"] for r in await cur.fetchall()}

    async def _put_deletion(self, conn, sync_id, deleted_at) -> None:
        await conn.execute(
            "INSERT INTO deleted_jobs (sync_id, deleted_at) VALUES (?, ?) "
            "ON CONFLICT(sync_id) DO UPDATE SET deleted_at = excluded.deleted_at",
            (sync_id, deleted_at),
        )

    async def _clear_deletion(self, conn, sync_id) -> None:
        await conn.execute("DELETE FROM deleted_jobs WHERE sync_id = ?", (sync_id,))

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
            ts = await self._next_ts(conn)
            deletions = await self._load_deletions(conn)

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
                    if prev is None or prev["deleted"] or prev["data_json"] != cj:
                        records.append(self._live_record(adapter.entity, sid, ts, merged))
                        await self._put_snapshot(conn, adapter.entity, sid, ts, False, cj)
                        stats.live += 1
                for sid, prev in snap.items():
                    # Job deletions are broadcast authoritatively from deleted_jobs
                    # (below) at their recorded time, not inferred here at export
                    # time — skip them so we don't double-emit with a wrong clock.
                    if adapter.entity == "job" and sid in deletions:
                        continue
                    if not prev["deleted"] and sid not in current:
                        records.append(self._tombstone_record(adapter.entity, sid, ts))
                        await self._put_snapshot(conn, adapter.entity, sid, ts, True, None)
                        stats.tombstones += 1

            # Durable job tombstones: broadcast each recorded deletion once, at
            # the time it happened (the LWW clock), so a peer's older live record
            # loses and the row stays gone everywhere.
            job_snap = await self._load_snapshot(conn, "job")
            for sid, del_at in deletions.items():
                prev = job_snap.get(sid)
                if prev is None or not prev["deleted"] or prev["updated_at"] != del_at:
                    records.append(self._tombstone_record("job", sid, del_at))
                    await self._put_snapshot(conn, "job", sid, del_at, True, None)
                    stats.tombstones += 1

            if self._load_profile is not None:
                prof = self._load_profile()
                if prof is not None:
                    canon_prof = pm.desktop_to_canonical(prof)
                    cj = _canon(canon_prof)
                    snap = await self._load_snapshot(conn, PROFILE_ENTITY)
                    prev = snap.get(PROFILE_ID)
                    if prev is None or prev["deleted"] or prev["data_json"] != cj:
                        records.append(
                            self._live_record(PROFILE_ENTITY, PROFILE_ID, ts, canon_prof)
                        )
                        await self._put_snapshot(
                            conn, PROFILE_ENTITY, PROFILE_ID, ts, False, cj
                        )
                        stats.live += 1

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
        with log.open("a", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

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
            deletions = await self._load_deletions(conn)
            job_adapter = adapters_by_entity.get("job")

            # A deletion is overridden only by a newer *engaged* live record —
            # one where the user kept/advanced the job (shortlisted or into the
            # pipeline). A plain re-discovery ('discovered') or a dismiss
            # ('passed') never resurrects a delete. Find the newest engaged live
            # timestamp per job so a stale tombstone can't nuke a job the user
            # just shortlisted on another device.
            newest_engaged: dict[str, str] = {}
            for rec in records:
                if rec.get("entity") == "job" and not rec.get("deleted"):
                    st = (rec.get("data") or {}).get("status", "discovered")
                    if st not in _UNENGAGED_STATUSES:
                        sid, ts = rec["id"], rec["updated_at"]
                        if ts > newest_engaged.get(sid, ""):
                            newest_engaged[sid] = ts

            # Fold job tombstones into the durable deletion set (and drop the
            # row) unless a newer engaged live record supersedes them. This is
            # how a re-fetched posting stays deleted while a shortlist wins.
            for rec in records:
                if rec.get("entity") != "job" or not rec.get("deleted"):
                    continue
                sid, ts = rec["id"], rec["updated_at"]
                if newest_engaged.get(sid, "") > ts:
                    continue  # user re-engaged after this delete — keep the job
                if sid not in deletions:
                    deletions[sid] = ts
                    await self._put_deletion(conn, sid, ts)
                    if job_adapter is not None:
                        await job_adapter.apply_tombstone(conn, sid)
                        stats.deletes += 1
                elif ts > deletions[sid]:
                    deletions[sid] = ts
                    await self._put_deletion(conn, sid, ts)

            # Live upserts, FK order (job -> answer -> application).
            deferred: set[tuple[str, str]] = set()
            for adapter in adapters:
                for (entity, sid), rec in winners.items():
                    if entity != adapter.entity or rec.get("deleted"):
                        continue
                    # A tombstoned job comes back only via a newer engaged live
                    # record (shortlist/pipeline); a re-fetch stays deleted.
                    if entity == "job" and sid in deletions:
                        st = (rec.get("data") or {}).get("status", "discovered")
                        if rec["updated_at"] > deletions[sid] and st not in _UNENGAGED_STATUSES:
                            await self._clear_deletion(conn, sid)
                            del deletions[sid]
                        else:
                            continue
                    try:
                        await adapter.apply_live(conn, sid, rec.get("data", {}))
                        stats.upserts += 1
                    except DeferRecord as e:
                        deferred.add((entity, sid))
                        stats.deferred += 1
                        stats.deferred_keys.append(f"{entity}:{sid}")
                        logger.info("sync import deferred: %s", e)

            # Tombstones, reverse FK order (children before parents). Jobs are
            # already handled by the permanent-latch fold above.
            for adapter in reversed(adapters):
                if adapter.entity == "job":
                    continue
                for (entity, sid), rec in winners.items():
                    if entity == adapter.entity and rec.get("deleted"):
                        await adapter.apply_tombstone(conn, sid)
                        stats.deletes += 1

            # Profile.
            prof_winner = winners.get((PROFILE_ENTITY, PROFILE_ID))
            if (
                prof_winner
                and not prof_winner.get("deleted")
                and self._load_profile is not None
                and self._save_profile is not None
            ):
                base = self._load_profile() or {}
                merged = pm.canonical_to_desktop(prof_winner["data"], base=base)
                self._save_profile(merged)
                stats.profile_updated = True

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

        for (entity, sid), rec in winners.items():
            if rec.get("deleted"):
                await self._put_snapshot(conn, entity, sid, rec["updated_at"], True, None)
                continue
            if (entity, sid) in deferred:
                continue  # not applied; leave it to retry on a later import
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
