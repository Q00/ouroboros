"""Run / Stage / Step / Artifact / Verdict projection records.

These records are the public, schema-versioned read-model that the Ouroboros
harness presents over the underlying ``EventStore``. They give plugins,
maintainers, evaluation pipelines, and CLI consumers a stable vocabulary
for describing the work a run performed without each consumer reinventing
its own status format.

Design constraints (per the acceptance criteria attached to issue #946):

* Records are immutable Pydantic models (``frozen=True``) so they can be
  treated as values in projections, comparisons, and persistence layers.
  ``metadata`` mappings are wrapped in :class:`types.MappingProxyType`
  views to block top-level ``record.metadata[key] = value`` mutation, and
  tuple-of-identifier fields reject blank or whitespace-only entries so
  cross-record references remain usable.
* Every ``StepRecord`` either links to one or more source event IDs via
  :attr:`StepRecord.source_event_ids`, or explicitly marks itself as legacy
  or inferred via :attr:`StepRecord.legacy_inferred`. Projections that
  cannot honor this invariant should refuse to construct the record.
* ``ArtifactRecord`` exposes a small, owner-agnostic shape so plugins can
  attach output to steps without inventing plugin-local schemas.
* Each record carries a ``schema_version``; v1 is the initial release and
  future additive fields bump the version.

This module intentionally contains *only* the record schema and a small
set of factory helpers. The projection builder that walks ``EventStore``
events into these records is delivered in a follow-up PR so this surface
can be reviewed independently of any wiring.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

PROJECTION_SCHEMA_VERSION = 1
"""Initial schema version for the projection vocabulary."""

ProjectionSchemaVersion = Literal[1]
"""Supported projection schema version for v1 records."""


# ---------------------------------------------------------------------------
# Internal helpers — immutable metadata + identifier hygiene
# ---------------------------------------------------------------------------


def _freeze_value(value: Any) -> Any:
    """Recursively copy common JSON-like containers into immutable views."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze_value(item) for item in value)
    return value


def _thaw_value(value: Any) -> Any:
    """Convert immutable container views back to plain serializable containers."""
    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple | frozenset):
        return [_thaw_value(item) for item in value]
    return value


def _coerce_to_mapping(value: Any) -> Mapping[str, Any]:
    """Normalize incoming mapping-shaped input into a recursively frozen copy."""
    if value is None:
        return MappingProxyType({})
    if isinstance(value, Mapping):
        frozen = _freeze_value(value)
        if isinstance(frozen, MappingProxyType):
            return frozen
    msg = f"metadata must be a mapping, got {type(value).__name__}"
    raise ValueError(msg)


def _empty_frozen_metadata() -> Mapping[str, Any]:
    """Default factory that returns an empty read-only mapping.

    Returning a ``MappingProxyType`` directly avoids relying on Pydantic
    running validators against default values; the runtime invariant
    that ``record.metadata`` is read-only must hold even when no
    metadata was supplied at construction time.
    """
    return MappingProxyType({})


def _ensure_frozen_after(value: Any) -> Mapping[str, Any]:
    """Final-stage wrapper guaranteeing recursively immutable values."""
    if isinstance(value, Mapping):
        frozen = _freeze_value(value)
        if isinstance(frozen, MappingProxyType):
            return frozen
    msg = f"metadata must be a mapping, got {type(value).__name__}"
    raise ValueError(msg)


FrozenMetadata = Annotated[
    Mapping[str, Any],
    BeforeValidator(_coerce_to_mapping),
    AfterValidator(_ensure_frozen_after),
    PlainSerializer(lambda value: _thaw_value(value), return_type=dict, when_used="always"),
]
"""Metadata field type that blocks top-level dict mutation.

Once a record is constructed the resulting ``Mapping`` is a
``MappingProxyType`` view, so ``record.metadata[key] = value`` raises
``TypeError`` at runtime. Serializers convert the view back to a plain
``dict`` for JSON output.
"""


def _normalize_id_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    """Reject empty or whitespace-only identifiers inside a tuple field.

    Trims each entry so consumers can rely on the stored value matching
    the canonical id format used elsewhere in the projection vocabulary.
    Raises ``TypeError`` on non-string entries and ``ValueError`` on
    blank or whitespace-only entries.
    """
    normalized: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str):
            msg = f"identifier at index {index} must be a string; got {type(value).__name__}"
            raise TypeError(msg)
        stripped = value.strip()
        if not stripped:
            msg = (
                f"identifier at index {index} is empty or whitespace-only; "
                "projections require usable IDs for cross-record references"
            )
            raise ValueError(msg)
        normalized.append(stripped)
    return tuple(normalized)


IdentifierTuple = Annotated[tuple[str, ...], AfterValidator(_normalize_id_tuple)]
"""Tuple-of-identifier field type that rejects blank or whitespace entries."""


def _normalize_identifier(value: str) -> str:
    """Trim and reject blank scalar identifiers used for cross-record links."""
    if not isinstance(value, str):
        msg = f"identifier must be a string; got {type(value).__name__}"
        raise TypeError(msg)
    stripped = value.strip()
    if not stripped:
        msg = "identifier is empty or whitespace-only; projections require usable IDs"
        raise ValueError(msg)
    return stripped


Identifier = Annotated[str, AfterValidator(_normalize_identifier)]
"""Scalar identifier type that trims whitespace and rejects blank values."""


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class StageKind(StrEnum):
    """Named harness phases that group ``StepRecord`` entries.

    Values match the public surface names users see in CLI output and
    documentation. Additional kinds are added as additive entries; old
    values are retained for replay compatibility.
    """

    INTERVIEW = "interview"
    SEED = "seed"
    EXECUTE = "execute"
    EVALUATE = "evaluate"
    EVOLVE = "evolve"
    PLUGIN = "plugin"
    HITL = "hitl"


class StepKind(StrEnum):
    """Bounded unit-of-work classifications for ``StepRecord``."""

    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    SHELL_COMMAND = "shell_command"
    SUBAGENT_DISPATCH = "subagent_dispatch"
    PLUGIN_COMMAND = "plugin_command"
    EVALUATION_CHECK = "evaluation_check"
    EVIDENCE_SUBMISSION = "evidence_submission"
    HARNESS_INTERNAL = "harness_internal"


class VerdictOutcome(StrEnum):
    """Terminal status of a verdict record."""

    PASS = "pass"
    FAIL = "fail"
    ESCALATE_HUMAN = "escalate_human"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Record models
# ---------------------------------------------------------------------------


def _new_id(prefix: str) -> str:
    """Return a stable, prefixed identifier suitable for projection records."""
    return f"{prefix}_{uuid4().hex[:12]}"


class ArtifactRecord(BaseModel, frozen=True):
    """A produced artifact attached to a single step.

    Artifacts are owner-agnostic — plugins and core harness modules use the
    same shape. The ``kind`` discriminator allows downstream consumers to
    filter without inspecting opaque payloads.

    Attributes:
        artifact_id: Stable identifier for cross-record references.
        step_id: Identifier of the producing :class:`StepRecord`.
        kind: Short discriminator such as ``"file"``, ``"patch"``,
            ``"verdict"``, ``"evidence"``, ``"log_excerpt"``, ``"capsule"``.
            The set is intentionally open so plugins can register new kinds
            in a later PR; the harness only enforces that the value is a
            non-empty string.
        path: Optional filesystem path or URI when the artifact lives on
            disk or in remote storage.
        media_type: Optional IANA media type (for example
            ``"application/json"`` or ``"text/markdown"``).
        size_bytes: Optional payload size for storage-aware consumers.
        digest: Optional content digest (``algorithm:hex``) so artifacts
            can be addressed without holding their payload in memory.
        summary: Short, human-readable description suitable for CLI output.
        metadata: Free-form metadata bag, stored as a read-only
            ``MappingProxyType`` view. Consumers must treat it as opaque
            and additive; top-level mutation is blocked at runtime.
    """

    schema_version: ProjectionSchemaVersion = PROJECTION_SCHEMA_VERSION
    artifact_id: Identifier = Field(default_factory=lambda: _new_id("artifact"))
    step_id: Identifier
    kind: str = Field(..., min_length=1)
    path: str | None = Field(default=None)
    media_type: str | None = Field(default=None)
    size_bytes: int | None = Field(default=None, ge=0)
    digest: str | None = Field(default=None)
    summary: str = Field(default="")
    metadata: FrozenMetadata = Field(default_factory=_empty_frozen_metadata)

    @field_validator("kind")
    @classmethod
    def _kind_must_be_non_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            msg = "ArtifactRecord.kind must be a non-empty string"
            raise ValueError(msg)
        return normalized


class StepRecord(BaseModel, frozen=True):
    """One bounded unit of work observed by the harness.

    A step is the projection equivalent of a single model call, tool call,
    shell command, plugin dispatch, or harness-internal action. It is the
    smallest publicly addressable unit of work in a run.

    Attributes:
        step_id: Stable identifier for cross-record references.
        run_id: Identifier of the owning :class:`RunRecord`.
        stage_id: Identifier of the owning :class:`StageRecord`.
        kind: Discriminator from :class:`StepKind`.
        name: Short human-readable label such as the tool name, command
            name, or plugin command.
        ac_id: Optional acceptance-criterion identifier when the step is
            executed inside an AC context.
        started_at: When the unit of work began.
        ended_at: When the unit of work finished (``None`` while running).
        ok: Optional success indicator. ``True`` indicates success,
            ``False`` indicates failure, ``None`` means undetermined.
        source_event_ids: Identifiers of the source events that produced
            this projection. Empty only when ``legacy_inferred`` is True;
            blank or whitespace entries are rejected so projection
            consumers can rely on every entry being a usable id.
        legacy_inferred: Marks the record as projected from legacy data
            that did not preserve enough metadata to link source events.
            Consumers may filter on this flag when computing audit views.
        artifact_ids: Identifiers of artifacts produced by the step. Each
            value must match a :attr:`ArtifactRecord.artifact_id`; blank
            entries are rejected.
        metadata: Free-form metadata bag, stored as a read-only
            ``MappingProxyType`` view.
    """

    schema_version: ProjectionSchemaVersion = PROJECTION_SCHEMA_VERSION
    step_id: Identifier = Field(default_factory=lambda: _new_id("step"))
    run_id: Identifier
    stage_id: Identifier
    kind: StepKind
    name: str = Field(default="", description="Short human-readable label")
    ac_id: Identifier | None = Field(default=None)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = Field(default=None)
    ok: bool | None = Field(default=None)
    source_event_ids: IdentifierTuple = Field(default_factory=tuple)
    legacy_inferred: bool = Field(default=False)
    artifact_ids: IdentifierTuple = Field(default_factory=tuple)
    metadata: FrozenMetadata = Field(default_factory=_empty_frozen_metadata)

    @field_validator("ac_id")
    @classmethod
    def _ac_id_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            msg = (
                "StepRecord.ac_id must be None or a non-blank identifier; "
                "blank strings break cross-record references"
            )
            raise ValueError(msg)
        return stripped

    @model_validator(mode="after")
    def _enforce_source_event_invariant(self) -> StepRecord:
        if not self.source_event_ids and not self.legacy_inferred:
            msg = (
                "StepRecord must link source_event_ids or set legacy_inferred=True; "
                "see #946 acceptance criterion #3."
            )
            raise ValueError(msg)
        if self.ended_at is not None and self.ended_at < self.started_at:
            msg = "StepRecord.ended_at cannot precede started_at"
            raise ValueError(msg)
        return self


class StageRecord(BaseModel, frozen=True):
    """A named harness phase that groups related step records.

    Stages mirror the user-visible workflow phases (``interview``,
    ``seed``, ``execute``, ``evaluate``, ``evolve``, ``plugin``,
    ``hitl``). They allow CLI and inspection tooling to summarize work at
    a level coarser than individual steps but finer than an entire run.

    Attributes:
        stage_id: Stable identifier for cross-record references.
        run_id: Identifier of the owning :class:`RunRecord`.
        kind: Phase discriminator from :class:`StageKind`.
        started_at: When the stage entered.
        ended_at: When the stage exited (``None`` while active).
        step_ids: Step identifiers contained in this stage, in execution
            order. Blank entries are rejected.
        metadata: Free-form metadata bag, stored as a read-only
            ``MappingProxyType`` view.
    """

    schema_version: ProjectionSchemaVersion = PROJECTION_SCHEMA_VERSION
    stage_id: Identifier = Field(default_factory=lambda: _new_id("stage"))
    run_id: Identifier
    kind: StageKind
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = Field(default=None)
    step_ids: IdentifierTuple = Field(default_factory=tuple)
    metadata: FrozenMetadata = Field(default_factory=_empty_frozen_metadata)

    @model_validator(mode="after")
    def _validate_timestamps(self) -> StageRecord:
        if self.ended_at is not None and self.ended_at < self.started_at:
            msg = "StageRecord.ended_at cannot precede started_at"
            raise ValueError(msg)
        return self


class VerdictRecord(BaseModel, frozen=True):
    """Run- or AC-level verdict with explicit evidence links.

    Verdicts are the harness-owned terminal judgment over either an entire
    run or an individual acceptance criterion. They reference the source
    events and artifacts that justified the outcome so consumers do not
    have to mine raw logs to defend the decision later.

    Attributes:
        verdict_id: Stable identifier.
        run_id: Identifier of the owning :class:`RunRecord`.
        scope: ``"run"`` for run-level verdicts, ``"ac"`` for per-AC.
        ac_id: Acceptance-criterion identifier when ``scope == "ac"``;
            must be ``None`` otherwise. Blank strings are rejected.
        outcome: Terminal status from :class:`VerdictOutcome`.
        rationale: Short structured rationale string. Consumers should
            treat it as a stable summary, not as the full evidence body.
        evidence_event_ids: Identifiers of source events backing this
            verdict. Blank entries are rejected.
        evidence_artifact_ids: Identifiers of artifacts backing this
            verdict. Blank entries are rejected.
        recorded_at: When the verdict was recorded.
        metadata: Free-form metadata bag, stored as a read-only
            ``MappingProxyType`` view.
    """

    schema_version: ProjectionSchemaVersion = PROJECTION_SCHEMA_VERSION
    verdict_id: Identifier = Field(default_factory=lambda: _new_id("verdict"))
    run_id: Identifier
    scope: Literal["run", "ac"]
    ac_id: Identifier | None = Field(default=None)
    outcome: VerdictOutcome
    rationale: str = Field(default="")
    evidence_event_ids: IdentifierTuple = Field(default_factory=tuple)
    evidence_artifact_ids: IdentifierTuple = Field(default_factory=tuple)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: FrozenMetadata = Field(default_factory=_empty_frozen_metadata)

    @field_validator("ac_id")
    @classmethod
    def _ac_id_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            msg = "VerdictRecord.ac_id must be None or a non-blank identifier"
            raise ValueError(msg)
        return stripped

    @model_validator(mode="after")
    def _validate_scope_and_ac(self) -> VerdictRecord:
        if self.scope == "ac" and not self.ac_id:
            msg = "VerdictRecord with scope='ac' must include ac_id"
            raise ValueError(msg)
        if self.scope == "run" and self.ac_id is not None:
            msg = "VerdictRecord with scope='run' must not include ac_id"
            raise ValueError(msg)
        return self


class RunRecord(BaseModel, frozen=True):
    """A single user-goal / Seed execution envelope.

    A run is the top-level projection unit. It carries the goal, the seed
    identifier, the stage sequence, and the run-level verdict (when
    available).

    Attributes:
        run_id: Stable identifier for the run.
        seed_id: Identifier of the seed that drove the run.
        goal: Human-readable goal text.
        started_at: When the run began.
        ended_at: When the run finished (``None`` while still active).
        stage_ids: Stage identifiers in execution order. Blank entries
            are rejected.
        verdict_id: Optional identifier of the run-level
            :class:`VerdictRecord`.
        metadata: Free-form metadata bag, stored as a read-only
            ``MappingProxyType`` view.
    """

    schema_version: ProjectionSchemaVersion = PROJECTION_SCHEMA_VERSION
    run_id: Identifier = Field(default_factory=lambda: _new_id("run"))
    seed_id: Identifier
    goal: str = Field(default="")
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = Field(default=None)
    stage_ids: IdentifierTuple = Field(default_factory=tuple)
    verdict_id: Identifier | None = Field(default=None)
    metadata: FrozenMetadata = Field(default_factory=_empty_frozen_metadata)

    @field_validator("verdict_id")
    @classmethod
    def _verdict_id_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            msg = "RunRecord.verdict_id must be None or a non-blank identifier"
            raise ValueError(msg)
        return stripped

    @model_validator(mode="after")
    def _validate_timestamps(self) -> RunRecord:
        if self.ended_at is not None and self.ended_at < self.started_at:
            msg = "RunRecord.ended_at cannot precede started_at"
            raise ValueError(msg)
        return self


__all__ = [
    "PROJECTION_SCHEMA_VERSION",
    "ArtifactRecord",
    "FrozenMetadata",
    "Identifier",
    "ProjectionSchemaVersion",
    "IdentifierTuple",
    "RunRecord",
    "StageKind",
    "StageRecord",
    "StepKind",
    "StepRecord",
    "VerdictOutcome",
    "VerdictRecord",
]
