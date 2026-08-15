"""Shared evaluation handlers for executable MCP composition roots."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ouroboros.mcp.tools.evaluation_handlers import (
    ChecklistVerifyHandler,
    EvaluateHandler,
)


def create_shared_evaluation_handlers(
    evaluate_factory: Callable[..., EvaluateHandler],
    event_store: Any,
    llm_backend: str,
    agent_runtime_backend: str,
    opencode_mode: str | None,
) -> tuple[EvaluateHandler, ChecklistVerifyHandler]:
    """Create one evaluator authority and its checklist-verification facade."""
    evaluate = evaluate_factory(
        event_store=event_store,
        llm_backend=llm_backend,
        agent_runtime_backend=agent_runtime_backend,
        opencode_mode=opencode_mode,
    )
    checklist = ChecklistVerifyHandler(
        evaluate_handler=evaluate,
        llm_backend=llm_backend,
    )
    return evaluate, checklist


__all__ = ["create_shared_evaluation_handlers"]
