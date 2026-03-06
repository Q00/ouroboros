"""Workflow management commands for Ouroboros.

List interrupted sessions and resume workflows from checkpoints.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from rich.console import Console
from rich.table import Table
import typer

from ouroboros.cli.formatters.panels import print_error, print_info, print_success

app = typer.Typer(
    name="workflow",
    help="Manage workflow sessions (list, resume).",
    no_args_is_help=True,
)

console = Console()


def _load_interrupted_sessions() -> list[dict]:
    """Load interrupted/paused sessions from the event store.

    Returns:
        List of session dicts with id, goal, progress, and timestamp.
    """
    import asyncio

    from ouroboros.persistence.event_store import EventStore

    async def _query() -> list[dict]:
        store = EventStore()
        await store.initialize()

        # Get all sessions and filter for paused/interrupted ones
        sessions = await store.get_all_sessions()
        interrupted = []

        for session in sessions:
            payload = session.get("payload", {})
            # Check if session was interrupted (look for interruption events)
            session_id = payload.get("session_id", "")
            if not session_id:
                continue

            # Replay session events to find status
            events = await store.replay("orchestrator", session_id)
            status = "unknown"
            goal = payload.get("seed_goal", "Unknown")
            messages = 0
            timestamp = session.get("timestamp", "")

            for event in events:
                ep = event.get("payload", {})
                etype = event.get("event_type", "")
                if "completed" in etype:
                    status = "completed"
                elif "failed" in etype:
                    error_type = ep.get("error_type", "")
                    if error_type == "KeyboardInterrupt":
                        status = "interrupted"
                    else:
                        status = "failed"
                    messages = ep.get("messages_processed", 0)

            if status == "interrupted":
                interrupted.append({
                    "session_id": session_id,
                    "goal": goal,
                    "messages": messages,
                    "timestamp": timestamp,
                })

        return interrupted

    return asyncio.run(_query())


def _build_resume_context(session_id: str) -> str | None:
    """Build context summary for resuming an interrupted session.

    Reconstructs what was completed before interruption for prompt injection.

    Args:
        session_id: The session to build context for.

    Returns:
        Context summary string, or None if session not found.
    """
    from ouroboros.persistence.checkpoint import CheckpointStore

    store = CheckpointStore()

    # Try to load checkpoint for this session's seed
    # Checkpoints are keyed by seed_id, so we need to find the right one
    import asyncio

    from ouroboros.persistence.event_store import EventStore

    async def _find_seed_id() -> str | None:
        es = EventStore()
        await es.initialize()
        events = await es.replay("orchestrator", session_id)
        for event in events:
            payload = event.get("payload", {})
            if "seed_id" in payload:
                return payload["seed_id"]
        return None

    seed_id = asyncio.run(_find_seed_id())
    if not seed_id:
        return None

    checkpoint_result = store.load(seed_id)
    if checkpoint_result.is_err or checkpoint_result.value is None:
        return None

    checkpoint = checkpoint_result.value
    state = checkpoint.state

    # Build human-readable context summary
    goal = state.get("seed_goal", "Unknown")
    acs = state.get("acceptance_criteria", [])
    workflow = state.get("workflow_state", {})
    completed_acs = []
    pending_acs = []

    ac_list = workflow.get("acceptance_criteria", [])
    for ac in ac_list:
        if ac.get("status") == "completed":
            completed_acs.append(ac.get("content", ""))
        else:
            pending_acs.append(ac.get("content", ""))

    # Fallback if workflow_state doesn't have AC details
    if not completed_acs and not pending_acs and acs:
        completed_count = workflow.get("completed_count", 0)
        completed_acs = acs[:completed_count]
        pending_acs = acs[completed_count:]

    messages = state.get("messages_processed", 0)
    duration = state.get("duration_seconds", 0)

    lines = [
        f"## Resuming Interrupted Session: {session_id}",
        f"**Goal**: {goal}",
        f"**Progress before interruption**: {messages} messages, {duration:.0f}s",
        "",
    ]

    if completed_acs:
        lines.append("### Completed Acceptance Criteria")
        for ac in completed_acs:
            lines.append(f"- [x] {ac}")
        lines.append("")

    if pending_acs:
        lines.append("### Remaining Acceptance Criteria (resume from here)")
        for ac in pending_acs:
            lines.append(f"- [ ] {ac}")
        lines.append("")

    lines.append("**Instructions**: Continue from where the previous session left off.")
    lines.append("Do NOT redo completed ACs. Start with the first remaining AC.")

    return "\n".join(lines)


@app.command("list")
def list_sessions(
    interrupted: Annotated[
        bool,
        typer.Option(
            "--interrupted",
            help="Show only interrupted sessions.",
        ),
    ] = False,
) -> None:
    """List workflow sessions.

    Examples:
        ouroboros workflow list --interrupted
    """
    try:
        sessions = _load_interrupted_sessions()
    except Exception as e:
        print_error(f"Failed to load sessions: {e}")
        raise typer.Exit(1) from e

    if not sessions:
        print_info("No interrupted sessions found.")
        return

    table = Table(title="Interrupted Sessions")
    table.add_column("Session ID", style="cyan")
    table.add_column("Goal", style="white", max_width=50)
    table.add_column("Messages", style="yellow", justify="right")
    table.add_column("Interrupted At", style="dim")

    for s in sessions:
        table.add_row(
            s["session_id"],
            s["goal"][:50],
            str(s["messages"]),
            s["timestamp"][:19] if s["timestamp"] else "",
        )

    console.print(table)
    console.print(f"\n[blue]Resume with: ouroboros workflow resume <session_id>[/blue]")


@app.command()
def resume(
    session_id: Annotated[
        str,
        typer.Argument(help="Session ID to resume (or 'latest')."),
    ] = "latest",
) -> None:
    """Resume an interrupted workflow session.

    Examples:
        ouroboros workflow resume latest
        ouroboros workflow resume sess-abc-123
    """
    try:
        sessions = _load_interrupted_sessions()
    except Exception as e:
        print_error(f"Failed to load sessions: {e}")
        raise typer.Exit(1) from e

    if not sessions:
        print_info("No interrupted sessions to resume.")
        return

    # Resolve 'latest'
    target_id = session_id
    if target_id == "latest":
        target_id = sessions[0]["session_id"]
        print_info(f"Resuming latest interrupted session: {target_id}")

    # Find the session
    target = next((s for s in sessions if s["session_id"] == target_id), None)
    if not target:
        print_error(f"Session not found: {target_id}")
        print_info("Use 'ouroboros workflow list --interrupted' to see available sessions.")
        raise typer.Exit(1)

    # Build resume context
    context = _build_resume_context(target_id)
    if not context:
        print_error(f"Could not build resume context for session {target_id}")
        print_info("The checkpoint may have been lost. Try re-running with 'ooo run'.")
        raise typer.Exit(1)

    # Display resume info
    print_success(f"Session {target_id} ready to resume")
    console.print(f"  Goal: {target['goal'][:80]}")
    console.print(f"  Messages processed: {target['messages']}")
    console.print()

    # Output the context summary for use in prompts
    console.print("[bold]Resume Context (inject into next execution):[/bold]")
    console.print(context)
    console.print()
    console.print("[blue]📍 Next: Run 'ooo run' with this session's seed to continue[/blue]")
    console.print(f"[blue]   Pass session_id={target_id} to resume from checkpoint[/blue]")


__all__ = ["app"]
