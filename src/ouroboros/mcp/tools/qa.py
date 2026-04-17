"""QA Loop tool handler for ouroboros MCP server.

General-purpose quality assurance verdict for any artifact type.
Returns structured JSON verdict with score, dimensions, differences,
and actionable suggestions. Designed for iterative loop usage.

Inspired by oh-my-codex $visual-verdict by @Yeachan-Heo.
https://github.com/Yeachan-Heo/oh-my-codex/commit/6fd5471
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any
import uuid

import structlog

from ouroboros.config import get_qa_model
from ouroboros.core.types import Result
from ouroboros.evaluation.json_utils import extract_json_payload
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.tools.subagent import (
    build_qa_subagent,
    build_subagent_result,
    emit_subagent_dispatched_event,
    should_dispatch_via_plugin,
)
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.persistence.event_store import EventStore
from ouroboros.providers import create_llm_adapter
from ouroboros.providers.base import LLMAdapter

log = structlog.get_logger(__name__)

# Verdict thresholds
DEFAULT_PASS_THRESHOLD = 0.80
FAIL_THRESHOLD = 0.40

# JSON schema for QA verdict output
QA_VERDICT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "score": {"type": "number", "description": "Quality score 0.0-1.0"},
        "verdict": {
            "type": "string",
            "enum": ["pass", "revise", "fail"],
            "description": "Overall verdict",
        },
        "dimensions": {
            "type": "object",
            "description": "Per-dimension scores",
            "additionalProperties": {"type": "number"},
        },
        "differences": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific differences found",
        },
        "suggestions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Actionable improvement suggestions",
        },
        "reasoning": {"type": "string", "description": "Explanation of assessment"},
    },
    "required": ["score", "verdict", "dimensions", "differences", "suggestions", "reasoning"],
    "additionalProperties": False,
}

VALID_ARTIFACT_TYPES = ("code", "api_response", "document", "screenshot", "test_output", "custom")
VALID_VERDICTS = ("pass", "revise", "fail")


@dataclass(frozen=True, slots=True)
class QAVerdict:
    """Parsed QA verdict from LLM response."""

    score: float
    verdict: str
    dimensions: dict[str, float]
    differences: list[str]
    suggestions: list[str]
    reasoning: str


def _get_qa_system_prompt() -> str:
    """Lazy-load QA judge system prompt."""
    from ouroboros.agents.loader import load_agent_prompt

    return load_agent_prompt("qa-judge")


def _build_qa_user_prompt(
    artifact: str,
    artifact_type: str,
    quality_bar: str,
    reference: str | None = None,
    pass_threshold: float = DEFAULT_PASS_THRESHOLD,
    iteration_history: list[dict[str, Any]] | None = None,
    seed_content: str | None = None,
) -> str:
    """Build the user prompt for QA evaluation."""
    reference_section = ""
    if reference:
        reference_section = f"""
## Reference
```
{reference}
```
"""

    history_section = ""
    if iteration_history:
        history_lines = []
        for entry in iteration_history:
            history_lines.append(
                f"  - Iteration {entry.get('iteration', '?')}: "
                f"score={entry.get('score', '?')}, "
                f"verdict={entry.get('verdict', '?')}"
            )
        history_section = f"""
## Previous Iterations
{chr(10).join(history_lines)}
"""

    seed_section = ""
    if seed_content:
        seed_section = f"""
## Seed Specification
```yaml
{seed_content}
```
"""

    return f"""## Quality Bar
{quality_bar}

## Pass Threshold
{pass_threshold}

## Artifact Type
{artifact_type}

## Artifact Content
```
{artifact}
```
{reference_section}{history_section}{seed_section}
Provide your evaluation as a JSON object."""


def _unwrap_verdict_data(data: dict[str, Any]) -> dict[str, Any]:
    """Unwrap nested verdict objects.

    LLMs sometimes wrap the verdict in a key like ``{"qa_verdict": {...}}``.
    This function detects that pattern and returns the inner dict.
    """
    if "score" in data:
        return data
    # Check for single-key wrapper containing the score field
    for key in ("qa_verdict", "verdict", "result", "evaluation"):
        if key in data and isinstance(data[key], dict) and "score" in data[key]:
            return data[key]
    # Fallback: if there's exactly one dict-valued key containing 'score', use it
    dict_values = [(k, v) for k, v in data.items() if isinstance(v, dict) and "score" in v]
    if len(dict_values) == 1:
        return dict_values[0][1]
    return data


_LINE_DECORATION_RE = re.compile(r"^\s*(?:[-*]\s+|\d+[.)]\s+)")
_BOLD_RE = re.compile(r"\*{1,2}([^*]+)\*{1,2}")
_KV_RE = re.compile(r"(?im)^(\w[\w ]*?)[ \t]*[:=\-][ \t]*(.+)$")
_SCORE_FALLBACK_RE = re.compile(r"(?im)\bscore\b[ \t]*(?:is|-)[ \t]*([0-9]*\.?[0-9]+)")


def _strip_line_decorations(text: str) -> str:
    """Strip markdown/bullet formatting so downstream parsing stays simple."""
    lines = []
    for line in text.splitlines():
        line = _LINE_DECORATION_RE.sub("", line)
        line = _BOLD_RE.sub(r"\1", line)
        lines.append(line)
    return "\n".join(lines)


def _extract_kv_fields(text: str) -> dict[str, str]:
    """Extract ``Key: Value`` or ``Key = Value`` pairs from cleaned text."""
    fields: dict[str, str] = {}
    for m in _KV_RE.finditer(text):
        fields.setdefault(m.group(1).strip().lower(), m.group(2).strip())
    return fields


def _parse_non_json_qa_response(response_text: str) -> dict[str, Any] | None:
    """Parse QA verdicts from plain-text fallbacks.

    Some providers occasionally ignore structured-output constraints and return
    readable prose such as ``Score: 0.84`` / ``Verdict: pass`` instead of JSON.
    When that happens we still want the tool to degrade gracefully rather than
    failing the whole QA step.
    """
    cleaned = _strip_line_decorations(response_text)
    fields = _extract_kv_fields(cleaned)

    raw_score = fields.get("score")
    if raw_score:
        score_num = re.match(r"([0-9]*\.?[0-9]+)", raw_score)
    else:
        # Fallback: "score is 0.85" / "score - 0.85" (word-bounded, not in KV)
        score_num = _SCORE_FALLBACK_RE.search(cleaned)

    if not score_num:
        return None
    try:
        score = float(score_num.group(1))
    except ValueError:
        return None

    differences: list[str] = []
    suggestions: list[str] = []
    dimensions: dict[str, float] = {}

    # Track which section we're in based on headers
    current_section = "differences"
    _SECTION_RE = re.compile(r"(?i)^\s*#*\s*(suggestions?|differences?|dimensions?)\s*:?\s*$")
    _BULLET_RE = re.compile(r"(?m)^\s*(?:[-*]|\d+[.)])\s+(.+)$")
    _DIM_RE = re.compile(r"^(\w[\w ]*?)[:=\-]\s*([0-9]*\.?[0-9]+)\s*$")

    for line in response_text.splitlines():
        section_match = _SECTION_RE.match(line)
        if section_match:
            header = section_match.group(1).lower().rstrip("s")
            if header == "suggestion":
                current_section = "suggestions"
            elif header == "dimension":
                current_section = "dimensions"
            else:
                current_section = "differences"
            continue

        bullet_match = _BULLET_RE.match(line)
        if bullet_match:
            item = bullet_match.group(1).strip()
            # Check if this bullet is a dimension (e.g. "accuracy: 0.9")
            dim_match = _DIM_RE.match(item)
            if dim_match and current_section == "dimensions":
                dimensions[dim_match.group(1).strip().lower()] = float(dim_match.group(2))
            elif current_section == "suggestions":
                suggestions.append(item)
            else:
                differences.append(item)

    return {
        "score": score,
        "verdict": fields.get("verdict", ""),
        "dimensions": dimensions,
        "differences": differences,
        "suggestions": suggestions,
        "reasoning": fields.get("reasoning", ""),
    }


def _parse_qa_response(
    response_text: str,
    pass_threshold: float = DEFAULT_PASS_THRESHOLD,
) -> Result[QAVerdict, str]:
    """Parse LLM response into QAVerdict.

    Returns:
        Result containing QAVerdict or error string.
    """
    json_str = extract_json_payload(response_text)
    if json_str:
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            return Result.err(f"Invalid JSON in QA response: {e}")
    else:
        data = _parse_non_json_qa_response(response_text)
        if data is None:
            return Result.err("Could not find JSON in QA response")

    # Unwrap nested verdict objects (e.g. {"qa_verdict": {...}})
    data = _unwrap_verdict_data(data)

    # Validate required fields
    score = data.get("score")
    if not isinstance(score, (int, float)) or score < 0.0 or score > 1.0:
        return Result.err(f"score must be a float between 0.0 and 1.0, got: {score}")

    verdict = data.get("verdict", "").lower().strip()
    if verdict not in VALID_VERDICTS:
        # Derive verdict from score if LLM didn't produce a valid one
        if score >= pass_threshold:
            verdict = "pass"
        elif score >= FAIL_THRESHOLD:
            verdict = "revise"
        else:
            verdict = "fail"

    dimensions = data.get("dimensions", {})
    if not isinstance(dimensions, dict):
        dimensions = {}

    differences = data.get("differences", [])
    if not isinstance(differences, list):
        differences = []
    differences = [str(d).strip() for d in differences if str(d).strip()]

    suggestions = data.get("suggestions", [])
    if not isinstance(suggestions, list):
        suggestions = []
    suggestions = [str(s).strip() for s in suggestions if str(s).strip()]

    reasoning = str(data.get("reasoning", "")).strip()

    return Result.ok(
        QAVerdict(
            score=float(score),
            verdict=verdict,
            dimensions={k: float(v) for k, v in dimensions.items() if isinstance(v, (int, float))},
            differences=differences,
            suggestions=suggestions,
            reasoning=reasoning,
        )
    )


def _determine_loop_action(verdict: QAVerdict, pass_threshold: float) -> str:
    """Determine loop action based on verdict."""
    if verdict.score >= pass_threshold:
        return "done"
    if verdict.score >= FAIL_THRESHOLD:
        return "continue"
    return "escalate"


def _format_verdict_text(
    verdict: QAVerdict,
    pass_threshold: float,
    loop_action: str,
    iteration: int,
    qa_session_id: str,
) -> str:
    """Format verdict as human-readable text."""
    status_label = verdict.verdict.upper()
    lines = [
        f"QA Verdict [Iteration {iteration}]",
        "=" * 60,
        f"Session: {qa_session_id}",
        f"Score: {verdict.score:.2f} / 1.00 [{status_label}]",
        f"Verdict: {verdict.verdict}",
        f"Threshold: {pass_threshold:.2f}",
        "",
    ]

    if verdict.dimensions:
        lines.append("Dimensions:")
        for dim_name, dim_score in verdict.dimensions.items():
            label = dim_name.replace("_", " ").title()
            lines.append(f"  {label:20s} {dim_score:.2f}")
        lines.append("")

    if verdict.differences:
        lines.append("Differences:")
        for diff in verdict.differences:
            lines.append(f"  - {diff}")
        lines.append("")

    if verdict.suggestions:
        lines.append("Suggestions:")
        for sug in verdict.suggestions:
            lines.append(f"  - {sug}")
        lines.append("")

    if verdict.reasoning:
        lines.append(f"Reasoning: {verdict.reasoning}")
        lines.append("")

    lines.append(f"Loop Action: {loop_action}")

    return "\n".join(lines)


@dataclass
class QAHandler:
    """Handler for the ouroboros_qa tool.

    Performs general-purpose QA verdict on any artifact type.
    Supports iterative loop until pass or max_iterations reached.
    """

    llm_adapter: LLMAdapter | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)
    event_store: EventStore | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_qa",
            description=(
                "General-purpose QA verdict for any artifact type. "
                "Evaluates code, API responses, documents, screenshots, or custom artifacts "
                "against a quality bar. Returns structured verdict with score, differences, "
                "and actionable suggestions. Designed for iterative loop usage."
            ),
            parameters=(
                MCPToolParameter(
                    name="artifact",
                    type=ToolInputType.STRING,
                    description="The artifact content to evaluate (code, text, JSON, etc.)",
                    required=True,
                ),
                MCPToolParameter(
                    name="quality_bar",
                    type=ToolInputType.STRING,
                    description=(
                        "Natural language description of what 'pass' means. "
                        "E.g., 'All public functions must have type hints and docstrings.'"
                    ),
                    required=True,
                ),
                MCPToolParameter(
                    name="artifact_type",
                    type=ToolInputType.STRING,
                    description=(
                        "Type of artifact: code, api_response, document, "
                        "screenshot, test_output, custom. Default: code"
                    ),
                    required=False,
                    default="code",
                    enum=VALID_ARTIFACT_TYPES,
                ),
                MCPToolParameter(
                    name="reference",
                    type=ToolInputType.STRING,
                    description=(
                        "Optional reference artifact for comparison "
                        "(expected output, target schema, reference description)."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="pass_threshold",
                    type=ToolInputType.NUMBER,
                    description="Score threshold for pass verdict (0.0-1.0). Default: 0.80",
                    required=False,
                    default=DEFAULT_PASS_THRESHOLD,
                ),
                MCPToolParameter(
                    name="qa_session_id",
                    type=ToolInputType.STRING,
                    description=(
                        "QA session ID for multi-iteration tracking. "
                        "If omitted, a new session is created."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="iteration_history",
                    type=ToolInputType.ARRAY,
                    description="Previous iteration results for loop context (JSON array).",
                    required=False,
                ),
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description="Optional seed YAML for additional context (goal, constraints).",
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a QA verdict request."""
        artifact = arguments.get("artifact")
        if not artifact:
            return Result.err(
                MCPToolError(
                    "artifact is required",
                    tool_name="ouroboros_qa",
                )
            )

        quality_bar = arguments.get("quality_bar")
        if not quality_bar:
            return Result.err(
                MCPToolError(
                    "quality_bar is required",
                    tool_name="ouroboros_qa",
                )
            )

        artifact_type = arguments.get("artifact_type", "code")
        reference = arguments.get("reference")
        pass_threshold = float(arguments.get("pass_threshold", DEFAULT_PASS_THRESHOLD))
        qa_session_id = arguments.get("qa_session_id") or f"qa-{uuid.uuid4().hex[:8]}"
        iteration_history = arguments.get("iteration_history") or []
        seed_content = arguments.get("seed_content")

        iteration = len(iteration_history) + 1

        log.info(
            "mcp.tool.qa",
            qa_session_id=qa_session_id,
            artifact_type=artifact_type,
            iteration=iteration,
            pass_threshold=pass_threshold,
        )

        # --- Subagent dispatch: gate on runtime + opencode_mode ---
        payload = build_qa_subagent(
            artifact=artifact,
            quality_bar=quality_bar,
            artifact_type=artifact_type,
            reference=reference,
            pass_threshold=pass_threshold,
            qa_session_id=qa_session_id,
            iteration_history=iteration_history,
            seed_content=seed_content,
        )
        if should_dispatch_via_plugin(self.agent_runtime_backend, self.opencode_mode):
            await emit_subagent_dispatched_event(
                self.event_store,
                session_id=qa_session_id,
                payload=payload,
            )
            return build_subagent_result(
                payload,
                response_shape={
                    "qa_session_id": qa_session_id,
                    "artifact_type": artifact_type,
                    "status": "delegated_to_subagent",
                    "dispatch_mode": "plugin",
                },
            )

        # Fall-through: real in-process QA LLM call (subprocess / non-opencode runtimes).

        try:
            from ouroboros.providers.base import CompletionConfig, Message, MessageRole

            system_prompt = _get_qa_system_prompt()
            user_prompt = _build_qa_user_prompt(
                artifact=artifact,
                artifact_type=artifact_type,
                quality_bar=quality_bar,
                reference=reference,
                pass_threshold=pass_threshold,
                iteration_history=iteration_history,
                seed_content=seed_content,
            )

            messages = [
                Message(role=MessageRole.SYSTEM, content=system_prompt),
                Message(role=MessageRole.USER, content=user_prompt),
            ]

            llm_adapter = self.llm_adapter or create_llm_adapter(
                backend=self.llm_backend,
                max_turns=1,
            )
            config = CompletionConfig(
                model=get_qa_model(self.llm_backend),
                temperature=0.2,
                max_tokens=2048,
                response_format={"type": "json_schema", "json_schema": QA_VERDICT_SCHEMA},
            )

            llm_result = await llm_adapter.complete(messages, config)
            if llm_result.is_err:
                return Result.err(
                    MCPToolError(
                        f"LLM call failed: {llm_result.error}",
                        tool_name="ouroboros_qa",
                    )
                )

            response = llm_result.value
            parse_result = _parse_qa_response(response.content, pass_threshold)

            if parse_result.is_err:
                return Result.err(
                    MCPToolError(
                        f"Failed to parse QA verdict: {parse_result.error}",
                        tool_name="ouroboros_qa",
                    )
                )

            verdict = parse_result.value
            loop_action = _determine_loop_action(verdict, pass_threshold)
            result_text = _format_verdict_text(
                verdict, pass_threshold, loop_action, iteration, qa_session_id
            )

            # Build iteration entry for history tracking
            iteration_entry = {
                "iteration": iteration,
                "score": verdict.score,
                "verdict": verdict.verdict,
                "loop_action": loop_action,
            }

            meta = {
                "qa_session_id": qa_session_id,
                "iteration": iteration,
                "score": verdict.score,
                "verdict": verdict.verdict,
                "loop_action": loop_action,
                "pass_threshold": pass_threshold,
                "passed": verdict.score >= pass_threshold,
                "dimensions": verdict.dimensions,
                "differences": verdict.differences,
                "suggestions": verdict.suggestions,
                "reasoning": verdict.reasoning,
                "iteration_entry": iteration_entry,
            }

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=result_text),),
                    is_error=False,
                    meta=meta,
                )
            )

        except (ValueError, RuntimeError) as e:
            # Configuration/bootstrap errors (unsupported backend, missing
            # provider install) — actionable by the user, safe to surface.
            log.warning("mcp.tool.qa.config_error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"QA setup failed: {e}",
                    tool_name="ouroboros_qa",
                )
            )
        except Exception:
            log.exception("mcp.tool.qa.error")
            return Result.err(
                MCPToolError(
                    "QA evaluation failed due to an internal error. Check server logs for details.",
                    tool_name="ouroboros_qa",
                )
            )
