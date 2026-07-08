"""Sync folder transport: manifest, device registry, and log compaction.

The transport is deliberately thin — the OS file provider (iCloud/Dropbox/a
bind mount) moves the bytes. This module only manages the two pieces of folder
housekeeping the apps own:

  * manifest.json — the format version and a display list of participating
    devices. It is the *only* shared-write file, so it is treated as advisory:
    the authoritative device list is derived from the changes/*.jsonl filenames
    (each device owns exactly one), and the manifest is rewritten only when it
    actually changes, keeping conflict-copies rare and non-fatal.
  * compaction — a device rewriting *its own* log to drop versions another
    device now supersedes. Safe without coordination because a device only ever
    rewrites the file it owns.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from . import merge as mergelib

FORMAT_VERSION = "0.1.0-draft"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".tmp-{os.getpid()}-{path.name}"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class SyncFolder:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    @property
    def changes_dir(self) -> Path:
        return self.root / "changes"

    @property
    def documents_dir(self) -> Path:
        return self.root / "documents"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def ensure_dirs(self) -> None:
        self.changes_dir.mkdir(parents=True, exist_ok=True)
        self.documents_dir.mkdir(parents=True, exist_ok=True)

    # -- devices -----------------------------------------------------------

    def log_device_ids(self) -> set[str]:
        """Authoritative device list: one changes/{id}.jsonl per device."""
        if not self.changes_dir.exists():
            return set()
        return {p.stem for p in self.changes_dir.glob("*.jsonl")}

    def read_manifest(self) -> dict:
        if self.manifest_path.exists():
            try:
                return json.loads(self.manifest_path.read_text())
            except (ValueError, OSError):
                pass
        return {"format_version": FORMAT_VERSION, "devices": []}

    def register_device(self, device_id: str, label: Optional[str] = None,
                        platform: Optional[str] = None) -> None:
        """Add this device to the manifest if absent. Rewrites only on change."""
        manifest = self.read_manifest()
        devices = manifest.get("devices", [])
        entry = next((d for d in devices if d.get("id") == device_id), None)
        changed = False

        if entry is None:
            entry = {"id": device_id}
            devices.append(entry)
            changed = True
        if label and entry.get("label") != label:
            entry["label"] = label
            changed = True
        if platform and entry.get("platform") != platform:
            entry["platform"] = platform
            changed = True
        if manifest.get("format_version") != FORMAT_VERSION:
            manifest["format_version"] = FORMAT_VERSION
            changed = True

        manifest["devices"] = devices
        if changed:
            _atomic_write_text(self.manifest_path, json.dumps(manifest, indent=2))

    # -- compaction --------------------------------------------------------

    def compact_own_log(self, device_id: str) -> int:
        """Rewrite this device's log to keep only the records it still wins,
        one line per key. Returns the number of lines dropped.

        A line is kept iff this device is the current global winner for its
        (entity, id): then nothing anywhere supersedes it. Everything else —
        older own versions, and keys another device now owns — is dropped
        without loss, because the surviving winner lives in some log."""
        own_log = self.changes_dir / f"{device_id}.jsonl"
        if not own_log.exists():
            return 0

        winners = _winners(mergelib.load_logs(self.root))
        kept: list[str] = []
        seen: set[tuple[str, str]] = set()
        dropped = 0
        for line in own_log.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            key = (rec["entity"], rec["id"])
            w = winners.get(key)
            is_winner = (
                w is not None
                and w["device"] == device_id
                and w["updated_at"] == rec["updated_at"]
                and key not in seen
            )
            if is_winner:
                kept.append(line)
                seen.add(key)
            else:
                dropped += 1

        if dropped:
            _atomic_write_text(own_log, "\n".join(kept) + ("\n" if kept else ""))
        return dropped


def _winners(records: list[dict]) -> dict[tuple[str, str], dict]:
    winners: dict[tuple[str, str], dict] = {}
    for rec in records:
        key = (rec["entity"], rec["id"])
        if key not in winners or mergelib._wins(rec, winners[key]):
            winners[key] = rec
    return winners
