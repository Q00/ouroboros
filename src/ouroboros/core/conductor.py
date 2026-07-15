"""Bounded contracts for Active Conductor decisions and successor directives."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
import re
from typing import Any
import unicodedata

CONDUCTOR_SCHEMA_VERSION = 1
MAX_DECISION_ID_BYTES = 160
MAX_EVENT_ID_BYTES = 256
MAX_ACTION_BYTES = 160
MAX_VERIFICATION_SUMMARY_BYTES = 2_000
MAX_DIRECTIVE_INSTRUCTION_BYTES = 2_000
MAX_REJECTED_REASON_BYTES = 500
MAX_REJECTED_REASONS = 10
MAX_RECEIPT_BYTES = 1_000

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{12,}"),
    re.compile(r"\bsk-(?:ant-|or-)?[a-zA-Z0-9_-]{16,}"),
    re.compile(r"\bAIza[a-zA-Z0-9_-]{20,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|"
        r"client[_-]?secret)\b\s*[:=]\s*[^\s,;]{8,}"
    ),
)


class ConductorActorMode(StrEnum):
    """Host policy that selected a conductor decision."""

    RUN = "run"
    AUTO = "auto"
    RALPH = "ralph"


class ConductorDecisionPhase(StrEnum):
    """Durable phases of one conductor decision aggregate."""

    SELECTED = "selected"
    COMPLETED = "completed"
    FAILED = "failed"
    DECLINED = "declined"

    @property
    def is_terminal(self) -> bool:
        return self is not ConductorDecisionPhase.SELECTED


class ConductorEffect(StrEnum):
    """Mutation class used for authority and ownership checks."""

    READ_ONLY = "read_only"
    SUCCESSOR_ONLY = "successor_only"
    SPECIFICATION_CHANGE = "specification_change"
    USER_ESCALATION = "user_escalation"

    @property
    def mutates(self) -> bool:
        return self in {
            ConductorEffect.SUCCESSOR_ONLY,
            ConductorEffect.SPECIFICATION_CHANGE,
        }


class EngineOwnershipState(StrEnum):
    """Authoritative ownership state supplied by the attention envelope."""

    ACTIVE = "active"
    CLOSED = "closed"
    UNKNOWN = "unknown"


def _visible(value: str) -> bool:
    return any(
        not char.isspace() and not unicodedata.category(char).startswith("C") for char in value
    )


def bounded_conductor_text(
    name: str,
    value: str,
    *,
    max_bytes: int,
    reject_secrets: bool = True,
) -> str:
    """Normalize one conductor text field and fail closed on unsafe content."""
    if not isinstance(value, str):
        raise TypeError(f"Conductor {name} must be a string")
    normalized = value.strip()
    if not normalized or not _visible(normalized):
        raise ValueError(f"Conductor {name} must contain visible text")
    if len(normalized.encode("utf-8")) > max_bytes:
        raise ValueError(f"Conductor {name} exceeds {max_bytes} UTF-8 bytes")
    if reject_secrets and any(pattern.search(normalized) for pattern in _SECRET_PATTERNS):
        raise ValueError(f"Conductor {name} must not contain secret-shaped content")
    return normalized


def bounded_conductor_optional_text(
    name: str,
    value: str | None,
    *,
    max_bytes: int,
    reject_secrets: bool = True,
) -> str | None:
    if value is None:
        return None
    return bounded_conductor_text(
        name,
        value,
        max_bytes=max_bytes,
        reject_secrets=reject_secrets,
    )


def stable_payload_digest(value: object) -> str:
    """Return a deterministic SHA-256 digest without persisting the payload."""
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ConductorDirective:
    """Additive corrective context for a successor run or generation."""

    source_attention_event_id: str
    instruction: str
    rejected_reasons: tuple[str, ...] = ()
    preserve_goal: bool = True
    preserve_acceptance_criteria: bool = True
    preserve_constraints: bool = True
    preserve_non_goals: bool = True
    deterministic: bool = False
    user_approval_event_id: str | None = None
    schema_version: int = CONDUCTOR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version < 1:
            raise ValueError("ConductorDirective schema_version must be >= 1")
        object.__setattr__(
            self,
            "source_attention_event_id",
            bounded_conductor_text(
                "source_attention_event_id",
                self.source_attention_event_id,
                max_bytes=MAX_EVENT_ID_BYTES,
                reject_secrets=False,
            ),
        )
        object.__setattr__(
            self,
            "instruction",
            bounded_conductor_text(
                "instruction",
                self.instruction,
                max_bytes=MAX_DIRECTIVE_INSTRUCTION_BYTES,
            ),
        )
        if len(self.rejected_reasons) > MAX_REJECTED_REASONS:
            raise ValueError(
                f"ConductorDirective rejected_reasons exceeds {MAX_REJECTED_REASONS} items"
            )
        normalized_reasons = tuple(
            bounded_conductor_text(
                f"rejected_reasons[{index}]",
                reason,
                max_bytes=MAX_REJECTED_REASON_BYTES,
            )
            for index, reason in enumerate(self.rejected_reasons)
        )
        object.__setattr__(self, "rejected_reasons", normalized_reasons)
        for field_name in (
            "preserve_goal",
            "preserve_acceptance_criteria",
            "preserve_constraints",
            "preserve_non_goals",
            "deterministic",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"ConductorDirective {field_name} must be a boolean")
        object.__setattr__(
            self,
            "user_approval_event_id",
            bounded_conductor_optional_text(
                "user_approval_event_id",
                self.user_approval_event_id,
                max_bytes=MAX_EVENT_ID_BYTES,
                reject_secrets=False,
            ),
        )
        if not self.is_non_relaxing and self.user_approval_event_id is None:
            raise ValueError(
                "A specification-changing ConductorDirective requires user_approval_event_id"
            )

    @property
    def is_non_relaxing(self) -> bool:
        """Return whether every approved direction field must be preserved."""
        return all(
            (
                self.preserve_goal,
                self.preserve_acceptance_criteria,
                self.preserve_constraints,
                self.preserve_non_goals,
            )
        )

    @property
    def digest(self) -> str:
        return stable_payload_digest(self.to_event_data())

    def validate_actor_policy(self, actor_mode: ConductorActorMode) -> None:
        """Prevent autonomous Auto/Ralph from weakening the approved contract."""
        if actor_mode in {ConductorActorMode.AUTO, ConductorActorMode.RALPH} and not (
            self.is_non_relaxing and self.deterministic
        ):
            raise ValueError(
                "Auto/Ralph conductor successors require a deterministic non-relaxing directive"
            )

    def to_event_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "schema_version": self.schema_version,
            "source_attention_event_id": self.source_attention_event_id,
            "instruction": self.instruction,
            "rejected_reasons": list(self.rejected_reasons),
            "preserve_goal": self.preserve_goal,
            "preserve_acceptance_criteria": self.preserve_acceptance_criteria,
            "preserve_constraints": self.preserve_constraints,
            "preserve_non_goals": self.preserve_non_goals,
            "deterministic": self.deterministic,
            "is_non_relaxing": self.is_non_relaxing,
        }
        if self.user_approval_event_id is not None:
            data["user_approval_event_id"] = self.user_approval_event_id
        return data

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ConductorDirective:
        if not isinstance(value, Mapping):
            raise TypeError("conductor_directive must be an object")
        raw_reasons = value.get("rejected_reasons", ())
        if raw_reasons is None:
            raw_reasons = ()
        if not isinstance(raw_reasons, list | tuple):
            raise TypeError("conductor_directive.rejected_reasons must be an array")
        return cls(
            source_attention_event_id=value.get("source_attention_event_id", ""),
            instruction=value.get("instruction", ""),
            rejected_reasons=tuple(raw_reasons),
            preserve_goal=value.get("preserve_goal", True),
            preserve_acceptance_criteria=value.get("preserve_acceptance_criteria", True),
            preserve_constraints=value.get("preserve_constraints", True),
            preserve_non_goals=value.get("preserve_non_goals", True),
            deterministic=value.get("deterministic", False),
            user_approval_event_id=value.get("user_approval_event_id"),
            schema_version=value.get("schema_version", CONDUCTOR_SCHEMA_VERSION),
        )


def validate_conductor_successor_authorization(
    selected_data: Mapping[str, Any],
    *,
    directive: ConductorDirective,
    predecessor_execution_id: str,
) -> ConductorActorMode:
    """Validate one selected decision before successor dispatch."""
    if selected_data.get("engine_ownership_state") != EngineOwnershipState.CLOSED.value:
        raise ValueError("Conductor successor dispatch requires closed engine ownership")
    if selected_data.get("selected_effect") not in {
        ConductorEffect.SUCCESSOR_ONLY.value,
        ConductorEffect.SPECIFICATION_CHANGE.value,
    }:
        raise ValueError("Selected conductor decision does not authorize a successor")
    if selected_data.get("predecessor_execution_id") != predecessor_execution_id:
        raise ValueError("predecessor_execution_id does not match the selected conductor decision")
    if selected_data.get("conductor_directive_digest") != directive.digest:
        raise ValueError("conductor_directive does not match the selected conductor decision")
    actor_mode = ConductorActorMode(str(selected_data.get("actor_mode")))
    directive.validate_actor_policy(actor_mode)
    if not directive.is_non_relaxing:
        if selected_data.get("selected_effect") != ConductorEffect.SPECIFICATION_CHANGE.value:
            raise ValueError("A relaxing directive requires a specification_change decision")
        if selected_data.get("user_approval_event_id") != directive.user_approval_event_id:
            raise ValueError("Successor user approval does not match the selected decision")
    return actor_mode


__all__ = [
    "CONDUCTOR_SCHEMA_VERSION",
    "MAX_ACTION_BYTES",
    "MAX_DECISION_ID_BYTES",
    "MAX_EVENT_ID_BYTES",
    "MAX_RECEIPT_BYTES",
    "MAX_VERIFICATION_SUMMARY_BYTES",
    "ConductorActorMode",
    "ConductorDecisionPhase",
    "ConductorDirective",
    "ConductorEffect",
    "EngineOwnershipState",
    "bounded_conductor_optional_text",
    "bounded_conductor_text",
    "stable_payload_digest",
    "validate_conductor_successor_authorization",
]
