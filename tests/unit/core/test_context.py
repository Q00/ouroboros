"""Tests for context management and compression."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.context import (
    MAX_AGE_HOURS,
    MAX_TOKENS,
    RECENT_HISTORY_COUNT,
    CompressionResult,
    ContextMetrics,
    FilteredContext,
    WorkflowContext,
    compress_context,
    compress_context_with_llm,
    count_context_tokens,
    count_tokens,
    create_filtered_context,
    get_context_metrics,
)
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    MessageRole,
    UsageInfo,
)
from ouroboros.providers.litellm_adapter import LiteLLMAdapter


class TestTokenCounting:
    """Tests for token counting functions."""

    def test_count_tokens_basic(self) -> None:
        """Test basic token counting."""
        text = "Hello, world!"
        count = count_tokens(text)
        assert count > 0
        assert isinstance(count, int)

    def test_count_tokens_empty(self) -> None:
        """Test token counting with empty string."""
        count = count_tokens("")
        assert count >= 0

    def test_count_tokens_long_text(self) -> None:
        """Test token counting with longer text."""
        text = "This is a longer text " * 100
        count = count_tokens(text)
        assert count > 100  # Should have many tokens

    def test_count_context_tokens(self) -> None:
        """Test counting tokens in a workflow context."""
        context = WorkflowContext(
            seed_summary="Build a web application",
            current_ac="Implement user authentication",
            history=[
                {"event": "started", "timestamp": "2024-01-01"},
                {"event": "completed_setup", "timestamp": "2024-01-02"},
            ],
            key_facts=["Using FastAPI", "PostgreSQL database"],
        )
        count = count_context_tokens(context)
        assert count > 0
        assert isinstance(count, int)

    def test_count_context_tokens_large_history(self) -> None:
        """Test token counting with large history."""
        context = WorkflowContext(
            seed_summary="Build a web application",
            current_ac="Implement user authentication",
            history=[{"event": f"iteration_{i}", "details": "x" * 100} for i in range(100)],
            key_facts=["fact"] * 50,
        )
        count = count_context_tokens(context)
        assert count > 1000  # Should be substantial


class TestContextMetrics:
    """Tests for context metrics calculation."""

    def test_get_context_metrics_small_context(self) -> None:
        """Test metrics for small context that doesn't need compression."""
        context = WorkflowContext(
            seed_summary="Simple task",
            current_ac="Do something",
            created_at=datetime.now(timezone.utc),
        )
        metrics = get_context_metrics(context)

        assert isinstance(metrics, ContextMetrics)
        assert metrics.token_count > 0
        assert metrics.age_hours < 1  # Just created
        assert not metrics.needs_compression  # Small and recent

    def test_get_context_metrics_old_context(self) -> None:
        """Test metrics for old context that needs compression."""
        old_time = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS + 1)
        context = WorkflowContext(
            seed_summary="Old task",
            current_ac="Still working",
            created_at=old_time,
        )
        metrics = get_context_metrics(context)

        assert metrics.age_hours > MAX_AGE_HOURS
        assert metrics.needs_compression  # Old enough

    def test_get_context_metrics_large_context(self) -> None:
        """Test metrics for large context that needs compression."""
        # Create a context with lots of data to exceed token limit (MAX_TOKENS = 100000)
        # Need more data to actually exceed the threshold
        large_history = [{"event": f"iteration_{i}", "data": "x" * 2000} for i in range(500)]
        context = WorkflowContext(
            seed_summary="Complex project " * 100,
            current_ac="Current work " * 100,
            history=large_history,
            key_facts=["fact " * 200] * 200,
        )
        metrics = get_context_metrics(context)

        # This should exceed MAX_TOKENS (100000)
        assert metrics.token_count > MAX_TOKENS or metrics.needs_compression


class TestWorkflowContext:
    """Tests for WorkflowContext model."""

    def test_workflow_context_creation(self) -> None:
        """Test creating a workflow context."""
        context = WorkflowContext(
            seed_summary="Build feature X",
            current_ac="Implement Y",
        )
        assert context.seed_summary == "Build feature X"
        assert context.current_ac == "Implement Y"
        assert context.history == []
        assert context.key_facts == []
        assert isinstance(context.created_at, datetime)

    def test_workflow_context_to_dict(self) -> None:
        """Test converting context to dictionary."""
        context = WorkflowContext(
            seed_summary="Build feature X",
            current_ac="Implement Y",
            history=[{"event": "test"}],
            key_facts=["fact1", "fact2"],
        )
        data = context.to_dict()

        assert data["seed_summary"] == "Build feature X"
        assert data["current_ac"] == "Implement Y"
        assert data["history"] == [{"event": "test"}]
        assert data["key_facts"] == ["fact1", "fact2"]
        assert "created_at" in data

    def test_workflow_context_from_dict(self) -> None:
        """Test creating context from dictionary."""
        data = {
            "seed_summary": "Build feature X",
            "current_ac": "Implement Y",
            "history": [{"event": "test"}],
            "key_facts": ["fact1"],
            "created_at": "2024-01-01T00:00:00+00:00",
            "metadata": {"version": "1.0"},
        }
        context = WorkflowContext.from_dict(data)

        assert context.seed_summary == "Build feature X"
        assert context.current_ac == "Implement Y"
        assert context.history == [{"event": "test"}]
        assert context.key_facts == ["fact1"]
        assert context.metadata == {"version": "1.0"}

    def test_workflow_context_roundtrip(self) -> None:
        """Test converting context to dict and back."""
        original = WorkflowContext(
            seed_summary="Test",
            current_ac="AC",
            history=[{"a": 1}],
            key_facts=["f"],
            metadata={"m": "v"},
        )
        data = original.to_dict()
        restored = WorkflowContext.from_dict(data)

        assert restored.seed_summary == original.seed_summary
        assert restored.current_ac == original.current_ac
        assert restored.history == original.history
        assert restored.key_facts == original.key_facts
        assert restored.metadata == original.metadata


class TestLLMCompression:
    """Tests for LLM-based compression."""

    async def test_compress_context_with_llm_success(self) -> None:
        """Test successful LLM compression."""
        context = WorkflowContext(
            seed_summary="Build web app",
            current_ac="Add auth",
            history=[
                {"event": "setup", "details": "Initialized project"},
                {"event": "database", "details": "Set up PostgreSQL"},
                {"event": "api", "details": "Created API endpoints"},
            ],
            key_facts=["Using FastAPI", "JWT tokens"],
        )

        # Mock LLM adapter
        mock_adapter = AsyncMock(spec=LiteLLMAdapter)
        mock_response = CompletionResponse(
            content="Summary: Set up project with PostgreSQL and API endpoints. Using FastAPI with JWT.",
            model="gpt-4",
            usage=UsageInfo(prompt_tokens=100, completion_tokens=20, total_tokens=120),
        )
        mock_adapter.complete.return_value = Result.ok(mock_response)

        result = await compress_context_with_llm(context, mock_adapter)

        assert result.is_ok
        assert "Summary" in result.value
        mock_adapter.complete.assert_called_once()

    async def test_compress_context_with_llm_failure(self) -> None:
        """Test LLM compression failure."""
        context = WorkflowContext(
            seed_summary="Build web app",
            current_ac="Add auth",
            history=[{"event": "test"}],
        )

        # Mock LLM adapter to fail
        mock_adapter = AsyncMock(spec=LiteLLMAdapter)
        mock_error = ProviderError("API rate limit exceeded", provider="openai", status_code=429)
        mock_adapter.complete.return_value = Result.err(mock_error)

        result = await compress_context_with_llm(context, mock_adapter)

        assert result.is_err
        assert result.error.status_code == 429


class TestContextCompression:
    """Tests for full context compression."""

    async def test_compress_context_llm_success(self) -> None:
        """Test compression with successful LLM summarization."""
        context = WorkflowContext(
            seed_summary="Build web application",
            current_ac="Implement authentication",
            history=[
                {"iteration": 1, "work": "Set up project structure"},
                {"iteration": 2, "work": "Configure database"},
                {"iteration": 3, "work": "Create API endpoints"},
                {"iteration": 4, "work": "Add validation"},
                {"iteration": 5, "work": "Write tests"},
            ],
            key_facts=["FastAPI framework", "PostgreSQL", "JWT tokens"],
        )

        # Mock successful LLM response
        mock_adapter = AsyncMock(spec=LiteLLMAdapter)
        mock_response = CompletionResponse(
            content="Completed project setup, database config, and API endpoints with validation and tests.",
            model="gpt-4",
            usage=UsageInfo(prompt_tokens=200, completion_tokens=30, total_tokens=230),
        )
        mock_adapter.complete.return_value = Result.ok(mock_response)

        result = await compress_context(context, mock_adapter)

        assert result.is_ok
        compression = result.value
        assert isinstance(compression, CompressionResult)
        assert compression.method == "llm"
        assert compression.before_tokens > 0
        assert compression.after_tokens > 0
        assert compression.after_tokens < compression.before_tokens
        assert 0 < compression.compression_ratio < 1

        # Check preserved content
        compressed = compression.compressed_context
        assert compressed["seed_summary"] == context.seed_summary
        assert compressed["current_ac"] == context.current_ac
        assert "history_summary" in compressed
        assert len(compressed["recent_history"]) == RECENT_HISTORY_COUNT
        assert compressed["key_facts"] == context.key_facts

    async def test_compress_context_llm_fallback(self) -> None:
        """Test compression fallback when LLM fails."""
        context = WorkflowContext(
            seed_summary="Build feature",
            current_ac="Implement logic",
            history=[{"i": i} for i in range(10)],
            key_facts=[f"fact_{i}" for i in range(10)],
        )

        # Mock LLM failure
        mock_adapter = AsyncMock(spec=LiteLLMAdapter)
        mock_error = ProviderError("Timeout", provider="openai")
        mock_adapter.complete.return_value = Result.err(mock_error)

        result = await compress_context(context, mock_adapter)

        assert result.is_ok
        compression = result.value
        assert compression.method == "truncate"  # Fallback method
        assert compression.before_tokens > 0
        assert compression.after_tokens > 0

        # Check aggressive truncation preserved critical info only
        compressed = compression.compressed_context
        assert compressed["seed_summary"] == context.seed_summary
        assert compressed["current_ac"] == context.current_ac
        assert "history_summary" not in compressed  # No history in fallback
        assert len(compressed["key_facts"]) <= 5  # Only top 5 facts
        assert compressed["metadata"]["compression_method"] == "aggressive_truncation"
        assert compressed["metadata"]["compression_reason"] == "llm_failure"

    async def test_compress_context_preserves_critical_info(self) -> None:
        """Test that compression always preserves critical information."""
        context = WorkflowContext(
            seed_summary="Critical seed information",
            current_ac="Critical AC information",
            history=[{"i": i, "data": "x" * 100} for i in range(20)],
            key_facts=["Critical fact 1", "Critical fact 2"],
        )

        # Mock LLM success
        mock_adapter = AsyncMock(spec=LiteLLMAdapter)
        mock_response = CompletionResponse(
            content="Summary of work done",
            model="gpt-4",
            usage=UsageInfo(prompt_tokens=100, completion_tokens=10, total_tokens=110),
        )
        mock_adapter.complete.return_value = Result.ok(mock_response)

        result = await compress_context(context, mock_adapter)

        assert result.is_ok
        compressed = result.value.compressed_context

        # Critical info must always be present
        assert compressed["seed_summary"] == "Critical seed information"
        assert compressed["current_ac"] == "Critical AC information"
        assert "Critical fact 1" in compressed["key_facts"]
        assert "Critical fact 2" in compressed["key_facts"]
        # Recent history preserved
        assert len(compressed["recent_history"]) == RECENT_HISTORY_COUNT

    async def test_compress_context_logs_metrics(self) -> None:
        """Test that compression logs before/after metrics."""
        context = WorkflowContext(
            seed_summary="Test",
            current_ac="Test AC",
            history=[{"i": i} for i in range(5)],
        )

        mock_adapter = AsyncMock(spec=LiteLLMAdapter)
        mock_response = CompletionResponse(
            content="Summary",
            model="gpt-4",
            usage=UsageInfo(prompt_tokens=50, completion_tokens=5, total_tokens=55),
        )
        mock_adapter.complete.return_value = Result.ok(mock_response)

        result = await compress_context(context, mock_adapter)

        assert result.is_ok
        # Check that metrics are present
        assert result.value.before_tokens > 0
        assert result.value.after_tokens > 0
        assert result.value.compression_ratio > 0


class TestFilteredContext:
    """Tests for SubAgent context filtering."""

    def test_create_filtered_context_basic(self) -> None:
        """Test creating filtered context for SubAgent."""
        context = WorkflowContext(
            seed_summary="Build web application",
            current_ac="Implement features",
            key_facts=["FastAPI", "PostgreSQL", "JWT authentication", "Redis cache"],
        )

        filtered = create_filtered_context(
            context,
            subagent_ac="Implement user registration endpoint",
        )

        assert isinstance(filtered, FilteredContext)
        assert filtered.current_ac == "Implement user registration endpoint"
        assert filtered.relevant_facts == context.key_facts  # All facts included
        assert "Build web application" in filtered.parent_summary

    def test_create_filtered_context_with_keywords(self) -> None:
        """Test filtered context with keyword filtering."""
        context = WorkflowContext(
            seed_summary="Build web application",
            current_ac="Implement features",
            key_facts=[
                "FastAPI framework used",
                "PostgreSQL for data storage",
                "JWT for authentication",
                "Redis for caching",
                "Authentication uses bcrypt",
            ],
        )

        filtered = create_filtered_context(
            context,
            subagent_ac="Implement authentication",
            relevant_fact_keywords=["authentication", "JWT", "bcrypt"],
        )

        # Should only include facts with relevant keywords
        assert "JWT for authentication" in filtered.relevant_facts
        assert "Authentication uses bcrypt" in filtered.relevant_facts
        assert "PostgreSQL for data storage" not in filtered.relevant_facts
        assert "Redis for caching" not in filtered.relevant_facts

    def test_create_filtered_context_no_matching_keywords(self) -> None:
        """Test filtered context when no facts match keywords."""
        context = WorkflowContext(
            seed_summary="Build application",
            current_ac="Current work",
            key_facts=["Fact A", "Fact B", "Fact C"],
        )

        filtered = create_filtered_context(
            context,
            subagent_ac="New task",
            relevant_fact_keywords=["nonexistent"],
        )

        # No matching facts
        assert filtered.relevant_facts == []

    def test_filtered_context_isolation(self) -> None:
        """Test that filtered context isolates SubAgent from full context."""
        full_context = WorkflowContext(
            seed_summary="Parent goal",
            current_ac="Parent AC",
            history=[{"sensitive": "data"}] * 100,  # SubAgent shouldn't see this
            key_facts=["fact1", "fact2", "fact3"],
        )

        filtered = create_filtered_context(
            full_context,
            subagent_ac="SubAgent AC",
            relevant_fact_keywords=["fact1"],
        )

        # SubAgent only sees its own AC and relevant facts
        assert filtered.current_ac == "SubAgent AC"
        assert filtered.current_ac != full_context.current_ac
        assert "fact1" in filtered.relevant_facts
        assert len(filtered.relevant_facts) < len(full_context.key_facts)


class TestEdgeCases:
    """Tests for edge cases and error conditions."""

    def test_count_tokens_with_special_characters(self) -> None:
        """Test token counting with special characters."""
        text = "Hello 🌍! Ñoño. 中文测试."
        count = count_tokens(text)
        assert count > 0

    async def test_compress_empty_context(self) -> None:
        """Test compressing an empty context."""
        context = WorkflowContext(
            seed_summary="",
            current_ac="",
            history=[],
            key_facts=[],
        )

        mock_adapter = AsyncMock(spec=LiteLLMAdapter)
        mock_response = CompletionResponse(
            content="Empty context",
            model="gpt-4",
            usage=UsageInfo(prompt_tokens=10, completion_tokens=2, total_tokens=12),
        )
        mock_adapter.complete.return_value = Result.ok(mock_response)

        result = await compress_context(context, mock_adapter)
        assert result.is_ok

    def test_context_with_future_timestamp(self) -> None:
        """Test context with future created_at timestamp."""
        future_time = datetime.now(timezone.utc) + timedelta(hours=1)
        context = WorkflowContext(
            seed_summary="Future context",
            current_ac="Test",
            created_at=future_time,
        )

        metrics = get_context_metrics(context)
        # Age should be negative
        assert metrics.age_hours < 0

    def test_workflow_context_from_dict_missing_fields(self) -> None:
        """Test creating context from dict with missing fields."""
        data = {"seed_summary": "Test"}  # Missing many fields

        context = WorkflowContext.from_dict(data)
        assert context.seed_summary == "Test"
        assert context.current_ac == ""  # Default value
        assert context.history == []  # Default value
        assert context.key_facts == []  # Default value


class TestFilteredContextWithRecentHistory:
    """Tests for Story 3.4: SubAgent Isolation - FilteredContext with recent_history."""

    def test_filtered_context_has_recent_history(self) -> None:
        """Test that FilteredContext includes recent_history field (AC 2)."""
        context = WorkflowContext(
            seed_summary="Build web application",
            current_ac="Implement features",
            history=[
                {"iteration": 1, "event": "setup"},
                {"iteration": 2, "event": "database"},
                {"iteration": 3, "event": "api"},
                {"iteration": 4, "event": "validation"},
                {"iteration": 5, "event": "testing"},
            ],
            key_facts=["FastAPI", "PostgreSQL"],
        )

        filtered = create_filtered_context(
            context,
            subagent_ac="Implement user endpoint",
        )

        # AC 2: Filtered context must include recent_history
        assert hasattr(filtered, "recent_history")
        assert len(filtered.recent_history) == RECENT_HISTORY_COUNT  # Last 3
        assert filtered.recent_history[-1] == {"iteration": 5, "event": "testing"}

    def test_filtered_context_immutable(self) -> None:
        """Test that FilteredContext is immutable (AC 3: main context not modified)."""
        # FilteredContext should be frozen dataclass
        filtered = FilteredContext(
            current_ac="test",
            relevant_facts=["fact"],
            parent_summary="parent",
            recent_history=[{"event": "test"}],
        )

        # Should raise error when trying to modify
        with pytest.raises((AttributeError, TypeError)):
            filtered.current_ac = "modified"  # type: ignore

    def test_filtered_context_short_history(self) -> None:
        """Test filtered context when history is shorter than RECENT_HISTORY_COUNT."""
        context = WorkflowContext(
            seed_summary="Build app",
            current_ac="Current",
            history=[{"iteration": 1, "event": "only_one"}],
            key_facts=["fact"],
        )

        filtered = create_filtered_context(
            context,
            subagent_ac="SubAgent AC",
        )

        # Should include all available history if less than RECENT_HISTORY_COUNT
        assert len(filtered.recent_history) == 1
        assert filtered.recent_history[0] == {"iteration": 1, "event": "only_one"}

    def test_filtered_context_empty_history(self) -> None:
        """Test filtered context when history is empty."""
        context = WorkflowContext(
            seed_summary="Build app",
            current_ac="Current",
            history=[],
            key_facts=["fact"],
        )

        filtered = create_filtered_context(
            context,
            subagent_ac="SubAgent AC",
        )

        assert filtered.recent_history == []

    def test_filtered_context_contains_all_required_fields(self) -> None:
        """Test FilteredContext contains all fields from AC 2: seed_summary, current_ac, recent_history, key_facts."""
        context = WorkflowContext(
            seed_summary="Build web application",
            current_ac="Parent AC",
            history=[{"i": 1}, {"i": 2}, {"i": 3}, {"i": 4}],
            key_facts=["FastAPI", "PostgreSQL"],
        )

        filtered = create_filtered_context(
            context,
            subagent_ac="SubAgent AC",
        )

        # AC 2: All required fields
        assert hasattr(filtered, "current_ac")
        assert filtered.current_ac == "SubAgent AC"

        assert hasattr(filtered, "relevant_facts")  # key_facts
        assert "FastAPI" in filtered.relevant_facts
        assert "PostgreSQL" in filtered.relevant_facts

        assert hasattr(filtered, "parent_summary")  # seed_summary
        assert "Build web application" in filtered.parent_summary

        assert hasattr(filtered, "recent_history")
        assert len(filtered.recent_history) == RECENT_HISTORY_COUNT
