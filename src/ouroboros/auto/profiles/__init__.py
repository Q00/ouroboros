"""Built-in DomainProfile registrations (#809 P3).

Importing this package registers all built-in profiles into
``DEFAULT_REGISTRY``.  Core code must not import individual profile
modules directly — import from here so registration side-effects fire
exactly once.
"""

from __future__ import annotations

# coding profile lands in PR-2 (#809 P3, PR 2/6); import conditionally
# so this package remains importable before that PR merges.
try:
    from .coding import CODING_PROFILE  # noqa: F401
except ImportError:
    CODING_PROFILE = None  # type: ignore[assignment]

from .research import RESEARCH_PROFILE  # noqa: F401

__all__ = ["CODING_PROFILE", "RESEARCH_PROFILE"]
