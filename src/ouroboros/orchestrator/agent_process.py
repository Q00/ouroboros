"""``AgentProcess`` — cooperative lifecycle for long-running workflows.

Issue #518 — M6 of the Phase-2 Agent OS RFC. The five verbs ``spawn``,
``pause``, ``resume``, ``cancel``, and ``replay`` are the unified
abstraction every long-running workflow consumes (ralph, evolve_step,
execute_seed). This module is **slice 1 of #518** — the interface
itself, an in-memory implementation that supports cooperative
``cancel()``, ``pause()``, ``resume()``, and ``status()``, plus the
lifecycle directive emission that lands ``control.directive.emitted``
events with ``target_type="agent_process"``.

The verbs whose durability is the headline of #518 are intentionally
left for follow-up slices so this PR stays single-responsibility:

* ``replay()`` raises :class:`NotImplementedError`. Slice 3 (#518)
  reads the EventStore and reconstructs a timeline.
* ``pause()`` / ``resume()`` are in-memory only here — they signal a
  cooperative work loop via :meth:`AgentProcessHandle.should_pause`
  but they do **not** persist a checkpoint. Slice 2 (#518) extends
  the existing :class:`CheckpointStore` (#338) so pause survives a
  process restart.

Cooperative semantics, locked here:

* ``cancel()`` sets a flag. The work loop checks it at deterministic
  points (start of each AC iteration, before each LLM call, before
  each tool call — see #518 sub-thread). In-flight LLM/tool calls
  finish naturally; the loop exits at the next checkpoint.
* ``pause()`` sets a flag. The work loop awaits
  :meth:`AgentProcessHandle.wait_unpaused` whenever it reaches a
  checkpoint, releasing only when ``resume()`` is called.
* Per #476, the trust model is cooperative: a misbehaving work
  function can ignore the flags, but the runtime does not police
  identity. Forced kill is Tier-3 C2 territory, gated by evidence.

Lifecycle directive emission, per the body of #518:

* On every status transition the runtime appends a
  ``control.directive.emitted`` event with
  ``target_type="agent_process"`` and ``target_id=<process_id>``.
* Mapping (locked): pause → ``WAIT``, resume → ``CONTINUE``,
  cancel → ``CANCEL``, complete → ``CONVERGE``.
* Internal loop directives (``RETRY``, ``EVOLVE`` …) are *not*
  emitted by this module — those are the workflow's job
  (e.g. evolution emits ``RETRY``/``CONVERGE`` itself per #525).

The module deliberately does not import any handler-side type so
adopting :class:`AgentProcess` is a one-import change for the three
reference migrations in slices 4–6 of #518.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
import logging
from typing import Any, Final, Protocol
from uuid import uuid4

from ouroboros.core.directive import Directive
from ouroboros.events.control import create_control_directive_emitted_event

logger = logging.getLogger(__name__)


_TARGET_TYPE: Final[str] = "agent_process"
_EMITTED_BY: Final[str] = "agent_process"


class AgentProcessStatus(StrEnum):
    """Lifecycle state of an :class:`AgentProcessHandle`.

    Transitions land a ``control.directive.emitted`` event so the
    journal answers "what was this process doing at time T?" without
    requiring runtime logs (the M2 invariant from #476).
    """

    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


_TERMINAL_STATUSES: Final[frozenset[AgentProcessStatus]] = frozenset(
    {
        AgentProcessStatus.CANCELLED,
        AgentProcessStatus.COMPLETED,
        AgentProcessStatus.FAILED,
    }
)


# Mapping from a status transition to the directive that lands on the
# journal. Per the body of #518, only externally-observed lifecycle
# transitions emit directives; *internal* loop semantics (RETRY,
# EVOLVE) remain the workflow's responsibility.
_TRANSITION_DIRECTIVE: Final[dict[AgentProcessStatus, Directive]] = {
    AgentProcessStatus.RUNNING: Directive.CONTINUE,
    AgentProcessStatus.PAUSED: Directive.WAIT,
    AgentProcessStatus.CANCELLED: Directive.CANCEL,
    AgentProcessStatus.COMPLETED: Directive.CONVERGE,
    # FAILED has no canonical Directive in the current vocabulary.
    # The workflow that produced the failure is responsible for the
    # specific reason directive (e.g. RETRY exhaustion → CANCEL via
    # the evolution mapping in #525).
}


class _AppendableEventStore(Protocol):
    """Structural type for the recorder's ``event_store`` argument.

    Defined here instead of imported so the module has no runtime
    dependency on the persistence layer; tests use a list-backed fake.
    """

    def append(self, event: Any) -> Awaitable[None]:  # pragma: no cover — Protocol-style
        ...


async def _ensure_event_store_initialized(store: _AppendableEventStore) -> None:
    """Initialize concrete EventStore-like objects before first append.

    The real persistence EventStore requires ``initialize()`` before
    ``append()``. Fakes used in tests usually do not expose that method,
    so this stays duck-typed and no-ops when unavailable.
    """
    initialize = getattr(store, "initialize", None)
    if callable(initialize):
        await initialize()


@dataclass(slots=True)
class AgentProcessHandle:
    """Cooperative handle returned from :meth:`AgentProcess.spawn`.

    The handle is the surface workflows interact with. Internal work
    loops drive the handle's flag state via :meth:`should_cancel` and
    :meth:`wait_unpaused`; external callers drive lifecycle via the
    five verbs (``pause`` / ``resume`` / ``cancel`` / ``replay`` /
    ``status``).
    """

    process_id: str
    _status: AgentProcessStatus = AgentProcessStatus.RUNNING
    _cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    _paused_event: asyncio.Event = field(default_factory=asyncio.Event)
    _completed_event: asyncio.Event = field(default_factory=asyncio.Event)
    _cancel_reason: str = "cancel requested"
    _emit_directive: Callable[[Directive, str, AgentProcessStatus], Awaitable[None]] | None = None

    def __post_init__(self) -> None:
        # The paused-event is "set" when the loop is *not* paused so a
        # ``wait_unpaused()`` returns immediately by default. ``pause()``
        # clears the event.
        self._paused_event.set()

    # ------------------------------------------------------------------
    # External lifecycle verbs
    # ------------------------------------------------------------------

    async def pause(self) -> None:
        """Request a cooperative pause.

        The work loop reaches a checkpoint, awaits :meth:`wait_unpaused`,
        and resumes only when :meth:`resume` is called. No-op when the
        process has already terminated.
        """
        if self._status in _TERMINAL_STATUSES or self.should_cancel():
            return
        self._paused_event.clear()

    async def resume(self) -> None:
        """Release a paused work loop.

        No-op when the process is not currently paused. Returns it to
        :attr:`AgentProcessStatus.RUNNING`.
        """
        if self._status in _TERMINAL_STATUSES or not self.should_pause():
            return
        self._paused_event.set()
        if self._status is AgentProcessStatus.PAUSED:
            await self._set_status(AgentProcessStatus.RUNNING, reason="resume requested")

    async def cancel(self, reason: str = "cancel requested") -> None:
        """Request a cooperative cancel.

        The work loop sees the cancel flag at the next checkpoint and
        exits cleanly. In-flight LLM/tool calls finish naturally; the
        loop exits before starting the next iteration.
        """
        if self._status in _TERMINAL_STATUSES:
            return
        self._cancel_reason = reason
        self._cancel_event.set()
        # Clearing the paused-flag releases a paused loop so it can
        # observe the cancel flag immediately. The CANCELLED transition
        # itself is emitted only when the work task actually exits.
        self._paused_event.set()

    async def replay(self) -> Any:
        """Replay the process timeline (slice 3 of #518; not yet implemented)."""
        raise NotImplementedError(
            "AgentProcessHandle.replay() lands in slice 3 of #518; "
            "this PR ships the interface and the cooperative cancel/pause path only."
        )

    def status(self) -> AgentProcessStatus:
        """Return the current lifecycle status."""
        return self._status

    # ------------------------------------------------------------------
    # Internal cooperative signals (consumed by the work loop)
    # ------------------------------------------------------------------

    def should_cancel(self) -> bool:
        """``True`` once :meth:`cancel` has been called."""
        return self._cancel_event.is_set()

    def should_pause(self) -> bool:
        """``True`` while the loop is paused; pairs with :meth:`wait_unpaused`."""
        return not self._paused_event.is_set()

    async def wait_unpaused(self) -> None:
        """Block until the loop is unpaused.

        The workflow loop calls this at every checkpoint; if the process
        is not paused the call returns immediately.
        """
        if self.should_pause() and self._status is AgentProcessStatus.RUNNING:
            await self._set_status(AgentProcessStatus.PAUSED, reason="pause acknowledged")
        await self._paused_event.wait()
        if self._status is AgentProcessStatus.PAUSED and not self.should_cancel():
            await self._set_status(AgentProcessStatus.RUNNING, reason="resume requested")

    async def wait_until_complete(self, *, timeout: float | None = None) -> AgentProcessStatus:
        """Wait for a terminal status transition.

        Useful for tests and synchronous callers that want to block on
        completion. Returns the terminal status.
        """
        await asyncio.wait_for(self._completed_event.wait(), timeout=timeout)
        return self._status

    # ------------------------------------------------------------------
    # Status transition machinery
    # ------------------------------------------------------------------

    async def _mark_completed(self, *, reason: str = "work loop returned") -> None:
        """Mark the process as completed and emit the lifecycle directive."""
        if self._status in _TERMINAL_STATUSES:
            return
        await self._set_status(AgentProcessStatus.COMPLETED, reason=reason)

    async def _mark_failed(self, *, reason: str, force: bool = False) -> None:
        """Mark the process as failed and persist structured lifecycle status.

        ``FAILED`` does not have a canonical Directive in the current
        vocabulary, so the directive remains ``CANCEL`` while the journal
        stores ``extra.lifecycle_status=failed`` for replay/projectors.
        ``force=True`` is used by the runner exception path so a leaked
        internal terminal transition cannot hide a later work failure.
        """
        if self._status in _TERMINAL_STATUSES and not force:
            return
        self._status = AgentProcessStatus.FAILED
        if self._emit_directive is not None:
            await self._emit_directive(Directive.CANCEL, reason, AgentProcessStatus.FAILED)
        self._completed_event.set()

    async def _mark_cancelled(self) -> None:
        """Mark the process as cancelled after the work task has exited."""
        if self._status in {AgentProcessStatus.COMPLETED, AgentProcessStatus.FAILED}:
            return
        await self._set_status(AgentProcessStatus.CANCELLED, reason=self._cancel_reason)
        self._completed_event.set()

    def _mark_work_exited(self) -> None:
        """Mark the underlying work task as exited without changing lifecycle status."""
        self._completed_event.set()

    async def _set_status(self, new_status: AgentProcessStatus, *, reason: str) -> None:
        if new_status == self._status:
            return
        self._status = new_status
        directive = _TRANSITION_DIRECTIVE.get(new_status)
        if directive is not None and self._emit_directive is not None:
            await self._emit_directive(directive, reason, new_status)
        if new_status in _TERMINAL_STATUSES:
            self._completed_event.set()


@dataclass(frozen=True, slots=True)
class AgentProcess:
    """Factory that spawns :class:`AgentProcessHandle` instances.

    Construction:
        process = AgentProcess(event_store=event_store)
        handle = await process.spawn(
            intent="ralph",
            work_fn=async_work_function,
        )
        await handle.wait_until_complete()

    The factory:

    * Allocates a new ``process_id`` per spawn (UUID4 hex).
    * Wires the lifecycle directive emitter so transitions land on the
      EventStore.
    * Drives the work function on the event loop and finalises the
      handle's status when the work returns or raises.
    """

    event_store: _AppendableEventStore | None = None

    async def spawn(
        self,
        *,
        intent: str,
        work_fn: Callable[[AgentProcessHandle], Awaitable[Any]],
        process_id: str | None = None,
    ) -> AgentProcessHandle:
        """Start a new agent process and return its handle.

        Args:
            intent: Short human-readable label for the workflow
                (``"ralph"``, ``"evolve_step"`` …). Surfaced in the
                lifecycle directive's ``reason`` field as
                ``"<intent>: <reason>"`` so projections can group by
                workflow without joining back to context events.
            work_fn: An async function that performs the workflow.
                The function receives the :class:`AgentProcessHandle`
                so it can poll :meth:`AgentProcessHandle.should_cancel`
                and ``await`` :meth:`AgentProcessHandle.wait_unpaused`
                at cooperative checkpoints.
            process_id: Optional identifier override. By default a
                fresh hex token is allocated.

        Returns:
            The :class:`AgentProcessHandle` wired to the work loop.
        """
        pid = process_id or _new_process_id()
        emit = self._make_emitter(intent=intent, process_id=pid)
        handle = AgentProcessHandle(process_id=pid, _emit_directive=emit)
        # Emit the initial RUNNING transition so projections have a
        # spawn marker even if the loop fails before the first
        # cooperative checkpoint.
        if emit is not None:
            await emit(Directive.CONTINUE, "spawned", AgentProcessStatus.RUNNING)

        async def _runner() -> None:
            try:
                await work_fn(handle)
            except asyncio.CancelledError:
                await handle.cancel(reason="cancelled by event loop")
                await handle._mark_cancelled()
                raise
            except BaseException as exc:  # noqa: BLE001 — runtime must capture every failure
                if handle.status() in _TERMINAL_STATUSES:
                    await handle._mark_failed(
                        reason=f"work raised {type(exc).__name__}: {exc!s}", force=True
                    )
                else:
                    await handle._mark_failed(reason=f"work raised {type(exc).__name__}: {exc!s}")
                logger.exception("agent_process.work_failed", extra={"process_id": pid})
                return
            else:
                if handle.should_cancel():
                    await handle._mark_cancelled()
                else:
                    await handle._mark_completed(reason="work returned")

        # Spawn but do not await — the caller drives lifecycle through
        # the handle.
        asyncio.create_task(_runner(), name=f"agent_process:{pid}")
        return handle

    def _make_emitter(
        self, *, intent: str, process_id: str
    ) -> Callable[[Directive, str, AgentProcessStatus], Awaitable[None]] | None:
        """Build the directive-emit callable used by the handle."""
        store = self.event_store
        if store is None:
            return None

        async def emit(
            directive: Directive, reason: str, lifecycle_status: AgentProcessStatus
        ) -> None:
            try:
                await _ensure_event_store_initialized(store)
                event = create_control_directive_emitted_event(
                    target_type=_TARGET_TYPE,
                    target_id=process_id,
                    emitted_by=_EMITTED_BY,
                    directive=directive,
                    reason=f"{intent}: {reason}" if reason else intent,
                    extra={"intent": intent, "lifecycle_status": lifecycle_status.value},
                )
                await store.append(event)
            except Exception:  # noqa: BLE001 — observational-first
                # Per #476 the journal stays out of the way. Failures
                # here are logged but never propagate; lifecycle
                # transitions complete regardless.
                logger.warning(
                    "agent_process.directive_emit_failed",
                    extra={"process_id": process_id, "directive": directive.value},
                )

        return emit


def _new_process_id() -> str:
    """Return a fresh process_id."""
    return uuid4().hex
