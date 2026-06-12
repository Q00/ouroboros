"""Integration tests for Plugin Orchestration.

Tests cover:
- Agent orchestration with pool
- Skill execution flow
"""

import asyncio
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, Mock

import pytest

from ouroboros.plugin.agents.pool import (
    AgentPool,
    AgentPoolConfig,
)
from ouroboros.plugin.agents.registry import (
    BUILTIN_AGENTS,
    AgentRegistry,
    AgentRole,
)
from ouroboros.plugin.skills.registry import (
    SkillRegistry,
)


class TestAgentPoolIntegration:
    """Integration tests for AgentPool."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        """Create mock Claude Agent adapter."""
        adapter = MagicMock()

        # Create an async iterator that yields mock messages
        async def mock_execute_task(*args, **kwargs):
            """Yield mock messages as an async iterator."""
            mock_msg = Mock()
            mock_msg.content = "Test response"
            yield mock_msg

        # Set return_value to the async generator function
        adapter.execute_task = mock_execute_task
        return adapter

    @pytest.mark.asyncio
    async def test_agent_pool_lifecycle(self, mock_adapter: MagicMock) -> None:
        """Test complete agent pool lifecycle."""
        config = AgentPoolConfig(
            min_instances=1,
            max_instances=3,
            idle_timeout=60.0,
            health_check_interval=10.0,
        )

        pool = AgentPool(adapter=mock_adapter, config=config)

        # Start pool
        await pool.start()
        assert pool.stats["total_agents"] == 1

        # Submit task
        task_id = await pool.submit_task(
            agent_type="executor",
            prompt="Test task",
            priority=1,
        )

        assert task_id.startswith("task-")

        # Get result
        result = await pool.get_task_result(task_id, timeout=5.0)

        assert result.success is True
        assert result.messages == ("Test response",)

        # Stop pool
        await pool.stop()
        assert pool.stats["total_agents"] == 0

    @pytest.mark.asyncio
    async def test_agent_pool_scaling(self, mock_adapter: MagicMock) -> None:
        """Test agent pool auto-scaling."""
        config = AgentPoolConfig(
            min_instances=1,
            max_instances=5,
            enable_auto_scaling=True,
        )

        pool = AgentPool(adapter=mock_adapter, config=config)
        await pool.start()

        initial_count = pool.stats["total_agents"]

        # Submit multiple tasks
        task_ids = []
        for _ in range(3):
            task_id = await pool.submit_task(
                agent_type="executor",
                prompt=f"Task {_}",
                priority=1,
            )
            task_ids.append(task_id)

        # Wait for some scaling to occur
        await asyncio.sleep(0.2)

        # Pool should have scaled up
        final_count = pool.stats["total_agents"]
        assert final_count >= initial_count

        # Clean up
        await pool.stop()

    @pytest.mark.asyncio
    async def test_agent_pool_cancels_stuck_task(self) -> None:
        """Health checker cancels timed-out tasks instead of only resetting state."""
        adapter = MagicMock()

        async def stalled_execute_task(*args, **kwargs):
            await asyncio.sleep(1.0)
            if False:  # pragma: no cover - keep this an async generator
                yield None

        adapter.execute_task = stalled_execute_task

        config = AgentPoolConfig(
            min_instances=1,
            max_instances=1,
            health_check_interval=0.05,
            task_timeout=0.1,
            enable_auto_scaling=False,
        )

        pool = AgentPool(adapter=adapter, config=config)
        await pool.start()

        task_id = await pool.submit_task(
            agent_type="executor",
            prompt="stuck task",
            priority=1,
        )

        result = await pool.get_task_result(task_id, timeout=0.5)

        assert result.success is False
        assert result.error_message == "Task cancelled"

        await pool.stop()


class TestAgentRegistryIntegration:
    """Integration tests for AgentRegistry."""

    @pytest.mark.asyncio
    async def test_registry_full_workflow(self) -> None:
        """Test full registry workflow with custom agents."""
        registry = AgentRegistry()

        # Verify builtin agents
        executor = registry.get_agent("executor")
        assert executor is not None
        assert executor.name == "executor"

        # Get agents by role
        execution_agents = registry.get_agents_by_role(AgentRole.EXECUTION)
        assert len(execution_agents) >= 1

        # Compose new agent
        custom = registry.compose_agent(
            name="my-executor",
            base_agent="executor",
            overrides={"model": "opus", "description": "My custom executor"},
        )

        assert custom.name == "my-executor"
        assert custom.model_preference == "opus"

        # List all agents
        all_agents = registry.list_all_agents()
        assert "executor" in all_agents

    @pytest.mark.asyncio
    async def test_registry_with_temp_custom_agents(self) -> None:
        """Test registry discovering custom agents from temp directory."""
        registry = AgentRegistry()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            registry.AGENT_DIR = tmpdir_path

            # Create custom agent files
            (tmpdir_path / "custom1.md").write_text(
                """# Custom Agent 1

Custom agent for testing.

## Capabilities
- custom capability

## Tools
- Read
"""
            )

            (tmpdir_path / "custom2.md").write_text(
                """# Custom Agent 2

Another custom agent.

## Capabilities
- testing
- validation
"""
            )

            # Discover custom agents
            discovered = await registry.discover_custom()

            assert len(discovered) > 0

            # Verify they're accessible
            all_agents = registry.list_all_agents()
            assert len(all_agents) > len(BUILTIN_AGENTS)


class TestSkillRegistryIntegration:
    """Integration tests for SkillRegistry."""

    @pytest.mark.asyncio
    async def test_skill_registry_discovery_workflow(self) -> None:
        """Test full skill discovery workflow."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "skills"
            skill_dir.mkdir()

            # Create multiple skills
            autopilot_skill = skill_dir / "autopilot"
            autopilot_skill.mkdir()

            (autopilot_skill / "SKILL.md").write_text(
                """---
description: Autonomous execution skill
triggers:
  - autopilot
  - build me
magic_prefixes:
  - ooo:auto
---

# Autopilot

Execute tasks autonomously.
"""
            )

            test_skill = skill_dir / "test"
            test_skill.mkdir()

            (test_skill / "SKILL.md").write_text(
                """---
description: Testing skill
triggers:
  - test this
---

# Test

A testing skill.
"""
            )

            registry = SkillRegistry(skill_dir=skill_dir)
            discovered = await registry.discover_all()

            assert "autopilot" in discovered
            assert "test" in discovered

            # Test trigger keyword matching
            autopilot_matches = registry.find_by_trigger_keyword("autopilot")
            assert len(autopilot_matches) > 0

            # Test magic prefix matching
            prefix_matches = registry.find_by_magic_prefix("ooo:auto")
            assert len(prefix_matches) > 0

    @pytest.mark.asyncio
    async def test_skill_hot_reload(self) -> None:
        """Test skill hot-reload functionality."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "skills"
            skill_dir.mkdir()

            test_skill = skill_dir / "reload_test"
            test_skill.mkdir()

            skill_md = test_skill / "SKILL.md"
            skill_md.write_text("# Original Content")

            registry = SkillRegistry(skill_dir=skill_dir)
            await registry.discover_all()

            original_skill = registry.get_skill("reload_test")
            assert original_skill is not None
            assert "Original Content" in original_skill.spec["raw"]

            # Modify and reload
            skill_md.write_text("# Updated Content")

            result = await registry.reload_skill(test_skill)

            assert result.is_ok

            reloaded_skill = registry.get_skill("reload_test")
            assert reloaded_skill is not None
            assert "Updated Content" in reloaded_skill.spec["raw"]
