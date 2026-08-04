"""Seed reconstruction for one evolve_step resume decision.

Extracted from ``EvolutionaryLoop.evolve_step`` so the resume ladder is
testable on its own and the loop module stays inside its size budget. The
precedence is unchanged: an explicit caller seed wins; an interrupted
generation resumes from its own durable ``seed_json``; otherwise the last
completed generation's seed is used, resetting phase-level resume so stale
phases are never skipped with a different generation's seed.
"""

from __future__ import annotations

import json

from ouroboros.core.errors import OuroborosError
from ouroboros.core.lineage import GenerationPhase, OntologyLineage
from ouroboros.core.seed import Seed
from ouroboros.core.types import Result


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

    if last_phase == GenerationPhase.INTERRUPTED:
        # Try to use the interrupted generation's seed (preserves evolved state)
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
