"""ControlContract schema and invariants for control-plane decisions.

The ControlContract is the stable payload contract behind
``control.directive.emitted`` events. It keeps the global ``Directive``
vocabulary, target identity, terminality, replay correlation, and idempotency
metadata in one validated shape before any transport or event factory serializes
it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from ouroboros.core.directive import Directive

CONTROL_CONTRACT_SCHEMA_VERSION = 1
"""Current additive ControlContract schema version."""

CANONICAL_CONTROL_TARGET_TYPES: frozenset[str] = frozenset(
    {
        "session",
        "execution",
        "lineage",
        "agent_process",
        "contract",
        "execution_node",
    }
)
"""Documented target types for ControlContract producers.

The set is advisory rather than closed: target_type remains a string so future
Agent OS targets can land additively without changing stored event schemas.
"""


def _require_non_empty(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"ControlContract {name} must be non-empty")
    return normalized


@dataclass(frozen=True, slots=True)
class ControlContract:
    """Validated control-plane decision contract.

    ``is_terminal`` is intentionally derived from ``directive`` instead of being
    caller-provided. That prevents payloads from claiming terminal semantics that
    disagree with the global directive vocabulary.
    """

    directive: Directive
    target_type: str
    target_id: str
    emitted_by: str
    reason: str
    schema_version: int = CONTROL_CONTRACT_SCHEMA_VERSION
    phase: str | None = None
    context_snapshot_id: str | None = None
    session_id: str | None = None
    execution_id: str | None = None
    lineage_id: str | None = None
    generation_number: int | None = None
    parent_directive_id: str | None = None
    idempotency_key: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    EVENT_TYPE: ClassVar[str] = "control.directive.emitted"

    def __post_init__(self) -> None:
        if self.schema_version < 1:
            raise ValueError("ControlContract schema_version must be >= 1")
        if not isinstance(self.directive, Directive):
            raise TypeError("ControlContract directive must be a Directive")

        object.__setattr__(self, "target_type", _require_non_empty("target_type", self.target_type))
        object.__setattr__(self, "target_id", _require_non_empty("target_id", self.target_id))
        object.__setattr__(self, "emitted_by", _require_non_empty("emitted_by", self.emitted_by))
        object.__setattr__(self, "reason", _require_non_empty("reason", self.reason))

        if self.generation_number is not None and self.generation_number < 1:
            raise ValueError("ControlContract generation_number must be >= 1 when provided")
        if self.lineage_id is not None and not self.lineage_id.strip():
            raise ValueError("ControlContract lineage_id must be non-empty when provided")
        if self.execution_id is not None and not self.execution_id.strip():
            raise ValueError("ControlContract execution_id must be non-empty when provided")
        if self.session_id is not None and not self.session_id.strip():
            raise ValueError("ControlContract session_id must be non-empty when provided")
        if self.phase is not None and not self.phase.strip():
            raise ValueError("ControlContract phase must be non-empty when provided")
        if self.context_snapshot_id is not None and not self.context_snapshot_id.strip():
            raise ValueError("ControlContract context_snapshot_id must be non-empty when provided")
        if self.parent_directive_id is not None and not self.parent_directive_id.strip():
            raise ValueError("ControlContract parent_directive_id must be non-empty when provided")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ValueError("ControlContract idempotency_key must be non-empty when provided")

        if not isinstance(self.extra, dict):
            raise TypeError("ControlContract extra must be a dict")
        object.__setattr__(self, "extra", dict(self.extra))

    @property
    def is_terminal(self) -> bool:
        """Return the global terminality for this decision's directive."""
        return self.directive.is_terminal

    @property
    def effective_idempotency_key(self) -> tuple[str, str, str, str] | None:
        """Projection-level effective decision key when idempotency metadata exists.

        Event UUIDs identify raw rows. This key identifies the effective control
        decision for replay/backfill/mesh consumers that must dedupe repeated
        delivery or reconstruction.
        """
        if self.idempotency_key is None:
            return None
        return (
            self.target_type,
            self.target_id,
            self.directive.value,
            self.idempotency_key,
        )

    def to_event_data(self) -> dict[str, Any]:
        """Serialize the contract to a JSON-safe event payload."""
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "emitted_by": self.emitted_by,
            "directive": self.directive.value,
            "is_terminal": self.is_terminal,
            "reason": self.reason,
        }
        if self.phase is not None:
            data["phase"] = self.phase
        if self.context_snapshot_id is not None:
            data["context_snapshot_id"] = self.context_snapshot_id
        if self.session_id is not None:
            data["session_id"] = self.session_id
        if self.execution_id is not None:
            data["execution_id"] = self.execution_id
        if self.lineage_id is not None:
            data["lineage_id"] = self.lineage_id
        if self.generation_number is not None:
            data["generation_number"] = self.generation_number
        if self.parent_directive_id is not None:
            data["parent_directive_id"] = self.parent_directive_id
        if self.idempotency_key is not None:
            data["idempotency_key"] = self.idempotency_key
        if self.extra:
            data["extra"] = dict(self.extra)
        return data
