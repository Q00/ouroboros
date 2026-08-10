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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from typing import Any

# ``communicate()`` has already materialized stdout when this parser runs, but
# the cap still prevents a hostile response from causing another large decode,
# event list, and raw-response copy.  Legitimate Claude result envelopes are
# several orders of magnitude smaller than this.
MAX_CLAUDE_CLI_OUTPUT_CHARS = 8 * 1024 * 1024
MAX_CLAUDE_CLI_EVENTS = 4096
_MAX_SESSION_ID_CHARS = 4096
_MAX_TOKEN_COUNT = (1 << 63) - 1


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
    session_id = _consistent_session_id(events)
    usage = _result_usage(events, final)
    result_text = _result_text(final)
    is_error = _optional_bool(final, "is_error", default=False)
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
        object_pairs_hook=_unique_object,
    )
    documents: list[Any] = []
    position = 0
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

        line_number = stdout.count("\n", 0, position) + 1
        try:
            document, position = decoder.raw_decode(stdout, position)
            documents.append(document)
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
    if raw_usage is None:
        for event in reversed(events[:-1]):
            raw_usage = event.get("usage")
            message = event.get("message")
            if raw_usage is None and isinstance(message, Mapping):
                raw_usage = message.get("usage")
            if raw_usage is not None:
                break
    if raw_usage is None:
        return None
    if not isinstance(raw_usage, Mapping):
        raise ClaudeCliOutputError("usage must be a JSON object")

    usage = dict(raw_usage)
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= _MAX_TOKEN_COUNT
        ):
            raise ClaudeCliOutputError(f"usage.{key} must be a bounded non-negative integer")
    return usage


def _result_text(final: Mapping[str, Any]) -> str:
    if "result" in final:
        value = final["result"]
    else:
        # Some stream wrappers retain the terminal text in the same nested
        # message/content shape used by assistant events.
        message = final.get("message")
        if isinstance(message, Mapping) and "content" in message:
            value = message["content"]
        elif final.get("type") is None and "is_error" in final:
            # Legacy untyped result envelopes used omission and ``result: null``
            # interchangeably for an empty response.  The caller's existing
            # empty-content policy decides whether to retry or report it.
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
