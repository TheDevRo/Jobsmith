"""Transport: manifest registration (idempotent) and own-log compaction."""
import json

from backend.sync import merge as mergelib
from backend.sync.transport import FORMAT_VERSION, SyncFolder


def test_register_device_is_idempotent(tmp_path):
    sf = SyncFolder(tmp_path)
    sf.register_device("A1B2", label="Mac", platform="macos")
    manifest = json.loads(sf.manifest_path.read_text())
    assert manifest["format_version"] == FORMAT_VERSION
    assert manifest["devices"] == [{"id": "A1B2", "label": "Mac", "platform": "macos"}]

    mtime = sf.manifest_path.stat().st_mtime_ns
    sf.register_device("A1B2", label="Mac", platform="macos")  # unchanged
    assert sf.manifest_path.stat().st_mtime_ns == mtime  # not rewritten

    sf.register_device("C3D4", label="iPhone", platform="ios")
    ids = {d["id"] for d in json.loads(sf.manifest_path.read_text())["devices"]}
    assert ids == {"A1B2", "C3D4"}


def test_log_device_ids_from_filenames(tmp_path):
    sf = SyncFolder(tmp_path)
    sf.ensure_dirs()
    (sf.changes_dir / "A1B2.jsonl").write_text("")
    (sf.changes_dir / "C3D4.jsonl").write_text("")
    assert sf.log_device_ids() == {"A1B2", "C3D4"}


def _rec(entity, rid, ts, device, deleted=False, data=None):
    r = {"v": 1, "entity": entity, "id": rid, "updated_at": ts,
         "device": device, "deleted": deleted}
    if not deleted:
        r["data"] = data or {}
    return r


def test_compaction_drops_superseded_but_preserves_merge(tmp_path):
    sf = SyncFolder(tmp_path)
    sf.ensure_dirs()

    # Own log: an old + newer version of job:1 (we still win it), plus job:2
    # which device C later supersedes.
    own = [
        _rec("job", "1", "2026-07-08T10:00:00.000Z", "A1B2", data={"v": "old"}),
        _rec("job", "1", "2026-07-08T11:00:00.000Z", "A1B2", data={"v": "new"}),
        _rec("job", "2", "2026-07-08T10:00:00.000Z", "A1B2", data={"v": "mine"}),
    ]
    other = [
        _rec("job", "2", "2026-07-08T12:00:00.000Z", "C3D4", data={"v": "theirs"}),
    ]
    (sf.changes_dir / "A1B2.jsonl").write_text("\n".join(json.dumps(r) for r in own) + "\n")
    (sf.changes_dir / "C3D4.jsonl").write_text("\n".join(json.dumps(r) for r in other) + "\n")

    before = mergelib.merge_folder(tmp_path)
    dropped = sf.compact_own_log("A1B2")
    after = mergelib.merge_folder(tmp_path)

    assert dropped == 2  # old job:1 version + job:2 (C now owns it)
    assert after == before  # merged state is unchanged

    kept = [json.loads(l) for l in (sf.changes_dir / "A1B2.jsonl").read_text().splitlines()]
    assert len(kept) == 1
    assert kept[0]["id"] == "1" and kept[0]["data"] == {"v": "new"}


def test_evicted_log_ids_flags_undownloaded_icloud_placeholders(tmp_path):
    """An iCloud-evicted peer log appears only as a `.icloud` placeholder with no
    real .jsonl beside it. evicted_log_ids() flags exactly those so the service
    can skip merging a partial folder rather than silently drop that device."""
    sf = SyncFolder(tmp_path)
    sf.ensure_dirs()

    # A downloaded log (real bytes present) — NOT evicted.
    (sf.changes_dir / "A1B2.jsonl").write_text('{"ok": 1}\n')
    # A peer whose log is only an undownloaded placeholder (macOS hidden name).
    (sf.changes_dir / ".C3D4.jsonl.icloud").write_text("")
    # A peer with BOTH a placeholder and the real file (download landed) — NOT evicted.
    (sf.changes_dir / "E5F6.jsonl").write_text('{"ok": 1}\n')
    (sf.changes_dir / ".E5F6.jsonl.icloud").write_text("")

    assert sf.evicted_log_ids() == {"C3D4"}
    # The evicted placeholder does not pollute the authoritative device list.
    assert sf.log_device_ids() == {"A1B2", "E5F6"}
