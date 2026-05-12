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
keep working). Any other expression shape raises EvidenceError so that
profile authors get an immediate, loud failure instead of silent acceptance.

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
from dataclasses import dataclass, field
import json
import re
from typing import Any

from ouroboros.orchestrator.profile_loader import ExecutionProfile

# Fence delimiters for ```json ... ``` evidence blocks. Leaf prompts in
# later PRs instruct executors to emit evidence inside one of these.
# We deliberately do NOT use a single regex to extract the JSON body:
# a non-greedy `{.*?}` stops at the first `}` even inside a quoted
# string, so any valid evidence payload that contains "}" in a string
# value would be truncated and rejected (bot finding on PR #883).
# Instead we slice between fences and let json.loads handle string
# escaping correctly.
_FENCE_OPENERS: tuple[str, ...] = ("```json", "```JSON", "```")
_FENCE_CLOSER: str = "```"
_EXPR_RE = re.compile(r"^\s*(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s*==\s*(?P<lit>.+?)\s*$")


class EvidenceError(ValueError):
    """Raised when evidence cannot be parsed or a profile expression is invalid."""


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of validating an evidence record against a profile.

    Attributes:
        ok: True iff no required field is missing and no rejected_if matched.
        missing_fields: Required fields the record did not provide.
        rejected_by: rejected_if expressions that evaluated True against
            the record (verbatim, in profile order).
    """

    ok: bool
    missing_fields: tuple[str, ...] = ()
    rejected_by: tuple[str, ...] = ()

    def reasons(self) -> tuple[str, ...]:
        """Human-readable, harness-friendly summary of all failure reasons."""
        out: list[str] = []
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


def _extract_fenced_payload(text: str) -> str | None:
    """Return the body of the first ```json ... ``` (or bare ```) fence.

    The slice respects fence markers — not braces inside JSON strings —
    so a payload like `{"note": "hello } more"}` survives extraction.
    Returns None when no fence is found.
    """
    best_open = -1
    best_open_len = 0
    for opener in _FENCE_OPENERS:
        idx = text.find(opener)
        if idx == -1:
            continue
        if best_open == -1 or idx < best_open:
            best_open = idx
            best_open_len = len(opener)
    if best_open == -1:
        return None

    body_start = best_open + best_open_len
    close_idx = text.find(_FENCE_CLOSER, body_start)
    if close_idx == -1:
        return None
    return text[body_start:close_idx].strip()


def extract_evidence(text: str) -> EvidenceRecord:
    """Pull a JSON evidence record out of a leaf executor's raw output.

    Accepts either a bare JSON object or a single ```json``` fenced block.
    Raises EvidenceError on missing / malformed payloads so the harness can
    surface a clear failure instead of silently accepting empty results.
    """
    if not text or not text.strip():
        msg = "Leaf output is empty; no evidence record to validate."
        raise EvidenceError(msg)

    payload = _extract_fenced_payload(text)
    if payload is None:
        payload = text.strip()

    try:
        parsed = json.loads(payload)
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
        raise EvidenceError(msg) from exc


def _evaluate_rejection(expr: str, data: dict[str, Any]) -> bool:
    """Evaluate a single rejected_if expression.

    Grammar: `<field> == <literal>` only. Anything else raises EvidenceError
    so profile authors notice immediately instead of silently passing.
    """
    match = _EXPR_RE.match(expr)
    if not match:
        msg = (
            f"Unsupported rejected_if expression: {expr!r}. "
            "Only '<field> == <literal>' is currently supported."
        )
        raise EvidenceError(msg)
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
        EvidenceError: If any rejected_if expression has unsupported syntax.
            (Profile bugs should be loud, not silent.)
    """
    schema = profile.evidence_schema

    missing = tuple(name for name in schema.required if name not in record.data)
    rejected = tuple(expr for expr in schema.rejected_if if _evaluate_rejection(expr, record.data))

    return ValidationResult(
        ok=not missing and not rejected,
        missing_fields=missing,
        rejected_by=rejected,
    )


__all__ = [
    "EvidenceError",
    "EvidenceRecord",
    "ValidationResult",
    "extract_evidence",
    "validate_evidence",
]
