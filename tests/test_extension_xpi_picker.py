"""
tests/test_extension_xpi_picker.py

The signed-XPI picker must prefer the highest version across both artifact
locations (web-ext-artifacts/ and the committed extension/signed/), parsing
the version from the filename — git checkouts reset mtimes, so mtime alone
would misrank a committed newer XPI under a stale local one.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.routers import extension as ext_router


def _touch(path: Path, mtime_offset: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"xpi")
    t = time.time() + mtime_offset
    os.utime(path, (t, t))
    return path


def test_highest_version_wins_over_newer_mtime(tmp_path, monkeypatch):
    artifacts = tmp_path / "web-ext-artifacts"
    signed = tmp_path / "signed"
    # Old version has the NEWER mtime (e.g. fresh git checkout of a stale dir)
    _touch(artifacts / "abc-0.2.1.xpi", mtime_offset=100)
    newer = _touch(signed / "abc-0.2.2.xpi", mtime_offset=-100)
    monkeypatch.setattr(ext_router, "_EXT_SIGNED_DIRS", (artifacts, signed))
    assert ext_router._latest_signed_xpi() == newer


def test_mtime_breaks_version_ties(tmp_path, monkeypatch):
    artifacts = tmp_path / "web-ext-artifacts"
    signed = tmp_path / "signed"
    _touch(signed / "abc-0.2.2.xpi", mtime_offset=-100)
    fresher = _touch(artifacts / "def-0.2.2.xpi", mtime_offset=100)
    monkeypatch.setattr(ext_router, "_EXT_SIGNED_DIRS", (artifacts, signed))
    assert ext_router._latest_signed_xpi() == fresher


def test_unversioned_filenames_rank_below_versioned(tmp_path, monkeypatch):
    d = tmp_path / "signed"
    _touch(d / "no-version-name.xpi", mtime_offset=100)
    versioned = _touch(d / "abc-0.1.0.xpi", mtime_offset=-100)
    monkeypatch.setattr(ext_router, "_EXT_SIGNED_DIRS", (d,))
    assert ext_router._latest_signed_xpi() == versioned


def test_missing_dirs_return_none(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ext_router, "_EXT_SIGNED_DIRS", (tmp_path / "nope", tmp_path / "also-nope")
    )
    assert ext_router._latest_signed_xpi() is None


def test_repo_ships_a_signed_xpi():
    """Guard the Docker/source-checkout channel: a committed signed XPI must
    exist so images built without AMO credentials can serve one."""
    signed_dir = Path(__file__).resolve().parent.parent / "extension" / "signed"
    assert list(signed_dir.glob("*.xpi")), (
        "extension/signed/ has no committed .xpi — Docker and source installs "
        "would have no signed Firefox extension (see extension/signed/README.md)"
    )
