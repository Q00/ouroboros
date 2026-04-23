"""Unit tests for SerialCompoundingExecutor."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.dependency_analyzer import (
    ACNode,
    DependencyGraph,
    ExecutionStage,
    StagedExecutionPlan,
)
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
)
from ouroboros.orchestrator.serial_executor import (
    SerialCompoundingExecutor,
    linearize_execution_plan,
)


def _make_seed(*acs: str) -> Seed:
    return Seed(
        goal="Serial compounding execution",
        constraints=(),
        acceptance_criteria=acs,
        ontology_schema=OntologySchema(name="Serial", description="test"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


def _make_plan(*stages: tuple[int, ...]) -> StagedExecutionPlan:
    """Build a StagedExecutionPlan with the given stages of AC indices.

    Each stage is a tuple of AC indices. Stage N depends on stage N-1 via
    ``depends_on_stages``.
    """
    stage_objs: list[ExecutionStage] = []
    seen: set[int] = set()
    nodes: list[ACNode] = []
    for i, indices in enumerate(stages):
        stage_objs.append(
            ExecutionStage(
                index=i,
                ac_indices=tuple(indices),
                depends_on_stages=tuple(range(i)) if i > 0 else (),
            )
        )
        for ac_idx in indices:
            if ac_idx not in seen:
                seen.add(ac_idx)
                nodes.append(ACNode(index=ac_idx, content=f"AC {ac_idx}"))
    return StagedExecutionPlan(nodes=tuple(nodes), stages=tuple(stage_objs))


def _make_executor() -> SerialCompoundingExecutor:
    event_store, _ = _make_replaying_event_store()
    executor = SerialCompoundingExecutor(
        adapter=MagicMock(),
        event_store=event_store,
        console=MagicMock(),
        enable_decomposition=False,
    )
    executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
    return executor


def _make_replaying_event_store() -> tuple[AsyncMock, list[BaseEvent]]:
    event_store = AsyncMock()
    appended: list[BaseEvent] = []

    async def _append(event: BaseEvent) -> None:
        appended.append(event)

    event_store.append.side_effect = _append
    event_store.replay.side_effect = lambda *a, **k: []
    # Attach to the mock so tests can read it.
    event_store._appended = appended  # type: ignore[attr-defined]
    return event_store, appended


def _ok_result(
    ac_index: int,
    ac_content: str,
    *,
    final_message: str = "done",
    files_written: tuple[str, ...] = (),
) -> ACExecutionResult:
    messages: list[AgentMessage] = []
    for path in files_written:
        messages.append(
            AgentMessage(
                type="tool_use",
                content=f"writing {path}",
                tool_name="Write",
                data={"tool_input": {"file_path": path}},
            )
        )
    return ACExecutionResult(
        ac_index=ac_index,
        ac_content=ac_content,
        success=True,
        messages=tuple(messages),
        final_message=final_message,
        duration_seconds=0.1,
    )


def _fail_result(ac_index: int, ac_content: str, *, error: str = "boom") -> ACExecutionResult:
    return ACExecutionResult(
        ac_index=ac_index,
        ac_content=ac_content,
        success=False,
        error=error,
        outcome=ACExecutionOutcome.FAILED,
    )


class TestLinearizeExecutionPlan:
    def test_single_stage_sorted(self) -> None:
        plan = _make_plan((2, 0, 1))
        assert linearize_execution_plan(plan) == (0, 1, 2)

    def test_multi_stage_respects_stage_order(self) -> None:
        plan = _make_plan((1,), (0, 2))
        # Stage 0 before stage 1 regardless of AC index.
        assert linearize_execution_plan(plan) == (1, 0, 2)

    def test_no_duplicates(self) -> None:
        # Defense: if an AC appears twice (bad plan), it should not repeat.
        plan = _make_plan((0,), (0, 1))
        assert linearize_execution_plan(plan) == (0, 1)


class TestSerialCompoundingExecutor:
    @pytest.mark.asyncio
    async def test_two_ac_chain_ac2_sees_ac1_postmortem(self) -> None:
        """The whole point: AC 2's prompt contains AC 1's postmortem."""
        seed = _make_seed("Create user model", "Create user endpoint")
        executor = _make_executor()

        captured_overrides: list[str | None] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            captured_overrides.append(kwargs.get("context_override"))
            ac_index = int(kwargs["ac_index"])
            return _ok_result(
                ac_index,
                str(kwargs["ac_content"]),
                final_message=f"AC {ac_index + 1} done",
                files_written=(f"src/ac{ac_index}.py",),
            )

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_1",
            execution_id="exec_1",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
        )

        assert result.success_count == 2
        assert result.failure_count == 0
        assert len(captured_overrides) == 2
        # AC 1 sees an empty chain (no postmortems yet).
        assert captured_overrides[0] == ""
        # AC 2's override must reference AC 1's postmortem content.
        ac2_override = captured_overrides[1] or ""
        assert "Prior AC Postmortems" in ac2_override
        assert "Create user model" in ac2_override
        assert "src/ac0.py" in ac2_override  # from AC 1's files_modified

    @pytest.mark.asyncio
    async def test_postmortem_event_emitted_per_ac(self) -> None:
        seed = _make_seed("AC a", "AC b")
        executor = _make_executor()
        # Extract the event store we set up and confirm it collected events.
        event_store: Any = executor._event_store
        appended: list[BaseEvent] = event_store._appended

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_1",
            execution_id="exec_1",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
        )

        pm_events = [
            e for e in appended if e.type == "execution.ac.postmortem.captured"
        ]
        assert len(pm_events) == 2
        assert pm_events[0].aggregate_id == "ac_0"
        assert pm_events[0].data["status"] == "pass"
        assert pm_events[1].aggregate_id == "ac_1"

    @pytest.mark.asyncio
    async def test_fail_fast_halts_on_ac_failure(self) -> None:
        seed = _make_seed("AC 1 fails", "AC 2 never runs")
        executor = _make_executor()
        calls: list[int] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            calls.append(ac_index)
            if ac_index == 0:
                return _fail_result(0, str(kwargs["ac_content"]), error="missing dep")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_1",
            execution_id="exec_1",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )

        # AC 0 ran, AC 1 was blocked — not executed.
        assert calls == [0]
        assert result.failure_count == 1
        assert result.blocked_count == 1
        assert result.results[0].success is False
        assert result.results[1].outcome == ACExecutionOutcome.BLOCKED
        assert result.results[1].error and "halted" in result.results[1].error

    @pytest.mark.asyncio
    async def test_fail_forward_continues_past_failure(self) -> None:
        seed = _make_seed("AC 1 fails", "AC 2 still runs")
        executor = _make_executor()
        calls: list[int] = []
        captured_overrides: list[str | None] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            calls.append(ac_index)
            captured_overrides.append(kwargs.get("context_override"))
            if ac_index == 0:
                return _fail_result(0, str(kwargs["ac_content"]), error="timeout")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_1",
            execution_id="exec_1",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=False,
        )

        # Both ran; AC 2 saw AC 1's failed postmortem.
        assert calls == [0, 1]
        assert result.failure_count == 1
        assert result.success_count == 1
        ac2_override = captured_overrides[1] or ""
        assert "[fail]" in ac2_override
        assert "timeout" in ac2_override  # gotcha from failed AC surfaces

    @pytest.mark.asyncio
    async def test_exception_captured_as_failed_postmortem(self) -> None:
        """An unexpected exception in _execute_single_ac does not crash the loop."""
        seed = _make_seed("AC 1 raises", "AC 2 blocked")
        executor = _make_executor()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            raise RuntimeError("adapter exploded")

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_1",
            execution_id="exec_1",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )
        assert result.failure_count == 1
        assert "adapter exploded" in (result.results[0].error or "")

    @pytest.mark.asyncio
    async def test_dependency_graph_used_when_plan_absent(self) -> None:
        seed = _make_seed("AC 1", "AC 2")
        executor = _make_executor()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content="AC 1"),
                ACNode(index=1, content="AC 2", depends_on=(0,)),
            ),
            execution_levels=((0,), (1,)),
        )
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_1",
            execution_id="exec_1",
            tools=[],
            system_prompt="SYSTEM",
            dependency_graph=graph,
        )
        assert result.success_count == 2

    @pytest.mark.asyncio
    async def test_missing_plan_and_graph_raises(self) -> None:
        executor = _make_executor()
        with pytest.raises(ValueError, match="execution_plan is required"):
            await executor.execute_serial(
                seed=_make_seed("AC 1"),
                session_id="sess_1",
                execution_id="exec_1",
                tools=[],
                system_prompt="SYSTEM",
            )

    @pytest.mark.asyncio
    async def test_invariants_accumulate_across_chain(self) -> None:
        """When postmortems carry invariants, later ACs see the cumulative list."""
        seed = _make_seed("AC a", "AC b", "AC c")
        executor = _make_executor()
        captured_overrides: list[str] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            captured_overrides.append(kwargs.get("context_override") or "")
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        # We can't set invariants from the executor directly (phase 1
        # doesn't populate them), but we CAN verify the chain plumbing
        # passes AC 1's summary data into AC 3's override.
        plan = _make_plan((0,), (1,), (2,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_1",
            execution_id="exec_1",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
        )

        # AC 3 must see references to AC 1 and AC 2 (chain grows).
        assert "AC a" in captured_overrides[2]
        assert "AC b" in captured_overrides[2]
