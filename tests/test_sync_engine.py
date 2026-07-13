"""Desktop sync engine: export -> import round-trip, LWW, tombstones, profile.

Two independent SQLite databases ("device A" and "device B") sync through a
temp folder, exactly as two real machines would through iCloud/a bind mount.
Uses raw sqlite3 for deterministic seeding/inspection and a fake monotonic
clock so last-writer-wins is reproducible.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from backend import database as dbmod
from backend.sync import SyncEngine


class Clock:
    """Each call returns a strictly-later UTC time (drives deterministic LWW)."""

    def __init__(self):
        self.t = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


async def _init_db(path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    await dbmod.init_db()


def _seed_device_a(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """INSERT INTO jobs (id, source, external_id, title, company, location,
                 url, description, salary_min, salary_max, salary_period, tags,
                 date_posted, date_discovered, status, fit_score, fit_reasoning,
                 is_remote, is_easy_apply, apply_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("job-a", "greenhouse", "111", "Engineer", "Acme", "Remote",
             "https://x/111", "Build things", 100000, 150000, "annual",
             '["python"]', "2026-06-30", "2026-07-01T00:00:00Z", "discovered",
             87.5, "strong match", 1, 0, "external"),
        )
        conn.execute(
            """INSERT INTO applications (id, job_id, tailored_resume_path,
                 resume_content, cover_letter_content, custom_answers, status,
                 auto_approved, created_at, honesty_level)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("app-1", "job-a", "/Users/a/local/resume.pdf", "RESUME", "COVER",
             '{"why":"because"}', "approved", 1, "2026-07-02T00:00:00Z", "honest"),
        )
        conn.execute(
            """INSERT INTO qa_cache (question_normalized, answer, confidence,
                 source, created_at, updated_at)
               VALUES (?,?,?,?,?,?)""",
            ("why do you want to work here", "I admire the mission", "high",
             "lm_studio", "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


def _rows(path, sql, params=()):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_export_import_round_trip(tmp_path, monkeypatch):
    path_a = tmp_path / "a.db"
    path_b = tmp_path / "b.db"
    folder = tmp_path / "sync"
    clock = Clock()

    await _init_db(path_a, monkeypatch)
    _seed_device_a(path_a)
    await _init_db(path_b, monkeypatch)  # empty

    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    b = SyncEngine(path_b, "C3D4", now_fn=clock)

    exp = await a.export_changes(folder)
    # job facts + triage decision + answer + application
    assert exp.live == 4 and exp.tombstones == 0

    log = folder / "changes" / "A1B2.jsonl"
    lines = [json.loads(l) for l in log.read_text().splitlines()]
    assert {r["entity"] for r in lines} == {"job", "triage", "answer", "application"}

    imp = await b.import_changes(folder)
    assert imp.upserts == 4 and imp.deletes == 0 and imp.deferred == 0

    # The job's decision arrived on its own entity and set the local status.
    assert _rows(path_b, "SELECT status FROM jobs")[0]["status"] == "discovered"

    # Job arrived with its portable fields; B minted its own local uuid.
    jobs = _rows(path_b, "SELECT * FROM jobs")
    assert len(jobs) == 1
    job = jobs[0]
    assert job["external_id"] == "111"
    assert job["title"] == "Engineer"
    assert job["fit_score"] == 87.5
    assert job["is_remote"] == 1
    assert job["tags"] == '["python"]'
    assert job["id"] != "job-a"  # local identity is independent

    # Application relinked to B's local job id; machine-local path NOT synced.
    apps = _rows(path_b, "SELECT * FROM applications")
    assert len(apps) == 1
    app = apps[0]
    assert app["id"] == "app-1"
    assert app["job_id"] == job["id"]
    assert app["status"] == "approved"
    assert app["auto_approved"] == 1
    assert json.loads(app["custom_answers"]) == {"why": "because"}
    assert app["tailored_resume_path"] is None  # excluded from sync

    answers = _rows(path_b, "SELECT * FROM qa_cache")
    assert len(answers) == 1
    assert answers[0]["answer"] == "I admire the mission"

    # A re-export against a fresh folder emits nothing (snapshot is in sync).
    reexp = await a.export_changes(tmp_path / "sync2")
    assert reexp.total == 0
    # Likewise B does not re-emit what it just imported.
    reimp_export = await b.export_changes(tmp_path / "sync3")
    assert reimp_export.total == 0


@pytest.mark.asyncio
async def test_last_writer_wins(tmp_path, monkeypatch):
    path_a = tmp_path / "a.db"
    path_b = tmp_path / "b.db"
    folder = tmp_path / "sync"
    clock = Clock()

    await _init_db(path_a, monkeypatch)
    _seed_device_a(path_a)
    await _init_db(path_b, monkeypatch)

    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    b = SyncEngine(path_b, "C3D4", now_fn=clock)

    await a.export_changes(folder)
    await b.import_changes(folder)

    # B edits the job later than A's create -> B must win.
    conn = sqlite3.connect(path_b)
    conn.execute("UPDATE jobs SET fit_score = 99.0, status = 'applied' WHERE external_id = '111'")
    conn.commit()
    conn.close()

    bexp = await b.export_changes(folder)
    # a facts change (fit_score) and a decision change (status) — two entities.
    assert bexp.live == 2  # job facts + triage

    await a.import_changes(folder)
    job_a = _rows(path_a, "SELECT * FROM jobs WHERE external_id = '111'")[0]
    assert job_a["fit_score"] == 99.0
    assert job_a["status"] == "applied"


@pytest.mark.asyncio
async def test_generic_tombstone_propagates(tmp_path, monkeypatch):
    """The generic tombstone path (used for real application/answer deletes, and
    as a safety net if a job row is physically removed). User-facing job deletes
    are soft — see test_delete_via_triage_propagates — but a vanished row must
    still converge."""
    path_a = tmp_path / "a.db"
    path_b = tmp_path / "b.db"
    folder = tmp_path / "sync"
    clock = Clock()

    await _init_db(path_a, monkeypatch)
    _seed_device_a(path_a)
    await _init_db(path_b, monkeypatch)

    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    b = SyncEngine(path_b, "C3D4", now_fn=clock)

    await a.export_changes(folder)
    await b.import_changes(folder)
    assert _rows(path_b, "SELECT * FROM jobs")

    # A physically removes the job row (and its child application).
    conn = sqlite3.connect(path_a)
    conn.execute("DELETE FROM applications WHERE job_id = 'job-a'")
    conn.execute("DELETE FROM jobs WHERE id = 'job-a'")
    conn.commit()
    conn.close()

    aexp = await a.export_changes(folder)
    # job facts + triage decision + application all vanished.
    assert aexp.tombstones == 3

    imp = await b.import_changes(folder)
    assert imp.deletes == 3
    assert _rows(path_b, "SELECT * FROM jobs") == []
    assert _rows(path_b, "SELECT * FROM applications") == []


@pytest.mark.asyncio
async def test_unknown_keys_preserved_on_writeback(tmp_path, monkeypatch):
    """An edit on this device must not drop columns only another client models
    (the spec's write-back invariant) — e.g. iOS's style_preset."""
    path_a = tmp_path / "a.db"
    folder = tmp_path / "sync"
    (folder / "changes").mkdir(parents=True)
    clock = Clock()

    await _init_db(path_a, monkeypatch)

    # A log produced by a hypothetical iOS device: a job + an application whose
    # payload carries an iOS-only key the desktop schema doesn't model.
    ios_log = folder / "changes" / "IOS9.jsonl"
    job_rec = {
        "v": 1, "entity": "job", "id": "greenhouse:999",
        "updated_at": "2026-07-08T10:00:00.000Z", "device": "IOS9", "deleted": False,
        "data": {"source": "greenhouse", "external_id": "999", "title": "Dev",
                 "date_discovered": "2026-07-08T09:00:00Z"},
    }
    triage_rec = {
        "v": 1, "entity": "triage", "id": "greenhouse:999",
        "updated_at": "2026-07-08T10:00:00.000Z", "device": "IOS9", "deleted": False,
        "data": {"status": "discovered"},
    }
    app_rec = {
        "v": 1, "entity": "application", "id": "app-ios",
        "updated_at": "2026-07-08T10:00:00.000Z", "device": "IOS9", "deleted": False,
        "data": {"job_ref": "greenhouse:999", "resume_content": "R",
                 "status": "approved", "created_at": "2026-07-08T09:00:00Z",
                 "style_preset": "modern", "updated_at": "2026-07-08T09:00:00Z"},
    }
    ios_log.write_text(
        json.dumps(job_rec) + "\n" + json.dumps(triage_rec) + "\n" + json.dumps(app_rec) + "\n"
    )

    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    await a.import_changes(folder)

    # Desktop applied the fields it models; style_preset isn't a column here.
    app = _rows(path_a, "SELECT * FROM applications WHERE id = 'app-ios'")[0]
    assert app["status"] == "approved"
    assert "style_preset" not in app

    # Desktop edits a field it does model, then exports.
    conn = sqlite3.connect(path_a)
    conn.execute("UPDATE applications SET status = 'applied' WHERE id = 'app-ios'")
    conn.commit()
    conn.close()
    exp = await a.export_changes(folder)
    assert exp.live == 1

    lines = [json.loads(l) for l in (folder / "changes" / "A1B2.jsonl").read_text().splitlines()]
    emitted = next(r for r in lines if r["id"] == "app-ios")
    assert emitted["data"]["status"] == "applied"        # our edit
    assert emitted["data"]["style_preset"] == "modern"   # iOS key preserved verbatim


@pytest.mark.asyncio
async def test_profile_syncs_without_secrets(tmp_path, monkeypatch):
    path_a = tmp_path / "a.db"
    path_b = tmp_path / "b.db"
    folder = tmp_path / "sync"
    clock = Clock()

    await _init_db(path_a, monkeypatch)
    await _init_db(path_b, monkeypatch)

    profile_a = {
        "full_name": "Jane Doe",
        "email": "jane@example.com",
        "summary": "Backend engineer.",
        "gender": "Female",
        "workday_email": "jane@example.com",
        "workday_password": "SECRET-A-should-never-sync",
        "ats_login_password": "SECRET-A2-should-never-sync",
    }
    # B has its OWN local secret that must survive an import.
    profile_b = {"ats_login_password": "SECRET-B-local-only"}

    a = SyncEngine(path_a, "A1B2", now_fn=clock, load_profile=lambda: profile_a)
    saved_b = {}

    def load_b():
        return dict(profile_b, **saved_b)

    def save_b(p):
        saved_b.clear()
        saved_b.update(p)

    b = SyncEngine(path_b, "C3D4", now_fn=clock, load_profile=load_b, save_profile=save_b)

    exp = await a.export_changes(folder)
    assert exp.live == 1  # profile only

    rec = json.loads((folder / "changes" / "A1B2.jsonl").read_text().splitlines()[0])
    assert rec["entity"] == "profile" and rec["id"] == "me"
    assert not ({"workday_email", "workday_password", "ats_login_password"} & set(rec["data"]))

    imp = await b.import_changes(folder)
    assert imp.profile_updated

    # B got A's synced fields, kept its own local secret, gained no secret from A.
    assert saved_b["full_name"] == "Jane Doe"
    assert saved_b["summary"] == "Backend engineer."
    assert saved_b["gender"] == "Female"
    assert saved_b["ats_login_password"] == "SECRET-B-local-only"
    assert "workday_password" not in saved_b


def _write_peer_triage(folder, sync_id, status, ts, device="PEER"):
    """Append a peer's `triage` decision record to the folder (a delete is just
    status='deleted')."""
    (folder / "changes").mkdir(parents=True, exist_ok=True)
    rec = {"v": 1, "entity": "triage", "id": sync_id, "updated_at": ts,
           "device": device, "deleted": False, "data": {"status": status}}
    with (folder / "changes" / f"{device}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


@pytest.mark.asyncio
async def test_delete_via_triage_propagates(tmp_path, monkeypatch):
    """The real delete path is soft: status='deleted', synced as a `triage`
    record. It reaches the other device, hides the job there, and needs no
    tombstone and no side table."""
    path_a = tmp_path / "a.db"
    path_b = tmp_path / "b.db"
    folder = tmp_path / "sync"
    clock = Clock()

    await _init_db(path_a, monkeypatch)
    _seed_device_a(path_a)
    await _init_db(path_b, monkeypatch)

    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    b = SyncEngine(path_b, "C3D4", now_fn=clock)

    await a.export_changes(folder)
    await b.import_changes(folder)
    assert _rows(path_b, "SELECT id FROM jobs WHERE external_id='111'")

    # A deletes through the real (soft) delete path.
    monkeypatch.setattr(dbmod, "DB_PATH", path_a)
    assert await dbmod.delete_jobs(["job-a"]) == 1
    assert _rows(path_a, "SELECT status FROM jobs WHERE id='job-a'")[0]["status"] == "deleted"

    # One cycle converges B to 'deleted'.
    await a.export_changes(folder)
    await b.import_changes(folder)
    row_b = _rows(path_b, "SELECT status FROM jobs WHERE external_id='111'")
    assert row_b and row_b[0]["status"] == "deleted"

    # And it's hidden from B's listing.
    monkeypatch.setattr(dbmod, "DB_PATH", path_b)
    listing = await dbmod.get_jobs()
    assert all(j["external_id"] != "111" for j in listing["jobs"])


@pytest.mark.asyncio
async def test_refetch_stays_deleted(tmp_path, monkeypatch):
    """Permanent delete: once status='deleted', a later fetch of the same posting
    finds it as a duplicate and does NOT resurrect it (status stays 'deleted')."""
    path_a = tmp_path / "a.db"
    await _init_db(path_a, monkeypatch)
    monkeypatch.setattr(dbmod, "DB_PATH", path_a)

    posting = {"source": "greenhouse", "external_id": "777", "title": "Dev",
               "company": "Acme", "url": "https://x/777", "description": "d"}
    await dbmod.upsert_job(posting)
    jid = _rows(path_a, "SELECT id FROM jobs WHERE external_id='777'")[0]["id"]
    assert await dbmod.delete_jobs([jid]) == 1

    # Fetcher re-finds the same posting.
    refound = await dbmod.upsert_job(posting)
    assert refound is None
    assert _rows(path_a, "SELECT status FROM jobs WHERE external_id='777'")[0]["status"] == "deleted"


@pytest.mark.asyncio
async def test_newer_shortlist_beats_older_delete(tmp_path, monkeypatch):
    """Symmetric LWW: a shortlist stamped after a peer's delete wins — you can't
    lose a job by shortlisting it. No engaged-status heuristic. Export-before-
    import (as the service runs it) stamps the local shortlist so it out-ranks
    the older delete already in the folder."""
    path_a = tmp_path / "a.db"
    folder = tmp_path / "sync"
    clock = Clock()  # 2026-07-08 12:00+, strictly after the peer delete below

    await _init_db(path_a, monkeypatch)
    conn = sqlite3.connect(path_a)
    conn.execute(
        """INSERT INTO jobs (id, source, external_id, title, company, url,
             description, status, date_discovered)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("job-x", "greenhouse", "555", "Dev", "Acme", "https://x/555", "d",
         "shortlisted", "2026-07-08T09:00:00Z"),
    )
    conn.commit()
    conn.close()

    _write_peer_triage(folder, "greenhouse:555", "deleted", "2026-07-08T10:00:00.000Z")

    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    await a.export_changes(folder)   # local shortlist stamped ~12:00:01
    await a.import_changes(folder)

    assert _rows(path_a, "SELECT status FROM jobs WHERE external_id='555'")[0]["status"] == "shortlisted"
    # The shortlist is now in the folder for the peer to converge on.
    lines = [json.loads(l) for l in (folder / "changes" / "A1B2.jsonl").read_text().splitlines()]
    tri = next(r for r in lines if r["entity"] == "triage" and r["id"] == "greenhouse:555")
    assert tri["data"]["status"] == "shortlisted"


@pytest.mark.asyncio
async def test_newer_delete_beats_older_shortlist(tmp_path, monkeypatch):
    """The mirror case: a delete stamped after our shortlist wins and hides the
    job locally — deletes and shortlists are fully symmetric."""
    path_a = tmp_path / "a.db"
    folder = tmp_path / "sync"
    clock = Clock()

    await _init_db(path_a, monkeypatch)
    conn = sqlite3.connect(path_a)
    conn.execute(
        """INSERT INTO jobs (id, source, external_id, title, company, url,
             description, status, date_discovered)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("job-y", "greenhouse", "556", "Dev", "Acme", "https://x/556", "d",
         "shortlisted", "2026-07-08T09:00:00Z"),
    )
    conn.commit()
    conn.close()

    # Export our shortlist first (stamped ~12:00:01), then a peer deletes later.
    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    await a.export_changes(folder)
    _write_peer_triage(folder, "greenhouse:556", "deleted", "2026-07-08T20:00:00.000Z")

    await a.import_changes(folder)
    assert _rows(path_a, "SELECT status FROM jobs WHERE external_id='556'")[0]["status"] == "deleted"


@pytest.mark.asyncio
async def test_gc_compacts_long_deleted_jobs(tmp_path, monkeypatch):
    """GC strips heavy blobs from jobs deleted AND unseen for a while, but keeps
    the row + 'deleted' status (durable delete) and leaves recent/live jobs."""
    path_a = tmp_path / "a.db"
    await _init_db(path_a, monkeypatch)
    monkeypatch.setattr(dbmod, "DB_PATH", path_a)

    # Deleted long ago (last seen 60 days back) with a heavy description.
    await dbmod.upsert_job({"source": "greenhouse", "external_id": "gc1", "title": "Old",
                            "company": "Acme", "url": "https://x/gc1", "description": "x" * 5000})
    jid = _rows(path_a, "SELECT id FROM jobs WHERE external_id='gc1'")[0]["id"]
    await dbmod.delete_jobs([jid])
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    conn = sqlite3.connect(path_a)
    conn.execute("UPDATE jobs SET last_seen = ? WHERE id = ?", (old, jid))
    conn.commit()
    conn.close()

    # Deleted but recently seen — keep its facts (might be un-deleted/re-fetched).
    await dbmod.upsert_job({"source": "greenhouse", "external_id": "gc2", "title": "Recent",
                            "company": "Acme", "url": "https://x/gc2", "description": "y" * 5000})
    await dbmod.delete_jobs([_rows(path_a, "SELECT id FROM jobs WHERE external_id='gc2'")[0]["id"]])
    # A live job — must be untouched.
    await dbmod.upsert_job({"source": "greenhouse", "external_id": "gc3", "title": "Live",
                            "company": "Acme", "url": "https://x/gc3", "description": "z" * 5000})

    assert await dbmod.gc_deleted_jobs() == 1  # only the old deleted one

    row1 = _rows(path_a, "SELECT status, description FROM jobs WHERE external_id='gc1'")[0]
    assert row1["status"] == "deleted"   # still deleted — durability preserved
    assert row1["description"] == ""     # heavy blob reclaimed
    assert _rows(path_a, "SELECT description FROM jobs WHERE external_id='gc2'")[0]["description"] == "y" * 5000
    assert _rows(path_a, "SELECT description FROM jobs WHERE external_id='gc3'")[0]["description"] == "z" * 5000

    # Idempotent: a second run compacts nothing.
    assert await dbmod.gc_deleted_jobs() == 0


def _meta(path, key):
    rows = _rows(path, "SELECT value FROM sync_meta WHERE key = ?", (key,))
    return rows[0]["value"] if rows else None


@pytest.mark.asyncio
async def test_format_migration_forces_one_reexport(tmp_path, monkeypatch):
    """A device upgrading across a wire-format bump (older stored sync_format +
    non-empty snapshot) re-emits every record once so its correctly-folded
    payloads supersede the stale ones it broadcast under the old format —
    matching the iOS markMigrationIfNeeded path. The Python side previously had
    no such mechanism, so post-upgrade devices silently re-diverged."""
    path_a = tmp_path / "a.db"
    await _init_db(path_a, monkeypatch)
    _seed_device_a(path_a)
    a = SyncEngine(path_a, "A1B2", now_fn=Clock())

    # First export populates the snapshot and stamps sync_format at current.
    first = await a.export_changes(tmp_path / "sync1")
    assert first.live == 4
    assert _meta(path_a, "sync_format") == str(dbmod_sync_format())
    assert _meta(path_a, "pending_migration") in (None, "0")

    # A clean re-export changes nothing (snapshot is in sync).
    assert (await a.export_changes(tmp_path / "sync2")).total == 0

    # Simulate a device that had been running the OLD code: its snapshot rows
    # exist but were tagged for an older format.
    conn = sqlite3.connect(path_a)
    conn.execute("UPDATE sync_meta SET value = '1' WHERE key = 'sync_format'")
    conn.commit()
    conn.close()

    # The next export force-re-emits all 4 records despite no DB change...
    forced = await a.export_changes(tmp_path / "sync3")
    assert forced.live == 4 and forced.tombstones == 0
    assert _meta(path_a, "pending_migration") == "0"      # flag consumed
    assert _meta(path_a, "sync_format") == str(dbmod_sync_format())

    # ...and it is genuinely one-time: the following export is a no-op again.
    assert (await a.export_changes(tmp_path / "sync4")).total == 0


@pytest.mark.asyncio
async def test_fresh_import_does_not_trigger_migration(tmp_path, monkeypatch):
    """A brand-new device that merely imports another device's records must NOT
    force a re-broadcast — it has nothing of its own to migrate. (markMigration
    reads the pre-rebuild snapshot, which is empty during a fresh import.)"""
    path_a = tmp_path / "a.db"
    path_b = tmp_path / "b.db"
    folder = tmp_path / "sync"
    clock = Clock()

    await _init_db(path_a, monkeypatch)
    _seed_device_a(path_a)
    await _init_db(path_b, monkeypatch)  # empty

    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    b = SyncEngine(path_b, "C3D4", now_fn=clock)

    await a.export_changes(folder)
    await b.import_changes(folder)

    # B recorded the current format but never flagged a migration.
    assert _meta(path_b, "sync_format") == str(dbmod_sync_format())
    assert _meta(path_b, "pending_migration") in (None, "0")

    # And so B does not re-emit what it just imported.
    assert (await b.export_changes(tmp_path / "sync-b")).total == 0


def dbmod_sync_format():
    from backend.sync import engine as _eng
    return _eng.SYNC_FORMAT_VERSION


@pytest.mark.asyncio
async def test_outcome_survives_a_concurrent_edit_on_the_other_device(tmp_path, monkeypatch):
    """The hazard the `application_event` entity exists to prevent.

    Merging is last-writer-wins over the WHOLE record. When the outcome lived on
    the `application` entity, this sequence lost it: A records an interview, then
    B — which hasn't synced yet — edits the resume. B's application record is
    newer, so it won LWW and overwrote the outcome back to 'awaiting'. Carried as
    an append-only event instead, the two edits are independent and both survive.
    """
    path_a, path_b, folder = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "sync"
    clock = Clock()

    await _init_db(path_a, monkeypatch)
    _seed_device_a(path_a)
    await _init_db(path_b, monkeypatch)

    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    b = SyncEngine(path_b, "C3D4", now_fn=clock)

    # Both devices start in sync, with the application submitted.
    monkeypatch.setattr(dbmod, "DB_PATH", path_a)
    await dbmod.update_application_status("app-1", "applied")
    await a.export_changes(folder)
    await b.import_changes(folder)

    # A records the outcome...
    await dbmod.update_application_outcome("app-1", "interview")

    # ...while B, not yet having seen it, edits the resume. B's record is newer.
    conn = sqlite3.connect(path_b)
    conn.execute("UPDATE applications SET resume_content = ? WHERE id = ?", ("REVISED", "app-1"))
    conn.commit()
    conn.close()

    # Exchange in both directions.
    await a.export_changes(folder)
    await b.export_changes(folder)
    await b.import_changes(folder)
    await a.import_changes(folder)

    # Both edits landed on both devices — neither clobbered the other.
    for path in (path_a, path_b):
        app = _rows(path, "SELECT * FROM applications WHERE id = 'app-1'")[0]
        assert app["outcome"] == "interview", f"outcome lost on {path.name}"
        assert app["resume_content"] == "REVISED", f"resume edit lost on {path.name}"
        events = _rows(path, "SELECT * FROM application_events WHERE application_id = 'app-1'")
        assert [(e["from_outcome"], e["to_outcome"]) for e in events] == [("awaiting", "interview")]


@pytest.mark.asyncio
async def test_offline_outcome_histories_merge_as_a_union(tmp_path, monkeypatch):
    """Two devices each record outcomes offline; the merge keeps both, in order,
    and the derived `outcome` column converges to the latest event."""
    path_a, path_b, folder = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "sync"
    clock = Clock()

    await _init_db(path_a, monkeypatch)
    _seed_device_a(path_a)
    await _init_db(path_b, monkeypatch)

    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    b = SyncEngine(path_b, "C3D4", now_fn=clock)

    monkeypatch.setattr(dbmod, "DB_PATH", path_a)
    await dbmod.update_application_status("app-1", "applied")
    await a.export_changes(folder)
    await b.import_changes(folder)

    # A (desktop) marks the screener; B (phone) later marks the interview.
    await dbmod.update_application_outcome("app-1", "screening")
    monkeypatch.setattr(dbmod, "DB_PATH", path_b)
    await dbmod.update_application_outcome("app-1", "interview")

    await a.export_changes(folder)
    await b.export_changes(folder)
    await a.import_changes(folder)
    await b.import_changes(folder)

    for path in (path_a, path_b):
        events = _rows(
            path,
            "SELECT to_outcome FROM application_events WHERE application_id = 'app-1' ORDER BY occurred_at",
        )
        assert [e["to_outcome"] for e in events] == ["screening", "interview"], path.name
        # The column is derived from the history, so both devices agree.
        assert _rows(path, "SELECT outcome FROM applications")[0]["outcome"] == "interview"


@pytest.mark.asyncio
async def test_imports_ios_emitted_event_record(tmp_path, monkeypatch):
    """Wire-format interop, the other direction from KitTests'
    testImportsDesktopEmittedEventRecord. The record below is verbatim what the
    iOS SyncEngine emits — note it stamps `occurred_at` with milliseconds + 'Z'
    where the desktop writes microseconds + '+00:00'. Both must import, and the
    event's identity must be derived the same way on both sides.
    """
    path, folder = tmp_path / "a.db", tmp_path / "sync"
    await _init_db(path, monkeypatch)
    _seed_device_a(path)
    await dbmod.update_application_status("app-1", "applied")

    occurred_at = "2026-07-13T03:52:57.060Z"
    sync_id = f"app-1:{occurred_at}:screening"
    record = {
        "v": 1, "entity": "application_event", "id": sync_id,
        "updated_at": "2026-07-13T03:52:57.062Z", "device": "IPHN", "deleted": False,
        "data": {
            "application_ref": "app-1", "from_outcome": "awaiting",
            "to_outcome": "screening", "occurred_at": occurred_at, "source": "user",
        },
    }
    changes = folder / "changes"
    changes.mkdir(parents=True)
    (changes / "IPHN.jsonl").write_text(json.dumps(record) + "\n")

    imp = await SyncEngine(path, "A1B2", now_fn=Clock()).import_changes(folder)
    assert imp.deferred == 0

    events = _rows(path, "SELECT * FROM application_events WHERE application_id = 'app-1'")
    assert len(events) == 1
    assert events[0]["to_outcome"] == "screening"
    assert events[0]["occurred_at"] == occurred_at
    # ...and the derived column is rebuilt from the imported history.
    assert _rows(path, "SELECT outcome FROM applications")[0]["outcome"] == "screening"


@pytest.mark.asyncio
async def test_identically_stamped_events_converge(tmp_path, monkeypatch):
    """Two events stamped at the same instant must derive the same outcome on
    every device, whatever order each one imported them in.

    This is why the tiebreak in recompute_outcome can't be the rowid: rowids are
    local insertion order, so each device would rank its own event first and read
    a different "latest" outcome off an identical history. The tiebreak is the
    event's sync identity (occurred_at, to_outcome), which every device agrees on.
    Must stay in step with the Swift twin, KitTests testIdenticallyStampedEventsConverge.
    """
    from backend.sync.entities import recompute_outcome

    same_instant = "2026-07-13T10:00:00.000Z"
    outcomes = []

    # The same two events, inserted in opposite orders — as two devices would.
    for order in (["screening", "interview"], ["interview", "screening"]):
        path = tmp_path / f"{order[0]}.db"
        await _init_db(path, monkeypatch)
        _seed_device_a(path)

        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE applications SET status = 'applied' WHERE id = 'app-1'")
        for to_outcome in order:
            conn.execute(
                """INSERT INTO application_events
                   (application_id, from_outcome, to_outcome, occurred_at, source)
                   VALUES ('app-1', 'awaiting', ?, ?, 'user')""",
                (to_outcome, same_instant),
            )
        conn.commit()

        import aiosqlite
        async with aiosqlite.connect(path) as adb:
            adb.row_factory = aiosqlite.Row
            await recompute_outcome(adb, "app-1")
            await adb.commit()
        outcomes.append(_rows(path, "SELECT outcome FROM applications")[0]["outcome"])
        conn.close()

    assert outcomes[0] == outcomes[1], (
        f"devices disagree on an identical history: {outcomes}")


@pytest.mark.asyncio
async def test_schedule_survives_a_concurrent_edit(tmp_path, monkeypatch):
    """Reminder dates get their own entity for the same reason the outcome does:
    on `application` they'd be wiped by any unrelated edit from the other device."""
    path_a, path_b, folder = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "sync"
    clock = Clock()

    await _init_db(path_a, monkeypatch)
    _seed_device_a(path_a)
    await _init_db(path_b, monkeypatch)

    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    b = SyncEngine(path_b, "C3D4", now_fn=clock)

    monkeypatch.setattr(dbmod, "DB_PATH", path_a)
    await dbmod.update_application_status("app-1", "applied")
    await a.export_changes(folder)
    await b.import_changes(folder)

    # A schedules an interview; B, not yet having seen it, edits the resume.
    await dbmod.set_application_schedule("app-1", interview_at="2026-07-20T15:00:00+00:00")
    conn = sqlite3.connect(path_b)
    conn.execute("UPDATE applications SET resume_content = 'REVISED' WHERE id = 'app-1'")
    conn.commit()
    conn.close()

    await a.export_changes(folder)
    await b.export_changes(folder)
    await b.import_changes(folder)
    await a.import_changes(folder)

    for path in (path_a, path_b):
        app = _rows(path, "SELECT * FROM applications WHERE id = 'app-1'")[0]
        assert app["interview_at"] == "2026-07-20T15:00:00+00:00", f"interview lost on {path.name}"
        assert app["resume_content"] == "REVISED", f"resume edit lost on {path.name}"

    # Clearing the dates propagates as a tombstone — and must not delete the app.
    monkeypatch.setattr(dbmod, "DB_PATH", path_a)
    await dbmod.set_application_schedule("app-1", clear_interview=True, clear_follow_up=True)
    await a.export_changes(folder)
    await b.import_changes(folder)
    app_b = _rows(path_b, "SELECT * FROM applications WHERE id = 'app-1'")[0]
    assert app_b["interview_at"] is None
    assert app_b["resume_content"] == "REVISED"  # the application itself survives
