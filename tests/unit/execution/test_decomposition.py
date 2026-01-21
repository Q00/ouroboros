"""Unit tests for ouroboros.execution.decomposition module.

Tests cover:
- DecompositionResult model
- DecompositionError class
- JSON extraction from responses
- Child validation (count, cycles, empty)
- Context compression
- decompose_ac() function
- Max depth enforcement
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result


class TestDecompositionResult:
    """Tests for DecompositionResult dataclass."""

    def test_result_creation(self):
        """DecompositionResult should store all fields."""
        from ouroboros.execution.decomposition import DecompositionResult

        result = DecompositionResult(
            parent_ac_id="ac_parent123",
            child_acs=("Child AC 1", "Child AC 2"),
            child_ac_ids=("ac_child1", "ac_child2"),
            reasoning="Split by functionality",
            events=[],
        )

        assert result.parent_ac_id == "ac_parent123"
        assert len(result.child_acs) == 2
        assert len(result.child_ac_ids) == 2
        assert result.reasoning == "Split by functionality"

    def test_result_is_frozen(self):
        """DecompositionResult should be immutable."""
        from ouroboros.execution.decomposition import DecompositionResult

        result = DecompositionResult(
            parent_ac_id="ac_test",
            child_acs=("A", "B"),
            child_ac_ids=("ac_a", "ac_b"),
            reasoning="Test",
        )

        with pytest.raises((AttributeError, TypeError)):
            result.parent_ac_id = "modified"


class TestDecompositionError:
    """Tests for DecompositionError class."""

    def test_error_creation(self):
        """DecompositionError should store context."""
        from ouroboros.execution.decomposition import DecompositionError

        error = DecompositionError(
            message="Max depth reached",
            ac_id="ac_test",
            depth=5,
            error_type="max_depth_reached",
        )

        assert "Max depth reached" in str(error)
        assert error.ac_id == "ac_test"
        assert error.depth == 5
        assert error.error_type == "max_depth_reached"

    def test_error_with_details(self):
        """DecompositionError should store additional details."""
        from ouroboros.execution.decomposition import DecompositionError

        error = DecompositionError(
            message="Parse failed",
            ac_id="ac_test",
            depth=2,
            error_type="parse_failure",
            details={"response_preview": "invalid json"},
        )

        assert error.details == {"response_preview": "invalid json"}


class TestJsonExtraction:
    """Tests for JSON extraction from LLM responses."""

    def test_extract_direct_json(self):
        """Should extract direct JSON response."""
        from ouroboros.execution.decomposition import _extract_json_from_response

        response = '{"children": ["A", "B"], "reasoning": "Test"}'

        result = _extract_json_from_response(response)

        assert result is not None
        assert result["children"] == ["A", "B"]

    def test_extract_json_from_markdown(self):
        """Should extract JSON from markdown code block."""
        from ouroboros.execution.decomposition import _extract_json_from_response

        response = """Here's the decomposition:
```json
{"children": ["Task A", "Task B", "Task C"], "reasoning": "Split by domain"}
```
"""

        result = _extract_json_from_response(response)

        assert result is not None
        assert len(result["children"]) == 3

    def test_extract_json_with_children_array(self):
        """Should find JSON with children array pattern."""
        from ouroboros.execution.decomposition import _extract_json_from_response

        response = """I'll decompose this into:
{"children": ["Setup database", "Create API"], "reasoning": "Backend split"}
Additional notes here.
"""

        result = _extract_json_from_response(response)

        assert result is not None
        assert "children" in result

    def test_extract_invalid_returns_none(self):
        """Should return None for invalid JSON."""
        from ouroboros.execution.decomposition import _extract_json_from_response

        response = "No JSON here, just text about decomposition."

        result = _extract_json_from_response(response)

        assert result is None


class TestValidateChildren:
    """Tests for _validate_children() function."""

    def test_valid_children(self):
        """Should accept valid children list."""
        from ouroboros.execution.decomposition import _validate_children

        result = _validate_children(
            children=["Child 1", "Child 2", "Child 3"],
            parent_content="Parent AC",
            ac_id="ac_test",
            depth=0,
        )

        assert result.is_ok

    def test_too_few_children(self):
        """Should reject less than MIN_CHILDREN."""
        from ouroboros.execution.decomposition import _validate_children

        result = _validate_children(
            children=["Only one"],
            parent_content="Parent AC",
            ac_id="ac_test",
            depth=0,
        )

        assert result.is_err
        assert "minimum" in str(result.error).lower()
        assert result.error.error_type == "insufficient_children"

    def test_too_many_children(self):
        """Should reject more than MAX_CHILDREN."""
        from ouroboros.execution.decomposition import _validate_children

        result = _validate_children(
            children=["A", "B", "C", "D", "E", "F"],  # 6 children
            parent_content="Parent AC",
            ac_id="ac_test",
            depth=0,
        )

        assert result.is_err
        assert "maximum" in str(result.error).lower()
        assert result.error.error_type == "too_many_children"

    def test_cyclic_decomposition(self):
        """Should reject child identical to parent."""
        from ouroboros.execution.decomposition import _validate_children

        result = _validate_children(
            children=["Parent AC", "Different child"],  # First is same as parent
            parent_content="Parent AC",
            ac_id="ac_test",
            depth=0,
        )

        assert result.is_err
        assert "cyclic" in str(result.error).lower()
        assert result.error.error_type == "cyclic_decomposition"

    def test_cyclic_case_insensitive(self):
        """Should detect cycles case-insensitively."""
        from ouroboros.execution.decomposition import _validate_children

        result = _validate_children(
            children=["  PARENT AC  ", "Different child"],
            parent_content="parent ac",
            ac_id="ac_test",
            depth=0,
        )

        assert result.is_err
        assert result.error.error_type == "cyclic_decomposition"

    def test_empty_child(self):
        """Should reject empty child content."""
        from ouroboros.execution.decomposition import _validate_children

        result = _validate_children(
            children=["Valid child", "   ", "Another valid"],
            parent_content="Parent",
            ac_id="ac_test",
            depth=0,
        )

        assert result.is_err
        assert "empty" in str(result.error).lower()
        assert result.error.error_type == "empty_child"


class TestContextCompression:
    """Tests for _compress_context() function."""

    def test_no_compression_at_shallow_depth(self):
        """Should not compress at depth < 3."""
        from ouroboros.execution.decomposition import _compress_context

        insights = "A" * 1000  # 1000 chars

        result = _compress_context(insights, depth=2)

        assert result == insights  # Not compressed

    def test_compression_at_depth_3(self):
        """Should compress at depth >= 3."""
        from ouroboros.execution.decomposition import _compress_context

        insights = "A" * 1000

        result = _compress_context(insights, depth=3)

        assert len(result) < 1000
        assert "compressed for depth" in result

    def test_short_content_not_compressed(self):
        """Should not compress content under 500 chars."""
        from ouroboros.execution.decomposition import _compress_context

        insights = "Short insights"

        result = _compress_context(insights, depth=5)

        assert result == insights


class TestDecomposeAc:
    """Tests for decompose_ac() async function."""

    @pytest.fixture
    def mock_llm_adapter(self):
        """Create a mock LLM adapter that returns valid decomposition."""
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            MagicMock(
                content='{"children": ["Child AC 1", "Child AC 2", "Child AC 3"], "reasoning": "Split by functionality"}'
            )
        )
        return adapter

    @pytest.fixture
    def failing_llm_adapter(self):
        """Create a mock LLM adapter that fails."""
        adapter = AsyncMock()
        adapter.complete.return_value = Result.err(
            ProviderError("LLM timeout", provider="openrouter")
        )
        return adapter

    @pytest.mark.asyncio
    async def test_successful_decomposition(self, mock_llm_adapter):
        """decompose_ac() should return children on success."""
        from ouroboros.execution.decomposition import decompose_ac

        result = await decompose_ac(
            ac_content="Implement user authentication",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=mock_llm_adapter,
            discover_insights="User needs login and registration",
        )

        assert result.is_ok
        assert len(result.value.child_acs) == 3
        assert len(result.value.child_ac_ids) == 3
        assert all(id.startswith("ac_") for id in result.value.child_ac_ids)
        assert result.value.reasoning == "Split by functionality"

    @pytest.mark.asyncio
    async def test_decomposition_emits_event(self, mock_llm_adapter):
        """decompose_ac() should emit decomposition event."""
        from ouroboros.execution.decomposition import decompose_ac

        result = await decompose_ac(
            ac_content="Test AC",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=mock_llm_adapter,
        )

        assert result.is_ok
        assert len(result.value.events) == 1
        assert result.value.events[0].type == "ac.decomposition.completed"

    @pytest.mark.asyncio
    async def test_max_depth_rejection(self, mock_llm_adapter):
        """decompose_ac() should reject at max depth."""
        from ouroboros.execution.decomposition import MAX_DEPTH, decompose_ac

        result = await decompose_ac(
            ac_content="Test AC",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=MAX_DEPTH,  # At max depth
            llm_adapter=mock_llm_adapter,
        )

        assert result.is_err
        assert "max depth" in str(result.error).lower()
        mock_llm_adapter.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_failure_returns_error(self, failing_llm_adapter):
        """decompose_ac() should return error on LLM failure."""
        from ouroboros.execution.decomposition import decompose_ac

        result = await decompose_ac(
            ac_content="Test AC",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=failing_llm_adapter,
        )

        assert result.is_err
        assert isinstance(result.error, ProviderError)

    @pytest.mark.asyncio
    async def test_parse_failure_returns_error(self):
        """decompose_ac() should return error on parse failure."""
        from ouroboros.execution.decomposition import decompose_ac

        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            MagicMock(content="Not valid JSON response")
        )

        result = await decompose_ac(
            ac_content="Test AC",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=adapter,
        )

        assert result.is_err
        assert result.error.error_type == "parse_failure"

    @pytest.mark.asyncio
    async def test_validation_failure_returns_error(self):
        """decompose_ac() should return error on validation failure."""
        from ouroboros.execution.decomposition import decompose_ac

        adapter = AsyncMock()
        # Only 1 child - should fail validation
        adapter.complete.return_value = Result.ok(
            MagicMock(content='{"children": ["Only one"], "reasoning": "Test"}')
        )

        result = await decompose_ac(
            ac_content="Test AC",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=adapter,
        )

        assert result.is_err
        assert result.error.error_type == "insufficient_children"

    @pytest.mark.asyncio
    async def test_context_compression_at_depth(self, mock_llm_adapter):
        """decompose_ac() should compress context at depth >= 3."""
        from ouroboros.execution.decomposition import decompose_ac

        long_insights = "A" * 1000

        await decompose_ac(
            ac_content="Test AC",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=3,
            llm_adapter=mock_llm_adapter,
            discover_insights=long_insights,
        )

        # Check that LLM was called with compressed insights
        call_args = mock_llm_adapter.complete.call_args
        messages = call_args[0][0]
        user_message = messages[1].content
        assert "[compressed for depth]" in user_message

    @pytest.mark.asyncio
    async def test_child_ids_are_unique(self, mock_llm_adapter):
        """decompose_ac() should generate unique child IDs."""
        from ouroboros.execution.decomposition import decompose_ac

        result = await decompose_ac(
            ac_content="Test AC",
            ac_id="ac_parent",
            execution_id="exec_123",
            depth=0,
            llm_adapter=mock_llm_adapter,
        )

        assert result.is_ok
        # All IDs should be unique
        child_ids = result.value.child_ac_ids
        assert len(child_ids) == len(set(child_ids))


class TestDecompositionConstants:
    """Tests for module constants."""

    def test_min_children_is_2(self):
        """MIN_CHILDREN should be 2."""
        from ouroboros.execution.decomposition import MIN_CHILDREN

        assert MIN_CHILDREN == 2

    def test_max_children_is_5(self):
        """MAX_CHILDREN should be 5."""
        from ouroboros.execution.decomposition import MAX_CHILDREN

        assert MAX_CHILDREN == 5

    def test_max_depth_is_5(self):
        """MAX_DEPTH should be 5."""
        from ouroboros.execution.decomposition import MAX_DEPTH

        assert MAX_DEPTH == 5

    def test_compression_depth_is_3(self):
        """COMPRESSION_DEPTH should be 3."""
        from ouroboros.execution.decomposition import COMPRESSION_DEPTH

        assert COMPRESSION_DEPTH == 3


class TestDecompositionPrompts:
    """Tests for decomposition prompts."""

    def test_system_prompt_exists(self):
        """DECOMPOSITION_SYSTEM_PROMPT should be defined."""
        from ouroboros.execution.decomposition import DECOMPOSITION_SYSTEM_PROMPT

        assert "MECE" in DECOMPOSITION_SYSTEM_PROMPT
        assert "2-5" in DECOMPOSITION_SYSTEM_PROMPT

    def test_user_template_has_placeholders(self):
        """DECOMPOSITION_USER_TEMPLATE should have required placeholders."""
        from ouroboros.execution.decomposition import DECOMPOSITION_USER_TEMPLATE

        assert "{ac_content}" in DECOMPOSITION_USER_TEMPLATE
        assert "{discover_insights}" in DECOMPOSITION_USER_TEMPLATE
        assert "{depth}" in DECOMPOSITION_USER_TEMPLATE
        assert "{max_depth}" in DECOMPOSITION_USER_TEMPLATE
