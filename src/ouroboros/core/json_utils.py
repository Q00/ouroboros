"""Shared JSON extraction utilities for evaluation modules.

Provides a robust bracket-matching JSON extractor used by semantic,
consensus, and QA evaluation stages.
"""

from enum import Enum
import json
import re


class _FenceScanState(Enum):
    NO_FENCE = "no_fence"
    PAYLOAD = "payload"
    MALFORMED = "malformed"


_FENCE_MARKERS = ("`", "~")
_BLOCKQUOTE_FENCE_PREFIX = re.compile(r"^[ \t]{0,3}(?:>[ \t]?)+$")
_PLAIN_FENCE_PREFIX = re.compile(r"^ {0,3}$")
_INDENTED_CODE_PREFIX = re.compile(r"^(?: {4}| {0,3}\t)")


def extract_json_payload(text: str) -> str | None:
    """Extract one authoritative JSON object or array from text.

    A supported JSON fence is an explicit answer boundary and wins over
    surrounding prose.  Without such a fence, exactly one valid payload must
    exist; multiple payloads are ambiguous and fail closed rather than letting
    an earlier example become authoritative.

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

    payloads = [
        payload for segment in fallback_segments for payload in _extract_json_from_text(segment)
    ]
    return payloads[0] if len(payloads) == 1 else None


def _extract_fenced_json_payload(
    text: str,
) -> tuple[_FenceScanState, str | None, tuple[str, ...]]:
    """Extract fenced JSON and return safe outside-fence fallback segments."""
    fence_start = 0
    fallback_parts: list[str] = []
    supported_payloads: list[str] = []
    while True:
        opening = _find_opening_fence(text, fence_start)
        if opening is None:
            fallback_parts.append(text[fence_start:])
            if len(supported_payloads) == 1:
                return (_FenceScanState.PAYLOAD, supported_payloads[0], ())
            if supported_payloads:
                return (_FenceScanState.MALFORMED, None, ())
            return (_FenceScanState.NO_FENCE, None, tuple(fallback_parts))

        opener, opener_length, marker, quote_prefix = opening
        info_start = opener + opener_length
        line_end = text.find("\n", info_start)
        if line_end == -1:
            return (_FenceScanState.MALFORMED, None, ())

        info = text[info_start:line_end].strip().lower()
        if info not in ("", "json"):
            closing = _find_closing_fence(
                text,
                line_end + 1,
                opener_length,
                marker,
                quote_prefix=quote_prefix,
            )
            if closing is None:
                return (_FenceScanState.MALFORMED, None, ())
            closing_start, closing_length, _ = closing
            fallback_parts.append(text[fence_start:opener])
            fence_start = closing_start + closing_length
            continue

        body_start = line_end + 1
        closing = _find_closing_fence(
            text,
            body_start,
            opener_length,
            marker,
            quote_prefix=quote_prefix,
        )
        if closing is None:
            return (_FenceScanState.MALFORMED, None, ())
        closing_start, closing_length, closing_line_start = closing

        body = _decode_fenced_body(
            text[body_start:closing_line_start],
            quote_prefix=quote_prefix,
        )
        if body is None:
            return (_FenceScanState.MALFORMED, None, ())
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict | list):
            supported_payloads.append(body)
            fallback_parts.append(text[fence_start:opener])
            fence_start = closing_start + closing_length
            continue

        # A supported fence is an explicit JSON answer boundary. If it cannot
        # be parsed, do not let stale examples elsewhere in the response win.
        return (_FenceScanState.MALFORMED, None, ())


def _fence_run_length(text: str, start: int, marker: str) -> int:
    end = start
    while end < len(text) and text[end] == marker:
        end += 1
    return end - start


def _find_opening_fence(text: str, start: int) -> tuple[int, int, str, str | None] | None:
    """Return the next line-start backtick or tilde fence."""
    pos = start
    while True:
        candidates = [
            (candidate, marker)
            for marker in _FENCE_MARKERS
            if (candidate := text.find(marker * 3, pos)) != -1
        ]
        if not candidates:
            return None

        candidate, marker = min(candidates, key=lambda item: item[0])
        candidate_length = _fence_run_length(text, candidate, marker)
        line_start = text.rfind("\n", 0, candidate) + 1
        prefix = text[line_start:candidate]
        quote_prefix = _blockquote_prefix(prefix)
        if _PLAIN_FENCE_PREFIX.fullmatch(prefix) is not None or quote_prefix is not None:
            return candidate, candidate_length, marker, quote_prefix

        pos = candidate + candidate_length


def _find_closing_fence(
    text: str,
    start: int,
    opener_length: int,
    marker: str,
    *,
    quote_prefix: str | None,
) -> tuple[int, int, int] | None:
    """Return a clean same-marker fence at least as long as the opener."""
    pos = start
    while True:
        candidate = text.find(marker * 3, pos)
        if candidate == -1:
            return None

        candidate_length = _fence_run_length(text, candidate, marker)
        line_start = text.rfind("\n", 0, candidate) + 1
        line_end = text.find("\n", candidate + candidate_length)
        if line_end == -1:
            line_end = len(text)
        prefix = text[line_start:candidate]
        suffix = text[candidate + candidate_length : line_end]
        prefix_matches = (
            prefix == quote_prefix
            if quote_prefix is not None
            else _PLAIN_FENCE_PREFIX.fullmatch(prefix) is not None
        )
        if candidate_length >= opener_length and prefix_matches and suffix.strip() == "":
            return candidate, candidate_length, line_start

        pos = candidate + candidate_length


def _blockquote_prefix(prefix: str) -> str | None:
    """Return the exact Markdown quote prefix used by a fence line."""
    return prefix if _BLOCKQUOTE_FENCE_PREFIX.fullmatch(prefix) is not None else None


def _decode_fenced_body(body: str, *, quote_prefix: str | None) -> str | None:
    """Strip one fence's exact quote prefix from every quoted body line."""
    if quote_prefix is None:
        return body.strip()

    decoded: list[str] = []
    for line in body.splitlines():
        if not line.startswith(quote_prefix):
            return None
        decoded.append(line[len(quote_prefix) :])
    return "\n".join(decoded).strip()


def _indented_fence_line(
    text: str, line_start: int, line_end: int
) -> tuple[str, str, int, str] | None:
    """Describe a literal fence line inside a Markdown indented-code block."""
    marker_start = line_start
    while marker_start < line_end and text[marker_start] in " \t":
        marker_start += 1
    prefix = text[line_start:marker_start]
    if _INDENTED_CODE_PREFIX.match(prefix) is None or marker_start >= line_end:
        return None

    marker = text[marker_start]
    if marker not in _FENCE_MARKERS:
        return None
    marker_length = _fence_run_length(text, marker_start, marker)
    if marker_length < 3:
        return None
    suffix = text[marker_start + marker_length : line_end].strip()
    return prefix, marker, marker_length, suffix


def _indented_fence_example_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Return closed literal-fence examples that cannot supply answer JSON.

    Only the complete indented `````...`````-shaped example is excluded. Raw
    JSON candidates are otherwise scanned from the original text, preserving
    every nested four-space or tab-indented line byte-for-byte.
    """
    lines: list[tuple[int, int, int]] = []
    line_start = 0
    for line in text.splitlines(keepends=True):
        line_stop = line_start + len(line)
        content_end = line_stop
        while content_end > line_start and text[content_end - 1] in "\r\n":
            content_end -= 1
        lines.append((line_start, content_end, line_stop))
        line_start = line_stop
    if line_start < len(text) or not lines:
        lines.append((line_start, len(text), len(text)))

    ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        opener_start, opener_end, _ = lines[index]
        opener = _indented_fence_line(text, opener_start, opener_end)
        if opener is None:
            index += 1
            continue
        prefix, marker, marker_length, _ = opener

        closing_index = index + 1
        while closing_index < len(lines):
            closing_start, closing_end, closing_stop = lines[closing_index]
            closing = _indented_fence_line(text, closing_start, closing_end)
            if (
                closing is not None
                and closing[0] == prefix
                and closing[1] == marker
                and closing[2] >= marker_length
                and closing[3] == ""
            ):
                ranges.append((opener_start, closing_stop))
                index = closing_index + 1
                break
            closing_index += 1
        else:
            index += 1
    return tuple(ranges)


def _extract_json_from_text(text: str) -> tuple[str, ...]:
    """Return non-overlapping valid JSON payloads found in prose."""
    payloads: list[str] = []
    excluded_ranges = _indented_fence_example_ranges(text)
    excluded_index = 0
    pos = 0
    while True:
        obj_start = text.find("{", pos)
        arr_start = text.find("[", pos)
        if obj_start == -1 and arr_start == -1:
            return tuple(payloads)
        if obj_start == -1:
            start = arr_start
        elif arr_start == -1:
            start = obj_start
        else:
            start = min(obj_start, arr_start)

        while excluded_index < len(excluded_ranges) and excluded_ranges[excluded_index][1] <= start:
            excluded_index += 1
        if (
            excluded_index < len(excluded_ranges)
            and excluded_ranges[excluded_index][0] <= start < excluded_ranges[excluded_index][1]
        ):
            pos = excluded_ranges[excluded_index][1]
            continue

        candidate = _bracket_extract(text, start)
        if candidate is not None:
            try:
                json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                pass
            else:
                payloads.append(candidate)
                pos = start + len(candidate)
                continue
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
