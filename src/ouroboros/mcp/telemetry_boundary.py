"""Truthful, failure-isolated telemetry at MCP request and job boundaries."""

from __future__ import annotations

import builtins
from collections.abc import Awaitable, Callable
import json
import time
from typing import TYPE_CHECKING, Any

from ouroboros import telemetry as usage_telemetry
from ouroboros.backends.capabilities import SubagentDispatchMode
from ouroboros.core.types import Result
from ouroboros.mcp.failure_taxonomy import classify_failure
from ouroboros.mcp.host_context import (
    DispatchAuthority,
    HostCapabilitySource,
    HostSubagentCapability,
    MCPHostContext,
    current_mcp_host_context,
    host_worker_mismatch,
    normalized_worker_backend,
    use_mcp_host_context,
)

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
# (e.g. a registered extension raising AcmePrivateProjectError). A prior
# version of this gate trusted the class's own __module__ string -- but
# __module__ is just an attribute an extension-defined class can set to
# anything ("builtins", "ouroboros_acme_private.errors", ...), and it is not
# even guaranteed to be a string, so reading it could itself raise and
# replace the real Result.err with an AttributeError (breaking the
# never-raises contract this module promises). This gate instead uses a
# CLOSED output vocabulary: only class *names* enumerated in
# _SAFE_ERROR_TYPE_NAMES are ever serialized verbatim, and that set is built
# from sources we control, never from anything the error object claims about
# itself. This makes even a spoofed name harmless -- a class calling itself
# "ValueError" serializes the audited string "ValueError", which identifies
# no one. Kept distinct from _UNKNOWN_TOOL_NAME (a different axis: that one
# is about an unresolved tool name, this one is about an error class).
_EXTENSION_ERROR_TYPE = "ExtensionError"

# Enumerated by inspecting the interpreter's own `builtins` module -- this is
# OUR enumeration of what CPython ships, not a claim the error object makes
# about itself, so it cannot be spoofed by a class naming itself e.g.
# "ValueError" while living in a completely different, extension-owned type.
_BUILTIN_ERROR_NAMES = frozenset(
    name
    for name, obj in vars(builtins).items()
    if isinstance(obj, type) and issubclass(obj, BaseException)
)

# The complete, literal exception hierarchy from ouroboros/mcp/errors.py --
# every class this module's own MCP request/security/tool-dispatch path can
# construct and put into a Result.err. Keep this list in sync with that file
# (grep '^class ' src/ouroboros/mcp/errors.py) whenever it changes.
_OUROBOROS_ERROR_NAMES = frozenset(
    {
        "MCPError",
        "MCPClientError",
        "MCPConnectionError",
        "MCPTimeoutError",
        "MCPProtocolError",
        "MCPServerError",
        "MCPAuthError",
        "MCPResourceNotFoundError",
        "MCPToolError",
    }
)

# Common stdlib exception names that are NOT in `builtins` (they live in
# their own stdlib module, e.g. asyncio.CancelledError, json.JSONDecodeError)
# but are common/expected enough here to keep distinct rather than folding
# to the generic extension literal. Folding a rarer stdlib name is harmless
# -- this list only needs to cover what's worth keeping distinct, not be
# exhaustive.
_EXTRA_STDLIB_ERROR_NAMES = frozenset({"CancelledError", "JSONDecodeError"})

_SAFE_ERROR_TYPE_NAMES = _BUILTIN_ERROR_NAMES | _OUROBOROS_ERROR_NAMES | _EXTRA_STDLIB_ERROR_NAMES


def _safe_error_type(error: object) -> str:
    """Fold a non-audited exception/error class name to a fixed literal.

    Total and failure-isolated by construction: this can never raise, even
    against an error object with hostile metaclass behavior (a __name__
    that raises on access, a non-string __name__, ...), because every step
    is wrapped and any exception folds to the same safe literal a
    non-audited name would. The boundary's never-raises contract must hold
    even when the error value it is describing is itself adversarial.

    Catches ``BaseException``, not just ``Exception``: a hostile metaclass
    property can raise ``KeyboardInterrupt``/``SystemExit`` just as easily
    as ``RuntimeError`` -- that is not a real user interrupt or process
    exit, it is an extension-controlled property choosing an exception
    class designed to slip past a narrower ``except Exception`` and corrupt
    the caller's real result. Swallowing it here is correct: this function
    is sanitization-only, never re-raises, and a genuine Ctrl-C from the
    actual OS signal handler is delivered on the main thread independently
    of this call, not manufactured by reading an object's dunder.
    """
    try:
        name = type(error).__name__
        if isinstance(name, str) and name in _SAFE_ERROR_TYPE_NAMES:
            return name
    except BaseException:  # noqa: BLE001 -- sanitization-only, see docstring
        pass
    return _EXTENSION_ERROR_TYPE


def _is_logical_error(value: object) -> bool:
    """Total/failure-isolated read of ``MCPToolResult.is_error``.

    Mirrors :func:`_safe_error_type`'s isolation. ``getattr(value,
    "is_error", False)`` alone is not enough: its ``default`` only stands in
    when the attribute is genuinely *missing* (raises ``AttributeError``
    internally) -- if ``is_error`` were a property whose getter itself
    raises anything else against a hostile/malformed value, that exception
    would propagate straight past the default and out of this boundary,
    corrupting the real handler result the caller is waiting on. Falls back
    to ``False`` (not a logical error, so the outer ``Result.is_ok`` alone
    decides) rather than guessing either way.

    Catches ``BaseException`` for the same reason as :func:`_safe_error_type`:
    a hostile ``is_error`` property raising ``SystemExit``/``KeyboardInterrupt``
    is an extension-controlled dunder read, not a real interrupt or exit
    request, and this helper never re-raises regardless of what it catches.
    """
    try:
        return bool(getattr(value, "is_error", False))
    except BaseException:  # noqa: BLE001 -- sanitization-only, see docstring
        return False


def _duration_ms(started_at: float) -> float:
    return (time.monotonic() - started_at) * 1000


async def observe_adapter_tool_call[T, E](
    name: str,
    operation: Callable[[], Awaitable[Result[T, E]]],
    *,
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
        usage_telemetry.capture_tool_call(
            safe_name,
            ok=False,
            duration_ms=_duration_ms(started_at),
            error_type=_safe_error_type(exc),
        )
        raise
    logical_error = result.is_ok and _is_logical_error(result.value)
    usage_telemetry.capture_tool_call(
        safe_name,
        ok=result.is_ok and not logical_error,
        duration_ms=_duration_ms(started_at),
        error_type=_safe_error_type(result.error) if result.is_err else None,
        blocked=logical_error,
    )
    return result


def _sdk_tool_error(error: object) -> Exception:
    """Map an explicitly typed tool error onto the SDK's JSON-RPC error channel.

    ``MCPToolError.error_code`` opts into the public machine-readable contract.
    Its details cross the boundary only when they are a canonical JSON object;
    malformed details fall back to the existing execution-error behavior without
    rendering those details into the client-visible message. Untyped errors keep
    the legacy ``RuntimeError(str(error))`` path and therefore remain ordinary
    ``CallToolResult(isError=True)`` responses under the MCP SDK.
    """
    from mcp.shared.exceptions import MCPError as SDKMCPError
    from mcp.types import INTERNAL_ERROR

    from ouroboros.mcp.errors import MCPToolError

    if not isinstance(error, MCPToolError) or not error.error_code:
        return RuntimeError(str(error))

    try:
        details = json.loads(json.dumps(error.details, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        return RuntimeError(error.message)
    if not isinstance(details, dict) or details != error.details:
        return RuntimeError(error.message)

    return SDKMCPError(
        code=INTERNAL_ERROR,
        message=error.message,
        data={"error_code": error.error_code, "details": details},
    )


async def call_sdk_tool(
    adapter: MCPServerAdapter,
    name: str,
    arguments: dict[str, Any],
    *,
    host_context: MCPHostContext | None = None,
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
    from ouroboros.mcp.server.auth import current_auth_context

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
        # The private impl skips call_tool's observer wrapper: this SDK path
        # owns the one request-outcome event itself (no double counting).
        # On a token-protected network bind the SDK already verified the bearer
        # token and the raw credential is gone by now, so carry its decision
        # across instead -- that restores the client identity authorization and
        # rate limiting key on.
        with use_mcp_host_context(host_context or MCPHostContext()):
            result = await adapter._call_tool_impl(
                name,
                arguments,
                auth_context=current_auth_context(),
            )
        if result.is_err:
            error_type = _safe_error_type(result.error)
            raise _sdk_tool_error(result.error)
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
    logical_error = _is_logical_error(value)
    usage_telemetry.capture_tool_call(
        safe_name,
        ok=not logical_error,
        duration_ms=_duration_ms(started_at),
        blocked=logical_error,
    )
    return response


def record_direct_evaluation_outcome(
    *,
    final_approved: bool | None,
    failed: bool = False,
    failure_reason_code: str | None = None,
) -> None:
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
        resolution = classify_failure(
            status,
            {"failure_reason_code": failure_reason_code} if failure_reason_code else None,
        )
        properties: dict[str, Any] = {
            "command": "evaluate",
            "terminal_status": status,
            "verified": (not failed) and final_approved is True,
        }
        if resolution is not None:
            properties["failure_reason_code"] = resolution.reason_code.value
        usage_telemetry.capture(
            "workflow_outcome",
            properties,
        )
    except Exception:
        pass


def record_subagent_dispatch_emitted(
    *,
    fanout_kind: str,
    payload_count: int,
    dispatch_mode: SubagentDispatchMode,
    worker_backend: str | None,
    fanout_reentry_available: bool,
) -> None:
    """Record the privacy-safe contract emitted to a fan-out consumer."""
    try:
        context = current_mcp_host_context()
        if dispatch_mode is SubagentDispatchMode.PLUGIN_PASSIVE:
            authority = DispatchAuthority.PASSIVE_BRIDGE.value
            capability = HostSubagentCapability.PARALLEL.value
            capability_source = HostCapabilitySource.PASSIVE_BRIDGE.value
            delivery_mode = "passive_bridge"
            preference = "parallel"
            fallback = "none"
            reason = "passive_bridge_detected"
        else:
            authority = context.dispatch_authority.value
            capability = context.subagent_capability.value
            capability_source = context.capability_source.value
            delivery_mode = "inline_host" if context.is_mcp_host else "inline_runtime"
            preference = (
                "sequential" if dispatch_mode is SubagentDispatchMode.SEQUENTIAL else "parallel"
            )
            fallback = "sequential"
            if dispatch_mode is SubagentDispatchMode.HOST_DRIVEN:
                reason = "declared_parallel"
            elif context.is_mcp_host and dispatch_mode is SubagentDispatchMode.SEQUENTIAL:
                reason = "declared_sequential"
            elif dispatch_mode is SubagentDispatchMode.HOST_DECIDES:
                reason = "host_capability_undeclared"
            else:
                reason = "configured_internal_runtime"

        usage_telemetry.capture_subagent_dispatch(
            {
                "phase": "emitted",
                "fanout_kind": fanout_kind,
                "payload_count": payload_count,
                "invocation_surface": ("mcp_host" if context.is_mcp_host else "internal_runtime"),
                "dispatch_authority": authority,
                "host_family": context.host_family.value,
                "host_identity_status": context.identity_status.value,
                "host_capability": capability,
                "capability_source": capability_source,
                "delivery_mode": delivery_mode,
                "execution_preference": preference,
                "fallback_strategy": fallback,
                "configured_worker_backend": normalized_worker_backend(worker_backend),
                "host_worker_mismatch": host_worker_mismatch(context, worker_backend),
                "decision_reason": reason,
                "contract_version": "v2",
                "fanout_reentry_available": fanout_reentry_available,
            }
        )
    except Exception:
        pass


def record_subagent_dispatch_submitted(
    *,
    fanout_kind: str,
    submission_status: str,
    expected_count: int,
    received_count: int,
    undispatched_count: int,
) -> None:
    """Record fan-out re-entry counts without identifiers or output content."""
    usage_telemetry.capture_subagent_dispatch(
        {
            "phase": "submitted",
            "fanout_kind": fanout_kind,
            "submission_status": submission_status,
            "expected_count": expected_count,
            "received_count": received_count,
            "undispatched_count": undispatched_count,
            "contract_version": "v2",
        }
    )


def stamp_backend_context(
    runtime_backend: str | None,
    execute_runtime_backend: str | None,
    interview_llm_backend: str | None,
    evaluate_llm_backend: str | None,
) -> None:
    """Stamp only the retained runtime-backend dimension."""
    del execute_runtime_backend, interview_llm_backend, evaluate_llm_backend
    usage_telemetry.set_context(runtime_backend=runtime_backend)


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
    "record_subagent_dispatch_emitted",
    "record_subagent_dispatch_submitted",
    "stamp_backend_context",
]
