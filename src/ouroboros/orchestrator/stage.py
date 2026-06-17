"""Compatibility re-export for orchestrator stage routing primitives.

The canonical definitions live in :mod:`ouroboros.orchestrator_stage` so
configuration validation can import the closed stage vocabulary without
importing the full :mod:`ouroboros.orchestrator` package graph.
"""

from ouroboros.orchestrator_stage import (
    EVALUATE_LLM_ROLES,
    EXECUTE_LLM_ROLES,
    INTERVIEW_LLM_ROLES,
    LLM_ROLE_STAGE_MAP,
    REFLECT_LLM_ROLES,
    VALID_STAGE_KEYS,
    Stage,
    UnknownLLMRoleError,
    UnknownStageError,
    normalize_llm_role,
    parse_stage,
    resolve_runtime_for_llm_role,
    resolve_runtime_for_stage,
    stage_for_llm_role,
)

__all__ = [
    "Stage",
    "INTERVIEW_LLM_ROLES",
    "EVALUATE_LLM_ROLES",
    "EXECUTE_LLM_ROLES",
    "REFLECT_LLM_ROLES",
    "LLM_ROLE_STAGE_MAP",
    "VALID_STAGE_KEYS",
    "UnknownLLMRoleError",
    "UnknownStageError",
    "normalize_llm_role",
    "parse_stage",
    "resolve_runtime_for_llm_role",
    "resolve_runtime_for_stage",
    "stage_for_llm_role",
]
