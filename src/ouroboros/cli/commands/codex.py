"""Codex CLI integration helper commands."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tomllib
from typing import Annotated, Any, cast

from rich.markup import escape
import typer

from ouroboros.cli.formatters.panels import print_error, print_success, print_warning
from ouroboros.codex import install_codex_artifacts, resolve_codex_home
from ouroboros.package_profiles import UVX_PYTHON_FLOOR

app = typer.Typer(
    name="codex",
    help="Manage Ouroboros Codex CLI integration artifacts.",
    no_args_is_help=True,
)

_REQUIRED_CODEX_AUTO_TOOLS = frozenset(
    {
        "ouroboros_auto",
        "ouroboros_start_auto",
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_job_result",
        "ouroboros_interview",
        "ouroboros_generate_seed",
    }
)
_MCP_PROTOCOL_VERSION = "2024-11-05"
_MCP_STDERR_TAIL_BYTES = 8192
_CANONICAL_CODEX_UVX_MCP_ARGS = (
    "--isolated",
    "--python",
    UVX_PYTHON_FLOOR,
    "--from",
    "ouroboros-ai[mcp]",
    "ouroboros",
    "mcp",
    "serve",
)
_CODEX_MCP_RUNTIME_SUFFIX = ("--runtime", "codex", "--llm-backend", "codex")
_CANONICAL_CODEX_UVX_MCP_HOST_ARGS = (
    *_CANONICAL_CODEX_UVX_MCP_ARGS,
    *_CODEX_MCP_RUNTIME_SUFFIX,
)
_CANONICAL_CODEX_DIRECT_MCP_ARGS = ("mcp", "serve")
_CANONICAL_CODEX_MODULE_MCP_ARGS = ("-m", "ouroboros", "mcp", "serve")
# The shipped plugin descriptors pin the served package to the plugin
# manifest version (#2066) so plugin updates change the uvx cache key. The
# doctor accepts exactly that shape — the canonical requirement with a
# release-shaped exact pin — and continues rejecting every other
# requirement (ranges, other packages, a missing mcp extra).
_MCP_PINNED_FROM_SPEC_RE = re.compile(
    r"^ouroboros-ai\[mcp\]==(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?)$"
)


def _authoritative_plugin_version() -> str | None:
    """Return the shipped plugin version, or installed distribution fallback."""
    manifest_path = Path(__file__).resolve().parents[4] / ".codex-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        version = manifest.get("version")
        if isinstance(version, str) and version:
            return version
    except (OSError, ValueError):
        pass
    try:
        return metadata.version("ouroboros-ai")
    except metadata.PackageNotFoundError:
        return None


def _is_authoritative_mcp_pin(argument: str, expected_version: str | None) -> bool:
    match = _MCP_PINNED_FROM_SPEC_RE.match(argument)
    return bool(match and expected_version and match.group("version") == expected_version)


def _normalize_shipped_mcp_pin(
    arguments: list[str], *, expected_version: str | None
) -> tuple[str, ...]:
    """Map a shipped manifest-matching exact pin onto the canonical spec."""
    return tuple(
        "ouroboros-ai[mcp]" if _is_authoritative_mcp_pin(argument, expected_version) else argument
        for argument in arguments
    )


_CODEX_RUNTIME_ENV = {
    "OUROBOROS_AGENT_RUNTIME": "codex",
    "OUROBOROS_LLM_BACKEND": "codex",
}
_CODEX_RUNTIME_SELECTOR_ENV_KEYS = frozenset({*_CODEX_RUNTIME_ENV, "OUROBOROS_RUNTIME"})
_OUROBOROS_NESTED_ENV_KEY = "_OUROBOROS_NESTED"
_CODEX_MCP_APPROVAL_MODES = frozenset({"auto", "prompt", "approve"})
_CODEX_MCP_CONFIG_FIELDS = frozenset(
    {
        "command",
        "args",
        "env",
        "env_vars",
        "cwd",
        "http_headers",
        "env_http_headers",
        "url",
        "bearer_token",
        "bearer_token_env_var",
        "experimental_environment",
        "startup_timeout_sec",
        "startup_timeout_ms",
        "tool_timeout_sec",
        "enabled",
        "required",
        "supports_parallel_tool_calls",
        "default_tools_approval_mode",
        "enabled_tools",
        "disabled_tools",
        "scopes",
        "oauth",
        "oauth_resource",
        "name",
        "tools",
    }
)


class _StdioMcpFramingMismatch(RuntimeError):
    """Raised when a stdio MCP response clearly uses another wire framing."""


class _StdioMcpFramingProbeFailed(RuntimeError):
    """Raised when the initialize response failed before framing was established."""


@dataclass(frozen=True, slots=True)
class _CodexMCPCommandEntry:
    command: str
    args: tuple[str, ...]
    env: dict[str, str]


@app.callback()
def codex() -> None:
    """Manage Ouroboros Codex CLI integration artifacts."""


@app.command("refresh")
def refresh() -> None:
    """Refresh Codex rules and skills without changing MCP or Ouroboros config."""
    configured_codex_home = os.environ.get("CODEX_HOME")
    codex_dir = (
        Path(configured_codex_home).expanduser()
        if configured_codex_home
        else Path.home() / ".codex"
    )
    try:
        result = install_codex_artifacts(codex_dir=codex_dir, prune=False)
    except FileNotFoundError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc
    except OSError as exc:
        print_error(str(exc))
        raise typer.Exit(1) from exc

    print_success(f"Installed Codex rules → {result.rules_path}")
    print_success(f"Installed {len(result.skill_paths)} Codex skills → {codex_dir / 'skills'}")


@app.command("doctor")
def doctor(
    codex_dir: Annotated[
        Path | None,
        typer.Option(
            "--codex-dir",
            help="Codex configuration directory to inspect. Defaults to $CODEX_HOME or ~/.codex.",
        ),
    ] = None,
    live_mcp: Annotated[
        bool,
        typer.Option(
            "--live-mcp",
            help=(
                "Launch the configured stdio MCP server, run initialize/list_tools, "
                "and verify the official auto tools are exposed."
            ),
        ),
    ] = False,
) -> None:
    """Verify installed Codex artifacts can route ``ooo auto`` to Ouroboros."""
    resolved_codex_dir = resolve_codex_home(codex_dir)
    failures = _check_auto_dispatch_surface(resolved_codex_dir, live_mcp=live_mcp)

    if failures:
        message = (
            "Codex ooo auto dispatch: BROKEN\n"
            + "\n".join(f"- {failure}" for failure in failures)
            + "\n\nRun `ouroboros codex refresh` and ensure the `ouroboros` MCP server is enabled."
        )
        print_error(
            escape(message),
            title="Codex Doctor",
        )
        raise typer.Exit(1)

    print_success(
        "Codex ooo auto dispatch: OK\n"
        "- rule maps `ooo auto` to `ouroboros_start_auto`\n"
        "- auto skill declares MCP dispatch through `ouroboros_start_auto`\n"
        "- live MCP auto workflow includes job status/wait/result tools when probed\n"
        "- Codex config contains an `ouroboros` MCP server entry"
        + ("\n- live stdio initialize/list_tools exposes required auto tools" if live_mcp else ""),
        title="Codex Doctor",
    )


def _check_auto_dispatch_surface(codex_dir: Path, *, live_mcp: bool = False) -> list[str]:
    """Return configuration failures that can silently bypass ``ooo auto`` dispatch."""
    failures: list[str] = []

    rules_path = codex_dir / "rules" / "ouroboros.md"
    if not rules_path.is_file():
        failures.append(f"missing Codex rules file: {rules_path}")
    else:
        rules = _read_codex_text(rules_path, "Codex rules", failures)
        if rules is not None and ("`ooo auto" not in rules or "ouroboros_start_auto" not in rules):
            failures.append("Codex rules do not map `ooo auto` to `ouroboros_start_auto`")
        if rules is not None and (
            "manual" not in rules.lower() or "unavailable" not in rules.lower()
        ):
            failures.append("Codex rules do not describe fail-closed behavior for `ooo auto`")

    skill_path = codex_dir / "skills" / "ouroboros-auto" / "SKILL.md"
    if not skill_path.is_file():
        failures.append(f"missing auto skill file: {skill_path}")
    else:
        skill = _read_codex_text(skill_path, "auto skill", failures)
        if skill is not None and "mcp_tool: ouroboros_start_auto" not in skill:
            failures.append("auto skill does not declare `mcp_tool: ouroboros_start_auto`")
        if skill is not None and (
            "manual" not in skill.lower() or "unavailable" not in skill.lower()
        ):
            failures.append(
                "auto skill does not forbid manual fallback when dispatch is unavailable"
            )

    config_path = codex_dir / "config.toml"
    if not config_path.is_file():
        failures.append(f"missing Codex config file: {config_path}")
        return failures

    config_text = _read_codex_text(config_path, "Codex config", failures)
    if config_text is None:
        return failures

    try:
        config = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError as exc:
        failures.append(f"Codex config is not valid TOML: {exc}")
        return failures

    mcp_servers = config.get("mcp_servers")
    if not isinstance(mcp_servers, dict):
        failures.append("Codex config does not contain an [mcp_servers] table")
        return failures

    ouroboros_entry = mcp_servers.get("ouroboros")
    if not isinstance(ouroboros_entry, dict):
        failures.append("Codex config does not contain [mcp_servers.ouroboros]")
        return failures

    _check_mcp_schema_surface(ouroboros_entry, failures)
    _check_mcp_activation_surface(ouroboros_entry, failures)
    _check_mcp_execution_surface(ouroboros_entry, failures)

    url = ouroboros_entry.get("url")
    if isinstance(url, str) and url.strip():
        if live_mcp:
            failures.append(
                "Codex MCP server uses `url`, but `--live-mcp` currently verifies only "
                "stdio `command` entries; use a stdio command config or run without "
                "`--live-mcp`"
            )
        return failures

    command_entry: _CodexMCPCommandEntry | None = None
    command = ouroboros_entry.get("command")
    if not isinstance(command, str) or not command.strip():
        failures.append("[mcp_servers.ouroboros] is missing `command` or `url`")
    else:
        args_obj = ouroboros_entry.get("args", [])
        args: tuple[str, ...] | None = None
        if not isinstance(args_obj, list):
            failures.append("[mcp_servers.ouroboros].args must be an array of strings")
        else:
            invalid_args = [
                f"index {index} ({type(argument).__name__})"
                for index, argument in enumerate(args_obj)
                if not isinstance(argument, str)
            ]
            if invalid_args:
                failures.append(
                    "[mcp_servers.ouroboros].args contains non-string values: "
                    + ", ".join(invalid_args)
                )
            else:
                args = cast(tuple[str, ...], tuple(args_obj))

        env_obj = ouroboros_entry.get("env", {})
        env: dict[str, str] | None = None
        if not isinstance(env_obj, dict):
            failures.append("[mcp_servers.ouroboros].env must be a table of string values")
        else:
            invalid_env = [
                f"{key!r} ({type(value).__name__})"
                for key, value in env_obj.items()
                if not isinstance(key, str) or not isinstance(value, str)
            ]
            if invalid_env:
                failures.append(
                    "[mcp_servers.ouroboros].env contains non-string values: "
                    + ", ".join(invalid_env)
                )
            else:
                env = cast(dict[str, str], dict(env_obj))

        if args is not None and env is not None:
            command_entry = _CodexMCPCommandEntry(command=command, args=args, env=env)
            _check_mcp_runtime_dependency_surface(
                command,
                list(args),
                env,
                failures,
                check_local_import=not live_mcp,
            )

    if live_mcp and command_entry is not None and not failures:
        _check_live_mcp_tool_exposure(command_entry, failures)

    if failures:
        print_warning(
            "Detected a Codex surface where `ooo auto` may be interpreted as normal text.",
            title="Codex Doctor",
        )

    return failures


def _check_mcp_schema_surface(ouroboros_entry: Mapping[str, object], failures: list[str]) -> None:
    """Validate Codex 0.132's fail-closed MCP transport and shared schema."""
    unknown_fields = sorted(set(ouroboros_entry) - _CODEX_MCP_CONFIG_FIELDS)
    if unknown_fields:
        failures.append(
            "[mcp_servers.ouroboros] contains unsupported fields: " + ", ".join(unknown_fields)
        )

    has_command = "command" in ouroboros_entry
    has_url = "url" in ouroboros_entry
    if has_command and has_url:
        failures.append(
            "[mcp_servers.ouroboros] mixes stdio `command` with HTTP `url`; "
            "Codex requires exactly one transport"
        )

    if has_command:
        for field in (
            "bearer_token_env_var",
            "http_headers",
            "env_http_headers",
            "oauth",
            "oauth_resource",
        ):
            if field in ouroboros_entry:
                failures.append(
                    f"[mcp_servers.ouroboros].{field} is not supported for stdio `command`"
                )
    elif has_url:
        for field in ("args", "env", "env_vars", "cwd"):
            if field in ouroboros_entry:
                failures.append(f"[mcp_servers.ouroboros].{field} is not supported for HTTP `url`")

    if "bearer_token" in ouroboros_entry:
        failures.append(
            "[mcp_servers.ouroboros].bearer_token is unsupported; use "
            "`bearer_token_env_var` on an HTTP entry"
        )

    _check_optional_string_field(ouroboros_entry, "url", failures)
    _check_optional_string_field(ouroboros_entry, "bearer_token_env_var", failures)
    _check_optional_string_field(ouroboros_entry, "oauth_resource", failures)
    _check_optional_string_field(ouroboros_entry, "name", failures)

    for field in ("http_headers", "env_http_headers"):
        if field in ouroboros_entry:
            _check_string_table_field(ouroboros_entry[field], field, failures)

    for field in ("required", "supports_parallel_tool_calls"):
        if field in ouroboros_entry and not isinstance(ouroboros_entry[field], bool):
            failures.append(f"[mcp_servers.ouroboros].{field} must be a boolean")

    if "default_tools_approval_mode" in ouroboros_entry:
        _check_mcp_approval_mode(
            ouroboros_entry["default_tools_approval_mode"],
            "default_tools_approval_mode",
            failures,
        )

    if "scopes" in ouroboros_entry:
        scopes = ouroboros_entry["scopes"]
        if not isinstance(scopes, list):
            failures.append("[mcp_servers.ouroboros].scopes must be an array of strings")
        else:
            invalid_scopes = [
                f"index {index} ({type(scope).__name__})"
                for index, scope in enumerate(scopes)
                if not isinstance(scope, str) or not scope.strip()
            ]
            if invalid_scopes:
                failures.append(
                    "[mcp_servers.ouroboros].scopes contains empty or non-string values: "
                    + ", ".join(invalid_scopes)
                )

    if "oauth" in ouroboros_entry:
        oauth = ouroboros_entry["oauth"]
        if not isinstance(oauth, Mapping):
            failures.append("[mcp_servers.ouroboros].oauth must be a table")
        else:
            unknown_oauth_fields = sorted(set(oauth) - {"client_id"})
            if unknown_oauth_fields:
                failures.append(
                    "[mcp_servers.ouroboros].oauth contains unsupported fields: "
                    + ", ".join(unknown_oauth_fields)
                )
            _check_optional_string_field(oauth, "client_id", failures, prefix="oauth.")

    if "tools" in ouroboros_entry:
        tools = ouroboros_entry["tools"]
        if not isinstance(tools, Mapping):
            failures.append("[mcp_servers.ouroboros].tools must be a table")
        else:
            for tool_name, tool_config in tools.items():
                if not isinstance(tool_name, str) or not tool_name.strip():
                    failures.append(
                        "[mcp_servers.ouroboros].tools contains an empty or non-string tool name"
                    )
                    continue
                if not isinstance(tool_config, Mapping):
                    failures.append(f"[mcp_servers.ouroboros].tools.{tool_name} must be a table")
                    continue
                unknown_tool_fields = sorted(set(tool_config) - {"approval_mode"})
                if unknown_tool_fields:
                    failures.append(
                        f"[mcp_servers.ouroboros].tools.{tool_name} contains unsupported "
                        "fields: " + ", ".join(unknown_tool_fields)
                    )
                if "approval_mode" in tool_config:
                    _check_mcp_approval_mode(
                        tool_config["approval_mode"],
                        f"tools.{tool_name}.approval_mode",
                        failures,
                    )


def _check_optional_string_field(
    config: Mapping[object, object],
    field: str,
    failures: list[str],
    *,
    prefix: str = "",
) -> None:
    """Validate an optional Codex MCP string without truthy coercion."""
    if field in config and not isinstance(config[field], str):
        failures.append(f"[mcp_servers.ouroboros].{prefix}{field} must be a string")


def _check_string_table_field(value: object, field: str, failures: list[str]) -> None:
    """Validate a Codex MCP table whose keys and values must both be strings."""
    if not isinstance(value, Mapping):
        failures.append(f"[mcp_servers.ouroboros].{field} must be a table of strings")
        return
    invalid_entries = [
        f"{key!r} ({type(item).__name__})"
        for key, item in value.items()
        if not isinstance(key, str) or not isinstance(item, str)
    ]
    if invalid_entries:
        failures.append(
            f"[mcp_servers.ouroboros].{field} contains non-string values: "
            + ", ".join(invalid_entries)
        )


def _check_mcp_approval_mode(value: object, field: str, failures: list[str]) -> None:
    """Require the three approval modes accepted by Codex CLI 0.132."""
    if not isinstance(value, str) or value not in _CODEX_MCP_APPROVAL_MODES:
        failures.append(
            f"[mcp_servers.ouroboros].{field} must be one of: "
            + ", ".join(sorted(_CODEX_MCP_APPROVAL_MODES))
        )


def _check_mcp_activation_surface(
    ouroboros_entry: Mapping[str, object], failures: list[str]
) -> None:
    """Validate the Codex fields that decide whether tools are reachable.

    Codex defaults an omitted ``enabled`` field to true.  When an
    ``enabled_tools`` allow-list is present, only its members are registered;
    ``disabled_tools`` is then applied as a deny-list.  A direct live launch
    bypasses all three filters, so doctor must validate their persisted types
    and effects before it considers probing the configured transport.
    """
    enabled = ouroboros_entry.get("enabled", True)
    if not isinstance(enabled, bool):
        failures.append("[mcp_servers.ouroboros].enabled must be a boolean")
    elif not enabled:
        failures.append("[mcp_servers.ouroboros] is disabled (`enabled = false`)")

    tool_filters: dict[str, frozenset[str]] = {}
    for field in ("enabled_tools", "disabled_tools"):
        if field not in ouroboros_entry:
            continue
        value = ouroboros_entry[field]
        if not isinstance(value, list):
            failures.append(f"[mcp_servers.ouroboros].{field} must be an array of strings")
            continue
        invalid_values = [
            f"index {index} ({type(tool_name).__name__})"
            for index, tool_name in enumerate(value)
            if not isinstance(tool_name, str)
        ]
        if invalid_values:
            failures.append(
                f"[mcp_servers.ouroboros].{field} contains non-string values: "
                + ", ".join(invalid_values)
            )
            continue
        tool_filters[field] = frozenset(cast(list[str], value))

    enabled_tools = tool_filters.get("enabled_tools")
    if enabled_tools is not None:
        missing_tools = sorted(_REQUIRED_CODEX_AUTO_TOOLS - enabled_tools)
        if missing_tools:
            failures.append(
                "[mcp_servers.ouroboros].enabled_tools omits required auto tools: "
                + ", ".join(missing_tools)
            )

    disabled_tools = tool_filters.get("disabled_tools")
    if disabled_tools is not None:
        blocked_tools = sorted(_REQUIRED_CODEX_AUTO_TOOLS & disabled_tools)
        if blocked_tools:
            failures.append(
                "[mcp_servers.ouroboros].disabled_tools blocks required auto tools: "
                + ", ".join(blocked_tools)
            )


def _check_mcp_execution_surface(
    ouroboros_entry: Mapping[str, object], failures: list[str]
) -> None:
    """Reject Codex execution controls that the doctor probe cannot reproduce.

    The live probe deliberately supports only the launcher, argv, and explicit
    environment table represented by :class:`_CodexMCPCommandEntry`.  Silently
    ignoring any additional process or initialization controls would verify a
    different execution contract from the one Codex actually uses.
    """
    unsupported_fields = {
        "cwd": "the stdio process working directory",
        "env_vars": "which ambient environment variables Codex forwards",
        "experimental_environment": "the Codex execution environment",
        "startup_timeout_sec": "the Codex MCP initialization deadline",
        "startup_timeout_ms": "the legacy Codex MCP initialization deadline",
        "tool_timeout_sec": "the Codex MCP tool-call deadline",
    }
    for field, effect in unsupported_fields.items():
        if field in ouroboros_entry:
            failures.append(
                f"[mcp_servers.ouroboros].{field} is unsupported by doctor because "
                f"it changes {effect}; remove it before verifying this MCP contract"
            )


def _check_mcp_runtime_dependency_surface(
    command: str,
    args: list[object],
    env: object,
    failures: list[str],
    *,
    check_local_import: bool = True,
) -> None:
    """Detect Codex MCP server entries that cannot import the MCP runtime.

    ``ouroboros codex doctor`` used to validate only rules, skill metadata, and
    config presence. A direct ``ouroboros mcp serve`` entry can pass those checks
    while the installed ``ouroboros-ai`` environment lacks the optional ``mcp``
    extra, causing Codex's real stdio handshake to close before tools are listed.
    """
    invalid_args = [
        f"index {index} ({type(argument).__name__})"
        for index, argument in enumerate(args)
        if not isinstance(argument, str)
    ]
    if invalid_args:
        failures.append("Codex MCP args contains non-string values: " + ", ".join(invalid_args))
        return
    if not isinstance(env, Mapping):
        failures.append("Codex MCP env must be a mapping of string values")
        return
    invalid_env = [
        f"{key!r} ({type(value).__name__})"
        for key, value in env.items()
        if not isinstance(key, str) or not isinstance(value, str)
    ]
    if invalid_env:
        failures.append("Codex MCP env contains non-string values: " + ", ".join(invalid_env))
        return

    command_name = Path(command).name
    string_args = cast(list[str], args)
    string_env = cast(Mapping[str, str], env)

    if command_name == "uvx":
        if not _command_matches_path_program(command, "uvx"):
            failures.append(
                "Codex MCP uvx command does not resolve to the `uvx` executable on PATH"
            )
        joined_args = " ".join(string_args)
        if "ouroboros-ai" in joined_args and "ouroboros-ai[mcp]" not in joined_args:
            failures.append(
                "Codex MCP command installs `ouroboros-ai` without the `mcp` extra; "
                "use `ouroboros-ai[mcp]` so stdio initialize/list_tools can start"
            )

        expected_plugin_version = _authoritative_plugin_version()
        args_tuple = _normalize_shipped_mcp_pin(
            string_args, expected_version=expected_plugin_version
        )
        if args_tuple == _CANONICAL_CODEX_UVX_MCP_ARGS:
            _check_codex_runtime_env(string_env, failures, required=True)
            return
        if args_tuple == _CANONICAL_CODEX_UVX_MCP_HOST_ARGS:
            _check_codex_runtime_env(string_env, failures, required=False)
            return

        command_tail = ("ouroboros", "mcp", "serve")
        try:
            command_index = next(
                index
                for index in range(len(string_args) - len(command_tail) + 1)
                if tuple(string_args[index : index + len(command_tail)]) == command_tail
            )
        except StopIteration:
            command_index = None

        isolated_indices = [
            index for index, argument in enumerate(string_args) if argument == "--isolated"
        ]
        if len(isolated_indices) != 1 or (
            command_index is not None and isolated_indices[0] > command_index
        ):
            failures.append(
                "Codex MCP uvx command may reuse an installed MCP 1.x tool; "
                "use exactly one `--isolated` before the launched command"
            )

        python_indices = [
            index
            for index, argument in enumerate(string_args)
            if argument == "--python" or argument.startswith("--python=")
        ]
        python_selector = (
            string_args[python_indices[0] + 1]
            if len(python_indices) == 1
            and string_args[python_indices[0]] == "--python"
            and python_indices[0] + 1 < len(string_args)
            else None
        )
        if (
            len(python_indices) != 1
            or python_selector != UVX_PYTHON_FLOOR
            or (command_index is not None and python_indices[0] > command_index)
        ):
            failures.append(
                "Codex MCP uvx command does not select the supported Python floor; "
                "use exactly one `--python >=3.12` before the launched command"
            )

        from_indices = [
            index
            for index, argument in enumerate(string_args)
            if argument == "--from" or argument.startswith("--from=")
        ]
        package_spec = (
            string_args[from_indices[0] + 1]
            if len(from_indices) == 1
            and string_args[from_indices[0]] == "--from"
            and from_indices[0] + 1 < len(string_args)
            else None
        )
        package_spec_accepted = package_spec == "ouroboros-ai[mcp]" or (
            package_spec is not None
            and _is_authoritative_mcp_pin(package_spec, expected_plugin_version)
        )
        if (
            len(from_indices) != 1
            or not package_spec_accepted
            or (command_index is not None and from_indices[0] > command_index)
        ):
            failures.append(
                "Codex MCP uvx command must use exactly one `--from ouroboros-ai[mcp]` "
                "requirement before the launched command — unpinned, or the shipped "
                "plugin descriptor's manifest-matching exact release pin"
            )

        allowed_command_tails = {
            command_tail,
            (*command_tail, "--runtime", "codex", "--llm-backend", "codex"),
        }
        if command_index is None or tuple(string_args[command_index:]) not in allowed_command_tails:
            failures.append(
                "Codex MCP uvx command must end with `ouroboros mcp serve` or the exact "
                "Codex host selector `--runtime codex --llm-backend codex`; uvx launcher "
                "flags and arbitrary arguments are not allowed after that command"
            )

        failures.append(
            "Codex MCP uvx command is not canonical; expected the isolated base launcher "
            "with Codex runtime env, or: "
            "`uvx --isolated --python >=3.12 --from ouroboros-ai[mcp] "
            "ouroboros mcp serve --runtime codex --llm-backend codex`"
        )
        return

    if command_name == "uv":
        joined_args = " ".join(string_args)
        if "ouroboros-ai" in joined_args and "ouroboros-ai[mcp]" not in joined_args:
            failures.append(
                "Codex MCP command installs `ouroboros-ai` without the `mcp` extra; "
                "use `ouroboros-ai[mcp]` so stdio initialize/list_tools can start"
            )
        failures.append(
            "Codex MCP `uv run` launchers are unsupported because they can inherit a "
            "project environment; use the canonical isolated `uvx` launcher"
        )
        return

    normalized_command_name = (
        Path(command_name).stem if command_name.lower().endswith(".exe") else command_name
    )
    if normalized_command_name == "ouroboros":
        if not _command_matches_path_program(command, "ouroboros"):
            failures.append(
                "Codex MCP direct command does not resolve to the `ouroboros` executable on PATH"
            )
        _check_exact_codex_runtime_contract(
            string_args,
            string_env,
            base_args=_CANONICAL_CODEX_DIRECT_MCP_ARGS,
            launcher="direct `ouroboros`",
            failures=failures,
        )
        if check_local_import and importlib.util.find_spec("mcp") is None:
            failures.append(
                "current `ouroboros` environment cannot import `mcp`; replace the direct "
                "launcher with `ouroboros setup --runtime codex --mcp-mode stdio`, or "
                "reinstall for direct Codex MCP usage with "
                "`uv tool install --force 'ouroboros-ai[mcp]'`"
            )
        return

    if _command_is_current_python(command):
        _check_exact_codex_runtime_contract(
            string_args,
            string_env,
            base_args=_CANONICAL_CODEX_MODULE_MCP_ARGS,
            launcher="`python -m ouroboros`",
            failures=failures,
        )
        if check_local_import and importlib.util.find_spec("mcp") is None:
            failures.append(
                "configured Python module environment cannot import `mcp`; install the "
                "`ouroboros-ai[mcp]` profile before using it for Codex MCP"
            )
        return

    failures.append(
        "Codex MCP command is not a setup-supported executable; use canonical `uvx`, "
        "the `ouroboros` executable on PATH, or the current Python executable with "
        "`-m ouroboros`"
    )


def _command_matches_path_program(command: str, program: str) -> bool:
    """Return whether command resolves to the exact PATH-selected program."""
    expected = shutil.which(program)
    configured = shutil.which(command)
    if expected is None or configured is None:
        return False
    try:
        return Path(configured).resolve(strict=True) == Path(expected).resolve(strict=True)
    except OSError:
        return False


def _command_is_current_python(command: str) -> bool:
    """Match setup's exact ``sys.executable`` module launcher, without aliases."""
    configured = os.path.normcase(os.path.abspath(os.path.expanduser(command)))
    current = os.path.normcase(os.path.abspath(sys.executable))
    return configured == current


def _check_exact_codex_runtime_contract(
    args: list[str],
    env: Mapping[str, str],
    *,
    base_args: tuple[str, ...],
    launcher: str,
    failures: list[str],
) -> None:
    """Require one unambiguous Codex runtime selection for a local launcher."""
    args_tuple = tuple(args)
    if args_tuple == base_args:
        _check_codex_runtime_env(env, failures, required=True)
        return
    if args_tuple == (*base_args, *_CODEX_MCP_RUNTIME_SUFFIX):
        _check_codex_runtime_env(env, failures, required=False)
        return
    failures.append(
        f"Codex MCP {launcher} command must use exactly `{' '.join(base_args)}` with "
        "Codex selector env, or append exactly `--runtime codex --llm-backend codex`"
    )


def _check_codex_runtime_env(
    env: Mapping[str, str], failures: list[str], *, required: bool
) -> None:
    """Validate runtime selectors accompanying a canonical Codex uvx launcher."""
    if _OUROBOROS_NESTED_ENV_KEY in env:
        failures.append(
            "Codex MCP runtime environment persists `_OUROBOROS_NESTED`, but this "
            "reserved internal recursion sentinel can make the server exit before MCP "
            "initialization; "
            "remove it from [mcp_servers.ouroboros.env]"
        )

    hostile = {
        key: value
        for key, value in env.items()
        if key in _CODEX_RUNTIME_SELECTOR_ENV_KEYS and value != "codex"
    }
    if hostile:
        rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(hostile.items()))
        failures.append("Codex MCP runtime environment contains conflicting selectors: " + rendered)

    if not required:
        return

    missing_or_invalid = [
        f"{key}={expected!r}"
        for key, expected in _CODEX_RUNTIME_ENV.items()
        if env.get(key) != expected
    ]
    if missing_or_invalid:
        failures.append(
            "Codex MCP base uvx launcher requires runtime environment selectors: "
            + ", ".join(missing_or_invalid)
        )


def _check_live_mcp_tool_exposure(
    command_entry: _CodexMCPCommandEntry, failures: list[str]
) -> None:
    """Verify Codex's configured stdio command can initialize and list tools."""
    try:
        tool_names = asyncio.run(
            _list_stdio_mcp_tool_names(
                command_entry.command,
                command_entry.args,
                command_entry.env,
            )
        )
    except Exception as exc:
        failures.append(f"Codex MCP stdio initialize/list_tools failed: {exc}")
        return

    missing_tools = sorted(_REQUIRED_CODEX_AUTO_TOOLS - tool_names)
    if missing_tools:
        failures.append(
            "Codex MCP stdio list_tools is missing required auto tools: " + ", ".join(missing_tools)
        )


async def _list_stdio_mcp_tool_names(
    command: str, args: tuple[str, ...], env: dict[str, str]
) -> frozenset[str]:
    """Launch a stdio MCP server and return the names exposed by list_tools().

    This doctor probe intentionally speaks the small MCP initialize/list_tools
    JSON-RPC sequence directly instead of using :class:`MCPClientAdapter`.
    Codex can point at a self-contained command such as
    ``uvx --isolated --python >=3.12 --from ouroboros-ai[mcp] ouroboros mcp serve``;
    validating that
    setup must not first require the current ``ouroboros codex doctor``
    interpreter to have installed the optional local ``mcp`` extra.
    """
    try:
        return await _list_stdio_mcp_tool_names_with_framing(command, args, env, framing="jsonl")
    except _StdioMcpFramingProbeFailed:
        return await _list_stdio_mcp_tool_names_with_framing(
            command, args, env, framing="content-length"
        )


async def _list_stdio_mcp_tool_names_with_framing(
    command: str,
    args: tuple[str, ...],
    env: dict[str, str],
    *,
    framing: str,
) -> frozenset[str]:
    """Launch a stdio MCP server with one wire framing and return tool names."""
    process_env = os.environ.copy()
    process_env.update(env)
    proc = await asyncio.create_subprocess_exec(
        command,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=process_env,
        start_new_session=os.name == "posix",
    )
    stderr_buffer = bytearray()
    stderr_task = (
        asyncio.create_task(_drain_stdio_mcp_stderr(proc.stderr, stderr_buffer))
        if proc.stderr is not None
        else None
    )
    try:
        try:
            await _send_stdio_mcp_message(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": _MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {
                            "name": "ouroboros-codex-doctor",
                            "version": "0.0.0",
                        },
                    },
                },
                framing=framing,
            )
            await _read_stdio_mcp_response(
                proc,
                request_id=1,
                timeout=30.0,
                stderr_buffer=stderr_buffer,
                framing=framing,
            )
        except (
            OSError,
            json.JSONDecodeError,
            _StdioMcpFramingMismatch,
            RuntimeError,
            TimeoutError,
        ) as exc:
            if framing == "jsonl" and _should_retry_stdio_mcp_framing(exc):
                raise _StdioMcpFramingProbeFailed(str(exc)) from exc
            raise
        await _send_stdio_mcp_message(
            proc,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            framing=framing,
        )
        await _send_stdio_mcp_message(
            proc,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            framing=framing,
        )
        response = await _read_stdio_mcp_response(
            proc,
            request_id=2,
            timeout=30.0,
            stderr_buffer=stderr_buffer,
            framing=framing,
        )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise RuntimeError("tools/list response did not contain an object result")
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise RuntimeError("tools/list response did not contain a tools list")
        return frozenset(
            tool["name"]
            for tool in tools
            if isinstance(tool, Mapping) and isinstance(tool.get("name"), str)
        )
    finally:
        await _terminate_stdio_mcp_process(proc)
        if stderr_task is not None:
            try:
                await asyncio.wait_for(stderr_task, timeout=0.2)
            except TimeoutError:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)


def _should_retry_stdio_mcp_framing(exc: BaseException) -> bool:
    """Return True when JSONL failed before a valid initialize response existed."""
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, (OSError, json.JSONDecodeError, _StdioMcpFramingMismatch)):
        return True
    return isinstance(exc, RuntimeError) and "exited before response" in str(exc)


async def _send_stdio_mcp_message(
    proc: asyncio.subprocess.Process, message: Mapping[str, Any], *, framing: str
) -> None:
    if proc.stdin is None:
        raise RuntimeError("MCP stdio process has no stdin")
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if framing == "jsonl":
        proc.stdin.write(body + b"\n")
    elif framing == "content-length":
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        proc.stdin.write(header + body)
    else:  # pragma: no cover - internal defensive guard
        raise RuntimeError(f"unsupported MCP stdio framing: {framing}")
    await proc.stdin.drain()


async def _read_stdio_mcp_response(
    proc: asyncio.subprocess.Process,
    *,
    request_id: int,
    timeout: float,
    stderr_buffer: bytearray,
    framing: str,
) -> Mapping[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"timed out waiting for MCP stdio response id {request_id}")
        message = await asyncio.wait_for(
            _read_stdio_mcp_message(proc, stderr_buffer=stderr_buffer, framing=framing),
            timeout=remaining,
        )
        if message.get("id") != request_id:
            continue
        error = message.get("error")
        if isinstance(error, Mapping):
            raise RuntimeError(str(error.get("message") or error))
        return message


async def _read_stdio_mcp_message(
    proc: asyncio.subprocess.Process, *, stderr_buffer: bytearray, framing: str
) -> Mapping[str, Any]:
    if proc.stdout is None:
        raise RuntimeError("MCP stdio process has no stdout")

    if framing == "content-length":
        return await _read_content_length_stdio_mcp_message(proc, stderr_buffer=stderr_buffer)
    if framing != "jsonl":  # pragma: no cover - internal defensive guard
        raise RuntimeError(f"unsupported MCP stdio framing: {framing}")

    while True:
        line = await proc.stdout.readline()
        if line == b"":
            stderr = _format_stdio_mcp_stderr(stderr_buffer)
            detail = f": {stderr}" if stderr else ""
            raise RuntimeError(f"MCP stdio process exited before response{detail}")
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith(b"content-length:"):
            raise _StdioMcpFramingMismatch("MCP stdio response used Content-Length framing")
        decoded = json.loads(stripped.decode("utf-8"))
        if not isinstance(decoded, Mapping):
            raise RuntimeError("MCP stdio response was not a JSON object")
        return decoded


async def _read_content_length_stdio_mcp_message(
    proc: asyncio.subprocess.Process, *, stderr_buffer: bytearray
) -> Mapping[str, Any]:
    if proc.stdout is None:
        raise RuntimeError("MCP stdio process has no stdout")

    while True:
        headers: dict[str, str] = {}
        while True:
            line = await proc.stdout.readline()
            if line == b"":
                stderr = _format_stdio_mcp_stderr(stderr_buffer)
                detail = f": {stderr}" if stderr else ""
                raise RuntimeError(f"MCP stdio process exited before response{detail}")
            stripped = line.strip()
            if not stripped:
                break
            name, separator, value = stripped.decode("ascii", errors="replace").partition(":")
            if not separator:
                raise RuntimeError("MCP stdio response header was malformed")
            headers[name.lower()] = value.strip()

        content_length = headers.get("content-length")
        if content_length is None:
            continue
        body = await proc.stdout.readexactly(int(content_length))
        decoded = json.loads(body.decode("utf-8"))
        if not isinstance(decoded, Mapping):
            raise RuntimeError("MCP stdio response was not a JSON object")
        return decoded


async def _drain_stdio_mcp_stderr(stderr: asyncio.StreamReader, stderr_buffer: bytearray) -> None:
    while True:
        chunk = await stderr.read(4096)
        if not chunk:
            return
        stderr_buffer.extend(chunk)
        if len(stderr_buffer) > _MCP_STDERR_TAIL_BYTES:
            del stderr_buffer[: len(stderr_buffer) - _MCP_STDERR_TAIL_BYTES]


def _format_stdio_mcp_stderr(stderr_buffer: bytearray) -> str:
    return bytes(stderr_buffer).decode("utf-8", errors="replace").strip()


async def _terminate_stdio_mcp_process(proc: asyncio.subprocess.Process) -> None:
    if proc.stdin is not None:
        proc.stdin.close()

    if os.name != "posix":  # pragma: no cover - exercised by Windows CI/runtime
        if proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        return

    process_group_id = proc.pid
    _signal_stdio_mcp_process(proc, force=False)
    forced = False
    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:
            forced = True
    if not forced:
        try:
            await asyncio.wait_for(
                _wait_for_stdio_mcp_process_group_exit(process_group_id), timeout=2.0
            )
        except TimeoutError:
            forced = True
    if forced:
        _signal_stdio_mcp_process(proc, force=True)
        if proc.returncode is None:
            await proc.wait()


async def _wait_for_stdio_mcp_process_group_exit(process_group_id: int) -> None:
    """Wait until no wrapper descendant remains in the probe's POSIX group."""
    while True:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            # EPERM still proves that the group exists.  This can occur when
            # the session-leading wrapper exits before a surviving child and
            # the probe no longer owns permission to inspect that group.
            pass
        await asyncio.sleep(0.02)


def _signal_stdio_mcp_process(proc: asyncio.subprocess.Process, *, force: bool) -> None:
    """Stop the uvx wrapper and the MCP child that inherits its stdio pipes."""
    if os.name != "posix":  # pragma: no cover - exercised by Windows CI/runtime
        if force:
            proc.kill()
        else:
            proc.terminate()
        return

    try:
        os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        return


def _read_codex_text(path: Path, label: str, failures: list[str]) -> str | None:
    """Read a Codex artifact for doctor checks without crashing on broken files."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        failures.append(f"{label} is not valid UTF-8: {path}: {exc}")
    except OSError as exc:
        failures.append(f"could not read {label}: {path}: {exc}")
    return None
