"""Workflow IR lifecycle event contract.

This module is the narrow #956 lifecycle slice over the existing
``WorkflowSpec`` schema. It defines bounded, replay-safe lifecycle records
that can be persisted as events and later projected without making the IR
the live ``parallel_executor`` dispatch source.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
import json
from types import MappingProxyType
from typing import Any, Final, cast

from pydantic import BaseModel, Field, field_validator, model_validator

from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.workflow_ir import EdgeKind, WorkflowEdge, WorkflowNode, WorkflowSpec

WORKFLOW_LIFECYCLE_SCHEMA_VERSION: Final[int] = 1
MAX_WORKFLOW_LIFECYCLE_DATA_BYTES: Final[int] = 8192

_REPLAY_UNSAFE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "apikey",
        "auth_token",
        "bearer_token",
        "client_secret",
        "credential",
        "credentials",
        "password",
        "private_key",
        "prompt",
        "raw_prompt",
        "raw_stderr",
        "raw_stdout",
        "raw_output",
        "refresh_token",
        "secret",
        "stderr",
        "stdout",
        "token",
    }
)
_REPLAY_UNSAFE_SUFFIXES: Final[tuple[str, ...]] = (
    "_api_key",
    "_credential",
    "_credentials",
    "_password",
    "_prompt",
    "_secret",
    "_stderr",
    "_stdout",
    "_token",
)

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]
FrozenJsonValue = JsonScalar | Mapping[str, "FrozenJsonValue"] | tuple["FrozenJsonValue", ...]


class WorkflowLifecycleEventType(StrEnum):
    """Bounded lifecycle event vocabulary for #956 Workflow IR."""

    RUN_CREATED = "workflow.run.created"
    NODE_SCHEDULED = "workflow.node.scheduled"
    NODE_STARTED = "workflow.node.started"
    NODE_COMPLETED = "workflow.node.completed"
    NODE_FAILED = "workflow.node.failed"
    NODE_RETRIED = "workflow.node.retried"
    EDGE_TRAVERSED = "workflow.edge.traversed"
    CHECKPOINT_SAVED = "workflow.checkpoint.saved"
    RUN_COMPLETED = "workflow.run.completed"
    RUN_FAILED = "workflow.run.failed"
    RUN_CANCELLED = "workflow.run.cancelled"


class WorkflowNodeLifecycleState(StrEnum):
    """Effective node state derived from lifecycle history."""

    PENDING = "pending"
    SCHEDULED = "scheduled"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRIED = "retried"


class WorkflowRunLifecycleState(StrEnum):
    """Run-level lifecycle state represented by terminal run events."""

    CREATED = "created"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_NODE_EVENT_TYPES: Final[frozenset[WorkflowLifecycleEventType]] = frozenset(
    {
        WorkflowLifecycleEventType.NODE_SCHEDULED,
        WorkflowLifecycleEventType.NODE_STARTED,
        WorkflowLifecycleEventType.NODE_COMPLETED,
        WorkflowLifecycleEventType.NODE_FAILED,
        WorkflowLifecycleEventType.NODE_RETRIED,
    }
)
_RUN_EVENT_TYPES: Final[frozenset[WorkflowLifecycleEventType]] = frozenset(
    {
        WorkflowLifecycleEventType.RUN_CREATED,
        WorkflowLifecycleEventType.RUN_COMPLETED,
        WorkflowLifecycleEventType.RUN_FAILED,
        WorkflowLifecycleEventType.RUN_CANCELLED,
    }
)
_NODE_STATE_BY_EVENT: Final[dict[WorkflowLifecycleEventType, WorkflowNodeLifecycleState]] = {
    WorkflowLifecycleEventType.NODE_SCHEDULED: WorkflowNodeLifecycleState.SCHEDULED,
    WorkflowLifecycleEventType.NODE_STARTED: WorkflowNodeLifecycleState.STARTED,
    WorkflowLifecycleEventType.NODE_COMPLETED: WorkflowNodeLifecycleState.COMPLETED,
    WorkflowLifecycleEventType.NODE_FAILED: WorkflowNodeLifecycleState.FAILED,
    WorkflowLifecycleEventType.NODE_RETRIED: WorkflowNodeLifecycleState.RETRIED,
}


def _normalize_non_blank(name: str, value: str) -> str:
    if not isinstance(value, str):
        msg = f"Workflow lifecycle {name} must be a string"
        raise TypeError(msg)
    normalized = value.strip()
    if not normalized:
        msg = f"Workflow lifecycle {name} must be non-blank"
        raise ValueError(msg)
    return normalized


def _normalize_optional_non_blank(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    return _normalize_non_blank(name, value)


def _normalize_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        msg = "Workflow lifecycle timestamp must be a datetime"
        raise TypeError(msg)
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "Workflow lifecycle timestamp must be timezone-aware"
        raise ValueError(msg)
    return value.astimezone(UTC)


def _is_replay_unsafe_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized in _REPLAY_UNSAFE_KEYS or normalized.endswith(_REPLAY_UNSAFE_SUFFIXES)


def _normalize_json_value(name: str, value: Any, path: str) -> JsonValue:
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        if not json.dumps(value, allow_nan=False):  # pragma: no cover - json handles finite floats
            raise ValueError(f"Workflow lifecycle {name} value at {path} is not finite")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                msg = f"Workflow lifecycle {name} key at {path} must be a string"
                raise TypeError(msg)
            if _is_replay_unsafe_key(key):
                msg = f"Workflow lifecycle {name} must not persist replay-unsafe key {key!r}"
                raise ValueError(msg)
            normalized[key] = _normalize_json_value(name, item, f"{path}.{key}")
        return normalized
    if isinstance(value, list | tuple):
        return [
            _normalize_json_value(name, item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    msg = f"Workflow lifecycle {name} value at {path} must be JSON serializable"
    raise TypeError(msg)


def _freeze_json_value(value: JsonValue) -> FrozenJsonValue:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _thaw_json_value(value: FrozenJsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


def _normalize_data(value: Mapping[str, Any]) -> Mapping[str, FrozenJsonValue]:
    if not isinstance(value, Mapping):
        msg = "Workflow lifecycle data must be a mapping"
        raise TypeError(msg)
    normalized = _normalize_json_value("data", value, "data")
    encoded = json.dumps(normalized, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_WORKFLOW_LIFECYCLE_DATA_BYTES:
        msg = f"Workflow lifecycle data exceeds {MAX_WORKFLOW_LIFECYCLE_DATA_BYTES} bytes"
        raise ValueError(msg)
    return cast(Mapping[str, FrozenJsonValue], _freeze_json_value(normalized))


def _thaw_data(value: Mapping[str, Any]) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], _thaw_json_value(value))


def _normalize_refs(values: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        ref = _normalize_non_blank(f"refs[{index}]", value)
        if ref in seen:
            msg = f"Workflow lifecycle refs must be unique: {ref!r}"
            raise ValueError(msg)
        seen.add(ref)
        normalized.append(ref)
    return tuple(normalized)


class WorkflowLifecycleEvent(BaseModel, frozen=True):
    """Replay-safe lifecycle event for a ``WorkflowSpec`` run.

    ``workflow_id`` is the stable ``WorkflowSpec.spec_id`` anchor. Node and
    edge events use ``WorkflowNode.node_id`` / ``WorkflowEdge.edge_id`` so a
    future #946 projector can correlate lifecycle rows without inventing a
    second identity vocabulary.
    """

    schema_version: int = Field(default=WORKFLOW_LIFECYCLE_SCHEMA_VERSION, ge=1)
    event_type: WorkflowLifecycleEventType
    workflow_id: str
    node_id: str | None = None
    edge_id: str | None = None
    attempt: int | None = Field(default=None, ge=1)
    reason_code: str | None = None
    refs: tuple[str, ...] = Field(default_factory=tuple)
    data: Mapping[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("workflow_id")
    @classmethod
    def _workflow_id_non_blank(cls, value: str) -> str:
        return _normalize_non_blank("workflow_id", value)

    @field_validator("node_id", "edge_id", "reason_code")
    @classmethod
    def _optional_fields_non_blank(cls, value: str | None, info: Any) -> str | None:
        return _normalize_optional_non_blank(str(info.field_name), value)

    @field_validator("refs", mode="before")
    @classmethod
    def _coerce_refs(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str) or not isinstance(value, Iterable):
            msg = "Workflow lifecycle refs must be an iterable of strings"
            raise TypeError(msg)
        return _normalize_refs(value)

    @field_validator("data", mode="before")
    @classmethod
    def _coerce_data(cls, value: Any) -> Mapping[str, Any]:
        if value is None:
            return MappingProxyType({})
        return _normalize_data(value)

    @field_validator("timestamp")
    @classmethod
    def _timestamp_utc(cls, value: datetime) -> datetime:
        return _normalize_utc(value)

    @model_validator(mode="after")
    def _validate_shape(self) -> WorkflowLifecycleEvent:
        if self.event_type in _NODE_EVENT_TYPES and self.node_id is None:
            msg = f"{self.event_type.value} requires node_id"
            raise ValueError(msg)
        if self.event_type not in _NODE_EVENT_TYPES and self.node_id is not None:
            msg = f"{self.event_type.value} must not carry node_id"
            raise ValueError(msg)
        if self.event_type is WorkflowLifecycleEventType.EDGE_TRAVERSED and self.edge_id is None:
            msg = "workflow.edge.traversed requires edge_id"
            raise ValueError(msg)
        if (
            self.event_type is not WorkflowLifecycleEventType.EDGE_TRAVERSED
            and self.edge_id is not None
        ):
            msg = f"{self.event_type.value} must not carry edge_id"
            raise ValueError(msg)
        if (
            self.event_type
            in {
                WorkflowLifecycleEventType.NODE_FAILED,
                WorkflowLifecycleEventType.NODE_RETRIED,
                WorkflowLifecycleEventType.RUN_FAILED,
                WorkflowLifecycleEventType.RUN_CANCELLED,
            }
            and self.reason_code is None
        ):
            msg = f"{self.event_type.value} requires reason_code"
            raise ValueError(msg)
        if self.event_type is WorkflowLifecycleEventType.CHECKPOINT_SAVED and not self.refs:
            msg = "workflow.checkpoint.saved requires at least one checkpoint ref"
            raise ValueError(msg)
        if self.event_type in _RUN_EVENT_TYPES and self.attempt is not None:
            msg = f"{self.event_type.value} must not carry a node attempt"
            raise ValueError(msg)
        return self

    @property
    def aggregate_id(self) -> str:
        return self.workflow_id

    def to_event_data(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "schema_version": self.schema_version,
            "workflow_id": self.workflow_id,
            "timestamp": self.timestamp.isoformat(),
        }
        if self.node_id is not None:
            data["node_id"] = self.node_id
        if self.edge_id is not None:
            data["edge_id"] = self.edge_id
        if self.attempt is not None:
            data["attempt"] = self.attempt
        if self.reason_code is not None:
            data["reason_code"] = self.reason_code
        if self.refs:
            data["refs"] = list(self.refs)
        if self.data:
            data["data"] = _thaw_data(self.data)
        return data

    def to_base_event(self) -> BaseEvent:
        return BaseEvent(
            type=self.event_type.value,
            aggregate_type="workflow_ir",
            timestamp=self.timestamp,
            aggregate_id=self.aggregate_id,
            data=self.to_event_data(),
            event_version=self.schema_version,
        )


def lifecycle_event_for_spec(
    spec: WorkflowSpec,
    event_type: WorkflowLifecycleEventType,
    **kwargs: Any,
) -> WorkflowLifecycleEvent:
    """Create a lifecycle event anchored to ``WorkflowSpec.spec_id``."""
    return WorkflowLifecycleEvent(event_type=event_type, workflow_id=spec.spec_id, **kwargs)


def completed_node_ids(events: Iterable[WorkflowLifecycleEvent]) -> frozenset[str]:
    """Return nodes whose effective state is completed."""
    return frozenset(
        node_id
        for node_id, state in effective_node_states(events).items()
        if state is WorkflowNodeLifecycleState.COMPLETED
    )


def effective_node_states(
    events: Iterable[WorkflowLifecycleEvent],
) -> Mapping[str, WorkflowNodeLifecycleState]:
    """Project latest effective node state while preserving failed history in events."""
    states: dict[str, WorkflowNodeLifecycleState] = {}
    for event in sorted(events, key=lambda item: item.timestamp):
        if event.event_type not in _NODE_EVENT_TYPES or event.node_id is None:
            continue
        states[event.node_id] = _NODE_STATE_BY_EVENT[event.event_type]
    return MappingProxyType(states)


def next_runnable_node_ids(
    spec: WorkflowSpec,
    events: Iterable[WorkflowLifecycleEvent],
) -> tuple[str, ...]:
    """Return pending nodes whose incoming dependencies have completed.

    This is intentionally a pure projection helper: it reads the Workflow IR
    graph and lifecycle records only, performs no side effects, and does not
    dispatch work.
    """
    event_list = tuple(event for event in events if event.workflow_id == spec.spec_id)
    states = effective_node_states(event_list)
    completed = {
        node_id
        for node_id, state in states.items()
        if state is WorkflowNodeLifecycleState.COMPLETED
    }
    latest_node_attempts: dict[str, int | None] = {}
    traversed_edge_attempts: dict[str, set[int | None]] = {}
    for event in sorted(event_list, key=lambda item: item.timestamp):
        if event.event_type in _NODE_EVENT_TYPES and event.node_id is not None:
            latest_node_attempts[event.node_id] = event.attempt
        if event.event_type is WorkflowLifecycleEventType.EDGE_TRAVERSED and event.edge_id:
            traversed_edge_attempts.setdefault(event.edge_id, set()).add(event.attempt)
    nodes_by_id: dict[str, WorkflowNode] = {node.node_id: node for node in spec.nodes}
    incoming: dict[str, list[WorkflowEdge]] = {node_id: [] for node_id in nodes_by_id}
    for edge in spec.edges:
        if edge.target in incoming:
            incoming[edge.target].append(edge)

    def dependency_is_satisfied(edge: WorkflowEdge) -> bool:
        if edge.kind is EdgeKind.CONDITIONAL:
            if edge.source not in completed:
                return False
            edge_attempts = traversed_edge_attempts.get(edge.edge_id)
            if not edge_attempts:
                return False
            source_attempt = latest_node_attempts.get(edge.source)
            if source_attempt is None:
                return True
            return source_attempt in edge_attempts
        return edge.source in completed

    runnable: list[str] = []
    for node in spec.nodes:
        state = states.get(node.node_id)
        if state is not None and state is not WorkflowNodeLifecycleState.RETRIED:
            continue
        predecessors = incoming.get(node.node_id, ())
        if all(dependency_is_satisfied(edge) for edge in predecessors):
            runnable.append(node.node_id)
    return tuple(runnable)


__all__ = [
    "MAX_WORKFLOW_LIFECYCLE_DATA_BYTES",
    "WORKFLOW_LIFECYCLE_SCHEMA_VERSION",
    "WorkflowLifecycleEvent",
    "WorkflowLifecycleEventType",
    "WorkflowNodeLifecycleState",
    "WorkflowRunLifecycleState",
    "completed_node_ids",
    "effective_node_states",
    "lifecycle_event_for_spec",
    "next_runnable_node_ids",
]
