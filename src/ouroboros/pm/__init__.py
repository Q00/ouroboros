"""PM (Product Requirements Document) generation module.

This package provides the PM interview and document generation pipeline:
- PMInterviewEngine: Guided interview for PM-level requirements
- PMDocumentGenerator: LLM-based PM document generation
- PMSeed: Immutable product requirements specification
"""

from ouroboros.bigbang.pm_document import (
    PMDocumentGenerator,
    generate_pm_markdown,
    save_pm_document,
)
from ouroboros.bigbang.pm_seed import PMSeed, UserStory

__all__ = [
    "PMDocumentGenerator",
    "PMSeed",
    "UserStory",
    "generate_pm_markdown",
    "save_pm_document",
]
