"""Codex CLI integration helper commands."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import tomllib
from typing import Annotated, Any

import typer

from ouroboros.cli.formatters.panels import print_error, print_success, print_warning
from ouroboros.codex import install_codex_artifacts

app = typer.Typer(
    name="codex",
    help="Manage Ouroboros Codex CLI integration artifacts.",
    no_args_is_help=True,
)

_REQUIRED_CODEX_AUTO_TOOLS = frozenset(
    {
        "ouroboros_auto",
        "ouroboros_start_auto",
        "ouroboros_interview",
        "ouroboros_generate_seed",
    }
)
_MCP_PROTOCOL_VERSION = "2024-11-05"
_MCP_STDERR_TAIL_BYTES = 8192


class _StdioMcpFramingMismatch(RuntimeError):
    """Raised when a stdio MCP response clearly uses another wire framing."""


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
    codex_dir = Path.home() / ".codex"
    try:
        result = install_codex_artifacts(codex_dir=codex_dir, prune=False)
    except FileNotFoundError as exc:
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
            help="Codex configuration directory to inspect. Defaults to ~/.codex.",
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
    resolved_codex_dir = codex_dir or Path.home() / ".codex"
    failures = _check_auto_dispatch_surface(resolved_codex_dir, live_mcp=live_mcp)

    if failures:
        print_error(
            "Codex ooo auto dispatch: BROKEN\n"
            + "\n".join(f"- {failure}" for failure in failures)
            + "\n\nRun `ouroboros codex refresh` and ensure the `ouroboros` MCP server is enabled.",
            title="Codex Doctor",
        )
        raise typer.Exit(1)

    print_success(
        "Codex ooo auto dispatch: OK\n"
        "- rule maps `ooo auto` to `ouroboros_auto`\n"
        "- auto skill declares MCP dispatch through `ouroboros_auto`\n"
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
        if rules is not None and ("`ooo auto" not in rules or "ouroboros_auto" not in rules):
            failures.append("Codex rules do not map `ooo auto` to `ouroboros_auto`")
        if rules is not None and (
            "manual" not in rules.lower() or "unavailable" not in rules.lower()
        ):
            failures.append("Codex rules do not describe fail-closed behavior for `ooo auto`")

    skill_path = codex_dir / "skills" / "ouroboros-auto" / "SKILL.md"
    if not skill_path.is_file():
        failures.append(f"missing auto skill file: {skill_path}")
    else:
        skill = _read_codex_text(skill_path, "auto skill", failures)
        if skill is not None and "mcp_tool: ouroboros_auto" not in skill:
            failures.append("auto skill does not declare `mcp_tool: ouroboros_auto`")
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
        args_obj = ouroboros_entry.get("args")
        if not isinstance(args_obj, list):
            args_obj = []
        args = tuple(arg for arg in args_obj if isinstance(arg, str))
        env_obj = ouroboros_entry.get("env")
        env = (
            {
                key: value
                for key, value in env_obj.items()
                if isinstance(key, str) and isinstance(value, str)
            }
            if isinstance(env_obj, dict)
            else {}
        )
        command_entry = _CodexMCPCommandEntry(command=command, args=args, env=env)
        if not live_mcp:
            _check_mcp_runtime_dependency_surface(command, list(args), failures)

    if live_mcp and command_entry is not None and not failures:
        _check_live_mcp_tool_exposure(command_entry, failures)

    if failures:
        print_warning(
            "Detected a Codex surface where `ooo auto` may be interpreted as normal text.",
            title="Codex Doctor",
        )

    return failures


def _check_mcp_runtime_dependency_surface(
    command: str, args: list[object], failures: list[str]
) -> None:
    """Detect Codex MCP server entries that cannot import the MCP runtime.

    ``ouroboros codex doctor`` used to validate only rules, skill metadata, and
    config presence. A direct ``ouroboros mcp serve`` entry can pass those checks
    while the installed ``ouroboros-ai`` environment lacks the optional ``mcp``
    extra, causing Codex's real stdio handshake to close before tools are listed.
    """
    command_name = Path(command).name
    string_args = [arg for arg in args if isinstance(arg, str)]

    if command_name in {"uvx", "uv"}:
        joined_args = " ".join(string_args)
        if "ouroboros-ai" in joined_args and "ouroboros-ai[mcp]" not in joined_args:
            failures.append(
                "Codex MCP command installs `ouroboros-ai` without the `mcp` extra; "
                "use `ouroboros-ai[mcp]` so stdio initialize/list_tools can start"
            )
        return

    if command_name != "ouroboros":
        return

    if importlib.util.find_spec("mcp") is None:
        failures.append(
            "current `ouroboros` environment cannot import `mcp`; reinstall for Codex MCP "
            "usage with `uv tool install --force 'ouroboros-ai[mcp]'`"
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
    ``uvx --from ouroboros-ai[mcp] ouroboros mcp serve``; validating that
    setup must not first require the current ``ouroboros codex doctor``
    interpreter to have installed the optional local ``mcp`` extra.
    """
    try:
        return await _list_stdio_mcp_tool_names_with_framing(command, args, env, framing="jsonl")
    except (json.JSONDecodeError, _StdioMcpFramingMismatch):
        return await _list_stdio_mcp_tool_names_with_framing(
            command, args, env, framing="content-length"
        )
    except (RuntimeError, TimeoutError) as exc:
        if not _should_retry_stdio_mcp_framing(exc):
            raise
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
    )
    stderr_buffer = bytearray()
    stderr_task = (
        asyncio.create_task(_drain_stdio_mcp_stderr(proc.stderr, stderr_buffer))
        if proc.stderr is not None
        else None
    )
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
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()


def _read_codex_text(path: Path, label: str, failures: list[str]) -> str | None:
    """Read a Codex artifact for doctor checks without crashing on broken files."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        failures.append(f"{label} is not valid UTF-8: {path}: {exc}")
    except OSError as exc:
        failures.append(f"could not read {label}: {path}: {exc}")
    return None
