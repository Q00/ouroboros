"""Typed evidence record + validator (RFC v2 H2, #830).

Turns the H2 invariant from "the markdown says emit evidence" into a parser-
enforced contract: leaf executors emit a structured evidence record, the
harness validates it against the active ExecutionProfile's evidence_schema
before accepting the result.

This module is pure validator surface — it does not yet wire into
parallel_executor. The H1 verifier loop (next PR in the stack) consumes
the ValidationResult to decide between accept / retry / escalate.

The evaluator for `rejected_if` is intentionally narrow. It supports only
``<field> == <literal>`` where literal is parsed first as JSON (so YAML/JSON
authors can write ``null``, ``true``, ``false``, numbers, strings, lists) and
then as a Python literal as a fallback (so legacy ``None``/``True``/``False``
keep working). Any other expression shape raises ProfileEvidenceConfigError
so that profile authors get an immediate, loud failure instead of silent
acceptance.

Usage:
    from ouroboros.orchestrator.evidence_schema import (
        extract_evidence, validate_evidence,
    )
    record = extract_evidence(raw_leaf_text)
    result = validate_evidence(profile, record)
    if not result.ok:
        # surface result.missing_fields / result.rejected_by to the harness
        ...
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
import json
import re
from typing import Any

from ouroboros.orchestrator.profile_loader import ExecutionProfile

# Markdown literal contexts are not evidence boundaries. Backtick and tilde
# fences are both recognized so recovery cannot promote JSON-shaped examples.
# JSON-tagged and untagged fences remain supported evidence boundaries.
_FENCE_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]{0,3})(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)$",
    re.MULTILINE,
)
_EXPR_RE = re.compile(r"^\s*(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*==\s*(?P<lit>.+?)\s*$")
_DECODER = json.JSONDecoder()
_NUMERIC_LOOKING_LEXEME_RE = re.compile(r"[+-]?(?:[0-9]|\.[0-9])[0-9.eE+-]*\Z")
_RADIX_LOOKING_LEXEME_RE = re.compile(r"[+-]?0[xob][\w.+-]*\Z", re.IGNORECASE)
_NUMERIC_MANTISSA_LEXEME_RE = re.compile(r"[+-]?[0-9]+(?:\.[0-9]*)?\Z")
# Complete single-line literals own their contents, but never subsequent
# containers. Unclosed quotes/backticks leave later evidence openers visible.
_FENCE_TAIL_TOKEN_RE = re.compile(
    r"""
    "(?:\\[^\r\n]|[^"\\\r\n])*"
    | (?<!\w)'(?:\\[^\r\n]|[^'\\\r\n])*'
    | (?<!`)(?P<ticks>`+)(?!`)[^\r\n]*?(?<!`)(?P=ticks)(?!`)
    | (?P<opener>\{|\[)
    """,
    re.VERBOSE,
)


class EvidenceError(ValueError):
    """Raised when leaf evidence cannot be parsed or validated."""


class ProfileEvidenceConfigError(EvidenceError):
    """Raised when a profile-authored evidence expression is invalid."""


class BlockerCode(StrEnum):
    """Machine-readable terminal blocker classes surfaced by leaf evidence."""

    MISSING_AUTHORITY = "MISSING_AUTHORITY"
    MISSING_ACCESS = "MISSING_ACCESS"
    MISSING_TOOL = "MISSING_TOOL"
    MISSING_CONFIGURATION = "MISSING_CONFIGURATION"
    UNSAFE_SCOPE_CHANGE = "UNSAFE_SCOPE_CHANGE"
    EXTERNAL_DEPENDENCY = "EXTERNAL_DEPENDENCY"


@dataclass(frozen=True)
class EvidenceBlocker:
    """Typed precondition that prevents the leaf from completing an AC."""

    code: BlockerCode
    reason: str
    required_by: str = ""

    def summary(self) -> str:
        detail = f": {self.reason}" if self.reason else ""
        suffix = f" (required_by: {self.required_by})" if self.required_by else ""
        return f"blocked[{self.code.value}]{detail}{suffix}"


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating an evidence record against a profile.

    Attributes:
        ok: True iff no required field is missing and no rejected_if matched.
        missing_fields: Required fields the record did not provide.
        rejected_by: rejected_if expressions that evaluated True against
            the record (verbatim, in profile order).
        blocker: typed terminal blocker if the leaf could not satisfy a
            legitimate precondition. Blockers are not missing evidence.
    """

    ok: bool
    missing_fields: tuple[str, ...] = ()
    rejected_by: tuple[str, ...] = ()
    blocker: EvidenceBlocker | None = None

    def reasons(self) -> tuple[str, ...]:
        """Human-readable, harness-friendly summary of all failure reasons."""
        out: list[str] = []
        if self.blocker is not None:
            out.append(self.blocker.summary())
        if self.missing_fields:
            out.append("missing required fields: " + ", ".join(self.missing_fields))
        out.extend(f"rejected by {expr!r}" for expr in self.rejected_by)
        return tuple(out)


@dataclass(frozen=True)
class EvidenceRecord:
    """Container for the leaf-emitted evidence dict.

    Kept deliberately permissive — schema enforcement is the validator's
    job. We store the raw mapping plus a reference to the source text so
    callers can show provenance on rejection.
    """

    data: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    def get(self, name: str, default: Any = None) -> Any:
        return self.data.get(name, default)


@dataclass(frozen=True)
class _EvidenceExtent:
    end: int
    closed: bool


@dataclass(frozen=True)
class _EvidenceScan:
    values: list[tuple[int, int, Any]]
    decoded: dict[int, tuple[int, Any]]
    malformed: dict[int, _EvidenceExtent]
    nested_payloads: set[int]
    payload_attempts: set[int]


def _top_level_fence_body_starts(text: str) -> Iterator[tuple[str, int, int, int]]:
    """Yield ``(info, body_start, body_end, fence_end)`` for top-level fences."""
    search_pos = 0
    while True:
        opener = _FENCE_LINE_RE.search(text, search_pos)
        if opener is None:
            return

        fence_len = len(opener.group("fence"))
        info = opener.group("info").strip().lower()
        body_start = opener.end()
        if body_start < len(text) and text[body_start] == "\n":
            body_start += 1

        marker = re.escape(opener.group("fence")[0])
        closing_fence_re = re.compile(
            rf"^[ \t]{{0,3}}{marker}{{{fence_len},}}[ \t]*\r?$", re.MULTILINE
        )
        closer = closing_fence_re.search(text, body_start)
        body_end = len(text) if closer is None else closer.start()
        fence_end = len(text) if closer is None else closer.end()
        yield info, body_start, body_end, fence_end

        if closer is None:
            return
        search_pos = fence_end


def _skip_json_whitespace(text: str, start: int) -> int:
    """Move start to the first non-whitespace JSON character."""
    while start < len(text) and text[start] in " \t\r\n":
        start += 1
    return start


def _find_body_start(text: str) -> tuple[int, bool]:
    """Locate the authoritative JSON boundary.

    Evidence boundaries are ordered by position. The final supported top-level
    fence (JSON-tagged or untagged) is authoritative unless a later eligible
    bare value or malformed evidence container exists. Within an authoritative
    fence, the terminal value or malformed container owns the result. Applying
    the same rule before the initial ``raw_decode`` prevents a valid stale
    object from bypassing recovery's terminal-authority checks.
    """
    supported_fences: list[tuple[int, int, int]] = []
    for info, body_start, body_end, fence_end in _top_level_fence_body_starts(text):
        tag = info.split(maxsplit=1)[0:1]
        if not tag or tag[0] == "json":
            supported_fences.append((body_start, body_end, fence_end))

    recovery_text = _mask_markdown_examples(text)
    scan = _scan_evidence_values(recovery_text)
    top_level_values = scan.values

    if supported_fences:
        body_start, body_end, fence_end = supported_fences[-1]
        fenced_values = [
            candidate for candidate in top_level_values if body_start <= candidate[0] < body_end
        ]
        fenced_start = fenced_values[-1][0] if fenced_values else body_start
        fenced_end = fenced_values[-1][1] if fenced_values else body_start

        owned_until = fenced_end
        for pos in sorted(scan.decoded.keys() | scan.malformed.keys()):
            if pos < fenced_end or pos >= body_end or pos < owned_until:
                continue
            decoded = scan.decoded.get(pos)
            if decoded is None:
                extent = scan.malformed[pos]
                gap = recovery_text[fenced_end:pos]
                adjacent_to_value = "\n" not in gap and "\r" not in gap and not gap.strip()
                if adjacent_to_value or pos in scan.payload_attempts:
                    fenced_start = pos
                owned_until = extent.end
            else:
                owned_until = decoded[0]
                if "\n" in recovery_text[pos:owned_until]:
                    fenced_start = pos

        later_values = [candidate for candidate in top_level_values if candidate[0] >= fence_end]
        terminal_start = later_values[-1][0] if later_values else -1
        terminal_end = later_values[-1][1] if later_values else fence_end

        for pos in sorted(scan.malformed):
            if pos >= terminal_end and _is_terminal_malformed_payload(recovery_text, pos, scan):
                terminal_start = pos

        if terminal_start >= 0:
            return terminal_start, False
        return _skip_json_whitespace(text, fenced_start), True

    terminal_start = top_level_values[-1][0] if top_level_values else -1
    terminal_end = top_level_values[-1][1] if top_level_values else 0
    for pos in sorted(scan.malformed):
        if pos >= terminal_end and _is_terminal_malformed_payload(recovery_text, pos, scan):
            terminal_start = pos

    if terminal_start >= 0:
        return terminal_start, False
    return 0, False


def _mask_markdown_examples(text: str) -> str:
    """Hide Markdown literal/example contexts from bare-output recovery.

    The returned string preserves offsets. Explicitly tagged non-JSON fence
    bodies, blockquotes, and indented code blocks are examples rather than
    emitted evidence and must never become recovery candidates.
    """
    masked: list[str] | None = None

    fence_bodies = list(_top_level_fence_body_starts(text))
    for info, body_start, body_end, _ in fence_bodies:
        tag = info.split(maxsplit=1)[0:1]
        if tag and tag[0] != "json":
            if masked is None:
                masked = list(text)
            masked[body_start:body_end] = " " * (body_end - body_start)

    offset = 0
    in_indented_block = False
    previous_blank = True
    for line in text.splitlines(keepends=True):
        if any(body_start <= offset < body_end for _, body_start, body_end, _ in fence_bodies):
            in_indented_block = False
            previous_blank = False
            offset += len(line)
            continue
        content = line.rstrip("\r\n")
        blank = not content.strip()
        blockquote = re.match(r"^[ ]{0,3}>", content) is not None
        indented = content.startswith("\t") or content.startswith("    ")

        if not in_indented_block and indented and previous_blank:
            in_indented_block = True
        elif in_indented_block and not blank and not indented:
            in_indented_block = False
        if blockquote or in_indented_block:
            if masked is None:
                masked = list(text)
            masked[offset : offset + len(line)] = " " * len(line)

        offset += len(line)
        previous_blank = blank

    return text if masked is None else "".join(masked)


def _complete_inline_literal_end(text: str, position: int, limit: int) -> int:
    """Share candidate selection's complete literal spans with structural scans."""
    token = _FENCE_TAIL_TOKEN_RE.match(text, position, limit)
    return token.end() if token is not None and token.group("opener") is None else position


def _is_container_quote_start(text: str, position: int) -> bool:
    if text[position] == '"':
        return True
    return text[position] == "'" and (
        position == 0 or not (text[position - 1].isalnum() or text[position - 1] == "_")
    )


def _container_extent(text: str, opener_pos: int, *, limit: int | None = None) -> _EvidenceExtent:
    """Find the textual extent of a malformed JSON opener via bracket matching.

    When ``{`` or ``[`` at *opener_pos* fails to parse as valid JSON, this
    function determines how far the malformed structure extends by counting
    balanced brackets. Return the end and whether a matching closer exists;
    an unclosed container owns the remainder of its enclosing region.

    Quoted strings are treated as opaque (bracket characters inside quotes
    do not affect nesting) to avoid false boundaries from prose like
    ``{"key": "value with ] inside"}``.
    """
    stack: list[str] = []
    quote_char: str | None = None
    escape_next = False
    opaque_until = opener_pos

    boundary = len(text) if limit is None else min(limit, len(text))
    for i in range(opener_pos, boundary):
        if i < opaque_until:
            continue
        ch = text[i]
        if quote_char is not None:
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                continue
            if ch == quote_char:
                quote_char = None
            continue
        if ch in ('"', "'", "`"):
            opaque_until = _complete_inline_literal_end(text, i, boundary)
            if opaque_until > i:
                continue
        if ch in ('"', "'") and _is_container_quote_start(text, i):
            quote_char = ch
            continue
        if ch in "[{":
            stack.append(ch)
        elif ch in "]}":
            expected = "]" if stack[-1] == "[" else "}"
            if ch != expected:
                continue
            stack.pop()
            if not stack:
                return _EvidenceExtent(i + 1, True)
    return _EvidenceExtent(boundary, False)


_INLINE_EVIDENCE_LABEL_RE = re.compile(
    r"(?:(?:actual|validation)\s+)?evidence(?:\s+follows)?\s*:\s*$",
    re.IGNORECASE,
)
_INLINE_EVIDENCE_LABEL_PREFIX_RE = re.compile(
    r"(?:(?:actual|validation)\s+)?evidence(?:\s+follows)?\s*:\s*",
    re.IGNORECASE,
)


def _is_evidence_container_opener(text: str, opener_pos: int) -> bool:
    """Accept line-level openers and explicit inline evidence labels."""
    line_start = text.rfind("\n", 0, opener_pos) + 1
    prefix = text[line_start:opener_pos]
    return not prefix.strip() or _INLINE_EVIDENCE_LABEL_RE.search(prefix) is not None


def _inline_evidence_value_start(line: str) -> int | None:
    match = _INLINE_EVIDENCE_LABEL_PREFIX_RE.match(line)
    return match.end() if match is not None else None


def _looks_like_json_container(text: str, opener_pos: int, *, limit: int | None = None) -> bool:
    """Return whether a failed evidence opener is a JSON payload attempt.

    Recovery fails closed for line-level containers and containers introduced
    by an explicit inline evidence label. Ordinary prose delimiters remain
    ineligible boundaries.
    """
    if not _is_evidence_container_opener(text, opener_pos):
        return False
    line_start = text.rfind("\n", 0, opener_pos) + 1
    if _INLINE_EVIDENCE_LABEL_RE.search(text[line_start:opener_pos]) is not None:
        return True

    extent = _container_extent(text, opener_pos, limit=limit)
    boundary_end = extent.end
    if text[opener_pos] == "{" and not extent.closed:
        return True
    candidate = text[opener_pos + 1 : boundary_end - 1].lstrip()
    if not candidate:
        return True

    if text[opener_pos] == "[":
        numeric_atom = re.fullmatch(r"-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?\s*,?", candidate)
        return bool(
            candidate[0] in '{["'
            or numeric_atom
            or _is_malformed_scalar_lexeme(candidate)
            or re.match(r"(?:true|false|null)(?:\s*[,\]])", candidate)
            or re.fullmatch(r"(?:undefined|NaN)\s*,?", candidate)
        )

    if candidate[0] in '"}':
        return True
    return (
        re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*:", candidate) is not None
        or re.fullmatch(r"(?:undefined|NaN)\s*,?", candidate) is not None
    )


def _is_terminal_malformed_payload(text: str, position: int, scan: _EvidenceScan) -> bool:
    return position in scan.nested_payloads or (
        _is_evidence_container_opener(text, position) and position in scan.payload_attempts
    )


def _split_outer_components(
    text: str, opener_pos: int, limit: int
) -> tuple[list[str], bool, bool, bool]:
    """Split a container and retain its outer-level separator shape.

    Quoted strings and nested containers are opaque. The scan is bounded by
    the enclosing evidence fence so malformed input cannot borrow delimiters
    from later prose or a subsequent fence. The flags report whether the
    container has an outer colon or comma, and whether the last outer comma
    still leaves a dangling terminal separator.
    """
    opener = text[opener_pos]
    stack = [opener]
    component_start = opener_pos + 1
    components: list[str] = []
    has_outer_colon = False
    has_outer_comma = False
    has_trailing_comma = False
    quote_char: str | None = None
    escape_next = False
    opaque_until = opener_pos

    for index in range(opener_pos + 1, limit):
        if index < opaque_until:
            continue
        char = text[index]
        if quote_char is not None:
            if escape_next:
                escape_next = False
            elif char == "\\":
                escape_next = True
            elif char == quote_char:
                quote_char = None
            continue
        if char in ('"', "'", "`"):
            opaque_until = _complete_inline_literal_end(text, index, limit)
            if opaque_until > index:
                if len(stack) == 1:
                    has_trailing_comma = False
                continue
        if char in ('"', "'") and _is_container_quote_start(text, index):
            if len(stack) == 1:
                has_trailing_comma = False
            quote_char = char
        elif char in "[{":
            if len(stack) == 1:
                has_trailing_comma = False
            stack.append(char)
        elif char in "]}":
            expected = "]" if stack[-1] == "[" else "}"
            if char != expected:
                continue
            stack.pop()
            if not stack:
                components.append(text[component_start:index])
                return components, has_outer_colon, has_outer_comma, has_trailing_comma
        elif len(stack) == 1 and char == ",":
            components.append(text[component_start:index])
            component_start = index + 1
            has_outer_comma = True
            has_trailing_comma = True
        elif len(stack) == 1 and char == ":":
            has_outer_colon = True
            has_trailing_comma = False
        elif len(stack) == 1 and not char.isspace():
            has_trailing_comma = False

    components.append(text[component_start:limit])
    return components, has_outer_colon, has_outer_comma, has_trailing_comma


def _complete_json_value(value: str) -> bool:
    """Return whether *value* is exactly one JSON value, not a prefix."""
    start = _skip_json_whitespace(value, 0)
    if start == len(value):
        return False
    try:
        _, end_offset = _DECODER.raw_decode(value[start:])
    except json.JSONDecodeError:
        return False
    return not value[start + end_offset :].strip()


def _is_malformed_scalar_lexeme(value: str) -> bool:
    """Recognize numeric corruption and scalar literals outside JSON syntax."""
    candidate = value.strip()
    if _complete_json_value(candidate):
        return False
    if _RADIX_LOOKING_LEXEME_RE.fullmatch(candidate):
        return True
    if _NUMERIC_LOOKING_LEXEME_RE.fullmatch(candidate) and (
        "e" in candidate.lower() or _NUMERIC_MANTISSA_LEXEME_RE.fullmatch(candidate)
    ):
        return True
    try:
        literal = ast.literal_eval(candidate)
    except SyntaxError:
        return _NUMERIC_LOOKING_LEXEME_RE.fullmatch(candidate) is not None and not re.fullmatch(
            r"[+-]?[0-9]+(?:\.[0-9]+){2,}", candidate
        )
    except ValueError:
        return False
    return type(literal) in (int, float, complex, bool, type(None))


def _has_nonstructural_json_prefix_suffix(value: str) -> bool:
    """Return whether a decoded JSON prefix is followed by ordinary text."""
    try:
        _, prefix_end = _DECODER.raw_decode(value)
    except json.JSONDecodeError:
        return False
    suffix = value[prefix_end:].strip()
    if not suffix:
        return False
    if suffix.startswith((":", "/*", "//")):
        return False
    try:
        _, suffix_end = _DECODER.raw_decode(suffix)
    except json.JSONDecodeError:
        return True
    return bool(
        suffix_end < len(suffix)
        and not suffix[suffix_end].isspace()
        and suffix[suffix_end] not in '{["'
    )


def _complete_json_member(member: str) -> bool:
    """Return whether *member* is one complete JSON object member."""
    start = _skip_json_whitespace(member, 0)
    try:
        key, key_end = _DECODER.raw_decode(member[start:])
    except json.JSONDecodeError:
        return False
    if not isinstance(key, str):
        return False
    colon = _skip_json_whitespace(member, start + key_end)
    if colon == len(member) or member[colon] != ":":
        return False
    value_start = _skip_json_whitespace(member, colon + 1)
    try:
        _, value_end = _DECODER.raw_decode(member[value_start:])
    except json.JSONDecodeError:
        return False
    return not member[value_start + value_end :].strip()


def _has_structural_container_intent(text: str, opener_pos: int, limit: int) -> bool:
    """Recognize JSON-shaped attempts without new token names.

    A component is strong evidence only when it is a complete JSON value or
    the existing malformed-singleton classifier accepts it. Object members
    also expose their outer key/value separator. Arbitrary bare identifiers
    alone therefore remain prose. A one-component container is still checked:
    otherwise a malformed terminal singleton after prose can be skipped in
    favor of stale evidence.
    """
    components, has_outer_colon, has_outer_comma, has_trailing_comma = _split_outer_components(
        text, opener_pos, limit
    )
    opener = text[opener_pos]
    if (
        opener == "["
        and has_outer_comma
        and (has_trailing_comma or any(not component.strip() for component in components))
    ):
        # Any empty outer slot is an unmistakable structural boundary, even
        # when the non-empty components are prose-like names. Requiring an
        # outer comma keeps a valid empty array (``[]``) classified normally.
        return True
    closer = "]" if opener == "[" else "}"
    component_signal = False
    for component in components:
        stripped = component.strip()
        if not stripped:
            continue
        if _complete_json_value(stripped):
            component_signal = True
            continue
        if opener == "{" and _complete_json_member(stripped):
            component_signal = True
            continue

        lexemes = stripped.split(maxsplit=1)
        following = lexemes[1].split(maxsplit=1)[0] if len(lexemes) == 2 else ""
        following_payload = bool(following) and (
            _complete_json_value(following)
            or _is_malformed_scalar_lexeme(following)
            or following.startswith(("{", "[", '"', ":", "/*", "//"))
        )
        separated_payload = len(lexemes) == 2 and (
            (
                _is_evidence_container_opener(text, opener_pos)
                and lexemes[0] in ("true", "false", "null")
            )
            or (
                (_complete_json_value(lexemes[0]) or _is_malformed_scalar_lexeme(lexemes[0]))
                and following_payload
            )
        )
        if _is_malformed_scalar_lexeme(stripped) or separated_payload:
            component_signal = True
            continue

        # raw_decode accepts prefixes such as ``1`` in ``1word`` and ``"x"``
        # in ``"x"word``. Do not let the legacy classifier promote either
        # prefix to a complete component.
        if _has_nonstructural_json_prefix_suffix(stripped):
            continue

        try:
            _DECODER.raw_decode(stripped)
        except json.JSONDecodeError:
            pass
        else:
            component_signal = True
            continue

        wrapped = f"{opener}{stripped}{closer}"
        if _looks_like_json_container(wrapped, 0):
            component_signal = True

    if opener == "{":
        return has_outer_colon or component_signal
    return component_signal


def _scan_evidence_values(text: str) -> _EvidenceScan:
    """Parse eligible complete top-level JSON values in *text*.

    Recovery candidates must start on their own Markdown line and outside
    literal/example contexts (which are masked before this function runs).
    Container scans retain structural ownership of nested values, while a
    line-level scan also records scalar JSON values such as ``null`` or a
    quoted string so a prohibited terminal payload cannot be skipped in favor
    of stale earlier evidence. Retain closure and nested-value facts so
    boundary selection consumes the same ownership interpretation.
    """
    # Collect all successfully-decoded JSON containers with their spans.
    # Nested values are discovered separately so containment can be enforced.
    all_spans: list[tuple[int, int, Any]] = []
    # Every failed nonliteral opener owns its structural extent, regardless of
    # the first invalid token. Token-shape heuristics are suitable for deciding
    # whether trailing prose is authoritative, but not for containment: an
    # uncommon invalid key or array token must not expose a valid inner object.
    malformed_boundaries: dict[int, _EvidenceExtent] = {}
    fence_bodies = [(start, end) for _, start, end, _ in _top_level_fence_body_starts(text)]
    for token in _FENCE_TAIL_TOKEN_RE.finditer(text):
        if token.group("opener") is None:
            continue
        pos = token.start()
        try:
            parsed, end_offset = _DECODER.raw_decode(text[pos:])
            all_spans.append((pos, pos + end_offset, parsed))
        except json.JSONDecodeError:
            boundary = len(text)
            for body_start, body_end in fence_bodies:
                if pos < body_start:
                    boundary = body_start
                    break
                if pos < body_end:
                    boundary = body_end
                    break
            malformed_boundaries[pos] = _container_extent(text, pos, limit=boundary)

    # Containers can begin anywhere for ownership detection, but scalars are
    # eligible only when they occupy a Markdown line. This avoids interpreting
    # ordinary prose tokens as terminal JSON while preserving explicit scalar
    # payload authority.
    malformed_scalars: set[int] = set()
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        leading = len(content) - len(content.lstrip(" \t"))
        start = offset + leading
        candidate = content[leading:]
        inline_value_start = _inline_evidence_value_start(candidate)
        if inline_value_start is not None:
            payload = candidate[inline_value_start:]
            payload_leading = len(payload) - len(payload.lstrip(" \t"))
            start += inline_value_start + payload_leading
            candidate = payload[payload_leading:]
        if (candidate or inline_value_start is not None) and (
            not candidate or candidate[0] not in "{["
        ):
            complete_scalar = False
            try:
                parsed, end_offset = _DECODER.raw_decode(candidate)
            except json.JSONDecodeError:
                pass
            else:
                if not candidate[end_offset:].strip():
                    all_spans.append((start, start + end_offset, parsed))
                    complete_scalar = True
            if not complete_scalar and (
                inline_value_start is not None or _is_malformed_scalar_lexeme(candidate)
            ):
                malformed_boundaries[start] = _EvidenceExtent(start + len(candidate), True)
                malformed_scalars.add(start)
        offset += len(line)

    # Filter values that are nested within another decoded span or a malformed
    # structural boundary, and require the value itself to begin its line.
    top_level_values: list[tuple[int, int, Any]] = []
    for start, end, value in all_spans:
        if any(
            (other_start, other_end) != (start, end) and other_start <= start and other_end >= end
            for other_start, other_end, _ in all_spans
        ):
            continue
        if not _is_evidence_container_opener(text, start):
            continue
        if any(
            malformed_start < start and end <= extent.end
            for malformed_start, extent in malformed_boundaries.items()
        ):
            continue
        top_level_values.append((start, end, value))

    direct_payload_attempts = {
        position
        for position, extent in malformed_boundaries.items()
        if position in malformed_scalars
        or not extent.closed
        or _looks_like_json_container(text, position, limit=extent.end)
        or _has_structural_container_intent(text, position, extent.end)
    }
    nested_payloads = {
        owner
        for owner, extent in malformed_boundaries.items()
        if any(
            owner < start and end <= extent.end and _is_evidence_container_opener(text, start)
            for start, end, _ in all_spans
        )
        or any(
            owner < child and malformed_boundaries[child].end <= extent.end
            for child in direct_payload_attempts
        )
    }
    return _EvidenceScan(
        values=sorted(top_level_values, key=lambda candidate: (candidate[0], candidate[1])),
        decoded={start: (end, value) for start, end, value in all_spans},
        malformed=malformed_boundaries,
        nested_payloads=nested_payloads,
        payload_attempts=direct_payload_attempts | nested_payloads,
    )


def _has_json_attempt(text: str) -> bool:
    """Determine if text contains evidence of a JSON payload attempt.

    Used exclusively for **error classification**: distinguishing "no JSON
    present at all" from "JSON present but malformed" so the harness can
    surface the correct diagnostic message.

    Returns ``True`` when:
      • A JSON-tagged or untagged fenced block with non-empty content exists
        (explicitly non-JSON fences like ```python are excluded — they are
        code samples, not evidence attempts).
      • A ``{`` character exists anywhere in the text. Prose rarely contains
        a bare ``{`` that isn't a (possibly malformed) JSON object opener,
        so even a single ``{`` is treated as an evidence attempt.  This is
        intentionally aggressive: a false positive here only changes the
        error *message* (from "no JSON present" to "malformed JSON"), never
        the extraction outcome.
      • A ``[`` character begins content the parser can partially decode
        (``colno > 2``), indicating an actual array structure rather than
        a prose label like ``[TAG]``.

    Returns ``False`` only when the output is pure prose with no structural
    JSON delimiters at all.
    """
    # Check for fenced blocks that could be JSON evidence (json-tagged or untagged)
    for info, body_start, _, _ in _top_level_fence_body_starts(text):
        body = text[body_start:].strip()
        if not body:
            continue
        # An explicitly tagged non-JSON fence is not an evidence attempt
        tag = info.split(maxsplit=1)[0:1]
        if tag and tag[0] not in ("json", ""):
            continue
        # JSON-tagged or untagged fence with content = evidence attempt
        return True

    # Check for structural JSON value attempts: { or [ followed by content
    # that actually looks like a JSON structure.
    # - Any { is treated as a JSON object attempt (prose never uses bare {text})
    # - [ requires the parser to make progress past colno 2, because prose
    #   commonly uses [TAG] or [LABEL] patterns that aren't JSON attempts
    for ch in "{[":
        pos = text.find(ch)
        while pos != -1:
            try:
                _DECODER.raw_decode(text[pos:])
                # If it succeeds, we'd have found it in recovery — this
                # shouldn't happen, but if it does, it's a JSON attempt
                return True
            except json.JSONDecodeError as exc:
                if ch == "{":
                    # Any failed { parse is a malformed JSON object attempt
                    return True
                # For [, require the parser to get past the opener into
                # actual content (colno > 2 means it parsed at least one
                # element or got deep enough to be a real array attempt)
                if exc.colno > 2:
                    return True
            pos = text.find(ch, pos + 1)

    return False


def _recover_json_value(
    text: str, primary: int, primary_exc: json.JSONDecodeError, *, fence_found: bool
) -> Any:
    """Fallback for outputs whose strict parse failed: structural recovery.

    Uses the JSON parser to identify all complete top-level values in the text
    (values not contained within another JSON value). The **last** eligible
    value is authoritative, even when it is not an object and must therefore
    be rejected by :func:`extract_evidence`. This prevents stale earlier
    objects from substituting for terminal lists or scalar payloads.

    When *fence_found* is ``True``, the primary position was derived from an
    explicit Markdown code fence — the strongest structural boundary the
    output can provide. A fence that cannot be parsed is malformed, and
    recovery MUST fail closed: earlier illustrative values or valid inner
    fragments cannot override the malformed authoritative fence.

    When no fence is present (*fence_found* is ``False``), recovery scans the
    full text for top-level values, handling prose markers and non-JSON braces
    that precede the evidence record.

    Raises EvidenceError when no candidate decodes, with accurate diagnostics
    distinguishing "no JSON object present at all" from "JSON present but
    malformed".
    """
    # Fence authority: a malformed fence is the strongest signal that the
    # output's evidence section is broken. Recovery must not rescue inner
    # objects or earlier examples when the authoritative fence failed.
    if fence_found:
        msg = (
            f"Evidence is not valid JSON: {primary_exc.msg} (line {primary_exc.lineno}, "
            f"col {primary_exc.colno}). The evidence fence is malformed; "
            f"recovery is not attempted because the fence is the authoritative boundary."
        )
        raise EvidenceError(msg) from primary_exc

    recovery_text = _mask_markdown_examples(text)
    scan = _scan_evidence_values(recovery_text)
    top_level_values = scan.values

    if top_level_values:
        # A later malformed structural boundary is authoritative over every
        # earlier valid value, including an otherwise valid evidence object.
        last_value_end = max(end for _, end, _ in top_level_values)
        for ch_pos in sorted(scan.malformed):
            if ch_pos < last_value_end or not _is_terminal_malformed_payload(
                recovery_text, ch_pos, scan
            ):
                continue
            msg = (
                f"Evidence is not valid JSON: {primary_exc.msg} "
                f"(line {primary_exc.lineno}, col {primary_exc.colno}). "
                f"Malformed evidence at position {ch_pos} follows earlier "
                f"values; recovery refused because the final boundary "
                f"is authoritative."
            )
            raise EvidenceError(msg) from primary_exc

        # Return the final complete value. extract_evidence owns the object
        # type check so terminal non-object payloads fail with the same clear
        # contract error as trusted-position non-objects.
        _, _, parsed = top_level_values[-1]
        return parsed

    # No top-level values found — produce accurate error diagnostics
    if not _has_json_attempt(recovery_text):
        msg = "Leaf output contains no JSON object and no fenced evidence block."
        raise EvidenceError(msg)

    msg = (
        f"Evidence is not valid JSON: {primary_exc.msg} (line {primary_exc.lineno}, "
        f"col {primary_exc.colno}). Tried the fence-guided parse from offset "
        f"{primary} and structural recovery across the full output."
    )
    raise EvidenceError(msg) from primary_exc


def extract_evidence(text: str) -> EvidenceRecord:
    """Pull a JSON evidence record out of a leaf executor's raw output.

    Accepts either a bare JSON object or a single ```json``` fenced block.
    Body extraction is delegated to ``json.JSONDecoder.raw_decode`` so
    the parser — not sentinel scanning — decides where the JSON value
    ends. That keeps `}` and ``` inside string values from truncating
    valid payloads.

    **Resilience**: If the strict fence-based or bare-JSON-from-start parse
    fails, ``_recover_json_value`` structurally scans eligible output for
    complete top-level JSON values. This handles cases where smaller models
    (e.g. adaptive tier) emit prose markers like ``[AC_COMPLETE: 6]`` before
    the evidence JSON. The final eligible value remains authoritative even
    when it is a prohibited non-object payload. When the strict parse
    *succeeds*, its result is likewise authoritative: a non-object there is an
    error, never a cue to keep scanning (so ``[{...}]`` cannot leak its inner
    object out as evidence).

    Raises EvidenceError on missing / malformed payloads so the harness
    can surface a clear failure instead of silently accepting empty
    results.
    """
    if not text or not text.strip():
        msg = "Leaf output is empty; no evidence record to validate."
        raise EvidenceError(msg)

    primary, fence_found = _find_body_start(text)
    try:
        parsed, _ = _DECODER.raw_decode(text[primary:])
    except json.JSONDecodeError as exc:
        parsed = _recover_json_value(text, primary, exc, fence_found=fence_found)

    if not isinstance(parsed, dict):
        msg = f"Evidence must be a JSON object, got {type(parsed).__name__}"
        raise EvidenceError(msg)

    return EvidenceRecord(data=parsed, source=text)


def _parse_literal(raw: str) -> Any:
    """Safely parse the right-hand side of a `field == literal` expression.

    Profiles are YAML-authored and the evidence is JSON, so the natural
    literal spellings authors will reach for are ``null``, ``true``, ``false``,
    plus numbers / strings / lists. We try JSON first so those work
    out-of-the-box. We fall back to ast.literal_eval so legacy Python
    spellings (``None``, ``True``, ``False``) keep working too.
    """
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError) as exc:
        msg = f"Unsupported literal in rejected_if right-hand side: {raw!r} ({exc})"
        raise ProfileEvidenceConfigError(msg) from exc


def _parse_blocker(data: dict[str, Any]) -> EvidenceBlocker | None:
    """Return a typed blocker from a blocked evidence record, if present."""
    status = data.get("status")
    if status not in {"blocked", "BLOCKED"}:
        return None

    raw_blocker = data.get("blocker")
    if raw_blocker is None:
        # Preserve compatibility with ordinary evidence schemas that use
        # status == "blocked" as a domain field or rejected_if literal.
        # A terminal blocker is typed only when the blocker object is present.
        return None
    if not isinstance(raw_blocker, dict):
        msg = "Blocked evidence blocker must be an object."
        raise EvidenceError(msg)

    raw_code = raw_blocker.get("code")
    if not isinstance(raw_code, str):
        msg = "Blocked evidence blocker.code must be a string."
        raise EvidenceError(msg)
    try:
        code = BlockerCode(raw_code)
    except ValueError as exc:
        valid = ", ".join(item.value for item in BlockerCode)
        msg = f"Unknown blocker.code {raw_code!r}; expected one of: {valid}"
        raise EvidenceError(msg) from exc

    raw_reason = raw_blocker.get("reason")
    if not isinstance(raw_reason, str) or not raw_reason.strip():
        msg = "Blocked evidence blocker.reason must be a non-empty string."
        raise EvidenceError(msg)

    raw_required_by = raw_blocker.get("required_by", "")
    if raw_required_by is None:
        raw_required_by = ""
    if not isinstance(raw_required_by, str):
        msg = "Blocked evidence blocker.required_by must be a string when present."
        raise EvidenceError(msg)

    return EvidenceBlocker(
        code=code,
        reason=raw_reason.strip(),
        required_by=raw_required_by.strip(),
    )


def _evaluate_rejection(expr: str, data: dict[str, Any]) -> bool:
    """Evaluate a single rejected_if expression.

    Grammar: ``<field> == <literal>`` only. Anything else raises
    ProfileEvidenceConfigError so profile authors notice immediately instead
    of silently passing.
    """
    match = _EXPR_RE.match(expr)
    if not match:
        msg = (
            f"Unsupported rejected_if expression: {expr!r}. "
            "Only '<field> == <literal>' is currently supported."
        )
        raise ProfileEvidenceConfigError(msg)
    field_name = match.group("field")
    literal = _parse_literal(match.group("lit"))
    # Missing fields evaluate as None for comparison purposes — that way
    # `field == None` triggers on absent keys without needing a separate
    # `is_missing` predicate.
    return data.get(field_name) == literal


def validate_evidence(profile: ExecutionProfile, record: EvidenceRecord) -> ValidationResult:
    """Validate an evidence record against a profile's evidence_schema.

    Args:
        profile: Loaded ExecutionProfile (see profile_loader.load_profile).
        record: Parsed evidence record (see extract_evidence).

    Returns:
        ValidationResult with ok=True iff all required fields are present
        and no rejected_if expression matched.

    Raises:
        EvidenceError: If leaf evidence is malformed.
        ProfileEvidenceConfigError: If any rejected_if expression has unsupported
            syntax. (Profile bugs should be loud, not silent.)
    """
    schema = profile.evidence_schema

    rejected = tuple(expr for expr in schema.rejected_if if _evaluate_rejection(expr, record.data))
    blocker = _parse_blocker(record.data)
    if blocker is not None:
        return ValidationResult(ok=False, blocker=blocker)

    missing = tuple(name for name in schema.required if name not in record.data)

    return ValidationResult(
        ok=not missing and not rejected,
        missing_fields=missing,
        rejected_by=rejected,
    )


__all__ = [
    "BlockerCode",
    "EvidenceBlocker",
    "EvidenceError",
    "ProfileEvidenceConfigError",
    "EvidenceRecord",
    "ValidationResult",
    "extract_evidence",
    "validate_evidence",
]
