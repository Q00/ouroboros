"""Double Diamond cycle implementation for Phase 2 Execution.

The Double Diamond is a design thinking pattern with four phases:
1. Discover (Diverge): Explore problem space, gather insights
2. Define (Converge): Converge on approach, filter through ontology
3. Design (Diverge): Create solution options
4. Deliver (Converge): Implement and validate, filter through ontology

This module implements:
- Phase enum with phase metadata (divergent/convergent, ordering)
- PhaseContext for passing state between phases
- PhaseResult for capturing phase outputs
- DoubleDiamond class orchestrating the full cycle
- Retry logic with exponential backoff for phase failures
- Event emission for phase transitions and cycle lifecycle

Usage:
    from ouroboros.execution.double_diamond import DoubleDiamond

    dd = DoubleDiamond(llm_adapter=adapter)
    result = await dd.run_cycle(
        execution_id="exec-123",
        seed_id="seed-456",
        current_ac="Implement user authentication",
        iteration=1,
    )
    if result.is_ok:
        cycle_result = result.value
        print(f"Cycle completed: {cycle_result.success}")
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ouroboros.config import get_double_diamond_model
from ouroboros.core.errors import OuroborosError, ProviderError
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.observability.logging import get_logger
from ouroboros.resilience.stagnation import (
    ExecutionHistory,
    StagnationDetector,
    create_stagnation_event,
)

if TYPE_CHECKING:
    from ouroboros.providers.base import LLMAdapter

log = get_logger(__name__)


# =============================================================================
# Phase-specific prompts (extracted for maintainability)
# =============================================================================

PHASE_PROMPTS: dict[str, dict[str, str]] = {
    "discover": {
        "system": """You are an expert problem analyst in the Discover phase of the Double Diamond process.
Your role is to DIVERGE - explore the problem space widely and gather insights.

Guidelines:
- Ask clarifying questions about requirements
- Identify potential challenges and risks
- Explore different perspectives on the problem
- List assumptions that need validation
- Note any ambiguities or unknowns""",
        "user_template": """Acceptance Criterion: {current_ac}

Execution ID: {execution_id}
Iteration: {iteration}

Explore this problem space. What insights, questions, challenges, and considerations emerge?""",
        "output_key": "insights",
        "event_data_key": "insights_generated",
    },
    "define": {
        "system": """You are an expert analyst in the Define phase of the Double Diamond process.
Your role is to CONVERGE - narrow down and define the approach based on insights gathered.

Guidelines:
- Synthesize insights from the Discover phase
- Define clear requirements and constraints
- Prioritize what's most important
- Make decisions on approach
- Apply ontology filter: ensure alignment with domain concepts""",
        "user_template": """Acceptance Criterion: {current_ac}

Discover Phase Output:
{previous_output}

Based on the insights gathered, define the approach. What requirements, constraints, and priorities emerge?""",
        "output_key": "approach",
        "event_data_key": "approach_defined",
        "previous_phase": "discover",
    },
    "design": {
        "system": """You are an expert solution architect in the Design phase of the Double Diamond process.
Your role is to DIVERGE - create multiple solution options and explore possibilities.

Guidelines:
- Generate multiple solution approaches
- Consider trade-offs for each approach
- Be creative and explore alternatives
- Include both conventional and innovative solutions
- Document assumptions for each approach""",
        "user_template": """Acceptance Criterion: {current_ac}

Define Phase Output:
{previous_output}

Design solution options. What approaches can address the defined requirements?""",
        "output_key": "solution",
        "event_data_key": "solutions_designed",
        "previous_phase": "define",
    },
    "deliver": {
        "system": """You are an expert implementer in the Deliver phase of the Double Diamond process.
Your role is to CONVERGE - select the best solution and implement it.

Guidelines:
- Select the most appropriate solution from Design phase
- Provide concrete implementation details
- Validate the solution meets acceptance criteria
- Apply ontology filter: ensure alignment with domain concepts
- Document what was implemented and why""",
        "user_template": """Acceptance Criterion: {current_ac}

Design Phase Output:
{previous_output}

Deliver the solution. Select the best approach and provide implementation details.""",
        "output_key": "result",
        "event_data_key": "delivery_completed",
        "previous_phase": "design",
    },
}


# =============================================================================
# Phase Enum
# =============================================================================


class Phase(StrEnum):
    """Double Diamond phase enumeration.

    Four phases with alternating diverge/converge pattern:
    - DISCOVER: Diverge - explore problem space
    - DEFINE: Converge - narrow down approach (ontology filter active)
    - DESIGN: Diverge - create solution options
    - DELIVER: Converge - implement and validate (ontology filter active)

    Attributes:
        is_divergent: True for Discover and Design phases
        is_convergent: True for Define and Deliver phases
        next_phase: The next phase in sequence (None for DELIVER)
        order: Numeric ordering for sorting (0-3)
    """

    DISCOVER = "discover"
    DEFINE = "define"
    DESIGN = "design"
    DELIVER = "deliver"

    @property
    def is_divergent(self) -> bool:
        """Return True if this is a divergent phase (Discover, Design)."""
        return self in (Phase.DISCOVER, Phase.DESIGN)

    @property
    def is_convergent(self) -> bool:
        """Return True if this is a convergent phase (Define, Deliver)."""
        return self in (Phase.DEFINE, Phase.DELIVER)

    @property
    def next_phase(self) -> Phase | None:
        """Return the next phase in sequence, or None if this is DELIVER."""
        sequence = {
            Phase.DISCOVER: Phase.DEFINE,
            Phase.DEFINE: Phase.DESIGN,
            Phase.DESIGN: Phase.DELIVER,
            Phase.DELIVER: None,
        }
        return sequence[self]

    @property
    def order(self) -> int:
        """Return numeric order for sorting (0-3)."""
        ordering = {
            Phase.DISCOVER: 0,
            Phase.DEFINE: 1,
            Phase.DESIGN: 2,
            Phase.DELIVER: 3,
        }
        return ordering[self]


# =============================================================================
# Data Models (frozen for immutability)
# =============================================================================


@dataclass(frozen=True, slots=True)
class PhaseResult:
    """Result of executing a single phase.

    Attributes:
        phase: The phase that was executed.
        success: Whether the phase completed successfully.
        output: Phase-specific output data.
        events: Events emitted during phase execution.
        error_message: Error message if phase failed.
    """

    phase: Phase
    success: bool
    output: dict[str, Any]
    events: list[BaseEvent]
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class PhaseContext:
    """Context for phase execution.

    Contains all state needed to execute a phase, including results
    from previous phases in the current cycle.

    Attributes:
        execution_id: Unique identifier for this execution.
        seed_id: Identifier of the seed being executed.
        current_ac: The acceptance criterion being worked on.
        phase: The current phase being executed.
        iteration: Current iteration number.
        previous_results: Results from phases completed so far in this cycle.
            Note: While the dataclass is frozen, the dict contents are mutable.
            Callers should not modify this dict after construction.
    """

    execution_id: str
    seed_id: str
    current_ac: str
    phase: Phase
    iteration: int
    previous_results: dict[Phase, PhaseResult] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CycleResult:
    """Result of a complete Double Diamond cycle.

    Aggregates results from all four phases.

    Attributes:
        execution_id: Unique identifier for this execution.
        seed_id: Identifier of the seed being executed.
        current_ac: The acceptance criterion that was worked on.
        success: Whether the full cycle completed successfully.
        phase_results: Results from each phase.
        events: All events emitted during the cycle (including cycle-level events).
        is_decomposed: Whether this AC was decomposed into children.
        child_results: Results from child AC cycles (if decomposed).
        depth: Depth at which this cycle was executed.
    """

    execution_id: str
    seed_id: str
    current_ac: str
    success: bool
    phase_results: dict[Phase, PhaseResult]
    events: list[BaseEvent]
    is_decomposed: bool = False
    child_results: tuple[CycleResult, ...] = field(default_factory=tuple)
    depth: int = 0

    @property
    def final_output(self) -> dict[str, Any]:
        """Return the output from the DELIVER phase (final result)."""
        if Phase.DELIVER in self.phase_results:
            return self.phase_results[Phase.DELIVER].output
        return {}

    @property
    def all_events(self) -> list[BaseEvent]:
        """Return all events including from child cycles."""
        events = list(self.events)
        for child in self.child_results:
            events.extend(child.all_events)
        return events


# =============================================================================
# Errors
# =============================================================================


class ExecutionError(OuroborosError):
    """Error during Double Diamond execution.

    Attributes:
        phase: The phase that failed.
        attempt: The retry attempt number.
    """

    def __init__(
        self,
        message: str,
        *,
        phase: Phase | None = None,
        attempt: int = 0,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details)
        self.phase = phase
        self.attempt = attempt


# =============================================================================
# DoubleDiamond Orchestrator
# =============================================================================


class DoubleDiamond:
    """Orchestrator for the Double Diamond execution cycle.

    Manages the four-phase cycle with retry logic and event emission.

    Attributes:
        llm_adapter: LLM adapter for calling language models.
        default_model: Default model for LLM calls (can be overridden per-phase).
        temperature: Temperature setting for LLM calls.
        max_tokens: Maximum tokens for LLM responses.
        max_retries: Maximum retry attempts per phase (default: 3).
        base_delay: Base delay in seconds for exponential backoff (default: 2.0).
    """

    # Default model - can be overridden via __init__ for PAL router integration
    DEFAULT_MODEL = get_double_diamond_model()
    DEFAULT_TEMPERATURE = 0.7
    DEFAULT_MAX_TOKENS = 4096

    def __init__(
        self,
        llm_adapter: LLMAdapter,
        *,
        default_model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_retries: int = 3,
        base_delay: float = 2.0,
        enable_stagnation_detection: bool = True,
    ) -> None:
        """Initialize DoubleDiamond.

        Args:
            llm_adapter: LLM adapter for calling language models.
            default_model: Model identifier for LLM calls. Defaults to Gemini Flash.
                          Can be overridden for PAL router tier integration.
            temperature: Temperature for LLM sampling. Defaults to 0.7.
            max_tokens: Maximum tokens for LLM responses. Defaults to 4096.
            max_retries: Maximum retry attempts per phase.
            base_delay: Base delay in seconds for exponential backoff.
            enable_stagnation_detection: Enable stagnation pattern detection.
        """
        self._llm_adapter = llm_adapter
        self._default_model_is_explicit = default_model is not None
        self._default_model = default_model or get_double_diamond_model()
        self._temperature = temperature if temperature is not None else self.DEFAULT_TEMPERATURE
        self._max_tokens = max_tokens if max_tokens is not None else self.DEFAULT_MAX_TOKENS
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._enable_stagnation_detection = enable_stagnation_detection
        self._stagnation_detector = StagnationDetector() if enable_stagnation_detection else None

        # Execution history for stagnation detection (per execution_id)
        # Mutable state: tracks recent outputs/errors for pattern detection
        self._execution_histories: dict[str, dict[str, list]] = {}

    def _get_execution_history(self, execution_id: str) -> dict[str, list]:
        """Get or create execution history for stagnation detection.

        Args:
            execution_id: Execution identifier.

        Returns:
            Mutable dict with 'outputs', 'errors', 'drifts' lists.
        """
        if execution_id not in self._execution_histories:
            self._execution_histories[execution_id] = {
                "outputs": [],
                "errors": [],
                "drifts": [],
            }
        return self._execution_histories[execution_id]

    def _record_cycle_output(
        self,
        execution_id: str,
        phase_results: dict[Phase, PhaseResult],
        error_message: str | None = None,
    ) -> None:
        """Record cycle output for stagnation detection.

        Args:
            execution_id: Execution identifier.
            phase_results: Results from completed phases.
            error_message: Error message if cycle failed.
        """
        history = self._get_execution_history(execution_id)

        # Extract output from DELIVER phase (or last completed phase)
        output_text = ""
        for phase in [Phase.DELIVER, Phase.DESIGN, Phase.DEFINE, Phase.DISCOVER]:
            if phase in phase_results:
                output_key = PHASE_PROMPTS[phase.value]["output_key"]
                output_text = str(phase_results[phase].output.get(output_key, ""))
                break

        history["outputs"].append(output_text)

        # Keep only last 10 outputs (sliding window)
        if len(history["outputs"]) > 10:
            history["outputs"] = history["outputs"][-10:]

        if error_message:
            history["errors"].append(error_message)
            if len(history["errors"]) > 10:
                history["errors"] = history["errors"][-10:]

    def _check_stagnation(
        self,
        execution_id: str,
        seed_id: str,
        iteration: int,
    ) -> list[BaseEvent]:
        """Check for stagnation patterns and emit events.

        Args:
            execution_id: Execution identifier.
            seed_id: Seed identifier.
            iteration: Current iteration number.

        Returns:
            List of stagnation events (empty if none detected).
        """
        if not self._enable_stagnation_detection or not self._stagnation_detector:
            return []

        history_data = self._get_execution_history(execution_id)

        # Build ExecutionHistory from tracked data
        exec_history = ExecutionHistory.from_lists(
            phase_outputs=history_data["outputs"],
            error_signatures=history_data["errors"],
            drift_scores=history_data["drifts"],
            iteration=iteration,
        )

        # Run detection
        result = self._stagnation_detector.detect(exec_history)

        if result.is_err:
            log.warning(
                "execution.stagnation.detection_failed",
                execution_id=execution_id,
            )
            return []

        # Create events for detected patterns
        events: list[BaseEvent] = []
        for detection in result.value:
            if detection.detected:
                event = create_stagnation_event(
                    detection=detection,
                    execution_id=execution_id,
                    seed_id=seed_id,
                    iteration=iteration,
                )
                events.append(event)

                log.warning(
                    "execution.stagnation.pattern_detected",
                    execution_id=execution_id,
                    pattern=detection.pattern.value,
                    confidence=detection.confidence,
                    iteration=iteration,
                )

        return events

    def clear_execution_history(self, execution_id: str) -> None:
        """Clear execution history for an execution.

        Call this when execution completes successfully or is abandoned.

        Args:
            execution_id: Execution identifier to clear.
        """
        if execution_id in self._execution_histories:
            del self._execution_histories[execution_id]

    def _calculate_backoff(self, attempt: int) -> float:
        """Calculate exponential backoff delay.

        Args:
            attempt: The current attempt number (0-indexed).

        Returns:
            Delay in seconds (base_delay * 2^attempt).
        """
        return float(self._base_delay * (2**attempt))

    def _emit_event(
        self,
        event_type: str,
        execution_id: str,
        seed_id: str,
        data: dict[str, Any] | None = None,
    ) -> BaseEvent:
        """Emit an execution-related event.

        Args:
            event_type: Event type (e.g., "execution.cycle.started").
            execution_id: Execution identifier.
            seed_id: Seed identifier.
            data: Additional event data.

        Returns:
            The created event.
        """
        event_data = {"seed_id": seed_id, **(data or {})}
        return BaseEvent(
            type=event_type,
            aggregate_type="execution",
            aggregate_id=execution_id,
            data=event_data,
        )

    def _emit_phase_event(
        self,
        event_type: str,
        phase: Phase,
        execution_id: str,
        seed_id: str,
        data: dict[str, Any] | None = None,
    ) -> BaseEvent:
        """Emit a phase-related event.

        Args:
            event_type: Event type (e.g., "execution.phase.started").
            phase: The current phase.
            execution_id: Execution identifier.
            seed_id: Seed identifier.
            data: Additional event data.

        Returns:
            The created event.
        """
        event_data = {"phase": phase.value, "seed_id": seed_id, **(data or {})}
        return BaseEvent(
            type=event_type,
            aggregate_type="execution",
            aggregate_id=execution_id,
            data=event_data,
        )

    async def _execute_phase_with_retry(
        self,
        ctx: PhaseContext,
        phase_fn: Any,
    ) -> Result[PhaseResult, ExecutionError]:
        """Execute a phase with retry logic.

        Args:
            ctx: Phase context.
            phase_fn: The async phase function to execute.

        Returns:
            Result containing PhaseResult on success or ExecutionError on failure.
        """
        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            if attempt > 0:
                delay = self._calculate_backoff(attempt - 1)
                log.info(
                    "execution.phase.retry",
                    phase=ctx.phase.value,
                    attempt=attempt + 1,
                    delay_seconds=delay,
                    execution_id=ctx.execution_id,
                )
                await asyncio.sleep(delay)

            try:
                result: Result[PhaseResult, ProviderError] = await phase_fn(ctx)
                if result.is_ok:
                    return Result.ok(result.value)
                # LLM error - will retry
                last_error = result.error
                log.warning(
                    "execution.phase.failed",
                    phase=ctx.phase.value,
                    attempt=attempt + 1,
                    max_retries=self._max_retries,
                    error=str(last_error),
                    execution_id=ctx.execution_id,
                )
            except Exception as e:
                last_error = e
                log.exception(
                    "execution.phase.exception",
                    phase=ctx.phase.value,
                    attempt=attempt + 1,
                    execution_id=ctx.execution_id,
                )

        # All retries exhausted
        error_msg = f"Phase {ctx.phase.value} failed after {self._max_retries} attempts"
        log.error(
            "execution.phase.failed.max_retries",
            phase=ctx.phase.value,
            max_retries=self._max_retries,
            last_error=str(last_error),
            execution_id=ctx.execution_id,
        )

        return Result.err(
            ExecutionError(
                error_msg,
                phase=ctx.phase,
                attempt=self._max_retries,
                details={"last_error": str(last_error)},
            )
        )

    async def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> Result[str, ProviderError]:
        """Call LLM with configured settings.

        Args:
            system_prompt: System prompt for the LLM.
            user_prompt: User prompt for the LLM.

        Returns:
            Result containing LLM response content or ProviderError.
        """
        from ouroboros.providers.base import CompletionConfig, Message, MessageRole

        messages = [
            Message(role=MessageRole.SYSTEM, content=system_prompt),
            Message(role=MessageRole.USER, content=user_prompt),
        ]

        config = CompletionConfig(
            model=self._default_model,
            role="double_diamond",
            model_is_explicit=self._default_model_is_explicit,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )

        result = await self._llm_adapter.complete(messages, config)
        if result.is_ok:
            return Result.ok(result.value.content)
        return Result.err(result.error)

    async def _execute_phase(
        self,
        ctx: PhaseContext,
        phase: Phase,
    ) -> Result[PhaseResult, ExecutionError]:
        """Execute a single phase using the template pattern.

        This method eliminates code duplication by using phase-specific
        prompts from PHASE_PROMPTS configuration.

        Args:
            ctx: Phase context with execution state.
            phase: The phase to execute.

        Returns:
            Result containing PhaseResult on success or ExecutionError on failure.
        """
        log.info(
            "execution.phase.started",
            phase=phase.value,
            execution_id=ctx.execution_id,
            seed_id=ctx.seed_id,
            iteration=ctx.iteration,
        )

        prompts = PHASE_PROMPTS[phase.value]

        async def _execute(c: PhaseContext) -> Result[PhaseResult, ProviderError]:
            # Get previous phase output if needed
            previous_output = ""
            if "previous_phase" in prompts:
                prev_phase = Phase(prompts["previous_phase"])
                if prev_phase in c.previous_results:
                    previous_output = str(c.previous_results[prev_phase].output)

            # Format user prompt with context
            user_prompt = prompts["user_template"].format(
                current_ac=c.current_ac,
                execution_id=c.execution_id,
                iteration=c.iteration,
                previous_output=previous_output,
            )

            llm_result = await self._call_llm(prompts["system"], user_prompt)
            if llm_result.is_err:
                return Result.err(llm_result.error)

            event = self._emit_phase_event(
                "execution.phase.completed",
                phase,
                c.execution_id,
                c.seed_id,
                {prompts["event_data_key"]: True},
            )

            return Result.ok(
                PhaseResult(
                    phase=phase,
                    success=True,
                    output={prompts["output_key"]: llm_result.value},
                    events=[event],
                )
            )

        return await self._execute_phase_with_retry(ctx, _execute)

    async def discover(self, ctx: PhaseContext) -> Result[PhaseResult, ExecutionError]:
        """Execute the Discover phase (diverge).

        Explores the problem space and gathers insights.

        Args:
            ctx: Phase context with execution state.

        Returns:
            Result containing PhaseResult on success or ExecutionError on failure.
        """
        return await self._execute_phase(ctx, Phase.DISCOVER)

    async def define(self, ctx: PhaseContext) -> Result[PhaseResult, ExecutionError]:
        """Execute the Define phase (converge).

        Converges on approach, applying ontology filter.

        Args:
            ctx: Phase context with execution state.

        Returns:
            Result containing PhaseResult on success or ExecutionError on failure.
        """
        return await self._execute_phase(ctx, Phase.DEFINE)

    async def design(self, ctx: PhaseContext) -> Result[PhaseResult, ExecutionError]:
        """Execute the Design phase (diverge).

        Creates solution options.

        Args:
            ctx: Phase context with execution state.

        Returns:
            Result containing PhaseResult on success or ExecutionError on failure.
        """
        return await self._execute_phase(ctx, Phase.DESIGN)

    async def deliver(self, ctx: PhaseContext) -> Result[PhaseResult, ExecutionError]:
        """Execute the Deliver phase (converge).

        Implements and validates the solution.

        Args:
            ctx: Phase context with execution state.

        Returns:
            Result containing PhaseResult on success or ExecutionError on failure.
        """
        return await self._execute_phase(ctx, Phase.DELIVER)

    async def run_cycle(
        self,
        execution_id: str,
        seed_id: str,
        current_ac: str,
        iteration: int,
    ) -> Result[CycleResult, ExecutionError]:
        """Run a complete Double Diamond cycle.

        Executes all four phases in order: Discover → Define → Design → Deliver.
        Emits cycle-level events for full event sourcing traceability.

        Args:
            execution_id: Unique identifier for this execution.
            seed_id: Identifier of the seed being executed.
            current_ac: The acceptance criterion being worked on.
            iteration: Current iteration number.

        Returns:
            Result containing CycleResult on success or ExecutionError on failure.
        """
        log.info(
            "execution.cycle.started",
            execution_id=execution_id,
            seed_id=seed_id,
            iteration=iteration,
        )

        # Emit cycle started event
        cycle_started_event = self._emit_event(
            "execution.cycle.started",
            execution_id,
            seed_id,
            {"iteration": iteration, "current_ac": current_ac},
        )

        phase_results: dict[Phase, PhaseResult] = {}
        all_events: list[BaseEvent] = [cycle_started_event]

        # Execute phases in order
        phase_methods = [
            (Phase.DISCOVER, self.discover),
            (Phase.DEFINE, self.define),
            (Phase.DESIGN, self.design),
            (Phase.DELIVER, self.deliver),
        ]

        for phase, method in phase_methods:
            ctx = PhaseContext(
                execution_id=execution_id,
                seed_id=seed_id,
                current_ac=current_ac,
                phase=phase,
                iteration=iteration,
                previous_results=dict(phase_results),  # Copy to avoid mutation
            )

            log.info(
                "execution.phase.transition",
                from_phase=list(phase_results.keys())[-1].value if phase_results else None,
                to_phase=phase.value,
                execution_id=execution_id,
            )

            result = await method(ctx)

            if result.is_err:
                # Record error for stagnation detection
                self._record_cycle_output(
                    execution_id,
                    phase_results,
                    error_message=str(result.error),
                )

                # Check for stagnation patterns even on failure
                stagnation_events = self._check_stagnation(execution_id, seed_id, iteration)
                all_events.extend(stagnation_events)

                # Phase failed - emit cycle failed event and return error
                cycle_failed_event = self._emit_event(
                    "execution.cycle.failed",
                    execution_id,
                    seed_id,
                    {
                        "iteration": iteration,
                        "failed_phase": phase.value,
                        "error": str(result.error),
                        "stagnation_detected": len(stagnation_events) > 0,
                    },
                )
                all_events.append(cycle_failed_event)

                log.error(
                    "execution.cycle.failed",
                    failed_phase=phase.value,
                    execution_id=execution_id,
                    error=str(result.error),
                    stagnation_patterns_detected=len(stagnation_events),
                )
                return Result.err(result.error)

            phase_result = result.value
            phase_results[phase] = phase_result
            all_events.extend(phase_result.events)

        # Record output for stagnation detection
        self._record_cycle_output(execution_id, phase_results)

        # Check for stagnation patterns (Story 4.1)
        stagnation_events = self._check_stagnation(execution_id, seed_id, iteration)
        all_events.extend(stagnation_events)

        # Emit cycle completed event
        cycle_completed_event = self._emit_event(
            "execution.cycle.completed",
            execution_id,
            seed_id,
            {
                "iteration": iteration,
                "phases_completed": len(phase_results),
                "stagnation_detected": len(stagnation_events) > 0,
            },
        )
        all_events.append(cycle_completed_event)

        log.info(
            "execution.cycle.completed",
            execution_id=execution_id,
            seed_id=seed_id,
            iteration=iteration,
            phases_completed=len(phase_results),
            stagnation_patterns_detected=len(stagnation_events),
        )

        return Result.ok(
            CycleResult(
                execution_id=execution_id,
                seed_id=seed_id,
                current_ac=current_ac,
                success=True,
                phase_results=phase_results,
                events=all_events,
            )
        )
