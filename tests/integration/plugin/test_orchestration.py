"""Integration tests for Plugin Orchestration.

Tests cover:
- Agent orchestration with pool
- Skill execution flow
"""

import asyncio
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from ouroboros.plugin.agents.pool import (
    AgentPool,
    AgentPoolConfig,
    TaskRequest,
    TaskResult,
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
        task_started = asyncio.Event()
        never_finishes = asyncio.Event()

        async def stalled_execute_task(*args, **kwargs):
            task_started.set()
            await never_finishes.wait()
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
        await asyncio.wait_for(task_started.wait(), timeout=0.5)

        result = await pool.get_task_result(task_id, timeout=0.5)

        assert result.success is False
        assert result.error_message == "Task cancelled"
        assert await pool.get_task_result(task_id, timeout=0) is result
        assert task_id not in pool._task_waiters

        await pool.stop()

    @pytest.mark.asyncio
    async def test_agent_pool_fans_out_terminal_result_to_concurrent_waiters(self) -> None:
        """Completion owns waiter detachment; every registered waiter gets the result."""
        pool = AgentPool(adapter=MagicMock(), config=AgentPoolConfig(min_instances=0))
        task_id = "task-concurrent-waiters"
        result = pool._task_results.get(task_id)
        assert result is None

        waiters = [
            asyncio.create_task(pool.get_task_result(task_id, timeout=0.5)) for _ in range(16)
        ]
        await asyncio.sleep(0)

        expected = TaskResult(task_id=task_id, success=False, error_message="Task cancelled")
        pool._publish_task_result(expected)

        assert await asyncio.gather(*waiters) == [expected] * len(waiters)
        assert task_id not in pool._task_waiters
        assert await pool.get_task_result(task_id, timeout=0) is expected

    @pytest.mark.asyncio
    async def test_agent_pool_timeout_cleanup_preserves_late_result(self) -> None:
        """A caller timeout relinquishes only its waiter, not the terminal result."""
        pool = AgentPool(adapter=MagicMock(), config=AgentPoolConfig(min_instances=0))
        task_id = "task-timeout-then-result"

        with pytest.raises(TimeoutError):
            await pool.get_task_result(task_id, timeout=0)

        assert task_id not in pool._task_waiters

        expected = TaskResult(task_id=task_id, success=True)
        pool._publish_task_result(expected)
        assert await pool.get_task_result(task_id, timeout=0) is expected

    @pytest.mark.asyncio
    async def test_agent_pool_completion_wins_exact_timeout_boundary(self) -> None:
        """Publishing during timeout cleanup returns the result instead of raising KeyError."""
        pool = AgentPool(adapter=MagicMock(), config=AgentPoolConfig(min_instances=0))
        task_id = "task-exact-timeout-boundary"
        expected = TaskResult(task_id=task_id, success=False, error_message="Task cancelled")

        async def publish_then_timeout(*args, **kwargs):
            pool._publish_task_result(expected)
            raise TimeoutError

        with patch(
            "ouroboros.plugin.agents.pool.asyncio.wait_for",
            side_effect=publish_then_timeout,
        ):
            result = await pool.get_task_result(task_id, timeout=0.1)

        assert result is expected
        assert task_id not in pool._task_waiters

    @pytest.mark.asyncio
    async def test_agent_pool_stop_cancels_running_and_queued_results(self) -> None:
        """Shutdown terminalizes all accepted tasks and leaves no live waiter state."""
        adapter = MagicMock()
        first_started = asyncio.Event()
        never_finishes = asyncio.Event()

        async def stalled_execute_task(*args, **kwargs):
            first_started.set()
            await never_finishes.wait()
            if False:  # pragma: no cover - keep this an async generator
                yield None

        adapter.execute_task = stalled_execute_task
        pool = AgentPool(
            adapter=adapter,
            config=AgentPoolConfig(
                min_instances=1,
                max_instances=1,
                enable_auto_scaling=False,
            ),
        )
        await pool.start()
        running_id = await pool.submit_task(agent_type="executor", prompt="running")
        queued_id = await pool.submit_task(agent_type="executor", prompt="queued")
        await asyncio.wait_for(first_started.wait(), timeout=0.5)

        result_waiters = [
            asyncio.create_task(pool.get_task_result(task_id, timeout=0.5))
            for task_id in (running_id, queued_id)
        ]
        await asyncio.sleep(0)
        queue_join = AsyncMock(wraps=pool._task_queue.join)
        with patch.object(pool._task_queue, "join", queue_join):
            await pool.stop()

        results = await asyncio.gather(*result_waiters)
        assert [result.task_id for result in results] == [running_id, queued_id]
        assert all(result.success is False for result in results)
        assert all(result.error_message == "Task cancelled" for result in results)
        assert pool._running_tasks == {}
        assert pool._pending_task_ids == set()
        assert pool._task_waiters == {}
        assert pool._task_queue.empty()
        await asyncio.wait_for(pool._task_queue.join(), timeout=0.1)
        assert pool._dispatcher_task is None
        assert pool._scaler_task is None
        assert pool._health_task is None
        queue_join.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_agent_pool_rejects_submissions_during_and_after_shutdown(self) -> None:
        """The shutdown boundary permanently closes task acceptance."""
        pool = AgentPool(adapter=MagicMock(), config=AgentPoolConfig(min_instances=0))
        await pool.start()

        stopping = asyncio.create_task(pool.stop())
        while not pool._shutdown:
            await asyncio.sleep(0)

        with pytest.raises(RuntimeError, match="cannot accept new tasks"):
            await pool.submit_task(agent_type="executor", prompt="during shutdown")

        await stopping

        with pytest.raises(RuntimeError, match="cannot accept new tasks"):
            await pool.submit_task(agent_type="executor", prompt="after shutdown")

        assert pool._pending_task_ids == set()
        assert pool._task_queue.empty()

    @pytest.mark.asyncio
    async def test_agent_pool_dispatcher_exception_settles_acquired_task(self) -> None:
        """An acquired item becomes a terminal failure if dispatch raises."""
        pool = AgentPool(
            adapter=MagicMock(),
            config=AgentPoolConfig(
                min_instances=0,
                max_instances=0,
                enable_auto_scaling=False,
            ),
        )

        with patch.object(pool, "_wait_for_agent", side_effect=RuntimeError("dispatch broke")):
            await pool.start()
            task_id = await pool.submit_task(agent_type="executor", prompt="settle me")
            result = await pool.get_task_result(task_id, timeout=0.5)

        assert result.success is False
        assert result.error_message == "Task dispatch failed: dispatch broke"
        assert task_id not in pool._pending_task_ids
        await asyncio.wait_for(pool._task_queue.join(), timeout=0.1)
        await pool.stop()

    @pytest.mark.asyncio
    async def test_agent_pool_dispatcher_timeout_settles_acquired_task(self) -> None:
        """A timeout after queue acquisition is a terminal dispatch failure."""
        pool = AgentPool(
            adapter=MagicMock(),
            config=AgentPoolConfig(
                min_instances=0,
                max_instances=0,
                enable_auto_scaling=False,
            ),
        )

        with patch.object(
            pool,
            "_wait_for_agent",
            side_effect=TimeoutError("agent lookup timed out"),
        ):
            await pool.start()
            task_id = await pool.submit_task(agent_type="executor", prompt="settle timeout")
            result = await pool.get_task_result(task_id, timeout=0.5)

        assert result.success is False
        assert result.error_message == "Task dispatch failed: agent lookup timed out"
        assert task_id not in pool._pending_task_ids
        await asyncio.wait_for(pool._task_queue.join(), timeout=0.1)
        with pytest.raises(ValueError, match=r"task_done\(\) called too many times"):
            pool._task_queue.task_done()
        await pool.stop()

    @pytest.mark.asyncio
    async def test_agent_pool_queue_poll_timeout_does_not_fabricate_task(self) -> None:
        """An empty-queue poll timeout simply starts the next poll."""
        pool = AgentPool(adapter=MagicMock(), config=AgentPoolConfig(min_instances=0))
        first_poll_timed_out = asyncio.Event()
        second_poll_started = asyncio.Event()
        never_returns = asyncio.Event()
        poll_count = 0

        async def poll_queue() -> tuple[int, TaskRequest]:
            nonlocal poll_count
            poll_count += 1
            if poll_count == 1:
                first_poll_timed_out.set()
                raise TimeoutError
            second_poll_started.set()
            await never_returns.wait()
            raise AssertionError("unreachable")

        with patch.object(pool._task_queue, "get", side_effect=poll_queue):
            await pool.start()
            await asyncio.wait_for(first_poll_timed_out.wait(), timeout=0.5)
            await asyncio.wait_for(second_poll_started.wait(), timeout=0.5)

            assert pool._dispatcher_task is not None
            assert not pool._dispatcher_task.done()
            assert pool._pending_task_ids == set()
            assert pool._task_results == {}

            await pool.stop()

        assert poll_count == 2

    @pytest.mark.asyncio
    async def test_agent_pool_stop_acknowledges_dispatcher_owned_queue_item(self) -> None:
        """Cancelling a dispatcher-held item settles its queue ownership exactly once."""
        dispatcher_owned = asyncio.Event()
        never_returns = asyncio.Event()

        async def hold_dispatcher_ownership(*args, **kwargs):
            dispatcher_owned.set()
            await never_returns.wait()

        pool = AgentPool(
            adapter=MagicMock(),
            config=AgentPoolConfig(
                min_instances=0,
                max_instances=0,
                enable_auto_scaling=False,
            ),
        )
        with patch.object(pool, "_wait_for_agent", side_effect=hold_dispatcher_ownership):
            await pool.start()
            task_id = await pool.submit_task(agent_type="executor", prompt="dispatcher-owned")
            await asyncio.wait_for(dispatcher_owned.wait(), timeout=0.5)
            await pool.stop()

        result = await pool.get_task_result(task_id, timeout=0)
        assert result.error_message == "Task cancelled"
        await asyncio.wait_for(pool._task_queue.join(), timeout=0.1)
        with pytest.raises(ValueError, match=r"task_done\(\) called too many times"):
            pool._task_queue.task_done()

    @pytest.mark.asyncio
    async def test_agent_pool_stop_acknowledges_requeued_item_without_double_counting(
        self,
    ) -> None:
        """Requeue transfers ownership before shutdown drains the replacement item."""
        no_agent_checked = asyncio.Event()

        async def return_no_agent(*args, **kwargs):
            no_agent_checked.set()
            return None

        pool = AgentPool(
            adapter=MagicMock(),
            config=AgentPoolConfig(
                min_instances=0,
                max_instances=0,
                enable_auto_scaling=False,
            ),
        )
        with patch.object(pool, "_wait_for_agent", side_effect=return_no_agent):
            await pool.start()
            task_id = await pool.submit_task(agent_type="executor", prompt="requeued")
            await asyncio.wait_for(no_agent_checked.wait(), timeout=0.5)
            for _ in range(100):
                if not pool._task_queue.empty():
                    break
                await asyncio.sleep(0)
            assert pool._task_queue.qsize() == 1
            await pool.stop()

        result = await pool.get_task_result(task_id, timeout=0)
        assert result.error_message == "Task cancelled"
        await asyncio.wait_for(pool._task_queue.join(), timeout=0.1)
        with pytest.raises(ValueError, match=r"task_done\(\) called too many times"):
            pool._task_queue.task_done()

    @pytest.mark.asyncio
    async def test_agent_pool_stop_forgets_execution_cancelled_before_first_instruction(
        self,
    ) -> None:
        """Shutdown cleanup does not depend on the execution coroutine entering its body."""
        pool = AgentPool(adapter=MagicMock(), config=AgentPoolConfig(min_instances=0))
        agent = await pool._spawn_agent("agent-prestart-cancel")
        task = TaskRequest(
            id="task-prestart-cancel",
            agent_type="executor",
            prompt="never starts",
            context={},
        )
        pool._pending_task_ids.add(task.id)
        execution = asyncio.create_task(pool._execute_task(agent, task))
        pool._track_running_task(task.id, execution)
        execution.cancel()

        await pool.stop()

        result = await pool.get_task_result(task.id, timeout=0)
        assert result.error_message == "Task cancelled"
        assert execution.cancelled()
        assert pool._running_tasks == {}
        assert pool._pending_task_ids == set()

    @pytest.mark.asyncio
    async def test_agent_pool_running_task_cleanup_is_generation_safe(self) -> None:
        """An older execution finalizer cannot discard a replacement generation."""
        adapter = MagicMock()
        first_started = asyncio.Event()
        first_gate = asyncio.Event()

        async def execute_first(*args, **kwargs):
            first_started.set()
            await first_gate.wait()
            if False:  # pragma: no cover - keep this an async generator
                yield None

        adapter.execute_task = execute_first
        pool = AgentPool(adapter=adapter, config=AgentPoolConfig(min_instances=0))
        agent = await pool._spawn_agent("agent-generation-safe")
        request = TaskRequest(
            id="task-reused-tracking-key",
            agent_type="executor",
            prompt="old generation",
            context={},
        )
        replacement_gate = asyncio.Event()
        first = asyncio.create_task(pool._execute_task(agent, request))
        await asyncio.wait_for(first_started.wait(), timeout=0.5)
        replacement = asyncio.create_task(replacement_gate.wait())
        pool._track_running_task(request.id, first)
        pool._track_running_task(request.id, replacement)

        first_gate.set()
        await first
        await asyncio.sleep(0)
        assert pool._running_tasks[request.id] is replacement

        replacement.cancel()
        await asyncio.gather(replacement, return_exceptions=True)
        await asyncio.sleep(0)
        assert request.id not in pool._running_tasks


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
