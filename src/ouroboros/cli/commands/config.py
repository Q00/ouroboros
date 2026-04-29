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

_VALID_BACKENDS = ("claude", "codex", "opencode", "hermes", "gemini")
_SWITCHABLE_BACKENDS = ("claude", "codex", "hermes", "gemini")


def _load_config() -> tuple[dict, Path]:
    """Load config.yaml and return (dict, path).

    All top-level sections that should be mappings are validated to be dicts.
    Structurally invalid sections (e.g. ``orchestrator: []``) produce a
    controlled error instead of crashing downstream commands.
    """
    from ouroboros.config.models import get_config_dir

    config_path = get_config_dir() / "config.yaml"
    if not config_path.exists():
        print_error(f"Config not found: {config_path}\nRun [bold]ouroboros setup[/] first.")
        raise typer.Exit(1)
    try:
        data = yaml.safe_load(config_path.read_text()) or {}
    except (yaml.YAMLError, OSError) as exc:
        print_error(f"Cannot parse {config_path}: {exc}")
        raise typer.Exit(1) from None
    if not isinstance(data, dict):
        print_error(
            f"Invalid config format in {config_path} (expected mapping, got {type(data).__name__})"
        )
        raise typer.Exit(1)

    # Guard against sections that should be dicts but aren't (e.g. orchestrator: [])
    _MAPPING_SECTIONS = (
        "orchestrator",
        "llm",
        "logging",
        "persistence",
        "economics",
        "clarification",
        "execution",
        "resilience",
        "evaluation",
        "consensus",
        "drift",
    )
    for section in _MAPPING_SECTIONS:
        val = data.get(section)
        if val is not None and not isinstance(val, dict):
            print_error(
                f"Invalid config section '{section}' in {config_path} "
                f"(expected mapping, got {type(val).__name__})"
            )
            raise typer.Exit(1)

    return data, config_path


def _save_config(data: dict, path: Path) -> None:
    """Write config dict back to YAML."""
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


def _resolve_cli_path(data: dict) -> str | None:
    """Return the active CLI path based on the current runtime backend."""
    backend = data.get("orchestrator", {}).get("runtime_backend", "claude")
    if backend == "codex":
        return data.get("orchestrator", {}).get("codex_cli_path")
    if backend == "opencode":
        return data.get("orchestrator", {}).get("opencode_cli_path")
    if backend == "hermes":
        return data.get("orchestrator", {}).get("hermes_cli_path")
    if backend == "gemini":
        return data.get("orchestrator", {}).get("gemini_cli_path")
    return data.get("orchestrator", {}).get("cli_path")


def _resolve_db_path(data: dict, config_path: Path) -> str:
    """Return a user-facing database path summary."""
    db_path = data.get("persistence", {}).get("database_path")
    if db_path:
        path = Path(db_path)
        if not path.is_absolute():
            resolved = config_path.parent / path
            return f"{db_path} ({resolved})"
        return str(path)

    resolved = config_path.parent / "ouroboros.db"
    return f"ouroboros.db ({resolved})"


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
        typer.Argument(help="Backend to switch to (claude, codex, hermes, gemini)."),
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
    [dim]    ouroboros config backend hermes    # switch to Hermes[/dim]
    [dim]    ouroboros config backend gemini    # switch to Gemini CLI[/dim]
    """
    data, config_path = _load_config()
    current = data.get("orchestrator", {}).get("runtime_backend", "unknown")

    if new_backend is None:
        # Show current backend
        console.print(f"\n[bold]Current backend:[/bold] [cyan]{current}[/cyan]")
        cli_path = _resolve_cli_path(data)
        if cli_path:
            console.print(f"[bold]CLI path:[/bold]        [dim]{cli_path}[/dim]")
        console.print(
            "\n[dim]Switch with: ouroboros config backend <claude|codex|hermes|gemini>[/dim]\n"
        )
        return

    # Validate
    new_backend = new_backend.lower()
    if new_backend not in _SWITCHABLE_BACKENDS:
        print_error(
            f"Unsupported backend for switching: {new_backend}\n"
            f"Switchable backends: {', '.join(_SWITCHABLE_BACKENDS)}\n"
            "For opencode, edit config manually or run [bold]ouroboros setup[/]."
        )
        raise typer.Exit(1)

    if new_backend == current:
        print_info(f"Already using {new_backend}.")
        return

    # Detect CLI path. For backends that expose an env-var or persisted
    # config path (gemini), honor those before falling back to PATH so users
    # with explicit-path installs can still switch via the CLI.
    cli_name = {
        "claude": "claude",
        "codex": "codex",
        "hermes": "hermes",
        "gemini": "gemini",
    }[new_backend]
    cli_path = None
    if new_backend == "gemini":
        from ouroboros.config import get_gemini_cli_path

        cli_path = get_gemini_cli_path()
    if not cli_path:
        cli_path = shutil.which(cli_name)
    if not cli_path:
        if new_backend == "gemini":
            print_error(
                "gemini CLI not found.\n"
                "Set OUROBOROS_GEMINI_CLI_PATH, configure orchestrator.gemini_cli_path "
                "in config.yaml, or install gemini on PATH and retry."
            )
        else:
            print_error(f"{cli_name} CLI not found in PATH.\nInstall it first, then retry.")
        raise typer.Exit(1)

    # Delegate to the full setup flow for the chosen backend.
    # This ensures all side effects (MCP registration, Codex artifacts,
    # config writes) are applied consistently — no partial state.
    # Suppress setup output; detect non-exception failures by monkey-patching
    # print_error to set a flag.
    from ouroboros.cli.commands import setup as setup_mod
    from ouroboros.cli.commands.setup import (
        _setup_claude,
        _setup_codex,
        _setup_gemini,
        _setup_hermes,
    )

    _setup_had_errors = False
    _orig_print_error = setup_mod.print_error

    def _tracking_print_error(msg: str) -> None:
        nonlocal _setup_had_errors
        _setup_had_errors = True
        _orig_print_error(msg)

    prev_quiet = console.quiet
    setup_failed = False
    try:
        console.quiet = True
        setup_mod.print_error = _tracking_print_error  # type: ignore[assignment]
        if new_backend == "claude":
            _setup_claude(cli_path)
        elif new_backend == "codex":
            _setup_codex(cli_path)
        elif new_backend == "hermes":
            _setup_hermes(cli_path)
        elif new_backend == "gemini":
            _setup_gemini(cli_path)
    except Exception as exc:
        setup_failed = True
        console.quiet = prev_quiet
        print_warning(f"Backend config updated but setup steps failed: {exc}")
        print_info("Run [bold]ouroboros setup[/] to complete configuration.")
    finally:
        console.quiet = prev_quiet
        setup_mod.print_error = _orig_print_error  # type: ignore[assignment]

    if setup_failed:
        pass  # Already warned above
    elif _setup_had_errors:
        print_warning("Backend switched but some setup steps had issues.")
        print_info("Run [bold]ouroboros setup[/] to verify configuration.")
    else:
        print_success(f"Switched backend: [bold]{current}[/] → [bold]{new_backend}[/]")
        console.print(f"[dim]CLI: {cli_path}[/dim]\n")


@app.command()
def init() -> None:
    """Initialize Ouroboros configuration.

    Creates default configuration files if they don't exist.
    Only creates missing files — never overwrites existing ones.
    """
    from ouroboros.config.loader import create_default_config, ensure_config_dir

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"
    credentials_path = config_dir / "credentials.yaml"
    if config_path.exists() and credentials_path.exists():
        print_info(f"Config already initialized at {config_dir}")
        return

    has_config = config_path.exists()
    has_credentials = credentials_path.exists()

    if not has_config and not has_credentials:
        # Fresh init — create both files
        create_default_config(config_dir, overwrite=False)
    else:
        # Partial init — only create the missing file(s)
        from ouroboros.config.models import get_default_config, get_default_credentials

        if not has_config:
            default_config = get_default_config()
            config_dict = default_config.model_dump(mode="json")
            config_path.write_text(
                yaml.dump(config_dict, default_flow_style=False, sort_keys=False)
            )
        if not has_credentials:
            default_credentials = get_default_credentials()
            cred_dict = default_credentials.model_dump(mode="json")
            credentials_path.write_text(
                yaml.dump(cred_dict, default_flow_style=False, sort_keys=False)
            )
            import os
            import stat

            os.chmod(credentials_path, stat.S_IRUSR | stat.S_IWUSR)

    print_success(f"Initialized config at {config_dir}")


def _validate_key_path(keys: list[str]) -> str | None:
    """Validate that a dot-notation key path matches the config schema.

    Returns an error message if the key is invalid, or None if valid.
    """
    from ouroboros.config.models import OuroborosConfig

    model = OuroborosConfig
    for i, k in enumerate(keys):
        fields = model.model_fields
        if k not in fields:
            path = ".".join(keys[: i + 1])
            valid = ", ".join(sorted(fields.keys()))
            return f"Unknown config key '{path}'. Valid keys at this level: {valid}"
        field_info = fields[k]
        # If not the last key, drill into the sub-model
        if i < len(keys) - 1:
            annotation = field_info.annotation
            # Unwrap Optional, etc.
            origin = getattr(annotation, "__origin__", None)
            if origin is not None:
                # Not a plain model type — can't drill further
                break
            if isinstance(annotation, type) and hasattr(annotation, "model_fields"):
                model = annotation
            else:
                break
    return None


@app.command("set")
def set_value(
    key: Annotated[str, typer.Argument(help="Configuration key (dot notation).")],
    value: Annotated[str, typer.Argument(help="Value to set.")],
) -> None:
    """Set a configuration value.

    Use dot notation for nested keys (e.g., orchestrator.runtime_backend).
    Keys are validated against the config schema before writing.

    [dim]Examples:[/dim]
    [dim]    ouroboros config set logging.level debug[/dim]
    [dim]    ouroboros config set orchestrator.runtime_backend codex[/dim]
    """
    data, config_path = _load_config()

    # Validate key path against schema
    keys = key.split(".")
    error = _validate_key_path(keys)
    if error:
        print_error(error)
        raise typer.Exit(1)

    # Navigate dot notation
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
    elif backend_val not in _VALID_BACKENDS:
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
    elif backend_val == "opencode":
        cli = data.get("orchestrator", {}).get("opencode_cli_path")
        if cli and not Path(cli).exists():
            issues.append(f"OpenCode CLI path does not exist: {cli}")
    elif backend_val == "hermes":
        cli = data.get("orchestrator", {}).get("hermes_cli_path")
        if cli and not Path(cli).exists():
            issues.append(f"Hermes CLI path does not exist: {cli}")
    elif backend_val == "gemini":
        cli = data.get("orchestrator", {}).get("gemini_cli_path")
        if cli and not Path(cli).exists():
            issues.append(f"Gemini CLI path does not exist: {cli}")

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
