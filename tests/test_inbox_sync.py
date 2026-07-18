"""Inbox sync feature (desktop half).

Two halves, both driven off the shared cross-platform contract:

  * engine gating — with the `inbox` settings category OFF, the sync engine
    skips BOTH export and import of the `job` and `triage` entities (live rows
    AND tombstones) and leaves their sync_snapshot rows untouched, so flipping
    the category back on re-syncs cleanly. Default ON preserves prior behavior.
  * get_jobs — the lenient pay gate (pay_floor / require_stated_pay, parity with
    iOS JobListFilter.applyPayFilter + JobFilters.statedAnnualPay) and the new
    `salary` / `company` / NULL-sinking `fit_score` sort orders.
"""
import copy
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from backend import database as dbmod
from backend.sync import SyncEngine


class Clock:
    def __init__(self):
        self.t = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.t += timedelta(seconds=1)
        return self.t


class ConfigBox:
    """Stand-in for a device's config.yaml (deep-copied in/out)."""

    def __init__(self, cfg=None):
        self.cfg = cfg or {}

    def load(self):
        return copy.deepcopy(self.cfg)

    def save(self, cfg):
        self.cfg = copy.deepcopy(cfg)


async def _init_db(path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    await dbmod.init_db()


def _rows(path, sql, params=()):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _seed_job(path, ext="111", status="discovered"):
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """INSERT INTO jobs (id, source, external_id, title, company, location,
                 url, description, salary_min, salary_max, salary_period, tags,
                 date_posted, date_discovered, status, fit_score, is_remote,
                 is_easy_apply, apply_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"job-{ext}", "greenhouse", ext, "Engineer", "Acme", "Remote",
             f"https://x/{ext}", "Build", 100000, 150000, "annual", '["python"]',
             "2026-06-30", "2026-07-01T00:00:00Z", status, 87.5, 1, 0, "external"),
        )
        conn.commit()
    finally:
        conn.close()


def _log_records(folder, device):
    log = folder / "changes" / f"{device}.jsonl"
    if not log.exists():
        return []
    return [json.loads(l) for l in log.read_text().splitlines()]


def _write_peer(folder, records, device="PEER"):
    (folder / "changes").mkdir(parents=True, exist_ok=True)
    with (folder / "changes" / f"{device}.jsonl").open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# engine gating
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inbox_off_skips_job_triage_export_and_snapshot(tmp_path, monkeypatch):
    path_a = tmp_path / "a.db"
    await _init_db(path_a, monkeypatch)
    _seed_job(path_a)

    box = ConfigBox({"sync": {"settings": {"inbox": False}}})
    a = SyncEngine(path_a, "A1B2", now_fn=Clock(),
                   load_settings=box.load, save_settings=box.save)

    await a.export_changes(tmp_path / "sync")
    ents = {r["entity"] for r in _log_records(tmp_path / "sync", "A1B2")}
    assert "job" not in ents and "triage" not in ents

    # Nothing written to the snapshot for the gated entities — so a re-enable
    # diffs against an accurate (empty) snapshot rather than a suppressed one.
    snap = _rows(path_a, "SELECT entity FROM sync_snapshot WHERE entity IN ('job','triage')")
    assert snap == []

    # Flip inbox ON: the job facts + the triage decision now export.
    box.save({"sync": {"settings": {"inbox": True}}})
    await a.export_changes(tmp_path / "sync2")
    ents2 = {r["entity"] for r in _log_records(tmp_path / "sync2", "A1B2")}
    assert "job" in ents2 and "triage" in ents2


@pytest.mark.asyncio
async def test_inbox_off_skips_job_triage_import(tmp_path, monkeypatch):
    path_b = tmp_path / "b.db"
    folder = tmp_path / "sync"
    await _init_db(path_b, monkeypatch)

    ts = "2026-07-08T12:00:01.000Z"
    job_data = {
        "source": "greenhouse", "external_id": "111", "title": "Engineer",
        "company": "Acme", "location": "Remote", "url": "https://x/111",
        "description": "Build", "salary_min": 100000, "salary_max": 150000,
        "salary_period": "annual", "date_posted": "2026-06-30",
        "date_discovered": "2026-07-01T00:00:00Z", "fit_score": 87.5,
        "fit_reasoning": None, "apply_type": "external", "is_remote": True,
        "is_easy_apply": False, "tags": ["python"], "match_report": None,
        "embellishment_log": None,
    }
    _write_peer(folder, [
        {"v": 1, "entity": "job", "id": "greenhouse:111", "updated_at": ts,
         "device": "PEER", "deleted": False, "data": job_data},
        {"v": 1, "entity": "triage", "id": "greenhouse:111", "updated_at": ts,
         "device": "PEER", "deleted": False, "data": {"status": "shortlisted"}},
    ])

    box = ConfigBox({"sync": {"settings": {"inbox": False}}})
    b = SyncEngine(path_b, "B1", now_fn=Clock(),
                   load_settings=box.load, save_settings=box.save)

    imp = await b.import_changes(folder)
    assert imp.upserts == 0
    assert _rows(path_b, "SELECT * FROM jobs") == []
    assert _rows(path_b, "SELECT entity FROM sync_snapshot WHERE entity IN ('job','triage')") == []

    # Enable inbox and re-import: the job lands and its shortlist decision applies.
    box.save({"sync": {"settings": {"inbox": True}}})
    await b.import_changes(folder)
    jobs = _rows(path_b, "SELECT * FROM jobs")
    assert len(jobs) == 1
    assert jobs[0]["external_id"] == "111"
    assert jobs[0]["status"] == "shortlisted"


@pytest.mark.asyncio
async def test_inbox_default_on_syncs_job_triage(tmp_path, monkeypatch):
    """A config with no explicit inbox toggle defaults ON — job + triage sync."""
    path_a = tmp_path / "a.db"
    await _init_db(path_a, monkeypatch)
    _seed_job(path_a)

    box = ConfigBox({})  # no sync.settings at all -> every default applies
    a = SyncEngine(path_a, "A1B2", now_fn=Clock(),
                   load_settings=box.load, save_settings=box.save)

    await a.export_changes(tmp_path / "sync")
    ents = {r["entity"] for r in _log_records(tmp_path / "sync", "A1B2")}
    assert "job" in ents and "triage" in ents


# ---------------------------------------------------------------------------
# get_jobs — lenient pay gate + new sorts
# ---------------------------------------------------------------------------

def _seed_pay_jobs(path):
    """A spread covering every stated/unstated pay case."""
    rows = [
        # id,   ext,  smin,   smax,   period,   fit,  company,      est_min, est_max
        ("j1", "1", 100000, 120000, "annual", 90.0, "Zebra",   None,   None),   # stated 120k
        ("j2", "2",  90000,   None, "annual", 80.0, "alpha",   None,   None),   # stated 90k (min)
        ("j3", "3",     40,     50, "hourly", 70.0, "Beta",    None,   None),   # 50/hr -> 104k
        ("j4", "4",   None,   None, "unknown", 0.0, "gamma",   80000, 120000),  # unstated, est 100k
        ("j5", "5",  70000,  80000, "monthly", None, "",       None,   None),   # unknown period -> unstated
        ("j6", "6",      0,      0, "annual", 50.0, "Delta",   None,   None),   # zeros -> unstated
    ]
    conn = sqlite3.connect(path)
    try:
        for jid, ext, smin, smax, period, fit, company, emin, emax in rows:
            conn.execute(
                """INSERT INTO jobs (id, source, external_id, title, company, location,
                     url, salary_min, salary_max, salary_period, status, fit_score,
                     date_discovered, estimated_salary_min, estimated_salary_max)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (jid, "greenhouse", ext, f"Role {ext}", company, "Remote",
                 f"https://x/{ext}", smin, smax, period, "discovered", fit,
                 f"2026-07-0{ext}T00:00:00Z", emin, emax),
            )
        conn.commit()
    finally:
        conn.close()


async def _ids(path, monkeypatch, **kw):
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    res = await dbmod.get_jobs(limit=100, **kw)
    return [j["id"] for j in res["jobs"]]


@pytest.mark.asyncio
async def test_pay_floor_lenient_lets_unstated_through(tmp_path, monkeypatch):
    path = tmp_path / "p.db"
    await _init_db(path, monkeypatch)
    _seed_pay_jobs(path)

    ids = set(await _ids(path, monkeypatch, pay_floor=100000, require_stated_pay=False))
    # j1 (120k) + j3 (104k hourly) clear the floor; j4/j5/j6 are unstated -> pass;
    # only j2 (90k) states below the floor and is dropped.
    assert ids == {"j1", "j3", "j4", "j5", "j6"}


@pytest.mark.asyncio
async def test_require_stated_pay_hides_unstated(tmp_path, monkeypatch):
    path = tmp_path / "p.db"
    await _init_db(path, monkeypatch)
    _seed_pay_jobs(path)

    ids = set(await _ids(path, monkeypatch, pay_floor=100000, require_stated_pay=True))
    # Only jobs that STATE >= 100k: j1 (120k) and j3 (104k). j2 states under;
    # j4/j5/j6 are unstated (no amount / unknown period / zeros).
    assert ids == {"j1", "j3"}


@pytest.mark.asyncio
async def test_hourly_annualizes_at_the_boundary(tmp_path, monkeypatch):
    path = tmp_path / "p.db"
    await _init_db(path, monkeypatch)
    _seed_pay_jobs(path)

    # j3 is 50/hr -> 104,000/yr. It clears 100k but not 105k under strict mode.
    assert "j3" in await _ids(path, monkeypatch, pay_floor=100000, require_stated_pay=True)
    assert "j3" not in await _ids(path, monkeypatch, pay_floor=105000, require_stated_pay=True)


@pytest.mark.asyncio
async def test_no_floor_means_no_gate(tmp_path, monkeypatch):
    path = tmp_path / "p.db"
    await _init_db(path, monkeypatch)
    _seed_pay_jobs(path)

    # No floor: require_stated_pay is ignored entirely -> every job passes.
    all_ids = set(await _ids(path, monkeypatch, pay_floor=None, require_stated_pay=True))
    assert all_ids == {"j1", "j2", "j3", "j4", "j5", "j6"}
    zero_ids = set(await _ids(path, monkeypatch, pay_floor=0, require_stated_pay=True))
    assert zero_ids == all_ids


@pytest.mark.asyncio
async def test_sort_salary_uses_raw_stated_then_estimate_midpoint(tmp_path, monkeypatch):
    path = tmp_path / "p.db"
    await _init_db(path, monkeypatch)
    _seed_pay_jobs(path)

    ids = await _ids(path, monkeypatch, sort_by="salary", sort_dir="desc")
    # RAW stated numbers (NOT annualized): j1=120000, j4=est mid 100000, j2=90000,
    # j5=80000, j3=50 (raw hourly!), j6=0. The estimate midpoint (j4) outranks a
    # stated-but-lower salary, and the raw-but-small hourly j3 sinks near the end.
    assert ids == ["j1", "j4", "j2", "j5", "j3", "j6"]


@pytest.mark.asyncio
async def test_sort_company_blanks_last_case_insensitive(tmp_path, monkeypatch):
    path = tmp_path / "p.db"
    await _init_db(path, monkeypatch)
    _seed_pay_jobs(path)

    ids = await _ids(path, monkeypatch, sort_by="company", sort_dir="asc")
    # alpha, Beta, Delta, gamma, Zebra (case-insensitive), then j5 (blank) last.
    assert ids == ["j2", "j3", "j6", "j4", "j1", "j5"]


@pytest.mark.asyncio
async def test_sort_fit_score_sinks_null_and_zero(tmp_path, monkeypatch):
    path = tmp_path / "p.db"
    await _init_db(path, monkeypatch)
    _seed_pay_jobs(path)

    ids = await _ids(path, monkeypatch, sort_by="fit_score", sort_dir="desc")
    # Scored jobs first, in descending score; the 0-score (j4) and NULL-score
    # (j5) jobs sink to the bottom below every scored job.
    assert ids[:4] == ["j1", "j2", "j3", "j6"]  # 90, 80, 70, 50
    assert set(ids[4:]) == {"j4", "j5"}         # 0 and NULL last
