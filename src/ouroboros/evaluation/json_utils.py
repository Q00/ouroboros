"""Shared JSON extraction utilities for evaluation modules.

Provides a robust bracket-matching JSON extractor used by semantic,
consensus, and QA evaluation stages.
"""

import json
import re


def extract_json_payload(text: str) -> str | None:
    """Extract the first valid JSON object from text.

    Tries each ``{`` position via brace-depth counting and validates
    with ``json.loads``.  This handles LLM responses that contain
    prose (with stray braces) before the actual JSON payload.

    Args:
        text: Raw text potentially containing a JSON object

    Returns:
        Extracted JSON string, or None if no valid object is found
    """
    # Strip code fences first (```json ... ```)
    fence_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if fence_match:
        text = fence_match.group(1)

    pos = 0
    while True:
        start = text.find("{", pos)
        if start == -1:
            return None

        candidate = _brace_extract(text, start)
        if candidate is not None:
            try:
                json.loads(candidate)
                return candidate
            except (json.JSONDecodeError, ValueError):
                pass

        pos = start + 1


def _brace_extract(text: str, start: int) -> str | None:
    """Extract a brace-balanced substring starting at *start*.

    Returns the substring ``text[start:end+1]`` where *end* is the
    position of the matching ``}``, or ``None`` if braces never balance.
    """
    depth = 0
    in_string = False
    escape_next = False

    for i, char in enumerate(text[start:], start=start):
        if escape_next:
            escape_next = False
            continue

        if char == "\\":
            escape_next = True
            continue

        if char == '"' and not escape_next:
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return None
