"""Integration tests for Plugin Orchestration.

Tests cover:
- Agent orchestration with pool
- Skill execution flow
- State persistence across sessions
- Router integration with complexity estimation
- End-to-end workflows
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
from ouroboros.plugin.orchestration.router import ModelRouter, RoutingContext
from ouroboros.plugin.skills.registry import (
    SkillRegistry,
)
from ouroboros.plugin.state.manager import SessionStatus, StateManager
from ouroboros.plugin.state.store import StateMode, StateStore
from ouroboros.routing.tiers import Tier


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


class TestModelRouterIntegration:
    """Integration tests for ModelRouter."""

    @pytest.mark.asyncio
    async def test_router_with_learning(self) -> None:
        """Test router learns from routing history."""
        router = ModelRouter()

        # Route a simple task
        context = RoutingContext(
            task_type="simple",
            token_estimate=100,
            tool_count=1,
            ac_depth=1,
        )

        # First route - should use complexity estimation
        tier1 = await router.route(context)
        assert tier1 == Tier.FRUGAL

        # Record successful result
        await router.record_result(context, tier1, success=True)

        # Route again - should use history
        tier2 = await router.route(context)
        assert tier2 == Tier.FRUGAL

        # Verify history
        stats = router.get_statistics()
        assert stats["total_routes"] == 2
        assert stats["total_records"] == 1

    @pytest.mark.asyncio
    async def test_router_escalation_workflow(self) -> None:
        """Test router escalation after failures."""
        router = ModelRouter()

        context = RoutingContext(
            task_type="failing",
            token_estimate=100,
            tool_count=1,
            ac_depth=1,
        )

        # Initial route
        tier1 = await router.route(context)

        # Record failures
        for _ in range(router.ESCALATION_AFTER_FAILURES):
            await router.record_result(context, tier1, success=False)

        # Next route should escalate
        tier2 = await router.route(context)

        # Should be higher tier
        if tier1 == Tier.FRUGAL:
            assert tier2 == Tier.STANDARD
        elif tier1 == Tier.STANDARD:
            assert tier2 == Tier.FRONTIER

    @pytest.mark.asyncio
    async def test_router_cost_optimization(self) -> None:
        """Test cost optimization affects routing."""
        router = ModelRouter()
        router.set_cost_optimization(enabled=True)

        # Medium complexity task
        context = RoutingContext(
            task_type="medium",
            token_estimate=2000,
            tool_count=3,
            ac_depth=2,
        )

        tier = await router.route(context)

        # With cost optimization, might downgrade
        # The important thing is it makes a decision
        assert tier in [Tier.FRUGAL, Tier.STANDARD, Tier.FRONTIER]


class TestStateManagerIntegration:
    """Integration tests for StateManager."""

    @pytest.mark.asyncio
    async def test_session_lifecycle(self) -> None:
        """Test complete session lifecycle."""
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            store = StateStore(worktree=worktree)
            manager = StateManager(store=store)

            # Create session
            result = await manager.save_session(
                session_id="sess-test",
                execution_id="exec-1",
                seed_id="seed-1",
                seed_goal="Test goal",
                acceptance_criteria=["AC1", "AC2"],
                workflow_state={"step": "initial"},
                mode=StateMode.AUTOPILOT,
            )

            assert result.is_ok

            # Load session
            loaded = await manager.load_session("sess-test")
            assert loaded is not None
            assert loaded.seed_goal == "Test goal"

            # Update session
            update_result = await manager.update_session(
                session_id="sess-test",
                workflow_state={"step": "completed"},
                status=SessionStatus.COMPLETED,
            )

            assert update_result.is_ok

            # Create checkpoint
            ckpt_result = await manager.create_checkpoint(
                session_id="sess-test",
                phase="completion",
                state={"checkpointed": True},
            )

            assert ckpt_result.is_ok

            # List checkpoints
            checkpoints = await manager.list_checkpoints("sess-test")
            assert len(checkpoints) >= 1

    @pytest.mark.asyncio
    async def test_auto_checkpoint_workflow(self) -> None:
        """Test auto-checkpoint functionality."""
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            store = StateStore(worktree=worktree)
            manager = StateManager(store=store, auto_checkpoint=True)

            # Create active session
            await manager.save_session(
                session_id="sess-auto",
                execution_id="exec-1",
                seed_id="seed-1",
                seed_goal="Auto checkpoint test",
                acceptance_criteria=[],
                workflow_state={},
                mode=StateMode.AUTOPILOT,
            )

            # Start auto-checkpoint
            await manager.start_auto_checkpoint()

            # Wait a bit for checkpoint task to potentially run
            await asyncio.sleep(0.1)

            # Stop auto-checkpoint
            await manager.stop_auto_checkpoint()


class TestEndToEndWorkflow:
    """End-to-end workflow tests."""

    @pytest.mark.asyncio
    async def test_full_plugin_workflow(self) -> None:
        """Test complete plugin workflow end-to-end."""
        # 1. Initialize components
        AgentRegistry()
        router = ModelRouter()

        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            store = StateStore(worktree=worktree)
            state_manager = StateManager(store=store)

            # 2. Route a task
            context = RoutingContext(
                task_type="code",
                token_estimate=1500,
                tool_count=2,
                ac_depth=2,
            )

            tier = await router.route(context)
            assert tier in [Tier.FRUGAL, Tier.STANDARD, Tier.FRONTIER]

            # 3. Create session
            session_result = await state_manager.save_session(
                session_id="sess-e2e",
                execution_id="exec-e2e",
                seed_id="seed-e2e",
                seed_goal="End-to-end test",
                acceptance_criteria=[],
                workflow_state={"tier": tier.value},
                mode=StateMode.AUTOPILOT,
            )

            assert session_result.is_ok

            # 4. Record routing result
            await router.record_result(context, tier, success=True)

            # 5. Verify state
            stats = router.get_statistics()
            assert stats["total_routes"] == 1
            assert stats["total_records"] == 1

            loaded_session = await state_manager.load_session("sess-e2e")
            assert loaded_session is not None
            assert loaded_session.workflow_state["tier"] == tier.value
