"""PRD Document Renderer — generates human-readable prd.md from PRDSeed.

This module defines the PRDDocumentGenerator interface and template-based
rendering for producing PRD Markdown documents. The generator accepts full
Q&A history and a PRDSeed as inputs and returns a PRD markdown string.

Two generation strategies:
- **Template-based** (``generate_prd_markdown``): Deterministic, no LLM call.
  Produces a structured Markdown document from PRDSeed fields directly.
- **LLM-based** (``PRDDocumentGenerator``): Uses the full Q&A transcript plus
  PRDSeed to produce a richer, more readable document via LLM synthesis.
  Falls back to template-based on LLM failure.

Example usage::

    from ouroboros.prd.renderer import PRDDocumentGenerator, generate_prd_markdown

    # Template-based (no LLM)
    markdown = generate_prd_markdown(seed)

    # LLM-based
    generator = PRDDocumentGenerator(llm_adapter=adapter)
    result = await generator.generate(seed, qa_pairs=qa_history)
    if result.is_ok:
        prd_path = generator.save(result.value, seed)

    # Or combined generate + save
    result = await generator.generate_and_save(seed, qa_pairs=qa_history)
"""

from __future__ import annotations

# Re-export from bigbang implementation — single source of truth.
# The bigbang.prd_document module contains the full implementation;
# this module provides the canonical import path for the prd package.
from ouroboros.bigbang.prd_document import (
    PRDDocumentGenerator,
    generate_prd_markdown,
    save_prd_document,
)

__all__ = [
    "PRDDocumentGenerator",
    "generate_prd_markdown",
    "save_prd_document",
]
