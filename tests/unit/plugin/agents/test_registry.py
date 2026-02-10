"""Unit tests for Agent Registry.

Tests cover:
- AgentRole enum values
- AgentSpec dataclass creation and validation
- BUILTIN_AGENTS registry
- AgentRegistry class initialization
- Custom agent discovery from .md files
- Role-based agent lookup
- Agent composition with overrides
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ouroboros.plugin.agents.registry import (
    AgentRegistry,
    AgentRole,
    AgentSpec,
    BUILTIN_AGENTS,
)


class TestAgentRole:
    """Test AgentRole enum."""

    def test_all_role_values_defined(self) -> None:
        """Test that all expected role values are defined."""
        expected_roles = {
            "ANALYSIS",
            "PLANNING",
            "EXECUTION",
            "REVIEW",
            "DOMAIN",
            "PRODUCT",
            "COORDINATION",
        }
        actual_roles = {role.name for role in AgentRole}
        assert actual_roles == expected_roles

    def test_role_values_are_strings(self) -> None:
        """Test that role values are string representations."""
        assert AgentRole.ANALYSIS.value == "analysis"
        assert AgentRole.PLANNING.value == "planning"
        assert AgentRole.EXECUTION.value == "execution"
        assert AgentRole.REVIEW.value == "review"


class TestAgentSpec:
    """Test AgentSpec dataclass."""

    def test_create_agent_spec(self) -> None:
        """Test creating an AgentSpec."""
        spec = AgentSpec(
            name="test-agent",
            role=AgentRole.EXECUTION,
            model_preference="sonnet",
            system_prompt="You are a test agent.",
            tools=["Read", "Write"],
            capabilities=("testing",),
            description="A test agent",
        )

        assert spec.name == "test-agent"
        assert spec.role == AgentRole.EXECUTION
        assert spec.model_preference == "sonnet"
        assert spec.tools == ["Read", "Write"]
        assert spec.capabilities == ("testing",)

    def test_agent_spec_is_frozen(self) -> None:
        """Test that AgentSpec is immutable."""
        spec = AgentSpec(
            name="immutable",
            role=AgentRole.EXECUTION,
            model_preference="haiku",
            system_prompt="test",
            tools=[],
            capabilities=(),
            description="test",
        )

        with pytest.raises(Exception):  # FrozenInstanceError
            spec.name = "changed"  # type: ignore[misc]

    def test_agent_spec_has_slots(self) -> None:
        """Test that AgentSpec uses __slots__."""
        spec = AgentSpec(
            name="slots-test",
            role=AgentRole.EXECUTION,
            model_preference="haiku",
            system_prompt="test",
            tools=[],
            capabilities=(),
            description="test",
        )
        # Should have no __dict__ when using slots
        assert not hasattr(spec, "__dict__") or spec.__slots__ is not None


class TestBuiltinAgents:
    """Test BUILTIN_AGENTS registry."""

    def test_builtin_agents_contains_expected_keys(self) -> None:
        """Test that BUILTIN_AGENTS contains expected agents."""
        expected_keys = {"executor", "planner", "verifier", "analyst"}
        actual_keys = set(BUILTIN_AGENTS.keys())
        assert actual_keys == expected_keys

    def test_executor_agent_spec(self) -> None:
        """Test executor agent has correct specification."""
        executor = BUILTIN_AGENTS["executor"]

        assert executor.name == "executor"
        assert executor.role == AgentRole.EXECUTION
        assert executor.model_preference == "sonnet"
        assert "Read" in executor.tools
        assert "Write" in executor.tools
        assert "implementation" in executor.capabilities

    def test_planner_agent_spec(self) -> None:
        """Test planner agent has correct specification."""
        planner = BUILTIN_AGENTS["planner"]

        assert planner.name == "planner"
        assert planner.role == AgentRole.PLANNING
        assert planner.model_preference == "sonnet"
        assert "planning" in planner.capabilities

    def test_verifier_agent_spec(self) -> None:
        """Test verifier agent has correct specification."""
        verifier = BUILTIN_AGENTS["verifier"]

        assert verifier.name == "verifier"
        assert verifier.role == AgentRole.REVIEW
        assert verifier.model_preference == "haiku"
        assert "verification" in verifier.capabilities

    def test_analyst_agent_spec(self) -> None:
        """Test analyst agent has correct specification."""
        analyst = BUILTIN_AGENTS["analyst"]

        assert analyst.name == "analyst"
        assert analyst.role == AgentRole.ANALYSIS
        assert analyst.model_preference == "haiku"
        assert "analysis" in analyst.capabilities

    def test_all_builtin_agents_have_required_fields(self) -> None:
        """Test that all builtin agents have required fields populated."""
        for agent in BUILTIN_AGENTS.values():
            assert agent.name
            assert agent.role
            assert agent.model_preference
            assert agent.system_prompt
            assert agent.tools
            assert agent.capabilities
            assert agent.description


class TestAgentRegistryInit:
    """Test AgentRegistry initialization."""

    def test_registry_initializes_with_empty_custom_agents(self) -> None:
        """Test registry starts with no custom agents."""
        registry = AgentRegistry()
        assert registry._custom_agents == {}

    def test_registry_builds_role_index_from_builtin(self) -> None:
        """Test registry builds role index on initialization."""
        registry = AgentRegistry()
        role_index = registry._role_index

        # Check that builtin roles are indexed
        assert AgentRole.EXECUTION in role_index
        assert "executor" in role_index[AgentRole.EXECUTION]

        assert AgentRole.PLANNING in role_index
        assert "planner" in role_index[AgentRole.PLANNING]

        assert AgentRole.REVIEW in role_index
        assert "verifier" in role_index[AgentRole.REVIEW]

        assert AgentRole.ANALYSIS in role_index
        assert "analyst" in role_index[AgentRole.ANALYSIS]

    def test_agent_dir_constant(self) -> None:
        """Test AGENT_DIR constant is correctly set."""
        assert AgentRegistry.AGENT_DIR == Path(".claude-plugin/agents")


class TestAgentRegistryGetAgent:
    """Test AgentRegistry.get_agent method."""

    def test_get_builtin_executor_by_name(self) -> None:
        """Test getting executor builtin agent."""
        registry = AgentRegistry()
        agent = registry.get_agent("executor")

        assert agent is not None
        assert agent.name == "executor"
        assert agent.role == AgentRole.EXECUTION

    def test_get_builtin_planner_by_name(self) -> None:
        """Test getting planner builtin agent."""
        registry = AgentRegistry()
        agent = registry.get_agent("planner")

        assert agent is not None
        assert agent.name == "planner"

    def test_get_nonexistent_agent_returns_none(self) -> None:
        """Test getting nonexistent agent returns None."""
        registry = AgentRegistry()
        agent = registry.get_agent("nonexistent")

        assert agent is None

    def test_get_custom_agent_after_discovery(self) -> None:
        """Test getting custom agent after discovery."""
        registry = AgentRegistry()

        # Simulate adding a custom agent
        custom_spec = AgentSpec(
            name="custom",
            role=AgentRole.EXECUTION,
            model_preference="sonnet",
            system_prompt="Custom prompt",
            tools=["Read"],
            capabilities=("custom",),
            description="Custom agent",
        )
        registry._custom_agents["custom"] = custom_spec

        agent = registry.get_agent("custom")
        assert agent is not None
        assert agent.name == "custom"


class TestAgentRegistryGetAgentsByRole:
    """Test AgentRegistry.get_agents_by_role method."""

    def test_get_agents_by_execution_role(self) -> None:
        """Test getting agents with EXECUTION role."""
        registry = AgentRegistry()
        agents = registry.get_agents_by_role(AgentRole.EXECUTION)

        assert len(agents) >= 1
        assert any(a.name == "executor" for a in agents)
        assert all(a.role == AgentRole.EXECUTION for a in agents)

    def test_get_agents_by_planning_role(self) -> None:
        """Test getting agents with PLANNING role."""
        registry = AgentRegistry()
        agents = registry.get_agents_by_role(AgentRole.PLANNING)

        assert len(agents) >= 1
        assert any(a.name == "planner" for a in agents)

    def test_get_agents_by_review_role(self) -> None:
        """Test getting agents with REVIEW role."""
        registry = AgentRegistry()
        agents = registry.get_agents_by_role(AgentRole.REVIEW)

        assert len(agents) >= 1
        assert any(a.name == "verifier" for a in agents)

    def test_get_agents_by_empty_role_returns_empty_list(self) -> None:
        """Test that roles with no agents return empty list."""
        registry = AgentRegistry()
        agents = registry.get_agents_by_role(AgentRole.PRODUCT)

        assert agents == []

    def test_get_agents_by_role_includes_custom_agents(self) -> None:
        """Test that custom agents are included in role lookup."""
        registry = AgentRegistry()

        # Add custom agent with EXECUTION role
        custom_spec = AgentSpec(
            name="custom-executor",
            role=AgentRole.EXECUTION,
            model_preference="sonnet",
            system_prompt="Custom",
            tools=["Read"],
            capabilities=("custom",),
            description="Custom executor",
        )
        registry._custom_agents["custom-executor"] = custom_spec
        registry._role_index[AgentRole.EXECUTION].add("custom-executor")

        agents = registry.get_agents_by_role(AgentRole.EXECUTION)
        assert any(a.name == "custom-executor" for a in agents)


class TestAgentRegistryListAllAgents:
    """Test AgentRegistry.list_all_agents method."""

    def test_list_all_agents_includes_builtin(self) -> None:
        """Test that list_all_agents includes builtin agents."""
        registry = AgentRegistry()
        all_agents = registry.list_all_agents()

        assert "executor" in all_agents
        assert "planner" in all_agents
        assert "verifier" in all_agents
        assert "analyst" in all_agents

    def test_list_all_agents_includes_custom(self) -> None:
        """Test that list_all_agents includes custom agents."""
        registry = AgentRegistry()

        custom_spec = AgentSpec(
            name="custom-list-test",
            role=AgentRole.EXECUTION,
            model_preference="haiku",
            system_prompt="test",
            tools=[],
            capabilities=(),
            description="test",
        )
        registry._custom_agents["custom-list-test"] = custom_spec

        all_agents = registry.list_all_agents()
        assert "custom-list-test" in all_agents

    def test_list_all_agents_returns_dict(self) -> None:
        """Test that list_all_agents returns a dict."""
        registry = AgentRegistry()
        all_agents = registry.list_all_agents()

        assert isinstance(all_agents, dict)
        for name, spec in all_agents.items():
            assert isinstance(name, str)
            assert isinstance(spec, AgentSpec)


class TestAgentRegistryComposeAgent:
    """Test AgentRegistry.compose_agent method."""

    def test_compose_agent_with_overrides(self) -> None:
        """Test composing an agent with field overrides."""
        registry = AgentRegistry()

        composed = registry.compose_agent(
            name="my-executor",
            base_agent="executor",
            overrides={
                "model": "opus",
                "description": "My custom executor",
            },
        )

        assert composed.name == "my-executor"
        assert composed.model_preference == "opus"
        assert composed.description == "My custom executor"
        # Inherits from base
        assert composed.role == AgentRole.EXECUTION

    def test_compose_agent_inherits_base_properties(self) -> None:
        """Test that composed agent inherits base properties."""
        registry = AgentRegistry()

        composed = registry.compose_agent(
            name="derived-planner",
            base_agent="planner",
            overrides={},
        )

        base = BUILTIN_AGENTS["planner"]
        assert composed.role == base.role
        assert composed.model_preference == base.model_preference
        assert composed.system_prompt == base.system_prompt

    def test_compose_agent_override_tools(self) -> None:
        """Test overriding tools in composed agent."""
        registry = AgentRegistry()

        composed = registry.compose_agent(
            name="limited-executor",
            base_agent="executor",
            overrides={"tools": ["Read"]},
        )

        assert composed.tools == ["Read"]

    def test_compose_agent_override_capabilities(self) -> None:
        """Test overriding capabilities in composed agent."""
        registry = AgentRegistry()

        composed = registry.compose_agent(
            name="special-agent",
            base_agent="executor",
            overrides={"capabilities": ("special", "unique")},
        )

        assert composed.capabilities == ("special", "unique")

    def test_compose_agent_override_role_as_string(self) -> None:
        """Test overriding role using string value."""
        registry = AgentRegistry()

        composed = registry.compose_agent(
            name="reviewer",
            base_agent="executor",
            overrides={"role": "review"},
        )

        assert composed.role == AgentRole.REVIEW

    def test_compose_agent_override_role_as_enum(self) -> None:
        """Test overriding role using enum value."""
        registry = AgentRegistry()

        composed = registry.compose_agent(
            name="analyst-type",
            base_agent="executor",
            overrides={"role": AgentRole.ANALYSIS},
        )

        assert composed.role == AgentRole.ANALYSIS

    def test_compose_agent_invalid_base_raises_error(self) -> None:
        """Test that invalid base agent raises ValueError."""
        registry = AgentRegistry()

        with pytest.raises(ValueError, match="Base agent nonexistent not found"):
            registry.compose_agent(
                name="invalid",
                base_agent="nonexistent",
                overrides={},
            )


class TestAgentRegistryDiscoverCustom:
    """Test AgentRegistry.discover_custom method."""

    async def test_discover_custom_nonexistent_dir_returns_empty(self) -> None:
        """Test discovery with nonexistent agent directory."""
        registry = AgentRegistry()
        # Mock agent dir as non-existent
        registry.AGENT_DIR = Path("/nonexistent/path/agents")

        discovered = await registry.discover_custom()
        assert discovered == {}

    async def test_discover_custom_parses_markdown_files(self) -> None:
        """Test that discovery parses .md files correctly."""
        import tempfile

        registry = AgentRegistry()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            registry.AGENT_DIR = tmpdir_path

            # Create a test agent file
            agent_file = tmpdir_path / "test_agent.md"
            agent_file.write_text(
                """# Test Agent

You are a test agent.

## Role
This agent performs tests.

## Capabilities
- testing
- validation

## Tools
- Read
- Write

## Model Preference
sonnet
"""
            )

            discovered = await registry.discover_custom()

            assert "test_agent" in discovered or "Test Agent" in discovered
            # Check one of the keys matches
            keys = list(discovered.keys())
            assert len(keys) > 0

    async def test_discover_custom_updates_role_index(self) -> None:
        """Test that discovery updates the role index."""
        import tempfile

        registry = AgentRegistry()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            registry.AGENT_DIR = tmpdir_path

            # Create an analysis agent
            agent_file = tmpdir_path / "debugger.md"
            agent_file.write_text(
                """# Debugger

Debugs issues.

## Capabilities
- debug
- investigate
"""
            )

            await registry.discover_custom()

            # Check role index was updated
            assert AgentRole.ANALYSIS in registry._role_index

    async def test_discover_custom_handles_malformed_files(self) -> None:
        """Test that discovery handles malformed files gracefully."""
        import tempfile

        registry = AgentRegistry()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            registry.AGENT_DIR = tmpdir_path

            # Create an empty file
            (tmpdir_path / "empty.md").write_text("")

            # Should not raise exception
            discovered = await registry.discover_custom()

            # May or may not have parsed the empty file
            # Just verify it doesn't crash
            assert isinstance(discovered, dict)


class TestAgentRegistryBuildRoleIndex:
    """Test AgentRegistry._build_role_index method."""

    def test_build_role_index_empty_dict(self) -> None:
        """Test building role index from empty dict."""
        registry = AgentRegistry()
        index = registry._build_role_index({})

        assert index == {}

    def test_build_role_index_groups_by_role(self) -> None:
        """Test that role index correctly groups agents by role."""
        registry = AgentRegistry()

        agents: dict[str, AgentSpec] = {
            "agent1": AgentSpec(
                name="agent1",
                role=AgentRole.EXECUTION,
                model_preference="sonnet",
                system_prompt="test",
                tools=[],
                capabilities=(),
                description="test",
            ),
            "agent2": AgentSpec(
                name="agent2",
                role=AgentRole.EXECUTION,
                model_preference="haiku",
                system_prompt="test",
                tools=[],
                capabilities=(),
                description="test",
            ),
            "agent3": AgentSpec(
                name="agent3",
                role=AgentRole.PLANNING,
                model_preference="sonnet",
                system_prompt="test",
                tools=[],
                capabilities=(),
                description="test",
            ),
        }

        index = registry._build_role_index(agents)

        assert index[AgentRole.EXECUTION] == {"agent1", "agent2"}
        assert index[AgentRole.PLANNING] == {"agent3"}


class TestAgentRegistryParseAgentMd:
    """Test AgentRegistry._parse_agent_md method."""

    async def test_parse_agent_md_extracts_title(self) -> None:
        """Test parsing extracts title from heading."""
        import tempfile

        registry = AgentRegistry()

        with tempfile.TemporaryDirectory() as tmpdir:
            agent_file = Path(tmpdir) / "test.md"
            agent_file.write_text("# Custom Agent Name\n\nContent here.")

            spec = await registry._parse_agent_md(agent_file)

            assert spec is not None
            assert spec.name == "Custom Agent Name"

    async def test_parse_agent_md_extracts_capabilities(self) -> None:
        """Test parsing extracts capabilities from section."""
        import tempfile

        registry = AgentRegistry()

        with tempfile.TemporaryDirectory() as tmpdir:
            agent_file = Path(tmpdir) / "test.md"
            agent_file.write_text(
                """# Test
## Capabilities
- coding
- testing
- debugging
"""
            )

            spec = await registry._parse_agent_md(agent_file)

            assert spec is not None
            assert "coding" in spec.capabilities
            assert "testing" in spec.capabilities

    async def test_parse_agent_md_extracts_tools(self) -> None:
        """Test parsing extracts tools from section."""
        import tempfile

        registry = AgentRegistry()

        with tempfile.TemporaryDirectory() as tmpdir:
            agent_file = Path(tmpdir) / "test.md"
            agent_file.write_text(
                """# Test
## Tools
- Read
- Write
- Edit
"""
            )

            spec = await registry._parse_agent_md(agent_file)

            assert spec is not None
            assert "Read" in spec.tools
            assert "Write" in spec.tools

    async def test_parse_agent_md_infers_role_from_capabilities(self) -> None:
        """Test that role is inferred from capabilities."""
        import tempfile

        registry = AgentRegistry()

        # Test planning role inference
        with tempfile.TemporaryDirectory() as tmpdir:
            agent_file = Path(tmpdir) / "planner.md"
            agent_file.write_text(
                """# Planner
## Capabilities
- planning
- design
"""
            )

            spec = await registry._parse_agent_md(agent_file)
            assert spec is not None
            assert spec.role == AgentRole.PLANNING

    async def test_parse_agent_md_defaults_model_preference(self) -> None:
        """Test default model preference when not specified."""
        import tempfile

        registry = AgentRegistry()

        with tempfile.TemporaryDirectory() as tmpdir:
            agent_file = Path(tmpdir) / "test.md"
            agent_file.write_text("# Test\n\nNo model section.")

            spec = await registry._parse_agent_md(agent_file)

            assert spec is not None
            assert spec.model_preference == "sonnet"

    async def test_parse_agent_md_nonexistent_file_raises_error(self) -> None:
        """Test parsing nonexistent file raises error."""
        import pytest

        registry = AgentRegistry()

        with pytest.raises(FileNotFoundError):
            await registry._parse_agent_md(Path("/nonexistent/file.md"))
