"""Cancellation registry and lifecycle owned by the runner's cancellation collaborator."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ouroboros.core.errors import PersistenceError
from ouroboros.core.types import Result
from ouroboros.orchestrator.execution_authority import (
    ProcessLocalCancellationDisposition,
)
from ouroboros.orchestrator.session import (
    ACCEPTANCE_ROOT_INDICES_PROGRESS_KEY,
    SessionRepository,
    SessionStatus,
)

if TYPE_CHECKING:
    from ouroboros.orchestrator.heartbeat import CancellationRequest
    from ouroboros.orchestrator.runner import (
        OrchestratorError,
        OrchestratorResult,
        _PendingLifecycleIntent,
    )
    from ouroboros.persistence.event_store import EventStore

# One process-local registry shared by the runner compatibility imports.
_cancellation_registry: dict[str, CancellationRequest] = {}
_cancellation_lock: asyncio.Lock = asyncio.Lock()


async def request_cancellation(
    session_id: str,
    *,
    reason: str = "Cancellation detected during execution",
    cancelled_by: str = "runner",
) -> None:
    """Mark a session for cancellation.

    Called by the MCP cancel tool to signal that the runner should
    stop processing the given session at its next checkpoint.

    Args:
        session_id: Session to cancel.
    """
    from ouroboros.orchestrator.heartbeat import normalize_cancellation_request

    async with _cancellation_lock:
        _cancellation_registry[session_id] = normalize_cancellation_request(
            reason=reason,
            cancelled_by=cancelled_by,
        )


async def get_cancellation_request(session_id: str) -> CancellationRequest | None:
    """Return local or cross-process metadata for a pending cancellation."""
    async with _cancellation_lock:
        request = _cancellation_registry.get(session_id)
    if request is not None:
        return request
    from ouroboros.orchestrator.heartbeat import read_cancellation_request

    return read_cancellation_request(session_id)


async def is_cancellation_requested(session_id: str) -> bool:
    """Check whether cancellation has been requested for a session.

    Args:
        session_id: Session to check.

    Returns:
        True if cancellation was requested.
    """
    return await get_cancellation_request(session_id) is not None


async def clear_cancellation(session_id: str) -> None:
    """Remove a session from the cancellation registry.

    Called after the runner has acknowledged the cancellation and
    emitted the appropriate event, so the ID doesn't linger.

    Args:
        session_id: Session to clear.
    """
    async with _cancellation_lock:
        _cancellation_registry.pop(session_id, None)
    from ouroboros.orchestrator.heartbeat import clear_cancellation_request

    clear_cancellation_request(session_id)


async def get_pending_cancellations() -> frozenset[str]:
    """Return a snapshot of all pending cancellation session IDs.

    Returns:
        Frozen set of session IDs awaiting cancellation.
    """
    async with _cancellation_lock:
        return frozenset(_cancellation_registry)


@dataclass(frozen=True, slots=True)
class CancellationDependencies:
    """Read current runner state and compatibility callbacks at each call site."""

    event_store: Callable[[], EventStore]
    session_repo: Callable[[], SessionRepository]
    active_sessions: Callable[[], dict[str, str]]
    pending_lifecycle_intents: Callable[[], dict[str, _PendingLifecycleIntent]]
    console: Callable[[], Console]
    log: Callable[[], Any]
    task_summary: Callable[[], dict[str, Any]]
    request_cancellation: Callable[..., Awaitable[None]]
    get_cancellation_request: Callable[[str], Awaitable[CancellationRequest | None]]
    is_cancellation_requested: Callable[[str], Awaitable[bool]]
    request_process_local_cancellation: Callable[..., Awaitable[Any]]
    collect_cancellation_acceptance_plan: Callable[..., Awaitable[Any]]
    await_process_local_cleanup: Callable[..., Awaitable[Any]]
    create_execution_terminal_event: Callable[..., Any]
    cancel_session_directly: Callable[..., Awaitable[Result[dict[str, Any], OrchestratorError]]]
    check_cancellation: Callable[[str], Awaitable[bool]]
    handle_cancellation: Callable[
        ..., Coroutine[Any, Any, Result[OrchestratorResult, OrchestratorError]]
    ]
    cancellation_persistence_pending_result: Callable[
        ..., Result[OrchestratorResult, OrchestratorError]
    ]
    cleanup_terminal_process_local_state: Callable[..., Awaitable[None]]
    process_local_authority_held_elsewhere_error: Callable[..., OrchestratorError]
    report_frugality_retrospective: Callable[..., Awaitable[None]]
    preserve_process_local_owner_for_retry: Callable[..., None]
    reconcile_durable_terminal_and_cleanup: Callable[..., Awaitable[SessionStatus | None]]
    error_type: type[OrchestratorError]
    result_type: type[OrchestratorResult]
    pending_intent_type: type[_PendingLifecycleIntent]


class CancellationLifecycle:
    """Own cancellation transitions with explicitly injected runner dependencies."""

    def __init__(self, dependencies: CancellationDependencies) -> None:
        self._deps = dependencies

    async def cancel_execution(
        self,
        execution_id: str,
        reason: str = "Cancelled by user",
        cancelled_by: str = "user",
    ) -> Result[dict[str, Any], OrchestratorError]:
        """Cancel a running execution gracefully.

        This is the shared cancellation entry point used by both the MCP tool
        and CLI command. It signals the in-flight execution to stop at the
        next message boundary and updates the session status to CANCELLED.

        If the execution is actively running in this runner instance, adds
        the session to the cancellation registry so the message loop exits
        gracefully. If the execution is not found in-flight (e.g., orphaned
        or stuck), marks the session as cancelled directly via the repository.

        Args:
            execution_id: Execution ID to cancel.
            reason: Human-readable cancellation reason.
            cancelled_by: Who/what initiated cancellation ("user", "auto_cleanup").

        Returns:
            Result with cancellation details on success, or error.
        """
        session_id = self._deps.active_sessions().get(execution_id)

        if session_id is not None:
            # In-flight cancellation: signal via the cancellation registry
            await self._deps.request_cancellation(
                session_id,
                reason=reason,
                cancelled_by=cancelled_by,
            )
            self._deps.log().info(
                "orchestrator.runner.cancellation_requested",
                execution_id=execution_id,
                session_id=session_id,
                reason=reason,
                cancelled_by=cancelled_by,
                in_flight=True,
            )
            # The message loop will detect this and call _handle_cancellation
            return Result.ok(
                {
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "status": "cancellation_requested",
                    "in_flight": True,
                    "reason": reason,
                }
            )

        # Not in-flight: cancel directly via session repository
        return await self._deps.cancel_session_directly(
            execution_id=execution_id,
            reason=reason,
            cancelled_by=cancelled_by,
        )

    async def _cancel_session_directly(
        self,
        execution_id: str,
        reason: str,
        cancelled_by: str,
    ) -> Result[dict[str, Any], OrchestratorError]:
        """Cancel a session directly via the repository (not in-flight).

        Used for orphaned/stuck executions that are no longer actively
        running in this process. Looks up the session_id from the event
        store and marks it as cancelled.

        Args:
            execution_id: Execution ID being cancelled.
            reason: Human-readable cancellation reason.
            cancelled_by: Who/what initiated cancellation.

        Returns:
            Result with cancellation details on success, or error.
        """
        session_id: str | None = None
        # Try to find session_id from event store
        try:
            events = await self._deps.event_store().get_all_sessions()
            for event in events:
                if (
                    event.type == "orchestrator.session.started"
                    and event.data.get("execution_id") == execution_id
                ):
                    session_id = event.aggregate_id
                    break
        except Exception as e:
            self._deps.log().warning(
                "orchestrator.runner.session_lookup_failed",
                execution_id=execution_id,
                error=str(e),
            )

        if session_id is None:
            return Result.err(
                self._deps.error_type(
                    message=f"No session found for execution {execution_id}",
                    details={"execution_id": execution_id},
                )
            )

        tracker_result = await self._deps.session_repo().reconstruct_session(session_id)
        if tracker_result.is_err:
            return Result.err(
                self._deps.error_type(
                    message=f"Failed to reconstruct session for cancellation: {tracker_result.error}",
                    details={"execution_id": execution_id, "session_id": session_id},
                )
            )
        tracker = tracker_result.value
        if tracker.status in {
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
        }:
            await self._deps.cleanup_terminal_process_local_state(
                session_id=session_id,
                execution_id=execution_id,
            )
            return Result.ok(
                {
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "status": "already_terminal",
                    "terminal_status": tracker.status.value,
                    "reason": reason,
                }
            )

        process_local = await self._deps.request_process_local_cancellation(
            tracker,
            self._deps.session_repo(),
            reason=reason,
            cancelled_by=cancelled_by,
        )
        if process_local is not None:
            if (
                process_local.disposition
                == ProcessLocalCancellationDisposition.CANCELLATION_REQUESTED
            ):
                return Result.ok(
                    {
                        "execution_id": execution_id,
                        "session_id": session_id,
                        "status": "cancellation_requested",
                        "in_flight": True,
                        "reason": reason,
                    }
                )
            if process_local.disposition == ProcessLocalCancellationDisposition.HELD_ELSEWHERE:
                return Result.err(
                    self._deps.process_local_authority_held_elsewhere_error(
                        session_id, execution_id
                    )
                )
            if process_local.disposition == ProcessLocalCancellationDisposition.PERSISTENCE_PENDING:
                return Result.err(
                    self._deps.error_type(
                        message="Failed to persist cancellation; retained process-local owner must retry",
                        details={
                            "execution_id": execution_id,
                            "session_id": session_id,
                            "resume_blocked": "cancellation_persistence_pending",
                            "cause": str(process_local.error),
                        },
                    )
                )
            if process_local.disposition == ProcessLocalCancellationDisposition.ALREADY_TERMINAL:
                return Result.ok(
                    {
                        "execution_id": execution_id,
                        "session_id": session_id,
                        "status": "already_terminal",
                        "reason": reason,
                    }
                )

            await self._deps.report_frugality_retrospective(
                execution_id=execution_id,
                session_id=session_id,
                terminal_status="cancelled",
            )
            return Result.ok(
                {
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "status": "cancelled",
                    "in_flight": False,
                    "reason": reason,
                }
            )

        # Historical sessions have no live Foundation A capability to coordinate.
        raw_root_indices = tracker.progress.get(ACCEPTANCE_ROOT_INDICES_PROGRESS_KEY)
        expected_root_indices = (
            tuple(raw_root_indices) if isinstance(raw_root_indices, (list, tuple)) else None
        )
        try:
            acceptance_finalizations = await self._deps.collect_cancellation_acceptance_plan(
                session_id=session_id,
                execution_id=execution_id,
                event_store=self._deps.event_store(),
                expected_root_indices=expected_root_indices,
            )
        except Exception as exc:
            return self._deps.cancellation_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                cause=exc,
                cancellation_reason=reason,
                cancelled_by=cancelled_by,
            )
        cancel_result = await self._deps.session_repo().mark_cancelled(
            session_id=session_id,
            reason=reason,
            cancelled_by=cancelled_by,
            acceptance_finalizations=acceptance_finalizations,
        )

        if cancel_result.is_err:
            return Result.err(
                self._deps.error_type(
                    message=f"Failed to cancel session: {cancel_result.error}",
                    details={
                        "execution_id": execution_id,
                        "session_id": session_id,
                    },
                )
            )
        if cancel_result.value is False:
            terminal_result = await self._deps.session_repo().reconstruct_session(session_id)
            terminal_status = (
                terminal_result.value.status
                if terminal_result.is_ok
                and terminal_result.value.status
                in {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED}
                else None
            )
            return Result.ok(
                {
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "status": "already_terminal",
                    **(
                        {"terminal_status": terminal_status.value}
                        if terminal_status is not None
                        else {}
                    ),
                    "reason": reason,
                }
            )

        await self._deps.report_frugality_retrospective(
            execution_id=execution_id,
            session_id=session_id,
            terminal_status="cancelled",
        )

        self._deps.log().info(
            "orchestrator.runner.session_cancelled_directly",
            execution_id=execution_id,
            session_id=session_id,
            reason=reason,
            cancelled_by=cancelled_by,
        )

        return Result.ok(
            {
                "execution_id": execution_id,
                "session_id": session_id,
                "status": "cancelled",
                "in_flight": False,
                "reason": reason,
            }
        )

    async def _check_cancellation(self, session_id: str) -> bool:
        """Check for cancellation via in-memory registry and event store.

        First checks the in-memory cancellation registry (fast path) which is
        populated by the MCP cancel tool. Falls back to querying the event store
        for ``orchestrator.session.cancelled`` events so that cancellations
        persisted by the CLI or other processes are also detected.

        Args:
            session_id: Session ID to check for cancellation.

        Returns:
            True if cancellation was requested, False otherwise.
        """
        # Fast path: check the in-memory cancellation set first.
        # This is O(1) and requires no I/O.
        if await self._deps.is_cancellation_requested(session_id):
            return True

        # Slow path: check event store for externally-persisted cancellation
        try:
            events = await self._deps.event_store().query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
                limit=1,
            )
            return len(events) > 0
        except Exception:
            # Graceful degradation: if event store query fails,
            # don't interrupt execution — just log and continue
            self._deps.log().warning(
                "orchestrator.runner.cancellation_check_failed",
                session_id=session_id,
            )
            return False

    async def _check_startup_cancellation(self, session_id: str) -> bool:
        """Check cancellation before normal message-loop checkpoints exist."""
        if await self._deps.is_cancellation_requested(session_id):
            return True
        try:
            events = await self._deps.event_store().query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
                limit=1,
            )
            return len(events) > 0
        except Exception:
            self._deps.log().warning(
                "orchestrator.runner.startup_cancellation_check_failed",
                session_id=session_id,
            )
            return False

    async def _handle_requested_cancellation(
        self,
        *,
        session_id: str,
        execution_id: str,
        messages_processed: int,
        start_time: datetime,
        expected_root_indices: Iterable[int] | None = None,
    ) -> Result[OrchestratorResult, OrchestratorError] | None:
        """Apply the terminal cancellation transition at an effect choke point."""

        if not await self._deps.check_cancellation(session_id):
            return None
        return await self._deps.handle_cancellation(
            session_id=session_id,
            execution_id=execution_id,
            messages_processed=messages_processed,
            start_time=start_time,
            expected_root_indices=expected_root_indices,
        )

    def _cancellation_persistence_pending_result(
        self,
        *,
        session_id: str,
        execution_id: str,
        cause: object,
        acceptance_finalizations: list[dict[str, Any]] | None = None,
        cancellation_reason: str | None = None,
        cancelled_by: str = "runner",
    ) -> Result[OrchestratorResult, OrchestratorError]:
        """Leave a failed cancellation write retryable without stranding a claim.

        The live registration and heartbeat remain as a truthful same-process
        owner, and the cancellation request remains set.  The effectful claim,
        active route, and worktree lock belong to the coroutine that is now
        exiting, so they must be released for a retained owner to retry the
        durable terminal write on a later resume request.
        """
        self._deps.preserve_process_local_owner_for_retry(
            session_id=session_id,
            execution_id=execution_id,
        )
        if acceptance_finalizations is not None or cancellation_reason is not None:
            self._deps.pending_lifecycle_intents()[session_id] = self._deps.pending_intent_type(
                execution_id=execution_id,
                status=SessionStatus.CANCELLED,
                error_message=cancellation_reason or "Execution cancelled",
                cancelled_by=cancelled_by,
                acceptance_finalizations=acceptance_finalizations,
            )
        return Result.err(
            self._deps.error_type(
                message="Failed to persist cancellation; process-local authority remains live",
                details={
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "cause": str(cause),
                    "resume_blocked": "cancellation_persistence_pending",
                    "cancellation_persistence_pending": True,
                },
            )
        )

    async def _drain_requested_cancellation_before_pre_execution_cleanup(
        self,
        *,
        session_id: str,
        execution_id: str,
        messages_processed: int,
        start_time: datetime,
        expected_root_indices: Iterable[int] | None = None,
    ) -> Result[OrchestratorResult, OrchestratorError] | None:
        """Persist a published cancellation before abandoning a claimed setup.

        A public cancellation can arrive after a resume/new-run claims the
        generation but before the runner has registered its normal active
        route.  Raw task cancellation in that window must not retire the
        capability and leave a durable ``PAUSED`` tracker that a later resume
        reclassifies as lost authority.  Run the normal cancellation lifecycle
        in a shielded child task so a repeat caller cancellation cannot skip
        the durable write or its retryable-pending cleanup.

        Returns ``None`` when no cooperative cancellation is pending; otherwise
        it returns the normal cancellation result.  A second raw cancellation
        is re-raised after the child has drained its lifecycle work.
        """
        if not await self._deps.is_cancellation_requested(session_id):
            return None

        task = asyncio.create_task(
            self._deps.handle_cancellation(
                session_id=session_id,
                execution_id=execution_id,
                messages_processed=messages_processed,
                start_time=start_time,
                expected_root_indices=expected_root_indices,
            )
        )
        repeated_cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                repeated_cancellation = repeated_cancellation or exc

        try:
            result = task.result()
        except asyncio.CancelledError as exc:
            repeated_cancellation = repeated_cancellation or exc
            result = self._deps.cancellation_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                cause="cancellation lifecycle task cancelled",
            )
        except Exception as exc:  # pragma: no cover - defensive task boundary
            self._deps.log().exception(
                "orchestrator.runner.pre_execution_cancellation_drain_failed",
                session_id=session_id,
                execution_id=execution_id,
            )
            result = self._deps.cancellation_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                cause=exc,
            )

        if repeated_cancellation is not None:
            raise repeated_cancellation
        return result

    async def _handle_cancellation(
        self,
        session_id: str,
        execution_id: str,
        messages_processed: int,
        start_time: datetime,
        expected_root_indices: Iterable[int] | None = None,
    ) -> Result[OrchestratorResult, OrchestratorError]:
        """Handle a detected cancellation by marking the session and returning a result.

        Args:
            session_id: Session that was cancelled.
            execution_id: Execution ID for the result.
            messages_processed: Number of messages processed before cancellation.
            start_time: When execution started.

        Returns:
            Result containing OrchestratorResult with success=False and cancellation info.
        """
        duration = (datetime.now(UTC) - start_time).total_seconds()

        self._deps.log().info(
            "orchestrator.runner.execution_cancelled",
            session_id=session_id,
            execution_id=execution_id,
            messages_processed=messages_processed,
            duration_seconds=duration,
        )
        cancellation_request = await self._deps.get_cancellation_request(session_id)
        cancellation_reason = (
            cancellation_request.reason
            if cancellation_request is not None
            else "Cancellation detected during execution"
        )
        cancelled_by = (
            cancellation_request.cancelled_by if cancellation_request is not None else "runner"
        )

        # Determine and durably publish the terminal state *before* withdrawing
        # the process-local capability or its heartbeat.  A RUNNING tracker
        # with neither liveness signal is indistinguishable from a crashed
        # owner to another process, so releasing first can cause a concurrent
        # observer to terminalize this deliberate cancellation as lost
        # authority.  It would also make a persistence failure report a
        # cancellation that the durable session never recorded.
        session_result = await self._deps.session_repo().reconstruct_session(session_id)
        _terminal = {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED}
        # An unreadable snapshot cannot prove that a terminal state already
        # exists.  Continue through ``mark_cancelled`` while retaining the live
        # owner; that append is the authoritative terminal write and preserves
        # the legacy best-effort reconstruction posture without ever releasing
        # a RUNNING session first.
        session_already_terminal = session_result.is_ok and session_result.value.status in _terminal
        if session_already_terminal:
            terminal_status = session_result.value.status
            final_message = f"Execution already {terminal_status.value}"
            summary = {"terminal_status": terminal_status.value, **self._deps.task_summary()}
            if terminal_status == SessionStatus.CANCELLED:
                summary["cancelled"] = True
            try:
                execution_terminal_events = await self._deps.event_store().query_events(
                    aggregate_id=execution_id,
                    event_type="execution.terminal",
                    limit=1,
                )
            except Exception:
                execution_terminal_events = []

            async def _reconcile_existing_terminal_owner() -> None:
                try:
                    if not execution_terminal_events:
                        await self._deps.event_store().append(
                            self._deps.create_execution_terminal_event(
                                execution_id=execution_id,
                                session_id=session_id,
                                status=terminal_status.value,
                                summary=(
                                    summary if terminal_status == SessionStatus.COMPLETED else None
                                ),
                                error_message=(
                                    final_message
                                    if terminal_status != SessionStatus.COMPLETED
                                    else None
                                ),
                                messages_processed=messages_processed,
                            )
                        )
                finally:
                    await self._deps.cleanup_terminal_process_local_state(
                        session_id=session_id,
                        execution_id=execution_id,
                    )

            await self._deps.await_process_local_cleanup(_reconcile_existing_terminal_owner())
            await self._deps.report_frugality_retrospective(
                execution_id=execution_id,
                session_id=session_id,
                terminal_status=terminal_status.value,
            )
            return Result.ok(
                self._deps.result_type(
                    success=terminal_status == SessionStatus.COMPLETED,
                    session_id=session_id,
                    execution_id=execution_id,
                    summary=summary,
                    messages_processed=messages_processed,
                    final_message=final_message,
                    duration_seconds=duration,
                )
            )

        # Compute the complete observed-root decision set before the terminal
        # CAS.  The terminal session event carries this exact plan so a
        # cancellation cannot commit a durable winner and then lose its final
        # acceptance records to interruption.
        try:
            cancellation_acceptance_finalizations = (
                await self._deps.collect_cancellation_acceptance_plan(
                    session_id=session_id,
                    execution_id=execution_id,
                    event_store=self._deps.event_store(),
                    expected_root_indices=expected_root_indices,
                )
            )
        except Exception as exc:
            return self._deps.cancellation_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                cause=exc,
                cancellation_reason=cancellation_reason,
                cancelled_by=cancelled_by,
            )
        try:
            cancel_result = await self._deps.session_repo().mark_cancelled(
                session_id,
                reason=cancellation_reason,
                cancelled_by=cancelled_by,
                acceptance_finalizations=cancellation_acceptance_finalizations,
            )
        except asyncio.CancelledError:
            if (
                await self._deps.reconcile_durable_terminal_and_cleanup(
                    session_id=session_id,
                    execution_id=execution_id,
                )
                is None
            ):
                self._deps.pending_lifecycle_intents()[session_id] = self._deps.pending_intent_type(
                    execution_id=execution_id,
                    status=SessionStatus.CANCELLED,
                    error_message=cancellation_reason,
                    cancelled_by=cancelled_by,
                    acceptance_finalizations=cancellation_acceptance_finalizations,
                )
                self._deps.preserve_process_local_owner_for_retry(
                    session_id=session_id,
                    execution_id=execution_id,
                )
            raise
        except Exception as exc:
            durable_status = await self._deps.reconcile_durable_terminal_and_cleanup(
                session_id=session_id,
                execution_id=execution_id,
            )
            if durable_status is not None:
                return await self._deps.handle_cancellation(
                    session_id=session_id,
                    execution_id=execution_id,
                    messages_processed=messages_processed,
                    start_time=start_time,
                    expected_root_indices=expected_root_indices,
                )
            self._deps.log().warning(
                "orchestrator.runner.mark_cancelled_raised",
                session_id=session_id,
                error=str(exc),
            )
            return self._deps.cancellation_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                cause=exc,
                acceptance_finalizations=cancellation_acceptance_finalizations,
                cancellation_reason=cancellation_reason,
                cancelled_by=cancelled_by,
            )
        if cancel_result is not None and cancel_result.is_err:
            self._deps.log().warning(
                "orchestrator.runner.mark_cancelled_failed",
                session_id=session_id,
                error=str(cancel_result.error),
            )
            return self._deps.cancellation_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                cause=cancel_result.error,
                acceptance_finalizations=cancellation_acceptance_finalizations,
                cancellation_reason=cancellation_reason,
                cancelled_by=cancelled_by,
            )
        if cancel_result is not None and cancel_result.value is False:
            # The session became terminal after the initial reconstruction but
            # before this owner won its cancellation transition. Re-enter once
            # so the authoritative terminal branch mirrors the real winner and
            # tears down this process-local owner without reporting cancellation.
            winner = await self._deps.session_repo().reconstruct_session(session_id)
            if winner.is_ok and winner.value.status in _terminal:
                return await self._deps.handle_cancellation(
                    session_id=session_id,
                    execution_id=execution_id,
                    messages_processed=messages_processed,
                    start_time=start_time,
                    expected_root_indices=expected_root_indices,
                )
            return self._deps.cancellation_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                cause=PersistenceError(
                    "Terminal cancellation lost its CAS but the durable winner could not be read"
                ),
            )

        # The session is now terminal. Drain the complete reconciliation in a
        # shielded child task: repeated caller cancellation may not interrupt
        # marker acknowledgement, projection, or live-owner teardown after the
        # durable CAS has committed.
        async def _reconcile_cancelled_owner() -> None:
            try:
                await self._deps.event_store().append(
                    self._deps.create_execution_terminal_event(
                        execution_id=execution_id,
                        session_id=session_id,
                        status="cancelled",
                        error_message=cancellation_reason,
                        messages_processed=messages_processed,
                    )
                )
            finally:
                await self._deps.cleanup_terminal_process_local_state(
                    session_id=session_id,
                    execution_id=execution_id,
                )

        await self._deps.await_process_local_cleanup(_reconcile_cancelled_owner())
        await self._deps.report_frugality_retrospective(
            execution_id=execution_id,
            session_id=session_id,
            terminal_status="cancelled",
        )

        # Display cancellation notice
        self._deps.console().print(
            Panel(
                Text("Execution cancelled by external request", style="yellow"),
                title="[yellow]Execution Cancelled[/yellow]",
                border_style="yellow",
            )
        )

        return Result.ok(
            self._deps.result_type(
                success=False,
                session_id=session_id,
                execution_id=execution_id,
                summary={"cancelled": True, **self._deps.task_summary()},
                messages_processed=messages_processed,
                final_message="Execution cancelled by external request",
                duration_seconds=duration,
            )
        )
