"""Conformance: the vendored backend merge must agree with the sync oracle.

Runs every vendored merge vector (backend/sync/test-vectors/*/) through
backend.sync.merge and checks the result against expected.json, plus the
read-order-independence and idempotency invariants. This is the desktop side of
the cross-implementation agreement the contract requires (the Swift engine runs
the same vectors in Step 6).
"""
import json
import random
from pathlib import Path

import pytest

from backend.sync import merge as mergelib

VECTORS_DIR = Path(__file__).resolve().parent.parent / "backend" / "sync" / "test-vectors"


def _strip(obj):
    """Drop '_'-prefixed annotation keys recursively (documentation-only)."""
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def _vectors():
    return sorted(p for p in VECTORS_DIR.iterdir() if (p / "expected.json").exists())


def test_vectors_exist():
    assert _vectors(), f"no merge vectors vendored under {VECTORS_DIR}"


@pytest.mark.parametrize("vec", _vectors(), ids=lambda p: p.name)
def test_vector_matches_expected(vec):
    records = mergelib.load_logs(vec)
    expected = _strip(json.loads((vec / "expected.json").read_text()))
    actual = _strip(mergelib.merge(records))
    assert actual == expected


@pytest.mark.parametrize("vec", _vectors(), ids=lambda p: p.name)
def test_vector_invariants(vec):
    records = mergelib.load_logs(vec)
    base = mergelib.merge(records)

    assert mergelib.merge(list(reversed(records))) == base, "reversed order changed result"

    shuffled = list(records)
    random.Random(20260708).shuffle(shuffled)
    assert mergelib.merge(shuffled) == base, "shuffled order changed result"

    assert mergelib.merge(records + records) == base, "duplicate delivery changed result"
