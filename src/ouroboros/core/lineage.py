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
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

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
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class MutationAction(StrEnum):
    """Type of ontology mutation."""

    ADD = "add"
    MODIFY = "modify"
    REMOVE = "remove"


class ACResult(BaseModel, frozen=True):
    """Result of evaluating a single acceptance criterion."""

    ac_index: int
    ac_content: str
    passed: bool
    score: float | None = None
    evidence: str = ""
    verification_method: str = "unknown"
    ac_verdict_state: str = "evaluated"
    final_verdict: str = ""
    rendered_verdict: str = ""

    @model_validator(mode="before")
    @classmethod
    def _backfill_verdict(cls, data: dict) -> dict:  # type: ignore[override]
        """Derive verdict fields from passed for legacy callers."""
        if isinstance(data, dict):
            passed = data.get("passed", False)
            if not data.get("final_verdict"):
                data["final_verdict"] = "pass" if passed else "fail"
            # Normalize passed to match final_verdict (final_verdict wins)
            fv = data["final_verdict"].lower()
            data["passed"] = fv == "pass"
            if not data.get("rendered_verdict"):
                data["rendered_verdict"] = data["final_verdict"].upper()
        return data

    @property
    def verdict_label(self) -> str:
        """Canonical display label derived from final_verdict."""
        return self.final_verdict.upper()

    @property
    def provisional_verdict(self) -> str:
        """What the evaluator originally said before any override."""
        # If overridden, the original was the opposite
        if self.ac_verdict_state == "overridden":
            return "fail" if self.final_verdict == "pass" else "pass"
        return self.final_verdict

    @property
    def override_source(self) -> str | None:
        """Which verifier overrode the provisional verdict."""
        if self.ac_verdict_state == "overridden":
            return self.verification_method
        return None

    @property
    def override_reason(self) -> str | None:
        """Why the override happened."""
        if self.ac_verdict_state == "overridden":
            return self.evidence
        return None


class FeedbackMetadata(BaseModel, frozen=True):
    """Structured evaluation feedback that downstream loops can react to."""

    code: str = Field(description="Stable machine-readable feedback identifier.")
    severity: str = Field(
        default="info",
        description="Feedback severity (for example: info, warning, error).",
    )
    message: str = Field(description="Human-readable explanation of the feedback signal.")
    source: str = Field(
        default="evaluation",
        description="Subsystem that emitted this feedback signal.",
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON-safe structured payload with feedback-specific details.",
    )


_SEED_QUALITY_CANARY_CODES = frozenset({"decomposition_depth_warning"})


class EvaluationSummary(BaseModel, frozen=True):
    """Typed summary of evaluation results for a generation.

    Extracted from the full EvaluationResult to store in GenerationRecord
    without carrying the entire evaluation payload.
    """

    final_approved: bool
    highest_stage_passed: int = Field(ge=1, le=3)
    score: float | None = None
    drift_score: float | None = Field(
        default=None,
        description="Measures goal/constraint/ontology divergence from the seed intent (0.0-1.0). Distinct from reward_hacking_risk.",
    )
    reward_hacking_risk: float | None = Field(
        default=None,
        description="Suspicion that the artifact is optimized for evaluator, rubric, or test approval rather than solving the real task (0.0-1.0). Distinct from drift_score.",
    )
    failure_reason: str | None = None
    ac_results: tuple[ACResult, ...] = ()
    feedback_metadata: tuple[FeedbackMetadata, ...] = Field(
        default_factory=tuple,
        description=(
            "Structured evaluation feedback signals for downstream loops, "
            "including canaries such as decomposition-depth pressure."
        ),
    )
    execution_completion_status: str = Field(
        description="Whether execution finished successfully. Cross-reference with approval_status for full run verdict.",
    )
    approval_status: str = Field(
        description="Whether the evaluation approved the artifact. Cross-reference with execution_completion_status for full run verdict.",
    )

    @model_validator(mode="before")
    @classmethod
    def _backfill_status_fields(cls, data: dict) -> dict:  # type: ignore[override]
        """Derive status fields from final_approved for legacy callers."""
        if isinstance(data, dict):
            if "execution_completion_status" not in data:
                data["execution_completion_status"] = "completed"
            if "approval_status" not in data:
                approved = data.get("final_approved", False)
                data["approval_status"] = "approved" if approved else "rejected"
        return data

    @model_validator(mode="after")
    def _reconcile_approval_with_ac_results(self) -> EvaluationSummary:
        """Eagerly reconcile approval fields with AC results at construction time."""
        if self.ac_results:
            ac_passed = all(ac.passed for ac in self.ac_results)
            expected_status = "approved" if ac_passed else "rejected"
            if self.approval_status != expected_status:
                object.__setattr__(self, "approval_status", expected_status)
            if self.final_approved != ac_passed:
                object.__setattr__(self, "final_approved", ac_passed)
        return self

    @property
    def run_verdict_passed(self) -> bool:
        """Aggregate verdict incorporating execution status, approval, and AC results.

        Priority: execution_completion_status > ac_results > approval_status > final_approved.
        approval_status is already reconciled with AC results at construction time.
        """
        if self.execution_completion_status != "completed":
            return False
        if self.ac_results:
            return all(ac.passed for ac in self.ac_results)
        if self.approval_status == "rejected":
            return False
        return self.final_approved

    @property
    def run_verdict(self) -> str:
        """Human-readable aggregate verdict."""
        return "PASS" if self.run_verdict_passed else "FAIL"

    @property
    def seed_quality_canary_feedback(self) -> tuple[FeedbackMetadata, ...]:
        """Feedback signals that indicate seed-quality pressure in execution."""
        return tuple(
            feedback
            for feedback in self.feedback_metadata
            if feedback.code in _SEED_QUALITY_CANARY_CODES
        )


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
    execution_output: str | None = None
    seed_quality_canary_feedback: tuple[FeedbackMetadata, ...] = Field(
        default_factory=tuple,
        description=(
            "Subset of evaluation feedback that acts as a seed-quality canary, "
            "such as recursive decomposition depth pressure."
        ),
    )
    failure_error: str | None = None
    last_completed_phase: str | None = None
    partial_state: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _backfill_seed_quality_canary_feedback(cls, data: Any) -> Any:
        """Derive canary feedback from evaluation metadata when callers omit it."""
        if not isinstance(data, dict):
            return data
        if data.get("seed_quality_canary_feedback"):
            return data

        eval_summary = data.get("evaluation_summary")
        if isinstance(eval_summary, EvaluationSummary):
            data["seed_quality_canary_feedback"] = eval_summary.seed_quality_canary_feedback
        elif isinstance(eval_summary, dict):
            data["seed_quality_canary_feedback"] = EvaluationSummary.model_validate(
                eval_summary
            ).seed_quality_canary_feedback
        return data


class RewindRecord(BaseModel, frozen=True):
    """Immutable record of a single rewind operation.

    Captures the discarded generations so TUI can display them
    as a collapsed subtree under the rewind point.
    """

    from_generation: int
    to_generation: int
    rewound_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    discarded_generations: tuple[GenerationRecord, ...] = ()


class OntologyLineage(BaseModel, frozen=True):
    """Tracks O₁ → O₂ → O₃ evolution across generations.

    This is a read model projected from events. Never persisted directly;
    always reconstructed via LineageProjector from event replay.

    All mutation methods return new instances (frozen immutability).
    """

    lineage_id: str = Field(default_factory=lambda: f"lin_{uuid4().hex[:12]}")
    goal: str
    generations: tuple[GenerationRecord, ...] = Field(default_factory=tuple)
    rewind_history: tuple[RewindRecord, ...] = Field(default_factory=tuple)
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
        """Return new lineage with appended or replaced generation.

        If a generation with the same number already exists (e.g., from a
        failed/interrupted attempt), it is replaced instead of duplicated.
        """
        existing = tuple(
            g for g in self.generations if g.generation_number != record.generation_number
        )
        return self.model_copy(update={"generations": existing + (record,)})

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
