"""PRD (Product Requirements Document) generation module.

This package provides the PRD interview and document generation pipeline:
- PRDInterviewEngine: Guided interview for PM-level requirements
- PRDDocumentGenerator: LLM-based PRD document generation
- PRDSeed: Immutable product requirements specification
"""

from ouroboros.bigbang.prd_document import (
    PRDDocumentGenerator,
    generate_prd_markdown,
    save_prd_document,
)
from ouroboros.bigbang.prd_seed import PRDSeed, UserStory

__all__ = [
    "PRDDocumentGenerator",
    "PRDSeed",
    "UserStory",
    "generate_prd_markdown",
    "save_prd_document",
]
