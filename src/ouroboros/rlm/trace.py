"""Trace record models for persisted RLM Hermes sub-calls."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
from typing import TYPE_CHECKING, Any

from ouroboros.events.base import BaseEvent

RLM_TRACE_SCHEMA_VERSION = "rlm.trace.v1"
RLM_TRACE_AGGREGATE_TYPE = "rlm_run"
RLM_HERMES_CALL_STARTED_EVENT = "rlm.hermes.call.started"
RLM_HERMES_CALL_SUCCEEDED_EVENT = "rlm.hermes.call.succeeded"
RLM_HERMES_CALL_FAILED_EVENT = "rlm.hermes.call.failed"
RLM_HERMES_CALL_COMPLETED_EVENT = "rlm.hermes.call.completed"
RLM_HERMES_TRACE_EVENT_TYPES = frozenset(
    {
        RLM_HERMES_CALL_STARTED_EVENT,
        RLM_HERMES_CALL_SUCCEEDED_EVENT,
        RLM_HERMES_CALL_FAILED_EVENT,
        RLM_HERMES_CALL_COMPLETED_EVENT,
    }
)
RLM_ATOMIC_EXECUTION_TRACE_MODES = frozenset(
    {
        "execute_atomic",
        "summarize_chunk",
        "synthesize_parent",
    }
)
RLM_TERMINAL_HERMES_LIFECYCLE_STATUSES = frozenset({"succeeded", "failed", "completed"})

if TYPE_CHECKING:
    from ouroboros.persistence.event_store import EventStore


def hash_trace_text(value: str) -> str:
    """Return the stable RLM trace hash for a prompt or completion string."""
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _mapping(value: object) -> Mapping[str, Any]:
    """Return ``value`` when it is mapping-shaped, otherwise an empty mapping."""
    return value if isinstance(value, Mapping) else {}


def _str_or_default(value: object, default: str) -> str:
    """Return a string value or the supplied default."""
    return value if isinstance(value, str) else default


def _int_or_default(value: object, default: int) -> int:
    """Return a non-bool integer value or the supplied default."""
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _optional_int(value: object) -> int | None:
    """Return a non-bool integer value or ``None`` for legacy/missing fields."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_non_negative_int(value: object) -> int | None:
    """Return a non-bool non-negative integer or ``None`` for missing fields."""
    integer = _optional_int(value)
    if integer is None or integer < 0:
        return None
    return integer


def _optional_bool(value: object) -> bool | None:
    """Return a boolean value or ``None`` for legacy/missing fields."""
    return value if isinstance(value, bool) else None


def _first_str(*values: object) -> str | None:
    """Return the first string among candidate legacy/current fields."""
    for value in values:
        if isinstance(value, str):
            return value
    return None


def _string_tuple(value: object) -> tuple[str, ...]:
    """Return a tuple containing only string IDs from a JSON-ish sequence."""
    if isinstance(value, str):
        return (value,) if value else ()
    if not isinstance(value, Sequence):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _first_string_tuple(*values: object) -> tuple[str, ...]:
    """Return the first non-empty string tuple from candidate fields."""
    for value in values:
        items = _string_tuple(value)
        if items:
            return items
    return ()


def _optional_mapping_dict(value: object) -> dict[str, Any] | None:
    """Return a shallow dict copy for optional JSON object fields."""
    return dict(value) if isinstance(value, Mapping) else None


def _handle_id(value: object) -> str | None:
    """Extract a readable handle ID from a persisted RuntimeHandle-like object."""
    handle = _mapping(value)
    metadata = _mapping(handle.get("metadata"))
    return _first_str(
        handle.get("resume_handle_id"),
        handle.get("runtime_handle_id"),
        handle.get("control_session_id"),
        handle.get("resume_session_id"),
        handle.get("server_session_id"),
        metadata.get("server_session_id"),
        handle.get("native_session_id"),
        handle.get("conversation_id"),
        handle.get("previous_response_id"),
    )


@dataclass(frozen=True, slots=True)
class RLMHermesTraceRecord:
    """Replayable Hermes sub-call fields stored in RLM traces."""

    prompt: str = ""
    completion: str = ""
    parent_call_id: str | None = None
    depth: int = 0
    schema_version: str = RLM_TRACE_SCHEMA_VERSION
    trace_id: str | None = None
    subcall_id: str | None = None
    parent_trace_id: str | None = None
    causal_parent_event_id: str | None = None
    call_id: str | None = None
    mode: str = "none"
    generation_id: str | None = None
    rlm_node_id: str | None = None
    ac_node_id: str | None = None
    selected_chunk_ids: tuple[str, ...] = ()
    generated_child_ac_node_ids: tuple[str, ...] = ()
    resume_handle_id: str | None = None
    runtime_handle_id: str | None = None
    prompt_hash: str | None = None
    response_hash: str | None = None
    success: bool | None = None
    exit_code: int | None = None
    elapsed_ms: int | None = None
    adapter_error: dict[str, Any] | None = None
    system_prompt_hash: str | None = None
    runtime: str = "hermes"

    def __post_init__(self) -> None:
        """Normalize optional text-like fields and reject invalid depth."""
        if not isinstance(self.prompt, str):
            msg = "RLM Hermes trace prompt must be a string"
            raise TypeError(msg)
        if not isinstance(self.completion, str):
            msg = "RLM Hermes trace completion must be a string"
            raise TypeError(msg)
        if self.parent_call_id is not None and not isinstance(self.parent_call_id, str):
            msg = "RLM Hermes trace parent_call_id must be a string or None"
            raise TypeError(msg)
        if isinstance(self.depth, bool) or not isinstance(self.depth, int):
            msg = "RLM Hermes trace depth must be an integer"
            raise TypeError(msg)
        if self.depth < 0:
            msg = "RLM Hermes trace depth must be non-negative"
            raise ValueError(msg)
        if self.subcall_id is None and self.call_id is not None:
            object.__setattr__(self, "subcall_id", self.call_id)
        object.__setattr__(self, "selected_chunk_ids", _string_tuple(self.selected_chunk_ids))
        object.__setattr__(
            self,
            "generated_child_ac_node_ids",
            _string_tuple(self.generated_child_ac_node_ids),
        )
        for field_name in (
            "trace_id",
            "subcall_id",
            "parent_trace_id",
            "causal_parent_event_id",
            "call_id",
            "generation_id",
            "rlm_node_id",
            "ac_node_id",
            "resume_handle_id",
            "runtime_handle_id",
            "prompt_hash",
            "response_hash",
            "system_prompt_hash",
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                msg = f"RLM Hermes trace {field_name} must be a string or None"
                raise TypeError(msg)
        if self.success is not None and not isinstance(self.success, bool):
            msg = "RLM Hermes trace success must be a boolean or None"
            raise TypeError(msg)
        if self.elapsed_ms is not None:
            if isinstance(self.elapsed_ms, bool) or not isinstance(self.elapsed_ms, int):
                msg = "RLM Hermes trace elapsed_ms must be an integer or None"
                raise TypeError(msg)
            if self.elapsed_ms < 0:
                msg = "RLM Hermes trace elapsed_ms must be non-negative"
                raise ValueError(msg)
        if self.adapter_error is not None:
            if not isinstance(self.adapter_error, Mapping):
                msg = "RLM Hermes trace adapter_error must be an object or None"
                raise TypeError(msg)
            object.__setattr__(self, "adapter_error", dict(self.adapter_error))

    def to_dict(self) -> dict[str, Any]:
        """Serialize the trace record as an EventStore JSON payload fragment."""
        return {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "subcall_id": self.subcall_id,
            "parent_trace_id": self.parent_trace_id,
            "causal_parent_event_id": self.causal_parent_event_id,
            "call_id": self.call_id,
            "parent_call_id": self.parent_call_id,
            "runtime": self.runtime,
            "mode": self.mode,
            "generation_id": self.generation_id,
            "rlm_node_id": self.rlm_node_id,
            "ac_node_id": self.ac_node_id,
            "depth": self.depth,
            "selected_chunk_ids": list(self.selected_chunk_ids),
            "generated_child_ac_node_ids": list(self.generated_child_ac_node_ids),
            "resume_handle_id": self.resume_handle_id,
            "runtime_handle_id": self.runtime_handle_id,
            "prompt": self.prompt,
            "completion": self.completion,
            "prompt_hash": self.prompt_hash,
            "response_hash": self.response_hash,
            "success": self.success,
            "exit_code": self.exit_code,
            "elapsed_ms": self.elapsed_ms,
            "adapter_error": dict(self.adapter_error) if self.adapter_error is not None else None,
            "system_prompt_hash": self.system_prompt_hash,
        }

    def to_event_data(self) -> dict[str, Any]:
        """Serialize the trace record as a nested ``BaseEvent.data`` payload.

        The Hermes fragment remains under ``hermes`` while cross-tree IDs and
        selected chunks are duplicated in their replay-oriented locations.
        """
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "subcall_id": self.subcall_id,
            "parent_trace_id": self.parent_trace_id,
            "rlm_run_id": self.generation_id,
            "generation_id": self.generation_id,
            "mode": self.mode,
            "causal_parent_event_id": self.causal_parent_event_id,
            "hermes": self.to_dict(),
            "recursion": {
                "trace_id": self.trace_id,
                "subcall_id": self.subcall_id,
                "call_id": self.call_id,
                "parent_call_id": self.parent_call_id,
                "parent_trace_id": self.parent_trace_id,
                "causal_parent_event_id": self.causal_parent_event_id,
                "depth": self.depth,
                "rlm_node_id": self.rlm_node_id,
                "ac_node_id": self.ac_node_id,
                "selected_chunk_ids": list(self.selected_chunk_ids),
                "generated_child_ac_node_ids": list(self.generated_child_ac_node_ids),
            },
        }
        if any(
            value is not None
            for value in (
                self.trace_id,
                self.subcall_id,
                self.parent_trace_id,
                self.causal_parent_event_id,
                self.parent_call_id,
            )
        ) or self.depth != 0 or self.selected_chunk_ids:
            data["trace"] = {
                "trace_id": self.trace_id,
                "subcall_id": self.subcall_id,
                "parent_call_id": self.parent_call_id,
                "parent_trace_id": self.parent_trace_id,
                "causal_parent_event_id": self.causal_parent_event_id,
                "depth": self.depth,
                "selected_chunk_ids": list(self.selected_chunk_ids),
            }
        if self.rlm_node_id is not None:
            data["rlm_node"] = {"id": self.rlm_node_id, "depth": self.depth}
        if self.ac_node_id is not None:
            data["ac_node"] = {"id": self.ac_node_id, "depth": self.depth}
            if self.generated_child_ac_node_ids:
                data["ac_node"]["child_ids"] = list(self.generated_child_ac_node_ids)
        if self.selected_chunk_ids:
            data["context"] = {"selected_chunk_ids": list(self.selected_chunk_ids)}
        if self.generated_child_ac_node_ids:
            child_ac_node_ids = list(self.generated_child_ac_node_ids)
            data["replay"] = {
                "creates_ac_node_ids": child_ac_node_ids,
                "generated_child_ac_node_ids": child_ac_node_ids,
            }
        if self.mode in RLM_ATOMIC_EXECUTION_TRACE_MODES:
            data["atomic_ac_execution"] = {
                "rlm_node_id": self.rlm_node_id,
                "ac_node_id": self.ac_node_id,
                "call_id": self.call_id,
                "subcall_id": self.subcall_id,
                "parent_call_id": self.parent_call_id,
                "depth": self.depth,
                "selected_chunk_ids": list(self.selected_chunk_ids),
                "input": self.prompt,
                "output": self.completion,
                "exit_code": self.exit_code,
                "success": self.success,
            }
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RLMHermesTraceRecord:
        """Deserialize current or legacy EventStore trace payload fragments.

        The RLM MVP has used two shapes while the trace contract has been
        stabilizing:

        - a flat ``RLMHermesTraceRecord.to_dict()`` fragment, often stored under
          ``hermes_call`` in decomposition events;
        - the concept trace shape with Hermes fields nested under ``hermes`` and
          linked node IDs under ``rlm_node`` / ``ac_node``.

        Existing rows may also omit fields introduced later. Missing values are
        filled from the dataclass defaults so old records remain replayable.
        """
        hermes = _mapping(data.get("hermes"))
        rlm_node = _mapping(data.get("rlm_node"))
        ac_node = _mapping(data.get("ac_node"))
        context = _mapping(data.get("context"))
        trace = _mapping(data.get("trace"))
        recursion = _mapping(data.get("recursion"))
        replay = _mapping(data.get("replay"))

        call_id = _first_str(
            hermes.get("call_id"),
            data.get("call_id"),
            recursion.get("call_id"),
            hermes.get("subcall_id"),
            data.get("subcall_id"),
            trace.get("subcall_id"),
            recursion.get("subcall_id"),
        )
        subcall_id = _first_str(
            hermes.get("subcall_id"),
            data.get("subcall_id"),
            trace.get("subcall_id"),
            recursion.get("subcall_id"),
            call_id,
        )

        prompt = _first_str(
            hermes.get("prompt"),
            data.get("prompt"),
            data.get("input"),
        )
        completion = _first_str(
            hermes.get("completion"),
            data.get("completion"),
            data.get("response"),
            data.get("final_message"),
            data.get("output"),
        )

        return cls(
            prompt=prompt or "",
            completion=completion or "",
            parent_call_id=_first_str(
                hermes.get("parent_call_id"),
                data.get("parent_call_id"),
                recursion.get("parent_call_id"),
                trace.get("parent_call_id"),
            ),
            depth=_int_or_default(
                hermes.get(
                    "depth",
                    recursion.get(
                        "depth",
                        trace.get("depth", data.get("depth", data.get("recursion_depth"))),
                    ),
                ),
                0,
            ),
            schema_version=_str_or_default(
                data.get("schema_version"),
                _str_or_default(hermes.get("schema_version"), RLM_TRACE_SCHEMA_VERSION),
            ),
            trace_id=_first_str(
                hermes.get("trace_id"),
                data.get("trace_id"),
                trace.get("trace_id"),
                recursion.get("trace_id"),
            ),
            subcall_id=subcall_id,
            parent_trace_id=_first_str(
                hermes.get("parent_trace_id"),
                data.get("parent_trace_id"),
                trace.get("parent_trace_id"),
                recursion.get("parent_trace_id"),
                hermes.get("parent_trace_record_id"),
                data.get("parent_trace_record_id"),
            ),
            causal_parent_event_id=_first_str(
                hermes.get("causal_parent_event_id"),
                data.get("causal_parent_event_id"),
                trace.get("causal_parent_event_id"),
                recursion.get("causal_parent_event_id"),
                hermes.get("parent_event_id"),
                data.get("parent_event_id"),
            ),
            call_id=call_id,
            mode=_str_or_default(data.get("mode"), _str_or_default(hermes.get("mode"), "none")),
            generation_id=_first_str(
                data.get("generation_id"),
                data.get("rlm_run_id"),
                hermes.get("generation_id"),
            ),
            rlm_node_id=_first_str(
                data.get("rlm_node_id"),
                hermes.get("rlm_node_id"),
                rlm_node.get("id"),
                recursion.get("rlm_node_id"),
            ),
            ac_node_id=_first_str(
                data.get("ac_node_id"),
                hermes.get("ac_node_id"),
                ac_node.get("id"),
                recursion.get("ac_node_id"),
            ),
            selected_chunk_ids=_first_string_tuple(
                hermes.get("selected_chunk_ids"),
                context.get("selected_chunk_ids"),
                trace.get("selected_chunk_ids"),
                data.get("selected_chunk_ids"),
                recursion.get("selected_chunk_ids"),
                hermes.get("chunk_id"),
                data.get("chunk_id"),
            ),
            generated_child_ac_node_ids=_first_string_tuple(
                hermes.get("generated_child_ac_node_ids"),
                data.get("generated_child_ac_node_ids"),
                recursion.get("generated_child_ac_node_ids"),
                replay.get("generated_child_ac_node_ids"),
                replay.get("creates_ac_node_ids"),
                replay.get("child_ac_node_ids"),
                ac_node.get("child_ids"),
                data.get("child_ac_node_ids"),
                data.get("child_ac_ids"),
            ),
            resume_handle_id=_first_str(
                hermes.get("resume_handle_id"),
                trace.get("resume_handle_id"),
                data.get("resume_handle_id"),
                _handle_id(hermes.get("resume_handle")),
                _handle_id(data.get("resume_handle")),
            ),
            runtime_handle_id=_first_str(
                hermes.get("runtime_handle_id"),
                trace.get("runtime_handle_id"),
                data.get("runtime_handle_id"),
                _handle_id(hermes.get("runtime_handle")),
                _handle_id(data.get("runtime_handle")),
            ),
            prompt_hash=_first_str(hermes.get("prompt_hash"), data.get("prompt_hash")),
            response_hash=_first_str(
                hermes.get("response_hash"),
                hermes.get("completion_hash"),
                hermes.get("final_message_hash"),
                data.get("response_hash"),
                data.get("completion_hash"),
                data.get("final_message_hash"),
            ),
            success=_optional_bool(hermes.get("success", data.get("success"))),
            exit_code=_optional_int(hermes.get("exit_code", data.get("exit_code"))),
            elapsed_ms=_optional_non_negative_int(
                hermes.get(
                    "elapsed_ms",
                    hermes.get(
                        "elapsed_time_ms",
                        hermes.get(
                            "duration_ms",
                            data.get(
                                "elapsed_ms",
                                data.get("elapsed_time_ms", data.get("duration_ms")),
                            ),
                        ),
                    ),
                ),
            ),
            adapter_error=_optional_mapping_dict(
                hermes.get(
                    "adapter_error",
                    hermes.get(
                        "provider_error",
                        hermes.get(
                            "error_details",
                            data.get(
                                "adapter_error",
                                data.get("provider_error", data.get("error_details")),
                            ),
                        ),
                    ),
                ),
            ),
            system_prompt_hash=_first_str(
                hermes.get("system_prompt_hash"),
                data.get("system_prompt_hash"),
            ),
            runtime=_str_or_default(
                hermes.get("runtime"),
                _str_or_default(data.get("runtime"), "hermes"),
            ),
        )

    @classmethod
    def from_event_data(cls, data: Mapping[str, Any]) -> RLMHermesTraceRecord:
        """Deserialize a trace record from a persisted ``BaseEvent.data`` payload."""
        hermes_call = data.get("hermes_call")
        if isinstance(hermes_call, Mapping):
            return cls.from_dict(hermes_call)
        hermes_subcall = data.get("hermes_subcall")
        if isinstance(hermes_subcall, Mapping):
            return cls.from_dict(hermes_subcall)
        for key in ("hermes_calls", "hermes_subcalls"):
            value = data.get(key)
            if isinstance(value, Sequence) and not isinstance(value, str):
                for item in value:
                    item_mapping = _mapping(item)
                    if not item_mapping:
                        continue
                    nested_call = item_mapping.get("hermes_call")
                    if isinstance(nested_call, Mapping):
                        return cls.from_dict(nested_call)
                    nested_subcall = item_mapping.get("hermes_subcall")
                    if isinstance(nested_subcall, Mapping):
                        return cls.from_dict(nested_subcall)
                    if isinstance(item_mapping.get("hermes"), Mapping):
                        return cls.from_dict(item_mapping)
                    if _event_data_looks_like_trace_fragment(item_mapping):
                        return cls.from_dict(item_mapping)
        hermes_subquestion_results = data.get("hermes_subquestion_results")
        if isinstance(hermes_subquestion_results, Sequence) and not isinstance(
            hermes_subquestion_results,
            str,
        ):
            for item in hermes_subquestion_results:
                item_mapping = _mapping(item)
                nested_call = item_mapping.get("hermes_call")
                if isinstance(nested_call, Mapping):
                    return cls.from_dict(
                        _trace_fragment_from_legacy_wrapper(item_mapping, nested_call)
                    )
        return cls.from_dict(data)

    @classmethod
    def from_event_data_many(cls, data: Mapping[str, Any]) -> tuple[RLMHermesTraceRecord, ...]:
        """Deserialize every Hermes trace record embedded in one event payload."""
        records: list[RLMHermesTraceRecord] = []
        for key in ("hermes_call", "hermes_subcall"):
            value = data.get(key)
            if isinstance(value, Mapping):
                records.append(cls.from_dict(value))
        if isinstance(data.get("hermes"), Mapping):
            records.append(cls.from_dict(data))

        for key in ("hermes_calls", "hermes_subcalls"):
            value = data.get(key)
            if isinstance(value, Sequence) and not isinstance(value, str):
                for item in value:
                    item_mapping = _mapping(item)
                    if not item_mapping:
                        continue
                    nested_call = item_mapping.get("hermes_call")
                    nested_subcall = item_mapping.get("hermes_subcall")
                    if isinstance(nested_call, Mapping):
                        records.append(cls.from_dict(nested_call))
                    elif isinstance(nested_subcall, Mapping):
                        records.append(cls.from_dict(nested_subcall))
                    elif isinstance(item_mapping.get("hermes"), Mapping) or (
                        _event_data_looks_like_trace_fragment(item_mapping)
                    ):
                        records.append(cls.from_dict(item_mapping))

        hermes_subquestion_results = data.get("hermes_subquestion_results")
        if isinstance(hermes_subquestion_results, Sequence) and not isinstance(
            hermes_subquestion_results,
            str,
        ):
            for item in hermes_subquestion_results:
                item_mapping = _mapping(item)
                nested_call = item_mapping.get("hermes_call")
                if isinstance(nested_call, Mapping):
                    records.append(
                        cls.from_dict(
                            _trace_fragment_from_legacy_wrapper(item_mapping, nested_call)
                        )
                    )

        if records:
            return tuple(records)

        if _event_data_looks_like_trace_fragment(data):
            return (cls.from_dict(data),)
        return ()


RLMTraceRecord = RLMHermesTraceRecord


@dataclass(frozen=True, slots=True)
class RLMSubcallChildACLink:
    """Verified persisted link from a parent Hermes sub-call to child AC nodes."""

    parent_call_id: str
    parent_trace_id: str
    parent_subcall_id: str | None
    parent_ac_node_id: str | None
    generated_child_ac_node_ids: tuple[str, ...]
    child_call_ids: tuple[str, ...]
    child_trace_ids: tuple[str, ...]
    parent_lifecycle_statuses: tuple[str, ...]
    child_lifecycle_statuses: tuple[str, ...]
    event_ids: tuple[str, ...]


def _event_data_looks_like_trace_fragment(data: Mapping[str, Any]) -> bool:
    """Return True when a mapping contains trace fragment fields."""
    trace_keys = {
        "prompt",
        "completion",
        "trace_id",
        "subcall_id",
        "parent_trace_id",
        "causal_parent_event_id",
        "parent_call_id",
        "call_id",
        "generated_child_ac_node_ids",
        "selected_chunk_ids",
        "prompt_hash",
        "response_hash",
        "exit_code",
        "recursion",
        "replay",
    }
    return bool(trace_keys & set(data))


def _trace_fragment_from_legacy_wrapper(
    wrapper: Mapping[str, Any],
    nested: Mapping[str, Any],
) -> dict[str, Any]:
    """Overlay legacy wrapper node IDs when the nested Hermes call omitted them."""
    fragment = dict(nested)
    if not _first_str(
        fragment.get("ac_node_id"),
        _mapping(fragment.get("ac_node")).get("id"),
        _mapping(fragment.get("recursion")).get("ac_node_id"),
    ):
        child_ac_id = _first_str(wrapper.get("child_ac_id"), wrapper.get("child_ac_node_id"))
        if child_ac_id is not None:
            fragment["ac_node_id"] = child_ac_id
    if not _first_str(
        fragment.get("rlm_node_id"),
        _mapping(fragment.get("rlm_node")).get("id"),
        _mapping(fragment.get("recursion")).get("rlm_node_id"),
    ):
        child_node_id = _first_str(wrapper.get("child_node_id"), wrapper.get("child_rlm_node_id"))
        if child_node_id is not None:
            fragment["rlm_node_id"] = child_node_id
    return fragment


def _lifecycle_status_for_event_type(event_type: str) -> str:
    """Return the normalized Hermes call lifecycle status for an event type."""
    if event_type == RLM_HERMES_CALL_STARTED_EVENT:
        return "started"
    if event_type == RLM_HERMES_CALL_SUCCEEDED_EVENT:
        return "succeeded"
    if event_type == RLM_HERMES_CALL_FAILED_EVENT:
        return "failed"
    return "completed"


def _event_lifecycle_status(event: BaseEvent) -> str:
    """Return the lifecycle status stored on a replayed trace event."""
    lifecycle = _mapping(event.data.get("lifecycle"))
    status = lifecycle.get("status")
    return status if isinstance(status, str) else _lifecycle_status_for_event_type(event.type)


def _append_unique(values: list[str], value: str | None) -> None:
    """Append ``value`` once while preserving first-seen event order."""
    if value is not None and value not in values:
        values.append(value)


def _only_value(values: Sequence[str], *, field_name: str, call_id: str) -> str | None:
    """Return the unique non-empty value or raise when replay found drift."""
    unique_values = tuple(dict.fromkeys(value for value in values if value))
    if len(unique_values) > 1:
        msg = f"RLM persisted {field_name} drifted for Hermes call {call_id}: {unique_values!r}"
        raise ValueError(msg)
    return unique_values[0] if unique_values else None


def verify_rlm_subcall_child_ac_links(
    events: Sequence[BaseEvent],
) -> tuple[RLMSubcallChildACLink, ...]:
    """Verify persisted RLM events still reconstruct sub-call-to-child-AC links.

    The verifier operates only on replayed EventStore events, so it catches links
    that existed in the in-memory scaffold but were lost during persistence or
    lifecycle event emission. A parent sub-call link is valid when every persisted
    lifecycle record for that parent carries the same child AC IDs and every
    child AC record links back to the parent's call and trace IDs.
    """
    trace_entries: list[tuple[BaseEvent, RLMHermesTraceRecord, str]] = []
    for event in events:
        if event.type not in RLM_HERMES_TRACE_EVENT_TYPES:
            continue
        status = _event_lifecycle_status(event)
        for record in RLMHermesTraceRecord.from_event_data_many(event.data):
            trace_entries.append((event, record, status))

    entries_by_call_id: dict[str, list[tuple[BaseEvent, RLMHermesTraceRecord, str]]] = {}
    parent_call_ids: list[str] = []
    for entry in trace_entries:
        _event, record, _status = entry
        if record.call_id is None:
            continue
        entries_by_call_id.setdefault(record.call_id, []).append(entry)
        if record.generated_child_ac_node_ids:
            _append_unique(parent_call_ids, record.call_id)

    links: list[RLMSubcallChildACLink] = []
    for parent_call_id in parent_call_ids:
        parent_entries = entries_by_call_id[parent_call_id]
        parent_records = [record for _event, record, _status in parent_entries]
        missing_link_statuses = tuple(
            status for _event, record, status in parent_entries if not record.generated_child_ac_node_ids
        )
        if missing_link_statuses:
            msg = (
                "RLM persisted child AC link missing from lifecycle records for "
                f"Hermes call {parent_call_id}: {missing_link_statuses!r}"
            )
            raise ValueError(msg)

        generated_child_sets = tuple(
            tuple(dict.fromkeys(record.generated_child_ac_node_ids))
            for record in parent_records
        )
        first_child_set = generated_child_sets[0]
        if any(tuple(child_set) != first_child_set for child_set in generated_child_sets):
            msg = (
                "RLM persisted child AC IDs drifted across lifecycle records for "
                f"Hermes call {parent_call_id}"
            )
            raise ValueError(msg)

        parent_trace_id = _only_value(
            [record.trace_id or "" for record in parent_records],
            field_name="parent trace_id",
            call_id=parent_call_id,
        )
        if parent_trace_id is None:
            msg = f"RLM persisted child AC link for Hermes call {parent_call_id} has no trace_id"
            raise ValueError(msg)
        parent_subcall_id = _only_value(
            [record.subcall_id or "" for record in parent_records],
            field_name="parent subcall_id",
            call_id=parent_call_id,
        )
        parent_ac_node_id = _only_value(
            [record.ac_node_id or "" for record in parent_records],
            field_name="parent ac_node_id",
            call_id=parent_call_id,
        )
        parent_lifecycle_statuses = tuple(status for _event, _record, status in parent_entries)
        if not (set(parent_lifecycle_statuses) & RLM_TERMINAL_HERMES_LIFECYCLE_STATUSES):
            msg = f"RLM persisted child AC link for Hermes call {parent_call_id} has no terminal event"
            raise ValueError(msg)

        child_call_ids: list[str] = []
        child_trace_ids: list[str] = []
        child_lifecycle_statuses: list[str] = []
        event_ids: list[str] = []
        for event, _record, _status in parent_entries:
            _append_unique(event_ids, event.id)

        for child_ac_node_id in first_child_set:
            child_entries = [
                entry
                for entry in trace_entries
                if entry[1].ac_node_id == child_ac_node_id
                and entry[1].call_id != parent_call_id
            ]
            if not child_entries:
                msg = (
                    "RLM persisted child AC link for Hermes call "
                    f"{parent_call_id} references {child_ac_node_id}, but no child trace exists"
                )
                raise ValueError(msg)

            current_child_lifecycle_statuses: list[str] = []
            for event, child_record, status in child_entries:
                _append_unique(event_ids, event.id)
                _append_unique(child_call_ids, child_record.call_id)
                _append_unique(child_trace_ids, child_record.trace_id)
                child_lifecycle_statuses.append(status)
                current_child_lifecycle_statuses.append(status)
                if child_record.parent_call_id != parent_call_id:
                    msg = (
                        "RLM persisted child AC link mismatch for "
                        f"{child_ac_node_id}: parent_call_id={child_record.parent_call_id!r}, "
                        f"expected {parent_call_id!r}"
                    )
                    raise ValueError(msg)
                if child_record.parent_trace_id != parent_trace_id:
                    msg = (
                        "RLM persisted child AC link mismatch for "
                        f"{child_ac_node_id}: parent_trace_id={child_record.parent_trace_id!r}, "
                        f"expected {parent_trace_id!r}"
                    )
                    raise ValueError(msg)
                if child_record.causal_parent_event_id != parent_call_id:
                    msg = (
                        "RLM persisted child AC link mismatch for "
                        f"{child_ac_node_id}: causal_parent_event_id="
                        f"{child_record.causal_parent_event_id!r}, expected {parent_call_id!r}"
                    )
                    raise ValueError(msg)

            if not (
                set(current_child_lifecycle_statuses) & RLM_TERMINAL_HERMES_LIFECYCLE_STATUSES
            ):
                msg = (
                    "RLM persisted child AC link for Hermes call "
                    f"{parent_call_id} references {child_ac_node_id}, but no child terminal event exists"
                )
                raise ValueError(msg)

        links.append(
            RLMSubcallChildACLink(
                parent_call_id=parent_call_id,
                parent_trace_id=parent_trace_id,
                parent_subcall_id=parent_subcall_id,
                parent_ac_node_id=parent_ac_node_id,
                generated_child_ac_node_ids=first_child_set,
                child_call_ids=tuple(child_call_ids),
                child_trace_ids=tuple(child_trace_ids),
                parent_lifecycle_statuses=parent_lifecycle_statuses,
                child_lifecycle_statuses=tuple(child_lifecycle_statuses),
                event_ids=tuple(event_ids),
            )
        )

    return tuple(links)


def create_rlm_hermes_trace_event(
    record: RLMHermesTraceRecord,
    *,
    event_type: str = RLM_HERMES_CALL_COMPLETED_EVENT,
    aggregate_id: str | None = None,
    aggregate_type: str = RLM_TRACE_AGGREGATE_TYPE,
) -> BaseEvent:
    """Create the EventStore event for one persisted Hermes sub-call record."""
    if event_type not in RLM_HERMES_TRACE_EVENT_TYPES:
        msg = (
            "RLM Hermes trace event_type must be "
            f"one of {sorted(RLM_HERMES_TRACE_EVENT_TYPES)!r}"
        )
        raise ValueError(msg)

    resolved_aggregate_id = aggregate_id or record.generation_id
    if not resolved_aggregate_id:
        msg = "RLM Hermes trace records require an aggregate_id or generation_id"
        raise ValueError(msg)

    lifecycle_status = _lifecycle_status_for_event_type(event_type)
    data = record.to_event_data()
    data["lifecycle"] = {
        "event_type": event_type,
        "status": lifecycle_status,
        "success": record.success,
        "elapsed_ms": record.elapsed_ms,
    }
    hermes = data.get("hermes")
    if isinstance(hermes, dict):
        hermes["lifecycle_event_type"] = event_type
        hermes["lifecycle_status"] = lifecycle_status

    return BaseEvent(
        type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=resolved_aggregate_id,
        data=data,
    )


async def persist_rlm_hermes_trace_record(
    event_store: EventStore,
    record: RLMHermesTraceRecord,
    *,
    event_type: str = RLM_HERMES_CALL_COMPLETED_EVENT,
    aggregate_id: str | None = None,
    aggregate_type: str = RLM_TRACE_AGGREGATE_TYPE,
) -> BaseEvent:
    """Append one Hermes sub-call trace event and return the persisted event."""
    event = create_rlm_hermes_trace_event(
        record,
        event_type=event_type,
        aggregate_id=aggregate_id,
        aggregate_type=aggregate_type,
    )
    await event_store.append(event)
    return event


def rlm_hermes_trace_records_from_events(
    events: Sequence[BaseEvent],
) -> tuple[RLMHermesTraceRecord, ...]:
    """Extract every RLM Hermes sub-call trace record from replayed events."""
    records: list[RLMHermesTraceRecord] = []
    for event in events:
        if event.type not in RLM_HERMES_TRACE_EVENT_TYPES:
            continue
        records.extend(RLMHermesTraceRecord.from_event_data_many(event.data))
    return tuple(records)


async def replay_rlm_hermes_trace_records(
    event_store: EventStore,
    generation_id: str,
    *,
    aggregate_type: str = RLM_TRACE_AGGREGATE_TYPE,
) -> tuple[RLMHermesTraceRecord, ...]:
    """Replay RLM Hermes sub-call records for one RLM run/generation."""
    events = await event_store.replay(aggregate_type, generation_id)
    return rlm_hermes_trace_records_from_events(events)


@dataclass(frozen=True, slots=True)
class RLMTraceStore:
    """Small storage adapter for RLM Hermes sub-call trace records."""

    event_store: EventStore
    aggregate_type: str = RLM_TRACE_AGGREGATE_TYPE

    async def append_hermes_subcall(
        self,
        record: RLMHermesTraceRecord,
        *,
        event_type: str = RLM_HERMES_CALL_COMPLETED_EVENT,
        aggregate_id: str | None = None,
    ) -> BaseEvent:
        """Persist one Hermes sub-call trace record."""
        return await persist_rlm_hermes_trace_record(
            self.event_store,
            record,
            event_type=event_type,
            aggregate_id=aggregate_id,
            aggregate_type=self.aggregate_type,
        )

    async def append_hermes_call_started(
        self,
        record: RLMHermesTraceRecord,
        *,
        aggregate_id: str | None = None,
    ) -> BaseEvent:
        """Persist a Hermes sub-call start trace event."""
        return await self.append_hermes_subcall(
            record,
            event_type=RLM_HERMES_CALL_STARTED_EVENT,
            aggregate_id=aggregate_id,
        )

    async def append_hermes_call_completed(
        self,
        record: RLMHermesTraceRecord,
        *,
        aggregate_id: str | None = None,
    ) -> BaseEvent:
        """Persist a Hermes sub-call completion trace event."""
        return await self.append_hermes_subcall(
            record,
            event_type=RLM_HERMES_CALL_COMPLETED_EVENT,
            aggregate_id=aggregate_id,
        )

    async def append_hermes_call_succeeded(
        self,
        record: RLMHermesTraceRecord,
        *,
        aggregate_id: str | None = None,
    ) -> BaseEvent:
        """Persist a successful Hermes sub-call trace event."""
        return await self.append_hermes_subcall(
            record,
            event_type=RLM_HERMES_CALL_SUCCEEDED_EVENT,
            aggregate_id=aggregate_id,
        )

    async def append_hermes_call_failed(
        self,
        record: RLMHermesTraceRecord,
        *,
        aggregate_id: str | None = None,
    ) -> BaseEvent:
        """Persist a failed Hermes sub-call trace event."""
        return await self.append_hermes_subcall(
            record,
            event_type=RLM_HERMES_CALL_FAILED_EVENT,
            aggregate_id=aggregate_id,
        )

    async def replay_hermes_subcalls(
        self,
        generation_id: str,
    ) -> tuple[RLMHermesTraceRecord, ...]:
        """Replay all RLM Hermes sub-call records for one run/generation."""
        return await replay_rlm_hermes_trace_records(
            self.event_store,
            generation_id,
            aggregate_type=self.aggregate_type,
        )

__all__ = [
    "RLM_HERMES_CALL_COMPLETED_EVENT",
    "RLM_HERMES_CALL_FAILED_EVENT",
    "RLM_HERMES_CALL_STARTED_EVENT",
    "RLM_HERMES_CALL_SUCCEEDED_EVENT",
    "RLM_HERMES_TRACE_EVENT_TYPES",
    "RLM_ATOMIC_EXECUTION_TRACE_MODES",
    "RLM_TRACE_AGGREGATE_TYPE",
    "RLM_TRACE_SCHEMA_VERSION",
    "RLMHermesTraceRecord",
    "RLMTraceRecord",
    "RLMTraceStore",
    "RLMSubcallChildACLink",
    "create_rlm_hermes_trace_event",
    "hash_trace_text",
    "persist_rlm_hermes_trace_record",
    "replay_rlm_hermes_trace_records",
    "rlm_hermes_trace_records_from_events",
    "verify_rlm_subcall_child_ac_links",
]
