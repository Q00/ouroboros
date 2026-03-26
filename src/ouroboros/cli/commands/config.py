"""Config command group for Ouroboros.

Manage configuration settings and provider setup.
"""

from __future__ import annotations

from pathlib import Path
import shutil
from typing import Annotated

import typer
import yaml

from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error, print_info, print_success, print_warning
from ouroboros.cli.formatters.tables import create_key_value_table, print_table

app = typer.Typer(
    name="config",
    help="Manage Ouroboros configuration.",
    no_args_is_help=True,
)

_SUPPORTED_BACKENDS = ("claude", "codex")


def _load_config() -> tuple[dict, Path]:
    """Load config.yaml and return (dict, path)."""
    from ouroboros.config.models import get_config_dir

    config_path = get_config_dir() / "config.yaml"
    if not config_path.exists():
        print_error(f"Config not found: {config_path}\nRun [bold]ouroboros setup[/] first.")
        raise typer.Exit(1)
    return yaml.safe_load(config_path.read_text()) or {}, config_path


def _save_config(data: dict, path: Path) -> None:
    """Write config dict back to YAML."""
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


def _resolve_cli_path(data: dict) -> str | None:
    """Return the active CLI path based on the current runtime backend."""
    backend = data.get("orchestrator", {}).get("runtime_backend", "claude")
    if backend == "codex":
        return data.get("orchestrator", {}).get("codex_cli_path")
    return data.get("orchestrator", {}).get("cli_path")


def _resolve_db_path(data: dict, config_path: Path) -> str:
    """Return the actual database path from config or default."""
    db_path = data.get("persistence", {}).get("database_path")
    if db_path:
        return str(db_path)
    return str(config_path.parent / "ouroboros.db")


@app.command()
def show(
    section: Annotated[
        str | None,
        typer.Argument(help="Configuration section to display (e.g., 'orchestrator')."),
    ] = None,
) -> None:
    """Display current configuration.

    Shows all configuration if no section specified.
    """
    data, config_path = _load_config()

    if section:
        section_data = data.get(section)
        if section_data is None:
            print_error(f"Section '{section}' not found in config.")
            raise typer.Exit(1)
        if isinstance(section_data, dict):
            table = create_key_value_table(
                {k: str(v) for k, v in section_data.items()},
                f"Config: {section}",
            )
            print_table(table)
        else:
            console.print(f"[cyan]{section}[/] = {section_data}")
    else:
        config_summary = {
            "config_path": str(config_path),
            "runtime_backend": data.get("orchestrator", {}).get("runtime_backend", "?"),
            "llm_backend": data.get("llm", {}).get("backend", "?"),
            "cli_path": _resolve_cli_path(data) or "?",
            "database": _resolve_db_path(data, config_path),
            "log_level": data.get("logging", {}).get("level", "info"),
        }
        table = create_key_value_table(config_summary, "Current Configuration")
        print_table(table)


@app.command()
def backend(
    new_backend: Annotated[
        str | None,
        typer.Argument(help="Backend to switch to (claude, codex)."),
    ] = None,
) -> None:
    """Show or switch the runtime backend.

    Without arguments, shows the current backend.
    With an argument, switches to the specified backend.
    Delegates to the full setup flow to ensure all side effects
    (MCP registration, Codex artifacts) are applied consistently.

    [dim]Examples:[/dim]
    [dim]    ouroboros config backend           # show current[/dim]
    [dim]    ouroboros config backend codex     # switch to Codex[/dim]
    [dim]    ouroboros config backend claude    # switch to Claude Code[/dim]
    """
    data, config_path = _load_config()
    current = data.get("orchestrator", {}).get("runtime_backend", "unknown")

    if new_backend is None:
        # Show current backend
        console.print(f"\n[bold]Current backend:[/bold] [cyan]{current}[/cyan]")
        cli_path = _resolve_cli_path(data)
        if cli_path:
            console.print(f"[bold]CLI path:[/bold]        [dim]{cli_path}[/dim]")
        console.print("\n[dim]Switch with: ouroboros config backend <claude|codex>[/dim]\n")
        return

    # Validate
    new_backend = new_backend.lower()
    if new_backend not in _SUPPORTED_BACKENDS:
        print_error(
            f"Unsupported backend: {new_backend}\nSupported: {', '.join(_SUPPORTED_BACKENDS)}"
        )
        raise typer.Exit(1)

    if new_backend == current:
        print_info(f"Already using {new_backend}.")
        return

    # Detect CLI path
    cli_name = "claude" if new_backend == "claude" else "codex"
    cli_path = shutil.which(cli_name)
    if not cli_path:
        print_error(f"{cli_name} CLI not found in PATH.\nInstall it first, then retry.")
        raise typer.Exit(1)

    # Delegate to the full setup flow for the chosen backend.
    # This ensures all side effects (MCP registration, Codex artifacts,
    # config writes) are applied consistently — no partial state.
    # Suppress setup's verbose output — we show a clean summary instead.
    from ouroboros.cli.commands.setup import _setup_claude, _setup_codex

    prev_quiet = console.quiet
    try:
        console.quiet = True
        if new_backend == "claude":
            _setup_claude(cli_path)
        elif new_backend == "codex":
            _setup_codex(cli_path)
    finally:
        console.quiet = prev_quiet

    print_success(f"Switched backend: [bold]{current}[/] → [bold]{new_backend}[/]")
    console.print(f"[dim]CLI: {cli_path}[/dim]\n")


@app.command()
def init() -> None:
    """Initialize Ouroboros configuration.

    Creates default configuration files if they don't exist.
    """
    from ouroboros.config.loader import create_default_config, ensure_config_dir

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"
    if config_path.exists():
        print_warning(f"Config already exists: {config_path}")
        return
    create_default_config(config_dir)
    print_success(f"Created config at {config_path}")


@app.command("set")
def set_value(
    key: Annotated[str, typer.Argument(help="Configuration key (dot notation).")],
    value: Annotated[str, typer.Argument(help="Value to set.")],
) -> None:
    """Set a configuration value.

    Use dot notation for nested keys (e.g., orchestrator.runtime_backend).
    Values are validated by reloading the full config after writing.

    [dim]Examples:[/dim]
    [dim]    ouroboros config set logging.level debug[/dim]
    [dim]    ouroboros config set orchestrator.runtime_backend codex[/dim]
    """
    data, config_path = _load_config()

    # Navigate dot notation
    keys = key.split(".")
    target = data
    for k in keys[:-1]:
        target = target.setdefault(k, {})
        if not isinstance(target, dict):
            print_error(f"Cannot set nested key: {key} ('{k}' is not a section)")
            raise typer.Exit(1)

    old_value = target.get(keys[-1])

    # Infer type from existing value to avoid string/int/bool mismatches
    parsed_value: str | int | float | bool = value
    if old_value is not None:
        if isinstance(old_value, bool):
            parsed_value = value.lower() in ("true", "1", "yes")
        elif isinstance(old_value, int):
            try:
                parsed_value = int(value)
            except ValueError:
                pass
        elif isinstance(old_value, float):
            try:
                parsed_value = float(value)
            except ValueError:
                pass

    target[keys[-1]] = parsed_value
    _save_config(data, config_path)

    # Validate the written config loads without errors
    try:
        from ouroboros.config.loader import load_config

        load_config()
    except Exception as exc:
        # Rollback: restore old value or remove key
        if old_value is not None:
            target[keys[-1]] = old_value
        else:
            del target[keys[-1]]
        _save_config(data, config_path)
        print_error(f"Invalid value — rolled back.\n{exc}")
        raise typer.Exit(1) from None

    if old_value is not None:
        print_success(f"{key}: {old_value} → {parsed_value}")
    else:
        print_success(f"{key}: {parsed_value}")


@app.command()
def validate() -> None:
    """Validate current configuration.

    Checks configuration files for errors and missing required values.
    Exits with status 1 if issues are found (scriptable).
    """
    data, config_path = _load_config()

    issues: list[str] = []

    # Check runtime backend
    backend_val = data.get("orchestrator", {}).get("runtime_backend")
    if not backend_val:
        issues.append("orchestrator.runtime_backend is not set")
    elif backend_val not in _SUPPORTED_BACKENDS:
        issues.append(f"orchestrator.runtime_backend '{backend_val}' is not supported")

    # Check CLI path exists
    if backend_val == "claude":
        cli = data.get("orchestrator", {}).get("cli_path")
        if cli and not Path(cli).exists():
            issues.append(f"Claude CLI path does not exist: {cli}")
    elif backend_val == "codex":
        cli = data.get("orchestrator", {}).get("codex_cli_path")
        if cli and not Path(cli).exists():
            issues.append(f"Codex CLI path does not exist: {cli}")

    # Try loading config through the validated schema
    try:
        from ouroboros.config.loader import load_config

        load_config()
    except Exception as exc:
        issues.append(f"Schema validation failed: {exc}")

    if issues:
        console.print("\n[bold red]Issues found:[/bold red]")
        for issue in issues:
            console.print(f"  [red]![/red] {issue}")
        console.print()
        raise typer.Exit(1)

    print_success("Configuration is valid.")


__all__ = ["app"]
