"""Ontology lineage tracking for evolutionary loops.

Tracks the evolution of ontology across generations: O₁ → O₂ → O₃ → ... → Oₙ.
Each generation's Seed remains frozen (immutable), but the lineage records how
ontology evolves from one generation to the next.

All models are frozen (immutable). State transitions produce new instances via
with_*() methods. OntologyLineage is a read model projected from events -- never
persisted directly, always reconstructed via LineageProjector.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field

from ouroboros.core.seed import OntologyField, OntologySchema


class LineageStatus(StrEnum):
    """Status of the evolutionary lineage."""

    ACTIVE = "active"
    CONVERGED = "converged"
    EXHAUSTED = "exhausted"
    ABORTED = "aborted"


class GenerationPhase(StrEnum):
    """Lifecycle phase of a single generation (for error recovery)."""

    WONDERING = "wondering"
    REFLECTING = "reflecting"
    SEEDING = "seeding"
    EXECUTING = "executing"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    FAILED = "failed"


class MutationAction(StrEnum):
    """Type of ontology mutation."""

    ADD = "add"
    MODIFY = "modify"
    REMOVE = "remove"


class EvaluationSummary(BaseModel, frozen=True):
    """Typed summary of evaluation results for a generation.

    Extracted from the full EvaluationResult to store in GenerationRecord
    without carrying the entire evaluation payload.
    """

    final_approved: bool
    highest_stage_passed: int = Field(ge=1, le=3)
    score: float | None = None
    drift_score: float | None = None
    failure_reason: str | None = None


class FieldModification(BaseModel, frozen=True):
    """Records what changed in a single ontology field between generations."""

    field_name: str
    old_type: str
    new_type: str
    old_description: str
    new_description: str


class OntologyDelta(BaseModel, frozen=True):
    """Diff between two OntologySchemas.

    The similarity score uses weighted comparison:
    - name match: 0.5 (field exists in both)
    - type match: 0.3 (same name AND same type)
    - exact match: 0.2 (same name, type, AND description)
    """

    added_fields: tuple[OntologyField, ...] = Field(default_factory=tuple)
    removed_fields: tuple[str, ...] = Field(default_factory=tuple)
    modified_fields: tuple[FieldModification, ...] = Field(default_factory=tuple)
    similarity: float = Field(ge=0.0, le=1.0)

    @staticmethod
    def compute(old: OntologySchema, new: OntologySchema) -> OntologyDelta:
        """Compute delta between two ontology schemas with weighted similarity.

        Weights:
            - name presence: 0.5
            - type match: 0.3
            - exact match (type + description): 0.2
        """
        old_by_name = {f.name: f for f in old.fields}
        new_by_name = {f.name: f for f in new.fields}

        old_names = set(old_by_name.keys())
        new_names = set(new_by_name.keys())

        added_names = new_names - old_names
        removed_names = old_names - new_names
        common_names = old_names & new_names

        added = tuple(new_by_name[n] for n in sorted(added_names))
        removed = tuple(sorted(removed_names))

        modified: list[FieldModification] = []
        for name in sorted(common_names):
            old_f = old_by_name[name]
            new_f = new_by_name[name]
            if old_f.field_type != new_f.field_type or old_f.description != new_f.description:
                modified.append(
                    FieldModification(
                        field_name=name,
                        old_type=old_f.field_type,
                        new_type=new_f.field_type,
                        old_description=old_f.description,
                        new_description=new_f.description,
                    )
                )

        # Weighted similarity calculation
        all_names = old_names | new_names
        if not all_names:
            similarity = 1.0
        else:
            name_score = len(common_names) / len(all_names) if all_names else 1.0

            type_matches = sum(
                1 for n in common_names if old_by_name[n].field_type == new_by_name[n].field_type
            )
            type_score = type_matches / len(all_names) if all_names else 1.0

            exact_matches = sum(
                1
                for n in common_names
                if old_by_name[n].field_type == new_by_name[n].field_type
                and old_by_name[n].description == new_by_name[n].description
            )
            exact_score = exact_matches / len(all_names) if all_names else 1.0

            similarity = 0.5 * name_score + 0.3 * type_score + 0.2 * exact_score

        return OntologyDelta(
            added_fields=added,
            removed_fields=removed,
            modified_fields=tuple(modified),
            similarity=similarity,
        )


class GenerationRecord(BaseModel, frozen=True):
    """Immutable snapshot of a single generation in the lineage.

    Each record captures the complete state of a generation: what Seed was used,
    what ontology it had, how it was evaluated, and what Wonder discovered.
    """

    generation_number: int = Field(ge=1)
    seed_id: str
    parent_seed_id: str | None = None
    ontology_snapshot: OntologySchema
    evaluation_summary: EvaluationSummary | None = None
    wonder_questions: tuple[str, ...] = Field(default_factory=tuple)
    phase: GenerationPhase = GenerationPhase.COMPLETED
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    seed_json: str | None = None


class OntologyLineage(BaseModel, frozen=True):
    """Tracks O₁ → O₂ → O₃ evolution across generations.

    This is a read model projected from events. Never persisted directly;
    always reconstructed via LineageProjector from event replay.

    All mutation methods return new instances (frozen immutability).
    """

    lineage_id: str = Field(default_factory=lambda: f"lin_{uuid4().hex[:12]}")
    goal: str
    generations: tuple[GenerationRecord, ...] = Field(default_factory=tuple)
    status: LineageStatus = LineageStatus.ACTIVE
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def current_generation(self) -> int:
        """Return the latest generation number, or 0 if no generations yet."""
        return self.generations[-1].generation_number if self.generations else 0

    @property
    def current_ontology(self) -> OntologySchema | None:
        """Return the latest ontology snapshot, or None if no generations."""
        return self.generations[-1].ontology_snapshot if self.generations else None

    def with_generation(self, record: GenerationRecord) -> OntologyLineage:
        """Return new lineage with appended generation."""
        return self.model_copy(update={"generations": self.generations + (record,)})

    def with_status(self, status: LineageStatus) -> OntologyLineage:
        """Return new lineage with updated status."""
        return self.model_copy(update={"status": status})

    def rewind_to(self, generation_number: int) -> OntologyLineage:
        """Return lineage truncated to the given generation.

        The truncated lineage has ACTIVE status, ready for continued evolution.
        This enables snapshot/rewind: rewind to Oₙ and branch from there.

        Args:
            generation_number: The generation to rewind to (inclusive).

        Returns:
            New OntologyLineage truncated to the specified generation.

        Raises:
            ValueError: If generation_number is out of range.
        """
        if not self.generations:
            raise ValueError("Cannot rewind empty lineage")

        max_gen = self.generations[-1].generation_number
        if generation_number < 1 or generation_number > max_gen:
            raise ValueError(f"Generation {generation_number} out of range [1, {max_gen}]")

        truncated = tuple(g for g in self.generations if g.generation_number <= generation_number)
        return self.model_copy(
            update={
                "generations": truncated,
                "status": LineageStatus.ACTIVE,
            }
        )
