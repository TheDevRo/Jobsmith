"""SyncService: device-id persistence, enable/folder gating, and a full
document round-trip (a resume file synced A -> folder -> B)."""
import copy
import sqlite3

import pytest

from backend import database as dbmod
from backend.sync.service import SyncService


class FakeConfig:
    """In-memory stand-in for config.yaml with file-like load/save copies."""

    def __init__(self, initial=None):
        self._d = copy.deepcopy(initial or {})

    def load(self):
        return copy.deepcopy(self._d)

    def save(self, c):
        self._d = copy.deepcopy(c)


async def _init_db(path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    await dbmod.init_db()


def _seed_app_with_resume(db_path, resume_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO jobs (id, source, external_id, title, date_discovered) "
            "VALUES (?,?,?,?,?)",
            ("job-a", "greenhouse", "111", "Engineer", "2026-07-01T00:00:00Z"),
        )
        conn.execute(
            """INSERT INTO applications (id, job_id, resume_content,
                 cover_letter_content, status, created_at, tailored_resume_path)
               VALUES (?,?,?,?,?,?,?)""",
            ("app-1", "job-a", "R", "C", "approved", "2026-07-02T00:00:00Z",
             str(resume_path)),
        )
        conn.commit()
    finally:
        conn.close()


def test_device_id_persists_and_gating(tmp_path):
    fc = FakeConfig({"sync": {"enabled": False}})
    svc = SyncService(fc.load, fc.save, lambda: str(tmp_path / "x.db"),
                      tmp_path / "docs")
    did = svc.device_id()
    assert did and svc.device_id() == did  # stable
    assert fc.load()["sync"]["device_id"] == did  # persisted
    assert not svc.enabled


@pytest.mark.asyncio
async def test_disabled_sync_is_skipped(tmp_path):
    fc = FakeConfig({"sync": {"enabled": False, "folder": str(tmp_path / "s")}})
    svc = SyncService(fc.load, fc.save, lambda: str(tmp_path / "x.db"), tmp_path / "docs")
    result = await svc.sync_once()
    assert result["skipped"] and result["reason"] == "disabled"


@pytest.mark.asyncio
async def test_document_round_trip(tmp_path, monkeypatch):
    folder = tmp_path / "sync"
    path_a = tmp_path / "a.db"
    path_b = tmp_path / "b.db"
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 the-real-resume-bytes")

    await _init_db(path_a, monkeypatch)
    _seed_app_with_resume(path_a, resume)
    await _init_db(path_b, monkeypatch)

    cfg_a = FakeConfig({"sync": {"enabled": True, "folder": str(folder)}})
    cfg_b = FakeConfig({"sync": {"enabled": True, "folder": str(folder)}})
    svc_a = SyncService(cfg_a.load, cfg_a.save, lambda: str(path_a), tmp_path / "docs_a")
    svc_b = SyncService(cfg_b.load, cfg_b.save, lambda: str(path_b), tmp_path / "docs_b")

    res_a = await svc_a.sync_once()
    assert res_a["exported"]["live"] == 2  # job + application

    # The blob landed in the shared content-addressed store.
    blobs = list((folder / "documents").glob("*.pdf"))
    assert len(blobs) == 1
    assert blobs[0].read_bytes() == b"%PDF-1.4 the-real-resume-bytes"

    res_b = await svc_b.sync_once()
    assert res_b["imported"]["upserts"] == 2

    # B materialized the resume to a local file with identical bytes.
    row = sqlite3.connect(path_b).execute(
        "SELECT tailored_resume_path FROM applications WHERE id = 'app-1'"
    ).fetchone()
    local_path = row[0]
    assert local_path is not None
    with open(local_path, "rb") as f:
        assert f.read() == b"%PDF-1.4 the-real-resume-bytes"

    # Manifest registered both devices.
    assert cfg_a.load()["sync"]["device_id"] != cfg_b.load()["sync"]["device_id"]
