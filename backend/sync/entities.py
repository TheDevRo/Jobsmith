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

Lifecycle decision (the `triage` entity):
  The user's lifecycle decision about a job — discovered | shortlisted | passed |
  deleted | <pipeline sub-stage> — travels as its OWN entity (`triage`), keyed by
  the job's sync id, NOT as a field on `job`. This split is deliberate: posting
  facts (title, salary, …) churn whenever a fetcher re-enriches a listing, while
  the decision changes only when the user acts. Keeping them in one record made a
  local fact-backfill re-emit the local status and clobber a decision another
  device had just made. As separate entities they are independent last-writer-wins
  streams, so a shortlist and a delete each win purely on their own timestamp.

  A delete is simply status='deleted' — an ordinary live value, never a tombstone —
  so shortlist and delete are symmetric and need no side table or resurrection
  heuristics. The desktop stores the decision on its single `jobs.status` column;
  iOS folds its (triage, status) pair into the same canonical `status`
  (SyncEntities.foldStatus / unfoldStatus).
"""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)


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

    # Portable POSTING FACTS only. `status` is deliberately NOT here — the user's
    # lifecycle decision travels as the separate `triage` entity (see TriageAdapter)
    # so fact churn never races with a decision. Excludes volatile/local too:
    # last_seen, times_seen, estimated_salary_* and salary/quality caches.
    SCALAR = (
        "source", "external_id", "title", "company", "location", "url",
        "description", "salary_min", "salary_max", "salary_period",
        "date_posted", "date_discovered", "fit_score",
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
        await conn.execute(
            """DELETE FROM application_events WHERE application_id IN
               (SELECT id FROM applications WHERE job_id = ?)""",
            (row["id"],),
        )
        await conn.execute("DELETE FROM applications WHERE job_id = ?", (row["id"],))
        await conn.execute("DELETE FROM activity_log WHERE job_id = ?", (row["id"],))
        await conn.execute("DELETE FROM jobs WHERE id = ?", (row["id"],))


# ---------------------------------------------------------------------------
# triage  (the user's lifecycle decision about a job)
# ---------------------------------------------------------------------------

class TriageAdapter:
    """The user's lifecycle decision for a job, keyed by the job's sync id.

    Carries one canonical `status`:
        discovered | shortlisted | passed | deleted | <pipeline sub-stage>
    A delete is just status='deleted' — a normal last-writer-wins value, not a
    tombstone — so shortlist and delete are symmetric. Backed by the desktop's
    `jobs.status` column; a decision for a job whose facts haven't been imported
    yet defers and applies on a later import (same pattern as `application`)."""

    entity = "triage"

    async def snapshot(self, conn: aiosqlite.Connection) -> dict[str, dict]:
        cur = await conn.execute(
            "SELECT source, external_id, status FROM jobs "
            "WHERE external_id IS NOT NULL AND external_id != ''"
        )
        out: dict[str, dict] = {}
        for row in await cur.fetchall():
            r = dict(row)
            out[JobAdapter.sync_id(r["source"], r["external_id"])] = {
                "status": r["status"] or "discovered"
            }
        return out

    async def apply_live(self, conn: aiosqlite.Connection, sync_id: str, data: dict) -> None:
        source, _, external_id = sync_id.partition(":")
        cur = await conn.execute(
            "SELECT id FROM jobs WHERE source = ? AND external_id = ?",
            (source, external_id),
        )
        row = await cur.fetchone()
        if not row:
            raise DeferRecord(f"triage {sync_id}: job facts not present yet")
        await conn.execute(
            "UPDATE jobs SET status = ? WHERE id = ?",
            (data.get("status", "discovered"), row["id"]),
        )

    async def apply_tombstone(self, conn: aiosqlite.Connection, sync_id: str) -> None:
        # A decision is never tombstoned — 'deleted' is a live status value.
        return


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------

class ApplicationAdapter:
    entity = "application"

    # Excludes auto_apply_attempts (local retry counter). The machine-local FS
    # path columns below are NOT synced as paths; their *content* travels as a
    # content-addressed document reference (resume_doc / cover_doc).
    #
    # `outcome`/`outcome_updated_at` are deliberately NOT here. Merging is
    # last-writer-wins over the WHOLE record, so carrying the outcome as a field
    # on `application` meant any unrelated edit on one device (a resume tweak)
    # would clobber an outcome the other device had just set. The outcome travels
    # as the append-only `application_event` entity instead, and this column is
    # recomputed from that history on import (see ApplicationEventAdapter) — the
    # same reasoning that split `triage` out of `job`.
    SCALAR = (
        "resume_content", "cover_letter_content", "status", "applied_at",
        "created_at", "error_message", "honesty_level",
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
        await conn.execute(
            "DELETE FROM application_events WHERE application_id = ?", (sync_id,)
        )
        await conn.execute("DELETE FROM applications WHERE id = ?", (sync_id,))


# ---------------------------------------------------------------------------
# application_event  (the post-apply outcome history)
# ---------------------------------------------------------------------------

class ApplicationEventAdapter:
    """One outcome transition for an application — append-only and immutable.

    Why its own entity rather than a field on `application`: last-writer-wins
    resolves whole records, so an outcome carried on the application would be
    silently dropped whenever the other device happened to touch any other field
    of that application. Events never change once written, so two devices can
    each record outcomes offline and the merge is simply their union — there is
    no conflict to lose. `applications.outcome` becomes a derived cache,
    recomputed from the merged history after every import.

    Identity: "{application_id}:{occurred_at}:{to_outcome}" — content-derived, so
    the same transition imported twice is the same record. Application ids are
    UUIDs (no colons), so the id splits cleanly on the first colon.
    """

    entity = "application_event"

    SCALAR = ("from_outcome", "to_outcome", "occurred_at", "note", "source")

    @staticmethod
    def sync_id(application_id: str, occurred_at: str, to_outcome: str) -> str:
        return f"{application_id}:{occurred_at}:{to_outcome}"

    async def snapshot(self, conn: aiosqlite.Connection) -> dict[str, dict]:
        cols = ", ".join(f"e.{c}" for c in self.SCALAR)
        cur = await conn.execute(
            f"""SELECT e.application_id, {cols}
                FROM application_events e
                JOIN applications a ON a.id = e.application_id
                JOIN jobs j ON j.id = a.job_id
                WHERE j.external_id IS NOT NULL AND j.external_id != ''"""
        )
        out: dict[str, dict] = {}
        for row in await cur.fetchall():
            r = dict(row)
            data: dict = {c: r[c] for c in self.SCALAR}
            data["application_ref"] = r["application_id"]
            out[self.sync_id(r["application_id"], r["occurred_at"], r["to_outcome"])] = data
        return out

    async def apply_live(self, conn: aiosqlite.Connection, sync_id: str, data: dict) -> None:
        app_id = data.get("application_ref") or sync_id.partition(":")[0]
        cur = await conn.execute("SELECT id FROM applications WHERE id = ?", (app_id,))
        if not await cur.fetchone():
            raise DeferRecord(f"application_event {sync_id}: application {app_id} not present yet")

        # Immutable: if this exact transition is already recorded, there is
        # nothing to update — re-importing it must not duplicate the row.
        cur = await conn.execute(
            """SELECT 1 FROM application_events
               WHERE application_id = ? AND occurred_at = ? AND to_outcome = ?""",
            (app_id, data.get("occurred_at"), data.get("to_outcome")),
        )
        if not await cur.fetchone():
            await conn.execute(
                """INSERT INTO application_events
                   (application_id, from_outcome, to_outcome, occurred_at, note, source)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (app_id, data.get("from_outcome"), data.get("to_outcome"),
                 data.get("occurred_at"), data.get("note"), data.get("source") or "user"),
            )
        await recompute_outcome(conn, app_id)

    async def apply_tombstone(self, conn: aiosqlite.Connection, sync_id: str) -> None:
        app_id, _, rest = sync_id.partition(":")
        occurred_at, _, to_outcome = rest.rpartition(":")
        await conn.execute(
            """DELETE FROM application_events
               WHERE application_id = ? AND occurred_at = ? AND to_outcome = ?""",
            (app_id, occurred_at, to_outcome),
        )
        await recompute_outcome(conn, app_id)


async def recompute_outcome(conn: aiosqlite.Connection, application_id: str) -> None:
    """Rebuild `applications.outcome` from the merged event history.

    Events are the source of truth; the column is a cache that keeps the existing
    queries and the funnel fast. The latest event wins.

    The tiebreak is `to_outcome`, NOT the rowid: rowids are local insertion order,
    so two devices holding the same two same-millisecond events would order them
    differently and derive *different* outcomes — the histories converge but the
    answer read off them doesn't. `(occurred_at, to_outcome)` is the event's sync
    identity, so it is a total order that every device computes identically.
    """
    cur = await conn.execute(
        """SELECT to_outcome, occurred_at FROM application_events
           WHERE application_id = ?
           ORDER BY occurred_at DESC, to_outcome DESC LIMIT 1""",
        (application_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return  # no history — leave the default 'awaiting' alone
    await conn.execute(
        "UPDATE applications SET outcome = ?, outcome_updated_at = ? WHERE id = ?",
        (row["to_outcome"], row["occurred_at"], application_id),
    )


# ---------------------------------------------------------------------------
# application_schedule  (follow-up / interview reminder dates)
# ---------------------------------------------------------------------------

class ApplicationScheduleAdapter:
    """The reminder dates for an application, keyed by the application's sync id.

    Its own entity for the same reason the outcome is: LWW resolves whole records,
    so dates carried on `application` would be wiped whenever the other device
    edited an unrelated field. Unlike events these ARE mutable (you reschedule an
    interview), so they stay last-writer-wins — but on their own independent
    stream, which is exactly the `triage` pattern.
    """

    entity = "application_schedule"

    SCALAR = ("follow_up_at", "interview_at")

    async def snapshot(self, conn: aiosqlite.Connection) -> dict[str, dict]:
        cols = ", ".join(f"a.{c}" for c in self.SCALAR)
        cur = await conn.execute(
            f"""SELECT a.id, {cols}
                FROM applications a JOIN jobs j ON j.id = a.job_id
                WHERE j.external_id IS NOT NULL AND j.external_id != ''
                  AND (a.follow_up_at IS NOT NULL OR a.interview_at IS NOT NULL)"""
        )
        out: dict[str, dict] = {}
        for row in await cur.fetchall():
            r = dict(row)
            out[r["id"]] = {c: r[c] for c in self.SCALAR}
        return out

    async def apply_live(self, conn: aiosqlite.Connection, sync_id: str, data: dict) -> None:
        cur = await conn.execute("SELECT id FROM applications WHERE id = ?", (sync_id,))
        if not await cur.fetchone():
            raise DeferRecord(f"application_schedule {sync_id}: application not present yet")
        await conn.execute(
            "UPDATE applications SET follow_up_at = ?, interview_at = ? WHERE id = ?",
            (data.get("follow_up_at"), data.get("interview_at"), sync_id),
        )

    async def apply_tombstone(self, conn: aiosqlite.Connection, sync_id: str) -> None:
        # Clearing both dates drops the row from the snapshot, which the engine
        # emits as a tombstone — so a tombstone means "no dates", not "no
        # application". Never delete the application itself here.
        await conn.execute(
            "UPDATE applications SET follow_up_at = NULL, interview_at = NULL WHERE id = ?",
            (sync_id,),
        )


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


# ---------------------------------------------------------------------------
# work_request  (cross-device hand-off: "score everything unscored")
# ---------------------------------------------------------------------------

class WorkRequestAdapter:
    """A hand-off command from another device, keyed by its own UUID.

    One flat LWW record: the requester writes it `pending`, the fulfiller
    re-emits it `done` with a newer timestamp, and last-writer-wins settles it
    everywhere. It carries no job references on purpose — the fulfilling side
    derives "what's left" from its own database (the jobs still unscored), the
    same way a locally resumed run does — so a request can't go stale against
    the work it names, and there is nothing to defer on.

    Fulfillment is NOT the adapter's business: importing a pending request just
    lands the row. Whether this desktop acts on it is a separate, off-by-default
    setting (sync.fulfill_work_requests) checked after the sync cycle.
    Must match the iOS side (SyncEngine.workRequestSnapshot/applyWorkRequest).
    """

    entity = "work_request"

    # Kinds this build knows how to fulfill. An unknown kind can never be
    # retired here (nothing serves it), so it would sit `pending` forever and
    # re-import every cycle — drop it on apply instead. A build that understands
    # the kind will re-import it from the origin device, which still holds it.
    KNOWN_KINDS = {"score_all"}

    SCALAR = ("kind", "status", "requested_by", "requested_at",
              "completed_by", "completed_at")
    JSON = ("params",)
    COLS = SCALAR + JSON

    async def snapshot(self, conn: aiosqlite.Connection) -> dict[str, dict]:
        cols = ", ".join(("id",) + self.COLS)
        cur = await conn.execute(f"SELECT {cols} FROM work_requests")
        out: dict[str, dict] = {}
        for row in await cur.fetchall():
            r = dict(row)
            data: dict = {c: r[c] for c in self.SCALAR}
            data["params"] = _loads(r["params"], {})
            out[r["id"]] = data
        return out

    async def apply_live(self, conn: aiosqlite.Connection, sync_id: str, data: dict) -> None:
        kind = data.get("kind")
        if kind not in self.KNOWN_KINDS:
            logger.warning("Dropping work_request %s of unknown kind %r", sync_id, kind)
            return
        values = {c: data.get(c) for c in self.SCALAR}
        values["status"] = values.get("status") or "pending"
        values["params"] = _dumps(data.get("params") or {})
        cols = ["id", *values.keys()]
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c} = excluded.{c}" for c in values)
        await conn.execute(
            f"""INSERT INTO work_requests ({', '.join(cols)}) VALUES ({placeholders})
                ON CONFLICT(id) DO UPDATE SET {updates}""",
            (sync_id, *values.values()),
        )

    async def apply_tombstone(self, conn: aiosqlite.Connection, sync_id: str) -> None:
        # The requester pruning old news — just drop the row.
        await conn.execute("DELETE FROM work_requests WHERE id = ?", (sync_id,))


# ---------------------------------------------------------------------------
# ats_account  (per-tenant ATS account registry — e.g. Workday)
# ---------------------------------------------------------------------------

class AtsAccountAdapter:
    """One remembered ATS account per company tenant, keyed by provider+host.

    Workday requires a separate account per tenant
    ({company}.wd{N}.myworkdayjobs.com). This registry lets every surface skip
    the "sign in vs create account" DOM heuristic once an account is known.

    Identity: "{provider}:{tenant_host}" (provider has no colon; the host is the
    remainder). A flat last-writer-wins record. NEVER carries a password."""

    entity = "ats_account"

    SCALAR = ("provider", "tenant_host", "email", "status",
              "created_at", "last_sign_in_at", "updated_at")

    @staticmethod
    def sync_id(provider: str, tenant_host: str) -> str:
        return f"{provider}:{tenant_host}"

    async def snapshot(self, conn: aiosqlite.Connection) -> dict[str, dict]:
        cols = ", ".join(self.SCALAR)
        cur = await conn.execute(f"SELECT {cols} FROM ats_accounts")
        out: dict[str, dict] = {}
        for row in await cur.fetchall():
            r = dict(row)
            out[self.sync_id(r["provider"], r["tenant_host"])] = {c: r[c] for c in self.SCALAR}
        return out

    async def apply_live(self, conn: aiosqlite.Connection, sync_id: str, data: dict) -> None:
        provider, _, tenant_host = sync_id.partition(":")
        values = {c: data.get(c) for c in self.SCALAR}
        # The id is authoritative for the key columns; never trust the payload
        # to disagree with it.
        values["provider"] = data.get("provider") or provider
        values["tenant_host"] = data.get("tenant_host") or tenant_host
        cols = list(values.keys())
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "tenant_host")
        await conn.execute(
            f"""INSERT INTO ats_accounts ({', '.join(cols)}) VALUES ({placeholders})
                ON CONFLICT(tenant_host) DO UPDATE SET {updates}""",
            tuple(values.values()),
        )

    async def apply_tombstone(self, conn: aiosqlite.Connection, sync_id: str) -> None:
        _, _, tenant_host = sync_id.partition(":")
        await conn.execute("DELETE FROM ats_accounts WHERE tenant_host = ?", (tenant_host,))


# Live-apply order respects dependencies (a job's facts must exist before its
# triage decision or its application, and an application before its events).
# Tombstones are applied in reverse so children go before parents.
def db_adapters(document_store=None):
    return (
        JobAdapter(),
        TriageAdapter(),
        AnswerAdapter(),
        ApplicationAdapter(document_store),
        ApplicationEventAdapter(),
        ApplicationScheduleAdapter(),
        WorkRequestAdapter(),
        AtsAccountAdapter(),
    )


DB_ADAPTERS = db_adapters()
