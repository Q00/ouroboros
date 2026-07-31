"""High-level MCP SDK client adapter.

The public SDK ``Client`` is the protocol boundary.  It owns modern discovery,
safe legacy fallback, request metadata, response caching, and MRTR.  Ouroboros
only owns application DTO conversion, retry policy, and resource cleanup.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from types import MappingProxyType
from typing import Any, Self

import structlog

from ouroboros.core.retry import retry_async
from ouroboros.core.types import Result
from ouroboros.mcp.client.sdk_factory import SDKClientResources, build_sdk_client
from ouroboros.mcp.errors import MCPClientError, MCPConnectionError, MCPTimeoutError
from ouroboros.mcp.types import (
    MCPCapabilities,
    MCPPeerIdentity,
    MCPPromptDefinition,
    MCPPromptResult,
    MCPResourceContent,
    MCPResourceDefinition,
    MCPResourceResult,
    MCPServerConfig,
    MCPServerInfo,
    MCPServerSnapshot,
    MCPToolDefinition,
    MCPToolResult,
)

log = structlog.get_logger(__name__)

RETRIABLE_EXCEPTIONS = (TimeoutError, ConnectionError, OSError)


def _freeze_json(value: Any) -> Any:
    """Return an immutable defensive copy of a JSON-compatible value."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _capabilities_from_sdk(capabilities: Any) -> MCPCapabilities:
    """Build convenience flags plus the complete immutable capability model."""
    if capabilities is None:
        return MCPCapabilities()
    model_dump = getattr(capabilities, "model_dump", None)
    details = (
        _freeze_json(model_dump(by_alias=True, mode="json", exclude_none=True))
        if callable(model_dump)
        else MappingProxyType({})
    )
    return MCPCapabilities(
        tools=getattr(capabilities, "tools", None) is not None,
        resources=getattr(capabilities, "resources", None) is not None,
        prompts=getattr(capabilities, "prompts", None) is not None,
        logging=getattr(capabilities, "logging", None) is not None,
        completions=getattr(capabilities, "completions", None) is not None,
        tasks=getattr(capabilities, "tasks", None) is not None,
        experimental=getattr(capabilities, "experimental", None) is not None,
        details=details,
    )


def _sdk_model_details(model: Any) -> Any:
    """Capture every SDK model field by wire alias as immutable JSON."""
    if model is None:
        return MappingProxyType({})
    model_dump = getattr(model, "model_dump", None)
    if not callable(model_dump):
        return MappingProxyType({})
    return _freeze_json(model_dump(by_alias=True, mode="json"))


class MCPClientAdapter:
    """Connect to an MCP server through the official auto-negotiating client."""

    def __init__(
        self,
        *,
        max_retries: int = 3,
        retry_wait_initial: float = 1.0,
        retry_wait_max: float = 10.0,
    ) -> None:
        self._max_retries = max_retries
        self._retry_wait_initial = retry_wait_initial
        self._retry_wait_max = retry_wait_max
        self._client: Any = None
        self._http_client: Any = None
        self._server_info: MCPServerInfo | None = None
        self._server_snapshot: MCPServerSnapshot | None = None
        self._config: MCPServerConfig | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    @property
    def server_info(self) -> MCPServerInfo | None:
        return self._server_info

    @property
    def server_snapshot(self) -> MCPServerSnapshot | None:
        """Return the immutable negotiated server view, if connected."""
        return self._server_snapshot

    @property
    def protocol_version(self) -> str | None:
        """Return the negotiated MCP protocol version, if connected."""
        return self._server_snapshot.protocol_version if self._server_snapshot else None

    async def connect(self, config: MCPServerConfig) -> Result[MCPServerInfo, MCPClientError]:
        """Open a client and let SDK v2 negotiate modern or legacy MCP."""
        if self._client is not None or self._http_client is not None:
            disconnected = await self.disconnect()
            if disconnected.is_err:
                log.warning("mcp.disconnect_before_connect_failed", error=disconnected.error)

        self._config = config

        @retry_async(
            on=RETRIABLE_EXCEPTIONS,
            attempts=self._max_retries,
            wait_initial=self._retry_wait_initial,
            wait_max=self._retry_wait_max,
            wait_jitter=1.0,
        )
        async def _connect_with_retry() -> None:
            await self._raw_connect(config)

        try:
            await _connect_with_retry()
            if self._server_info is None:  # pragma: no cover - defensive invariant
                raise RuntimeError("MCP client connected without server information")
            log.info("mcp.connected", server=config.name, transport=config.transport.value)
            return Result.ok(self._server_info)
        except TimeoutError as exc:
            error = MCPTimeoutError(
                f"Connection timeout: {exc}",
                server_name=config.name,
                timeout_seconds=config.timeout,
                operation="connect",
            )
            error.__cause__ = exc
            return Result.err(error)
        except ConnectionError as exc:
            error = MCPConnectionError(
                f"Connection failed: {exc}",
                server_name=config.name,
                transport=config.transport.value,
            )
            error.__cause__ = exc
            return Result.err(error)
        except Exception as exc:
            return Result.err(
                MCPClientError.from_exception(exc, server_name=config.name, is_retriable=False)
            )

    async def _raw_connect(self, config: MCPServerConfig) -> None:
        resources: SDKClientResources | None = None
        try:
            resources = build_sdk_client(config)
            # Publish owned resources before entering so a partial failure can
            # always be cleaned by one atomic reset path.
            self._client = resources.client
            self._http_client = resources.http_client
            await resources.client.__aenter__()
            self._server_info = self._parse_server_info(resources.client, config.name)
            self._server_snapshot = self._parse_server_snapshot(resources.client)
        except Exception:
            # The high-level Client unwinds its own transport.  Ouroboros still
            # closes its explicitly owned HTTP client, even if Client teardown fails.
            if resources is not None:
                await self._reset_connection_state()
            raise

    async def _reset_connection_state(self) -> None:
        """Atomically clear and best-effort close every owned resource."""
        client = self._client
        http_client = self._http_client
        self._client = None
        self._http_client = None
        self._server_info = None
        self._server_snapshot = None

        errors: list[BaseException] = []
        if client is not None:
            try:
                await client.__aexit__(None, None, None)
            except Exception as exc:  # pragma: no cover - defensive cleanup
                errors.append(exc)
        if http_client is not None:
            try:
                await http_client.aclose()
            except Exception as exc:  # pragma: no cover - defensive cleanup
                errors.append(exc)
        if errors:
            raise errors[0]

    @staticmethod
    def _parse_server_info(client: Any, configured_name: str) -> MCPServerInfo:
        capabilities = client.server_capabilities
        identity = client.server_info
        return MCPServerInfo(
            name=identity.name if identity is not None else configured_name,
            version=identity.version if identity is not None else "unknown",
            protocol_version=client.protocol_version,
            capabilities=_capabilities_from_sdk(capabilities),
        )

    @staticmethod
    def _parse_server_snapshot(client: Any) -> MCPServerSnapshot:
        capabilities = client.server_capabilities
        identity = client.server_info
        identity_details = _sdk_model_details(identity)
        app_identity = (
            MCPPeerIdentity(
                name=identity.name,
                application_version=identity.version,
                title=identity.title,
                description=identity.description,
                website_url=identity.website_url,
                icons=tuple(identity_details.get("icons") or ()),
                details=identity_details,
            )
            if identity is not None
            else None
        )
        protocol_version = client.protocol_version
        discover_result = client.session.discover_result
        discovery_details = _sdk_model_details(discover_result)
        supported_versions = (
            tuple(discover_result.supported_versions)
            if discover_result is not None
            else (protocol_version,)
        )
        return MCPServerSnapshot(
            identity=app_identity,
            protocol_version=protocol_version,
            supported_protocol_versions=supported_versions,
            capabilities=_capabilities_from_sdk(capabilities),
            extensions=_freeze_json(getattr(capabilities, "extensions", None) or {}),
            instructions=client.instructions,
            meta=_freeze_json(getattr(discover_result, "meta", None) or {}),
            ttl_ms=getattr(discover_result, "ttl_ms", 0),
            cache_scope=getattr(discover_result, "cache_scope", "private"),
            result_type=getattr(discover_result, "result_type", "complete"),
            discovery_details=discovery_details,
        )

    async def disconnect(self) -> Result[None, MCPClientError]:
        if self._client is None and self._http_client is None:
            return Result.ok(None)
        server_name = self._config.name if self._config else None
        try:
            await self._reset_connection_state()
            log.info("mcp.disconnected", server=server_name or "unknown")
            return Result.ok(None)
        except Exception as exc:
            return Result.err(MCPClientError.from_exception(exc, server_name=server_name))

    def _ensure_connected(self) -> Result[None, MCPClientError]:
        if self._client is None:
            return Result.err(
                MCPConnectionError(
                    "Not connected to any server",
                    server_name=self._config.name if self._config else None,
                )
            )
        return Result.ok(None)

    async def list_tools(self) -> Result[Sequence[MCPToolDefinition], MCPClientError]:
        if (connected := self._ensure_connected()).is_err:
            return Result.err(connected.error)
        try:
            from ouroboros.mcp.sdk_mapping import tool_from_sdk

            values: list[MCPToolDefinition] = []
            cursor: str | None = None
            while True:
                page = await self._client.list_tools(cursor=cursor)
                values.extend(
                    tool_from_sdk(tool, self._config.name if self._config else None)
                    for tool in page.tools
                )
                cursor = page.next_cursor
                if cursor is None:
                    return Result.ok(tuple(values))
        except Exception as exc:
            return self._operation_error(exc)

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> Result[MCPToolResult, MCPClientError]:
        if (connected := self._ensure_connected()).is_err:
            return Result.err(connected.error)
        try:
            from ouroboros.mcp.sdk_mapping import tool_result_from_sdk

            return Result.ok(
                tool_result_from_sdk(await self._client.call_tool(name, arguments or {}))
            )
        except Exception as exc:
            message = str(exc).lower()
            if "not found" in message or "unknown tool" in message:
                return Result.err(self._not_found("tool", name, f"Tool not found: {name}"))
            return Result.err(
                MCPClientError(
                    f"Tool execution failed: {exc}",
                    server_name=self._config.name if self._config else None,
                    is_retriable=False,
                    details={"tool_name": name},
                )
            )

    async def list_resources(self) -> Result[Sequence[MCPResourceDefinition], MCPClientError]:
        if (connected := self._ensure_connected()).is_err:
            return Result.err(connected.error)
        try:
            from ouroboros.mcp.sdk_mapping import resource_from_sdk

            values: list[MCPResourceDefinition] = []
            cursor: str | None = None
            while True:
                page = await self._client.list_resources(cursor=cursor)
                values.extend(resource_from_sdk(resource) for resource in page.resources)
                cursor = page.next_cursor
                if cursor is None:
                    return Result.ok(tuple(values))
        except Exception as exc:
            return self._operation_error(exc)

    async def read_resource_result(self, uri: str) -> Result[MCPResourceResult, MCPClientError]:
        """Read all ordered resource contents and cache/result metadata."""
        if (connected := self._ensure_connected()).is_err:
            return Result.err(connected.error)
        try:
            from ouroboros.mcp.sdk_mapping import resource_result_from_sdk

            result = resource_result_from_sdk(await self._client.read_resource(uri))
            if not result.contents:
                return Result.err(self._not_found("resource", uri, f"Resource not found: {uri}"))
            return Result.ok(result)
        except Exception as exc:
            if "not found" in str(exc).lower():
                return Result.err(self._not_found("resource", uri, f"Resource not found: {uri}"))
            return self._operation_error(exc)

    async def read_resource(self, uri: str) -> Result[MCPResourceContent, MCPClientError]:
        """Compatibility view returning the first resource content item."""
        result = await self.read_resource_result(uri)
        if result.is_err:
            return Result.err(result.error)
        first = result.value.first_content
        if first is None:  # pragma: no cover - guarded in read_resource_result
            return Result.err(self._not_found("resource", uri, f"Resource not found: {uri}"))
        return Result.ok(first)

    async def list_prompts(self) -> Result[Sequence[MCPPromptDefinition], MCPClientError]:
        if (connected := self._ensure_connected()).is_err:
            return Result.err(connected.error)
        try:
            from ouroboros.mcp.sdk_mapping import prompt_from_sdk

            values: list[MCPPromptDefinition] = []
            cursor: str | None = None
            while True:
                page = await self._client.list_prompts(cursor=cursor)
                values.extend(prompt_from_sdk(prompt) for prompt in page.prompts)
                cursor = page.next_cursor
                if cursor is None:
                    return Result.ok(tuple(values))
        except Exception as exc:
            return self._operation_error(exc)

    async def get_prompt(
        self, name: str, arguments: dict[str, str] | None = None
    ) -> Result[str, MCPClientError]:
        """Compatibility view returning the joined text prompt messages."""
        result = await self.get_prompt_result(name, arguments)
        if result.is_err:
            return Result.err(result.error)
        return Result.ok(result.value.text_content)

    async def get_prompt_result(
        self, name: str, arguments: dict[str, str] | None = None
    ) -> Result[MCPPromptResult, MCPClientError]:
        """Get a prompt without flattening roles, content kinds, or result metadata."""
        if (connected := self._ensure_connected()).is_err:
            return Result.err(connected.error)
        try:
            from ouroboros.mcp.sdk_mapping import prompt_result_from_sdk

            return Result.ok(
                prompt_result_from_sdk(await self._client.get_prompt(name, arguments or {}))
            )
        except Exception as exc:
            if "not found" in str(exc).lower():
                return Result.err(self._not_found("prompt", name, f"Prompt not found: {name}"))
            return self._operation_error(exc)

    def _operation_error(self, exc: Exception) -> Result[Any, MCPClientError]:
        return Result.err(
            MCPClientError.from_exception(
                exc, server_name=self._config.name if self._config else None
            )
        )

    def _not_found(self, kind: str, identifier: str, message: str) -> MCPClientError:
        return MCPClientError(
            message,
            server_name=self._config.name if self._config else None,
            is_retriable=False,
            details={"resource_type": kind, "resource_id": identifier},
        )


@asynccontextmanager
async def create_mcp_client(
    config: MCPServerConfig, *, max_retries: int = 3
) -> AsyncIterator[MCPClientAdapter]:
    """Create, connect, and reliably disconnect an MCP adapter."""
    adapter = MCPClientAdapter(max_retries=max_retries)
    async with adapter:
        result = await adapter.connect(config)
        if result.is_err:
            raise result.error
        yield adapter
