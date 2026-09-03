"""Host-runtime launcher reconciliation used by the setup command."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
import json
import os
from pathlib import Path
import tomllib
from typing import Any

import yaml

from ouroboros.cli.formatters.panels import print_error, print_info, print_success


def _runtime_override(
    entry: dict[str, object],
    command_key: str,
    env_key: str,
    args_key: str | None = None,
) -> str | None:
    command = entry.get(args_key) if args_key is not None else entry.get(command_key)
    if isinstance(command, list):
        for index, arg in enumerate(command):
            if not isinstance(arg, str):
                continue
            if arg == "--runtime" and index + 1 < len(command):
                value = command[index + 1]
                if isinstance(value, str) and value.strip().lower() != "host":
                    return f"--runtime {value}"
            elif arg.startswith("--runtime=") and arg.partition("=")[2].strip().lower() != "host":
                return arg
    env = entry.get(env_key)
    if isinstance(env, dict):
        for key in ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_RUNTIME"):
            value = env.get(key)
            if isinstance(value, str) and value.strip() and value.strip().lower() != "host":
                return f"{key}={value}"
    return None


def _is_known_runtime_selector(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() in {
        "agy",
        "claude",
        "claude-cli",
        "claude_code",
        "codex",
        "copilot",
        "gemini",
        "gemini-cli",
        "gjc",
        "goose",
        "hermes",
        "kiro",
        "opencode",
        "pi",
        "omp",
    }


def _mcp_args_with_optional_selectors(args: Sequence[object], base: Sequence[str]) -> bool:
    values = tuple(arg for arg in args if isinstance(arg, str))
    expected = tuple(base)
    if len(values) != len(args):
        return False
    if values == expected:
        return True
    if len(values) != len(expected) + 4 or values[: len(expected)] != expected:
        return False
    return (
        values[len(expected)] == "--runtime"
        and _is_known_runtime_selector(values[len(expected) + 1])
        and values[len(expected) + 2] == "--llm-backend"
        and _is_known_runtime_selector(values[len(expected) + 3])
    )


def _is_known_ouroboros_launcher(
    setup: Any,
    command: object,
    args: object,
) -> bool:
    """Recognize only setup-generated Ouroboros MCP command shapes.

    Executable basenames alone are not ownership evidence: ``python``, ``uv``
    and ``uvx`` are common user launchers.  Keep this predicate fail-closed by
    matching the complete package/entrypoint argv contract instead.
    """
    if not isinstance(command, str) or not isinstance(args, list):
        return False
    program = os.path.basename(command).lower()
    if not all(isinstance(arg, str) for arg in args):
        return False
    values = tuple(args)
    if program == "uvx":
        known = tuple(getattr(setup, "_CODEX_LEGACY_UVX_MCP_ARGS", ()))
        return values in known or values == ("ouroboros-ai[mcp]", "mcp", "serve")
    if program == "pipx":
        legacy = ("run", "ouroboros-ai[mcp]", "mcp", "serve")
        canonical = ("run", "--spec", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve")
        return values == legacy or _mcp_args_with_optional_selectors(values, canonical)
    if program in {"ouroboros", "python", "python3"}:
        base = ("mcp", "serve") if program == "ouroboros" else ("-m", "ouroboros", "mcp", "serve")
        return _mcp_args_with_optional_selectors(values, base)
    if program == "uv":
        return _mcp_args_with_optional_selectors(
            values, ("run", "ouroboros", "mcp", "serve")
        ) or _mcp_args_with_optional_selectors(
            values,
            ("run", "--with", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"),
        )
    return False


def _is_setup_managed_json_entry(
    setup: Any,
    entry: dict[str, object],
    *,
    command_key: str,
    env_key: str,
    args_key: str | None,
    allowed_extra_keys: set[str],
    expected_runtime: str,
) -> bool:
    command = entry.get(command_key)
    if args_key is None:
        executable = command[0] if isinstance(command, list) and command else None
        args = command[1:] if isinstance(command, list) else None
    else:
        args = entry.get(args_key)
        executable = command
    if not _is_known_ouroboros_launcher(setup, executable, args):
        return False
    allowed_keys = {command_key, env_key, *allowed_extra_keys}
    if args_key is not None:
        allowed_keys.add(args_key)
    if set(entry) - allowed_keys:
        return False
    env = entry.get(env_key)
    if not isinstance(env, dict):
        return False
    runtime_values = [
        env[key] for key in ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_RUNTIME") if key in env
    ]
    if not runtime_values or any(
        not isinstance(value, str) or value.strip().lower() not in {expected_runtime, "host"}
        for value in runtime_values
    ):
        return False
    llm_backend = env.get("OUROBOROS_LLM_BACKEND")
    return isinstance(llm_backend, str) and llm_backend.strip().lower() == expected_runtime


def _migrate_json_entry(
    setup: Any,
    *,
    config_path: Path,
    host_name: str,
    servers_key: str,
    command_key: str,
    env_key: str,
    setup_managed: Callable[[dict[str, object]], bool],
    args_key: str | None = None,
    load_jsonc: bool = False,
) -> bool:
    if not config_path.exists():
        return True
    try:
        raw = config_path.read_text(encoding="utf-8")
        data = json.loads(setup._strip_jsonc(raw) if load_jsonc else raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print_error(f"Could not inspect {config_path} before host activation: {exc}")
        return False
    if not isinstance(data, dict):
        print_error(
            f"Could not inspect {config_path} before host activation: top-level is not an object"
        )
        return False
    servers = data.get(servers_key)
    if not isinstance(servers, dict):
        return True
    entry = servers.get("ouroboros")
    if not isinstance(entry, dict):
        return True
    override = _runtime_override(entry, command_key, env_key, args_key)
    if not setup_managed(entry):
        detail = (
            f" its explicit runtime selector is {override}"
            if override is not None
            else " because its command signature is not a setup-generated Ouroboros launcher"
        )
        print_error(
            f"Host setup cannot replace user-managed {config_path}:{detail}. "
            "Remove the entry or restore the exact Ouroboros setup launcher, then rerun setup."
        )
        return False
    migrated = deepcopy(entry)
    runtime_args_key = args_key or command_key
    command = migrated.get(runtime_args_key)
    if isinstance(command, list):
        args = [str(arg) for arg in command]
        for index, arg in enumerate(args):
            if arg == "--runtime" and index + 1 < len(args):
                args[index + 1] = "host"
            elif arg.startswith("--runtime="):
                args[index] = "--runtime=host"
        migrated[runtime_args_key] = args
    env = migrated.get(env_key)
    migrated_env = dict(env) if isinstance(env, dict) else {}
    for key in ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_RUNTIME"):
        if key in migrated_env:
            migrated_env[key] = "host"
    migrated_env["OUROBOROS_AGENT_RUNTIME"] = "host"
    migrated[env_key] = migrated_env
    if migrated == entry:
        return True
    servers["ouroboros"] = migrated
    setup._atomic_write_text(config_path, json.dumps(data, indent=2) + "\n")
    print_success(f"Migrated setup-managed {host_name} MCP runtime to host in {config_path}")
    return True


def _migrate_codex_entry(setup: Any) -> bool:
    config_path = setup.resolve_codex_home() / "config.toml"
    if not config_path.exists():
        return True
    try:
        raw = config_path.read_text(encoding="utf-8")
        parsed = tomllib.loads(raw)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        print_error(f"Could not inspect {config_path} before host activation: {exc}")
        return False
    entry = setup._codex_mcp_entry_from_toml(parsed)
    if entry is None:
        return True
    managed = setup._is_setup_managed_codex_mcp_entry(
        entry, has_managed_comment=setup._has_managed_codex_mcp_comment(raw)
    )
    override = _runtime_override(entry, "args", "env")
    if not managed:
        if override is not None:
            print_error(
                f"Host setup cannot replace user-managed {config_path}: its Ouroboros MCP "
                f"entry explicitly selects {override}. Remove that selector or change it to host, "
                "then rerun setup."
            )
            return False
        return True
    migrated = deepcopy(entry)
    args = [str(arg) for arg in migrated["args"]]
    for index, arg in enumerate(args):
        if arg == "--runtime" and index + 1 < len(args):
            args[index + 1] = "host"
        elif arg.startswith("--runtime="):
            args[index] = "--runtime=host"
    migrated["args"] = args
    env = migrated.get("env")
    migrated_env = dict(env) if isinstance(env, dict) else {}
    migrated_env["OUROBOROS_AGENT_RUNTIME"] = "host"
    lines = [*setup._CODEX_MCP_COMMENT_LINES, "", "[mcp_servers.ouroboros]"]
    for key in ("command", "args"):
        lines.append(f"{key} = {setup._render_toml_value(migrated[key])}")
    lines.extend(("", "[mcp_servers.ouroboros.env]"))
    lines.extend(
        f"{setup._render_toml_key(str(key))} = {setup._render_toml_value(value)}"
        for key, value in migrated_env.items()
    )
    section = "\n".join(lines) + "\n"
    content, _ = setup._upsert_codex_mcp_section(raw, section)
    if content != raw:
        setup._atomic_write_text(config_path, content)
        print_success(f"Migrated setup-managed Ouroboros MCP runtime to host in {config_path}")
    return True


def _migrate_launchers(setup: Any) -> bool:
    def managed_kiro(entry: dict[str, object]) -> bool:
        return (
            _is_setup_managed_json_entry(
                setup,
                entry,
                command_key="command",
                env_key="env",
                args_key="args",
                allowed_extra_keys={"disabled"},
                expected_runtime="kiro",
            )
            and entry.get("disabled", False) is False
        )

    def managed_copilot(entry: dict[str, object]) -> bool:
        return _is_setup_managed_json_entry(
            setup,
            entry,
            command_key="command",
            env_key="env",
            args_key="args",
            allowed_extra_keys=set(),
            expected_runtime="copilot",
        )

    def managed_opencode(entry: dict[str, object]) -> bool:
        return (
            _is_setup_managed_json_entry(
                setup,
                entry,
                command_key="command",
                env_key="environment",
                args_key=None,
                allowed_extra_keys={"type", "timeout"},
                expected_runtime="opencode",
            )
            and entry.get("type") == "local"
            and ("timeout" not in entry or entry.get("timeout") == 300000)
        )

    migrations = (
        lambda: _migrate_codex_entry(setup),
        lambda: _migrate_json_entry(
            setup,
            config_path=Path.home() / ".kiro" / "settings" / "mcp.json",
            host_name="Kiro",
            servers_key="mcpServers",
            command_key="command",
            env_key="env",
            setup_managed=managed_kiro,
            args_key="args",
        ),
        lambda: _migrate_json_entry(
            setup,
            config_path=Path.home() / ".copilot" / "mcp-config.json",
            host_name="Copilot",
            servers_key="mcpServers",
            command_key="command",
            env_key="env",
            setup_managed=managed_copilot,
            args_key="args",
        ),
        lambda: _migrate_json_entry(
            setup,
            config_path=setup._find_opencode_config(),
            host_name="OpenCode",
            servers_key="mcp",
            command_key="command",
            env_key="environment",
            setup_managed=managed_opencode,
            load_jsonc=True,
        ),
    )
    return all(migrate() for migrate in migrations)


def setup_host(setup: Any) -> bool:
    """Configure the CLI-less host runtime and reconcile setup-owned launchers."""
    from ouroboros.config.loader import create_default_config, ensure_config_dir
    from ouroboros.config.models import get_default_config

    config_dir = ensure_config_dir()
    config_path = config_dir / "config.yaml"
    missing = not config_path.exists()
    config = (
        get_default_config().model_dump(mode="json")
        if missing
        else yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    )
    if not isinstance(config, dict):
        print_error("~/.ouroboros/config.yaml top-level is not a mapping — aborting host setup.")
        return False
    orchestrator = config.get("orchestrator")
    if not isinstance(orchestrator, dict):
        orchestrator = {}
        config["orchestrator"] = orchestrator
    orchestrator["runtime_backend"] = "host"
    if not setup._commit_runtime_activation(
        runtime_name="Host",
        host_path=setup.resolve_codex_home() / "config.toml",
        additional_host_paths=(
            Path.home() / ".kiro" / "settings" / "mcp.json",
            Path.home() / ".copilot" / "mcp-config.json",
            setup._find_opencode_config(),
        ),
        config_path=config_path,
        config_was_missing=missing,
        runtime_content=yaml.safe_dump(config, default_flow_style=False, sort_keys=False),
        register_host=lambda: _migrate_launchers(setup),
        create_defaults=create_default_config,
    ):
        return False
    print_success("Configured host runtime (no CLI — host-driven dispatch)")
    print_info(f"Config saved to: {config_path}")
    print_info(
        "'host' only works from an MCP host session pumping ouroboros_job_wait (e.g. dsh). A terminal `ooo run` rejects it."
    )
    return True
