"""Progress-aware watchdog for long-running evolutionary generations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
import logging
import time
from typing import Any, Final

from ouroboros.config.models import RuntimeControlsConfig
from ouroboros.core.directive import Directive
from ouroboros.core.errors import OuroborosError
from ouroboros.events.base import BaseEvent
from ouroboros.events.control import create_control_directive_emitted_event
from ouroboros.events.lineage import lineage_generation_watchdog_decision
from ouroboros.evolution.directive_mapping import (
    is_terminal_directive,
    step_action_to_directive,
)
from ouroboros.evolution.material_progress import (
    EXECUTION_MATERIAL_EVENTS,
    LINEAGE_MATERIAL_EVENTS,
    SESSION_MATERIAL_EVENTS,
    TERMINAL_AC_STATUSES,
)
from ouroboros.persistence.event_store import EventStore

#: v0 cancellation contract (see docs/contributing/watchdog-cancellation.md).
#: Kept as a named constant so projectors and tests can assert the mode without
#: hardcoding the string.
WATCHDOG_CANCELLATION_MODE: Final[str] = "cooperative_direct_one_stage"

logger = logging.getLogger(__name__)


class GenerationWatchdogTimeout(OuroborosError):
    """Raised when a generation watchdog threshold is exceeded."""

    def __init__(
        self,
        *,
        timeout_kind: str,
        reason: str,
        details: dict[str, Any],
    ) -> None:
        super().__init__(reason, details=details)
        self.timeout_kind = timeout_kind


@dataclass(slots=True)
class GenerationProgressWatchdog:
    """Watch EventStore activity and material progress for one generation.

    Resume contract
    ---------------
    The EventStore is the recovery substrate. When ``watch()`` raises
    ``GenerationWatchdogTimeout``, the cancelled task is gone but every event
    from that attempt remains durably persisted. When watchdog decision
    persistence succeeds, the trailing ``lineage.generation.watchdog_decision``
    and ``control.directive.emitted`` events are appended atomically. If that
    persistence fails, the original ``GenerationWatchdogTimeout`` is preserved
    and re-raised with no partial watchdog decision or directive events. The
    production loop treats
    ``GenerationWatchdogTimeout`` as ``StepAction.FAILED``; replay consumers
    read the trailing directive via ``event_store.replay("lineage", lineage_id)``.
    Because the watchdog timeout path does not currently pass a real
    ``retry_budget_remaining`` value, that directive follows the default
    ``StepAction.FAILED`` mapping to ``Directive.RETRY``.
    The watchdog itself is stateless across attempts: each new instance starts
    fresh ``initialize_baseline()`` cursors, so stale events from the previous
    attempt are not double-counted as activity or material progress.
    """

    event_store: EventStore
    lineage_id: str
    generation_number: int
    execution_id: str | None
    controls: RuntimeControlsConfig
    _lineage_cursor: int = 0
    _execution_cursor: int = 0
    _attempt_start_cursor: int = 0
    _session_cursors: dict[str, int] = field(default_factory=dict)
    _related_execution_aggregate_ids: set[str] = field(default_factory=set)
    _seen_event_ids: set[str] = field(default_factory=set)
    _started_at: float = field(default_factory=time.monotonic)
    _last_activity_at: float = field(default_factory=time.monotonic)
    _last_material_progress_at: float = field(default_factory=time.monotonic)
    _activity_event_count: int = 0
    _material_event_count: int = 0
    _last_event_type: str | None = None
    _last_event_aggregate: str | None = None
    _last_material_event_type: str | None = None
    _workflow_fingerprint: tuple[Any, ...] | None = None
    _subtask_statuses: dict[str, str] = field(default_factory=dict)
    _baseline_initialized: bool = False

    async def watch[T](self, awaitable: Awaitable[T]) -> T:
        """Run *awaitable* until it finishes or watchdog policy cancels it.

        Cancellation contract (v0 — ``cooperative_direct_one_stage``):

        When a threshold is exceeded the watchdog:

        (a) Calls ``task.cancel()`` directly — one stage, no escalation.
            There is no SIGTERM-then-SIGKILL style two-stage sequence; a
            single ``CancelledError`` injection is the entire escalation path.
        (b) Awaits the cancelled task and swallows ``CancelledError`` so the
            inner coroutine has a chance to run its ``except CancelledError``
            cleanup block before control returns here.
        (c) Emits a ``lineage.generation.watchdog_decision`` event whose
            details carry
            ``cancellation_mode = WATCHDOG_CANCELLATION_MODE``.

        *Cooperative* because the inner task observes the ``CancelledError``
        and can react (e.g. flush state).  *Direct* because no
        ``AgentProcess`` intermediary is involved — the watchdog holds the
        asyncio task handle and cancels it inline.  *One-stage* because there
        is no escalation from a soft signal to a hard kill.

        To introduce two-stage escalation in the future, add a
        ``SIGTERM``-equivalent soft-cancel step, give the inner task a
        configurable grace period, then hard-cancel if still running.  Update
        ``WATCHDOG_CANCELLATION_MODE`` and the doc in
        ``docs/contributing/watchdog-cancellation.md`` accordingly.
        """
        await self.initialize_baseline()
        task: asyncio.Task[T] = asyncio.create_task(awaitable)
        try:
            while True:
                done, _ = await asyncio.wait(
                    {task},
                    timeout=self.controls.watchdog_poll_seconds,
                )
                if done:
                    return await task

                await self.poll()
                self._raise_if_threshold_exceeded()
        except GenerationWatchdogTimeout as exc:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # Watchdog timeouts surface to the runtime as a failed
            # generation. The directive follows the same retry/resilience
            # budget mapping as the evolution loop instead of deriving policy
            # directly from timeout_kind.
            directive = step_action_to_directive("failed")
            try:
                decision_metadata = await self.emit_decision(
                    action="timeout",
                    reason=exc.message,
                    details=exc.details,
                    directive=directive,
                )
                exc.details.update(decision_metadata)
            except Exception:
                logger.warning(
                    "Failed to persist watchdog decision for lineage %s generation %s",
                    self.lineage_id,
                    self.generation_number,
                    exc_info=True,
                )
            raise
        except asyncio.CancelledError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise
        except Exception:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            raise

    async def poll(self) -> None:
        """Consume newly persisted lineage and execution events."""
        lineage_events, self._lineage_cursor = await self.event_store.get_events_after(
            "lineage",
            self.lineage_id,
            self._lineage_cursor,
        )
        for event in lineage_events:
            self._record_event(event)

        if self.execution_id:
            execution_events, self._execution_cursor = await self.event_store.get_events_after(
                "execution",
                self.execution_id,
                self._execution_cursor,
            )
            for event in execution_events:
                self._record_event(event)

            await self._discover_sessions_for_execution()

        for session_id in tuple(self._session_cursors):
            await self._poll_session_related_events(session_id)

    async def emit_decision(
        self,
        *,
        action: str,
        reason: str,
        details: dict[str, Any] | None = None,
        directive: Directive | None = None,
    ) -> dict[str, str]:
        """Persist a watchdog control decision for status/debug surfaces.

        When *directive* is provided (issue #578), two events are
        appended atomically with ``EventStore.append_batch()`` and in
        this order:

        1. ``lineage.generation.watchdog_decision`` carries the
           timeout details with the resolved directive embedded in
           ``details["directive"]`` so existing consumers that already
           filter by this event type can pick up the directive without
           subscribing to a second stream.
        2. ``control.directive.emitted`` is the dedicated control-plane
           record, aggregated by ``(target_type="lineage",
           target_id=self.lineage_id)`` so it interleaves with
           ``StepAction``-level directives the evolution loop emits on
           the same lineage. ``emitted_by="generation.watchdog"`` lets
           projectors distinguish watchdog-sourced directives from
           evolver-sourced ones at a glance.

        ``directive=None`` preserves the pre-#578 behaviour: only the
        watchdog_decision event is persisted. This keeps callers that
        emit non-timeout decisions (or that don't yet have a directive
        mapping for their timeout kind) working unchanged.

        The stored event always includes ``cancellation_mode`` in its
        ``details`` dict so downstream projectors and tests can assert which
        cancellation contract was in effect without inspecting the source.
        """
        merged: dict[str, Any] = {"cancellation_mode": WATCHDOG_CANCELLATION_MODE}
        if details:
            merged.update(details)
        if directive is not None:
            merged.update(
                {
                    "directive": directive.value,
                    "directive_is_terminal": is_terminal_directive(directive),
                    "step_action": "failed",
                    "retry_budget_remaining": 1,
                }
            )

        decision_event = lineage_generation_watchdog_decision(
            self.lineage_id,
            self.generation_number,
            action,
            reason,
            execution_id=self.execution_id,
            details=merged,
        )

        if directive is None:
            await self.event_store.append(decision_event)
            return {"watchdog_decision_event_id": decision_event.id}

        timeout_kind = merged.get("timeout_kind")
        phase = merged.get("phase") if isinstance(merged.get("phase"), str) else "executing"
        idempotency_key = (
            f"generation.watchdog:{self.lineage_id}:"
            f"{self.generation_number}:{timeout_kind}:{decision_event.id}"
        )
        is_terminal = is_terminal_directive(directive)
        merged["watchdog_decision_event_id"] = decision_event.id
        merged["watchdog_directive_idempotency_key"] = idempotency_key

        directive_event = create_control_directive_emitted_event(
            target_type="lineage",
            target_id=self.lineage_id,
            emitted_by="generation.watchdog",
            directive=directive,
            reason=reason,
            lineage_id=self.lineage_id,
            generation_number=self.generation_number,
            phase=phase,
            execution_id=self.execution_id,
            idempotency_key=idempotency_key,
            extra={
                "watchdog_action": action,
                "timeout_kind": timeout_kind,
                "cancellation_mode": WATCHDOG_CANCELLATION_MODE,
                "is_terminal": is_terminal,
                "step_action": "failed",
                "retry_budget_remaining": 1,
                "watchdog_decision_event_id": decision_event.id,
            },
        )
        await self.event_store.append_batch([decision_event, directive_event])
        return {
            "watchdog_decision_event_id": decision_event.id,
            "watchdog_directive_event_id": directive_event.id,
            "watchdog_directive_idempotency_key": idempotency_key,
        }

    def _record_event(self, event: BaseEvent) -> None:
        if event.id in self._seen_event_ids:
            return
        self._seen_event_ids.add(event.id)
        self._discover_event_scopes(event)
        now = time.monotonic()
        self._last_activity_at = now
        self._activity_event_count += 1
        self._last_event_type = event.type
        self._last_event_aggregate = f"{event.aggregate_type}/{event.aggregate_id}"

        if self._is_material_progress(event):
            self._last_material_progress_at = now
            self._material_event_count += 1
            self._last_material_event_type = event.type

    def _is_material_progress(self, event: BaseEvent) -> bool:
        if event.type in LINEAGE_MATERIAL_EVENTS:
            return self._event_matches_generation(event)

        if event.type in EXECUTION_MATERIAL_EVENTS:
            return True

        if event.type in SESSION_MATERIAL_EVENTS:
            return True

        if event.type == "workflow.progress.updated":
            fingerprint = self._workflow_material_fingerprint(event.data)
            if fingerprint is None or fingerprint == self._workflow_fingerprint:
                return False
            self._workflow_fingerprint = fingerprint
            return True

        if event.type == "execution.subtask.updated":
            return self._subtask_status_changed(event.data)

        return False

    async def initialize_baseline(self) -> None:
        """Prime cursors so only events from this watchdog attempt count."""
        if self._baseline_initialized:
            return

        now = time.monotonic()
        self._started_at = now
        self._last_activity_at = now
        self._last_material_progress_at = now

        self._attempt_start_cursor = await self.event_store.get_current_rowid()
        self._lineage_cursor = self._attempt_start_cursor

        if self.execution_id:
            self._execution_cursor = self._attempt_start_cursor
            await self._prime_existing_sessions_for_execution()

        self._baseline_initialized = True

    async def _discover_sessions_for_execution(self) -> None:
        if not self.execution_id:
            return

        snapshots = await self.event_store.get_session_activity_snapshots()
        for snapshot in snapshots:
            if snapshot.execution_id == self.execution_id:
                self._remember_session(snapshot.session_id)

    async def _prime_existing_sessions_for_execution(self) -> None:
        if not self.execution_id:
            return

        snapshots = await self.event_store.get_session_activity_snapshots()
        for snapshot in snapshots:
            if snapshot.execution_id != self.execution_id:
                continue
            self._session_cursors[snapshot.session_id] = self._attempt_start_cursor

    async def _poll_session_related_events(self, session_id: str) -> None:
        if not self.execution_id:
            return

        (
            events,
            self._session_cursors[session_id],
        ) = await self.event_store.query_session_related_events_after(
            session_id=session_id,
            execution_id=self.execution_id,
            last_row_id=self._session_cursors[session_id],
        )
        for event in events:
            self._record_event(event)

    def _discover_event_scopes(self, event: BaseEvent) -> None:
        session_id = event.data.get("session_id")
        if isinstance(session_id, str) and session_id:
            self._remember_session(session_id)

        if (
            event.type == "orchestrator.session.started"
            and event.data.get("execution_id") == self.execution_id
        ):
            self._remember_session(event.aggregate_id)

        if event.aggregate_type == "execution" and self._event_belongs_to_known_execution(event):
            self._remember_related_execution_aggregate(event.aggregate_id)

        for key in ("ac_id", "session_scope_id"):
            aggregate_id = event.data.get(key)
            if isinstance(aggregate_id, str) and aggregate_id:
                self._remember_related_execution_aggregate(aggregate_id)

        runtime = event.data.get("runtime")
        if isinstance(runtime, dict):
            metadata = runtime.get("metadata")
            if isinstance(metadata, dict):
                for key in ("ac_id", "session_scope_id"):
                    aggregate_id = metadata.get(key)
                    if isinstance(aggregate_id, str) and aggregate_id:
                        self._remember_related_execution_aggregate(aggregate_id)

        acceptance_criteria = event.data.get("acceptance_criteria")
        if isinstance(acceptance_criteria, list):
            for criterion in acceptance_criteria:
                if not isinstance(criterion, dict):
                    continue
                aggregate_id = criterion.get("ac_id")
                if isinstance(aggregate_id, str) and aggregate_id:
                    self._remember_related_execution_aggregate(aggregate_id)

    def _event_belongs_to_known_execution(self, event: BaseEvent) -> bool:
        if self.execution_id and event.aggregate_id == self.execution_id:
            return True
        if event.aggregate_id in self._session_cursors:
            return True

        session_id = event.data.get("session_id")
        if isinstance(session_id, str) and session_id in self._session_cursors:
            return True

        execution_id = event.data.get("execution_id")
        if isinstance(execution_id, str) and execution_id == self.execution_id:
            return True

        parent_execution_id = event.data.get("parent_execution_id")
        return isinstance(parent_execution_id, str) and parent_execution_id == self.execution_id

    def _remember_session(self, session_id: str) -> None:
        if session_id:
            self._session_cursors.setdefault(session_id, self._attempt_start_cursor)

    def _remember_related_execution_aggregate(self, aggregate_id: str) -> None:
        if not aggregate_id or aggregate_id == self.execution_id:
            return
        self._related_execution_aggregate_ids.add(aggregate_id)

    def _event_matches_generation(self, event: BaseEvent) -> bool:
        event_generation = event.data.get("generation_number")
        if event_generation is None:
            return event.type in {"lineage.converged", "lineage.stagnated", "lineage.exhausted"}
        try:
            return int(event_generation) == self.generation_number
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _workflow_material_fingerprint(data: dict[str, Any]) -> tuple[Any, ...] | None:
        completed_count = data.get("completed_count")
        total_count = data.get("total_count")
        acceptance_criteria = data.get("acceptance_criteria")
        if not isinstance(acceptance_criteria, list):
            return (completed_count, total_count, data.get("current_phase"))

        statuses: list[tuple[Any, str]] = []
        terminal_count = 0
        for criterion in acceptance_criteria:
            if not isinstance(criterion, dict):
                continue
            status = criterion.get("status")
            if not isinstance(status, str):
                continue
            normalized = status.strip().lower()
            if normalized in TERMINAL_AC_STATUSES:
                terminal_count += 1
            statuses.append((criterion.get("index"), normalized))

        return (completed_count, total_count, terminal_count, tuple(statuses))

    def _subtask_status_changed(self, data: dict[str, Any]) -> bool:
        subtask_id = data.get("sub_task_id")
        status = data.get("status")
        if not isinstance(subtask_id, str) or not isinstance(status, str):
            return False

        normalized = status.strip().lower()
        previous = self._subtask_statuses.get(subtask_id)
        if previous == normalized:
            return False

        self._subtask_statuses[subtask_id] = normalized
        return True

    def _raise_if_threshold_exceeded(self) -> None:
        now = time.monotonic()
        elapsed = now - self._started_at
        idle_for = now - self._last_activity_at
        no_progress_for = now - self._last_material_progress_at

        safety_timeout = self.controls.generation_safety_timeout_seconds
        if safety_timeout and elapsed >= safety_timeout:
            self._raise_timeout(
                "safety_timeout",
                f"Generation exceeded safety timeout after {safety_timeout}s",
                elapsed=elapsed,
                idle_for=idle_for,
                no_progress_for=no_progress_for,
            )

        idle_timeout = self.controls.generation_idle_timeout_seconds
        if idle_timeout and idle_for >= idle_timeout:
            self._raise_timeout(
                "idle_timeout",
                f"Generation idle for {idle_for:.1f}s (limit {idle_timeout}s)",
                elapsed=elapsed,
                idle_for=idle_for,
                no_progress_for=no_progress_for,
            )

        no_progress_timeout = self.controls.generation_no_progress_timeout_seconds
        if no_progress_timeout and no_progress_for >= no_progress_timeout:
            self._raise_timeout(
                "no_material_progress_timeout",
                (
                    "Generation had no material progress for "
                    f"{no_progress_for:.1f}s (limit {no_progress_timeout}s)"
                ),
                elapsed=elapsed,
                idle_for=idle_for,
                no_progress_for=no_progress_for,
            )

    def _raise_timeout(
        self,
        timeout_kind: str,
        reason: str,
        *,
        elapsed: float,
        idle_for: float,
        no_progress_for: float,
    ) -> None:
        raise GenerationWatchdogTimeout(
            timeout_kind=timeout_kind,
            reason=reason,
            details={
                "timeout_kind": timeout_kind,
                "lineage_id": self.lineage_id,
                "generation_number": self.generation_number,
                "execution_id": self.execution_id,
                "elapsed_seconds": round(elapsed, 3),
                "idle_seconds": round(idle_for, 3),
                "no_material_progress_seconds": round(no_progress_for, 3),
                "activity_event_count": self._activity_event_count,
                "material_event_count": self._material_event_count,
                "last_event_type": self._last_event_type,
                "last_event_aggregate": self._last_event_aggregate,
                "last_material_event_type": self._last_material_event_type,
                "tracked_session_count": len(self._session_cursors),
                "tracked_related_execution_count": len(self._related_execution_aggregate_ids),
                "thresholds": {
                    "generation_idle_timeout_seconds": (
                        self.controls.generation_idle_timeout_seconds
                    ),
                    "generation_no_progress_timeout_seconds": (
                        self.controls.generation_no_progress_timeout_seconds
                    ),
                    "generation_safety_timeout_seconds": (
                        self.controls.generation_safety_timeout_seconds
                    ),
                    "watchdog_poll_seconds": self.controls.watchdog_poll_seconds,
                },
            },
        )


__all__ = [
    "GenerationProgressWatchdog",
    "GenerationWatchdogTimeout",
    "WATCHDOG_CANCELLATION_MODE",
]
