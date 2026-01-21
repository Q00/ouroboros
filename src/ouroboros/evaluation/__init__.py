"""Three-stage evaluation pipeline for Ouroboros.

This module provides the evaluation infrastructure for verifying outputs
through three progressive stages:

1. Stage 1 - Mechanical Verification ($0): Lint, build, test, static analysis
2. Stage 2 - Semantic Evaluation (Standard tier): AC compliance, goal alignment
3. Stage 3 - Multi-Model Consensus (Frontier tier): 3-model voting

Classes:
    CheckResult: Result of a single mechanical check
    CheckType: Types of mechanical checks
    MechanicalResult: Aggregated Stage 1 results
    SemanticResult: Stage 2 LLM evaluation results
    Vote: Single model vote in consensus
    ConsensusResult: Aggregated Stage 3 results
    EvaluationResult: Complete pipeline result
    EvaluationContext: Input context for evaluation
    MechanicalVerifier: Stage 1 checker
    MechanicalConfig: Stage 1 configuration
    SemanticEvaluator: Stage 2 evaluator
    SemanticConfig: Stage 2 configuration
    ConsensusEvaluator: Stage 3 consensus builder
    ConsensusConfig: Stage 3 configuration
    ConsensusTrigger: Trigger matrix implementation
    TriggerType: Types of consensus triggers
    TriggerContext: Context for trigger evaluation
    TriggerResult: Result of trigger evaluation
    TriggerConfig: Trigger thresholds
    EvaluationPipeline: Full pipeline orchestrator
    PipelineConfig: Pipeline configuration
"""

from ouroboros.evaluation.consensus import (
    DEFAULT_CONSENSUS_MODELS,
    ConsensusConfig,
    ConsensusEvaluator,
    run_consensus_evaluation,
)
from ouroboros.evaluation.mechanical import (
    MechanicalConfig,
    MechanicalVerifier,
    run_mechanical_verification,
)
from ouroboros.evaluation.models import (
    CheckResult,
    CheckType,
    ConsensusResult,
    EvaluationContext,
    EvaluationResult,
    MechanicalResult,
    SemanticResult,
    Vote,
)
from ouroboros.evaluation.pipeline import (
    EvaluationPipeline,
    PipelineConfig,
    run_evaluation_pipeline,
)
from ouroboros.evaluation.semantic import (
    DEFAULT_SEMANTIC_MODEL,
    SemanticConfig,
    SemanticEvaluator,
    run_semantic_evaluation,
)
from ouroboros.evaluation.trigger import (
    ConsensusTrigger,
    TriggerConfig,
    TriggerContext,
    TriggerResult,
    TriggerType,
    check_consensus_trigger,
)

__all__ = [
    # Models
    "CheckResult",
    "CheckType",
    "ConsensusResult",
    "EvaluationContext",
    "EvaluationResult",
    "MechanicalResult",
    "SemanticResult",
    "Vote",
    # Stage 1
    "MechanicalConfig",
    "MechanicalVerifier",
    "run_mechanical_verification",
    # Stage 2
    "DEFAULT_SEMANTIC_MODEL",
    "SemanticConfig",
    "SemanticEvaluator",
    "run_semantic_evaluation",
    # Stage 3
    "DEFAULT_CONSENSUS_MODELS",
    "ConsensusConfig",
    "ConsensusEvaluator",
    "run_consensus_evaluation",
    # Trigger
    "ConsensusTrigger",
    "TriggerConfig",
    "TriggerContext",
    "TriggerResult",
    "TriggerType",
    "check_consensus_trigger",
    # Pipeline
    "EvaluationPipeline",
    "PipelineConfig",
    "run_evaluation_pipeline",
]
