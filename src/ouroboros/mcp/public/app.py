"""Composition root for the focused public ChatGPT Work MCP server."""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.public.policy import (
    PUBLIC_TOOL_FIELDS,
    PUBLIC_TOOL_POLICIES,
    PublicInputError,
    validate_public_arguments,
)
from ouroboros.mcp.server.adapter import MCPServerAdapter
from ouroboros.mcp.server.protocol import ToolHandler
from ouroboros.mcp.types import MCPToolDefinition, MCPToolResult

try:
    from mcp.types import ToolAnnotations
except ImportError:  # pragma: no cover - public serving requires the mcp extra.
    ToolAnnotations = None  # type: ignore[assignment,misc]

PUBLIC_INSTRUCTIONS = (
    "Use Ouroboros when the user wants to define one work outcome, approve its criteria, "
    "evaluate a supplied result, or resume an existing Ouroboros session. Use only content "
    "the user explicitly provides in the current conversation."
)

PUBLIC_TOOL_DESCRIPTIONS = {
    "ouroboros_interview": (
        "Clarify the user's stated work outcome through a focused interview. "
        "Start with initial_context, continue with session_id and answer, and return the next question."
    ),
    "ouroboros_generate_seed": (
        "Turn a completed interview session into an approval-ready Seed with the goal, constraints, "
        "acceptance criteria, and client gates."
    ),
    "ouroboros_evaluate": (
        "Evaluate user-provided work against the approved acceptance criteria and return the verdict, "
        "evidence, and remaining gaps."
    ),
    "ouroboros_session_status": (
        "Retrieve the current state of an Ouroboros work session using its session_id."
    ),
}

PUBLIC_META_FIELDS = frozenset({"session_id", "status", "ambiguity_score", "score"})


def _sanitize_public_result(result: MCPToolResult) -> MCPToolResult:
    """Remove internal orchestration and provider metadata from public responses."""
    return MCPToolResult(
        content=result.content,
        is_error=result.is_error,
        meta={key: value for key, value in result.meta.items() if key in PUBLIC_META_FIELDS},
        structured_content=None,
    )


class PublicToolHandler:
    """Validate public input before delegating to the existing Full handler."""

    def __init__(self, handler: ToolHandler) -> None:
        self._handler = handler
        self._temporary_directories: list[tempfile.TemporaryDirectory[str]] = []
        definition = handler.definition
        self.definition = MCPToolDefinition(
            name=definition.name,
            description=PUBLIC_TOOL_DESCRIPTIONS[definition.name],
            parameters=tuple(
                parameter
                for parameter in definition.parameters
                if parameter.name in PUBLIC_TOOL_FIELDS[definition.name]
            ),
            server_name=definition.server_name,
        )
        policy = PUBLIC_TOOL_POLICIES[self.definition.name]
        self.annotations = (
            ToolAnnotations(
                readOnlyHint=policy.read_only,
                openWorldHint=policy.open_world,
                destructiveHint=policy.destructive,
                idempotentHint=policy.read_only,
            )
            if ToolAnnotations is not None
            else None
        )

    async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, MCPServerError]:
        try:
            validate_public_arguments(arguments, tool_name=self.definition.name)
        except PublicInputError as exc:
            return Result.err(MCPToolError(str(exc), tool_name=self.definition.name))
        bounded = dict(arguments)
        if self.definition.name == "ouroboros_interview" and bounded.get("initial_context"):
            bounded["initial_context"] = "User-provided context:\n" + str(
                bounded["initial_context"]
            )
        if self.definition.name == "ouroboros_evaluate":
            temporary_directory = tempfile.TemporaryDirectory(prefix="ouroboros-public-eval-")
            self._temporary_directories.append(temporary_directory)
            bounded["working_dir"] = temporary_directory.name
        result = await self._handler.handle(bounded)
        if result.is_err:
            return result
        return Result.ok(_sanitize_public_result(result.value))

    async def close(self) -> None:
        """Remove hosted evaluation directories after all public work stops."""
        for directory in self._temporary_directories:
            directory.cleanup()
        self._temporary_directories.clear()


class _OwnedFullServer:
    """Adapt a Full server's shutdown contract to the owned-resource protocol."""

    def __init__(self, server: MCPServerAdapter) -> None:
        self._server = server

    async def close(self) -> None:
        await self._server.shutdown()


def create_public_server(*, source: MCPServerAdapter | None = None) -> MCPServerAdapter:
    """Create a public server by reusing selected handlers from Ouroboros Full."""
    if source is None:
        from ouroboros.mcp.server.adapter import create_ouroboros_server

        source = create_ouroboros_server()

    public = MCPServerAdapter(
        name="ouroboros-work",
        version=source.info.version,
        instructions=PUBLIC_INSTRUCTIONS,
    )
    missing: list[str] = []
    for name in PUBLIC_TOOL_POLICIES:
        handler = source.get_tool_handler(name)
        if handler is None:
            missing.append(name)
            continue
        public_handler = PublicToolHandler(handler)
        public.register_tool(public_handler)
        public.register_owned_resource(public_handler)
    if missing:
        raise ValueError(f"missing required Full tool: {', '.join(missing)}")
    public.register_owned_resource(_OwnedFullServer(source))
    return public


def public_bind_settings() -> tuple[str, int]:
    """Read the container bind address for the public Streamable HTTP service."""
    host = os.environ.get("OUROBOROS_PUBLIC_HOST", "127.0.0.1")
    raw_port = os.environ.get("OUROBOROS_PUBLIC_PORT", "8080")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("OUROBOROS_PUBLIC_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("OUROBOROS_PUBLIC_PORT must be between 1 and 65535")
    if host not in {"127.0.0.1", "localhost", "::1"} and os.environ.get(
        "OUROBOROS_PUBLIC_BEHIND_AUTH_GATEWAY"
    ) != "1":
        raise ValueError(
            "non-loopback public bind requires OUROBOROS_PUBLIC_BEHIND_AUTH_GATEWAY=1"
        )
    return host, port


async def serve_public() -> None:
    """Serve the focused public adapter over Streamable HTTP."""
    host, port = public_bind_settings()
    server = create_public_server()
    try:
        await server.serve(
            transport="streamable-http",
            host=host,
            port=port,
        )
    finally:
        await server.shutdown()


def main() -> None:
    """Process entrypoint for hosted deployments."""
    asyncio.run(serve_public())
