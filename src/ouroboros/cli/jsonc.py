"""String-aware JSONC-to-JSON converter.

Strips ``//`` line comments, ``/* … */`` block comments, and trailing commas
from JSONC text **without** mangling content inside quoted strings.

This module is shared by :mod:`~ouroboros.cli.commands.setup` and
:mod:`~ouroboros.cli.commands.uninstall` to keep config file handling
consistent.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Matches, in order of priority:
#  1. A double-quoted JSON string (may contain escaped chars)
#  2. A block comment  /* ... */
#  3. A line comment   // ...
_JSONC_TOKEN_RE = re.compile(
    r"""
    (?P<string>   "(?:[^"\\]|\\.)*" )   # JSON string literal
    | (?P<block>  /\*.*?\*/           )   # block comment
    | (?P<line>   //[^\n]*            )   # line comment
    """,
    re.DOTALL | re.VERBOSE,
)

# Trailing comma before closing brace/bracket — must be string-aware
# to avoid rewriting commas inside quoted values like ",}" or ",]".
_TRAILING_COMMA_TOKEN_RE = re.compile(
    r"""
    (?P<string>   "(?:[^"\\]|\\.)*" )   # JSON string literal — keep
    | (?P<comma>  ,\s*[}\]]          )   # trailing comma — strip comma
    """,
    re.DOTALL | re.VERBOSE,
)


def strip_jsonc(text: str) -> str:
    """Convert JSONC text to strict JSON by removing comments and trailing commas.

    Unlike a simple regex sweep, this parser is **string-aware**: comment
    tokens and trailing commas inside quoted strings are preserved verbatim.

    Examples::

        >>> strip_jsonc('{ "url": "https://foo" }')
        '{ "url": "https://foo" }'
        >>> strip_jsonc('{ "a": 1, // comment\\n}')
        '{ "a": 1, \\n}'

    Args:
        text: Raw JSONC content.

    Returns:
        Cleaned string safe for :func:`json.loads`.
    """

    def _replace_comments(m: re.Match[str]) -> str:
        if m.group("string"):
            return m.group("string")
        return ""

    def _replace_trailing(m: re.Match[str]) -> str:
        if m.group("string"):
            return m.group("string")
        # Keep only the closing bracket/brace, drop the comma + whitespace
        matched = m.group("comma")
        return matched.lstrip(",").lstrip()

    text = _JSONC_TOKEN_RE.sub(_replace_comments, text)
    text = _TRAILING_COMMA_TOKEN_RE.sub(_replace_trailing, text)
    return text


def parse_jsonc(text: str) -> Any:
    """Parse JSONC text into a Python object.

    Convenience wrapper that strips comments/trailing commas then delegates
    to :func:`json.loads`.

    Args:
        text: Raw JSONC content.

    Returns:
        The parsed JSON value (usually a dict).

    Raises:
        json.JSONDecodeError: If the cleaned text is not valid JSON.
    """
    return json.loads(strip_jsonc(text))
