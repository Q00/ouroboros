"""Shared JSON extraction utilities for evaluation modules.

Provides a robust bracket-matching JSON extractor used by semantic,
consensus, and QA evaluation stages.
"""

from dataclasses import dataclass
from enum import Enum
import json
import re


class _FenceScanState(Enum):
    NO_FENCE = "no_fence"
    PAYLOAD = "payload"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class _FenceContainer:
    """Canonical Markdown containers owning one fenced code block."""

    quote_depth: int = 0
    list_content_indent: int = 0


@dataclass(frozen=True)
class _LiteralFenceContainer:
    """Canonical container for a fence rendered as indented-code text."""

    quote_depth: int = 0
    content_indent: int = 4
    inner_container: _FenceContainer = _FenceContainer()


_FENCE_MARKERS = ("`", "~")
_PLAIN_FENCE_PREFIX = re.compile(r"^ {0,3}$")
_HTML_COMMENT = re.compile(r"<!---?>|<!--(?:[^-]|-[^-]|--[^>])*-->")


class _MalformedJsonBoundary(ValueError):
    """Raised when malformed wrapper syntax owns the remaining text."""


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

    try:
        payloads = [
            payload for segment in fallback_segments for payload in _extract_json_from_text(segment)
        ]
    except _MalformedJsonBoundary:
        return None
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

        opener, opener_length, marker, container = opening
        info_start = opener + opener_length
        line_end = text.find("\n", info_start)
        if line_end == -1:
            return (_FenceScanState.MALFORMED, None, ())

        info = text[info_start:line_end].strip().lower()
        if container.quote_depth > 0 and container.list_content_indent > 0:
            closing = _find_closing_fence(
                text,
                line_end + 1,
                opener_length,
                marker,
                container=container,
            )
            if closing is None:
                return (_FenceScanState.MALFORMED, None, ())
            closing_start, closing_length, _ = closing
            fallback_parts.append(text[fence_start:opener])
            fence_start = closing_start + closing_length
            continue
        if info not in ("", "json"):
            closing = _find_closing_fence(
                text,
                line_end + 1,
                opener_length,
                marker,
                container=container,
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
            container=container,
        )
        if closing is None:
            return (_FenceScanState.MALFORMED, None, ())
        closing_start, closing_length, closing_line_start = closing

        body = _decode_fenced_body(
            text[body_start:closing_line_start],
            container=container,
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


def _list_marker_end(text: str, start: int) -> int | None:
    """Return the end of one CommonMark list marker without its padding."""
    if start >= len(text):
        return None
    if text[start] in "*+-":
        return start + 1
    if not text[start].isdigit():
        return None
    end = start
    while end < len(text) and text[end].isdigit() and end - start < 9:
        end += 1
    if end < len(text) and text[end].isdigit():
        return None
    if end == start or end >= len(text) or text[end] not in ".)":
        return None
    return end + 1


def _markdown_column_width(text: str) -> int:
    """Return CommonMark indentation columns with four-column tab stops."""
    column = 0
    for char in text:
        column = (column // 4 + 1) * 4 if char == "\t" else column + 1
    return column


def _strip_indentation_columns(text: str, columns: int) -> str | None:
    """Remove indentation columns, preserving a tab's virtual overshoot."""
    column = 0
    position = 0
    while column < columns:
        if position >= len(text) or text[position] not in " \t":
            return None
        char = text[position]
        column = column + 1 if char == " " else (column // 4 + 1) * 4
        position += 1
    return " " * (column - columns) + text[position:]


def _list_prefix_indents(prefix: str) -> tuple[int | None, int | None]:
    """Return direct-fence and indented-code continuation columns for a list."""
    position = 0
    while True:
        marker_start = position
        marker_indent = 0
        while marker_start < len(prefix) and prefix[marker_start] in " \t":
            next_indent = (
                (marker_indent // 4 + 1) * 4 if prefix[marker_start] == "\t" else marker_indent + 1
            )
            if next_indent > 3:
                break
            marker_indent = next_indent
            marker_start += 1
        marker_end = _list_marker_end(prefix, marker_start)
        if marker_end is None or marker_end >= len(prefix):
            return None, None
        padding_end = marker_end
        while padding_end < len(prefix) and prefix[padding_end] in " \t":
            padding_end += 1
        marker_column = _markdown_column_width(prefix[:marker_end])
        padding_column = _markdown_column_width(prefix[:padding_end])
        padding_width = padding_column - marker_column
        if padding_width == 0:
            return None, None
        if padding_width > 4:
            list_content_indent = marker_column + 1
            remaining_indent = padding_column - list_content_indent
            if padding_end == len(prefix) and 4 <= remaining_indent <= 7:
                return None, list_content_indent + 4
            return None, None
        position = padding_end
        if _PLAIN_FENCE_PREFIX.fullmatch(prefix[position:]) is not None:
            return _markdown_column_width(prefix[:position]), None


def _list_content_indent(prefix: str) -> int | None:
    """Return continuation columns for a direct fenced list item."""
    return _list_prefix_indents(prefix)[0]


def _list_literal_content_indent(prefix: str) -> int | None:
    """Return indentation columns when list padding creates indented code."""
    return _list_prefix_indents(prefix)[1]


def _fence_containers(prefix: str) -> tuple[_FenceContainer, ...]:
    """Parse canonical quote/list containers before one opening fence."""
    containers: set[_FenceContainer] = set()
    quote_depth = 0
    positions = {0}
    while positions:
        for position in positions:
            remainder = prefix[position:]
            if _PLAIN_FENCE_PREFIX.fullmatch(remainder) is not None:
                containers.add(_FenceContainer(quote_depth=quote_depth))
            list_indent = _list_content_indent(remainder)
            if list_indent is not None:
                containers.add(
                    _FenceContainer(
                        quote_depth=quote_depth,
                        list_content_indent=list_indent,
                    )
                )
        positions = _next_blockquote_prefix_positions(prefix, positions)
        quote_depth += 1
    return tuple(
        sorted(
            containers,
            key=lambda container: (-container.quote_depth, container.list_content_indent),
        )
    )


def _container_line_remainders(line: str, container: _FenceContainer) -> tuple[str, ...]:
    """Remove a canonical fence container from one body or closer line."""
    remainders = (
        (line,)
        if container.quote_depth == 0
        else _blockquote_remainders(line, container.quote_depth)
    )
    if container.list_content_indent == 0:
        return remainders

    decoded: set[str] = set()
    for remainder in remainders:
        stripped = _strip_indentation_columns(remainder, container.list_content_indent)
        if stripped is not None:
            decoded.add(stripped)
    return tuple(sorted(decoded, key=len))


def _container_fence_prefix_matches(prefix: str, container: _FenceContainer) -> bool:
    return any(
        _PLAIN_FENCE_PREFIX.fullmatch(remainder) is not None
        for remainder in _container_line_remainders(prefix, container)
    )


def _find_opening_fence(text: str, start: int) -> tuple[int, int, str, _FenceContainer] | None:
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
        containers = _fence_containers(prefix)
        if containers:
            return candidate, candidate_length, marker, containers[0]

        pos = candidate + candidate_length


def _find_closing_fence(
    text: str,
    start: int,
    opener_length: int,
    marker: str,
    *,
    container: _FenceContainer,
) -> tuple[int, int, int] | None:
    """Return a clean closer without crossing a container discontinuity."""
    line_start = start
    while line_start <= len(text):
        line_stop = text.find("\n", line_start)
        if line_stop == -1:
            line_stop = len(text)
        line_end = line_stop
        if line_end > line_start and text[line_end - 1] == "\r":
            line_end -= 1

        fence_candidates = sorted(
            (candidate, candidate_marker)
            for candidate_marker in _FENCE_MARKERS
            if (candidate := text.find(candidate_marker * 3, line_start, line_end)) != -1
        )
        for candidate, candidate_marker in fence_candidates:
            prefix = text[line_start:candidate]
            if not _container_fence_prefix_matches(prefix, container):
                continue
            candidate_length = _fence_run_length(text, candidate, candidate_marker)
            suffix = text[candidate + candidate_length : line_end]
            if (
                candidate_marker == marker
                and candidate_length >= opener_length
                and suffix.strip() == ""
            ):
                return candidate, candidate_length, line_start
            return None

        if not _container_line_remainders(text[line_start:line_end], container):
            return None
        if line_stop == len(text):
            return None
        line_start = line_stop + 1
    return None


def _decode_fenced_body(body: str, *, container: _FenceContainer) -> str | None:
    """Strip one fence's canonical quote/list containers from its body."""
    if container == _FenceContainer():
        return body.strip()

    decoded: list[str] = []
    for line in body.splitlines():
        remainders = _container_line_remainders(line, container)
        if not remainders:
            return None
        decoded.append(
            min(
                remainders,
                key=lambda remainder: (
                    len(remainder) - len(remainder.lstrip(" \t")),
                    len(remainder),
                ),
            )
        )
    return "\n".join(decoded).strip()


def _indented_fence_line(
    text: str, line_start: int, line_end: int
) -> tuple[_LiteralFenceContainer, str, str, int, str] | None:
    """Describe a literal fence line inside a Markdown indented-code block."""
    fence_line = _literal_fence_marker_line(text, line_start, line_end)
    if fence_line is None:
        return None
    prefix, marker, marker_length, suffix = fence_line
    containers = _literal_fence_containers(prefix)
    if not containers:
        return None
    return containers[0], prefix, marker, marker_length, suffix


def _literal_fence_marker_line(
    text: str, line_start: int, line_end: int
) -> tuple[str, str, int, str] | None:
    """Describe a possible literal fence marker without inferring its owner."""
    marker_start = line_start
    while marker_start < line_end and text[marker_start] not in _FENCE_MARKERS:
        marker_start += 1
    prefix = text[line_start:marker_start]
    if marker_start >= line_end:
        return None

    marker = text[marker_start]
    if marker not in _FENCE_MARKERS:
        return None
    marker_length = _fence_run_length(text, marker_start, marker)
    if marker_length < 3:
        return None
    suffix = text[marker_start + marker_length : line_end].strip()
    return prefix, marker, marker_length, suffix


def _next_blockquote_prefix_positions(text: str, positions: set[int]) -> set[int]:
    """Advance normalized CommonMark blockquote prefixes by one marker."""
    next_positions: set[int] = set()
    for position in positions:
        marker_positions = [position]
        cursor = position
        marker_indent = 0
        while cursor < len(text) and text[cursor] in " \t":
            next_indent = (
                (marker_indent // 4 + 1) * 4 if text[cursor] == "\t" else marker_indent + 1
            )
            if next_indent > 3:
                break
            marker_indent = next_indent
            cursor += 1
            marker_positions.append(cursor)
        for marker_position in marker_positions:
            if marker_position >= len(text) or text[marker_position] != ">":
                continue
            after_marker = marker_position + 1
            next_positions.add(after_marker)
            if after_marker < len(text) and text[after_marker] in " \t":
                next_positions.add(after_marker + 1)
    return next_positions


def _blockquote_remainders(text: str, quote_depth: int) -> tuple[str, ...]:
    """Strip one normalized blockquote depth and return legal remainders."""
    positions = {0}
    for _ in range(quote_depth):
        positions = _next_blockquote_prefix_positions(text, positions)
        if not positions:
            return ()
    return tuple(text[position:] for position in positions)


def _literal_fence_containers(prefix: str) -> tuple[_LiteralFenceContainer, ...]:
    """Parse indented-code and list-indented literal fence containers."""
    containers: set[_LiteralFenceContainer] = set()
    positions = {0}
    quote_depth = 0
    while positions:
        for position in positions:
            remainder = prefix[position:]
            code_content = _strip_indentation_columns(remainder, 4)
            if code_content is not None:
                for inner_container in _fence_containers(code_content):
                    containers.add(
                        _LiteralFenceContainer(
                            quote_depth=quote_depth,
                            inner_container=inner_container,
                        )
                    )
            list_indent = _list_literal_content_indent(remainder)
            if list_indent is not None:
                containers.add(
                    _LiteralFenceContainer(
                        quote_depth=quote_depth,
                        content_indent=list_indent,
                    )
                )
        positions = _next_blockquote_prefix_positions(prefix, positions)
        quote_depth += 1
    return tuple(
        sorted(
            containers,
            key=lambda container: (-container.quote_depth, container.content_indent),
        )
    )


def _is_indented_code_content_line(
    text: str,
    line_start: int,
    line_end: int,
    *,
    container: _LiteralFenceContainer,
) -> bool:
    """Return whether a nonblank line remains inside an indented code block."""
    content = text[line_start:line_end]
    remainders = (
        (content,)
        if container.quote_depth == 0
        else _blockquote_remainders(content, container.quote_depth)
    )
    for remainder in remainders:
        if not remainder.strip():
            return True
        stripped = _strip_indentation_columns(remainder, container.content_indent)
        if stripped is not None and _container_line_remainders(stripped, container.inner_container):
            return True
    return False


def _literal_fence_prefix_matches(prefix: str, container: _LiteralFenceContainer) -> bool:
    remainders = (
        (prefix,)
        if container.quote_depth == 0
        else _blockquote_remainders(prefix, container.quote_depth)
    )
    for remainder in remainders:
        stripped = _strip_indentation_columns(remainder, container.content_indent)
        if stripped is not None and _container_fence_prefix_matches(
            stripped, container.inner_container
        ):
            return True
    return False


def _indented_fence_example_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Return closed literal-fence examples that cannot supply answer JSON.

    Only the complete indented `````...`````-shaped example is excluded. Raw
    JSON candidates are otherwise scanned from the original text, preserving
    every nested four-space or tab-indented line byte-for-byte. Once a literal
    opener is recognized, an invalid or missing contiguous closer owns the
    extraction boundary and fails closed instead of exposing its example body.
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
        container, _, marker, marker_length, _ = opener

        closing_index = index + 1
        while closing_index < len(lines):
            closing_start, closing_end, closing_stop = lines[closing_index]
            closing = _literal_fence_marker_line(text, closing_start, closing_end)
            if (
                closing is not None
                and closing[1] == marker
                and closing[2] >= marker_length
                and closing[3] == ""
                and _literal_fence_prefix_matches(closing[0], container)
            ):
                ranges.append((opener_start, closing_stop))
                index = closing_index + 1
                break
            if not _is_indented_code_content_line(
                text,
                closing_start,
                closing_end,
                container=container,
            ):
                raise _MalformedJsonBoundary
            closing_index += 1
        else:
            raise _MalformedJsonBoundary
    return tuple(ranges)


def _is_backslash_escaped(text: str, position: int) -> bool:
    """Return whether Markdown backslashes escape the character at *position*."""
    backslashes = 0
    cursor = position
    while cursor > 0 and text[cursor - 1] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _closing_code_span(text: str, start: int, delimiter_length: int) -> int | None:
    """Return the end of the next equal-length Markdown backtick run."""
    cursor = start
    while (candidate := text.find("`", cursor)) != -1:
        run_length = _fence_run_length(text, candidate, "`")
        if run_length == delimiter_length:
            return candidate + run_length
        cursor = candidate + run_length
    return None


def _markdown_non_answer_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Return inline Markdown contexts that cannot own answer JSON.

    Code-span delimiters close only on an equal-length backtick run, matching
    CommonMark's variable-delimiter rule.  An unmatched run remains ordinary
    prose so it cannot hide a later answer.  HTML comments use CommonMark's
    complete inline grammar, including its two short empty-comment forms;
    escaped, malformed, and unclosed openers remain ordinary text.
    """
    ranges: list[tuple[int, int]] = []
    position = 0
    while position < len(text):
        comment_start = text.find("<!--", position)
        code_start = text.find("`", position)
        if comment_start == -1 and code_start == -1:
            break

        if comment_start != -1 and (code_start == -1 or comment_start < code_start):
            if _is_backslash_escaped(text, comment_start):
                position = comment_start + 4
                continue
            comment = _HTML_COMMENT.match(text, comment_start)
            if comment is None:
                position = comment_start + 4
                continue
            ranges.append(comment.span())
            position = comment.end()
            continue

        assert code_start != -1
        delimiter_length = _fence_run_length(text, code_start, "`")
        if _is_backslash_escaped(text, code_start):
            position = code_start + delimiter_length
            continue
        code_end = _closing_code_span(
            text,
            code_start + delimiter_length,
            delimiter_length,
        )
        if code_end is None:
            position = code_start + delimiter_length
            continue
        ranges.append((code_start, code_end))
        position = code_end
    return tuple(ranges)


def _non_answer_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Return sorted, merged Markdown example ranges for raw fallback."""
    ranges = sorted((*_indented_fence_example_ranges(text), *_markdown_non_answer_ranges(text)))
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _extract_json_from_text(text: str) -> tuple[str, ...]:
    """Return non-overlapping valid JSON payloads found in prose."""
    payloads: list[str] = []
    excluded_ranges = _non_answer_ranges(text)
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
                # A complete balanced candidate is one structured-output
                # boundary, even when its contents are invalid JSON.  Treat
                # it as opaque so a valid nested fragment cannot be promoted
                # to the authoritative payload.  Independent payloads after
                # the failed boundary remain eligible for extraction.
                pos = start + len(candidate)
                continue
            else:
                payloads.append(candidate)
                pos = start + len(candidate)
                continue
        else:
            # An unbalanced structured opener owns the remaining text. Its
            # nested delimiters have no independent boundary.
            raise _MalformedJsonBoundary
        pos = start + 1


def _bracket_extract(text: str, start: int) -> str | None:
    """Extract a bracket-balanced substring starting at *start*.

    Supports nested ``{}`` and ``[]`` boundaries and treats double-quoted,
    single-quoted, and backtick spans as lexically opaque. Returns the
    substring through the matching outer closer, or ``None`` when the outer
    boundary never balances or delimiters mismatch.
    """
    matching_opener = {"}": "{", "]": "["}
    stack: list[str] = []
    quote_char: str | None = None
    escape_next = False

    for i, char in enumerate(text[start:], start=start):
        if quote_char is not None:
            if escape_next:
                escape_next = False
                continue
            if char == "\\":
                escape_next = True
                continue
            if char == quote_char:
                quote_char = None
            continue

        if char in ('"', "'", "`"):
            quote_char = char
            continue

        if char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack or stack[-1] != matching_opener[char]:
                return None
            stack.pop()
            if not stack:
                return text[start : i + 1]

    return None
