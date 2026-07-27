"""Cancel command group for Ouroboros.

Cancel stuck or orphaned executions by session ID, cancel all running sessions,
or interactively pick from active executions.
Interacts directly with the EventStore (not via MCP tool).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import inspect
import os
from typing import Annotated
from uuid import uuid4

import typer

from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error, print_info, print_success, print_warning
from ouroboros.cli.formatters.tables import create_table, print_table
from ouroboros.core.hitl_contract import (
    HumanInputKind,
    HumanInputRequest,
    HumanInputResponse,
    HumanInputResponseKind,
    HumanInputRiskClass,
    HumanInputSource,
)
from ouroboros.events.hitl import (
    create_hitl_answered_event,
    create_hitl_cancelled_event,
    create_hitl_requested_event,
)
from ouroboros.orchestrator.execution_authority import (
    ProcessLocalCancellationDisposition,
    collect_cancellation_acceptance_plan,
    request_process_local_cancellation,
)
from ouroboros.orchestrator.session import ACCEPTANCE_ROOT_INDICES_PROGRESS_KEY

app = typer.Typer(
    name="cancel",
    help="Cancel stuck or orphaned executions.",
    invoke_without_command=True,
)


async def _confirm_cancel_session_with_hitl(
    event_store,
    *,
    session_id: str,
    status: str,
) -> bool:
    """Ask for cancellation confirmation through the typed HITL contract."""

    requested_at = datetime.now(UTC)
    request = HumanInputRequest(
        request_id=f"hitl_cancel_{uuid4().hex[:12]}",
        session_id=session_id,
        run_id=session_id,
        created_by="ouroboros.cancel",
        kind=HumanInputKind.DESTRUCTIVE_CONFIRMATION,
        source=HumanInputSource.CONTROL_PLANE,
        risk_class=HumanInputRiskClass.DESTRUCTIVE,
        question=f"Cancel session {session_id} ({status})?",
        resume_target=f"cancel:execution:{session_id}",
        title="Cancel execution",
        body="Confirm before cancelling a running or paused execution.",
        surface="cli.cancel.execution",
        payload={"session_status": status},
        created_at=requested_at,
    )
    requested_event = create_hitl_requested_event(request)
    try:
        approved = typer.confirm(request.question)
    except (KeyboardInterrupt, EOFError, typer.Abort):
        await event_store.append_batch(
            [
                requested_event,
                create_hitl_cancelled_event(
                    request,
                    reason="Local CLI confirmation prompt aborted",
                    actor="local-user",
                ),
            ]
        )
        return False

    response = HumanInputResponse(
        request_id=request.request_id,
        session_id=session_id,
        run_id=session_id,
        actor="local-user",
        response_kind=HumanInputResponseKind.APPROVAL,
        approval_decision=approved,
        surface="cli.cancel.execution",
        received_at=datetime.now(UTC),
    )
    await event_store.append_batch(
        [
            requested_event,
            create_hitl_answered_event(request, response),
        ]
    )
    return approved


async def _get_event_store():
    """Create and initialize an EventStore instance.

    Returns:
        Initialized EventStore.
    """
    from ouroboros.persistence.event_store import EventStore

    db_path = os.path.expanduser("~/.ouroboros/ouroboros.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    event_store = EventStore(f"sqlite+aiosqlite:///{db_path}")
    await event_store.initialize()
    return event_store


async def _cancel_session(
    event_store,
    session_id: str,
    reason: str = "Cancelled by user via CLI",
) -> ProcessLocalCancellationDisposition | None:
    """Cancel a single session by ID.

    Args:
        event_store: Initialized EventStore instance.
        session_id: Session ID to cancel.
        reason: Reason for cancellation.

    Returns:
        The exact cancellation disposition, or ``None`` when no cancellation
        action was accepted.
    """
    from ouroboros.orchestrator.session import SessionRepository, SessionStatus

    repo = SessionRepository(event_store)

    # Reconstruct session to verify it exists and check current status
    result = await repo.reconstruct_session(session_id)
    if result.is_err:
        print_error(f"Session not found: {session_id}")
        return None

    tracker = result.value
    if tracker.status == SessionStatus.CANCELLED:
        print_warning(f"Session {session_id} is already cancelled.")
        return None

    if tracker.status in (SessionStatus.COMPLETED, SessionStatus.FAILED):
        print_warning(
            f"Session {session_id} is already {tracker.status.value}. "
            "Only running or paused sessions can be cancelled."
        )
        return None

    process_local = await request_process_local_cancellation(
        tracker,
        repo,
        reason=reason,
        cancelled_by="user",
    )
    if process_local is not None:
        if process_local.disposition in {
            ProcessLocalCancellationDisposition.CANCELLED,
            ProcessLocalCancellationDisposition.CANCELLATION_REQUESTED,
        }:
            return process_local.disposition
        if process_local.disposition == ProcessLocalCancellationDisposition.HELD_ELSEWHERE:
            print_warning(
                f"Session {session_id} is held by another live process-local owner; "
                "cancel it through that owner."
            )
        elif process_local.disposition == ProcessLocalCancellationDisposition.PERSISTENCE_PENDING:
            print_warning(
                f"Cancellation for session {session_id} is pending durable persistence; retry it."
            )
        else:
            print_warning(f"Session {session_id} became terminal before cancellation completed.")
        return None

    # Historical sessions have no live Foundation A capability to coordinate.
    raw_root_indices = tracker.progress.get(ACCEPTANCE_ROOT_INDICES_PROGRESS_KEY)
    expected_root_indices = (
        tuple(raw_root_indices) if isinstance(raw_root_indices, (list, tuple)) else None
    )
    try:
        acceptance_finalizations = await collect_cancellation_acceptance_plan(
            session_id=session_id,
            execution_id=tracker.execution_id,
            event_store=event_store,
            expected_root_indices=expected_root_indices,
        )
    except Exception as exc:
        print_error(f"Failed to build cancellation acceptance plan: {exc}")
        return None
    mark_cancelled_kwargs = {
        "session_id": session_id,
        "reason": reason,
        "cancelled_by": "user",
    }
    if "acceptance_finalizations" in inspect.signature(repo.mark_cancelled).parameters:
        mark_cancelled_kwargs["acceptance_finalizations"] = acceptance_finalizations
    cancel_result = await repo.mark_cancelled(**mark_cancelled_kwargs)

    if cancel_result.is_err:
        print_error(f"Failed to cancel session {session_id}: {cancel_result.error}")
        return None
    if cancel_result.value is False:
        print_warning(f"Session {session_id} became terminal before cancellation completed.")
        return None

    return ProcessLocalCancellationDisposition.CANCELLED


async def _list_active_sessions(event_store) -> list:
    """List all active (running/paused) sessions.

    Args:
        event_store: Initialized EventStore instance.

    Returns:
        List of SessionTracker objects for active sessions.
    """
    from ouroboros.orchestrator.session import SessionRepository, SessionStatus

    repo = SessionRepository(event_store)
    session_events = await event_store.get_all_sessions()

    if not session_events:
        return []

    active = []
    for event in session_events:
        session_id = event.aggregate_id
        result = await repo.reconstruct_session(session_id)
        if result.is_err:
            continue
        tracker = result.value
        if tracker.status in (SessionStatus.RUNNING, SessionStatus.PAUSED):
            active.append(tracker)

    return active


async def _cancel_all_running(
    event_store,
    reason: str = "Cancelled all running sessions via CLI",
) -> tuple[int, int, int, int]:
    """Cancel all running/paused sessions.

    Args:
        event_store: Initialized EventStore instance.
        reason: Reason for cancellation.

    Returns:
        Tuple of (cancelled_count, requested_count,
        retryable_failed_count, skipped_count).
    """
    from ouroboros.orchestrator.session import SessionRepository, SessionStatus

    repo = SessionRepository(event_store)

    # Get all session start events
    session_events = await event_store.get_all_sessions()

    if not session_events:
        return (0, 0, 0, 0)

    cancelled = 0
    requested = 0
    retryable_failed = 0
    skipped = 0

    for event in session_events:
        session_id = event.aggregate_id

        # Reconstruct to get current status
        result = await repo.reconstruct_session(session_id)
        if result.is_err:
            retryable_failed += 1
            console.print(f"  [yellow]Retry required:[/] {session_id} (session read failed)")
            continue

        tracker = result.value
        if tracker.status not in (SessionStatus.RUNNING, SessionStatus.PAUSED):
            skipped += 1
            continue

        process_local = await request_process_local_cancellation(
            tracker,
            repo,
            reason=reason,
            cancelled_by="user",
        )
        if process_local is not None:
            if process_local.disposition == ProcessLocalCancellationDisposition.CANCELLED:
                cancelled += 1
                console.print(f"  [dim]Cancelled:[/] {session_id}")
            elif (
                process_local.disposition
                == ProcessLocalCancellationDisposition.CANCELLATION_REQUESTED
            ):
                requested += 1
                console.print(f"  [dim]Cancellation requested:[/] {session_id}")
            elif (
                process_local.disposition == ProcessLocalCancellationDisposition.PERSISTENCE_PENDING
            ):
                retryable_failed += 1
                console.print(
                    f"  [yellow]Retry required:[/] {session_id} (cancellation persistence pending)"
                )
            else:
                skipped += 1
            continue

        # Historical sessions have no live Foundation A capability to coordinate.
        raw_root_indices = tracker.progress.get(ACCEPTANCE_ROOT_INDICES_PROGRESS_KEY)
        expected_root_indices = (
            tuple(raw_root_indices) if isinstance(raw_root_indices, (list, tuple)) else None
        )
        try:
            acceptance_finalizations = await collect_cancellation_acceptance_plan(
                session_id=session_id,
                execution_id=tracker.execution_id,
                event_store=event_store,
                expected_root_indices=expected_root_indices,
            )
        except Exception as exc:
            retryable_failed += 1
            console.print(
                f"  [yellow]Retry required:[/] {session_id} (acceptance plan failed: {exc})"
            )
            continue
        mark_cancelled_kwargs = {
            "session_id": session_id,
            "reason": reason,
            "cancelled_by": "user",
        }
        if "acceptance_finalizations" in inspect.signature(repo.mark_cancelled).parameters:
            mark_cancelled_kwargs["acceptance_finalizations"] = acceptance_finalizations
        cancel_result = await repo.mark_cancelled(**mark_cancelled_kwargs)

        if cancel_result.is_ok and cancel_result.value is not False:
            cancelled += 1
            console.print(f"  [dim]Cancelled:[/] {session_id}")
        elif cancel_result.is_err:
            retryable_failed += 1
            console.print(f"  [yellow]Retry required:[/] {session_id} (terminal write failed)")
        else:
            skipped += 1

    return (cancelled, requested, retryable_failed, skipped)


def _display_active_sessions(sessions: list) -> None:
    """Display active sessions in a numbered table for interactive selection.

    Args:
        sessions: List of SessionTracker objects for active sessions.
    """
    table = create_table("Active Executions")
    table.add_column("#", style="bold", no_wrap=True, justify="right")
    table.add_column("Session ID", style="cyan", no_wrap=True)
    table.add_column("Execution ID", style="dim")
    table.add_column("Seed ID", style="dim")
    table.add_column("Status", justify="center")
    table.add_column("Started", style="dim")

    for idx, tracker in enumerate(sessions, 1):
        status = tracker.status.value
        status_style = "success" if status == "running" else "warning"
        table.add_row(
            str(idx),
            tracker.session_id,
            tracker.execution_id,
            tracker.seed_id,
            f"[{status_style}]{status}[/]",
            tracker.start_time.isoformat(),
        )

    print_table(table)


async def _interactive_cancel(reason: str) -> None:
    """Interactive mode: list active executions and prompt user to pick one.

    Args:
        reason: Reason for cancellation.
    """
    event_store = await _get_event_store()

    try:
        active_sessions = await _list_active_sessions(event_store)

        if not active_sessions:
            print_info("No active executions found.")
            return

        _display_active_sessions(active_sessions)
        console.print()

        # Prompt user to pick a session number
        choice = typer.prompt(
            f"Enter number to cancel (1-{len(active_sessions)}), or 'q' to quit",
            default="q",
        )

        if choice.strip().lower() == "q":
            print_info("Cancelled. No executions were modified.")
            return

        try:
            index = int(choice) - 1
        except ValueError:
            print_error(f"Invalid selection: {choice}")
            raise typer.Exit(1)

        if index < 0 or index >= len(active_sessions):
            print_error(f"Selection out of range: {choice}. Expected 1-{len(active_sessions)}.")
            raise typer.Exit(1)

        selected = active_sessions[index]
        session_id = selected.session_id

        try:
            confirmed = await _confirm_cancel_session_with_hitl(
                event_store,
                session_id=session_id,
                status=selected.status.value,
            )
        except Exception as exc:
            print_error(f"Failed to record cancellation confirmation: {exc}")
            raise typer.Exit(1) from exc

        if not confirmed:
            print_info("Cancelled. No executions were modified.")
            return

        disposition = await _cancel_session(event_store, session_id, reason)
        if disposition == ProcessLocalCancellationDisposition.CANCELLED:
            print_success(f"Cancelled execution: {session_id}")
        elif disposition == ProcessLocalCancellationDisposition.CANCELLATION_REQUESTED:
            print_info(f"Cancellation requested for execution: {session_id}")
    finally:
        await event_store.close()


@app.command("execution")
def cancel_execution(
    execution_id: Annotated[
        str | None,
        typer.Argument(help="Session/execution ID to cancel."),
    ] = None,
    all_: Annotated[
        bool,
        typer.Option("--all", "-a", help="Cancel all running/paused executions."),
    ] = False,
    reason: Annotated[
        str,
        typer.Option("--reason", "-r", help="Reason for cancellation."),
    ] = "Cancelled by user via CLI",
) -> None:
    """Cancel a stuck or orphaned execution.

    Cancel a specific execution by session ID, or use --all to cancel
    every running/paused execution. When called without arguments,
    lists active executions and prompts you to pick one.

    This command interacts directly with the event store to emit
    cancellation events.

    Examples:

        # Interactive mode - list and pick
        ouroboros cancel execution

        # Cancel a specific execution
        ouroboros cancel execution orch_abc123def456

        # Cancel all running executions
        ouroboros cancel execution --all

        # Cancel with a custom reason
        ouroboros cancel execution orch_abc123 --reason "Stuck for 2 hours"
    """
    if execution_id and all_:
        print_error("Cannot specify both an execution ID and --all. Choose one.")
        raise typer.Exit(1)

    if not execution_id and not all_:
        # Interactive mode: list active executions and prompt user to pick one
        asyncio.run(_interactive_cancel(reason))
        return

    asyncio.run(_cancel_execution_async(execution_id, all_, reason))


async def _cancel_execution_async(
    execution_id: str | None,
    all_: bool,
    reason: str,
) -> None:
    """Async implementation for cancel execution command.

    Args:
        execution_id: Specific session ID to cancel, or None for --all mode.
        all_: Whether to cancel all running sessions.
        reason: Reason for cancellation.
    """
    event_store = await _get_event_store()

    try:
        if all_:
            print_info("Cancelling all running executions...")
            cancelled, requested, retryable_failed, skipped = await _cancel_all_running(
                event_store, reason
            )

            if cancelled == 0 and requested == 0 and retryable_failed == 0:
                print_info("No running executions found to cancel.")
            else:
                if cancelled:
                    print_success(f"Cancelled {cancelled} execution(s).")
                if requested:
                    print_info(f"Cancellation requested for {requested} execution(s).")
                if retryable_failed:
                    print_warning(
                        f"Cancellation failed for {retryable_failed} execution(s); retry required."
                    )
        else:
            assert execution_id is not None
            disposition = await _cancel_session(event_store, execution_id, reason)
            if disposition == ProcessLocalCancellationDisposition.CANCELLED:
                print_success(f"Cancelled execution: {execution_id}")
            elif disposition == ProcessLocalCancellationDisposition.CANCELLATION_REQUESTED:
                print_info(f"Cancellation requested for execution: {execution_id}")
    finally:
        await event_store.close()


__all__ = ["app"]
