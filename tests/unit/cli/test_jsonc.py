"""Unit tests for the shared JSONC-to-JSON converter."""

from __future__ import annotations

import json

import pytest

from ouroboros.cli.jsonc import parse_jsonc, strip_jsonc


class TestStripJsonc:
    """Tests for strip_jsonc — the string-aware JSONC stripper."""

    def test_passthrough_valid_json(self) -> None:
        """Plain JSON passes through unchanged."""
        text = '{"key": "value", "n": 42}'
        assert strip_jsonc(text) == text

    def test_removes_full_line_comments(self) -> None:
        """Full-line // comments are removed."""
        text = '{\n  // this is a comment\n  "a": 1\n}'
        result = strip_jsonc(text)
        parsed = parse_jsonc(text)
        assert parsed == {"a": 1}
        assert "//" not in result

    def test_removes_inline_comments(self) -> None:
        """Inline // comments after values are removed."""
        text = '{\n  "a": 1 // inline comment\n}'
        parsed = parse_jsonc(text)
        assert parsed == {"a": 1}

    def test_removes_block_comments(self) -> None:
        """Block /* ... */ comments are removed."""
        text = '{\n  /* block\n     comment */\n  "a": 1\n}'
        parsed = parse_jsonc(text)
        assert parsed == {"a": 1}

    def test_removes_trailing_commas_before_brace(self) -> None:
        """Trailing commas before } are stripped."""
        text = '{"a": 1,}'
        parsed = parse_jsonc(text)
        assert parsed == {"a": 1}

    def test_removes_trailing_commas_before_bracket(self) -> None:
        """Trailing commas before ] are stripped."""
        text = '{"a": [1, 2,]}'
        parsed = parse_jsonc(text)
        assert parsed == {"a": [1, 2]}

    # ── Quoted-string edge cases ──────────────────────────────

    def test_preserves_double_slash_in_url_value(self) -> None:
        """Double-slash inside a URL string must not be treated as a comment."""
        text = '{"url": "https://example.com/path"}'
        assert strip_jsonc(text) == text
        assert parse_jsonc(text)["url"] == "https://example.com/path"

    def test_preserves_double_slash_in_middle_of_string(self) -> None:
        """// inside a quoted value (not a URL) must not be stripped."""
        text = '{"note": "keep // this intact"}'
        assert strip_jsonc(text) == text
        assert parse_jsonc(text)["note"] == "keep // this intact"

    def test_preserves_block_comment_syntax_in_string(self) -> None:
        """/* ... */ inside a quoted value must not be stripped."""
        text = '{"pattern": "a/*b*/c"}'
        assert strip_jsonc(text) == text
        assert parse_jsonc(text)["pattern"] == "a/*b*/c"

    def test_preserves_escaped_quotes_in_strings(self) -> None:
        r"""Escaped quotes inside strings must not break the parser."""
        text = r'{"msg": "say \"hello\" // world"}'
        result = strip_jsonc(text)
        assert "// world" in result  # must survive — inside string
        parsed = parse_jsonc(text)
        assert "// world" in parsed["msg"]

    def test_comment_after_string_with_slashes(self) -> None:
        """A real comment after a value containing slashes is still removed."""
        text = '{\n  "url": "https://example.com" // this is a comment\n}'
        parsed = parse_jsonc(text)
        assert parsed == {"url": "https://example.com"}

    # ── Combined features ─────────────────────────────────────

    def test_all_jsonc_features_combined(self) -> None:
        """Realistic config with comments, trailing commas, and URLs."""
        text = """{
  // OpenCode config
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["foo",], /* plugins list */
  "mcp": {
    "server": {
      "url": "https://my.server/api", // endpoint
    },
  },
}"""
        parsed = parse_jsonc(text)
        assert parsed["$schema"] == "https://opencode.ai/config.json"
        assert parsed["plugin"] == ["foo"]
        assert parsed["mcp"]["server"]["url"] == "https://my.server/api"

    def test_empty_input(self) -> None:
        """Empty string returns empty string."""
        assert strip_jsonc("") == ""

    def test_only_comments(self) -> None:
        """Input that is purely comments with no JSON raises on parse."""
        text = "// just a comment\n/* block */"
        with pytest.raises(Exception):
            parse_jsonc(text)

    def test_trailing_comma_inside_string_preserved(self) -> None:
        """Commas followed by } or ] inside strings must not be stripped."""
        text = '{"pattern": ",}", "list": ",]"}'
        result = strip_jsonc(text)
        parsed = json.loads(result)
        assert parsed["pattern"] == ",}"
        assert parsed["list"] == ",]"

    def test_trailing_comma_inside_string_with_actual_trailing(self) -> None:
        """String containing ,} plus actual trailing comma — both handled."""
        text = '{"x": ",}",\n}'
        result = strip_jsonc(text)
        parsed = json.loads(result)
        assert parsed["x"] == ",}"


class TestParseJsonc:
    """Tests for parse_jsonc convenience wrapper."""

    def test_returns_dict(self) -> None:
        """Standard config returns a dict."""
        assert parse_jsonc('{"a": 1}') == {"a": 1}

    def test_raises_on_invalid_json(self) -> None:
        """Malformed content raises JSONDecodeError after stripping."""
        with pytest.raises(Exception):
            parse_jsonc("{not valid json}")
