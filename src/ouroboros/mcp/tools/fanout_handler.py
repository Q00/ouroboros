"""MCP re-entry handler for bounded disposable fan-out synthesis."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import hashlib
import json
from typing import TYPE_CHECKING, Any

import structlog

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.telemetry_boundary import record_subagent_dispatch_submitted
from ouroboros.mcp.tools.fanout import (
    FANOUT_KIND_HOST_EXECUTION,
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
from ouroboros.orchestrator.host_dispatch import HOST_EXECUTION_RESULT_KEY
from ouroboros.persistence.artifact_errors import ArtifactStoreError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ouroboros.persistence.artifact_store import ArtifactStore

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
    # HostDispatchBridge from the MCP composition root. Execution-kind
    # submissions wake the parked runtime waiter through it instead of running
    # advisory synthesis; ``None`` (a root with no bridge) fails them closed.
    host_dispatch_bridge: Any | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._registry = self.fanout_registry or FanoutRegistry()

    @property
    def artifact_store(self) -> ArtifactStore | None:
        """Return the store this handler publishes into, or ``None`` if it cannot.

        Handed to advisory producers so a reader asks the same store that wrote,
        rather than deriving a path from the workspace both were built from.
        Two derivations are not one address: this side resolves when it is
        constructed and a producer would resolve when a question is asked, so a
        relative workspace and a change of process directory in between would
        put the reader and the writer in different places.

        The store rather than its root, because what a reader needs from it is
        not only where to look: publication time, membership and bounded reads
        are all things the store already answers, and re-deriving them beside it
        is what produced the review round this replaced.
        """
        if self.disposable_memory is None:
            return None
        return self.disposable_memory.artifact_store

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
                "Completed advisory fan-outs return a bounded disposable artifact "
                "envelope for `ouroboros_fetch_artifact`. Host-execution submissions "
                "instead acknowledge delivery to the execution engine; keep polling "
                "`ouroboros_job_wait` for verification and further dispatches."
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
        record = self._registry.load(fanout_id)
        expected_count = len(record.expected_keys) if record is not None else 0
        received_count = sum(
            1
            for item in raw_results
            if isinstance(item, dict) and "content" in item and "undispatched" not in item
        )
        undispatched_count = sum(
            1 for item in raw_results if isinstance(item, dict) and item.get("undispatched") is True
        )
        outcome = await self._publish_or_synthesize(prepared, fanout_id=fanout_id)
        fanout_kind = record.kind if record is not None else "unknown"
        if isinstance(outcome, MCPToolError):
            record_subagent_dispatch_submitted(
                fanout_kind=fanout_kind,
                submission_status="publication_failed",
                expected_count=expected_count,
                received_count=received_count,
                undispatched_count=undispatched_count,
            )
            return Result.err(outcome)
        submission_status = (
            "complete"
            if isinstance(prepared, PreparedFanoutSynthesis)
            else str(outcome.get("status") or "unknown_kind")
        )
        record_subagent_dispatch_submitted(
            fanout_kind=fanout_kind,
            submission_status=submission_status,
            expected_count=expected_count,
            received_count=received_count,
            undispatched_count=undispatched_count,
        )
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
        if prepared.record.kind == FANOUT_KIND_HOST_EXECUTION:
            # Execution submissions are transport, not synthesis: deliver the
            # validated lane to the parked HostDispatchRuntime waiter. The
            # server-side verify gate — not this reply — judges the work.
            if self.host_dispatch_bridge is None:
                return MCPToolError(
                    "execution dispatch submission requires a composed "
                    "host-dispatch bridge (start the run through the MCP "
                    "server that issued this dispatch)",
                    tool_name="ouroboros_submit_fanout_results",
                )
            undispatched_keys = prepared.completion_report.get("undispatched_keys") or ()
            return self.host_dispatch_bridge.submit(
                fanout_id,
                prepared.provided,
                undispatched=HOST_EXECUTION_RESULT_KEY in undispatched_keys,
            )
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


@dataclass
class FetchArtifactHandler:
    """Expose explicit disposable-artifact reads to MCP-only hosts."""

    disposable_memory: DisposableMemory | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the public explicit-fetch contract."""
        return MCPToolDefinition(
            name="ouroboros_fetch_artifact",
            description=(
                "Fetch and integrity-check a disposable Ouroboros artifact by the "
                "contract_id returned in an artifact envelope, or offered to an "
                "advisory lane beside the lane_id that produced it. For fan-out "
                "completion, continue from the synthesis in the returned `body`. "
                "This is an explicit read and never re-executes the originating work."
            ),
            parameters=(
                MCPToolParameter(
                    name="contract_id",
                    type=ToolInputType.STRING,
                    description=(
                        "The contract_id from a disposable artifact envelope. Omit it "
                        "and pass lane_id alone to list instead: that lane's own "
                        "recent findings in this project, newest first, as "
                        "contract_ids to read back with this same tool."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="lane_id",
                    type=ToolInputType.STRING,
                    description=(
                        "Optional. Narrows a fan-out artifact to the output of one "
                        "lane, returning that lane's body alone. Pass the lane_id "
                        "offered beside the contract_id; omit it to read the whole "
                        "artifact. A supplied lane the artifact does not carry is "
                        "an error, never a broader read."
                    ),
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Fetch one verified body, or list what a lane published recently."""
        contract_id = str(arguments.get("contract_id") or "").strip()
        if not contract_id:
            # A lane on its own is the listing: which of its own findings exist
            # to be read. Sending that list in every prompt spent a fifth of it
            # on identifiers nothing could choose between, and a lane that
            # wants none of them paid for it anyway. The window and the cap are
            # the query's, not the caller's, so a wider read cannot be asked
            # for (RFC Q00/ouroboros#2167).
            lane = str(arguments.get("lane_id") or "").strip()
            if not lane:
                return Result.err(
                    MCPToolError(
                        "pass a contract_id to read one artifact, or a lane_id alone "
                        "to list what that lane published here recently",
                        tool_name="ouroboros_fetch_artifact",
                    )
                )
            if self.disposable_memory is None:
                return Result.err(
                    MCPToolError(
                        "artifact fetch requires a configured project artifact service",
                        tool_name="ouroboros_fetch_artifact",
                    )
                )
            from ouroboros.mcp.tools.recent_findings import recent_findings_by_lane

            found = await asyncio.to_thread(
                recent_findings_by_lane,
                self.disposable_memory.artifact_store,
                lanes={lane},
            )
            listing = {"lane_id": lane, "recent": found.get(lane, [])}
            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=json.dumps(listing, ensure_ascii=False, sort_keys=True),
                        ),
                    ),
                    is_error=False,
                    meta=listing,
                )
            )
        if self.disposable_memory is None:
            return Result.err(
                MCPToolError(
                    "artifact fetch requires a configured project artifact service",
                    tool_name="ouroboros_fetch_artifact",
                )
            )
        # Presence decides the path; the value is never coerced toward the
        # broader read.  Normalizing the argument first ("strip, then branch on
        # truthiness") turned a supplied-but-blank lane into an unscoped fetch
        # -- a malformed request quietly granted every sibling's output.  Here
        # only an absent or JSON-null argument means the legacy whole-artifact
        # read; anything supplied is looked up verbatim, and a lane no fan-out
        # ever dispatched (blank included) fails as not-found rather than
        # falling open.
        lane_argument = arguments.get("lane_id")
        lane_id = None if lane_argument is None else str(lane_argument)
        try:
            if lane_id is None:
                fetched = await asyncio.to_thread(self.disposable_memory.fetch, contract_id)
            else:
                fetched = await asyncio.to_thread(
                    self.disposable_memory.fetch_lane, contract_id, lane_id
                )
        except (ArtifactStoreError, OSError, ValueError) as exc:
            return Result.err(
                MCPToolError(
                    f"artifact fetch failed: {exc}",
                    tool_name="ouroboros_fetch_artifact",
                )
            )

        payload: dict[str, Any] = {
            "contract_id": fetched.envelope.contract_id,
            "body": fetched.body,
        }
        if lane_id is not None:
            payload["lane_id"] = lane_id
        return Result.ok(
            MCPToolResult(
                content=(
                    MCPContentItem(
                        type=ContentType.TEXT,
                        text=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    ),
                ),
                is_error=False,
                meta=payload,
            )
        )


def create_fanout_handler(
    fanout_registry: FanoutRegistry,
    project_dir: Any,
    event_store: Any,
    *,
    ensure_ready: Callable[[], Awaitable[None]] | None = None,
) -> SubmitFanoutResultsHandler:
    """Build the production fan-out boundary for a resolved workspace."""
    from ouroboros.persistence.artifact_store import ArtifactStore

    return SubmitFanoutResultsHandler(
        fanout_registry=fanout_registry,
        disposable_memory=DisposableMemory(
            artifact_store=ArtifactStore.for_project(project_dir),
            event_store=event_store,
            ensure_ready=ensure_ready,
        ),
    )


def create_artifact_fetch_handler(project_dir: Any) -> FetchArtifactHandler:
    """Build the production explicit-fetch boundary for a resolved workspace."""
    from ouroboros.persistence.artifact_store import ArtifactStore

    return FetchArtifactHandler(
        disposable_memory=DisposableMemory(
            artifact_store=ArtifactStore.for_project(project_dir),
        )
    )


def create_fanout_handlers(
    fanout_registry: FanoutRegistry,
    project_dir: Any,
    event_store: Any,
    *,
    ensure_ready: Callable[[], Awaitable[None]] | None = None,
) -> tuple[SubmitFanoutResultsHandler, FetchArtifactHandler]:
    """Build the paired submit/fetch production boundary for one workspace."""
    submit = create_fanout_handler(
        fanout_registry,
        project_dir,
        event_store,
        ensure_ready=ensure_ready,
    )
    return submit, FetchArtifactHandler(disposable_memory=submit.disposable_memory)


__all__ = [
    "FetchArtifactHandler",
    "SubmitFanoutResultsHandler",
    "create_artifact_fetch_handler",
    "create_fanout_handler",
    "create_fanout_handlers",
]
