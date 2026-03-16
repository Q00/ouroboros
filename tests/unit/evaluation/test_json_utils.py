"""Tests for extract_json_payload — the shared JSON extractor."""

import pytest

from ouroboros.evaluation.json_utils import extract_json_payload


class TestExtractJsonPayload:
    """extract_json_payload must find the first *valid* JSON object."""

    def test_pure_json(self):
        text = '{"score": 0.85, "verdict": "pass"}'
        result = extract_json_payload(text)
        assert result is not None
        assert '"score": 0.85' in result

    def test_json_in_code_fence(self):
        text = '```json\n{"score": 0.85}\n```'
        result = extract_json_payload(text)
        assert result is not None
        assert '"score": 0.85' in result

    def test_prose_before_json(self):
        """The classic Anthropic prefill failure: prose with braces before JSON."""
        text = (
            '{I will analyze this artifact carefully.\n\n'
            'The {complexity} is moderate.\n\n'
            '{"score": 0.90, "verdict": "pass"}'
        )
        result = extract_json_payload(text)
        assert result is not None
        assert '"score": 0.90' in result

    def test_prose_with_curly_braces_before_json(self):
        """Stray braces in prose should be skipped."""
        text = (
            'Let me evaluate the {artifact} quality.\n'
            'Based on {criteria} analysis:\n\n'
            '{"score": 0.75, "verdict": "revise", "reasoning": "needs work"}'
        )
        result = extract_json_payload(text)
        assert result is not None
        assert '"score": 0.75' in result

    def test_nested_json(self):
        text = '{"outer": {"inner": 42}, "key": "value"}'
        result = extract_json_payload(text)
        assert result is not None
        assert '"inner": 42' in result

    def test_escaped_braces_in_strings(self):
        text = '{"msg": "use \\"{key}\\\" syntax", "ok": true}'
        result = extract_json_payload(text)
        assert result is not None

    def test_no_json(self):
        text = "This is plain text with no JSON at all."
        assert extract_json_payload(text) is None

    def test_unbalanced_braces(self):
        text = '{"key": "value"'
        assert extract_json_payload(text) is None

    def test_empty_object(self):
        text = "prefix {} suffix"
        result = extract_json_payload(text)
        assert result == "{}"

    def test_anthropic_prefill_happy(self):
        """Anthropic prefill success: response is just continuation."""
        text = '{"score": 0.84, "verdict": "revise", "dimensions": {"correctness": 0.85}}'
        result = extract_json_payload(text)
        assert result is not None
        assert '"score": 0.84' in result

    def test_anthropic_prefill_failure_mode(self):
        """Anthropic prefill failure: LLM explains before JSON.

        The adapter prepends '{' to response, so we get:
        '{Let me think...\n{"score": ...}'
        """
        text = (
            "{Let me carefully evaluate this document.\n\n"
            "Based on the quality bar provided:\n\n"
            '{"score": 0.88, "verdict": "pass", '
            '"dimensions": {"correctness": 0.90, "completeness": 0.85}}'
        )
        result = extract_json_payload(text)
        assert result is not None
        assert '"score": 0.88' in result

    def test_multiple_json_objects_returns_first_valid(self):
        text = 'prefix {"a": 1} middle {"b": 2} suffix'
        result = extract_json_payload(text)
        assert result is not None
        assert '"a": 1' in result

    def test_invalid_json_with_valid_later(self):
        """First brace-balanced block is not valid JSON, second is."""
        text = '{not json at all} {"valid": true}'
        result = extract_json_payload(text)
        assert result is not None
        assert '"valid": true' in result
