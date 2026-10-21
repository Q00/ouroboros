"""Read-only Project Map projection over durable session events.

The EventStore remains authoritative.  This module only validates immutable
project anchors, reuses :class:`SessionRepository` lifecycle reconstruction,
and returns frozen values; it has no append or execution-control surface.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from ouroboros.core.project_identity import ProjectIdentity, ProjectIdentityError
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.session import SessionRepository, SessionStatus

if TYPE_CHECKING:
    from ouroboros.persistence.event_store import EventStore

PROJECT_MAP_SCHEMA_VERSION = 1
_SESSION_STARTED = "orchestrator.session.started"
_TOP_LEVEL_IDENTITY_KEYS = frozenset({"project_id", "project_root", "workspace_path"})
_NESTED_IDENTITY_KEYS = frozenset({"project_root", "workspace_path"})

ProjectIdentitySource = Literal["session_start", "execution_contract"]


class ProjectProjectionError(RuntimeError):
    """Raised when a complete deterministic Project Map cannot be built."""


class ProjectIdentityConflictError(ProjectProjectionError):
    """Raised when one attributable start event has conflicting identity."""


class ProjectRunLimitError(ProjectProjectionError):
    """Raised instead of returning an unmarked partial project map."""

    def __init__(self, *, project_id: str, actual_runs: int, limit: int) -> None:
        self.project_id = project_id
        self.actual_runs = actual_runs
        self.limit = limit
        super().__init__(
            f"Project {project_id} has {actual_runs} runs, exceeding the explicit limit {limit}"
        )


def _non_empty_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a non-empty canonical string")
    return value


def _optional_non_empty_string(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _non_empty_string(value, field=field)


def _path_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty path string")
    return value


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ProjectRunSummary(BaseModel, frozen=True):
    """One project-attributed session with status owned by SessionRepository."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = PROJECT_MAP_SCHEMA_VERSION
    project_id: str
    project_root: str
    workspace_path: str
    identity_source: ProjectIdentitySource
    session_id: str
    execution_id: str
    seed_id: str
    seed_goal: str | None = None
    gate_forced: bool | None = None
    runtime_backend: str | None = None
    llm_backend: str | None = None
    status: SessionStatus
    started_at: datetime
    source_start_event_id: str

    @field_validator(
        "project_id",
        "session_id",
        "execution_id",
        "seed_id",
        "source_start_event_id",
    )
    @classmethod
    def _validate_required_strings(cls, value: object, info) -> str:
        return _non_empty_string(value, field=info.field_name)

    @field_validator("project_root", "workspace_path")
    @classmethod
    def _validate_paths(cls, value: object, info) -> str:
        return _path_string(value, field=info.field_name)

    @field_validator("runtime_backend", "llm_backend")
    @classmethod
    def _validate_optional_strings(cls, value: object, info) -> str | None:
        return _optional_non_empty_string(value, field=info.field_name)

    @field_validator("seed_goal")
    @classmethod
    def _validate_seed_goal(cls, value: object) -> str | None:
        if value is not None and not isinstance(value, str):
            raise ValueError("seed_goal must be a string when present")
        return value

    @field_validator("started_at")
    @classmethod
    def _normalize_started_at(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def _validate_identity(self) -> ProjectRunSummary:
        ProjectIdentity(
            project_id=self.project_id,
            project_root=self.project_root,
            workspace_path=self.workspace_path,
        )
        return self


def _gate_counts(
    runs: tuple[ProjectRunSummary, ...] | list[ProjectRunSummary],
) -> tuple[int, int, int, float | None]:
    """Summarize one ambiguity-gate decision per distinct Seed.

    A Seed can be executed more than once, so run-attempt cardinality must not
    inflate the project-level Seed statistics. Known and unknown observations
    are merged conservatively: a known decision wins over an unknown repeat,
    while conflicting known decisions are treated as unknown.
    """
    decisions: dict[str, bool | None] = {}
    conflicts: set[str] = set()
    for run in runs:
        seed_id = run.seed_id
        current = run.gate_forced
        if seed_id not in decisions:
            decisions[seed_id] = current
            continue
        if seed_id in conflicts:
            continue
        previous = decisions[seed_id]
        if previous is None:
            decisions[seed_id] = current
        elif current is not None and previous != current:
            decisions[seed_id] = None
            conflicts.add(seed_id)

    gated = sum(decision is False for decision in decisions.values())
    forced = sum(decision is True for decision in decisions.values())
    unknown = sum(decision is None for decision in decisions.values())
    known = gated + forced
    rate = forced / known if known else None
    return gated, forced, unknown, rate


class ProjectRecord(BaseModel, frozen=True):
    """Complete immutable run projection for one project and optional workspace."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = PROJECT_MAP_SCHEMA_VERSION
    project_id: str
    project_root: str
    workspace_path: str | None = None
    run_count: int
    runs: tuple[ProjectRunSummary, ...]
    gated_seed_count: int = 0
    forced_seed_count: int = 0
    unknown_seed_count: int = 0
    override_rate: float | None = None

    @field_validator("project_id")
    @classmethod
    def _validate_required_strings(cls, value: object, info) -> str:
        return _non_empty_string(value, field=info.field_name)

    @field_validator("project_root")
    @classmethod
    def _validate_project_root(cls, value: object) -> str:
        return _path_string(value, field="project_root")

    @field_validator("workspace_path")
    @classmethod
    def _validate_workspace_path(cls, value: object) -> str | None:
        return None if value is None else _path_string(value, field="workspace_path")

    @field_validator("gated_seed_count", "forced_seed_count", "unknown_seed_count")
    @classmethod
    def _validate_gate_counts(cls, value: object, info) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{info.field_name} must be a non-negative integer")
        return value

    @field_validator("override_rate")
    @classmethod
    def _validate_override_rate(cls, value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
            raise ValueError("override_rate must be between 0 and 1")
        return float(value)

    @model_validator(mode="after")
    def _validate_population(self) -> ProjectRecord:
        scope = self.workspace_path or "."
        ProjectIdentity(
            project_id=self.project_id,
            project_root=self.project_root,
            workspace_path=scope,
        )
        if self.run_count != len(self.runs):
            raise ValueError("run_count must equal the complete run population")
        expected_counts = _gate_counts(self.runs)
        actual_counts = (
            self.gated_seed_count,
            self.forced_seed_count,
            self.unknown_seed_count,
            self.override_rate,
        )
        stats_fields = {
            "gated_seed_count",
            "forced_seed_count",
            "unknown_seed_count",
            "override_rate",
        }
        if not stats_fields.intersection(self.model_fields_set):
            object.__setattr__(self, "gated_seed_count", expected_counts[0])
            object.__setattr__(self, "forced_seed_count", expected_counts[1])
            object.__setattr__(self, "unknown_seed_count", expected_counts[2])
            object.__setattr__(self, "override_rate", expected_counts[3])
        elif actual_counts != expected_counts:
            raise ValueError("gate statistics must match the complete run population")
        for run in self.runs:
            if run.project_id != self.project_id or run.project_root != self.project_root:
                raise ValueError("every run must belong to the ProjectRecord identity")
            if self.workspace_path is not None and run.workspace_path != self.workspace_path:
                raise ValueError("every run must match the requested workspace filter")
        return self


@dataclass(frozen=True, slots=True)
class _AttributedStart:
    event: BaseEvent
    identity: ProjectIdentity
    identity_source: ProjectIdentitySource


def _nested_scope(data: Mapping[str, object]) -> tuple[Mapping[str, object] | None, set[str]]:
    contract = data.get("execution_contract")
    if not isinstance(contract, Mapping):
        return None, set()
    proof = contract.get("frugality_proof")
    if not isinstance(proof, Mapping):
        return None, set()
    return proof, set(_NESTED_IDENTITY_KEYS.intersection(proof))


def _is_project_candidate(event: BaseEvent, project: ProjectIdentity) -> bool:
    data = event.data
    proof, _ = _nested_scope(data)
    return (
        data.get("project_id") == project.project_id
        or data.get("project_root") == project.project_root
        or (proof is not None and proof.get("project_root") == project.project_root)
    )


def _identity_from_start(event: BaseEvent, project: ProjectIdentity) -> _AttributedStart:
    data = event.data
    top_keys = set(_TOP_LEVEL_IDENTITY_KEYS.intersection(data))
    proof, nested_keys = _nested_scope(data)
    if top_keys and top_keys != _TOP_LEVEL_IDENTITY_KEYS:
        raise ProjectIdentityConflictError(
            f"Session {event.aggregate_id} has a partial top-level project identity"
        )
    if nested_keys and nested_keys != _NESTED_IDENTITY_KEYS:
        raise ProjectIdentityConflictError(
            f"Session {event.aggregate_id} has a partial execution-contract project identity"
        )

    try:
        top_identity = (
            ProjectIdentity(
                project_id=data["project_id"],
                project_root=data["project_root"],
                workspace_path=data["workspace_path"],
            )
            if top_keys
            else None
        )
        nested_identity = (
            ProjectIdentity.from_root(
                proof["project_root"],
                workspace_path=proof["workspace_path"],
            )
            if proof is not None and nested_keys
            else None
        )
    except (KeyError, TypeError, ProjectIdentityError) as exc:
        raise ProjectIdentityConflictError(
            f"Session {event.aggregate_id} has an invalid project identity"
        ) from exc

    if top_identity is not None:
        if nested_identity is not None and top_identity != nested_identity:
            raise ProjectIdentityConflictError(
                f"Session {event.aggregate_id} has conflicting project identity anchors"
            )
        identity = top_identity
        source: ProjectIdentitySource = "session_start"
    elif nested_identity is not None:
        identity = nested_identity
        source = "execution_contract"
    else:
        raise ProjectIdentityConflictError(
            f"Session {event.aggregate_id} has no complete project identity"
        )

    if identity.project_id != project.project_id or identity.project_root != project.project_root:
        raise ProjectIdentityConflictError(
            f"Session {event.aggregate_id} conflicts with the requested project identity"
        )
    return _AttributedStart(event=event, identity=identity, identity_source=source)


class ProjectMapBuilder:
    """Build complete Project Map values without mutating the EventStore."""

    def __init__(self, event_store: EventStore) -> None:
        self._event_store = event_store
        self._sessions = SessionRepository(event_store)

    async def build(
        self,
        project: ProjectIdentity,
        *,
        workspace_path: str | None = None,
        limit: int = 100,
    ) -> ProjectRecord:
        """Rebuild one complete project view or fail instead of truncating it."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        if workspace_path is not None:
            workspace_path = ProjectIdentity(
                project_id=project.project_id,
                project_root=project.project_root,
                workspace_path=workspace_path,
            ).workspace_path

        try:
            lifecycle = await self._event_store.get_all_sessions()
        except Exception as exc:
            raise ProjectProjectionError("Cannot enumerate complete session history") from exc

        start_events = [event for event in lifecycle if event.type == _SESSION_STARTED]
        start_counts = Counter(event.aggregate_id for event in start_events)
        starts: list[_AttributedStart] = []
        for event in sorted(start_events, key=lambda item: (_utc(item.timestamp), item.id)):
            if not _is_project_candidate(event, project):
                continue
            if start_counts[event.aggregate_id] != 1:
                raise ProjectIdentityConflictError(
                    f"Session {event.aggregate_id} has duplicate start identity events"
                )
            attributed = _identity_from_start(event, project)
            if workspace_path is None or attributed.identity.workspace_path == workspace_path:
                starts.append(attributed)

        if len(starts) > limit:
            raise ProjectRunLimitError(
                project_id=project.project_id,
                actual_runs=len(starts),
                limit=limit,
            )

        runs = [await self._run_summary(start) for start in starts]
        runs.sort(key=lambda run: (run.started_at, run.session_id, run.source_start_event_id))
        gated, forced, unknown, override_rate = _gate_counts(runs)
        return ProjectRecord(
            project_id=project.project_id,
            project_root=project.project_root,
            workspace_path=workspace_path,
            run_count=len(runs),
            runs=tuple(runs),
            gated_seed_count=gated,
            forced_seed_count=forced,
            unknown_seed_count=unknown,
            override_rate=override_rate,
        )

    async def _run_summary(self, start: _AttributedStart) -> ProjectRunSummary:
        event = start.event
        try:
            reconstructed = await self._sessions.reconstruct_session(
                event.aggregate_id,
                strict_related_events=True,
            )
        except Exception as exc:
            raise ProjectProjectionError(
                f"Cannot reconstruct project session {event.aggregate_id}"
            ) from exc
        if reconstructed.is_err:
            raise ProjectProjectionError(
                f"Cannot reconstruct project session {event.aggregate_id}"
            ) from reconstructed.error
        tracker = reconstructed.value
        try:
            execution_id = _non_empty_string(event.data.get("execution_id"), field="execution_id")
            seed_id = _non_empty_string(event.data.get("seed_id"), field="seed_id")
        except ValueError as exc:
            raise ProjectProjectionError(
                f"Cannot represent project session {event.aggregate_id}"
            ) from exc
        if (
            tracker.session_id != event.aggregate_id
            or tracker.execution_id != execution_id
            or tracker.seed_id != seed_id
        ):
            raise ProjectProjectionError(
                f"Reconstructed identity conflicts for project session {event.aggregate_id}"
            )
        try:
            return ProjectRunSummary(
                project_id=start.identity.project_id,
                project_root=start.identity.project_root,
                workspace_path=start.identity.workspace_path,
                identity_source=start.identity_source,
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                seed_id=tracker.seed_id,
                seed_goal=event.data.get("seed_goal"),
                gate_forced=event.data.get("gate_forced"),
                runtime_backend=event.data.get("runtime_backend"),
                llm_backend=event.data.get("llm_backend"),
                status=tracker.status,
                started_at=tracker.start_time,
                source_start_event_id=event.id,
            )
        except (TypeError, ValueError) as exc:
            raise ProjectProjectionError(
                f"Cannot represent project session {event.aggregate_id}"
            ) from exc


__all__ = [
    "PROJECT_MAP_SCHEMA_VERSION",
    "ProjectIdentityConflictError",
    "ProjectMapBuilder",
    "ProjectProjectionError",
    "ProjectRecord",
    "ProjectRunLimitError",
    "ProjectRunSummary",
]
