"""Streamlit Dashboard for Ouroboros AI.

This module provides a visual dashboard demonstrating Gemini 3's
1M token context capability for analyzing iteration history.

Key Components:
- GeminiContextAnalyzer: Analyzes 50+ iterations in single API call
- GeminiAPILogger: Logs all Gemini API interactions
- TimelineView: Builds timeline visualization data
- generate_demo_data: Creates complex maze problem iterations

Advanced Analysis (migrated from gemini3/):
- PatternAnalyzer: Detect recurring failure patterns
- DependencyPredictor: Predict AC blocking dependencies
- EnhancedDevilAdvocate: Deep Socratic challenging with 1M context

Usage:
    from ouroboros.dashboard import GeminiContextAnalyzer, generate_demo_data

    # Generate 60 iterations of maze-solving data
    iterations = generate_demo_data(iteration_count=60)

    # Analyze with Gemini 3's 1M token context
    analyzer = GeminiContextAnalyzer()
    result = await analyzer.analyze_full_history(iterations)

    # Pattern analysis
    from ouroboros.dashboard import PatternAnalyzer
    pattern_analyzer = PatternAnalyzer()
    patterns = await pattern_analyzer.analyze_patterns(iterations)

    # Dependency prediction
    from ouroboros.dashboard import DependencyPredictor
    predictor = DependencyPredictor()
    deps = await predictor.predict_dependencies(iterations)

Run the Streamlit dashboard:
    streamlit run src/ouroboros/dashboard/streamlit_app.py
"""

# Core models (unified) - always available
from ouroboros.dashboard.models import (
    IterationData,
    IterationOutcome,
    Phase,
    ConvergenceState,
    ConvergenceCurvePoint,
    AnalysisInsight,
    ProgressTrajectory,
    FullHistoryAnalysis,
)

# Pattern analysis (migrated from gemini3/)
from ouroboros.dashboard.pattern_analyzer import (
    PatternAnalyzer,
    FailurePattern,
    PatternCategory,
    PatternSeverity,
    PatternCluster,
    PatternNetwork,
    PatternNetworkNode,
    PatternNetworkEdge,
)

# Dependency prediction (migrated from gemini3/)
from ouroboros.dashboard.dependency_predictor import (
    DependencyPredictor,
    ACDependency,
    BlockingPrediction,
    DependencyType,
    DependencyStrength,
    DependencyTreeNode,
    DependencyGraph,
)

# Enhanced Devil's Advocate (migrated from gemini3/)
from ouroboros.dashboard.devil_advocate import (
    EnhancedDevilAdvocate,
    DeepChallenge,
    ChallengeType,
    ChallengeIntensity,
    ChallengeResult,
)


# Lazy imports for components with heavy dependencies
def __getattr__(name):
    """Lazy import for components with heavy dependencies."""
    if name == "GeminiContextAnalyzer":
        from ouroboros.dashboard.gemini_analyzer import GeminiContextAnalyzer
        return GeminiContextAnalyzer
    elif name == "GeminiAPILogger":
        from ouroboros.dashboard.api_logger import GeminiAPILogger
        return GeminiAPILogger
    elif name == "APILogEntry":
        from ouroboros.dashboard.api_logger import APILogEntry
        return APILogEntry
    elif name == "TimelineView":
        from ouroboros.dashboard.timeline_view import TimelineView
        return TimelineView
    elif name == "TimelineEvent":
        from ouroboros.dashboard.timeline_view import TimelineEvent
        return TimelineEvent
    elif name == "generate_demo_data":
        from ouroboros.dashboard.maze_problem import generate_demo_data
        return generate_demo_data
    elif name == "MazeGenerator":
        from ouroboros.dashboard.maze_problem import MazeGenerator
        return MazeGenerator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Core models
    "IterationData",
    "IterationOutcome",
    "Phase",
    "ConvergenceState",
    "ConvergenceCurvePoint",
    "AnalysisInsight",
    "ProgressTrajectory",
    "FullHistoryAnalysis",
    # Main analyzer (lazy)
    "GeminiContextAnalyzer",
    # API logging (lazy)
    "GeminiAPILogger",
    "APILogEntry",
    # Timeline visualization (lazy)
    "TimelineView",
    "TimelineEvent",
    # Demo data generation (lazy)
    "generate_demo_data",
    "MazeGenerator",
    # Pattern analysis
    "PatternAnalyzer",
    "FailurePattern",
    "PatternCategory",
    "PatternSeverity",
    "PatternCluster",
    "PatternNetwork",
    "PatternNetworkNode",
    "PatternNetworkEdge",
    # Dependency prediction
    "DependencyPredictor",
    "ACDependency",
    "BlockingPrediction",
    "DependencyType",
    "DependencyStrength",
    "DependencyTreeNode",
    "DependencyGraph",
    # Devil's Advocate
    "EnhancedDevilAdvocate",
    "DeepChallenge",
    "ChallengeType",
    "ChallengeIntensity",
    "ChallengeResult",
]
