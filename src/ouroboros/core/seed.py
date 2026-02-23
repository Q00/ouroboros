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

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


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
    """A field in the ontology schema.

    Attributes:
        name: Field identifier.
        field_type: Type of the field (string, number, boolean, array, object).
        description: Purpose of this field.
        required: Whether this field is required.
    """

    model_config = {"populate_by_name": True}

    name: str = Field(..., min_length=1)
    field_type: str = Field(..., min_length=1, alias="type")
    description: str = Field(..., min_length=1)
    required: bool = Field(default=True)


class OntologySchema(BaseModel, frozen=True):
    """Schema defining the structure of workflow outputs.

    The ontology schema defines what data structure the workflow should
    produce and maintain throughout iterations.

    Attributes:
        name: Name of the ontology.
        description: Purpose and scope of this ontology.
        fields: List of fields in the ontology.
    """

    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    fields: tuple[OntologyField, ...] = Field(default_factory=tuple)


class SeedMetadata(BaseModel, frozen=True):
    """Metadata about the Seed generation.

    Attributes:
        seed_id: Unique identifier for this seed.
        version: Schema version for forward compatibility.
        created_at: When this seed was generated.
        ambiguity_score: The ambiguity score at generation time.
        interview_id: Reference to the source interview.
    """

    seed_id: str = Field(default_factory=lambda: f"seed_{uuid4().hex[:12]}")
    version: str = Field(default="1.0.0")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ambiguity_score: float = Field(..., ge=0.0, le=1.0)
    interview_id: str | None = Field(default=None)
    parent_seed_id: str | None = Field(default=None)


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
        ontology_schema: Schema for workflow output structure.
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
    constraints: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Hard constraints that must be satisfied",
    )
    acceptance_criteria: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Specific criteria for success evaluation",
    )

    # Structure
    ontology_schema: OntologySchema = Field(
        ...,
        description="Schema defining the structure of workflow outputs",
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
