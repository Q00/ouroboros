"""Hidden CLI entrypoint for external frontdoors that need ``ooo`` dispatch."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import time
from typing import Annotated

import typer

from ouroboros.cli.formatters import console
from ouroboros.cli.formatters.panels import print_error
from ouroboros.mcp.tools.job_handlers import JobResultHandler, JobWaitHandler
from ouroboros.mcp.types import MCPToolResult
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.command_dispatcher import create_codex_command_dispatcher
from ouroboros.router import InvalidSkill, NotHandled, ResolveRequest, resolve_skill_dispatch

_UNSUPPORTED_DISPATCH_EXIT_CODE = 78
_AUTO_MONITOR_DEFAULT_TIMEOUT_SECONDS = 6 * 60 * 60
_AUTO_MONITOR_WAIT_SECONDS = 120


def _join_prompt(prompt_parts: list[str]) -> str:
    return " ".join(part for part in prompt_parts if part is not None).strip()


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _print_tool_result(result: MCPToolResult) -> None:
    text = result.text_content.strip()
    if text:
        console.print(text)


def _result_message(messages: tuple[AgentMessage, ...]) -> AgentMessage | None:
    return next((message for message in messages if message.type == "result"), None)


def _coerce_cursor(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str):
        try:
            return max(int(value), 0)
        except ValueError:
            return 0
    return 0


async def _monitor_auto_job(
    job_id: str,
    *,
    cursor: int = 0,
    max_seconds: int | None = None,
) -> int:
    """Own the foreground UX for ``ooo auto`` background jobs."""
    wait_handler = JobWaitHandler()
    result_handler = JobResultHandler()
    deadline = time.monotonic() + (
        max_seconds
        if max_seconds is not None
        else _env_positive_int(
            "OUROBOROS_DISPATCH_AUTO_MONITOR_TIMEOUT_SECONDS",
            _AUTO_MONITOR_DEFAULT_TIMEOUT_SECONDS,
        )
    )

    console.print(f"Monitoring auto job: {job_id}")
    last_printed = ""
    while True:
        remaining = max(0, int(deadline - time.monotonic()))
        if remaining <= 0:
            print_error(
                f"Auto job monitor timed out before terminal status: {job_id}. "
                f"Resume with `ouroboros job wait {job_id}`."
            )
            return 1

        wait_result = await wait_handler.handle(
            {
                "job_id": job_id,
                "cursor": cursor,
                "timeout_seconds": min(_AUTO_MONITOR_WAIT_SECONDS, remaining),
                "view": "summary",
                "stream": "linked",
            }
        )
        if wait_result.is_err:
            print_error(wait_result.error.message)
            return 1

        snapshot = wait_result.value
        meta = snapshot.meta
        cursor = _coerce_cursor(meta.get("cursor"))
        text = snapshot.text_content.strip()
        is_terminal = bool(meta.get("is_terminal"))
        changed = bool(meta.get("changed")) or is_terminal
        if text and changed and text != last_printed:
            console.print(text)
            last_printed = text

        if not is_terminal:
            continue

        terminal_result = await result_handler.handle({"job_id": job_id})
        if terminal_result.is_err:
            print_error(terminal_result.error.message)
            return 1
        _print_tool_result(terminal_result.value)
        return 1 if snapshot.is_error or terminal_result.value.is_error else 0


async def _dispatch_prompt(
    prompt: str,
    *,
    runtime: str,
    llm_backend: str | None,
    cwd: Path,
) -> int:
    resolved = resolve_skill_dispatch(ResolveRequest(prompt=prompt, cwd=cwd))
    if isinstance(resolved, NotHandled):
        return _UNSUPPORTED_DISPATCH_EXIT_CODE
    if isinstance(resolved, InvalidSkill):
        if resolved.reason.startswith("missing required frontmatter key:"):
            return _UNSUPPORTED_DISPATCH_EXIT_CODE
        print_error(f"Invalid Ouroboros skill command: {resolved.reason} ({resolved.skill_path})")
        return 2

    dispatcher = create_codex_command_dispatcher(
        cwd=cwd,
        runtime_backend=runtime,
        llm_backend=llm_backend,
    )
    messages = await dispatcher(resolved, None)
    if not messages:
        print_error(f"Ouroboros dispatch produced no output for: {resolved.command_prefix}")
        return 1

    result_message = _result_message(messages)
    exit_code = 0
    for message in messages:
        if message.type == "result":
            if message.content:
                console.print(message.content)
            if message.data.get("tool_error") or message.data.get("subtype") == "error":
                exit_code = 1
    if (
        exit_code == 0
        and resolved.mcp_tool == "ouroboros_start_auto"
        and result_message is not None
        and isinstance(result_message.data.get("job_id"), str)
    ):
        exit_code = await _monitor_auto_job(
            result_message.data["job_id"],
            cursor=_coerce_cursor(result_message.data.get("cursor")),
        )
    return exit_code


def dispatch_command(
    prompt_parts: Annotated[
        list[str],
        typer.Argument(help="The full ooo command to dispatch."),
    ],
    runtime: Annotated[
        str,
        typer.Option(
            "--runtime",
            help="Runtime backend to bind the MCP dispatch server to.",
        ),
    ] = "pi",
    llm_backend: Annotated[
        str | None,
        typer.Option(
            "--llm-backend",
            help="Optional LLM backend override for authoring/evaluation handlers.",
        ),
    ] = None,
    cwd: Annotated[
        Path | None,
        typer.Option(
            "--cwd",
            help="Working directory to resolve skill dispatch and handler context from.",
        ),
    ] = None,
) -> None:
    """Dispatch an exact-prefix ``ooo`` command through Ouroboros MCP handlers.

    This command is intentionally hidden from normal help. It gives external
    frontdoors such as Pi extensions a deterministic bridge into the same
    shared skill router used by subprocess runtimes.
    """
    prompt = _join_prompt(prompt_parts)
    if not prompt:
        print_error("Usage: ouroboros dispatch 'ooo <command> [args...]'")
        raise typer.Exit(2)

    exit_code = asyncio.run(
        _dispatch_prompt(
            prompt,
            runtime=runtime,
            llm_backend=llm_backend,
            cwd=(cwd or Path.cwd()).resolve(),
        )
    )
    if exit_code:
        raise typer.Exit(exit_code)


__all__ = ["dispatch_command"]
