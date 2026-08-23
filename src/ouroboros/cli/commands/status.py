"""Status command group for Ouroboros.

Check system status and execution history.
"""

import asyncio
import json
import os
from pathlib import Path
import shutil
from typing import Annotated, Any

from rich.markup import escape
from rich.table import Table
from rich.text import Text
import typer
import yaml

from ouroboros.auto.intent_guard import diagnose_auto_pipeline_state
from ouroboros.auto.state import AutoPhase, AutoStore
from ouroboros.backends import (
    get_backend_capability,
    resolve_llm_backend_name,
    resolve_runtime_backend_name,
)
from ouroboros.cli.commands.config import (
    _database_file_path,
    _load_config,
)
from ouroboros.cli.formatters.panels import print_error, print_info
from ouroboros.cli.formatters.tables import (
    create_key_value_table,
    create_status_table,
    create_table,
    print_table,
)
from ouroboros.config.loader import get_zcode_cli_path, load_config
from ouroboros.config.models import resolve_event_store_path
from ouroboros.config.zcode_model_config import inspect_zcode_model_config
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.project_status_handler import ProjectStatusHandler
from ouroboros.mcp.tools.projection_handlers import ProjectionQueryHandler
from ouroboros.persistence.event_store import EventStore, sqlite_database_url
from ouroboros.zcode_cli_launcher import resolve_zcode_command_prefix

app = typer.Typer(
    name="status",
    help="Check Ouroboros system status.",
    no_args_is_help=True,
)


def _format_auto_status(state) -> str:
    """Render a unified auto + ralph status block as plain text.

    Pinned by the snapshot test in ``tests/integration/auto/test_status_unified.py``.
    Each line is intentionally compact (one fact per line) so a human can grep
    the output and a Cucumber-style assertion can match it line-by-line. Layout
    mirrors :py:meth:`SessionStatusHandler._handle_auto_session` so the CLI and
    MCP surfaces never disagree.
    """
    lines = [
        "Auto status",
        "===========",
        f"Auto session: {state.auto_session_id}",
        f"Phase: {state.phase.value}",
    ]

    is_terminal = state.phase in {
        AutoPhase.COMPLETE,
        AutoPhase.BLOCKED,
        AutoPhase.FAILED,
    }
    lines.append(f"Terminal: {is_terminal}")
    lines.append(f"Last progress: {state.last_progress_message}")
    if state.pending_question:
        lines.append("Pending question:")
        for line in str(state.pending_question).strip().splitlines() or [""]:
            lines.append(f"  {line}")
    if state.auto_answer_log:
        recent = state.auto_answer_log[-3:]
        lines.append(f"Recent auto answers (last {len(recent)}):")
        for entry in recent:
            round_value = entry.get("round", "?")
            source = entry.get("source", "?")
            question = str(entry.get("question", "")).strip()
            answer = str(entry.get("answer", "")).strip()
            lines.append(f"  round {round_value} [{source}] Q: {question}")
            lines.append(f"    A: {answer}")

    is_gap_window = (
        state.phase is AutoPhase.RALPH_HANDOFF
        and state.ralph_lineage_id is not None
        and state.ralph_job_id is None
        and state.ralph_dispatch_mode not in {"plugin", "plugin_pending"}
    )
    is_plugin_pending = (
        state.phase is AutoPhase.RALPH_HANDOFF and state.ralph_dispatch_mode == "plugin_pending"
    )

    if state.ralph_dispatch_mode == "plugin":
        lines.append("Ralph (plugin):")
        lines.append("  dispatch_mode: plugin")
        lines.append("  guidance: ralph delegated to OpenCode Task widget; follow that lifecycle")
    elif is_plugin_pending:
        lines.append("Ralph (plugin pending):")
        lines.append("  dispatch_mode: plugin_pending")
        lines.append(f"  lineage_id: {state.ralph_lineage_id}")
        lines.append("  status: interrupted plugin dispatch")
        lines.append("  guidance: plugin dispatch unconfirmed; resume will retry or block")
    elif state.ralph_job_id is not None:
        lines.append("Ralph (job):")
        lines.append(f"  job_id: {state.ralph_job_id}")
        lines.append(f"  lineage_id: {state.ralph_lineage_id}")
        lines.append(f"  status: {state.ralph_job_status}")
        lines.append(f"  current_generation: {state.ralph_current_generation}")
        lines.append(f"  stop_reason: {state.ralph_stop_reason}")
    elif is_gap_window:
        lines.append("Ralph (pending):")
        lines.append(f"  lineage_id: {state.ralph_lineage_id}")
        lines.append("  pending: starting ralph")

    if state.last_error:
        lines.append(f"Blocker: {state.last_error}")
    lines.extend(diagnose_auto_pipeline_state(state).render_lines())

    return "\n".join(lines) + "\n"


@app.command()
def auto(
    auto_session_id: Annotated[
        str,
        typer.Argument(help="Auto session id to inspect (auto_<hex>)."),
    ],
) -> None:
    """Show unified auto + ralph status for an ``ooo auto`` session.

    Q00/ouroboros#782 — renders both the auto pipeline phase and the ralph
    sub-block in a single human-readable view.
    """
    if not auto_session_id.startswith("auto_"):
        print_error("auto_session_id must start with auto_")
        raise typer.Exit(1)
    try:
        state = AutoStore().load(auto_session_id)
    except ValueError as exc:
        print_error(f"Auto status failed: {exc}")
        raise typer.Exit(1) from exc
    typer.echo(_format_auto_status(state), nl=False)


# Exit codes for `ouroboros status run`. These mirror Wave-1 #946 S2:
#   * ``0`` — projection rendered successfully.
#   * ``2`` — run anchor (run_id / execution_id / session_id) is unknown.
#   * ``64`` — malformed input (missing or conflicting selectors).
# Any other handler failure surfaces as exit code ``1`` so existing callers
# that already special-case generic failure keep working.
_STATUS_RUN_EXIT_OK = 0
_STATUS_RUN_EXIT_GENERIC_ERROR = 1
_STATUS_RUN_EXIT_UNKNOWN_RUN = 2
_STATUS_RUN_EXIT_MALFORMED_INPUT = 64
_STATUS_EXECUTION_EVENT_PAGE_SIZE = 500
_ROOT_EXECUTION_EVENT_STATUS = {
    "execution.completed": "complete",
    "execution.failed": "failed",
    "execution.plan.created": "running",
    "execution.run.configuration_resolved": "running",
    "execution.started": "running",
    "workflow.progress.updated": "running",
}
_ABSORBING_EXECUTION_EVENT_TYPES = {
    "execution.completed",
    "execution.failed",
}
_ABSORBING_TERMINAL_STATUSES = {
    "cancelled",
    "complete",
    "completed",
    "error",
    "failed",
    "failure",
    "success",
    "succeeded",
}
_TERMINAL_STATUS_ALIASES = {
    "active": "running",
    "blocked": "blocked",
    "cancelled": "cancelled",
    "complete": "complete",
    "completed": "complete",
    "error": "failed",
    "failed": "failed",
    "failure": "failed",
    "paused": "paused",
    "pending": "pending",
    "running": "running",
    "success": "complete",
    "succeeded": "complete",
    "waiting": "waiting",
}


def _is_unknown_run_error(message: str) -> bool:
    lowered = message.lower()
    return "no events found" in lowered


def _configured_event_store_path() -> Path:
    db_path = resolve_event_store_path()
    if not db_path.exists():
        raise FileNotFoundError(f"configured database does not exist: {db_path}")
    if not db_path.is_file():
        raise OSError(f"configured database is not a file: {db_path}")
    return db_path


async def _recent_execution_events(
    db_path: Path,
    *,
    execution_limit: int | None,
) -> list[tuple[str, BaseEvent]]:
    store = EventStore(sqlite_database_url(db_path), read_only=True)
    await store.initialize(create_schema=False)
    try:
        persisted = await store.query_latest_events_per_aggregate(
            aggregate_type="execution",
            event_types={"execution.terminal", *_ROOT_EXECUTION_EVENT_STATUS},
            preferred_event_type="execution.terminal",
            preferred_event_types=_ABSORBING_EXECUTION_EVENT_TYPES,
            preferred_event_statuses=_ABSORBING_TERMINAL_STATUSES,
            preserve_session_candidates=True,
            # Session aggregates used by resumed executions must be collapsed
            # before applying the user-visible execution limit.
            limit=None,
        )
        snapshots = await store.get_session_activity_snapshots()
    finally:
        await store.close()

    execution_by_session = {
        snapshot.session_id: snapshot.execution_id
        for snapshot in snapshots
        if snapshot.execution_id
    }
    events_by_execution: dict[str, list[BaseEvent]] = {}
    for event in persisted:
        execution_id = execution_by_session.get(event.aggregate_id, event.aggregate_id)
        events_by_execution.setdefault(execution_id, []).append(event)
    latest_by_execution = {
        execution_id: latest
        for execution_id, events in events_by_execution.items()
        if (latest := _latest_execution_lifecycle(events)) is not None
    }
    ordered = sorted(
        latest_by_execution.items(),
        key=lambda item: (item[1].timestamp, item[1].id),
        reverse=True,
    )
    return ordered if execution_limit is None else ordered[:execution_limit]


def _latest_execution_lifecycle(events: list[BaseEvent]) -> BaseEvent | None:
    """Select current lifecycle across original and resumed session scopes."""
    latest_by_session: dict[str, BaseEvent] = {}
    for event in events:
        if _root_execution_status(event) is None:
            continue
        raw_session_id = event.data.get("session_id")
        session_id = (
            raw_session_id.strip()
            if isinstance(raw_session_id, str) and raw_session_id.strip()
            else event.aggregate_id
        )
        current = latest_by_session.get(session_id)
        should_replace = current is None
        if current is not None:
            current_absorbing = _root_execution_status(current) in {
                "cancelled",
                "complete",
                "failed",
            }
            event_absorbing = _root_execution_status(event) in {
                "cancelled",
                "complete",
                "failed",
            }
            should_replace = not current_absorbing and (
                event_absorbing or (event.timestamp, event.id) > (current.timestamp, current.id)
            )
        if should_replace:
            latest_by_session[session_id] = event
    return max(
        latest_by_session.values(),
        key=lambda event: (event.timestamp, event.id),
        default=None,
    )


async def _execution_events(
    db_path: Path,
    execution_id: str,
    *,
    include_all: bool,
) -> list[BaseEvent]:
    store = EventStore(sqlite_database_url(db_path), read_only=True)
    await store.initialize(create_schema=False)
    try:
        snapshots = await store.get_session_activity_snapshots()
        session_ids = [
            snapshot.session_id for snapshot in snapshots if snapshot.execution_id == execution_id
        ]
        if session_ids:
            related_pages = await asyncio.gather(
                *(
                    store.query_session_related_events(
                        session_id,
                        execution_id=execution_id,
                        limit=None,
                    )
                    for session_id in session_ids
                )
            )
            persisted = list({event.id: event for page in related_pages for event in page}.values())
        else:
            persisted = []
            offset = 0
            while True:
                page = await store.query_events(
                    aggregate_id=execution_id,
                    limit=_STATUS_EXECUTION_EVENT_PAGE_SIZE,
                    offset=offset,
                    aggregate_type="execution",
                )
                persisted.extend(page)
                if len(page) < _STATUS_EXECUTION_EVENT_PAGE_SIZE:
                    break
                offset += len(page)
    finally:
        await store.close()
    persisted.sort(key=lambda event: (event.timestamp, event.id), reverse=True)
    if include_all:
        return persisted
    latest = _latest_execution_lifecycle(persisted)
    return [latest] if latest is not None else []


async def _persisted_event_count(db_path: Path) -> int:
    """Validate the store read-only and return its persisted event count.

    ``create_schema=False`` keeps this a pure probe: a missing or corrupt
    store fails cleanly and nothing is ever created or migrated (#1813).
    """
    store = EventStore(sqlite_database_url(db_path), read_only=True)
    await store.initialize(create_schema=False)
    try:
        # The one-row query validates the full column shape (a bare COUNT
        # would accept any table that happens to be named `events`).
        await store.query_events(limit=1)
        return await store.count_events()
    finally:
        await store.close()


def _root_execution_status(event: BaseEvent) -> str | None:
    if event.type == "execution.terminal":
        explicit = event.data.get("status")
        if isinstance(explicit, str) and explicit.strip():
            return _TERMINAL_STATUS_ALIASES.get(explicit.strip().casefold(), "unknown")
        return "unknown"
    return _ROOT_EXECUTION_EVENT_STATUS.get(event.type)


@app.command(name="run")
def run_projection(
    run_id: Annotated[
        str | None,
        typer.Argument(
            metavar="[RUN_ID]",
            help=(
                "Optional run anchor (execution aggregate ID). Equivalent to "
                "passing --execution-id; mutually exclusive with --session-id."
            ),
        ),
    ] = None,
    session_id: Annotated[
        str | None,
        typer.Option("--session-id", help="Optional orchestrator session ID to project."),
    ] = None,
    execution_id: Annotated[
        str | None,
        typer.Option("--execution-id", help="Optional execution aggregate ID to project."),
    ] = None,
    seed_id: Annotated[
        str | None,
        typer.Option("--seed-id", help="Optional seed ID override for projection labels."),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Optional event count safety cap."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable projection JSON."),
    ] = False,
) -> None:
    """Build a read-only Run/Stage/Step projection from persisted events.

    The CLI is a thin surface over ``ouroboros_query_projection``: the same
    run anchor returns byte-identical JSON between the MCP query and this
    command when ``--json`` is set. Exit codes follow the Wave-1 #946 S2
    convention (``0`` ok, ``2`` unknown run, ``64`` malformed input).
    """

    if run_id is not None:
        if execution_id is not None and execution_id != run_id:
            print_error(
                "Run projection failed: RUN_ID and --execution-id refer to "
                "different anchors; provide only one."
            )
            raise typer.Exit(_STATUS_RUN_EXIT_MALFORMED_INPUT)
        if session_id is not None:
            print_error(
                "Run projection failed: RUN_ID positional cannot be combined "
                "with --session-id; pass either a run anchor or a session."
            )
            raise typer.Exit(_STATUS_RUN_EXIT_MALFORMED_INPUT)
        execution_id = run_id

    if session_id is None and execution_id is None:
        print_error(
            "Run projection failed: a RUN_ID (execution anchor), "
            "--execution-id, or --session-id is required."
        )
        raise typer.Exit(_STATUS_RUN_EXIT_MALFORMED_INPUT)

    arguments: dict[str, Any] = {}
    if session_id is not None:
        arguments["session_id"] = session_id
    if execution_id is not None:
        arguments["execution_id"] = execution_id
    if seed_id is not None:
        arguments["seed_id"] = seed_id
    if limit is not None:
        if limit <= 0:
            print_error("Run projection failed: limit must be a positive integer")
            raise typer.Exit(_STATUS_RUN_EXIT_MALFORMED_INPUT)
        arguments["limit"] = limit

    result = asyncio.run(ProjectionQueryHandler().handle(arguments))
    if result.is_err:
        message = str(result.error)
        print_error(f"Run projection failed: {message}")
        if _is_unknown_run_error(message):
            raise typer.Exit(_STATUS_RUN_EXIT_UNKNOWN_RUN)
        raise typer.Exit(_STATUS_RUN_EXIT_GENERIC_ERROR)

    tool_result = result.value
    if json_output:
        typer.echo(json.dumps(tool_result.meta, indent=2, sort_keys=True))
        return
    typer.echo(tool_result.text_content, nl=False)


@app.command(name="project")
def project_status(
    project_dir: Annotated[
        str | None,
        typer.Argument(
            metavar="[PROJECT_DIR]",
            help="Project or workspace directory. Defaults to the current directory.",
        ),
    ] = None,
    workspace: Annotated[
        str | None,
        typer.Option(
            "--workspace",
            help="Optional canonical project-relative workspace filter.",
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            help="Complete-run safety cap; the command never truncates silently.",
        ),
    ] = 100,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the exact MCP ProjectRecord JSON payload."),
    ] = False,
) -> None:
    """Show complete read-only run status for one project."""
    if limit <= 0:
        print_error("Project status failed: limit must be a positive integer")
        raise typer.Exit(_STATUS_RUN_EXIT_MALFORMED_INPUT)
    if workspace is not None and (not workspace or workspace != workspace.strip()):
        print_error("Project status failed: workspace must be a canonical relative path")
        raise typer.Exit(_STATUS_RUN_EXIT_MALFORMED_INPUT)

    arguments: dict[str, Any] = {
        "project_dir": project_dir if project_dir is not None else str(Path.cwd()),
        "limit": limit,
    }
    if workspace is not None:
        arguments["workspace"] = workspace

    result = asyncio.run(ProjectStatusHandler().handle(arguments))
    if result.is_err:
        print_error(f"Project status failed: {escape(str(result.error))}")
        raise typer.Exit(_STATUS_RUN_EXIT_GENERIC_ERROR)

    tool_result = result.value
    if json_output:
        if not isinstance(tool_result.structured_content, dict):
            print_error("Project status failed: MCP handler returned no ProjectRecord")
            raise typer.Exit(_STATUS_RUN_EXIT_GENERIC_ERROR)
        typer.echo(json.dumps(tool_result.structured_content, indent=2, sort_keys=True))
        return
    typer.echo(tool_result.text_content, nl=False)


@app.command()
def executions(
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Number of executions to show."),
    ] = 10,
    all_: Annotated[
        bool,
        typer.Option("--all", "-a", help="Show all executions."),
    ] = False,
) -> None:
    """List recent executions.

    Shows execution history with status information.
    """
    if limit <= 0:
        print_error("Execution status failed: limit must be a positive integer")
        raise typer.Exit(_STATUS_RUN_EXIT_MALFORMED_INPUT)
    try:
        persisted = asyncio.run(
            _recent_execution_events(
                _configured_event_store_path(),
                execution_limit=None if all_ else limit,
            )
        )
    except Exception as exc:
        print_error(f"Execution status failed: {escape(str(exc))}")
        raise typer.Exit(_STATUS_RUN_EXIT_GENERIC_ERROR) from exc

    rows = [
        {"name": execution_id, "status": _root_execution_status(event) or "unknown"}
        for execution_id, event in persisted
    ]
    if not all_:
        rows = rows[:limit]
    table = create_status_table(rows, "Recent Executions")
    print_table(table)

    if not all_:
        print_info(f"Showing last {limit} executions. Use --all to see more.")


@app.command()
def execution(
    execution_id: Annotated[
        str,
        typer.Argument(help="Execution ID to inspect."),
    ],
    events: Annotated[
        bool,
        typer.Option("--events", "-e", help="Show execution events."),
    ] = False,
) -> None:
    """Show details for a specific execution.

    Displays execution metadata, progress, and optionally events.
    """
    try:
        persisted = asyncio.run(
            _execution_events(
                _configured_event_store_path(),
                execution_id,
                include_all=True,
            )
        )
    except Exception as exc:
        print_error(f"Execution status failed: {escape(str(exc))}")
        raise typer.Exit(_STATUS_RUN_EXIT_GENERIC_ERROR) from exc
    latest_lifecycle = _latest_execution_lifecycle(persisted)
    if latest_lifecycle is None:
        print_error(
            f"Execution status failed: no persisted execution found: {escape(execution_id)}"
        )
        raise typer.Exit(_STATUS_RUN_EXIT_UNKNOWN_RUN)

    print_table(
        create_key_value_table(
            {
                "Execution ID": execution_id,
                "Status": _root_execution_status(latest_lifecycle) or "unknown",
                "Latest event": latest_lifecycle.type,
                "Updated": latest_lifecycle.timestamp.isoformat(),
            },
            "Execution Details",
        )
    )
    if events:
        table = create_table("Execution Events")
        table.add_column("Timestamp", no_wrap=True)
        table.add_column("Event", style="cyan")
        table.add_column("Status")
        for event in reversed(persisted):
            table.add_row(
                Text(event.timestamp.isoformat()),
                Text(event.type),
                Text(_root_execution_status(event) or ""),
            )
        print_table(table)


_CREDENTIAL_PROVIDER_BY_LLM_BACKEND = {
    "claude": "anthropic",
    "claude_code": "anthropic",
    "gemini": "google",
    "litellm": "openrouter",
    "openai": "openai",
    "openrouter": "openrouter",
}

_API_KEY_ENV_BY_PROVIDER = {
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

_CLI_PATH_ENV_BY_BACKEND = {
    "claude": "OUROBOROS_CLI_PATH",
    "codex": "OUROBOROS_CODEX_CLI_PATH",
    "copilot": "OUROBOROS_COPILOT_CLI_PATH",
    "gemini": "OUROBOROS_GEMINI_CLI_PATH",
    "goose": "OUROBOROS_GOOSE_CLI_PATH",
    "gjc": "OUROBOROS_GJC_CLI_PATH",
    "hermes": "OUROBOROS_HERMES_CLI_PATH",
    "kiro": "OUROBOROS_KIRO_CLI_PATH",
    "opencode": "OUROBOROS_OPENCODE_CLI_PATH",
}


def _health_row(name: str, status: str, detail: str | None = None) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail or ""}


def _health_status_text(status: str) -> Text:
    style_by_status = {
        "ok": "success",
        "warning": "warning",
        "error": "error",
    }
    return Text(status, style=style_by_status.get(status, ""))


def _health_table(checks: list[dict[str, str]]) -> Table:
    table = Table(title="System Health", border_style="blue", header_style="bold cyan")
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Status", justify="center", no_wrap=True)
    table.add_column("Detail", overflow="fold")

    for check in checks:
        table.add_row(
            check["name"],
            _health_status_text(check["status"]),
            check.get("detail", ""),
        )

    return table


def _print_health_details(checks: list[dict[str, str]]) -> None:
    """Emit copyable health details after the Rich table.

    The table is for scanning, but long CLI paths and config-file paths can be
    truncated by terminal width. These plain lines are the diagnostic source a
    user can copy into an issue or use to fix their local setup.
    """
    for check in checks:
        detail = check.get("detail", "")
        if detail:
            typer.echo(f"{check['name']}: {check['status']} - {detail}")


def _candidate_cli_paths(backend: str, data: dict) -> list[str]:
    """Return CLI path candidates using the same precedence as runtime launchers."""
    candidates: list[str] = []
    if backend == "zcode":
        zcode_path = get_zcode_cli_path()
        if zcode_path:
            candidates.append(zcode_path)
    env_key = _CLI_PATH_ENV_BY_BACKEND.get(backend)
    if env_key is not None:
        env_path = os.environ.get(env_key, "").strip()
        if env_path:
            candidates.append(str(Path(env_path).expanduser()))

    capability = get_backend_capability(backend)
    config_key = capability.cli_config_key if capability is not None else None
    configured_cli = None
    if config_key:
        configured_cli = data.get("orchestrator", {}).get(config_key)

    # Do not consult the config-file runtime backend here: env overrides may
    # select a different effective backend for this process, and that backend's
    # own configured CLI path still mirrors the runtime launcher contract.
    if configured_cli and configured_cli not in candidates:
        candidates.append(configured_cli)

    if not candidates and backend != "claude" and capability is not None and capability.cli_name:
        candidates.append(capability.cli_name)
    return candidates


def _effective_runtime_backend(data: dict) -> str:
    env_backend = os.environ.get("OUROBOROS_AGENT_RUNTIME", "").strip().lower()
    if env_backend:
        return env_backend
    env_runtime = os.environ.get("OUROBOROS_RUNTIME", "").strip().lower()
    if env_runtime:
        return env_runtime
    return str(data.get("orchestrator", {}).get("runtime_backend", "claude"))


def _check_runtime_backend(data: dict) -> dict[str, str]:
    try:
        backend = resolve_runtime_backend_name(_effective_runtime_backend(data))
    except ValueError as exc:
        return _health_row("Runtime backend", "error", str(exc))

    capability = get_backend_capability(backend)
    candidates = _candidate_cli_paths(backend, data)
    if backend == "claude" and not candidates:
        return _health_row("Runtime backend", "ok", "claude: SDK default")

    for candidate in candidates:
        if not candidate:
            continue
        expanded = Path(candidate).expanduser()
        if expanded.is_absolute() or len(expanded.parts) > 1:
            script = backend == "zcode" and expanded.suffix.lower() in {".cjs", ".js", ".mjs"}
            if (
                expanded.exists()
                and expanded.is_file()
                and (os.access(expanded, os.R_OK) if script else os.access(expanded, os.X_OK))
            ):
                if backend == "zcode":
                    try:
                        resolve_zcode_command_prefix(expanded)
                    except RuntimeError as exc:
                        return _health_row("Runtime backend", "error", str(exc))
                return _runtime_backend_health_row(backend, f"{backend}: {expanded}")
            continue
        resolved = shutil.which(candidate)
        if resolved:
            if backend == "zcode":
                try:
                    resolve_zcode_command_prefix(resolved)
                except RuntimeError as exc:
                    return _health_row("Runtime backend", "error", str(exc))
            return _runtime_backend_health_row(backend, f"{backend}: {resolved}")

    expected = candidates[0] if candidates else (capability.cli_name if capability else backend)
    return _health_row("Runtime backend", "error", f"{backend} CLI not found: {expected}")


def _runtime_backend_health_row(backend: str, detail: str) -> dict[str, str]:
    """Augment the zcode runtime row with the CLI's own model readiness.

    Finding the zcode executable is necessary but not sufficient: every
    headless invocation also needs ``model.main`` in ``~/.zcode/cli/config.json``
    or the vendor CLI exits with ``Model config is missing`` after Ouroboros
    has already launched it.
    """

    if backend != "zcode":
        return _health_row("Runtime backend", "ok", detail)
    zcode_model = inspect_zcode_model_config()
    if not zcode_model.ok:
        return _health_row(
            "Runtime backend",
            "warning",
            f"{detail}; model config: {zcode_model.detail}",
        )
    return _health_row("Runtime backend", "ok", detail)


def _credential_provider_for_backend(backend: str) -> str | None:
    normalized = backend.strip().lower()
    if normalized in _CREDENTIAL_PROVIDER_BY_LLM_BACKEND:
        return _CREDENTIAL_PROVIDER_BY_LLM_BACKEND[normalized]
    capability = get_backend_capability(normalized)
    if capability is None:
        return None
    return _CREDENTIAL_PROVIDER_BY_LLM_BACKEND.get(capability.name)


def _codex_auth_file_exists() -> bool:
    auth_base = os.environ.get("CODEX_HOME")
    if not auth_base:
        home = os.environ.get("HOME")
        if not home:
            return False
        auth_base = str(Path(home).expanduser() / ".codex")
    return (Path(auth_base).expanduser() / "auth.json").is_file()


def _provider_env_key_present(provider: str) -> bool:
    env_key = _API_KEY_ENV_BY_PROVIDER.get(provider)
    if not env_key:
        return False
    return bool(os.environ.get(env_key, "").strip())


def _effective_llm_backend(data: dict) -> str:
    env_backend = os.environ.get("OUROBOROS_LLM_BACKEND", "").strip().lower()
    if env_backend:
        return env_backend

    env_runtime = os.environ.get("OUROBOROS_RUNTIME", "").strip().lower()
    runtime_capability = get_backend_capability(env_runtime)
    if runtime_capability is not None and runtime_capability.supports_llm:
        if env_runtime == "claude_code":
            return "claude_code"
        return runtime_capability.name

    return str(data.get("llm", {}).get("backend", "claude_code"))


def _check_credentials(data: dict, config_path: Path) -> dict[str, str]:
    backend = _effective_llm_backend(data)
    try:
        canonical_backend = resolve_llm_backend_name(backend)
    except ValueError as exc:
        return _health_row("Credentials", "error", str(exc))

    if canonical_backend == "codex":
        if _codex_auth_file_exists():
            return _health_row("Credentials", "ok", "codex OAuth file present")
        if os.environ.get("OPENAI_API_KEY", "").strip():
            return _health_row("Credentials", "ok", "OPENAI_API_KEY present for codex")
        return _health_row(
            "Credentials", "error", "missing Codex OAuth auth.json or OPENAI_API_KEY"
        )

    provider = _credential_provider_for_backend(canonical_backend)
    if provider is None:
        if canonical_backend == "zcode":
            zcode_model = inspect_zcode_model_config()
            if not zcode_model.ok:
                return _health_row(
                    "Credentials",
                    "warning",
                    f"zcode model config: {zcode_model.detail}",
                )
            return _health_row(
                "Credentials",
                "ok",
                f"zcode uses local CLI authentication; {zcode_model.detail}",
            )
        return _health_row(
            "Credentials", "ok", f"{canonical_backend} uses local CLI authentication"
        )

    if _provider_env_key_present(provider):
        return _health_row("Credentials", "ok", f"{_API_KEY_ENV_BY_PROVIDER[provider]} present")

    credentials_path = config_path.parent / "credentials.yaml"
    if not credentials_path.exists():
        return _health_row("Credentials", "error", f"missing {credentials_path} for {provider}")

    try:
        raw_credentials = yaml.safe_load(credentials_path.read_text())
        if raw_credentials is None:
            raw_credentials = {}
    except (OSError, yaml.YAMLError) as exc:
        return _health_row("Credentials", "error", f"cannot read credentials: {exc}")

    if not isinstance(raw_credentials, dict):
        return _health_row("Credentials", "error", "credentials.yaml must contain a mapping")
    providers = raw_credentials.get("providers", {})
    if not isinstance(providers, dict):
        return _health_row("Credentials", "error", "credentials providers must be a mapping")
    provider_config = providers.get(provider, {})
    if not isinstance(provider_config, dict):
        return _health_row(
            "Credentials", "error", f"credentials provider {provider} must be a mapping"
        )

    api_key = str(provider_config.get("api_key", "")).strip()
    if not api_key:
        return _health_row("Credentials", "warning", f"{provider} key is empty")
    if api_key.startswith("YOUR_") and api_key.endswith("_API_KEY"):
        return _health_row("Credentials", "warning", f"{provider} key is still a template value")
    return _health_row("Credentials", "ok", f"{provider} key present")


@app.command()
def health() -> None:
    """Check system health.

    Verifies configuration, database, runtime backend, and credentials.
    """
    checks: list[dict[str, str]] = []
    data: dict | None = None
    config_path: Path | None = None

    try:
        data, config_path = _load_config()
        load_config(config_path)
    except Exception as exc:
        checks.append(_health_row("Configuration", "error", str(exc)))
    else:
        checks.append(_health_row("Configuration", "ok", str(config_path)))

    if data is None or config_path is None:
        checks.extend(
            [
                _health_row("Database", "error", "configuration unavailable"),
                _health_row("Runtime backend", "error", "configuration unavailable"),
                _health_row("Credentials", "error", "configuration unavailable"),
            ]
        )
    else:
        try:
            db_path = _database_file_path(data, config_path)
            db_detail = str(db_path)
            if not db_path.exists():
                checks.append(
                    _health_row(
                        "Database", "warning", f"missing; will be created on first run: {db_detail}"
                    )
                )
            elif not db_path.is_file():
                checks.append(_health_row("Database", "error", f"not a file: {db_detail}"))
            else:
                try:
                    with db_path.open("rb"):
                        pass
                except OSError as exc:
                    checks.append(_health_row("Database", "error", f"not readable: {exc}"))
                else:
                    try:
                        event_count = asyncio.run(_persisted_event_count(db_path))
                    except Exception as exc:
                        checks.append(
                            _health_row("Database", "error", f"invalid event store: {exc}")
                        )
                    else:
                        checks.append(
                            _health_row(
                                "Database",
                                "ok",
                                f"{db_detail} ({event_count} events persisted)",
                            )
                        )
        except Exception as exc:
            checks.append(_health_row("Database", "error", str(exc)))

        checks.append(_check_runtime_backend(data))
        checks.append(_check_credentials(data, config_path))

    print_table(_health_table(checks))
    _print_health_details(checks)
    if any(check["status"] == "error" for check in checks):
        raise typer.Exit(1)


__all__ = ["app"]
