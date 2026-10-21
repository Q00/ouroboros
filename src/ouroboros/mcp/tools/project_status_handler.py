"""Read-only MCP surface for the cross-run Project Map projection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, cast

import structlog

from ouroboros.core.project_identity import resolve_project_identity
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.types import (
    ContentType,
    JSONObject,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.persistence.event_store import EventStore
from ouroboros.project_map import ProjectMapBuilder, ProjectRecord

log = structlog.get_logger(__name__)

_TOOL_NAME = "ouroboros_project_status"
_DEFAULT_RUN_LIMIT = 100


@dataclass
class ProjectStatusHandler:
    """Build one complete Project Map record without mutating durable state."""

    event_store: EventStore | None = field(default=None, repr=False)
    default_project_dir: str | Path | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the read-only Project Map tool definition."""
        return MCPToolDefinition(
            name=_TOOL_NAME,
            title="Project status",
            description=(
                "Resolve a project identity and rebuild its complete read-only run status "
                "from persisted Ouroboros session events. Identity conflicts, projection "
                "failures, and limits below the complete population return no partial record."
            ),
            parameters=(
                MCPToolParameter(
                    name="project_dir",
                    type=ToolInputType.STRING,
                    description=(
                        "Project or workspace directory. Defaults to the MCP server caller "
                        "directory captured at startup."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="workspace",
                    type=ToolInputType.STRING,
                    description="Optional canonical project-relative workspace filter.",
                    required=False,
                ),
                MCPToolParameter(
                    name="limit",
                    type=ToolInputType.INTEGER,
                    description=(
                        "Complete-run safety cap. The query fails instead of truncating when "
                        "the matching population exceeds this value."
                    ),
                    required=False,
                    default=_DEFAULT_RUN_LIMIT,
                ),
            ),
            output_schema=cast(JSONObject, ProjectRecord.model_json_schema(mode="serialization")),
            annotations={
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Resolve and project one project, failing closed on incomplete reads."""
        raw_project_dir = arguments.get("project_dir")
        if raw_project_dir is None:
            raw_project_dir = self.default_project_dir
        if raw_project_dir is None:
            try:
                raw_project_dir = Path.cwd()
            except OSError as exc:
                return _tool_error(f"Cannot determine caller project directory: {exc}")
        if not isinstance(raw_project_dir, (str, Path)) or not str(raw_project_dir).strip():
            return _tool_error("project_dir must be a non-empty path")

        workspace = arguments.get("workspace")
        if workspace is not None and (
            not isinstance(workspace, str) or not workspace or workspace != workspace.strip()
        ):
            return _tool_error("workspace must be a non-empty canonical relative path")

        raw_limit = arguments.get("limit", _DEFAULT_RUN_LIMIT)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit < 1:
            return _tool_error("limit must be a positive integer")

        store = self.event_store
        owns_event_store = False
        try:
            project = await asyncio.to_thread(resolve_project_identity, raw_project_dir)
            if store is None:
                store = EventStore(read_only=True)
                owns_event_store = True
            await store.initialize(create_schema=False)
            record = await ProjectMapBuilder(store).build(
                project,
                workspace_path=workspace,
                limit=raw_limit,
            )
            payload = cast(JSONObject, record.model_dump(mode="json"))
            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=format_project_status_text(record),
                        ),
                    ),
                    structured_content=payload,
                    meta={
                        "read_only": True,
                        "project_id": record.project_id,
                        "run_count": record.run_count,
                    },
                )
            )
        except Exception as exc:
            log.error(
                "mcp.tool.project_status.error",
                project_dir=str(raw_project_dir),
                error=str(exc),
            )
            return _tool_error(f"Failed to query project status: {exc}")
        finally:
            if owns_event_store and store is not None:
                await store.close()


def format_project_status_text(record: ProjectRecord) -> str:
    """Render deterministic human-readable text from one complete record."""
    workspace = record.workspace_path if record.workspace_path is not None else "all"
    lines = [
        "Project Status",
        "==============",
        f"Project: {_json_string(record.project_id)}",
        f"Root: {_json_string(record.project_root)}",
        f"Workspace: {_json_string(workspace)}",
        f"Runs: {record.run_count}",
        "",
        "Ambiguity gate:",
        f"  Gated Seeds: {record.gated_seed_count}",
        f"  Forced Seeds: {record.forced_seed_count}",
        f"  Unknown Seeds: {record.unknown_seed_count}",
        f"  Override rate: {_format_override_rate(record.override_rate)}",
    ]
    if record.runs:
        lines.extend(("", "Run history:"))
        for run in record.runs:
            lines.append(
                "- "
                f"{run.started_at.isoformat()} "
                f"session={_json_string(run.session_id)} "
                f"execution={_json_string(run.execution_id)} "
                f"status={run.status.value} "
                f"workspace={_json_string(run.workspace_path)}"
            )
    return "\n".join(lines) + "\n"


def _json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _format_override_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _tool_error(message: str) -> Result[MCPToolResult, MCPServerError]:
    return Result.err(MCPToolError(message, tool_name=_TOOL_NAME))


__all__ = ["ProjectStatusHandler", "format_project_status_text"]
