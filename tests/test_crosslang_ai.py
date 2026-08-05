"""AI response-parsing conformance (Python side + Swift cross-check).

The iOS app re-implements the desktop's LLM score-response fallback chain in
Swift (ScoreResponseParser, extracted from ScoringService). Divergence there is
invisible until a user notices iOS and desktop scoring the same job differently
— so the parser is pinned on both sides by shared fixtures.

Two layers, same fixtures (tests/fixtures/ai_score_responses.json):

  * test_python_parser_matches_fixture — asserts the Python half
    (backend.ai_engine.parse_score_response) on every platform.
  * test_swift_parser_matches_python — compiles the REAL JobsmithKit parser
    sources into a host tool (tools/ai-crosslang) and requires its output to
    equal Python's on every fixture. macOS-only, same pattern and reasoning
    as tests/test_sync_crosslang.py.

If you change the parse chain on either side: change both, and add a fixture
capturing the new behaviour.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from backend.ai_engine import parse_score_response

REPO = Path(__file__).resolve().parent.parent
AI_SRC = REPO / "ios-standalone/JobsmithKit/Sources/JobsmithKit/AI"
TOOL_SRC = REPO / "tools/ai-crosslang/main.swift"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ai_score_responses.json"

FIXTURES = json.loads(FIXTURE_PATH.read_text())["cases"]


@pytest.mark.parametrize("case", FIXTURES, ids=[c["name"] for c in FIXTURES])
def test_python_parser_matches_fixture(case):
    parsed = parse_score_response(case["response"])
    expect = case["expect"]

    if expect is None:
        assert parsed is None, f"expected unparseable, got {parsed!r}"
        return

    assert parsed is not None, "expected a parse, got None"
    score, reasoning, report, _method = parsed
    assert score == expect["score"]
    if "reasoning" in expect:
        assert reasoning == expect["reasoning"]
    if "report" in expect:
        assert report == expect["report"]


def test_fixture_names_unique():
    names = [c["name"] for c in FIXTURES]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# Swift ↔ Python conformance (macOS only)
# ---------------------------------------------------------------------------

# Darwin-only for the same reason as test_sync_crosslang.py: the parser sources
# lean on Foundation behaviours (NSRegularExpression, JSONSerialization NSNumber
# bridging) that the Linux toolchain doesn't ship identically. The macOS CI job
# is where this earns its keep.
_swift = pytest.mark.skipif(
    shutil.which("swiftc") is None or sys.platform != "darwin",
    reason="needs swiftc on macOS",
)


@pytest.fixture(scope="module")
def ai_tool(tmp_path_factory):
    out = tmp_path_factory.mktemp("swifttool") / "ai-crosslang"
    sources = [str(AI_SRC / f) for f in ("LenientJSON.swift", "ScoreResponseParser.swift")]
    subprocess.run(["swiftc", "-O", *sources, str(TOOL_SRC), "-o", str(out)], check=True)
    return out


@_swift
def test_swift_parser_matches_python(ai_tool):
    """The real Swift parse chain must agree with Python on every fixture."""
    result = subprocess.run(
        [str(ai_tool), str(FIXTURE_PATH)], capture_output=True, text=True, check=True
    )
    swift_cases = {c["name"]: c["parsed"] for c in json.loads(result.stdout)["cases"]}

    for case in FIXTURES:
        name = case["name"]
        assert name in swift_cases, f"Swift tool returned nothing for {name}"
        swift = swift_cases[name]
        py = parse_score_response(case["response"])

        if py is None:
            assert swift is None, f"{name}: Python found nothing, Swift parsed {swift!r}"
            continue

        assert swift is not None, f"{name}: Python parsed, Swift found nothing"
        score, reasoning, report, _method = py
        assert swift["score"] == score, f"{name}: score {swift['score']} != {score}"
        assert swift["reasoning"] == reasoning, (
            f"{name}: reasoning diverged\n  swift: {swift['reasoning']!r}\n  py:    {reasoning!r}"
        )
        assert swift["report"] == report, (
            f"{name}: match report diverged\n  swift: {swift['report']!r}\n  py:    {report!r}"
        )
