"""Level Coordinator for inter-level review and conflict resolution.

Detects file conflicts from parallel AC execution results and optionally
invokes a Claude session to auto-resolve them. Acts as an intelligent
review gate between dependency levels.

Architecture: Approach A (Pragmatic)
- Pure Python conflict detection from in-memory ACExecutionResult data
- Claude session only when file conflicts are detected
- Zero cost when no conflicts exist

Usage:
    coordinator = LevelCoordinator(adapter)
    conflicts = coordinator.detect_file_conflicts(level_results)

    if conflicts:
        review = await coordinator.run_review(
            execution_id="exec_123",
            conflicts=conflicts,
            level_context=level_ctx,
            level_number=1,
        )
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import json
import math
import re
from typing import TYPE_CHECKING, Any

from ouroboros.evolution.provider_usage import tracked_agent_task
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import DEFAULT_TOOLS, RuntimeHandle
from ouroboros.orchestrator.capabilities import build_capability_graph
from ouroboros.orchestrator.execution_runtime_scope import (
    build_level_coordinator_runtime_scope,
)
from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog
from ouroboros.orchestrator.policy import (
    PolicyContext,
    PolicyExecutionPhase,
    PolicySessionRole,
    allowed_capability_names,
)
from ouroboros.orchestrator.recoverable_failure import (
    UsageLimitPauseConsequence,
    usage_limit_pause_consequence_from_message,
)
from ouroboros.orchestrator.runtime_param_negotiation import (
    announce_execution_param_degradations,
)

if TYPE_CHECKING:
    from ouroboros.orchestrator.adapter import AgentMessage, AgentRuntime
    from ouroboros.orchestrator.level_context import LevelContext
    from ouroboros.orchestrator.parallel_executor import ACExecutionResult

log = get_logger(__name__)

_LEVEL_COORDINATOR_SESSION_KIND = "level_coordinator"
_COORDINATOR_SCOPE = "level"
_COORDINATOR_SESSION_ROLE = "coordinator"
_COORDINATOR_ARTIFACT_TYPE = "coordinator_review"
_MAX_COORDINATOR_STRING_ITEMS = 1_024
_MAX_COORDINATOR_ID_CHARS = 1_024
_MAX_COORDINATOR_PATH_CHARS = 4_096
_MAX_COORDINATOR_CONFLICT_PATH_CHARS = 32_768
_MAX_COORDINATOR_ITEM_CHARS = 8_192
_MAX_COORDINATOR_SUMMARY_CHARS = 64_000
_MAX_COORDINATOR_ARTIFACT_CHARS = 256_000
_REASONING_EFFORT_UNSET = object()


def _mapping_has_exact_keys(value: object, expected: frozenset[str]) -> bool:
    """Inspect at most one key beyond a finite coordinator schema."""

    if not isinstance(value, Mapping):
        return False
    try:
        iterator = iter(value)
    except Exception:
        return False
    seen: set[str] = set()
    for index in range(len(expected) + 1):
        try:
            key = next(iterator)
        except StopIteration:
            return len(seen) == len(expected)
        except Exception:
            return False
        if index >= len(expected) or type(key) is not str or key not in expected or key in seen:
            return False
        seen.add(key)
    return False


def _require_bounded_string(
    value: object,
    *,
    field_name: str,
    max_chars: int,
    optional: bool = False,
) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or len(value) > max_chars:
        raise ValueError(f"coordinator artifact {field_name} exceeds its string bound")
    return value


def _require_bounded_string_tuple(value: object, *, field_name: str) -> tuple[str, ...]:
    if type(value) is not list or len(value) > _MAX_COORDINATOR_STRING_ITEMS:
        raise ValueError(f"coordinator artifact {field_name} exceeds its population bound")
    if any(type(item) is not str or len(item) > _MAX_COORDINATOR_ITEM_CHARS for item in value):
        raise ValueError(f"coordinator artifact {field_name} contains an invalid item")
    return tuple(value)


# System prompt for the Coordinator agent
COORDINATOR_SYSTEM_PROMPT = (
    "You are a Level Coordinator reviewing parallel AC execution results. "
    "Your job is to detect and resolve file conflicts, then provide actionable "
    "guidance for the next level of execution. Be concise and precise."
)


def derive_coordinator_tools(runtime_backend: str | None) -> list[str]:
    """Derive the coordinator envelope from the engine policy plane."""
    capability_graph = build_capability_graph(assemble_session_tool_catalog(DEFAULT_TOOLS))
    return allowed_capability_names(
        capability_graph,
        PolicyContext(
            runtime_backend=runtime_backend,
            session_role=PolicySessionRole.COORDINATOR,
            execution_phase=PolicyExecutionPhase.COORDINATOR_REVIEW,
        ),
    )


@dataclass(frozen=True, slots=True)
class FileConflict:
    """A file modified by multiple ACs in the same level.

    Attributes:
        file_path: Path to the conflicting file.
        ac_indices: Which ACs modified this file.
        resolved: Whether the conflict was resolved by the Coordinator.
        resolution_description: How the conflict was resolved.
    """

    file_path: str
    ac_indices: tuple[int, ...]
    resolved: bool = False
    resolution_description: str = ""


def _validate_file_conflict(conflict: object) -> FileConflict:
    if not isinstance(conflict, FileConflict):
        raise ValueError("coordinator conflict must use the closed FileConflict schema")
    if (
        type(conflict.file_path) is not str
        or not conflict.file_path
        or len(conflict.file_path) > _MAX_COORDINATOR_CONFLICT_PATH_CHARS
        or type(conflict.ac_indices) is not tuple
        or any(type(index) is not int or index < 0 for index in conflict.ac_indices)
        or tuple(sorted(set(conflict.ac_indices))) != conflict.ac_indices
        or type(conflict.resolved) is not bool
        or type(conflict.resolution_description) is not str
        or len(conflict.resolution_description) > _MAX_COORDINATOR_ITEM_CHARS
    ):
        raise ValueError("coordinator conflict exceeds its durable bounds")
    return conflict


def build_coordinator_started_payload(
    *,
    execution_id: str,
    session_id: str,
    level_number: int,
    session_scope_id: str,
    session_state_path: str,
    conflicts: list[FileConflict],
) -> dict[str, object]:
    """Build the one bounded producer shape admitted by coordinator replay."""

    _require_bounded_string(
        execution_id,
        field_name="execution_id",
        max_chars=_MAX_COORDINATOR_ID_CHARS,
    )
    _require_bounded_string(
        session_id,
        field_name="session_id",
        max_chars=_MAX_COORDINATOR_ID_CHARS,
    )
    _require_bounded_string(
        session_scope_id,
        field_name="session_scope_id",
        max_chars=_MAX_COORDINATOR_ID_CHARS,
    )
    _require_bounded_string(
        session_state_path,
        field_name="session_state_path",
        max_chars=_MAX_COORDINATOR_PATH_CHARS,
    )
    if type(level_number) is not int or level_number < 1:
        raise ValueError("coordinator level_number is invalid")
    if type(conflicts) is not list or not conflicts:
        raise ValueError("coordinator conflict population is invalid")
    validated_conflicts = [_validate_file_conflict(conflict) for conflict in conflicts]
    return {
        "schema_version": 1,
        "execution_id": execution_id,
        "session_id": session_id,
        "scope": "level",
        "session_role": "coordinator",
        "stage_index": level_number - 1,
        "level_number": level_number,
        "session_scope_id": session_scope_id,
        "session_state_path": session_state_path,
        "conflict_count": len(validated_conflicts),
        "conflicts": [
            {
                "file_path": conflict.file_path,
                "ac_indices": list(conflict.ac_indices),
            }
            for conflict in validated_conflicts
        ],
    }


def validate_coordinator_started_payload(
    payload: object,
    *,
    execution_id: str,
    session_id: str,
    level_number: int,
    session_scope_id: str,
    session_state_path: str,
    expected_conflicts: tuple[FileConflict, ...],
) -> None:
    """Validate the complete started population without open-ended traversal."""

    _require_bounded_string(
        execution_id,
        field_name="execution_id",
        max_chars=_MAX_COORDINATOR_ID_CHARS,
    )
    _require_bounded_string(
        session_id,
        field_name="session_id",
        max_chars=_MAX_COORDINATOR_ID_CHARS,
    )
    _require_bounded_string(
        session_scope_id,
        field_name="session_scope_id",
        max_chars=_MAX_COORDINATOR_ID_CHARS,
    )
    _require_bounded_string(
        session_state_path,
        field_name="session_state_path",
        max_chars=_MAX_COORDINATOR_PATH_CHARS,
    )
    if type(level_number) is not int or level_number < 1 or type(expected_conflicts) is not tuple:
        raise ValueError("current coordinator owner is invalid")

    expected_keys = frozenset(
        {
            "schema_version",
            "execution_id",
            "session_id",
            "scope",
            "session_role",
            "stage_index",
            "level_number",
            "session_scope_id",
            "session_state_path",
            "conflict_count",
            "conflicts",
        }
    )
    if not _mapping_has_exact_keys(payload, expected_keys):
        raise ValueError("coordinator started fields do not match schema v1")
    assert isinstance(payload, Mapping)
    expected_values = {
        "schema_version": 1,
        "execution_id": execution_id,
        "session_id": session_id,
        "scope": "level",
        "session_role": "coordinator",
        "stage_index": level_number - 1,
        "level_number": level_number,
        "session_scope_id": session_scope_id,
        "session_state_path": session_state_path,
        "conflict_count": len(expected_conflicts),
    }
    for key, expected in expected_values.items():
        value = payload.get(key)
        if type(value) is not type(expected) or value != expected:
            raise ValueError(f"coordinator started {key} drifted from its owner")

    raw_conflicts = payload.get("conflicts")
    if type(raw_conflicts) is not list or len(raw_conflicts) != len(expected_conflicts):
        raise ValueError("coordinator started conflict population drifted")
    conflict_keys = frozenset({"file_path", "ac_indices"})
    for raw_conflict, expected in zip(raw_conflicts, expected_conflicts, strict=True):
        _validate_file_conflict(expected)
        if not _mapping_has_exact_keys(raw_conflict, conflict_keys):
            raise ValueError("coordinator started conflict fields are invalid")
        assert isinstance(raw_conflict, Mapping)
        raw_indices = raw_conflict.get("ac_indices")
        if (
            raw_conflict.get("file_path") != expected.file_path
            or type(raw_conflict.get("file_path")) is not str
            or len(raw_conflict["file_path"]) > _MAX_COORDINATOR_CONFLICT_PATH_CHARS
            or type(raw_indices) is not list
            or len(raw_indices) != len(expected.ac_indices)
            or any(type(index) is not int or index < 0 for index in raw_indices)
            or tuple(raw_indices) != expected.ac_indices
        ):
            raise ValueError("coordinator started conflict drifted from the current stage")


@dataclass(frozen=True, slots=True)
class CoordinatorReview:
    """Result of a Coordinator review between dependency levels.

    Attributes:
        level_number: Which level was reviewed.
        conflicts_detected: File conflicts found.
        review_summary: Coordinator's analysis text.
        fixes_applied: Descriptions of fixes made.
        warnings_for_next_level: Injected into next level prompt.
        duration_seconds: Time spent on review.
        session_id: Claude session ID (None if no session was needed).
        session_scope_id: Stable identity for persisted reconciliation runtime state.
        session_state_path: Stable state path for persisted reconciliation runtime state.
        final_output: Raw final coordinator output captured for level-scoped artifacts.
        recoverable_quota_pause: Complete durable consequence of a quota-ending review.
        messages: Runtime messages retained in memory for normalized audit emission.
    """

    level_number: int
    conflicts_detected: tuple[FileConflict, ...] = field(default_factory=tuple)
    review_summary: str = ""
    fixes_applied: tuple[str, ...] = field(default_factory=tuple)
    warnings_for_next_level: tuple[str, ...] = field(default_factory=tuple)
    duration_seconds: float = 0.0
    session_id: str | None = None
    session_scope_id: str | None = None
    session_state_path: str | None = None
    final_output: str = ""
    recoverable_quota_pause: UsageLimitPauseConsequence | None = None
    messages: tuple[AgentMessage, ...] = field(default_factory=tuple)

    def with_recoverable_quota_state(
        self,
        *,
        now: datetime,
        default_pause_seconds: int,
    ) -> CoordinatorReview:
        """Freeze the complete quota consequence from this fresh review."""

        for message in reversed(self.messages):
            consequence = usage_limit_pause_consequence_from_message(
                message,
                now=now,
                default_pause_seconds=default_pause_seconds,
            )
            if consequence is not None:
                return replace(self, recoverable_quota_pause=consequence)
        return replace(self, recoverable_quota_pause=None)

    @property
    def scope(self) -> str:
        """Coordinator reconciliation is always attributed at level scope."""
        return _COORDINATOR_SCOPE

    @property
    def session_role(self) -> str:
        """Coordinator reconciliation never impersonates an AC session."""
        return _COORDINATOR_SESSION_ROLE

    @property
    def stage_index(self) -> int:
        """Return the 0-based execution stage index for this level."""
        return self.level_number - 1

    @property
    def artifact_type(self) -> str:
        """Return the persisted artifact type for coordinator output."""
        return _COORDINATOR_ARTIFACT_TYPE

    @property
    def artifact_owner(self) -> str:
        """Coordinator artifacts are owned by the level coordinator."""
        return _COORDINATOR_SESSION_ROLE

    @property
    def artifact_scope(self) -> str:
        """Coordinator artifacts belong to the shared level workspace state."""
        return _COORDINATOR_SCOPE

    @property
    def artifact_owner_id(self) -> str:
        """Return the stable coordinator scope identifier used for persistence."""
        if self.session_scope_id:
            return self.session_scope_id
        return f"level_{self.level_number}_coordinator_reconciliation"

    @property
    def artifact_state_path(self) -> str:
        """Return the stable persistence path for coordinator runtime state."""
        if self.session_state_path:
            return self.session_state_path
        return f"execution.levels.level_{self.level_number}.coordinator_reconciliation_session"

    def to_artifact_payload(self) -> dict[str, Any]:
        """Build normalized persisted artifact metadata for coordinator output."""
        _validate_coordinator_review(self)
        return {
            "scope": self.scope,
            "session_role": self.session_role,
            "stage_index": self.stage_index,
            "level_number": self.level_number,
            "session_scope_id": self.artifact_owner_id,
            "session_state_path": self.artifact_state_path,
            "artifact_scope": self.artifact_scope,
            "artifact_owner": self.artifact_owner,
            "artifact_owner_id": self.artifact_owner_id,
            "artifact": self.final_output,
            "artifact_type": self.artifact_type,
        }

    def to_completed_event_payload(
        self,
        *,
        execution_id: str,
        session_id: str,
    ) -> dict[str, object]:
        """Build the complete bounded artifact consumed by crash replay."""

        _validate_coordinator_review(self)
        _require_bounded_string(
            execution_id,
            field_name="execution_id",
            max_chars=_MAX_COORDINATOR_ID_CHARS,
        )
        _require_bounded_string(
            session_id,
            field_name="session_id",
            max_chars=_MAX_COORDINATOR_ID_CHARS,
        )
        return {
            "schema_version": 3,
            "execution_id": execution_id,
            "session_id": session_id,
            "coordinator_session_id": self.session_id,
            "recoverable_quota_pause": (
                self.recoverable_quota_pause.to_payload()
                if self.recoverable_quota_pause is not None
                else None
            ),
            **self.to_artifact_payload(),
            "conflicts_detected": [
                {
                    "file_path": conflict.file_path,
                    "ac_indices": list(conflict.ac_indices),
                    "resolved": conflict.resolved,
                    "resolution_description": conflict.resolution_description,
                }
                for conflict in self.conflicts_detected
            ],
            "review_summary": self.review_summary,
            "fixes_applied": list(self.fixes_applied),
            "warnings_for_next_level": list(self.warnings_for_next_level),
            "duration_seconds": self.duration_seconds,
        }

    @classmethod
    def from_artifact_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        level_number: int,
        expected_conflicts: tuple[FileConflict, ...],
        execution_id: str,
        session_id: str,
        session_scope_id: str,
        session_state_path: str,
    ) -> CoordinatorReview:
        """Restore one exact completed coordinator effect from durable data.

        Coordinator reconciliation can mutate the workspace, so replay cannot
        reinterpret a partial or loosely-shaped artifact.  This parser admits
        only the closed producer schema and binds the artifact to the current
        level, conflict population, and deterministic runtime scope.
        """

        _require_bounded_string(
            execution_id,
            field_name="execution_id",
            max_chars=_MAX_COORDINATOR_ID_CHARS,
        )
        _require_bounded_string(
            session_id,
            field_name="session_id",
            max_chars=_MAX_COORDINATOR_ID_CHARS,
        )
        _require_bounded_string(
            session_scope_id,
            field_name="session_scope_id",
            max_chars=_MAX_COORDINATOR_ID_CHARS,
        )
        _require_bounded_string(
            session_state_path,
            field_name="session_state_path",
            max_chars=_MAX_COORDINATOR_PATH_CHARS,
        )
        if (
            type(level_number) is not int
            or level_number < 1
            or type(expected_conflicts) is not tuple
        ):
            raise ValueError("current coordinator owner is invalid")
        for conflict in expected_conflicts:
            _validate_file_conflict(conflict)

        expected_keys = frozenset(
            {
                "schema_version",
                "execution_id",
                "session_id",
                "coordinator_session_id",
                "recoverable_quota_pause",
                "scope",
                "session_role",
                "stage_index",
                "level_number",
                "session_scope_id",
                "session_state_path",
                "artifact_scope",
                "artifact_owner",
                "artifact_owner_id",
                "artifact",
                "artifact_type",
                "conflicts_detected",
                "review_summary",
                "fixes_applied",
                "warnings_for_next_level",
                "duration_seconds",
            }
        )
        if not _mapping_has_exact_keys(payload, expected_keys):
            raise ValueError("coordinator artifact fields do not match schema v3")

        def require_exact(key: str, expected: object) -> None:
            value = payload.get(key)
            if type(value) is not type(expected) or value != expected:
                raise ValueError(f"coordinator artifact {key} does not match its owner")

        require_exact("schema_version", 3)
        require_exact("execution_id", execution_id)
        require_exact("session_id", session_id)
        require_exact("scope", "level")
        require_exact("session_role", "coordinator")
        require_exact("stage_index", level_number - 1)
        require_exact("level_number", level_number)
        require_exact("session_scope_id", session_scope_id)
        require_exact("session_state_path", session_state_path)
        require_exact("artifact_scope", "level")
        require_exact("artifact_owner", "coordinator")
        require_exact("artifact_owner_id", session_scope_id)
        require_exact("artifact_type", "coordinator_review")
        raw_recoverable_quota_pause = payload.get("recoverable_quota_pause")
        try:
            recoverable_quota_pause = (
                None
                if raw_recoverable_quota_pause is None
                else UsageLimitPauseConsequence.from_payload(raw_recoverable_quota_pause)
            )
        except ValueError as exc:
            raise ValueError(
                "coordinator artifact recoverable quota consequence is invalid"
            ) from exc

        raw_conflicts = payload.get("conflicts_detected")
        if type(raw_conflicts) is not list or len(raw_conflicts) != len(expected_conflicts):
            raise ValueError("coordinator artifact conflict population drifted")
        restored_conflicts: list[FileConflict] = []
        for raw_conflict, expected_conflict in zip(
            raw_conflicts,
            expected_conflicts,
            strict=True,
        ):
            if not _mapping_has_exact_keys(
                raw_conflict,
                frozenset(
                    {
                        "file_path",
                        "ac_indices",
                        "resolved",
                        "resolution_description",
                    }
                ),
            ):
                raise ValueError("coordinator artifact conflict fields are invalid")
            assert isinstance(raw_conflict, Mapping)
            file_path = raw_conflict.get("file_path")
            raw_indices = raw_conflict.get("ac_indices")
            resolved = raw_conflict.get("resolved")
            resolution = raw_conflict.get("resolution_description")
            if (
                type(file_path) is not str
                or not file_path
                or len(file_path) > _MAX_COORDINATOR_CONFLICT_PATH_CHARS
                or file_path != expected_conflict.file_path
            ):
                raise ValueError("coordinator artifact conflict path is invalid")
            if (
                type(raw_indices) is not list
                or len(raw_indices) != len(expected_conflict.ac_indices)
                or any(type(index) is not int or index < 0 for index in raw_indices)
                or tuple(raw_indices) != expected_conflict.ac_indices
            ):
                raise ValueError("coordinator artifact conflict indices are invalid")
            if (
                type(resolved) is not bool
                or type(resolution) is not str
                or len(resolution) > _MAX_COORDINATOR_ITEM_CHARS
            ):
                raise ValueError("coordinator artifact conflict result is invalid")
            restored_conflicts.append(
                FileConflict(
                    file_path=file_path,
                    ac_indices=tuple(raw_indices),
                    resolved=resolved,
                    resolution_description=resolution,
                )
            )

        duration = payload.get("duration_seconds")
        if (
            not isinstance(duration, int | float)
            or isinstance(duration, bool)
            or not math.isfinite(duration)
            or duration < 0
        ):
            raise ValueError("coordinator artifact duration_seconds is invalid")

        return cls(
            level_number=level_number,
            conflicts_detected=tuple(restored_conflicts),
            review_summary=_require_bounded_string(
                payload.get("review_summary"),
                field_name="review_summary",
                max_chars=_MAX_COORDINATOR_SUMMARY_CHARS,
            )
            or "",
            fixes_applied=_require_bounded_string_tuple(
                payload.get("fixes_applied"),
                field_name="fixes_applied",
            ),
            warnings_for_next_level=_require_bounded_string_tuple(
                payload.get("warnings_for_next_level"),
                field_name="warnings_for_next_level",
            ),
            duration_seconds=float(duration),
            session_id=_require_bounded_string(
                payload.get("coordinator_session_id"),
                field_name="coordinator_session_id",
                max_chars=_MAX_COORDINATOR_ID_CHARS,
                optional=True,
            ),
            session_scope_id=session_scope_id,
            session_state_path=session_state_path,
            final_output=_require_bounded_string(
                payload.get("artifact"),
                field_name="artifact",
                max_chars=_MAX_COORDINATOR_ARTIFACT_CHARS,
            )
            or "",
            recoverable_quota_pause=recoverable_quota_pause,
        )


def _validate_coordinator_review(review: object) -> CoordinatorReview:
    """Validate every field persisted by the completed-event producer."""

    if not isinstance(review, CoordinatorReview):
        raise ValueError("coordinator review must use the closed review schema")
    if (
        type(review.level_number) is not int
        or review.level_number < 1
        or type(review.conflicts_detected) is not tuple
        or type(review.fixes_applied) is not tuple
        or len(review.fixes_applied) > _MAX_COORDINATOR_STRING_ITEMS
        or type(review.warnings_for_next_level) is not tuple
        or len(review.warnings_for_next_level) > _MAX_COORDINATOR_STRING_ITEMS
        or type(review.review_summary) is not str
        or len(review.review_summary) > _MAX_COORDINATOR_SUMMARY_CHARS
        or type(review.final_output) is not str
        or len(review.final_output) > _MAX_COORDINATOR_ARTIFACT_CHARS
        or (
            review.recoverable_quota_pause is not None
            and not isinstance(review.recoverable_quota_pause, UsageLimitPauseConsequence)
        )
        or any(
            type(item) is not str or len(item) > _MAX_COORDINATOR_ITEM_CHARS
            for item in (*review.fixes_applied, *review.warnings_for_next_level)
        )
        or not isinstance(review.duration_seconds, int | float)
        or isinstance(review.duration_seconds, bool)
        or not math.isfinite(review.duration_seconds)
        or review.duration_seconds < 0
    ):
        raise ValueError("coordinator review exceeds its durable bounds")
    if review.recoverable_quota_pause is not None:
        review.recoverable_quota_pause.to_payload()
    for conflict in review.conflicts_detected:
        _validate_file_conflict(conflict)
    _require_bounded_string(
        review.session_id,
        field_name="coordinator_session_id",
        max_chars=_MAX_COORDINATOR_ID_CHARS,
        optional=True,
    )
    _require_bounded_string(
        review.artifact_owner_id,
        field_name="artifact_owner_id",
        max_chars=_MAX_COORDINATOR_ID_CHARS,
    )
    _require_bounded_string(
        review.artifact_state_path,
        field_name="session_state_path",
        max_chars=_MAX_COORDINATOR_PATH_CHARS,
    )
    return review


class LevelCoordinator:
    """Coordinates between parallel execution levels.

    Detects file conflicts from AC execution results and optionally
    invokes Claude to resolve them.
    """

    def __init__(
        self,
        adapter: AgentRuntime,
        inherited_runtime_handle: RuntimeHandle | None = None,
        task_cwd: str | None = None,
        reasoning_effort: object = _REASONING_EFFORT_UNSET,
    ) -> None:
        """Initialize coordinator.

        Args:
            adapter: Agent runtime for conflict resolution sessions.
            inherited_runtime_handle: Optional parent Claude runtime handle for
                        delegated child executions.
            reasoning_effort: Frozen execution-contract effort. Omitting it
                        preserves standalone coordinator config lookup behavior.
        """
        self._adapter = adapter
        approval_mode = getattr(adapter, "permission_mode", None)
        self._inherited_runtime_handle = (
            replace(inherited_runtime_handle, approval_mode=approval_mode.strip())
            if inherited_runtime_handle is not None
            and isinstance(approval_mode, str)
            and approval_mode.strip()
            else inherited_runtime_handle
        )
        self._task_cwd = task_cwd
        self._level_runtime_handles: dict[tuple[str, int], RuntimeHandle] = {}
        self._announced_param_degradations: set[tuple[str, str]] = set()
        # Effort-first investment dial (RFC #1405): the coordinator's review pass
        # is a top-level unit (never a decomposed child), so it runs at the base
        # level on runtimes that enforce it. None ⇒ dormant. No effort_routed
        # event is emitted here because a review is not an acceptance criterion.
        if reasoning_effort is _REASONING_EFFORT_UNSET:
            from ouroboros.config import get_agent_reasoning_effort

            resolved_effort: str | None = get_agent_reasoning_effort()
        elif reasoning_effort is None or type(reasoning_effort) is str:
            resolved_effort = reasoning_effort
        else:
            raise TypeError("reasoning_effort must be a string or None")
        self._reasoning_effort = resolved_effort

    def _build_level_runtime_handle(
        self,
        execution_id: str,
        level_number: int,
        *,
        previous_review: CoordinatorReview | None = None,
    ) -> RuntimeHandle | None:
        """Build or resume the runtime handle for level-scoped coordinator work."""
        runtime_scope = build_level_coordinator_runtime_scope(execution_id, level_number)
        cache_key = (execution_id, level_number)
        seeded_handle = self._level_runtime_handles.get(cache_key)
        backend = self._adapter.runtime_backend
        if not backend:
            # Fallback: use inherited runtime handle if available
            return self._inherited_runtime_handle

        cwd = self._task_cwd or self._adapter.working_directory
        approval_mode = getattr(self._adapter, "permission_mode", None)
        native_session_id = seeded_handle.native_session_id if seeded_handle is not None else None
        if native_session_id is None and previous_review is not None:
            if previous_review.level_number == level_number:
                native_session_id = previous_review.session_id

        metadata: dict[str, object] = (
            dict(seeded_handle.metadata) if seeded_handle is not None else {}
        )
        metadata.update(
            {
                "scope": "level",
                "execution_id": execution_id,
                "level_number": level_number,
                "session_role": "coordinator",
                "session_scope_id": runtime_scope.aggregate_id,
                "session_state_path": runtime_scope.state_path,
            }
        )
        if seeded_handle is not None:
            return replace(
                seeded_handle,
                backend=backend,
                kind=seeded_handle.kind or _LEVEL_COORDINATOR_SESSION_KIND,
                native_session_id=native_session_id,
                cwd=(
                    seeded_handle.cwd
                    if seeded_handle.cwd
                    else cwd
                    if isinstance(cwd, str) and cwd
                    else None
                ),
                approval_mode=approval_mode
                if isinstance(approval_mode, str) and approval_mode
                else seeded_handle.approval_mode,
                updated_at=datetime.now(UTC).isoformat(),
                metadata=metadata,
            )

        return RuntimeHandle(
            backend=backend,
            kind=_LEVEL_COORDINATOR_SESSION_KIND,
            native_session_id=native_session_id,
            cwd=cwd if isinstance(cwd, str) and cwd else None,
            approval_mode=approval_mode
            if isinstance(approval_mode, str) and approval_mode
            else None,
            updated_at=datetime.now(UTC).isoformat(),
            metadata=metadata,
        )

    def _remember_level_runtime_handle(
        self,
        execution_id: str,
        level_number: int,
        runtime_handle: RuntimeHandle | None,
    ) -> None:
        """Cache the latest runtime handle for repeated same-level reconciliation."""
        if runtime_handle is None:
            return
        self._level_runtime_handles[(execution_id, level_number)] = runtime_handle

    @staticmethod
    def detect_file_conflicts(
        level_results: list[ACExecutionResult],
    ) -> list[FileConflict]:
        """Detect files modified by multiple ACs in the same level.

        Scans ACExecutionResult.messages for Write/Edit tool calls and
        identifies files touched by more than one AC.

        Args:
            level_results: Results from ACs executed in the same level.

        Returns:
            List of FileConflict for files modified by 2+ ACs.
        """
        # Map file_path → set of ac_indices that modified it
        file_to_acs: dict[str, set[int]] = defaultdict(set)

        for result in level_results:
            _collect_file_modifications(result, file_to_acs)

        # Filter to files with 2+ writers
        conflicts: list[FileConflict] = []
        for file_path, ac_indices in sorted(file_to_acs.items()):
            if len(ac_indices) >= 2:
                conflicts.append(
                    FileConflict(
                        file_path=file_path,
                        ac_indices=tuple(sorted(ac_indices)),
                    )
                )

        if conflicts:
            log.warning(
                "coordinator.conflicts_detected",
                conflict_count=len(conflicts),
                files=[c.file_path for c in conflicts],
            )
        else:
            log.info("coordinator.no_conflicts")

        return conflicts

    async def run_review(
        self,
        execution_id: str,
        conflicts: list[FileConflict],
        level_context: LevelContext,
        level_number: int,
        *,
        previous_review: CoordinatorReview | None = None,
    ) -> CoordinatorReview:
        """Run a Claude session to review and resolve file conflicts.

        Only called when conflicts are detected (Approach A).

        Args:
            conflicts: Detected file conflicts.
            level_context: Context from the completed level.
            level_number: Which level was just completed.

        Returns:
            CoordinatorReview with resolution details.
        """
        start_time = datetime.now(UTC)
        runtime_scope = build_level_coordinator_runtime_scope(execution_id, level_number)

        prompt = _build_review_prompt(conflicts, level_context, level_number)

        log.info(
            "coordinator.review.started",
            level=level_number,
            conflict_count=len(conflicts),
        )

        runtime_handle = self._build_level_runtime_handle(
            execution_id,
            level_number,
            previous_review=previous_review,
        )
        session_id: str | None = None
        final_text = ""
        messages: list[AgentMessage] = []
        tools = derive_coordinator_tools(self._adapter.runtime_backend)

        try:
            announce_execution_param_degradations(
                self._adapter,
                system_prompt=COORDINATOR_SYSTEM_PROMPT,
                tools=tools,
                announced=self._announced_param_degradations,
                log_event="coordinator.param_degraded",
            )
            from ouroboros.orchestrator.effort_routing import resolve_execute_effort

            _, effort_kwargs = resolve_execute_effort(
                self._adapter,
                base_effort=self._reasoning_effort,
                is_decomposed_child=False,
            )
            async for message in tracked_agent_task(
                self._adapter,
                role="executor_coordinator_review",
                prompt=prompt,
                tools=tools,
                system_prompt=COORDINATOR_SYSTEM_PROMPT,
                resume_handle=runtime_handle,
                **effort_kwargs,
            ):
                messages.append(message)
                if message.resume_handle is not None:
                    runtime_handle = message.resume_handle
                    self._remember_level_runtime_handle(
                        execution_id,
                        level_number,
                        runtime_handle,
                    )
                native_session_id = (
                    message.resume_handle.native_session_id
                    if message.resume_handle is not None
                    else None
                )
                metadata_session_id = message.data.get("session_id")
                for candidate_session_id in (native_session_id, metadata_session_id):
                    if (
                        type(candidate_session_id) is str
                        and 0 < len(candidate_session_id) <= _MAX_COORDINATOR_ID_CHARS
                    ):
                        session_id = candidate_session_id
                        break
                if message.is_final:
                    final_text = message.content[:_MAX_COORDINATOR_ARTIFACT_CHARS]
            self._remember_level_runtime_handle(execution_id, level_number, runtime_handle)

        except Exception as e:
            log.exception(
                "coordinator.review.failed",
                level=level_number,
                error=str(e),
            )
            self._remember_level_runtime_handle(execution_id, level_number, runtime_handle)
            duration = (datetime.now(UTC) - start_time).total_seconds()
            failure_text = f"Coordinator review failed: {e}"[:_MAX_COORDINATOR_SUMMARY_CHARS]
            return CoordinatorReview(
                level_number=level_number,
                conflicts_detected=tuple(conflicts),
                review_summary=failure_text,
                duration_seconds=duration,
                session_scope_id=runtime_scope.aggregate_id,
                session_state_path=runtime_scope.state_path,
                session_id=session_id,
                final_output=failure_text,
                messages=tuple(messages),
            )

        duration = (datetime.now(UTC) - start_time).total_seconds()

        # Parse structured response from Claude
        review = replace(
            _parse_review_response(
                final_text,
                conflicts,
                level_number,
                duration,
                session_id,
                session_scope_id=runtime_scope.aggregate_id,
                session_state_path=runtime_scope.state_path,
            ),
            final_output=final_text,
            messages=tuple(messages),
        )

        log.info(
            "coordinator.review.completed",
            level=level_number,
            fixes_applied=len(review.fixes_applied),
            warnings=len(review.warnings_for_next_level),
            duration_seconds=duration,
        )

        return review


def _collect_file_modifications(
    result: ACExecutionResult,
    file_to_acs: dict[str, set[int]],
) -> None:
    """Recursively collect file modifications from an AC result.

    Handles both atomic and decomposed (Sub-AC) results.

    Args:
        result: AC execution result to scan.
        file_to_acs: Accumulator mapping file_path → ac_indices.
    """
    # Every durable node stores only its local canonical file projection. Walk
    # the complete tree for both live transcripts and replayed projections so a
    # grandchild has identical coordinator semantics before and after resume.
    root_ac_index = result.ac_index

    def visit(current: ACExecutionResult) -> None:
        if current.conflict_files is not None:
            for file_path in current.conflict_files:
                file_to_acs.setdefault(file_path, set()).add(root_ac_index)
        else:
            for msg in current.messages:
                if msg.tool_name not in ("Write", "Edit"):
                    continue
                tool_input = msg.data.get("tool_input", {})
                if not isinstance(tool_input, Mapping):
                    continue
                file_path = tool_input.get("file_path")
                if isinstance(file_path, str) and file_path:
                    file_to_acs.setdefault(file_path, set()).add(root_ac_index)
        for sub_result in current.sub_results:
            visit(sub_result)

    visit(result)


def _build_review_prompt(
    conflicts: list[FileConflict],
    level_context: LevelContext,
    level_number: int,
) -> str:
    """Build the prompt for the Coordinator Claude session.

    Args:
        conflicts: Detected file conflicts.
        level_context: Context from the completed level.
        level_number: Which level was just completed.

    Returns:
        Formatted prompt string.
    """
    context_text = level_context.to_prompt_text()

    conflict_lines: list[str] = []
    for conflict in conflicts:
        ac_list = ", ".join(f"AC {i + 1}" for i in conflict.ac_indices)
        conflict_lines.append(f"- `{conflict.file_path}` modified by: {ac_list}")
    conflict_text = "\n".join(conflict_lines)

    return f"""Review the results of Level {level_number} parallel AC execution.

## Level {level_number} Results
{context_text}

## File Conflicts Detected
{conflict_text}

## Your Tasks
1. Read the conflicting files using the Read tool
2. Run `git diff` if needed to understand changes
3. If edits from different ACs conflict, resolve them using the Edit tool
4. Provide your review as a structured JSON response:

```json
{{
  "review_summary": "Brief analysis of the level results",
  "fixes_applied": ["Description of fix 1", "..."],
  "warnings_for_next_level": ["Warning 1 for next ACs", "..."],
  "conflicts_resolved": ["{conflicts[0].file_path if conflicts else ""}"]
}}
```

Respond with the JSON block after completing your review and any fixes.
"""


def _parse_review_response(
    response_text: str,
    original_conflicts: list[FileConflict],
    level_number: int,
    duration: float,
    session_id: str | None,
    *,
    session_scope_id: str | None = None,
    session_state_path: str | None = None,
) -> CoordinatorReview:
    """Parse the Coordinator's structured JSON response.

    Falls back to using the raw response as review_summary if JSON parsing fails.

    Args:
        response_text: Raw response from the Coordinator Claude session.
        original_conflicts: Original conflict list for resolution tracking.
        level_number: Level that was reviewed.
        duration: Time spent on review.
        session_id: Claude session ID.

    Returns:
        CoordinatorReview populated from the parsed response.
    """
    bounded_response = response_text[:_MAX_COORDINATOR_ARTIFACT_CHARS]
    review_summary = ""
    fixes_applied: list[str] = []
    warnings: list[str] = []
    resolved_files: set[str] = set()

    # Try to extract JSON from the response
    json_match = re.search(r"```json\s*\n(.*?)\n```", bounded_response, re.DOTALL)
    if not json_match:
        # Try bare JSON object
        json_match = re.search(r"\{[^{}]*\}", bounded_response, re.DOTALL)

    if json_match:
        try:
            data = json.loads(
                json_match.group(1) if "```" in json_match.group() else json_match.group()
            )
            raw_summary = data.get("review_summary", "")
            raw_fixes = data.get("fixes_applied", [])
            raw_warnings = data.get("warnings_for_next_level", [])
            raw_resolved = data.get("conflicts_resolved", [])
            if type(raw_summary) is str:
                review_summary = raw_summary[:_MAX_COORDINATOR_SUMMARY_CHARS]
            if type(raw_fixes) is list:
                fixes_applied = [
                    item[:_MAX_COORDINATOR_ITEM_CHARS]
                    for item in raw_fixes[:_MAX_COORDINATOR_STRING_ITEMS]
                    if type(item) is str
                ]
            if type(raw_warnings) is list:
                warnings = [
                    item[:_MAX_COORDINATOR_ITEM_CHARS]
                    for item in raw_warnings[:_MAX_COORDINATOR_STRING_ITEMS]
                    if type(item) is str
                ]
            if type(raw_resolved) is list:
                resolved_files = {
                    item
                    for item in raw_resolved
                    if type(item) is str and 0 < len(item) <= _MAX_COORDINATOR_CONFLICT_PATH_CHARS
                }
        except (AttributeError, json.JSONDecodeError, IndexError):
            log.warning(
                "coordinator.parse_failed",
                response_preview=bounded_response[:200],
            )

    # Fallback: use raw text as summary
    if not review_summary:
        review_summary = bounded_response[:500].strip() if bounded_response else "No review output"

    # Mark conflicts as resolved based on Coordinator's report
    updated_conflicts: list[FileConflict] = []
    for conflict in original_conflicts:
        is_resolved = conflict.file_path in resolved_files
        updated_conflicts.append(
            FileConflict(
                file_path=conflict.file_path,
                ac_indices=conflict.ac_indices,
                resolved=is_resolved,
                resolution_description="Resolved by Coordinator" if is_resolved else "",
            )
        )

    return CoordinatorReview(
        level_number=level_number,
        conflicts_detected=tuple(updated_conflicts),
        review_summary=review_summary,
        fixes_applied=tuple(fixes_applied),
        warnings_for_next_level=tuple(warnings),
        duration_seconds=duration,
        session_id=session_id,
        session_scope_id=session_scope_id,
        session_state_path=session_state_path,
    )


__all__ = [
    "CoordinatorReview",
    "FileConflict",
    "LevelCoordinator",
    "build_coordinator_started_payload",
    "derive_coordinator_tools",
    "validate_coordinator_started_payload",
]
