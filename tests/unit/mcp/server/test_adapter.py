"""Tests for MCP server adapter."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.events.io_recorder import get_current_io_journal_recorder
from ouroboros.mcp.errors import MCPResourceNotFoundError, MCPServerError
from ouroboros.mcp.server.adapter import (
    VALID_TRANSPORTS,
    MCPServerAdapter,
    _build_tool_signature_with_aliases,
    _extract_feedback_metadata_from_artifact,
    _project_dir_from_artifact,
    _project_dir_from_seed,
    _safe_cwd,
    validate_transport,
)
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPResourceContent,
    MCPResourceDefinition,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.orchestrator.agent_runtime_context import AgentRuntimeContext
from ouroboros.orchestrator.control_bus import ControlBus, ControlBusDrainError


class MockToolHandler:
    """Mock tool handler for testing."""

    def __init__(self, name: str = "test_tool") -> None:
        self._name = name
        self.handle_mock = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="Success"),),
                )
            )
        )

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name=self._name,
            description="A test tool",
            parameters=(
                MCPToolParameter(
                    name="input",
                    type=ToolInputType.STRING,
                    description="Input value",
                ),
            ),
        )

    async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, MCPServerError]:
        return await self.handle_mock(arguments)


class MockResourceHandler:
    """Mock resource handler for testing."""

    def __init__(self, uri: str = "test://resource") -> None:
        self._uri = uri
        self.handle_mock = AsyncMock(
            return_value=Result.ok(MCPResourceContent(uri=uri, text="Resource content"))
        )

    @property
    def definitions(self) -> list[MCPResourceDefinition]:
        return [
            MCPResourceDefinition(
                uri=self._uri,
                name="Test Resource",
                description="A test resource",
            )
        ]

    async def handle(self, uri: str) -> Result[MCPResourceContent, MCPServerError]:
        return await self.handle_mock(uri)


class TestMCPServerAdapter:
    """Test MCPServerAdapter class."""

    def test_adapter_creation(self) -> None:
        """Adapter is created with correct defaults."""
        adapter = MCPServerAdapter()
        assert adapter.info.name == "ouroboros-mcp"
        assert adapter.info.version == "1.0.0"

    def test_adapter_custom_name(self) -> None:
        """Adapter accepts custom name and version."""
        adapter = MCPServerAdapter(name="custom-server", version="2.0.0")
        assert adapter.info.name == "custom-server"
        assert adapter.info.version == "2.0.0"

    def test_project_dir_from_seed_uses_primary_brownfield_reference(self, tmp_path) -> None:
        """Brownfield primary context should be treated as the project directory."""
        seed = SimpleNamespace(
            metadata=SimpleNamespace(project_dir=None, working_directory=None),
            brownfield_context=SimpleNamespace(
                context_references=(SimpleNamespace(path=str(tmp_path), role="primary"),)
            ),
        )

        assert _project_dir_from_seed(seed) == str(tmp_path)

    def test_project_dir_from_artifact_detects_package_json_root(self, tmp_path) -> None:
        """Artifact path discovery should support package.json-based projects."""
        project_dir = tmp_path / "web-app"
        nested_dir = project_dir / "src" / "components"
        nested_dir.mkdir(parents=True)
        (project_dir / "package.json").write_text('{"name":"web-app"}')

        artifact = f"Write: {nested_dir / 'app.tsx'}"

        assert _project_dir_from_artifact(artifact) == str(project_dir)

    def test_project_dir_from_artifact_handles_spaces_in_paths(self, tmp_path) -> None:
        """Artifact extraction should detect spaced file paths."""
        project_dir = tmp_path / "my project"
        nested_dir = project_dir / "src" / "components"
        nested_dir.mkdir(parents=True)
        (project_dir / "pyproject.toml").write_text("[build-system]")

        artifact = f"Edit: {nested_dir / 'app.tsx'}"

        assert _project_dir_from_artifact(artifact) == str(project_dir)

    def test_build_tool_signature_sanitizes_non_identifier_parameter_names(self) -> None:
        """Invalid MCP parameter names are sanitized to valid Python signatures."""
        parameters = (
            MCPToolParameter(name="file-path", type=ToolInputType.STRING),
            MCPToolParameter(name="max.tokens", type=ToolInputType.INTEGER),
            MCPToolParameter(name="class", type=ToolInputType.BOOLEAN),
        )

        signature, aliases = _build_tool_signature_with_aliases(parameters)
        names = tuple(param.name for param in signature.parameters.values())
        assert names == ("file_path", "max_tokens", "_class")
        assert aliases == {
            "file_path": "file-path",
            "max_tokens": "max.tokens",
            "_class": "class",
        }

    def test_extract_feedback_metadata_from_artifact_parses_structured_warning(self) -> None:
        """Execution artifacts should expose structured evaluation feedback metadata."""
        artifact = """
Parallel Execution Verification Report
Success: 1/1

## Feedback Metadata
Feedback Metadata JSON: {"feedback_metadata": [{"code": "decomposition_depth_warning", "details": {"affected_ac_paths": ["1.1.1"], "affected_count": 1, "max_depth": 3}, "message": "Recursive decomposition reached the soft depth safety net; affected leaves were forced to atomic execution.", "severity": "warning", "source": "parallel_executor"}]}

## AC Results
### AC 1: [PASS] Ship feature
""".strip()

        feedback = _extract_feedback_metadata_from_artifact(artifact)

        assert len(feedback) == 1
        assert feedback[0].code == "decomposition_depth_warning"
        assert feedback[0].severity == "warning"
        assert feedback[0].source == "parallel_executor"
        assert feedback[0].details["max_depth"] == 3
        assert feedback[0].details["affected_ac_paths"] == ["1.1.1"]


class TestMCPServerAdapterTools:
    """Test MCPServerAdapter tool operations."""

    def test_register_tool(self) -> None:
        """register_tool adds a tool handler."""
        adapter = MCPServerAdapter()
        handler = MockToolHandler()

        adapter.register_tool(handler)

        assert adapter.info.capabilities.tools is True

    async def test_list_tools(self) -> None:
        """list_tools returns registered tools."""
        adapter = MCPServerAdapter()
        handler = MockToolHandler("my_tool")

        adapter.register_tool(handler)
        tools = await adapter.list_tools()

        assert len(tools) == 1
        assert tools[0].name == "my_tool"

    async def test_call_tool_success(self) -> None:
        """call_tool invokes handler and returns result."""
        adapter = MCPServerAdapter()
        handler = MockToolHandler("my_tool")
        adapter.register_tool(handler)

        result = await adapter.call_tool("my_tool", {"input": "test"})

        assert result.is_ok
        assert result.value.text_content == "Success"
        handler.handle_mock.assert_called_once_with({"input": "test"})

    async def test_call_tool_scopes_io_journal_recorder_from_runtime_context(self) -> None:
        """MCP tool calls provide per-call journal identity to shared adapters."""

        class _FakeEventStore:
            async def append(self, event: object) -> None:
                pass

        class _RecorderProbeHandler(MockToolHandler):
            def __init__(self) -> None:
                super().__init__("probe_tool")
                self.recorder = None

            async def handle(
                self, arguments: dict[str, Any]
            ) -> Result[MCPToolResult, MCPServerError]:
                self.recorder = get_current_io_journal_recorder()
                return Result.ok(
                    MCPToolResult(
                        content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
                    )
                )

        adapter = MCPServerAdapter()
        adapter.set_runtime_context(
            AgentRuntimeContext(
                event_store=_FakeEventStore(),
                runtime_backend="codex",
                llm_backend="litellm",
            )
        )
        handler = _RecorderProbeHandler()
        adapter.register_tool(handler)

        result = await adapter.call_tool(
            "probe_tool",
            {
                "execution_id": "exec_123",
                "session_id": "sess_123",
                "phase": "reflect",
                "generation_number": 2,
            },
        )

        assert result.is_ok
        assert handler.recorder is not None
        assert handler.recorder.target_type == "execution"
        assert handler.recorder.target_id == "exec_123"
        assert handler.recorder.session_id == "sess_123"
        assert handler.recorder.execution_id == "exec_123"
        assert handler.recorder.phase == "reflect"
        assert handler.recorder.generation_number == 2
        assert get_current_io_journal_recorder() is None

    async def test_call_tool_not_found(self) -> None:
        """call_tool returns error for unknown tool."""
        adapter = MCPServerAdapter()

        result = await adapter.call_tool("unknown_tool", {})

        assert result.is_err
        assert isinstance(result.error, MCPResourceNotFoundError)

    async def test_call_tool_handler_error(self) -> None:
        """call_tool handles handler errors."""
        adapter = MCPServerAdapter()
        handler = MockToolHandler()
        handler.handle_mock.side_effect = RuntimeError("Handler failed")
        adapter.register_tool(handler)

        result = await adapter.call_tool("test_tool", {})

        assert result.is_err
        assert "Handler failed" in str(result.error)


class TestMCPServerAdapterResources:
    """Test MCPServerAdapter resource operations."""

    def test_register_resource(self) -> None:
        """register_resource adds a resource handler."""
        adapter = MCPServerAdapter()
        handler = MockResourceHandler()

        adapter.register_resource(handler)

        assert adapter.info.capabilities.resources is True

    async def test_list_resources(self) -> None:
        """list_resources returns registered resources."""
        adapter = MCPServerAdapter()
        handler = MockResourceHandler("test://my-resource")

        adapter.register_resource(handler)
        resources = await adapter.list_resources()

        assert len(resources) == 1
        assert resources[0].uri == "test://my-resource"

    async def test_read_resource_success(self) -> None:
        """read_resource invokes handler and returns content."""
        adapter = MCPServerAdapter()
        handler = MockResourceHandler("test://resource")
        adapter.register_resource(handler)

        result = await adapter.read_resource("test://resource")

        assert result.is_ok
        assert result.value.text == "Resource content"

    async def test_read_resource_not_found(self) -> None:
        """read_resource returns error for unknown resource."""
        adapter = MCPServerAdapter()

        result = await adapter.read_resource("unknown://resource")

        assert result.is_err
        assert isinstance(result.error, MCPResourceNotFoundError)


class TestMCPServerAdapterInfo:
    """Test MCPServerAdapter info property."""

    def test_info_updates_with_registrations(self) -> None:
        """Server info reflects registered handlers."""
        adapter = MCPServerAdapter()

        # Initially no capabilities
        assert adapter.info.capabilities.tools is False
        assert adapter.info.capabilities.resources is False

        # After registering tool
        adapter.register_tool(MockToolHandler())
        assert adapter.info.capabilities.tools is True

        # After registering resource
        adapter.register_resource(MockResourceHandler())
        assert adapter.info.capabilities.resources is True

    def test_info_includes_tool_definitions(self) -> None:
        """Server info includes tool definitions."""
        adapter = MCPServerAdapter()
        adapter.register_tool(MockToolHandler("tool1"))
        adapter.register_tool(MockToolHandler("tool2"))

        info = adapter.info

        assert len(info.tools) == 2
        tool_names = {t.name for t in info.tools}
        assert "tool1" in tool_names
        assert "tool2" in tool_names


# ── Transport validation ────────────────────────────────────────────


class TestValidateTransport:
    """Tests for validate_transport()."""

    def test_valid_lowercase(self):
        assert validate_transport("stdio") == "stdio"
        assert validate_transport("sse") == "sse"
        assert validate_transport("streamable-http") == "streamable-http"

    def test_case_insensitive(self):
        assert validate_transport("SSE") == "sse"
        assert validate_transport("Stdio") == "stdio"
        assert validate_transport("sSe") == "sse"
        assert validate_transport("STREAMABLE-HTTP") == "streamable-http"
        assert validate_transport("streamable_http") == "streamable-http"

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Invalid transport"):
            validate_transport("http")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Invalid transport"):
            validate_transport("")

    def test_valid_transports_constant(self):
        assert "stdio" in VALID_TRANSPORTS
        assert "sse" in VALID_TRANSPORTS
        assert "streamable-http" in VALID_TRANSPORTS


class TestServeTransport:
    """Tests for MCPServerAdapter.serve() transport handling."""

    @pytest.mark.asyncio
    async def test_invalid_transport_raises(self):
        adapter = MCPServerAdapter()
        with pytest.raises(ValueError, match="Invalid transport"):
            await adapter.serve(transport="bogus")

    @pytest.mark.asyncio
    async def test_sse_passes_host_port_to_fastmcp(self):
        """Verify host/port are forwarded to FastMCP constructor."""
        from unittest.mock import MagicMock, patch

        mock_fastmcp_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.tool = MagicMock(return_value=lambda f: f)
        mock_instance.resource = MagicMock(return_value=lambda f: f)
        mock_instance.run_sse_async = AsyncMock()
        mock_fastmcp_cls.return_value = mock_instance

        adapter = MCPServerAdapter()

        with (
            patch(
                "ouroboros.mcp.server.adapter.FastMCP",
                mock_fastmcp_cls,
                create=True,
            ),
            patch.dict(
                "sys.modules",
                {"mcp.server.fastmcp": MagicMock(FastMCP=mock_fastmcp_cls)},
            ),
        ):
            await adapter.serve(transport="sse", host="0.0.0.0", port=9000)

        mock_fastmcp_cls.assert_called_once()
        call_kwargs = mock_fastmcp_cls.call_args
        assert call_kwargs.kwargs["host"] == "0.0.0.0"
        assert call_kwargs.kwargs["port"] == 9000

    @pytest.mark.asyncio
    async def test_sse_ephemeral_port_zero(self):
        """port=0 must reach FastMCP without being rewritten."""
        from unittest.mock import MagicMock, patch

        mock_fastmcp_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.tool = MagicMock(return_value=lambda f: f)
        mock_instance.resource = MagicMock(return_value=lambda f: f)
        mock_instance.run_sse_async = AsyncMock()
        mock_fastmcp_cls.return_value = mock_instance

        adapter = MCPServerAdapter()

        with (
            patch(
                "ouroboros.mcp.server.adapter.FastMCP",
                mock_fastmcp_cls,
                create=True,
            ),
            patch.dict(
                "sys.modules",
                {"mcp.server.fastmcp": MagicMock(FastMCP=mock_fastmcp_cls)},
            ),
        ):
            await adapter.serve(transport="sse", host="localhost", port=0)

        assert mock_fastmcp_cls.call_args.kwargs["port"] == 0

    @pytest.mark.asyncio
    async def test_streamable_http_passes_host_port_to_fastmcp(self):
        """Verify host/port are forwarded to FastMCP for streamable HTTP."""
        from unittest.mock import MagicMock, patch

        mock_fastmcp_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.tool = MagicMock(return_value=lambda f: f)
        mock_instance.resource = MagicMock(return_value=lambda f: f)
        mock_instance.run_streamable_http_async = AsyncMock()
        mock_fastmcp_cls.return_value = mock_instance

        adapter = MCPServerAdapter()

        with (
            patch(
                "ouroboros.mcp.server.adapter.FastMCP",
                mock_fastmcp_cls,
                create=True,
            ),
            patch.dict(
                "sys.modules",
                {"mcp.server.fastmcp": MagicMock(FastMCP=mock_fastmcp_cls)},
            ),
        ):
            await adapter.serve(transport="streamable-http", host="127.0.0.1", port=9100)

        mock_fastmcp_cls.assert_called_once()
        call_kwargs = mock_fastmcp_cls.call_args
        assert call_kwargs.kwargs["host"] == "127.0.0.1"
        assert call_kwargs.kwargs["port"] == 9100
        mock_instance.run_streamable_http_async.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_streamable_http_real_fastmcp_exposes_mcp_path(self) -> None:
        """Real FastMCP streamable HTTP serving exposes the advertised /mcp path."""
        from unittest.mock import patch

        pytest.importorskip("mcp.server.fastmcp")
        pytest.importorskip("uvicorn")

        served = SimpleNamespace(config=None)

        async def capture_serve(server, *args, **kwargs) -> None:
            served.config = server.config

        adapter = MCPServerAdapter()

        with patch("uvicorn.Server.serve", new=capture_serve):
            await adapter.serve(transport="streamable-http", host="127.0.0.1", port=9100)

        assert served.config is not None
        assert served.config.host == "127.0.0.1"
        assert served.config.port == 9100

        fastmcp = adapter._mcp_server
        assert fastmcp.settings.streamable_http_path == "/mcp"

        route_paths = {getattr(route, "path", None) for route in served.config.app.routes}
        assert "/mcp" in route_paths

    @pytest.mark.asyncio
    async def test_fastmcp_path_enforces_security(self):
        """FastMCP tool wrapper routes through call_tool to enforce security checks."""
        from unittest.mock import MagicMock, patch

        # Create adapter with no auth but input validation enabled (default)
        adapter = MCPServerAdapter()
        adapter.register_tool(MockToolHandler(name="secure_tool"))

        mock_fastmcp_cls = MagicMock()
        mock_instance = MagicMock()
        captured_wrapper = None

        def capture_tool_decorator(name, description):
            """Capture the tool wrapper function."""

            def decorator(func):
                nonlocal captured_wrapper
                captured_wrapper = func
                return func

            return decorator

        mock_instance.tool = capture_tool_decorator
        mock_instance.resource = MagicMock(return_value=lambda f: f)
        mock_instance.run_stdio_async = AsyncMock()
        mock_fastmcp_cls.return_value = mock_instance

        with (
            patch(
                "ouroboros.mcp.server.adapter.FastMCP",
                mock_fastmcp_cls,
                create=True,
            ),
            patch.dict(
                "sys.modules",
                {"mcp.server.fastmcp": MagicMock(FastMCP=mock_fastmcp_cls)},
            ),
        ):
            await adapter.serve(transport="stdio")

        # Verify wrapper was captured
        assert captured_wrapper is not None

        # Test: Path traversal should be rejected by input validation
        with pytest.raises(RuntimeError, match="Path traversal detected"):
            await captured_wrapper(input="../../../etc/passwd")

    @pytest.mark.asyncio
    async def test_fastmcp_rejects_auth_config_at_startup(self):
        """FastMCP serve() rejects auth config upfront with clear error.

        This guard prevents the confusing failure mode where the server
        starts successfully but then rejects every tool call at runtime.
        """
        from ouroboros.mcp.server.security import AuthConfig, AuthMethod

        # Create adapter with auth required
        auth_config = AuthConfig(
            method=AuthMethod.API_KEY,
            api_keys=frozenset(["valid-key"]),
            required=True,
        )
        adapter = MCPServerAdapter(auth_config=auth_config)

        # serve() should reject the incompatible configuration immediately
        with pytest.raises(
            ValueError,
            match="FastMCP transport does not support authentication",
        ):
            await adapter.serve(transport="stdio")

    @pytest.mark.asyncio
    async def test_fastmcp_allows_none_auth_with_required_true(self):
        """FastMCP allows AuthMethod.NONE even with required=True.

        This edge case verifies that required=True with method=NONE doesn't
        trigger the guard, since NONE always allows access regardless of
        the required flag.
        """
        from unittest.mock import MagicMock, patch

        from ouroboros.mcp.server.security import AuthConfig, AuthMethod

        # required=True with method=NONE should not trigger guard
        auth_config = AuthConfig(
            method=AuthMethod.NONE,
            required=True,  # Has no effect when method is NONE
        )
        adapter = MCPServerAdapter(auth_config=auth_config)
        adapter.register_tool(MockToolHandler(name="test_tool"))

        mock_fastmcp_cls = MagicMock()
        mock_instance = MagicMock()
        mock_instance.tool = MagicMock(return_value=lambda f: f)
        mock_instance.resource = MagicMock(return_value=lambda f: f)
        mock_instance.run_stdio_async = AsyncMock()
        mock_fastmcp_cls.return_value = mock_instance

        with (
            patch(
                "ouroboros.mcp.server.adapter.FastMCP",
                mock_fastmcp_cls,
                create=True,
            ),
            patch.dict(
                "sys.modules",
                {"mcp.server.fastmcp": MagicMock(FastMCP=mock_fastmcp_cls)},
            ),
        ):
            # Should not raise - method is NONE so guard passes
            await adapter.serve(transport="stdio")

    @pytest.mark.asyncio
    async def test_fastmcp_rejects_rate_limit_config(self):
        """FastMCP serve() rejects rate limiting config upfront.

        Rate limiting requires client identity which FastMCP cannot provide,
        so the guard prevents the false sense of security.
        """
        from ouroboros.mcp.server.security import RateLimitConfig

        adapter = MCPServerAdapter(
            rate_limit_config=RateLimitConfig(
                enabled=True,
                requests_per_minute=100,
            )
        )

        with pytest.raises(
            ValueError,
            match="FastMCP transport does not support rate limiting",
        ):
            await adapter.serve(transport="stdio")


# ── _safe_cwd helper ──────────────────────────────────────────────────


class TestSafeCwd:
    """Tests for _safe_cwd() fallback logic (issue #400)."""

    def test_returns_cwd_when_writable_and_not_root(self, tmp_path, monkeypatch):
        """Normal writable directory is returned as-is."""
        monkeypatch.chdir(tmp_path)
        assert _safe_cwd() == tmp_path

    def test_falls_back_to_home_when_cwd_is_root(self, monkeypatch):
        """When cwd is /, _safe_cwd should return Path.home()."""
        from pathlib import Path
        from unittest.mock import patch

        with patch("ouroboros.mcp.server.adapter.Path.cwd", return_value=Path("/")):
            result = _safe_cwd()
        assert result == Path.home()

    def test_falls_back_to_home_when_cwd_not_writable(self, tmp_path, monkeypatch):
        """When cwd is not writable, _safe_cwd should return Path.home()."""
        from pathlib import Path
        from unittest.mock import patch

        monkeypatch.chdir(tmp_path)
        with patch("os.access", return_value=False):
            result = _safe_cwd()
        assert result == Path.home()


# ── Factory-level create_ouroboros_server test ───────────────────────


class TestCreateOuroborosServerCwdFallback:
    """Verify create_ouroboros_server() propagates _safe_cwd() fallback to components."""

    def test_cwd_root_propagates_fallback_to_all_components(self, tmp_path):
        """When cwd=/, runtime and LLM adapters receive the fallback directory.

        This is the factory-level complement to the unit-level TestSafeCwd tests.
        """
        import contextlib
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        expected_fallback = Path.home()

        # Track calls to key dependency factories
        mock_create_runtime = MagicMock(return_value=MagicMock())
        mock_create_llm = MagicMock(return_value=MagicMock())

        mock_event_store = MagicMock()
        mock_event_store.initialize = MagicMock()

        def _mock_handler(name: str) -> MagicMock:
            return MagicMock(return_value=MagicMock(definition=MagicMock(name=name)))

        patch_targets = {
            # Force _safe_cwd to see cwd=/
            "ouroboros.mcp.server.adapter.Path.cwd": MagicMock(return_value=Path("/")),
            # Intercept the two adapters that receive cwd=
            "ouroboros.orchestrator.create_agent_runtime": mock_create_runtime,
            "ouroboros.providers.create_llm_adapter": mock_create_llm,
            "ouroboros.orchestrator.resolve_agent_runtime_backend": MagicMock(
                return_value="claude"
            ),
            # Stub heavy service classes
            "ouroboros.bigbang.interview.InterviewEngine": MagicMock(),
            "ouroboros.bigbang.seed_generator.SeedGenerator": MagicMock(),
            "ouroboros.evaluation.EvaluationPipeline": MagicMock(),
            "ouroboros.evolution.loop.EvolutionaryLoop": MagicMock(),
            "ouroboros.evolution.wonder.WonderEngine": MagicMock(),
            "ouroboros.evolution.reflect.ReflectEngine": MagicMock(),
            "ouroboros.verification.extractor.AssertionExtractor": MagicMock(),
            "ouroboros.mcp.job_manager.JobManager": MagicMock(),
            # Stub all tool handler classes
            "ouroboros.mcp.tools.definitions.ExecuteSeedHandler": _mock_handler(
                "ouroboros_execute_seed"
            ),
            "ouroboros.mcp.tools.definitions.StartExecuteSeedHandler": _mock_handler(
                "ouroboros_start_execute_seed"
            ),
            "ouroboros.mcp.tools.definitions.SessionStatusHandler": _mock_handler(
                "ouroboros_session_status"
            ),
            "ouroboros.mcp.tools.definitions.JobStatusHandler": _mock_handler(
                "ouroboros_job_status"
            ),
            "ouroboros.mcp.tools.definitions.JobWaitHandler": _mock_handler("ouroboros_job_wait"),
            "ouroboros.mcp.tools.definitions.JobResultHandler": _mock_handler(
                "ouroboros_job_result"
            ),
            "ouroboros.mcp.tools.definitions.CancelJobHandler": _mock_handler(
                "ouroboros_cancel_job"
            ),
            "ouroboros.mcp.tools.definitions.QueryEventsHandler": _mock_handler(
                "ouroboros_query_events"
            ),
            "ouroboros.mcp.tools.definitions.GenerateSeedHandler": _mock_handler(
                "ouroboros_generate_seed"
            ),
            "ouroboros.mcp.tools.definitions.MeasureDriftHandler": _mock_handler(
                "ouroboros_measure_drift"
            ),
            "ouroboros.mcp.tools.definitions.InterviewHandler": _mock_handler(
                "ouroboros_interview"
            ),
            "ouroboros.mcp.tools.definitions.EvaluateHandler": _mock_handler("ouroboros_evaluate"),
            "ouroboros.mcp.tools.definitions.LateralThinkHandler": _mock_handler(
                "ouroboros_lateral_think"
            ),
            "ouroboros.mcp.tools.definitions.EvolveStepHandler": _mock_handler(
                "ouroboros_evolve_step"
            ),
            "ouroboros.mcp.tools.definitions.StartEvolveStepHandler": _mock_handler(
                "ouroboros_start_evolve_step"
            ),
            "ouroboros.mcp.tools.definitions.LineageStatusHandler": _mock_handler(
                "ouroboros_lineage_status"
            ),
            "ouroboros.mcp.tools.definitions.EvolveRewindHandler": _mock_handler(
                "ouroboros_evolve_rewind"
            ),
            "ouroboros.mcp.tools.definitions.ACDashboardHandler": _mock_handler(
                "ouroboros_ac_dashboard"
            ),
            "ouroboros.mcp.tools.definitions.ACTreeHUDHandler": _mock_handler(
                "ouroboros_ac_tree_hud"
            ),
            "ouroboros.mcp.tools.definitions.CancelExecutionHandler": _mock_handler(
                "ouroboros_cancel_execution"
            ),
            "ouroboros.mcp.tools.pm_handler.PMInterviewHandler": _mock_handler(
                "ouroboros_pm_interview"
            ),
            "ouroboros.mcp.tools.brownfield_handler.BrownfieldHandler": _mock_handler(
                "ouroboros_brownfield"
            ),
            "ouroboros.mcp.tools.qa.QAHandler": _mock_handler("ouroboros_qa"),
            "ouroboros.mcp.tools.registry.ToolRegistry": MagicMock(),
            "ouroboros.config.get_clarification_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_semantic_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_wonder_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_reflect_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_assertion_extraction_model": MagicMock(return_value="test-model"),
        }

        with contextlib.ExitStack() as stack:
            for target, mock_obj in patch_targets.items():
                stack.enter_context(patch(target, mock_obj))

            from ouroboros.mcp.server.adapter import create_ouroboros_server

            create_ouroboros_server(event_store=mock_event_store)

        # 1) Runtime adapter received the fallback directory
        runtime_call = mock_create_runtime.call_args_list[0]
        assert runtime_call.kwargs["cwd"] == expected_fallback, (
            f"create_agent_runtime should receive cwd={expected_fallback}, "
            f"got {runtime_call.kwargs['cwd']}"
        )

        # 2) LLM adapter received the fallback directory
        llm_call = mock_create_llm.call_args
        assert llm_call.kwargs["cwd"] == expected_fallback, (
            f"create_llm_adapter should receive cwd={expected_fallback}, "
            f"got {llm_call.kwargs['cwd']}"
        )


class TestCreateOuroborosServerOpenCodeMode:
    """Verify create_ouroboros_server() threads opencode_mode to handlers."""

    def test_config_resolves_to_opencode_threads_mode_to_subagent_handlers(self):
        """Config-resolved OpenCode plugin mode reaches subagent-aware handlers."""
        import contextlib
        from unittest.mock import MagicMock, patch

        captured_modes: dict[str, list[tuple[str | None, str | None]]] = {}

        def _capture_handler(name: str) -> type:
            """Factory that records runtime backend and opencode mode."""

            class _Handler:
                def __init__(self, **kwargs):
                    mode = kwargs.get("opencode_mode")
                    backend = kwargs.get("agent_runtime_backend")
                    captured_modes.setdefault(name, []).append((backend, mode))
                    self.opencode_mode = mode
                    self.agent_runtime_backend = backend
                    self.definition = MagicMock(name=name)

            return _Handler

        mock_event_store = MagicMock()
        mock_event_store.initialize = MagicMock()

        gated_handlers = {
            "ouroboros_execute_seed": "ouroboros.mcp.tools.definitions.ExecuteSeedHandler",
            "ouroboros_start_execute_seed": "ouroboros.mcp.tools.definitions.StartExecuteSeedHandler",
            "ouroboros_generate_seed": "ouroboros.mcp.tools.definitions.GenerateSeedHandler",
            "ouroboros_interview": "ouroboros.mcp.tools.definitions.InterviewHandler",
            "ouroboros_evaluate": "ouroboros.mcp.tools.definitions.EvaluateHandler",
            "ouroboros_lateral_think": "ouroboros.mcp.tools.definitions.LateralThinkHandler",
            "ouroboros_evolve_step": "ouroboros.mcp.tools.definitions.EvolveStepHandler",
            "ouroboros_start_evolve_step": "ouroboros.mcp.tools.definitions.StartEvolveStepHandler",
            "ouroboros_pm_interview": "ouroboros.mcp.tools.pm_handler.PMInterviewHandler",
            "ouroboros_qa": "ouroboros.mcp.tools.qa.QAHandler",
        }

        def _simple_mock_handler(name: str) -> type:
            """Non-gated handler mock."""

            class _H:
                def __init__(self, **kwargs):
                    self.definition = MagicMock(name=name)

            return _H

        patch_targets = {
            # Config resolves to opencode without runtime_backend arg
            "ouroboros.orchestrator.resolve_agent_runtime_backend": MagicMock(
                return_value="opencode"
            ),
            "ouroboros.config.get_opencode_mode": MagicMock(return_value="plugin"),
            "ouroboros.orchestrator.create_agent_runtime": MagicMock(return_value=MagicMock()),
            "ouroboros.providers.create_llm_adapter": MagicMock(return_value=MagicMock()),
            "ouroboros.bigbang.interview.InterviewEngine": MagicMock(),
            "ouroboros.bigbang.seed_generator.SeedGenerator": MagicMock(),
            "ouroboros.evaluation.EvaluationPipeline": MagicMock(),
            "ouroboros.evolution.loop.EvolutionaryLoop": MagicMock(),
            "ouroboros.evolution.wonder.WonderEngine": MagicMock(),
            "ouroboros.evolution.reflect.ReflectEngine": MagicMock(),
            "ouroboros.verification.extractor.AssertionExtractor": MagicMock(),
            "ouroboros.mcp.job_manager.JobManager": MagicMock(),
            "ouroboros.mcp.tools.definitions.SessionStatusHandler": _simple_mock_handler(
                "ouroboros_session_status"
            ),
            "ouroboros.mcp.tools.definitions.JobStatusHandler": _simple_mock_handler(
                "ouroboros_job_status"
            ),
            "ouroboros.mcp.tools.definitions.JobWaitHandler": _simple_mock_handler(
                "ouroboros_job_wait"
            ),
            "ouroboros.mcp.tools.definitions.JobResultHandler": _simple_mock_handler(
                "ouroboros_job_result"
            ),
            "ouroboros.mcp.tools.definitions.CancelJobHandler": _simple_mock_handler(
                "ouroboros_cancel_job"
            ),
            "ouroboros.mcp.tools.definitions.QueryEventsHandler": _simple_mock_handler(
                "ouroboros_query_events"
            ),
            "ouroboros.mcp.tools.definitions.MeasureDriftHandler": _simple_mock_handler(
                "ouroboros_measure_drift"
            ),
            "ouroboros.mcp.tools.definitions.LineageStatusHandler": _simple_mock_handler(
                "ouroboros_lineage_status"
            ),
            "ouroboros.mcp.tools.definitions.EvolveRewindHandler": _simple_mock_handler(
                "ouroboros_evolve_rewind"
            ),
            "ouroboros.mcp.tools.definitions.ACDashboardHandler": _simple_mock_handler(
                "ouroboros_ac_dashboard"
            ),
            "ouroboros.mcp.tools.definitions.ACTreeHUDHandler": _simple_mock_handler(
                "ouroboros_ac_tree_hud"
            ),
            "ouroboros.mcp.tools.definitions.CancelExecutionHandler": _simple_mock_handler(
                "ouroboros_cancel_execution"
            ),
            "ouroboros.mcp.tools.brownfield_handler.BrownfieldHandler": _simple_mock_handler(
                "ouroboros_brownfield"
            ),
            "ouroboros.mcp.tools.registry.ToolRegistry": MagicMock(),
            "ouroboros.config.get_clarification_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_semantic_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_wonder_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_reflect_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_assertion_extraction_model": MagicMock(return_value="test-model"),
        }

        for handler_name, patch_path in gated_handlers.items():
            patch_targets[patch_path] = _capture_handler(handler_name)

        with contextlib.ExitStack() as stack:
            for target, mock_obj in patch_targets.items():
                stack.enter_context(patch(target, mock_obj))

            from ouroboros.mcp.server.adapter import create_ouroboros_server

            create_ouroboros_server(event_store=mock_event_store)

        for name in gated_handlers:
            assert captured_modes.get(name), f"{name} was not constructed"
            assert all(backend == "opencode" for backend, _mode in captured_modes[name])
            assert all(mode == "plugin" for _backend, mode in captured_modes[name])


class TestCreateOuroborosServerBrownfieldStore:
    """Verify create_ouroboros_server() can share a brownfield store."""

    def test_injected_store_is_shared_with_handler_and_owned_by_server(self):
        """Shared brownfield stores should be injected and closed with the server."""
        import contextlib
        from unittest.mock import MagicMock, patch

        captured_handler_kwargs: dict[str, object] = {}

        class _BrownfieldHandler:
            def __init__(self, **kwargs):
                captured_handler_kwargs.update(kwargs)
                self.definition = MagicMock(name="ouroboros_brownfield")

        def _simple_mock_handler(name: str) -> type:
            class _H:
                def __init__(self, **kwargs):
                    self.definition = MagicMock(name=name)

            return _H

        mock_event_store = MagicMock()
        mock_event_store.initialize = MagicMock()
        mock_brownfield_store = MagicMock()

        patch_targets = {
            "ouroboros.orchestrator.resolve_agent_runtime_backend": MagicMock(
                return_value="claude"
            ),
            "ouroboros.orchestrator.create_agent_runtime": MagicMock(return_value=MagicMock()),
            "ouroboros.providers.create_llm_adapter": MagicMock(return_value=MagicMock()),
            "ouroboros.bigbang.interview.InterviewEngine": MagicMock(),
            "ouroboros.bigbang.seed_generator.SeedGenerator": MagicMock(),
            "ouroboros.evaluation.EvaluationPipeline": MagicMock(),
            "ouroboros.evolution.loop.EvolutionaryLoop": MagicMock(),
            "ouroboros.evolution.wonder.WonderEngine": MagicMock(),
            "ouroboros.evolution.reflect.ReflectEngine": MagicMock(),
            "ouroboros.verification.extractor.AssertionExtractor": MagicMock(),
            "ouroboros.mcp.job_manager.JobManager": MagicMock(),
            "ouroboros.mcp.tools.definitions.ExecuteSeedHandler": _simple_mock_handler(
                "ouroboros_execute_seed"
            ),
            "ouroboros.mcp.tools.definitions.StartExecuteSeedHandler": _simple_mock_handler(
                "ouroboros_start_execute_seed"
            ),
            "ouroboros.mcp.tools.definitions.SessionStatusHandler": _simple_mock_handler(
                "ouroboros_session_status"
            ),
            "ouroboros.mcp.tools.definitions.JobStatusHandler": _simple_mock_handler(
                "ouroboros_job_status"
            ),
            "ouroboros.mcp.tools.definitions.JobWaitHandler": _simple_mock_handler(
                "ouroboros_job_wait"
            ),
            "ouroboros.mcp.tools.definitions.JobResultHandler": _simple_mock_handler(
                "ouroboros_job_result"
            ),
            "ouroboros.mcp.tools.definitions.CancelJobHandler": _simple_mock_handler(
                "ouroboros_cancel_job"
            ),
            "ouroboros.mcp.tools.definitions.QueryEventsHandler": _simple_mock_handler(
                "ouroboros_query_events"
            ),
            "ouroboros.mcp.tools.definitions.GenerateSeedHandler": _simple_mock_handler(
                "ouroboros_generate_seed"
            ),
            "ouroboros.mcp.tools.definitions.MeasureDriftHandler": _simple_mock_handler(
                "ouroboros_measure_drift"
            ),
            "ouroboros.mcp.tools.definitions.InterviewHandler": _simple_mock_handler(
                "ouroboros_interview"
            ),
            "ouroboros.mcp.tools.definitions.EvaluateHandler": _simple_mock_handler(
                "ouroboros_evaluate"
            ),
            "ouroboros.mcp.tools.definitions.LateralThinkHandler": _simple_mock_handler(
                "ouroboros_lateral_think"
            ),
            "ouroboros.mcp.tools.definitions.EvolveStepHandler": _simple_mock_handler(
                "ouroboros_evolve_step"
            ),
            "ouroboros.mcp.tools.definitions.StartEvolveStepHandler": _simple_mock_handler(
                "ouroboros_start_evolve_step"
            ),
            "ouroboros.mcp.tools.definitions.LineageStatusHandler": _simple_mock_handler(
                "ouroboros_lineage_status"
            ),
            "ouroboros.mcp.tools.definitions.EvolveRewindHandler": _simple_mock_handler(
                "ouroboros_evolve_rewind"
            ),
            "ouroboros.mcp.tools.definitions.ACDashboardHandler": _simple_mock_handler(
                "ouroboros_ac_dashboard"
            ),
            "ouroboros.mcp.tools.definitions.ACTreeHUDHandler": _simple_mock_handler(
                "ouroboros_ac_tree_hud"
            ),
            "ouroboros.mcp.tools.definitions.CancelExecutionHandler": _simple_mock_handler(
                "ouroboros_cancel_execution"
            ),
            "ouroboros.mcp.tools.pm_handler.PMInterviewHandler": _simple_mock_handler(
                "ouroboros_pm_interview"
            ),
            "ouroboros.mcp.tools.brownfield_handler.BrownfieldHandler": _BrownfieldHandler,
            "ouroboros.mcp.tools.qa.QAHandler": _simple_mock_handler("ouroboros_qa"),
            "ouroboros.mcp.tools.registry.ToolRegistry": MagicMock(),
            "ouroboros.config.get_opencode_mode": MagicMock(return_value="subprocess"),
            "ouroboros.config.get_clarification_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_semantic_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_wonder_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_reflect_model": MagicMock(return_value="test-model"),
            "ouroboros.config.get_assertion_extraction_model": MagicMock(return_value="test-model"),
        }

        with contextlib.ExitStack() as stack:
            for target, mock_obj in patch_targets.items():
                stack.enter_context(patch(target, mock_obj))

            from ouroboros.mcp.server.adapter import create_ouroboros_server

            server = create_ouroboros_server(
                event_store=mock_event_store,
                brownfield_store=mock_brownfield_store,
            )

        assert captured_handler_kwargs["_store"] is mock_brownfield_store
        assert isinstance(server._owned_resources[0], ControlBus)
        assert server._owned_resources[1:] == [mock_event_store, mock_brownfield_store]


def test_create_ouroboros_server_retains_runtime_context() -> None:
    """The composition root must keep AgentRuntimeContext reachable after return."""
    from ouroboros.mcp.server.adapter import create_ouroboros_server

    server = create_ouroboros_server(runtime_backend="codex", llm_backend="claude_code")

    assert server.runtime_context is not None
    assert server.runtime_context.runtime_backend == "codex"
    assert server.runtime_context.llm_backend == "claude_code"
    assert server.runtime_context.control is not None


@pytest.mark.asyncio
async def test_server_shutdown_drains_runtime_control_bus() -> None:
    """Server-owned ControlBus must not leave subscriber tasks behind."""
    from ouroboros.mcp.server.adapter import create_ouroboros_server

    server = create_ouroboros_server(runtime_backend="codex", llm_backend="claude_code")
    assert server.runtime_context is not None
    bus = server.runtime_context.control
    assert bus is not None
    bus._close_timeout_s = 0.01

    started = asyncio.Event()

    async def blocked(_event: BaseEvent) -> None:
        started.set()
        await asyncio.sleep(60)

    bus.subscribe(lambda _event: True, blocked)
    tasks = bus.publish(
        BaseEvent(
            type="control.directive.emitted",
            aggregate_type="lineage",
            aggregate_id="lin_shutdown_probe",
            data={"directive": "cancel"},
        )
    )
    await asyncio.wait_for(started.wait(), timeout=0.5)

    await server.shutdown()

    assert tasks[0].cancelled()
    assert bus._tasks == set()
    assert server._owned_resources == []


@pytest.mark.asyncio
async def test_server_shutdown_stops_before_dependents_when_control_bus_refuses_drain() -> None:
    """Do not close dependent resources while control subscribers are still live."""
    server = MCPServerAdapter()
    bus = ControlBus(_close_timeout_s=0.01, _cancel_timeout_s=0.01)
    started = asyncio.Event()
    release = asyncio.Event()

    class _DependentResource:
        closed = False

        async def close(self) -> None:
            self.closed = True

    resource = _DependentResource()

    async def stubborn(_event: BaseEvent) -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await release.wait()

    server.register_owned_resource(bus)
    server.register_owned_resource(resource)
    bus.subscribe(lambda _event: True, stubborn)
    tasks = bus.publish(
        BaseEvent(
            type="control.directive.emitted",
            aggregate_type="lineage",
            aggregate_id="lin_shutdown_probe",
            data={"directive": "cancel"},
        )
    )
    await asyncio.wait_for(started.wait(), timeout=0.5)

    with pytest.raises(ControlBusDrainError):
        await asyncio.wait_for(server.shutdown(), timeout=0.5)

    assert resource.closed is False

    release.set()
    await asyncio.wait_for(tasks[0], timeout=0.5)
