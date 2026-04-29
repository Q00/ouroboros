"""AC decomposition for hierarchical task breakdown.

Decomposes non-atomic Acceptance Criteria into smaller, manageable child ACs.
Uses LLM to intelligently break down complex tasks based on:
- Insights from the Discover phase
- Parent AC context
- Domain-specific decomposition strategies

The decomposition follows these rules:
- Each decomposition produces 2-5 child ACs
- Max depth is 5 levels (NFR10)
- Context is compressed at depth 3+
- Cyclic decomposition is prevented

Usage:
    from ouroboros.execution.decomposition import decompose_ac

    result = await decompose_ac(
        ac_content="Implement user authentication system",
        ac_id="ac_123",
        execution_id="exec_456",
        depth=0,
        llm_adapter=adapter,
        discover_insights="User needs login, registration, password reset...",
    )

    if result.is_ok:
        for child_ac in result.value.child_acs:
            print(f"Child AC: {child_ac}")
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ouroboros.config import get_decomposition_model
from ouroboros.core.ac_tree import ACNode, ACStatus, ACTree
from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.events.decomposition import (
    create_ac_decomposed_event,
    create_ac_decomposition_failed_event,
)
from ouroboros.observability.logging import get_logger
from ouroboros.rlm.contracts import (
    RLM_HERMES_DECOMPOSE_AC_MODE,
    RLMHermesACDecompositionArtifact,
    RLMHermesACDecompositionResult,
    RLMHermesACSubQuestion,
    RLMHermesContractError,
    RLMHermesControl,
    RLMHermesResidualGap,
)
from ouroboros.rlm.trace import RLMHermesTraceRecord, hash_trace_text

if TYPE_CHECKING:
    from ouroboros.orchestrator.adapter import AgentRuntime
    from ouroboros.providers.base import LLMAdapter

log = get_logger(__name__)


# Decomposition constraints
MIN_CHILDREN = 2
MAX_CHILDREN = 5
MAX_DEPTH = 5
COMPRESSION_DEPTH = 3
HERMES_CHILD_AC_SOURCE = "rlm.hermes.decomposition"


def _new_hermes_decomposition_subcall_id() -> str:
    """Return a unique ID for one Hermes decomposition boundary call."""
    return f"rlm_subcall_{uuid4().hex}"


def _trace_id_for_hermes_decomposition_subcall(
    call_id: str | None,
    subcall_id: str | None,
) -> str | None:
    """Return the trace-record ID used to link child AC nodes to this sub-call."""
    trace_source = call_id or subcall_id
    return f"rlm_trace_{trace_source}" if trace_source else None


def _subcall_id_from_task_result(task_result: Any) -> str | None:
    """Extract a propagated Hermes adapter sub-call ID when available."""
    for message in reversed(getattr(task_result, "messages", ()) or ()):
        data = getattr(message, "data", None)
        if not isinstance(data, dict):
            continue
        value = data.get("subcall_id")
        if isinstance(value, str) and value.strip():
            return value
    return None


@dataclass(frozen=True, slots=True)
class HermesDecompositionSubcall:
    """Hermes inner-LM result used to guide AC decomposition."""

    prompt: str = ""
    completion: str = ""
    parent_call_id: str | None = None
    depth: int = 0
    exit_code: int = 0
    call_id: str | None = None
    subcall_id: str | None = None
    structured_result: RLMHermesACDecompositionResult | None = None
    rlm_node_id: str | None = None
    ac_node_id: str | None = None

    def to_trace_record(self) -> RLMHermesTraceRecord:
        """Return the replayable Hermes trace record for this decomposition call."""
        return RLMHermesTraceRecord(
            prompt=self.prompt,
            completion=self.completion,
            parent_call_id=self.parent_call_id,
            depth=self.depth,
            trace_id=_trace_id_for_hermes_decomposition_subcall(
                self.call_id,
                self.subcall_id,
            ),
            subcall_id=self.subcall_id,
            call_id=self.call_id,
            mode=RLM_HERMES_DECOMPOSE_AC_MODE,
            rlm_node_id=self.rlm_node_id,
            ac_node_id=self.ac_node_id,
            prompt_hash=hash_trace_text(self.prompt),
            response_hash=hash_trace_text(self.completion),
            success=self.exit_code == 0,
            exit_code=self.exit_code,
        )


@dataclass(frozen=True, slots=True)
class CanonicalChildACNodeInput:
    """Canonical input used to materialize a child ``ACNode``."""

    id: str
    content: str
    depth: int
    parent_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    status: ACStatus = ACStatus.PENDING
    is_atomic: bool = False
    children_ids: tuple[str, ...] = field(default_factory=tuple)
    execution_id: str | None = None
    originating_subcall_trace_id: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            msg = "canonical child AC node input id must not be empty"
            raise ValueError(msg)
        if not self.content.strip():
            msg = "canonical child AC node input content must not be empty"
            raise ValueError(msg)
        if not self.parent_id.strip():
            msg = "canonical child AC node input parent_id must not be empty"
            raise ValueError(msg)
        if self.depth < 0 or self.depth > MAX_DEPTH:
            msg = f"canonical child AC node input depth must be between 0 and {MAX_DEPTH}"
            raise ValueError(msg)
        if self.originating_subcall_trace_id is not None:
            if not isinstance(self.originating_subcall_trace_id, str):
                msg = "canonical child AC node input originating_subcall_trace_id must be a string"
                raise TypeError(msg)
            if not self.originating_subcall_trace_id.strip():
                msg = "canonical child AC node input originating_subcall_trace_id must not be empty"
                raise ValueError(msg)

        status = self.status if isinstance(self.status, ACStatus) else ACStatus(self.status)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "metadata", dict(self.metadata))
        object.__setattr__(self, "children_ids", tuple(self.children_ids))

    def to_ac_node(self) -> ACNode:
        """Materialize this canonical input as an immutable AC tree node."""
        return ACNode(
            id=self.id,
            content=self.content,
            depth=self.depth,
            parent_id=self.parent_id,
            status=self.status,
            is_atomic=self.is_atomic,
            children_ids=self.children_ids,
            execution_id=self.execution_id,
            originating_subcall_trace_id=self.originating_subcall_trace_id,
            metadata=dict(self.metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize this input using the same shape as ``ACTree`` nodes."""
        return _serialize_child_ac_node(self.to_ac_node())


@dataclass(frozen=True, slots=True)
class DecompositionResult:
    """Result of AC decomposition.

    Attributes:
        parent_ac_id: ID of the parent AC that was decomposed.
        child_acs: Tuple of child AC content strings.
        child_ac_ids: Tuple of generated child AC IDs.
        reasoning: LLM explanation of decomposition strategy.
        events: Events emitted during decomposition.
        dependencies: Tuple of dependency tuples. Each tuple contains indices of
            sibling ACs that must complete before this AC can start.
            Example: ((),(0,),(0,1)) means:
            - Child 0: no dependencies
            - Child 1: depends on child 0
            - Child 2: depends on child 0 and 1
        hermes_subcall: Optional Hermes inner-LM guidance used by RLM
            decomposition.
        hermes_subquestion_results: Structured Hermes sub-question records
            attached to generated child AC IDs for persisted trace replay.
        child_ac_node_inputs: Canonical inputs used to materialize child AC
            tree nodes.
        child_ac_nodes: Materialized child AC tree nodes created from the
            accepted decomposition result.
        persisted_child_ac_nodes: New Hermes-derived child AC nodes committed to
            the supplied AC tree or persisted decomposition event.
    """

    parent_ac_id: str
    child_acs: tuple[str, ...]
    child_ac_ids: tuple[str, ...]
    reasoning: str
    events: list[BaseEvent] = field(default_factory=list)
    dependencies: tuple[tuple[int, ...], ...] = field(default_factory=tuple)
    hermes_subcall: HermesDecompositionSubcall | None = None
    hermes_subquestion_results: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    child_ac_node_inputs: tuple[CanonicalChildACNodeInput, ...] = field(default_factory=tuple)
    child_ac_nodes: tuple[ACNode, ...] = field(default_factory=tuple)
    persisted_child_ac_nodes: tuple[ACNode, ...] = field(default_factory=tuple)


class DecompositionError(ValidationError):
    """Error during AC decomposition.

    Extends ValidationError with decomposition-specific context.
    """

    def __init__(
        self,
        message: str,
        *,
        ac_id: str | None = None,
        depth: int | None = None,
        error_type: str = "decomposition_error",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, field=error_type, value=ac_id, details=details)
        self.ac_id = ac_id
        self.depth = depth
        self.error_type = error_type


# LLM prompts for decomposition
DECOMPOSITION_SYSTEM_PROMPT = """You are an expert at breaking down complex acceptance criteria into smaller, actionable tasks.

When decomposing an AC, follow these principles:
1. MECE (Mutually Exclusive, Collectively Exhaustive) - children should not overlap and should cover the full scope
2. Each child should be simpler than the parent
3. Each child should be independently executable when dependencies are met
4. Use consistent granularity across children
5. Maintain clear boundaries between children
6. Identify dependencies between children - which tasks must complete before others can start

Produce 2-5 child ACs. Each should be:
- Specific and actionable
- Independently verifiable
- Clear about its scope
- Explicit about dependencies on sibling tasks (if any)"""

DECOMPOSITION_USER_TEMPLATE = """Parent Acceptance Criterion:
{ac_content}

Insights from Discovery Phase:
{discover_insights}

Current Depth: {depth} / {max_depth}

Decompose this AC into 2-5 smaller, focused child ACs.
For each child, identify which other children (by zero-based index) must complete before it can start.

Respond with a JSON object:
{{
    "children": [
        {{"content": "Child AC 1: specific, actionable description", "depends_on": []}},
        {{"content": "Child AC 2: depends on child 1", "depends_on": [0]}},
        {{"content": "Child AC 3: independent task", "depends_on": []}}
    ],
    "reasoning": "Brief explanation of your decomposition strategy and why certain tasks depend on others"
}}

Dependencies use zero-based indices. An empty array [] means no dependencies (can run in parallel with others).
Only respond with the JSON, no other text."""

HERMES_DECOMPOSITION_SYSTEM_PROMPT = """You are the inner language model in a dual-layer recursive execution loop.

Analyze the parent acceptance criterion and return concise decomposition guidance for Ouroboros.
Do not invoke Ouroboros, do not run any ooo command, and do not delegate recursively.
Return a single JSON object that follows the requested output contract."""

HERMES_DECOMPOSITION_USER_TEMPLATE = """Structured Hermes AC decomposition sub-question.

Return exactly one JSON object. Do not wrap it in Markdown.

Required output contract:
{{
  "schema_version": "rlm.hermes.output.v1",
  "mode": "decompose_ac",
  "rlm_node_id": "{rlm_node_id}",
  "ac_node_id": "{ac_id}",
  "verdict": "atomic | decomposed | partial | retryable | failed",
  "confidence": 0.0,
  "result": {{"summary": "Concise local decomposition result."}},
  "evidence_references": [
    {{
      "chunk_id": "optional supplied context id",
      "source_path": null,
      "start_line": null,
      "end_line": null,
      "claim": "Grounded local claim."
    }}
  ],
  "residual_gaps": [
    {{
      "gap": "Missing or uncertain fact.",
      "impact": "Why it matters locally.",
      "suggested_next_step": "Bounded follow-up Ouroboros may schedule."
    }}
  ],
  "artifacts": [
    {{
      "artifact_type": "decomposition",
      "is_atomic": false,
      "atomic_rationale": null,
      "proposed_child_acs": [
        {{
          "title": "Child AC title",
          "statement": "Child AC acceptance criterion statement",
          "success_criteria": ["Concrete verification criterion"],
          "rationale": "Why this child belongs in the decomposition",
          "depends_on": [],
          "estimated_chunk_needs": ["Optional context need"]
        }}
      ]
    }}
  ],
  "control": {{
    "requires_retry": false,
    "suggested_next_mode": "none",
    "must_not_recurse": false
  }}
}}

RLM Call Context:
- subcall_id: {subcall_id}
- call_id: {call_id}
- parent_call_id: {parent_call_id}
- depth: {depth}

For an atomic result, use verdict "atomic", set artifacts[0].is_atomic to true,
provide artifacts[0].atomic_rationale, and leave proposed_child_acs empty.
For a non-atomic result, provide 2-5 proposed_child_acs. depends_on entries may
only reference prior sibling indices.

Parent Acceptance Criterion:
{ac_content}

Discovery Insights:
{discover_insights}

Current Depth: {depth} / {max_depth}

Provide concise guidance for decomposing this AC:
- likely child acceptance criteria
- dependency boundaries
- risks that should stay visible to the parent decomposition step"""


def _build_hermes_decomposition_prompt(
    ac_content: str,
    discover_insights: str,
    depth: int,
    *,
    ac_id: str,
    rlm_node_id: str,
    call_id: str,
    subcall_id: str,
    parent_call_id: str | None,
) -> str:
    """Build the prompt for the Hermes inner-LM decomposition sub-call."""
    return HERMES_DECOMPOSITION_USER_TEMPLATE.format(
        ac_id=ac_id,
        ac_content=ac_content,
        discover_insights=discover_insights or "No specific insights available.",
        depth=depth,
        max_depth=MAX_DEPTH,
        rlm_node_id=rlm_node_id,
        call_id=call_id,
        subcall_id=subcall_id,
        parent_call_id=parent_call_id,
    )


def _merge_hermes_guidance(
    discover_insights: str,
    hermes_subcall: HermesDecompositionSubcall,
) -> str:
    """Append Hermes decomposition guidance to discovery insights."""
    base = discover_insights or "No specific insights available."
    normalized = (
        hermes_subcall.structured_result.to_json()
        if hermes_subcall.structured_result is not None
        else "{}"
    )
    return (
        f"{base}\n\nHermes decomposition sub-call guidance:\n{hermes_subcall.completion}"
        f"\n\nHermes normalized structured result:\n{normalized}"
    )


async def _run_hermes_decomposition_subcall(
    *,
    ac_content: str,
    ac_id: str,
    discover_insights: str,
    depth: int,
    hermes_runtime: AgentRuntime,
    parent_call_id: str | None,
    rlm_node_id: str | None,
    call_id: str | None = None,
) -> Result[HermesDecompositionSubcall, ProviderError]:
    """Ask Hermes for inner-LM decomposition guidance."""
    active_rlm_node_id = rlm_node_id or parent_call_id or f"rlm_decompose_{ac_id}"
    active_call_id = call_id or active_rlm_node_id
    active_subcall_id = _new_hermes_decomposition_subcall_id()
    prompt = _build_hermes_decomposition_prompt(
        ac_content,
        discover_insights,
        depth,
        ac_id=ac_id,
        rlm_node_id=active_rlm_node_id,
        call_id=active_call_id,
        subcall_id=active_subcall_id,
        parent_call_id=parent_call_id,
    )
    result = await hermes_runtime.execute_task_to_result(
        prompt=prompt,
        tools=[],
        system_prompt=HERMES_DECOMPOSITION_SYSTEM_PROMPT,
    )

    if result.is_err:
        error = ProviderError(
            "Hermes decomposition sub-call failed",
            provider="hermes",
            details={
                "ac_depth": depth,
                "parent_call_id": parent_call_id,
                "subcall_id": active_subcall_id,
                "cause": str(result.error),
            },
        )
        return Result.err(error)

    task_result = result.value
    if not task_result.success:
        error = ProviderError(
            "Hermes decomposition sub-call did not complete successfully",
            provider="hermes",
            details={
                "ac_depth": depth,
                "parent_call_id": parent_call_id,
                "rlm_node_id": active_rlm_node_id,
                "subcall_id": active_subcall_id,
                "completion_preview": task_result.final_message[:200],
            },
        )
        return Result.err(error)

    try:
        structured_result = _normalize_hermes_decomposition_response(
            task_result.final_message,
            expected_rlm_node_id=active_rlm_node_id,
            expected_ac_node_id=ac_id,
        )
    except RLMHermesContractError as exc:
        log.warning(
            "decomposition.hermes_structured_contract_invalid",
            ac_id=ac_id,
            depth=depth,
            expected_mode=RLM_HERMES_DECOMPOSE_AC_MODE,
            error=str(exc),
        )
        error = ProviderError(
            "Hermes decomposition response violated the RLM structured contract",
            provider="hermes",
            details={
                "ac_depth": depth,
                "parent_call_id": parent_call_id,
                "rlm_node_id": active_rlm_node_id,
                "subcall_id": active_subcall_id,
                "contract_error": str(exc),
            },
        )
        return Result.err(error)

    return Result.ok(
        HermesDecompositionSubcall(
            prompt=prompt,
            completion=task_result.final_message,
            parent_call_id=parent_call_id,
            depth=depth,
            exit_code=0,
            call_id=active_call_id,
            subcall_id=_subcall_id_from_task_result(task_result) or active_subcall_id,
            structured_result=structured_result,
            rlm_node_id=active_rlm_node_id,
            ac_node_id=ac_id,
        )
    )


def _extract_json_from_response(response: str) -> dict[str, Any] | None:
    """Extract JSON from LLM response, handling various formats.

    Args:
        response: Raw LLM response text.

    Returns:
        Parsed JSON dict or None if parsing fails.
    """
    # Try direct parsing first
    try:
        result = json.loads(response.strip())
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Try to find JSON in markdown code blocks
    json_pattern = r"```(?:json)?\s*(.*?)```"
    matches = re.findall(json_pattern, response, re.DOTALL)
    for match in matches:
        try:
            result = json.loads(match.strip())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue

    # Try to find JSON-like content with array
    brace_pattern = r"\{[^{}]*\"children\"\s*:\s*\[[^\]]+\][^{}]*\}"
    matches = re.findall(brace_pattern, response, re.DOTALL)
    for match in matches:
        try:
            result = json.loads(match.strip())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            continue

    return None


def _parse_decomposition_children(
    children_data: object,
    *,
    ac_id: str,
) -> tuple[list[str], list[tuple[int, ...]]]:
    """Parse legacy children payloads into statements and dependencies."""
    if not isinstance(children_data, list):
        raise TypeError("children must be a list")

    children: list[str] = []
    dependencies: list[tuple[int, ...]] = []

    for i, child_item in enumerate(children_data):
        if isinstance(child_item, str):
            children.append(child_item)
            dependencies.append(())
            continue

        if not isinstance(child_item, dict):
            raise TypeError(f"Child {i} must be string or dict, got {type(child_item)}")

        content = (
            child_item.get("content")
            or child_item.get("statement")
            or child_item.get("title")
            or ""
        )
        if not content:
            raise ValueError(f"Child {i} has no content")
        children.append(str(content))

        deps = child_item.get("depends_on", [])
        if not isinstance(deps, list):
            deps = []

        valid_deps_list: list[int] = []
        invalid_deps: list[int] = []
        for dependency in deps:
            if not isinstance(dependency, int):
                continue
            if dependency < 0 or dependency >= i:
                invalid_deps.append(dependency)
            else:
                valid_deps_list.append(dependency)

        if invalid_deps:
            log.warning(
                "decomposition.invalid_dependencies_filtered",
                ac_id=ac_id,
                child_idx=i,
                original_deps=deps,
                invalid_deps=invalid_deps,
                valid_deps=valid_deps_list,
                reason="forward_or_self_reference",
            )

        dependencies.append(tuple(valid_deps_list))

    return children, dependencies


def _is_structured_hermes_payload(payload: dict[str, Any]) -> bool:
    """Return whether a JSON object appears to be an RLM Hermes contract."""
    return any(
        key in payload
        for key in ("schema_version", "mode", "rlm_node_id", "ac_node_id", "artifacts")
    )


def _short_title(statement: str, index: int) -> str:
    """Derive a compact child title from a legacy child statement."""
    first_line = statement.strip().splitlines()[0] if statement.strip() else ""
    if not first_line:
        return f"Child AC {index + 1}"
    return first_line[:80]


def _numeric_confidence(value: object, default: float) -> float:
    """Normalize an optional confidence value into the RLM contract range."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return min(1.0, max(0.0, float(value)))


def _normalize_legacy_decomposition_payload(
    payload: dict[str, Any],
    *,
    expected_rlm_node_id: str,
    expected_ac_node_id: str,
) -> RLMHermesACDecompositionResult:
    """Map a legacy children JSON object into the structured RLM schema."""
    reasoning = payload.get("reasoning")
    summary = str(reasoning) if reasoning else "Hermes returned legacy decomposition JSON."

    if payload.get("is_atomic") is True:
        return RLMHermesACDecompositionResult(
            rlm_node_id=expected_rlm_node_id,
            ac_node_id=expected_ac_node_id,
            verdict="atomic",
            confidence=_numeric_confidence(payload.get("confidence"), 0.5),
            result={"summary": summary},
            artifact=RLMHermesACDecompositionArtifact(
                is_atomic=True,
                atomic_rationale=summary,
            ),
        )

    children, dependencies = _parse_decomposition_children(
        payload.get("children", []),
        ac_id=expected_ac_node_id,
    )
    proposed_child_acs = tuple(
        RLMHermesACSubQuestion(
            title=_short_title(child, index),
            statement=child,
            success_criteria=("Child AC is independently verifiable.",),
            rationale=summary,
            depends_on=dependencies[index],
        )
        for index, child in enumerate(children)
    )
    return RLMHermesACDecompositionResult(
        rlm_node_id=expected_rlm_node_id,
        ac_node_id=expected_ac_node_id,
        verdict="decomposed" if len(proposed_child_acs) >= MIN_CHILDREN else "partial",
        confidence=_numeric_confidence(payload.get("confidence"), 0.5),
        result={"summary": summary},
        artifact=RLMHermesACDecompositionArtifact(
            is_atomic=False,
            proposed_child_acs=proposed_child_acs,
        ),
    )


def _normalize_unstructured_hermes_text(
    response: str,
    *,
    expected_rlm_node_id: str,
    expected_ac_node_id: str,
) -> RLMHermesACDecompositionResult:
    """Wrap plain Hermes text in a partial structured result for traceability."""
    summary = response.strip() or "Hermes returned an empty decomposition response."
    return RLMHermesACDecompositionResult(
        rlm_node_id=expected_rlm_node_id,
        ac_node_id=expected_ac_node_id,
        verdict="partial",
        confidence=0.0,
        result={"summary": summary},
        residual_gaps=(
            RLMHermesResidualGap(
                gap="Hermes did not return a structured decomposition object.",
                impact="Ouroboros cannot directly mutate the AC tree from this response.",
                suggested_next_step=(
                    "Use the legacy decomposition adapter as a fallback or retry the "
                    "Hermes sub-question with a stricter contract prompt."
                ),
            ),
        ),
        artifact=RLMHermesACDecompositionArtifact(is_atomic=False),
        control=RLMHermesControl(
            requires_retry=True,
            suggested_next_mode=RLM_HERMES_DECOMPOSE_AC_MODE,
        ),
    )


def _normalize_hermes_decomposition_response(
    response: str,
    *,
    expected_rlm_node_id: str,
    expected_ac_node_id: str,
) -> RLMHermesACDecompositionResult:
    """Normalize one Hermes decomposition response into the RLM result schema."""
    payload = _extract_json_from_response(response)
    if payload is None:
        return _normalize_unstructured_hermes_text(
            response,
            expected_rlm_node_id=expected_rlm_node_id,
            expected_ac_node_id=expected_ac_node_id,
        )

    if _is_structured_hermes_payload(payload):
        return RLMHermesACDecompositionResult.from_dict(
            payload,
            expected_rlm_node_id=expected_rlm_node_id,
            expected_ac_node_id=expected_ac_node_id,
        )

    if "children" in payload or payload.get("is_atomic") is True:
        return _normalize_legacy_decomposition_payload(
            payload,
            expected_rlm_node_id=expected_rlm_node_id,
            expected_ac_node_id=expected_ac_node_id,
        )

    return _normalize_unstructured_hermes_text(
        response,
        expected_rlm_node_id=expected_rlm_node_id,
        expected_ac_node_id=expected_ac_node_id,
    )


def _validate_children(
    children: list[str],
    parent_content: str,
    ac_id: str,
    depth: int,
) -> Result[None, DecompositionError]:
    """Validate decomposition children.

    Args:
        children: List of child AC contents.
        parent_content: Parent AC content for cycle detection.
        ac_id: Parent AC ID.
        depth: Current depth.

    Returns:
        Result with None on success or DecompositionError on failure.
    """
    # Check count
    if len(children) < MIN_CHILDREN:
        return Result.err(
            DecompositionError(
                f"Decomposition produced only {len(children)} children, minimum is {MIN_CHILDREN}",
                ac_id=ac_id,
                depth=depth,
                error_type="insufficient_children",
            )
        )

    if len(children) > MAX_CHILDREN:
        return Result.err(
            DecompositionError(
                f"Decomposition produced {len(children)} children, maximum is {MAX_CHILDREN}",
                ac_id=ac_id,
                depth=depth,
                error_type="too_many_children",
            )
        )

    # Check for cycles (child content identical to parent)
    parent_normalized = parent_content.strip().lower()
    for i, child in enumerate(children):
        child_normalized = child.strip().lower()
        if child_normalized == parent_normalized:
            return Result.err(
                DecompositionError(
                    f"Child {i + 1} is identical to parent (cyclic decomposition)",
                    ac_id=ac_id,
                    depth=depth,
                    error_type="cyclic_decomposition",
                )
            )

    # Check for empty children
    for i, child in enumerate(children):
        if not child.strip():
            return Result.err(
                DecompositionError(
                    f"Child {i + 1} is empty",
                    ac_id=ac_id,
                    depth=depth,
                    error_type="empty_child",
                )
            )

    return Result.ok(None)


def _accepted_hermes_subquestions(
    hermes_subcall: HermesDecompositionSubcall | None,
) -> tuple[RLMHermesACSubQuestion, ...]:
    """Return normalized Hermes child AC proposals accepted by Ouroboros."""
    if hermes_subcall is None or hermes_subcall.structured_result is None:
        return ()
    if hermes_subcall.structured_result.verdict != "decomposed":
        return ()
    if hermes_subcall.structured_result.control.requires_retry:
        return ()
    return hermes_subcall.structured_result.artifact.proposed_child_acs


def _stable_hermes_child_ac_id(
    *,
    parent_ac_id: str,
    child_index: int,
    subquestion: RLMHermesACSubQuestion,
    hermes_subcall: HermesDecompositionSubcall,
) -> str:
    """Derive a stable AC identifier for an accepted Hermes child proposal."""
    structured_result = hermes_subcall.structured_result
    digest_input = {
        "schema": "rlm.hermes.child_ac_id.v1",
        "parent_ac_id": parent_ac_id,
        "child_index": child_index,
        "rlm_node_id": structured_result.rlm_node_id if structured_result else None,
        "source_ac_node_id": structured_result.ac_node_id if structured_result else None,
        "title": subquestion.title,
        "statement": subquestion.statement,
        "success_criteria": list(subquestion.success_criteria),
    }
    payload = json.dumps(digest_input, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"ac_{digest[:12]}"


def _stable_child_ac_identity(
    *,
    parent_ac_id: str,
    child_content: str,
    subquestion: RLMHermesACSubQuestion | None,
) -> str:
    """Return a stable semantic identity for duplicate child AC detection."""
    statement = subquestion.statement if subquestion is not None else child_content
    normalized_statement = " ".join(statement.casefold().split())
    digest_input = {
        "schema": "ouroboros.child_ac_identity.v1",
        "parent_ac_id": parent_ac_id,
        "statement": normalized_statement,
    }
    payload = json.dumps(digest_input, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"child_ac_identity:{digest}"


def _child_ac_id_for_index(
    *,
    parent_ac_id: str,
    child_index: int,
    hermes_subcall: HermesDecompositionSubcall | None,
    subquestion: RLMHermesACSubQuestion | None,
) -> str:
    """Return a stable Hermes child ID or the legacy generated ID."""
    if hermes_subcall is not None and subquestion is not None:
        return _stable_hermes_child_ac_id(
            parent_ac_id=parent_ac_id,
            child_index=child_index,
            subquestion=subquestion,
            hermes_subcall=hermes_subcall,
        )
    return f"ac_{uuid4().hex[:12]}"


def _child_ac_metadata(
    *,
    ac_id: str,
    child_ac_id: str,
    execution_id: str,
    child_index: int,
    child_content: str,
    dependencies: tuple[int, ...],
    hermes_subcall: HermesDecompositionSubcall | None,
    subquestion: RLMHermesACSubQuestion | None,
) -> dict[str, Any]:
    """Build serializable child AC metadata from normalized Hermes fields."""
    metadata: dict[str, Any] = {
        "title": subquestion.title
        if subquestion is not None
        else _short_title(
            child_content,
            child_index,
        ),
        "statement": child_content,
        "success_criteria": list(subquestion.success_criteria)
        if subquestion is not None
        else ["Child AC is independently verifiable."],
        "rationale": subquestion.rationale if subquestion is not None else None,
        "depends_on": list(dependencies),
        "estimated_chunk_needs": list(subquestion.estimated_chunk_needs)
        if subquestion is not None
        else [],
        "child_index": child_index,
        "parent_ac_id": ac_id,
        "execution_id": execution_id,
        "stable_identity": _stable_child_ac_identity(
            parent_ac_id=ac_id,
            child_content=child_content,
            subquestion=subquestion,
        ),
        "stable_identity_schema": "ouroboros.child_ac_identity.v1",
        "source": HERMES_CHILD_AC_SOURCE if subquestion is not None else "legacy.decomposition",
    }

    if (
        subquestion is not None
        and hermes_subcall is not None
        and hermes_subcall.structured_result is not None
    ):
        structured_result = hermes_subcall.structured_result
        originating_subcall_trace_id = hermes_subcall.to_trace_record().trace_id
        child_call_id = f"rlm_call_decompose_{child_ac_id}"
        child_call_context = {
            "call_id": child_call_id,
            "parent_call_id": hermes_subcall.call_id,
            "parent_subcall_id": hermes_subcall.subcall_id,
            "depth": hermes_subcall.depth + 1,
        }
        metadata.update(
            {
                "rlm_node_id": structured_result.rlm_node_id,
                "source_ac_node_id": structured_result.ac_node_id,
                "hermes_verdict": structured_result.verdict,
                "hermes_confidence": structured_result.confidence,
                "hermes_result": dict(structured_result.result),
                "rlm_call_id": child_call_id,
                "rlm_parent_call_id": hermes_subcall.call_id,
                "rlm_parent_subcall_id": hermes_subcall.subcall_id,
                "rlm_call_depth": hermes_subcall.depth + 1,
                "rlm_child_call_context": child_call_context,
                "originating_subcall_trace_id": originating_subcall_trace_id,
            }
        )

    return metadata


def _validate_dependency_indices(
    dependencies: tuple[int, ...],
    *,
    child_index: int,
) -> None:
    """Ensure dependencies only point to earlier siblings."""
    for dependency in dependencies:
        if dependency < 0 or dependency >= child_index:
            msg = (
                "canonical child AC dependencies must reference prior sibling "
                f"indices; child {child_index} has invalid dependency {dependency}"
            )
            raise ValueError(msg)


def _normalize_child_ac_node_inputs(
    *,
    ac_id: str,
    execution_id: str,
    depth: int,
    child_acs: tuple[str, ...],
    child_ac_ids: tuple[str, ...],
    dependencies: tuple[tuple[int, ...], ...],
    hermes_subcall: HermesDecompositionSubcall | None,
) -> tuple[CanonicalChildACNodeInput, ...]:
    """Normalize child payloads into canonical AC tree node inputs.

    Accepted Hermes child proposals are the source of truth for statement,
    criteria, and dependency fields. Legacy LLM children use the same canonical
    node-input shape, but keep ``source=legacy.decomposition`` in metadata.
    """
    if len(child_ac_ids) != len(child_acs):
        msg = "canonical child AC node input IDs must match child AC count"
        raise ValueError(msg)

    subquestions = _accepted_hermes_subquestions(hermes_subcall)
    inputs: list[CanonicalChildACNodeInput] = []
    for index, (child_ac_id, child_content) in enumerate(zip(child_ac_ids, child_acs, strict=True)):
        subquestion = subquestions[index] if index < len(subquestions) else None
        normalized_content = subquestion.statement if subquestion is not None else child_content
        dependency_indices = (
            subquestion.depends_on
            if subquestion is not None
            else dependencies[index]
            if index < len(dependencies)
            else ()
        )
        _validate_dependency_indices(dependency_indices, child_index=index)
        originating_subcall_trace_id = (
            hermes_subcall.to_trace_record().trace_id
            if subquestion is not None and hermes_subcall is not None
            else None
        )

        inputs.append(
            CanonicalChildACNodeInput(
                id=child_ac_id,
                content=normalized_content,
                depth=depth + 1,
                parent_id=ac_id,
                status=ACStatus.PENDING,
                is_atomic=False,
                children_ids=(),
                execution_id=None,
                originating_subcall_trace_id=originating_subcall_trace_id,
                metadata=_child_ac_metadata(
                    ac_id=ac_id,
                    child_ac_id=child_ac_id,
                    execution_id=execution_id,
                    child_index=index,
                    child_content=normalized_content,
                    dependencies=dependency_indices,
                    hermes_subcall=hermes_subcall,
                    subquestion=subquestion,
                ),
            )
        )
    return tuple(inputs)


def _materialize_child_ac_nodes(
    child_ac_node_inputs: tuple[CanonicalChildACNodeInput, ...],
) -> tuple[ACNode, ...]:
    """Materialize canonical child AC node inputs into ``ACNode`` objects."""
    return tuple(node_input.to_ac_node() for node_input in child_ac_node_inputs)


def _create_child_ac_nodes(
    *,
    ac_id: str,
    execution_id: str,
    depth: int,
    child_acs: tuple[str, ...],
    child_ac_ids: tuple[str, ...],
    dependencies: tuple[tuple[int, ...], ...],
    hermes_subcall: HermesDecompositionSubcall | None,
) -> tuple[ACNode, ...]:
    """Materialize one child ``ACNode`` per accepted decomposition result."""
    return _materialize_child_ac_nodes(
        _normalize_child_ac_node_inputs(
            ac_id=ac_id,
            execution_id=execution_id,
            depth=depth,
            child_acs=child_acs,
            child_ac_ids=child_ac_ids,
            dependencies=dependencies,
            hermes_subcall=hermes_subcall,
        )
    )


def _serialize_child_ac_node(node: ACNode) -> dict[str, Any]:
    """Serialize a child AC node using the same field shape as ``ACTree``."""
    return {
        "id": node.id,
        "content": node.content,
        "depth": node.depth,
        "parent_id": node.parent_id,
        "status": node.status.value,
        "is_atomic": node.is_atomic,
        "children_ids": list(node.children_ids),
        "execution_id": node.execution_id,
        "originating_subcall_trace_id": node.originating_subcall_trace_id,
        "metadata": dict(node.metadata),
    }


def _normalized_child_identity(node: ACNode) -> tuple[str, str, str]:
    """Return a local identity key for de-duplicating candidate child nodes."""
    metadata = node.metadata if isinstance(node.metadata, dict) else {}
    stable_identity = metadata.get("stable_identity")
    if isinstance(stable_identity, str) and stable_identity.strip():
        return ("stable_identity", node.parent_id or "", stable_identity.strip())

    statement = metadata.get("statement")
    identity_text = statement if isinstance(statement, str) and statement.strip() else node.content
    normalized_text = " ".join(identity_text.casefold().split())
    return ("content", node.parent_id or "", normalized_text)


def _is_hermes_child_for_parent(node: ACNode, *, parent_ac_id: str) -> bool:
    """Return whether a child AC node came from accepted Hermes guidance."""
    metadata = node.metadata if isinstance(node.metadata, dict) else {}
    return node.parent_id == parent_ac_id and metadata.get("source") == HERMES_CHILD_AC_SOURCE


def _new_hermes_child_ac_nodes(
    *,
    parent_ac_id: str,
    child_ac_nodes: tuple[ACNode, ...],
    ac_tree: ACTree | None = None,
) -> tuple[ACNode, ...]:
    """Select only new Hermes-derived child AC nodes for one parent."""
    seen_ids: set[str] = set()
    seen_identities: set[tuple[str, str, str]] = set()
    accepted: list[ACNode] = []

    for node in child_ac_nodes:
        if not _is_hermes_child_for_parent(node, parent_ac_id=parent_ac_id):
            continue
        if node.id in seen_ids:
            continue

        identity = _normalized_child_identity(node)
        if identity in seen_identities:
            continue
        if ac_tree is not None:
            if node.id in ac_tree.nodes:
                continue
            if ac_tree.find_duplicate_child(node) is not None:
                continue

        accepted.append(node)
        seen_ids.add(node.id)
        seen_identities.add(identity)

    return tuple(accepted)


def persist_hermes_child_ac_nodes(
    ac_tree: ACTree,
    *,
    parent_ac_id: str,
    child_ac_nodes: tuple[ACNode, ...],
) -> tuple[ACNode, ...]:
    """Persist only new Hermes-derived child AC nodes under ``parent_ac_id``."""
    if parent_ac_id not in ac_tree.nodes:
        msg = f"Parent AC node {parent_ac_id} not found in AC tree"
        raise KeyError(msg)

    new_child_nodes = _new_hermes_child_ac_nodes(
        parent_ac_id=parent_ac_id,
        child_ac_nodes=child_ac_nodes,
        ac_tree=ac_tree,
    )
    for child_node in new_child_nodes:
        ac_tree.add_node(child_node)

    return new_child_nodes


def _build_hermes_subquestion_results(
    *,
    child_ac_ids: tuple[str, ...],
    hermes_subcall: HermesDecompositionSubcall | None,
) -> tuple[dict[str, Any], ...]:
    """Attach structured Hermes child proposals to generated AC IDs.

    Hermes proposes child sub-questions but never owns AC identifiers. The
    outer decomposition step generates AC IDs first, then records a replayable
    mapping from each generated child ID to the corresponding Hermes proposal.
    """
    if hermes_subcall is None or hermes_subcall.structured_result is None:
        return ()

    structured_result = hermes_subcall.structured_result
    subquestions = _accepted_hermes_subquestions(hermes_subcall)
    if not subquestions:
        return ()

    return tuple(
        {
            "artifact_type": "rlm.hermes.decomposition_subquestion_result",
            "schema_version": structured_result.schema_version,
            "mode": structured_result.mode,
            "child_index": index,
            "child_ac_id": child_ac_id,
            "parent_ac_id": structured_result.ac_node_id,
            "rlm_node_id": structured_result.rlm_node_id,
            "ac_node_id": structured_result.ac_node_id,
            "verdict": structured_result.verdict,
            "confidence": structured_result.confidence,
            "result": dict(structured_result.result),
            "subquestion": subquestion.to_dict(),
            "evidence_references": [
                reference.to_dict() for reference in structured_result.evidence_references
            ],
            "residual_gaps": [gap.to_dict() for gap in structured_result.residual_gaps],
            "control": structured_result.control.to_dict(),
            "hermes_call": hermes_subcall.to_trace_record().to_dict(),
        }
        for index, (child_ac_id, subquestion) in enumerate(
            zip(child_ac_ids, subquestions, strict=False)
        )
    )


def _compress_context(discover_insights: str, depth: int) -> str:
    """Compress discovery insights at depth 3+.

    Args:
        discover_insights: Original insights from Discover phase.
        depth: Current depth in AC tree.

    Returns:
        Compressed or original insights string.
    """
    if depth < COMPRESSION_DEPTH:
        return discover_insights

    # At depth 3+, only keep first 500 characters
    if len(discover_insights) > 500:
        compressed = discover_insights[:500] + "... [compressed for depth]"
        log.debug(
            "decomposition.context.compressed",
            original_length=len(discover_insights),
            compressed_length=len(compressed),
            depth=depth,
        )
        return compressed

    return discover_insights


def _create_decomposition_result(
    *,
    ac_content: str,
    ac_id: str,
    execution_id: str,
    depth: int,
    children: list[str],
    dependencies: list[tuple[int, ...]],
    reasoning: str,
    hermes_subcall: HermesDecompositionSubcall | None = None,
    ac_tree: ACTree | None = None,
) -> Result[DecompositionResult, DecompositionError]:
    """Create a validated DecompositionResult and completion event."""
    validation_result = _validate_children(children, ac_content, ac_id, depth)
    if validation_result.is_err:
        _failed_event = create_ac_decomposition_failed_event(
            ac_id=ac_id,
            execution_id=execution_id,
            error_message=str(validation_result.error),
            error_type=validation_result.error.error_type,
            depth=depth,
        )
        return Result.err(validation_result.error)

    child_acs = tuple(children)
    dependencies_tuple = tuple(dependencies)
    subquestions = _accepted_hermes_subquestions(hermes_subcall)
    child_ac_ids = tuple(
        _child_ac_id_for_index(
            parent_ac_id=ac_id,
            child_index=index,
            hermes_subcall=hermes_subcall,
            subquestion=subquestions[index] if index < len(subquestions) else None,
        )
        for index, _child in enumerate(child_acs)
    )
    try:
        child_ac_node_inputs = _normalize_child_ac_node_inputs(
            ac_id=ac_id,
            execution_id=execution_id,
            depth=depth,
            child_acs=child_acs,
            child_ac_ids=child_ac_ids,
            dependencies=dependencies_tuple,
            hermes_subcall=hermes_subcall,
        )
    except ValueError as exc:
        return Result.err(
            DecompositionError(
                f"Failed to normalize child AC node inputs: {exc}",
                ac_id=ac_id,
                depth=depth,
                error_type="invalid_child_ac_node_input",
            )
        )

    child_acs = tuple(node_input.content for node_input in child_ac_node_inputs)
    dependencies_tuple = tuple(
        tuple(node_input.metadata.get("depends_on", ())) for node_input in child_ac_node_inputs
    )
    child_ac_nodes = _materialize_child_ac_nodes(child_ac_node_inputs)
    try:
        persisted_child_ac_nodes = (
            persist_hermes_child_ac_nodes(
                ac_tree,
                parent_ac_id=ac_id,
                child_ac_nodes=child_ac_nodes,
            )
            if ac_tree is not None
            else _new_hermes_child_ac_nodes(
                parent_ac_id=ac_id,
                child_ac_nodes=child_ac_nodes,
            )
        )
    except (KeyError, ValueError) as exc:
        return Result.err(
            DecompositionError(
                f"Failed to persist Hermes child AC nodes: {exc}",
                ac_id=ac_id,
                depth=depth,
                error_type="ac_tree_persistence_failed",
            )
        )

    hermes_subquestion_results = _build_hermes_subquestion_results(
        child_ac_ids=child_ac_ids,
        hermes_subcall=hermes_subcall,
    )

    has_dependencies = any(deps for deps in dependencies_tuple)
    log.debug(
        "decomposition.dependencies_parsed",
        ac_id=ac_id,
        child_count=len(children),
        has_dependencies=has_dependencies,
        dependency_structure=[list(d) for d in dependencies_tuple],
    )

    decomposed_event = create_ac_decomposed_event(
        parent_ac_id=ac_id,
        execution_id=execution_id,
        child_ac_ids=list(child_ac_ids),
        child_contents=list(child_acs),
        depth=depth,
        reasoning=reasoning,
        child_ac_nodes=(
            [_serialize_child_ac_node(node) for node in persisted_child_ac_nodes]
            if persisted_child_ac_nodes
            else None
        ),
        hermes_subquestion_results=(
            [dict(item) for item in hermes_subquestion_results]
            if hermes_subquestion_results
            else None
        ),
    )

    result = DecompositionResult(
        parent_ac_id=ac_id,
        child_acs=child_acs,
        child_ac_ids=child_ac_ids,
        reasoning=reasoning,
        events=[decomposed_event],
        dependencies=dependencies_tuple,
        hermes_subcall=hermes_subcall,
        hermes_subquestion_results=hermes_subquestion_results,
        child_ac_node_inputs=child_ac_node_inputs,
        child_ac_nodes=child_ac_nodes,
        persisted_child_ac_nodes=persisted_child_ac_nodes,
    )

    log.info(
        "decomposition.completed",
        ac_id=ac_id,
        child_count=len(child_acs),
        reasoning=reasoning[:100],
    )

    return Result.ok(result)


def _decomposition_result_from_structured_hermes(
    *,
    ac_content: str,
    ac_id: str,
    execution_id: str,
    depth: int,
    hermes_subcall: HermesDecompositionSubcall,
    ac_tree: ACTree | None = None,
) -> Result[DecompositionResult, DecompositionError] | None:
    """Use an accepted structured Hermes decomposition as AC tree input."""
    structured_result = hermes_subcall.structured_result
    if structured_result is None:
        return None
    if structured_result.verdict != "decomposed":
        return None
    if structured_result.control.requires_retry:
        return None

    children = [child.statement for child in structured_result.artifact.proposed_child_acs]
    dependencies = [child.depends_on for child in structured_result.artifact.proposed_child_acs]
    summary = structured_result.result.get("summary")
    reasoning = (
        str(summary)
        if isinstance(summary, str) and summary.strip()
        else ("Hermes structured decomposition")
    )
    return _create_decomposition_result(
        ac_content=ac_content,
        ac_id=ac_id,
        execution_id=execution_id,
        depth=depth,
        children=children,
        dependencies=dependencies,
        reasoning=reasoning,
        hermes_subcall=hermes_subcall,
        ac_tree=ac_tree,
    )


async def decompose_ac(
    ac_content: str,
    ac_id: str,
    execution_id: str,
    depth: int,
    llm_adapter: LLMAdapter,
    discover_insights: str = "",
    *,
    model: str | None = None,
    hermes_runtime: AgentRuntime | None = None,
    parent_call_id: str | None = None,
    call_id: str | None = None,
    rlm_node_id: str | None = None,
    ac_tree: ACTree | None = None,
) -> Result[DecompositionResult, DecompositionError | ProviderError]:
    """Decompose a non-atomic AC into child ACs using LLM.

    Uses the Discover phase insights to inform intelligent decomposition.
    Enforces max depth and prevents cyclic decomposition.

    Args:
        ac_content: The AC content to decompose.
        ac_id: Unique identifier for the parent AC.
        execution_id: Associated execution ID.
        depth: Current depth in AC tree (0-indexed).
        llm_adapter: LLM adapter for making completion requests.
        discover_insights: Insights from Discover phase (compressed at depth 3+).
        model: Model to use for decomposition.
        hermes_runtime: Optional Hermes runtime for RLM inner-LM decomposition guidance.
        parent_call_id: Optional RLM parent call identifier for tracing the sub-call.
        call_id: Optional RLM call identifier for this decomposition sub-call.
        rlm_node_id: Optional active RLM node ID for the Hermes output echo contract.
        ac_tree: Optional AC tree to mutate with accepted Hermes-derived child nodes.

    Returns:
        Result containing DecompositionResult or error.

    Raises:
        DecompositionError for max depth, cyclic decomposition, or validation failures.
        ProviderError for LLM failures.
    """
    log.info(
        "decomposition.started",
        ac_id=ac_id,
        execution_id=execution_id,
        depth=depth,
        ac_length=len(ac_content),
    )

    # Check max depth
    if depth >= MAX_DEPTH:
        error = DecompositionError(
            f"Max depth {MAX_DEPTH} reached, cannot decompose further",
            ac_id=ac_id,
            depth=depth,
            error_type="max_depth_reached",
        )
        _failed_event = create_ac_decomposition_failed_event(
            ac_id=ac_id,
            execution_id=execution_id,
            error_message=str(error),
            error_type="max_depth_reached",
            depth=depth,
        )
        log.warning(
            "decomposition.max_depth_reached",
            ac_id=ac_id,
            depth=depth,
        )
        return Result.err(error)

    # Compress context at depth 3+
    compressed_insights = _compress_context(discover_insights, depth)
    hermes_subcall: HermesDecompositionSubcall | None = None

    if hermes_runtime is not None:
        hermes_result = await _run_hermes_decomposition_subcall(
            ac_content=ac_content,
            ac_id=ac_id,
            discover_insights=compressed_insights,
            depth=depth,
            hermes_runtime=hermes_runtime,
            parent_call_id=parent_call_id,
            call_id=call_id,
            rlm_node_id=rlm_node_id,
        )
        if hermes_result.is_err:
            log.error(
                "decomposition.hermes_subcall_failed",
                ac_id=ac_id,
                depth=depth,
                parent_call_id=parent_call_id,
                error=str(hermes_result.error),
            )
            return Result.err(hermes_result.error)

        hermes_subcall = hermes_result.value
        structured_decomposition = _decomposition_result_from_structured_hermes(
            ac_content=ac_content,
            ac_id=ac_id,
            execution_id=execution_id,
            depth=depth,
            hermes_subcall=hermes_subcall,
            ac_tree=ac_tree,
        )
        if structured_decomposition is not None:
            return structured_decomposition

        compressed_insights = _merge_hermes_guidance(compressed_insights, hermes_subcall)
        log.info(
            "decomposition.hermes_subcall_completed",
            ac_id=ac_id,
            depth=depth,
            parent_call_id=parent_call_id,
            completion_length=len(hermes_subcall.completion),
        )

    # Build LLM request
    from ouroboros.providers.base import CompletionConfig, Message, MessageRole

    messages = [
        Message(role=MessageRole.SYSTEM, content=DECOMPOSITION_SYSTEM_PROMPT),
        Message(
            role=MessageRole.USER,
            content=DECOMPOSITION_USER_TEMPLATE.format(
                ac_content=ac_content,
                discover_insights=compressed_insights or "No specific insights available.",
                depth=depth,
                max_depth=MAX_DEPTH,
            ),
        ),
    ]

    config = CompletionConfig(
        model=model or get_decomposition_model(),
        temperature=0.5,  # Balanced creativity and consistency
        max_tokens=1000,
    )

    llm_result = await llm_adapter.complete(messages, config)

    if llm_result.is_err:
        llm_error = ProviderError(
            f"LLM decomposition failed: {llm_result.error}",
            provider="litellm",
        )
        _failed_event = create_ac_decomposition_failed_event(
            ac_id=ac_id,
            execution_id=execution_id,
            error_message=str(llm_error),
            error_type="llm_failure",
            depth=depth,
        )
        log.error(
            "decomposition.llm_failed",
            ac_id=ac_id,
            error=str(llm_result.error),
        )
        return Result.err(llm_error)

    # Parse LLM response
    response_text = llm_result.value.content
    parsed = _extract_json_from_response(response_text)

    if parsed is None:
        error = DecompositionError(
            "Failed to parse LLM decomposition response",
            ac_id=ac_id,
            depth=depth,
            error_type="parse_failure",
            details={"response_preview": response_text[:200]},
        )
        _failed_event = create_ac_decomposition_failed_event(
            ac_id=ac_id,
            execution_id=execution_id,
            error_message=str(error),
            error_type="parse_failure",
            depth=depth,
        )
        log.warning(
            "decomposition.parse_failed",
            ac_id=ac_id,
            response_preview=response_text[:200],
        )
        return Result.err(error)

    try:
        children_data = parsed.get("children", [])
        reasoning = parsed.get("reasoning", "LLM decomposition")
        children, dependencies = _parse_decomposition_children(children_data, ac_id=ac_id)
        return _create_decomposition_result(
            ac_content=ac_content,
            ac_id=ac_id,
            execution_id=execution_id,
            depth=depth,
            children=children,
            dependencies=dependencies,
            reasoning=reasoning,
            hermes_subcall=hermes_subcall,
            ac_tree=ac_tree,
        )

    except (ValueError, TypeError, KeyError) as e:
        error = DecompositionError(
            f"Failed to process decomposition response: {e}",
            ac_id=ac_id,
            depth=depth,
            error_type="processing_error",
            details={"exception": str(e)},
        )
        _failed_event = create_ac_decomposition_failed_event(
            ac_id=ac_id,
            execution_id=execution_id,
            error_message=str(error),
            error_type="processing_error",
            depth=depth,
        )
        log.error(
            "decomposition.processing_error",
            ac_id=ac_id,
            error=str(e),
        )
        return Result.err(error)
