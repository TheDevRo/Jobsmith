"""
tests/auto_apply/test_extract_json.py

Unit tests for _extract_json() and _trim_trailing_prose() in llm_client.py.

All tests run offline — no LM Studio or network required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.auto_apply.llm_client import _extract_json, _trim_trailing_prose


# ---------------------------------------------------------------------------
# _trim_trailing_prose
# ---------------------------------------------------------------------------

class TestTrimTrailingProse:
    def test_no_trailing_prose(self):
        text = '[{"a": 1}]'
        assert _trim_trailing_prose(text) == text

    def test_trailing_explanation(self):
        text = '[{"field": "name", "value": "John"}] Here is the explanation of my choices.'
        assert _trim_trailing_prose(text) == '[{"field": "name", "value": "John"}]'

    def test_trailing_newline_and_text(self):
        text = '[{"x": 1}]\nSome trailing text\non multiple lines.'
        assert _trim_trailing_prose(text) == '[{"x": 1}]'

    def test_object_with_trailing_prose(self):
        text = '{"key": "value"} extra stuff here'
        assert _trim_trailing_prose(text) == '{"key": "value"}'

    def test_bracket_inside_string_not_counted(self):
        # The ] inside the string value must not close the array early.
        text = '[{"v": "a[b]c"}] trailing'
        assert _trim_trailing_prose(text) == '[{"v": "a[b]c"}]'

    def test_empty_array_with_trailing(self):
        assert _trim_trailing_prose("[] oops") == "[]"

    def test_no_opener_returns_as_is(self):
        text = "just plain text"
        assert _trim_trailing_prose(text) == text

    def test_nested_array(self):
        text = '[[1, 2], [3, 4]] trailing'
        assert _trim_trailing_prose(text) == '[[1, 2], [3, 4]]'


# ---------------------------------------------------------------------------
# _extract_json — trailing-prose cases
# ---------------------------------------------------------------------------

class TestExtractJsonTrailingProse:
    def test_array_with_trailing_explanation(self):
        """Primary scenario from the task spec."""
        raw = '[{"field": "name", "value": "John"}] Here is the explanation...'
        result = _extract_json(raw)
        assert result == [{"field": "name", "value": "John"}]

    def test_multi_element_array_with_trailing(self):
        raw = '[{"a": 1}, {"b": 2}] Done.'
        result = _extract_json(raw)
        assert result == [{"a": 1}, {"b": 2}]

    def test_object_with_trailing(self):
        raw = '{"key": "val"} some prose'
        result = _extract_json(raw)
        assert result == {"key": "val"}

    def test_trailing_newline_prose(self):
        raw = '[{"x": true}]\nThis is my reasoning.'
        result = _extract_json(raw)
        assert result == [{"x": True}]


# ---------------------------------------------------------------------------
# _extract_json — existing behaviour preserved
# ---------------------------------------------------------------------------

class TestExtractJsonExistingBehaviour:
    def test_clean_json(self):
        raw = '[{"field_id": "f1", "value": "hello"}]'
        assert _extract_json(raw) == [{"field_id": "f1", "value": "hello"}]

    def test_markdown_json_fence(self):
        raw = '```json\n[{"a": 1}]\n```'
        assert _extract_json(raw) == [{"a": 1}]

    def test_markdown_plain_fence(self):
        raw = '```\n[{"a": 1}]\n```'
        assert _extract_json(raw) == [{"a": 1}]

    def test_leading_prose_trimmed(self):
        raw = 'Sure, here you go:\n[{"a": 1}]'
        assert _extract_json(raw) == [{"a": 1}]

    def test_leading_and_trailing_prose(self):
        raw = 'Here:\n[{"a": 1}]\nDone.'
        assert _extract_json(raw) == [{"a": 1}]

    def test_invalid_raises(self):
        with pytest.raises(Exception):
            _extract_json("not json at all")
