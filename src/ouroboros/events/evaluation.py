"""Event factories for the evaluation pipeline.

This module provides factory functions for creating evaluation-related events.
All events follow the dot.notation.past_tense naming convention.

Event Types:
    evaluation.stage1.started - Mechanical verification started
    evaluation.stage1.completed - Mechanical verification completed
    evaluation.stage2.started - Semantic evaluation started
    evaluation.stage2.completed - Semantic evaluation completed
    evaluation.stage3.started - Consensus evaluation started
    evaluation.stage3.completed - Consensus evaluation completed
    evaluation.consensus.triggered - Consensus trigger activated
    evaluation.pipeline.completed - Full pipeline completed
"""

from typing import Any

from ouroboros.events.base import BaseEvent


def create_stage1_started_event(
    execution_id: str,
    checks_to_run: list[str],
) -> BaseEvent:
    """Create event for Stage 1 mechanical verification starting.

    Args:
        execution_id: Unique execution identifier
        checks_to_run: List of check types to execute

    Returns:
        BaseEvent for stage1 start
    """
    return BaseEvent(
        type="evaluation.stage1.started",
        aggregate_type="evaluation",
        aggregate_id=execution_id,
        data={
            "checks_to_run": checks_to_run,
        },
    )


def create_stage1_completed_event(
    execution_id: str,
    passed: bool,
    checks: list[dict[str, Any]],
    coverage_score: float | None,
) -> BaseEvent:
    """Create event for Stage 1 mechanical verification completion.

    Args:
        execution_id: Unique execution identifier
        passed: Overall pass/fail status
        checks: List of check results as dicts
        coverage_score: Test coverage score if measured

    Returns:
        BaseEvent for stage1 completion
    """
    return BaseEvent(
        type="evaluation.stage1.completed",
        aggregate_type="evaluation",
        aggregate_id=execution_id,
        data={
            "passed": passed,
            "checks": checks,
            "coverage_score": coverage_score,
            "failed_count": sum(1 for c in checks if not c.get("passed", True)),
        },
    )


def create_stage2_started_event(
    execution_id: str,
    model: str,
    current_ac: str,
) -> BaseEvent:
    """Create event for Stage 2 semantic evaluation starting.

    Args:
        execution_id: Unique execution identifier
        model: LLM model being used
        current_ac: Acceptance criterion being evaluated

    Returns:
        BaseEvent for stage2 start
    """
    return BaseEvent(
        type="evaluation.stage2.started",
        aggregate_type="evaluation",
        aggregate_id=execution_id,
        data={
            "model": model,
            "current_ac": current_ac,
        },
    )


def create_stage2_completed_event(
    execution_id: str,
    score: float,
    ac_compliance: bool,
    goal_alignment: float,
    drift_score: float,
    uncertainty: float,
) -> BaseEvent:
    """Create event for Stage 2 semantic evaluation completion.

    Args:
        execution_id: Unique execution identifier
        score: Overall evaluation score
        ac_compliance: Whether AC is met
        goal_alignment: Goal alignment score
        drift_score: Drift from seed
        uncertainty: Model uncertainty

    Returns:
        BaseEvent for stage2 completion
    """
    return BaseEvent(
        type="evaluation.stage2.completed",
        aggregate_type="evaluation",
        aggregate_id=execution_id,
        data={
            "score": score,
            "ac_compliance": ac_compliance,
            "goal_alignment": goal_alignment,
            "drift_score": drift_score,
            "uncertainty": uncertainty,
        },
    )


def create_stage3_started_event(
    execution_id: str,
    models: list[str],
    trigger_reason: str,
) -> BaseEvent:
    """Create event for Stage 3 consensus evaluation starting.

    Args:
        execution_id: Unique execution identifier
        models: List of models participating in consensus
        trigger_reason: Reason consensus was triggered

    Returns:
        BaseEvent for stage3 start
    """
    return BaseEvent(
        type="evaluation.stage3.started",
        aggregate_type="evaluation",
        aggregate_id=execution_id,
        data={
            "models": models,
            "trigger_reason": trigger_reason,
        },
    )


def create_stage3_completed_event(
    execution_id: str,
    approved: bool,
    votes: list[dict[str, Any]],
    majority_ratio: float,
    disagreements: list[str],
) -> BaseEvent:
    """Create event for Stage 3 consensus evaluation completion.

    Args:
        execution_id: Unique execution identifier
        approved: Whether consensus approved
        votes: List of vote dicts
        majority_ratio: Approval ratio
        disagreements: List of dissenting reasons

    Returns:
        BaseEvent for stage3 completion
    """
    return BaseEvent(
        type="evaluation.stage3.completed",
        aggregate_type="evaluation",
        aggregate_id=execution_id,
        data={
            "approved": approved,
            "votes": votes,
            "majority_ratio": majority_ratio,
            "disagreements": disagreements,
            "total_votes": len(votes),
            "approving_votes": sum(1 for v in votes if v.get("approved", False)),
        },
    )


def create_consensus_triggered_event(
    execution_id: str,
    trigger_type: str,
    trigger_details: dict[str, Any],
) -> BaseEvent:
    """Create event when consensus is triggered by trigger matrix.

    Args:
        execution_id: Unique execution identifier
        trigger_type: Type of trigger activated
        trigger_details: Additional context about trigger

    Returns:
        BaseEvent for consensus trigger
    """
    return BaseEvent(
        type="evaluation.consensus.triggered",
        aggregate_type="evaluation",
        aggregate_id=execution_id,
        data={
            "trigger_type": trigger_type,
            "trigger_details": trigger_details,
        },
    )


def create_pipeline_completed_event(
    execution_id: str,
    final_approved: bool,
    highest_stage: int,
    failure_reason: str | None,
) -> BaseEvent:
    """Create event for full evaluation pipeline completion.

    Args:
        execution_id: Unique execution identifier
        final_approved: Overall approval status
        highest_stage: Highest stage number completed
        failure_reason: Reason for failure if not approved

    Returns:
        BaseEvent for pipeline completion
    """
    return BaseEvent(
        type="evaluation.pipeline.completed",
        aggregate_type="evaluation",
        aggregate_id=execution_id,
        data={
            "final_approved": final_approved,
            "highest_stage": highest_stage,
            "failure_reason": failure_reason,
        },
    )
