"""Normalize current execution-event identity and terminal semantics.

The runtime persists provider-local session and tool identifiers.  Public
projections instead need acceptance-criterion identity that survives retries,
plus terminal outcomes that do not turn malformed provider claims into success.
Keeping those rules together prevents steps, artifacts, and verdicts from
quietly choosing different interpretations of the same event.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def canonical_ac_id(data: Mapping[str, Any]) -> str | None:
    """Return the public acceptance-criterion identity for one event.

    Current runtime ``ac_id`` values identify implementation sessions and may
    include execution/retry prefixes.  ``root_ac_index`` is the durable join
    key used by terminal acceptance events, so it wins whenever available.
    Older events without root metadata retain their explicit AC identity.
    """
    root_ac_index = data.get("root_ac_index")
    if (
        isinstance(root_ac_index, int)
        and not isinstance(root_ac_index, bool)
        and root_ac_index >= 0
    ):
        return f"ac_{root_ac_index}"

    ac_id = data.get("ac_id")
    if isinstance(ac_id, str) and ac_id.strip():
        return ac_id.strip()
    return None


def extract_tool_call_id(data: Mapping[str, Any]) -> str | None:
    """Return a supported provider tool-call identifier when one exists."""
    for envelope in _tool_envelopes(data):
        for key in ("call_id", "tool_call_id", "tool_use_id"):
            value = envelope.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def current_attempt_identity(data: Mapping[str, Any]) -> str | None:
    """Return the narrowest durable identity for one current AC attempt.

    Current producers persist both a globally unique session_attempt_id
    and a zero-based retry_attempt. Prefer the former so distinct runtime
    nodes under the same root AC cannot collide; retain the retry index as a
    compatibility fallback for partially populated historical events.
    """
    session_attempt_id = data.get("session_attempt_id")
    if isinstance(session_attempt_id, str) and session_attempt_id.strip():
        return f"session:{session_attempt_id.strip()}"

    retry_attempt = data.get("retry_attempt")
    if (
        isinstance(retry_attempt, int)
        and not isinstance(retry_attempt, bool)
        and retry_attempt >= 0
    ):
        return f"retry:{retry_attempt}"
    return None


def scoped_current_identity(data: Mapping[str, Any], local_identity: str) -> str:
    """Scope a provider/read-model-local identity by public AC and attempt."""
    attempt_scope = scoped_attempt_identity(data)
    return f"{attempt_scope}\0{local_identity}" if attempt_scope else local_identity


def scoped_attempt_identity(data: Mapping[str, Any]) -> str | None:
    """Return canonical public AC plus private attempt scope when available."""
    scopes = tuple(
        value
        for value in (canonical_ac_id(data), current_attempt_identity(data))
        if value is not None
    )
    return "\0".join(scopes) if scopes else None


def scoped_tool_call_identity(data: Mapping[str, Any], call_id: str) -> str:
    """Scope a provider-local call ID to its canonical AC and attempt."""
    return scoped_current_identity(data, call_id)


def resolve_tool_terminal_ok(data: Mapping[str, Any]) -> bool | None:
    """Resolve top-level/nested tool outcomes without laundering bad claims.

    A reported error, an invalid marker, a malformed verdict, or a malformed
    nested result envelope is terminal failure.  Explicit non-error markers are
    success only when every supplied verdict channel agrees.  With no verdict
    signal at all, the projection remains unknown.
    """
    nested = data.get("tool_result")
    if nested is not None and not isinstance(nested, Mapping):
        return False

    envelopes: tuple[Mapping[str, Any], ...] = (
        (data, nested) if isinstance(nested, Mapping) else (data,)
    )
    saw_verdict = False
    for envelope in envelopes:
        if envelope.get("is_error_invalid") is True:
            return False
        if "is_error" not in envelope:
            continue
        saw_verdict = True
        is_error = envelope.get("is_error")
        if not isinstance(is_error, bool) or is_error:
            return False
    return True if saw_verdict else None


def _tool_envelopes(data: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    envelopes: list[Mapping[str, Any]] = [data]
    tool_result = data.get("tool_result")
    if isinstance(tool_result, Mapping):
        envelopes.append(tool_result)
        meta = tool_result.get("meta")
        if isinstance(meta, Mapping):
            envelopes.append(meta)
    return tuple(envelopes)


__all__ = [
    "canonical_ac_id",
    "current_attempt_identity",
    "extract_tool_call_id",
    "resolve_tool_terminal_ok",
    "scoped_attempt_identity",
    "scoped_current_identity",
    "scoped_tool_call_identity",
]
