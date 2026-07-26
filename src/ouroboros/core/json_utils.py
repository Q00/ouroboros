"""Shared JSON extraction utilities for evaluation modules.

Provides a robust bracket-matching JSON extractor used by semantic,
consensus, and QA evaluation stages.
"""

from enum import Enum
import json


class _FenceScanState(Enum):
    NO_FENCE = "no_fence"
    PAYLOAD = "payload"
    MALFORMED = "malformed"


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
    fence_state, fenced_payload, fallback_segments = _extract_fenced_json_payload(text)
    if fence_state is _FenceScanState.PAYLOAD:
        return fenced_payload
    if fence_state is _FenceScanState.MALFORMED:
        return None

    for segment in fallback_segments:
        payload = _extract_first_json_from_text(segment)
        if payload is not None:
            return payload
    return None


def _extract_fenced_json_payload(
    text: str,
) -> tuple[_FenceScanState, str | None, tuple[str, ...]]:
    """Extract fenced JSON and return safe outside-fence fallback segments."""
    fence_start = 0
    fallback_parts: list[str] = []
    while True:
        opener = text.find("```", fence_start)
        if opener == -1:
            fallback_parts.append(text[fence_start:])
            return (_FenceScanState.NO_FENCE, None, tuple(fallback_parts))

        info_start = opener + 3
        line_end = text.find("\n", info_start)
        if line_end == -1:
            return (_FenceScanState.MALFORMED, None, ())

        info = text[info_start:line_end].strip().lower()
        if info not in ("", "json"):
            closing = _find_closing_fence(text, line_end + 1)
            if closing is None:
                return (_FenceScanState.MALFORMED, None, ())
            fallback_parts.append(text[fence_start:opener])
            fence_start = closing + 3
            continue

        body_start = line_end + 1
        closing = _find_closing_fence(text, body_start)
        if closing is None:
            return (_FenceScanState.MALFORMED, None, ())

        body = text[body_start:closing]
        payload = _extract_first_json_from_text(body)
        if payload is not None:
            return (_FenceScanState.PAYLOAD, payload, ())

        # A supported fence is an explicit JSON answer boundary. If it cannot
        # be parsed, do not let stale examples elsewhere in the response win.
        return (_FenceScanState.MALFORMED, None, ())


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
