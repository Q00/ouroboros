"""Shared redaction for harness-owned verifier values."""

from __future__ import annotations

from collections.abc import Iterable
import json
import shlex


def hidden_contract_variants(values: Iterable[str | None]) -> tuple[str, ...]:
    """Return longest-first raw, quoted, and escaped hidden values."""

    variants: set[str] = set()
    for hidden in values:
        if not hidden:
            continue
        json_quoted = json.dumps(hidden, ensure_ascii=False)
        variants.update(
            {
                hidden,
                repr(hidden),
                shlex.quote(hidden),
                json_quoted,
                json_quoted[1:-1],
            }
        )
    return tuple(
        sorted((value for value in variants if value), key=lambda value: (-len(value), value))
    )


def redact_hidden_contract_values(
    text: str,
    values: Iterable[str | None],
    *,
    replacement: str = "[REDACTED CONTRACT VALUE]",
) -> str:
    """Remove every supported encoding of harness-owned values from text."""

    redacted = text
    for hidden in hidden_contract_variants(values):
        redacted = redacted.replace(hidden, replacement)
    return redacted
