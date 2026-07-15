"""Ouroboros Synapse contracts for directed AC-session intent signals.

This module is transport-neutral.  It defines the immutable signal, runtime
capabilities, capability resolution, and lifecycle vocabulary used by later MCP
and worker-runtime integrations.  All capabilities default to unsupported so
adding the contract cannot change existing runtime behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import re
import unicodedata

SESSION_SIGNAL_SCHEMA_VERSION = 1

MAX_SIGNAL_ID_BYTES = 160
MAX_TARGET_ID_BYTES = 256
MAX_MESSAGE_BYTES = 8_192
MAX_REASON_BYTES = 1_000
MAX_REPLY_BYTES = 1_000
MAX_APPROVAL_ID_BYTES = 256

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{12,}"),
    re.compile(r"\bsk-(?:ant-|or-)?[a-zA-Z0-9_-]{16,}"),
    re.compile(r"\bAIza[a-zA-Z0-9_-]{20,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|"
        r"client[_-]?secret)\b\s*[:=]\s*[^\s,;]{8,}"
    ),
)


class SessionSignalMode(StrEnum):
    """How a signal may affect the addressed runtime attempt."""

    INFORM = "inform"
    AFTER_TURN = "after_turn"
    REDIRECT = "redirect"
    REPLACE = "replace"


class SessionSignalContractEffect(StrEnum):
    """Whether the message preserves or changes the shared execution contract."""

    ADDITIVE = "additive"
    SPECIFICATION_CHANGE = "specification_change"


class SessionSignalSource(StrEnum):
    """Audited origin of a signal, ordered by authority."""

    USER = "user"
    CONDUCTOR = "conductor"
    WORKER = "worker"

    @property
    def priority(self) -> int:
        """Return a larger number for a more authoritative source."""
        return {
            SessionSignalSource.USER: 3,
            SessionSignalSource.CONDUCTOR: 2,
            SessionSignalSource.WORKER: 1,
        }[self]


class SessionSignalState(StrEnum):
    """Durable lifecycle states for one signal aggregate."""

    REQUESTED = "requested"
    ACCEPTED = "accepted"
    QUEUED = "queued"
    DELIVERING = "delivering"
    APPLIED = "applied"
    REJECTED = "rejected"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    COMPLETED = "completed"

    @property
    def is_terminal(self) -> bool:
        return self in {
            SessionSignalState.REJECTED,
            SessionSignalState.DELIVERY_UNCERTAIN,
            SessionSignalState.COMPLETED,
        }


class SessionSignalCapabilityError(ValueError):
    """Raised when a runtime cannot enforce the requested signal mode."""


@dataclass(frozen=True, slots=True)
class SessionSignalCapabilities:
    """Independent runtime abilities used by Synapse mode resolution."""

    inform_delivery: bool = False
    background_reply: bool = False
    after_turn_delivery: bool = False
    checkpoint_redirect: bool = False
    owned_turn_abort: bool = False
    replacement_resume: bool = False

    def to_event_data(self) -> dict[str, bool]:
        """Return a stable JSON-safe capability snapshot."""
        return {
            "inform_delivery": self.inform_delivery,
            "background_reply": self.background_reply,
            "after_turn_delivery": self.after_turn_delivery,
            "checkpoint_redirect": self.checkpoint_redirect,
            "owned_turn_abort": self.owned_turn_abort,
            "replacement_resume": self.replacement_resume,
        }


def _has_visible_text(value: str) -> bool:
    return any(
        not char.isspace() and not unicodedata.category(char).startswith("C") for char in value
    )


def _bounded_text(name: str, value: str, *, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"SessionSignal {name} must be a string")
    normalized = value.strip()
    if not normalized or not _has_visible_text(normalized):
        raise ValueError(f"SessionSignal {name} must contain visible text")
    if len(normalized.encode("utf-8")) > max_bytes:
        raise ValueError(f"SessionSignal {name} exceeds {max_bytes} UTF-8 bytes")
    return normalized


def _reject_secret_shaped_text(name: str, value: str) -> None:
    if session_signal_text_contains_secret(value):
        raise ValueError(f"SessionSignal {name} must not contain secret-shaped content")


def session_signal_text_contains_secret(value: str) -> bool:
    """Return whether bounded text resembles a credential or bearer secret."""
    if not isinstance(value, str):
        raise TypeError("SessionSignal secret inspection requires a string")
    return any(pattern.search(value) for pattern in _SECRET_PATTERNS)


def bounded_session_signal_reply(value: str) -> str:
    """Return one safe, bounded AC-to-main reply without persisting transcripts."""
    normalized = _bounded_text("reply", value, max_bytes=max(MAX_REPLY_BYTES, len(value.encode())))
    if session_signal_text_contains_secret(normalized):
        return "[Reply omitted because it contained secret-shaped content.]"
    encoded = normalized.encode("utf-8")
    if len(encoded) <= MAX_REPLY_BYTES:
        return normalized
    suffix = "…"
    budget = MAX_REPLY_BYTES - len(suffix.encode("utf-8"))
    truncated = encoded[:budget]
    while truncated:
        try:
            return f"{truncated.decode('utf-8')}{suffix}"
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return suffix


@dataclass(frozen=True, slots=True)
class SessionSignal:
    """Validated intent signal addressed to one exact AC runtime attempt."""

    signal_id: str
    target_session_scope_id: str
    target_session_attempt_id: str
    expected_execution_id: str
    mode: SessionSignalMode
    message: str
    source: SessionSignalSource
    reason: str
    idempotency_key: str
    contract_effect: SessionSignalContractEffect = SessionSignalContractEffect.ADDITIVE
    fallback_mode: SessionSignalMode | None = None
    expires_at: datetime | None = None
    user_approval_event_id: str | None = None
    expected_contract_version: int | None = None
    schema_version: int = SESSION_SIGNAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version < 1:
            raise ValueError("SessionSignal schema_version must be >= 1")
        if not isinstance(self.mode, SessionSignalMode):
            raise TypeError("SessionSignal mode must be a SessionSignalMode")
        if not isinstance(self.source, SessionSignalSource):
            raise TypeError("SessionSignal source must be a SessionSignalSource")
        if not isinstance(self.contract_effect, SessionSignalContractEffect):
            raise TypeError("SessionSignal contract_effect must be a SessionSignalContractEffect")

        bounded_fields = {
            "signal_id": (self.signal_id, MAX_SIGNAL_ID_BYTES),
            "target_session_scope_id": (self.target_session_scope_id, MAX_TARGET_ID_BYTES),
            "target_session_attempt_id": (self.target_session_attempt_id, MAX_TARGET_ID_BYTES),
            "expected_execution_id": (self.expected_execution_id, MAX_TARGET_ID_BYTES),
            "message": (self.message, MAX_MESSAGE_BYTES),
            "reason": (self.reason, MAX_REASON_BYTES),
            "idempotency_key": (self.idempotency_key, MAX_TARGET_ID_BYTES),
        }
        for name, (value, max_bytes) in bounded_fields.items():
            object.__setattr__(self, name, _bounded_text(name, value, max_bytes=max_bytes))

        _reject_secret_shaped_text("message", self.message)
        _reject_secret_shaped_text("reason", self.reason)

        if self.fallback_mode is not None:
            if not isinstance(self.fallback_mode, SessionSignalMode):
                raise TypeError("SessionSignal fallback_mode must be a SessionSignalMode")
            if self.mode is not SessionSignalMode.REDIRECT:
                raise ValueError("SessionSignal fallback_mode is valid only for redirect")
            if self.fallback_mode is not SessionSignalMode.AFTER_TURN:
                raise ValueError("SessionSignal redirect fallback must be after_turn")

        if self.expires_at is not None:
            if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
                raise ValueError("SessionSignal expires_at must be timezone-aware")
            object.__setattr__(self, "expires_at", self.expires_at.astimezone(UTC))

        if self.user_approval_event_id is not None:
            object.__setattr__(
                self,
                "user_approval_event_id",
                _bounded_text(
                    "user_approval_event_id",
                    self.user_approval_event_id,
                    max_bytes=MAX_APPROVAL_ID_BYTES,
                ),
            )
        if self.mode is SessionSignalMode.REPLACE and self.user_approval_event_id is None:
            raise ValueError("SessionSignal replace requires user_approval_event_id")
        if (
            self.contract_effect is SessionSignalContractEffect.SPECIFICATION_CHANGE
            and self.user_approval_event_id is None
        ):
            raise ValueError("SessionSignal specification_change requires user_approval_event_id")

        if self.expected_contract_version is not None and self.expected_contract_version < 1:
            raise ValueError("SessionSignal expected_contract_version must be >= 1")

    @property
    def message_digest(self) -> str:
        """Return the stable SHA-256 digest used in every lifecycle event."""
        return hashlib.sha256(self.message.encode("utf-8")).hexdigest()

    @property
    def effective_idempotency_key(self) -> tuple[str, str, str, str]:
        """Return the exact-attempt idempotency identity."""
        return (
            self.expected_execution_id,
            self.target_session_scope_id,
            self.target_session_attempt_id,
            self.idempotency_key,
        )

    def is_expired(self, *, at: datetime | None = None) -> bool:
        """Return whether this signal is expired at a timezone-aware instant."""
        if self.expires_at is None:
            return False
        reference = at or datetime.now(UTC)
        if reference.tzinfo is None or reference.utcoffset() is None:
            raise ValueError("SessionSignal expiry reference must be timezone-aware")
        return reference.astimezone(UTC) >= self.expires_at

    def to_event_data(self, *, include_message: bool = False) -> dict[str, object]:
        """Serialize the bounded transport-neutral signal metadata."""
        data: dict[str, object] = {
            "schema_version": self.schema_version,
            "signal_id": self.signal_id,
            "target_session_scope_id": self.target_session_scope_id,
            "target_session_attempt_id": self.target_session_attempt_id,
            "expected_execution_id": self.expected_execution_id,
            # Correlation alias consumed by EventStore related-event queries and
            # the existing job observer. It is not a second authority field.
            "execution_id": self.expected_execution_id,
            "requested_mode": self.mode.value,
            "contract_effect": self.contract_effect.value,
            "source": self.source.value,
            "reason": self.reason,
            "idempotency_key": self.idempotency_key,
            "message_digest": self.message_digest,
        }
        if include_message:
            data["message"] = self.message
        if self.fallback_mode is not None:
            data["fallback_mode"] = self.fallback_mode.value
        if self.expires_at is not None:
            data["expires_at"] = self.expires_at.isoformat()
        if self.user_approval_event_id is not None:
            data["user_approval_event_id"] = self.user_approval_event_id
        if self.expected_contract_version is not None:
            data["expected_contract_version"] = self.expected_contract_version
        return data

    @classmethod
    def from_event_data(cls, data: dict[str, object]) -> SessionSignal:
        """Reconstruct one validated signal from its durable requested event."""

        def required_text(name: str) -> str:
            value = data.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"SessionSignal event requires non-empty {name}")
            return value

        raw_expires_at = data.get("expires_at")
        expires_at: datetime | None = None
        if raw_expires_at is not None:
            if not isinstance(raw_expires_at, str):
                raise ValueError("SessionSignal event expires_at must be a string")
            expires_at = datetime.fromisoformat(
                f"{raw_expires_at[:-1]}+00:00" if raw_expires_at.endswith("Z") else raw_expires_at
            )
        raw_fallback = data.get("fallback_mode")
        fallback_mode = SessionSignalMode(raw_fallback) if isinstance(raw_fallback, str) else None
        raw_contract_version = data.get("expected_contract_version")
        if raw_contract_version is not None and (
            isinstance(raw_contract_version, bool) or not isinstance(raw_contract_version, int)
        ):
            raise ValueError("SessionSignal event expected_contract_version must be an integer")
        raw_schema_version = data.get("schema_version", SESSION_SIGNAL_SCHEMA_VERSION)
        if isinstance(raw_schema_version, bool) or not isinstance(raw_schema_version, int):
            raise ValueError("SessionSignal event schema_version must be an integer")
        return cls(
            signal_id=required_text("signal_id"),
            target_session_scope_id=required_text("target_session_scope_id"),
            target_session_attempt_id=required_text("target_session_attempt_id"),
            expected_execution_id=required_text("expected_execution_id"),
            mode=SessionSignalMode(required_text("requested_mode")),
            message=required_text("message"),
            source=SessionSignalSource(required_text("source")),
            reason=required_text("reason"),
            idempotency_key=required_text("idempotency_key"),
            contract_effect=SessionSignalContractEffect(
                str(data.get("contract_effect", SessionSignalContractEffect.ADDITIVE.value))
            ),
            fallback_mode=fallback_mode,
            expires_at=expires_at,
            user_approval_event_id=(
                str(data["user_approval_event_id"])
                if isinstance(data.get("user_approval_event_id"), str)
                else None
            ),
            expected_contract_version=raw_contract_version,
            schema_version=raw_schema_version,
        )


def resolve_session_signal_mode(
    signal: SessionSignal,
    capabilities: SessionSignalCapabilities,
) -> SessionSignalMode:
    """Resolve an enforceable effective mode or fail closed."""
    if not isinstance(capabilities, SessionSignalCapabilities):
        raise TypeError("capabilities must be SessionSignalCapabilities")

    if signal.mode is SessionSignalMode.INFORM:
        if capabilities.inform_delivery:
            return SessionSignalMode.INFORM
        raise SessionSignalCapabilityError("runtime does not support inform delivery")

    if signal.mode is SessionSignalMode.AFTER_TURN:
        if capabilities.after_turn_delivery:
            return SessionSignalMode.AFTER_TURN
        raise SessionSignalCapabilityError("runtime does not support after_turn delivery")

    if signal.mode is SessionSignalMode.REDIRECT:
        if capabilities.checkpoint_redirect:
            return SessionSignalMode.REDIRECT
        if (
            signal.fallback_mode is SessionSignalMode.AFTER_TURN
            and capabilities.after_turn_delivery
        ):
            return SessionSignalMode.AFTER_TURN
        raise SessionSignalCapabilityError("runtime does not support checkpoint redirect")

    if signal.mode is SessionSignalMode.REPLACE:
        if capabilities.owned_turn_abort and capabilities.replacement_resume:
            return SessionSignalMode.REPLACE
        raise SessionSignalCapabilityError("runtime cannot abort and resume a replacement")

    raise SessionSignalCapabilityError(f"unsupported SessionSignal mode: {signal.mode}")


def derive_session_signal_id(
    *,
    expected_execution_id: str,
    target_session_scope_id: str,
    target_session_attempt_id: str,
    idempotency_key: str,
) -> str:
    """Derive a stable public signal ID from the exact idempotency identity."""
    parts = (
        _bounded_text(
            "expected_execution_id",
            expected_execution_id,
            max_bytes=MAX_TARGET_ID_BYTES,
        ),
        _bounded_text(
            "target_session_scope_id",
            target_session_scope_id,
            max_bytes=MAX_TARGET_ID_BYTES,
        ),
        _bounded_text(
            "target_session_attempt_id",
            target_session_attempt_id,
            max_bytes=MAX_TARGET_ID_BYTES,
        ),
        _bounded_text("idempotency_key", idempotency_key, max_bytes=MAX_TARGET_ID_BYTES),
    )
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"sig_{digest[:24]}"


__all__ = [
    "MAX_APPROVAL_ID_BYTES",
    "MAX_MESSAGE_BYTES",
    "MAX_REASON_BYTES",
    "MAX_REPLY_BYTES",
    "MAX_SIGNAL_ID_BYTES",
    "MAX_TARGET_ID_BYTES",
    "SESSION_SIGNAL_SCHEMA_VERSION",
    "SessionSignal",
    "SessionSignalCapabilities",
    "SessionSignalCapabilityError",
    "SessionSignalContractEffect",
    "SessionSignalMode",
    "SessionSignalSource",
    "SessionSignalState",
    "derive_session_signal_id",
    "bounded_session_signal_reply",
    "resolve_session_signal_mode",
    "session_signal_text_contains_secret",
]
