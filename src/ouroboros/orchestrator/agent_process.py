"""``AgentProcess`` — cooperative lifecycle for long-running workflows.

Issue #518 — M6 of the Phase-2 Agent OS RFC. The five verbs ``spawn``,
``pause``, ``resume``, ``cancel``, and ``replay`` are the unified
abstraction every long-running workflow consumes (ralph, evolve_step,
execute_seed). This module is the AgentProcess spine for #518: the
interface, cooperative ``cancel()``, ``pause()``, ``resume()``, and
``status()`` operations, lifecycle directive emission that lands
``control.directive.emitted`` events with ``target_type="agent_process"``,
durable pause checkpoints, and ``replay()`` projection from the persisted
lifecycle journal.

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
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import logging
from typing import Any, Final, Protocol
from uuid import uuid4

from ouroboros.core.control_contract import ControlContract
from ouroboros.core.directive import Directive
from ouroboros.events.control import create_control_directive_emitted_event
from ouroboros.persistence.checkpoint import CheckpointData, CheckpointStore

logger = logging.getLogger(__name__)


_TARGET_TYPE: Final[str] = "agent_process"
_EMITTED_BY: Final[str] = "agent_process"
_DIRECTIVE_EMIT_TIMEOUT_SECONDS: Final[float] = 1.0


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


@dataclass(frozen=True, slots=True)
class AgentProcessSnapshot:
    """Replayable read model for one agent-process lifecycle.

    This is the durable state-model slice for #518. It intentionally projects
    only fields already present in ``control.directive.emitted`` rows so future
    checkpoint/replay work can build on an additive contract instead of
    inspecting live ``AgentProcessHandle`` instances.
    """

    process_id: str
    status: AgentProcessStatus
    intent: str | None = None
    directive_count: int = 0
    last_reason: str | None = None

    @property
    def is_terminal(self) -> bool:
        """Whether the reconstructed lifecycle is terminal."""
        return self.status in _TERMINAL_STATUSES


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


def project_agent_process_snapshot(
    events: Iterable[Any], *, process_id: str | None = None
) -> AgentProcessSnapshot | None:
    """Project an :class:`AgentProcessSnapshot` from lifecycle directive events.

    Only ``control.directive.emitted`` events targeted at ``agent_process`` are
    considered. Malformed rows are skipped rather than corrupting replay state;
    the raw events remain available from the EventStore for diagnostics.

    Args:
        events: EventStore rows or event-like test fakes.
        process_id: Optional process id filter. If omitted, the first valid
            event determines the snapshot process id and later events for other
            processes are ignored.

    Returns:
        Reconstructed snapshot, or ``None`` when no valid agent-process
        lifecycle event is present.
    """
    snapshot: AgentProcessSnapshot | None = None
    target_process_id = process_id

    valid_events: list[tuple[int, Any, str, AgentProcessStatus, str | None, str | None]] = []

    for sequence, event in enumerate(events):
        if getattr(event, "type", None) != ControlContract.EVENT_TYPE:
            continue
        if getattr(event, "aggregate_type", None) != _TARGET_TYPE:
            continue

        event_process_id = getattr(event, "aggregate_id", None)
        if not isinstance(event_process_id, str) or not event_process_id:
            continue
        if target_process_id is None:
            target_process_id = event_process_id
        if event_process_id != target_process_id:
            continue

        data = getattr(event, "data", None)
        if not isinstance(data, dict):
            continue
        extra = data.get("extra")
        if not isinstance(extra, dict):
            continue
        raw_status = extra.get("lifecycle_status")
        if not isinstance(raw_status, str):
            continue
        try:
            status = AgentProcessStatus(raw_status)
        except ValueError:
            continue
        timestamp = getattr(event, "timestamp", None)
        if not isinstance(timestamp, datetime):
            continue
        event_id = getattr(event, "id", None)
        if not isinstance(event_id, str):
            continue

        raw_intent = extra.get("intent")
        intent = raw_intent if isinstance(raw_intent, str) and raw_intent else None
        raw_reason = data.get("reason")
        reason = raw_reason if isinstance(raw_reason, str) and raw_reason else None
        valid_events.append((sequence, event, event_process_id, status, intent, reason))

    valid_events.sort(
        key=lambda item: (
            item[1].timestamp,
            item[1].id,
            item[0],
        )
    )

    for _, _event, event_process_id, status, intent, reason in valid_events:
        snapshot = AgentProcessSnapshot(
            process_id=event_process_id,
            status=status,
            intent=intent if intent is not None else (snapshot.intent if snapshot else None),
            directive_count=(snapshot.directive_count if snapshot is not None else 0) + 1,
            last_reason=reason
            if reason is not None
            else (snapshot.last_reason if snapshot else None),
        )

    return snapshot


class _AppendableEventStore(Protocol):
    """Structural type for the recorder's ``event_store`` argument.

    Defined here instead of imported so the module has no runtime
    dependency on the persistence layer; tests use a list-backed fake.
    """

    def append(self, event: Any) -> Awaitable[None]:  # pragma: no cover — Protocol-style
        ...


class _ReplayableEventStore(Protocol):
    """Structural type for the replay-capable event store."""

    async def replay(self, aggregate_type: str, aggregate_id: str) -> list[Any]:  # pragma: no cover
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
    _failure: BaseException | None = None
    _complete_on_return_after_cancel: bool = False
    _emit_directive: Callable[[Directive, str, AgentProcessStatus], Awaitable[None]] | None = None
    _replay_store: _ReplayableEventStore | None = None
    _validate_replay_against_live_status: bool = False
    _pending_emit_statuses: set[AgentProcessStatus] = field(default_factory=set)
    _pending_emit_count: int = 0
    _expected_directive_count: int = 0
    _emit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _work_task: asyncio.Task[None] | None = None
    _pause_checkpoint_store: CheckpointStore | None = field(default=None, repr=False)
    _pause_checkpoint_reason: str | None = field(default=None, repr=False)
    _pause_checkpoint_requested: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        # The paused-event is "set" when the loop is *not* paused so a
        # ``wait_unpaused()`` returns immediately by default. ``pause()``
        # clears the event.
        self._paused_event.set()

    # ------------------------------------------------------------------
    # External lifecycle verbs
    # ------------------------------------------------------------------

    async def pause(
        self,
        *,
        reason: str | None = None,
        store: CheckpointStore | None = None,
    ) -> None:
        """Request a cooperative pause.

        The work loop reaches a checkpoint, awaits :meth:`wait_unpaused`,
        and resumes only when :meth:`resume` is called. No-op when the
        process has already terminated.

        Slice 2 (#518): records the checkpoint store/reason for durable
        pause acknowledgement. The checkpoint itself is written only when
        the work loop reaches :meth:`wait_unpaused` and enters
        :attr:`AgentProcessStatus.PAUSED`, so restart recovery reflects
        acknowledged lifecycle truth rather than a merely requested pause.

        Checkpoint rows use an agent-process-specific key derived from
        ``process_id`` so lifecycle persistence cannot collide with generic
        workflow checkpoints in the shared :class:`CheckpointStore`.
        """
        if self._status in _TERMINAL_STATUSES or self.should_cancel():
            return
        if self.should_pause():
            return
        self._paused_event.clear()
        self._pause_checkpoint_store = store
        self._pause_checkpoint_reason = reason
        self._pause_checkpoint_requested = True

    async def resume(
        self,
        *,
        store: CheckpointStore | None = None,
    ) -> None:
        """Release a paused work loop.

        No-op when the process is not currently paused. Returns it to
        :attr:`AgentProcessStatus.RUNNING`.

        Slice 2 (#518): overwrites the persisted pause checkpoint with a
        ``running`` row so ``load_persisted_pause`` returns False after
        resume. Persistence failures are raised before the loop is released
        so durable state cannot remain paused while in-memory work resumes.
        If *store* differs from the store captured by the acknowledged
        pause, the captured store remains authoritative and *store* is
        ignored.
        """
        if self._status in _TERMINAL_STATUSES or not self.should_pause():
            return

        # Overwrite any acknowledged paused checkpoint before releasing the
        # work loop; otherwise a failed save could leave durable state paused
        # while in-memory work has resumed.
        self._save_lifecycle_checkpoint(
            phase="agent_process_running",
            status="running",
            event_key="resumed_at",
            log_key="resume",
            store=self._pause_checkpoint_store,
            strict=True,
        )
        self._pause_checkpoint_reason = None
        self._pause_checkpoint_requested = False

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
        self._request_cancel(reason)

    async def abort(self, reason: str = "abort requested") -> None:
        """Cancel the underlying work task for caller-abort propagation."""
        if self._status in _TERMINAL_STATUSES:
            return
        self._request_cancel(reason)
        if self._work_task is not None and not self._work_task.done():
            self._work_task.cancel()

    async def replay(self) -> AgentProcessSnapshot:
        """Reconstruct the process timeline from persisted lifecycle directives."""
        if self._replay_store is None:
            raise RuntimeError(
                "AgentProcessHandle.replay() requires an event store; "
                "pass event_store= to AgentProcess.spawn() or inject "
                "_replay_store directly."
            )

        events = await self._replay_store.replay(_TARGET_TYPE, self.process_id)
        snapshot = project_agent_process_snapshot(events, process_id=self.process_id)
        if snapshot is None:
            raise RuntimeError(
                "AgentProcessHandle.replay() found no persisted lifecycle "
                f"events for process {self.process_id!r}; recovery cannot "
                "infer status from an empty or corrupt event history."
            )

        if self._validate_replay_against_live_status:
            durable_directive_floor = self._expected_directive_count - self._pending_emit_count
            if snapshot.directive_count < durable_directive_floor:
                raise RuntimeError(
                    "AgentProcessHandle.replay() reconstructed partial lifecycle "
                    f"history for process {self.process_id!r}: persisted "
                    f"{snapshot.directive_count}, expected at least "
                    f"{durable_directive_floor}."
                )
            if snapshot.status is not self._status:
                if self._status in self._pending_emit_statuses:
                    return snapshot
                raise RuntimeError(
                    "AgentProcessHandle.replay() reconstructed stale lifecycle "
                    f"state for process {self.process_id!r}: persisted "
                    f"{snapshot.status.value!r}, live {self._status.value!r}."
                )

        return snapshot

    def status(self) -> AgentProcessStatus:
        """Return the current lifecycle status."""
        return self._status

    @classmethod
    def load_persisted_pause(
        cls,
        process_id: str,
        *,
        store: CheckpointStore | None = None,
    ) -> bool:
        """Return True iff the latest persisted checkpoint marks this process as paused.

        This is the restart-recovery primitive for slice 2 (#518). A caller
        restarting the process calls this to ask "was I paused before the
        restart?" and, if True, calls :meth:`pause` again to restore the
        in-memory flag.

        Args:
            process_id: The process identifier to look up. UUID4 hex is
                used by default (:func:`_new_process_id`); the hex alphabet
                ``[0-9a-f]`` is safe through :meth:`CheckpointStore._sanitize_seed_id`
                without truncation or collision.
            store: Optional :class:`CheckpointStore` override. When omitted,
                the default store location (``~/.ouroboros/data/checkpoints/``)
                is used.

        Returns:
            ``True`` when the latest checkpoint phase is
            ``"agent_process_paused"``, ``False`` for any other phase or
            when no checkpoint exists.
        """
        _store = store if store is not None else CheckpointStore()
        try:
            _store.initialize()
            # Pause recovery needs the newest durable lifecycle truth, not
            # CheckpointStore.load()'s generic rollback-to-older-valid behavior:
            # if the latest row is corrupt, fail closed to not paused rather
            # than resurrecting a stale paused checkpoint from .1/.2/.3.
            result = _store._load_checkpoint_level(  # noqa: SLF001
                _pause_checkpoint_seed_id(process_id), 0
            )
            if result.is_err:
                return False
            return result.value.phase == "agent_process_paused"
        except Exception:  # noqa: BLE001 — fault-tolerant; absence of checkpoint == not paused
            logger.warning(
                "agent_process.load_persisted_pause_failed",
                extra={"process_id": process_id},
            )
            return False

    def failure(self) -> BaseException | None:
        """Return the exception captured from the work task, if it failed."""
        return self._failure

    def complete_on_return_after_cancel(self) -> None:
        """Treat a late cooperative cancel as completed once work returns.

        Workflows call this after crossing their own durability point of no
        return. Hard task cancellation still raises ``CancelledError`` and
        failures still mark ``FAILED``; this only prevents a sticky cooperative
        cancel flag from contradicting already-committed successful work.
        """
        self._complete_on_return_after_cancel = True

    def should_complete_on_return_after_cancel(self) -> bool:
        """Return True once the workflow crossed its cancel point of no return."""
        return self._complete_on_return_after_cancel

    def _request_cancel(self, reason: str) -> None:
        self._cancel_reason = reason
        self._cancel_event.set()
        # Clearing the paused-flag releases a paused loop so it can
        # observe the cancel flag immediately. The CANCELLED transition
        # itself is emitted only when the work task actually exits.
        self._paused_event.set()

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
            if self.should_pause() and self._status is AgentProcessStatus.PAUSED:
                self._save_lifecycle_checkpoint(
                    phase="agent_process_paused",
                    status="paused",
                    event_key="paused_at",
                    log_key="pause",
                    reason=self._pause_checkpoint_reason,
                    store=self._pause_checkpoint_store,
                    strict=True,
                )
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
        if self._pause_checkpoint_requested:
            try:
                self._save_lifecycle_checkpoint(
                    phase="agent_process_failed",
                    status="failed",
                    event_key="failed_at",
                    log_key="terminal",
                    store=self._pause_checkpoint_store,
                    strict=True,
                )
            except Exception:
                logger.warning(
                    "agent_process.failed_checkpoint_cleanup_failed",
                    extra={"process_id": self.process_id},
                )
                self._delete_lifecycle_checkpoint(store=self._pause_checkpoint_store)
        self._status = AgentProcessStatus.FAILED
        self._completed_event.set()
        if self._emit_directive is not None:
            self._expected_directive_count += 1
            self._pending_emit_count += 1
            self._pending_emit_statuses.add(AgentProcessStatus.FAILED)
            try:
                async with self._emit_lock:
                    await self._emit_directive(Directive.CANCEL, reason, AgentProcessStatus.FAILED)
            except Exception:  # noqa: BLE001 — failed work must still unblock waiters
                logger.warning(
                    "agent_process.directive_emit_failed",
                    extra={
                        "process_id": self.process_id,
                        "directive": Directive.CANCEL.value,
                    },
                    exc_info=True,
                )
            finally:
                self._pending_emit_count -= 1
                self._pending_emit_statuses.discard(AgentProcessStatus.FAILED)

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
        if new_status in _TERMINAL_STATUSES and self._pause_checkpoint_requested:
            self._save_lifecycle_checkpoint(
                phase=f"agent_process_{new_status.value}",
                status=new_status.value,
                event_key=f"{new_status.value}_at",
                log_key="terminal",
                store=self._pause_checkpoint_store,
                strict=True,
            )
        self._status = new_status
        if new_status in _TERMINAL_STATUSES:
            self._completed_event.set()
        directive = _TRANSITION_DIRECTIVE.get(new_status)
        if directive is not None and self._emit_directive is not None:
            self._expected_directive_count += 1
            self._pending_emit_count += 1
            self._pending_emit_statuses.add(new_status)
            try:
                async with self._emit_lock:
                    await self._emit_directive(directive, reason, new_status)
            except Exception:  # noqa: BLE001 — lifecycle completion must not hang on emit failure
                logger.warning(
                    "agent_process.directive_emit_failed",
                    extra={
                        "process_id": self.process_id,
                        "directive": directive.value,
                    },
                    exc_info=True,
                )
            finally:
                self._pending_emit_count -= 1
                self._pending_emit_statuses.discard(new_status)

    def _save_lifecycle_checkpoint(
        self,
        *,
        phase: str,
        status: str,
        event_key: str,
        log_key: str,
        reason: str | None = None,
        store: CheckpointStore | None = None,
        strict: bool = False,
    ) -> None:
        """Persist a durable lifecycle checkpoint for restart recovery."""
        checkpoint_store = store if store is not None else CheckpointStore()
        try:
            checkpoint_store.initialize()
            state: dict[str, str | None] = {
                "status": status,
                event_key: datetime.now(UTC).isoformat(),
            }
            if reason is not None:
                state["reason"] = reason
            checkpoint = CheckpointData.create(
                seed_id=_pause_checkpoint_seed_id(self.process_id),
                phase=phase,
                state=state,
            )
            result = checkpoint_store.save(checkpoint)
            if result.is_err:
                logger.warning(
                    f"agent_process.{log_key}_checkpoint_save_failed",
                    extra={"process_id": self.process_id, "error": str(result.error)},
                )
                if strict:
                    raise result.error
        except Exception:
            logger.warning(
                f"agent_process.{log_key}_checkpoint_save_failed",
                extra={"process_id": self.process_id},
            )
            if strict:
                raise

    def _delete_lifecycle_checkpoint(self, *, store: CheckpointStore | None = None) -> None:
        """Best-effort fail-closed deletion of a stale pause checkpoint."""
        checkpoint_store = store if store is not None else CheckpointStore()
        try:
            checkpoint_store.initialize()
            checkpoint_path = checkpoint_store._get_checkpoint_path(  # noqa: SLF001
                _pause_checkpoint_seed_id(self.process_id), 0
            )
            if checkpoint_path.exists():
                checkpoint_path.unlink()
        except Exception:  # noqa: BLE001 — no safer recovery remains
            logger.warning(
                "agent_process.lifecycle_checkpoint_delete_failed",
                extra={"process_id": self.process_id},
            )


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
        replay_store: _ReplayableEventStore | None = (
            self.event_store  # type: ignore[assignment]
            if self.event_store is not None and hasattr(self.event_store, "replay")
            else None
        )
        handle = AgentProcessHandle(
            process_id=pid,
            _emit_directive=emit,
            _replay_store=replay_store,
            _validate_replay_against_live_status=True,
        )
        # Emit the initial RUNNING transition so projections have a
        # spawn marker even if the loop fails before the first
        # cooperative checkpoint.
        if emit is not None:
            handle._expected_directive_count += 1
            await emit(Directive.CONTINUE, "spawned", AgentProcessStatus.RUNNING)

        async def _runner() -> None:
            try:
                await work_fn(handle)
            except asyncio.CancelledError:
                if not handle.should_cancel():
                    await handle.cancel(reason="cancelled by event loop")
                try:
                    await handle._mark_cancelled()
                except BaseException as exc:  # noqa: BLE001 — terminal durability failure
                    await handle._mark_failed(
                        reason=f"cancel finalization failed {type(exc).__name__}: {exc!s}"
                    )
                    logger.exception(
                        "agent_process.cancel_finalization_failed",
                        extra={"process_id": pid},
                    )
                raise
            except BaseException as exc:  # noqa: BLE001 — runtime must capture every failure
                handle._failure = exc
                if handle.status() in _TERMINAL_STATUSES:
                    await handle._mark_failed(
                        reason=f"work raised {type(exc).__name__}: {exc!s}", force=True
                    )
                else:
                    await handle._mark_failed(reason=f"work raised {type(exc).__name__}: {exc!s}")
                logger.exception("agent_process.work_failed", extra={"process_id": pid})
                return
            else:
                try:
                    if handle.should_cancel() and not handle._complete_on_return_after_cancel:
                        await handle._mark_cancelled()
                    else:
                        await handle._mark_completed(reason="work returned")
                except BaseException as exc:  # noqa: BLE001 — terminal durability failure
                    await handle._mark_failed(
                        reason=f"terminal transition failed {type(exc).__name__}: {exc!s}"
                    )
                    logger.exception(
                        "agent_process.terminal_transition_failed",
                        extra={"process_id": pid},
                    )

        # Spawn but do not await — the caller drives lifecycle through
        # the handle.
        handle._work_task = asyncio.create_task(_runner(), name=f"agent_process:{pid}")
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
            async def append_event() -> None:
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

            try:
                await asyncio.wait_for(append_event(), timeout=_DIRECTIVE_EMIT_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001 — observational-first
                # Per #476 the journal stays out of the way. Failures
                # here are logged but never propagate; lifecycle
                # transitions complete regardless. The timeout makes
                # that guarantee hold even when persistence I/O wedges.
                logger.warning(
                    "agent_process.directive_emit_failed",
                    extra={"process_id": process_id, "directive": directive.value},
                )

        return emit


def _pause_checkpoint_seed_id(process_id: str) -> str:
    """Return the CheckpointStore key for agent-process pause state."""
    return f"agent_process_{hashlib.sha256(process_id.encode()).hexdigest()}"


def _new_process_id() -> str:
    """Return a fresh process_id."""
    return uuid4().hex


async def _wait_for_lifecycle_emit_drain(
    handle: AgentProcessHandle, *, timeout: float = 1.0
) -> None:
    """Best-effort wait for AgentProcess lifecycle journal writes to drain."""
    if handle._pending_emit_count <= 0:
        return

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while handle._pending_emit_count > 0 and loop.time() < deadline:
        await asyncio.sleep(0.01)


async def run_with_agent_process[T](
    *,
    event_store: _AppendableEventStore | None,
    intent: str,
    work_fn: Callable[[AgentProcessHandle], Awaitable[T]],
    timeout: float | None = None,
) -> T:
    """Run one production surface through :class:`AgentProcess.spawn`.

    This helper is the shared acceptance boundary for long-running MCP
    surfaces.  It lets each surface keep its existing JobManager contract
    while ensuring the actual background runner emits the uniform
    AgentProcess lifecycle directives (RUNNING/CONVERGE/CANCEL/FAILED).
    """
    result_box: list[T] = []
    error_box: list[BaseException] = []

    async def _work(handle: AgentProcessHandle) -> None:
        try:
            result = await work_fn(handle)
            result_box.append(result)
            if getattr(result, "is_error", False):
                reason = getattr(result, "text_content", None) or f"{intent} returned is_error=True"
                meta = getattr(result, "meta", {})
                terminal_kind = None
                if isinstance(meta, dict):
                    terminal_kind = meta.get("action") or meta.get("status")
                if terminal_kind in {"cancel", "cancelled", "interrupted"}:
                    await handle.cancel(reason=str(reason)[:500])
                    await handle._mark_cancelled()
                else:
                    await handle._mark_failed(reason=str(reason)[:500])
        except BaseException as exc:  # noqa: BLE001 - preserve the original runner failure
            error_box.append(exc)
            raise

    async def _request_work_cancellation() -> asyncio.Task[None] | None:
        await handle.cancel(reason="cancelled by job runner")
        work_task = handle._work_task
        if work_task is not None and not work_task.done():
            work_task.cancel()
        return work_task

    process = AgentProcess(event_store=event_store)
    handle = await process.spawn(intent=intent, work_fn=_work)
    try:
        final_status = await handle.wait_until_complete(timeout=timeout)
    except asyncio.CancelledError:
        work_task = await _request_work_cancellation()
        if work_task is not None:
            while not work_task.done():
                try:
                    await asyncio.shield(work_task)
                except asyncio.CancelledError:
                    # The JobManager may cancel this wrapper more than once.
                    # Do not re-cancel the work task while it is already
                    # unwinding; a second injected CancelledError can abort
                    # workflow cleanup and create the false-terminal state this
                    # adapter is responsible for preventing.
                    continue
        if handle.status() not in _TERMINAL_STATUSES:
            await handle._mark_cancelled()
        raise
    except TimeoutError:
        work_task = await _request_work_cancellation()
        if work_task is not None and not work_task.done():
            done, pending = await asyncio.wait({work_task}, timeout=1.0)
            for completed in done:
                try:
                    await completed
                except asyncio.CancelledError:
                    pass
            if pending:
                # Timeout callers may return, but the lifecycle must not claim
                # terminal cancellation until the owned work task really exits.
                # The AgentProcess runner will publish the terminal transition
                # later if the workflow eventually honors cancellation.
                raise
        if handle.status() not in _TERMINAL_STATUSES:
            await handle._mark_cancelled()
        raise

    await _wait_for_lifecycle_emit_drain(handle)

    if final_status is AgentProcessStatus.CANCELLED:
        if result_box:
            return result_box[0]
        raise asyncio.CancelledError(f"{intent} cancelled")
    if final_status is AgentProcessStatus.FAILED:
        if result_box:
            return result_box[0]
        if error_box:
            exc = error_box[0]
            if isinstance(exc, Exception):
                raise exc
            raise RuntimeError(f"{intent} failed") from exc
        raise RuntimeError(f"{intent} failed")
    if not result_box:
        raise RuntimeError(f"{intent} completed without a result")
    return result_box[0]
