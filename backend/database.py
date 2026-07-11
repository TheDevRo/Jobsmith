"""
database.py — SQLite database layer for Jobsmith.

Uses aiosqlite for async operations. All tables are created on first run.
Provides helper functions for jobs, applications, and activity logging.
"""

import uuid
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
from .paths import project_root

logger = logging.getLogger(__name__)

DB_PATH = project_root() / "data" / "jobsmith.db"


async def _get_db() -> aiosqlite.Connection:
    """Open a connection with row_factory enabled."""
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db() -> None:
    """Create all tables if they do not exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await _get_db()
    try:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id              TEXT PRIMARY KEY,
                source          TEXT NOT NULL,
                external_id     TEXT,
                title           TEXT NOT NULL,
                company         TEXT,
                location        TEXT,
                url             TEXT,
                description     TEXT,
                salary_min      INTEGER,
                salary_max      INTEGER,
                salary_period   TEXT DEFAULT 'unknown',
                tags            TEXT DEFAULT '[]',
                date_posted     TEXT,
                date_discovered TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status          TEXT DEFAULT 'discovered',
                fit_score       REAL,
                fit_reasoning   TEXT,
                -- Structured skill/keyword gap breakdown from scoring (JSON):
                --   matched_skills, missing_skills, matched_soft_skills,
                --   missing_soft_skills, title_alignment, keywords
                match_report    TEXT,
                is_remote       BOOLEAN DEFAULT 0,
                is_easy_apply   BOOLEAN DEFAULT 0,
                -- apply_type classifies how the application is handled:
                --   easy_apply  : LinkedIn Easy Apply (handled fully in-app)
                --   quick_apply : Indeed Quick Apply (handled fully in-app)
                --   external    : redirects to ATS or external site (Applicant Assist flow)
                --   unknown     : not yet classified
                apply_type        TEXT DEFAULT 'unknown',
                embellishment_log TEXT,
                UNIQUE(source, external_id)
            );

            CREATE TABLE IF NOT EXISTS applications (
                id                      TEXT PRIMARY KEY,
                job_id                  TEXT NOT NULL REFERENCES jobs(id),
                tailored_resume_path    TEXT,
                tailored_cover_letter_path TEXT,
                resume_content          TEXT,
                cover_letter_content    TEXT,
                custom_answers          TEXT DEFAULT '{}',
                status                  TEXT DEFAULT 'pending_review',
                auto_approved           BOOLEAN DEFAULT 0,
                applied_at              TIMESTAMP,
                created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                error_message           TEXT
            );

            CREATE TABLE IF NOT EXISTS activity_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                action      TEXT NOT NULL,
                details     TEXT,
                job_id      TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);
            CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(fit_score);
            CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
            CREATE INDEX IF NOT EXISTS idx_applications_job ON applications(job_id);
        """)
        await db.commit()

        # Migrations for existing databases
        try:
            await db.execute("ALTER TABLE jobs ADD COLUMN is_easy_apply BOOLEAN DEFAULT 0")
            await db.commit()
        except Exception:
            pass  # Column already exists

        try:
            await db.execute("ALTER TABLE jobs ADD COLUMN apply_type TEXT DEFAULT 'unknown'")
            await db.commit()
        except Exception:
            pass  # Column already exists

        try:
            await db.execute("ALTER TABLE jobs ADD COLUMN embellishment_log TEXT")
            await db.commit()
        except Exception:
            pass  # Column already exists

        try:
            await db.execute("ALTER TABLE jobs ADD COLUMN salary_period TEXT DEFAULT 'unknown'")
            await db.commit()
        except Exception:
            pass  # Column already exists

        for col, ddl in (
            ("match_report",                  "TEXT"),
            ("estimated_salary_min",          "INTEGER"),
            ("estimated_salary_max",          "INTEGER"),
            ("estimated_salary_period",       "TEXT DEFAULT 'annual'"),
            ("estimated_salary_source",       "TEXT"),
            ("estimated_salary_confidence",   "TEXT"),
            ("estimated_salary_metadata",     "TEXT"),
            ("estimated_salary_generated_at", "TIMESTAMP"),
            # Ghost-job / posting-quality tracking
            ("last_seen",                     "TIMESTAMP"),
            ("times_seen",                    "INTEGER DEFAULT 1"),
            ("quality_report",                "TEXT"),
            ("quality_score",                 "REAL"),
        ):
            try:
                await db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
                await db.commit()
            except Exception:
                pass  # Column already exists

        await db.execute("""
            CREATE TABLE IF NOT EXISTS salary_lookup_cache (
                cache_key   TEXT PRIMARY KEY,
                payload     TEXT NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

        # Global Q&A cache — shared across all applications.
        # Keyed by normalized question text.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS qa_cache (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                question_normalized TEXT    UNIQUE NOT NULL,
                answer              TEXT    NOT NULL,
                confidence          TEXT    DEFAULT 'high',
                source              TEXT    DEFAULT 'lm_studio',
                created_at          TEXT    NOT NULL,
                updated_at          TEXT    NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_qa_cache_question ON qa_cache(question_normalized)"
        )
        # Deletion is a soft, syncable state: a deleted job stays in `jobs` with
        # status='deleted' (hidden from every listing) and propagates through the
        # normal `triage` last-writer-wins path. No separate tombstone table.
        # Speeds up the "exclude deleted / re-discovery" checks on the hot paths.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)"
        )
        await db.commit()

        try:
            await db.execute(
                "ALTER TABLE applications ADD COLUMN auto_apply_attempts INTEGER DEFAULT 0"
            )
            await db.commit()
        except Exception:
            pass  # Column already exists

        # Post-apply lifecycle tracking (orthogonal to status — status drives
        # the apply orchestrator, outcome tracks what happened after submission).
        for col, ddl in (
            ("outcome", "TEXT DEFAULT 'awaiting'"),
            ("outcome_updated_at", "TIMESTAMP"),
            ("honesty_level", "TEXT"),
        ):
            try:
                await db.execute(f"ALTER TABLE applications ADD COLUMN {col} {ddl}")
                await db.commit()
            except Exception:
                pass  # Column already exists

        # Reset any applications stuck in 'applying' — these were interrupted by a server restart
        await db.execute(
            """
            UPDATE applications
            SET status = 'manual',
                error_message = 'Reset: server restarted while applying'
            WHERE status = 'applying'
            """
        )
        await db.commit()

        logger.info("Database initialized at %s", DB_PATH)
    finally:
        await db.close()


async def upsert_job(job: dict) -> Optional[str]:
    """Insert or update a job by (source, external_id).

    If the job already exists, backfill description/salary/tags when the
    existing record has empty values and the new data provides them.
    Returns the job id on insert, None on duplicate (even if updated).
    """
    db = await _get_db()
    try:
        job_id = job.get("id") or str(uuid.uuid4())
        tags = json.dumps(job.get("tags", []))
        # Re-discovery is handled by the existing-row path below: a job the user
        # deleted stays as a hidden row (status='deleted'), so a later fetch finds
        # it as a duplicate and backfills facts WITHOUT ever touching status — it
        # stays deleted. No separate guard needed.
        # Check for existing
        cursor = await db.execute(
            "SELECT id, description, salary_min, salary_max, salary_period, tags, apply_type FROM jobs WHERE source = ? AND external_id = ?",
            (job.get("source", ""), job.get("external_id", "")),
        )
        existing = await cursor.fetchone()
        if existing:
            # Backfill empty fields with new data
            updates = []
            params: list = []
            new_desc = job.get("description", "")
            if new_desc and not existing["description"]:
                updates.append("description = ?")
                params.append(new_desc)
            if job.get("salary_min") and not existing["salary_min"]:
                updates.append("salary_min = ?")
                params.append(job["salary_min"])
            if job.get("salary_max") and not existing["salary_max"]:
                updates.append("salary_max = ?")
                params.append(job["salary_max"])
            new_period = job.get("salary_period")
            if new_period and new_period != "unknown" and existing["salary_period"] in (None, "", "unknown"):
                updates.append("salary_period = ?")
                params.append(new_period)
            new_tags = job.get("tags", [])
            if new_tags and existing["tags"] in (None, "", "[]"):
                updates.append("tags = ?")
                params.append(tags)
            if job.get("is_easy_apply"):
                updates.append("is_easy_apply = ?")
                params.append(True)
            new_apply_type = job.get("apply_type", "unknown")
            if new_apply_type and new_apply_type != "unknown" and existing["apply_type"] in (None, "unknown"):
                updates.append("apply_type = ?")
                params.append(new_apply_type)
            # Repost tracking: this job re-appeared in a later fetch.
            updates.append("last_seen = ?")
            params.append(datetime.now(timezone.utc).isoformat())
            updates.append("times_seen = COALESCE(times_seen, 1) + 1")
            params.append(existing["id"])
            await db.execute(
                f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            await db.commit()
            backfilled = [u.split(" =")[0] for u in updates if u.split(" =")[0] not in ("last_seen", "times_seen")]
            if backfilled:
                logger.info("Backfilled %s for existing job %s", ", ".join(backfilled), existing["id"])
            return None

        now_iso = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """INSERT INTO jobs (id, source, external_id, title, company, location,
               url, description, salary_min, salary_max, salary_period, tags, date_posted,
               date_discovered, status, is_remote, is_easy_apply, apply_type,
               last_seen, times_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', ?, ?, ?, ?, 1)""",
            (
                job_id,
                job.get("source", "unknown"),
                job.get("external_id", ""),
                job.get("title", ""),
                job.get("company", ""),
                job.get("location", ""),
                job.get("url", ""),
                job.get("description", ""),
                job.get("salary_min"),
                job.get("salary_max"),
                job.get("salary_period", "unknown"),
                tags,
                job.get("date_posted", ""),
                now_iso,
                job.get("is_remote", False),
                job.get("is_easy_apply", False),
                job.get("apply_type", "unknown"),
                now_iso,
            ),
        )
        await db.commit()
        logger.info("Inserted job %s: %s at %s", job_id, job.get("title"), job.get("company"))
        return job_id
    finally:
        await db.close()


async def get_known_external_ids(source: str) -> set[str]:
    """External_ids stored for a source with a non-empty description. Lets
    scrapers skip the expensive detail/enrichment fetch for jobs already in
    the database. Jobs whose enrichment failed (empty description — e.g. the
    detail fetch lost a 429 retry race) are deliberately excluded so the next
    fetch retries them; upsert_job backfills the empty field."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT external_id FROM jobs WHERE source = ? AND external_id != '' "
            "AND description IS NOT NULL AND description != ''",
            (source,),
        )
        rows = await cursor.fetchall()
        return {row["external_id"] for row in rows}
    finally:
        await db.close()


async def get_company_signals(min_score: float = 70.0, limit: int = 15) -> list[dict]:
    """Companies from the user's own feed worth watching directly: ones that
    repeatedly score well and/or were applied to. Feeds the AI company
    recommender's zero-hallucination candidate pool."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            """
            SELECT company,
                   COUNT(*) AS matched,
                   ROUND(MAX(fit_score), 0) AS best_score,
                   SUM(CASE WHEN status IN ('applied', 'submitted') THEN 1 ELSE 0 END) AS applied
            FROM jobs
            WHERE company IS NOT NULL AND company != '' AND fit_score >= ?
            GROUP BY company COLLATE NOCASE
            ORDER BY applied DESC, matched DESC, best_score DESC
            LIMIT ?
            """,
            (min_score, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


async def get_jobs(
    status: Optional[str] = None,
    source: Optional[str] = None,
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
    unscored_only: bool = False,
    search: Optional[str] = None,
    location: Optional[str] = None,
    company: Optional[str] = None,
    remote_only: bool = False,
    easy_apply_only: bool = False,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    min_salary: Optional[int] = None,
    include_estimated: bool = False,
    sort_by: str = "date_discovered",
    sort_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Return filtered, paginated job listing with total count."""
    db = await _get_db()
    try:
        conditions = []
        params: list = []

        if status:
            if status == "review":
                conditions.append("(j.status = 'review' OR a.status = 'pending_review')")
            elif status == "discovered":
                # Only truly new jobs — no application exists
                conditions.append("j.status = 'discovered' AND a.id IS NULL")
            else:
                conditions.append("COALESCE(a.status, j.status) = ?")
                params.append(status)
        # Soft-deleted jobs are hidden from every listing unless explicitly asked for.
        if status != "deleted":
            conditions.append("j.status != 'deleted'")
        if source:
            conditions.append("j.source = ?")
            params.append(source)
        if unscored_only:
            conditions.append("(j.fit_score IS NULL OR j.fit_score = 0)")
        else:
            if min_score is not None:
                conditions.append("j.fit_score >= ?")
                params.append(min_score)
            if max_score is not None:
                conditions.append("j.fit_score <= ?")
                params.append(max_score)
        if search:
            conditions.append("(j.title LIKE ? OR j.company LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        if location:
            conditions.append("j.location LIKE ?")
            params.append(f"%{location}%")
        if company:
            conditions.append("j.company LIKE ?")
            params.append(f"%{company}%")
        if remote_only:
            conditions.append("(j.is_remote = 1 OR j.location LIKE '%remote%')")
        if easy_apply_only:
            conditions.append(
                "(j.is_easy_apply = 1 OR j.apply_type IN ('easy_apply', 'quick_apply'))"
            )
        if date_from:
            conditions.append("j.date_discovered >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("j.date_discovered <= ?")
            params.append(date_to + " 23:59:59")
        if min_salary is not None:
            # Hourly rates are stored raw (e.g. 25 for $25/hr); compare against
            # the user's annual threshold by multiplying through 2080 hours/yr.
            real_clause = (
                "(CASE WHEN j.salary_period = 'hourly' THEN j.salary_min * 2080 ELSE j.salary_min END) >= ?"
                " OR "
                "(CASE WHEN j.salary_period = 'hourly' THEN j.salary_max * 2080 ELSE j.salary_max END) >= ?"
            )
            if include_estimated:
                conditions.append(
                    "("
                    + real_clause
                    + " OR j.estimated_salary_min >= ? OR j.estimated_salary_max >= ?"
                    + ")"
                )
                params.extend([min_salary, min_salary, min_salary, min_salary])
            else:
                conditions.append("(" + real_clause + ")")
                params.extend([min_salary, min_salary])

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        # Validate sort column to prevent injection
        allowed_sorts = {"date_discovered", "fit_score", "title", "company", "salary_min", "quality_score"}
        direction = "ASC" if sort_dir.lower() == "asc" else "DESC"
        if sort_by == "applied_at":
            order_clause = f"ORDER BY (a.applied_at IS NULL), a.applied_at {direction}, j.date_discovered {direction}"
        elif sort_by == "salary_min":
            # Sort by annualized salary so hourly rates rank against annual ones.
            order_clause = (
                f"ORDER BY (CASE WHEN j.salary_period = 'hourly' "
                f"          THEN j.salary_min * 2080 ELSE j.salary_min END) {direction}"
            )
        else:
            col = sort_by if sort_by in allowed_sorts else "date_discovered"
            order_clause = f"ORDER BY j.{col} {direction}"

        # Use a subquery to get only the most recent application per job
        # This prevents duplicate rows when a job has multiple applications
        app_join = """LEFT JOIN (
                SELECT job_id, id, status, applied_at,
                       ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY applied_at DESC, created_at DESC) as rn
                FROM applications
            ) a ON a.job_id = j.id AND a.rn = 1"""

        # Total count
        cursor = await db.execute(f"SELECT COUNT(*) as cnt FROM jobs j {app_join} {where}", params)
        row = await cursor.fetchone()
        total = row["cnt"]

        # Fetch page
        cursor = await db.execute(
            f"""SELECT j.*, a.id as app_id, a.status as app_status, a.applied_at
                FROM jobs j {app_join}
                {where}
                {order_clause}
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        )
        rows = await cursor.fetchall()
        jobs = [dict(r) for r in rows]

        return {"jobs": jobs, "total": total, "limit": limit, "offset": offset}
    finally:
        await db.close()


async def delete_jobs(job_ids: list[str]) -> int:
    """Soft-delete jobs: mark status='deleted' so the removal syncs (via the
    `triage` LWW path) and resists re-discovery, while the row stays to hold the
    tombstone-free 'deleted' state. Returns count of jobs affected."""
    if not job_ids:
        return 0
    db = await _get_db()
    try:
        placeholders = ",".join("?" for _ in job_ids)
        cursor = await db.execute(
            f"UPDATE jobs SET status = 'deleted' "
            f"WHERE id IN ({placeholders}) AND status != 'deleted'",
            job_ids,
        )
        await db.commit()
        return cursor.rowcount
    finally:
        await db.close()


async def delete_all_jobs() -> int:
    """Soft-delete every job except those with an 'applied' application (status
    -> 'deleted'). Returns count of jobs affected."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            """UPDATE jobs SET status = 'deleted'
               WHERE status != 'deleted' AND id NOT IN (
                   SELECT DISTINCT job_id FROM applications WHERE status = 'applied'
               )"""
        )
        await db.commit()
        return cursor.rowcount
    finally:
        await db.close()


async def delete_jobs_filtered(
    status: str | None = None,
    source: str | None = None,
) -> int:
    """Soft-delete jobs matching filters (status -> 'deleted'). Returns count of
    jobs affected."""
    db = await _get_db()
    try:
        conditions = ["status != 'deleted'"]
        params: list = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if source:
            conditions.append("source = ?")
            params.append(source)
        where = "WHERE " + " AND ".join(conditions)
        cursor = await db.execute(f"UPDATE jobs SET status = 'deleted' {where}", params)
        await db.commit()
        return cursor.rowcount
    finally:
        await db.close()


async def gc_deleted_jobs(older_than_days: int = 30) -> int:
    """Reclaim space from long-soft-deleted jobs.

    A deleted job keeps its row (status='deleted') so the deletion stays durable
    and keeps propagating through the `triage` sync path — but its heavy text
    blobs (description, reasoning, reports) are dead weight. This strips those
    blobs from jobs that have been deleted AND unseen in feeds for
    `older_than_days` (a still-listed posting keeps its facts in case the user
    un-deletes or it's re-fetched). Identity + status are preserved, so the
    delete is untouched. Idempotent: already-stripped rows are skipped.

    Returns the number of rows compacted. A stripped row re-exports once as a
    smaller facts record; peers converge to the same compacted (still-hidden)
    state.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    db = await _get_db()
    try:
        cursor = await db.execute(
            """UPDATE jobs
               SET description = '', fit_reasoning = NULL, match_report = NULL,
                   embellishment_log = NULL
               WHERE status = 'deleted'
                 AND last_seen IS NOT NULL AND last_seen < ?
                 AND (description != '' OR fit_reasoning IS NOT NULL
                      OR match_report IS NOT NULL OR embellishment_log IS NOT NULL)""",
            (cutoff,),
        )
        await db.commit()
        return cursor.rowcount
    finally:
        await db.close()


async def get_job(job_id: str) -> Optional[dict]:
    """Get a single job with its application if one exists."""
    db = await _get_db()
    try:
        cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        job = await cursor.fetchone()
        if not job:
            return None
        result = dict(job)

        cursor = await db.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,))
        app = await cursor.fetchone()
        result["application"] = dict(app) if app else None
        return result
    finally:
        await db.close()


async def get_job_by_source_external(source: str, external_id: str) -> Optional[dict]:
    """Look up a job by its (source, external_id) tuple."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM jobs WHERE source = ? AND external_id = ?",
            (source, external_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def set_job_quality_report(job_id: str, report: dict) -> bool:
    """Store a posting-quality report (JSON) and its score on a job row."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            "UPDATE jobs SET quality_report = ?, quality_score = ? WHERE id = ?",
            (json.dumps(report), report.get("score"), job_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def refill_manual_job(job_id: str, job: dict) -> None:
    """Overwrite scrape-derived fields on a manually-ingested job.

    Used when the original ingestion couldn't extract title/company/etc
    (e.g. an unsupported source) and a re-paste now has better data.
    """
    db = await _get_db()
    try:
        tags = json.dumps(job.get("tags", []))
        await db.execute(
            """UPDATE jobs SET
                 title = ?, company = ?, location = ?, description = ?,
                 tags = ?, salary_min = COALESCE(?, salary_min),
                 salary_max = COALESCE(?, salary_max),
                 salary_period = COALESCE(?, salary_period),
                 is_remote = ?, date_posted = COALESCE(NULLIF(?, ''), date_posted)
               WHERE id = ?""",
            (
                job.get("title", ""),
                job.get("company", ""),
                job.get("location", ""),
                job.get("description", ""),
                tags,
                job.get("salary_min"),
                job.get("salary_max"),
                job.get("salary_period"),
                bool(job.get("is_remote", False)),
                job.get("date_posted", ""),
                job_id,
            ),
        )
        await db.commit()
        logger.info("Refilled manual job %s with fresh parse", job_id)
    finally:
        await db.close()


async def update_job_status(job_id: str, status: str) -> bool:
    """Update a job's status. Returns True if the job existed."""
    db = await _get_db()
    try:
        cursor = await db.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def update_job_score(
    job_id: str, score: float, reasoning: str, match_report: Optional[dict] = None
) -> None:
    """Set the AI fit score, reasoning, and match report for a job.

    A None match_report clears any stored report — a stale breakdown from a
    previous scoring run must not sit next to a fresh score.
    """
    db = await _get_db()
    try:
        await db.execute(
            "UPDATE jobs SET fit_score = ?, fit_reasoning = ?, match_report = ? WHERE id = ?",
            (score, reasoning, json.dumps(match_report) if match_report else None, job_id),
        )
        await db.commit()
    finally:
        await db.close()


async def update_job_estimated_salary(job_id: str, payload: dict) -> bool:
    """Persist an external salary estimate on a job row.

    `payload` is the dict returned by salary_estimator.estimate_salary —
    {min, max, period, source, confidence, metadata, generated_at}.
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            """UPDATE jobs SET
                 estimated_salary_min          = ?,
                 estimated_salary_max          = ?,
                 estimated_salary_period       = ?,
                 estimated_salary_source       = ?,
                 estimated_salary_confidence   = ?,
                 estimated_salary_metadata     = ?,
                 estimated_salary_generated_at = ?
               WHERE id = ?""",
            (
                payload.get("min"),
                payload.get("max"),
                payload.get("period", "annual"),
                payload.get("source"),
                payload.get("confidence"),
                json.dumps(payload.get("metadata") or {}),
                payload.get("generated_at") or datetime.now(timezone.utc).isoformat(),
                job_id,
            ),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def get_jobs_missing_salary_estimate(limit: Optional[int] = None) -> list[dict]:
    """Return jobs that have no estimated salary yet, ordered by recency.

    Used by the batch salary-estimation pipeline so re-running it doesn't
    re-pull data for jobs already estimated. Includes the fields the
    estimator needs (id, title, location, description).
    """
    db = await _get_db()
    try:
        sql = (
            "SELECT id, title, company, location, description "
            "FROM jobs "
            "WHERE estimated_salary_min IS NULL AND estimated_salary_max IS NULL "
            "ORDER BY date_discovered DESC"
        )
        params: list = []
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_salary_cache(cache_key: str, max_age_days: int = 30) -> Optional[dict]:
    """Return a cached salary-lookup payload by key, or None if missing/stale.

    Treats any DB error as a cache miss (returns None) — a transient sqlite
    failure must never break the estimator pipeline. The error is logged once
    so we still see it in the server log.
    """
    try:
        db = await _get_db()
    except Exception as e:
        logger.warning("salary cache get: open failed (%s) — treating as miss", e)
        return None
    try:
        cursor = await db.execute(
            "SELECT payload, created_at FROM salary_lookup_cache WHERE cache_key = ?",
            (cache_key,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        try:
            created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - created > timedelta(days=max_age_days):
                return None
        except Exception:
            pass
        try:
            return json.loads(row["payload"])
        except Exception:
            return None
    except Exception as e:
        logger.warning("salary cache get: query failed (%s) — treating as miss", e)
        return None
    finally:
        try:
            await db.close()
        except Exception:
            pass


async def set_salary_cache(cache_key: str, payload: dict) -> None:
    """Upsert a salary-lookup payload into the cache.

    Swallows DB errors — the estimator can still return a valid result even
    if we fail to cache it.
    """
    try:
        db = await _get_db()
    except Exception as e:
        logger.warning("salary cache set: open failed (%s) — skipping cache write", e)
        return
    try:
        await db.execute(
            """INSERT INTO salary_lookup_cache (cache_key, payload, created_at)
                 VALUES (?, ?, ?)
               ON CONFLICT(cache_key) DO UPDATE SET
                 payload    = excluded.payload,
                 created_at = excluded.created_at""",
            (cache_key, json.dumps(payload), datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    except Exception as e:
        logger.warning("salary cache set: write failed (%s) — skipping", e)
    finally:
        try:
            await db.close()
        except Exception:
            pass


async def get_jobs_missing_descriptions(source: str = "linkedin", limit: Optional[int] = None) -> list[dict]:
    """Return jobs from `source` with empty/null description. Includes id and url."""
    db = await _get_db()
    try:
        sql = (
            "SELECT id, url FROM jobs "
            "WHERE source = ? AND (description IS NULL OR description = '') AND url IS NOT NULL AND url != ''"
        )
        params: list = [source]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def backfill_job_detail(job_id: str, job: dict) -> bool:
    """Update a job's description and salary fields from a re-fetched detail dict.

    Only writes fields that are present in `job`. Used by the refetch-descriptions flow.
    """
    db = await _get_db()
    try:
        updates = []
        params: list = []
        desc = job.get("description")
        if desc:
            updates.append("description = ?")
            params.append(desc)
        if job.get("salary_min") is not None:
            updates.append("salary_min = ?")
            params.append(job["salary_min"])
        if job.get("salary_max") is not None:
            updates.append("salary_max = ?")
            params.append(job["salary_max"])
        period = job.get("salary_period")
        if period and period != "unknown":
            updates.append("salary_period = ?")
            params.append(period)
        if job.get("is_easy_apply"):
            updates.append("is_easy_apply = ?")
            params.append(True)
        if not updates:
            return False
        params.append(job_id)
        cursor = await db.execute(
            f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def update_job_apply_type(job_id: str, apply_type: str) -> None:
    """Set the apply_type classification for a job."""
    db = await _get_db()
    try:
        await db.execute(
            "UPDATE jobs SET apply_type = ? WHERE id = ?",
            (apply_type, job_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_unclassified_jobs(limit: int = 10000) -> list[dict]:
    """Return jobs whose apply_type is still 'unknown' or NULL.

    Used by the apply-type detection pipeline.  Returns all fields so that
    each source detector has access to url, is_easy_apply, and any metadata
    flags set by the fetcher.
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM jobs WHERE apply_type IS NULL OR apply_type = 'unknown' LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def create_application(
    job_id: str,
    resume_content: str,
    cover_letter_content: str,
    resume_path: Optional[str] = None,
    cover_letter_path: Optional[str] = None,
    auto_approved: bool = False,
    honesty_level: Optional[str] = None,
) -> str:
    """Create an application record for a job. Returns the application id."""
    db = await _get_db()
    try:
        app_id = str(uuid.uuid4())
        status = "approved" if auto_approved else "pending_review"
        await db.execute(
            """INSERT INTO applications
               (id, job_id, tailored_resume_path, tailored_cover_letter_path,
                resume_content, cover_letter_content, status, auto_approved,
                honesty_level, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                app_id,
                job_id,
                resume_path,
                cover_letter_path,
                resume_content,
                cover_letter_content,
                status,
                auto_approved,
                honesty_level,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.execute("UPDATE jobs SET status = ? WHERE id = ?", ("review", job_id))
        await db.commit()
        logger.info("Created application %s for job %s", app_id, job_id)
        return app_id
    finally:
        await db.close()


async def reset_stuck_applications() -> int:
    """Reset applications left in 'applying' state back to 'approved'.

    Called on server startup to recover from ungraceful shutdowns where the
    auto-apply process was killed mid-run and never got a chance to update status.
    Returns the number of applications reset.
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            "UPDATE applications SET status = 'manual', error_message = 'Reset: server restarted while applying' "
            "WHERE status = 'applying'"
        )
        await db.commit()
        return cursor.rowcount
    finally:
        await db.close()


async def update_application_status(app_id: str, status: str, error_message: Optional[str] = None) -> bool:
    """Update an application's status.

    If the current status is 'needs_review', automatic transitions to 'manual'
    or 'applied' are blocked — a human must explicitly change needs_review
    applications.  Direct status writes from auto_apply_job() (which set
    needs_review itself) are not affected because they happen before the caller
    tries to set 'manual'.
    """
    db = await _get_db()
    try:
        if status in ("manual", "applied"):
            cursor = await db.execute(
                "SELECT status FROM applications WHERE id = ?", (app_id,)
            )
            row = await cursor.fetchone()
            if row and row[0] == "needs_review":
                # Preserve needs_review — do not let the auto-apply pipeline
                # silently overwrite it with manual/applied.
                return True

        if status == "applied":
            await db.execute(
                "UPDATE applications SET status = ?, applied_at = ?, error_message = ? WHERE id = ?",
                (status, datetime.now(timezone.utc).isoformat(), error_message, app_id),
            )
        else:
            await db.execute(
                "UPDATE applications SET status = ?, error_message = ? WHERE id = ?",
                (status, error_message, app_id),
            )
        await db.commit()
        return True
    finally:
        await db.close()


# Post-apply lifecycle outcomes — orthogonal to `status` (which drives the
# apply orchestrator). Tracks what happened after the application was submitted.
VALID_OUTCOMES = {
    "awaiting",
    "no_response",
    "screening",
    "interview",
    "offer",
    "rejected",
    "withdrawn",
}


async def update_application_outcome(app_id: str, outcome: str) -> bool:
    """Update the post-apply outcome for an application.

    Raises ValueError for outcomes outside VALID_OUTCOMES. Returns False when
    no application matches app_id.
    """
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"outcome must be one of: {sorted(VALID_OUTCOMES)}")
    db = await _get_db()
    try:
        cursor = await db.execute(
            "UPDATE applications SET outcome = ?, outcome_updated_at = ? WHERE id = ?",
            (outcome, datetime.now(timezone.utc).isoformat(), app_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def increment_apply_attempts(app_id: str) -> int:
    """Increment auto_apply_attempts for an application and return the new count."""
    db = await _get_db()
    try:
        await db.execute(
            """UPDATE applications
               SET auto_apply_attempts = COALESCE(auto_apply_attempts, 0) + 1
               WHERE id = ?""",
            (app_id,),
        )
        await db.commit()
        cursor = await db.execute(
            "SELECT auto_apply_attempts FROM applications WHERE id = ?", (app_id,)
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else 1
    finally:
        await db.close()


async def get_apply_attempts(app_id: str) -> int:
    """Return the current auto_apply_attempts count for an application."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT auto_apply_attempts FROM applications WHERE id = ?", (app_id,)
        )
        row = await cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    finally:
        await db.close()


async def update_application_content(app_id: str, resume_content: str, cover_letter_content: str) -> bool:
    """Update the resume/cover letter content for an application (manual edits)."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            "UPDATE applications SET resume_content = ?, cover_letter_content = ? WHERE id = ?",
            (resume_content, cover_letter_content, app_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()


async def get_pending_reviews(limit: int = 20) -> list[dict]:
    """Get applications pending review, joined with their job data."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            """SELECT a.*, j.title, j.company, j.location, j.url, j.description,
                      j.fit_score, j.fit_reasoning, j.match_report, j.tags, j.source
               FROM applications a
               JOIN jobs j ON j.id = a.job_id
               WHERE a.status IN ('pending_review', 'paused')
               ORDER BY (a.status = 'paused') DESC, j.fit_score DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_submitted_applications(limit: int = 50) -> list[dict]:
    """Get applications that were successfully submitted (applied only)."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            """SELECT a.*, j.title, j.company, j.location, j.url, j.description,
                      j.fit_score, j.fit_reasoning, j.tags, j.source
               FROM applications a
               JOIN jobs j ON j.id = a.job_id
               WHERE a.status = 'applied'
               ORDER BY a.applied_at DESC, a.created_at DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_failed_applications(limit: int = 50) -> list[dict]:
    """Get applications that failed or require manual intervention."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            """SELECT a.*, j.title, j.company, j.location, j.url, j.description,
                      j.fit_score, j.fit_reasoning, j.tags, j.source
               FROM applications a
               JOIN jobs j ON j.id = a.job_id
               WHERE a.status IN ('manual', 'failed')
               ORDER BY a.created_at DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_outcome_analytics() -> dict:
    """Aggregate post-apply outcome analytics over submitted applications.

    Only applications with status='applied' are considered. "Responded" means
    the outcome moved past awaiting/no_response (i.e. the employer reacted).
    Returns zeroed structures when there are no submitted applications.
    """
    _RESPONDED = "COALESCE(a.outcome, 'awaiting') NOT IN ('awaiting', 'no_response')"

    db = await _get_db()
    try:
        # Counts per current outcome (zero-filled for all known outcomes)
        cursor = await db.execute(
            """SELECT COALESCE(a.outcome, 'awaiting') AS outcome, COUNT(*) AS cnt
               FROM applications a
               WHERE a.status = 'applied'
               GROUP BY COALESCE(a.outcome, 'awaiting')"""
        )
        rows = await cursor.fetchall()
        outcome_counts = {o: 0 for o in sorted(VALID_OUTCOMES)}
        for r in rows:
            outcome_counts[r["outcome"]] = r["cnt"]
        total_applied = sum(outcome_counts.values())

        # Funnel: how many applications reached at least each stage.
        # (rejected/withdrawn don't carry stage history, so they only count
        # toward "applied".)
        funnel = [
            {"stage": "applied", "count": total_applied},
            {
                "stage": "screening",
                "count": outcome_counts["screening"]
                + outcome_counts["interview"]
                + outcome_counts["offer"],
            },
            {
                "stage": "interview",
                "count": outcome_counts["interview"] + outcome_counts["offer"],
            },
            {"stage": "offer", "count": outcome_counts["offer"]},
        ]

        def _rate(responded: int, total: int) -> float:
            return round(100.0 * responded / total, 1) if total else 0.0

        async def _grouped_rates(group_expr: str) -> list[dict]:
            cursor = await db.execute(
                f"""SELECT {group_expr} AS grp,
                           COUNT(*) AS total,
                           SUM(CASE WHEN {_RESPONDED} THEN 1 ELSE 0 END) AS responded
                    FROM applications a
                    JOIN jobs j ON j.id = a.job_id
                    WHERE a.status = 'applied'
                    GROUP BY {group_expr}
                    ORDER BY total DESC"""
            )
            rows = await cursor.fetchall()
            return [
                {
                    "key": r["grp"],
                    "total": r["total"],
                    "responded": r["responded"] or 0,
                    "rate": _rate(r["responded"] or 0, r["total"]),
                }
                for r in rows
            ]

        by_source = await _grouped_rates("COALESCE(j.source, 'unknown')")
        by_fit_band = await _grouped_rates(
            """CASE
                   WHEN j.fit_score IS NULL THEN 'unscored'
                   WHEN j.fit_score < 40 THEN '0-39'
                   WHEN j.fit_score < 70 THEN '40-69'
                   ELSE '70-100'
               END"""
        )
        by_honesty = await _grouped_rates("COALESCE(a.honesty_level, 'unknown')")

        responded_total = sum(
            cnt for o, cnt in outcome_counts.items() if o not in ("awaiting", "no_response")
        )

        return {
            "total_applied": total_applied,
            "outcome_counts": outcome_counts,
            "funnel": funnel,
            "response_rate": {
                "overall": {
                    "total": total_applied,
                    "responded": responded_total,
                    "rate": _rate(responded_total, total_applied),
                },
                "by_source": by_source,
                "by_fit_band": by_fit_band,
                "by_honesty": by_honesty,
            },
        }
    finally:
        await db.close()


async def get_stats() -> dict:
    """Return dashboard statistics."""
    db = await _get_db()
    try:
        stats = {}

        # Job counts by status
        cursor = await db.execute("SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status")
        rows = await cursor.fetchall()
        stats["jobs_by_status"] = {r["status"]: r["cnt"] for r in rows}
        stats["total_jobs"] = sum(stats["jobs_by_status"].values())

        # Application counts by status
        cursor = await db.execute("SELECT status, COUNT(*) as cnt FROM applications GROUP BY status")
        rows = await cursor.fetchall()
        stats["apps_by_status"] = {r["status"]: r["cnt"] for r in rows}

        # Pending review count
        stats["pending_review"] = stats["apps_by_status"].get("pending_review", 0) + stats["apps_by_status"].get("paused", 0)
        stats["paused"] = stats["apps_by_status"].get("paused", 0)

        # Applied today — bucket by the server's local date.
        # applied_at is stored as UTC ISO; compute the [local-midnight, next-local-midnight)
        # window and convert to UTC ISO bounds so the SQL stays a simple range scan.
        local_now = datetime.now().astimezone()
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = local_midnight.astimezone(timezone.utc).isoformat()
        end_utc = (local_midnight + timedelta(days=1)).astimezone(timezone.utc).isoformat()
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM applications "
            "WHERE status = 'applied' AND applied_at >= ? AND applied_at < ?",
            (start_utc, end_utc),
        )
        row = await cursor.fetchone()
        stats["applied_today"] = row["cnt"]

        # Total applied (applications table + jobs manually marked as applied without an application)
        cursor = await db.execute(
            """SELECT COUNT(*) as cnt FROM (
                SELECT j.id FROM jobs j
                LEFT JOIN applications a ON a.job_id = j.id
                WHERE a.status = 'applied' OR j.status IN ('applied', 'manual')
            )"""
        )
        row = await cursor.fetchone()
        stats["total_applied"] = row["cnt"]

        # Average fit score
        cursor = await db.execute("SELECT AVG(fit_score) as avg_score FROM jobs WHERE fit_score IS NOT NULL")
        row = await cursor.fetchone()
        stats["avg_fit_score"] = round(row["avg_score"], 1) if row["avg_score"] else 0

        return stats
    finally:
        await db.close()


async def get_fit_breakdown() -> dict:
    """Return fit score distribution and job status breakdown for the fit breakdown page."""
    db = await _get_db()
    try:
        # Score buckets
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM jobs WHERE fit_score >= 70"
        )
        high = (await cursor.fetchone())["cnt"]

        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM jobs WHERE fit_score >= 40 AND fit_score < 70"
        )
        mid = (await cursor.fetchone())["cnt"]

        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM jobs WHERE fit_score > 0 AND fit_score < 40"
        )
        low = (await cursor.fetchone())["cnt"]

        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM jobs WHERE fit_score IS NULL OR fit_score = 0"
        )
        unscored = (await cursor.fetchone())["cnt"]

        # Average score (scored jobs only)
        cursor = await db.execute(
            "SELECT AVG(fit_score) as avg_score FROM jobs WHERE fit_score IS NOT NULL AND fit_score > 0"
        )
        row = await cursor.fetchone()
        avg_score = round(row["avg_score"], 1) if row["avg_score"] else 0

        # Status breakdown
        cursor = await db.execute("SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status")
        rows = await cursor.fetchall()
        status_breakdown = {r["status"]: r["cnt"] for r in rows}

        return {
            "score_buckets": {"high": high, "mid": mid, "low": low, "unscored": unscored},
            "avg_fit_score": avg_score,
            "total_jobs": high + mid + low + unscored,
            "total_scored": high + mid + low,
            "status_breakdown": status_breakdown,
        }
    finally:
        await db.close()


async def get_activity(limit: int = 20) -> list[dict]:
    """Get recent activity log entries."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM activity_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def applied_job_urls_today() -> list[Optional[str]]:
    """Job URLs for successful applications logged today (local date).

    Sourced from ``activity_log`` rows with action='applied' — the same event
    the auto-apply rate limiter counts — joined to ``jobs`` for the URL. Used to
    rehydrate the orchestrator's in-memory daily/per-domain counters on startup
    so a server restart no longer silently resets the applications-per-day cap.

    activity_log timestamps are stored as UTC ISO strings; the rate limiter keys
    by local ``date.today()``, so we bucket by local date here to match. Each
    element is the job URL (or None if the job row was since deleted).
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            """SELECT a.timestamp AS ts, j.url AS url
                 FROM activity_log a
                 LEFT JOIN jobs j ON j.id = a.job_id
                WHERE a.action = 'applied'
                ORDER BY a.timestamp DESC
                LIMIT 500"""
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    today = datetime.now().date()  # local
    urls: list[Optional[str]] = []
    for r in rows:
        ts = r["ts"]
        if not ts:
            continue
        try:
            when = datetime.fromisoformat(ts)
        except ValueError:
            continue
        # Stored UTC → local for date comparison; naive stamps assumed UTC.
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when.astimezone().date() == today:
            urls.append(r["url"])
    return urls


async def log_activity(action: str, details: str, job_id: Optional[str] = None) -> None:
    """Write an entry to the activity log."""
    db = await _get_db()
    try:
        await db.execute(
            "INSERT INTO activity_log (timestamp, action, details, job_id) VALUES (?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), action, details, job_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_embellishment_log(job_id: str) -> Optional[dict]:
    """Return the parsed embellishment_log for a job, or None if unset."""
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT embellishment_log FROM jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        raw = row["embellishment_log"]
        return json.loads(raw) if raw else None
    finally:
        await db.close()


async def set_embellishment_log(job_id: str, log: dict) -> bool:
    """Persist an EmbellishmentLog (as JSON) on the job record.

    Returns True if a row was updated, False if the job was not found.
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            "UPDATE jobs SET embellishment_log = ? WHERE id = ?",
            (json.dumps(log), job_id),
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()
