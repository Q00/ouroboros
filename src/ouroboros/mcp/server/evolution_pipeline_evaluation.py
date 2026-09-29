"""Evaluate an evolve generation whose output carries no per-task report.

When the worker output lacks ``### Task N: [COMPLETED|FAILED]`` markers, the
evolve loop cannot take the strict spec-verifier path. This module evaluates
such a generation with the evaluation pipeline under the acceptance rule that
``ouroboros_evaluate`` also follows: executed Stage 1 checks grant approval,
and semantic review is advisory. When nothing executes, the generation is not
approved but unverified, and the semantic review is returned as structured
feedback that Wonder and Reflect render for the next generation.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
from typing import Any

import structlog

from ouroboros.core.lineage import ACResult, EvaluationSummary, FeedbackMetadata
from ouroboros.core.seed import ac_texts
from ouroboros.evaluation.models import (
    AcceptanceState,
    EvaluationResult,
    model_review_withhold_reason,
)

log = structlog.get_logger(__name__)

EVOLVE_STAGE1_ENV = "OUROBOROS_EVOLVE_STAGE1"
_ENV_ENABLED = frozenset({"true", "1"})
_ENV_DISABLED = frozenset({"false", "0"})

_APPROVAL_STATUS: dict[AcceptanceState, str] = {
    AcceptanceState.APPROVED: "approved",
    AcceptanceState.REJECTED: "rejected",
    # No executed verification ran, so no acceptance verdict was minted.
    AcceptanceState.UNVERIFIED: "not_evaluated",
}


def evolve_stage1_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Read ``OUROBOROS_EVOLVE_STAGE1``; Stage 1 is on unless explicitly disabled.

    Stage 1 is the only source of executed evidence on this path, so without
    it a generation can never be approved here. Accepted values are
    ``true``/``1`` and ``false``/``0`` (case-insensitive). Any other value is
    rejected with a warning and the default (enabled) applies; disabling is
    never inferred from a malformed value.
    """
    source = os.environ if environ is None else environ
    raw = source.get(EVOLVE_STAGE1_ENV)
    if raw is None or not raw.strip():
        return True
    value = raw.strip().lower()
    if value in _ENV_ENABLED:
        return True
    if value in _ENV_DISABLED:
        return False
    log.warning(
        "evolution.evaluation.invalid_stage1_env",
        variable=EVOLVE_STAGE1_ENV,
        value=raw,
        accepted=sorted(_ENV_ENABLED | _ENV_DISABLED),
        applied=True,
    )
    return True


async def _project_mechanical_config(
    project_dir: Path,
    llm_adapter: Any,
    detector_backend: str | None,
) -> Any:
    """Resolve Stage 1 commands for ``project_dir`` the way ``ouroboros_evaluate`` does.

    Commands come only from ``.ouroboros/mechanical.toml``. When it is absent
    the existing detector authors it once (a model constructing checks, whose
    commands are validated before they are persisted). A failed detection
    leaves Stage 1 with no configured check, which yields an unverified
    result rather than an approval.
    """
    from ouroboros.evaluation.detector import ensure_mechanical_toml, has_mechanical_toml
    from ouroboros.evaluation.languages import build_mechanical_config

    if not has_mechanical_toml(project_dir):
        try:
            await ensure_mechanical_toml(project_dir, llm_adapter, backend=detector_backend)
        except Exception as exc:  # noqa: BLE001 - detection is best-effort; outcome is unverified
            log.warning(
                "evolution.evaluation.detect_failed",
                project_dir=str(project_dir),
                error=str(exc),
            )
    return build_mechanical_config(project_dir)


def evaluation_summary_from_pipeline_result(
    result: EvaluationResult,
    *,
    stage1_note: str | None = None,
) -> EvaluationSummary:
    """Project a pipeline result into a generation summary that carries feedback.

    Approval is copied from the pipeline, whose single gate already requires
    executed evidence. The semantic review is attached as feedback whether it
    was favorable or not, marked advisory, so the next generation sees what
    the model concluded without that conclusion acting as a verdict.
    """
    state = result.acceptance_state
    stage2 = result.stage2_result
    feedback: list[FeedbackMetadata] = []

    if state is AcceptanceState.UNVERIFIED:
        executed = result.stage1_result.executed_checks if result.stage1_result else ()
        missing = stage1_note or (
            "Stage 1 did not run"
            if result.stage1_result is None
            else "Stage 1 ran no configured check"
        )
        feedback.append(
            FeedbackMetadata(
                code="acceptance_unverified",
                severity="warning",
                message=(
                    f"Not approved: unverified ({missing}). Model review cannot grant "
                    "acceptance; configure executable checks in .ouroboros/mechanical.toml "
                    "so a generation can be verified."
                ),
                source="evaluation_pipeline",
                details={
                    "stage1_ran": result.stage1_result is not None,
                    "executed_checks": len(executed),
                },
            )
        )

    if stage2 is not None:
        withheld = model_review_withhold_reason(stage2_result=stage2, stage3_result=None)
        feedback.append(
            FeedbackMetadata(
                code="semantic_review",
                severity="warning" if withheld is not None else "info",
                message=stage2.reasoning or "Semantic review returned no reasoning.",
                source="semantic_evaluation",
                details={
                    "advisory": True,
                    "withheld": withheld,
                    "ac_compliance": stage2.ac_compliance,
                    "score": stage2.score,
                    "goal_alignment": stage2.goal_alignment,
                    "drift_score": stage2.drift_score,
                    "reward_hacking_risk": stage2.reward_hacking_risk,
                    "evidence": list(stage2.evidence),
                    "questions_used": list(stage2.questions_used),
                },
            )
        )

    return EvaluationSummary(
        final_approved=result.final_approved,
        highest_stage_passed=max(1, result.highest_stage_completed),
        score=stage2.score if stage2 else None,
        drift_score=stage2.drift_score if stage2 else None,
        reward_hacking_risk=stage2.reward_hacking_risk if stage2 else None,
        failure_reason=result.failure_reason,
        feedback_metadata=tuple(feedback),
        execution_completion_status="completed",
        approval_status=_APPROVAL_STATUS[state],
    )


async def evaluate_generation_with_pipeline(
    *,
    seed: Any,
    artifact: str,
    project_dir: str | None,
    llm_adapter: Any,
    semantic_model: str,
    detector_backend: str | None,
    stage1_enabled: bool,
) -> EvaluationSummary:
    """Evaluate one unstructured generation: executed checks grant, review advises."""
    from ouroboros.evaluation import (
        EvaluationContext,
        EvaluationPipeline,
        PipelineConfig,
        SemanticConfig,
    )
    from ouroboros.evaluation.artifact_collector import ArtifactCollector

    acs = getattr(seed, "acceptance_criteria", None)
    if acs:
        current_ac = "\n".join(f"AC {i + 1}: {ac}" for i, ac in enumerate(ac_texts(acs)))
    else:
        current_ac = "Verify execution output meets requirements"

    stage1_note: str | None = None
    mechanical = None
    run_stage1 = stage1_enabled and project_dir is not None
    if not stage1_enabled:
        stage1_note = f"Stage 1 disabled by {EVOLVE_STAGE1_ENV}"
    elif project_dir is None:
        stage1_note = "Stage 1 did not run: project directory could not be resolved"
    else:
        mechanical = await _project_mechanical_config(
            Path(project_dir), llm_adapter, detector_backend
        )

    pipeline = EvaluationPipeline(
        llm_adapter=llm_adapter,
        config=PipelineConfig(
            stage1_enabled=run_stage1,
            stage2_enabled=True,
            stage3_enabled=False,
            mechanical=mechanical,
            semantic=SemanticConfig(model=semantic_model),
        ),
    )

    eval_context = EvaluationContext(
        execution_id=f"eval_{seed.metadata.seed_id}",
        seed_id=seed.metadata.seed_id,
        current_ac=current_ac,
        artifact=artifact,
        artifact_type="code",
        goal=seed.goal,
        constraints=tuple(seed.constraints),
        artifact_bundle=ArtifactCollector().collect(artifact, project_dir),
    )

    eval_result = await pipeline.evaluate(eval_context)
    if eval_result.is_err:
        return EvaluationSummary(
            final_approved=False,
            highest_stage_passed=1,
            score=0.0,
            drift_score=1.0,
            failure_reason=str(eval_result.error),
        )
    return evaluation_summary_from_pipeline_result(eval_result.value, stage1_note=stage1_note)


async def evaluate_criteria_with_pipeline(
    seed: Any,
    indices: tuple[int, ...],
    *,
    artifact: str,
    project_dir: str | None,
    llm_adapter: Any,
    semantic_model: str,
    detector_backend: str | None,
    stage1_enabled: bool,
) -> dict[int, ACResult]:
    """Decide the Seed criteria at ``indices`` one by one, as ``ouroboros_evaluate`` does.

    A criterion no check package and no verifier decided (for example a
    preservation criterion, which has no reproduction check on any base) is
    evaluated by the pipeline: the project's command checks run once in
    ``project_dir`` and are shared, then each criterion gets the advisory
    review, which sees those executed checks. Approval still needs executed
    evidence and the review can only withhold. A criterion the pipeline
    leaves unverified, or could not evaluate, is returned not evaluated.
    """
    from ouroboros.core.seed import AcceptanceCriterionSpec
    from ouroboros.evaluation import (
        EvaluationContext,
        EvaluationPipeline,
        PipelineConfig,
        SemanticConfig,
    )
    from ouroboros.evaluation.artifact_collector import ArtifactCollector

    criteria = tuple(getattr(seed, "acceptance_criteria", ()) or ())
    texts = ac_texts(criteria)
    run_stage1 = stage1_enabled and project_dir is not None
    mechanical = (
        await _project_mechanical_config(Path(project_dir), llm_adapter, detector_backend)
        if run_stage1 and project_dir is not None
        else None
    )
    pipeline = EvaluationPipeline(
        llm_adapter=llm_adapter,
        config=PipelineConfig(
            stage1_enabled=run_stage1,
            stage2_enabled=True,
            stage3_enabled=False,
            mechanical=mechanical,
            semantic=SemanticConfig(model=semantic_model),
        ),
    )
    bundle = ArtifactCollector().collect(artifact, project_dir)
    stage1 = None
    rows: dict[int, ACResult] = {}
    for index in indices:
        if not 0 <= index < len(criteria):
            continue
        criterion = criteria[index]
        context = EvaluationContext(
            execution_id=f"eval_{seed.metadata.seed_id}_ac{index}",
            seed_id=seed.metadata.seed_id,
            current_ac=texts[index],
            current_ac_spec=criterion if isinstance(criterion, AcceptanceCriterionSpec) else None,
            artifact=artifact,
            artifact_type="code",
            goal=seed.goal,
            constraints=tuple(seed.constraints),
            artifact_bundle=bundle,
        )
        outcome = await pipeline.evaluate(context, stage1_result=stage1)
        if outcome.is_err:
            log.warning(
                "evolution.evaluation.criterion_failed", ac_index=index, error=str(outcome.error)
            )
            continue
        result = outcome.value
        if stage1 is None and result.stage1_result is not None:
            stage1 = result.stage1_result.command_checks()
        state = result.acceptance_state
        verdict = {AcceptanceState.APPROVED: "pass", AcceptanceState.REJECTED: "fail"}.get(state)
        stage2 = result.stage2_result
        rows[index] = ACResult(
            ac_index=index,
            ac_content=texts[index],
            semantic_ac_key=getattr(criterion, "semantic_ac_key", None),
            passed=verdict == "pass",
            score=stage2.score if stage2 is not None else None,
            evidence=(
                result.failure_reason
                or (stage2.reasoning if stage2 is not None else "")
                or f"evaluation pipeline: {state.value}"
            ),
            verification_method="evaluation_pipeline",
            ac_verdict_state="evaluated" if verdict is not None else "not_evaluated",
            final_verdict=verdict or "fail",
            rendered_verdict=(verdict or "not_evaluated").upper(),
        )
    return rows


__all__ = [
    "EVOLVE_STAGE1_ENV",
    "evaluate_criteria_with_pipeline",
    "evaluate_generation_with_pipeline",
    "evaluation_summary_from_pipeline_result",
    "evolve_stage1_enabled",
]
