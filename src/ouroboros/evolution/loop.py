"""EvolutionaryLoop orchestrator for generation-level execution.

Runs Seed → Execute → Evaluate, then Wonder → Reflect feedback until convergence.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from enum import StrEnum
import inspect
import json
import logging
import signal
from typing import Any

from ouroboros.config.models import RuntimeControlsConfig
from ouroboros.core.conductor import ConductorDirective
from ouroboros.core.errors import OuroborosError
from ouroboros.core.lineage import (
    EvaluationSummary,
    GenerationPhase,
    GenerationRecord,
    LineageStatus,
    OntologyDelta,
    OntologyLineage,
)
from ouroboros.core.seed import Seed
from ouroboros.core.types import Result
from ouroboros.events.control import create_control_directive_emitted_event
from ouroboros.events.lineage import (
    lineage_converged,
    lineage_created,
    lineage_exhausted,
    lineage_generation_completed,
    lineage_generation_failed,
    lineage_generation_interrupted,
    lineage_ontology_evolved,
    lineage_stagnated,
    lineage_wonder_degraded,
)
from ouroboros.evolution import focus, frugality, loop_support, provider_usage
from ouroboros.evolution.convergence import ConvergenceCriteria, ConvergenceSignal
from ouroboros.evolution.directive_mapping import (
    is_terminal_directive,
    watchdog_timeout_to_directive,
)
from ouroboros.evolution.drift_recording import record_generation_drift
from ouroboros.evolution.generation_claims import (
    LineageStepClaimDenied,
    StepLease,
    append_lineage_event_if_owner,
    owned_lineage_step,
    step_claims_for,
)
from ouroboros.evolution.reflect import ReflectEngine, ReflectOutput
from ouroboros.evolution.regression import RegressionDetector, RegressionReport
from ouroboros.evolution.rewind import (
    CommittedRewindResult,
    NoOpRewindObserver,
    RewindObserver,
)
from ouroboros.evolution.step_seed import prepare_existing_step
from ouroboros.evolution.validation_result import normalize_validation_result
from ouroboros.evolution.watchdog import (
    GenerationProgressWatchdog,
    GenerationWatchdogTimeout,
)
from ouroboros.evolution.wonder import WonderEngine, WonderOutput
from ouroboros.orchestrator.agent_process import AgentProcess, AgentProcessHandle
from ouroboros.persistence.event_store import EventStore

logger = logging.getLogger(__name__)
_default_runtime_controls = loop_support.default_runtime_controls
_conductor_preservation_error = loop_support.conductor_preservation_error


@dataclass
class EvolutionaryLoopConfig:
    """Configuration for the evolutionary loop."""

    max_generations: int = 30
    convergence_threshold: float = 0.95
    stagnation_window: int = 3
    min_generations: int = 3
    generation_timeout_seconds: int = 0  # Deprecated: use runtime_controls.
    runtime_controls: RuntimeControlsConfig = field(default_factory=_default_runtime_controls)
    enable_oscillation_detection: bool = True
    eval_gate_enabled: bool = True
    eval_min_score: float = 0.7
    outcome_gate_enabled: bool = True
    evaluation_plateau_epsilon: float = 0.01
    scoped_reexecution: bool = True
    focused_evolution: bool = True

    def __post_init__(self) -> None:
        """Map legacy generation_timeout_seconds onto no-progress detection."""
        if self.generation_timeout_seconds > 0:
            self.runtime_controls = RuntimeControlsConfig.model_validate(
                {
                    **self.runtime_controls.model_dump(),
                    "generation_no_progress_timeout_seconds": (self.generation_timeout_seconds),
                }
            )


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
    active_ac_indices: tuple[int, ...] = ()
    frozen_ac_indices: tuple[int, ...] = ()
    frugality_evidence: frugality.EvolutionFrugalityEvidence | None = None
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
    ONTOLOGY_STABLE = "ontology_stable"
    STAGNATED = "stagnated"
    EXHAUSTED = "exhausted"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class StepResult:
    """Result of a single evolve_step() call."""

    generation_result: GenerationResult
    convergence_signal: ConvergenceSignal
    lineage: OntologyLineage
    action: StepAction
    next_generation: int

    @property
    def is_interrupted(self) -> bool:
        """Whether this step was interrupted by graceful shutdown."""
        return self.action == StepAction.INTERRUPTED


@dataclass
class _StepResultContainer:
    """Mutable container for passing StepResult out of AgentProcess work."""

    result: Result[StepResult, OuroborosError] | None = None


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
        agent_process: AgentProcess | None = None,
        rewind_observer: RewindObserver | None = None,
    ) -> None:
        self.event_store = event_store
        self.config = config or EvolutionaryLoopConfig()
        self.wonder_engine = wonder_engine
        self.reflect_engine = reflect_engine
        self.seed_generator = seed_generator
        self.executor = executor
        self.evaluator = evaluator
        self.validator = validator
        self._rewind_observer = (
            rewind_observer if rewind_observer is not None else NoOpRewindObserver()
        )
        self._agent_process = agent_process or AgentProcess(event_store=event_store)
        self._project_dir_context: ContextVar[str | None] = ContextVar(
            "evolutionary_loop_project_dir",
            default=None,
        )
        self._shutdown_requested = False
        self._shutdown_event = asyncio.Event()
        self._original_sigint_handler: signal.Handlers | None = None
        self._sigint_installed = False
        self._convergence = ConvergenceCriteria(
            convergence_threshold=self.config.convergence_threshold,
            stagnation_window=self.config.stagnation_window,
            min_generations=self.config.min_generations,
            max_generations=self.config.max_generations,
            enable_oscillation_detection=self.config.enable_oscillation_detection,
            eval_gate_enabled=self.config.eval_gate_enabled,
            eval_min_score=self.config.eval_min_score,
            outcome_gate_enabled=self.config.outcome_gate_enabled,
            evaluation_plateau_epsilon=self.config.evaluation_plateau_epsilon,
        )

    def _install_sigint_handler(self) -> None:
        """Replace SIGINT handler with graceful shutdown flag."""
        if self._sigint_installed:
            return
        self._shutdown_requested = False
        self._shutdown_event = asyncio.Event()

        def _handle_sigint(signum: int, frame: Any) -> None:  # noqa: ARG001
            if self._shutdown_requested:
                # Second Ctrl+C: force exit
                logger.warning("evolution.force_shutdown")
                raise KeyboardInterrupt
            logger.info("evolution.graceful_shutdown_requested")
            self._shutdown_requested = True
            self._shutdown_event.set()

        try:
            self._original_sigint_handler = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, _handle_sigint)
            self._sigint_installed = True
        except (ValueError, OSError) as exc:
            logger.warning(
                "evolution.sigint_handler_unavailable",
                extra={"reason": str(exc)},
            )
            self._original_sigint_handler = None

    def _uninstall_sigint_handler(self) -> None:
        """Restore the original SIGINT handler."""
        if not self._sigint_installed:
            return
        if self._original_sigint_handler is not None:
            try:
                signal.signal(signal.SIGINT, self._original_sigint_handler)
            except (ValueError, OSError) as exc:
                logger.warning(
                    "evolution.sigint_handler_restore_failed",
                    extra={"reason": str(exc)},
                )
            self._original_sigint_handler = None
        self._sigint_installed = False

    def set_project_dir(self, project_dir: str | None) -> Token[str | None]:
        """Set task-local project directory context for the current generation."""
        return self._project_dir_context.set(project_dir)

    def get_project_dir(self) -> str | None:
        """Return the task-local project directory for the current execution."""
        return self._project_dir_context.get()

    def reset_project_dir(self, token: Token[str | None]) -> None:
        """Restore the previous task-local project directory context."""
        self._project_dir_context.reset(token)

    async def _emit_watchdog_timeout_directive(
        self,
        exc: GenerationWatchdogTimeout,
        *,
        lineage_id: str,
        generation_number: int,
        phase: str,
        action: StepAction | None = None,
    ) -> None:
        """Emit ``control.directive.emitted`` for a mapped watchdog timeout."""
        directive = watchdog_timeout_to_directive(exc.timeout_kind)
        if directive is None:
            return
        await self.event_store.append(
            create_control_directive_emitted_event(
                target_type="lineage",
                target_id=lineage_id,
                emitted_by="evolver.watchdog",
                directive=directive,
                reason=exc.message,
                execution_id=exc.details.get("execution_id"),
                lineage_id=lineage_id,
                generation_number=generation_number,
                phase=phase,
                extra={
                    "timeout_kind": exc.timeout_kind,
                    "watchdog_details": dict(exc.details),
                    "is_terminal": is_terminal_directive(directive),
                    **({"step_action": action.value} if action is not None else {}),
                },
            )
        )

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
        await self.event_store.append(lineage_created(lineage.lineage_id, lineage.goal))

        self._install_sigint_handler()
        generation_results: list[GenerationResult] = []
        current_seed = initial_seed

        try:
            return await self._run_loop(
                lineage,
                current_seed,
                generation_results,
            )
        finally:
            self._uninstall_sigint_handler()

    async def _run_loop(
        self,
        lineage: OntologyLineage,
        current_seed: Seed,
        generation_results: list[GenerationResult],
    ) -> Result[EvolutionaryResult, OuroborosError]:
        """Inner loop extracted for SIGINT handler bracket."""
        generation_number = 0
        failure_error: OuroborosError | None = None

        while True:
            generation_number += 1

            logger.info(
                "evolution.generation.starting",
                extra={
                    "lineage_id": lineage.lineage_id,
                    "generation": generation_number,
                },
            )

            # Run generation with progress-aware liveness controls.
            gen_result = await self._run_generation_with_watchdog(
                lineage=lineage,
                generation_number=generation_number,
                current_seed=current_seed,
            )
            if gen_result.is_err and isinstance(gen_result.error, GenerationWatchdogTimeout):
                failure_error = gen_result.error
                logger.error(
                    "evolution.generation.watchdog_timeout",
                    extra={
                        "lineage_id": lineage.lineage_id,
                        "generation": generation_number,
                        "timeout_kind": gen_result.error.timeout_kind,
                        "details": gen_result.error.details,
                    },
                )
                if loop_support.watchdog_has_directive_metadata(gen_result.error.details):
                    await self._emit_watchdog_timeout_directive(
                        gen_result.error,
                        lineage_id=lineage.lineage_id,
                        generation_number=generation_number,
                        phase=await loop_support.phase_for_failed_step_directive(
                            self.event_store,
                            lineage_id=lineage.lineage_id,
                            generation_number=generation_number,
                        ),
                        action=StepAction(
                            loop_support.watchdog_timeout_action(gen_result.error.timeout_kind)
                        ),
                    )
                break

            if gen_result.is_err:
                failure_error = gen_result.error
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

            # Graceful shutdown: generation was interrupted between phases
            if result.phase == GenerationPhase.INTERRUPTED:
                logger.info(
                    "evolution.generation.interrupted_gracefully",
                    extra={
                        "lineage_id": lineage.lineage_id,
                        "generation": generation_number,
                    },
                )
                generation_results.append(result)
                current_seed = result.seed  # Use interrupted gen's seed (may be evolved)
                break

            generation_results.append(result)

            # Record generation in lineage
            seed_json = json.dumps(result.seed.to_dict())
            record = GenerationRecord(
                generation_number=generation_number,
                seed_id=result.seed.metadata.seed_id,
                parent_seed_id=result.seed.metadata.parent_seed_id,
                ontology_snapshot=result.seed.ontology_schema,
                evaluation_summary=result.evaluation_summary,
                wonder_questions=result.wonder_output.questions if result.wonder_output else (),
                phase=result.phase,
                seed_json=seed_json,
                execution_output=result.execution_output,
                active_ac_indices=result.active_ac_indices,
                frozen_ac_indices=result.frozen_ac_indices,
            )
            lineage = lineage.with_generation(record)

            # Emit generation completed event (with seed_json for cross-session reconstruction)
            await self.event_store.append(
                lineage_generation_completed(
                    lineage.lineage_id,
                    generation_number,
                    result.seed.metadata.seed_id,
                    result.seed.ontology_schema.model_dump(mode="json"),
                    result.evaluation_summary.model_dump(mode="json")
                    if result.evaluation_summary
                    else None,
                    list(result.wonder_output.questions) if result.wonder_output else None,
                    seed_json=seed_json,
                    execution_output=result.execution_output,
                    parent_seed_id=result.seed.metadata.parent_seed_id,
                    seed_quality_canary_feedback=[
                        feedback.model_dump(mode="json")
                        for feedback in record.seed_quality_canary_feedback
                    ]
                    or None,
                    active_ac_indices=list(result.active_ac_indices),
                    frozen_ac_indices=list(result.frozen_ac_indices),
                )
            )

            # Emit ontology evolved event if delta exists
            if result.ontology_delta and result.ontology_delta.similarity < 1.0:
                await self.event_store.append(
                    lineage_ontology_evolved(
                        lineage.lineage_id,
                        generation_number,
                        result.ontology_delta.model_dump(mode="json"),
                    )
                )

            # Check convergence
            conv_signal = self._convergence.evaluate(
                lineage,
                result.wonder_output,
                latest_evaluation=result.evaluation_summary,
                validation_output=result.validation_output,
                latest_seed=result.seed,
                evaluation_expected=True,
                validation_expected=self.validator is not None,
            )

            if conv_signal.should_stop:
                logger.info(
                    "evolution.converged" if conv_signal.converged else "evolution.stopped",
                    extra={
                        "lineage_id": lineage.lineage_id,
                        "generation": generation_number,
                        "reason": conv_signal.reason,
                        "similarity": conv_signal.ontology_similarity,
                        "converged": conv_signal.converged,
                    },
                )

                # Emit appropriate termination event
                if conv_signal.converged:
                    await self.event_store.append(
                        lineage_converged(
                            lineage.lineage_id,
                            generation_number,
                            conv_signal.reason,
                            conv_signal.ontology_similarity,
                        )
                    )
                    lineage = lineage.with_status(LineageStatus.CONVERGED)
                elif generation_number >= self.config.max_generations:
                    await self.event_store.append(
                        lineage_exhausted(
                            lineage.lineage_id,
                            generation_number,
                            self.config.max_generations,
                        )
                    )
                    lineage = lineage.with_status(LineageStatus.EXHAUSTED)
                else:
                    await self.event_store.append(
                        lineage_stagnated(
                            lineage.lineage_id,
                            generation_number,
                            conv_signal.reason,
                            self.config.stagnation_window,
                        )
                    )
                    # Stagnation is a non-terminal control handoff: the shared
                    # Directive contract maps STAGNATED to UNSTUCK, so keep the
                    # lineage resumable for the lateral-thinking recovery path.
                break

            # Prepare for next generation
            current_seed = result.seed

        # Best-so-far recovery: if no generations completed, report error
        # But allow interrupted results through (they enable resume)
        completed_results = [r for r in generation_results if r.phase == GenerationPhase.COMPLETED]
        has_interrupted = any(r.phase == GenerationPhase.INTERRUPTED for r in generation_results)
        if not completed_results and not has_interrupted:
            return Result.err(
                failure_error or OuroborosError("No generations completed before failure")
            )

        # Partial results available — return best-so-far (lineage stays ACTIVE for resume)
        # total_generations counts only completed generations to avoid overstating progress
        return Result.ok(
            EvolutionaryResult(
                lineage=lineage,
                total_generations=len(completed_results),
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
        conductor_directive: ConductorDirective | None = None,
        benchmark_control: bool = False,
        on_generation_claimed: loop_support.GenerationClaimCallback | None = None,
    ) -> Result[StepResult, OuroborosError]:
        """Advance one lineage once while a durable lease owns all effects."""
        from ouroboros.evolution.step_receipt import decode_step_result, encode_step_result

        durable_policy = loop_support.evolution_execution_policy(self.config, benchmark_control)

        async def _owned() -> Result[StepResult, OuroborosError]:
            try:
                async with owned_lineage_step(
                    step_claims_for(self.event_store), lineage_id
                ) as lease:
                    while True:
                        generation_number = await loop_support.planned_evolve_generation(
                            self.event_store, lineage_id, execute=execute
                        )
                        request_key = loop_support.evolve_request_key(
                            initial_seed,
                            execute=execute,
                            parallel=parallel,
                            conductor_directive=conductor_directive,
                            project_dir=self.get_project_dir(),
                            generation_number=generation_number,
                            execution_policy=durable_policy,
                        )
                        try:
                            return await loop_support.run_lineage_single_flight(
                                self.event_store,
                                lineage_id,
                                request_key,
                                lambda: loop_support.run_durable_lineage_single_flight(
                                    self.event_store,
                                    lineage_id,
                                    request_key,
                                    lambda: self._evolve_step_once(
                                        lineage_id,
                                        initial_seed=initial_seed,
                                        execute=execute,
                                        parallel=parallel,
                                        conductor_directive=conductor_directive,
                                        lease=lease,
                                    ),
                                    generation_number=generation_number,
                                    encode=encode_step_result,
                                    decode=lambda payload: decode_step_result(
                                        self.event_store, payload
                                    ),
                                    on_claimed=on_generation_claimed,
                                ),
                                replan_on_different=True,
                            )
                        except loop_support.LineageWinnerAdvanced:
                            continue
            except LineageStepClaimDenied as denied:
                return Result.err(OuroborosError(str(denied)))

        preflight_key = loop_support.evolve_request_key(
            initial_seed,
            execute=execute,
            parallel=parallel,
            conductor_directive=conductor_directive,
            project_dir=self.get_project_dir(),
            execution_policy=durable_policy,
        )
        with loop_support.evolution_execution_policy_context(self.config, benchmark_control):
            try:
                return await loop_support.run_lineage_single_flight(
                    self.event_store,
                    lineage_id,
                    preflight_key,
                    _owned,
                    scope="evolve-lease",
                    reject_different=True,
                )
            except loop_support.LineageFlightConflict as conflict:
                return Result.err(OuroborosError(str(conflict)))

    @staticmethod
    def _lease_lost_error(context: str) -> OuroborosError:
        return OuroborosError(
            f"evolve_step: lineage step lease was lost {context}; "
            "the reclaiming caller owns this lineage's record"
        )

    async def _evolve_step_once(
        self,
        lineage_id: str,
        initial_seed: Seed | None = None,
        execute: bool = True,
        parallel: bool = True,
        conductor_directive: ConductorDirective | None = None,
        lease: StepLease | None = None,
    ) -> Result[StepResult, OuroborosError]:
        """Run one event-reconstructed generation under lineage ownership."""
        if lease is None:
            return Result.err(OuroborosError("evolve_step requires a lineage step lease"))
        hard_crash_record: GenerationRecord | None = None

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
            created = await append_lineage_event_if_owner(
                self.event_store,
                step_claims_for(self.event_store),
                lineage_id,
                lease,
                lineage_created(lineage.lineage_id, lineage.goal),
            )
            if not created:
                return Result.err(self._lease_lost_error("before the lineage was created"))
            generation_number = 1
            current_seed = initial_seed
            last_phase = GenerationPhase.COMPLETED  # Gen 1: no prior state
            interrupted_at_phase = None

        else:
            prepared = prepare_existing_step(events, initial_seed=initial_seed, execute=execute)
            if prepared.is_err:
                return Result.err(prepared.error)
            (
                lineage,
                generation_number,
                last_phase,
                interrupted_at_phase,
                current_seed,
                hard_crash_record,
            ) = prepared.value

        if lineage.verification_handoff_pending and not execute:
            previous = next(
                record
                for record in reversed(lineage.generations)
                if record.phase == GenerationPhase.COMPLETED
            )
            reason = "Stable Seed is awaiting Execute -> Evaluate verification"
            return Result.ok(
                StepResult(
                    generation_result=GenerationResult(
                        generation_number=previous.generation_number,
                        seed=current_seed,
                        execution_output=previous.execution_output,
                        evaluation_summary=previous.evaluation_summary,
                        active_ac_indices=previous.active_ac_indices,
                        frozen_ac_indices=previous.frozen_ac_indices,
                    ),
                    convergence_signal=ConvergenceSignal(
                        converged=False,
                        reason=reason,
                        ontology_similarity=1.0,
                        generation=previous.generation_number,
                        should_stop=True,
                        ontology_stable=True,
                    ),
                    lineage=lineage,
                    action=StepAction.ONTOLOGY_STABLE,
                    next_generation=generation_number,
                )
            )

        approved_seed = current_seed
        if conductor_directive is not None:
            current_seed = Seed.from_dict(
                {
                    **current_seed.to_dict(),
                    "conductor_directive": conductor_directive.to_event_data(),
                }
            )

        # Run one generation in AgentProcess; replayed state stays outside its boundary.
        resume_after_phase = None
        if last_phase == GenerationPhase.INTERRUPTED:
            resume_after_phase = interrupted_at_phase
        elif hard_crash_record is not None:
            resume_after_phase = hard_crash_record.last_completed_phase
        container = _StepResultContainer()

        async def _generation_work(handle: AgentProcessHandle) -> None:
            self._install_sigint_handler()
            try:
                # Persist pause, cancel, and SIGINT through the normal shutdown checkpoint.
                interrupted_before_start = await self._check_shutdown(
                    lineage.lineage_id,
                    generation_number,
                    resume_after_phase,
                    current_seed,
                    agent_process_handle=handle,
                )
                if interrupted_before_start is not None:
                    conv_signal = ConvergenceSignal(
                        converged=False,
                        reason=(
                            "AgentProcess cancel requested before generation start"
                            if handle.should_cancel()
                            else "Generation interrupted by SIGINT"
                        ),
                        ontology_similarity=0.0,
                        generation=generation_number,
                    )
                    await loop_support.emit_step_directive(
                        self.event_store,
                        StepAction.INTERRUPTED,
                        lineage_id=lineage.lineage_id,
                        generation_number=generation_number,
                        phase="interrupted",
                        reason=conv_signal.reason,
                    )
                    container.result = Result.ok(
                        StepResult(
                            generation_result=interrupted_before_start,
                            convergence_signal=conv_signal,
                            lineage=await loop_support.refresh_lineage(self.event_store, lineage),
                            action=StepAction.INTERRUPTED,
                            next_generation=generation_number,
                        )
                    )
                    return

                gen_result = await self._run_generation_with_watchdog(
                    lineage=lineage,
                    generation_number=generation_number,
                    current_seed=current_seed,
                    execute=execute,
                    parallel=parallel,
                    resume_after_phase=resume_after_phase,
                    agent_process_handle=handle,
                    conductor_directive=conductor_directive,
                    lease=lease,
                )
            finally:
                self._uninstall_sigint_handler()

            if gen_result.is_err and isinstance(gen_result.error, GenerationWatchdogTimeout):
                failed_gen = GenerationResult(
                    generation_number=generation_number,
                    seed=current_seed,
                    phase=GenerationPhase.FAILED,
                    success=False,
                )
                conv_signal = ConvergenceSignal(
                    converged=False,
                    reason=gen_result.error.message,
                    ontology_similarity=0.0,
                    generation=generation_number,
                )
                if loop_support.watchdog_has_directive_metadata(gen_result.error.details):
                    watchdog_action = StepAction(
                        loop_support.watchdog_timeout_action(gen_result.error.timeout_kind)
                    )
                    await self._emit_watchdog_timeout_directive(
                        gen_result.error,
                        lineage_id=lineage.lineage_id,
                        generation_number=generation_number,
                        phase=await loop_support.phase_for_failed_step_directive(
                            self.event_store,
                            lineage_id=lineage.lineage_id,
                            generation_number=generation_number,
                        ),
                        action=watchdog_action,
                    )
                else:
                    watchdog_action = StepAction.FAILED
                container.result = Result.ok(
                    StepResult(
                        generation_result=failed_gen,
                        convergence_signal=conv_signal,
                        lineage=await loop_support.refresh_lineage(self.event_store, lineage),
                        action=watchdog_action,
                        next_generation=generation_number,
                    )
                )
                return

            if gen_result.is_err:
                # Note: _run_generation_phases already emits a phase-specific
                # generation.failed event. No duplicate emission here.
                failed_gen = GenerationResult(
                    generation_number=generation_number,
                    seed=current_seed,
                    phase=GenerationPhase.FAILED,
                    success=False,
                )
                conv_signal = ConvergenceSignal(
                    converged=False,
                    reason=str(gen_result.error),
                    ontology_similarity=0.0,
                    generation=generation_number,
                )
                await loop_support.emit_step_directive(
                    self.event_store,
                    StepAction.FAILED,
                    lineage_id=lineage.lineage_id,
                    generation_number=generation_number,
                    phase=await loop_support.phase_for_failed_step_directive(
                        self.event_store,
                        lineage_id=lineage.lineage_id,
                        generation_number=generation_number,
                    ),
                    reason=conv_signal.reason,
                )
                container.result = Result.ok(
                    StepResult(
                        generation_result=failed_gen,
                        convergence_signal=conv_signal,
                        lineage=await loop_support.refresh_lineage(self.event_store, lineage),
                        action=StepAction.FAILED,
                        next_generation=generation_number,
                    )
                )
                return

            result = gen_result.value

            async def _append_owned(event: Any) -> bool:
                appended = await append_lineage_event_if_owner(
                    self.event_store,
                    step_claims_for(self.event_store),
                    lineage.lineage_id,
                    lease,
                    event,
                )
                if not appended:
                    logger.warning(
                        "evolution.generation.fenced_write_refused",
                        extra={"lineage_id": lineage.lineage_id, "generation": generation_number},
                    )
                    container.result = Result.err(self._lease_lost_error("before persistence"))
                return appended

            preservation_error = _conductor_preservation_error(
                approved_seed,
                result.seed,
                conductor_directive,
            )
            if preservation_error is not None:
                if not await _append_owned(
                    lineage_generation_failed(
                        lineage.lineage_id,
                        generation_number,
                        "conductor_preservation",
                        preservation_error,
                    )
                ):
                    return
                container.result = Result.err(OuroborosError(preservation_error))
                return

            # After generation work has returned a completed result, finish
            # durable lineage/post-processing writes without another
            # cooperative cancellation checkpoint. Cancelling here would drop
            # already-completed generation side effects before
            # lineage.generation.completed is journaled, making replay rerun
            # work that may have already happened.

            # Handle graceful interruption — return without emitting completed.
            if result.phase == GenerationPhase.INTERRUPTED:
                conv_signal = ConvergenceSignal(
                    converged=False,
                    reason=(
                        "AgentProcess cancel requested during generation"
                        if handle.should_cancel()
                        else "Generation interrupted by SIGINT"
                    ),
                    ontology_similarity=0.0,
                    generation=generation_number,
                )
                await loop_support.emit_step_directive(
                    self.event_store,
                    StepAction.INTERRUPTED,
                    lineage_id=lineage.lineage_id,
                    generation_number=generation_number,
                    phase="interrupted",
                    reason=conv_signal.reason,
                )
                container.result = Result.ok(
                    StepResult(
                        generation_result=result,
                        convergence_signal=conv_signal,
                        lineage=await loop_support.refresh_lineage(self.event_store, lineage),
                        action=StepAction.INTERRUPTED,
                        next_generation=generation_number,
                    )
                )
                return

            handle.complete_on_return_after_cancel()

            # Step 3: Emit generation completed event (with seed_json).
            nonlocal_lineage = lineage
            seed_json = json.dumps(result.seed.to_dict())
            record = GenerationRecord(
                generation_number=generation_number,
                seed_id=result.seed.metadata.seed_id,
                parent_seed_id=result.seed.metadata.parent_seed_id,
                ontology_snapshot=result.seed.ontology_schema,
                evaluation_summary=result.evaluation_summary,
                wonder_questions=result.wonder_output.questions if result.wonder_output else (),
                phase=result.phase,
                seed_json=seed_json,
                execution_output=result.execution_output,
                active_ac_indices=result.active_ac_indices,
                frozen_ac_indices=result.frozen_ac_indices,
            )
            nonlocal_lineage = nonlocal_lineage.with_generation(record)
            # Persist the stable Seed and its verification handoff atomically.
            conv_signal = self._convergence.evaluate(
                nonlocal_lineage,
                result.wonder_output,
                latest_evaluation=result.evaluation_summary,
                validation_output=result.validation_output,
                latest_seed=result.seed,
                evaluation_expected=execute,
                validation_expected=execute and self.validator is not None,
            )
            if conv_signal.ontology_stable:
                record = record.model_copy(update={"verification_handoff_pending": True})
                nonlocal_lineage = nonlocal_lineage.with_generation(record)
            if not await _append_owned(
                lineage_generation_completed(
                    nonlocal_lineage.lineage_id,
                    generation_number,
                    result.seed.metadata.seed_id,
                    result.seed.ontology_schema.model_dump(mode="json"),
                    result.evaluation_summary.model_dump(mode="json")
                    if result.evaluation_summary
                    else None,
                    list(result.wonder_output.questions) if result.wonder_output else None,
                    seed_json=seed_json,
                    execution_output=result.execution_output,
                    parent_seed_id=result.seed.metadata.parent_seed_id,
                    seed_quality_canary_feedback=[
                        feedback.model_dump(mode="json")
                        for feedback in record.seed_quality_canary_feedback
                    ]
                    or None,
                    active_ac_indices=list(result.active_ac_indices),
                    frozen_ac_indices=list(result.frozen_ac_indices),
                    verification_handoff_pending=record.verification_handoff_pending,
                )
            ):
                return
            # Emit ontology evolved event if delta exists.
            if result.ontology_delta and result.ontology_delta.similarity < 1.0:
                if not await _append_owned(
                    lineage_ontology_evolved(
                        nonlocal_lineage.lineage_id,
                        generation_number,
                        result.ontology_delta.model_dump(mode="json"),
                    )
                ):
                    return
            action = StepAction.CONTINUE
            if conv_signal.should_stop:
                if conv_signal.converged:
                    if not await _append_owned(
                        lineage_converged(
                            nonlocal_lineage.lineage_id,
                            generation_number,
                            conv_signal.reason,
                            conv_signal.ontology_similarity,
                        )
                    ):
                        return
                    nonlocal_lineage = nonlocal_lineage.with_status(LineageStatus.CONVERGED)
                    action = StepAction.CONVERGED
                elif conv_signal.ontology_stable:
                    action = StepAction.ONTOLOGY_STABLE
                elif generation_number >= self.config.max_generations:
                    if not await _append_owned(
                        lineage_exhausted(
                            nonlocal_lineage.lineage_id,
                            generation_number,
                            self.config.max_generations,
                        )
                    ):
                        return
                    nonlocal_lineage = nonlocal_lineage.with_status(LineageStatus.EXHAUSTED)
                    action = StepAction.EXHAUSTED
                else:
                    if not await _append_owned(
                        lineage_stagnated(
                            nonlocal_lineage.lineage_id,
                            generation_number,
                            conv_signal.reason,
                            self.config.stagnation_window,
                        )
                    ):
                        return
                    # Stagnation is a non-terminal control handoff: the shared
                    # Directive contract maps STAGNATED to UNSTUCK, so keep the
                    # lineage resumable for the lateral-thinking recovery path.
                    action = StepAction.STAGNATED
            try:
                await loop_support.emit_step_directive(
                    self.event_store,
                    action,
                    lineage_id=nonlocal_lineage.lineage_id,
                    generation_number=generation_number,
                    phase=str(result.phase),
                    reason=conv_signal.reason,
                )
            except Exception:
                if action is not StepAction.ONTOLOGY_STABLE:
                    raise
                logger.warning("evolution.ontology_stable.directive_emit_failed", exc_info=True)
            container.result = Result.ok(
                StepResult(
                    generation_result=result,
                    convergence_signal=conv_signal,
                    lineage=nonlocal_lineage,
                    action=action,
                    next_generation=generation_number + 1,
                )
            )

        if lease.lost.is_set():
            return Result.err(self._lease_lost_error("before the generation started"))
        handle = await self._agent_process.spawn(
            intent=f"evolve_step generation={generation_number}",
            work_fn=_generation_work,
        )

        async def _fence_on_lease_loss() -> None:
            await lease.lost.wait()
            await handle.abort(reason="lineage step lease lost to a reclaimer")

        fence = asyncio.create_task(_fence_on_lease_loss())
        try:
            await handle.wait_until_complete()
        except asyncio.CancelledError:
            if handle.should_complete_on_return_after_cancel():
                await handle.cancel(
                    reason="evolve_step caller cancelled after generation completed"
                )
            else:
                await handle.abort(reason="evolve_step caller cancelled")
            with suppress(asyncio.CancelledError):
                await asyncio.shield(handle.wait_until_complete())
            raise
        finally:
            fence.cancel()
            with suppress(asyncio.CancelledError):
                await fence

        if container.result is None:
            failure = handle.failure()
            if failure is not None:
                return Result.err(
                    OuroborosError(
                        "evolve_step: agent process failed during generation work: "
                        f"{type(failure).__name__}: {failure!s}"
                    )
                )
            return Result.err(OuroborosError("evolve_step: agent process exited without result"))
        return container.result

    async def _run_generation(
        self,
        lineage: OntologyLineage,
        generation_number: int,
        current_seed: Seed,
        execute: bool = True,
        parallel: bool = True,
        resume_after_phase: str | None = None,
        execution_id: str | None = None,
        agent_process_handle: AgentProcessHandle | None = None,
        conductor_directive: ConductorDirective | None = None,
        lease: StepLease | None = None,
    ) -> Result[GenerationResult, OuroborosError]:
        """Run a single generation within the loop.

        Gen 1: Execute → Evaluate (seed already provided)
        Gen 2+: Wonder → Reflect → Seed → Execute → Evaluate

        Args:
            resume_after_phase: If set, skip phases up to and including this
                phase (for resuming interrupted generations).
        """
        try:
            return await self._run_generation_phases(
                lineage=lineage,
                generation_number=generation_number,
                current_seed=current_seed,
                execute=execute,
                parallel=parallel,
                resume_after_phase=resume_after_phase,
                execution_id=execution_id,
                agent_process_handle=agent_process_handle,
                conductor_directive=conductor_directive,
            )
        except asyncio.CancelledError:
            if lease is not None and lease.lost.is_set():
                logger.warning(
                    "evolution.generation.fenced",
                    extra={"lineage_id": lineage.lineage_id, "generation": generation_number},
                )
                raise
            # MCP transport disconnect, timeout, or external task cancellation.
            # Use 'failed' (not 'interrupted') to avoid conflicting with the
            # graceful SIGINT shutdown path which emits 'interrupted'.
            logger.warning(
                "evolution.generation.cancelled",
                extra={
                    "lineage_id": lineage.lineage_id,
                    "generation": generation_number,
                },
            )
            try:
                await self.event_store.append(
                    lineage_generation_failed(
                        lineage.lineage_id,
                        generation_number,
                        "cancelled",
                        "Generation cancelled (MCP transport disconnect or task cancellation)",
                    )
                )
            except Exception:
                logger.warning("evolution.generation.cancelled_event_failed", exc_info=True)
            raise

    async def _run_generation_with_watchdog(
        self,
        lineage: OntologyLineage,
        generation_number: int,
        current_seed: Seed,
        execute: bool = True,
        parallel: bool = True,
        resume_after_phase: str | None = None,
        agent_process_handle: AgentProcessHandle | None = None,
        conductor_directive: ConductorDirective | None = None,
        lease: StepLease | None = None,
    ) -> Result[GenerationResult, OuroborosError]:
        """Run one generation under progress-aware liveness controls."""
        execution_id = loop_support.generation_execution_id(lineage.lineage_id, generation_number)
        watchdog = GenerationProgressWatchdog(
            event_store=self.event_store,
            lineage_id=lineage.lineage_id,
            generation_number=generation_number,
            execution_id=execution_id,
            controls=self.config.runtime_controls,
        )
        try:
            return await watchdog.watch(
                self._run_generation(
                    lineage=lineage,
                    generation_number=generation_number,
                    current_seed=current_seed,
                    execute=execute,
                    parallel=parallel,
                    resume_after_phase=resume_after_phase,
                    execution_id=execution_id,
                    agent_process_handle=agent_process_handle,
                    conductor_directive=conductor_directive,
                    lease=lease,
                )
            )
        except GenerationWatchdogTimeout as exc:
            return Result.err(exc)

    async def _call_executor(
        self,
        seed: Seed,
        *,
        parallel: bool,
        execution_id: str | None,
        externally_satisfied_acs: dict[int, dict[str, Any]] | None = None,
    ) -> Any:
        """Call the configured executor with optional execution_id support."""
        return await focus.call_executor(
            self.executor,
            seed,
            parallel=parallel,
            execution_id=execution_id,
            externally_satisfied_acs=externally_satisfied_acs,
        )

    @staticmethod
    def _callable_accepts_keyword(callable_obj: Any, keyword: str) -> bool:
        """Return True when *callable_obj* accepts *keyword*."""
        return focus.callable_accepts_keyword(callable_obj, keyword, inspect.signature)

    async def _check_shutdown(
        self,
        lineage_id: str,
        generation_number: int,
        last_completed_phase: str | None,
        current_seed: Seed,
        wonder_output: WonderOutput | None = None,
        reflect_output: ReflectOutput | None = None,
        execution_output: str | None = None,
        evaluation_summary: EvaluationSummary | None = None,
        validation_output: str | None = None,
        agent_process_handle: AgentProcessHandle | None = None,
    ) -> GenerationResult | None:
        """Check if graceful shutdown was requested.

        Returns a GenerationResult with INTERRUPTED phase if shutdown was
        requested, or None to continue normally.
        """
        agent_process_cancel_requested = False
        if agent_process_handle is not None and not self._shutdown_requested:
            pause_task = asyncio.create_task(agent_process_handle.wait_unpaused())
            shutdown_task = asyncio.create_task(self._shutdown_event.wait())
            done, pending = await asyncio.wait(
                {pause_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError):
                    await task
            for task in done:
                task.result()
            agent_process_cancel_requested = agent_process_handle.should_cancel()

        if not self._shutdown_requested and not agent_process_cancel_requested:
            return None

        logger.info(
            "evolution.generation.graceful_interrupt",
            extra={
                "lineage_id": lineage_id,
                "generation": generation_number,
                "last_completed_phase": last_completed_phase,
            },
        )

        try:
            execution_completed = last_completed_phase in {
                GenerationPhase.EXECUTING.value,
                GenerationPhase.EVALUATING.value,
            }
            partial_state = loop_support.generation_partial_state(
                wonder_output=wonder_output,
                reflect_output=reflect_output,
                execution_output=execution_output,
                execution_boundary_completed=execution_completed,
                validation_output=validation_output,
                validation_boundary_completed=execution_completed,
                evaluation_summary=evaluation_summary,
            )
        except (TypeError, ValueError, KeyError):
            logger.warning("evolution.generation.partial_state_build_failed", exc_info=True)
            partial_state = {}

        try:
            try:
                seed_json_str = json.dumps(current_seed.to_dict())
            except (TypeError, AttributeError):
                seed_json_str = None

            await self.event_store.append(
                lineage_generation_interrupted(
                    lineage_id,
                    generation_number,
                    last_completed_phase=last_completed_phase,
                    partial_state=partial_state or None,
                    seed_json=seed_json_str,
                )
            )
        except Exception:
            logger.error(
                "evolution.generation.interrupted_event_failed",
                extra={
                    "lineage_id": lineage_id,
                    "generation": generation_number,
                    "last_completed_phase": last_completed_phase,
                },
                exc_info=True,
            )
            logger.warning(
                "evolution.generation.resume_may_fail: interrupted event was NOT persisted. "
                "On next resume, this generation may restart from scratch."
            )

        return GenerationResult(
            generation_number=generation_number,
            seed=current_seed,
            wonder_output=wonder_output,
            reflect_output=reflect_output,
            execution_output=execution_output,
            evaluation_summary=evaluation_summary,
            validation_output=validation_output,
            phase=GenerationPhase.INTERRUPTED,
            success=False,
        )

    @provider_usage.capture_provider_usage
    async def _run_generation_phases(
        self,
        lineage: OntologyLineage,
        generation_number: int,
        current_seed: Seed,
        execute: bool = True,
        parallel: bool = True,
        resume_after_phase: str | None = None,
        execution_id: str | None = None,
        agent_process_handle: AgentProcessHandle | None = None,
        conductor_directive: ConductorDirective | None = None,
    ) -> Result[GenerationResult, OuroborosError]:
        """Inner implementation of _run_generation with all phase logic.

        Separated from _run_generation to allow CancelledError guard at the
        outer level without deeply nesting the entire method body.
        """

        def _should_skip(phase: str) -> bool:
            return loop_support.phase_should_skip(phase, resume_after_phase)

        execution_policy = loop_support.current_evolution_execution_policy(self.config)
        ontology_delta: OntologyDelta | None = None
        generation_parent_seed = current_seed
        generation_focus = focus.initial_evolution_focus(current_seed)
        project_baseline = frugality.capture_project_baseline(self.get_project_dir())
        executor_result: Any | None = None
        prev_gen = (
            next(
                (g for g in reversed(lineage.generations) if g.phase == GenerationPhase.COMPLETED),
                lineage.generations[-1],  # fallback if no completed gen exists
            )
            if generation_number > 1 and lineage.generations
            else None
        )
        try:
            # Previous evaluation belongs to the completed parent, not the interrupted candidate.
            generation_parent_seed = loop_support.generation_parent_seed(current_seed, prev_gen)
        except ValueError as exc:
            return Result.err(OuroborosError(str(exc)))
        try:
            restored = loop_support.restore_phase_state(
                lineage,
                generation_number,
                current_seed,
                generation_parent_seed,
                resume_after_phase,
            )
        except ValueError as exc:
            return Result.err(OuroborosError(str(exc)))
        resume_after_phase = restored.resume_after_phase
        wonder_output = restored.wonder_output
        reflect_output = restored.reflect_output
        restored_execution_output = restored.execution_output
        restored_execution_boundary_completed = restored.execution_boundary_completed
        restored_evaluation_summary = restored.evaluation_summary
        restored_validation_output = restored.validation_output
        restored_validation_boundary_completed = restored.validation_boundary_completed
        checkpoint_focus = restored.checkpoint_focus
        if checkpoint_focus is not None:
            generation_focus = checkpoint_focus
        if prev_gen is not None and not (execute and lineage.verification_handoff_pending):
            regression_report: RegressionReport = RegressionDetector().detect(lineage)
            generation_focus = focus.select_evolution_focus(
                generation_parent_seed,
                current_seed,
                prev_gen.evaluation_summary,
                regression_report=regression_report,
                enabled=execution_policy.focused_evolution,
            )

            if self.wonder_engine and not _should_skip("wondering"):
                wonder_kwargs: dict[str, Any] = {
                    "current_ontology": current_seed.ontology_schema,
                    "evaluation_summary": prev_gen.evaluation_summary,
                    "execution_output": prev_gen.execution_output,
                    "lineage": lineage,
                    "seed": current_seed,
                }
                focus.add_active_focus(
                    wonder_kwargs,
                    self.wonder_engine.wonder,
                    generation_focus,
                )
                wonder_result = await self.wonder_engine.wonder(**wonder_kwargs)
                if wonder_result.is_ok:
                    wonder_output = wonder_result.value
                    if (
                        not execute
                        and not wonder_output.should_continue
                        and not wonder_output.questions
                    ):
                        generation_focus = focus.select_evolution_focus(
                            generation_parent_seed,
                            current_seed,
                            prev_gen.evaluation_summary,
                            wonder=wonder_output,
                            regression_report=regression_report,
                            enabled=execution_policy.focused_evolution,
                        )
                        if checkpoint_focus is not None:
                            generation_focus = checkpoint_focus
                        await loop_support.emit_generation_started_once(
                            self.event_store,
                            lineage_id=lineage.lineage_id,
                            generation_number=generation_number,
                            phase=GenerationPhase.WONDERING.value,
                            seed=current_seed,
                            focus=generation_focus,
                        )
                        # Question-bearing output still requires Reflect; only an empty stop returns.
                        await frugality.record_wonder_stop(
                            self,
                            lineage,
                            (
                                generation_number,
                                current_seed,
                                generation_focus,
                                execution_policy,
                            ),
                        )
                        return Result.ok(
                            GenerationResult(
                                generation_number=generation_number,
                                seed=current_seed,
                                wonder_output=wonder_output,
                                active_ac_indices=generation_focus.active_ac_indices,
                                frozen_ac_indices=generation_focus.frozen_ac_indices,
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
                    await self.event_store.append(
                        lineage_wonder_degraded(
                            lineage.lineage_id,
                            generation_number,
                            str(wonder_result.error),
                        )
                    )

            post_wonder_phase = (
                GenerationPhase.WONDERING.value if wonder_output is not None else None
            )
            interrupted = await self._check_shutdown(
                lineage.lineage_id,
                generation_number,
                post_wonder_phase,
                current_seed,
                wonder_output=wonder_output,
                agent_process_handle=agent_process_handle,
            )
            if interrupted:
                return Result.ok(interrupted)

            if not _should_skip("reflecting"):
                # Phase transition: wondering → reflecting
                await self.event_store.append(
                    loop_support.generation_phase_checkpoint(
                        lineage_id=lineage.lineage_id,
                        generation_number=generation_number,
                        phase=GenerationPhase.REFLECTING,
                        last_completed_phase=GenerationPhase.WONDERING.value,
                        seed=current_seed,
                        active_ac_indices=generation_focus.active_ac_indices,
                        frozen_ac_indices=generation_focus.frozen_ac_indices,
                        wonder_output=wonder_output,
                    )
                )

            if (
                self.reflect_engine
                and wonder_output
                and (wonder_output.should_continue or wonder_output.questions)
                and not _should_skip("reflecting")
            ):
                max_reflect_attempts = 2
                for attempt in range(max_reflect_attempts):
                    reflect_kwargs: dict[str, Any] = {
                        "current_seed": current_seed,
                        "execution_output": prev_gen.execution_output or "",
                        "evaluation_summary": prev_gen.evaluation_summary,
                        "wonder_output": wonder_output,
                        "lineage": lineage,
                        "regression_report": regression_report,
                        "conductor_directive": conductor_directive,
                    }
                    focus.add_active_focus(
                        reflect_kwargs,
                        self.reflect_engine.reflect,
                        generation_focus,
                    )
                    reflect_result = await self.reflect_engine.reflect(**reflect_kwargs)

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
                        await self.event_store.append(
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

                # Check for graceful shutdown after Reflect phase
                interrupted = await self._check_shutdown(
                    lineage.lineage_id,
                    generation_number,
                    GenerationPhase.REFLECTING.value,
                    current_seed,
                    wonder_output=wonder_output,
                    reflect_output=reflect_output,
                    agent_process_handle=agent_process_handle,
                )
                if interrupted:
                    return Result.ok(interrupted)

            # Seed generation — outside Reflect block so it runs even when
            # Reflect is skipped on resume (resume_after_phase="reflecting")
            # When seeding is skipped on resume, still compute ontology_delta
            # so lineage.ontology.evolved events are emitted consistently.
            if reflect_output and _should_skip("seeding"):
                # Seeding was already done before interruption; compute delta
                # from the previous generation's ontology to the current seed.
                if lineage.generations:
                    prev_completed = next(
                        (
                            g
                            for g in reversed(lineage.generations)
                            if g.phase == GenerationPhase.COMPLETED
                        ),
                        None,
                    )
                    if prev_completed:
                        ontology_delta = OntologyDelta.compute(
                            prev_completed.ontology_snapshot,
                            current_seed.ontology_schema,
                        )
            elif reflect_output and not _should_skip("seeding"):
                # Phase transition: reflecting → seeding
                await self.event_store.append(
                    loop_support.generation_phase_checkpoint(
                        lineage_id=lineage.lineage_id,
                        generation_number=generation_number,
                        phase=GenerationPhase.SEEDING,
                        last_completed_phase=GenerationPhase.REFLECTING.value,
                        seed=current_seed,
                        active_ac_indices=generation_focus.active_ac_indices,
                        frozen_ac_indices=generation_focus.frozen_ac_indices,
                        wonder_output=wonder_output,
                        reflect_output=reflect_output,
                    )
                )

                if self.seed_generator:
                    seed_result = self.seed_generator.generate_from_reflect(
                        current_seed,
                        reflect_output,
                    )
                    if seed_result.is_err:
                        await self.event_store.append(
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
                    if conductor_directive is not None:
                        new_seed = Seed.from_dict(
                            {
                                **new_seed.to_dict(),
                                "conductor_directive": conductor_directive.to_event_data(),
                            }
                        )

                    preservation_error = _conductor_preservation_error(
                        current_seed,
                        new_seed,
                        conductor_directive,
                    )
                    if preservation_error is not None:
                        await self.event_store.append(
                            lineage_generation_failed(
                                lineage.lineage_id,
                                generation_number,
                                "conductor_preservation",
                                preservation_error,
                            )
                        )
                        return Result.err(OuroborosError(preservation_error))

                    # Compute ontology delta
                    ontology_delta = OntologyDelta.compute(
                        current_seed.ontology_schema,
                        new_seed.ontology_schema,
                    )

                    current_seed = new_seed

        else:
            # Gen 1, or a Gen 2+ ontology-stable verification handoff.
            await loop_support.emit_execution_started_once(
                self.event_store,
                lineage.lineage_id,
                generation_number,
                current_seed,
                generation_focus,
            )

        if prev_gen is not None and not (execute and lineage.verification_handoff_pending):
            generation_focus = focus.select_evolution_focus(
                generation_parent_seed,
                current_seed,
                prev_gen.evaluation_summary,
                wonder=wonder_output,
                regression_report=regression_report,
                enabled=execution_policy.focused_evolution,
            )
            if checkpoint_focus is not None:
                generation_focus = checkpoint_focus
            generation_focus.log_selection(generation_number)
            await loop_support.emit_execution_started_once(
                self.event_store,
                lineage.lineage_id,
                generation_number,
                current_seed,
                generation_focus,
            )

        # Check for graceful shutdown before executing.
        # Derive the actual last completed phase from what ran:
        # - reflect_output set → seeding completed
        # - wonder_output set but no reflect → only wondering completed
        # - neither → Gen 1 or no prior phases ran
        if reflect_output is not None:
            pre_exec_phase = GenerationPhase.SEEDING.value
        elif wonder_output is not None:
            pre_exec_phase = GenerationPhase.WONDERING.value
        else:
            pre_exec_phase = None  # Gen 1: no phase completed yet
        interrupted = await self._check_shutdown(
            lineage.lineage_id,
            generation_number,
            pre_exec_phase,
            current_seed,
            wonder_output=wonder_output,
            reflect_output=reflect_output,
            agent_process_handle=agent_process_handle,
        )
        if interrupted:
            return Result.ok(interrupted)

        if not _should_skip("executing"):
            # Phase transition: → executing
            await self.event_store.append(
                loop_support.generation_phase_checkpoint(
                    lineage_id=lineage.lineage_id,
                    generation_number=generation_number,
                    phase=GenerationPhase.EXECUTING,
                    last_completed_phase=pre_exec_phase,
                    seed=current_seed,
                    active_ac_indices=generation_focus.active_ac_indices,
                    frozen_ac_indices=generation_focus.frozen_ac_indices,
                    wonder_output=wonder_output,
                    reflect_output=reflect_output,
                )
            )

        execution_output: str | None = restored_execution_output
        if restored_execution_boundary_completed and _should_skip("executing"):
            logger.info(
                "evolution.generation.execution_restored_from_checkpoint",
                extra={"generation": generation_number},
            )
        elif execute and self.executor:
            externally_satisfied_acs = focus.select_externally_satisfied_acs(
                scoped_reexecution=execution_policy.scoped_reexecution,
                focused_evolution=execution_policy.focused_evolution,
                focus=generation_focus,
                settled_ac_indices=(
                    reflect_output.settled_ac_indices if reflect_output is not None else ()
                ),
                reflect_ran_fresh=not _should_skip("reflecting"),
            )
            try:
                exec_result = await self._call_executor(
                    current_seed,
                    parallel=parallel,
                    execution_id=execution_id,
                    externally_satisfied_acs=externally_satisfied_acs,
                )
                if hasattr(exec_result, "is_ok") and exec_result.is_ok:
                    orch_result = exec_result.value
                    executor_result = orch_result
                    summary = getattr(orch_result, "summary", {})
                    verification_report = (
                        summary.get("verification_report") if isinstance(summary, dict) else None
                    )
                    execution_output = (
                        verification_report
                        if isinstance(verification_report, str) and verification_report
                        else getattr(orch_result, "final_message", str(orch_result))
                    )
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
                    await self.event_store.append(
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
                await self.event_store.append(
                    lineage_generation_failed(
                        lineage.lineage_id,
                        generation_number,
                        GenerationPhase.EXECUTING.value,
                        str(e),
                    )
                )
                return Result.err(OuroborosError(f"Execution error: {e}"))

        # Validate phase - reconcile parallel execution artifacts
        # Skip if restored from checkpoint (resume after evaluating)
        validation_output: str | None = restored_validation_output
        if restored_validation_boundary_completed and _should_skip("executing"):
            logger.info(
                "evolution.generation.validation_restored_from_checkpoint",
                extra={"generation": generation_number},
            )
        elif execute and execution_output and self.validator:
            try:
                validation_result = await focus.call(self.validator, current_seed, execution_output)
                validation_output = normalize_validation_result(validation_result)
                if validation_output and "skipped" in validation_output.lower():
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

        # Check for graceful shutdown after executing
        interrupted = await self._check_shutdown(
            lineage.lineage_id,
            generation_number,
            GenerationPhase.EXECUTING.value,
            current_seed,
            wonder_output=wonder_output,
            reflect_output=reflect_output,
            execution_output=execution_output,
            agent_process_handle=agent_process_handle,
        )
        if interrupted:
            return Result.ok(interrupted)

        if not _should_skip("evaluating"):
            # Phase transition: → evaluating
            await self.event_store.append(
                loop_support.generation_phase_checkpoint(
                    lineage_id=lineage.lineage_id,
                    generation_number=generation_number,
                    phase=GenerationPhase.EVALUATING,
                    last_completed_phase=GenerationPhase.EXECUTING.value,
                    seed=current_seed,
                    active_ac_indices=generation_focus.active_ac_indices,
                    frozen_ac_indices=generation_focus.frozen_ac_indices,
                    wonder_output=wonder_output,
                    reflect_output=reflect_output,
                    execution_output=execution_output,
                    execution_boundary_completed=True,
                    validation_output=validation_output,
                    validation_boundary_completed=True,
                )
            )

        # Evaluate phase (placeholder - actual evaluation via EvaluationPipeline)
        # Skip if already completed before interruption (use restored summary)
        evaluation_summary: EvaluationSummary | None = restored_evaluation_summary
        if _should_skip("evaluating"):
            logger.info(
                "evolution.generation.evaluation_restored_from_checkpoint",
                extra={"generation": generation_number},
            )
        elif execute and self.evaluator:
            try:
                from ouroboros.evolution.evaluation_result import normalize_evaluator_result

                eval_result = await focus.call(self.evaluator, current_seed, execution_output)
                evaluation_summary = normalize_evaluator_result(eval_result)
            except Exception as e:
                logger.warning(
                    "evolution.evaluation.failed",
                    extra={"error": str(e), "generation": generation_number},
                )
                from ouroboros.evolution.evaluation_result import evaluation_error_summary

                evaluation_summary = evaluation_error_summary(str(e))

        await self.event_store.append(
            loop_support.generation_phase_checkpoint(
                lineage_id=lineage.lineage_id,
                generation_number=generation_number,
                phase=GenerationPhase.EVALUATING,
                last_completed_phase=GenerationPhase.EVALUATING.value,
                seed=current_seed,
                active_ac_indices=generation_focus.active_ac_indices,
                frozen_ac_indices=generation_focus.frozen_ac_indices,
                wonder_output=wonder_output,
                reflect_output=reflect_output,
                execution_output=execution_output,
                execution_boundary_completed=True,
                validation_output=validation_output,
                validation_boundary_completed=True,
                evaluation_summary=evaluation_summary,
            )
        )

        await record_generation_drift(
            self.event_store, lineage.lineage_id, generation_number, current_seed, execution_output
        )

        # Check for graceful shutdown after evaluating
        interrupted = await self._check_shutdown(
            lineage.lineage_id,
            generation_number,
            GenerationPhase.EVALUATING.value,
            current_seed,
            wonder_output=wonder_output,
            reflect_output=reflect_output,
            execution_output=execution_output,
            evaluation_summary=evaluation_summary,
            validation_output=validation_output,
            agent_process_handle=agent_process_handle,
        )
        if interrupted:
            return Result.ok(interrupted)

        frugality_evidence = await frugality.record_generation(
            event_store=self.event_store,
            lineage_id=lineage.lineage_id,
            generation_number=generation_number,
            execution_id=execution_id or lineage.lineage_id,
            parent_seed=generation_parent_seed,
            current_seed=current_seed,
            previous_evaluation=(prev_gen.evaluation_summary if prev_gen is not None else None),
            current_evaluation=evaluation_summary,
            focus=generation_focus,
            baseline=project_baseline,
            focused_evolution=execution_policy.focused_evolution,
            scoped_reexecution=execution_policy.scoped_reexecution,
            executor_result=executor_result,
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
                active_ac_indices=generation_focus.active_ac_indices,
                frozen_ac_indices=generation_focus.frozen_ac_indices,
                frugality_evidence=frugality_evidence,
                phase=GenerationPhase.COMPLETED,
                success=True,
            )
        )

    async def rewind_to(
        self,
        lineage: OntologyLineage,
        generation_number: int,
    ) -> Result[CommittedRewindResult, OuroborosError]:
        """Rewind lineage to a specific generation for re-evolution.

        The lineage event is committed before the optional observer runs. The
        captured commit result is returned unchanged if observer dispatch fails.

        Args:
            lineage: Current lineage.
            generation_number: Generation to rewind to (inclusive).

        Returns:
            Result containing the committed rewind identity and lineage.
        """
        try:
            from_gen = lineage.current_generation
            if generation_number == from_gen:
                return Result.err(
                    OuroborosError(f"Already at generation {generation_number}, nothing to rewind")
                )
            rewound = lineage.rewind_to(generation_number)

            from ouroboros.events.lineage import lineage_rewound

            rewind_event = lineage_rewound(
                lineage.lineage_id,
                from_gen,
                generation_number,
            )
        except ValueError as e:
            return Result.err(OuroborosError(str(e)))

        try:
            await self.event_store.append(rewind_event)
        except Exception as e:
            return Result.err(OuroborosError(f"Failed to append rewind event: {e}"))

        committed = CommittedRewindResult(
            lineage=rewound,
            lineage_id=lineage.lineage_id,
            from_generation=from_gen,
            to_generation=generation_number,
            rewind_event_id=rewind_event.id,
            rewind_occurred_at=rewind_event.timestamp,
        )

        logger.info(
            "evolution.rewound",
            extra={
                "lineage_id": lineage.lineage_id,
                "from": from_gen,
                "to": generation_number,
                "rewind_event_id": rewind_event.id,
            },
        )

        try:
            await self._rewind_observer.observe(committed.observation_snapshot())
        except Exception as e:
            logger.warning(
                "evolution.rewind_observer_failed",
                extra={
                    "lineage_id": lineage.lineage_id,
                    "rewind_event_id": rewind_event.id,
                    "error": str(e),
                },
            )

        return Result.ok(committed)
