"""Retry and persistence reconciliation for auto interview backend calls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

from ouroboros.auto.state import AutoPipelineState

log = structlog.get_logger(__name__)

_INTERVIEW_TRANSIENT_ATTEMPTS = 3
_INTERVIEW_TRANSIENT_BACKOFF_SECONDS = (1.0, 5.0)


@dataclass(frozen=True)
class ReconcileOutcome[T]:
    """Explicitly distinguish recovered, retry-safe, and inconclusive state."""

    value: T | None = None
    retry_safe: bool = False


async def with_timeout[T](
    awaitable: Awaitable[T],
    state: AutoPipelineState,
    *,
    tool_name: str,
    timeout_seconds: float,
) -> T:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except TimeoutError as exc:
        msg = (
            f"{tool_name} timed out after {timeout_seconds:.0f}s for {state.auto_session_id} "
            "(policy: state.timeout_seconds_by_phase[interview])"
        )
        raise TimeoutError(msg) from exc


async def with_transient_retry[T](
    make_awaitable: Callable[[], Awaitable[T]],
    state: AutoPipelineState,
    *,
    tool_name: str,
    timeout_seconds: float,
    reconcile: Callable[[Exception], Awaitable[T | ReconcileOutcome[T] | None]] | None = None,
    require_reconcile_confirmation: bool = False,
) -> T:
    """Retry one idempotently re-enterable interview backend operation."""
    for attempt in range(1, _INTERVIEW_TRANSIENT_ATTEMPTS + 1):
        try:
            return await with_timeout(
                make_awaitable(), state, tool_name=tool_name, timeout_seconds=timeout_seconds
            )
        except Exception as exc:
            if reconcile is not None:
                try:
                    reconciled = await reconcile(exc)
                except Exception:
                    if require_reconcile_confirmation:
                        raise exc
                    reconciled = None
                if isinstance(reconciled, ReconcileOutcome):
                    if reconciled.value is not None:
                        return reconciled.value
                    if not reconciled.retry_safe:
                        raise exc
                elif reconciled is not None:
                    return reconciled
                elif require_reconcile_confirmation:
                    raise exc
            if attempt == _INTERVIEW_TRANSIENT_ATTEMPTS:
                raise
            backoff = _INTERVIEW_TRANSIENT_BACKOFF_SECONDS[
                min(attempt - 1, len(_INTERVIEW_TRANSIENT_BACKOFF_SECONDS) - 1)
            ]
            if backoff:
                await asyncio.sleep(backoff)
    raise AssertionError("unreachable: loop always returns or raises on last attempt")


async def reconcile_persisted_session(backend: Any, session_id: str | None) -> Any | None:
    """Resume a preassigned interview only when persistence is observable."""
    persisted_id = session_id
    probe = getattr(backend, "is_session_persisted", None)
    if persisted_id and probe is not None:
        try:
            if not probe(persisted_id):
                persisted_id = None
        except Exception:
            persisted_id = None
    if not persisted_id:
        return None
    try:
        return await backend.resume(persisted_id)
    except Exception:
        return None


def record_evidence_based_session_id(
    state: AutoPipelineState,
    backend: Any,
    exc: BaseException,
    preassigned_id: str | None,
) -> None:
    """Persist an interview session ID only when the backend supplies evidence."""
    if state.interview_session_id:
        return
    from ouroboros.auto.adapters import PartialInterviewStartError

    if isinstance(exc, PartialInterviewStartError) and exc.session_id:
        state.interview_session_id = exc.session_id
        return
    if not preassigned_id:
        return
    probe = getattr(backend, "is_session_persisted", None)
    if probe is None:
        return
    try:
        persisted = probe(preassigned_id)
    except Exception as probe_exc:  # pragma: no cover - defensive
        log.warning(
            "auto.interview.persistence_probe_failed",
            preassigned_id=preassigned_id,
            error=str(probe_exc),
        )
        return
    if persisted:
        state.interview_session_id = preassigned_id
