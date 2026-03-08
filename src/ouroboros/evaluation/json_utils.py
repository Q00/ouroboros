"""Shared JSON extraction utilities for evaluation modules.

Provides a robust bracket-matching JSON extractor used by semantic,
consensus, and QA evaluation stages.
"""

import re


def extract_json_payload(text: str) -> str | None:
    """Extract the first complete JSON object from text.

    Uses brace-depth counting to find the first balanced ``{...}`` block,
    correctly handling nested objects, strings with escaped characters,
    and code-fenced responses.

    Args:
        text: Raw text potentially containing a JSON object

    Returns:
        Extracted JSON string, or None if no complete object is found
    """
    # Strip code fences first (```json ... ```)
    fence_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if fence_match:
        text = fence_match.group(1)

    start = text.find("{")
    if start == -1:
        return None

    # Count braces to find matching closing brace
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
