"""Run / Stage / Step / Artifact / Verdict projection records.

These records are the public, schema-versioned read-model that the Ouroboros
harness presents over the underlying ``EventStore``. They give plugins,
maintainers, evaluation pipelines, and CLI consumers a stable vocabulary
for describing the work a run performed without each consumer reinventing
its own status format.

Design constraints (per the acceptance criteria attached to issue #946):

* Records are immutable Pydantic models (``frozen=True``) so they can be
  treated as values in projections, comparisons, and persistence layers.
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

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

PROJECTION_SCHEMA_VERSION = 1
"""Initial schema version for the projection vocabulary."""


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
        metadata: Free-form metadata bag; consumers must treat it as
            opaque and additive.
    """

    schema_version: int = Field(default=PROJECTION_SCHEMA_VERSION, ge=1)
    artifact_id: str = Field(default_factory=lambda: _new_id("artifact"), min_length=1)
    step_id: str = Field(..., min_length=1)
    kind: str = Field(..., min_length=1)
    path: str | None = Field(default=None)
    media_type: str | None = Field(default=None)
    size_bytes: int | None = Field(default=None, ge=0)
    digest: str | None = Field(default=None)
    summary: str = Field(default="")
    metadata: dict[str, Any] = Field(default_factory=dict)

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
            this projection. Empty only when ``legacy_inferred`` is True.
        legacy_inferred: Marks the record as projected from legacy data
            that did not preserve enough metadata to link source events.
            Consumers may filter on this flag when computing audit views.
        artifact_ids: Identifiers of artifacts produced by the step. Each
            value must match a :attr:`ArtifactRecord.artifact_id`.
        metadata: Free-form metadata bag for additive consumer data.
    """

    schema_version: int = Field(default=PROJECTION_SCHEMA_VERSION, ge=1)
    step_id: str = Field(default_factory=lambda: _new_id("step"), min_length=1)
    run_id: str = Field(..., min_length=1)
    stage_id: str = Field(..., min_length=1)
    kind: StepKind
    name: str = Field(default="", description="Short human-readable label")
    ac_id: str | None = Field(default=None)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = Field(default=None)
    ok: bool | None = Field(default=None)
    source_event_ids: tuple[str, ...] = Field(default_factory=tuple)
    legacy_inferred: bool = Field(default=False)
    artifact_ids: tuple[str, ...] = Field(default_factory=tuple)
    metadata: dict[str, Any] = Field(default_factory=dict)

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
            order.
        metadata: Free-form metadata bag for additive consumer data.
    """

    schema_version: int = Field(default=PROJECTION_SCHEMA_VERSION, ge=1)
    stage_id: str = Field(default_factory=lambda: _new_id("stage"), min_length=1)
    run_id: str = Field(..., min_length=1)
    kind: StageKind
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = Field(default=None)
    step_ids: tuple[str, ...] = Field(default_factory=tuple)
    metadata: dict[str, Any] = Field(default_factory=dict)

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
            must be ``None`` otherwise.
        outcome: Terminal status from :class:`VerdictOutcome`.
        rationale: Short structured rationale string. Consumers should
            treat it as a stable summary, not as the full evidence body.
        evidence_event_ids: Identifiers of source events backing this
            verdict.
        evidence_artifact_ids: Identifiers of artifacts backing this
            verdict.
        recorded_at: When the verdict was recorded.
        metadata: Free-form metadata bag for additive consumer data.
    """

    schema_version: int = Field(default=PROJECTION_SCHEMA_VERSION, ge=1)
    verdict_id: str = Field(default_factory=lambda: _new_id("verdict"), min_length=1)
    run_id: str = Field(..., min_length=1)
    scope: Literal["run", "ac"]
    ac_id: str | None = Field(default=None)
    outcome: VerdictOutcome
    rationale: str = Field(default="")
    evidence_event_ids: tuple[str, ...] = Field(default_factory=tuple)
    evidence_artifact_ids: tuple[str, ...] = Field(default_factory=tuple)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)

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
        stage_ids: Stage identifiers in execution order.
        verdict_id: Optional identifier of the run-level
            :class:`VerdictRecord`.
        metadata: Free-form metadata bag for additive consumer data.
    """

    schema_version: int = Field(default=PROJECTION_SCHEMA_VERSION, ge=1)
    run_id: str = Field(default_factory=lambda: _new_id("run"), min_length=1)
    seed_id: str = Field(..., min_length=1)
    goal: str = Field(default="")
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = Field(default=None)
    stage_ids: tuple[str, ...] = Field(default_factory=tuple)
    verdict_id: str | None = Field(default=None)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_timestamps(self) -> RunRecord:
        if self.ended_at is not None and self.ended_at < self.started_at:
            msg = "RunRecord.ended_at cannot precede started_at"
            raise ValueError(msg)
        return self


__all__ = [
    "PROJECTION_SCHEMA_VERSION",
    "ArtifactRecord",
    "RunRecord",
    "StageKind",
    "StageRecord",
    "StepKind",
    "StepRecord",
    "VerdictOutcome",
    "VerdictRecord",
]
