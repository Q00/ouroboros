"""EvolutionaryLoop orchestrator - manages generation-level execution.

Transforms the linear pipeline into a closed evolutionary loop:

    Gen 1: Seed(O₁) → Execute → Validate → Evaluate
    Gen 2: Wonder(O₁, E₁) → Reflect → Seed(O₂) → Execute → Validate → Evaluate
    Gen 3: Wonder(O₂, E₂) → Reflect → Seed(O₃) → Execute → Validate → Evaluate
    ...until convergence or max_generations

The loop accepts a pre-built Seed for Gen 1 (interview is handled externally)
and autonomously evolves through Wonder → Reflect cycles for Gen 2+.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from dataclasses import dataclass
from enum import StrEnum
import json
import logging
import os
from typing import Any

from ouroboros.core.errors import OuroborosError
from ouroboros.core.lineage import (
    EvaluationSummary,
    GenerationPhase,
    GenerationRecord,
    LineageStatus,
    OntologyDelta,
    OntologyLineage,
    TerminationReason,
)
from ouroboros.core.seed import Seed
from ouroboros.core.types import Result
from ouroboros.events.lineage import (
    lineage_converged,
    lineage_created,
    lineage_exhausted,
    lineage_generation_completed,
    lineage_generation_failed,
    lineage_generation_phase_changed,
    lineage_generation_started,
    lineage_ontology_evolved,
    lineage_stagnated,
    lineage_wonder_degraded,
)
from ouroboros.evolution.convergence import ConvergenceCriteria, ConvergenceSignal
from ouroboros.evolution.guard import LineageGuard
from ouroboros.evolution.projector import LineageProjector
from ouroboros.evolution.reflect import ReflectEngine, ReflectOutput
from ouroboros.evolution.wonder import WonderEngine, WonderOutput
from ouroboros.observability.drift import DriftMeasuredEvent, DriftMeasurement
from ouroboros.persistence.event_store import EventStore

logger = logging.getLogger(__name__)


@dataclass
class EvolutionaryLoopConfig:
    """Configuration for the evolutionary loop."""

    max_generations: int = 30
    convergence_threshold: float = 0.95
    stagnation_window: int = 3
    min_generations: int = 3
    generation_timeout_seconds: int = int(
        os.environ.get("OUROBOROS_GENERATION_TIMEOUT", "0")
    )  # 0 = no timeout
    enable_oscillation_detection: bool = True
    eval_gate_enabled: bool = True
    eval_min_score: float = 0.7


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """Result of a single generation's execution."""

    generation_number: int
    seed: Seed
    execution_output: str | None = None
    evaluation_summary: EvaluationSummary | None = None
    wonder_output: WonderOutput | None = None
    reflect_output: ReflectOutput | None = None
    ontology_delta: OntologyDelta | None = None
    validation_output: str | None = None
    validation_passed: bool | None = None
    phase: GenerationPhase = GenerationPhase.COMPLETED
    success: bool = True


@dataclass(frozen=True, slots=True)
class EvolutionaryResult:
    """Final result of the evolutionary loop."""

    lineage: OntologyLineage
    total_generations: int
    converged: bool
    final_seed: Seed
    generation_results: tuple[GenerationResult, ...] = ()


class StepAction(StrEnum):
    """What the caller should do after an evolve_step() call."""

    CONTINUE = "continue"
    CONVERGED = "converged"
    STAGNATED = "stagnated"
    EXHAUSTED = "exhausted"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StepResult:
    """Result of a single evolve_step() call."""

    generation_result: GenerationResult
    convergence_signal: ConvergenceSignal
    lineage: OntologyLineage
    action: StepAction
    next_generation: int


class EvolutionaryLoop:
    """Manages the evolutionary cycle across generations.

    Gen 1 lifecycle (seed provided externally):
    1. Execute(Seed₁) → execution_output
    2. Evaluate(execution_output) → E₁
    3. Record generation → check convergence

    Gen 2+ lifecycle (autonomous):
    1. Wonder(Oₙ, Eₙ) → WonderOutput
    2. Reflect(Seedₙ, output, Eₙ, wonder) → ReflectOutput
    3. SeedGenerator(reflect_output, parent=Seedₙ) → Seed_{n+1}
    4. Execute(Seed_{n+1}) → execution_output
    5. Evaluate(execution_output) → E_{n+1}
    6. Record generation → check convergence(Oₙ, O_{n+1})
    7. If not converged → goto 1 with n+1
    """

    def __init__(
        self,
        event_store: EventStore,
        config: EvolutionaryLoopConfig | None = None,
        wonder_engine: WonderEngine | None = None,
        reflect_engine: ReflectEngine | None = None,
        seed_generator: Any | None = None,
        executor: Any | None = None,
        evaluator: Any | None = None,
        validator: Any | None = None,
    ) -> None:
        self.event_store = event_store
        self._guard = LineageGuard(event_store)
        self.config = config or EvolutionaryLoopConfig()
        self.wonder_engine = wonder_engine
        self.reflect_engine = reflect_engine
        self.seed_generator = seed_generator
        self.executor = executor
        self.evaluator = evaluator
        self.validator = validator
        self._project_dir_context: ContextVar[str | None] = ContextVar(
            "evolutionary_loop_project_dir",
            default=None,
        )
        self._convergence = ConvergenceCriteria(
            convergence_threshold=self.config.convergence_threshold,
            stagnation_window=self.config.stagnation_window,
            min_generations=self.config.min_generations,
            max_generations=self.config.max_generations,
            enable_oscillation_detection=self.config.enable_oscillation_detection,
            eval_gate_enabled=self.config.eval_gate_enabled,
            eval_min_score=self.config.eval_min_score,
        )

    def set_project_dir(self, project_dir: str | None) -> Token[str | None]:
        """Set task-local project directory context for the current generation."""
        return self._project_dir_context.set(project_dir)

    def get_project_dir(self) -> str | None:
        """Return the task-local project directory for the current execution."""
        return self._project_dir_context.get()

    def reset_project_dir(self, token: Token[str | None]) -> None:
        """Restore the previous task-local project directory context."""
        self._project_dir_context.reset(token)

    async def run(
        self,
        initial_seed: Seed,
        lineage_id: str | None = None,
    ) -> Result[EvolutionaryResult, OuroborosError]:
        """Run the full evolutionary loop starting from an initial seed.

        The initial seed is assumed to come from a completed interview (Gen 1).
        The loop autonomously evolves through Wonder → Reflect cycles for Gen 2+.

        Args:
            initial_seed: The first generation's seed (from interview).
            lineage_id: Optional lineage ID (auto-generated if not provided).

        Returns:
            Result containing EvolutionaryResult or error.
        """
        # Create lineage
        lineage = OntologyLineage(
            lineage_id=lineage_id or f"lin_{initial_seed.metadata.seed_id}",
            goal=initial_seed.goal,
        )

        # Emit lineage created event
        await self._guard.gated_append(lineage_created(lineage.lineage_id, lineage.goal))

        generation_results: list[GenerationResult] = []
        current_seed = initial_seed
        generation_number = 0

        while True:
            generation_number += 1

            logger.info(
                "evolution.generation.starting",
                extra={
                    "lineage_id": lineage.lineage_id,
                    "generation": generation_number,
                },
            )

            # Run generation with timeout
            loop_timeout = self.config.generation_timeout_seconds or None
            try:
                gen_result = await asyncio.wait_for(
                    self._run_generation(
                        lineage=lineage,
                        generation_number=generation_number,
                        current_seed=current_seed,
                    ),
                    timeout=loop_timeout,
                )
            except TimeoutError:
                logger.error(
                    "evolution.generation.timeout",
                    extra={
                        "lineage_id": lineage.lineage_id,
                        "generation": generation_number,
                        "timeout": self.config.generation_timeout_seconds,
                    },
                )
                await self._guard.gated_append(
                    lineage_generation_failed(
                        lineage.lineage_id,
                        generation_number,
                        "executing",
                        f"Generation timed out after {self.config.generation_timeout_seconds}s",
                    )
                )
                break

            if gen_result.is_err:
                logger.error(
                    "evolution.generation.failed",
                    extra={
                        "lineage_id": lineage.lineage_id,
                        "generation": generation_number,
                        "error": str(gen_result.error),
                    },
                )
                break

            result = gen_result.value
            generation_results.append(result)

            # Record generation in lineage
            record = GenerationRecord(
                generation_number=generation_number,
                seed_id=result.seed.metadata.seed_id,
                parent_seed_id=result.seed.metadata.parent_seed_id,
                ontology_snapshot=result.seed.ontology_schema,
                evaluation_summary=result.evaluation_summary,
                wonder_questions=result.wonder_output.questions if result.wonder_output else (),
                phase=result.phase,
                execution_output=result.execution_output,
            )
            lineage = lineage.with_generation(record)

            # Emit generation completed event (with seed_json for cross-session reconstruction)
            await self._guard.gated_append(
                lineage_generation_completed(
                    lineage.lineage_id,
                    generation_number,
                    result.seed.metadata.seed_id,
                    result.seed.ontology_schema.model_dump(mode="json"),
                    result.evaluation_summary.model_dump(mode="json")
                    if result.evaluation_summary
                    else None,
                    list(result.wonder_output.questions) if result.wonder_output else None,
                    seed_json=json.dumps(result.seed.to_dict()),
                    execution_output=result.execution_output,
                )
            )

            # Emit ontology evolved event if delta exists
            if result.ontology_delta and result.ontology_delta.similarity < 1.0:
                await self._guard.gated_append(
                    lineage_ontology_evolved(
                        lineage.lineage_id,
                        generation_number,
                        result.ontology_delta.model_dump(mode="json"),
                    )
                )

            # Check convergence
            signal = self._convergence.evaluate(
                lineage,
                result.wonder_output,
                latest_evaluation=result.evaluation_summary,
                validation_output=result.validation_output,
                validation_passed=result.validation_passed,
            )

            if signal.converged:
                logger.info(
                    "evolution.converged",
                    extra={
                        "lineage_id": lineage.lineage_id,
                        "generation": generation_number,
                        "reason": signal.reason,
                        "similarity": signal.ontology_similarity,
                        "termination_reason": str(signal.termination_reason),
                    },
                )

                lineage = await self._emit_termination(
                    signal, lineage, generation_number
                )
                break

            # Prepare for next generation
            current_seed = result.seed

        # Best-so-far recovery: if no generations completed, report error
        if not generation_results:
            return Result.err(OuroborosError("No generations completed before failure"))

        # Partial results available — return best-so-far (lineage stays ACTIVE for resume)
        return Result.ok(
            EvolutionaryResult(
                lineage=lineage,
                total_generations=len(generation_results),
                converged=lineage.status == LineageStatus.CONVERGED,
                final_seed=current_seed,
                generation_results=tuple(generation_results),
            )
        )

    async def evolve_step(
        self,
        lineage_id: str,
        initial_seed: Seed | None = None,
        execute: bool = True,
        parallel: bool = True,
    ) -> Result[StepResult, OuroborosError]:
        """Run exactly one generation of the evolutionary loop.

        Stateless between calls: all state is reconstructed from EventStore
        via LineageProjector. Designed for Ralph integration where each call
        may happen in a different session context.

        Args:
            lineage_id: Lineage ID to continue (or new ID for Gen 1).
            initial_seed: Seed for Gen 1 (required if no events exist).
                          Omit for Gen 2+ (reconstructed from events).

        Returns:
            Result containing StepResult with generation result, convergence
            signal, and action (CONTINUE/CONVERGED/STAGNATED/EXHAUSTED/FAILED).
        """
        projector = LineageProjector()

        # Step 1: Replay events to reconstruct state
        events = await self.event_store.replay_lineage(lineage_id)

        if not events:
            # Gen 1: no events exist yet
            if initial_seed is None:
                return Result.err(
                    OuroborosError(
                        "No events found for lineage and no initial_seed provided. "
                        "Gen 1 requires an initial_seed."
                    )
                )

            lineage = OntologyLineage(
                lineage_id=lineage_id,
                goal=initial_seed.goal,
            )
            await self._guard.gated_append(lineage_created(lineage.lineage_id, lineage.goal))
            generation_number = 1
            current_seed = initial_seed

        else:
            # Gen 2+: reconstruct from events
            lineage = projector.project(events)
            if lineage is None:
                return Result.err(OuroborosError("Failed to project lineage from events"))

            # Check if lineage is already terminated
            if lineage.status in (LineageStatus.CONVERGED, LineageStatus.EXHAUSTED):
                return Result.err(
                    OuroborosError(
                        f"Lineage already terminated with status: {lineage.status.value}"
                    )
                )

            # Determine resume point
            last_gen, last_phase = projector.find_resume_point(events)

            if last_phase == GenerationPhase.FAILED:
                # Resume the failed generation
                generation_number = last_gen
            else:
                generation_number = last_gen + 1

            # Reconstruct seed from last completed generation
            if initial_seed is not None:
                # Caller provided seed explicitly (e.g., after rewind)
                current_seed = initial_seed
            elif lineage.generations:
                last_completed = next(
                    (
                        g
                        for g in reversed(lineage.generations)
                        if g.phase == GenerationPhase.COMPLETED
                    ),
                    None,
                )
                if last_completed is None:
                    return Result.err(
                        OuroborosError("Events exist but no completed generations found")
                    )
                if last_completed.seed_json:
                    try:
                        current_seed = Seed.from_dict(json.loads(last_completed.seed_json))
                    except Exception as e:
                        return Result.err(
                            OuroborosError(f"Failed to reconstruct seed from seed_json: {e}")
                        )
                else:
                    return Result.err(
                        OuroborosError(
                            "Cannot reconstruct seed: no seed_json in last generation's events. "
                            "This lineage may have been created with an older version."
                        )
                    )
            else:
                return Result.err(OuroborosError("Events exist but no completed generations found"))

        # Step 2: Run one generation
        timeout = self.config.generation_timeout_seconds or None  # 0 = no timeout
        try:
            gen_result = await asyncio.wait_for(
                self._run_generation(
                    lineage=lineage,
                    generation_number=generation_number,
                    current_seed=current_seed,
                    execute=execute,
                    parallel=parallel,
                ),
                timeout=timeout,
            )
        except TimeoutError:
            # Note: _run_generation's CancelledError handler already emits
            # generation.failed (asyncio.wait_for cancels the task before
            # raising TimeoutError). No duplicate event emission needed here.

            # Return FAILED step result
            failed_gen = GenerationResult(
                generation_number=generation_number,
                seed=current_seed,
                phase=GenerationPhase.FAILED,
                success=False,
            )
            signal = ConvergenceSignal(
                converged=False,
                reason=f"Generation timed out after {self.config.generation_timeout_seconds}s",
                ontology_similarity=0.0,
                generation=generation_number,
            )
            return Result.ok(
                StepResult(
                    generation_result=failed_gen,
                    convergence_signal=signal,
                    lineage=lineage,
                    action=StepAction.FAILED,
                    next_generation=generation_number,
                )
            )

        if gen_result.is_err:
            # Emit generation.failed event so the event store reflects the failure.
            # Without this, only generation.started is recorded, leaving an orphan.
            await self._guard.gated_append(
                lineage_generation_failed(
                    lineage.lineage_id,
                    generation_number,
                    "executing",
                    str(gen_result.error),
                )
            )
            failed_gen = GenerationResult(
                generation_number=generation_number,
                seed=current_seed,
                phase=GenerationPhase.FAILED,
                success=False,
            )
            signal = ConvergenceSignal(
                converged=False,
                reason=str(gen_result.error),
                ontology_similarity=0.0,
                generation=generation_number,
            )
            return Result.ok(
                StepResult(
                    generation_result=failed_gen,
                    convergence_signal=signal,
                    lineage=lineage,
                    action=StepAction.FAILED,
                    next_generation=generation_number,
                )
            )

        result = gen_result.value

        # Step 3: Emit generation completed event (with seed_json)
        record = GenerationRecord(
            generation_number=generation_number,
            seed_id=result.seed.metadata.seed_id,
            parent_seed_id=result.seed.metadata.parent_seed_id,
            ontology_snapshot=result.seed.ontology_schema,
            evaluation_summary=result.evaluation_summary,
            wonder_questions=result.wonder_output.questions if result.wonder_output else (),
            phase=result.phase,
            seed_json=json.dumps(result.seed.to_dict()),
            execution_output=result.execution_output,
        )
        lineage = lineage.with_generation(record)

        await self._guard.gated_append(
            lineage_generation_completed(
                lineage.lineage_id,
                generation_number,
                result.seed.metadata.seed_id,
                result.seed.ontology_schema.model_dump(mode="json"),
                result.evaluation_summary.model_dump(mode="json")
                if result.evaluation_summary
                else None,
                list(result.wonder_output.questions) if result.wonder_output else None,
                seed_json=json.dumps(result.seed.to_dict()),
                execution_output=result.execution_output,
            )
        )

        # Emit ontology evolved event if delta exists
        if result.ontology_delta and result.ontology_delta.similarity < 1.0:
            await self._guard.gated_append(
                lineage_ontology_evolved(
                    lineage.lineage_id,
                    generation_number,
                    result.ontology_delta.model_dump(mode="json"),
                )
            )

        # Step 4: Check convergence
        signal = self._convergence.evaluate(
            lineage,
            result.wonder_output,
            latest_evaluation=result.evaluation_summary,
            validation_output=result.validation_output,
        )

        action = StepAction.CONTINUE
        if signal.converged:
            lineage, action = await self._emit_termination_step(
                signal, lineage, generation_number
            )

        return Result.ok(
            StepResult(
                generation_result=result,
                convergence_signal=signal,
                lineage=lineage,
                action=action,
                next_generation=generation_number + 1,
            )
        )

    async def _emit_termination(
        self,
        signal: ConvergenceSignal,
        lineage: OntologyLineage,
        generation_number: int,
    ) -> OntologyLineage:
        """Emit termination event and update lineage status. Used by run()."""
        tr = signal.termination_reason

        if tr == TerminationReason.EXHAUSTED:
            await self._guard.gated_append(
                lineage_exhausted(
                    lineage.lineage_id,
                    generation_number,
                    self.config.max_generations,
                    termination_reason=str(tr),
                )
            )
            return lineage.with_status(LineageStatus.EXHAUSTED, termination_reason=tr)

        if tr in (
            TerminationReason.STAGNATED,
            TerminationReason.OSCILLATED,
            TerminationReason.REPETITIVE,
        ):
            await self._guard.gated_append(
                lineage_stagnated(
                    lineage.lineage_id,
                    generation_number,
                    signal.reason,
                    self.config.stagnation_window,
                    termination_reason=str(tr),
                )
            )
            return lineage.with_status(LineageStatus.CONVERGED, termination_reason=tr)

        # Default: CONVERGED (ontology stable)
        await self._guard.gated_append(
            lineage_converged(
                lineage.lineage_id,
                generation_number,
                signal.reason,
                signal.ontology_similarity,
                termination_reason=str(tr),
            )
        )
        return lineage.with_status(LineageStatus.CONVERGED, termination_reason=tr)

    async def _emit_termination_step(
        self,
        signal: ConvergenceSignal,
        lineage: OntologyLineage,
        generation_number: int,
    ) -> tuple[OntologyLineage, StepAction]:
        """Emit termination event and return (lineage, action). Used by evolve_step()."""
        tr = signal.termination_reason
        lineage = await self._emit_termination(signal, lineage, generation_number)

        if tr == TerminationReason.EXHAUSTED:
            return lineage, StepAction.EXHAUSTED
        if tr in (
            TerminationReason.STAGNATED,
            TerminationReason.OSCILLATED,
            TerminationReason.REPETITIVE,
        ):
            return lineage, StepAction.STAGNATED
        return lineage, StepAction.CONVERGED

    async def _run_generation(
        self,
        lineage: OntologyLineage,
        generation_number: int,
        current_seed: Seed,
        execute: bool = True,
        parallel: bool = True,
    ) -> Result[GenerationResult, OuroborosError]:
        """Run a single generation within the loop.

        Gen 1: Execute → Evaluate (seed already provided)
        Gen 2+: Wonder → Reflect → Seed → Execute → Evaluate
        """
        try:
            return await self._run_generation_phases(
                lineage=lineage,
                generation_number=generation_number,
                current_seed=current_seed,
                execute=execute,
                parallel=parallel,
            )
        except asyncio.CancelledError:
            # MCP transport disconnect or task cancellation after generation.started
            # was emitted. Emit generation.failed so we don't leave orphaned events.
            logger.warning(
                "evolution.generation.cancelled",
                extra={
                    "lineage_id": lineage.lineage_id,
                    "generation": generation_number,
                },
            )
            try:
                await self._guard.gated_append(
                    lineage_generation_failed(
                        lineage.lineage_id,
                        generation_number,
                        "cancelled",
                        "Generation cancelled (MCP transport disconnect or task cancellation)",
                    )
                )
            except Exception:
                pass  # Best-effort: event store may also be shutting down
            raise

    async def _run_generation_phases(
        self,
        lineage: OntologyLineage,
        generation_number: int,
        current_seed: Seed,
        execute: bool = True,
        parallel: bool = True,
    ) -> Result[GenerationResult, OuroborosError]:
        """Inner implementation of _run_generation with all phase logic.

        Separated from _run_generation to allow CancelledError guard at the
        outer level without deeply nesting the entire method body.
        """
        wonder_output: WonderOutput | None = None
        reflect_output: ReflectOutput | None = None
        ontology_delta: OntologyDelta | None = None

        # Gen 2+: Wonder and Reflect phases
        if generation_number > 1 and lineage.generations:
            prev_gen = lineage.generations[-1]

            # Emit generation started
            await self._guard.gated_append(
                lineage_generation_started(
                    lineage.lineage_id,
                    generation_number,
                    GenerationPhase.WONDERING.value,
                )
            )

            # Wonder phase
            if self.wonder_engine:
                wonder_result = await self.wonder_engine.wonder(
                    current_ontology=current_seed.ontology_schema,
                    evaluation_summary=prev_gen.evaluation_summary,
                    execution_output=prev_gen.execution_output,
                    lineage=lineage,
                )
                if wonder_result.is_ok:
                    wonder_output = wonder_result.value
                    if not wonder_output.should_continue and not wonder_output.questions:
                        # Only early-return if Wonder has NO questions at all.
                        # If questions exist, we must continue to Reflect even if
                        # should_continue=false, because the questions represent
                        # ontological gaps that need to be addressed.
                        logger.info("evolution.wonder.nothing_to_learn")
                        return Result.ok(
                            GenerationResult(
                                generation_number=generation_number,
                                seed=current_seed,
                                wonder_output=wonder_output,
                                phase=GenerationPhase.COMPLETED,
                                success=True,
                            )
                        )
                    if not wonder_output.should_continue and wonder_output.questions:
                        logger.warning(
                            "evolution.wonder.continue_override",
                            extra={
                                "generation": generation_number,
                                "question_count": len(wonder_output.questions),
                                "reason": "Wonder said stop but has unanswered questions",
                            },
                        )
                else:
                    # Wonder degraded - emit event but continue
                    await self._guard.gated_append(
                        lineage_wonder_degraded(
                            lineage.lineage_id,
                            generation_number,
                            str(wonder_result.error),
                        )
                    )

            # Phase transition: wondering → reflecting
            await self._guard.gated_append(
                lineage_generation_phase_changed(
                    lineage.lineage_id,
                    generation_number,
                    GenerationPhase.REFLECTING.value,
                )
            )

            # Reflect phase (with retry on parse failure)
            if self.reflect_engine and wonder_output and prev_gen.evaluation_summary:
                max_reflect_attempts = 2
                for attempt in range(max_reflect_attempts):
                    reflect_result = await self.reflect_engine.reflect(
                        current_seed=current_seed,
                        execution_output=prev_gen.execution_output or "",
                        evaluation_summary=prev_gen.evaluation_summary,
                        wonder_output=wonder_output,
                        lineage=lineage,
                    )

                    if reflect_result.is_ok:
                        break

                    if attempt < max_reflect_attempts - 1:
                        logger.warning(
                            "evolution.reflect.retry",
                            extra={
                                "generation": generation_number,
                                "attempt": attempt + 1,
                                "error": str(reflect_result.error),
                            },
                        )
                    else:
                        await self._guard.gated_append(
                            lineage_generation_failed(
                                lineage.lineage_id,
                                generation_number,
                                GenerationPhase.REFLECTING.value,
                                str(reflect_result.error),
                            )
                        )
                        return Result.err(
                            OuroborosError(
                                f"Reflect failed after {max_reflect_attempts} attempts: {reflect_result.error}"
                            )
                        )

                reflect_output = reflect_result.value

                # Warn if Reflect produced no ontology mutations despite Wonder questions
                if wonder_output.questions and not reflect_output.ontology_mutations:
                    logger.warning(
                        "evolution.reflect.empty_mutations",
                        extra={
                            "generation": generation_number,
                            "wonder_question_count": len(wonder_output.questions),
                        },
                    )

                # Phase transition: reflecting → seeding
                await self._guard.gated_append(
                    lineage_generation_phase_changed(
                        lineage.lineage_id,
                        generation_number,
                        GenerationPhase.SEEDING.value,
                    )
                )

                # Generate evolved seed
                if self.seed_generator:
                    seed_result = self.seed_generator.generate_from_reflect(
                        current_seed,
                        reflect_output,
                    )
                    if seed_result.is_err:
                        await self._guard.gated_append(
                            lineage_generation_failed(
                                lineage.lineage_id,
                                generation_number,
                                GenerationPhase.SEEDING.value,
                                str(seed_result.error),
                            )
                        )
                        return Result.err(
                            OuroborosError(f"Seed generation failed: {seed_result.error}")
                        )
                    new_seed = seed_result.value

                    # Compute ontology delta
                    ontology_delta = OntologyDelta.compute(
                        current_seed.ontology_schema,
                        new_seed.ontology_schema,
                    )

                    current_seed = new_seed

        else:
            # Gen 1: just emit started event
            await self._guard.gated_append(
                lineage_generation_started(
                    lineage.lineage_id,
                    generation_number,
                    GenerationPhase.EXECUTING.value,
                    current_seed.metadata.seed_id,
                )
            )

        # Phase transition: → executing
        await self._guard.gated_append(
            lineage_generation_phase_changed(
                lineage.lineage_id,
                generation_number,
                GenerationPhase.EXECUTING.value,
            )
        )

        # Execute phase (placeholder - actual execution via OrchestratorRunner)
        execution_output: str | None = None
        if execute and self.executor:
            try:
                exec_result = await self.executor(current_seed, parallel=parallel)
                if hasattr(exec_result, "is_ok") and exec_result.is_ok:
                    orch_result = exec_result.value
                    execution_output = getattr(orch_result, "final_message", str(orch_result))
                    # Log structured metadata for observability
                    logger.info(
                        "evolution.generation.executed",
                        extra={
                            "generation": generation_number,
                            "duration_seconds": getattr(orch_result, "duration_seconds", None),
                            "messages_processed": getattr(orch_result, "messages_processed", None),
                            "success": getattr(orch_result, "success", None),
                        },
                    )
                elif hasattr(exec_result, "is_ok"):
                    await self._guard.gated_append(
                        lineage_generation_failed(
                            lineage.lineage_id,
                            generation_number,
                            GenerationPhase.EXECUTING.value,
                            str(exec_result.error),
                        )
                    )
                    return Result.err(OuroborosError(f"Execution failed: {exec_result.error}"))
                else:
                    execution_output = str(exec_result)
            except Exception as e:
                await self._guard.gated_append(
                    lineage_generation_failed(
                        lineage.lineage_id,
                        generation_number,
                        GenerationPhase.EXECUTING.value,
                        str(e),
                    )
                )
                return Result.err(OuroborosError(f"Execution error: {e}"))

        # Validate phase - reconcile parallel execution artifacts
        validation_output: str | None = None
        validation_passed: bool | None = None
        if execute and execution_output and self.validator:
            try:
                validation_result = await self.validator(current_seed, execution_output)
                if isinstance(validation_result, str):
                    validation_output = validation_result
                    validation_passed = True
                elif hasattr(validation_result, "is_ok"):
                    if validation_result.is_ok:
                        validation_output = str(validation_result.value)
                        validation_passed = True
                    else:
                        validation_output = f"Validation error: {validation_result.error}"
                        validation_passed = False
                else:
                    validation_output = str(validation_result)
                    validation_passed = True
                if validation_output and "skipped" in validation_output.lower():
                    validation_passed = False
                    logger.warning(
                        "evolution.generation.validation_skipped",
                        extra={"generation": generation_number, "output": validation_output},
                    )
                else:
                    logger.info(
                        "evolution.generation.validated",
                        extra={"generation": generation_number},
                    )
            except Exception as e:
                logger.warning(
                    "evolution.validation.failed",
                    extra={"error": str(e), "generation": generation_number},
                )
                validation_output = f"Validation skipped: {e}"
                validation_passed = False

        # Phase transition: → evaluating
        await self._guard.gated_append(
            lineage_generation_phase_changed(
                lineage.lineage_id,
                generation_number,
                GenerationPhase.EVALUATING.value,
            )
        )

        # Evaluate phase (placeholder - actual evaluation via EvaluationPipeline)
        evaluation_summary: EvaluationSummary | None = None
        if execute and self.evaluator:
            try:
                eval_result = await self.evaluator(current_seed, execution_output)
                if hasattr(eval_result, "is_ok") and eval_result.is_ok:
                    evaluation_summary = eval_result.value
                elif isinstance(eval_result, EvaluationSummary):
                    evaluation_summary = eval_result
            except Exception as e:
                logger.warning(
                    "evolution.evaluation.failed",
                    extra={"error": str(e), "generation": generation_number},
                )

        # Measure drift after evaluation
        if execution_output:
            try:
                drift_measurement = DriftMeasurement()
                drift_metrics = drift_measurement.measure(
                    current_output=execution_output,
                    constraint_violations=[],
                    current_concepts=[],
                    seed=current_seed,
                )
                drift_event = DriftMeasuredEvent(
                    execution_id=lineage.lineage_id,
                    seed_id=current_seed.metadata.seed_id,
                    iteration=generation_number,
                    metrics=drift_metrics,
                )
                await self._guard.gated_append(drift_event)
            except Exception as e:
                logger.warning(
                    "evolution.drift.measurement_failed",
                    extra={"error": str(e), "generation": generation_number},
                )

        return Result.ok(
            GenerationResult(
                generation_number=generation_number,
                seed=current_seed,
                execution_output=execution_output,
                evaluation_summary=evaluation_summary,
                wonder_output=wonder_output,
                reflect_output=reflect_output,
                ontology_delta=ontology_delta,
                validation_output=validation_output,
                validation_passed=validation_passed,
                phase=GenerationPhase.COMPLETED,
                success=True,
            )
        )

    async def rewind_to(
        self,
        lineage: OntologyLineage,
        generation_number: int,
    ) -> Result[OntologyLineage, OuroborosError]:
        """Rewind lineage to a specific generation for re-evolution.

        Emits a lineage.rewound event and returns truncated lineage.

        Args:
            lineage: Current lineage.
            generation_number: Generation to rewind to (inclusive).

        Returns:
            Result containing truncated OntologyLineage.
        """
        try:
            from_gen = lineage.current_generation
            rewound = lineage.rewind_to(generation_number)

            from ouroboros.events.lineage import lineage_rewound

            await self._guard.gated_append(
                lineage_rewound(
                    lineage.lineage_id,
                    from_gen,
                    generation_number,
                )
            )

            logger.info(
                "evolution.rewound",
                extra={
                    "lineage_id": lineage.lineage_id,
                    "from": from_gen,
                    "to": generation_number,
                },
            )

            return Result.ok(rewound)

        except ValueError as e:
            return Result.err(OuroborosError(str(e)))
