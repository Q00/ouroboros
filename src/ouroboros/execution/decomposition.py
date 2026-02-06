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
import json
import re
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.events.decomposition import (
    create_ac_decomposed_event,
    create_ac_decomposition_failed_event,
)
from ouroboros.observability.logging import get_logger

if TYPE_CHECKING:
    from ouroboros.providers.litellm_adapter import LiteLLMAdapter

log = get_logger(__name__)


# Decomposition constraints
MIN_CHILDREN = 2
MAX_CHILDREN = 5
MAX_DEPTH = 5
COMPRESSION_DEPTH = 3


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
    """

    parent_ac_id: str
    child_acs: tuple[str, ...]
    child_ac_ids: tuple[str, ...]
    reasoning: str
    events: list[BaseEvent] = field(default_factory=list)
    dependencies: tuple[tuple[int, ...], ...] = field(default_factory=tuple)


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


async def decompose_ac(
    ac_content: str,
    ac_id: str,
    execution_id: str,
    depth: int,
    llm_adapter: LiteLLMAdapter,
    discover_insights: str = "",
    *,
    model: str = "openrouter/anthropic/claude-3.5-sonnet",
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
        model=model,
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

        # Ensure children is a list
        if not isinstance(children_data, list):
            raise TypeError("children must be a list")

        # Extract content and dependencies from children
        # Support both old format (list of strings) and new format (list of dicts)
        children: list[str] = []
        dependencies: list[tuple[int, ...]] = []

        for i, child_item in enumerate(children_data):
            if isinstance(child_item, str):
                # Old format: plain string - no dependencies (backward compatibility)
                children.append(child_item)
                dependencies.append(tuple())
            elif isinstance(child_item, dict):
                # New format: dict with content and depends_on
                content = child_item.get("content", "")
                if not content:
                    raise ValueError(f"Child {i} has no content")
                children.append(str(content))

                # Parse depends_on, validating indices
                deps = child_item.get("depends_on", [])
                if not isinstance(deps, list):
                    deps = []

                # Filter and validate dependency indices
                # Only keep indices that are: int, >= 0, and < current index (no forward/self refs)
                valid_deps_list: list[int] = []
                invalid_deps: list[int] = []
                for d in deps:
                    if not isinstance(d, int):
                        continue  # Skip non-integer values silently
                    if d < 0 or d >= i:
                        # Forward reference (d >= i) or self-reference or negative index
                        invalid_deps.append(d)
                    else:
                        valid_deps_list.append(d)

                # Log if dependencies were filtered out
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
            else:
                raise TypeError(f"Child {i} must be string or dict, got {type(child_item)}")

        # Validate children
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

        # Generate child IDs
        child_ac_ids = tuple(f"ac_{uuid4().hex[:12]}" for _ in children)
        child_acs = tuple(children)
        dependencies_tuple = tuple(dependencies)

        # Log dependency structure for observability
        has_dependencies = any(deps for deps in dependencies_tuple)
        log.debug(
            "decomposition.dependencies_parsed",
            ac_id=ac_id,
            child_count=len(children),
            has_dependencies=has_dependencies,
            dependency_structure=[list(d) for d in dependencies_tuple],
        )

        # Create success event
        decomposed_event = create_ac_decomposed_event(
            parent_ac_id=ac_id,
            execution_id=execution_id,
            child_ac_ids=list(child_ac_ids),
            child_contents=list(child_acs),
            depth=depth,
            reasoning=reasoning,
        )

        result = DecompositionResult(
            parent_ac_id=ac_id,
            child_acs=child_acs,
            child_ac_ids=child_ac_ids,
            reasoning=reasoning,
            events=[decomposed_event],
            dependencies=dependencies_tuple,
        )

        log.info(
            "decomposition.completed",
            ac_id=ac_id,
            child_count=len(child_acs),
            reasoning=reasoning[:100],
        )

        return Result.ok(result)

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
