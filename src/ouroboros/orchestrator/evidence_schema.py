"""Typed evidence record + validator (RFC v2 H2, #830).

Turns the H2 invariant from "the markdown says emit evidence" into a parser-
enforced contract: leaf executors emit a structured evidence record, the
harness validates it against the active ExecutionProfile's evidence_schema
before accepting the result.

This module is pure validator surface — it does not yet wire into
parallel_executor. The H1 verifier loop (next PR in the stack) consumes
the ValidationResult to decide between accept / retry / escalate.

The evaluator for `rejected_if` is intentionally narrow. It supports only
`<field> == <literal>` where literal is parsed first as JSON (so YAML/JSON
authors can write `null`, `true`, `false`, numbers, strings, lists) and
then as a Python literal as a fallback (so legacy `None`/`True`/`False`
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


def extract_evidence(text: str) -> EvidenceRecord:
    """Pull a JSON evidence record out of a leaf executor's raw output.

    Accepts either a bare JSON object or a single ```json``` fenced block.
    Body extraction is delegated to ``json.JSONDecoder.raw_decode`` so
    the parser — not sentinel scanning — decides where the JSON value
    ends. That keeps `}` and ``` inside string values from truncating
    valid payloads.

    Raises EvidenceError on missing / malformed payloads so the harness
    can surface a clear failure instead of silently accepting empty
    results.
    """
    if not text or not text.strip():
        msg = "Leaf output is empty; no evidence record to validate."
        raise EvidenceError(msg)

    start = _find_body_start(text)
    body = text[start:]

    try:
        parsed, _ = _DECODER.raw_decode(body)
    except json.JSONDecodeError as exc:
        msg = f"Evidence is not valid JSON: {exc.msg} (line {exc.lineno}, col {exc.colno})"
        raise EvidenceError(msg) from exc

    if not isinstance(parsed, dict):
        msg = f"Evidence must be a JSON object, got {type(parsed).__name__}"
        raise EvidenceError(msg)

    return EvidenceRecord(data=parsed, source=text)


def _parse_literal(raw: str) -> Any:
    """Safely parse the right-hand side of a `field == literal` expression.

    Profiles are YAML-authored and the evidence is JSON, so the natural
    literal spellings authors will reach for are `null`, `true`, `false`,
    plus numbers / strings / lists. We try JSON first so those work
    out-of-the-box. We fall back to ast.literal_eval so legacy Python
    spellings (`None`, `True`, `False`) keep working too.
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

    Grammar: `<field> == <literal>` only. Anything else raises
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
