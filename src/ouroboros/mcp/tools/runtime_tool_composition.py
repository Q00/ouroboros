"""Configured handler graph for runtime-owned builtin MCP interception.

Capability discovery intentionally uses lightweight handler constructors. A
runtime interceptor, however, executes those handlers and therefore needs the
same configured run -> evaluate -> Ralph -> evolve graph as the MCP server.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import inspect
from typing import Any

_COMPOSING_RUNTIME_TOOLS: ContextVar[bool] = ContextVar(
    "ouroboros_composing_runtime_tools",
    default=False,
)


class ConfiguredRuntimeTools(tuple[Any, ...]):
    """Configured handlers plus the composition root that owns their resources.

    Builtin runtimes index the handlers into a local registry, but the handlers
    retain EventStore, ControlBus, bridge, and JobManager dependencies owned by
    the temporary :class:`MCPServerAdapter`.  Keeping that adapter here provides
    a deterministic shutdown path instead of discarding the only resource owner.
    """

    server_owner: Any
    _closed: bool
    _shutdown_lock: asyncio.Lock

    def __new__(
        cls,
        handlers: tuple[Any, ...],
        *,
        server_owner: Any,
    ) -> ConfiguredRuntimeTools:
        instance = super().__new__(cls, handlers)
        instance.server_owner = server_owner
        instance._closed = False
        instance._shutdown_lock = asyncio.Lock()
        return instance

    async def shutdown(self) -> None:
        """Drain live jobs, then close the composed resources exactly once."""
        async with self._shutdown_lock:
            if self._closed:
                return
            job_manager = getattr(self.server_owner, "job_manager", None)
            drain = getattr(job_manager, "drain", None)
            if callable(drain):
                await drain()
            await self.server_owner.shutdown()
            self._closed = True


def _retain_runtime_tool_composition(
    runtime_adapter: Any,
    composition: ConfiguredRuntimeTools,
) -> None:
    existing = getattr(runtime_adapter, "_builtin_mcp_tool_composition", None)
    if existing is not None and existing is not composition:
        raise RuntimeError("Builtin MCP tool composition is already configured")
    runtime_adapter._builtin_mcp_tool_composition = composition  # noqa: SLF001
    previous_aclose = getattr(runtime_adapter, "aclose", None)

    async def close_runtime() -> None:
        await shutdown_configured_runtime_tools(runtime_adapter)
        if getattr(runtime_adapter, "aclose", None) is close_runtime:
            runtime_adapter.aclose = previous_aclose
        if inspect.iscoroutinefunction(previous_aclose):
            await previous_aclose()

    runtime_adapter.aclose = close_runtime


async def shutdown_configured_runtime_tools(runtime_adapter: Any) -> None:
    """Release and clear a runtime's configured builtin handler resources."""
    composition = getattr(runtime_adapter, "_builtin_mcp_tool_composition", None)
    if composition is None:
        return
    await composition.shutdown()
    if getattr(runtime_adapter, "_builtin_mcp_tool_composition", None) is composition:
        runtime_adapter._builtin_mcp_tool_composition = None  # noqa: SLF001
        if hasattr(runtime_adapter, "_builtin_mcp_handlers"):
            runtime_adapter._builtin_mcp_handlers = None  # noqa: SLF001


def lightweight_runtime_tool_map(
    *, runtime_backend: str | None, llm_backend: str | None
) -> dict[str, Any]:
    """Build the side-effect-free handler map used for authority discovery."""
    from ouroboros.mcp.tools.definitions import get_ouroboros_tools

    return {
        handler.definition.name: handler
        for handler in get_ouroboros_tools(
            runtime_backend=runtime_backend,
            llm_backend=llm_backend,
        )
    }


def configured_runtime_tools(
    *,
    runtime_backend: str | None,
    llm_backend: str | None,
    opencode_mode: str | None,
    include_auto: bool,
    mcp_bridge: Any | None,
    runtime_adapter: Any | None = None,
) -> ConfiguredRuntimeTools | None:
    """Return the production handler graph, or ``None`` for lightweight fallback.

    ``create_ouroboros_server`` constructs runtime adapters whose registry
    fingerprint calls back into ``get_ouroboros_tools``. The ContextVar makes
    that nested discovery use lightweight definitions while the outer call
    finishes the real graph.
    """

    # These runtimes execute handlers from their builtin registry rather than
    # using it only for capability discovery. They therefore need the complete
    # parent-owned run -> evaluate -> Ralph -> evolve graph. OpenCode also uses
    # the graph as the process-local vault behind its opaque hidden-Seed handoff.
    executing_builtin_runtime = runtime_adapter is not None and (
        runtime_backend in {"codex", "hermes"}
        or (runtime_backend == "opencode" and opencode_mode == "plugin")
    )
    if not executing_builtin_runtime or _COMPOSING_RUNTIME_TOOLS.get():
        return None

    from ouroboros.mcp.server.adapter import create_ouroboros_server
    from ouroboros.mcp.tools.evaluation_handlers import ChecklistVerifyHandler

    compose_token = _COMPOSING_RUNTIME_TOOLS.set(True)
    try:
        try:
            server = create_ouroboros_server(
                runtime_backend=runtime_backend,
                llm_backend=llm_backend,
                opencode_mode=opencode_mode,
                mcp_bridge=mcp_bridge,
                runtime_adapter=runtime_adapter,
                # Embedded interceptors own their event loop. Preserve this
                # factory's historical in-process JobManager behavior.
                durable_jobs=False,
            )
        except RuntimeError as exc:
            # Capability discovery stays importable when an optional provider
            # extra is absent. Actual calls cannot use that backend either.
            if isinstance(exc.__cause__, ImportError):
                return None
            raise
    finally:
        _COMPOSING_RUNTIME_TOOLS.reset(compose_token)

    configured = dict(server._tool_handlers)  # noqa: SLF001 - composition reuse
    configured["ouroboros_checklist_verify"] = ChecklistVerifyHandler(
        evaluate_handler=configured["ouroboros_evaluate"],
        llm_backend=llm_backend,
    )
    ordered_names = (
        "ouroboros_execute_seed",
        "ouroboros_start_execute_seed",
        *(("ouroboros_auto", "ouroboros_start_auto") if include_auto else ()),
        "ouroboros_session_status",
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_job_result",
        "ouroboros_ac_tree_hud",
        "ouroboros_cancel_job",
        "ouroboros_query_events",
        "ouroboros_query_projection",
        "ouroboros_project_status",
        "ouroboros_generate_seed",
        "ouroboros_measure_drift",
        "ouroboros_interview",
        "ouroboros_evaluate",
        "ouroboros_start_evaluate",
        "ouroboros_checklist_verify",
        "ouroboros_lateral_think",
        "ouroboros_submit_fanout_results",
        "ouroboros_evolve_step",
        "ouroboros_start_evolve_step",
        "ouroboros_ralph",
        "ouroboros_start_ralph",
        "ouroboros_lineage_status",
        "ouroboros_evolve_rewind",
        "ouroboros_cancel_execution",
        "ouroboros_brownfield",
        "ouroboros_pm_interview",
        "ouroboros_qa",
    )
    tools = ConfiguredRuntimeTools(
        tuple(configured[name] for name in ordered_names),
        server_owner=server,
    )
    if runtime_adapter is not None:
        _retain_runtime_tool_composition(runtime_adapter, tools)
    return tools
