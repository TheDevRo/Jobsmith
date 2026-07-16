"""Evicted-iCloud-peer detection: a sync cycle must not merge a partial folder.

When iCloud evicts a peer's changes log it leaves only a `.icloud` placeholder,
so the real bytes are absent. Merging then would silently drop that device's
changes, so sync_once skips the cycle and surfaces the pending download instead.
"""
import asyncio

from backend.sync.service import SyncService


def _service(folder, tmp_path):
    cfg = {"sync": {"enabled": True, "folder": str(folder),
                    "device_id": "A1B2", "device_label": "Mac"}}

    def load_config():
        return cfg

    def save_config(c):
        cfg.clear()
        cfg.update(c)

    return SyncService(
        load_config, save_config,
        db_path_fn=lambda: str(tmp_path / "x.db"),
        docs_dir=tmp_path / "docs",
    )


def test_sync_once_skips_merge_when_peer_log_is_an_icloud_placeholder(tmp_path):
    folder = tmp_path / "sync"
    (folder / "changes").mkdir(parents=True)
    # This device's own log is downloaded...
    (folder / "changes" / "A1B2.jsonl").write_text("")
    # ...but a peer's log is only an undownloaded iCloud placeholder.
    (folder / "changes" / ".C3D4.jsonl.icloud").write_text("")

    svc = _service(folder, tmp_path)
    result = asyncio.run(svc.sync_once())

    assert result["skipped"] is True
    assert result["reason"] == "pending_downloads"
    assert result["pending_downloads"] == ["C3D4"]
    # The status endpoint surfaces it so the UI can explain the stall.
    assert svc.pending_downloads == ["C3D4"]
    assert svc.status()["pending_downloads"] == ["C3D4"]


def test_sync_once_clears_pending_downloads_once_logs_are_present(tmp_path):
    folder = tmp_path / "sync"
    (folder / "changes").mkdir(parents=True)
    (folder / "changes" / "A1B2.jsonl").write_text("")

    svc = _service(folder, tmp_path)
    # Seed a stale pending list, then run a clean cycle (no placeholders).
    svc.pending_downloads = ["C3D4"]
    result = asyncio.run(svc.sync_once())

    assert result.get("reason") != "pending_downloads"
    assert svc.pending_downloads == []
