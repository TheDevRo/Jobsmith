"""Drift guards for the vendored sync contract (see backend/sync/VENDOR.md).

Two failure modes bit us before these tests existed: the vendored schema fell
behind the entities the engines actually emit (`setting`, `work_request` were
missing from its enum), and the vendored merge/profile files were edited
in-tree without the fix ever reaching the upstream `jobsmith-sync` repo — so
the conformance oracle silently disagreed with both shipping engines.
"""
import json
import os
from pathlib import Path

import jsonschema
import pytest

SYNC_DIR = Path(__file__).resolve().parent.parent / "backend" / "sync"
VECTORS_DIR = SYNC_DIR / "test-vectors"
SCHEMA_PATH = SYNC_DIR / "schema" / "change-record.schema.json"

# The sibling contract checkout, when present (dev machines / opt-in CI).
UPSTREAM = Path(os.environ.get("JOBSMITH_SYNC", str(Path.home() / "jobsmith-sync")))


def _vector_records():
    for log in sorted(VECTORS_DIR.glob("*/changes/*.jsonl")):
        for n, line in enumerate(log.read_text().split("\n"), start=1):
            if line.strip():
                yield f"{log.parent.parent.name}/{log.name}:{n}", json.loads(line)


def test_every_vector_record_validates_against_vendored_schema():
    schema = json.loads(SCHEMA_PATH.read_text())
    validator = jsonschema.Draft202012Validator(schema)
    checked = 0
    for where, record in _vector_records():
        errors = list(validator.iter_errors(record))
        assert not errors, f"{where}: {[e.message for e in errors]}"
        checked += 1
    assert checked, "no vector records found — vendored test-vectors missing?"


def test_vector_entities_cover_every_schema_entity():
    # A schema entity no vector exercises is exactly how the enum went stale
    # unnoticed last time — every entity must appear in at least one vector.
    schema_entities = set(
        json.loads(SCHEMA_PATH.read_text())["properties"]["entity"]["enum"]
    )
    vector_entities = {record["entity"] for _, record in _vector_records()}
    assert schema_entities <= vector_entities, (
        f"schema entities never exercised by a vector: "
        f"{sorted(schema_entities - vector_entities)}"
    )


def _without_vendored_header(path: Path) -> str:
    # The in-tree copies carry a "# VENDORED from jobsmith-sync@…" header the
    # upstream files don't; ignore it (and only it) when comparing.
    return "".join(
        line for line in path.read_text().splitlines(keepends=True)
        if not (line.startswith("# VENDORED") or line.startswith("# see backend/sync/VENDOR.md"))
    )


@pytest.mark.skipif(not UPSTREAM.is_dir(), reason="no jobsmith-sync checkout")
def test_vendored_files_match_upstream():
    pairs = [
        (SYNC_DIR / "merge.py", UPSTREAM / "reference" / "merge.py"),
        (SYNC_DIR / "profile_map.py", UPSTREAM / "reference" / "profile_map.py"),
    ]
    pairs += [
        (p, UPSTREAM / "schema" / p.name) for p in (SYNC_DIR / "schema").glob("*.json")
    ]
    pairs += [
        (p, UPSTREAM / "test-vectors" / p.relative_to(VECTORS_DIR))
        for p in VECTORS_DIR.rglob("*") if p.is_file()
    ]
    drifted = [
        str(local.relative_to(SYNC_DIR))
        for local, remote in pairs
        if not remote.exists()
        or _without_vendored_header(local) != _without_vendored_header(remote)
    ]
    assert not drifted, (
        f"vendored files drifted from {UPSTREAM}: {drifted} — "
        "fix upstream and re-run the refresh block in backend/sync/VENDOR.md"
    )
