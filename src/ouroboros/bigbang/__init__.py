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
from ouroboros.bigbang.brownfield import (
    BrownfieldEntry,
    generate_desc,
    get_default_brownfield_context,
    load_brownfield_repos_as_dicts,
    register_repo,
    scan_and_register,
    scan_home_for_repos,
    set_default_repo,
)
from ouroboros.bigbang.explore import (
    CodebaseExplorer,
    CodebaseExploreResult,
    format_explore_results,
)
from ouroboros.bigbang.interview import InterviewEngine, InterviewState
from ouroboros.bigbang.pm_document import (
    PMDocumentGenerator,
    generate_pm_markdown,
    save_pm_document,
)
from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.bigbang.pm_seed import PMSeed, UserStory
from ouroboros.bigbang.question_classifier import (
    ClassificationResult,
    QuestionCategory,
    QuestionClassifier,
)
from ouroboros.bigbang.seed_generator import (
    SeedGenerator,
    load_seed,
    save_seed_sync,
)

__all__ = [
    # Brownfield
    "BrownfieldEntry",
    "generate_desc",
    "get_default_brownfield_context",
    "load_brownfield_repos_as_dicts",
    "register_repo",
    "scan_and_register",
    "scan_home_for_repos",
    "set_default_repo",
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
    # PM Interview
    "PMInterviewEngine",
    "PMSeed",
    "UserStory",
    "QuestionClassifier",
    "QuestionCategory",
    "ClassificationResult",
    "PMDocumentGenerator",
    "generate_pm_markdown",
    "save_pm_document",
    # Seed Generation
    "SeedGenerator",
    "load_seed",
    "save_seed_sync",
]
