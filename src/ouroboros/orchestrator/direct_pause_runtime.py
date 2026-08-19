"""Finite runtime state shared by direct execution pause and resume paths."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ouroboros.core.attempt_budget import AttemptBudgetProgress
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.parallel_executor_models import CoordinatorQuotaPause
from ouroboros.orchestrator.route_policy import RouteCandidate
from ouroboros.orchestrator.session import SessionRepository

DIRECT_ATTEMPT_BUDGET_PROGRESS_KEY = "direct_attempt_budget_progress"

log = get_logger(__name__)


def mapping_has_exact_keys(value: object, expected: frozenset[str]) -> bool:
    """Inspect at most one key beyond a finite durable-contract schema."""

    if not isinstance(value, Mapping):
        return False
    try:
        iterator = iter(value)
    except Exception:
        return False
    seen: set[str] = set()
    for index in range(len(expected) + 1):
        try:
            key = next(iterator)
        except StopIteration:
            return len(seen) == len(expected)
        except Exception:
            return False
        if index >= len(expected) or type(key) is not str or key not in expected or key in seen:
            return False
        seen.add(key)
    return False


@dataclass(frozen=True, slots=True)
class RecoverableFailurePause:
    """Structured pause decision for recoverable final runtime failures."""

    pause_kind: str
    reason: str
    resume_hint: str
    pause_seconds: int | None = None
    resume_after: datetime | None = None
    coordinator_owner: CoordinatorQuotaPause | None = None


@dataclass(frozen=True, slots=True)
class DirectRouteResumeState:
    """Exact nonterminal Routing D state owned by the direct runner."""

    episode_id: str
    attempt_index: int
    prior_route_ids: tuple[str, ...]
    candidate: RouteCandidate
    attempt_budget_progress: AttemptBudgetProgress


def classify_direct_route_failure(message: AgentMessage | None) -> Any:
    """Classify a direct final error without inventing retry permission."""

    from ouroboros.orchestrator.failure_taxonomy import (
        FailureClass,
        classify_hard_precondition,
    )

    if message is None or not (message.is_final and message.is_error):
        return FailureClass.EVIDENCE_MISSING
    return (
        classify_hard_precondition(message.content, message.data) or FailureClass.EVIDENCE_MISSING
    )


def has_exact_resumable_runtime_handle(runtime_handle: RuntimeHandle | None) -> bool:
    """Return whether a pause can reconnect to an existing provider session."""

    return bool(
        runtime_handle is not None and runtime_handle.can_resume and not runtime_handle.is_terminal
    )


async def persist_direct_pause_runtime_state(
    *,
    session_repo: SessionRepository,
    session_id: str,
    runtime_handle: RuntimeHandle | None,
    messages_processed: int,
    attempt_budget_progress: AttemptBudgetProgress,
    require_exact_handle: bool,
) -> bool:
    """Durably bind a direct PAUSED transition to its finite runtime state."""

    has_exact_handle = has_exact_resumable_runtime_handle(runtime_handle)
    if require_exact_handle and not has_exact_handle:
        return False
    progress: dict[str, Any] = {
        "messages_processed": messages_processed,
        DIRECT_ATTEMPT_BUDGET_PROGRESS_KEY: attempt_budget_progress.to_contract_data(),
    }
    if has_exact_handle:
        assert runtime_handle is not None
        progress["runtime"] = runtime_handle.to_session_state_dict()
        progress["runtime_backend"] = runtime_handle.backend
        if runtime_handle.backend == "claude" and runtime_handle.native_session_id:
            progress["agent_session_id"] = runtime_handle.native_session_id
    try:
        persisted = await session_repo.track_progress(session_id, progress)
    except Exception:
        log.exception(
            "orchestrator.runner.direct_pause_handle_persist_failed",
            session_id=session_id,
        )
        return False
    if persisted.is_err:
        log.warning(
            "orchestrator.runner.direct_pause_handle_persist_failed",
            session_id=session_id,
            error=str(persisted.error),
        )
        return False
    return True


__all__ = [
    "DIRECT_ATTEMPT_BUDGET_PROGRESS_KEY",
    "DirectRouteResumeState",
    "RecoverableFailurePause",
    "classify_direct_route_failure",
    "has_exact_resumable_runtime_handle",
    "mapping_has_exact_keys",
    "persist_direct_pause_runtime_state",
]
