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
    assert exp.live == 3 and exp.tombstones == 0  # job, answer, application

    log = folder / "changes" / "A1B2.jsonl"
    lines = [json.loads(l) for l in log.read_text().splitlines()]
    assert {r["entity"] for r in lines} == {"job", "answer", "application"}

    imp = await b.import_changes(folder)
    assert imp.upserts == 3 and imp.deletes == 0 and imp.deferred == 0

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
    assert bexp.live == 1  # just the job

    await a.import_changes(folder)
    job_a = _rows(path_a, "SELECT * FROM jobs WHERE external_id = '111'")[0]
    assert job_a["fit_score"] == 99.0
    assert job_a["status"] == "applied"


@pytest.mark.asyncio
async def test_tombstone_deletes_across_devices(tmp_path, monkeypatch):
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

    # A deletes the job (and its child application) locally.
    conn = sqlite3.connect(path_a)
    conn.execute("DELETE FROM applications WHERE job_id = 'job-a'")
    conn.execute("DELETE FROM jobs WHERE id = 'job-a'")
    conn.commit()
    conn.close()

    aexp = await a.export_changes(folder)
    assert aexp.tombstones == 2  # job + application both vanished

    imp = await b.import_changes(folder)
    assert imp.deletes == 2
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
                 "status": "discovered", "date_discovered": "2026-07-08T09:00:00Z"},
    }
    app_rec = {
        "v": 1, "entity": "application", "id": "app-ios",
        "updated_at": "2026-07-08T10:00:00.000Z", "device": "IOS9", "deleted": False,
        "data": {"job_ref": "greenhouse:999", "resume_content": "R",
                 "status": "approved", "created_at": "2026-07-08T09:00:00Z",
                 "style_preset": "modern", "updated_at": "2026-07-08T09:00:00Z"},
    }
    ios_log.write_text(json.dumps(job_rec) + "\n" + json.dumps(app_rec) + "\n")

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


@pytest.mark.asyncio
async def test_delete_propagates_through_import_first_cycle(tmp_path, monkeypatch):
    """A hard delete must survive a real sync cycle (import BEFORE export, as
    service.sync_once runs it) and reach the other device. Without a durable
    tombstone the import re-adds the job from the folder's still-live record
    before export can notice it vanished — the resurrection bug."""
    path_a = tmp_path / "a.db"
    path_b = tmp_path / "b.db"
    folder = tmp_path / "sync"
    clock = Clock()

    await _init_db(path_a, monkeypatch)
    _seed_device_a(path_a)
    await _init_db(path_b, monkeypatch)

    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    b = SyncEngine(path_b, "C3D4", now_fn=clock)

    # Both devices hold the job.
    await a.export_changes(folder)
    await b.import_changes(folder)
    assert _rows(path_b, "SELECT id FROM jobs WHERE external_id='111'")

    # A deletes through the real delete path (records a durable tombstone).
    # Fixed deletion clock, strictly after the seed/export timestamps.
    monkeypatch.setattr(dbmod, "DB_PATH", path_a)
    monkeypatch.setattr(dbmod, "_sync_now", lambda: "2026-07-08T18:00:00.000Z")
    assert await dbmod.delete_jobs(["job-a"]) == 1
    assert _rows(path_a, "SELECT sync_id FROM deleted_jobs") == [{"sync_id": "greenhouse:111"}]

    # Full cycle, import first: the old live record must NOT resurrect the job.
    await a.import_changes(folder)
    assert _rows(path_a, "SELECT id FROM jobs WHERE id='job-a'") == []
    await a.export_changes(folder)

    # B converges: job gone, and the tombstone is recorded durably on B too.
    imp = await b.import_changes(folder)
    assert imp.deletes >= 1
    assert _rows(path_b, "SELECT id FROM jobs WHERE external_id='111'") == []
    assert _rows(path_b, "SELECT sync_id FROM deleted_jobs") == [{"sync_id": "greenhouse:111"}]

    # B, now holding the marker, also refuses to re-discover the same posting.
    monkeypatch.setattr(dbmod, "DB_PATH", path_b)
    refound = await dbmod.upsert_job(
        {"source": "greenhouse", "external_id": "111", "title": "Engineer",
         "company": "Acme", "url": "https://x/111", "description": "Build things"}
    )
    assert refound is None
    assert _rows(path_b, "SELECT id FROM jobs WHERE external_id='111'") == []

    # Stays gone across another A cycle — no flip-flop.
    await a.import_changes(folder)
    await a.export_changes(folder)
    assert _rows(path_a, "SELECT id FROM jobs WHERE id='job-a'") == []


@pytest.mark.asyncio
async def test_newer_edit_overrides_a_deletion(tmp_path, monkeypatch):
    """LWW still holds: if a peer edits the job AFTER we deleted it, the newer
    edit wins and the deletion is cleared (not a permanent gravestone)."""
    path_a = tmp_path / "a.db"
    folder = tmp_path / "sync"
    (folder / "changes").mkdir(parents=True)
    clock = Clock()

    await _init_db(path_a, monkeypatch)
    monkeypatch.setattr(dbmod, "DB_PATH", path_a)
    monkeypatch.setattr(dbmod, "_sync_now", lambda: "2026-07-08T18:00:00.000Z")

    # A had, then deleted, greenhouse:777.
    await dbmod.upsert_job(
        {"source": "greenhouse", "external_id": "777", "title": "Dev",
         "company": "Acme", "url": "https://x/777", "description": "d"}
    )
    jid = _rows(path_a, "SELECT id FROM jobs WHERE external_id='777'")[0]["id"]
    assert await dbmod.delete_jobs([jid]) == 1

    # A peer's edit stamped AFTER the deletion arrives in the folder.
    peer = folder / "changes" / "PEER.jsonl"
    peer.write_text(json.dumps({
        "v": 1, "entity": "job", "id": "greenhouse:777",
        "updated_at": "2026-07-08T19:00:00.000Z", "device": "PEER", "deleted": False,
        "data": {"source": "greenhouse", "external_id": "777", "title": "Dev (edited)",
                 "status": "shortlisted", "date_discovered": "2026-07-08T09:00:00Z"},
    }) + "\n")

    a = SyncEngine(path_a, "A1B2", now_fn=clock)
    await a.import_changes(folder)

    # Newer edit wins: job restored, deletion tombstone cleared.
    rows = _rows(path_a, "SELECT title FROM jobs WHERE external_id='777'")
    assert rows and rows[0]["title"] == "Dev (edited)"
    assert _rows(path_a, "SELECT sync_id FROM deleted_jobs") == []
