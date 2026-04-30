"""Event factories for the M3 I/O Journal (RFC #476).

The I/O Journal records *every* outbound LLM call and tool dispatch as
events on the EventStore, so a past session's behaviour is reconstructable
from the journal alone — without keeping runtime logs around. This is one of
the Tier-1 Must items in the Phase-2 Agent OS RFC and the prerequisite for
:doc:`docs/rfc/contract-ledger`'s ``replay_state`` / ``replay_timeline``
projections being able to answer "why did the evaluator retry?" from the
journal alone.

This module is observational-first, mirroring the stance taken for
``control.directive.emitted`` (#492). It defines four event factories
and the helpers they share, but it adds no emission sites — those are
introduced by per-adapter follow-up PRs (Anthropic / Claude Code /
Codex CLI / Gemini CLI / LiteLLM / OpenCode) and the central MCP tool
dispatch path.

Payload policy locked in the sub-thread on #517 and re-stated here so
this module is the single source of truth:

* ``sha256`` for every ``*_hash`` field — same hashing family as the
  artifact-ref store in :doc:`docs/rfc/disposable-memory`.
* Preview caps: 256 chars default, hard caps 4096 (LLM) / 1024 (tool).
  Truncation marker ``… <truncated len=N>`` is appended *outside* the
  cap so callers can detect truncation without re-hashing.
* ``call_id`` is a ULID — sortable, log-friendly, no extra dependency.
* Privacy switch ``OUROBOROS_IO_JOURNAL_PREVIEWS`` accepts ``on``
  (default), ``off`` (hashes + counts only), and ``redacted``
  (preview replaced with ``<redacted len=N>``).

The module is independent of the LLM and MCP adapters; the adapters
import it and call the factories at the right boundary.
"""

from __future__ import annotations

from enum import StrEnum
import hashlib
import os
import re
import secrets
import time
from typing import Any, Final

from ouroboros.events.base import BaseEvent

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default preview length applied to both LLM and tool I/O. Picked so a
#: typical preview fits one TUI line; callers can drop below this with a
#: per-call override but never above the hard cap.
PREVIEW_DEFAULT_CHARS: Final[int] = 256

#: Hard cap on LLM-side previews. Stops a misconfigured caller from
#: journaling megabytes of completion text.
PREVIEW_HARD_CAP_CHARS_LLM: Final[int] = 4096

#: Hard cap on tool-side previews. Tool args/results are typically
#: smaller than LLM payloads; a tighter cap keeps tool I/O readable.
PREVIEW_HARD_CAP_CHARS_TOOL: Final[int] = 1024

#: Truncation marker appended *outside* the cap so consumers can detect
#: truncation without re-hashing. Format: ``… <truncated len=N>``.
TRUNCATION_MARKER_TEMPLATE: Final[str] = "… <truncated len={length}>"

#: Redaction marker emitted when the privacy switch is ``redacted``.
REDACTION_MARKER_TEMPLATE: Final[str] = "<redacted len={length}>"

#: Environment variable that selects the privacy mode at process start.
PRIVACY_ENV_VAR: Final[str] = "OUROBOROS_IO_JOURNAL_PREVIEWS"

#: Crockford base32 alphabet used by ULID. Excluded letters: I L O U.
_ULID_ALPHABET: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


class PrivacyMode(StrEnum):
    """Operator-selected privacy mode for the I/O Journal.

    ``ON`` keeps previews populated (the local-first cooperative-trust
    default). ``OFF`` strips previews entirely; only hashes and counts
    survive. ``REDACTED`` keeps the field shape but replaces the value
    with the ``<redacted len=N>`` marker so projections see *that* a
    payload existed without seeing it.
    """

    ON = "on"
    OFF = "off"
    REDACTED = "redacted"


def get_privacy_mode() -> PrivacyMode:
    """Resolve the active privacy mode from the environment.

    Unknown values fall back to :attr:`PrivacyMode.ON`. The env var is
    read on every call so tests can flip it without restarting the
    process; this is fine because the read is `os.environ.get`-cheap.
    """
    raw = os.environ.get(PRIVACY_ENV_VAR, PrivacyMode.ON.value).lower()
    try:
        return PrivacyMode(raw)
    except ValueError:
        return PrivacyMode.ON


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def content_hash(payload: bytes | str) -> str:
    """Return a ``sha256:<hex>`` hash of *payload* using the journal family.

    The same hashing family is used for ``artifact_ref`` in
    ``docs/rfc/disposable-memory.md`` so projections can correlate the
    journal with the artifact store without a second hashing scheme.
    """
    if isinstance(payload, str):
        encoded = payload.encode("utf-8")
    else:
        encoded = payload
    digest = hashlib.sha256(encoded).hexdigest()
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# Preview shaping
# ---------------------------------------------------------------------------


def truncate_preview(
    text: str,
    cap: int = PREVIEW_DEFAULT_CHARS,
    *,
    hard_cap: int = PREVIEW_HARD_CAP_CHARS_LLM,
) -> str:
    """Cap *text* at min(cap, hard_cap) chars + append a truncation marker.

    The marker is written *outside* the cap so the truncated body itself
    never exceeds ``min(cap, hard_cap)``; downstream projectors that
    only render the cap-prefix stay readable. When the input fits inside
    the cap the marker is omitted.
    """
    effective_cap = min(cap, hard_cap)
    if effective_cap <= 0:
        return ""
    if len(text) <= effective_cap:
        return text
    truncated_len = len(text) - effective_cap
    return text[:effective_cap] + TRUNCATION_MARKER_TEMPLATE.format(length=truncated_len)


def shape_preview(
    text: str | None,
    *,
    cap: int = PREVIEW_DEFAULT_CHARS,
    hard_cap: int = PREVIEW_HARD_CAP_CHARS_LLM,
    privacy: PrivacyMode | None = None,
) -> str | None:
    """Apply the privacy switch + truncation to *text*.

    Returns ``None`` when the source is ``None`` (preserves the
    "nothing to record" signal) and when the privacy mode is ``OFF``
    (preview field is omitted entirely from the payload). Returns a
    ``<redacted len=N>`` marker when the mode is ``REDACTED``.
    """
    if text is None:
        return None
    mode = privacy if privacy is not None else get_privacy_mode()
    if mode is PrivacyMode.OFF:
        return None
    if mode is PrivacyMode.REDACTED:
        if text.startswith("<redacted len=") and text.endswith(">"):
            return text
        return REDACTION_MARKER_TEMPLATE.format(length=len(text))
    return truncate_preview(text, cap, hard_cap=hard_cap)


# ---------------------------------------------------------------------------
# Call IDs (ULID)
# ---------------------------------------------------------------------------


def new_call_id() -> str:
    """Return a fresh ULID-style ``call_id``.

    Format: 26 chars from the Crockford base32 alphabet. The first 10
    chars encode a 48-bit millisecond timestamp; the remaining 16 chars
    are 80 bits of cryptographic randomness. Sortable lexicographically
    and stringifies to a fixed width — easier to grep and tail than
    UUIDv4. Implementation is dependency-free so the journal does not
    pull in a new third-party package.
    """
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    randomness = secrets.randbits(80)
    raw = (timestamp_ms << 80) | randomness
    chars = []
    for _ in range(26):
        chars.append(_ULID_ALPHABET[raw & 0x1F])
        raw >>= 5
    return "".join(reversed(chars))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_io_event_data(
    *,
    target_type: str,
    target_id: str,
    correlation: dict[str, Any],
    fields: dict[str, Any],
) -> dict[str, Any]:
    """Compose the persisted ``data`` payload for an I/O Journal event.

    Optional correlation fields (``session_id``, ``execution_id``,
    ``lineage_id``, ``generation_number``, ``phase``, ``call_id``)
    appear in the payload only when provided; ``None`` means "absent",
    so stored rows stay compact and projections can distinguish
    "missing" from "explicit None". Mirrors the policy used in
    ``events/control.py``.
    """
    payload: dict[str, Any] = {
        "target_type": target_type,
        "target_id": target_id,
    }
    for key, value in correlation.items():
        if value is not None:
            payload[key] = value
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    return payload


def _validate_target(target_type: str, target_id: str) -> None:
    if not target_type:
        raise ValueError("I/O Journal events require a non-empty target_type.")
    if not target_id:
        raise ValueError("I/O Journal events require a non-empty target_id.")


def _validate_call_id(call_id: str) -> None:
    if not _ULID_RE.fullmatch(call_id):
        raise ValueError("I/O Journal events require call_id to be a 26-character ULID.")


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def create_tool_call_started_event(
    *,
    target_type: str,
    target_id: str,
    call_id: str,
    tool_name: str,
    args_hash: str,
    args_preview: str | None = None,
    preview_cap: int = PREVIEW_DEFAULT_CHARS,
    preview_hard_cap: int = PREVIEW_HARD_CAP_CHARS_TOOL,
    privacy: PrivacyMode | None = None,
    caller: str | None = None,
    mcp_server: str | None = None,
    session_id: str | None = None,
    execution_id: str | None = None,
    lineage_id: str | None = None,
    generation_number: int | None = None,
    phase: str | None = None,
    extra: dict[str, Any] | None = None,
) -> BaseEvent:
    """Record the *start* of a tool dispatch.

    The factory does not compute the hash itself — callers are expected
    to pass a pre-hashed value so identical args across runs collapse
    to the same hash without repeatedly hashing per-call.
    """
    _validate_target(target_type, target_id)
    _validate_call_id(call_id)
    args_preview = shape_preview(
        args_preview,
        cap=preview_cap,
        hard_cap=preview_hard_cap,
        privacy=privacy,
    )
    data = _build_io_event_data(
        target_type=target_type,
        target_id=target_id,
        correlation={
            "session_id": session_id,
            "execution_id": execution_id,
            "lineage_id": lineage_id,
            "generation_number": generation_number,
            "phase": phase,
        },
        fields={
            "call_id": call_id,
            "tool_name": tool_name,
            "args_hash": args_hash,
            "args_preview": args_preview,
            "caller": caller,
            "mcp_server": mcp_server,
            "extra": dict(extra) if extra else None,
        },
    )
    return BaseEvent(
        type="tool.call.started",
        aggregate_type=target_type,
        aggregate_id=target_id,
        data=data,
    )


def create_tool_call_returned_event(
    *,
    target_type: str,
    target_id: str,
    call_id: str,
    tool_name: str,
    duration_ms: int,
    is_error: bool,
    result_hash: str | None = None,
    result_preview: str | None = None,
    preview_cap: int = PREVIEW_DEFAULT_CHARS,
    preview_hard_cap: int = PREVIEW_HARD_CAP_CHARS_TOOL,
    privacy: PrivacyMode | None = None,
    error_kind: str | None = None,
    session_id: str | None = None,
    execution_id: str | None = None,
    lineage_id: str | None = None,
    generation_number: int | None = None,
    phase: str | None = None,
    extra: dict[str, Any] | None = None,
) -> BaseEvent:
    """Record the *completion* of a tool dispatch (paired by ``call_id``)."""
    _validate_target(target_type, target_id)
    _validate_call_id(call_id)
    result_preview = shape_preview(
        result_preview,
        cap=preview_cap,
        hard_cap=preview_hard_cap,
        privacy=privacy,
    )
    data = _build_io_event_data(
        target_type=target_type,
        target_id=target_id,
        correlation={
            "session_id": session_id,
            "execution_id": execution_id,
            "lineage_id": lineage_id,
            "generation_number": generation_number,
            "phase": phase,
        },
        fields={
            "call_id": call_id,
            "tool_name": tool_name,
            "duration_ms": duration_ms,
            "is_error": is_error,
            "result_hash": result_hash,
            "result_preview": result_preview,
            "error_kind": error_kind,
            "extra": dict(extra) if extra else None,
        },
    )
    return BaseEvent(
        type="tool.call.returned",
        aggregate_type=target_type,
        aggregate_id=target_id,
        data=data,
    )


def create_llm_call_requested_event(
    *,
    target_type: str,
    target_id: str,
    call_id: str,
    model_id: str,
    prompt_hash: str,
    caller: str | None = None,
    prompt_preview: str | None = None,
    preview_cap: int = PREVIEW_DEFAULT_CHARS,
    preview_hard_cap: int = PREVIEW_HARD_CAP_CHARS_LLM,
    privacy: PrivacyMode | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    tool_choice: str | None = None,
    session_id: str | None = None,
    execution_id: str | None = None,
    lineage_id: str | None = None,
    generation_number: int | None = None,
    phase: str | None = None,
    extra: dict[str, Any] | None = None,
) -> BaseEvent:
    """Record the *start* of an outbound LLM call."""
    _validate_target(target_type, target_id)
    _validate_call_id(call_id)
    prompt_preview = shape_preview(
        prompt_preview,
        cap=preview_cap,
        hard_cap=preview_hard_cap,
        privacy=privacy,
    )
    data = _build_io_event_data(
        target_type=target_type,
        target_id=target_id,
        correlation={
            "session_id": session_id,
            "execution_id": execution_id,
            "lineage_id": lineage_id,
            "generation_number": generation_number,
            "phase": phase,
        },
        fields={
            "call_id": call_id,
            "model_id": model_id,
            "prompt_hash": prompt_hash,
            "prompt_preview": prompt_preview,
            "caller": caller,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "tool_choice": tool_choice,
            "extra": dict(extra) if extra else None,
        },
    )
    return BaseEvent(
        type="llm.call.requested",
        aggregate_type=target_type,
        aggregate_id=target_id,
        data=data,
    )


def create_llm_call_returned_event(
    *,
    target_type: str,
    target_id: str,
    call_id: str,
    model_id: str,
    prompt_hash: str,
    duration_ms: int,
    is_error: bool,
    completion_preview: str | None = None,
    preview_cap: int = PREVIEW_DEFAULT_CHARS,
    preview_hard_cap: int = PREVIEW_HARD_CAP_CHARS_LLM,
    privacy: PrivacyMode | None = None,
    completion_hash: str | None = None,
    finish_reason: str | None = None,
    token_count_in: int | None = None,
    token_count_out: int | None = None,
    error_kind: str | None = None,
    session_id: str | None = None,
    execution_id: str | None = None,
    lineage_id: str | None = None,
    generation_number: int | None = None,
    phase: str | None = None,
    extra: dict[str, Any] | None = None,
) -> BaseEvent:
    """Record the *completion* of an outbound LLM call (paired by ``call_id``).

    ``finish_reason`` is intentionally an opaque string — provider
    vocabularies (``stop``, ``length``, ``tool_calls``, ``content_filter``,
    …) drift, so this module does not normalise across providers. A
    later projector PR can normalise without changing the event schema.
    """
    _validate_target(target_type, target_id)
    _validate_call_id(call_id)
    completion_preview = shape_preview(
        completion_preview,
        cap=preview_cap,
        hard_cap=preview_hard_cap,
        privacy=privacy,
    )
    data = _build_io_event_data(
        target_type=target_type,
        target_id=target_id,
        correlation={
            "session_id": session_id,
            "execution_id": execution_id,
            "lineage_id": lineage_id,
            "generation_number": generation_number,
            "phase": phase,
        },
        fields={
            "call_id": call_id,
            "model_id": model_id,
            "prompt_hash": prompt_hash,
            "completion_preview": completion_preview,
            "completion_hash": completion_hash,
            "duration_ms": duration_ms,
            "is_error": is_error,
            "finish_reason": finish_reason,
            "token_count_in": token_count_in,
            "token_count_out": token_count_out,
            "error_kind": error_kind,
            "extra": dict(extra) if extra else None,
        },
    )
    return BaseEvent(
        type="llm.call.returned",
        aggregate_type=target_type,
        aggregate_id=target_id,
        data=data,
    )
