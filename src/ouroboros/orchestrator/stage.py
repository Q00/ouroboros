"""Compatibility re-export for orchestrator stage routing primitives.

The canonical definitions live in :mod:`ouroboros.orchestrator_stage` so
configuration validation can import the closed stage vocabulary without
importing the full :mod:`ouroboros.orchestrator` package graph.
"""

from ouroboros.orchestrator_stage import (
    VALID_STAGE_KEYS,
    Stage,
    UnknownStageError,
    parse_stage,
    resolve_runtime_for_stage,
)

__all__ = [
    "Stage",
    "VALID_STAGE_KEYS",
    "UnknownStageError",
    "parse_stage",
    "resolve_runtime_for_stage",
]
