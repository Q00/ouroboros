"""``call_sdk_tool`` resolves the called tool by name, not by listing every tool.

``call_sdk_tool`` used to find the called tool's definition with
``next(d for d in await adapter.list_tools() if d.name == name)``, which
rebuilt every registered tool's definition (one of them regenerates a pydantic
JSON schema) on every SDK tool call. It now reads the one handler from the
adapter's name-keyed registry. These tests pin that the lookup is equivalent to
the old scan and that the scan no longer happens.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import ANY, AsyncMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.server.adapter import MCPServerAdapter
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)

pytest.importorskip("mcp.server")

_CAPTURE = "ouroboros.mcp.telemetry_boundary.usage_telemetry.capture_tool_call"
_OK_TEXT = (MCPContentItem(type=ContentType.TEXT, text="ok"),)


class _ProbeHandler:
    """Minimal tool handler whose ``definition`` property counts evaluations."""

    def __init__(
        self,
        name: str,
        result: Result[MCPToolResult, MCPServerError] | None = None,
        *,
        output_schema: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._output_schema = output_schema
        self.definition_builds = 0
        self.handle_mock = AsyncMock(
            return_value=result if result is not None else Result.ok(MCPToolResult(_OK_TEXT))
        )

    @property
    def definition(self) -> MCPToolDefinition:
        self.definition_builds += 1
        return MCPToolDefinition(
            name=self._name,
            description="lookup probe",
            parameters=(MCPToolParameter(name="input", type=ToolInputType.STRING),),
            output_schema=self._output_schema,
        )

    async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, MCPServerError]:
        return await self.handle_mock(arguments)


async def _reference_call_sdk_tool(
    adapter: MCPServerAdapter,
    name: str,
    arguments: dict[str, Any],
) -> Any:
    """``call_sdk_tool`` exactly as it was before this change (list_tools scan)."""
    import time

    from jsonschema import Draft202012Validator

    from ouroboros.mcp import telemetry_boundary as tb
    from ouroboros.mcp.host_context import MCPHostContext, use_mcp_host_context
    from ouroboros.mcp.sdk_mapping import tool_result_to_sdk
    from ouroboros.mcp.server.adapter import _validate_parameter_constraints
    from ouroboros.mcp.server.auth import current_auth_context

    started_at = time.monotonic()
    error_type: str | None = None
    registered = False
    safe_name = tb._UNKNOWN_TOOL_NAME
    try:
        definition = next(
            (item for item in await adapter.list_tools() if item.name == name),
            None,
        )
        if definition is None:
            raise RuntimeError(f"Tool not found: {name}")
        registered = True
        safe_name = name
        if set(arguments) == {"kwargs"} and isinstance(arguments.get("kwargs"), dict):
            arguments = arguments["kwargs"]
        _validate_parameter_constraints(definition.parameters, arguments)
        Draft202012Validator(definition.to_input_schema()).validate(arguments)
        with use_mcp_host_context(MCPHostContext()):
            result = await adapter._call_tool_impl(
                name,
                arguments,
                auth_context=current_auth_context(),
            )
        if result.is_err:
            error_type = tb._safe_error_type(result.error)
            raise tb._sdk_tool_error(result.error)
        value = result.value
        if definition.output_schema is not None:
            Draft202012Validator(definition.output_schema).validate(value.structured_content)
        response = tool_result_to_sdk(value)
    except BaseException as exc:
        tb.usage_telemetry.capture_tool_call(
            safe_name,
            ok=False,
            duration_ms=tb._duration_ms(started_at),
            error_type=error_type or tb._safe_error_type(exc),
            registered=registered,
        )
        raise
    logical_error = tb._is_logical_error(value)
    tb.usage_telemetry.capture_tool_call(
        safe_name,
        ok=not logical_error,
        duration_ms=tb._duration_ms(started_at),
        blocked=logical_error,
        registered=registered,
        origin=tb._seed_origin(safe_name, value),
    )
    return response


def _probe_adapter() -> MCPServerAdapter:
    adapter = MCPServerAdapter()
    for handler in (
        _ProbeHandler("probe_ok"),
        _ProbeHandler("probe_err", Result.err(MCPServerError("adapter failed"))),
        _ProbeHandler(
            "probe_typed_err",
            Result.err(
                MCPToolError(
                    "typed failure",
                    tool_name="probe_typed_err",
                    error_code="probe_code",
                    details={"reason": "probe"},
                )
            ),
        ),
        _ProbeHandler("probe_logical", Result.ok(MCPToolResult(_OK_TEXT, is_error=True))),
        _ProbeHandler(
            "probe_output",
            Result.ok(MCPToolResult(_OK_TEXT, structured_content={"approved": "yes"})),
            output_schema={
                "type": "object",
                "properties": {"approved": {"type": "boolean"}},
                "required": ["approved"],
            },
        ),
        _ProbeHandler(
            "ouroboros_generate_seed",
            Result.ok(
                MCPToolResult(
                    _OK_TEXT, meta={"source": "session_context", "status": "seed_generated"}
                )
            ),
        ),
    ):
        adapter.register_tool(handler)
    return adapter


async def _outcome(
    call: Callable[[MCPServerAdapter, Any, dict[str, Any]], Awaitable[Any]],
    name: Any,
    arguments: dict[str, Any],
) -> tuple[Any, ...]:
    """Run one call on a fresh adapter; return its result/exception and telemetry."""
    adapter = _probe_adapter()
    with patch(_CAPTURE) as capture:
        try:
            outcome: tuple[Any, ...] = ("returned", await call(adapter, name, dict(arguments)))
        except Exception as exc:  # noqa: BLE001 -- the outcome under comparison
            outcome = ("raised", type(exc), str(exc))
    telemetry = [
        (c.args, {**c.kwargs, "duration_ms": ANY} if "duration_ms" in c.kwargs else c.kwargs)
        for c in capture.call_args_list
    ]
    dispatched = {
        key: handler.handle_mock.await_args_list for key, handler in adapter._tool_handlers.items()
    }
    return (*outcome, telemetry, dispatched)


_CASES: list[tuple[Any, dict[str, Any]]] = [
    ("probe_ok", {"input": "safe"}),
    ("probe_ok", {"kwargs": {"input": "safe"}}),
    ("probe_ok", {"input": 5}),
    ("probe_ok", {}),
    ("probe_err", {"input": "safe"}),
    ("probe_typed_err", {"input": "safe"}),
    ("probe_logical", {"input": "safe"}),
    ("probe_output", {"input": "safe"}),
    ("ouroboros_generate_seed", {"input": "safe"}),
    # Unknown names: unregistered, empty, case/whitespace/NUL variants.
    ("ouroboros_missing", {"input": "safe"}),
    ("", {}),
    ("PROBE_OK", {"input": "safe"}),
    (" probe_ok", {"input": "safe"}),
    ("probe_ok\x00", {"input": "safe"}),
    ("ouroboros_/home/alice/private-project", {}),
    # Non-str names can never equal a registered (str) definition name.
    (None, {}),
    (7, {}),
    (("probe_ok",), {}),
    (["probe_ok"], {}),
    ({"name": "probe_ok"}, {}),
]


@pytest.mark.parametrize(("name", "arguments"), _CASES, ids=[repr(c[0]) for c in _CASES])
async def test_call_sdk_tool_matches_list_tools_scan_reference(
    name: Any, arguments: dict[str, Any]
) -> None:
    """Same response or exception, same telemetry, same dispatch as the old scan."""
    from ouroboros.mcp.telemetry_boundary import call_sdk_tool

    expected = await _outcome(_reference_call_sdk_tool, name, arguments)
    actual = await _outcome(call_sdk_tool, name, arguments)

    assert actual == expected


async def test_unknown_tool_keeps_error_and_unregistered_telemetry() -> None:
    from ouroboros.mcp.telemetry_boundary import call_sdk_tool

    adapter = _probe_adapter()
    with patch(_CAPTURE) as capture:
        with pytest.raises(RuntimeError) as raised:
            await call_sdk_tool(adapter, "ouroboros_missing", {"input": "safe"})

    assert type(raised.value) is RuntimeError
    assert str(raised.value) == "Tool not found: ouroboros_missing"
    capture.assert_called_once_with(
        "ouroboros_unknown_tool",
        ok=False,
        duration_ms=ANY,
        error_type="RuntimeError",
        registered=False,
    )
    for handler in adapter._tool_handlers.values():
        handler.handle_mock.assert_not_awaited()


async def test_call_works_without_listing_tools() -> None:
    """A call no longer goes through list_tools(); if it raises, calls still work."""
    from ouroboros.mcp.telemetry_boundary import call_sdk_tool

    adapter = _probe_adapter()
    with (
        patch.object(
            adapter, "list_tools", side_effect=AssertionError("list_tools() called per tool call")
        ) as list_tools,
        patch(_CAPTURE) as capture,
    ):
        response = await call_sdk_tool(adapter, "probe_ok", {"input": "safe"})

    list_tools.assert_not_called()
    assert response.is_error is False
    assert [item.text for item in response.content] == ["ok"]
    adapter._tool_handlers["probe_ok"].handle_mock.assert_awaited_once_with({"input": "safe"})
    capture.assert_called_once_with(
        "probe_ok",
        ok=True,
        duration_ms=ANY,
        blocked=False,
        registered=True,
        origin=None,
    )


async def test_call_builds_only_the_called_tools_definition() -> None:
    """Deterministic saving: one definition build per call instead of one per tool."""
    from ouroboros.mcp.telemetry_boundary import call_sdk_tool

    calls = 5
    reference_adapter = _probe_adapter()
    adapter = _probe_adapter()
    for handler in (*reference_adapter._tool_handlers.values(), *adapter._tool_handlers.values()):
        handler.definition_builds = 0  # register_tool() read each definition once
    with patch(_CAPTURE):
        for _ in range(calls):
            await _reference_call_sdk_tool(reference_adapter, "probe_ok", {"input": "safe"})
        for _ in range(calls):
            await call_sdk_tool(adapter, "probe_ok", {"input": "safe"})

    reference_builds = {k: h.definition_builds for k, h in reference_adapter._tool_handlers.items()}
    builds = {k: h.definition_builds for k, h in adapter._tool_handlers.items()}
    # The old scan built every registered definition on every call.
    assert set(reference_builds.values()) == {calls}
    assert builds == {key: (calls if key == "probe_ok" else 0) for key in builds}


@pytest.mark.parametrize(
    "server_kwargs",
    [{}, {"runtime_backend": "codex", "llm_backend": "claude_code"}],
    ids=["default", "codex-runtime"],
)
async def test_production_registry_keys_equal_definition_names(
    server_kwargs: dict[str, Any],
) -> None:
    """Every registered production tool is found by key exactly as the scan found it."""
    from ouroboros.mcp.server.adapter import create_ouroboros_server

    server = create_ouroboros_server(name="sdk-lookup-probe", **server_kwargs)
    listed = list(await server.list_tools())
    registry = server._tool_handlers

    assert len(registry) >= 30  # the composition root registered the real tool set
    assert [definition.name for definition in listed] == list(registry)
    for key, handler in registry.items():
        assert handler.definition.name == key
    for name in [*registry, "ouroboros_missing", "", "OUROBOROS_QA"]:
        scanned = next((item for item in listed if item.name == name), None)
        by_key = registry[name].definition if name in registry else None
        assert by_key == scanned
