"""Gemini 3 HOTL (Human-on-the-Loop) Convergence Accelerator.

NOTE: Core modules have been migrated to ouroboros.dashboard.
This package re-exports from dashboard for backward compatibility.

For new code, import directly from ouroboros.dashboard:
    from ouroboros.dashboard import (
        PatternAnalyzer,
        DependencyPredictor,
        EnhancedDevilAdvocate,
        IterationData,
        IterationOutcome,
    )

Run Demo:
    python -m ouroboros.gemini3.demo_runner --full-demo

Run Streamlit Dashboard:
    streamlit run src/ouroboros/dashboard/streamlit_app.py
"""

# Re-export from dashboard for backward compatibility
from ouroboros.dashboard import (
    # Core models
    IterationData,
    IterationOutcome,
    ConvergenceState,
    ConvergenceCurvePoint,
    # Pattern Analysis
    PatternAnalyzer,
    FailurePattern,
    PatternCategory,
    PatternSeverity,
    PatternCluster,
    PatternNetwork,
    # Dependency Prediction
    DependencyPredictor,
    ACDependency,
    BlockingPrediction,
    DependencyType,
    DependencyStrength,
    DependencyTreeNode,
    # Enhanced Devil's Advocate
    EnhancedDevilAdvocate,
    DeepChallenge,
    ChallengeType,
    ChallengeIntensity,
    ChallengeResult,
)

__all__ = [
    # Core models
    "IterationData",
    "IterationOutcome",
    "ConvergenceState",
    "ConvergenceCurvePoint",
    # Pattern Analysis
    "PatternAnalyzer",
    "FailurePattern",
    "PatternCategory",
    "PatternSeverity",
    "PatternCluster",
    "PatternNetwork",
    # Dependency Prediction
    "DependencyPredictor",
    "ACDependency",
    "BlockingPrediction",
    "DependencyType",
    "DependencyStrength",
    "DependencyTreeNode",
    # Enhanced Devil's Advocate
    "EnhancedDevilAdvocate",
    "DeepChallenge",
    "ChallengeType",
    "ChallengeIntensity",
    "ChallengeResult",
]
