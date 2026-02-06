"""Unit tests for execution strategy module."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.execution_strategy import (
    AnalysisStrategy,
    CodeStrategy,
    ExecutionStrategy,
    ResearchStrategy,
    get_strategy,
    register_strategy,
)
from ouroboros.orchestrator.workflow_state import ActivityType


class TestCodeStrategy:
    """Tests for CodeStrategy."""

    def test_implements_protocol(self) -> None:
        """Test that CodeStrategy satisfies ExecutionStrategy protocol."""
        strategy = CodeStrategy()
        assert isinstance(strategy, ExecutionStrategy)

    def test_get_tools(self) -> None:
        """Test code strategy provides code-oriented tools."""
        tools = CodeStrategy().get_tools()
        assert "Read" in tools
        assert "Write" in tools
        assert "Edit" in tools
        assert "Bash" in tools
        assert "Glob" in tools
        assert "Grep" in tools

    def test_get_system_prompt_fragment(self) -> None:
        """Test system prompt mentions coding context."""
        fragment = CodeStrategy().get_system_prompt_fragment()
        assert "coding agent" in fragment.lower()
        assert "clean" in fragment.lower()

    def test_get_task_prompt_suffix(self) -> None:
        """Test task prompt suffix for code tasks."""
        suffix = CodeStrategy().get_task_prompt_suffix()
        assert "code" in suffix.lower()

    def test_get_activity_map(self) -> None:
        """Test tool-to-activity mapping for code strategy."""
        activity_map = CodeStrategy().get_activity_map()
        assert activity_map["Read"] == ActivityType.EXPLORING
        assert activity_map["Edit"] == ActivityType.BUILDING
        assert activity_map["Write"] == ActivityType.BUILDING
        assert activity_map["Bash"] == ActivityType.TESTING


class TestResearchStrategy:
    """Tests for ResearchStrategy."""

    def test_implements_protocol(self) -> None:
        """Test that ResearchStrategy satisfies ExecutionStrategy protocol."""
        assert isinstance(ResearchStrategy(), ExecutionStrategy)

    def test_get_tools(self) -> None:
        """Test research strategy tools include Read/Write but no Edit."""
        tools = ResearchStrategy().get_tools()
        assert "Read" in tools
        assert "Write" in tools
        assert "Edit" not in tools

    def test_get_system_prompt_fragment(self) -> None:
        """Test system prompt mentions research context."""
        fragment = ResearchStrategy().get_system_prompt_fragment()
        assert "research" in fragment.lower()
        assert "markdown" in fragment.lower()

    def test_activity_map_bash_is_exploring(self) -> None:
        """Test Bash maps to EXPLORING (not TESTING) for research."""
        activity_map = ResearchStrategy().get_activity_map()
        assert activity_map["Bash"] == ActivityType.EXPLORING


class TestAnalysisStrategy:
    """Tests for AnalysisStrategy."""

    def test_implements_protocol(self) -> None:
        """Test that AnalysisStrategy satisfies ExecutionStrategy protocol."""
        assert isinstance(AnalysisStrategy(), ExecutionStrategy)

    def test_get_system_prompt_fragment(self) -> None:
        """Test system prompt mentions analytical context."""
        fragment = AnalysisStrategy().get_system_prompt_fragment()
        assert "analy" in fragment.lower()

    def test_get_task_prompt_suffix(self) -> None:
        """Test task prompt suffix for analysis tasks."""
        suffix = AnalysisStrategy().get_task_prompt_suffix()
        assert "analy" in suffix.lower()


class TestGetStrategy:
    """Tests for get_strategy registry function."""

    def test_get_code_strategy(self) -> None:
        """Test retrieving code strategy."""
        strategy = get_strategy("code")
        assert isinstance(strategy, CodeStrategy)

    def test_get_research_strategy(self) -> None:
        """Test retrieving research strategy."""
        strategy = get_strategy("research")
        assert isinstance(strategy, ResearchStrategy)

    def test_get_analysis_strategy(self) -> None:
        """Test retrieving analysis strategy."""
        strategy = get_strategy("analysis")
        assert isinstance(strategy, AnalysisStrategy)

    def test_case_insensitive(self) -> None:
        """Test strategy lookup is case-insensitive."""
        assert isinstance(get_strategy("Code"), CodeStrategy)
        assert isinstance(get_strategy("RESEARCH"), ResearchStrategy)

    def test_unknown_type_raises(self) -> None:
        """Test that unknown task type raises ValueError."""
        with pytest.raises(ValueError, match="Unknown task_type"):
            get_strategy("unknown_type")

    def test_default_is_code(self) -> None:
        """Test that default strategy is code."""
        strategy = get_strategy()
        assert isinstance(strategy, CodeStrategy)


class TestRegisterStrategy:
    """Tests for register_strategy function."""

    def test_register_custom_strategy(self) -> None:
        """Test registering and retrieving a custom strategy."""

        class CustomStrategy:
            def get_tools(self) -> list[str]:
                return ["Read"]

            def get_system_prompt_fragment(self) -> str:
                return "Custom agent"

            def get_task_prompt_suffix(self) -> str:
                return "Custom suffix"

            def get_activity_map(self) -> dict[str, ActivityType]:
                return {"Read": ActivityType.EXPLORING}

        register_strategy("custom", CustomStrategy())
        strategy = get_strategy("custom")
        assert strategy.get_tools() == ["Read"]
        assert strategy.get_system_prompt_fragment() == "Custom agent"


class TestStrategyProtocol:
    """Tests verifying ExecutionStrategy protocol compliance."""

    @pytest.mark.parametrize(
        "strategy_class",
        [CodeStrategy, ResearchStrategy, AnalysisStrategy],
    )
    def test_all_strategies_return_non_empty_tools(
        self, strategy_class: type
    ) -> None:
        """Test all strategies return at least one tool."""
        tools = strategy_class().get_tools()
        assert len(tools) > 0
        assert all(isinstance(t, str) for t in tools)

    @pytest.mark.parametrize(
        "strategy_class",
        [CodeStrategy, ResearchStrategy, AnalysisStrategy],
    )
    def test_all_strategies_return_non_empty_prompt(
        self, strategy_class: type
    ) -> None:
        """Test all strategies return non-empty system prompt."""
        fragment = strategy_class().get_system_prompt_fragment()
        assert len(fragment) > 0

    @pytest.mark.parametrize(
        "strategy_class",
        [CodeStrategy, ResearchStrategy, AnalysisStrategy],
    )
    def test_all_strategies_return_valid_activity_map(
        self, strategy_class: type
    ) -> None:
        """Test all strategies return valid activity maps."""
        activity_map = strategy_class().get_activity_map()
        assert len(activity_map) > 0
        for tool, activity in activity_map.items():
            assert isinstance(tool, str)
            assert isinstance(activity, ActivityType)
