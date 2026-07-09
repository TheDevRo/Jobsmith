"""Content-addressed document store — see spec/FORMAT.md "Documents".

Binary outputs (resume / cover-letter PDF/DOCX) live at
`documents/{sha256}.{ext}` in the sync folder. Content-addressing gives
collision-free names, natural dedup, and immutability: the same bytes always map
to the same file, so two devices that generate identical output never conflict.

A document is referenced from an application record by
`{"hash": "<sha256>", "ext": "pdf"}`. Missing blobs are non-fatal — a record can
arrive before its bytes; the app treats it as "document syncing".

All writes are atomic (temp file + os.replace) so an interrupted sync can never
leave a half-written blob that would hash-mismatch its name.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Optional

_CHUNK = 1 << 20  # 1 MiB


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _ext(path: str | Path) -> str:
    return Path(path).suffix.lstrip(".").lower()


def _atomic_copy(src: str | Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".tmp-{os.getpid()}-{dest.name}"
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)  # atomic on same filesystem
    finally:
        if tmp.exists():
            tmp.unlink()


class DocumentStore:
    """Reads/writes the sync folder's `documents/` dir and materializes blobs
    into a machine-local directory on import."""

    def __init__(self, store_dir: str | Path, local_dir: Optional[str | Path] = None):
        self.store_dir = Path(store_dir)
        self.local_dir = Path(local_dir) if local_dir else None

    def filename(self, ref: dict) -> str:
        ext = ref.get("ext")
        return f"{ref['hash']}.{ext}" if ext else ref["hash"]

    def blob_path(self, ref: dict) -> Path:
        return self.store_dir / self.filename(ref)

    def has(self, ref: dict) -> bool:
        return self.blob_path(ref).is_file()

    def put(self, local_path: str | Path) -> dict:
        """Copy a local file into the store (if not already present) and return
        its `{"hash", "ext"}` reference."""
        digest = sha256_file(local_path)
        ref = {"hash": digest, "ext": _ext(local_path)}
        dest = self.blob_path(ref)
        if not dest.exists():
            _atomic_copy(local_path, dest)
        return ref

    def materialize(self, ref: dict, dest_basename: str) -> Optional[str]:
        """Copy a stored blob to `local_dir/dest_basename.<ext>`. Returns the
        local path, or None if the blob hasn't synced yet."""
        if self.local_dir is None or not self.has(ref):
            return None
        ext = ref.get("ext")
        dest = self.local_dir / (f"{dest_basename}.{ext}" if ext else dest_basename)
        _atomic_copy(self.blob_path(ref), dest)
        return str(dest)
