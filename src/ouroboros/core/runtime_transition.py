"""Runtime transition validation contract for long-running flows.

The #925 reliability surface needs a small boundary object that can be
checked before mutating MCP/auto/plugin/job state. This module is pure and
additive: callers pass the current state/revision plus the transition they
intend to apply, and receive an accepted/rejected result with retryable vs
terminal/blocking classification.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import json
import re
from types import MappingProxyType
from typing import Any, Final, cast

RUNTIME_TRANSITION_SCHEMA_VERSION: Final[int] = 1
MAX_RUNTIME_TRANSITION_PAYLOAD_BYTES: Final[int] = 8192
_CONCRETE_JOB_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^job-[A-Za-z0-9][A-Za-z0-9_.:-]*$")

_SECRET_KEYS: Final[frozenset[str]] = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth_token",
        "bearer_token",
        "client_secret",
        "credential",
        "credentials",
        "id_token",
        "passwd",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "token",
    }
)
_SECRET_SUFFIXES: Final[tuple[str, ...]] = (
    "_api_key",
    "_credential",
    "_credentials",
    "_passwd",
    "_password",
    "_secret",
    "_token",
)

JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | dict[str, "JsonValue"] | list["JsonValue"]
FrozenJsonValue = JsonScalar | Mapping[str, "FrozenJsonValue"] | tuple["FrozenJsonValue", ...]


class RuntimeScope(StrEnum):
    """Runtime surfaces covered by the #925 transition boundary."""

    AUTO = "auto"
    RALPH = "ralph"
    TEAM = "team"
    PLUGIN = "plugin"
    MCP_JOB = "mcp_job"
    HARNESS_RUNNER = "harness_runner"


class RuntimeTransitionActor(StrEnum):
    """Who requested a runtime state transition."""

    HARNESS = "harness"
    MCP_CLIENT = "mcp_client"
    PLUGIN = "plugin"
    USER = "user"
    SYSTEM = "system"


class RuntimeTransitionDecision(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class RuntimeFailureClass(StrEnum):
    """User-facing recovery class for rejected transitions."""

    RETRYABLE = "retryable"
    BLOCKING = "blocking"
    TERMINAL = "terminal"


class RuntimeTransitionFailureKind(StrEnum):
    STALE_REVISION = "stale_revision"
    INVALID_STATE = "invalid_state"
    TERMINAL_STATE = "terminal_state"
    MISSING_EVIDENCE = "missing_evidence"
    INVALID_SCOPE = "invalid_scope"


def _require_non_blank(name: str, value: str) -> str:
    if not isinstance(value, str):
        msg = f"RuntimeTransition {name} must be a string"
        raise TypeError(msg)
    normalized = value.strip()
    if not normalized:
        msg = f"RuntimeTransition {name} must be non-blank"
        raise ValueError(msg)
    return normalized


def _normalize_utc(name: str, value: datetime) -> datetime:
    if not isinstance(value, datetime):
        msg = f"RuntimeTransition {name} must be a datetime"
        raise TypeError(msg)
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"RuntimeTransition {name} must be timezone-aware"
        raise ValueError(msg)
    return value.astimezone(UTC)


def _is_secret_key(value: str) -> bool:
    camel_split = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value.strip())
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", camel_split)
    normalized = (
        camel_split.lower().replace("-", "_").replace(" ", "_").replace(".", "_").replace("/", "_")
    )
    return normalized in _SECRET_KEYS or normalized.endswith(_SECRET_SUFFIXES)


def _normalize_json_value(name: str, value: Any, path: str) -> JsonValue:
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        # json.dumps(... allow_nan=False) raises for NaN/Infinity.
        json.dumps(value, allow_nan=False)
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                msg = f"RuntimeTransition {name} key at {path} must be a string"
                raise TypeError(msg)
            if _is_secret_key(key):
                msg = f"RuntimeTransition {name} must not persist secret-like key {key!r}"
                raise ValueError(msg)
            normalized[key] = _normalize_json_value(name, item, f"{path}.{key}")
        return normalized
    if isinstance(value, list | tuple):
        return [
            _normalize_json_value(name, item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    msg = f"RuntimeTransition {name} value at {path} must be JSON serializable"
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


def _normalize_payload(name: str, value: Mapping[str, Any]) -> Mapping[str, FrozenJsonValue]:
    if not isinstance(value, Mapping):
        msg = f"RuntimeTransition {name} must be a mapping"
        raise TypeError(msg)
    normalized = _normalize_json_value(name, value, name)
    encoded = json.dumps(normalized, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_RUNTIME_TRANSITION_PAYLOAD_BYTES:
        msg = f"RuntimeTransition {name} exceeds {MAX_RUNTIME_TRANSITION_PAYLOAD_BYTES} bytes"
        raise ValueError(msg)
    return cast(Mapping[str, FrozenJsonValue], _freeze_json_value(normalized))


def _payload_to_event_data(value: Mapping[str, FrozenJsonValue]) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], _thaw_json_value(value))


def _normalize_refs(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str | bytes):
        msg = "RuntimeTransition evidence_refs must be an iterable of strings, not a string"
        raise TypeError(msg)
    normalized: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        ref = _require_non_blank(f"evidence_refs[{index}]", value)
        if ref in seen:
            msg = f"RuntimeTransition evidence_refs must be unique: {ref!r}"
            raise ValueError(msg)
        seen.add(ref)
        normalized.append(ref)
    return tuple(normalized)


def _looks_like_concrete_job_id(value: object) -> bool:
    return isinstance(value, str) and _CONCRETE_JOB_ID_PATTERN.fullmatch(value) is not None


def _plugin_reuses_concrete_job_id(transition: RuntimeTransition) -> bool:
    if transition.runtime_scope is not RuntimeScope.PLUGIN:
        return False
    if _looks_like_concrete_job_id(transition.subject_id):
        return True
    return _looks_like_concrete_job_id(transition.metadata.get("job_id"))


@dataclass(frozen=True, slots=True)
class RuntimeTransition:
    """Intent to move one runtime subject from one state to another."""

    runtime_scope: RuntimeScope
    subject_id: str
    from_state: str
    to_state: str
    reason: str
    actor: RuntimeTransitionActor
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    evidence_refs: tuple[str, ...] = ()
    expected_revision: int | None = None
    idempotency_key: str | None = None
    metadata: Mapping[str, FrozenJsonValue] = field(default_factory=dict)
    schema_version: int = RUNTIME_TRANSITION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_scope, RuntimeScope):
            raise TypeError("RuntimeTransition runtime_scope must be a RuntimeScope")
        if not isinstance(self.actor, RuntimeTransitionActor):
            raise TypeError("RuntimeTransition actor must be a RuntimeTransitionActor")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("RuntimeTransition schema_version must be a positive integer")
        for field_name in ("subject_id", "from_state", "to_state", "reason"):
            object.__setattr__(
                self, field_name, _require_non_blank(field_name, getattr(self, field_name))
            )
        if self.from_state == self.to_state:
            raise ValueError("RuntimeTransition from_state and to_state must differ")
        if self.expected_revision is not None:
            if type(self.expected_revision) is not int:
                raise TypeError("RuntimeTransition expected_revision must be an int")
            if self.expected_revision < 0:
                raise ValueError("RuntimeTransition expected_revision must be >= 0")
        if self.idempotency_key is not None:
            object.__setattr__(
                self, "idempotency_key", _require_non_blank("idempotency_key", self.idempotency_key)
            )
        object.__setattr__(self, "timestamp", _normalize_utc("timestamp", self.timestamp))
        object.__setattr__(self, "evidence_refs", _normalize_refs(self.evidence_refs))
        object.__setattr__(self, "metadata", _normalize_payload("metadata", self.metadata))

    @property
    def aggregate_id(self) -> str:
        return f"{self.runtime_scope.value}:{self.subject_id}"

    def to_event_data(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "schema_version": self.schema_version,
            "runtime_scope": self.runtime_scope.value,
            "subject_id": self.subject_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "reason": self.reason,
            "actor": self.actor.value,
            "timestamp": self.timestamp.isoformat(),
        }
        if self.evidence_refs:
            data["evidence_refs"] = list(self.evidence_refs)
        if self.expected_revision is not None:
            data["expected_revision"] = self.expected_revision
        if self.idempotency_key is not None:
            data["idempotency_key"] = self.idempotency_key
        if self.metadata:
            data["metadata"] = _payload_to_event_data(self.metadata)
        return data


@dataclass(frozen=True, slots=True)
class RuntimeTransitionResult:
    """Accepted/rejected result for a requested runtime transition."""

    transition: RuntimeTransition
    decision: RuntimeTransitionDecision
    failure_class: RuntimeFailureClass | None = None
    failure_kind: RuntimeTransitionFailureKind | None = None
    message: str = ""
    current_revision: int | None = None
    current_state: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.transition, RuntimeTransition):
            raise TypeError("RuntimeTransitionResult transition must be a RuntimeTransition")
        if not isinstance(self.decision, RuntimeTransitionDecision):
            raise TypeError("RuntimeTransitionResult decision must be a RuntimeTransitionDecision")
        if self.decision is RuntimeTransitionDecision.ACCEPTED:
            if self.failure_class is not None or self.failure_kind is not None:
                raise ValueError("accepted RuntimeTransitionResult must not carry failure data")
        else:
            if self.failure_class is None or self.failure_kind is None:
                raise ValueError("rejected RuntimeTransitionResult requires failure data")
            if not isinstance(self.failure_class, RuntimeFailureClass):
                raise TypeError(
                    "RuntimeTransitionResult failure_class must be a RuntimeFailureClass"
                )
            if not isinstance(self.failure_kind, RuntimeTransitionFailureKind):
                raise TypeError(
                    "RuntimeTransitionResult failure_kind must be a RuntimeTransitionFailureKind"
                )
        if self.message:
            object.__setattr__(self, "message", _require_non_blank("message", self.message))
        if self.current_revision is not None:
            if type(self.current_revision) is not int:
                raise TypeError("RuntimeTransitionResult current_revision must be an int")
            if self.current_revision < 0:
                raise ValueError("RuntimeTransitionResult current_revision must be >= 0")
        if self.current_state is not None:
            object.__setattr__(
                self, "current_state", _require_non_blank("current_state", self.current_state)
            )

    @property
    def accepted(self) -> bool:
        return self.decision is RuntimeTransitionDecision.ACCEPTED

    def to_event_data(self) -> dict[str, JsonValue]:
        data = self.transition.to_event_data()
        data["decision"] = self.decision.value
        if self.failure_class is not None:
            data["failure_class"] = self.failure_class.value
        if self.failure_kind is not None:
            data["failure_kind"] = self.failure_kind.value
        if self.message:
            data["message"] = self.message
        if self.current_revision is not None:
            data["current_revision"] = self.current_revision
        if self.current_state is not None:
            data["current_state"] = self.current_state
        return data


def evaluate_runtime_transition(
    transition: RuntimeTransition,
    *,
    current_state: str,
    allowed_transitions: Iterable[tuple[str, str]],
    terminal_states: Iterable[str] = (),
    current_revision: int | None = None,
    require_evidence: bool = False,
) -> RuntimeTransitionResult:
    """Validate a transition request without mutating runtime state."""
    normalized_current = _require_non_blank("current_state", current_state)
    if current_revision is not None and type(current_revision) is not int:
        return RuntimeTransitionResult(
            transition=transition,
            decision=RuntimeTransitionDecision.REJECTED,
            failure_class=RuntimeFailureClass.RETRYABLE,
            failure_kind=RuntimeTransitionFailureKind.STALE_REVISION,
            message="current_revision must be an int; reload the latest runtime snapshot",
            current_state=normalized_current,
        )
    if current_revision is not None and current_revision < 0:
        return RuntimeTransitionResult(
            transition=transition,
            decision=RuntimeTransitionDecision.REJECTED,
            failure_class=RuntimeFailureClass.RETRYABLE,
            failure_kind=RuntimeTransitionFailureKind.STALE_REVISION,
            message="current_revision must be >= 0; reload the latest runtime snapshot",
            current_state=normalized_current,
        )
    if transition.expected_revision is not None:
        if current_revision is None:
            return RuntimeTransitionResult(
                transition=transition,
                decision=RuntimeTransitionDecision.REJECTED,
                failure_class=RuntimeFailureClass.RETRYABLE,
                failure_kind=RuntimeTransitionFailureKind.STALE_REVISION,
                message=(
                    "transition expected_revision requires current_revision; reload the "
                    "latest runtime snapshot before retrying"
                ),
                current_revision=current_revision,
                current_state=normalized_current,
            )
        if transition.expected_revision != current_revision:
            return RuntimeTransitionResult(
                transition=transition,
                decision=RuntimeTransitionDecision.REJECTED,
                failure_class=RuntimeFailureClass.RETRYABLE,
                failure_kind=RuntimeTransitionFailureKind.STALE_REVISION,
                message="transition expected_revision is stale; retry with the latest revision",
                current_revision=current_revision,
                current_state=normalized_current,
            )
    if isinstance(terminal_states, str | bytes):
        raise TypeError("RuntimeTransition terminal_states must be an iterable of strings")
    terminal_set = {_require_non_blank("terminal_state", state) for state in terminal_states}
    if _plugin_reuses_concrete_job_id(transition):
        return RuntimeTransitionResult(
            transition=transition,
            decision=RuntimeTransitionDecision.REJECTED,
            failure_class=RuntimeFailureClass.BLOCKING,
            failure_kind=RuntimeTransitionFailureKind.INVALID_SCOPE,
            message=(
                "plugin runtime transitions must use plugin child-session subjects, "
                "not concrete JobManager job ids"
            ),
            current_revision=current_revision,
            current_state=normalized_current,
        )
    if normalized_current in terminal_set:
        return RuntimeTransitionResult(
            transition=transition,
            decision=RuntimeTransitionDecision.REJECTED,
            failure_class=RuntimeFailureClass.TERMINAL,
            failure_kind=RuntimeTransitionFailureKind.TERMINAL_STATE,
            message="current state is terminal and cannot transition further",
            current_revision=current_revision,
            current_state=normalized_current,
        )
    if transition.from_state != normalized_current:
        return RuntimeTransitionResult(
            transition=transition,
            decision=RuntimeTransitionDecision.REJECTED,
            failure_class=RuntimeFailureClass.RETRYABLE,
            failure_kind=RuntimeTransitionFailureKind.INVALID_STATE,
            message=(
                "transition from_state does not match current_state; reload the latest "
                "runtime snapshot before retrying"
            ),
            current_revision=current_revision,
            current_state=normalized_current,
        )
    allowed = {
        (_require_non_blank("from_state", source), _require_non_blank("to_state", target))
        for source, target in allowed_transitions
    }
    if (transition.from_state, transition.to_state) not in allowed:
        return RuntimeTransitionResult(
            transition=transition,
            decision=RuntimeTransitionDecision.REJECTED,
            failure_class=RuntimeFailureClass.BLOCKING,
            failure_kind=RuntimeTransitionFailureKind.INVALID_SCOPE,
            message="transition is not allowed for this runtime scope",
            current_revision=current_revision,
            current_state=normalized_current,
        )
    if (require_evidence or transition.to_state in terminal_set) and not transition.evidence_refs:
        return RuntimeTransitionResult(
            transition=transition,
            decision=RuntimeTransitionDecision.REJECTED,
            failure_class=RuntimeFailureClass.BLOCKING,
            failure_kind=RuntimeTransitionFailureKind.MISSING_EVIDENCE,
            message="transition requires at least one evidence reference",
            current_revision=current_revision,
            current_state=normalized_current,
        )
    return RuntimeTransitionResult(
        transition=transition,
        decision=RuntimeTransitionDecision.ACCEPTED,
        current_revision=current_revision,
        current_state=normalized_current,
    )


__all__ = [
    "MAX_RUNTIME_TRANSITION_PAYLOAD_BYTES",
    "RUNTIME_TRANSITION_SCHEMA_VERSION",
    "RuntimeFailureClass",
    "RuntimeScope",
    "RuntimeTransition",
    "RuntimeTransitionActor",
    "RuntimeTransitionDecision",
    "RuntimeTransitionFailureKind",
    "RuntimeTransitionResult",
    "evaluate_runtime_transition",
]
