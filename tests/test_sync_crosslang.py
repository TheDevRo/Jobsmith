"""Cross-language convergence (Step 8, contract level).

Compiles the real JobsmithKit sync sources (JSONValue + SyncMerge + SyncEntities)
into a host tool and checks the two implementations agree through the shared
folder format, in both directions:

  * a change log emitted by the Swift mappers imports correctly into the desktop
    engine (job/application/profile land; a secret is stripped by Swift);
  * the Swift merge of a Python-produced folder equals the Python oracle.

Skipped when swiftc is unavailable. On-device GRDB/iCloud verification is a
manual Xcode/device step (see backend/sync/SYNC.md); this proves the wire format
and merge rules match across languages without needing the iOS build.
"""
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from backend import database as dbmod
from backend.sync import merge as mergelib
from backend.sync import SyncEngine

REPO = Path(__file__).resolve().parent.parent
SYNC_SRC = REPO / "ios-standalone/JobsmithKit/Sources/JobsmithKit/Sync"
TOOL_SRC = REPO / "tools/sync-crosslang/main.swift"

# Darwin-only, not merely swiftc-only: the Linux toolchain ships swiftc but no
# CoreFoundation, and JSONValue.swift needs CFGetTypeID/CFBooleanGetTypeID to tell
# a Bool from an NSNumber. On the Linux CI runner swiftc exists and the compile
# fails; the macOS job is where this test earns its keep.
pytestmark = pytest.mark.skipif(
    shutil.which("swiftc") is None or sys.platform != "darwin",
    reason="needs swiftc on macOS (JSONValue.swift depends on CoreFoundation)",
)


@pytest.fixture(scope="module")
def tool(tmp_path_factory):
    out = tmp_path_factory.mktemp("swifttool") / "sync-crosslang"
    sources = [str(SYNC_SRC / f) for f in ("JSONValue.swift", "SyncMerge.swift", "SyncEntities.swift")]
    subprocess.run(["swiftc", "-O", *sources, str(TOOL_SRC), "-o", str(out)], check=True)
    return out


async def _init_db(path, monkeypatch):
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    await dbmod.init_db()


@pytest.mark.asyncio
async def test_swift_emitted_log_imports_into_desktop(tool, tmp_path, monkeypatch):
    folder = tmp_path / "sync"
    subprocess.run([str(tool), "emit", str(folder)], check=True)

    db = tmp_path / "desktop.db"
    await _init_db(db, monkeypatch)

    saved = {"ats_login_password": "DESKTOP-LOCAL-SECRET"}
    engine = SyncEngine(
        db, "MAC1",
        load_profile=lambda: dict(saved),
        save_profile=lambda p: (saved.clear(), saved.update(p)),
    )
    imp = await engine.import_changes(folder)
    assert imp.upserts == 3 and imp.profile_updated  # job facts + triage + application, profile

    # Lifecycle fold: the iOS row is triage='shortlisted' + status='discovered'.
    # The Swift mapper folds that pair into the canonical status the desktop
    # speaks, so a shortlist on iOS lands the job in the desktop's Pipeline
    # (status='shortlisted') rather than being lost as an unknown `triage` key.
    job = sqlite3.connect(db).execute(
        "SELECT title, fit_score, is_remote, status FROM jobs WHERE source='greenhouse' AND external_id='777'"
    ).fetchone()
    assert job is not None
    assert job[0] == "iOS Engineer" and job[1] == 91.0 and job[2] == 1
    assert job[3] == "shortlisted"  # triage='shortlisted' folded into status

    app = sqlite3.connect(db).execute(
        "SELECT status, honesty_level FROM applications WHERE id='app-ios-1'"
    ).fetchone()
    assert app == ("approved", "honest")

    # Profile merged in, desktop's local secret preserved, Swift's stripped.
    assert saved["full_name"] == "Alex Kim"
    assert saved["summary"] == "iOS developer"
    assert saved["ats_login_password"] == "DESKTOP-LOCAL-SECRET"
    assert "workday_password" not in saved


@pytest.mark.asyncio
async def test_swift_merge_matches_python_oracle(tool, tmp_path, monkeypatch):
    folder = tmp_path / "sync"
    db = tmp_path / "desktop.db"
    await _init_db(db, monkeypatch)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO jobs (id, source, external_id, title, status, fit_score, "
        "is_remote, tags, date_discovered) VALUES (?,?,?,?,?,?,?,?,?)",
        ("j1", "lever", "200", "Platform Engineer", "discovered", 87.5, 1,
         '["python"]', "2026-07-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    await SyncEngine(db, "MAC1").export_changes(folder)

    out = subprocess.run([str(tool), "merge", str(folder)], check=True, capture_output=True, text=True)
    swift_merged = json.loads(out.stdout)
    python_merged = mergelib.merge_folder(folder)

    assert swift_merged == python_merged
