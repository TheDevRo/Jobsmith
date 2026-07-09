"""Entity adapters — map SQLite rows to/from sync `data` payloads.

Each adapter knows, for one entity:
  * `snapshot(conn)`      — the current synced state as {sync_id: data}
  * `apply_live(conn, id, data)`     — upsert a winning record
  * `apply_tombstone(conn, id)`      — delete a record whose winner is a delete

`data` is the payload that travels in a change record (see spec/FORMAT.md). It
holds only the *portable, user-meaningful* columns — local/volatile columns
(fetch counters, caches, machine-specific file paths, retry counts) are
deliberately excluded so they never cause sync churn or leak between machines.

Identity (must match every other implementation):
  * job          — "{source}:{external_id}"   (jobs without an external_id have
                   no stable cross-device key and are not synced)
  * application  — its own UUID; the parent job travels as `job_ref`
                   ("{source}:{external_id}"), resolved to a local job on import
  * answer       — the normalized question text (qa_cache's natural key)

Booleans are carried as JSON true/false; JSON-string columns (tags, reports,
custom_answers) are carried as parsed objects so the payload is clean and
language-neutral.

Lifecycle field (status vs. triage):
  The desktop models the inbox/shortlist/dismiss lifecycle on the single `job.status`
  column (discovered | shortlisted | passed | <pipeline sub-stage>). iOS splits it
  across two columns — `triage` (new | shortlisted | dismissed) and `status` (the
  pipeline sub-stage). The canonical wire uses the desktop's `status` vocabulary, so
  the desktop maps 1:1 here while iOS folds its (triage, status) pair into `status`
  on export and unfolds it on import (SyncEntities.foldStatus / unfoldStatus). That
  is why `triage` is NOT a synced column on this side: a shortlist or a dismiss on
  either app must reach the other's Pipeline/Inbox through `status` alone.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Optional

import aiosqlite


class DeferRecord(Exception):
    """Raised by apply_live when a dependency isn't present yet (e.g. an
    application whose job hasn't been imported). The engine skips the record;
    it resolves on a later import once the dependency arrives."""


def _loads(raw, default):
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def _dumps(value) -> Optional[str]:
    return None if value is None else json.dumps(value)


# ---------------------------------------------------------------------------
# job
# ---------------------------------------------------------------------------

class JobAdapter:
    entity = "job"

    # Portable columns. Excludes volatile/local: last_seen, times_seen, the
    # estimated_salary_* and salary/quality caches (all re-derivable per device).
    SCALAR = (
        "source", "external_id", "title", "company", "location", "url",
        "description", "salary_min", "salary_max", "salary_period",
        "date_posted", "date_discovered", "status", "fit_score",
        "fit_reasoning", "apply_type",
    )
    BOOL = ("is_remote", "is_easy_apply")
    JSON = ("tags", "match_report", "embellishment_log")
    COLS = SCALAR + BOOL + JSON

    @staticmethod
    def sync_id(source: str, external_id: str) -> str:
        return f"{source}:{external_id}"

    async def snapshot(self, conn: aiosqlite.Connection) -> dict[str, dict]:
        cols = ", ".join(self.COLS)
        cur = await conn.execute(
            f"SELECT {cols} FROM jobs WHERE external_id IS NOT NULL AND external_id != ''"
        )
        out: dict[str, dict] = {}
        for row in await cur.fetchall():
            r = dict(row)
            data: dict = {c: r[c] for c in self.SCALAR}
            for c in self.BOOL:
                data[c] = bool(r[c])
            for c in self.JSON:
                data[c] = _loads(r[c], [] if c == "tags" else None)
            out[self.sync_id(r["source"], r["external_id"])] = data
        return out

    async def apply_live(self, conn: aiosqlite.Connection, sync_id: str, data: dict) -> None:
        source, external_id = data["source"], data["external_id"]
        values = {c: data.get(c) for c in self.SCALAR}
        for c in self.BOOL:
            values[c] = 1 if data.get(c) else 0
        for c in self.JSON:
            values[c] = _dumps(data.get(c))
        cur = await conn.execute(
            "SELECT id FROM jobs WHERE source = ? AND external_id = ?",
            (source, external_id),
        )
        existing = await cur.fetchone()
        if existing:
            assignments = ", ".join(f"{c} = ?" for c in values)
            await conn.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?",
                (*values.values(), existing["id"]),
            )
        else:
            cols = ["id", *values.keys()]
            placeholders = ", ".join("?" for _ in cols)
            await conn.execute(
                f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({placeholders})",
                (str(uuid.uuid4()), *values.values()),
            )

    async def apply_tombstone(self, conn: aiosqlite.Connection, sync_id: str) -> None:
        source, _, external_id = sync_id.partition(":")
        cur = await conn.execute(
            "SELECT id FROM jobs WHERE source = ? AND external_id = ?",
            (source, external_id),
        )
        row = await cur.fetchone()
        if not row:
            return
        # Applications can't outlive their job; clear children so the FK holds.
        await conn.execute("DELETE FROM applications WHERE job_id = ?", (row["id"],))
        await conn.execute("DELETE FROM activity_log WHERE job_id = ?", (row["id"],))
        await conn.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------

class ApplicationAdapter:
    entity = "application"

    # Excludes auto_apply_attempts (local retry counter). The machine-local FS
    # path columns below are NOT synced as paths; their *content* travels as a
    # content-addressed document reference (resume_doc / cover_doc).
    SCALAR = (
        "resume_content", "cover_letter_content", "status", "applied_at",
        "created_at", "error_message", "outcome", "outcome_updated_at",
        "honesty_level",
    )
    BOOL = ("auto_approved",)
    JSON = ("custom_answers",)
    # canonical doc-ref key -> local path column
    DOCS = {"resume_doc": "tailored_resume_path", "cover_doc": "tailored_cover_letter_path"}
    COLS = ("id",) + SCALAR + BOOL + JSON

    def __init__(self, document_store=None):
        self.docs = document_store

    async def snapshot(self, conn: aiosqlite.Connection) -> dict[str, dict]:
        cols = ", ".join(f"a.{c}" for c in self.COLS)
        path_cols = ", ".join(f"a.{c}" for c in self.DOCS.values())
        cur = await conn.execute(
            f"""SELECT {cols}, {path_cols}, j.source AS _src, j.external_id AS _ext
                FROM applications a JOIN jobs j ON j.id = a.job_id
                WHERE j.external_id IS NOT NULL AND j.external_id != ''"""
        )
        out: dict[str, dict] = {}
        for row in await cur.fetchall():
            r = dict(row)
            data: dict = {c: r[c] for c in self.SCALAR}
            for c in self.BOOL:
                data[c] = bool(r[c])
            for c in self.JSON:
                data[c] = _loads(r[c], {})
            data["job_ref"] = JobAdapter.sync_id(r["_src"], r["_ext"])
            if self.docs is not None:
                for ref_key, path_col in self.DOCS.items():
                    path = r[path_col]
                    if path and Path(path).is_file():
                        data[ref_key] = self.docs.put(path)
            out[r["id"]] = data
        return out

    async def apply_live(self, conn: aiosqlite.Connection, sync_id: str, data: dict) -> None:
        source, _, external_id = data["job_ref"].partition(":")
        cur = await conn.execute(
            "SELECT id FROM jobs WHERE source = ? AND external_id = ?",
            (source, external_id),
        )
        job = await cur.fetchone()
        if not job:
            raise DeferRecord(f"application {sync_id}: job {data['job_ref']} not present yet")
        job_id = job["id"]

        values = {c: data.get(c) for c in self.SCALAR}
        for c in self.BOOL:
            values[c] = 1 if data.get(c) else 0
        for c in self.JSON:
            values[c] = _dumps(data.get(c))

        cur = await conn.execute("SELECT id FROM applications WHERE id = ?", (sync_id,))
        if await cur.fetchone():
            assignments = ", ".join(f"{c} = ?" for c in ("job_id", *values.keys()))
            await conn.execute(
                f"UPDATE applications SET {assignments} WHERE id = ?",
                (job_id, *values.values(), sync_id),
            )
        else:
            cols = ["id", "job_id", *values.keys()]
            placeholders = ", ".join("?" for _ in cols)
            await conn.execute(
                f"INSERT INTO applications ({', '.join(cols)}) VALUES ({placeholders})",
                (sync_id, job_id, *values.values()),
            )

        # Materialize any synced documents into local files. A ref whose blob
        # hasn't arrived yet leaves the path untouched — it fills in on a later
        # import (spec: "document syncing").
        if self.docs is not None:
            for ref_key, path_col in self.DOCS.items():
                ref = data.get(ref_key)
                if not ref:
                    continue
                base = f"{sync_id}_{ref_key.replace('_doc', '')}"
                local = self.docs.materialize(ref, base)
                if local:
                    await conn.execute(
                        f"UPDATE applications SET {path_col} = ? WHERE id = ?",
                        (local, sync_id),
                    )

    async def apply_tombstone(self, conn: aiosqlite.Connection, sync_id: str) -> None:
        await conn.execute("DELETE FROM applications WHERE id = ?", (sync_id,))


# ---------------------------------------------------------------------------
# answer (qa_cache)
# ---------------------------------------------------------------------------

class AnswerAdapter:
    entity = "answer"

    SCALAR = ("question_normalized", "answer", "confidence", "source",
              "created_at", "updated_at")

    async def snapshot(self, conn: aiosqlite.Connection) -> dict[str, dict]:
        cols = ", ".join(self.SCALAR)
        cur = await conn.execute(f"SELECT {cols} FROM qa_cache")
        out: dict[str, dict] = {}
        for row in await cur.fetchall():
            r = dict(row)
            out[r["question_normalized"]] = {c: r[c] for c in self.SCALAR}
        return out

    async def apply_live(self, conn: aiosqlite.Connection, sync_id: str, data: dict) -> None:
        values = {c: data.get(c) for c in self.SCALAR}
        values["question_normalized"] = sync_id
        cols = list(values.keys())
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "question_normalized")
        await conn.execute(
            f"""INSERT INTO qa_cache ({', '.join(cols)}) VALUES ({placeholders})
                ON CONFLICT(question_normalized) DO UPDATE SET {updates}""",
            tuple(values.values()),
        )

    async def apply_tombstone(self, conn: aiosqlite.Connection, sync_id: str) -> None:
        await conn.execute(
            "DELETE FROM qa_cache WHERE question_normalized = ?", (sync_id,)
        )


# Live-apply order respects FK dependencies (a job must exist before its
# application). Tombstones are applied in reverse so children go before parents.
def db_adapters(document_store=None):
    return (JobAdapter(), AnswerAdapter(), ApplicationAdapter(document_store))


DB_ADAPTERS = db_adapters()
