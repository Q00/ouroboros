"""Runner-owned process-local authority lifecycle and liveness coordination."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ouroboros.orchestrator.execution_authority import (
    _authenticate_process_local_prepared_contract,
    _await_process_local_cleanup,
    _claim_process_local_authority_generation,
    _discard_process_local_authority_generation,
    _has_live_process_local_authority_registration,
    _has_live_process_local_authority_session,
    _live_process_local_authority_generation,
    _mint_process_local_authority_generation,
    _process_local_authority_contract,
    _ProcessLocalAuthorityGeneration,
    _register_process_local_authority_generation,
    _register_process_local_authority_terminal_finalizer,
    _release_process_local_authority_generation,
    _retire_process_local_authority_generation,
    _seal_process_local_prepared_contract,
    valid_process_local_authority_contract,
)
from ouroboros.orchestrator.session import SessionStatus

if TYPE_CHECKING:
    from ouroboros.orchestrator.runner import OrchestratorError


@dataclass(frozen=True, slots=True)
class AuthorityDependencies:
    owner_id: Callable[[], int]
    adapter: Callable[[], Any]
    task_workspace: Callable[[], Any]
    task_workspace_users: Callable[[], set[tuple[str, str]]]
    set_workspace_lock_held: Callable[[bool], None]
    authorities: Callable[[], dict[tuple[str, str], _ProcessLocalAuthorityGeneration]]
    active_sessions: Callable[[], dict[str, str]]
    event_store: Callable[[], Any]
    session_repo: Callable[[], Any]
    pending_lifecycle_intents: Callable[[], dict[str, Any]]
    has_live_authority: Callable[..., bool]
    has_live_registration: Callable[..., bool]
    retire_authority: Callable[..., bool]
    release_authority: Callable[..., None]
    unregister_session: Callable[..., None]
    release_workspace: Callable[..., bool]
    cleanup_external_terminal: Callable[..., None]
    unavailable_error: Callable[..., Any]
    preserve_owner_for_retry: Callable[..., None]
    reconcile_terminal: Callable[..., Any]
    clear_cancellation: Callable[..., Any]
    collect_terminal_plan: Callable[..., Any]
    log: Callable[[], Any]
    error_type: type
    pending_intent_type: type


class ProcessLocalAuthorityLifecycle:
    """Manage the lifecycle using current runner dependencies and callbacks."""

    def __init__(self, dependencies: AuthorityDependencies) -> None:
        self._deps = dependencies

    async def _cleanup_terminal_process_local_state(
        self,
        *,
        session_id: str,
        execution_id: str,
    ) -> None:
        """Retire all live state, then acknowledge cancellation last."""

        async def _cleanup() -> None:
            retired = self._deps.retire_authority(
                session_id=session_id,
                execution_id=execution_id,
            )
            if not retired:
                # A different live adapter/runner may still own this exact
                # session. Never unregister its route or release its PID
                # heartbeat merely because this cleanup observer saw a
                # terminal record. If this runner has a stale local route,
                # remove only that route while preserving the shared lease.
                if self._deps.active_sessions().get(execution_id) == session_id:
                    self._deps.unregister_session(
                        execution_id,
                        session_id,
                        release_liveness_lease=False,
                    )
                self._deps.release_workspace(
                    session_id=session_id,
                    execution_id=execution_id,
                )
                from ouroboros.orchestrator.heartbeat import (
                    is_holder_alive,
                    is_owned_by_current_process,
                )

                if not _has_live_process_local_authority_session(session_id) and not (
                    is_holder_alive(session_id) and not is_owned_by_current_process(session_id)
                ):
                    await self._deps.clear_cancellation(session_id)
                self._deps.log().warning(
                    "orchestrator.runner.terminal_cleanup_authority_not_retired",
                    session_id=session_id,
                    execution_id=execution_id,
                )
                return
            self._deps.unregister_session(execution_id, session_id)
            self._deps.release_workspace(
                session_id=session_id,
                execution_id=execution_id,
            )
            await self._deps.clear_cancellation(session_id)

        await _await_process_local_cleanup(_cleanup())

    def _preserve_process_local_owner_for_retry(
        self,
        *,
        execution_id: str,
        session_id: str,
    ) -> None:
        """Release an exiting coroutine's effects without retiring authority.

        A durable RUNNING session remains truthful only while its exact local
        generation and liveness lease remain available.  Persistence failures
        and raw task cancellation therefore release the exclusive claim,
        active route, and worktree lock, but keep the registration resumable by
        the same retained owner.
        """
        self._deps.release_authority(
            session_id=session_id,
            execution_id=execution_id,
        )
        self._deps.unregister_session(
            execution_id,
            session_id,
            release_liveness_lease=False,
        )
        self._deps.release_workspace(
            session_id=session_id,
            execution_id=execution_id,
        )

    @staticmethod
    def _begin_process_local_authority_generation() -> _ProcessLocalAuthorityGeneration:
        """Mint one fresh live-only authority generation for a new session.

        The generation is deliberately returned to the caller rather than kept
        in a mutable runner-wide slot.  A single runner can prepare several
        sessions concurrently, and each preparation must retain its exact
        generation through contract creation and registry binding.
        """
        return _mint_process_local_authority_generation()

    @staticmethod
    def _process_local_authority_contract(
        generation: _ProcessLocalAuthorityGeneration,
    ) -> dict[str, object]:
        """Build evidence-only scope data for one issued live generation."""
        return _process_local_authority_contract(generation)

    def _has_live_process_local_authority(
        self,
        session_id: str,
        execution_id: str,
        raw_contract: object,
    ) -> bool:
        """Check live authority before restoring runtime-controlled state."""
        if not isinstance(raw_contract, Mapping):
            return False
        authority = raw_contract.get("foundation_a_authority")
        return (
            _live_process_local_authority_generation(
                session_id,
                execution_id,
                authority,
                self._deps.adapter(),
            )
            is not None
        )

    def _has_live_process_local_authority_registration(
        self,
        session_id: str,
        execution_id: str,
        raw_contract: object,
    ) -> bool:
        """See a local capability without granting another adapter its use.

        This distinction lets an observer reject safely when another live
        adapter owns the generation, instead of turning a valid paused or
        transitioning session into a false crash recovery.
        """
        if not isinstance(raw_contract, Mapping):
            return False
        return _has_live_process_local_authority_registration(
            session_id,
            execution_id,
            raw_contract.get("foundation_a_authority"),
        )

    def _process_local_authority_held_elsewhere(
        self,
        session_id: str,
        execution_id: str,
        raw_contract: object,
    ) -> bool:
        """Return whether another live owner retains this process-local session.

        A different adapter must never receive the opaque capability, but it
        must also never turn a valid owner into a false crash-recovery path.
        The in-process registration covers a foreign adapter in this PID; the
        PID lease covers a holder in another process.  The exact adapter's own
        capability is intentionally excluded so normal non-paused validation
        continues to report the session state rather than an ownership error.
        """
        if self._deps.has_live_authority(session_id, execution_id, raw_contract):
            return False
        if self._deps.has_live_registration(
            session_id,
            execution_id,
            raw_contract,
        ):
            return True
        from ouroboros.orchestrator.heartbeat import is_holder_alive

        return is_holder_alive(session_id)

    def _live_process_local_authority_generation(
        self,
        session_id: str,
        execution_id: str,
        raw_contract: object,
    ) -> _ProcessLocalAuthorityGeneration | None:
        """Return the registry-issued generation for an already-live session."""
        if not isinstance(raw_contract, Mapping):
            return None
        return _live_process_local_authority_generation(
            session_id,
            execution_id,
            raw_contract.get("foundation_a_authority"),
            self._deps.adapter(),
        )

    def _claim_process_local_authority_generation(
        self,
        session_id: str,
        execution_id: str,
        raw_contract: object,
    ) -> tuple[_ProcessLocalAuthorityGeneration | None, bool]:
        """Claim a live capability before any effectful session work begins."""
        if not isinstance(raw_contract, Mapping):
            return None, False
        generation, already_claimed = _claim_process_local_authority_generation(
            session_id,
            execution_id,
            raw_contract.get("foundation_a_authority"),
            self._deps.adapter(),
        )
        if generation is not None and self._deps.task_workspace() is not None:
            self._deps.task_workspace_users().add((session_id, execution_id))
            self._deps.set_workspace_lock_held(True)
        return generation, already_claimed

    def _release_process_local_authority(
        self,
        *,
        session_id: str,
        execution_id: str,
    ) -> None:
        """Make a deliberately paused session resumable in this process again."""
        _release_process_local_authority_generation(
            session_id,
            execution_id,
            self._deps.adapter(),
        )

    def _seal_process_local_prepared_contract(
        self,
        *,
        session_id: str,
        execution_id: str,
        generation: _ProcessLocalAuthorityGeneration,
        execution_contract: Mapping[str, object],
    ) -> None:
        """Bind the successfully persisted prepare contract to its live owner."""
        _seal_process_local_prepared_contract(
            session_id,
            execution_id,
            generation,
            self._deps.adapter(),
            execution_contract,
        )

    def _authenticate_process_local_prepared_contract(
        self,
        *,
        session_id: str,
        execution_id: str,
        generation: _ProcessLocalAuthorityGeneration,
        execution_contract: object,
    ) -> dict[str, Any] | None:
        """Recover the sealed snapshot only for an exact claimed caller copy."""
        return _authenticate_process_local_prepared_contract(
            session_id,
            execution_id,
            generation,
            self._deps.adapter(),
            execution_contract,
        )

    def _cleanup_process_local_authority_after_external_terminal(
        self,
        *,
        session_id: str,
        execution_id: str,
    ) -> None:
        """Drop runner-local references after another surface wrote terminal state.

        The registry has already invalidated the opaque capability.  This
        callback intentionally cleans only runner-local bookkeeping; the
        registry owns the liveness-lease release so it remains one operation.
        """
        self._deps.authorities().pop((session_id, execution_id), None)
        self._deps.active_sessions().pop(execution_id, None)
        self._deps.release_workspace(
            session_id=session_id,
            execution_id=execution_id,
        )

    def _register_process_local_authority(
        self,
        *,
        session_id: str,
        execution_id: str,
        execution_contract: Mapping[str, object],
        generation: _ProcessLocalAuthorityGeneration,
    ) -> None:
        """Bind the persisted correlation record to its live process capability."""
        authority = execution_contract.get("foundation_a_authority")
        if (
            not valid_process_local_authority_contract(authority)
            or authority.get("correlation_id") != generation.correlation_id
        ):
            raise self._deps.error_type(
                message="Cannot create an invalid process-local execution authority",
                details={
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "invalid": "foundation_a_authority",
                },
            )
        from ouroboros.orchestrator.heartbeat import acquire as acquire_session_lock

        try:
            _register_process_local_authority_generation(
                session_id,
                execution_id,
                generation,
                self._deps.adapter(),
            )
        except ValueError as exc:
            raise self._deps.error_type(
                message="Cannot register process-local execution authority",
                details={
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "cause": str(exc),
                },
            ) from exc
        self._deps.authorities()[(session_id, execution_id)] = generation
        if not _register_process_local_authority_terminal_finalizer(
            session_id,
            execution_id,
            authority,
            self._deps.adapter(),
            ("runner", self._deps.owner_id()),
            lambda: self._deps.cleanup_external_terminal(
                session_id=session_id,
                execution_id=execution_id,
            ),
        ):
            self._deps.retire_authority(
                session_id=session_id,
                execution_id=execution_id,
            )
            raise self._deps.error_type(
                message="Cannot register process-local terminal cleanup",
                details={
                    "session_id": session_id,
                    "execution_id": execution_id,
                },
            )
        # Establish the cross-process liveness record as soon as a session owns
        # a live capability, before a detached caller can observe its persisted
        # RUNNING tracker.  It is a lease/liveness marker, never authority.
        try:
            acquire_session_lock(session_id)
        except OSError as exc:
            # A registry entry without its liveness lease would let another
            # process misclassify a durable RUNNING tracker as crashed. Undo
            # the exact binding before returning the setup failure.
            self._deps.retire_authority(
                session_id=session_id,
                execution_id=execution_id,
            )
            raise self._deps.error_type(
                message="Cannot establish process-local execution liveness lease",
                details={
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "cause": str(exc),
                },
            ) from exc
        if self._deps.task_workspace() is not None:
            self._deps.task_workspace_users().add((session_id, execution_id))
            self._deps.set_workspace_lock_held(True)

    def _retire_process_local_authority(
        self,
        *,
        session_id: str,
        execution_id: str,
    ) -> bool:
        """Discard a terminal session's capability only when this adapter owns it."""
        retired = _retire_process_local_authority_generation(
            session_id,
            execution_id,
            self._deps.adapter(),
        )
        if retired:
            from ouroboros.orchestrator.heartbeat import (
                release_if_owned_by_current_process as release_session_lock,
            )

            release_session_lock(session_id)
        self._deps.authorities().pop((session_id, execution_id), None)
        self._deps.task_workspace_users().discard((session_id, execution_id))
        return retired

    def _discard_process_local_authority(
        self,
        generation: _ProcessLocalAuthorityGeneration,
    ) -> None:
        """Discard an unregistered capability after failed preparation."""
        _discard_process_local_authority_generation(generation)

    async def _mark_process_local_resume_unavailable(
        self,
        *,
        session_id: str,
        execution_id: str,
    ) -> OrchestratorError:
        """Terminally record a lost local capability without trying a fallback."""
        error = self._deps.unavailable_error(session_id, execution_id)
        last_error: object | None = None
        acceptance_finalizations: list[dict[str, Any]] | None = None
        try:
            acceptance_finalizations = await self._deps.collect_terminal_plan(
                session_id=session_id,
                execution_id=execution_id,
                event_store=self._deps.event_store(),
                terminal_status=SessionStatus.FAILED.value,
            )
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            last_error = exc

        if acceptance_finalizations is None:
            self._deps.pending_lifecycle_intents()[session_id] = self._deps.pending_intent_type(
                execution_id=execution_id,
                status=SessionStatus.FAILED,
                error_message=error.message,
                error_details=dict(error.details),
                acceptance_finalizations=None,
            )
            return self._deps.error_type(
                message="Failed to collect lost process-local authority acceptance plan",
                details={
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "requested_status": SessionStatus.FAILED.value,
                    "cause": str(last_error),
                    "resume_blocked": "terminal_persistence_pending",
                    "terminal_persistence_pending": True,
                },
            )

        for attempt in range(3):
            try:
                result = await self._deps.session_repo().mark_failed_if_active(
                    session_id,
                    error.message,
                    error.details,
                    acceptance_finalizations=acceptance_finalizations,
                )
            except (Exception, asyncio.CancelledError) as exc:
                durable_status = await self._deps.reconcile_terminal(
                    session_id=session_id,
                    execution_id=execution_id,
                )
                if durable_status is not None:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    return error
                if isinstance(exc, asyncio.CancelledError):
                    self._deps.pending_lifecycle_intents()[session_id] = (
                        self._deps.pending_intent_type(
                            execution_id=execution_id,
                            status=SessionStatus.FAILED,
                            error_message=error.message,
                            error_details=dict(error.details),
                            acceptance_finalizations=acceptance_finalizations,
                        )
                    )
                    self._deps.preserve_owner_for_retry(
                        session_id=session_id,
                        execution_id=execution_id,
                    )
                    raise
                last_error = exc
            else:
                if result.is_ok:
                    if not result.value:
                        self._deps.log().info(
                            "orchestrator.runner.process_local_resume_terminal_already_persisted",
                            session_id=session_id,
                            execution_id=execution_id,
                        )
                    self._deps.retire_authority(
                        session_id=session_id,
                        execution_id=execution_id,
                    )
                    return error
                last_error = result.error
            self._deps.log().warning(
                "orchestrator.runner.process_local_resume_terminal_mark_retry",
                session_id=session_id,
                execution_id=execution_id,
                attempt=attempt + 1,
                error=str(last_error),
            )
            if attempt < 2:
                await asyncio.sleep(0.05 * (2**attempt))
        self._deps.pending_lifecycle_intents()[session_id] = self._deps.pending_intent_type(
            execution_id=execution_id,
            status=SessionStatus.FAILED,
            error_message=error.message,
            error_details=dict(error.details),
            acceptance_finalizations=acceptance_finalizations,
        )
        return self._deps.error_type(
            message="Failed to persist lost process-local authority terminal state",
            details={
                "session_id": session_id,
                "execution_id": execution_id,
                "requested_status": SessionStatus.FAILED.value,
                "cause": str(last_error),
                "resume_blocked": "terminal_persistence_pending",
                "terminal_persistence_pending": True,
            },
        )
