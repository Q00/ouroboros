"""Unit tests for execution strategy module."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.execution_strategy import (
    AnalysisStrategy,
    ArtifactStrategy,
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


class TestArtifactStrategy:
    """Tests for artifact/document/presentation strategy."""

    def test_implements_protocol(self) -> None:
        """Test that ArtifactStrategy satisfies ExecutionStrategy protocol."""
        assert isinstance(ArtifactStrategy(), ExecutionStrategy)

    def test_get_tools(self) -> None:
        """Test artifact strategy supports file edits and artifact checks."""
        tools = ArtifactStrategy().get_tools()
        assert "Read" in tools
        assert "Write" in tools
        assert "Edit" in tools
        assert "Bash" in tools

    def test_prompt_is_artifact_oriented(self) -> None:
        """Test prompt steers away from code-test evidence."""
        fragment = ArtifactStrategy().get_system_prompt_fragment()
        assert "artifact" in fragment.lower()
        assert "unit-test evidence" in fragment.lower()

    def test_task_suffix_mentions_verification_commands(self) -> None:
        """Test artifact tasks preserve verify_command as the contract."""
        suffix = ArtifactStrategy().get_task_prompt_suffix()
        assert "artifact" in suffix.lower()
        assert "verification commands" in suffix.lower()


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

    @pytest.mark.parametrize("task_type", ["artifact", "document", "documentation", "presentation"])
    def test_get_artifact_strategy_aliases(self, task_type: str) -> None:
        """Test retrieving artifact-oriented strategy aliases."""
        strategy = get_strategy(task_type)
        assert isinstance(strategy, ArtifactStrategy)

    def test_case_insensitive(self) -> None:
        """Test strategy lookup is case-insensitive."""
        assert isinstance(get_strategy("Code"), CodeStrategy)
        assert isinstance(get_strategy("RESEARCH"), ResearchStrategy)
        assert isinstance(get_strategy("PRESENTATION"), ArtifactStrategy)

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

    def test_register_with_whitespace_strips_at_registration(self) -> None:
        """Whitespace around task_type at registration is canonicalized.

        Regression: register/get used only .lower() while Seed used
        .strip().lower(), causing a mismatch when whitespace was present
        at registration time.
        """
        from ouroboros.orchestrator.execution_strategy import (
            _STRATEGY_REGISTRY,
            is_registered_task_type,
        )

        class _WhitespaceStrategy:
            def get_tools(self) -> list[str]:
                return ["Read"]

            def get_system_prompt_fragment(self) -> str:
                return "ws"

            def get_task_prompt_suffix(self) -> str:
                return "ws"

            def get_activity_map(self) -> dict[str, ActivityType]:
                return {"Read": ActivityType.EXPLORING}

        # Register with leading/trailing whitespace
        register_strategy("  whitespace_type  ", _WhitespaceStrategy())
        try:
            # Canonical key must be stripped
            assert "whitespace_type" in _STRATEGY_REGISTRY
            assert "  whitespace_type  " not in _STRATEGY_REGISTRY

            # Lookup must succeed without whitespace
            assert is_registered_task_type("whitespace_type") is True
            assert is_registered_task_type("  whitespace_type  ") is True
            assert is_registered_task_type("WHITESPACE_TYPE") is True
            assert is_registered_task_type(" Whitespace_Type ") is True

            # get_strategy must resolve it
            strategy = get_strategy("whitespace_type")
            assert strategy.get_system_prompt_fragment() == "ws"
            strategy2 = get_strategy("  whitespace_type  ")
            assert strategy2.get_system_prompt_fragment() == "ws"
        finally:
            _STRATEGY_REGISTRY.pop("whitespace_type", None)

    def test_canonicalize_task_type_strips_and_lowers(self) -> None:
        """_canonicalize_task_type applies strip then lower consistently."""
        from ouroboros.orchestrator.execution_strategy import _canonicalize_task_type

        assert _canonicalize_task_type("  Code  ") == "code"
        assert _canonicalize_task_type("RESEARCH") == "research"
        assert _canonicalize_task_type("\tMy_Type\n") == "my_type"
        assert _canonicalize_task_type("already_normal") == "already_normal"

    def test_whitespace_registration_compatible_with_seed_validation(self) -> None:
        """Seed validator finds types registered with surrounding whitespace.

        End-to-end regression: the Seed validator and the strategy registry
        must agree on canonicalization so that registering with whitespace
        still allows Seed construction without whitespace (and vice-versa).
        """
        from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
        from ouroboros.orchestrator.execution_strategy import _STRATEGY_REGISTRY

        class _SeedCompatStrategy:
            def get_tools(self) -> list[str]:
                return ["Read"]

            def get_system_prompt_fragment(self) -> str:
                return "compat"

            def get_task_prompt_suffix(self) -> str:
                return "compat"

            def get_activity_map(self) -> dict[str, ActivityType]:
                return {"Read": ActivityType.EXPLORING}

        # Register WITH whitespace
        register_strategy("  compat_task  ", _SeedCompatStrategy())
        try:
            # Seed uses stripped value — must resolve
            seed = Seed(
                goal="Compatibility test",
                task_type="compat_task",
                ontology_schema=OntologySchema(name="T", description="T"),
                metadata=SeedMetadata(ambiguity_score=0.1),
            )
            assert seed.task_type == "compat_task"

            # Seed with whitespace also works
            seed2 = Seed(
                goal="Compatibility test 2",
                task_type="  Compat_Task  ",
                ontology_schema=OntologySchema(name="T", description="T"),
                metadata=SeedMetadata(ambiguity_score=0.1),
            )
            assert seed2.task_type == "compat_task"
        finally:
            _STRATEGY_REGISTRY.pop("compat_task", None)


class TestStrategyProtocol:
    """Tests verifying ExecutionStrategy protocol compliance."""

    @pytest.mark.parametrize(
        "strategy_class",
        [CodeStrategy, ResearchStrategy, AnalysisStrategy, ArtifactStrategy],
    )
    def test_all_strategies_return_non_empty_tools(self, strategy_class: type) -> None:
        """Test all strategies return at least one tool."""
        tools = strategy_class().get_tools()
        assert len(tools) > 0
        assert all(isinstance(t, str) for t in tools)

    @pytest.mark.parametrize(
        "strategy_class",
        [CodeStrategy, ResearchStrategy, AnalysisStrategy, ArtifactStrategy],
    )
    def test_all_strategies_return_non_empty_prompt(self, strategy_class: type) -> None:
        """Test all strategies return non-empty system prompt."""
        fragment = strategy_class().get_system_prompt_fragment()
        assert len(fragment) > 0

    @pytest.mark.parametrize(
        "strategy_class",
        [CodeStrategy, ResearchStrategy, AnalysisStrategy, ArtifactStrategy],
    )
    def test_all_strategies_return_valid_activity_map(self, strategy_class: type) -> None:
        """Test all strategies return valid activity maps."""
        activity_map = strategy_class().get_activity_map()
        assert len(activity_map) > 0
        for tool, activity in activity_map.items():
            assert isinstance(tool, str)
            assert isinstance(activity, ActivityType)
