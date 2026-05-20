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
from typing import Any, Final, Literal, cast

from pydantic import BaseModel, Field, field_validator, model_validator

from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.workflow_ir import (
    EdgeKind,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
    validate_workflow,
)

WORKFLOW_LIFECYCLE_SCHEMA_VERSION: Final[int] = 1
MAX_WORKFLOW_LIFECYCLE_DATA_BYTES: Final[int] = 8192
MAX_WORKFLOW_LIFECYCLE_REF_COUNT: Final[int] = 16
MAX_WORKFLOW_LIFECYCLE_REF_BYTES: Final[int] = 512
MAX_WORKFLOW_LIFECYCLE_REFS_BYTES: Final[int] = 4096
MAX_WORKFLOW_LIFECYCLE_WORKFLOW_ID_LEN: Final[int] = 256
MAX_WORKFLOW_LIFECYCLE_REASON_CODE_LEN: Final[int] = 1024
MAX_WORKFLOW_LIFECYCLE_REF_LEN_CHARS: Final[int] = 512
_MAX_NESTING_DEPTH: Final[int] = 32

WORKFLOW_LIFECYCLE_AGGREGATE_TYPE: Final[str] = "workflow_ir"
"""EventStore aggregate-type bucket for #956 Workflow IR lifecycle events.

This is the durable event-family anchor that pairs with
``WorkflowLifecycleEvent.to_base_event()``. It is intentionally distinct
from the runtime ``execution`` / ``session`` aggregates so existing event
families are untouched and downstream projections can filter the
lifecycle stream without scanning unrelated families.
"""

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


WORKFLOW_LIFECYCLE_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    member.value for member in WorkflowLifecycleEventType
)
"""Stable set of event-type strings persisted by the lifecycle family."""


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
_TERMINAL_RUN_EVENT_TYPES: Final[frozenset[WorkflowLifecycleEventType]] = frozenset(
    {
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

_EVENT_SORT_ORDER: Final[dict[WorkflowLifecycleEventType, int]] = {
    WorkflowLifecycleEventType.RUN_CREATED: 0,
    WorkflowLifecycleEventType.NODE_SCHEDULED: 10,
    WorkflowLifecycleEventType.NODE_STARTED: 20,
    WorkflowLifecycleEventType.NODE_FAILED: 30,
    WorkflowLifecycleEventType.NODE_RETRIED: 40,
    WorkflowLifecycleEventType.NODE_COMPLETED: 50,
    WorkflowLifecycleEventType.EDGE_TRAVERSED: 60,
    WorkflowLifecycleEventType.CHECKPOINT_SAVED: 70,
    WorkflowLifecycleEventType.RUN_COMPLETED: 80,
    WorkflowLifecycleEventType.RUN_FAILED: 80,
    WorkflowLifecycleEventType.RUN_CANCELLED: 80,
}


def _event_sort_key(
    event: WorkflowLifecycleEvent,
) -> tuple[datetime, int, str, str, str, str, int, str, tuple[str, ...], str]:
    return (
        event.timestamp,
        _EVENT_SORT_ORDER[event.event_type],
        event.event_type.value,
        event.workflow_id,
        event.node_id or "",
        event.edge_id or "",
        event.attempt or 0,
        event.reason_code or "",
        event.refs,
        _canonical_json(event.data),
    )


def _run_boundary_group_order(
    event: WorkflowLifecycleEvent,
    *,
    has_terminal_restart_tie: bool,
    prefer_restart_tie: bool,
) -> tuple[int, str]:
    if not has_terminal_restart_tie or not prefer_restart_tie:
        return (_EVENT_SORT_ORDER[event.event_type], event.event_type.value)
    if event.event_type in _TERMINAL_RUN_EVENT_TYPES:
        return (100, event.event_type.value)
    if event.event_type is WorkflowLifecycleEventType.RUN_CREATED:
        return (101, event.event_type.value)
    return (_EVENT_SORT_ORDER[event.event_type], event.event_type.value)


def _timestamp_groups(
    events: Iterable[WorkflowLifecycleEvent],
) -> tuple[tuple[WorkflowLifecycleEvent, ...], ...]:
    sorted_events = sorted(events, key=lambda event: event.timestamp)
    grouped_events: list[list[WorkflowLifecycleEvent]] = []
    for event in sorted_events:
        if grouped_events and grouped_events[-1][0].timestamp == event.timestamp:
            grouped_events[-1].append(event)
        else:
            grouped_events.append([event])
    return tuple(tuple(group) for group in grouped_events)


def _has_terminal_restart_tie(events: Iterable[WorkflowLifecycleEvent]) -> bool:
    event_tuple = tuple(events)
    return any(event.event_type in _TERMINAL_RUN_EVENT_TYPES for event in event_tuple) and any(
        event.event_type is WorkflowLifecycleEventType.RUN_CREATED for event in event_tuple
    )


def _has_scheduling_event(events: Iterable[WorkflowLifecycleEvent]) -> bool:
    return any(
        event.event_type in _NODE_EVENT_TYPES
        or event.event_type is WorkflowLifecycleEventType.EDGE_TRAVERSED
        or event.event_type is WorkflowLifecycleEventType.CHECKPOINT_SAVED
        for event in events
    )


def _run_lifecycle_segment(
    events: Iterable[WorkflowLifecycleEvent],
) -> tuple[WorkflowRunLifecycleState | None, tuple[WorkflowLifecycleEvent, ...], bool, bool]:
    active_run = False
    terminal_state: WorkflowRunLifecycleState | None = None
    terminal_allows_restart = False
    terminal_timestamp: datetime | None = None
    latest_segment: list[WorkflowLifecycleEvent] = []
    latest_segment_ambiguous = False
    post_terminal_violation = False
    timestamp_group_list = _timestamp_groups(events)
    for timestamp_events in timestamp_group_list:
        active_run_at_timestamp = active_run
        prefer_restart_tie = active_run_at_timestamp
        timestamp_ambiguous = _has_terminal_restart_tie(timestamp_events) and _has_scheduling_event(
            timestamp_events
        )
        for event in sorted(
            timestamp_events,
            key=lambda item: _run_boundary_group_order(
                item,
                has_terminal_restart_tie=_has_terminal_restart_tie(timestamp_events),
                prefer_restart_tie=prefer_restart_tie,
            ),
        ):
            if terminal_state is not None:
                can_restart_after_terminal = (
                    terminal_allows_restart
                    or (terminal_timestamp is not None and event.timestamp > terminal_timestamp)
                    or (prefer_restart_tie and event.timestamp == terminal_timestamp)
                )
                if (
                    event.event_type is WorkflowLifecycleEventType.RUN_CREATED
                    and can_restart_after_terminal
                ):
                    active_run = True
                    terminal_state = None
                    terminal_allows_restart = False
                    terminal_timestamp = None
                    latest_segment = [event]
                    latest_segment_ambiguous = timestamp_ambiguous
                    continue
                latest_segment.append(event)
                latest_segment_ambiguous = latest_segment_ambiguous or timestamp_ambiguous
                post_terminal_violation = True
                continue
            if event.event_type is WorkflowLifecycleEventType.RUN_CREATED:
                if active_run:
                    latest_segment.append(event)
                    latest_segment_ambiguous = latest_segment_ambiguous or timestamp_ambiguous
                    post_terminal_violation = True
                    continue
                active_run = True
                latest_segment = [event]
                latest_segment_ambiguous = timestamp_ambiguous
                continue
            latest_segment.append(event)
            latest_segment_ambiguous = latest_segment_ambiguous or timestamp_ambiguous
            if event.event_type in _TERMINAL_RUN_EVENT_TYPES:
                terminal_state = {
                    WorkflowLifecycleEventType.RUN_COMPLETED: WorkflowRunLifecycleState.COMPLETED,
                    WorkflowLifecycleEventType.RUN_FAILED: WorkflowRunLifecycleState.FAILED,
                    WorkflowLifecycleEventType.RUN_CANCELLED: WorkflowRunLifecycleState.CANCELLED,
                }[event.event_type]
                terminal_allows_restart = active_run
                terminal_timestamp = event.timestamp
                active_run = False
    if terminal_state is not None:
        return (
            terminal_state,
            tuple(latest_segment),
            latest_segment_ambiguous,
            post_terminal_violation,
        )
    if active_run:
        return (
            WorkflowRunLifecycleState.CREATED,
            tuple(latest_segment),
            latest_segment_ambiguous,
            post_terminal_violation,
        )
    return None, tuple(latest_segment), latest_segment_ambiguous, post_terminal_violation


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


def _canonical_json(value: Any) -> str:
    if isinstance(value, Mapping):
        value = {key: json.loads(_canonical_json(item)) for key, item in value.items()}
    elif isinstance(value, tuple):
        value = [json.loads(_canonical_json(item)) for item in value]
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _is_replay_unsafe_ref(ref: str) -> bool:
    normalized = ref.strip().lower()
    tokenized = "".join(character if character.isalnum() else "_" for character in normalized)
    tokens = tuple(token for token in tokenized.split("_") if token)
    candidates = {tokenized, *tokens}
    candidates.update(
        "_".join(tokens[start:end])
        for start in range(len(tokens))
        for end in range(start + 1, len(tokens) + 1)
    )
    return any(_is_replay_unsafe_key(candidate) for candidate in candidates)


def _normalize_json_value(name: str, value: Any, path: str, _depth: int = 0) -> JsonValue:
    if _depth > _MAX_NESTING_DEPTH:
        msg = (
            f"Workflow lifecycle {name} value at {path} exceeds maximum nesting depth "
            f"of {_MAX_NESTING_DEPTH}"
        )
        raise ValueError(msg)
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
            normalized[key] = _normalize_json_value(name, item, f"{path}.{key}", _depth + 1)
        return normalized
    if isinstance(value, list | tuple):
        return [
            _normalize_json_value(name, item, f"{path}[{index}]", _depth + 1)
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
    encoded = _canonical_json(normalized).encode("utf-8")
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
        if index >= MAX_WORKFLOW_LIFECYCLE_REF_COUNT:
            msg = f"Workflow lifecycle refs exceed {MAX_WORKFLOW_LIFECYCLE_REF_COUNT} entries"
            raise ValueError(msg)
        ref = _normalize_non_blank(f"refs[{index}]", value)
        ref_bytes = ref.encode("utf-8")
        if len(ref_bytes) > MAX_WORKFLOW_LIFECYCLE_REF_BYTES:
            msg = f"Workflow lifecycle ref exceeds {MAX_WORKFLOW_LIFECYCLE_REF_BYTES} bytes"
            raise ValueError(msg)
        if _is_replay_unsafe_ref(ref):
            msg = f"Workflow lifecycle refs must not persist replay-unsafe ref {ref!r}"
            raise ValueError(msg)
        if ref in seen:
            msg = f"Workflow lifecycle refs must be unique: {ref!r}"
            raise ValueError(msg)
        seen.add(ref)
        normalized.append(ref)
    encoded = _canonical_json(normalized).encode("utf-8")
    if len(encoded) > MAX_WORKFLOW_LIFECYCLE_REFS_BYTES:
        msg = f"Workflow lifecycle refs exceed {MAX_WORKFLOW_LIFECYCLE_REFS_BYTES} bytes"
        raise ValueError(msg)
    return tuple(normalized)


class WorkflowConformanceIssue(BaseModel, frozen=True):
    """One lifecycle/spec conformance finding."""

    severity: Literal["error", "warning"]
    code: Literal[
        "ambiguous_run_boundary_timestamp",
        "invalid_spec",
        "unknown_node_id",
        "unknown_edge_id",
        "lifecycle_before_run_created",
        "event_after_terminal_run",
        "run_created_before_terminal",
    ]
    message: str
    event_type: WorkflowLifecycleEventType | None = None
    node_id: str | None = None
    edge_id: str | None = None


class WorkflowConformanceReport(BaseModel, frozen=True):
    """Validation report connecting a WorkflowSpec with lifecycle history."""

    workflow_id: str
    ok: bool
    issues: tuple[WorkflowConformanceIssue, ...] = Field(default_factory=tuple)
    event_count: int = 0

    @property
    def errors(self) -> tuple[WorkflowConformanceIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def warnings(self) -> tuple[WorkflowConformanceIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")


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
        normalized = _normalize_non_blank("workflow_id", value)
        if len(normalized) > MAX_WORKFLOW_LIFECYCLE_WORKFLOW_ID_LEN:
            msg = (
                f"Workflow lifecycle workflow_id exceeds "
                f"{MAX_WORKFLOW_LIFECYCLE_WORKFLOW_ID_LEN} characters"
            )
            raise ValueError(msg)
        return normalized

    @field_validator("node_id", "edge_id", "reason_code")
    @classmethod
    def _optional_fields_non_blank(cls, value: str | None, info: Any) -> str | None:
        normalized = _normalize_optional_non_blank(str(info.field_name), value)
        if (
            normalized is not None
            and info.field_name == "reason_code"
            and len(normalized) > MAX_WORKFLOW_LIFECYCLE_REASON_CODE_LEN
        ):
            msg = (
                f"Workflow lifecycle reason_code exceeds "
                f"{MAX_WORKFLOW_LIFECYCLE_REASON_CODE_LEN} characters"
            )
            raise ValueError(msg)
        return normalized

    @field_validator("refs", mode="before")
    @classmethod
    def _coerce_refs(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str) or not isinstance(value, Iterable):
            msg = "Workflow lifecycle refs must be an iterable of strings"
            raise TypeError(msg)
        materialized = tuple(value)
        for index, ref in enumerate(materialized):
            if isinstance(ref, str) and len(ref) > MAX_WORKFLOW_LIFECYCLE_REF_LEN_CHARS:
                msg = (
                    f"Workflow lifecycle refs exceed "
                    f"{MAX_WORKFLOW_LIFECYCLE_REF_LEN_CHARS} characters "
                    f"at index {index}"
                )
                raise ValueError(msg)
        return _normalize_refs(materialized)

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
            aggregate_type=WORKFLOW_LIFECYCLE_AGGREGATE_TYPE,
            timestamp=self.timestamp,
            aggregate_id=self.aggregate_id,
            data=self.to_event_data(),
            event_version=self.schema_version,
        )

    @classmethod
    def from_base_event(cls, base: BaseEvent) -> WorkflowLifecycleEvent:
        """Rehydrate a lifecycle event from a persisted ``BaseEvent``.

        Foreign event families are rejected so callers can safely pass a
        mixed EventStore replay and surface a clear error if a row is
        misrouted.
        """
        if base.aggregate_type != WORKFLOW_LIFECYCLE_AGGREGATE_TYPE:
            msg = (
                "BaseEvent is not from the workflow lifecycle family: "
                f"aggregate_type={base.aggregate_type!r}"
            )
            raise ValueError(msg)
        if base.type not in WORKFLOW_LIFECYCLE_EVENT_TYPES:
            msg = f"BaseEvent type {base.type!r} is not a workflow lifecycle event type"
            raise ValueError(msg)

        payload = dict(base.data)
        payload.pop("workflow_id", None)
        payload.pop("schema_version", None)
        payload.pop("timestamp", None)
        timestamp = base.timestamp
        if isinstance(timestamp, datetime) and (
            timestamp.tzinfo is None or timestamp.utcoffset() is None
        ):
            # ``BaseEvent`` is created from EventStore rows whose SQLite
            # backend drops tzinfo on roundtrip. Lifecycle events are
            # canonically UTC, so reattach UTC before re-validating.
            timestamp = timestamp.replace(tzinfo=UTC)
        kwargs: dict[str, Any] = {
            "event_type": WorkflowLifecycleEventType(base.type),
            "workflow_id": base.aggregate_id,
            "schema_version": base.event_version or WORKFLOW_LIFECYCLE_SCHEMA_VERSION,
            "timestamp": timestamp,
        }
        for key in ("node_id", "edge_id", "attempt", "reason_code"):
            if key in payload:
                kwargs[key] = payload[key]
        refs = payload.get("refs")
        if refs is not None:
            kwargs["refs"] = tuple(refs) if not isinstance(refs, tuple) else refs
        if "data" in payload and payload["data"] is not None:
            kwargs["data"] = payload["data"]
        return cls(**kwargs)


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
    for event in sorted(events, key=_event_sort_key):
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
    (
        latest_run_state,
        latest_run_events,
        latest_run_ambiguous,
        post_terminal_violation,
    ) = _run_lifecycle_segment(event_list)
    if latest_run_ambiguous or post_terminal_violation:
        return ()
    if latest_run_state in {
        WorkflowRunLifecycleState.COMPLETED,
        WorkflowRunLifecycleState.FAILED,
        WorkflowRunLifecycleState.CANCELLED,
    }:
        return ()

    states = effective_node_states(latest_run_events)
    completed = {
        node_id
        for node_id, state in states.items()
        if state is WorkflowNodeLifecycleState.COMPLETED
    }
    latest_node_attempts: dict[str, int | None] = {}
    latest_node_completed_at: dict[str, datetime] = {}
    traversed_edge_events: dict[str, list[WorkflowLifecycleEvent]] = {}
    for event in sorted(latest_run_events, key=_event_sort_key):
        if event.event_type in _NODE_EVENT_TYPES and event.node_id is not None:
            latest_node_attempts[event.node_id] = event.attempt
            if event.event_type is WorkflowLifecycleEventType.NODE_COMPLETED:
                latest_node_completed_at[event.node_id] = event.timestamp
        if event.event_type is WorkflowLifecycleEventType.EDGE_TRAVERSED and event.edge_id:
            traversed_edge_events.setdefault(event.edge_id, []).append(event)
    nodes_by_id: dict[str, WorkflowNode] = {node.node_id: node for node in spec.nodes}
    incoming: dict[str, list[WorkflowEdge]] = {node_id: [] for node_id in nodes_by_id}
    for edge in spec.edges:
        if edge.target in incoming:
            incoming[edge.target].append(edge)

    def dependency_is_satisfied(edge: WorkflowEdge) -> bool:
        if edge.kind is EdgeKind.CONDITIONAL:
            if edge.source not in completed:
                return False
            source_completed_at = latest_node_completed_at.get(edge.source)
            if source_completed_at is None:
                return False
            source_attempt = latest_node_attempts.get(edge.source)
            for traversal in traversed_edge_events.get(edge.edge_id, ()):
                if traversal.timestamp < source_completed_at:
                    continue
                if source_attempt is not None and traversal.attempt != source_attempt:
                    continue
                return True
            return False
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


def validate_workflow_lifecycle_conformance(
    spec: WorkflowSpec,
    events: Iterable[WorkflowLifecycleEvent],
) -> WorkflowConformanceReport:
    """Validate lifecycle events against the Workflow IR graph.

    This is a pure conformance read: it does not dispatch nodes, mutate the
    workflow, or persist state. It rejects lifecycle rows that point at unknown
    node/edge ids, flags node events before ``workflow.run.created``, and flags
    history appended after a terminal run event. Foreign workflow ids are ignored
    so callers may pass a mixed EventStore replay safely.
    """

    event_list = tuple(event for event in events if event.workflow_id == spec.spec_id)
    issues: list[WorkflowConformanceIssue] = []

    validation = validate_workflow(spec)
    for error in validation.errors:
        issues.append(
            WorkflowConformanceIssue(
                severity="error",
                code="invalid_spec",
                message=error.message,
                node_id=error.node_id,
                edge_id=error.edge_id,
            )
        )
    for warning in validation.warnings:
        issues.append(
            WorkflowConformanceIssue(
                severity="warning",
                code="invalid_spec",
                message=warning.message,
                node_id=warning.node_id,
                edge_id=warning.edge_id,
            )
        )

    node_ids = {node.node_id for node in spec.nodes}
    edge_ids = {edge.edge_id for edge in spec.edges}
    seen_run_created = False
    terminal_seen = False
    terminal_seen_at: datetime | None = None
    active_run = False
    terminal_allows_restart = False
    timestamp_group_list = _timestamp_groups(event_list)
    for timestamp_events in timestamp_group_list:
        active_run_at_timestamp = active_run
        prefer_restart_tie = active_run_at_timestamp
        if _has_terminal_restart_tie(timestamp_events) and _has_scheduling_event(timestamp_events):
            issues.append(
                WorkflowConformanceIssue(
                    severity="error",
                    code="ambiguous_run_boundary_timestamp",
                    message=(
                        "Timestamp contains a terminal run event, workflow.run.created, "
                        "and node/edge/checkpoint lifecycle rows; add a distinct timestamp "
                        "or run id before validating restart conformance."
                    ),
                )
            )

        for event in sorted(
            timestamp_events,
            key=lambda item: _run_boundary_group_order(
                item,
                has_terminal_restart_tie=_has_terminal_restart_tie(timestamp_events),
                prefer_restart_tie=prefer_restart_tie,
            ),
        ):
            if terminal_seen:
                can_restart_after_terminal = (
                    terminal_allows_restart
                    or (terminal_seen_at is not None and event.timestamp > terminal_seen_at)
                    or (prefer_restart_tie and event.timestamp == terminal_seen_at)
                )
                if (
                    event.event_type is WorkflowLifecycleEventType.RUN_CREATED
                    and can_restart_after_terminal
                ):
                    terminal_seen = False
                    terminal_seen_at = None
                    terminal_allows_restart = False
                    active_run = True
                    seen_run_created = True
                    continue
                if event.event_type in _NODE_EVENT_TYPES and event.node_id not in node_ids:
                    issues.append(
                        WorkflowConformanceIssue(
                            severity="error",
                            code="unknown_node_id",
                            message=f"Lifecycle event references unknown node id {event.node_id!r}.",
                            event_type=event.event_type,
                            node_id=event.node_id,
                        )
                    )
                if (
                    event.event_type is WorkflowLifecycleEventType.EDGE_TRAVERSED
                    and event.edge_id not in edge_ids
                ):
                    issues.append(
                        WorkflowConformanceIssue(
                            severity="error",
                            code="unknown_edge_id",
                            message=f"Lifecycle event references unknown edge id {event.edge_id!r}.",
                            event_type=event.event_type,
                            edge_id=event.edge_id,
                        )
                    )
                issues.append(
                    WorkflowConformanceIssue(
                        severity="error",
                        code="event_after_terminal_run",
                        message=(
                            "Workflow lifecycle event appears after a terminal run event "
                            "without a new workflow.run.created boundary."
                        ),
                        event_type=event.event_type,
                        node_id=event.node_id,
                        edge_id=event.edge_id,
                    )
                )
                continue

            if event.event_type is WorkflowLifecycleEventType.RUN_CREATED:
                if active_run:
                    issues.append(
                        WorkflowConformanceIssue(
                            severity="error",
                            code="run_created_before_terminal",
                            message=(
                                "workflow.run.created appears before the active run reached "
                                "a terminal event."
                            ),
                            event_type=event.event_type,
                        )
                    )
                    continue
                seen_run_created = True
                active_run = True

            if event.event_type in _NODE_EVENT_TYPES and event.node_id is not None:
                if event.node_id not in node_ids:
                    issues.append(
                        WorkflowConformanceIssue(
                            severity="error",
                            code="unknown_node_id",
                            message=f"Lifecycle event references unknown node id {event.node_id!r}.",
                            event_type=event.event_type,
                            node_id=event.node_id,
                        )
                    )
                if not seen_run_created:
                    issues.append(
                        WorkflowConformanceIssue(
                            severity="warning",
                            code="lifecycle_before_run_created",
                            message="Node lifecycle event appears before workflow.run.created.",
                            event_type=event.event_type,
                            node_id=event.node_id,
                        )
                    )

            if event.event_type is WorkflowLifecycleEventType.EDGE_TRAVERSED and event.edge_id:
                if event.edge_id not in edge_ids:
                    issues.append(
                        WorkflowConformanceIssue(
                            severity="error",
                            code="unknown_edge_id",
                            message=f"Lifecycle event references unknown edge id {event.edge_id!r}.",
                            event_type=event.event_type,
                            edge_id=event.edge_id,
                        )
                    )
                if not seen_run_created:
                    issues.append(
                        WorkflowConformanceIssue(
                            severity="warning",
                            code="lifecycle_before_run_created",
                            message="Edge traversal event appears before workflow.run.created.",
                            event_type=event.event_type,
                            edge_id=event.edge_id,
                        )
                    )

            if event.event_type in _TERMINAL_RUN_EVENT_TYPES:
                terminal_seen = True
                terminal_seen_at = event.timestamp
                terminal_allows_restart = active_run
                active_run = False

    return WorkflowConformanceReport(
        workflow_id=spec.spec_id,
        ok=not any(issue.severity == "error" for issue in issues),
        issues=tuple(issues),
        event_count=len(event_list),
    )


class WorkflowEdgeTraversalRecord(BaseModel, frozen=True):
    """Projected record of a single edge traversal."""

    edge_id: str
    attempt: int | None = None
    timestamp: datetime


class WorkflowCheckpointRecord(BaseModel, frozen=True):
    """Projected record of a single checkpoint save."""

    refs: tuple[str, ...]
    timestamp: datetime


class WorkflowLifecycleProjection(BaseModel, frozen=True):
    """Deterministic projection of a lifecycle event slice.

    The projection is intentionally bounded: it carries no raw payload,
    only durable, replay-safe identifiers (``ControlContract`` /
    ``CheckpointStore`` / ``IOJournal`` refs already live on
    :class:`WorkflowLifecycleEvent`). Two replays of the same event slice
    produce the same projection, modulo input ordering — see
    :func:`project_workflow_lifecycle`.
    """

    workflow_id: str
    run_state: WorkflowRunLifecycleState | None = None
    terminal_reason_code: str | None = None
    node_states: Mapping[str, WorkflowNodeLifecycleState] = Field(default_factory=dict)
    node_attempts: Mapping[str, int] = Field(default_factory=dict)
    traversed_edges: tuple[WorkflowEdgeTraversalRecord, ...] = Field(default_factory=tuple)
    checkpoints: tuple[WorkflowCheckpointRecord, ...] = Field(default_factory=tuple)
    event_count: int = 0

    def is_terminal(self) -> bool:
        return self.run_state in {
            WorkflowRunLifecycleState.COMPLETED,
            WorkflowRunLifecycleState.FAILED,
            WorkflowRunLifecycleState.CANCELLED,
        }


def project_workflow_lifecycle(
    workflow_id: str,
    events: Iterable[WorkflowLifecycleEvent],
) -> WorkflowLifecycleProjection:
    """Deterministically project a lifecycle event slice into a summary.

    The projection rebuilds the same lifecycle slice regardless of input
    iteration order: events are filtered to ``workflow_id`` and sorted by
    the same canonical key used elsewhere in this module.
    """
    workflow_id = _normalize_non_blank("workflow_id", workflow_id)
    scoped = tuple(
        sorted(
            (event for event in events if event.workflow_id == workflow_id),
            key=_event_sort_key,
        )
    )

    node_states: dict[str, WorkflowNodeLifecycleState] = {}
    node_attempts: dict[str, int] = {}
    traversed_edges: list[WorkflowEdgeTraversalRecord] = []
    checkpoints: list[WorkflowCheckpointRecord] = []
    run_state: WorkflowRunLifecycleState | None = None
    terminal_reason_code: str | None = None

    for event in scoped:
        if event.event_type is WorkflowLifecycleEventType.RUN_CREATED:
            # Fresh run boundary resets the projection so a replay of a
            # restart segment converges on the latest run only.
            node_states = {}
            node_attempts = {}
            traversed_edges = []
            checkpoints = []
            run_state = WorkflowRunLifecycleState.CREATED
            terminal_reason_code = None
            continue
        if event.event_type is WorkflowLifecycleEventType.RUN_COMPLETED:
            run_state = WorkflowRunLifecycleState.COMPLETED
            terminal_reason_code = event.reason_code
            continue
        if event.event_type is WorkflowLifecycleEventType.RUN_FAILED:
            run_state = WorkflowRunLifecycleState.FAILED
            terminal_reason_code = event.reason_code
            continue
        if event.event_type is WorkflowLifecycleEventType.RUN_CANCELLED:
            run_state = WorkflowRunLifecycleState.CANCELLED
            terminal_reason_code = event.reason_code
            continue
        if event.event_type in _NODE_EVENT_TYPES and event.node_id is not None:
            node_states[event.node_id] = _NODE_STATE_BY_EVENT[event.event_type]
            if event.attempt is not None:
                node_attempts[event.node_id] = event.attempt
            continue
        if event.event_type is WorkflowLifecycleEventType.EDGE_TRAVERSED and event.edge_id:
            traversed_edges.append(
                WorkflowEdgeTraversalRecord(
                    edge_id=event.edge_id,
                    attempt=event.attempt,
                    timestamp=event.timestamp,
                )
            )
            continue
        if event.event_type is WorkflowLifecycleEventType.CHECKPOINT_SAVED:
            checkpoints.append(
                WorkflowCheckpointRecord(
                    refs=event.refs,
                    timestamp=event.timestamp,
                )
            )
            continue

    return WorkflowLifecycleProjection(
        workflow_id=workflow_id,
        run_state=run_state,
        terminal_reason_code=terminal_reason_code,
        node_states=MappingProxyType(dict(node_states)),
        node_attempts=MappingProxyType(dict(node_attempts)),
        traversed_edges=tuple(traversed_edges),
        checkpoints=tuple(checkpoints),
        event_count=len(scoped),
    )


__all__ = [
    "MAX_WORKFLOW_LIFECYCLE_DATA_BYTES",
    "MAX_WORKFLOW_LIFECYCLE_REASON_CODE_LEN",
    "MAX_WORKFLOW_LIFECYCLE_REF_BYTES",
    "MAX_WORKFLOW_LIFECYCLE_REF_COUNT",
    "MAX_WORKFLOW_LIFECYCLE_REF_LEN_CHARS",
    "MAX_WORKFLOW_LIFECYCLE_REFS_BYTES",
    "MAX_WORKFLOW_LIFECYCLE_WORKFLOW_ID_LEN",
    "WORKFLOW_LIFECYCLE_AGGREGATE_TYPE",
    "WORKFLOW_LIFECYCLE_EVENT_TYPES",
    "WORKFLOW_LIFECYCLE_SCHEMA_VERSION",
    "WorkflowCheckpointRecord",
    "WorkflowConformanceIssue",
    "WorkflowConformanceReport",
    "WorkflowEdgeTraversalRecord",
    "WorkflowLifecycleEvent",
    "WorkflowLifecycleEventType",
    "WorkflowLifecycleProjection",
    "WorkflowNodeLifecycleState",
    "WorkflowRunLifecycleState",
    "completed_node_ids",
    "effective_node_states",
    "lifecycle_event_for_spec",
    "next_runnable_node_ids",
    "project_workflow_lifecycle",
    "validate_workflow_lifecycle_conformance",
]
