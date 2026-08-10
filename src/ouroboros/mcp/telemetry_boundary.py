"""Truthful, failure-isolated telemetry at MCP request and job boundaries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import sys
import time
from typing import TYPE_CHECKING, Any

from ouroboros import telemetry as usage_telemetry
from ouroboros.core.types import Result

if TYPE_CHECKING:
    from ouroboros.mcp.server.adapter import MCPServerAdapter

_TERMINAL_JOB_EVENTS = frozenset(
    {
        "mcp.job.completed",
        "mcp.job.failed",
        "mcp.job.cancelled",
        "mcp.job.interrupted",
    }
)

# capture_tool_call() derives both "tool" and "command" from whatever name
# reaches it, so a caller-controlled unregistered name (e.g. a filesystem
# path smuggled in as a tool call) must never be queued verbatim -- that
# would leak arbitrary caller-controlled strings into telemetry despite
# TELEMETRY.md promising file paths are never collected. Every unregistered
# lookup is folded to this fixed literal instead. It keeps the "ouroboros_"
# prefix so capture_tool_call's existing funnel gate still records it (as
# command="unknown_tool", is_funnel=False).
_UNKNOWN_TOOL_NAME = "ouroboros_unknown_tool"

# error_type is a plain exception-class name, so it is just as capable of
# smuggling a caller-controlled/extension-defined identifier as `tool` was
# (e.g. a registered extension raising AcmePrivateProjectError). Only class
# names we can vouch for -- stdlib, builtins, or our own `ouroboros`
# package -- are ever queued verbatim; every third-party/extension class
# folds to this fixed literal instead. Kept distinct from _UNKNOWN_TOOL_NAME
# (a different axis: that one is about an unresolved tool name, this one is
# about an error class we don't recognize).
_EXTENSION_ERROR_TYPE = "ExtensionError"


def _safe_error_type(error: object) -> str:
    """Fold a non-audited exception/error class name to a fixed literal.

    Verbatim only for: builtins (``ValueError``, ``RuntimeError``, ...),
    the standard library (top-level package in ``sys.stdlib_module_names``),
    or our own code (``__module__`` starting with ``"ouroboros"`` -- covers
    ``MCPToolError``/``MCPServerError`` and every internal exception).
    Everything else -- a third-party dependency's exception, or a class an
    extension/registered tool defines itself -- is a class name we cannot
    vouch for as non-identifying, so it folds to ``_EXTENSION_ERROR_TYPE``.
    """
    module = type(error).__module__
    top_level = module.partition(".")[0]
    if (
        module == "builtins"
        or top_level in sys.stdlib_module_names
        or module.startswith("ouroboros")
    ):
        return type(error).__name__
    return _EXTENSION_ERROR_TYPE


def _duration_ms(started_at: float) -> float:
    return (time.monotonic() - started_at) * 1000


async def observe_adapter_tool_call[T, E](
    name: str,
    operation: Callable[[], Awaitable[Result[T, E]]],
    *,
    enabled: bool,
    registered: bool,
) -> Result[T, E]:
    """Run one typed adapter call and emit exactly one sanitized outcome.

    ``name`` is caller-controlled and never trusted for telemetry on its own:
    the caller must assert whether it resolved to a registered tool via
    ``registered``. Only a registered name is ever queued verbatim; otherwise
    the fixed ``_UNKNOWN_TOOL_NAME`` literal stands in.

    ``ok`` reflects both layers of the Result-wrapping-MCPToolResult shape:
    the outer ``Result.is_ok`` (did the call raise or return an error
    object) AND the inner ``MCPToolResult.is_error`` (did the handler
    itself report a logical failure while still returning ``Result.ok``,
    e.g. a validation/input-required response). A logical error has no
    exception to name, so it carries ``error_type=None`` -- the request
    completed, the outcome was a logical error, and there is no dishonest
    "unknown"/"none of the above" value to invent in its place.
    """
    safe_name = name if registered else _UNKNOWN_TOOL_NAME
    started_at = time.monotonic()
    try:
        result = await operation()
    except BaseException as exc:
        if enabled:
            usage_telemetry.capture_tool_call(
                safe_name,
                ok=False,
                duration_ms=_duration_ms(started_at),
                error_type=_safe_error_type(exc),
            )
        raise
    if enabled:
        # getattr, not a direct attribute access: this function is generic
        # over T, and a telemetry-shape assumption must never be able to
        # crash the real call it is only supposed to be observing.
        logical_error = result.is_ok and bool(getattr(result.value, "is_error", False))
        usage_telemetry.capture_tool_call(
            safe_name,
            ok=result.is_ok and not logical_error,
            duration_ms=_duration_ms(started_at),
            error_type=_safe_error_type(result.error) if result.is_err else None,
        )
    return result


async def call_sdk_tool(
    adapter: MCPServerAdapter,
    name: str,
    arguments: dict[str, Any],
) -> Any:
    """Own SDK validation plus the one complete request-outcome event.

    ``ok`` reflects both the outer ``Result.is_ok`` and, once a definition
    is resolved and the call actually runs, the inner
    ``MCPToolResult.is_error`` -- a logical-error response (e.g. a
    validation/input-required payload returned as ``Result.ok``) counts as
    ``ok=False`` here too, matching :func:`observe_adapter_tool_call`. Its
    ``error_type`` stays ``None``: there is no exception to name, only a
    completed request whose outcome was a logical error.
    """
    from jsonschema import Draft202012Validator

    from ouroboros.mcp.sdk_mapping import tool_result_to_sdk
    from ouroboros.mcp.server.adapter import _validate_parameter_constraints

    started_at = time.monotonic()
    error_type: str | None = None
    # Unregistered until a matching definition is found below; never
    # overwritten with the caller-controlled ``name`` before that (see
    # _UNKNOWN_TOOL_NAME).
    safe_name = _UNKNOWN_TOOL_NAME
    try:
        definition = next(
            (item for item in await adapter.list_tools() if item.name == name),
            None,
        )
        if definition is None:
            raise RuntimeError(f"Tool not found: {name}")
        safe_name = name
        if set(arguments) == {"kwargs"} and isinstance(arguments.get("kwargs"), dict):
            arguments = arguments["kwargs"]
        _validate_parameter_constraints(definition.parameters, arguments)
        Draft202012Validator(definition.to_input_schema()).validate(arguments)
        result = await adapter.call_tool(name, arguments, _capture_telemetry=False)
        if result.is_err:
            error_type = _safe_error_type(result.error)
            raise RuntimeError(str(result.error))
        value = result.value
        if definition.output_schema is not None:
            Draft202012Validator(definition.output_schema).validate(value.structured_content)
        response = tool_result_to_sdk(value)
    except BaseException as exc:
        usage_telemetry.capture_tool_call(
            safe_name,
            ok=False,
            duration_ms=_duration_ms(started_at),
            error_type=error_type or _safe_error_type(exc),
        )
        raise
    # getattr, not a direct attribute access: value is always MCPToolResult
    # at the sole current call site, but this must never crash a genuinely
    # successful call over a telemetry-shape assumption.
    logical_error = bool(getattr(value, "is_error", False))
    usage_telemetry.capture_tool_call(
        safe_name, ok=not logical_error, duration_ms=_duration_ms(started_at)
    )
    return response


def record_direct_evaluation_outcome(*, final_approved: bool | None, failed: bool = False) -> None:
    """Durable-terminal telemetry for the direct (non-job) ouroboros_evaluate path.

    Job-backed evaluations reach ``workflow_outcome`` via JobTelemetryBoundary's
    terminal events; a direct ``ouroboros_evaluate`` call never creates a job,
    so without this boundary its completions are invisible to the published
    verified active-user rule and per-backend success rates. Emits the same
    event shape as :func:`ouroboros.telemetry.capture_job_outcome` for
    ``job_type="evaluate"``. No ``$insert_id``: each direct invocation is its
    own outcome, there is no durable job row to replay/deduplicate against.
    """
    try:
        status = "failed" if failed else "completed"
        usage_telemetry.capture(
            "workflow_outcome",
            {
                "command": "evaluate",
                "phase": "terminal",
                "terminal_status": status,
                "ok": not failed,
                "verified": (not failed) and final_approved is True,
                "final_approved": final_approved if isinstance(final_approved, bool) else None,
            },
        )
    except Exception:
        pass


class JobTelemetryBoundary:
    """Remember privacy-safe job classes and observe durable terminal appends."""

    def __init__(self) -> None:
        self._job_types: dict[str, str] = {}

    def remember(self, job_id: str, data: dict[str, Any]) -> None:
        self._job_types[job_id] = str(data.get("job_type", "unknown"))

    def forget(self, job_id: str) -> None:
        self._job_types.pop(job_id, None)

    def observe(self, event_type: str, job_id: str, data: dict[str, Any]) -> None:
        if event_type == "mcp.job.created":
            self.remember(job_id, data)
        if event_type not in _TERMINAL_JOB_EVENTS:
            return
        status = data.get("status")
        if not isinstance(status, str):
            status = event_type.removeprefix("mcp.job.")
        usage_telemetry.capture_job_outcome(
            job_id,
            self._job_types.get(job_id, "unknown"),
            terminal_status=status,
            result_meta=(
                data.get("result_meta") if isinstance(data.get("result_meta"), dict) else None
            ),
        )


__all__ = [
    "JobTelemetryBoundary",
    "call_sdk_tool",
    "observe_adapter_tool_call",
    "record_direct_evaluation_outcome",
]
