#!/usr/bin/env python3
# VENDORED from jobsmith-sync@0dc5bec (reference/merge.py). Do not edit here —
# see backend/sync/VENDOR.md. This is the merge oracle the desktop engine uses.
"""Reference merge engine for the Jobsmith sync contract.

This is the *oracle*: a dependency-free implementation of the merge rules in
spec/FORMAT.md. The real iOS (Swift) and desktop (Python) engines must produce
identical results, proven by test-vectors/. Keep this file small and obvious —
it exists to be trusted, not extended.

Rules (see spec/FORMAT.md):
  - Records live in changes/{deviceId}.jsonl, one JSON object per line.
  - Merge = fold all records grouped by (entity, id).
  - Winner per key = max by (updated_at, device): newest timestamp wins;
    equal timestamps broken by the higher device-id string.
  - A winning tombstone (deleted=true) removes the record; otherwise its data
    is the live value.
  - The result is independent of the order records are read in.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _parse_ts(value: str) -> datetime:
    """Parse an RFC3339 UTC timestamp. Tolerates a trailing 'Z'."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _wins(candidate: dict, current: dict) -> bool:
    """True if `candidate` should replace `current` as the winner for a key."""
    ct, pt = _parse_ts(candidate["updated_at"]), _parse_ts(current["updated_at"])
    if ct != pt:
        return ct > pt
    return candidate["device"] > current["device"]  # deterministic tiebreak


def merge(records: list[dict]) -> dict:
    """Fold change records into {"live": ..., "tombstones": ...}.

    live[entity][id]        = winning data payload (non-deleted records)
    tombstones[entity][id]  = {} (records whose winning version is a delete)
    """
    winners: dict[tuple[str, str], dict] = {}
    for rec in records:
        key = (rec["entity"], rec["id"])
        if key not in winners or _wins(rec, winners[key]):
            winners[key] = rec

    live: dict[str, dict] = {}
    tombstones: dict[str, dict] = {}
    for (entity, rid), rec in winners.items():
        bucket = tombstones if rec.get("deleted") else live
        bucket.setdefault(entity, {})[rid] = {} if rec.get("deleted") else rec.get("data", {})
    return {"live": live, "tombstones": tombstones}


def load_logs(folder: Path) -> list[dict]:
    """Read every changes/*.jsonl line under `folder` into a flat record list."""
    records: list[dict] = []
    changes_dir = folder / "changes"
    for log in sorted(changes_dir.glob("*.jsonl")):
        # Split on '\n' only. Records are newline-delimited and json.dumps
        # escapes any real '\n' inside a string value, so '\n' is an
        # unambiguous delimiter. str.splitlines() additionally breaks on
        # U+0085/U+2028/U+2029/VT/FF, which json.dumps(ensure_ascii=False)
        # leaves literal inside string values (e.g. a scraped job
        # description) — splitting there tears a record in two and yields
        # "Unterminated string" on parse.
        for line in log.read_text().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("sync: skipping unparseable line in %s", log.name)
    return records


def merge_folder(folder: Path) -> dict:
    return merge(load_logs(folder))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: merge.py <sync-folder>", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(merge_folder(Path(sys.argv[1])), indent=2, sort_keys=True))
