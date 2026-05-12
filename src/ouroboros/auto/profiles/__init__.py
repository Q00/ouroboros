"""Built-in DomainProfile registrations (#809 P3).

Importing this package registers all built-in profiles into
``DEFAULT_REGISTRY``.  Core code must not import individual profile
modules directly — import from here so registration side-effects fire
exactly once.
"""

from __future__ import annotations

from importlib import import_module
from importlib.util import find_spec
from typing import Any

# coding profile lands in PR-2 (#809 P3, PR 2/6); import conditionally
# so this package remains importable before that PR merges.  Check for module
# absence before import so ImportError raised inside an existing coding profile
# still propagates instead of being mistaken for an optional missing module.
CODING_PROFILE: Any = None
if find_spec(f"{__name__}.coding") is not None:
    CODING_PROFILE = import_module(f"{__name__}.coding").CODING_PROFILE

from .research import RESEARCH_PROFILE  # noqa: E402,F401

__all__ = ["CODING_PROFILE", "RESEARCH_PROFILE"]
