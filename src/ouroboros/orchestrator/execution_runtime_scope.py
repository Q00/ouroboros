"""Helpers for naming persisted execution-runtime scopes.

This keeps implementation-session and coordinator-reconciliation state in
distinct, stable locations without leaking runtime-specific details upward.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import blake2b
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


def _display_path(path: tuple[int, ...]) -> str:
    """Return a human-readable 1-based dotted path for an execution node."""
    return ".".join(str(segment + 1) for segment in path)


def _build_local_node_id(path: tuple[int, ...]) -> str:
    """Return a stable, compact node id unique within an execution."""
    if not path:
        msg = "execution node path must not be empty"
        raise ValueError(msg)
    if len(path) == 1:
        return f"ac_{path[0]}"

    digest = blake2b(
        ".".join(str(segment) for segment in path).encode("utf-8"),
        digest_size=8,
    ).hexdigest()
    return f"node_{digest}"


@dataclass(frozen=True, slots=True)
class ExecutionNodeIdentity:
    """Canonical hierarchical identity for one AC/Sub-AC execution node.

    ``node_id`` is intentionally local to an execution; event payloads also
    carry ``execution_id`` and runtime scopes prefix it with the workflow scope.
    ``path`` is metadata for ordering/display, not the storage key.
    """

    execution_context_id: str | None
    root_ac_index: int
    path: tuple[int, ...]
    node_id: str
    parent_node_id: str | None
    depth: int
    ordinal: int

    def __post_init__(self) -> None:
        if not self.path:
            msg = "execution node path must not be empty"
            raise ValueError(msg)
        if self.root_ac_index < 0:
            msg = "root_ac_index must be >= 0"
            raise ValueError(msg)
        if self.depth != len(self.path) - 1:
            msg = "depth must match execution node path length"
            raise ValueError(msg)
        if self.ordinal < 0:
            msg = "ordinal must be >= 0"
            raise ValueError(msg)

    @classmethod
    def root(
        cls,
        *,
        execution_context_id: str | None,
        ac_index: int,
    ) -> ExecutionNodeIdentity:
        """Build identity for a top-level AC node."""
        path = (ac_index,)
        return cls(
            execution_context_id=execution_context_id,
            root_ac_index=ac_index,
            path=path,
            node_id=_build_local_node_id(path),
            parent_node_id=None,
            depth=0,
            ordinal=ac_index,
        )

    def child(self, ordinal: int) -> ExecutionNodeIdentity:
        """Build identity for a direct child node."""
        path = (*self.path, ordinal)
        return ExecutionNodeIdentity(
            execution_context_id=self.execution_context_id,
            root_ac_index=self.root_ac_index,
            path=path,
            node_id=_build_local_node_id(path),
            parent_node_id=self.node_id,
            depth=self.depth + 1,
            ordinal=ordinal,
        )

    @property
    def display_path(self) -> str:
        """Return a stable human-readable dotted path, e.g. ``1.2.3``."""
        return _display_path(self.path)

    @property
    def root_ac_number(self) -> int:
        """Return the human-readable top-level AC number."""
        return self.root_ac_index + 1

    def to_event_metadata(self) -> dict[str, object]:
        """Serialize node identity fields for persisted events."""
        metadata: dict[str, object] = {
            "identity_model": "execution_node_v1",
            "schema_version": 1,
            "node_id": self.node_id,
            "parent_node_id": self.parent_node_id,
            "root_ac_index": self.root_ac_index,
            "root_ac_number": self.root_ac_number,
            "path": list(self.path),
            "display_path": self.display_path,
            "depth": self.depth,
            "ordinal": self.ordinal,
        }
        if self.execution_context_id:
            metadata["execution_id"] = self.execution_context_id
        return metadata


@dataclass(frozen=True, slots=True)
class ACRuntimeIdentity:
    """Stable AC/session ownership metadata for one implementation attempt."""

    runtime_scope: ExecutionRuntimeScope
    ac_index: int | None = None
    parent_ac_index: int | None = None
    sub_ac_index: int | None = None
    node_id: str | None = None
    parent_node_id: str | None = None
    root_ac_index: int | None = None
    node_path: tuple[int, ...] = ()
    display_path: str | None = None
    depth: int | None = None
    ordinal: int | None = None
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
        if self.node_id is not None:
            metadata["identity_model"] = "execution_node_v1"
            metadata["schema_version"] = 1
            metadata["node_id"] = self.node_id
            metadata["parent_node_id"] = self.parent_node_id
        if self.root_ac_index is not None:
            metadata["root_ac_index"] = self.root_ac_index
            metadata["root_ac_number"] = self.root_ac_index + 1
        if self.node_path:
            metadata["path"] = list(self.node_path)
        if self.display_path is not None:
            metadata["display_path"] = self.display_path
        if self.depth is not None:
            metadata["depth"] = self.depth
        if self.ordinal is not None:
            metadata["ordinal"] = self.ordinal
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
    node_id: str | None = None,
    node_path: tuple[int, ...] = (),
) -> ExecutionRuntimeScope:
    """Build the persisted runtime scope for an AC implementation session."""
    workflow_scope = (
        _normalize_scope_segment(execution_context_id, fallback="workflow")
        if execution_context_id
        else None
    )
    if node_id:
        normalized_node_id = _normalize_scope_segment(node_id, fallback="node")
        aggregate_id = (
            f"{workflow_scope}_{normalized_node_id}" if workflow_scope else normalized_node_id
        )
        if len(node_path) == 1:
            state_path = f"execution.acceptance_criteria.ac_{node_path[0]}.implementation_session"
            if workflow_scope is not None:
                state_path = (
                    "execution.workflows."
                    f"{workflow_scope}.acceptance_criteria.ac_{node_path[0]}."
                    "implementation_session"
                )
        elif workflow_scope is not None:
            state_path = (
                "execution.workflows."
                f"{workflow_scope}.nodes.{normalized_node_id}.implementation_session"
            )
        else:
            state_path = f"execution.nodes.{normalized_node_id}.implementation_session"
        return ExecutionRuntimeScope(
            aggregate_type="execution",
            aggregate_id=aggregate_id,
            state_path=state_path,
            retry_attempt=retry_attempt,
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
    node_identity: ExecutionNodeIdentity | None = None,
) -> ACRuntimeIdentity:
    """Build stable AC/session identity metadata for one implementation attempt."""
    runtime_scope = build_ac_runtime_scope(
        ac_index,
        execution_context_id=execution_context_id,
        is_sub_ac=is_sub_ac,
        parent_ac_index=parent_ac_index,
        sub_ac_index=sub_ac_index,
        retry_attempt=retry_attempt,
        node_id=node_identity.node_id if node_identity is not None else None,
        node_path=node_identity.path if node_identity is not None else (),
    )
    return ACRuntimeIdentity(
        runtime_scope=runtime_scope,
        ac_index=None if is_sub_ac else ac_index,
        parent_ac_index=parent_ac_index if is_sub_ac else None,
        sub_ac_index=sub_ac_index if is_sub_ac else None,
        node_id=node_identity.node_id if node_identity is not None else None,
        parent_node_id=node_identity.parent_node_id if node_identity is not None else None,
        root_ac_index=node_identity.root_ac_index if node_identity is not None else None,
        node_path=node_identity.path if node_identity is not None else (),
        display_path=node_identity.display_path if node_identity is not None else None,
        depth=node_identity.depth if node_identity is not None else None,
        ordinal=node_identity.ordinal if node_identity is not None else None,
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
    "ExecutionNodeIdentity",
    "build_ac_runtime_scope",
    "build_level_coordinator_runtime_scope",
]
