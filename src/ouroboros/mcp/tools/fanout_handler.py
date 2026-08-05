"""MCP re-entry handler for bounded disposable fan-out synthesis."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any

import structlog

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.tools.fanout import (
    FanoutRegistry,
    PreparedFanoutSynthesis,
    prepare_fanout_results,
    synthesize_fanout_results,
)
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.orchestrator.agent_process import AgentProcessHandle
from ouroboros.orchestrator.disposable_memory import DisposableMemory

log = structlog.get_logger(__name__)

_FANOUT_SYNTHESIS_INPUT_SCHEMA = "ouroboros.mcp.fanout-synthesis-input.v1"
_FANOUT_SYNTHESIS_RUNTIME_ID = "mcp:fanout-synthesis:v2"


def _fanout_synthesis_contract_id(prepared: PreparedFanoutSynthesis) -> str:
    """Bind disposable replay authority to the complete validated request."""
    canonical_input = json.dumps(
        {
            "schema": _FANOUT_SYNTHESIS_INPUT_SCHEMA,
            "fanout_id": prepared.fanout_id,
            "record": prepared.record.to_dict(),
            "provided": prepared.provided,
            "completion_report": prepared.completion_report,
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"fanout:{hashlib.sha256(canonical_input).hexdigest()}"


@dataclass
class SubmitFanoutResultsHandler:
    """Validate fan-out re-entry and publish terminal synthesis by reference."""

    fanout_registry: FanoutRegistry | None = field(default=None, repr=False)
    disposable_memory: DisposableMemory | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._registry = self.fanout_registry or FanoutRegistry()

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the public re-entry contract."""
        return MCPToolDefinition(
            name="ouroboros_submit_fanout_results",
            description=(
                "Submit correlated results from a subagent fan-out back to "
                "Ouroboros. After spawning the advisory/persona/investigation "
                "subagents declared by a prior tool's `meta` (which stamped a "
                "`fanout_id` and a `result_correlation_key`), call this tool with "
                "one {key, content} per child output — `key` is the value of the "
                "correlation field for that child. A child you could not spawn "
                "at all is exactly {key, undispatched: true}; never invent output. "
                "Missing required keys return `status=partial`; retry with EVERY lane. "
                "A complete submission returns a bounded disposable artifact envelope; "
                "fetch its body explicitly with `ouroboros artifacts fetch CONTRACT_ID`."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="Interview/lateral session id the fan-out belongs to.",
                    required=False,
                ),
                MCPToolParameter(
                    name="fanout_id",
                    type=ToolInputType.STRING,
                    description="The fanout_id stamped into the originating tool's meta.",
                    required=True,
                ),
                MCPToolParameter(
                    name="correlation_key",
                    type=ToolInputType.STRING,
                    description=(
                        "The result_correlation_key from the originating meta "
                        "(e.g. 'context.persona' or 'code_facts')."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="results",
                    type=ToolInputType.ARRAY,
                    description=(
                        "Correlated child outputs: objects with a 'key' (the "
                        "correlation value) and a 'content' (the child result), "
                        "or 'undispatched': true when the child never ran."
                    ),
                    required=True,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Return partial validation inline or terminal synthesis by reference."""
        fanout_id = str(arguments.get("fanout_id") or "").strip()
        if not fanout_id:
            return Result.err(
                MCPToolError(
                    "fanout_id is required",
                    tool_name="ouroboros_submit_fanout_results",
                )
            )

        raw_results = arguments.get("results")
        if not isinstance(raw_results, (list, tuple)):
            return Result.err(
                MCPToolError(
                    "results must be a list of {key, content} objects",
                    tool_name="ouroboros_submit_fanout_results",
                )
            )
        prepared = prepare_fanout_results(
            self._registry,
            session_id=str(arguments.get("session_id") or ""),
            correlation_key=str(arguments.get("correlation_key") or ""),
            results=list(raw_results),
            fanout_id=fanout_id,
        )
        outcome = await self._publish_or_synthesize(prepared, fanout_id=fanout_id)
        if isinstance(outcome, MCPToolError):
            return Result.err(outcome)
        if outcome.get("status") == "unknown_fanout_id":
            return Result.err(
                MCPToolError(
                    str(outcome.get("error") or "unknown fanout_id"),
                    tool_name="ouroboros_submit_fanout_results",
                )
            )
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=json.dumps(outcome, ensure_ascii=False, sort_keys=True),
                    ),
                ),
                is_error=False,
                meta=outcome,
            )
        )

    async def _publish_or_synthesize(
        self,
        prepared: dict[str, Any] | PreparedFanoutSynthesis,
        *,
        fanout_id: str,
    ) -> dict[str, Any] | MCPToolError:
        if not isinstance(prepared, PreparedFanoutSynthesis):
            return prepared
        if self.disposable_memory is None:
            return MCPToolError(
                "terminal fan-out synthesis requires a configured disposable artifact service",
                tool_name="ouroboros_submit_fanout_results",
            )

        async def synthesize(_handle: AgentProcessHandle) -> dict[str, Any]:
            return synthesize_fanout_results(prepared)

        try:
            contract_id = _fanout_synthesis_contract_id(prepared)
            envelope = await self.disposable_memory.run(
                intent=f"Synthesize terminal fan-out {fanout_id}",
                runtime_id=_FANOUT_SYNTHESIS_RUNTIME_ID,
                work_fn=synthesize,
                contract_id=contract_id,
            )
        except Exception as exc:
            log.error(
                "mcp.tool.submit_fanout_results.publication_failed",
                fanout_id=fanout_id,
                error=str(exc),
            )
            return MCPToolError(
                f"fan-out result publication failed: {exc}",
                tool_name="ouroboros_submit_fanout_results",
            )
        return envelope.model_dump(mode="json")


__all__ = ["SubmitFanoutResultsHandler"]
