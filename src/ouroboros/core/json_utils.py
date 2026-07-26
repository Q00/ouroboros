"""Shared JSON extraction utilities for evaluation modules.

Provides a robust bracket-matching JSON extractor used by semantic,
consensus, and QA evaluation stages.
"""

import json


def extract_json_payload(text: str) -> str | None:
    """Extract the first valid JSON object or array from text.

    Tries each ``{`` or ``[`` position via bracket-depth counting and
    validates with ``json.loads``.  This handles LLM responses that
    contain prose before the actual JSON payload.

    Args:
        text: Raw text potentially containing a JSON object or array

    Returns:
        Extracted JSON string, or None if no valid JSON is found
    """
    fenced_payload = _extract_fenced_json_payload(text)
    if fenced_payload is not None:
        return fenced_payload

    return _extract_first_json_from_text(text)


def _extract_fenced_json_payload(text: str) -> str | None:
    """Extract the first valid JSON payload from a JSON or bare code fence."""
    fence_start = 0
    while True:
        opener = text.find("```", fence_start)
        if opener == -1:
            return None

        info_start = opener + 3
        line_end = text.find("\n", info_start)
        if line_end == -1:
            return None

        info = text[info_start:line_end].strip().lower()
        if info not in ("", "json"):
            fence_start = info_start
            continue

        body_start = line_end + 1
        closing = _find_closing_fence(text, body_start)
        if closing is None:
            return None

        body = text[body_start:closing]
        payload = _extract_first_json_from_text(body)
        if payload is not None:
            return payload

        fence_start = closing + 3


def _find_closing_fence(text: str, start: int) -> int | None:
    """Return the next markdown closing fence at line start."""
    pos = start
    while True:
        candidate = text.find("```", pos)
        if candidate == -1:
            return None

        line_start = text.rfind("\n", start, candidate) + 1
        prefix = text[line_start:candidate]
        if prefix.strip() == "":
            return candidate

        pos = candidate + 3


def _extract_first_json_from_text(text: str) -> str | None:
    pos = 0
    while True:
        obj_start = text.find("{", pos)
        arr_start = text.find("[", pos)
        if obj_start == -1 and arr_start == -1:
            return None
        if obj_start == -1:
            start = arr_start
        elif arr_start == -1:
            start = obj_start
        else:
            start = min(obj_start, arr_start)

        candidate = _bracket_extract(text, start)
        if candidate is not None:
            try:
                json.loads(candidate)
                return candidate
            except (json.JSONDecodeError, ValueError):
                pass
        pos = start + 1


def _bracket_extract(text: str, start: int) -> str | None:
    """Extract a bracket-balanced substring starting at *start*.

    Supports both ``{}`` (objects) and ``[]`` (arrays).  Returns the
    substring ``text[start:end+1]`` where *end* is the position of
    the matching closer, or ``None`` if brackets never balance.
    """
    open_char = text[start]
    close_char = "}" if open_char == "{" else "]"
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

        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return None
