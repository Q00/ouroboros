"""Immutable Seed schema for workflow execution.

The Seed is the "constitution" of a workflow - an immutable specification
generated from the Big Bang interview phase when ambiguity score <= 0.2.

Key properties:
- Seed.direction (goal, constraints, acceptance_criteria) is IMMUTABLE
- Effective ontology can evolve with consensus during iterations
- Contains all information needed to execute and evaluate the workflow

This module defines:
- Seed: The immutable Pydantic model with frozen=True
- SeedMetadata: Version and creation metadata
- Supporting types for seed components
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class ExitCondition(BaseModel, frozen=True):
    """Defines when the workflow should terminate.

    Attributes:
        name: Short identifier for the condition.
        description: Detailed explanation of the exit condition.
        evaluation_criteria: How to determine if condition is met.
    """

    model_config = {"populate_by_name": True}

    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    evaluation_criteria: str = Field(..., min_length=1, alias="criteria")


class EvaluationPrinciple(BaseModel, frozen=True):
    """A principle for evaluating workflow outputs.

    Attributes:
        name: Short identifier for the principle.
        description: What this principle evaluates.
        weight: Relative importance (0.0 to 1.0).
    """

    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    weight: float = Field(default=1.0, ge=0.0, le=1.0)


class OntologyField(BaseModel, frozen=True):
    """A field in an ontology schema.

    Attributes:
        name: Field identifier.
        field_type: Field type (string, number, boolean, array, object).
        description: Purpose of this field.
        required: Whether this field is required.
    """

    model_config = {"populate_by_name": True}

    name: str = Field(..., min_length=1)
    field_type: str = Field(..., min_length=1, alias="type")
    description: str = Field(..., min_length=1)
    required: bool = Field(default=True)


class OntologySchema(BaseModel, frozen=True):
    """Schema describing workflow domain structure.

    The ontology schema names the domain fields and boundaries the workflow
    should preserve throughout iterations. It is not, by itself, a mandatory
    output shape.

    Attributes:
        name: Name of the ontology.
        description: Purpose, scope, and perspective of this ontology.
        fields: Fields in the ontology.
    """

    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    fields: tuple[OntologyField, ...] = Field(default_factory=tuple)


class ContextReference(BaseModel, frozen=True):
    """Reference to an existing codebase directory.

    Attributes:
        path: Absolute path to the codebase directory.
        role: 'primary' (modify this) or 'reference' (read-only).
        summary: Auto-generated codebase summary from exploration.
    """

    path: str = Field(..., min_length=1, description="Absolute path to codebase")
    role: str = Field(..., description="'primary' (modify this) or 'reference' (read-only)")
    summary: str = Field(default="", description="Auto-generated codebase summary")


class BrownfieldContext(BaseModel, frozen=True):
    """Context for brownfield projects.

    For greenfield projects, this remains at defaults (project_type="greenfield",
    empty references). For brownfield, it carries discovered codebase context.

    Attributes:
        project_type: 'greenfield' or 'brownfield'.
        context_references: Referenced codebase directories with roles.
        existing_patterns: Key patterns discovered in existing code.
        existing_dependencies: Key dependencies discovered in existing code.
    """

    project_type: str = Field(
        default="greenfield",
        description="'greenfield' or 'brownfield'",
    )
    context_references: tuple[ContextReference, ...] = Field(
        default_factory=tuple,
        description="Referenced codebase directories",
    )
    existing_patterns: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Key patterns discovered in existing code",
    )
    existing_dependencies: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Key dependencies discovered in existing code",
    )


class SeedMetadata(BaseModel, frozen=True):
    """Metadata about the Seed generation.

    Attributes:
        seed_id: Unique identifier for this seed.
        version: Schema version for forward compatibility.
        created_at: When this seed was generated.
        ambiguity_score: The ambiguity score at generation time.
        interview_id: Reference to the source interview.
        generation_mode: Provenance label for how the Seed was synthesized.
            ``"normal"`` is the legacy ledger-complete path; degraded recovery
            paths (e.g. ``"partial_seed_from_evidence"``) MUST set this so
            grading/run gates can distinguish a fully-resolved Seed from one
            built under deadline pressure (#1257).
        degraded: ``True`` when the Seed was synthesized under a recovery path
            rather than a clean low-ambiguity closure, so grade/run gates route
            it to a typed partial-product terminal instead of auto-RUN. Always
            pairs with a non-default ``generation_mode``. Two cases:
            (a) incomplete ledger (``partial_seed_from_evidence``) — carries at
            least one entry in ``unresolved_slots``; (b) complete ledger closed
            via the interview-phase deadline (#1302) — ``unresolved_slots`` is
            empty because the ledger is fully resolved, but no backend-confirmed
            low ambiguity exists.
        unresolved_slots: Ledger sections that were still
            MISSING/WEAK/CONFLICTING/BLOCKED at synthesis time. Empty for normal
            seeds and for complete-ledger deadline-recovery seeds. Surfaced
            verbatim so downstream gates can transform them into ``next_step``
            hints instead of terminal blockers.
        recovery_reason: Free-form description of why the degraded path was
            taken (e.g. ``"interview_phase_deadline"``). ``None`` for normal
            seeds.
    """

    seed_id: str = Field(default_factory=lambda: f"seed_{uuid4().hex[:12]}")
    version: str = Field(default="1.0.0")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ambiguity_score: float = Field(default=0.15, ge=0.0, le=1.0)
    interview_id: str | None = Field(default=None)
    parent_seed_id: str | None = Field(default=None)
    generation_mode: str = Field(default="normal", min_length=1)
    degraded: bool = Field(default=False)
    unresolved_slots: tuple[str, ...] = Field(default_factory=tuple)
    recovery_reason: str | None = Field(default=None)


class Seed(BaseModel, frozen=True):
    """Immutable specification for workflow execution.

    The Seed is the "constitution" of the workflow - once generated, it cannot
    be modified. This ensures consistency throughout the workflow lifecycle.

    Direction (goal, constraints, acceptance_criteria) is IMMUTABLE:
    - These define WHAT the workflow should achieve
    - Cannot be changed after generation
    - Serves as the ground truth for evaluation

    Attributes:
        goal: The primary objective of the workflow.
        constraints: Hard constraints that must be satisfied.
        acceptance_criteria: Specific criteria for success.
        ontology_schema: Conceptual lens for workflow coherence.
        evaluation_principles: Principles for evaluating outputs.
        exit_conditions: Conditions for terminating the workflow.
        metadata: Generation metadata (version, timestamp, etc.).

    Example:
        seed = Seed(
            goal="Build a CLI task management tool",
            constraints=("Python 3.14+", "No external database"),
            acceptance_criteria=("Tasks can be created", "Tasks can be listed"),
            ontology_schema=OntologySchema(
                name="TaskManager",
                description="Task management ontology",
                fields=(
                    OntologyField(
                        name="tasks",
                        field_type="array",
                        description="List of tasks",
                    ),
                ),
            ),
            evaluation_principles=(
                EvaluationPrinciple(
                    name="completeness",
                    description="All requirements are met",
                ),
            ),
            exit_conditions=(
                ExitCondition(
                    name="all_criteria_met",
                    description="All acceptance criteria satisfied",
                    evaluation_criteria="100% criteria pass",
                ),
            ),
            metadata=SeedMetadata(ambiguity_score=0.15),
        )

        # Attempting to modify raises an error:
        seed.goal = "New goal"  # Raises ValidationError (frozen)
    """

    # Direction - IMMUTABLE
    goal: str = Field(..., min_length=1, description="Primary objective of the workflow")
    task_type: str = Field(
        default="code",
        description="Type of task execution: 'code', 'research', or 'analysis'",
    )
    brownfield_context: BrownfieldContext = Field(
        default_factory=BrownfieldContext,
        description="Brownfield project context (empty for greenfield)",
    )
    constraints: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Hard constraints that must be satisfied",
    )
    acceptance_criteria: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Specific criteria for success evaluation",
    )

    # Conceptual lens
    ontology_schema: OntologySchema = Field(
        ...,
        description="Ontology defining the workflow's conceptual lens",
    )

    # Evaluation
    evaluation_principles: tuple[EvaluationPrinciple, ...] = Field(
        default_factory=tuple,
        description="Principles for evaluating workflow outputs",
    )

    # Termination
    exit_conditions: tuple[ExitCondition, ...] = Field(
        default_factory=tuple,
        description="Conditions for terminating the workflow",
    )

    # Metadata
    metadata: SeedMetadata = Field(
        ...,
        description="Generation metadata (version, timestamp, etc.)",
    )

    @field_validator("evaluation_principles", mode="before")
    @classmethod
    def _coerce_string_evaluation_principles(cls, value: Any) -> Any:
        """Accept prose lists for hand-written seeds.

        Human-authored seed drafts often express evaluation principles as a
        simple YAML string list. Lift those entries into the documented object
        shape so manual ``ooo run`` users get a usable seed instead of an
        opaque Pydantic ``model_type`` error.
        """
        if isinstance(value, list | tuple):
            return tuple(
                {"name": f"principle_{index}", "description": item}
                if isinstance(item, str)
                else item
                for index, item in enumerate(value, start=1)
            )
        return value

    @field_validator("exit_conditions", mode="before")
    @classmethod
    def _coerce_string_exit_conditions(cls, value: Any) -> Any:
        """Accept prose lists for hand-written seed exit conditions."""
        if isinstance(value, list | tuple):
            return tuple(
                {
                    "name": f"condition_{index}",
                    "description": item,
                    "criteria": item,
                }
                if isinstance(item, str)
                else item
                for index, item in enumerate(value, start=1)
            )
        return value

    def to_dict(self) -> dict[str, Any]:
        """Convert seed to dictionary for serialization.

        Returns:
            Dictionary representation of the seed.
        """
        return self.model_dump(mode="json", by_alias=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Seed:
        """Create seed from dictionary.

        Args:
            data: Dictionary representation of the seed.

        Returns:
            Seed instance.
        """
        return cls.model_validate(data)
