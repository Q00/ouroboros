"""Seed and event construction helpers for one evolve_step attempt.

Extracted from ``EvolutionaryLoop.evolve_step`` so the resume ladder is
testable on its own and the loop module stays inside its size budget. The
precedence: an explicit caller seed wins; an interrupted generation
resumes from its own durable ``seed_json``; any other unfinished
generation — including one abandoned by an expired lease mid-phase —
restarts from the last completed generation's seed, resetting phase-level
resume so stale phases are never skipped with a different generation's
seed; and a lineage with nothing completed asks for an explicit seed
rather than guessing.
"""

from __future__ import annotations

import json
from typing import Any

from ouroboros.core.errors import OuroborosError
from ouroboros.core.lineage import (
    GenerationPhase,
    GenerationRecord,
    LineageStatus,
    OntologyLineage,
)
from ouroboros.core.seed import Seed
from ouroboros.core.types import Result
from ouroboros.events.lineage import lineage_generation_completed
from ouroboros.evolution import loop_support
from ouroboros.evolution.projector import LineageProjector


def prepare_existing_step(
    events: list[Any],
    *,
    initial_seed: Seed | None,
    execute: bool,
) -> Result[
    tuple[OntologyLineage, int, GenerationPhase, str | None, Seed, GenerationRecord | None],
    OuroborosError,
]:
    """Project current main's recovery state without weakening lease replay."""
    projected = project_step_state(events)
    if projected.is_err:
        return Result.err(projected.error)
    lineage, _projected_generation, last_phase, interrupted_at_phase = projected.value
    last_generation, _, _ = LineageProjector().find_resume_point(events)
    try:
        generation_number, recovered_seed = loop_support.recovery_plan(
            lineage, last_generation, last_phase
        )
    except ValueError as exc:
        return Result.err(OuroborosError(str(exc)))
    hard_crash_record, recovery_error = loop_support.hard_crash_recovery(
        lineage, generation_number, last_phase, execute=execute
    )
    if recovery_error is not None:
        return Result.err(OuroborosError(recovery_error))
    if recovered_seed is not None:
        current_seed = recovered_seed
    elif initial_seed is not None and not lineage.verification_handoff_pending:
        current_seed = initial_seed
    else:
        reconstructed = reconstruct_step_seed(
            initial_seed=None,
            last_phase=last_phase,
            lineage=lineage,
            interrupted_at_phase=interrupted_at_phase,
        )
        if reconstructed.is_err:
            return Result.err(reconstructed.error)
        current_seed, interrupted_at_phase = reconstructed.value
    return Result.ok(
        (
            lineage,
            generation_number,
            last_phase,
            interrupted_at_phase,
            current_seed,
            hard_crash_record,
        )
    )


def reconstruct_step_seed(
    *,
    initial_seed: Seed | None,
    last_phase: GenerationPhase,
    lineage: OntologyLineage,
    interrupted_at_phase: str | None,
) -> Result[tuple[Seed, str | None], OuroborosError]:
    """Choose the seed for the next generation of an existing lineage.

    Returns the seed together with the (possibly reset) phase-level resume
    marker, or the error the caller must surface unchanged.
    """
    if initial_seed is not None:
        # Caller provided seed explicitly (e.g., after rewind)
        return Result.ok((initial_seed, interrupted_at_phase))

    if last_phase not in (GenerationPhase.COMPLETED, GenerationPhase.FAILED):
        # INTERRUPTED, or a nonterminal phase abandoned by an expired lease.
        # Try to use the interrupted generation's seed (preserves evolved
        # state); abandoned phases journal no seed of their own and fall to
        # the last completed generation below, or to the explicit-seed error.
        interrupted_gen = next(
            (g for g in reversed(lineage.generations) if g.phase == GenerationPhase.INTERRUPTED),
            None,
        )
        if interrupted_gen and interrupted_gen.seed_json:
            try:
                seed = Seed.from_dict(json.loads(interrupted_gen.seed_json))
            except Exception as e:
                # A present interrupted Seed is the durable state for this
                # generation. If its structured contract is no longer valid,
                # rolling back to the prior completed Seed would silently
                # change acceptance semantics and may redispatch work under
                # stale direction.
                return Result.err(
                    OuroborosError(f"Failed to reconstruct interrupted seed from seed_json: {e}")
                )
            return Result.ok((seed, interrupted_at_phase))

        # Fallback: use last completed generation's seed.
        # IMPORTANT: also reset interrupted_at_phase so we don't skip phases
        # with a stale seed from a different generation.
        last_completed = next(
            (g for g in reversed(lineage.generations) if g.phase == GenerationPhase.COMPLETED),
            None,
        )
        if last_completed and last_completed.seed_json:
            try:
                seed = Seed.from_dict(json.loads(last_completed.seed_json))
            except Exception as e:
                return Result.err(
                    OuroborosError(f"Failed to reconstruct fallback seed from seed_json: {e}")
                )
            return Result.ok((seed, None))
        return Result.err(
            OuroborosError(
                "Lineage was interrupted before any generation completed. "
                "Re-provide initial_seed to resume."
            )
        )

    if lineage.generations:
        last_completed = next(
            (g for g in reversed(lineage.generations) if g.phase == GenerationPhase.COMPLETED),
            None,
        )
        if last_completed is None:
            has_interrupted = any(
                g.phase == GenerationPhase.INTERRUPTED for g in lineage.generations
            )
            if has_interrupted:
                return Result.err(
                    OuroborosError(
                        "Lineage was interrupted before any generation completed. "
                        "Re-provide initial_seed to resume."
                    )
                )
            return Result.err(OuroborosError("Events exist but no completed generations found"))
        if last_completed.seed_json:
            try:
                seed = Seed.from_dict(json.loads(last_completed.seed_json))
            except Exception as e:
                return Result.err(OuroborosError(f"Failed to reconstruct seed from seed_json: {e}"))
            return Result.ok((seed, interrupted_at_phase))
        return Result.err(
            OuroborosError(
                "Cannot reconstruct seed: no seed_json in last generation's events. "
                "This lineage may have been created with an older version."
            )
        )

    return Result.err(OuroborosError("Events exist but no completed generations found"))


def generation_record_for(result: Any) -> GenerationRecord:
    """Build the lineage record for one finished generation result."""
    return GenerationRecord(
        generation_number=result.generation_number,
        seed_id=result.seed.metadata.seed_id,
        parent_seed_id=result.seed.metadata.parent_seed_id,
        ontology_snapshot=result.seed.ontology_schema,
        evaluation_summary=result.evaluation_summary,
        wonder_questions=result.wonder_output.questions if result.wonder_output else (),
        phase=result.phase,
        seed_json=json.dumps(result.seed.to_dict()),
        execution_output=result.execution_output,
    )


def generation_completed_event(lineage_id: str, result: Any, record: GenerationRecord) -> Any:
    """Build the completion event carrying the durable cross-session state."""
    return lineage_generation_completed(
        lineage_id,
        result.generation_number,
        result.seed.metadata.seed_id,
        result.seed.ontology_schema.model_dump(mode="json"),
        result.evaluation_summary.model_dump(mode="json") if result.evaluation_summary else None,
        list(result.wonder_output.questions) if result.wonder_output else None,
        seed_json=json.dumps(result.seed.to_dict()),
        execution_output=result.execution_output,
        parent_seed_id=result.seed.metadata.parent_seed_id,
        seed_quality_canary_feedback=[
            feedback.model_dump(mode="json") for feedback in record.seed_quality_canary_feedback
        ]
        or None,
    )


def project_step_state(
    events: list[Any],
) -> Result[tuple[OntologyLineage, int, GenerationPhase, str | None], OuroborosError]:
    """Project the lineage and pick the generation this attempt must run.

    A terminated lineage is refused; a last phase of COMPLETED advances to
    the next generation; FAILED, INTERRUPTED, or a nonterminal phase
    abandoned by an expired lease retries its generation, never skips it.
    """
    projector = LineageProjector()
    lineage = projector.project(events)
    if lineage is None:
        return Result.err(OuroborosError("Failed to project lineage from events"))
    if lineage.status in (LineageStatus.CONVERGED, LineageStatus.EXHAUSTED):
        return Result.err(
            OuroborosError(f"Lineage already terminated with status: {lineage.status.value}")
        )
    last_gen, last_phase, interrupted_at_phase = projector.find_resume_point(events)
    if last_phase is GenerationPhase.COMPLETED:
        generation_number = last_gen + 1
    else:
        generation_number = last_gen
    return Result.ok((lineage, generation_number, last_phase, interrupted_at_phase))
