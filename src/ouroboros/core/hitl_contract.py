"""Typed human-in-the-loop contracts for WAIT/RESUME flows.

The HITL contract is the stable payload shape behind ``hitl.*`` events.
It intentionally models request/response correlation and persistence-safe
payloads without choosing a renderer (CLI, MCP, TUI, or structured question).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import InitVar, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import json
from types import MappingProxyType
from typing import Any, ClassVar, Self, cast

HITL_CONTRACT_SCHEMA_VERSION = 1
MAX_HITL_PAYLOAD_BYTES = 8192
_SECRET_KEY_NAMES = frozenset(
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
_SECRET_KEY_SUFFIXES = (
    "_api_key",
    "_credential",
    "_credentials",
    "_passwd",
    "_password",
    "_secret",
    "_token",
)

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | dict[str, JsonValue] | list[JsonValue]
type FrozenJsonValue = JsonScalar | Mapping[str, FrozenJsonValue] | tuple[FrozenJsonValue, ...]


class HumanInputKind(StrEnum):
    FREE_TEXT = "free_text"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    APPROVAL = "approval"
    DESTRUCTIVE_CONFIRMATION = "destructive_confirmation"


class HumanInputSource(StrEnum):
    INTERVIEW = "interview"
    PLUGIN_FIREWALL = "plugin_firewall"
    CONTROL_PLANE = "control_plane"
    PLAN_APPROVAL = "plan_approval"
    RUNTIME_POLICY = "runtime_policy"


class HumanInputRiskClass(StrEnum):
    LOW = "low"
    MATERIAL_BRANCH = "material_branch"
    CREDENTIAL_GATED = "credential_gated"
    EXTERNAL_PRODUCTION = "external_production"
    DESTRUCTIVE = "destructive"


class HumanInputTimeoutAction(StrEnum):
    STAY_WAITING = "stay_waiting"
    CANCEL = "cancel"
    EXPIRE_BLOCKED = "expire_blocked"


class HumanInputResponseKind(StrEnum):
    TEXT = "text"
    SELECTION = "selection"
    APPROVAL = "approval"
    CANCEL = "cancel"
    TIMEOUT = "timeout"


def _require_non_empty(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"HumanInput {name} must be non-empty")
    return normalized


def _require_non_blank_text(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"HumanInput {name} must be a string")
    if not value.strip():
        raise ValueError(f"HumanInput {name} must be non-empty")
    return value


def _normalize_string_tuple(name: str, value: Any) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, tuple | list):
        raise TypeError(f"HumanInput {name} must be a tuple or list of strings")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"HumanInput {name} entries must be strings")
        normalized_item = _require_non_empty(name, item)
        if normalized_item in seen:
            raise ValueError(f"HumanInput {name} entries must be unique after trimming")
        seen.add(normalized_item)
        normalized.append(normalized_item)
    return tuple(normalized)


def _normalize_utc_datetime(name: str, value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"HumanInput {name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"HumanInput {name} must be timezone-aware")
    return value.astimezone(UTC)


def _is_secret_like_key(value: str) -> bool:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized in _SECRET_KEY_NAMES or normalized.endswith(_SECRET_KEY_SUFFIXES)


def _normalize_json_value(name: str, value: Any, path: str) -> JsonValue:
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"HumanInput {name} key at {path} must be a string")
            if _is_secret_like_key(key):
                raise ValueError(f"HumanInput {name} must not persist secret-like content")
            normalized[key] = _normalize_json_value(name, item, f"{path}.{key}")
        return normalized
    if isinstance(value, list | tuple):
        return [
            _normalize_json_value(name, item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(f"HumanInput {name} value at {path} must be JSON serializable")


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


def _ensure_json_safe_payload(name: str, value: Mapping[str, Any]) -> Mapping[str, FrozenJsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError(f"HumanInput {name} must be a mapping")
    normalized = _normalize_json_value(name, value, name)
    encoded = json.dumps(normalized, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_HITL_PAYLOAD_BYTES:
        raise ValueError(f"HumanInput {name} exceeds {MAX_HITL_PAYLOAD_BYTES} bytes")
    return cast(Mapping[str, FrozenJsonValue], _freeze_json_value(normalized))


def _payload_to_event_data(value: Mapping[str, FrozenJsonValue]) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], _thaw_json_value(value))


@dataclass(frozen=True, slots=True)
class HumanInputRequest:
    request_id: str
    session_id: str
    created_by: str
    kind: HumanInputKind
    source: HumanInputSource
    risk_class: HumanInputRiskClass
    question: str
    resume_target: str
    schema_version: int = HITL_CONTRACT_SCHEMA_VERSION
    run_id: str | None = None
    invocation_id: str | None = None
    title: str | None = None
    body: str | None = None
    options: tuple[str, ...] = ()
    required_permission: str | None = None
    timeout_seconds: int | None = None
    timeout_action: HumanInputTimeoutAction = HumanInputTimeoutAction.STAY_WAITING
    surface: str | None = None
    payload: Mapping[str, FrozenJsonValue] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    _legacy_schema_v1_replay: InitVar[bool] = False

    REQUESTED_EVENT_TYPE: ClassVar[str] = "hitl.requested"
    TIMED_OUT_EVENT_TYPE: ClassVar[str] = "hitl.timed_out"
    CANCELLED_EVENT_TYPE: ClassVar[str] = "hitl.cancelled"

    @classmethod
    def from_persisted_schema_v1(cls, **kwargs: Any) -> Self:
        """Rehydrate a schema v1 request from already-persisted event data.

        Early v1 plugin-firewall approval events did not persist all fields that
        new requests must include.  Keep that compatibility path explicit so the
        normal constructor remains strict for fresh requests.
        """

        if kwargs.get("schema_version", HITL_CONTRACT_SCHEMA_VERSION) != 1:
            raise ValueError("legacy HITL request replay only supports schema_version=1")
        return cls(**kwargs, _legacy_schema_v1_replay=True)

    @classmethod
    def from_persisted_event_data(cls, **kwargs: Any) -> Self:
        """Rehydrate a request from persisted event data.

        Fresh requests must always use the strict constructor.  Persisted replay
        only relaxes early schema v1 plugin-firewall approval records that are
        missing fields later required by the v1 permission contract.
        """

        if cls._requires_legacy_schema_v1_plugin_firewall_replay(kwargs):
            return cls.from_persisted_schema_v1(**kwargs)
        return cls(**kwargs)

    @staticmethod
    def _requires_legacy_schema_v1_plugin_firewall_replay(kwargs: Mapping[str, Any]) -> bool:
        if kwargs.get("schema_version", HITL_CONTRACT_SCHEMA_VERSION) != 1:
            return False
        source = kwargs.get("source")
        if source not in {HumanInputSource.PLUGIN_FIREWALL, HumanInputSource.PLUGIN_FIREWALL.value}:
            return False

        kind = kwargs.get("kind")
        if kind not in {
            HumanInputKind.APPROVAL,
            HumanInputKind.DESTRUCTIVE_CONFIRMATION,
            HumanInputKind.APPROVAL.value,
            HumanInputKind.DESTRUCTIVE_CONFIRMATION.value,
        }:
            return False

        payload = kwargs.get("payload", {})
        permission_scope = payload.get("permission_scope") if isinstance(payload, Mapping) else None
        return (
            kwargs.get("required_permission") is None
            or kwargs.get("surface") is None
            or permission_scope is None
        )

    def __post_init__(self, _legacy_schema_v1_replay: bool) -> None:
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("HumanInputRequest schema_version must be a positive integer")
        if not isinstance(self.kind, HumanInputKind):
            raise TypeError("HumanInputRequest kind must be a HumanInputKind")
        if not isinstance(self.source, HumanInputSource):
            raise TypeError("HumanInputRequest source must be a HumanInputSource")
        if not isinstance(self.risk_class, HumanInputRiskClass):
            raise TypeError("HumanInputRequest risk_class must be a HumanInputRiskClass")
        if not isinstance(self.timeout_action, HumanInputTimeoutAction):
            raise TypeError("HumanInputRequest timeout_action must be a HumanInputTimeoutAction")

        for field_name in ("request_id", "session_id", "created_by", "question", "resume_target"):
            object.__setattr__(
                self, field_name, _require_non_empty(field_name, getattr(self, field_name))
            )
        for field_name in (
            "run_id",
            "invocation_id",
            "title",
            "body",
            "required_permission",
            "surface",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _require_non_empty(field_name, value))
        if self.timeout_seconds is not None:
            if type(self.timeout_seconds) is not int:
                raise TypeError("HumanInputRequest timeout_seconds must be an int")
            if self.timeout_seconds < 1:
                raise ValueError("HumanInputRequest timeout_seconds must be >= 1 when provided")
        elif self.timeout_action is not HumanInputTimeoutAction.STAY_WAITING:
            raise ValueError(
                "HumanInputRequest timeout_action requires a timeout_seconds value "
                "(only STAY_WAITING is valid when timeout_seconds is None)"
            )
        if (
            self.kind in {HumanInputKind.SINGLE_SELECT, HumanInputKind.MULTI_SELECT}
            and not self.options
        ):
            raise ValueError("select HumanInputRequest requires at least one option")
        object.__setattr__(self, "options", _normalize_string_tuple("options", self.options))
        object.__setattr__(self, "payload", _ensure_json_safe_payload("payload", self.payload))
        self._validate_source_specific_contract(
            allow_legacy_schema_v1_replay=_legacy_schema_v1_replay
        )
        object.__setattr__(
            self, "created_at", _normalize_utc_datetime("created_at", self.created_at)
        )

    def _validate_source_specific_contract(
        self, *, allow_legacy_schema_v1_replay: bool = False
    ) -> None:
        if self.source is not HumanInputSource.PLUGIN_FIREWALL or self.kind not in {
            HumanInputKind.APPROVAL,
            HumanInputKind.DESTRUCTIVE_CONFIRMATION,
        }:
            return

        if allow_legacy_schema_v1_replay and self.schema_version == 1:
            self._validate_legacy_plugin_firewall_replay_contract()
            return

        if self.required_permission is None:
            raise ValueError(
                "plugin_firewall approval HumanInputRequest requires required_permission"
            )
        if self.surface is None:
            raise ValueError("plugin_firewall approval HumanInputRequest requires surface")
        if not self.payload:
            raise ValueError("plugin_firewall approval HumanInputRequest requires payload")

        permission_scope = self.payload.get("permission_scope")
        if permission_scope != self.required_permission:
            raise ValueError(
                "plugin_firewall approval HumanInputRequest payload permission_scope "
                "must match required_permission"
            )

    def _validate_legacy_plugin_firewall_replay_contract(self) -> None:
        if self.required_permission is None:
            return

        permission_scope = self.payload.get("permission_scope")
        if permission_scope is not None and permission_scope != self.required_permission:
            raise ValueError(
                "plugin_firewall approval HumanInputRequest payload permission_scope "
                "must match required_permission"
            )

    @property
    def aggregate_id(self) -> str:
        return self.request_id

    def to_event_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "kind": self.kind.value,
            "source": self.source.value,
            "risk_class": self.risk_class.value,
            "question": self.question,
            "resume_target": self.resume_target,
            "created_by": self.created_by,
            "timeout_action": self.timeout_action.value,
            "created_at": self.created_at.isoformat(),
        }
        if self.run_id is not None:
            data["run_id"] = self.run_id
        if self.invocation_id is not None:
            data["invocation_id"] = self.invocation_id
        if self.title is not None:
            data["title"] = self.title
        if self.body is not None:
            data["body"] = self.body
        if self.options:
            data["options"] = list(self.options)
        if self.required_permission is not None:
            data["required_permission"] = self.required_permission
        if self.timeout_seconds is not None:
            data["timeout_seconds"] = self.timeout_seconds
        if self.surface is not None:
            data["surface"] = self.surface
        if self.payload:
            data["payload"] = _payload_to_event_data(self.payload)
        return data


@dataclass(frozen=True, slots=True)
class HumanInputResponse:
    request_id: str
    actor: str
    response_kind: HumanInputResponseKind
    schema_version: int = HITL_CONTRACT_SCHEMA_VERSION
    session_id: str | None = None
    run_id: str | None = None
    invocation_id: str | None = None
    selected_values: tuple[str, ...] = ()
    text: str | None = None
    approval_decision: bool | None = None
    surface: str | None = None
    payload: Mapping[str, FrozenJsonValue] = field(default_factory=dict)
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    ANSWERED_EVENT_TYPE: ClassVar[str] = "hitl.answered"

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("HumanInputResponse schema_version must be a positive integer")
        if not isinstance(self.response_kind, HumanInputResponseKind):
            raise TypeError("HumanInputResponse response_kind must be a HumanInputResponseKind")
        object.__setattr__(self, "request_id", _require_non_empty("request_id", self.request_id))
        object.__setattr__(self, "actor", _require_non_empty("actor", self.actor))
        for field_name in ("session_id", "run_id", "invocation_id", "surface"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _require_non_empty(field_name, value))
        if self.text is not None:
            object.__setattr__(self, "text", _require_non_blank_text("text", self.text))
        object.__setattr__(
            self,
            "selected_values",
            _normalize_string_tuple("selected_values", self.selected_values),
        )
        self._validate_response_content()
        object.__setattr__(self, "payload", _ensure_json_safe_payload("payload", self.payload))
        object.__setattr__(
            self, "received_at", _normalize_utc_datetime("received_at", self.received_at)
        )

    def _validate_response_content(self) -> None:
        has_text = self.text is not None
        has_selection = bool(self.selected_values)
        has_approval = self.approval_decision is not None

        if has_approval and self.response_kind is not HumanInputResponseKind.APPROVAL:
            raise ValueError("approval_decision is only valid for approval responses")

        if self.response_kind is HumanInputResponseKind.TEXT:
            if not has_text:
                raise ValueError("text responses require text")
            if has_selection:
                raise ValueError("text responses must not include selection content")
            return

        if self.response_kind is HumanInputResponseKind.SELECTION:
            if not has_selection:
                raise ValueError("selection responses require selected_values")
            if has_text or has_approval:
                raise ValueError("selection responses must not include text or approval content")
            return

        if self.response_kind is HumanInputResponseKind.APPROVAL:
            if not has_approval:
                raise ValueError("approval responses require approval_decision")
            if has_text or has_selection:
                raise ValueError("approval responses must not include text or selection content")
            return

        if has_text or has_selection or has_approval:
            raise ValueError("cancel and timeout responses must not include answer content")

    @property
    def aggregate_id(self) -> str:
        return self.request_id

    def to_event_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "actor": self.actor,
            "response_kind": self.response_kind.value,
            "received_at": self.received_at.isoformat(),
        }
        if self.session_id is not None:
            data["session_id"] = self.session_id
        if self.run_id is not None:
            data["run_id"] = self.run_id
        if self.invocation_id is not None:
            data["invocation_id"] = self.invocation_id
        if self.selected_values:
            data["selected_values"] = list(self.selected_values)
        if self.text is not None:
            data["text"] = self.text
        if self.approval_decision is not None:
            data["approval_decision"] = self.approval_decision
        if self.surface is not None:
            data["surface"] = self.surface
        if self.payload:
            data["payload"] = _payload_to_event_data(self.payload)
        return data
