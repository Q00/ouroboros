"""Normalize Claude CLI JSON output across supported wire shapes.

Claude Code has emitted both a single ``result`` object and event streams from
``-p`` mode.  Depending on the CLI version and wrapper, the stream can arrive
as NDJSON or as one top-level JSON array.  Consumers must not guess which shape
they received and then call mapping methods on a list.

This module deliberately recognizes only a terminal Claude ``result``
envelope.  Assistant text on its own is not completion evidence: accepting it
would turn a truncated stream into a successful request.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import json
import math
from typing import Any

# ``communicate()`` has already materialized stdout when this parser runs, but
# the cap still prevents a hostile response from causing another large decode,
# event list, and raw-response copy.  Legitimate Claude result envelopes are
# several orders of magnitude smaller than this.
MAX_CLAUDE_CLI_OUTPUT_CHARS = 8 * 1024 * 1024
MAX_CLAUDE_CLI_EVENTS = 4096
_MAX_SESSION_ID_CHARS = 4096
_MAX_TOKEN_COUNT = (1 << 63) - 1
# Keep this trust-boundary schema aligned with the worker telemetry consumers
# in ``orchestrator.adapter`` and ``orchestrator.frugality_evidence``.  Aliases
# ending in ``_tokens`` are validated by the same contract below even when a
# current consumer does not attribute them.
_TOKEN_USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "total_tokens",
    }
)
_TOKEN_SPEND_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
_PRIMARY_TOKEN_KEYS = frozenset({"input_tokens", "output_tokens"})
_TOKEN_KEY_ALIASES = {
    "prompt_tokens": "input_tokens",
    "completion_tokens": "output_tokens",
}
# Stay well below CPython's configurable 4,300-digit default so hostile JSON
# cannot escape this boundary as an interpreter-level ``ValueError``.
_MAX_JSON_INTEGER_DIGITS = 1024


class ClaudeCliOutputError(ValueError):
    """The CLI output did not contain one trustworthy terminal result."""


@dataclass(frozen=True, slots=True)
class ClaudeCliResult:
    """Provider-neutral view of a terminal Claude CLI result envelope."""

    result: str
    is_error: bool
    session_id: str | None
    usage: dict[str, Any] | None
    subtype: str | None
    stop_reason: str | None
    raw_payload: dict[str, Any]
    event_count: int


def normalize_claude_cli_output(stdout: str) -> ClaudeCliResult:
    """Return the single terminal result from object, array, or NDJSON output.

    Non-JSON diagnostic lines may surround an NDJSON stream.  A malformed line
    that *looks* like JSON is rejected instead of skipped, because otherwise a
    truncated or corrupted event could silently change the meaning of the
    terminal result.  Duplicate keys, non-finite numbers, multiple results,
    events after a result, inconsistent session ids, and malformed field types
    are rejected for the same fail-closed reason.
    """
    if not isinstance(stdout, str):
        raise ClaudeCliOutputError("stdout must be text")
    if len(stdout) > MAX_CLAUDE_CLI_OUTPUT_CHARS:
        raise ClaudeCliOutputError(
            f"stdout exceeds {MAX_CLAUDE_CLI_OUTPUT_CHARS} character safety limit"
        )

    stripped = stdout.strip()
    if not stripped:
        raise ClaudeCliOutputError("stdout is empty")

    documents = _decode_documents(stripped)
    events = _flatten_events(documents)
    final = _terminal_result(events)
    is_error = _optional_bool(final, "is_error", default=False)
    session_id = _consistent_session_id(events)
    usage = _result_usage(events, final)
    result_text = _result_text(final, is_error=is_error)
    subtype = _optional_string(final, "subtype")
    stop_reason = _optional_string(final, "stop_reason")

    raw_payload = dict(final)
    raw_payload["result"] = result_text
    raw_payload["is_error"] = is_error
    if session_id is not None:
        raw_payload["session_id"] = session_id
    if usage is not None:
        raw_payload["usage"] = dict(usage)

    return ClaudeCliResult(
        result=result_text,
        is_error=is_error,
        session_id=session_id,
        usage=usage,
        subtype=subtype,
        stop_reason=stop_reason,
        raw_payload=raw_payload,
        event_count=len(events),
    )


def _decode_documents(stdout: str) -> list[Any]:
    """Decode complete JSON documents amid line-oriented diagnostics.

    ``raw_decode`` matters here: unlike splitting lines, it supports pretty
    printed arrays/objects while still supporting multiple NDJSON documents.
    Diagnostics are ignored only when they start their own line.  Garbage
    appended to a JSON document on the same line is protocol corruption.
    """
    decoder = json.JSONDecoder(
        parse_constant=_reject_constant,
        parse_float=_finite_json_float,
        parse_int=_bounded_json_int,
        object_pairs_hook=_unique_object,
    )
    documents: list[Any] = []
    position = 0
    previous_document_end: int | None = None
    while position < len(stdout):
        while position < len(stdout) and stdout[position].isspace():
            position += 1
        if position >= len(stdout):
            break

        if stdout[position] not in "{[":
            line_start = stdout.rfind("\n", 0, position) + 1
            if stdout[line_start:position].strip():
                raise ClaudeCliOutputError("unexpected trailing data after JSON document")
            newline = stdout.find("\n", position)
            position = len(stdout) if newline < 0 else newline + 1
            continue

        if previous_document_end is not None:
            separator = stdout[previous_document_end:position]
            if "\n" not in separator and "\r" not in separator:
                raise ClaudeCliOutputError("multiple JSON documents must have a newline boundary")

        line_number = stdout.count("\n", 0, position) + 1
        try:
            document, position = decoder.raw_decode(stdout, position)
            documents.append(document)
            previous_document_end = position
        except json.JSONDecodeError as exc:
            raise ClaudeCliOutputError(
                f"malformed JSON-looking stdout line {line_number}: {exc.msg}"
            ) from exc
        except RecursionError as exc:
            raise ClaudeCliOutputError("JSON nesting exceeds the parser safety limit") from exc
    if not documents:
        raise ClaudeCliOutputError("stdout contains no JSON document")
    return documents


def _reject_constant(token: str) -> None:
    raise ClaudeCliOutputError(f"non-finite JSON number is not allowed: {token}")


def _bounded_json_int(token: str) -> int:
    digits = token[1:] if token.startswith("-") else token
    if len(digits) > _MAX_JSON_INTEGER_DIGITS:
        raise ClaudeCliOutputError(
            f"JSON integer exceeds {_MAX_JSON_INTEGER_DIGITS} digit safety limit"
        )
    try:
        return int(token)
    except ValueError as exc:  # pragma: no cover - decoder supplies JSON integer syntax
        raise ClaudeCliOutputError("invalid JSON integer") from exc


def _finite_json_float(token: str) -> float:
    try:
        value = float(token)
    except (OverflowError, ValueError) as exc:  # pragma: no cover - decoder validates syntax
        raise ClaudeCliOutputError("invalid JSON number") from exc
    if not math.isfinite(value):
        raise ClaudeCliOutputError(f"non-finite JSON number is not allowed: {token}")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ClaudeCliOutputError(f"duplicate JSON key is not allowed: {key}")
        result[key] = value
    return result


def _flatten_events(documents: Sequence[Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for document in documents:
        if isinstance(document, Mapping):
            candidates: Sequence[Any] = (document,)
        elif isinstance(document, list):
            candidates = document
        else:
            raise ClaudeCliOutputError("top-level JSON document must be an object or event array")

        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                raise ClaudeCliOutputError("every Claude event must be a JSON object")
            events.append(dict(candidate))
            if len(events) > MAX_CLAUDE_CLI_EVENTS:
                raise ClaudeCliOutputError(
                    f"event count exceeds {MAX_CLAUDE_CLI_EVENTS} safety limit"
                )
    if not events:
        raise ClaudeCliOutputError("Claude event array is empty")
    return events


def _terminal_result(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    result_indexes: list[int] = []
    activity_seen = False
    init_seen = False

    for index, event in enumerate(events):
        event_type = event.get("type")
        if event_type is not None and not isinstance(event_type, str):
            raise ClaudeCliOutputError(f"event {index} has a non-string type")
        # Older ``--output-format json`` envelopes were untyped.  ``is_error``
        # is the discriminator when such an envelope omits an empty ``result``
        # key; an arbitrary object is still not completion evidence.
        is_result = event_type == "result" or (
            event_type is None and ("result" in event or "is_error" in event)
        )
        if is_result:
            result_indexes.append(index)
            continue

        is_init = event_type == "system" and event.get("subtype") == "init"
        if is_init:
            if init_seen or activity_seen:
                raise ClaudeCliOutputError("system/init event is duplicated or out of order")
            init_seen = True
        else:
            activity_seen = True

    if not result_indexes:
        raise ClaudeCliOutputError("no terminal Claude result event found")
    if len(result_indexes) != 1:
        raise ClaudeCliOutputError("multiple Claude result events found")
    if result_indexes[0] != len(events) - 1:
        raise ClaudeCliOutputError("Claude result event is not terminal")
    return events[result_indexes[0]]


def _consistent_session_id(events: Sequence[dict[str, Any]]) -> str | None:
    observed: set[str] = set()
    for index, event in enumerate(events):
        value = event.get("session_id")
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ClaudeCliOutputError(f"event {index} has a non-string session_id")
        if len(value) > _MAX_SESSION_ID_CHARS:
            raise ClaudeCliOutputError("session_id exceeds the safety limit")
        observed.add(value)
    if len(observed) > 1:
        raise ClaudeCliOutputError("Claude events contain inconsistent session_id values")
    return next(iter(observed), None)


def _result_usage(
    events: Sequence[dict[str, Any]], final: Mapping[str, Any]
) -> dict[str, Any] | None:
    raw_usage = final.get("usage")
    if raw_usage is not None:
        # Claude's terminal result usage is the request-wide total.  It is
        # authoritative when present, so adding assistant-event usage here
        # would count the same work twice.
        return _validated_usage(raw_usage)

    turn_usages: list[dict[str, Any]] = []
    for index, event in enumerate(events[:-1]):
        top_level = event.get("usage")
        message = event.get("message")
        nested = message.get("usage") if isinstance(message, Mapping) else None
        if top_level is not None and nested is not None and top_level != nested:
            raise ClaudeCliOutputError(
                f"assistant event {index} has conflicting top-level and nested usage"
            )
        event_usage = top_level if top_level is not None else nested
        if event_usage is None:
            continue
        if event.get("type") != "assistant":
            raise ClaudeCliOutputError(f"non-assistant event {index} cannot supply fallback usage")
        turn_usages.append(_validated_usage(event_usage))

    if not turn_usages:
        return None
    if len(turn_usages) == 1:
        return turn_usages[0]
    return _aggregate_turn_usages(turn_usages)


def _validated_usage(raw_usage: Any) -> dict[str, Any]:
    """Return one canonical, internally reconciled usage authority.

    Claude wrappers occasionally rename the primary counters, but every
    consumer needs the same names and the same total.  Canonicalization happens
    here, before the worker and provider adapter diverge.  A total-only payload
    remains total-only: no prompt/completion allocation can be inferred from a
    scalar total.
    """
    if not isinstance(raw_usage, Mapping):
        raise ClaudeCliOutputError("usage must be a JSON object")

    usage = dict(raw_usage)
    for key, value in usage.items():
        if key not in _TOKEN_USAGE_KEYS and not key.endswith("_tokens"):
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= _MAX_TOKEN_COUNT
        ):
            raise ClaudeCliOutputError(f"usage.{key} must be a bounded non-negative integer")

    for alias, canonical in _TOKEN_KEY_ALIASES.items():
        if alias not in usage:
            continue
        if canonical in usage and usage[canonical] != usage[alias]:
            raise ClaudeCliOutputError(f"usage.{alias} disagrees with canonical usage.{canonical}")
        usage[canonical] = usage[alias]
        del usage[alias]

    unsupported_token_keys = {
        key for key in usage if key.endswith("_tokens") and key not in _TOKEN_USAGE_KEYS
    }
    if unsupported_token_keys:
        keys = ", ".join(sorted(unsupported_token_keys))
        raise ClaudeCliOutputError(f"usage has unsupported token counters: {keys}")

    fallback_keys = {key for key in _TOKEN_SPEND_KEYS if key in usage}
    has_components = _PRIMARY_TOKEN_KEYS.issubset(fallback_keys)
    total = usage.get("total_tokens")
    if total is not None:
        if fallback_keys and not has_components:
            raise ClaudeCliOutputError("usage with total_tokens has incomplete additive counters")
        if has_components:
            component_total = _bounded_usage_sum(
                (usage[key] for key in _TOKEN_SPEND_KEYS if key in usage),
                key="additive token counters",
            )
            if total != component_total:
                raise ClaudeCliOutputError(
                    "usage.total_tokens disagrees with additive token counters"
                )
        return usage

    if not has_components:
        raise ClaudeCliOutputError(
            "usage must include total_tokens or both input_tokens and output_tokens"
        )
    usage["total_tokens"] = _bounded_usage_sum(
        (usage[key] for key in _TOKEN_SPEND_KEYS if key in usage),
        key="total_tokens",
    )
    return usage


def _aggregate_turn_usages(turn_usages: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate event-local assistant usage into one request-wide usage object.

    Each assistant event is one billed model turn.  ``total_tokens`` is
    authoritative for that turn when supplied; otherwise the two primary
    counters plus Anthropic's additive cache counters determine its spend.
    Partial event telemetry is rejected because summing only the fields that
    happen to be present could manufacture a smaller request total.
    """
    all_turns_have_components = True
    for usage in turn_usages:
        fallback_keys = {key for key in _TOKEN_SPEND_KEYS if key in usage}
        has_components = _PRIMARY_TOKEN_KEYS.issubset(fallback_keys)
        all_turns_have_components = all_turns_have_components and has_components

    token_keys = {
        key
        for usage in turn_usages
        for key in usage
        if key in _TOKEN_USAGE_KEYS or key.endswith("_tokens")
    }
    aggregated = {
        key: _bounded_usage_sum(
            (usage[key] for usage in turn_usages if key in usage),
            key=key,
        )
        for key in token_keys
    }

    if not all_turns_have_components:
        # Some turns expose only their authoritative total, so a complete
        # per-counter breakdown cannot be reconstructed.  Preserve the exact
        # request spend as a total and omit partial additive components.
        for key in _TOKEN_SPEND_KEYS:
            aggregated.pop(key, None)

    # Preserve stable non-token metadata (for example ``service_tier``) only
    # when every turn reports the same value.  Selecting one turn's metadata
    # would be the same last-event attribution bug as selecting one usage.
    common_metadata = set.intersection(
        *(
            {key for key in usage if key not in _TOKEN_USAGE_KEYS and not key.endswith("_tokens")}
            for usage in turn_usages
        )
    )
    for key in common_metadata:
        first = turn_usages[0][key]
        if all(usage[key] == first for usage in turn_usages[1:]):
            aggregated[key] = first
    return aggregated


def _bounded_usage_sum(values: Iterable[int], *, key: str) -> int:
    """Add already validated token counters without crossing the wire bound."""
    total = 0
    for value in values:
        if value > _MAX_TOKEN_COUNT - total:
            raise ClaudeCliOutputError(
                f"aggregated usage.{key} exceeds the bounded token-count limit"
            )
        total += value
    return total


def _result_text(final: Mapping[str, Any], *, is_error: bool) -> str:
    if "result" in final:
        value = final["result"]
        if value is None and is_error:
            return ""
    else:
        # Some stream wrappers retain the terminal text in the same nested
        # message/content shape used by assistant events.
        message = final.get("message")
        if isinstance(message, Mapping) and "content" in message:
            value = message["content"]
        elif is_error:
            # Typed and legacy untyped error envelopes use omission and
            # ``result: null`` interchangeably when no human-readable detail is
            # available.  Metadata remains authoritative completion evidence.
            return ""
        else:
            raise ClaudeCliOutputError("terminal result event has no result content")

    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if "content" not in value:
            raise ClaudeCliOutputError("result object has no content field")
        return _content_blocks_text(value["content"])
    if isinstance(value, list):
        return _content_blocks_text(value)
    raise ClaudeCliOutputError("result content must be text or recognized content blocks")


def _content_blocks_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ClaudeCliOutputError("nested result content must be text or a content-block array")

    text_parts: list[str] = []
    for block in value:
        if not isinstance(block, Mapping):
            raise ClaudeCliOutputError("result content blocks must be JSON objects")
        block_type = block.get("type")
        text_value = block.get("text")
        if block_type != "text" or not isinstance(text_value, str):
            raise ClaudeCliOutputError("result content contains a non-text block")
        text_parts.append(text_value)
    return "".join(text_parts)


def _optional_bool(payload: Mapping[str, Any], key: str, *, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ClaudeCliOutputError(f"{key} must be a boolean")
    return value


def _optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ClaudeCliOutputError(f"{key} must be a string")
    return value
