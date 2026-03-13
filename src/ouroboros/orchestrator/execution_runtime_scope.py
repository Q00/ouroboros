"""Helpers for naming persisted execution-runtime scopes.

This keeps implementation-session and coordinator-reconciliation state in
distinct, stable locations without leaking runtime-specific details upward.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class ExecutionRuntimeScope:
    """A stable identity/path pair for persisted execution runtime state."""

    aggregate_type: str
    aggregate_id: str
    state_path: str
    retry_attempt: int = 0

    def __post_init__(self) -> None:
        """Validate retry metadata for stable AC/session ownership."""
        if self.retry_attempt < 0:
            msg = "retry_attempt must be >= 0"
            raise ValueError(msg)

    @property
    def attempt_number(self) -> int:
        """Human-readable execution attempt number (1-based)."""
        return self.retry_attempt + 1


@dataclass(frozen=True, slots=True)
class ACRuntimeIdentity:
    """Stable AC/session ownership metadata for one implementation attempt."""

    runtime_scope: ExecutionRuntimeScope
    ac_index: int | None = None
    parent_ac_index: int | None = None
    sub_ac_index: int | None = None
    scope: str = "ac"
    session_role: str = "implementation"

    @property
    def ac_id(self) -> str:
        """Return the stable AC identity shared across retries."""
        return self.runtime_scope.aggregate_id

    @property
    def session_scope_id(self) -> str:
        """Return the stable session scope reused only within the same AC."""
        return self.runtime_scope.aggregate_id

    @property
    def session_state_path(self) -> str:
        """Return the persisted runtime state location for this AC."""
        return self.runtime_scope.state_path

    @property
    def retry_attempt(self) -> int:
        """Return the zero-based retry attempt for this AC execution."""
        return self.runtime_scope.retry_attempt

    @property
    def attempt_number(self) -> int:
        """Return the human-readable attempt number for this AC execution."""
        return self.runtime_scope.attempt_number

    @property
    def session_attempt_id(self) -> str:
        """Return the unique implementation-session identity for this attempt."""
        return f"{self.session_scope_id}_attempt_{self.attempt_number}"

    @property
    def cache_key(self) -> str:
        """Return the cache key used for same-attempt resume state."""
        return self.session_attempt_id

    def to_metadata(self) -> dict[str, object]:
        """Serialize identity fields for runtime-handle persistence."""
        metadata: dict[str, object] = {
            "ac_id": self.ac_id,
            "scope": self.scope,
            "session_role": self.session_role,
            "retry_attempt": self.retry_attempt,
            "attempt_number": self.attempt_number,
            "session_scope_id": self.session_scope_id,
            "session_attempt_id": self.session_attempt_id,
            "session_state_path": self.session_state_path,
        }
        if self.parent_ac_index is not None:
            metadata["parent_ac_index"] = self.parent_ac_index
        if self.sub_ac_index is not None:
            metadata["sub_ac_index"] = self.sub_ac_index
        if self.ac_index is not None and self.parent_ac_index is None:
            metadata["ac_index"] = self.ac_index
        return metadata


def _normalize_scope_segment(value: str, *, fallback: str) -> str:
    """Normalize dynamic identifiers for safe inclusion in scope metadata."""
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_")
    return normalized or fallback


def build_ac_runtime_scope(
    ac_index: int,
    *,
    execution_context_id: str | None = None,
    is_sub_ac: bool = False,
    parent_ac_index: int | None = None,
    sub_ac_index: int | None = None,
    retry_attempt: int = 0,
) -> ExecutionRuntimeScope:
    """Build the persisted runtime scope for an AC implementation session."""
    workflow_scope = (
        _normalize_scope_segment(execution_context_id, fallback="workflow")
        if execution_context_id
        else None
    )
    if is_sub_ac:
        if parent_ac_index is None or sub_ac_index is None:
            msg = "parent_ac_index and sub_ac_index are required for sub-AC runtime scopes"
            raise ValueError(msg)
        aggregate_id = f"sub_ac_{parent_ac_index}_{sub_ac_index}"
        state_path = (
            "execution.acceptance_criteria."
            f"ac_{parent_ac_index}.sub_acs.sub_ac_{sub_ac_index}.implementation_session"
        )
        if workflow_scope is not None:
            aggregate_id = f"{workflow_scope}_{aggregate_id}"
            state_path = (
                "execution.workflows."
                f"{workflow_scope}.acceptance_criteria."
                f"ac_{parent_ac_index}.sub_acs.sub_ac_{sub_ac_index}.implementation_session"
            )
        return ExecutionRuntimeScope(
            aggregate_type="execution",
            aggregate_id=aggregate_id,
            state_path=state_path,
            retry_attempt=retry_attempt,
        )

    aggregate_id = f"ac_{ac_index}"
    state_path = f"execution.acceptance_criteria.ac_{ac_index}.implementation_session"
    if workflow_scope is not None:
        aggregate_id = f"{workflow_scope}_{aggregate_id}"
        state_path = (
            "execution.workflows."
            f"{workflow_scope}.acceptance_criteria.ac_{ac_index}.implementation_session"
        )

    return ExecutionRuntimeScope(
        aggregate_type="execution",
        aggregate_id=aggregate_id,
        state_path=state_path,
        retry_attempt=retry_attempt,
    )


def build_ac_runtime_identity(
    ac_index: int,
    *,
    execution_context_id: str | None = None,
    is_sub_ac: bool = False,
    parent_ac_index: int | None = None,
    sub_ac_index: int | None = None,
    retry_attempt: int = 0,
) -> ACRuntimeIdentity:
    """Build stable AC/session identity metadata for one implementation attempt."""
    runtime_scope = build_ac_runtime_scope(
        ac_index,
        execution_context_id=execution_context_id,
        is_sub_ac=is_sub_ac,
        parent_ac_index=parent_ac_index,
        sub_ac_index=sub_ac_index,
        retry_attempt=retry_attempt,
    )
    return ACRuntimeIdentity(
        runtime_scope=runtime_scope,
        ac_index=None if is_sub_ac else ac_index,
        parent_ac_index=parent_ac_index if is_sub_ac else None,
        sub_ac_index=sub_ac_index if is_sub_ac else None,
    )


def build_level_coordinator_runtime_scope(
    execution_id: str,
    level_number: int,
) -> ExecutionRuntimeScope:
    """Build the persisted runtime scope for level-scoped reconciliation work."""
    execution_scope = _normalize_scope_segment(
        execution_id,
        fallback="workflow",
    )
    return ExecutionRuntimeScope(
        aggregate_type="execution",
        aggregate_id=(f"{execution_scope}_level_{level_number}_coordinator_reconciliation"),
        state_path=(
            "execution.workflows."
            f"{execution_scope}.levels.level_{level_number}."
            "coordinator_reconciliation_session"
        ),
    )


__all__ = [
    "ACRuntimeIdentity",
    "build_ac_runtime_identity",
    "ExecutionRuntimeScope",
    "build_ac_runtime_scope",
    "build_level_coordinator_runtime_scope",
]
