r"""
Regression: sync logs must be split on '\n' only, not str.splitlines().

json.dumps(rec, ensure_ascii=False) leaves Unicode line separators
(U+0085 NEL, U+2028, U+2029, VT, FF) literal inside string values. Python's
str.splitlines() treats those as line boundaries, which tore a record in two
and produced "Unterminated string" when parsing. '\n' is the only real
delimiter (json.dumps escapes any '\n' inside a string).
"""

import json

from pathlib import Path

from backend.sync import merge
from backend.sync.transport import SyncFolder

# Every separator str.splitlines() breaks on but '\n'.split does not:
# U+0085 NEL, U+2028 LINE SEP, U+2029 PARA SEP, U+000B VT, U+000C FF, U+001C-1E.
SEPARATORS = "\x85  \x0b\x0c\x1c\x1d\x1e"


def _write_log(folder: Path, device: str, records: list[dict]) -> None:
    changes = folder / "changes"
    changes.mkdir(parents=True, exist_ok=True)
    log = changes / f"{device}.jsonl"
    with log.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _rec(rid: str, desc: str) -> dict:
    return {
        "entity": "job",
        "id": rid,
        "updated_at": "2026-07-09T00:00:00Z",
        "device": "A1",
        "deleted": False,
        "data": {"title": "Engineer", "description": desc},
    }


def test_load_logs_preserves_unicode_line_separators(tmp_path):
    nasty = f"before{SEPARATORS}after"
    _write_log(tmp_path, "A1", [_rec("j1", nasty), _rec("j2", "plain")])

    recs = merge.load_logs(tmp_path)

    assert len(recs) == 2, "record with U+0085 etc. must not be split into pieces"
    by_id = {r["id"]: r for r in recs}
    assert by_id["j1"]["data"]["description"] == nasty  # preserved verbatim
    assert by_id["j2"]["data"]["description"] == "plain"


def test_load_logs_skips_truncated_line_without_crashing(tmp_path):
    changes = tmp_path / "changes"
    changes.mkdir(parents=True)
    good = json.dumps(_rec("j1", "ok"), ensure_ascii=False)
    truncated = '{"entity":"job","id":"j2","data":{"description":"unterminated'
    (changes / "A1.jsonl").write_text(good + "\n" + truncated + "\n", encoding="utf-8")

    recs = merge.load_logs(tmp_path)

    assert [r["id"] for r in recs] == ["j1"]  # good kept, corrupt line skipped


def test_compaction_preserves_separator_records(tmp_path):
    nasty = f"a{SEPARATORS}b"
    _write_log(tmp_path, "A1", [_rec("j1", nasty)])
    # Winner is A1's own only record → compaction keeps it, drops nothing.
    dropped = SyncFolder(tmp_path).compact_own_log("A1")
    assert dropped == 0
    recs = merge.load_logs(tmp_path)
    assert recs[0]["data"]["description"] == nasty
