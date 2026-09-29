"""Evaluation Pipeline Orchestrator.

Orchestrates the three-stage evaluation pipeline:
1. Stage 1: Mechanical Verification ($0)
2. Stage 2: Semantic Evaluation (Standard tier)
3. Stage 3: Multi-Model Consensus (Frontier tier, if triggered)

The pipeline respects configuration flags and trigger conditions.

Acceptance authority: only executed Stage 1 evidence can grant approval.
Stage 2 and Stage 3 are advisory; they can withhold approval and supply
feedback, never grant it. Without executed evidence the result is
unverified (see ``AcceptanceState``).
"""

from dataclasses import dataclass

from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.types import Result
from ouroboros.evaluation.consensus import ConsensusConfig, ConsensusEvaluator
from ouroboros.evaluation.mechanical import (
    MechanicalConfig,
    MechanicalVerifier,
)
from ouroboros.evaluation.models import (
    COMMAND_CHECK_TYPES,
    ConsensusResult,
    EvaluationContext,
    EvaluationResult,
    MechanicalDisposition,
    MechanicalResult,
    SemanticResult,
    build_failure_reason,
    decide_final_approval,
    derive_acceptance_state,
)
from ouroboros.evaluation.semantic import SemanticConfig, SemanticEvaluator
from ouroboros.evaluation.trigger import (
    ConsensusTrigger,
    TriggerConfig,
    TriggerContext,
)
from ouroboros.events.base import BaseEvent
from ouroboros.events.evaluation import create_pipeline_completed_event
from ouroboros.providers.base import LLMAdapter


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Configuration for the evaluation pipeline.

    Attributes:
        stage1_enabled: Run mechanical verification
        stage2_enabled: Run semantic evaluation
        stage3_enabled: Allow consensus if triggered
        mechanical: Stage 1 configuration
        semantic: Stage 2 configuration
        consensus: Stage 3 configuration
        trigger: Trigger matrix configuration
    """

    stage1_enabled: bool = True
    stage2_enabled: bool = True
    stage3_enabled: bool = True
    mechanical: MechanicalConfig | None = None
    semantic: SemanticConfig | None = None
    consensus: ConsensusConfig | None = None
    trigger: TriggerConfig | None = None


class EvaluationPipeline:
    """Orchestrates the three-stage evaluation pipeline.

    Runs stages sequentially, respecting configuration and triggers.
    Stage 3 is only run if trigger conditions are met.

    Example:
        pipeline = EvaluationPipeline(llm_adapter, config)
        result = await pipeline.evaluate(context)
    """

    def __init__(
        self,
        llm_adapter: LLMAdapter,
        config: PipelineConfig | None = None,
    ) -> None:
        """Initialize pipeline.

        Args:
            llm_adapter: LLM adapter for semantic and consensus
            config: Pipeline configuration
        """
        self._llm = llm_adapter
        self._config = config or PipelineConfig()

        # Initialize stage evaluators
        self._mechanical = MechanicalVerifier(self._config.mechanical)
        self._semantic = SemanticEvaluator(llm_adapter, self._config.semantic)
        self._consensus = ConsensusEvaluator(llm_adapter, self._config.consensus)
        self._trigger = ConsensusTrigger(self._config.trigger)

    async def evaluate(
        self,
        context: EvaluationContext,
        trigger_context: TriggerContext | None = None,
        *,
        stage1_result: MechanicalResult | None = None,
    ) -> Result[EvaluationResult, ProviderError | ValidationError]:
        """Run the evaluation pipeline.

        Args:
            context: Evaluation context with artifact
            trigger_context: Optional pre-populated trigger context
            stage1_result: Pre-computed Stage 1 result.  When provided,
                Stage 1 mechanical verification is skipped and this result
                is reused.  This allows multi-AC callers to run
                lint/build/test once and share the outcome across parallel
                semantic evaluations.

                INVARIANT: Stage 1 checks (lint, build, test, static
                analysis, coverage) must be AC-agnostic — they verify
                project-wide code quality, not AC-specific behavior.  The
                multi-AC checklist path (``_handle_multi_ac`` in
                ``EvaluateHandler``, introduced in #385) relies on this
                invariant to run Stage 1 exactly once across all ACs and
                share the result via this parameter.

                If future Stage 1 additions become AC-specific (e.g.
                AC-tagged test filtering or per-AC coverage thresholds),
                this dedup becomes incorrect and the multi-AC caller must
                be updated to run Stage 1 per AC again.

        Returns:
            Result containing EvaluationResult or error
        """
        events: list[BaseEvent] = []
        stage2_result = None
        stage3_result = None

        # Stage 1: Mechanical Verification
        # When a pre-computed result is injected, skip re-running the
        # AC-agnostic lint/build/test checks.
        if stage1_result is None and self._config.stage1_enabled:
            result = await self._mechanical.verify(
                context.execution_id,
                checks=list(COMMAND_CHECK_TYPES),
            )
            if result.is_err:
                return Result.err(result.error)

            stage1_result, stage1_events = result.value
            events.extend(stage1_events)

        # What the controller already ran for this criterion (its recorded
        # check package decision) is executed evidence for this criterion
        # only; the one classifier decides over it and the command checks.
        if context.recorded_checks:
            base = stage1_result or MechanicalResult(passed=True, checks=())
            stage1_result = base.with_recorded(context.recorded_checks)

        # Only an executed failure stops here. A result with no executed
        # evidence continues so the advisory review can supply feedback;
        # the acceptance gate still refuses to approve it.
        if (
            stage1_result is not None
            and stage1_result.disposition is MechanicalDisposition.EXECUTED_FAIL
        ):
            return self._build_result(
                context.execution_id,
                events,
                stage1_result=stage1_result,
            )

        # Stage 2: Semantic Evaluation (advisory). It still runs when Stage 1
        # produced no executed evidence: its review becomes the feedback that
        # accompanies an unverified result, not a verdict.
        if self._config.stage2_enabled:
            result = await self._semantic.evaluate(context)
            if result.is_err:
                return Result.err(result.error)

            stage2_result, stage2_events = result.value
            events.extend(stage2_events)

            # Stage 2 withheld on compliance. trigger_consensus still asks
            # Stage 3 for a second opinion that is reported, but a consensus
            # approval cannot lift this block.
            if not stage2_result.ac_compliance and not context.trigger_consensus:
                return self._build_result(
                    context.execution_id,
                    events,
                    stage1_result=stage1_result,
                    stage2_result=stage2_result,
                )

        # Build or enrich trigger context — outside Stage 2 block so that
        # trigger_consensus=True works even when stage2_enabled=False.
        if trigger_context is None:
            trigger_context = TriggerContext(
                execution_id=context.execution_id,
                semantic_result=stage2_result,
                manual_consensus_request=context.trigger_consensus,
            )
        elif context.trigger_consensus and not trigger_context.manual_consensus_request:
            # Caller supplied a TriggerContext (e.g. for drift data) but
            # trigger_consensus was set separately — merge the override.
            trigger_context = TriggerContext(
                execution_id=trigger_context.execution_id,
                seed_modified=trigger_context.seed_modified,
                ontology_changed=trigger_context.ontology_changed,
                goal_reinterpreted=trigger_context.goal_reinterpreted,
                drift_score=trigger_context.drift_score,
                uncertainty_score=trigger_context.uncertainty_score,
                lateral_thinking_adopted=trigger_context.lateral_thinking_adopted,
                semantic_result=trigger_context.semantic_result or stage2_result,
                manual_consensus_request=True,
            )

        # Stage 3: Consensus (if triggered)
        if self._config.stage3_enabled and trigger_context:
            trigger_result = self._trigger.evaluate(trigger_context)
            if trigger_result.is_err:
                return Result.err(trigger_result.error)

            trigger_decision, trigger_events = trigger_result.value
            events.extend(trigger_events)

            if trigger_decision.should_trigger:
                trigger_reason = (
                    trigger_decision.trigger_type.value
                    if trigger_decision.trigger_type
                    else "manual"
                )
                result = await self._consensus.evaluate(context, trigger_reason)
                if result.is_err:
                    return Result.err(result.error)

                stage3_result, stage3_events = result.value
                events.extend(stage3_events)

        return self._build_result(
            context.execution_id,
            events,
            stage1_result=stage1_result,
            stage2_result=stage2_result,
            stage3_result=stage3_result,
        )

    def _build_result(
        self,
        execution_id: str,
        events: list[BaseEvent],
        stage1_result: MechanicalResult | None = None,
        stage2_result: SemanticResult | None = None,
        stage3_result: ConsensusResult | None = None,
    ) -> Result[EvaluationResult, ValidationError]:
        """Build the final evaluation result.

        Approval is decided here and nowhere else, by
        ``decide_final_approval``: executed Stage 1 evidence grants it, and
        any model review that ran (Stage 2 compliance or score, Stage 3
        consensus, the reward-hacking veto) may withhold it. No caller passes
        an approval in, so no branch can mint one from a model verdict.

        Args:
            execution_id: Execution identifier
            events: Collected events
            stage1_result: Stage 1 result if completed
            stage2_result: Stage 2 result if completed
            stage3_result: Stage 3 result if triggered

        Returns:
            Result containing EvaluationResult
        """
        final_approved = decide_final_approval(
            stage1_result=stage1_result,
            stage2_result=stage2_result,
            stage3_result=stage3_result,
        )

        # Calculate highest stage before creating immutable result
        highest_stage = 0
        if stage1_result is not None:
            highest_stage = 1
        if stage2_result is not None:
            highest_stage = 2
        if stage3_result is not None:
            highest_stage = 3

        # Calculate failure reason before creating immutable result.  Shared
        # with ``EvaluationResult.failure_reason`` so the two surfaces cannot
        # drift: this one is built before the result exists, that one after.
        failure_reason = build_failure_reason(
            final_approved=final_approved,
            stage1_result=stage1_result,
            stage2_result=stage2_result,
            stage3_result=stage3_result,
        )

        # Create completion event
        completion_event = create_pipeline_completed_event(
            execution_id=execution_id,
            final_approved=final_approved,
            highest_stage=highest_stage,
            failure_reason=failure_reason,
            acceptance_state=derive_acceptance_state(
                final_approved=final_approved,
                stage1_result=stage1_result,
            ).value,
        )

        # Build complete event list before creating frozen result
        all_events = [*events, completion_event]

        result = EvaluationResult(
            execution_id=execution_id,
            stage1_result=stage1_result,
            stage2_result=stage2_result,
            stage3_result=stage3_result,
            final_approved=final_approved,
            events=all_events,
        )

        return Result.ok(result)


async def run_evaluation_pipeline(
    context: EvaluationContext,
    llm_adapter: LLMAdapter,
    config: PipelineConfig | None = None,
    trigger_context: TriggerContext | None = None,
) -> Result[EvaluationResult, ProviderError | ValidationError]:
    """Convenience function for running the evaluation pipeline.

    Args:
        context: Evaluation context
        llm_adapter: LLM adapter
        config: Optional configuration
        trigger_context: Optional trigger context

    Returns:
        Result with EvaluationResult
    """
    pipeline = EvaluationPipeline(llm_adapter, config)
    return await pipeline.evaluate(context, trigger_context)
