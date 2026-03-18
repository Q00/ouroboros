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
    BROWNFIELD_PATH,
    BrownfieldEntry,
    load_brownfield_repos,
    load_brownfield_repos_as_dicts,
    register_brownfield_repo,
    save_brownfield_repos,
    validate_entries,
)
from ouroboros.bigbang.explore import (
    CodebaseExplorer,
    CodebaseExploreResult,
    format_explore_results,
)
from ouroboros.bigbang.interview import InterviewEngine, InterviewState
from ouroboros.bigbang.prd_document import (
    PRDDocumentGenerator,
    generate_prd_markdown,
    save_prd_document,
)
from ouroboros.bigbang.prd_interview import PRDInterviewEngine
from ouroboros.bigbang.prd_seed import PRDSeed, UserStory
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
    "BROWNFIELD_PATH",
    "BrownfieldEntry",
    "load_brownfield_repos",
    "load_brownfield_repos_as_dicts",
    "register_brownfield_repo",
    "save_brownfield_repos",
    "validate_entries",
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
    # PRD Interview
    "PRDInterviewEngine",
    "PRDSeed",
    "UserStory",
    "QuestionClassifier",
    "QuestionCategory",
    "ClassificationResult",
    "PRDDocumentGenerator",
    "generate_prd_markdown",
    "save_prd_document",
    # Seed Generation
    "SeedGenerator",
    "load_seed",
    "save_seed_sync",
]
