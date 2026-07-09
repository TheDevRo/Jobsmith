"""Jobsmith desktop sync — serverless, folder-based, last-writer-wins.

The sync *contract* (format, merge rules, profile mapping, JSON schemas, and
conformance vectors) lives in the `jobsmith-sync` repo and is vendored here
under `merge.py`, `profile_map.py`, `schema/`, and `test-vectors/` — see
VENDOR.md. The desktop *implementation* is `engine.py` + `entities.py`.

Public surface:
    SyncEngine(db_path, device_id, load_profile=, save_profile=, now_fn=)
        .export_changes(folder) -> ExportStats
        .import_changes(folder) -> ImportStats
"""
from .engine import ExportStats, ImportStats, SyncEngine, iso_ms

__all__ = ["SyncEngine", "ExportStats", "ImportStats", "iso_ms"]
