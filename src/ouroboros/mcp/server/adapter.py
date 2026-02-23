"""MCP Server adapter implementation.

This module provides the MCPServerAdapter class that implements the MCPServer
protocol using the MCP SDK (FastMCP). It handles tool registration, resource
handling, and server lifecycle.
"""

import asyncio
from collections.abc import Sequence
from typing import Any

import structlog

from ouroboros.core.types import Result
from ouroboros.mcp.errors import (
    MCPResourceNotFoundError,
    MCPServerError,
    MCPToolError,
)
from ouroboros.mcp.server.protocol import PromptHandler, ResourceHandler, ToolHandler
from ouroboros.mcp.server.security import AuthConfig, RateLimitConfig, SecurityLayer
from ouroboros.mcp.types import (
    MCPCapabilities,
    MCPPromptDefinition,
    MCPResourceContent,
    MCPResourceDefinition,
    MCPServerInfo,
    MCPToolDefinition,
    MCPToolResult,
)

log = structlog.get_logger(__name__)


class MCPServerAdapter:
    """Concrete implementation of MCPServer protocol.

    Uses the MCP SDK to expose Ouroboros functionality as an MCP server.
    Supports tool registration, resource handling, and optional security.

    Example:
        server = MCPServerAdapter(
            name="ouroboros-mcp",
            version="1.0.0",
        )

        # Register handlers
        server.register_tool(ExecuteSeedHandler())
        server.register_resource(SessionResourceHandler())

        # Start serving
        await server.serve()
    """

    def __init__(
        self,
        *,
        name: str = "ouroboros-mcp",
        version: str = "1.0.0",
        auth_config: AuthConfig | None = None,
        rate_limit_config: RateLimitConfig | None = None,
    ) -> None:
        """Initialize the server adapter.

        Args:
            name: Server name for identification.
            version: Server version.
            auth_config: Optional authentication configuration.
            rate_limit_config: Optional rate limiting configuration.
        """
        self._name = name
        self._version = version
        self._tool_handlers: dict[str, ToolHandler] = {}
        self._resource_handlers: dict[str, ResourceHandler] = {}
        self._prompt_handlers: dict[str, PromptHandler] = {}
        self._mcp_server: Any = None

        # Initialize security layer
        self._security = SecurityLayer(
            auth_config=auth_config or AuthConfig(),
            rate_limit_config=rate_limit_config or RateLimitConfig(),
        )

    @property
    def info(self) -> MCPServerInfo:
        """Return server information."""
        return MCPServerInfo(
            name=self._name,
            version=self._version,
            capabilities=MCPCapabilities(
                tools=len(self._tool_handlers) > 0,
                resources=len(self._resource_handlers) > 0,
                prompts=len(self._prompt_handlers) > 0,
                logging=True,
            ),
            tools=tuple(h.definition for h in self._tool_handlers.values()),
            resources=tuple(
                defn for handler in self._resource_handlers.values() for defn in handler.definitions
            ),
            prompts=tuple(h.definition for h in self._prompt_handlers.values()),
        )

    def register_tool(self, handler: ToolHandler) -> None:
        """Register a tool handler.

        Args:
            handler: The tool handler to register.
        """
        name = handler.definition.name
        self._tool_handlers[name] = handler
        log.info("mcp.server.tool_registered", tool=name)

    def register_resource(self, handler: ResourceHandler) -> None:
        """Register a resource handler.

        Args:
            handler: The resource handler to register.
        """
        for defn in handler.definitions:
            self._resource_handlers[defn.uri] = handler
            log.info("mcp.server.resource_registered", uri=defn.uri)

    def register_prompt(self, handler: PromptHandler) -> None:
        """Register a prompt handler.

        Args:
            handler: The prompt handler to register.
        """
        name = handler.definition.name
        self._prompt_handlers[name] = handler
        log.info("mcp.server.prompt_registered", prompt=name)

    async def list_tools(self) -> Sequence[MCPToolDefinition]:
        """List all registered tools.

        Returns:
            Sequence of tool definitions.
        """
        return tuple(h.definition for h in self._tool_handlers.values())

    async def list_resources(self) -> Sequence[MCPResourceDefinition]:
        """List all registered resources.

        Returns:
            Sequence of resource definitions.
        """
        # Collect unique definitions from all handlers
        seen_uris: set[str] = set()
        definitions: list[MCPResourceDefinition] = []

        for handler in self._resource_handlers.values():
            for defn in handler.definitions:
                if defn.uri not in seen_uris:
                    seen_uris.add(defn.uri)
                    definitions.append(defn)

        return definitions

    async def list_prompts(self) -> Sequence[MCPPromptDefinition]:
        """List all registered prompts.

        Returns:
            Sequence of prompt definitions.
        """
        return tuple(h.definition for h in self._prompt_handlers.values())

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        credentials: dict[str, str] | None = None,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Call a registered tool.

        Args:
            name: Name of the tool to call.
            arguments: Arguments for the tool.
            credentials: Optional credentials for authentication.

        Returns:
            Result containing the tool result or an error.
        """
        handler = self._tool_handlers.get(name)
        if not handler:
            return Result.err(
                MCPResourceNotFoundError(
                    f"Tool not found: {name}",
                    server_name=self._name,
                    resource_type="tool",
                    resource_id=name,
                )
            )

        # Security check
        security_result = await self._security.check_request(name, arguments, credentials)
        if security_result.is_err:
            return Result.err(security_result.error)

        try:
            timeout = getattr(handler, "TIMEOUT_SECONDS", 30.0)
            result = await asyncio.wait_for(handler.handle(arguments), timeout=timeout)
            return result
        except TimeoutError:
            log.error("mcp.server.tool_timeout", tool=name)
            return Result.err(
                MCPToolError(
                    f"Tool execution timed out after {timeout}s: {name}",
                    server_name=self._name,
                    tool_name=name,
                )
            )
        except Exception as e:
            log.error("mcp.server.tool_error", tool=name, error=str(e))
            return Result.err(
                MCPToolError(
                    f"Tool execution failed: {e}",
                    server_name=self._name,
                    tool_name=name,
                )
            )

    async def read_resource(
        self,
        uri: str,
    ) -> Result[MCPResourceContent, MCPServerError]:
        """Read a registered resource.

        Args:
            uri: URI of the resource to read.

        Returns:
            Result containing the resource content or an error.
        """
        handler = self._resource_handlers.get(uri)
        if not handler:
            return Result.err(
                MCPResourceNotFoundError(
                    f"Resource not found: {uri}",
                    server_name=self._name,
                    resource_type="resource",
                    resource_id=uri,
                )
            )

        try:
            result = await handler.handle(uri)
            return result
        except Exception as e:
            log.error("mcp.server.resource_error", uri=uri, error=str(e))
            return Result.err(
                MCPServerError(
                    f"Resource read failed: {e}",
                    server_name=self._name,
                )
            )

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str],
    ) -> Result[str, MCPServerError]:
        """Get a filled prompt.

        Args:
            name: Name of the prompt.
            arguments: Arguments to fill in the template.

        Returns:
            Result containing the filled prompt or an error.
        """
        handler = self._prompt_handlers.get(name)
        if not handler:
            return Result.err(
                MCPResourceNotFoundError(
                    f"Prompt not found: {name}",
                    server_name=self._name,
                    resource_type="prompt",
                    resource_id=name,
                )
            )

        try:
            result = await handler.handle(arguments)
            return result
        except Exception as e:
            log.error("mcp.server.prompt_error", prompt=name, error=str(e))
            return Result.err(
                MCPServerError(
                    f"Prompt generation failed: {e}",
                    server_name=self._name,
                )
            )

    async def serve(
        self,
        transport: str = "stdio",
        host: str = "localhost",
        port: int = 8080,
    ) -> None:
        """Start serving MCP requests.

        This method blocks until the server is stopped.
        Uses the MCP SDK's FastMCP server implementation.

        Args:
            transport: Transport type - "stdio" or "sse".
            host: Host to bind to (SSE only).
            port: Port to bind to (SSE only).
        """
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError as e:
            msg = "mcp package not installed. Install with: pip install mcp"
            raise ImportError(msg) from e

        # Create FastMCP server
        self._mcp_server = FastMCP(self._name)

        # Register tools with FastMCP
        for _name, handler in self._tool_handlers.items():
            defn = handler.definition

            def _make_tool_wrapper(h: ToolHandler) -> Any:
                async def tool_wrapper(**kwargs: Any) -> Any:
                    # Unwrap nested kwargs from FastMCP schema inference.
                    # FastMCP infers a single "kwargs" param from **kwargs signature,
                    # so clients send {"kwargs": {actual params}} instead of flat params.
                    if (
                        "kwargs" in kwargs
                        and len(kwargs) == 1
                        and isinstance(kwargs["kwargs"], dict)
                    ):
                        kwargs = kwargs["kwargs"]
                    result = await h.handle(kwargs)
                    if result.is_ok:
                        # Convert MCPToolResult to FastMCP format
                        tool_result = result.value
                        return tool_result.text_content
                    else:
                        # Raise so FastMCP returns a proper MCP error response
                        # with isError: true, instead of a success with error text.
                        raise RuntimeError(str(result.error))

                return tool_wrapper

            wrapper = _make_tool_wrapper(handler)
            self._mcp_server.tool(
                name=defn.name,
                description=defn.description,
            )(wrapper)

        # Register resources with FastMCP
        for uri, res_handler in self._resource_handlers.items():

            def _make_resource_wrapper(h: ResourceHandler, resource_uri: str) -> Any:
                async def resource_wrapper() -> str:
                    result = await h.handle(resource_uri)
                    if result.is_ok:
                        content = result.value
                        return content.text or ""
                    else:
                        raise RuntimeError(str(result.error))

                return resource_wrapper

            wrapper = _make_resource_wrapper(res_handler, uri)
            self._mcp_server.resource(uri)(wrapper)

        log.info(
            "mcp.server.starting",
            name=self._name,
            tools=len(self._tool_handlers),
            resources=len(self._resource_handlers),
        )

        # Run the server with the appropriate transport
        if transport == "sse":
            await self._mcp_server.run_sse_async(host=host, port=port)
        else:
            await self._mcp_server.run_stdio_async()

    async def shutdown(self) -> None:
        """Shutdown the server gracefully."""
        log.info("mcp.server.shutdown", name=self._name)
        # FastMCP handles its own shutdown when run_async completes


def create_ouroboros_server(
    *,
    name: str = "ouroboros-mcp",
    version: str = "1.0.0",
    auth_config: AuthConfig | None = None,
    rate_limit_config: RateLimitConfig | None = None,
    event_store: Any | None = None,
    state_dir: Any | None = None,
) -> MCPServerAdapter:
    """Create an Ouroboros MCP server with all tools and dependencies wired.

    This is a composition root that creates all service instances and performs
    dependency injection to tool handlers.

    Services created:
    - LiteLLMAdapter: LLM provider adapter
    - EventStore: Event persistence (optional, defaults to SQLite)
    - InterviewEngine: Interactive interview for requirements
    - SeedGenerator: Converts interviews to immutable Seeds
    - EvaluationPipeline: Three-stage evaluation (mechanical, semantic, consensus)
    - LateralThinker: Alternative thinking approaches for stagnation

    Args:
        name: Server name.
        version: Server version.
        auth_config: Optional authentication configuration.
        rate_limit_config: Optional rate limiting configuration.
        event_store: Optional EventStore instance. If not provided, creates default.
        state_dir: Optional pathlib.Path for interview state directory.
                   If not provided, uses ~/.ouroboros/data.

    Returns:
        Configured MCPServerAdapter with all 10 tools registered.

    Raises:
        ImportError: If MCP SDK is not installed.
    """
    # Import tool definitions
    from pathlib import Path

    from rich.console import Console

    # Import service dependencies
    from ouroboros.bigbang.interview import InterviewEngine
    from ouroboros.bigbang.seed_generator import SeedGenerator
    from ouroboros.evaluation import (
        EvaluationContext,
        EvaluationPipeline,
        PipelineConfig,
    )
    from ouroboros.mcp.tools.definitions import (
        EvaluateHandler,
        EvolveStepHandler,
        ExecuteSeedHandler,
        GenerateSeedHandler,
        InterviewHandler,
        LateralThinkHandler,
        LineageStatusHandler,
        MeasureDriftHandler,
        QueryEventsHandler,
        SessionStatusHandler,
    )
    from ouroboros.mcp.tools.registry import ToolRegistry
    from ouroboros.orchestrator.adapter import ClaudeAgentAdapter
    from ouroboros.orchestrator.runner import (
        OrchestratorRunner,
    )
    from ouroboros.providers.claude_code_adapter import ClaudeCodeAdapter
    from ouroboros.resilience.lateral import LateralThinker

    # Create LLM adapter (shared across services)
    # Default to ClaudeCodeAdapter — uses Max Plan auth, no API key needed.
    llm_adapter = ClaudeCodeAdapter(max_turns=1)

    # Create or use provided EventStore
    if event_store is None:
        from ouroboros.persistence.event_store import EventStore

        event_store = EventStore()

    # Create state directory for interviews
    if state_dir is None:
        state_dir = Path.home() / ".ouroboros" / "data"
        state_dir.mkdir(parents=True, exist_ok=True)

    # Create core service instances
    interview_engine = InterviewEngine(
        llm_adapter=llm_adapter,
        state_dir=state_dir,
    )

    seed_generator = SeedGenerator(llm_adapter=llm_adapter)

    # Create evolution engines for evolve_step
    from ouroboros.core.lineage import EvaluationSummary
    from ouroboros.evolution.loop import EvolutionaryLoop, EvolutionaryLoopConfig
    from ouroboros.evolution.reflect import ReflectEngine
    from ouroboros.evolution.wonder import WonderEngine

    wonder_engine = WonderEngine(llm_adapter=llm_adapter, model="default")
    reflect_engine = ReflectEngine(llm_adapter=llm_adapter, model="default")

    # Wire real execution/evaluation callables for evolve_step so that
    # generation quality is validated, not only ontology deltas.
    agent_adapter = ClaudeAgentAdapter(permission_mode="acceptEdits")
    evolution_runner = OrchestratorRunner(
        adapter=agent_adapter,
        event_store=event_store,
        console=Console(),
        debug=False,
        enable_decomposition=True,
    )
    evolution_eval_pipeline = EvaluationPipeline(
        llm_adapter=llm_adapter,
        # Stage 1 is intentionally disabled here to avoid running full
        # mechanical checks on every generation step.
        config=PipelineConfig(
            stage1_enabled=False,
            stage2_enabled=True,
            stage3_enabled=False,
        ),
    )
    evolution_store_initialized = False
    evolution_store_init_lock = asyncio.Lock()

    async def _ensure_evolution_store_initialized() -> None:
        nonlocal evolution_store_initialized
        if evolution_store_initialized:
            return

        async with evolution_store_init_lock:
            if not evolution_store_initialized:
                await event_store.initialize()
                evolution_store_initialized = True

    async def _evolution_executor(seed: Any) -> Any:
        await _ensure_evolution_store_initialized()
        return await evolution_runner.execute_seed(
            seed=seed,
            execution_id=None,
            parallel=True,
        )

    async def _evolution_evaluator(seed: Any, execution_output: str | None) -> EvaluationSummary:
        await _ensure_evolution_store_initialized()

        artifact = execution_output or ""
        if not artifact.strip():
            return EvaluationSummary(
                final_approved=False,
                highest_stage_passed=1,
                score=0.0,
                drift_score=1.0,
                failure_reason="Empty execution output",
            )

        current_ac = (
            seed.acceptance_criteria[0]
            if getattr(seed, "acceptance_criteria", None)
            else "Verify execution output meets requirements"
        )
        eval_context = EvaluationContext(
            execution_id=f"eval_{seed.metadata.seed_id}",
            seed_id=seed.metadata.seed_id,
            current_ac=current_ac,
            artifact=artifact,
            artifact_type="code",
            goal=seed.goal,
            constraints=tuple(seed.constraints),
        )
        eval_result = await evolution_eval_pipeline.evaluate(eval_context)
        if eval_result.is_err:
            return EvaluationSummary(
                final_approved=False,
                highest_stage_passed=1,
                score=0.0,
                drift_score=1.0,
                failure_reason=str(eval_result.error),
            )

        result = eval_result.value
        stage2 = result.stage2_result
        return EvaluationSummary(
            final_approved=result.final_approved,
            highest_stage_passed=max(1, result.highest_stage_completed),
            score=stage2.score if stage2 else None,
            drift_score=stage2.drift_score if stage2 else None,
            failure_reason=result.failure_reason,
        )

    evolutionary_loop = EvolutionaryLoop(
        event_store=event_store,
        config=EvolutionaryLoopConfig(),
        wonder_engine=wonder_engine,
        reflect_engine=reflect_engine,
        seed_generator=seed_generator,
        executor=_evolution_executor,
        evaluator=_evolution_evaluator,
    )

    # Create tool registry for dependency injection
    registry = ToolRegistry()

    # Create and register tool handlers with injected dependencies
    tool_handlers = [
        ExecuteSeedHandler(
            event_store=event_store,
            llm_adapter=llm_adapter,
        ),
        SessionStatusHandler(
            event_store=event_store,
        ),
        QueryEventsHandler(
            event_store=event_store,
        ),
        GenerateSeedHandler(
            interview_engine=interview_engine,
            seed_generator=seed_generator,
            llm_adapter=llm_adapter,
        ),
        MeasureDriftHandler(
            event_store=event_store,
        ),
        InterviewHandler(
            interview_engine=interview_engine,
        ),
        EvaluateHandler(
            event_store=event_store,
            llm_adapter=llm_adapter,
        ),
        LateralThinkHandler(),
        EvolveStepHandler(
            evolutionary_loop=evolutionary_loop,
        ),
        LineageStatusHandler(
            event_store=event_store,
        ),
    ]

    # Create server adapter
    server = MCPServerAdapter(
        name=name,
        version=version,
        auth_config=auth_config,
        rate_limit_config=rate_limit_config,
    )

    # Register all tools with the server
    for handler in tool_handlers:
        server.register_tool(handler)
        registry.register(handler, category="ouroboros")

    log.info(
        "mcp.server.composition_root_complete",
        name=name,
        version=version,
        tools_registered=len(tool_handlers),
        tool_names=[h.definition.name for h in tool_handlers],
    )

    return server
