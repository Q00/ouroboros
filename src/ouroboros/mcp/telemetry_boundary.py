"""Truthful, failure-isolated telemetry at MCP request and job boundaries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
                error_type=type(exc).__name__,
            )
        raise
    if enabled:
        usage_telemetry.capture_tool_call(
            safe_name,
            ok=result.is_ok,
            duration_ms=_duration_ms(started_at),
            error_type=type(result.error).__name__ if result.is_err else None,
        )
    return result


async def call_sdk_tool(
    adapter: MCPServerAdapter,
    name: str,
    arguments: dict[str, Any],
) -> Any:
    """Own SDK validation plus the one complete request-outcome event."""
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
            error_type = type(result.error).__name__
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
            error_type=error_type or type(exc).__name__,
        )
        raise
    usage_telemetry.capture_tool_call(safe_name, ok=True, duration_ms=_duration_ms(started_at))
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
