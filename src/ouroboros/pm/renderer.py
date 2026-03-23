"""PM Document Renderer — generates human-readable pm.md from PMSeed.

This module defines the PMDocumentGenerator interface and template-based
rendering for producing PM Markdown documents. The generator accepts full
Q&A history and a PMSeed as inputs and returns a PM markdown string.

Two generation strategies:
- **Template-based** (``generate_pm_markdown``): Deterministic, no LLM call.
  Produces a structured Markdown document from PMSeed fields directly.
- **LLM-based** (``PMDocumentGenerator``): Uses the full Q&A transcript plus
  PMSeed to produce a richer, more readable document via LLM synthesis.
  Falls back to template-based on LLM failure.

Example usage::

    from ouroboros.pm.renderer import PMDocumentGenerator, generate_pm_markdown

    # Template-based (no LLM)
    markdown = generate_pm_markdown(seed)

    # LLM-based
    generator = PMDocumentGenerator(llm_adapter=adapter)
    result = await generator.generate(seed, qa_pairs=qa_history)
    if result.is_ok:
        pm_path = generator.save(result.value, seed)

    # Or combined generate + save
    result = await generator.generate_and_save(seed, qa_pairs=qa_history)
"""

from __future__ import annotations

# Re-export from bigbang implementation — single source of truth.
# The bigbang.pm_document module contains the full implementation;
# this module provides the canonical import path for the pm package.
from ouroboros.bigbang.pm_document import (
    PMDocumentGenerator,
    generate_pm_markdown,
    save_pm_document,
)

__all__ = [
    "PMDocumentGenerator",
    "generate_pm_markdown",
    "save_pm_document",
]
