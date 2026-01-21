"""Context management and compression for Ouroboros.

This module provides context size monitoring, compression, and preservation
of critical information during long-running workflows.

Key features:
- Token counting using LiteLLM
- Automatic compression when context exceeds limits (100K tokens or 6 hours)
- Preservation of critical info (seed, current AC, recent history, key facts)
- Fallback to aggressive truncation on compression failures
- Comprehensive observability with before/after metrics
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import litellm
import structlog

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.litellm_adapter import LiteLLMAdapter

log = structlog.get_logger()

# Context compression thresholds
MAX_TOKENS = 100_000  # NFR7: Max 100,000 tokens
MAX_AGE_HOURS = 6  # Trigger compression after 6 hours
RECENT_HISTORY_COUNT = 3  # Preserve last 3 iterations


@dataclass(frozen=True, slots=True)
class ContextMetrics:
    """Metrics about context size and age.

    Attributes:
        token_count: Current token count.
        age_hours: Age of context in hours.
        created_at: When the context was created.
        needs_compression: Whether compression is needed.
    """

    token_count: int
    age_hours: float
    created_at: datetime
    needs_compression: bool


@dataclass(frozen=True, slots=True)
class CompressionResult:
    """Result of a compression operation.

    Attributes:
        compressed_context: The compressed context data.
        before_tokens: Token count before compression.
        after_tokens: Token count after compression.
        compression_ratio: Ratio of compression (after/before).
        method: Compression method used ('llm' or 'truncate').
    """

    compressed_context: dict[str, Any]
    before_tokens: int
    after_tokens: int
    compression_ratio: float
    method: str


@dataclass(slots=True)
class WorkflowContext:
    """Context for a running workflow.

    Attributes:
        seed_summary: Initial seed/goal for the workflow.
        current_ac: Current acceptance criteria being worked on.
        history: Historical iterations/events.
        key_facts: Important facts extracted from history.
        created_at: When this context was created.
        metadata: Additional metadata.
    """

    seed_summary: str
    current_ac: str
    history: list[dict[str, Any]] = field(default_factory=list)
    key_facts: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert context to dictionary format.

        Returns:
            Dictionary representation of the context.
        """
        return {
            "seed_summary": self.seed_summary,
            "current_ac": self.current_ac,
            "history": self.history,
            "key_facts": self.key_facts,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowContext:
        """Create context from dictionary.

        Args:
            data: Dictionary representation of context.

        Returns:
            WorkflowContext instance.
        """
        created_at_str = data.get("created_at")
        created_at = (
            datetime.fromisoformat(created_at_str)
            if created_at_str
            else datetime.now(UTC)
        )

        return cls(
            seed_summary=data.get("seed_summary", ""),
            current_ac=data.get("current_ac", ""),
            history=data.get("history", []),
            key_facts=data.get("key_facts", []),
            created_at=created_at,
            metadata=data.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class FilteredContext:
    """Filtered context for SubAgent isolation.

    This contains only the information a SubAgent needs, isolating it
    from the full workflow context. The frozen dataclass ensures immutability,
    preventing SubAgent actions from modifying the main context (AC 3).

    Attributes:
        current_ac: The acceptance criteria for this SubAgent.
        relevant_facts: Facts relevant to this SubAgent's task (key_facts).
        parent_summary: Summary of parent context (seed_summary).
        recent_history: Recent history items (last RECENT_HISTORY_COUNT).
    """

    current_ac: str
    relevant_facts: list[str]
    parent_summary: str = ""
    recent_history: list[dict[str, Any]] = field(default_factory=list)


def count_tokens(text: str, model: str = "gpt-4") -> int:
    """Count tokens in text using LiteLLM's token counter.

    Args:
        text: The text to count tokens for.
        model: The model to use for tokenization. Default 'gpt-4'.

    Returns:
        The number of tokens in the text.
    """
    try:
        return litellm.token_counter(model=model, text=text)
    except Exception as e:
        # Fallback to rough estimation if token counting fails
        log.warning(
            "context.token_count.failed",
            error=str(e),
            using_fallback=True,
        )
        # Rough estimate: ~4 characters per token
        return len(text) // 4


def count_context_tokens(context: WorkflowContext, model: str = "gpt-4") -> int:
    """Count total tokens in a workflow context.

    Args:
        context: The workflow context to measure.
        model: The model to use for tokenization. Default 'gpt-4'.

    Returns:
        Total token count for the context.
    """
    # Convert context to a string representation
    context_str = f"""
Seed: {context.seed_summary}

Current AC: {context.current_ac}

Key Facts:
{chr(10).join(f"- {fact}" for fact in context.key_facts)}

History ({len(context.history)} items):
{chr(10).join(str(item) for item in context.history)}
"""
    return count_tokens(context_str, model)


def get_context_metrics(context: WorkflowContext, model: str = "gpt-4") -> ContextMetrics:
    """Get metrics about a workflow context.

    Args:
        context: The workflow context to analyze.
        model: The model to use for tokenization. Default 'gpt-4'.

    Returns:
        ContextMetrics with size and age information.
    """
    token_count = count_context_tokens(context, model)
    age = datetime.now(UTC) - context.created_at
    age_hours = age.total_seconds() / 3600

    needs_compression = token_count > MAX_TOKENS or age_hours > MAX_AGE_HOURS

    return ContextMetrics(
        token_count=token_count,
        age_hours=age_hours,
        created_at=context.created_at,
        needs_compression=needs_compression,
    )


async def compress_context_with_llm(
    context: WorkflowContext,
    llm_adapter: LiteLLMAdapter,
    model: str = "gpt-4",
) -> Result[str, ProviderError]:
    """Compress context using LLM summarization.

    Args:
        context: The workflow context to compress.
        llm_adapter: LLM adapter for making completion requests.
        model: The model to use for summarization.

    Returns:
        Result containing the compressed summary or a ProviderError.
    """
    # Build summarization prompt
    # Exclude recent history items from summarization
    items_to_summarize = (
        context.history[:-RECENT_HISTORY_COUNT] if len(context.history) > RECENT_HISTORY_COUNT else []
    )
    history_text = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(items_to_summarize))

    prompt = f"""Summarize the following workflow history, preserving key facts and decisions.
Focus on what was accomplished and what important information should be retained.

SEED/GOAL:
{context.seed_summary}

HISTORY TO SUMMARIZE:
{history_text}

CURRENT KEY FACTS:
{chr(10).join(f"- {fact}" for fact in context.key_facts)}

Provide a concise summary that captures:
1. Key accomplishments and milestones
2. Important decisions made
3. Critical facts that must be preserved
4. Any blockers or issues encountered

Keep the summary focused and factual. Omit unnecessary details."""

    messages = [Message(role=MessageRole.USER, content=prompt)]
    config = CompletionConfig(
        model=model,
        temperature=0.3,  # Lower temperature for more consistent summaries
        max_tokens=2000,  # Limit summary size
    )

    log.debug(
        "context.compression.llm.started",
        model=model,
        history_items=len(context.history),
    )

    result = await llm_adapter.complete(messages, config)

    if result.is_ok:
        log.debug(
            "context.compression.llm.completed",
            summary_tokens=result.value.usage.completion_tokens,
        )
        return Result.ok(result.value.content)
    else:
        log.warning(
            "context.compression.llm.failed",
            error=str(result.error),
        )
        return Result.err(result.error)


async def compress_context(
    context: WorkflowContext,
    llm_adapter: LiteLLMAdapter,
    model: str = "gpt-4",
) -> Result[CompressionResult, str]:
    """Compress a workflow context when it exceeds limits.

    This function implements the full compression logic:
    1. Try LLM-based summarization of history
    2. Preserve critical info (seed, current AC, recent history, key facts)
    3. Fall back to aggressive truncation on LLM failure
    4. Log compression events with metrics

    Args:
        context: The workflow context to compress.
        llm_adapter: LLM adapter for making completion requests.
        model: The model to use for summarization and token counting.

    Returns:
        Result containing CompressionResult or error message.
    """
    before_tokens = count_context_tokens(context, model)

    log.info(
        "context.compression.started",
        before_tokens=before_tokens,
        history_items=len(context.history),
        age_hours=get_context_metrics(context, model).age_hours,
    )

    # Try LLM-based compression first
    summary_result = await compress_context_with_llm(context, llm_adapter, model)

    if summary_result.is_ok:
        # LLM compression succeeded
        summary = summary_result.value

        # Build compressed context preserving critical info
        compressed_context = {
            "seed_summary": context.seed_summary,  # Always preserve
            "current_ac": context.current_ac,  # Always preserve
            "history_summary": summary,  # Compressed history
            "recent_history": context.history[-RECENT_HISTORY_COUNT:],  # Last 3 items
            "key_facts": context.key_facts,  # Always preserve
            "metadata": {
                **context.metadata,
                "compression_timestamp": datetime.now(UTC).isoformat(),
                "original_history_count": len(context.history),
            },
        }

        # Count tokens in compressed context
        compressed_str = f"""
Seed: {compressed_context['seed_summary']}
Current AC: {compressed_context['current_ac']}
Summary: {compressed_context['history_summary']}
Recent: {compressed_context['recent_history']}
Facts: {compressed_context['key_facts']}
"""
        after_tokens = count_tokens(compressed_str, model)
        compression_ratio = after_tokens / before_tokens if before_tokens > 0 else 1.0

        log.info(
            "context.compression.completed",
            method="llm",
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            compression_ratio=compression_ratio,
            reduction_percent=int((1 - compression_ratio) * 100),
        )

        return Result.ok(
            CompressionResult(
                compressed_context=compressed_context,
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                compression_ratio=compression_ratio,
                method="llm",
            )
        )
    else:
        # LLM compression failed - fall back to aggressive truncation
        log.warning(
            "context.compression.llm_failed.using_fallback",
            error=str(summary_result.error),
        )

        # Fallback: Keep only seed + current AC (most critical info)
        compressed_context = {
            "seed_summary": context.seed_summary,
            "current_ac": context.current_ac,
            "key_facts": context.key_facts[:5],  # Keep top 5 facts only
            "metadata": {
                **context.metadata,
                "compression_timestamp": datetime.now(UTC).isoformat(),
                "compression_method": "aggressive_truncation",
                "compression_reason": "llm_failure",
                "original_history_count": len(context.history),
            },
        }

        compressed_str = f"""
Seed: {compressed_context['seed_summary']}
Current AC: {compressed_context['current_ac']}
Facts: {compressed_context['key_facts']}
"""
        after_tokens = count_tokens(compressed_str, model)
        compression_ratio = after_tokens / before_tokens if before_tokens > 0 else 1.0

        log.warning(
            "context.compression.completed.fallback",
            method="truncate",
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            compression_ratio=compression_ratio,
            reduction_percent=int((1 - compression_ratio) * 100),
        )

        return Result.ok(
            CompressionResult(
                compressed_context=compressed_context,
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                compression_ratio=compression_ratio,
                method="truncate",
            )
        )


def create_filtered_context(
    context: WorkflowContext,
    subagent_ac: str,
    relevant_fact_keywords: list[str] | None = None,
) -> FilteredContext:
    """Create a filtered context for SubAgent isolation.

    Creates an immutable FilteredContext containing only the information
    a SubAgent needs to execute its task, isolating it from the full
    workflow context. This prevents context pollution (AC 3).

    The filtered context includes (per AC 2):
    - current_ac: The SubAgent's specific acceptance criterion
    - relevant_facts: Filtered key_facts (all or by keywords)
    - parent_summary: Summary including seed_summary
    - recent_history: Last RECENT_HISTORY_COUNT history items

    Args:
        context: The full workflow context.
        subagent_ac: The acceptance criteria for this SubAgent.
        relevant_fact_keywords: Optional keywords to filter relevant facts.

    Returns:
        FilteredContext with only information relevant to the SubAgent.
    """
    # Filter facts by keywords if provided
    if relevant_fact_keywords:
        relevant_facts = [
            fact
            for fact in context.key_facts
            if any(keyword.lower() in fact.lower() for keyword in relevant_fact_keywords)
        ]
    else:
        # If no keywords, include all facts (SubAgent might need them)
        relevant_facts = list(context.key_facts)  # Copy to ensure isolation

    # Create parent summary from seed
    parent_summary = f"Parent Goal: {context.seed_summary}"

    # Extract recent history (last RECENT_HISTORY_COUNT items)
    recent_history = list(context.history[-RECENT_HISTORY_COUNT:])

    return FilteredContext(
        current_ac=subagent_ac,
        relevant_facts=relevant_facts,
        parent_summary=parent_summary,
        recent_history=recent_history,
    )
