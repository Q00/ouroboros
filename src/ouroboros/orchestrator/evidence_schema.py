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

# Fence openers signal where the JSON evidence body starts. Prefer
# language-tagged JSON fences over bare fences anywhere in the output:
# leaf results commonly include earlier non-JSON code fences before the
# final "Validation evidence" block. Once we've located the opener,
# parsing the body is delegated to JSON itself via
# json.JSONDecoder.raw_decode — that's how we avoid every sentinel-
# scanning class of bug (the closing ``` or any `}` may appear inside a
# JSON string value, and only a real JSON parser knows string boundaries).
_FENCE_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]{0,3})(?P<fence>`{3,})(?P<info>[^`\n]*)$",
    re.MULTILINE,
)
_EXPR_RE = re.compile(r"^\s*(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*==\s*(?P<lit>.+?)\s*$")
_DECODER = json.JSONDecoder()


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


def _top_level_fence_body_starts(text: str) -> Iterator[tuple[str, int]]:
    """Yield (info, body_start) for Markdown fences outside other fences."""
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

        yield info, body_start

        closing_fence_re = re.compile(rf"^[ \t]{{0,3}}`{{{fence_len},}}[ \t]*\r?$", re.MULTILINE)
        closer = closing_fence_re.search(text, body_start)
        if closer is None:
            return
        search_pos = closer.end()


def _skip_json_whitespace(text: str, start: int) -> int:
    """Move start to the first non-whitespace JSON character."""
    while start < len(text) and text[start] in " \t\r\n":
        start += 1
    return start


def _find_body_start(text: str) -> int:
    """Locate where the JSON body begins.

    Prefer the first explicit top-level JSON fence (```json / ```JSON),
    even if an earlier prose/code fence exists. Fence detection is itself
    fence-aware: a literal ````json`` token printed inside an earlier
    non-JSON code block is not treated as the evidence opener. If no
    explicit JSON fence is found, use the first top-level fence. If no
    fence is found, treat the whole input as a bare JSON body — the JSON
    decoder will skip leading whitespace itself.
    """
    first_fence_body_start: int | None = None

    for info, body_start in _top_level_fence_body_starts(text):
        if first_fence_body_start is None:
            first_fence_body_start = body_start
        if info.split(maxsplit=1)[0:1] == ["json"]:
            return _skip_json_whitespace(text, body_start)

    if first_fence_body_start is not None:
        return _skip_json_whitespace(text, first_fence_body_start)
    return 0


def _collect_top_level_objects(text: str) -> list[tuple[int, int, Any]]:
    """Parse all complete JSON objects in *text* and return only top-level ones.

    Returns a list of (start, end, parsed_value) tuples for objects that are
    not contained within any other successfully-parsed JSON value (object or
    array). This uses the JSON parser itself for structural awareness — no
    heuristic character scanning.

    Strategy:
      1. Find every ``{`` and ``[`` in text, attempt ``raw_decode`` from each.
      2. Record all successfully-decoded spans (start, end).
      3. Filter to only objects (dicts) whose span is not contained within
         any other successfully-decoded span (structural top-level check).
    """
    # Collect all successfully-decoded JSON values with their spans
    all_spans: list[tuple[int, int, Any]] = []  # (start, end, value)
    pos = 0
    while pos < len(text):
        ch = text[pos]
        if ch in "{[":
            try:
                parsed, end_offset = _DECODER.raw_decode(text[pos:])
                all_spans.append((pos, pos + end_offset, parsed))
                # Don't skip ahead — we need to also record inner objects
                # to know containment, but we advance by 1 to find them
            except json.JSONDecodeError:
                pass
        pos += 1

    # Filter to only dict values that are not contained within another span
    top_level_objects: list[tuple[int, int, Any]] = []
    for start, end, value in all_spans:
        if not isinstance(value, dict):
            continue
        # Check if this span is strictly contained within any other span
        is_nested = False
        for other_start, other_end, _ in all_spans:
            if other_start == start and other_end == end:
                continue
            if other_start <= start and other_end >= end:
                is_nested = True
                break
        if not is_nested:
            top_level_objects.append((start, end, value))

    return top_level_objects


def _has_json_attempt(text: str) -> bool:
    """Determine if text contains a JSON-like structure (for error classification).

    Returns True when there is evidence of a JSON payload that was attempted
    but malformed — i.e., a JSON/untagged fenced block with content, or a ``{``
    / ``[`` character followed by something that looks like the start of a JSON
    value (not just prose that happens to contain brackets). This is used to
    distinguish "no JSON present at all" from "JSON present but malformed".

    Explicitly non-JSON fences (e.g. ```python) are not considered evidence
    attempts — they are code samples, not malformed evidence.
    """
    # Check for fenced blocks that could be JSON evidence (json-tagged or untagged)
    for info, body_start in _top_level_fence_body_starts(text):
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


def _recover_json_object(text: str, primary: int, primary_exc: json.JSONDecodeError) -> Any:
    """Fallback for outputs whose strict parse failed: structural recovery.

    Uses the JSON parser to identify all complete top-level objects in the
    text (objects not contained within any other JSON value). Selects the
    **last** such object, which is the authoritative terminal evidence record.

    This approach:
      - Never extracts an inner/nested object from a larger JSON structure.
      - Never extracts objects that are elements of a top-level array.
      - Prefers the final evidence record over earlier illustrative objects.

    Raises EvidenceError when no candidate decodes, with accurate diagnostics
    distinguishing "no JSON object present at all" from "JSON present but
    malformed".
    """
    top_level_objects = _collect_top_level_objects(text)

    if top_level_objects:
        # Return the last top-level object (authoritative terminal evidence)
        _, _, parsed = top_level_objects[-1]
        return parsed

    # No top-level objects found — produce accurate error diagnostics
    if not _has_json_attempt(text):
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

    **Resilience**: If the strict fence-based or bare-JSON-from-start
    parse fails, ``_recover_json_object`` scans every ``{`` in the output
    and adopts the first one that decodes. This handles cases where
    smaller models (e.g. adaptive tier) emit prose markers like
    ``[AC_COMPLETE: 6]`` before the evidence JSON. When the strict parse
    *succeeds*, its result is authoritative: a non-object there is an
    error, never a cue to keep scanning (so ``[{...}]`` cannot leak its
    inner object out as evidence).

    Raises EvidenceError on missing / malformed payloads so the harness
    can surface a clear failure instead of silently accepting empty
    results.
    """
    if not text or not text.strip():
        msg = "Leaf output is empty; no evidence record to validate."
        raise EvidenceError(msg)

    primary = _find_body_start(text)
    try:
        parsed, _ = _DECODER.raw_decode(text[primary:])
    except json.JSONDecodeError as exc:
        parsed = _recover_json_object(text, primary, exc)

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
