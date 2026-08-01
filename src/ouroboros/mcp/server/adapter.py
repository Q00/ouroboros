"""MCP Server adapter implementation.

This module provides the MCPServerAdapter class that implements the MCPServer
protocol using the MCP SDK v2 ``MCPServer``. It handles tool registration, resource
handling, and server lifecycle.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Sequence
import inspect
import keyword
import os
from pathlib import Path
import re
import time
from typing import TYPE_CHECKING, Any

from pydantic import Field
import structlog

from ouroboros.config._model_defaults import DEFAULT_SONNET_MODEL
from ouroboros.config.loader import get_execution_model
from ouroboros.core.seed import ac_text, ac_texts
from ouroboros.core.types import Result
from ouroboros.events.io import new_call_id
from ouroboros.events.io_recorder import IOJournalRecorder, use_io_journal_recorder
from ouroboros.mcp.errors import (
    MCPResourceNotFoundError,
    MCPServerError,
    MCPToolError,
)

# Re-exported: split out in #1754, still imported from here by evaluation tests.
from ouroboros.mcp.server.project_dir import (  # noqa: F401
    _PROJECT_ROOT_MARKERS,
    _looks_like_project_root,
    _project_dir_from_artifact,
    _project_dir_from_seed,
)
from ouroboros.mcp.server.protocol import PromptHandler, ResourceHandler, ToolHandler
from ouroboros.mcp.server.security import AuthConfig, AuthMethod, RateLimitConfig, SecurityLayer
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
from ouroboros.orchestrator.agent_runtime_context import AgentRuntimeContext
from ouroboros.orchestrator.control_bus import ControlBus, ControlBusDrainError

if TYPE_CHECKING:
    from ouroboros.mcp.job_manager import JobManager

log = structlog.get_logger(__name__)

try:  # Keep the core package importable when the optional MCP extra is absent.
    from mcp.server import MCPServer as _SDKMCPServer
except ImportError:  # pragma: no cover - exercised by packaging smoke tests.
    _SDKMCPServer = None  # type: ignore[assignment,misc]


if _SDKMCPServer is not None:

    class _OuroborosSDKServer(_SDKMCPServer):  # type: ignore[misc,valid-type]
        """Public SDK server specialized for Ouroboros's typed handler boundary.

        MCPServer's decorator API derives schemas from Python signatures. Ouroboros
        already owns canonical JSON Schema 2020-12 definitions, so deriving them a
        second time would lose ``$defs``, composition keywords, output schemas, and
        descriptor metadata. Overriding the public primitive methods keeps MCPServer
        responsible for discovery, transports, request metadata, and response framing
        while Ouroboros remains responsible for its application handlers.
        """

        def __init__(self, adapter: MCPServerAdapter, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._ouroboros_adapter = adapter

        async def list_tools(self) -> list[Any]:
            from ouroboros.mcp.sdk_mapping import tool_to_sdk

            return [
                tool_to_sdk(definition) for definition in await self._ouroboros_adapter.list_tools()
            ]

        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any],
            context: Any = None,
        ) -> Any:
            del context
            from jsonschema import Draft202012Validator

            from ouroboros.mcp.sdk_mapping import tool_result_to_sdk

            definition = next(
                (item for item in await self._ouroboros_adapter.list_tools() if item.name == name),
                None,
            )
            if definition is None:
                raise RuntimeError(f"Tool not found: {name}")

            # Accept the one historical wrapper shape at the application edge,
            # but validate the normalized payload against the canonical schema.
            if set(arguments) == {"kwargs"} and isinstance(arguments.get("kwargs"), dict):
                arguments = arguments["kwargs"]
            _validate_parameter_constraints(definition.parameters, arguments)
            Draft202012Validator(definition.to_input_schema()).validate(arguments)

            result = await self._ouroboros_adapter.call_tool(name, arguments)
            if result.is_err:
                raise RuntimeError(str(result.error))
            value = result.value
            if definition.output_schema is not None:
                Draft202012Validator(definition.output_schema).validate(value.structured_content)
            return tool_result_to_sdk(value)

        async def list_resources(self) -> list[Any]:
            from ouroboros.mcp.sdk_mapping import resource_to_sdk

            return [
                resource_to_sdk(definition)
                for definition in await self._ouroboros_adapter.list_resources()
            ]

        async def read_resource(self, uri: Any, context: Any = None) -> list[Any]:
            """Read through the typed adapter without losing content metadata."""
            del context
            from mcp.server.lowlevel.helper_types import ReadResourceContents

            result = await self._ouroboros_adapter.read_resource(str(uri))
            if result.is_err:
                raise RuntimeError(str(result.error))
            content = result.value
            if content.text is not None and content.blob is not None:
                raise ValueError("Resource content cannot contain both text and blob")
            if content.blob is not None:
                payload: str | bytes = base64.b64decode(content.blob, validate=True)
            elif content.text is not None:
                payload = content.text
            else:
                raise ValueError("Resource content requires text or blob")
            return [
                ReadResourceContents(
                    content=payload,
                    mime_type=content.mime_type,
                    meta=content.meta or None,
                )
            ]

        async def list_prompts(self) -> list[Any]:
            from ouroboros.mcp.sdk_mapping import prompt_to_sdk

            return [
                prompt_to_sdk(definition)
                for definition in await self._ouroboros_adapter.list_prompts()
            ]

        async def get_prompt(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            context: Any = None,
        ) -> Any:
            """Render original wire argument names without Python identifier aliases."""
            del context
            from mcp.types import GetPromptResult, PromptMessage, TextContent

            definitions = await self._ouroboros_adapter.list_prompts()
            definition = next((item for item in definitions if item.name == name), None)
            if definition is None:
                raise RuntimeError(f"Prompt not found: {name}")
            wire_arguments = arguments or {}
            declared_names = {argument.name for argument in definition.arguments}
            unknown_names = set(wire_arguments) - declared_names
            if unknown_names:
                raise ValueError(
                    f"Unknown prompt arguments for {name}: {', '.join(sorted(unknown_names))}"
                )
            missing_names = {
                argument.name
                for argument in definition.arguments
                if argument.required and argument.name not in wire_arguments
            }
            if missing_names:
                raise ValueError(
                    f"Missing required prompt arguments for {name}: "
                    f"{', '.join(sorted(missing_names))}"
                )
            result = await self._ouroboros_adapter.get_prompt(
                name,
                {key: str(value) for key, value in wire_arguments.items()},
            )
            if result.is_err:
                raise RuntimeError(str(result.error))
            return GetPromptResult(
                description=definition.description or None,
                messages=[PromptMessage(role="user", content=TextContent(text=result.value))],
            )

else:  # pragma: no cover - construction is rejected before this sentinel is used.
    _OuroborosSDKServer = None  # type: ignore[assignment,misc]

VALID_TRANSPORTS: frozenset[str] = frozenset({"stdio", "sse", "streamable-http"})


def _is_single_segment_resource_uri(uri: str) -> bool:
    """Return True for base URIs like ``scheme://name``."""
    _scheme, separator, rest = uri.partition("://")
    if not separator:
        return "/" not in uri
    return "/" not in rest


def _safe_cwd() -> Path:
    """Return cwd if it looks like a usable project directory, else fall back to home.

    Some launchers can spawn the MCP server with ``cwd=/``, which is not a
    writable project root. This helper centralises the fallback so every
    consumer inside ``create_ouroboros_server`` uses the same safe directory.
    """
    cwd = Path.cwd()
    if cwd == Path("/") or not os.access(cwd, os.W_OK):
        return Path.home()
    return cwd


def _to_mcp_tool_result(tool_result: MCPToolResult) -> Any:
    """Convert internal tool results to MCP SDK results without dropping meta."""
    try:
        from ouroboros.mcp.sdk_mapping import tool_result_to_sdk
    except ImportError as exc:  # pragma: no cover - start() already checks this path.
        msg = "mcp package not installed. Install with: pip install 'ouroboros-ai[mcp]'"
        raise RuntimeError(msg) from exc

    return tool_result_to_sdk(tool_result)


def _sdk_icon(value: dict[str, Any]) -> Any:
    """Build a public SDK icon model at the optional-dependency boundary."""
    from mcp.types import Icon

    return Icon.model_validate(value)


def _sdk_annotations(value: dict[str, Any] | None) -> Any:
    """Build public SDK annotations without importing MCP in the core profile."""
    if value is None:
        return None
    from mcp.types import Annotations

    return Annotations.model_validate(value)


# Kept as a compatibility alias for callers that imported this private helper
# before the SDK v2 migration.
_to_fastmcp_tool_result = _to_mcp_tool_result


def _duration_ms(started_at: float) -> int:
    """Return elapsed monotonic time in whole milliseconds."""
    return int((time.monotonic() - started_at) * 1000)


def _default_interview_state_dir() -> Path:
    """Return the global interview state directory for MCP handlers."""
    from ouroboros.config.models import get_config_dir

    return get_config_dir() / "data"


def _string_argument(arguments: dict[str, Any], *names: str) -> str | None:
    """Return the first non-empty string argument among *names*."""
    for name in names:
        value = arguments.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _int_argument(arguments: dict[str, Any], *names: str) -> int | None:
    """Return the first integer argument among *names*."""
    for name in names:
        value = arguments.get(name)
        if isinstance(value, int):
            return value
    return None


def validate_transport(transport: str) -> str:
    """Normalize and validate a transport string.

    Returns the lowercased transport if valid, raises ValueError otherwise.
    """
    transport = transport.lower().replace("_", "-")
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


# Map MCPToolParameter types to Python annotations for MCPServer schema inference.
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

    MCPServer infers JSON schema from function signatures via inspect.signature().
    Using **kwargs produces a single "kwargs" parameter in the schema, which
    forces clients to wrap arguments as {"kwargs": {actual_args}}.

    By setting __signature__ with explicit parameters, MCPServer generates the
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
        default: Any = inspect.Parameter.empty if p.required else p.default
        if p.description or p.enum is not None or p.items is not None:
            schema_extra: dict[str, Any] = {}
            if p.enum is not None:
                schema_extra["enum"] = list(p.enum)
            if p.items is not None:
                schema_extra["items"] = p.items
            field_kwargs: dict[str, Any] = {
                "description": p.description or None,
                "json_schema_extra": schema_extra or None,
            }
            if p.required:
                # A required parameter still validates as required, but JSON Schema
                # permits a `default` annotation on it and
                # `MCPToolDefinition.to_input_schema()` emits one. Pydantic drops
                # the value along with `Field(default=...)`, so carry it through
                # `json_schema_extra` to keep both surfaces describing the same tool.
                field_kwargs["default"] = ...
                if p.default is not None:
                    schema_extra["default"] = p.default
                    field_kwargs["json_schema_extra"] = schema_extra
            else:
                field_kwargs["default"] = p.default
                if p.default is None:
                    field_kwargs["json_schema_extra"] = lambda schema, extra=schema_extra: (
                        schema.pop("default", None),
                        schema.update(extra),
                    )[-1]
            default = Field(**field_kwargs)
        elif not p.required and p.default is None:
            default = Field(
                default=None,
                json_schema_extra=lambda schema: schema.pop("default", None),
            )

        if p.required:
            sig_params.append(
                inspect.Parameter(
                    name=parameter_name,
                    kind=inspect.Parameter.KEYWORD_ONLY,
                    annotation=python_type,
                    default=(
                        default
                        if p.description or p.enum is not None or p.items is not None
                        else inspect.Parameter.empty
                    ),
                )
            )
        else:
            sig_params.append(
                inspect.Parameter(
                    name=parameter_name,
                    kind=inspect.Parameter.KEYWORD_ONLY,
                    default=default,
                    annotation=python_type,
                )
            )

    return inspect.Signature(parameters=sig_params), alias_to_original


def _build_prompt_signature_with_aliases(
    definition: MCPPromptDefinition,
) -> tuple[inspect.Signature, dict[str, str]]:
    """Build a prompt signature and its safe-name to wire-name mapping."""
    parameters = []
    aliases: dict[str, str] = {}
    alias_counts: dict[str, int] = {}
    for argument in definition.arguments:
        parameter_name = _to_safe_signature_name(argument.name)
        alias_count = alias_counts.get(parameter_name, 0) + 1
        alias_counts[parameter_name] = alias_count
        if alias_count > 1:
            parameter_name = f"{parameter_name}_{alias_count}"
        aliases[parameter_name] = argument.name

        default: Any = inspect.Parameter.empty if argument.required else None
        if argument.description:
            default = Field(
                default=... if argument.required else None,
                description=argument.description,
            )
        parameters.append(
            inspect.Parameter(
                name=parameter_name,
                kind=inspect.Parameter.KEYWORD_ONLY,
                annotation=str,
                default=default,
            )
        )
    return inspect.Signature(parameters=parameters), aliases


def _build_prompt_signature(definition: MCPPromptDefinition) -> inspect.Signature:
    """Build the explicit string signature used for MCP prompt discovery."""
    signature, _ = _build_prompt_signature_with_aliases(definition)
    return signature


def _validate_parameter_constraints(
    parameters: tuple[MCPToolParameter, ...],
    arguments: dict[str, Any],
) -> None:
    def _is_integer(item: Any) -> bool:
        # JSON Schema `type: integer` matches any number with zero fractional
        # part, so `1.0` is a valid integer while `1.5` is not. Booleans are
        # excluded even though `bool` subclasses `int`.
        if isinstance(item, bool):
            return False
        if isinstance(item, int):
            return True
        return isinstance(item, float) and item.is_integer()

    def _is_number(item: Any) -> bool:
        return not isinstance(item, bool) and isinstance(item, int | float)

    item_validators: dict[str, Callable[[Any], bool]] = {
        "string": lambda item: isinstance(item, str),
        "integer": _is_integer,
        "number": _is_number,
        "boolean": lambda item: isinstance(item, bool),
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
    }
    for parameter in parameters:
        if parameter.name not in arguments:
            continue
        value = arguments[parameter.name]
        if value is not None and parameter.enum is not None and value not in parameter.enum:
            raise ValueError(
                f"Invalid value for {parameter.name}: expected one of {parameter.enum}"
            )
        if parameter.items is None or not isinstance(value, list):
            continue
        item_type = parameter.items.get("type")
        is_valid_item = item_validators.get(item_type or "")
        if is_valid_item is None:
            continue
        if any(not is_valid_item(item) for item in value):
            raise ValueError(f"Invalid items for {parameter.name}: expected {item_type} values")


def _parse_legacy_execution_task_summary(artifact: str, seed: Any) -> Any | None:
    """Parse legacy parallel execution output into task completion results.

    Legacy reports rendered worker execution as ``### AC N: [PASS|FAIL]``.
    New reports render the same execution signal as
    ``### Task N: [COMPLETED|FAILED]``. Both describe execution completion, not
    formal evaluator/verifier AC verdicts, so they populate ``task_results``
    instead of ``ac_results``.
    """
    from ouroboros.core.lineage import EvaluationSummary, TaskResult

    task_line_matches = re.findall(
        r"### (?:Task|AC) (\d+): \[(COMPLETED|FAILED|PASS|FAIL)\]\s*(.*)", artifact
    )
    if not task_line_matches:
        return None

    seed_acs = getattr(seed, "acceptance_criteria", None) or ()
    feedback_metadata = _extract_feedback_metadata_from_artifact(artifact)

    task_results: list[TaskResult] = []
    for ac_num_str, status, description in task_line_matches:
        task_idx = int(ac_num_str) - 1
        task_content = (
            ac_text(seed_acs[task_idx]) if task_idx < len(seed_acs) else description.strip()
        )
        completed = status in {"COMPLETED", "PASS"}
        task_results.append(
            TaskResult(
                task_index=task_idx,
                task_content=task_content,
                status="completed" if completed else "failed",
                completed=completed,
                source_ac_index=task_idx,
                evidence=description.strip(),
                execution_method="parallel_report",
            )
        )

    total = len(task_results)
    completed_count = sum(1 for result in task_results if result.completed)
    score = completed_count / total if total > 0 else 0.0

    total_expected_tasks = len(seed_acs) if seed_acs else total
    all_expected_tasks_reported = total >= total_expected_tasks
    all_tasks_completed = completed_count == total and all_expected_tasks_reported

    failed_indices = [result.task_index + 1 for result in task_results if not result.completed]
    failure_reason = None
    if not all_tasks_completed:
        if failed_indices:
            failure_reason = (
                f"{len(failed_indices)}/{total} tasks failed "
                f"(Task {', '.join(str(i) for i in failed_indices)})"
            )
        else:
            failure_reason = (
                f"{completed_count}/{total_expected_tasks} tasks completed; "
                "formal AC evaluation not run"
            )

    execution_completion_status = "completed" if all_tasks_completed else "failed"

    return EvaluationSummary(
        final_approved=False,
        highest_stage_passed=2 if all_tasks_completed else 1,
        score=score,
        drift_score=None,
        failure_reason=failure_reason,
        ac_results=(),
        task_results=tuple(task_results),
        feedback_metadata=feedback_metadata,
        execution_completion_status=execution_completion_status,
        approval_status="not_evaluated",
    )


def _agent_results_from_execution_summary(mechanical: Any) -> dict[int, bool]:
    """Return legacy agent-reported AC outcomes for spec verification.

    Formal ``ACResult`` values take precedence when present.  For legacy
    execution-only summaries, preserve the worker task completion signal via
    ``source_ac_index`` so skipped/unverifiable assertions do not convert a
    worker-reported failure into formal approval.
    """
    agent_results = {ac.ac_index: ac.passed for ac in mechanical.ac_results}
    for task in mechanical.task_results:
        source_ac_index = task.source_ac_index
        if source_ac_index is None:
            source_ac_index = task.task_index
        agent_results.setdefault(source_ac_index, task.completed)
    return agent_results


def _evaluation_summary_from_spec_verification(
    mechanical: Any,
    verification_summary: Any,
) -> Any | None:
    """Promote complete verifier coverage into formal AC verdict results.

    Spec verification may only return reports for ACs that produced extractable
    assertions. Missing reports and reports with no concrete verification
    results are not formal approval: they become failed/not-evaluated AC
    results so a partial verifier pass cannot approve the whole run.
    """
    from ouroboros.core.lineage import ACResult, EvaluationSummary

    reports = tuple(getattr(verification_summary, "reports", ()) or ())
    if not reports:
        return None

    expected_ac_content: dict[int, str] = {
        ac.ac_index: ac.ac_content for ac in mechanical.ac_results
    }
    expected_agent_results = _agent_results_from_execution_summary(mechanical)
    for task in mechanical.task_results:
        source_ac_index = task.source_ac_index
        if source_ac_index is None:
            source_ac_index = task.task_index
        expected_ac_content.setdefault(source_ac_index, task.task_content)

    reports_by_index = {report.ac_index: report for report in reports}
    expected_indices = set(expected_ac_content) | set(expected_agent_results)
    result_indices = sorted(expected_indices | set(reports_by_index))

    ac_results: list[ACResult] = []
    missing_indices: list[int] = []
    unverifiable_indices: list[int] = []
    for ac_index in result_indices:
        report = reports_by_index.get(ac_index)
        if report is None:
            missing_indices.append(ac_index)
            ac_results.append(
                ACResult(
                    ac_index=ac_index,
                    ac_content=expected_ac_content.get(
                        ac_index, f"Acceptance criterion {ac_index + 1}"
                    ),
                    passed=False,
                    score=0.0,
                    evidence="No spec verification report was produced for this AC.",
                    verification_method="spec_verifier",
                    ac_verdict_state="not_evaluated",
                    final_verdict="fail",
                    rendered_verdict="NOT_EVALUATED",
                )
            )
            continue

        details = [result.detail for result in report.results if result.detail]
        evidence = "; ".join(details)
        if not report.results:
            unverifiable_indices.append(ac_index)
            evidence = "No independently verifiable assertions; formal AC verdict not evaluated."
            passed = False
            verdict_state = "not_evaluated"
            rendered_verdict = "NOT_EVALUATED"
        else:
            passed = bool(report.verified_pass)
            verifier_overrode_pass = bool(report.agent_reported_pass) and not passed
            verdict_state = "overridden" if verifier_overrode_pass else "evaluated"
            rendered_verdict = "PASS" if passed else "FAIL"
            if not evidence:
                evidence = "Spec verifier produced no evidence details."

        ac_results.append(
            ACResult(
                ac_index=report.ac_index,
                ac_content=report.ac_text,
                passed=passed,
                score=1.0 if passed else 0.0,
                evidence=evidence,
                verification_method="spec_verifier",
                ac_verdict_state=verdict_state,
                final_verdict="pass" if passed else "fail",
                rendered_verdict=rendered_verdict,
            )
        )

    total = len(ac_results)
    passed_count = sum(1 for result in ac_results if result.passed)
    score = passed_count / total if total > 0 else 0.0
    complete_coverage = bool(expected_indices) and expected_indices.issubset(reports_by_index)
    execution_completed = mechanical.execution_completion_status == "completed"
    approved = complete_coverage and passed_count == total and total > 0 and execution_completed

    failure_reason = None
    if not approved:
        failed_indices = [result.ac_index + 1 for result in ac_results if not result.passed]
        discrepancy_count = getattr(verification_summary, "discrepancy_count", 0)
        reason_parts = []
        if failed_indices:
            reason_parts.append(
                f"{len(failed_indices)}/{total} ACs failed "
                f"(AC {', '.join(str(i) for i in failed_indices)})"
            )
        if discrepancy_count:
            reason_parts.append(f"{discrepancy_count} spec verification override(s)")
        if missing_indices:
            reason_parts.append(
                "missing verifier report for AC " + ", ".join(str(i + 1) for i in missing_indices)
            )
        if unverifiable_indices:
            reason_parts.append(
                "no independently verifiable assertions for AC "
                + ", ".join(str(i + 1) for i in unverifiable_indices)
            )
        if not execution_completed:
            reason_parts.append(
                f"execution_completion_status={mechanical.execution_completion_status}"
            )
        if not reason_parts:
            reason_parts.append("spec verification did not approve the run")
        failure_reason = reason_parts[0]
        if len(reason_parts) > 1:
            failure_reason += f" [{'; '.join(reason_parts[1:])}]"

    return EvaluationSummary(
        final_approved=approved,
        highest_stage_passed=3 if approved else 2,
        score=score,
        drift_score=None,
        failure_reason=failure_reason,
        ac_results=tuple(ac_results),
        task_results=mechanical.task_results,
        feedback_metadata=mechanical.feedback_metadata,
        execution_completion_status=mechanical.execution_completion_status,
        approval_status="approved" if approved else "rejected",
    )


class MCPServerAdapter:
    """Concrete implementation of MCPServer protocol.

    Uses the MCP SDK to expose Ouroboros functionality as an MCP server.
    Supports tool registration, resource handling, and optional security.

    Example:
        server = MCPServerAdapter(
            name="ouroboros-mcp",
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
        version: str | None = None,
        instructions: str | None = None,
        auth_config: AuthConfig | None = None,
        rate_limit_config: RateLimitConfig | None = None,
    ) -> None:
        """Initialize the server adapter.

        Args:
            name: Server name for identification.
            version: Server version.
            instructions: Optional MCP server ``instructions`` text injected into
                every MCP client's context at session start (the cross-provider
                "ubiquitous language" channel). Truncated by some hosts (~2KB).
            auth_config: Optional authentication configuration.
            rate_limit_config: Optional rate limiting configuration.
        """
        if version is None:
            from ouroboros import __version__

            version = __version__
        self._name = name
        self._version = version
        self._instructions = instructions
        self._tool_handlers: dict[str, ToolHandler] = {}
        self._resource_handlers: dict[str, ResourceHandler] = {}
        self._prompt_handlers: dict[str, PromptHandler] = {}
        self._mcp_server: Any = None
        self._owned_resources: list[Any] = []  # objects with async close()
        self._runtime_context: AgentRuntimeContext | None = None
        self._job_manager: JobManager | None = None
        self._last_tool_activity = time.monotonic()

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

    def _find_resource_handler(self, uri: str) -> ResourceHandler | None:
        """Find a resource handler by exact URI or registered base URI prefix."""
        exact_handler = self._resource_handlers.get(uri)
        if exact_handler is not None:
            return exact_handler

        matching_base_uri = max(
            (
                registered_uri
                for registered_uri in self._resource_handlers
                if uri.startswith(f"{registered_uri}/")
            ),
            key=len,
            default=None,
        )
        if matching_base_uri is None:
            return None
        return self._resource_handlers[matching_base_uri]

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
        started_at = time.monotonic()
        self._last_tool_activity = time.monotonic()
        log.info(
            "mcp.server.call_tool.start",
            tool=name,
            server_name=self._name,
            pid=os.getpid(),
            argument_keys=sorted(arguments),
        )
        handler = self._tool_handlers.get(name)
        if not handler:
            error = MCPResourceNotFoundError(
                f"Tool not found: {name}",
                server_name=self._name,
                resource_type="tool",
                resource_id=name,
            )
            log.info(
                "mcp.server.call_tool.return",
                tool=name,
                server_name=self._name,
                pid=os.getpid(),
                duration_ms=_duration_ms(started_at),
                ok=False,
                error_type=type(error).__name__,
            )
            return Result.err(error)

        # Security check
        security_result = await self._security.check_request(name, arguments, credentials)
        if security_result.is_err:
            log.info(
                "mcp.server.call_tool.return",
                tool=name,
                server_name=self._name,
                pid=os.getpid(),
                duration_ms=_duration_ms(started_at),
                ok=False,
                error_type=type(security_result.error).__name__,
            )
            return Result.err(security_result.error)

        try:
            timeout = getattr(handler, "TIMEOUT_SECONDS", None)

            async def invoke_handler() -> Result[MCPToolResult, MCPServerError]:
                if timeout is not None and timeout > 0:
                    return await asyncio.wait_for(handler.handle(arguments), timeout=timeout)
                return await handler.handle(arguments)

            recorder = self._io_recorder_for_tool_call(name, arguments)
            if recorder is not None:
                with use_io_journal_recorder(recorder):
                    result = await invoke_handler()
            else:
                result = await invoke_handler()
            log.info(
                "mcp.server.call_tool.return",
                tool=name,
                server_name=self._name,
                pid=os.getpid(),
                duration_ms=_duration_ms(started_at),
                ok=result.is_ok,
                error_type=type(result.error).__name__ if result.is_err else None,
            )
            return result
        except MCPServerError as exc:
            return Result.err(exc)
        except TimeoutError:
            duration_ms = _duration_ms(started_at)
            log.error("mcp.server.tool_timeout", tool=name, duration_ms=duration_ms)
            log.error(
                "mcp.server.call_tool.error",
                tool=name,
                server_name=self._name,
                pid=os.getpid(),
                duration_ms=duration_ms,
                error_type="TimeoutError",
                error=str(timeout),
            )
            return Result.err(
                MCPToolError(
                    f"Tool execution timed out after {timeout}s: {name}",
                    server_name=self._name,
                    tool_name=name,
                )
            )
        except Exception as e:
            duration_ms = _duration_ms(started_at)
            log.error(
                "mcp.server.tool_error",
                tool=name,
                error=str(e),
                duration_ms=duration_ms,
                exc_info=True,
            )
            log.error(
                "mcp.server.call_tool.error",
                tool=name,
                server_name=self._name,
                pid=os.getpid(),
                duration_ms=duration_ms,
                error_type=type(e).__name__,
                error=str(e),
            )
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
        handler = self._find_resource_handler(uri)
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
        Uses the MCP SDK v2's public ``MCPServer`` implementation.

        Args:
            transport: Transport type - "stdio", "sse", or "streamable-http"
                (case-insensitive).
            host: Host to bind to for network transports. Defaults to "localhost".
            port: Port to bind to for network transports. Defaults to 8080.

        Raises:
            ValueError: If transport is invalid or incompatible with security config.
        """
        transport = validate_transport(transport)

        # The SDK transport cannot provide credentials or client identity.
        if self._security.auth_config.method != AuthMethod.NONE:
            msg = (
                f"MCPServer transport does not support authentication. "
                f"Configured auth method: {self._security.auth_config.method.value}. "
                f"All tool calls will be rejected. Use AuthMethod.NONE for MCPServer transports."
            )
            raise ValueError(msg)

        if self._security.rate_limit_config.enabled:
            msg = (
                "MCPServer transport does not support rate limiting "
                "(requires client identity). Configured rate_limit_config.enabled=True "
                "will have no effect. Disable rate limiting for MCPServer transports."
            )
            raise ValueError(msg)

        if _SDKMCPServer is None:
            msg = "mcp package not installed. Install with: pip install 'ouroboros-ai[mcp]'"
            raise ImportError(msg)

        if _OuroborosSDKServer is None:  # pragma: no cover - mirrors import guard.
            raise ImportError("MCP SDK server boundary unavailable")
        self._mcp_server = _OuroborosSDKServer(
            self,
            name=self._name,
            instructions=self._instructions,
            version=self._version,
        )

        # Register tools with MCPServer.
        for _name, handler in self._tool_handlers.items():
            defn = handler.definition

            def _make_tool_wrapper(h: ToolHandler) -> Any:
                async def tool_wrapper(**kwargs: Any) -> Any:
                    wrapper_started_at = time.monotonic()
                    log.info(
                        "mcp.server.fastmcp_tool_wrapper.entry",
                        tool=h.definition.name,
                        server_name=self._name,
                        pid=os.getpid(),
                        raw_argument_keys=sorted(kwargs),
                    )
                    # Backward compat: unwrap nested kwargs from clients that
                    # used the old schema where the server inferred one "kwargs" param.
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
                        normalized_kwargs.setdefault(alias_to_original.get(key, key), value)
                    optional_parameter_names = {
                        parameter.name
                        for parameter in h.definition.parameters
                        if not parameter.required
                    }
                    normalized_kwargs = {
                        key: value
                        for key, value in normalized_kwargs.items()
                        if value is not None or key not in optional_parameter_names
                    }

                    _validate_parameter_constraints(
                        h.definition.parameters,
                        normalized_kwargs,
                    )

                    # Route through call_tool() to enforce security checks.
                    # MCPServer does not provide credentials, so:
                    # - Input validation is enforced
                    # - Auth/authorization will reject if any auth method configured
                    # - Rate limiting cannot apply (requires client_id)
                    result = await self.call_tool(h.definition.name, normalized_kwargs)
                    if result.is_ok:
                        # Convert MCPToolResult to the SDK boundary type.
                        tool_result = result.value
                        converted = _to_mcp_tool_result(tool_result)
                        log.info(
                            "mcp.server.fastmcp_tool_wrapper.return",
                            tool=h.definition.name,
                            server_name=self._name,
                            pid=os.getpid(),
                            duration_ms=_duration_ms(wrapper_started_at),
                            ok=True,
                        )
                        return converted
                    else:
                        # Raise so the SDK returns a proper MCP error response
                        # with isError: true, instead of a success with error text.
                        log.info(
                            "mcp.server.fastmcp_tool_wrapper.return",
                            tool=h.definition.name,
                            server_name=self._name,
                            pid=os.getpid(),
                            duration_ms=_duration_ms(wrapper_started_at),
                            ok=False,
                            error_type=type(result.error).__name__,
                        )
                        raise RuntimeError(str(result.error))

                # Set a proper signature so MCPServer generates correct JSON schema
                # instead of a single "kwargs" parameter.
                tool_wrapper.__signature__ = _build_tool_signature(h.definition.parameters)
                return tool_wrapper

            wrapper = _make_tool_wrapper(handler)
            self._mcp_server.tool(
                name=defn.name,
                description=defn.description,
            )(wrapper)

        # Register resources with MCPServer.
        for uri, res_handler in self._resource_handlers.items():
            resource_definition = next(
                definition for definition in res_handler.definitions if definition.uri == uri
            )

            def _make_resource_wrapper(h: ResourceHandler, resource_uri: str) -> Any:
                async def resource_wrapper() -> str | bytes:
                    result = await h.handle(resource_uri)
                    if result.is_ok:
                        content = result.value
                        if content.text is not None and content.blob is not None:
                            raise ValueError("Resource content cannot contain both text and blob")
                        if content.blob is not None:
                            return base64.b64decode(content.blob, validate=True)
                        if content.text is not None:
                            return content.text
                        raise ValueError("Resource content requires text or blob")
                    else:
                        raise RuntimeError(str(result.error))

                return resource_wrapper

            wrapper = _make_resource_wrapper(res_handler, uri)
            self._mcp_server.resource(
                uri,
                name=resource_definition.name,
                title=resource_definition.title,
                description=resource_definition.description,
                mime_type=resource_definition.mime_type,
                icons=[_sdk_icon(icon) for icon in resource_definition.icons] or None,
                annotations=_sdk_annotations(resource_definition.annotations),
                meta=resource_definition.meta or None,
            )(wrapper)

            if _is_single_segment_resource_uri(uri):

                def _make_resource_template_wrapper(h: ResourceHandler, base_uri: str) -> Any:
                    async def resource_template_wrapper(resource_id: str) -> str | bytes:
                        resource_uri = f"{base_uri}/{resource_id}"
                        result = await h.handle(resource_uri)
                        if result.is_ok:
                            content = result.value
                            if content.text is not None and content.blob is not None:
                                raise ValueError(
                                    "Resource content cannot contain both text and blob"
                                )
                            if content.blob is not None:
                                return base64.b64decode(content.blob, validate=True)
                            if content.text is not None:
                                return content.text
                            raise ValueError("Resource content requires text or blob")
                        else:
                            raise RuntimeError(str(result.error))

                    return resource_template_wrapper

                template = f"{uri}/{{resource_id}}"
                template_wrapper = _make_resource_template_wrapper(res_handler, uri)
                self._mcp_server.resource(
                    template,
                    name=resource_definition.name,
                    title=resource_definition.title,
                    description=resource_definition.description,
                    mime_type=resource_definition.mime_type,
                    icons=[_sdk_icon(icon) for icon in resource_definition.icons] or None,
                    annotations=_sdk_annotations(resource_definition.annotations),
                    meta=resource_definition.meta or None,
                )(template_wrapper)

        # Prompts are first-class discoverable MCP primitives in v2. Register
        # them alongside tools/resources instead of exposing them only through
        # the adapter's local API.
        for _name, prompt_handler in self._prompt_handlers.items():
            prompt_definition = prompt_handler.definition

            def _make_prompt_wrapper(h: PromptHandler) -> Any:
                async def prompt_wrapper(**kwargs: str) -> str:
                    _, alias_to_original = _build_prompt_signature_with_aliases(h.definition)
                    arguments = {
                        alias_to_original.get(name, name): value
                        for name, value in kwargs.items()
                        if value is not None
                    }
                    result = await h.handle(arguments)
                    if result.is_ok:
                        return result.value
                    raise RuntimeError(str(result.error))

                prompt_wrapper.__signature__ = _build_prompt_signature(h.definition)
                prompt_wrapper.__annotations__ = {
                    **dict.fromkeys(prompt_wrapper.__signature__.parameters, str),
                    "return": str,
                }
                return prompt_wrapper

            prompt_wrapper = _make_prompt_wrapper(prompt_handler)
            self._mcp_server.prompt(
                name=prompt_definition.name,
                title=prompt_definition.title,
                description=prompt_definition.description,
                icons=[_sdk_icon(icon) for icon in prompt_definition.icons] or None,
            )(prompt_wrapper)

        log.info(
            "mcp.server.starting",
            name=self._name,
            tools=len(self._tool_handlers),
            resources=len(self._resource_handlers),
        )
        serve_started_at = time.monotonic()

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
        try:
            if transport == "sse":
                await self._mcp_server.run_sse_async(host=host, port=port)
            elif transport == "streamable-http":
                await self._mcp_server.run_streamable_http_async(
                    host=host,
                    port=port,
                    stateless_http=True,
                )
            else:
                await self._mcp_server.run_stdio_async()
        except BaseException as exc:
            log.error(
                "mcp.server.serve_error",
                name=self._name,
                transport=transport,
                pid=os.getpid(),
                duration_ms=_duration_ms(serve_started_at),
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=True,
            )
            raise
        finally:
            log.info(
                "mcp.server.serve_exit",
                name=self._name,
                transport=transport,
                pid=os.getpid(),
                duration_ms=_duration_ms(serve_started_at),
            )

    @property
    def runtime_context(self) -> AgentRuntimeContext | None:
        """Return the session-scoped runtime context owned by this server."""
        return self._runtime_context

    def set_runtime_context(self, context: AgentRuntimeContext) -> None:
        """Attach the session-scoped runtime context to the server object graph."""
        self._runtime_context = context

    def register_owned_resource(self, resource: Any) -> None:
        """Register a resource whose ``close()`` will be called on shutdown."""
        self._owned_resources.append(resource)

    @property
    def job_manager(self) -> JobManager | None:
        """Return the background-job manager owned by this server, if any.

        Exposed so the serve shutdown path can drain live jobs *before* the
        EventStore closes; job tasks killed by ``asyncio.run`` teardown after
        the store is gone fail their terminal appends and leave RUNNING
        zombie rows in the DB.
        """
        return self._job_manager

    def set_job_manager(self, job_manager: JobManager) -> None:
        """Attach the background-job manager to the server object graph."""
        self._job_manager = job_manager

    @property
    def seconds_since_last_tool_call(self) -> float:
        """Seconds since the last tool call (or server creation) — idle gauge."""
        return time.monotonic() - self._last_tool_activity

    def _io_recorder_for_tool_call(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> IOJournalRecorder | None:
        """Build a per-MCP-call recorder for shared LLM adapters."""
        context = self._runtime_context
        if context is None:
            return None
        event_store = getattr(context, "event_store", None)
        if event_store is None:
            return None

        session_id = _string_argument(arguments, "session_id", "qa_session_id")
        execution_id = _string_argument(arguments, "execution_id")
        lineage_id = _string_argument(arguments, "lineage_id")
        generation_number = _int_argument(arguments, "generation_number", "generation")
        phase = _string_argument(arguments, "phase", "current_phase")

        if execution_id is not None:
            target_type = "execution"
            target_id = execution_id
        elif lineage_id is not None:
            target_type = "lineage"
            target_id = lineage_id
        elif session_id is not None:
            target_type = "session"
            target_id = session_id
        else:
            target_type = "mcp_tool"
            target_id = f"{name}:{new_call_id()}"

        return IOJournalRecorder(
            event_store=event_store,
            target_type=target_type,
            target_id=target_id,
            session_id=session_id,
            execution_id=execution_id,
            lineage_id=lineage_id,
            generation_number=generation_number,
            phase=phase,
        )

    async def shutdown(self) -> None:
        """Shutdown the server gracefully, closing owned resources."""
        log.info("mcp.server.shutdown", name=self._name)
        for resource in self._owned_resources:
            close_fn = getattr(resource, "close", None)
            if callable(close_fn):
                try:
                    await close_fn()
                except ControlBusDrainError:
                    log.error(
                        "mcp.server.control_bus_close_failed",
                        resource=type(resource).__name__,
                    )
                    raise
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
    version: str | None = None,
    instructions: str | None = None,
    auth_config: AuthConfig | None = None,
    rate_limit_config: RateLimitConfig | None = None,
    event_store: Any | None = None,
    brownfield_store: Any | None = None,
    state_dir: Any | None = None,
    runtime_backend: str | None = None,
    llm_backend: str | None = None,
    opencode_mode: str | None = None,
    mcp_bridge: Any | None = None,
    durable_jobs: bool = True,
    forced_inline_job_id: str | None = None,
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
        brownfield_store: Optional BrownfieldStore instance for shared brownfield
            MCP access. If not provided, handlers create their own store.
        state_dir: Optional pathlib.Path for interview state directory.
                   If not provided, uses ``get_config_dir() / "data"``
                   (typically ``~/.ouroboros/data``).
        runtime_backend: Optional orchestrator runtime backend override.
        llm_backend: Optional LLM-only backend override.
        opencode_mode: Optional OpenCode integration mode (``"plugin"`` or
            ``"subprocess"``). When None, resolved from
            ``orchestrator.opencode_mode`` in the config file. Controls
            whether ``_subagent`` envelopes are emitted (plugin) or handlers
            run in-process (subprocess / non-opencode runtimes).
        durable_jobs: When true, Start* background work is owned by detached
            worker processes so it survives MCP/client turn shutdown.
        forced_inline_job_id: Internal one-shot recursion boundary used by a
            detached worker. Its top-level Start* call runs inline under this
            accepted job id; nested background work remains durable.

    Returns:
        Configured MCPServerAdapter with all tools registered.

    Raises:
        ImportError: If MCP SDK is not installed.
    """
    from rich.console import Console

    # Import service dependencies
    from ouroboros.bigbang.interview import InterviewEngine
    from ouroboros.bigbang.seed_generator import SeedGenerator
    from ouroboros.config import (
        get_llm_backend_for_role,
        get_llm_model_for_role,
        get_runtime_controls_config,
        load_config,
    )
    from ouroboros.core.errors import ConfigError
    from ouroboros.evaluation import (
        EvaluationContext,
        EvaluationPipeline,
        PipelineConfig,
        SemanticConfig,
    )
    from ouroboros.mcp.job_manager import JobManager
    from ouroboros.mcp.resources.handlers import (
        EventsResourceHandler,
        SeedsResourceHandler,
        SessionsResourceHandler,
    )
    from ouroboros.mcp.tools.brownfield_handler import BrownfieldHandler
    from ouroboros.mcp.tools.conductor_handler import RecordConductorDecisionHandler
    from ouroboros.mcp.tools.definitions import (
        ACDashboardHandler,
        ACTreeHUDHandler,
        AutoHandler,
        CancelExecutionHandler,
        CancelJobHandler,
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
        ProjectionQueryHandler,
        QueryEventsHandler,
        RalphHandler,
        SessionStatusHandler,
        StartAutoHandler,
        StartEvaluateHandler,
        StartEvolveStepHandler,
        StartExecuteSeedHandler,
        StartRalphHandler,
        SubmitFanoutResultsHandler,
    )
    from ouroboros.mcp.tools.fanout import FanoutRegistry
    from ouroboros.mcp.tools.pm_handler import PMInterviewHandler
    from ouroboros.mcp.tools.qa import QAHandler
    from ouroboros.mcp.tools.registry import ToolRegistry
    from ouroboros.mcp.tools.synapse_handler import SynapseSignalHandler, SynapseTargetsHandler
    from ouroboros.orchestrator import create_agent_runtime, resolve_agent_runtime_backend
    from ouroboros.orchestrator.runner import (
        OrchestratorRunner,
    )
    from ouroboros.orchestrator.synapse import (
        EventStoreSessionSignalTargetResolver,
        SessionSignalHub,
        SessionSignalMailbox,
    )
    from ouroboros.orchestrator_stage import (
        Stage,
        parse_stage,
        resolve_runtime_for_stage,
    )
    from ouroboros.providers import create_llm_adapter

    resolved_runtime_backend = resolve_agent_runtime_backend(runtime_backend)

    profile_stages: dict[Stage, str] | None = None
    profile_default: str | None = None
    try:
        config = load_config()
        profile = config.orchestrator.runtime_profile
        if profile is not None:
            profile_stages = {
                parse_stage(stage): resolve_agent_runtime_backend(backend)
                for stage, backend in profile.stages.items()
            }
            if profile.default:
                profile_default = resolve_agent_runtime_backend(profile.default)
    except ConfigError:
        from ouroboros.config.models import get_default_config

        config = get_default_config()
        profile_stages = None
        profile_default = None

    def stage_runtime_backend(stage: Stage) -> str:
        return resolve_runtime_for_stage(
            stage,
            stages=profile_stages,
            default=profile_default,
            fallback=resolved_runtime_backend,
        )

    def role_llm_backend(role: str) -> str:
        # Single source of truth: delegate to the loader's resolver so the MCP
        # server honors the exact same precedence as every other call site —
        # explicit --llm-backend > per-stage Agent > runtime_profile.default >
        # legacy llm.backend / OUROBOROS_LLM_BACKEND override > this server's
        # runtime_backend arg > configured default agent runtime. Passing
        # resolved_runtime_backend keeps an explicit create_ouroboros_server
        # runtime_backend honored as the default-agent fallback.
        return get_llm_backend_for_role(
            role,
            explicit_backend=llm_backend,
            fallback_runtime_backend=resolved_runtime_backend,
        )

    interview_runtime_backend = stage_runtime_backend(Stage.INTERVIEW)
    execute_runtime_backend = stage_runtime_backend(Stage.EXECUTE)
    evaluate_runtime_backend = stage_runtime_backend(Stage.EVALUATE)
    reflect_runtime_backend = stage_runtime_backend(Stage.REFLECT)
    interview_llm_backend = role_llm_backend("interview")
    evaluate_llm_backend = role_llm_backend("semantic_evaluation")
    reflect_llm_backend = role_llm_backend("reflect")

    # Resolve opencode_mode from config file if caller did not pass one.
    # Controls _subagent envelope dispatch gate in every handler.
    if opencode_mode is None:
        from ouroboros.config import get_opencode_mode

        opencode_mode = get_opencode_mode()

    # Resolve a safe working directory once so all consumers agree.
    # When the MCP server is spawned with cwd=/, Path.cwd() is unusable as a
    # project root, so _safe_cwd() falls back to $HOME.
    effective_cwd = _safe_cwd()

    # Materialize the default runtime once at server creation so backend wiring
    # is validated up front and composition-root tests can assert the selected
    # runtime backend without waiting for a tool invocation.
    default_execute_runtime = create_agent_runtime(
        backend=execute_runtime_backend,
        model=None,
        cwd=effective_cwd,
        llm_backend=evaluate_llm_backend,
    )

    # Create shared LLM adapter for interview/seed paths.
    # Evaluation constructs its own adapter with higher max_turns — see
    # EvaluateHandler.handle in mcp/tools/evaluation_handlers.py.
    # Keep the empty tool envelope for providers that support it, but do not
    # force a single-turn budget: even denied or empty-envelope tool attempts
    # can consume the first turn before the model emits final text.
    stage_max_turns = config.orchestrator.default_max_turns
    from ouroboros.backends import backend_supports_tool_envelope
    from ouroboros.providers import resolve_llm_backend

    llm_adapters: dict[str, Any] = {}

    def create_stage_llm_adapter(backend: str) -> Any:
        return create_llm_adapter(
            backend=backend,
            max_turns=stage_max_turns,
            cwd=effective_cwd,
            allowed_tools=(
                [] if backend_supports_tool_envelope(resolve_llm_backend(backend)) else None
            ),
        )

    def shared_stage_llm_adapter(backend: str) -> Any:
        if backend not in llm_adapters:
            llm_adapters[backend] = create_stage_llm_adapter(backend)
        return llm_adapters[backend]

    llm_adapter = shared_stage_llm_adapter(interview_llm_backend)
    evaluation_llm_adapter = shared_stage_llm_adapter(evaluate_llm_backend)
    reflect_llm_adapter = shared_stage_llm_adapter(reflect_llm_backend)

    # The shared interview adapter above is catalog-sealed for
    # envelope-capable backends (``allowed_tools=[]`` → ``--tools ""``), so
    # everything it is injected into must pair it with the tool-less prompt
    # variant: the full socratic-interviewer prompt advertises tool use the
    # subprocess cannot answer, which tempts phantom tool calls (#1537).
    # The gate inside ``InterviewHandler`` only covers adapters the handler
    # constructs itself — injected adapters need this wiring here.
    interview_envelope_sealed = backend_supports_tool_envelope(
        resolve_llm_backend(interview_llm_backend)
    )

    # Create or use provided EventStore
    if event_store is None:
        from ouroboros.persistence.event_store import EventStore

        event_store = EventStore()

    # Create state directory for interviews
    state_dir_path = (
        _default_interview_state_dir() if state_dir is None else Path(state_dir).expanduser()
    )
    state_dir_path.mkdir(parents=True, exist_ok=True)

    # Create core service instances
    interview_engine = InterviewEngine(
        llm_adapter=llm_adapter,
        state_dir=state_dir_path,
        model=get_llm_model_for_role("interview", backend=interview_llm_backend),
        suppress_tool_use_prompt_cues=interview_envelope_sealed,
    )

    seed_generator = SeedGenerator(
        llm_adapter=llm_adapter,
        model=get_llm_model_for_role("seed_generation", backend=interview_llm_backend),
    )

    # Create evolution engines for evolve_step
    from ouroboros.core.lineage import EvaluationSummary
    from ouroboros.evaluation.artifact_collector import ArtifactCollector
    from ouroboros.evolution.loop import EvolutionaryLoop, EvolutionaryLoopConfig
    from ouroboros.evolution.reflect import ReflectEngine
    from ouroboros.evolution.wonder import WonderEngine
    from ouroboros.verification.extractor import AssertionExtractor
    from ouroboros.verification.verifier import SpecVerifier

    def fresh_llm_adapter(role: str = "reflect"):
        backend = role_llm_backend(role)
        return create_llm_adapter(
            backend=backend,
            max_turns=stage_max_turns,
            cwd=effective_cwd,
            allowed_tools=(
                [] if backend_supports_tool_envelope(resolve_llm_backend(backend)) else None
            ),
        )

    def fresh_reflect_stage_llm_adapter():
        return fresh_llm_adapter("reflect")

    wonder_engine = WonderEngine(
        llm_adapter=reflect_llm_adapter,
        adapter_factory=fresh_reflect_stage_llm_adapter,
        adapter_backend=reflect_llm_backend,
        adapter_backend_factory=lambda: role_llm_backend("wonder"),
    )
    reflect_engine = ReflectEngine(
        llm_adapter=reflect_llm_adapter,
        adapter_factory=fresh_reflect_stage_llm_adapter,
        adapter_backend=reflect_llm_backend,
        adapter_backend_factory=lambda: role_llm_backend("reflect"),
    )

    # Wire real execution/evaluation callables for evolve_step so that
    # generation quality is validated, not only ontology deltas.
    # Use Sonnet for execution (frugal) — Opus is overkill for code generation.
    execution_model = get_execution_model()
    if execution_model is None and execute_runtime_backend == "claude":
        execution_model = DEFAULT_SONNET_MODEL
    # Use stderr console: in MCP stdio mode, stdout is the JSON-RPC channel.
    # Any non-protocol output on stdout corrupts the MCP communication.
    # Stage 1 (mechanical checks: lint/build/test) can be enabled via env var.
    # Disabled by default to reduce latency per generation step.
    evolve_stage1 = os.environ.get("OUROBOROS_EVOLVE_STAGE1", "false").lower() == "true"
    evolution_eval_pipeline = EvaluationPipeline(
        llm_adapter=evaluation_llm_adapter,
        config=PipelineConfig(
            stage1_enabled=evolve_stage1,
            stage2_enabled=True,
            stage3_enabled=False,
            semantic=SemanticConfig(
                model=get_llm_model_for_role(
                    "semantic_evaluation",
                    backend=evaluate_llm_backend,
                )
            ),
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

    async def _evolution_executor(
        seed: Any,
        *,
        parallel: bool = True,
        execution_id: str | None = None,
        externally_satisfied_acs: dict[int, dict[str, Any]] | None = None,
    ) -> Any:
        await _ensure_evolution_store_initialized()
        task_cwd = evolutionary_loop.get_project_dir()
        runner_adapter = create_agent_runtime(
            backend=execute_runtime_backend,
            model=execution_model,
            cwd=task_cwd or effective_cwd,
            # Executor's internal LLM follows its own EXECUTE stage, not EVALUATE.
            llm_backend=execute_runtime_backend,
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
            execution_id=execution_id,
            parallel=parallel,
            externally_satisfied_acs=externally_satisfied_acs,
        )

    def _evaluate_mechanically(artifact: str, seed: Any) -> EvaluationSummary | None:
        """Parse legacy execution completion output without fabricating AC verdicts.

        The parallel executor emits worker task completion lines. Keep both the
        current ``### Task N: [COMPLETED/FAILED]`` syntax and legacy
        ``### AC N: [PASS/FAIL]`` syntax parseable, but map them to task
        completion results rather than formal ``ACResult`` verdicts.
        """
        return _parse_legacy_execution_task_summary(artifact, seed)

    spec_extractor = AssertionExtractor(
        llm_adapter=evaluation_llm_adapter,
        model=get_llm_model_for_role(
            "assertion_extraction",
            backend=role_llm_backend("assertion_extraction"),
        ),
    )

    def _extract_project_dir(artifact: str, seed: Any = None) -> str | None:
        """Resolve project directory from explicit config, seed context, or artifacts."""
        configured_project_dir = evolutionary_loop.get_project_dir()
        if configured_project_dir:
            return configured_project_dir

        seed_project_dir = _project_dir_from_seed(seed)
        if seed_project_dir:
            return seed_project_dir

        artifact_project_dir = _project_dir_from_artifact(artifact)
        if artifact_project_dir:
            return artifact_project_dir

        if _looks_like_project_root(effective_cwd):
            return str(effective_cwd)

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
        extract_result = await spec_extractor.extract(seed_id, ac_texts(seed_acs))
        if extract_result.is_err:
            log.warning("spec_verification.extraction_failed", error=str(extract_result.error))
            return None

        assertions = extract_result.value
        if not assertions:
            return None

        # Build agent results map from formal AC results or legacy task completion.
        agent_results = _agent_results_from_execution_summary(mechanical)

        # Run verification
        verifier = SpecVerifier(project_dir=project_dir)
        summary = verifier.verify_all(assertions, agent_results)

        if summary.has_discrepancies:
            log.warning(
                "spec_verification.discrepancies_found",
                count=summary.discrepancy_count,
                project_dir=project_dir,
            )

        return _evaluation_summary_from_spec_verification(mechanical, summary)

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
            current_ac = "\n".join(f"AC {i + 1}: {ac}" for i, ac in enumerate(ac_texts(acs)))
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
        validation_model = os.environ.get("OUROBOROS_VALIDATION_MODEL") or execution_model
        if validation_model is None and execute_runtime_backend == "claude":
            validation_model = DEFAULT_SONNET_MODEL
        validation_adapter = create_agent_runtime(
            backend=execute_runtime_backend,
            model=validation_model,
            cwd=project_dir,
            permission_mode="bypassPermissions",
            # Validation runs on the EXECUTE stage; align its internal LLM too.
            llm_backend=execute_runtime_backend,
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

    _scoped_reexecution_env = os.environ.get("OUROBOROS_SCOPED_REEXECUTION", "").strip().lower()
    _scoped_reexecution = _scoped_reexecution_env not in ("0", "false")
    from ouroboros.plugin.rewind import build_lockfile_rewind_observer

    evolutionary_loop = EvolutionaryLoop(
        event_store=event_store,
        config=EvolutionaryLoopConfig(
            runtime_controls=get_runtime_controls_config(),
            scoped_reexecution=_scoped_reexecution,
        ),
        wonder_engine=wonder_engine,
        reflect_engine=reflect_engine,
        seed_generator=seed_generator,
        executor=_evolution_executor,
        evaluator=_evolution_evaluator,
        validator=_evolution_validator,
        rewind_observer=build_lockfile_rewind_observer(event_store),
    )
    job_manager = JobManager(
        event_store,
        durable_jobs=durable_jobs,
        forced_inline_job_id=forced_inline_job_id,
    )
    session_signal_hub = SessionSignalHub(event_store=event_store)
    session_signal_target_resolver = EventStoreSessionSignalTargetResolver(
        event_store=event_store,
        capabilities_by_backend={
            default_execute_runtime.runtime_backend: (
                default_execute_runtime.capabilities.session_signals
            ),
            execute_runtime_backend: default_execute_runtime.capabilities.session_signals,
        },
    )

    # Create tool registry for dependency injection
    registry = ToolRegistry()

    # Create and register tool handlers with injected dependencies
    execute_seed = ExecuteSeedHandler(
        event_store=event_store,
        llm_adapter=evaluation_llm_adapter,
        agent_runtime_backend=execute_runtime_backend,
        opencode_mode=opencode_mode,
        llm_backend=evaluate_llm_backend,
        session_signal_hub=session_signal_hub,
    )
    synapse_signal = SynapseSignalHandler(
        SessionSignalMailbox(
            event_store=event_store,
            target_resolver=session_signal_target_resolver,
            delivery_queue=session_signal_hub,
        )
    )
    evolve_step = EvolveStepHandler(
        evolutionary_loop=evolutionary_loop,
        event_store=event_store,
        agent_runtime_backend=execute_runtime_backend,
        opencode_mode=opencode_mode,
    )
    auto_mcp_manager = mcp_bridge.manager if mcp_bridge is not None else None
    auto_mcp_prefix = (
        mcp_bridge.tool_prefix
        if mcp_bridge is not None and hasattr(mcp_bridge, "tool_prefix")
        else ""
    )
    start_execute_seed = StartExecuteSeedHandler(
        execute_handler=execute_seed,
        event_store=event_store,
        job_manager=job_manager,
        agent_runtime_backend=execute_runtime_backend,
        opencode_mode=opencode_mode,
    )

    def build_ralph_handler(
        runtime_backend: str | None,
        ralph_opencode_mode: str | None,
    ) -> RalphHandler:
        return RalphHandler(
            evolve_handler=evolve_step,
            event_store=event_store,
            job_manager=job_manager,
            agent_runtime_backend=runtime_backend,
            opencode_mode=ralph_opencode_mode,
        )

    ralph_handler = build_ralph_handler(execute_runtime_backend, opencode_mode)
    start_ralph_handler = StartRalphHandler(
        evolve_handler=evolve_step,
        event_store=event_store,
        job_manager=job_manager,
        agent_runtime_backend=execute_runtime_backend,
        opencode_mode=opencode_mode,
    )
    # ONE registry, shared by every producer and by the re-entry tool. A fan-out
    # registered by the interview handler is redeemed through
    # ``ouroboros_submit_fanout_results``, so both sides must observe the same
    # directory. Until #1754 this composition root injected no registry and
    # registered no submit handler, so on the shipped stdio server no
    # ``fanout_id`` was ever stamped and the re-entry contract in
    # skills/interview/SKILL.md named a tool that was not there.
    #
    # Built at its FINAL directory rather than at the default plus a later
    # re-root. This root resolved ``state_dir_path`` hundreds of lines above, so
    # the mutable path never had a reason to exist here — and leaving it mutable
    # is not free: a producer that registers before the first interview turn (a
    # lateral panel) would have its record moved out from under an already
    # issued fan-out id, whose valid submission then returns
    # ``unknown_fanout_id``.
    fanout_registry = FanoutRegistry(state_dir_path / "fanout")
    # No shared-adapter injection for interview handlers: the injected stage
    # adapter has no strict MCP isolation, and ``self.llm_adapter or ...``
    # would bypass the handler's own strict factory (#765, #1768). Injection
    # remains available for tests and custom wiring only.
    interview = InterviewHandler(
        event_store=event_store,
        llm_backend=interview_llm_backend,
        agent_runtime_backend=interview_runtime_backend,
        opencode_mode=opencode_mode,
        fanout_registry=fanout_registry,
        suppress_tool_use_prompt_cues=interview_envelope_sealed,
    )
    generate_seed = GenerateSeedHandler(
        event_store=event_store,
        llm_adapter=llm_adapter,
        llm_backend=interview_llm_backend,
        agent_runtime_backend=interview_runtime_backend,
        opencode_mode=opencode_mode,
    )
    conductor_action_tools = frozenset(
        {
            "ouroboros_record_conductor_decision",
            "ouroboros_start_execute_seed",
            "ouroboros_start_ralph",
        }
    )

    tool_handlers = [
        execute_seed,
        start_execute_seed,
        AutoHandler(
            interview_handler=interview,
            generate_seed_handler=generate_seed,
            start_execute_seed_handler=start_execute_seed,
            llm_backend=interview_llm_backend,
            agent_runtime_backend=reflect_runtime_backend,
            opencode_mode=opencode_mode,
            mcp_manager=auto_mcp_manager,
            mcp_tool_prefix=auto_mcp_prefix,
            event_store=event_store,
            ralph_handler_factory=build_ralph_handler,
        ),
        StartAutoHandler(
            interview_handler=interview,
            generate_seed_handler=generate_seed,
            start_execute_seed_handler=start_execute_seed,
            event_store=event_store,
            job_manager=job_manager,
            llm_backend=interview_llm_backend,
            agent_runtime_backend=reflect_runtime_backend,
            opencode_mode=opencode_mode,
            mcp_manager=auto_mcp_manager,
            mcp_tool_prefix=auto_mcp_prefix,
            ralph_handler_factory=build_ralph_handler,
        ),
        SessionStatusHandler(
            event_store=event_store,
        ),
        RecordConductorDecisionHandler(event_store=event_store),
        SynapseTargetsHandler(session_signal_target_resolver),
        synapse_signal,
        JobStatusHandler(
            event_store=event_store,
            job_manager=job_manager,
        ),
        JobWaitHandler(
            event_store=event_store,
            job_manager=job_manager,
            available_conductor_tools=conductor_action_tools,
        ),
        JobResultHandler(
            event_store=event_store,
            job_manager=job_manager,
            available_conductor_tools=conductor_action_tools,
        ),
        CancelJobHandler(
            event_store=event_store,
            job_manager=job_manager,
        ),
        QueryEventsHandler(
            event_store=event_store,
        ),
        ProjectionQueryHandler(
            event_store=event_store,
        ),
        GenerateSeedHandler(
            interview_engine=interview_engine,
            seed_generator=seed_generator,
            llm_adapter=llm_adapter,
            llm_backend=interview_llm_backend,
            event_store=event_store,
            agent_runtime_backend=interview_runtime_backend,
            opencode_mode=opencode_mode,
        ),
        MeasureDriftHandler(
            event_store=event_store,
        ),
        InterviewHandler(
            interview_engine=interview_engine,
            event_store=event_store,
            llm_backend=interview_llm_backend,
            agent_runtime_backend=interview_runtime_backend,
            opencode_mode=opencode_mode,
            fanout_registry=fanout_registry,
            suppress_tool_use_prompt_cues=interview_envelope_sealed,
        ),
        PMInterviewHandler(
            data_dir=state_dir_path,
            llm_backend=interview_llm_backend,
            event_store=event_store,
            agent_runtime_backend=interview_runtime_backend,
            opencode_mode=opencode_mode,
        ),
        BrownfieldHandler(_store=brownfield_store),
        EvaluateHandler(
            event_store=event_store,
            llm_backend=evaluate_llm_backend,
            agent_runtime_backend=evaluate_runtime_backend,
            opencode_mode=opencode_mode,
        ),
        StartEvaluateHandler(
            event_store=event_store,
            job_manager=job_manager,
            llm_backend=evaluate_llm_backend,
            agent_runtime_backend=evaluate_runtime_backend,
            opencode_mode=opencode_mode,
        ),
        LateralThinkHandler(
            agent_runtime_backend=reflect_runtime_backend,
            opencode_mode=opencode_mode,
            fanout_registry=fanout_registry,
        ),
        SubmitFanoutResultsHandler(fanout_registry=fanout_registry),
        evolve_step,
        StartEvolveStepHandler(
            evolve_handler=evolve_step,
            event_store=event_store,
            job_manager=job_manager,
            agent_runtime_backend=execute_runtime_backend,
            opencode_mode=opencode_mode,
        ),
        ralph_handler,
        start_ralph_handler,
        LineageStatusHandler(
            event_store=event_store,
        ),
        EvolveRewindHandler(
            evolutionary_loop=evolutionary_loop,
        ),
        ACDashboardHandler(
            event_store=event_store,
        ),
        ACTreeHUDHandler(
            event_store=event_store,
        ),
        QAHandler(
            llm_adapter=evaluation_llm_adapter,
            llm_backend=evaluate_llm_backend,
            event_store=event_store,
            agent_runtime_backend=evaluate_runtime_backend,
            opencode_mode=opencode_mode,
        ),
        CancelExecutionHandler(
            event_store=event_store,
        ),
    ]

    resource_handlers = [
        SeedsResourceHandler(),
        SessionsResourceHandler(event_store=event_store),
        EventsResourceHandler(event_store=event_store),
    ]

    # Create server adapter. The MCP ``instructions`` field is the cross-provider
    # "ubiquitous language" channel — default to the registry-rendered guidance so
    # every MCP client (Claude included) receives it at session start.
    from ouroboros.backends import render_mcp_server_instructions

    server = MCPServerAdapter(
        name=name,
        version=version,
        instructions=instructions if instructions is not None else render_mcp_server_instructions(),
        auth_config=auth_config,
        rate_limit_config=rate_limit_config,
    )

    # Build the AgentRuntimeContext that #474 funnels through every
    # handler. For now the context only exposes the EventStore, the
    # backend labels, the optional MCP bridge, and a fresh ControlBus
    # for #515. Subsequent migration slices move handler internals to
    # consume context.mcp_bridge directly instead of self.mcp_manager.
    control_bus = ControlBus()
    agent_runtime_context = AgentRuntimeContext(
        event_store=event_store,
        runtime_backend=resolved_runtime_backend,
        llm_backend=llm_backend,
        mcp_bridge=mcp_bridge,
        control=control_bus,
        synapse=session_signal_hub,
    )
    server.set_runtime_context(agent_runtime_context)
    server.set_job_manager(job_manager)

    # Close the reactive control surface before stores/bridges it may
    # reference from subscriber tasks.
    server.register_owned_resource(control_bus)
    server.register_owned_resource(event_store)
    if brownfield_store is not None:
        server.register_owned_resource(brownfield_store)

    # Inject the bridge from the runtime context into every
    # BridgeAwareMixin handler. ``inject_runtime_context`` is byte-
    # equivalent to the legacy ``inject_bridge`` for the same bridge —
    # the swap is purely about giving every handler a single funnel
    # (the context) instead of the per-handler ``mcp_manager`` plumbing
    # this PR series is replacing.
    if mcp_bridge is not None:
        from ouroboros.mcp.tools.bridge_mixin import inject_runtime_context

        injected = [
            type(h).__name__
            for h in tool_handlers
            if inject_runtime_context(h, agent_runtime_context)
        ]
        if injected:
            log.info("mcp.bridge.injected", handlers=injected)
        server.register_owned_resource(mcp_bridge)

    # Register all tools with the server
    for handler in tool_handlers:
        server.register_tool(handler)
        registry.register(handler, category="ouroboros")

    for handler in resource_handlers:
        server.register_resource(handler)

    log.info(
        "mcp.server.composition_root_complete",
        name=name,
        version=server.info.version,
        tools_registered=len(tool_handlers),
        resources_registered=len(resource_handlers),
        tool_names=[h.definition.name for h in tool_handlers],
    )

    return server
