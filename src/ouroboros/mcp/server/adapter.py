"""MCP Server adapter implementation.

This module provides the MCPServerAdapter class that implements the MCPServer
protocol using the MCP SDK (FastMCP). It handles tool registration, resource
handling, and server lifecycle.
"""

import asyncio
from collections.abc import Sequence
import inspect
import keyword
import os
from pathlib import Path
import re
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
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)

log = structlog.get_logger(__name__)

VALID_TRANSPORTS: frozenset[str] = frozenset({"stdio", "sse"})


def validate_transport(transport: str) -> str:
    """Normalize and validate a transport string.

    Returns the lowercased transport if valid, raises ValueError otherwise.
    """
    transport = transport.lower()
    if transport not in VALID_TRANSPORTS:
        msg = f"Invalid transport {transport!r}. Must be one of: {', '.join(sorted(VALID_TRANSPORTS))}"
        raise ValueError(msg)
    return transport


def _extract_feedback_metadata_from_artifact(artifact: str) -> tuple[Any, ...]:
    """Extract structured feedback metadata emitted inside execution artifacts."""
    import json
    import re

    from ouroboros.core.lineage import FeedbackMetadata

    matches = re.findall(r"^Feedback Metadata JSON:\s*(\{.+\})$", artifact, flags=re.MULTILINE)
    if not matches:
        return ()

    feedback_items: list[FeedbackMetadata] = []
    for payload in matches:
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue

        raw_feedback = parsed.get("feedback_metadata")
        if not isinstance(raw_feedback, list):
            continue

        for item in raw_feedback:
            if not isinstance(item, dict):
                continue
            try:
                feedback_items.append(FeedbackMetadata.model_validate(item))
            except Exception:
                continue

    return tuple(feedback_items)


# Map MCPToolParameter types to Python annotations for FastMCP schema inference.
_TOOL_TYPE_MAP: dict[ToolInputType, type] = {
    ToolInputType.STRING: str,
    ToolInputType.INTEGER: int,
    ToolInputType.NUMBER: float,
    ToolInputType.BOOLEAN: bool,
    ToolInputType.ARRAY: list,
    ToolInputType.OBJECT: dict,
}


def _build_tool_signature(parameters: tuple[MCPToolParameter, ...]) -> inspect.Signature:
    """Build an inspect.Signature from MCPToolParameter definitions.

    FastMCP infers JSON schema from function signatures via inspect.signature().
    Using **kwargs produces a single "kwargs" parameter in the schema, which
    forces clients to wrap arguments as {"kwargs": {actual_args}}.

    By setting __signature__ with explicit parameters, FastMCP generates the
    correct schema and clients can send flat argument dicts.
    """
    signature, _ = _build_tool_signature_with_aliases(parameters)
    return signature


def _to_safe_signature_name(name: str) -> str:
    """Return a valid Python identifier for a tool parameter name."""
    if name.isidentifier() and not keyword.iskeyword(name):
        return name

    # Replace invalid characters with underscore and avoid starting with a digit.
    sanitized = re.sub(r"[^0-9a-zA-Z_]", "_", name)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    if not sanitized or sanitized[0].isdigit():
        sanitized = f"_{sanitized or 'param'}"

    if keyword.iskeyword(sanitized):
        sanitized = f"_{sanitized}"

    return sanitized


def _build_tool_signature_with_aliases(
    parameters: tuple[MCPToolParameter, ...],
) -> tuple[inspect.Signature, dict[str, str]]:
    """Build signature plus map from schema args to original MCP parameter names."""

    sig_params = []
    alias_counts: dict[str, int] = {}
    alias_to_original: dict[str, str] = {}

    for p in parameters:
        parameter_name = _to_safe_signature_name(p.name)
        alias_count = alias_counts.get(parameter_name, 0) + 1
        alias_counts[parameter_name] = alias_count
        if alias_count > 1:
            parameter_name = f"{parameter_name}_{alias_count}"

        alias_to_original[parameter_name] = p.name

        python_type = _TOOL_TYPE_MAP.get(p.type, Any)
        if p.required:
            sig_params.append(
                inspect.Parameter(
                    name=parameter_name,
                    kind=inspect.Parameter.KEYWORD_ONLY,
                    annotation=python_type,
                )
            )
        else:
            default = p.default if p.default is not None else None
            sig_params.append(
                inspect.Parameter(
                    name=parameter_name,
                    kind=inspect.Parameter.KEYWORD_ONLY,
                    default=default,
                    annotation=python_type | None,
                )
            )

    return inspect.Signature(parameters=sig_params), alias_to_original


_PROJECT_ROOT_MARKERS = (
    # VCS (most universal — nearly every project has one)
    ".git",
    # Python
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    # Node.js
    "package.json",
    # Rust
    "Cargo.toml",
    # Go
    "go.mod",
    # Java / Kotlin
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    # Ruby
    "Gemfile",
    # PHP
    "composer.json",
)


def _looks_like_project_root(path: object) -> bool:
    """Return True when the given path looks like a project root."""
    from pathlib import Path

    if not isinstance(path, Path):
        return False

    return any((path / marker).exists() for marker in _PROJECT_ROOT_MARKERS)


def _project_dir_from_seed(seed: Any) -> str | None:
    """Extract a likely project directory from seed metadata or brownfield context."""
    if seed is None:
        return None

    seed_meta = getattr(seed, "metadata", None)
    if seed_meta:
        project_dir = getattr(seed_meta, "project_dir", None) or getattr(
            seed_meta,
            "working_directory",
            None,
        )
        if project_dir:
            return str(project_dir)

    brownfield_context = getattr(seed, "brownfield_context", None)
    context_references = getattr(brownfield_context, "context_references", ()) or ()

    for reference in context_references:
        path = getattr(reference, "path", None)
        role = getattr(reference, "role", None)
        if isinstance(path, str) and path and role == "primary":
            return path

    for reference in context_references:
        path = getattr(reference, "path", None)
        if isinstance(path, str) and path:
            return path

    return None


def _project_dir_from_artifact(artifact: str) -> str | None:
    """Extract a likely project root from Write/Edit/File tool output."""
    from pathlib import Path
    import re

    # Match quoted paths (spaces allowed) and unquoted paths.
    # Examples:  Write: /foo/bar.py  |  File: "/path with spaces/bar.py"
    write_matches: list[str] = []
    for m in re.finditer(r'(?:Write|Edit|File): (?:"([^"]+)"|(.+))', artifact):
        path_candidate = m.group(1) or m.group(2)
        if path_candidate:
            write_matches.append(path_candidate.strip())
    for path_str in write_matches:
        candidate = Path(path_str).parent
        for _ in range(10):
            if _looks_like_project_root(candidate):
                return str(candidate)
            if candidate == candidate.parent:
                break
            candidate = candidate.parent

    return None


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
        self._owned_resources: list[Any] = []  # objects with async close()

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
            timeout = getattr(handler, "TIMEOUT_SECONDS", None)
            if timeout is not None and timeout > 0:
                result = await asyncio.wait_for(handler.handle(arguments), timeout=timeout)
            else:
                result = await handler.handle(arguments)
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
            transport: Transport type - "stdio" or "sse" (case-insensitive).
            host: Host to bind to (SSE only). Defaults to "localhost".
            port: Port to bind to (SSE only). Defaults to 8080.
        """
        transport = validate_transport(transport)

        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError as e:
            msg = "mcp package not installed. Install with: pip install 'ouroboros-ai[mcp]'"
            raise ImportError(msg) from e

        # Pass host/port at construction time — FastMCP reads these from
        # its internal settings, so run_sse_async() alone won't pick them up.
        if transport == "sse":
            self._mcp_server = FastMCP(
                self._name,
                host=host,
                port=port,
            )
        else:
            self._mcp_server = FastMCP(self._name)

        # Register tools with FastMCP
        for _name, handler in self._tool_handlers.items():
            defn = handler.definition

            def _make_tool_wrapper(h: ToolHandler) -> Any:
                async def tool_wrapper(**kwargs: Any) -> Any:
                    # Backward compat: unwrap nested kwargs from clients that
                    # used the old schema where FastMCP inferred a single "kwargs" param.
                    if (
                        "kwargs" in kwargs
                        and len(kwargs) == 1
                        and isinstance(kwargs["kwargs"], dict)
                    ):
                        kwargs = kwargs["kwargs"]

                    _, alias_to_original = _build_tool_signature_with_aliases(
                        h.definition.parameters,
                    )
                    normalized_kwargs: dict[str, Any] = {}
                    for alias_key, original_key in alias_to_original.items():
                        if alias_key in kwargs:
                            normalized_kwargs[original_key] = kwargs[alias_key]
                    for key, value in kwargs.items():
                        normalized_kwargs.setdefault(key, value)

                    result = await h.handle(normalized_kwargs)
                    if result.is_ok:
                        # Convert MCPToolResult to FastMCP format
                        tool_result = result.value
                        return tool_result.text_content
                    else:
                        # Raise so FastMCP returns a proper MCP error response
                        # with isError: true, instead of a success with error text.
                        raise RuntimeError(str(result.error))

                # Set proper signature so FastMCP generates correct JSON schema
                # instead of a single "kwargs" parameter.
                tool_wrapper.__signature__ = _build_tool_signature(h.definition.parameters)
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

        # Log sandbox environment for diagnostics.  Note: CODEX_SANDBOX_
        # NETWORK_DISABLED=1 does NOT necessarily block MCP-spawned child
        # processes — Codex may grant MCP servers a different seatbelt
        # profile than shell commands.
        if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
            log.info(
                "mcp.server.sandbox_env_detected",
                detail=(
                    "CODEX_SANDBOX_NETWORK_DISABLED=1 detected. "
                    "MCP-spawned agent runtimes may still have network "
                    "access. If they fail, consider running the parent "
                    "Codex with --sandbox danger-full-access."
                ),
            )

        # Run the server with the appropriate transport
        if transport == "sse":
            await self._mcp_server.run_sse_async()
        else:
            await self._mcp_server.run_stdio_async()

    def register_owned_resource(self, resource: Any) -> None:
        """Register a resource whose ``close()`` will be called on shutdown."""
        self._owned_resources.append(resource)

    async def shutdown(self) -> None:
        """Shutdown the server gracefully, closing owned resources."""
        log.info("mcp.server.shutdown", name=self._name)
        for resource in self._owned_resources:
            close_fn = getattr(resource, "close", None)
            if callable(close_fn):
                try:
                    await close_fn()
                except Exception as exc:
                    log.warning(
                        "mcp.server.resource_close_failed",
                        resource=type(resource).__name__,
                        error=str(exc),
                    )
        self._owned_resources.clear()


def create_ouroboros_server(
    *,
    name: str = "ouroboros-mcp",
    version: str = "1.0.0",
    auth_config: AuthConfig | None = None,
    rate_limit_config: RateLimitConfig | None = None,
    event_store: Any | None = None,
    state_dir: Any | None = None,
    runtime_backend: str | None = None,
    llm_backend: str | None = None,
    mcp_bridge: Any | None = None,
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
        runtime_backend: Optional orchestrator runtime backend override.
        llm_backend: Optional LLM-only backend override.

    Returns:
        Configured MCPServerAdapter with all 10 tools registered.

    Raises:
        ImportError: If MCP SDK is not installed.
    """
    from rich.console import Console

    # Import service dependencies
    from ouroboros.bigbang.interview import InterviewEngine
    from ouroboros.bigbang.seed_generator import SeedGenerator
    from ouroboros.config import (
        get_assertion_extraction_model,
        get_clarification_model,
        get_reflect_model,
        get_semantic_model,
        get_wonder_model,
    )
    from ouroboros.evaluation import (
        EvaluationContext,
        EvaluationPipeline,
        PipelineConfig,
        SemanticConfig,
    )
    from ouroboros.mcp.job_manager import JobManager
    from ouroboros.mcp.tools.brownfield_handler import BrownfieldHandler
    from ouroboros.mcp.tools.definitions import (
        ACDashboardHandler,
        CancelExecutionHandler,
        CancelJobHandler,
        ChannelWorkflowHandler,
        EvaluateHandler,
        EvolveRewindHandler,
        EvolveStepHandler,
        ExecuteSeedHandler,
        GenerateSeedHandler,
        InterviewHandler,
        JobResultHandler,
        JobStatusHandler,
        JobWaitHandler,
        LateralThinkHandler,
        LineageStatusHandler,
        MeasureDriftHandler,
        QueryEventsHandler,
        SessionStatusHandler,
        StartEvolveStepHandler,
        StartExecuteSeedHandler,
    )
    from ouroboros.mcp.tools.pm_handler import PMInterviewHandler
    from ouroboros.mcp.tools.qa import QAHandler
    from ouroboros.mcp.tools.registry import ToolRegistry
    from ouroboros.openclaw.workflow import ChannelRepoRegistry, ChannelWorkflowManager
    from ouroboros.orchestrator import create_agent_runtime, resolve_agent_runtime_backend
    from ouroboros.orchestrator.runner import (
        OrchestratorRunner,
    )
    from ouroboros.providers import create_llm_adapter

    resolved_runtime_backend = resolve_agent_runtime_backend(runtime_backend)

    # Materialize the default runtime once at server creation so backend wiring
    # is validated up front and composition-root tests can assert the selected
    # runtime backend without waiting for a tool invocation.
    create_agent_runtime(
        backend=resolved_runtime_backend,
        model=None,
        cwd=Path.cwd(),
        llm_backend=llm_backend,
    )

    # Create shared LLM adapter for interview/seed/evaluation paths.
    llm_adapter = create_llm_adapter(
        backend=llm_backend,
        max_turns=1,
        cwd=Path.cwd(),
    )

    # Create or use provided EventStore
    if event_store is None:
        from ouroboros.persistence.event_store import EventStore

        event_store = EventStore()

    # Create state directory for interviews
    if state_dir is None:
        state_dir = Path.cwd() / ".ouroboros" / "data"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Create core service instances
    interview_engine = InterviewEngine(
        llm_adapter=llm_adapter,
        state_dir=state_dir,
        model=get_clarification_model(llm_backend),
    )

    seed_generator = SeedGenerator(
        llm_adapter=llm_adapter,
        model=get_clarification_model(llm_backend),
    )

    # Create evolution engines for evolve_step
    from ouroboros.core.lineage import ACResult, EvaluationSummary
    from ouroboros.evaluation.artifact_collector import ArtifactCollector
    from ouroboros.evolution.loop import EvolutionaryLoop, EvolutionaryLoopConfig
    from ouroboros.evolution.reflect import ReflectEngine
    from ouroboros.evolution.wonder import WonderEngine
    from ouroboros.verification.extractor import AssertionExtractor
    from ouroboros.verification.verifier import SpecVerifier

    wonder_engine = WonderEngine(
        llm_adapter=llm_adapter,
        model=get_wonder_model(llm_backend),
    )
    reflect_engine = ReflectEngine(
        llm_adapter=llm_adapter,
        model=get_reflect_model(llm_backend),
    )

    # Wire real execution/evaluation callables for evolve_step so that
    # generation quality is validated, not only ontology deltas.
    # Use Sonnet for execution (frugal) — Opus is overkill for code generation.
    execution_model = os.environ.get("OUROBOROS_EXECUTION_MODEL")
    if execution_model is None and resolved_runtime_backend == "claude":
        execution_model = "claude-sonnet-4-6"
    # Use stderr console: in MCP stdio mode, stdout is the JSON-RPC channel.
    # Any non-protocol output on stdout corrupts the MCP communication.
    # Stage 1 (mechanical checks: lint/build/test) can be enabled via env var.
    # Disabled by default to reduce latency per generation step.
    evolve_stage1 = os.environ.get("OUROBOROS_EVOLVE_STAGE1", "false").lower() == "true"
    evolution_eval_pipeline = EvaluationPipeline(
        llm_adapter=llm_adapter,
        config=PipelineConfig(
            stage1_enabled=evolve_stage1,
            stage2_enabled=True,
            stage3_enabled=False,
            semantic=SemanticConfig(model=get_semantic_model(llm_backend)),
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

    async def _evolution_executor(seed: Any, *, parallel: bool = True) -> Any:
        await _ensure_evolution_store_initialized()
        task_cwd = evolutionary_loop.get_project_dir()
        runner_adapter = create_agent_runtime(
            backend=resolved_runtime_backend,
            model=execution_model,
            cwd=task_cwd or Path.cwd(),
            llm_backend=llm_backend,
        )
        _evo_mcp_manager = mcp_bridge.manager if mcp_bridge is not None else None
        _evo_mcp_prefix = (
            mcp_bridge.tool_prefix
            if mcp_bridge is not None and hasattr(mcp_bridge, "tool_prefix")
            else ""
        )
        evolution_runner = OrchestratorRunner(
            adapter=runner_adapter,
            event_store=event_store,
            console=Console(stderr=True),
            mcp_manager=_evo_mcp_manager,
            mcp_tool_prefix=_evo_mcp_prefix,
            debug=False,
            enable_decomposition=True,
        )
        return await evolution_runner.execute_seed(
            seed=seed,
            execution_id=None,
            parallel=parallel,
        )

    def _evaluate_mechanically(artifact: str, seed: Any) -> EvaluationSummary | None:
        """Parse structured AC results from execution output.

        The parallel executor emits '### AC N: [PASS/FAIL] ...' lines.
        When these are present, we can score mechanically without an LLM call,
        which is more reliable in MCP stdio mode where nested CLI spawning
        is unstable.

        Returns None if the output doesn't contain parseable AC results.
        """
        import re

        # Match full AC lines: "### AC 3: [PASS] Some description..."
        ac_line_matches = re.findall(r"### AC (\d+): \[(PASS|FAIL)\]\s*(.*)", artifact)
        if not ac_line_matches:
            return None

        seed_acs = getattr(seed, "acceptance_criteria", None) or ()
        feedback_metadata = _extract_feedback_metadata_from_artifact(artifact)

        ac_results: list[ACResult] = []
        for ac_num_str, status, description in ac_line_matches:
            ac_idx = int(ac_num_str) - 1  # 0-based index
            ac_content = seed_acs[ac_idx] if ac_idx < len(seed_acs) else description.strip()
            ac_results.append(
                ACResult(
                    ac_index=ac_idx,
                    ac_content=ac_content,
                    passed=status == "PASS",
                    score=1.0 if status == "PASS" else 0.0,
                    evidence=description.strip(),
                    verification_method="mechanical",
                )
            )

        total = len(ac_results)
        passed = sum(1 for r in ac_results if r.passed)
        score = passed / total if total > 0 else 0.0

        total_acs = len(seed_acs) if seed_acs else total
        approved = passed >= total_acs and passed == total

        failure_reason = None
        if not approved:
            failed_indices = [r.ac_index + 1 for r in ac_results if not r.passed]
            failure_reason = f"{len(failed_indices)}/{total} ACs failed (AC {', '.join(str(i) for i in failed_indices)})"

        return EvaluationSummary(
            final_approved=approved,
            highest_stage_passed=3 if approved else 2,
            score=score,
            drift_score=1.0 - score,
            failure_reason=failure_reason,
            ac_results=tuple(ac_results),
            feedback_metadata=feedback_metadata,
        )

    spec_extractor = AssertionExtractor(
        llm_adapter=llm_adapter,
        model=get_assertion_extraction_model(llm_backend),
    )

    def _extract_project_dir(artifact: str, seed: Any = None) -> str | None:
        """Resolve project directory from explicit config, seed context, or artifacts."""
        from pathlib import Path

        configured_project_dir = evolutionary_loop.get_project_dir()
        if configured_project_dir:
            return configured_project_dir

        seed_project_dir = _project_dir_from_seed(seed)
        if seed_project_dir:
            return seed_project_dir

        artifact_project_dir = _project_dir_from_artifact(artifact)
        if artifact_project_dir:
            return artifact_project_dir

        cwd = Path.cwd()
        if _looks_like_project_root(cwd):
            return str(cwd)

        return None

    async def _verify_spec_compliance(
        seed: Any,
        artifact: str,
        mechanical: EvaluationSummary,
    ) -> EvaluationSummary | None:
        """Run spec verification and override mechanical results if discrepancies found.

        Returns a corrected EvaluationSummary if discrepancies are detected,
        or None if no override is needed (verification passed or unavailable).
        """
        project_dir = _extract_project_dir(artifact, seed=seed)
        if not project_dir:
            return None

        seed_acs = getattr(seed, "acceptance_criteria", None) or ()
        if not seed_acs:
            return None

        seed_id = getattr(getattr(seed, "metadata", None), "seed_id", None)
        if not seed_id:
            return None

        # Extract assertions from ACs (cached by seed_id)
        extract_result = await spec_extractor.extract(seed_id, seed_acs)
        if extract_result.is_err:
            log.warning("spec_verification.extraction_failed", error=str(extract_result.error))
            return None

        assertions = extract_result.value
        if not assertions:
            return None

        # Build agent results map from mechanical evaluation
        agent_results = {ac.ac_index: ac.passed for ac in mechanical.ac_results}

        # Run verification
        verifier = SpecVerifier(project_dir=project_dir)
        summary = verifier.verify_all(assertions, agent_results)

        if not summary.has_discrepancies:
            return None

        # Override: rebuild ac_results with verification corrections
        log.warning(
            "spec_verification.discrepancies_found",
            count=summary.discrepancy_count,
            project_dir=project_dir,
        )

        corrected_results: list[ACResult] = []
        discrepant_indices: set[int] = set()
        for report in summary.reports:
            if report.has_discrepancy:
                discrepant_indices.add(report.ac_index)

        for ac in mechanical.ac_results:
            if ac.ac_index in discrepant_indices:
                # Find the verification detail for evidence
                detail = ""
                for report in summary.reports:
                    if report.ac_index == ac.ac_index:
                        details = [r.detail for r in report.results if r.discrepancy]
                        detail = "; ".join(details)
                        break

                corrected_results.append(
                    ACResult(
                        ac_index=ac.ac_index,
                        ac_content=ac.ac_content,
                        passed=False,
                        score=0.0,
                        evidence=f"Spec verification override: {detail}",
                        verification_method="spec_verifier",
                    )
                )
            else:
                corrected_results.append(ac)

        total = len(corrected_results)
        passed = sum(1 for r in corrected_results if r.passed)
        score = passed / total if total > 0 else 0.0

        failed_indices = [r.ac_index + 1 for r in corrected_results if not r.passed]
        failure_reason = (
            f"{len(failed_indices)}/{total} ACs failed "
            f"(AC {', '.join(str(i) for i in failed_indices)}) "
            f"[{summary.discrepancy_count} spec verification override(s)]"
        )

        return EvaluationSummary(
            final_approved=False,
            highest_stage_passed=2,
            score=score,
            drift_score=1.0 - score,
            failure_reason=failure_reason,
            ac_results=tuple(corrected_results),
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

        # Use mechanical evaluation from structured AC results.
        # More reliable than LLM-based evaluation in MCP stdio mode.
        mechanical = _evaluate_mechanically(artifact, seed)
        if mechanical is not None:
            # Run spec verification to catch agent self-report lies
            verified = await _verify_spec_compliance(seed, artifact, mechanical)
            if verified is not None:
                return verified
            return mechanical

        # Fallback: LLM-based evaluation when no structured AC results
        acs = getattr(seed, "acceptance_criteria", None)
        if acs:
            current_ac = "\n".join(f"AC {i + 1}: {ac}" for i, ac in enumerate(acs))
        else:
            current_ac = "Verify execution output meets requirements"

        # Collect file-based artifacts for richer evaluation
        project_dir = _extract_project_dir(artifact, seed=seed)
        artifact_bundle = ArtifactCollector().collect(artifact, project_dir)

        eval_context = EvaluationContext(
            execution_id=f"eval_{seed.metadata.seed_id}",
            seed_id=seed.metadata.seed_id,
            current_ac=current_ac,
            artifact=artifact,
            artifact_type="code",
            goal=seed.goal,
            constraints=tuple(seed.constraints),
            artifact_bundle=artifact_bundle,
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
            reward_hacking_risk=stage2.reward_hacking_risk if stage2 else None,
            failure_reason=result.failure_reason,
        )

    async def _evolution_validator(seed: Any, execution_output: str | None) -> str:
        """Validate and reconcile code generated by parallel AC execution.

        After parallel ACs generate code independently, inconsistencies
        can arise (missing imports, conflicting module structures, etc.).
        This phase runs pytest --collect-only to detect issues and spawns
        a Claude session to fix them.

        Returns a summary of validation results.
        """
        from pathlib import Path  # noqa: I001
        import re
        import subprocess  # noqa: S404  # nosec

        project_dir = _extract_project_dir(execution_output or "", seed=seed)

        if not project_dir:
            log.warning(
                "evolution.validation.skipped",
                reason="could not determine project directory",
                has_seed_metadata=_project_dir_from_seed(seed) is not None,
                execution_output_length=len(execution_output) if execution_output else 0,
            )
            return "Validation skipped: could not determine project directory"

        # Detect the correct Python binary (prefer project venv over system)
        project_path = Path(project_dir)
        venv_python = project_path / ".venv" / "bin" / "python"
        python_cmd = str(venv_python) if venv_python.exists() else "python"

        async def _run_collect() -> subprocess.CompletedProcess[str]:
            """Run pytest --collect-only without blocking the event loop."""
            return await asyncio.to_thread(
                subprocess.run,
                [python_cmd, "-m", "pytest", "--collect-only", "-q", "--no-header"],
                capture_output=True,
                text=True,
                cwd=project_dir,
                timeout=60,
            )

        max_attempts = 3
        # Use Sonnet for validation fixes — import error resolution doesn't need Opus
        validation_model = os.environ.get("OUROBOROS_VALIDATION_MODEL")
        if validation_model is None and resolved_runtime_backend == "claude":
            validation_model = "claude-sonnet-4-6"
        validation_adapter = create_agent_runtime(
            backend=resolved_runtime_backend,
            model=validation_model,
            cwd=project_dir,
            llm_backend=llm_backend,
        )

        for attempt in range(1, max_attempts + 1):
            collect_result = await _run_collect()

            if collect_result.returncode == 0:
                return f"Validation passed (attempt {attempt}/{max_attempts})"

            # Parse collection errors
            stderr = collect_result.stderr or ""
            stdout = collect_result.stdout or ""
            error_output = stderr + "\n" + stdout

            # Check for ImportError or ModuleNotFoundError
            import_errors = re.findall(r"(?:ImportError|ModuleNotFoundError): (.+)", error_output)
            if not import_errors:
                # Non-import errors (syntax, etc.) - still try to fix
                error_lines = [
                    line for line in error_output.split("\n") if "ERROR" in line or "Error" in line
                ]
                if not error_lines:
                    return f"Validation: no fixable errors detected (exit code {collect_result.returncode})"

            # Spawn Claude session to fix the errors
            fix_prompt = (
                f"The project at {project_dir} has import/collection errors that prevent tests from running.\n\n"
                f"pytest --collect-only output:\n```\n{error_output[:3000]}\n```\n\n"
                "Fix these errors by:\n"
                "1. Reading the failing __init__.py and module files\n"
                "2. Adding missing imports, classes, or functions\n"
                "3. Removing references to non-existent modules\n"
                "4. Do NOT delete test files - fix the source code instead\n"
                "5. Run pytest --collect-only again to verify the fix\n\n"
                "Be minimal: only fix what's broken, don't refactor."
            )

            log.info(
                "evolution.validation.fixing",
                attempt=attempt,
                error_count=len(import_errors) or len(error_lines),
                project_dir=project_dir,
            )

            fix_result = await validation_adapter.execute_task_to_result(
                prompt=fix_prompt,
                tools=["Read", "Edit", "Write", "Bash", "Glob", "Grep"],
            )

            if fix_result.is_err:
                return f"Validation fix failed (attempt {attempt}): {fix_result.error}"

        # After max attempts, report remaining errors
        final_collect = await _run_collect()
        if final_collect.returncode == 0:
            return f"Validation passed after {max_attempts} fix attempts"
        remaining = re.findall(r"ERROR (.+)", final_collect.stdout or "")
        return (
            f"Validation: {len(remaining)} errors remain after {max_attempts} attempts. "
            f"Remaining: {', '.join(remaining[:5])}"
        )

    evolutionary_loop = EvolutionaryLoop(
        event_store=event_store,
        config=EvolutionaryLoopConfig(),
        wonder_engine=wonder_engine,
        reflect_engine=reflect_engine,
        seed_generator=seed_generator,
        executor=_evolution_executor,
        evaluator=_evolution_evaluator,
        validator=_evolution_validator,
    )
    job_manager = JobManager(event_store)
    cwd = Path.cwd()
    openclaw_db_path = Path.home() / ".ouroboros" / "ouroboros.db"
    workflow_manager = ChannelWorkflowManager(openclaw_db_path)
    repo_registry = ChannelRepoRegistry(openclaw_db_path)

    # Create tool registry for dependency injection
    registry = ToolRegistry()

    # Create and register tool handlers with injected dependencies
    execute_seed = ExecuteSeedHandler(
        event_store=event_store,
        llm_adapter=llm_adapter,
        agent_runtime_backend=runtime_backend,
        llm_backend=llm_backend,
    )
    evolve_step = EvolveStepHandler(
        evolutionary_loop=evolutionary_loop,
    )
    tool_handlers = [
        execute_seed,
        StartExecuteSeedHandler(
            execute_handler=execute_seed,
            event_store=event_store,
            job_manager=job_manager,
        ),
        SessionStatusHandler(
            event_store=event_store,
        ),
        JobStatusHandler(
            event_store=event_store,
            job_manager=job_manager,
        ),
        JobWaitHandler(
            event_store=event_store,
            job_manager=job_manager,
        ),
        JobResultHandler(
            event_store=event_store,
            job_manager=job_manager,
        ),
        CancelJobHandler(
            event_store=event_store,
            job_manager=job_manager,
        ),
        QueryEventsHandler(
            event_store=event_store,
        ),
        GenerateSeedHandler(
            interview_engine=interview_engine,
            seed_generator=seed_generator,
            llm_adapter=llm_adapter,
            llm_backend=llm_backend,
        ),
        MeasureDriftHandler(
            event_store=event_store,
        ),
        InterviewHandler(
            interview_engine=interview_engine,
            event_store=event_store,
            llm_adapter=llm_adapter,
            llm_backend=llm_backend,
        ),
        PMInterviewHandler(
            data_dir=state_dir,
            llm_adapter=llm_adapter,
            llm_backend=llm_backend,
        ),
        BrownfieldHandler(),
        ChannelWorkflowHandler(
            workflow_manager=workflow_manager,
            repo_registry=repo_registry,
            default_repo=str(cwd) if str(cwd) != "/" else None,
            interview_handler=InterviewHandler(
                interview_engine=interview_engine,
                event_store=event_store,
                llm_adapter=llm_adapter,
                llm_backend=llm_backend,
            ),
            generate_seed_handler=GenerateSeedHandler(
                interview_engine=interview_engine,
                seed_generator=seed_generator,
                llm_adapter=llm_adapter,
                llm_backend=llm_backend,
            ),
            start_execute_seed_handler=StartExecuteSeedHandler(
                execute_handler=execute_seed,
                event_store=event_store,
                job_manager=job_manager,
            ),
            job_status_handler=JobStatusHandler(
                event_store=event_store,
                job_manager=job_manager,
            ),
            job_wait_handler=JobWaitHandler(
                event_store=event_store,
                job_manager=job_manager,
            ),
            job_result_handler=JobResultHandler(
                event_store=event_store,
                job_manager=job_manager,
            ),
        ),
        EvaluateHandler(
            event_store=event_store,
            llm_adapter=llm_adapter,
            llm_backend=llm_backend,
        ),
        LateralThinkHandler(),
        evolve_step,
        StartEvolveStepHandler(
            evolve_handler=evolve_step,
            event_store=event_store,
            job_manager=job_manager,
        ),
        LineageStatusHandler(
            event_store=event_store,
        ),
        EvolveRewindHandler(
            evolutionary_loop=evolutionary_loop,
        ),
        ACDashboardHandler(
            event_store=event_store,
        ),
        QAHandler(
            llm_adapter=llm_adapter,
            llm_backend=llm_backend,
        ),
        CancelExecutionHandler(
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

    # The server owns the shared event store lifecycle
    server.register_owned_resource(event_store)

    # Inject bridge into all BridgeAwareMixin handlers (loop-based auto-discovery)
    if mcp_bridge is not None:
        from ouroboros.mcp.tools.bridge_mixin import inject_bridge

        injected = [type(h).__name__ for h in tool_handlers if inject_bridge(h, mcp_bridge)]
        if injected:
            log.info("mcp.bridge.injected", handlers=injected)
        server.register_owned_resource(mcp_bridge)

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
