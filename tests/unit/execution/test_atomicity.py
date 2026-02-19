"""Unit tests for ouroboros.execution.atomicity module.

Tests cover:
- AtomicityCriteria validation
- AtomicityResult model
- Heuristic atomicity detection
- LLM-based atomicity detection
- JSON extraction from responses
- Fallback behavior on LLM failure
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.errors import ProviderError, ValidationError
from ouroboros.core.types import Result


class TestAtomicityCriteria:
    """Tests for AtomicityCriteria dataclass."""

    def test_default_criteria(self):
        """AtomicityCriteria should have sensible defaults."""
        from ouroboros.execution.atomicity import (
            DEFAULT_MAX_COMPLEXITY,
            DEFAULT_MAX_DURATION_SECONDS,
            DEFAULT_MAX_TOOL_COUNT,
            AtomicityCriteria,
        )

        criteria = AtomicityCriteria()

        assert criteria.max_complexity == DEFAULT_MAX_COMPLEXITY
        assert criteria.max_tool_count == DEFAULT_MAX_TOOL_COUNT
        assert criteria.max_duration_seconds == DEFAULT_MAX_DURATION_SECONDS

    def test_custom_criteria(self):
        """AtomicityCriteria should accept custom values."""
        from ouroboros.execution.atomicity import AtomicityCriteria

        criteria = AtomicityCriteria(
            max_complexity=0.5,
            max_tool_count=2,
            max_duration_seconds=120,
        )

        assert criteria.max_complexity == 0.5
        assert criteria.max_tool_count == 2
        assert criteria.max_duration_seconds == 120

    def test_criteria_validate_success(self):
        """validate() should return Ok for valid criteria."""
        from ouroboros.execution.atomicity import AtomicityCriteria

        criteria = AtomicityCriteria(
            max_complexity=0.5,
            max_tool_count=3,
            max_duration_seconds=300,
        )

        result = criteria.validate()

        assert result.is_ok

    def test_criteria_validate_invalid_complexity_low(self):
        """validate() should reject complexity < 0."""
        from ouroboros.execution.atomicity import AtomicityCriteria

        criteria = AtomicityCriteria(max_complexity=-0.1)

        result = criteria.validate()

        assert result.is_err
        assert "max_complexity" in str(result.error)

    def test_criteria_validate_invalid_complexity_high(self):
        """validate() should reject complexity > 1."""
        from ouroboros.execution.atomicity import AtomicityCriteria

        criteria = AtomicityCriteria(max_complexity=1.5)

        result = criteria.validate()

        assert result.is_err
        assert "max_complexity" in str(result.error)

    def test_criteria_validate_invalid_tool_count(self):
        """validate() should reject negative tool_count."""
        from ouroboros.execution.atomicity import AtomicityCriteria

        criteria = AtomicityCriteria(max_tool_count=-1)

        result = criteria.validate()

        assert result.is_err
        assert "max_tool_count" in str(result.error)

    def test_criteria_validate_invalid_duration(self):
        """validate() should reject negative duration."""
        from ouroboros.execution.atomicity import AtomicityCriteria

        criteria = AtomicityCriteria(max_duration_seconds=-100)

        result = criteria.validate()

        assert result.is_err
        assert "max_duration_seconds" in str(result.error)

    def test_criteria_is_frozen(self):
        """AtomicityCriteria should be immutable."""
        from ouroboros.execution.atomicity import AtomicityCriteria

        criteria = AtomicityCriteria()

        with pytest.raises((AttributeError, TypeError)):
            criteria.max_complexity = 0.9


class TestAtomicityResult:
    """Tests for AtomicityResult dataclass."""

    def test_result_creation(self):
        """AtomicityResult should store all fields."""
        from ouroboros.execution.atomicity import AtomicityResult

        result = AtomicityResult(
            is_atomic=True,
            complexity_score=0.3,
            tool_count=2,
            estimated_duration=60,
            reasoning="Simple task",
            method="llm",
        )

        assert result.is_atomic is True
        assert result.complexity_score == 0.3
        assert result.tool_count == 2
        assert result.estimated_duration == 60
        assert result.reasoning == "Simple task"
        assert result.method == "llm"

    def test_result_default_method(self):
        """AtomicityResult method should default to 'llm'."""
        from ouroboros.execution.atomicity import AtomicityResult

        result = AtomicityResult(
            is_atomic=True,
            complexity_score=0.5,
            tool_count=1,
            estimated_duration=30,
            reasoning="Test",
        )

        assert result.method == "llm"

    def test_result_to_dict(self):
        """to_dict() should serialize result correctly."""
        from ouroboros.execution.atomicity import AtomicityResult

        result = AtomicityResult(
            is_atomic=False,
            complexity_score=0.8,
            tool_count=5,
            estimated_duration=400,
            reasoning="Complex multi-step task",
            method="heuristic",
        )

        data = result.to_dict()

        assert data["is_atomic"] is False
        assert data["complexity_score"] == 0.8
        assert data["tool_count"] == 5
        assert data["estimated_duration"] == 400
        assert data["reasoning"] == "Complex multi-step task"
        assert data["method"] == "heuristic"

    def test_result_is_frozen(self):
        """AtomicityResult should be immutable."""
        from ouroboros.execution.atomicity import AtomicityResult

        result = AtomicityResult(
            is_atomic=True,
            complexity_score=0.3,
            tool_count=1,
            estimated_duration=30,
            reasoning="Test",
        )

        with pytest.raises((AttributeError, TypeError)):
            result.is_atomic = False


class TestHeuristicAtomicityCheck:
    """Tests for heuristic-based atomicity detection."""

    def test_simple_ac_is_atomic(self):
        """Simple, short ACs should be detected as atomic."""
        from ouroboros.execution.atomicity import (
            AtomicityCriteria,
            _heuristic_atomicity_check,
        )

        result = _heuristic_atomicity_check(
            ac_content="Add a button to the header",
            criteria=AtomicityCriteria(),
        )

        assert result.method == "heuristic"
        # Short content with few tool keywords
        assert result.tool_count <= 3

    def test_complex_ac_non_atomic(self):
        """Complex ACs with many tools should be non-atomic."""
        from ouroboros.execution.atomicity import (
            AtomicityCriteria,
            _heuristic_atomicity_check,
        )

        # AC with many tool keywords
        ac_content = """
        Implement user authentication with database storage and API endpoints.
        Add git hooks for testing, configure npm build process,
        deploy to docker container after migration.
        """

        result = _heuristic_atomicity_check(
            ac_content=ac_content,
            criteria=AtomicityCriteria(),
        )

        assert result.method == "heuristic"
        # Should detect multiple tool dependencies
        assert result.tool_count >= 3

    def test_heuristic_detects_complexity_indicators(self):
        """Heuristic should detect complexity from words like 'and', 'then'."""
        from ouroboros.execution.atomicity import (
            AtomicityCriteria,
            _heuristic_atomicity_check,
        )

        ac_content = "First do A and then do B and after that do C while D runs"

        result = _heuristic_atomicity_check(
            ac_content=ac_content,
            criteria=AtomicityCriteria(),
        )

        # Complexity boost from indicators
        assert result.complexity_score > 0.3

    def test_heuristic_respects_criteria(self):
        """Heuristic should respect custom criteria thresholds."""
        from ouroboros.execution.atomicity import (
            AtomicityCriteria,
            _heuristic_atomicity_check,
        )

        # Very strict criteria
        strict_criteria = AtomicityCriteria(
            max_complexity=0.1,
            max_tool_count=1,
            max_duration_seconds=10,
        )

        result = _heuristic_atomicity_check(
            ac_content="Simple task with database",
            criteria=strict_criteria,
        )

        # Should be non-atomic due to strict criteria
        assert result.is_atomic is False
        assert "[Heuristic]" in result.reasoning


class TestJsonExtraction:
    """Tests for JSON extraction from LLM responses."""

    def test_extract_direct_json(self):
        """Should extract direct JSON response."""
        from ouroboros.execution.atomicity import _extract_json_from_response

        response = '{"is_atomic": true, "complexity_score": 0.3}'

        result = _extract_json_from_response(response)

        assert result is not None
        assert result["is_atomic"] is True

    def test_extract_json_from_markdown(self):
        """Should extract JSON from markdown code block."""
        from ouroboros.execution.atomicity import _extract_json_from_response

        response = """Here's the analysis:
```json
{"is_atomic": false, "complexity_score": 0.8}
```
"""

        result = _extract_json_from_response(response)

        assert result is not None
        assert result["is_atomic"] is False

    def test_extract_json_from_code_block(self):
        """Should extract JSON from generic code block."""
        from ouroboros.execution.atomicity import _extract_json_from_response

        response = """
```
{"is_atomic": true, "tool_count": 2}
```
"""

        result = _extract_json_from_response(response)

        assert result is not None
        assert result["tool_count"] == 2

    def test_extract_invalid_json_returns_none(self):
        """Should return None for invalid JSON."""
        from ouroboros.execution.atomicity import _extract_json_from_response

        response = "This is not JSON at all"

        result = _extract_json_from_response(response)

        assert result is None


class TestCheckAtomicity:
    """Tests for check_atomicity() async function."""

    @pytest.fixture
    def mock_llm_adapter(self):
        """Create a mock LLM adapter that returns atomic result."""
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            MagicMock(
                content='{"is_atomic": true, "complexity_score": 0.3, "tool_count": 1, "estimated_duration": 60, "reasoning": "Simple task"}'
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
    async def test_llm_based_check(self, mock_llm_adapter):
        """check_atomicity() should use LLM when enabled."""
        from ouroboros.execution.atomicity import check_atomicity

        result = await check_atomicity(
            ac_content="Add a login button",
            llm_adapter=mock_llm_adapter,
            use_llm=True,
        )

        assert result.is_ok
        assert result.value.method == "llm"
        assert result.value.is_atomic is True
        mock_llm_adapter.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_heuristic_when_llm_disabled(self, mock_llm_adapter):
        """check_atomicity() should use heuristic when LLM disabled."""
        from ouroboros.execution.atomicity import check_atomicity

        result = await check_atomicity(
            ac_content="Add a login button",
            llm_adapter=mock_llm_adapter,
            use_llm=False,
        )

        assert result.is_ok
        assert result.value.method == "heuristic"
        mock_llm_adapter.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_to_heuristic_on_llm_failure(self, failing_llm_adapter):
        """check_atomicity() should fallback to heuristic on LLM failure."""
        from ouroboros.execution.atomicity import check_atomicity

        result = await check_atomicity(
            ac_content="Add a login button",
            llm_adapter=failing_llm_adapter,
            use_llm=True,
        )

        assert result.is_ok
        assert result.value.method == "heuristic"

    @pytest.mark.asyncio
    async def test_fallback_on_parse_failure(self):
        """check_atomicity() should fallback on JSON parse failure."""
        from ouroboros.execution.atomicity import check_atomicity

        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            MagicMock(content="This is not valid JSON response")
        )

        result = await check_atomicity(
            ac_content="Add a login button",
            llm_adapter=adapter,
            use_llm=True,
        )

        assert result.is_ok
        assert result.value.method == "heuristic"

    @pytest.mark.asyncio
    async def test_respects_llm_atomic_decision(self):
        """check_atomicity() should respect LLM's is_atomic decision."""
        from ouroboros.execution.atomicity import AtomicityCriteria, check_atomicity

        # LLM says atomic with new format
        adapter = AsyncMock()
        adapter.complete.return_value = Result.ok(
            MagicMock(
                content='{"is_atomic": true, "reasoning": "Simple single task", "if_not_atomic": null}'
            )
        )

        criteria = AtomicityCriteria()

        result = await check_atomicity(
            ac_content="Test AC",
            llm_adapter=adapter,
            criteria=criteria,
            use_llm=True,
        )

        assert result.is_ok
        # LLM said atomic, so result should be atomic
        assert result.value.is_atomic is True
        assert result.value.method == "llm"

    @pytest.mark.asyncio
    async def test_rejects_invalid_criteria(self):
        """check_atomicity() should reject invalid criteria."""
        from ouroboros.execution.atomicity import AtomicityCriteria, check_atomicity

        adapter = AsyncMock()
        invalid_criteria = AtomicityCriteria(max_complexity=2.0)  # Invalid

        result = await check_atomicity(
            ac_content="Test AC",
            llm_adapter=adapter,
            criteria=invalid_criteria,
        )

        assert result.is_err
        assert isinstance(result.error, ValidationError)

    @pytest.mark.asyncio
    async def test_default_criteria_used(self, mock_llm_adapter):
        """check_atomicity() should use default criteria when None."""
        from ouroboros.execution.atomicity import check_atomicity

        result = await check_atomicity(
            ac_content="Test AC",
            llm_adapter=mock_llm_adapter,
            criteria=None,
        )

        assert result.is_ok


class TestAtomicityPrompts:
    """Tests for atomicity detection prompts."""

    def test_system_prompt_exists(self):
        """ATOMICITY_SYSTEM_PROMPT should be defined."""
        from ouroboros.execution.atomicity import ATOMICITY_SYSTEM_PROMPT

        assert "atomic" in ATOMICITY_SYSTEM_PROMPT.lower()
        assert len(ATOMICITY_SYSTEM_PROMPT) > 100

    def test_user_template_has_placeholders(self):
        """ATOMICITY_USER_TEMPLATE should have ac_content placeholder."""
        from ouroboros.execution.atomicity import ATOMICITY_USER_TEMPLATE

        assert "{ac_content}" in ATOMICITY_USER_TEMPLATE
        assert "is_atomic" in ATOMICITY_USER_TEMPLATE
        # Prompt now uses qualitative questions instead of complexity_score
        assert "reasoning" in ATOMICITY_USER_TEMPLATE
