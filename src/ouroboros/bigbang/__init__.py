"""Big Bang phase - Interactive interview for requirement clarification.

This package implements Phase 0: Big Bang, which transforms vague user ideas
into clear, executable requirements through an interactive interview process.
"""

from ouroboros.bigbang.ambiguity import (
    AMBIGUITY_THRESHOLD,
    AmbiguityScore,
    AmbiguityScorer,
    ComponentScore,
    ScoreBreakdown,
    format_score_display,
    is_ready_for_seed,
)
from ouroboros.bigbang.explore import (
    CodebaseExplorer,
    CodebaseExploreResult,
    format_explore_results,
)
from ouroboros.bigbang.interview import InterviewEngine, InterviewState
from ouroboros.bigbang.seed_generator import (
    SeedGenerator,
    load_seed,
    save_seed_sync,
)

__all__ = [
    # Ambiguity
    "AMBIGUITY_THRESHOLD",
    "AmbiguityScore",
    "AmbiguityScorer",
    "ComponentScore",
    "ScoreBreakdown",
    "format_score_display",
    "is_ready_for_seed",
    # Explore
    "CodebaseExploreResult",
    "CodebaseExplorer",
    "format_explore_results",
    # Interview
    "InterviewEngine",
    "InterviewState",
    # Seed Generation
    "SeedGenerator",
    "load_seed",
    "save_seed_sync",
]
