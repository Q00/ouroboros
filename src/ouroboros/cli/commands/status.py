"""Status command group for Ouroboros.

Check system status and execution history.
"""

import asyncio
import json
import os
from pathlib import Path
import shutil
from typing import Annotated, Any

import typer
import yaml

from ouroboros.auto.state import AutoPhase, AutoStore
from ouroboros.backends import get_backend_capability, resolve_runtime_backend_name
from ouroboros.cli.commands.config import _load_config, _resolve_db_path
from ouroboros.cli.formatters.panels import print_error, print_info
from ouroboros.cli.formatters.tables import create_status_table, print_table
from ouroboros.config.loader import load_config
from ouroboros.mcp.tools.projection_handlers import ProjectionQueryHandler

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


@app.command(name="run")
def run_projection(
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
    """Build a read-only Run/Stage/Step projection from persisted events."""

    arguments: dict[str, Any] = {}
    if session_id is not None:
        arguments["session_id"] = session_id
    if execution_id is not None:
        arguments["execution_id"] = execution_id
    if seed_id is not None:
        arguments["seed_id"] = seed_id
    if limit is not None:
        arguments["limit"] = limit

    result = asyncio.run(ProjectionQueryHandler().handle(arguments))
    if result.is_err:
        print_error(f"Run projection failed: {result.error}")
        raise typer.Exit(1)

    tool_result = result.value
    if json_output:
        typer.echo(json.dumps(tool_result.meta, indent=2, sort_keys=True))
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
    # Placeholder implementation with example data
    example_data = [
        {"name": "exec-001", "status": "complete"},
        {"name": "exec-002", "status": "running"},
        {"name": "exec-003", "status": "failed"},
    ]
    table = create_status_table(example_data, "Recent Executions")
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
    # Placeholder implementation
    print_info(f"Would show details for execution: {execution_id}")
    if events:
        print_info("Would include event history")


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
    "hermes": "OUROBOROS_HERMES_CLI_PATH",
    "kiro": "OUROBOROS_KIRO_CLI_PATH",
    "opencode": "OUROBOROS_OPENCODE_CLI_PATH",
}


def _health_row(name: str, status: str, detail: str | None = None) -> dict[str, str]:
    label = name if not detail else f"{name} — {detail}"
    return {"name": label, "status": status}


def _database_file_path(data: dict, config_path: Path) -> Path:
    configured = data.get("persistence", {}).get("database_path")
    if configured:
        path = Path(str(configured)).expanduser()
        if path.is_absolute():
            return path
        return config_path.parent / path
    return config_path.parent / "ouroboros.db"


def _candidate_cli_paths(backend: str, data: dict) -> list[str]:
    """Return CLI path candidates using the same precedence as runtime launchers."""
    candidates: list[str] = []
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

    if (
        not candidates
        and backend != "claude"
        and capability is not None
        and capability.cli_name
    ):
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
            if expanded.exists() and expanded.is_file() and os.access(expanded, os.X_OK):
                return _health_row("Runtime backend", "ok", f"{backend}: {expanded}")
            continue
        resolved = shutil.which(candidate)
        if resolved:
            return _health_row("Runtime backend", "ok", f"{backend}: {resolved}")

    expected = candidates[0] if candidates else (capability.cli_name if capability else backend)
    return _health_row("Runtime backend", "error", f"{backend} CLI not found: {expected}")


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
    normalized_backend = backend.strip().lower()
    capability = get_backend_capability(normalized_backend)
    canonical_backend = capability.name if capability is not None else normalized_backend
    if canonical_backend == "codex":
        if _codex_auth_file_exists():
            return _health_row("Credentials", "ok", "codex OAuth file present")
        if os.environ.get("OPENAI_API_KEY", "").strip():
            return _health_row("Credentials", "ok", "OPENAI_API_KEY present for codex")
        return _health_row(
            "Credentials", "error", "missing Codex OAuth auth.json or OPENAI_API_KEY"
        )

    provider = _credential_provider_for_backend(backend)
    if provider is None:
        return _health_row("Credentials", "ok", f"{backend} uses local CLI authentication")

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
    api_key = (
        str(provider_config.get("api_key", "")).strip() if isinstance(provider_config, dict) else ""
    )
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
            db_detail = _resolve_db_path(data, config_path)
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
                    checks.append(_health_row("Database", "ok", db_detail))
        except Exception as exc:
            checks.append(_health_row("Database", "error", str(exc)))

        checks.append(_check_runtime_backend(data))
        checks.append(_check_credentials(data, config_path))

    table = create_status_table(checks, "System Health")
    print_table(table)
    if any(check["status"] == "error" for check in checks):
        raise typer.Exit(1)


__all__ = ["app"]
