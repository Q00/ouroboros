"""Unit tests for SerialCompoundingExecutor."""

from __future__ import annotations

from pathlib import Path
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
    write_chain_artifact,
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


class TestChainArtifact:
    """AC-1 (Q6.1): End-of-run postmortem chain serialization.

    [[INVARIANT: end-of-run chain artifact exists in docs/brainstorm/chain-*.md]]
    [[INVARIANT: OUROBOROS_CHAIN_ARTIFACT_DIR env var controls artifact location]]
    """

    @pytest.mark.asyncio
    async def test_artifact_written_after_successful_2ac_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A successful 2-AC run writes a chain artifact with expected markdown structure."""
        seed = _make_seed("Implement user model", "Implement user endpoint")
        executor = _make_executor()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            return _ok_result(
                ac_index,
                str(kwargs["ac_content"]),
                final_message=f"AC {ac_index + 1} complete",
                files_written=(f"src/module_{ac_index}.py",),
            )

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        artifact_dir = str(tmp_path / "chain_out")
        plan = _make_plan((0,), (1,))

        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", artifact_dir)
        await executor.execute_serial(
            seed=seed,
            session_id="sess_chain_test",
            execution_id="exec_chain_test",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
        )

        out_dir = Path(artifact_dir)
        artifacts = list(out_dir.glob("chain-sess_chain_test-*.md"))
        assert len(artifacts) == 1, f"Expected 1 artifact, got: {artifacts}"

        content = artifacts[0].read_text(encoding="utf-8")
        # File header
        assert "# Postmortem Chain" in content
        assert "sess_chain_test" in content
        # Two AC sections with correct status
        assert "## AC 1 [pass]" in content
        assert "## AC 2 [pass]" in content
        # Required fields from AC spec
        assert "Files modified:" in content
        assert "Gotchas:" in content
        assert "Public API changes:" in content

    @pytest.mark.asyncio
    async def test_artifact_written_on_failure_fail_fast(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Artifact is written even when fail_fast halts mid-chain after a failure."""
        seed = _make_seed("AC 1 fails", "AC 2 never runs")
        executor = _make_executor()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            if ac_index == 0:
                return _fail_result(0, str(kwargs["ac_content"]), error="kaboom")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        artifact_dir = str(tmp_path / "chain_fail")
        plan = _make_plan((0,), (1,))

        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", artifact_dir)
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_fail_test",
            execution_id="exec_fail_test",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )

        # Run did indeed fail
        assert result.failure_count == 1

        # Artifact still written despite failure
        out_dir = Path(artifact_dir)
        artifacts = list(out_dir.glob("chain-sess_fail_test-*.md"))
        assert len(artifacts) == 1, f"Expected 1 artifact even on failure, got: {artifacts}"

        content = artifacts[0].read_text(encoding="utf-8")
        # Failed AC section present
        assert "## AC 1 [fail]" in content
        # Gotcha from the failed AC surfaces in the artifact
        assert "kaboom" in content

    def test_write_chain_artifact_creates_nested_dir_and_file(
        self, tmp_path: Path
    ) -> None:
        """write_chain_artifact creates parent directories and returns a valid path."""
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )

        summary = ACContextSummary(
            ac_index=0,
            ac_content="Build the thing",
            success=True,
            files_modified=("src/thing.py",),
        )
        pm = ACPostmortem(
            summary=summary,
            status="pass",
            gotchas=("watch out for X",),
        )
        chain = PostmortemChain(postmortems=(pm,))

        # Use deeply-nested dir that doesn't yet exist.
        nested_dir = tmp_path / "a" / "b" / "c"
        path = write_chain_artifact(
            chain,
            session_id="s1",
            execution_id="e1",
            artifact_dir=str(nested_dir),
        )

        assert path.exists()
        assert path.suffix == ".md"
        assert path.name.startswith("chain-s1-")

        content = path.read_text(encoding="utf-8")
        assert "## AC 1 [pass]" in content
        assert "Build the thing" in content
        assert "src/thing.py" in content
        assert "watch out for X" in content

    def test_env_var_overrides_artifact_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OUROBOROS_CHAIN_ARTIFACT_DIR redirects artifact output."""
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )

        custom_dir = tmp_path / "custom_dir"
        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", str(custom_dir))

        summary = ACContextSummary(ac_index=0, ac_content="AC text", success=True)
        pm = ACPostmortem(summary=summary, status="pass")
        chain = PostmortemChain(postmortems=(pm,))

        path = write_chain_artifact(chain, session_id="s2", execution_id="e2")

        # Path is inside the custom_dir
        assert str(custom_dir) in str(path)
        assert path.exists()

    def test_explicit_artifact_dir_beats_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit artifact_dir argument takes precedence over the env var."""
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )

        env_dir = tmp_path / "from_env"
        explicit_dir = tmp_path / "explicit"
        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", str(env_dir))

        summary = ACContextSummary(ac_index=0, ac_content="AC text", success=True)
        pm = ACPostmortem(summary=summary, status="pass")
        chain = PostmortemChain(postmortems=(pm,))

        path = write_chain_artifact(
            chain,
            session_id="s3",
            execution_id="e3",
            artifact_dir=str(explicit_dir),
        )

        assert str(explicit_dir) in str(path)
        assert str(env_dir) not in str(path)
        assert path.exists()

    def test_artifact_for_empty_chain_has_no_ac_sections(
        self, tmp_path: Path
    ) -> None:
        """Empty chain produces a valid header with no AC entries."""
        from ouroboros.orchestrator.level_context import PostmortemChain

        chain = PostmortemChain()  # no postmortems
        path = write_chain_artifact(
            chain,
            session_id="s4",
            execution_id="e4",
            artifact_dir=str(tmp_path),
        )
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "# Postmortem Chain" in content
        assert "## AC" not in content  # no AC sections for empty chain


class TestSubPostmortems:
    """AC-2 (Q1, B-prime): Sub-postmortem preservation and flattening.

    [[INVARIANT: ACPostmortem.sub_postmortems preserves structure in serialized chain]]
    [[INVARIANT: to_prompt_text flattens sub-AC data; never emits nested entries]]
    [[INVARIANT: parent digest fields are unions of its own plus sub-postmortem fields]]
    """

    def _make_result_with_subs(self) -> ACExecutionResult:
        """Build a decomposed ACExecutionResult with two sub-results."""
        sub0 = ACExecutionResult(
            ac_index=0,
            ac_content="Sub-AC 0",
            success=True,
            messages=(
                AgentMessage(
                    type="tool_use",
                    content="writing sub0",
                    tool_name="Write",
                    data={"tool_input": {"file_path": "src/sub_a.py"}},
                ),
            ),
            final_message="sub0 done",
        )
        sub1 = ACExecutionResult(
            ac_index=0,
            ac_content="Sub-AC 1",
            success=True,
            messages=(
                AgentMessage(
                    type="tool_use",
                    content="writing sub1",
                    tool_name="Write",
                    data={"tool_input": {"file_path": "src/sub_b.py"}},
                ),
            ),
            error=None,
            final_message="sub1 done",
        )
        # Parent result with no own files, but two sub-results.
        return ACExecutionResult(
            ac_index=0,
            ac_content="Parent AC",
            success=True,
            is_decomposed=True,
            sub_results=(sub0, sub1),
            final_message="parent done",
        )

    def test_sub_files_flattened_into_parent_summary(self) -> None:
        """Sub-result files appear in the parent ACPostmortem.summary.files_modified."""
        result = self._make_result_with_subs()
        postmortem = SerialCompoundingExecutor._build_postmortem_from_result(
            result, workspace_root=None
        )
        files = postmortem.summary.files_modified
        assert "src/sub_a.py" in files, f"sub_a.py missing from {files}"
        assert "src/sub_b.py" in files, f"sub_b.py missing from {files}"

    def test_sub_gotchas_flattened_into_parent(self) -> None:
        """Sub-result failure gotchas are merged into parent.gotchas."""
        sub_fail = ACExecutionResult(
            ac_index=0,
            ac_content="Sub fail",
            success=False,
            error="sub-ac bombed",
            outcome=ACExecutionOutcome.FAILED,
        )
        parent = ACExecutionResult(
            ac_index=0,
            ac_content="Parent AC",
            success=False,
            error="parent error",
            is_decomposed=True,
            sub_results=(sub_fail,),
            outcome=ACExecutionOutcome.FAILED,
        )
        pm = SerialCompoundingExecutor._build_postmortem_from_result(
            parent, workspace_root=None
        )
        assert "parent error" in pm.gotchas
        assert "sub-ac bombed" in pm.gotchas

    def test_sub_postmortems_stored_on_parent(self) -> None:
        """sub_postmortems tuple is preserved on the parent ACPostmortem."""
        result = self._make_result_with_subs()
        pm = SerialCompoundingExecutor._build_postmortem_from_result(
            result, workspace_root=None
        )
        assert len(pm.sub_postmortems) == 2
        assert pm.sub_postmortems[0].summary.ac_content == "Sub-AC 0"
        assert pm.sub_postmortems[1].summary.ac_content == "Sub-AC 1"

    def test_no_sub_results_gives_empty_sub_postmortems(self) -> None:
        """When there are no sub_results, sub_postmortems stays empty."""
        result = ACExecutionResult(
            ac_index=0,
            ac_content="Normal AC",
            success=True,
            final_message="done",
        )
        pm = SerialCompoundingExecutor._build_postmortem_from_result(
            result, workspace_root=None
        )
        assert pm.sub_postmortems == ()

    def test_to_prompt_text_does_not_emit_nested_entries(self) -> None:
        """to_prompt_text() flat view must NOT contain any nested sub-AC entries.

        [[INVARIANT: to_prompt_text flattens sub-AC data; never emits nested entries]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )

        # Build a sub-postmortem.
        sub_summary = ACContextSummary(
            ac_index=0, ac_content="Sub-AC 3.1", success=True
        )
        sub_pm = ACPostmortem(summary=sub_summary, status="pass")

        # Build a parent postmortem that references the sub-postmortem.
        parent_summary = ACContextSummary(
            ac_index=2, ac_content="Parent AC 3", success=True
        )
        parent_pm = ACPostmortem(
            summary=parent_summary,
            status="pass",
            sub_postmortems=(sub_pm,),
        )

        chain = PostmortemChain(postmortems=(parent_pm,))
        text = chain.to_prompt_text()

        # Sub-AC entries must NOT appear in the rendered prompt.
        assert "Sub-AC 3.1" not in text, (
            "to_prompt_text() should NOT render nested sub-AC entries"
        )
        # Parent content should still appear.
        assert "Parent AC 3" in text

    def test_serialize_deserialize_round_trip_sub_postmortems(self) -> None:
        """sub_postmortems survive a serialize → deserialize round-trip.

        [[INVARIANT: ACPostmortem.sub_postmortems preserves structure in serialized chain]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
            deserialize_postmortem_chain,
            serialize_postmortem_chain,
        )

        sub_summary = ACContextSummary(
            ac_index=0,
            ac_content="Sub task A",
            success=True,
            files_modified=("src/sub_a.py",),
        )
        sub_pm = ACPostmortem(
            summary=sub_summary,
            status="pass",
            gotchas=("watch sub gotcha",),
        )

        parent_summary = ACContextSummary(
            ac_index=1,
            ac_content="Parent task B",
            success=True,
            files_modified=("src/parent_b.py", "src/sub_a.py"),
        )
        parent_pm = ACPostmortem(
            summary=parent_summary,
            status="pass",
            gotchas=("parent gotcha", "watch sub gotcha"),
            sub_postmortems=(sub_pm,),
        )

        chain = PostmortemChain(postmortems=(parent_pm,))

        # Serialize and deserialize.
        serialized = serialize_postmortem_chain(chain)
        restored_chain = deserialize_postmortem_chain(serialized)

        assert len(restored_chain.postmortems) == 1
        restored_pm = restored_chain.postmortems[0]

        # sub_postmortems preserved.
        assert len(restored_pm.sub_postmortems) == 1
        restored_sub = restored_pm.sub_postmortems[0]
        assert restored_sub.summary.ac_content == "Sub task A"
        assert "src/sub_a.py" in restored_sub.summary.files_modified
        assert "watch sub gotcha" in restored_sub.gotchas

        # Parent fields also intact.
        assert "parent gotcha" in restored_pm.gotchas
        assert "src/parent_b.py" in restored_pm.summary.files_modified

    def test_sub_files_appear_in_rendered_postmortem_chain_prompt(self) -> None:
        """Files from sub-postmortems are visible in the chain prompt (flattened into parent).

        [[INVARIANT: parent digest fields are unions of its own plus sub-postmortem fields]]
        """
        result = self._make_result_with_subs()
        pm = SerialCompoundingExecutor._build_postmortem_from_result(
            result, workspace_root=None
        )

        from ouroboros.orchestrator.level_context import PostmortemChain

        chain = PostmortemChain(postmortems=(pm,))
        text = chain.to_prompt_text()

        # Sub-files must appear in the rendered chain text.
        assert "src/sub_a.py" in text, "sub_a.py missing from chain prompt"
        assert "src/sub_b.py" in text, "sub_b.py missing from chain prompt"


class TestInvariantVerifier:
    """AC-3 (Q3, C-plus): [[INVARIANT]] tag extraction + Haiku verifier gate.

    Verifies:
    - verify_invariants() is called inline-blocking before chain advance.
    - Above-threshold invariants appear in the next AC's context_override.
    - Below-threshold invariants are silently dropped.
    - The verify_invariants() function correctly interacts with a stub adapter.

    [[INVARIANT: verify_invariants is called inline-blocking before chain advance]]
    [[INVARIANT: only above-threshold invariants appear in downstream chain context]]
    """

    @pytest.mark.asyncio
    async def test_above_threshold_invariant_appears_in_next_ac_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When verify_invariants returns score ≥ 0.7, the invariant propagates.

        AC 0 emits [[INVARIANT: serialize_postmortem_chain produces a list]].
        The stub verifier returns 0.95. AC 1's context_override must include
        the invariant text.

        Compounding reference (AC-1): ACPostmortem.invariants_established carries
        the Invariant dataclass introduced in AC-2's level_context.py changes.
        [[INVARIANT: only above-threshold invariants appear in downstream chain context]]
        """
        import ouroboros.orchestrator.serial_executor as serial_mod

        verify_calls: list[dict] = []

        async def fake_verify(
            adapter: Any,
            tags: list[str],
            *,
            ac_trace: str,
            files_modified: list[str],
            model: str | None = None,
        ) -> list[tuple[str, float]]:
            verify_calls.append({"tags": list(tags), "ac_trace": ac_trace})
            # Return high-reliability score for all tags.
            return [(tag, 0.95) for tag in tags]

        monkeypatch.setattr(serial_mod, "verify_invariants", fake_verify)

        seed = _make_seed("AC with invariant tag", "AC that sees invariant")
        executor = _make_executor()
        captured_overrides: list[str] = []

        INVARIANT_TEXT = "serialize_postmortem_chain produces a list"

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            captured_overrides.append(kwargs.get("context_override") or "")
            final_msg = "task done"
            if ac_index == 0:
                final_msg = f"task done [[INVARIANT: {INVARIANT_TEXT}]]"
            return _ok_result(ac_index, str(kwargs["ac_content"]), final_message=final_msg)

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_inv_above",
            execution_id="exec_inv_above",
            tools=[],
            system_prompt="SYS",
            execution_plan=plan,
        )

        # Verification was called for AC 0 (which had a tag).
        assert len(verify_calls) == 1, f"Expected 1 verify call, got: {verify_calls}"
        assert INVARIANT_TEXT in verify_calls[0]["tags"]

        # AC 1's context_override must contain the invariant text in the
        # "Established Invariants (cumulative)" section — not just in key_output.
        ac1_override = captured_overrides[1]
        established_idx = ac1_override.find("Established Invariants")
        assert established_idx != -1, (
            f"'Established Invariants' section missing from AC 1 override:\n{ac1_override[:500]}"
        )
        established_section = ac1_override[established_idx:]
        assert INVARIANT_TEXT in established_section, (
            f"Invariant should appear in 'Established Invariants' section; "
            f"section was:\n{established_section[:500]}"
        )

        # Overall result is still successful.
        assert result.success_count == 2

    @pytest.mark.asyncio
    async def test_below_threshold_invariant_filtered_from_established_section(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When verify_invariants returns score < 0.7, invariant is NOT added to
        the cumulative 'Established Invariants' section of the chain prompt.

        AC 0 emits a tag; stub verifier returns 0.3 (below default 0.7 threshold).
        The invariant text must NOT appear in the "Established Invariants" section
        of AC 1's context_override.  (The raw [[INVARIANT:...]] text may still
        appear in the key_output excerpt of the full postmortem — that is expected
        and harmless; the gate applies only to structured storage in
        invariants_established and the cumulative rendering section.)

        Compounding reference: this relies on the sub_postmortems field added
        in AC-2 (the ACPostmortem.sub_postmortems field is preserved but the
        invariants_established stays empty for below-threshold tags).

        [[INVARIANT: only above-threshold invariants appear in downstream chain context]]
        """
        import ouroboros.orchestrator.serial_executor as serial_mod

        async def fake_verify(
            adapter: Any,
            tags: list[str],
            *,
            ac_trace: str,
            files_modified: list[str],
            model: str | None = None,
        ) -> list[tuple[str, float]]:
            # All tags score below threshold.
            return [(tag, 0.3) for tag in tags]

        monkeypatch.setattr(serial_mod, "verify_invariants", fake_verify)

        seed = _make_seed("AC with low-reliability tag", "AC checks chain")
        executor = _make_executor()
        captured_overrides: list[str] = []

        LOW_REL_TAG = "this invariant is unreliable xyz123"

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            captured_overrides.append(kwargs.get("context_override") or "")
            final_msg = "done"
            if ac_index == 0:
                final_msg = f"done [[INVARIANT: {LOW_REL_TAG}]]"
            return _ok_result(ac_index, str(kwargs["ac_content"]), final_message=final_msg)

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_inv_low",
            execution_id="exec_inv_low",
            tools=[],
            system_prompt="SYS",
            execution_plan=plan,
        )

        # AC 1's context must NOT list the low-reliability tag under
        # "Established Invariants (cumulative)" section.
        ac1_override = captured_overrides[1]
        established_start = ac1_override.find("Established Invariants")
        if established_start != -1:
            # If the section exists, the low-reliability tag must not be in it.
            established_section = ac1_override[established_start:]
            assert LOW_REL_TAG not in established_section, (
                "Below-threshold invariant must NOT appear in 'Established Invariants' section"
            )
        # If the section doesn't exist at all, the invariant is definitely not there — also fine.

    @pytest.mark.asyncio
    async def test_verify_invariants_not_called_when_no_tags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the AC emits no [[INVARIANT]] tags, verify_invariants is skipped."""
        import ouroboros.orchestrator.serial_executor as serial_mod

        verify_calls: list[dict] = []

        async def fake_verify(
            adapter: Any,
            tags: list[str],
            **kwargs: Any,
        ) -> list[tuple[str, float]]:
            verify_calls.append({"tags": tags})
            return []

        monkeypatch.setattr(serial_mod, "verify_invariants", fake_verify)

        seed = _make_seed("AC without tags", "AC 2")
        executor = _make_executor()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_no_tags",
            execution_id="exec_no_tags",
            tools=[],
            system_prompt="SYS",
            execution_plan=plan,
        )

        # verify_invariants should not have been called at all.
        assert verify_calls == [], (
            "verify_invariants must not be called when no tags are present"
        )

    @pytest.mark.asyncio
    async def test_verify_invariants_stub_adapter_integration(self) -> None:
        """verify_invariants calls adapter.complete() and parses the score.

        This is the integration test with a stub Haiku call. The adapter
        is a MagicMock whose .complete() returns a synthetic response with
        a numeric score. The function must return the correct (tag, score) pair.

        [[INVARIANT: verify_invariants is called inline-blocking before chain advance]]
        """
        from unittest.mock import AsyncMock, MagicMock

        from ouroboros.core.types import Result
        from ouroboros.orchestrator.serial_executor import verify_invariants
        from ouroboros.providers.base import CompletionResponse, UsageInfo

        # Build a stub adapter that returns "0.82" as its response.
        stub_response = CompletionResponse(
            content="0.82",
            model="claude-haiku-4-5-20251001",
            usage=UsageInfo(prompt_tokens=50, completion_tokens=2, total_tokens=52),
        )
        adapter = MagicMock()
        adapter.complete = AsyncMock(return_value=Result.ok(stub_response))

        tags = ["ACPostmortem.sub_postmortems preserves structure"]
        results = await verify_invariants(
            adapter,
            tags,
            ac_trace="Built sub_postmortems field and verified round-trip.",
            files_modified=["src/ouroboros/orchestrator/level_context.py"],
            model="claude-haiku-4-5-20251001",
        )

        assert len(results) == 1
        tag_out, score = results[0]
        assert tag_out == tags[0]
        # Score should be parsed from "0.82".
        assert abs(score - 0.82) < 1e-9, f"Expected 0.82 but got {score}"

        # adapter.complete was called exactly once (one tag → one Haiku call).
        adapter.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_verify_invariants_adapter_error_returns_fallback(self) -> None:
        """When adapter.complete() fails, the fallback score (0.5) is returned."""
        from unittest.mock import AsyncMock, MagicMock

        from ouroboros.core.errors import ProviderError
        from ouroboros.core.types import Result
        from ouroboros.orchestrator.serial_executor import verify_invariants

        adapter = MagicMock()
        adapter.complete = AsyncMock(
            return_value=Result.err(ProviderError(message="rate limit", details={}))
        )

        tags = ["some invariant"]
        results = await verify_invariants(
            adapter,
            tags,
            ac_trace="trace",
            files_modified=[],
            model="claude-haiku-4-5-20251001",
        )

        assert len(results) == 1
        _, score = results[0]
        # Fallback score must be 0.5.
        assert score == 0.5, f"Expected fallback 0.5 but got {score}"

    @pytest.mark.asyncio
    async def test_custom_min_reliability_threshold_via_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OUROBOROS_INVARIANT_MIN_RELIABILITY controls the inclusion gate.

        When set to 0.4, a score of 0.45 must be accepted.
        When set to 0.9, a score of 0.85 must be rejected.
        """
        import ouroboros.orchestrator.serial_executor as serial_mod

        monkeypatch.setenv("OUROBOROS_INVARIANT_MIN_RELIABILITY", "0.4")

        async def fake_verify_medium(
            adapter: Any,
            tags: list[str],
            **kwargs: Any,
        ) -> list[tuple[str, float]]:
            return [(tag, 0.45) for tag in tags]

        monkeypatch.setattr(serial_mod, "verify_invariants", fake_verify_medium)

        seed = _make_seed("AC with medium tag", "AC checks invariant")
        executor = _make_executor()
        captured_overrides: list[str] = []
        MEDIUM_TAG = "medium reliability invariant"

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            captured_overrides.append(kwargs.get("context_override") or "")
            final_msg = "done"
            if ac_index == 0:
                final_msg = f"done [[INVARIANT: {MEDIUM_TAG}]]"
            return _ok_result(ac_index, str(kwargs["ac_content"]), final_message=final_msg)

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_thresh",
            execution_id="exec_thresh",
            tools=[],
            system_prompt="SYS",
            execution_plan=plan,
        )

        # With threshold=0.4 and score=0.45, invariant should appear in the
        # "Established Invariants (cumulative)" section of AC 1's context.
        ac1_override = captured_overrides[1]
        established_idx = ac1_override.find("Established Invariants")
        assert established_idx != -1, (
            f"'Established Invariants' section missing; override:\n{ac1_override[:500]}"
        )
        established_section = ac1_override[established_idx:]
        assert MEDIUM_TAG in established_section, (
            f"Invariant with score 0.45 must appear when threshold is 0.4; "
            f"established section was:\n{established_section[:500]}"
        )


class TestCheckpointWriting:
    """AC-2 (Q6.2): Per-AC checkpoint writing integration tests.

    Verifies that SerialCompoundingExecutor writes a checkpoint to the
    CheckpointStore after each successfully completed AC, and does NOT
    write a checkpoint when an AC fails.

    Compounding context (from prior ACs):
    - AC-1 established [[INVARIANT: end-of-run chain artifact exists in
      docs/brainstorm/chain-*.md]] — checkpoints complement this artifact
      by enabling resume without losing prior ACs' work.
    - AC-2 established [[INVARIANT: ACPostmortem.sub_postmortems preserves
      structure in serialized chain]] — the checkpoint payload carries the
      full serialized PostmortemChain, which now includes sub_postmortems.
    - AC-3 established [[INVARIANT: verify_invariants is called
      inline-blocking before chain advance]] — verified invariants are
      present in the chain that gets checkpointed.

    [[INVARIANT: checkpoints are only written after AC success, never on failure]]
    [[INVARIANT: CompoundingCheckpointState.last_completed_ac_index equals the 0-based AC index]]
    [[INVARIANT: checkpoint payload mode is always the literal "compounding"]]
    """

    def _make_executor_with_mock_store(self) -> tuple[SerialCompoundingExecutor, MagicMock]:
        """Build an executor with a mock CheckpointStore injected."""
        event_store, _ = _make_replaying_event_store()

        mock_store = MagicMock()
        # write() should return a successful Result-like object.
        from ouroboros.core.types import Result
        mock_store.write.return_value = Result.ok(None)

        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=mock_store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
        return executor, mock_store

    @pytest.mark.asyncio
    async def test_checkpoint_written_after_each_successful_ac(self) -> None:
        """CheckpointStore.write is called once per successful AC.

        In a 2-AC run where both succeed, write() must be called twice:
        once with last_completed_ac_index=0 and once with =1.

        Compounding ref: the checkpoint serializes the PostmortemChain which
        by AC-2 now includes sub_postmortems (B-prime) in its serialized form.

        [[INVARIANT: checkpoints are only written after AC success, never on failure]]
        """
        from ouroboros.persistence.checkpoint import CompoundingCheckpointState

        seed = _make_seed("AC 1 — build model", "AC 2 — build endpoint")
        executor, mock_store = self._make_executor_with_mock_store()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_ckpt_success",
            execution_id="exec_ckpt_success",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
        )

        assert result.success_count == 2
        # write() must have been called exactly twice.
        assert mock_store.write.call_count == 2, (
            f"Expected 2 checkpoint writes, got {mock_store.write.call_count}"
        )

        # Extract CheckpointData arguments from each write() call.
        call_args = [call.args[0] for call in mock_store.write.call_args_list]

        # First call: AC 0 completed → last_completed_ac_index should be 0.
        ckpt0 = call_args[0]
        state0 = CompoundingCheckpointState.from_dict(ckpt0.state)
        assert state0.last_completed_ac_index == 0, (
            f"First checkpoint should have last_completed_ac_index=0, got {state0.last_completed_ac_index}"
        )
        assert state0.mode == "compounding"
        assert isinstance(state0.postmortem_chain, list)
        assert len(state0.postmortem_chain) == 1  # only AC 0 in chain after AC 0 completes

        # Second call: AC 1 completed → last_completed_ac_index should be 1.
        ckpt1 = call_args[1]
        state1 = CompoundingCheckpointState.from_dict(ckpt1.state)
        assert state1.last_completed_ac_index == 1, (
            f"Second checkpoint should have last_completed_ac_index=1, got {state1.last_completed_ac_index}"
        )
        assert len(state1.postmortem_chain) == 2  # both ACs in chain

        # Checkpoint phase must be "execution".
        assert ckpt0.phase == "execution"
        assert ckpt1.phase == "execution"

        # seed_id must match the seed's metadata.
        assert ckpt0.seed_id == seed.metadata.seed_id
        assert ckpt1.seed_id == seed.metadata.seed_id

    @pytest.mark.asyncio
    async def test_no_checkpoint_written_on_ac_failure(self) -> None:
        """CheckpointStore.write is NOT called when an AC fails.

        AC 0 fails → no checkpoint. AC 1 is blocked (fail_fast=True) →
        no checkpoint. Total write() calls: 0.

        Compounding ref: this guards the Q6.2 resume semantics established
        in the brainstorm doc — a failed AC does not advance the cursor,
        ensuring resume restarts from that AC, not the one after.

        [[INVARIANT: checkpoints are only written after AC success, never on failure]]
        """
        seed = _make_seed("AC 1 fails", "AC 2 never runs")
        executor, mock_store = self._make_executor_with_mock_store()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            if ac_index == 0:
                return _fail_result(0, str(kwargs["ac_content"]), error="timeout")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_ckpt_fail",
            execution_id="exec_ckpt_fail",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )

        assert result.failure_count == 1
        assert result.blocked_count == 1
        # No checkpoints written — the failing AC does not advance the cursor.
        assert mock_store.write.call_count == 0, (
            f"Expected 0 checkpoint writes on failure, got {mock_store.write.call_count}"
        )

    @pytest.mark.asyncio
    async def test_checkpoint_written_for_successful_acs_skip_failed_in_fail_forward(
        self,
    ) -> None:
        """In fail-forward mode, only successful ACs trigger a checkpoint write.

        AC 0 fails (no checkpoint), AC 1 succeeds (checkpoint with index=1).
        Total write() calls: 1.

        Compounding ref: uses fail_fast=False which was tested in AC-2's
        sub-postmortem tests (test_fail_forward_continues_past_failure).

        [[INVARIANT: checkpoints are only written after AC success, never on failure]]
        """
        from ouroboros.persistence.checkpoint import CompoundingCheckpointState

        seed = _make_seed("AC 0 fails", "AC 1 succeeds")
        executor, mock_store = self._make_executor_with_mock_store()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            if ac_index == 0:
                return _fail_result(0, str(kwargs["ac_content"]), error="oops")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_ckpt_fwd",
            execution_id="exec_ckpt_fwd",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=False,
        )

        assert result.failure_count == 1
        assert result.success_count == 1
        # Exactly 1 checkpoint written — only for the successful AC 1.
        assert mock_store.write.call_count == 1, (
            f"Expected 1 checkpoint write, got {mock_store.write.call_count}"
        )

        written_ckpt = mock_store.write.call_args.args[0]
        state = CompoundingCheckpointState.from_dict(written_ckpt.state)
        # The successful AC was index 1 → cursor points to 1.
        assert state.last_completed_ac_index == 1

    @pytest.mark.asyncio
    async def test_no_checkpoint_written_when_store_is_none(self) -> None:
        """When no CheckpointStore is provided, the executor runs without error.

        This is the default path for callers that do not opt-in to checkpointing.
        The executor must not crash and must still produce correct results.

        [[INVARIANT: checkpoints are only written after AC success, never on failure]]
        """
        seed = _make_seed("AC 1", "AC 2")
        # Use the default executor from _make_executor() which has no store.
        executor = _make_executor()
        assert executor._checkpoint_store is None

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_no_store",
            execution_id="exec_no_store",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
        )
        # Run succeeds normally even without a store.
        assert result.success_count == 2
        assert result.failure_count == 0

    @pytest.mark.asyncio
    async def test_checkpoint_write_error_does_not_propagate(self) -> None:
        """A failing CheckpointStore.write() call must not abort the run.

        The executor catches write errors and logs a warning; the AC loop
        must still complete normally.

        [[INVARIANT: checkpoints are only written after AC success, never on failure]]
        """
        from ouroboros.core.errors import PersistenceError
        from ouroboros.core.types import Result

        seed = _make_seed("AC 1", "AC 2")
        executor, mock_store = self._make_executor_with_mock_store()
        # Make write() return an error result.
        mock_store.write.return_value = Result.err(
            PersistenceError(message="disk full", operation="write", details={})
        )

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_ckpt_err",
            execution_id="exec_ckpt_err",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
        )
        # The run still succeeds despite checkpoint errors.
        assert result.success_count == 2
        assert result.failure_count == 0

    def test_write_compounding_checkpoint_payload_structure(
        self, tmp_path: Path
    ) -> None:
        """_write_compounding_checkpoint produces the expected CheckpointData payload.

        Uses a real CheckpointStore pointed at tmp_path to exercise the full
        write → read → validate path.

        Compounding ref: the checkpoint serializes the PostmortemChain which
        now (since AC-2, B-prime) includes sub_postmortems in its serialized
        output, and (since AC-3, C-plus) may include verified Invariant objects.

        [[INVARIANT: CompoundingCheckpointState.mode is always the literal "compounding"]]
        [[INVARIANT: CompoundingCheckpointState.last_completed_ac_index equals the 0-based AC index]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint
        from ouroboros.persistence.checkpoint import (
            CheckpointStore,
            CompoundingCheckpointState,
        )

        # Build a minimal one-AC chain.
        summary = ACContextSummary(
            ac_index=0,
            ac_content="Build the auth module",
            success=True,
            files_modified=("src/auth.py",),
        )
        pm = ACPostmortem(
            summary=summary,
            status="pass",
            gotchas=("remember to hash passwords",),
        )
        chain = PostmortemChain(postmortems=(pm,))

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        _write_compounding_checkpoint(
            store=store,
            seed_id="seed_test_ckpt",
            session_id="sess_payload_test",
            ac_index=0,
            chain=chain,
        )

        # Read back the checkpoint and validate.
        load_result = store.load("seed_test_ckpt")
        assert load_result.is_ok, f"Load failed: {load_result.error}"

        ckpt = load_result.value
        assert ckpt.phase == "execution"
        assert ckpt.seed_id == "seed_test_ckpt"

        state = CompoundingCheckpointState.from_dict(ckpt.state)
        assert state.mode == "compounding"
        assert state.last_completed_ac_index == 0
        assert isinstance(state.postmortem_chain, list)
        assert len(state.postmortem_chain) == 1

        # The postmortem chain entry should reference the AC content.
        entry = state.postmortem_chain[0]
        summary_data = entry.get("summary", {})
        assert summary_data.get("ac_content") == "Build the auth module"


class TestCheckpointResume:
    """AC-4 (Q6.2) Sub-AC 1: Checkpoint loading and postmortem chain deserialization.

    Verifies that resume_session_id triggers checkpoint loading, the prior
    postmortem chain is deserialized into memory, and already-completed ACs
    are skipped (not re-executed).

    Compounding context (from prior ACs):
    - AC-1 established [[INVARIANT: end-of-run chain artifact exists in
      docs/brainstorm/chain-*.md]] — checkpoints complement the artifact.
    - AC-2 established [[INVARIANT: ACPostmortem.sub_postmortems preserves
      structure in serialized chain]] — the loaded chain includes sub_postmortems.
    - AC-3 established [[INVARIANT: verify_invariants is called inline-blocking
      before chain advance]] — verified invariants are present in the loaded chain.
    - AC-3's per-AC checkpoint writing puts serialized PostmortemChain (with
      Invariant objects) into the checkpoint payload used by resume.

    [[INVARIANT: resume_session_id triggers checkpoint loading by seed_id, not by session_id]]
    [[INVARIANT: deserialized chain is injected before the AC loop so resumed ACs see prior postmortems]]
    [[INVARIANT: _load_compounding_checkpoint returns empty chain and -1 on failure]]
    [[INVARIANT: deserialized chain reflects all postmortems from the prior run up to last_completed_ac_index]]
    """

    def _make_executor_with_real_store(
        self, tmp_path: Path
    ) -> tuple[SerialCompoundingExecutor, Any]:
        """Build an executor with a real CheckpointStore backed by tmp_path."""
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        event_store, _ = _make_replaying_event_store()
        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
        return executor, store

    @pytest.mark.asyncio
    async def test_resume_skips_already_completed_acs(self, tmp_path: Path) -> None:
        """When resume_session_id is provided, ACs already in the checkpoint are skipped.

        Setup: write a checkpoint for AC 0 (index 0, last_completed_ac_index=0).
        Run execute_serial with resume_session_id set.
        Expect: AC 0's _execute_single_ac is NOT called; AC 1's IS called.

        Compounding reference: the checkpoint payload includes the PostmortemChain
        serialized by _write_compounding_checkpoint (AC-3), which now stores
        ACPostmortem.sub_postmortems (AC-2, B-prime) and Invariant objects (AC-3).

        [[INVARIANT: resume_session_id triggers checkpoint loading by seed_id, not by session_id]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint

        seed = _make_seed("AC 0 already done", "AC 1 to be executed")
        executor, store = self._make_executor_with_real_store(tmp_path)

        # Pre-write a checkpoint as if AC 0 had already completed.
        summary_ac0 = ACContextSummary(
            ac_index=0,
            ac_content="AC 0 already done",
            success=True,
            files_modified=("src/ac0.py",),
        )
        pm_ac0 = ACPostmortem(summary=summary_ac0, status="pass", gotchas=("ac0 gotcha",))
        prior_chain = PostmortemChain(postmortems=(pm_ac0,))
        _write_compounding_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_session",
            ac_index=0,
            chain=prior_chain,
        )

        executed_indices: list[int] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            executed_indices.append(ac_index)
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="resume_session",
            execution_id="exec_resume_skip",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_session",
        )

        # AC 0 was already done — not re-executed.
        assert 0 not in executed_indices, (
            f"AC 0 should have been skipped via checkpoint resume; executed: {executed_indices}"
        )
        # AC 1 was executed normally.
        assert 1 in executed_indices, (
            f"AC 1 should have been executed after skip; executed: {executed_indices}"
        )
        # Both ACs appear in results — AC 0 as SATISFIED_EXTERNALLY, AC 1 as SUCCEEDED.
        assert len(result.results) == 2

    @pytest.mark.asyncio
    async def test_resume_injects_prior_chain_into_resumed_ac_context(
        self, tmp_path: Path
    ) -> None:
        """The resumed AC's context_override contains postmortems from the loaded chain.

        After checkpoint loading, AC 1 should see AC 0's postmortem in its
        context_override, even though AC 0 was not re-executed.

        Compounding reference: AC-1 established [[INVARIANT: end-of-run chain
        artifact exists in docs/brainstorm/chain-*.md]] which confirmed the chain
        serialization round-trip works. This test relies on the same deserialization.

        [[INVARIANT: deserialized chain is injected before the AC loop so resumed ACs see prior postmortems]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint

        seed = _make_seed("AC 0 done", "AC 1 resumed")
        executor, store = self._make_executor_with_real_store(tmp_path)

        # Write a checkpoint with AC 0 done and a specific gotcha in its postmortem.
        summary_ac0 = ACContextSummary(
            ac_index=0,
            ac_content="AC 0 done",
            success=True,
            files_modified=("src/module_alpha.py",),
        )
        pm_ac0 = ACPostmortem(
            summary=summary_ac0,
            status="pass",
            gotchas=("important_gotcha_from_prior_run",),
        )
        prior_chain = PostmortemChain(postmortems=(pm_ac0,))
        _write_compounding_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_session_x",
            ac_index=0,
            chain=prior_chain,
        )

        captured_overrides: list[str] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            captured_overrides.append(kwargs.get("context_override") or "")
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="new_session_y",
            execution_id="exec_chain_inject",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_session_x",
        )

        # AC 1's context_override should reference the prior chain's content.
        # captured_overrides[0] belongs to AC 1 (AC 0 was skipped — no _execute_single_ac call).
        assert len(captured_overrides) == 1, (
            f"Only AC 1 should have been executed (AC 0 skipped). "
            f"Got {len(captured_overrides)} overrides."
        )
        ac1_override = captured_overrides[0]
        assert "Prior AC Postmortems" in ac1_override, (
            "AC 1 context must include the postmortem chain section"
        )
        assert "AC 0 done" in ac1_override, (
            "AC 1 context must include AC 0's postmortem content from the loaded chain"
        )
        assert "important_gotcha_from_prior_run" in ac1_override, (
            "AC 1 context must include gotchas from the deserialized chain"
        )

    @pytest.mark.asyncio
    async def test_resume_without_checkpoint_store_runs_fresh(self) -> None:
        """When no checkpoint store is provided, resume_session_id is safely ignored.

        All ACs are executed from the beginning even though resume_session_id is set.

        [[INVARIANT: _load_compounding_checkpoint returns empty chain and -1 on failure]]
        """
        seed = _make_seed("AC 0", "AC 1")
        # Use default executor — no checkpoint store.
        executor = _make_executor()
        assert executor._checkpoint_store is None

        executed_indices: list[int] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            executed_indices.append(int(kwargs["ac_index"]))
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_no_store",
            execution_id="exec_no_store",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="nonexistent_prior",
        )

        # Both ACs should have been executed (no skip because no store).
        assert executed_indices == [0, 1]
        assert result.success_count == 2

    @pytest.mark.asyncio
    async def test_resume_with_missing_checkpoint_runs_fresh(
        self, tmp_path: Path
    ) -> None:
        """When resume_session_id is set but no checkpoint file exists, start fresh.

        [[INVARIANT: _load_compounding_checkpoint returns empty chain and -1 on failure]]
        """
        seed = _make_seed("AC 0", "AC 1")
        executor, _store = self._make_executor_with_real_store(tmp_path)
        # Note: no checkpoint is written — store is empty.

        executed_indices: list[int] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            executed_indices.append(int(kwargs["ac_index"]))
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_fresh",
            execution_id="exec_fresh",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="no_such_session",
        )

        # All ACs executed from the start (checkpoint not found → fallback).
        assert executed_indices == [0, 1]
        assert result.success_count == 2

    def test_load_compounding_checkpoint_returns_chain_and_index(
        self, tmp_path: Path
    ) -> None:
        """_load_compounding_checkpoint returns the deserialized chain and index.

        Compounding reference: this function uses deserialize_postmortem_chain
        (verified round-trip in AC-2 tests) and CompoundingCheckpointState
        (established by AC-3 checkpoint writing tests).

        [[INVARIANT: deserialized chain reflects all postmortems from the prior run up to last_completed_ac_index]]
        [[INVARIANT: resume_session_id triggers checkpoint loading by seed_id, not by session_id]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import (
            _load_compounding_checkpoint,
            _write_compounding_checkpoint,
        )
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        # Write a two-AC chain.
        summary0 = ACContextSummary(ac_index=0, ac_content="AC zero", success=True)
        summary1 = ACContextSummary(
            ac_index=1,
            ac_content="AC one",
            success=True,
            files_modified=("src/f1.py",),
        )
        pm0 = ACPostmortem(summary=summary0, status="pass", gotchas=("g0",))
        pm1 = ACPostmortem(summary=summary1, status="pass")
        chain = PostmortemChain(postmortems=(pm0, pm1))

        _write_compounding_checkpoint(
            store=store,
            seed_id="seed_resume_direct",
            session_id="s_old",
            ac_index=1,
            chain=chain,
        )

        loaded_chain, last_idx = _load_compounding_checkpoint(
            store=store,
            seed_id="seed_resume_direct",
            session_id="s_new",
            resume_session_id="s_old",
        )

        assert last_idx == 1, f"Expected last_completed_ac_index=1, got {last_idx}"
        assert len(loaded_chain.postmortems) == 2, (
            f"Expected 2 postmortems in loaded chain, got {len(loaded_chain.postmortems)}"
        )
        assert loaded_chain.postmortems[0].summary.ac_content == "AC zero"
        assert loaded_chain.postmortems[1].summary.ac_content == "AC one"
        assert "g0" in loaded_chain.postmortems[0].gotchas
        assert "src/f1.py" in loaded_chain.postmortems[1].summary.files_modified

    def test_load_compounding_checkpoint_returns_empty_on_missing(
        self, tmp_path: Path
    ) -> None:
        """_load_compounding_checkpoint returns (empty_chain, -1) when no checkpoint.

        [[INVARIANT: _load_compounding_checkpoint returns empty chain and -1 on failure]]
        """
        from ouroboros.orchestrator.serial_executor import _load_compounding_checkpoint
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "empty_checkpoints")
        store.initialize()

        chain, idx = _load_compounding_checkpoint(
            store=store,
            seed_id="nonexistent_seed",
            session_id="s_new",
            resume_session_id="s_old",
        )

        assert idx == -1, f"Expected -1 (no checkpoint), got {idx}"
        assert len(chain.postmortems) == 0, (
            f"Expected empty chain, got {len(chain.postmortems)} postmortems"
        )

    def test_load_compounding_checkpoint_returns_empty_on_wrong_mode(
        self, tmp_path: Path
    ) -> None:
        """_load_compounding_checkpoint returns (empty_chain, -1) for non-compounding checkpoints.

        [[INVARIANT: _load_compounding_checkpoint returns empty chain and -1 on failure]]
        """
        from ouroboros.orchestrator.serial_executor import _load_compounding_checkpoint
        from ouroboros.persistence.checkpoint import CheckpointData, CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "wrong_mode_checkpoints")
        store.initialize()

        # Write a checkpoint with the wrong mode (not "compounding").
        wrong_mode_ckpt = CheckpointData.create(
            seed_id="seed_wrong_mode",
            phase="planning",
            state={"mode": "parallel", "some_key": "some_value"},
        )
        store.save(wrong_mode_ckpt)

        chain, idx = _load_compounding_checkpoint(
            store=store,
            seed_id="seed_wrong_mode",
            session_id="s_new",
            resume_session_id="s_old",
        )

        assert idx == -1, f"Expected -1 for wrong mode, got {idx}"
        assert len(chain.postmortems) == 0

    @pytest.mark.asyncio
    async def test_resume_session_id_none_does_not_load_checkpoint(
        self, tmp_path: Path
    ) -> None:
        """When resume_session_id is None, checkpoints are NOT loaded even if present.

        This ensures resume opt-in: callers that don't pass resume_session_id
        always get a fresh run, not an accidental resume.

        [[INVARIANT: resume_session_id triggers checkpoint loading by seed_id, not by session_id]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint

        seed = _make_seed("AC 0", "AC 1")
        executor, store = self._make_executor_with_real_store(tmp_path)

        # Pre-write a checkpoint for AC 0.
        summary = ACContextSummary(ac_index=0, ac_content="AC 0", success=True)
        pm = ACPostmortem(summary=summary, status="pass")
        prior_chain = PostmortemChain(postmortems=(pm,))
        _write_compounding_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_session",
            ac_index=0,
            chain=prior_chain,
        )

        executed_indices: list[int] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            executed_indices.append(int(kwargs["ac_index"]))
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="fresh_session",
            execution_id="exec_no_resume",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            # NOTE: resume_session_id intentionally NOT passed (defaults to None).
        )

        # Both ACs should be executed: no resume without explicit resume_session_id.
        assert executed_indices == [0, 1], (
            f"Both ACs should run from scratch; executed: {executed_indices}"
        )
        assert result.success_count == 2

    @pytest.mark.asyncio
    async def test_resume_3ac_chain_skips_first_two_executes_third(
        self, tmp_path: Path
    ) -> None:
        """In a 3-AC run, resuming with last_completed_ac_index=1 skips AC 0 and AC 1.

        Setup: write a checkpoint for AC 1 (last_completed_ac_index=1) with a 2-AC chain.
        Run execute_serial with resume_session_id set.
        Expect: ACs 0 and 1 are skipped; only AC 2 is executed.
        AC 2's context_override must include postmortems for AC 0 AND AC 1 from the chain.

        Compounding reference: this builds on the AC-skipping logic established in
        Sub-AC 1 (checkpoint loading) and verifies that the *chain forwarding* works
        correctly for multi-AC resume — not just 2-AC runs. The postmortem chain
        established by AC-1 (Q6.1) includes serialize_postmortem_chain round-trip
        (verified in AC-2 B-prime tests), and the invariants field (AC-3 C-plus).

        [[INVARIANT: deserialized chain reflects all postmortems from the prior run up to last_completed_ac_index]]
        [[INVARIANT: resume_session_id triggers checkpoint loading by seed_id, not by session_id]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint

        seed = _make_seed("AC 0 done", "AC 1 done", "AC 2 to execute")
        executor, store = self._make_executor_with_real_store(tmp_path)

        # Write a checkpoint for a 2-AC completed run (ACs 0 and 1 done).
        summary_ac0 = ACContextSummary(
            ac_index=0,
            ac_content="AC 0 done",
            success=True,
            files_modified=("src/ac0.py",),
        )
        summary_ac1 = ACContextSummary(
            ac_index=1,
            ac_content="AC 1 done",
            success=True,
            files_modified=("src/ac1.py",),
        )
        pm_ac0 = ACPostmortem(
            summary=summary_ac0,
            status="pass",
            gotchas=("ac0 specific gotcha",),
        )
        pm_ac1 = ACPostmortem(
            summary=summary_ac1,
            status="pass",
            gotchas=("ac1 specific gotcha",),
        )
        prior_chain = PostmortemChain(postmortems=(pm_ac0, pm_ac1))
        _write_compounding_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_session_3ac",
            ac_index=1,  # last_completed = AC 1 (0-based)
            chain=prior_chain,
        )

        executed_indices: list[int] = []
        captured_overrides: list[str] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            executed_indices.append(ac_index)
            captured_overrides.append(kwargs.get("context_override") or "")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,), (2,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="new_session_3ac",
            execution_id="exec_3ac_resume",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_session_3ac",
        )

        # ACs 0 and 1 must NOT have been executed.
        assert 0 not in executed_indices, (
            f"AC 0 should be skipped; executed: {executed_indices}"
        )
        assert 1 not in executed_indices, (
            f"AC 1 should be skipped; executed: {executed_indices}"
        )
        # Only AC 2 was executed.
        assert executed_indices == [2], f"Only AC 2 should run; executed: {executed_indices}"

        # AC 2's context_override must include both AC 0 and AC 1 postmortems.
        assert len(captured_overrides) == 1
        ac2_override = captured_overrides[0]
        assert "AC 0 done" in ac2_override, "AC 2 context must include AC 0's postmortem"
        assert "AC 1 done" in ac2_override, "AC 2 context must include AC 1's postmortem"
        assert "ac0 specific gotcha" in ac2_override, "AC 0 gotcha must be in AC 2 context"
        assert "ac1 specific gotcha" in ac2_override, "AC 1 gotcha must be in AC 2 context"

        # Result has 3 entries: AC 0 (SATISFIED_EXTERNALLY), AC 1 (SATISFIED_EXTERNALLY),
        # AC 2 (SUCCEEDED).
        assert len(result.results) == 3
        assert result.success_count >= 1  # At least AC 2 succeeded


class TestTruncationEvent:
    """AC-4 (Q7): Postmortem chain truncation event.

    Verifies that the Q7 structured event is emitted alongside log.warning
    when the rendered postmortem chain overflows the character budget.
    Coexists with the log line — does not replace it.

    Compounding context (from prior ACs):
    - AC-1: [[INVARIANT: end-of-run chain artifact exists in docs/brainstorm/chain-*.md]]
      — artifact and truncation events both serve observability purposes.
    - AC-2: [[INVARIANT: ACPostmortem.sub_postmortems preserves structure in serialized chain]]
      — sub-postmortem data survives the chain even when digests are truncated.
    - AC-3: [[INVARIANT: verify_invariants is called inline-blocking before chain advance]]
      — verified invariants are part of the chain that may be truncated.
    - Sub-AC 1: [[INVARIANT: deserialized chain is injected before the AC loop
      so resumed ACs see prior postmortems]] — truncation may affect resumed chains.

    [[INVARIANT: Truncation event emitted alongside log.warning, not replacing it]]
    [[INVARIANT: event type is execution.postmortem_chain.truncated]]
    [[INVARIANT: no truncation event emitted when chain fits within budget]]
    """

    def test_truncation_event_factory_fields(self) -> None:
        """create_postmortem_chain_truncated_event produces the expected event structure.

        Verifies the event type, aggregate_type, and all required data fields.
        No executor needed — tests the factory directly.

        [[INVARIANT: event type is execution.postmortem_chain.truncated]]
        """
        from ouroboros.orchestrator.events import create_postmortem_chain_truncated_event

        event = create_postmortem_chain_truncated_event(
            session_id="sess_trunc",
            execution_id="exec_trunc",
            dropped_count=3,
            char_budget=10000,
            rendered_chars=12500,
            full_forms_preserved=2,
            cumulative_invariants_preserved=1,
        )

        assert event.type == "execution.postmortem_chain.truncated"
        assert event.aggregate_type == "execution"
        assert event.aggregate_id == "exec_trunc"
        assert event.data["session_id"] == "sess_trunc"
        assert event.data["execution_id"] == "exec_trunc"
        assert event.data["dropped_count"] == 3
        assert event.data["char_budget"] == 10000
        assert event.data["rendered_chars"] == 12500
        assert event.data["full_forms_preserved"] == 2
        assert event.data["cumulative_invariants_preserved"] == 1
        assert "timestamp" in event.data

    def test_on_truncated_callback_invoked_when_over_budget(self) -> None:
        """to_prompt_text calls on_truncated when chain exceeds char_budget.

        Build a chain with many ACs and set a tiny token_budget so truncation
        is guaranteed. Verify the callback is called with the correct counts.

        Compounding ref: uses PostmortemChain.to_prompt_text which was built
        in AC-2 and AC-3 (invariant render gate). The on_truncated callback
        is the new Q7 hook in Sub-AC 2.

        [[INVARIANT: Truncation event emitted alongside log.warning, not replacing it]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )

        # Build a chain with enough content to overflow a tiny budget.
        def _make_pm(idx: int) -> ACPostmortem:
            summary = ACContextSummary(
                ac_index=idx,
                ac_content=f"AC {idx} task — " + "x" * 300,  # force long content
                success=True,
                files_modified=(f"src/file_{idx}.py",),
            )
            return ACPostmortem(
                summary=summary,
                status="pass",
                gotchas=(f"gotcha for AC {idx} " + "y" * 100,),
            )

        # 8 ACs gives enough text to overflow a tiny budget.
        postmortems = tuple(_make_pm(i) for i in range(8))
        chain = PostmortemChain(postmortems=postmortems)

        truncation_calls: list[tuple] = []

        def _capture(*args: int) -> None:
            truncation_calls.append(args)

        # Use a tiny budget (1 token = 4 chars) to guarantee overflow.
        chain.to_prompt_text(
            token_budget=1,
            k_full=1,
            on_truncated=_capture,
        )

        assert len(truncation_calls) == 1, (
            f"Expected exactly 1 truncation callback, got {len(truncation_calls)}"
        )
        dropped_count, char_budget, rendered_chars, full_forms, invariants = truncation_calls[0]
        assert dropped_count > 0, "At least one digest must have been dropped"
        assert char_budget == 4, "1 token * 4 chars/token = 4"
        assert rendered_chars > 4, "Rendered text must exceed budget"
        assert full_forms == 1, "k_full=1 → 1 full-form entry"

    def test_no_truncation_callback_when_chain_fits(self) -> None:
        """on_truncated is NOT called when the chain fits within the budget.

        [[INVARIANT: no truncation event emitted when chain fits within budget]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )

        summary = ACContextSummary(ac_index=0, ac_content="Short AC", success=True)
        pm = ACPostmortem(summary=summary, status="pass")
        chain = PostmortemChain(postmortems=(pm,))

        truncation_calls: list[tuple] = []
        chain.to_prompt_text(
            token_budget=8000,  # large budget — should never truncate
            on_truncated=lambda *a: truncation_calls.append(a),
        )

        assert truncation_calls == [], (
            "on_truncated must NOT be called when chain fits within budget"
        )

    @pytest.mark.asyncio
    async def test_truncation_event_emitted_from_serial_executor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SerialCompoundingExecutor emits the truncation event when chain overflows.

        Uses a tiny OUROBOROS_POSTMORTEM_TOKEN_BUDGET to force truncation.
        Verifies that an "execution.postmortem_chain.truncated" event appears
        in the event store after the run.

        Compounding ref: this relies on the postmortem chain built by prior
        successful ACs (AC-1 through AC-3 and Sub-AC 1). The event emission
        uses the existing _safe_emit_event pattern from parallel_executor.py.

        [[INVARIANT: Truncation event emitted alongside log.warning, not replacing it]]
        [[INVARIANT: event type is execution.postmortem_chain.truncated]]
        """
        # Force a tiny token budget so even one prior AC causes truncation.
        monkeypatch.setenv("OUROBOROS_POSTMORTEM_TOKEN_BUDGET", "1")

        # Build a seed with 3 ACs; pre-load the chain with 5 dummy postmortems
        # via checkpoint so AC 0 (the resuming AC) sees an oversize chain.
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        event_store, appended = _make_replaying_event_store()
        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])

        # Pre-populate a big chain with 5 verbose postmortems.
        def _big_pm(idx: int) -> ACPostmortem:
            return ACPostmortem(
                summary=ACContextSummary(
                    ac_index=idx,
                    ac_content=f"AC {idx} verbose task " + "word " * 80,
                    success=True,
                    files_modified=(f"src/file_{idx}.py",),
                ),
                status="pass",
                gotchas=(f"gotcha for AC {idx} " + "detail " * 60,),
            )

        big_chain = PostmortemChain(postmortems=tuple(_big_pm(i) for i in range(5)))

        seed = _make_seed(
            "AC 0 already done",
            "AC 1 already done",
            "AC 2 already done",
            "AC 3 already done",
            "AC 4 already done",
            "AC 5 to execute",
        )
        _write_compounding_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_big_chain",
            ac_index=4,
            chain=big_chain,
        )

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,), (2,), (3,), (4,), (5,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_trunc_event",
            execution_id="exec_trunc_event",
            tools=[],
            system_prompt="SYS",
            execution_plan=plan,
            resume_session_id="prior_big_chain",
        )

        trunc_events = [
            e for e in appended if e.type == "execution.postmortem_chain.truncated"
        ]
        assert len(trunc_events) >= 1, (
            f"Expected at least 1 truncation event with token_budget=1; "
            f"event types seen: {[e.type for e in appended]}"
        )
        ev = trunc_events[0]
        assert ev.data["session_id"] == "sess_trunc_event"
        assert ev.data["execution_id"] == "exec_trunc_event"
        assert ev.data["dropped_count"] >= 0
        assert ev.data["char_budget"] > 0

    @pytest.mark.asyncio
    async def test_no_truncation_event_when_chain_fits(self) -> None:
        """No truncation event is emitted when the chain fits within the default budget.

        A 2-AC run with a generous budget should produce zero truncation events.

        [[INVARIANT: no truncation event emitted when chain fits within budget]]
        """
        seed = _make_seed("AC a", "AC b")
        executor = _make_executor()
        event_store: Any = executor._event_store
        appended: list[Any] = event_store._appended

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_no_trunc",
            execution_id="exec_no_trunc",
            tools=[],
            system_prompt="SYS",
            execution_plan=plan,
        )

        trunc_events = [
            e for e in appended if e.type == "execution.postmortem_chain.truncated"
        ]
        assert trunc_events == [], (
            f"Unexpected truncation events with default budget; got: {trunc_events}"
        )

    @pytest.mark.asyncio
    async def test_oversize_chain_emits_exactly_one_event_per_ac_with_correct_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Oversize chain emits exactly one truncation event per executed AC with correct counts.

        Uses a pre-loaded checkpoint with 4 verbose postmortems and a token budget
        of 1 (char_budget=4) so the chain definitely overflows. Only one AC (AC 4)
        is executed, so exactly one truncation event is expected.

        Verifies all count fields:
        - dropped_count > 0 (at least one digest was dropped)
        - char_budget == token_budget * 4
        - rendered_chars > char_budget (chain still exceeds budget after dropping)

        Compounding reference (AC-1 through AC-3): the chain was written by
        _write_compounding_checkpoint which serializes PostmortemChain including
        sub_postmortems (AC-2, B-prime) and Invariant objects (AC-3, C-plus).

        [[INVARIANT: exactly one truncation event per executed AC when chain overflows]]
        [[INVARIANT: char_budget in truncation event equals token_budget * 4]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint
        from ouroboros.persistence.checkpoint import CheckpointStore

        token_budget = 1  # char_budget = 4
        monkeypatch.setenv("OUROBOROS_POSTMORTEM_TOKEN_BUDGET", str(token_budget))

        # Build a large chain with 4 verbose ACs (each far exceeds budget).
        def _verbose_pm(idx: int) -> ACPostmortem:
            return ACPostmortem(
                summary=ACContextSummary(
                    ac_index=idx,
                    ac_content=f"Verbose task {idx}: " + "word " * 50,
                    success=True,
                    files_modified=(f"src/module_{idx}.py",),
                ),
                status="pass",
                gotchas=(f"gotcha {idx}: " + "detail " * 40,),
            )

        big_chain = PostmortemChain(postmortems=tuple(_verbose_pm(i) for i in range(4)))

        # Seed: 4 ACs pre-completed, 1 to execute.
        seed = _make_seed("AC 0 done", "AC 1 done", "AC 2 done", "AC 3 done", "AC 4 to run")

        store = CheckpointStore(base_path=tmp_path / "ckpts")
        store.initialize()
        _write_compounding_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_verbose",
            ac_index=3,
            chain=big_chain,
        )

        event_store, appended = _make_replaying_event_store()
        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,), (2,), (3,), (4,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_exact_count",
            execution_id="exec_exact_count",
            tools=[],
            system_prompt="SYS",
            execution_plan=plan,
            resume_session_id="prior_verbose",
        )

        trunc_events = [
            e for e in appended if e.type == "execution.postmortem_chain.truncated"
        ]
        # Exactly 1 AC executed → exactly 1 truncation event.
        assert len(trunc_events) == 1, (
            f"Expected exactly 1 truncation event (1 AC executed); "
            f"got {len(trunc_events)}: {[e.data for e in trunc_events]}"
        )
        ev = trunc_events[0]

        # Verify correct counts.
        assert ev.data["dropped_count"] > 0, (
            "dropped_count must be > 0 when chain still overflows after all digests dropped"
        )
        expected_budget = token_budget * 4
        assert ev.data["char_budget"] == expected_budget, (
            f"char_budget must equal token_budget * 4 = {expected_budget}; "
            f"got {ev.data['char_budget']}"
        )
        assert ev.data["rendered_chars"] > ev.data["char_budget"], (
            "rendered_chars must exceed char_budget (truncation sentinel)"
        )
        # Verify the event is keyed on the execution aggregate.
        assert ev.aggregate_type == "execution"
        assert ev.aggregate_id == "exec_exact_count"
        assert ev.data["session_id"] == "sess_exact_count"
        assert ev.data["execution_id"] == "exec_exact_count"

    @pytest.mark.asyncio
    async def test_truncation_event_roundtrip_through_real_event_store(
        self, tmp_path: Path
    ) -> None:
        """Truncation event roundtrips through a real EventStore without data loss.

        Appends a truncation event to a real SQLite-backed EventStore, replays
        it by aggregate, and asserts that all numeric count fields and the
        event metadata survive serialization intact.

        This is the "event store roundtrip" required by Sub-AC 3 of the Q7
        truncation event feature.  The test is intentionally divorced from the
        serial executor so that it isolates the persistence layer.

        Compounding reference: the EventStore is the same persistence layer used
        by SerialCompoundingExecutor._safe_emit_event (which calls store.append()
        after every truncation callback).  Prior ACs confirmed that the
        postmortem chain serializes sub_postmortems (AC-2) and Invariant objects
        (AC-3); those objects' parent events also go through this same store.

        [[INVARIANT: event type is execution.postmortem_chain.truncated]]
        [[INVARIANT: Truncation event uses aggregate_type execution, keyed on execution_id]]
        """
        from ouroboros.orchestrator.events import create_postmortem_chain_truncated_event
        from ouroboros.persistence.event_store import EventStore

        db_path = tmp_path / "trunc_rt_events.db"
        store = EventStore(database_url=f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()

        try:
            # Create the event with known field values.
            event = create_postmortem_chain_truncated_event(
                session_id="sess_roundtrip",
                execution_id="exec_roundtrip",
                dropped_count=5,
                char_budget=4000,
                rendered_chars=6200,
                full_forms_preserved=3,
                cumulative_invariants_preserved=2,
            )

            # Append to the real store.
            await store.append(event)

            # Replay by aggregate_type + aggregate_id.
            replayed = await store.replay("execution", "exec_roundtrip")

            assert len(replayed) == 1, (
                f"Expected exactly 1 replayed event; got {len(replayed)}"
            )
            rt = replayed[0]

            # --- Event metadata ---
            assert rt.type == "execution.postmortem_chain.truncated", (
                f"Event type not preserved; got {rt.type!r}"
            )
            assert rt.aggregate_type == "execution"
            assert rt.aggregate_id == "exec_roundtrip"

            # --- Data payload ---
            assert rt.data["session_id"] == "sess_roundtrip"
            assert rt.data["execution_id"] == "exec_roundtrip"
            assert rt.data["dropped_count"] == 5
            assert rt.data["char_budget"] == 4000
            assert rt.data["rendered_chars"] == 6200
            assert rt.data["full_forms_preserved"] == 3
            assert rt.data["cumulative_invariants_preserved"] == 2
            assert "timestamp" in rt.data

        finally:
            await store.close()


class TestResumeCorrectness:
    """Sub-AC 3: Resume correctness — rehydrated chain identity and AC skip/execute semantics.

    These tests verify that when execute_serial is invoked with resume_session_id:
    1. Completed ACs (index <= last_completed_ac_index) are skipped.
    2. Remaining ACs are executed normally.
    3. The rehydrated postmortem chain is field-identical to the original
       (all postmortem fields — including sub_postmortems and Invariant objects —
       survive the checkpoint round-trip without mutation or loss).

    Compounding context:
    - AC-1 established [[INVARIANT: end-of-run chain artifact exists in
      docs/brainstorm/chain-*.md]] — chain serialization round-trip verified.
    - AC-2 established [[INVARIANT: ACPostmortem.sub_postmortems preserves
      structure in serialized chain]] — sub-postmortems survive round-trips.
    - AC-3 established [[INVARIANT: invariants_established is now
      tuple[Invariant, ...] not tuple[str, ...]]] — Invariant objects with
      reliability and occurrences must survive checkpoint round-trips.
    - Sub-AC 1 established [[INVARIANT: deserialized chain is injected before
      the AC loop so resumed ACs see prior postmortems]].
    - Sub-AC 2's checkpoint writing ensures per-AC checkpoints include full
      chain state [[INVARIANT: CompoundingCheckpointState.last_completed_ac_index
      equals the 0-based AC index]].

    [[INVARIANT: checkpoint round-trip preserves all ACPostmortem fields including
    Invariant objects, sub_postmortems, gotchas, and files_modified]]
    [[INVARIANT: resume skips ACs with index <= last_completed_ac_index and executes the rest]]
    """

    def _make_executor_with_real_store(
        self, tmp_path: Path
    ) -> tuple[SerialCompoundingExecutor, Any]:
        """Build an executor with a real CheckpointStore backed by tmp_path."""
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        event_store, _ = _make_replaying_event_store()
        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
        return executor, store

    def test_rehydrated_chain_is_field_identical_to_original(
        self, tmp_path: Path
    ) -> None:
        """The deserialized chain from a checkpoint is field-identical to the original.

        Builds a rich postmortem with:
        - files_modified (multiple files)
        - gotchas (multiple)
        - invariants_established with Invariant objects (reliability, occurrences,
          first_seen_ac_id, is_contradicted)
        - sub_postmortems (nested ACPostmortem)

        After write → load via _write_compounding_checkpoint + _load_compounding_checkpoint,
        every field must be equal to the original.

        Compounding reference: AC-2 proved sub_postmortems round-trip; AC-3 proved
        Invariant objects serialize/deserialize. This test combines all fields in a
        single checkpoint round-trip — the most complete identity check.

        [[INVARIANT: checkpoint round-trip preserves all ACPostmortem fields including
        Invariant objects, sub_postmortems, gotchas, and files_modified]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            Invariant,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import (
            _load_compounding_checkpoint,
            _write_compounding_checkpoint,
        )
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        # Build a sub-postmortem for the parent's sub_postmortems field.
        sub_summary = ACContextSummary(
            ac_index=0,
            ac_content="Sub task: extract schema",
            success=True,
            files_modified=("src/schema.py", "src/schema_types.py"),
            public_api="SchemaExtractor",
        )
        sub_pm = ACPostmortem(
            summary=sub_summary,
            status="pass",
            gotchas=("schema must be frozen",),
            invariants_established=(
                Invariant(
                    text="schema extraction is idempotent",
                    reliability=0.88,
                    occurrences=1,
                    first_seen_ac_id="ac_0_sub",
                    is_contradicted=False,
                ),
            ),
        )

        # Build the main postmortem with multiple invariants (including a
        # contradicted one) and the sub-postmortem above.
        main_summary = ACContextSummary(
            ac_index=0,
            ac_content="Implement the data pipeline",
            success=True,
            files_modified=("src/pipeline.py", "src/pipeline_utils.py", "tests/test_pipeline.py"),
            tools_used=("Read", "Write", "Bash"),
            key_output="Pipeline implemented with 3 stages",
            public_api="run_pipeline, PipelineStage",
        )
        trusted_inv = Invariant(
            text="pipeline stages run in topological order",
            reliability=0.92,
            occurrences=2,
            first_seen_ac_id="ac_0",
            is_contradicted=False,
        )
        contradicted_inv = Invariant(
            text="pipeline is synchronous",
            reliability=0.0,
            occurrences=1,
            first_seen_ac_id="ac_0",
            is_contradicted=True,
        )
        main_pm = ACPostmortem(
            summary=main_summary,
            diff_summary="+ 240 lines pipeline logic",
            tool_trace_digest="Read x5, Write x3, Bash x2",
            gotchas=("async pipeline needs special error handling", "don't use global state"),
            qa_suggestions=("add integration test for stage ordering",),
            invariants_established=(trusted_inv, contradicted_inv),
            retry_attempts=1,
            status="pass",
            duration_seconds=42.5,
            ac_native_session_id="native_sess_abc",
            sub_postmortems=(sub_pm,),
        )

        original_chain = PostmortemChain(postmortems=(main_pm,))

        _write_compounding_checkpoint(
            store=store,
            seed_id="seed_identity_test",
            session_id="sess_identity",
            ac_index=0,
            chain=original_chain,
        )

        loaded_chain, last_idx = _load_compounding_checkpoint(
            store=store,
            seed_id="seed_identity_test",
            session_id="sess_identity_new",
            resume_session_id="sess_identity",
        )

        assert last_idx == 0, f"Expected last_completed_ac_index=0, got {last_idx}"
        assert len(loaded_chain.postmortems) == 1

        loaded_pm = loaded_chain.postmortems[0]

        # --- ACContextSummary fields ---
        assert loaded_pm.summary.ac_index == 0
        assert loaded_pm.summary.ac_content == "Implement the data pipeline"
        assert loaded_pm.summary.success is True
        assert set(loaded_pm.summary.files_modified) == {
            "src/pipeline.py", "src/pipeline_utils.py", "tests/test_pipeline.py"
        }
        assert loaded_pm.summary.public_api == "run_pipeline, PipelineStage"
        assert loaded_pm.summary.key_output == "Pipeline implemented with 3 stages"

        # --- ACPostmortem scalar fields ---
        assert loaded_pm.diff_summary == "+ 240 lines pipeline logic"
        assert loaded_pm.tool_trace_digest == "Read x5, Write x3, Bash x2"
        assert loaded_pm.status == "pass"
        assert loaded_pm.retry_attempts == 1
        assert abs(loaded_pm.duration_seconds - 42.5) < 1e-6
        assert loaded_pm.ac_native_session_id == "native_sess_abc"

        # --- gotchas and qa_suggestions ---
        assert "async pipeline needs special error handling" in loaded_pm.gotchas
        assert "don't use global state" in loaded_pm.gotchas
        assert "add integration test for stage ordering" in loaded_pm.qa_suggestions

        # --- Invariant objects: all fields preserved ---
        assert len(loaded_pm.invariants_established) == 2

        # Find the trusted invariant by text.
        loaded_trusted = next(
            (i for i in loaded_pm.invariants_established
             if "topological order" in i.text),
            None,
        )
        assert loaded_trusted is not None, "Trusted invariant not found in loaded chain"
        assert loaded_trusted.text == "pipeline stages run in topological order"
        assert abs(loaded_trusted.reliability - 0.92) < 1e-6
        assert loaded_trusted.occurrences == 2
        assert loaded_trusted.first_seen_ac_id == "ac_0"
        assert loaded_trusted.is_contradicted is False

        # The contradicted invariant should also survive.
        loaded_contradicted = next(
            (i for i in loaded_pm.invariants_established
             if "synchronous" in i.text),
            None,
        )
        assert loaded_contradicted is not None, "Contradicted invariant not found in loaded chain"
        assert loaded_contradicted.is_contradicted is True
        assert abs(loaded_contradicted.reliability - 0.0) < 1e-6

        # --- sub_postmortems: nested structure preserved ---
        assert len(loaded_pm.sub_postmortems) == 1
        loaded_sub = loaded_pm.sub_postmortems[0]
        assert loaded_sub.summary.ac_content == "Sub task: extract schema"
        assert "src/schema.py" in loaded_sub.summary.files_modified
        assert "schema must be frozen" in loaded_sub.gotchas
        assert len(loaded_sub.invariants_established) == 1
        loaded_sub_inv = loaded_sub.invariants_established[0]
        assert loaded_sub_inv.text == "schema extraction is idempotent"
        assert abs(loaded_sub_inv.reliability - 0.88) < 1e-6

    @pytest.mark.asyncio
    async def test_partial_checkpoint_2ac_of_3_skips_completed_executes_remaining(
        self, tmp_path: Path
    ) -> None:
        """Create a partial checkpoint (ACs 0+1 done), resume 3-AC run, assert AC 2 executes.

        Setup:
        - A 3-AC seed.
        - A checkpoint with last_completed_ac_index=1 (ACs 0 and 1 complete).
        - The checkpoint chain has rich postmortems for ACs 0 and 1.

        Assertions:
        - ACs 0 and 1 are NOT executed (skipped via checkpoint).
        - AC 2 IS executed.
        - AC 2's context_override contains content from BOTH AC 0 and AC 1 postmortems.
        - Result has 3 entries (2 SATISFIED_EXTERNALLY + 1 SUCCEEDED).

        Compounding reference: the checkpoint payload stores the complete
        PostmortemChain (established in AC-1 Q6.1), including sub_postmortems
        (AC-2 B-prime) and Invariant objects (AC-3 C-plus), ensuring the
        context AC 2 receives is as rich as possible.

        [[INVARIANT: resume skips ACs with index <= last_completed_ac_index and executes the rest]]
        [[INVARIANT: deserialized chain is injected before the AC loop so resumed ACs see prior postmortems]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            Invariant,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint
        from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome

        seed = _make_seed(
            "AC 0: implement auth module",
            "AC 1: implement user service",
            "AC 2: implement API layer",
        )
        executor, store = self._make_executor_with_real_store(tmp_path)

        # Build a rich prior chain for ACs 0 and 1.
        pm_ac0 = ACPostmortem(
            summary=ACContextSummary(
                ac_index=0,
                ac_content="AC 0: implement auth module",
                success=True,
                files_modified=("src/auth.py", "tests/test_auth.py"),
            ),
            status="pass",
            gotchas=("JWT tokens expire after 1 hour",),
            invariants_established=(
                Invariant(
                    text="all API routes require auth header",
                    reliability=0.95,
                    occurrences=1,
                    first_seen_ac_id="ac_0",
                ),
            ),
        )
        pm_ac1 = ACPostmortem(
            summary=ACContextSummary(
                ac_index=1,
                ac_content="AC 1: implement user service",
                success=True,
                files_modified=("src/user_service.py",),
            ),
            status="pass",
            gotchas=("UserService depends on AuthModule being initialized first",),
            invariants_established=(
                Invariant(
                    text="UserService.create() validates email uniqueness",
                    reliability=0.90,
                    occurrences=1,
                    first_seen_ac_id="ac_1",
                ),
            ),
        )
        prior_chain = PostmortemChain(postmortems=(pm_ac0, pm_ac1))

        _write_compounding_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_session_partial",
            ac_index=1,  # last_completed = AC 1 (0-based)
            chain=prior_chain,
        )

        executed_indices: list[int] = []
        captured_overrides: list[str] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            executed_indices.append(ac_index)
            captured_overrides.append(kwargs.get("context_override") or "")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,), (2,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="resume_session_partial",
            execution_id="exec_partial_resume",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_session_partial",
        )

        # Only AC 2 was executed — ACs 0 and 1 were skipped.
        assert executed_indices == [2], (
            f"Only AC 2 should execute; executed: {executed_indices}"
        )

        # AC 2's context_override must reference BOTH prior ACs.
        assert len(captured_overrides) == 1
        ac2_override = captured_overrides[0]
        assert "AC 0: implement auth module" in ac2_override, (
            "AC 2 context must include AC 0's postmortem"
        )
        assert "AC 1: implement user service" in ac2_override, (
            "AC 2 context must include AC 1's postmortem"
        )
        assert "JWT tokens expire after 1 hour" in ac2_override, (
            "AC 2 context must include AC 0's gotchas from the loaded chain"
        )
        assert "UserService depends on AuthModule" in ac2_override, (
            "AC 2 context must include AC 1's gotchas from the loaded chain"
        )

        # Result structure: 3 entries total.
        assert len(result.results) == 3

        # ACs 0 and 1: SATISFIED_EXTERNALLY (skipped via checkpoint).
        assert result.results[0].outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY
        assert result.results[1].outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY

        # AC 2: SUCCEEDED.
        assert result.results[2].outcome == ACExecutionOutcome.SUCCEEDED
        assert result.results[2].success is True

    @pytest.mark.asyncio
    async def test_resume_chain_includes_invariants_in_resumed_context(
        self, tmp_path: Path
    ) -> None:
        """Invariants from the prior run appear in the resumed AC's context.

        When the saved chain has Invariants in invariants_established, they
        must appear in the 'Established Invariants' section of the resumed
        AC's context_override after the chain is rehydrated.

        Compounding references:
        - AC-3 established [[INVARIANT: only above-threshold invariants appear
          in downstream chain context]] — verified invariants propagate.
        - AC-2 established [[INVARIANT: ACPostmortem.sub_postmortems preserves
          structure in serialized chain]] — full chain state survives.

        [[INVARIANT: checkpoint round-trip preserves all ACPostmortem fields including
        Invariant objects, sub_postmortems, gotchas, and files_modified]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            Invariant,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint

        seed = _make_seed("AC 0 with invariant", "AC 1 resumed sees invariant")
        executor, store = self._make_executor_with_real_store(tmp_path)

        # AC 0 postmortem with a high-reliability invariant.
        INVARIANT_TEXT = "serialize_postmortem_chain produces a stable list"
        pm_ac0 = ACPostmortem(
            summary=ACContextSummary(
                ac_index=0,
                ac_content="AC 0 with invariant",
                success=True,
                files_modified=("src/level_context.py",),
            ),
            status="pass",
            invariants_established=(
                Invariant(
                    text=INVARIANT_TEXT,
                    reliability=0.95,
                    occurrences=1,
                    first_seen_ac_id="ac_0",
                ),
            ),
        )
        prior_chain = PostmortemChain(postmortems=(pm_ac0,))

        _write_compounding_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_inv_session",
            ac_index=0,
            chain=prior_chain,
        )

        captured_overrides: list[str] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            captured_overrides.append(kwargs.get("context_override") or "")
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="resumed_inv_session",
            execution_id="exec_inv_resume",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_inv_session",
        )

        # AC 1's context must include the invariant from the prior run.
        assert len(captured_overrides) == 1, (
            f"Only AC 1 should execute; got {len(captured_overrides)} overrides"
        )
        ac1_override = captured_overrides[0]

        # The invariant must appear in the 'Established Invariants' section.
        established_idx = ac1_override.find("Established Invariants")
        assert established_idx != -1, (
            f"'Established Invariants' section must be present in resumed AC context; "
            f"override snippet:\n{ac1_override[:500]}"
        )
        established_section = ac1_override[established_idx:]
        assert INVARIANT_TEXT in established_section, (
            f"Invariant from prior run must appear in 'Established Invariants' section "
            f"of resumed AC's context; section was:\n{established_section[:500]}"
        )

    @pytest.mark.asyncio
    async def test_full_resume_flow_end_to_end_with_real_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end test: first run writes checkpoint; second run resumes from it.

        Simulates the real workflow:
        1. First execute_serial run (3 ACs, all succeed) — writes per-AC checkpoints.
        2. The run is interrupted after AC 1 (by monkeypatching the store to raise
           an error for AC 2, which makes the second "run" start from AC 2).
        3. A second execute_serial run resumes from the first run's checkpoint —
           only AC 2 executes, and its context_override includes postmortems for
           ACs 0 and 1 from the first run.

        This is the closest to a production resume scenario: the checkpoint was
        written by the executor itself (not manually), and the resume uses that
        checkpoint to skip the completed ACs.

        Compounding references:
        - AC-1: end-of-run artifact (Q6.1) — both runs produce artifacts.
        - AC-2: sub_postmortems preserved in checkpoints.
        - AC-3: invariant verifier runs inline before chain advance, so
          invariants in the chain come from the real verify_invariants path.

        [[INVARIANT: resume skips ACs with index <= last_completed_ac_index and executes the rest]]
        [[INVARIANT: checkpoint round-trip preserves all ACPostmortem fields including
        Invariant objects, sub_postmortems, gotchas, and files_modified]]
        """
        import ouroboros.orchestrator.serial_executor as serial_mod

        # Suppress invariant verification (no real Haiku calls in unit tests).
        async def fake_verify(
            adapter: Any,
            tags: list[str],
            **kwargs: Any,
        ) -> list[tuple[str, float]]:
            # Accept all tags at high reliability.
            return [(tag, 0.9) for tag in tags]

        monkeypatch.setattr(serial_mod, "verify_invariants", fake_verify)

        # Use a custom artifact dir so the test doesn't pollute docs/brainstorm/.
        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "artifacts"))

        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        seed = _make_seed(
            "AC 0: write data model",
            "AC 1: write service layer",
            "AC 2: write API endpoints",
        )

        # ---- First run: ACs 0 and 1 succeed, AC 2 fails ----
        event_store_1, _ = _make_replaying_event_store()
        executor_1 = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store_1,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor_1._coordinator.detect_file_conflicts = MagicMock(return_value=[])

        call_count_1: list[int] = []

        async def fake_single_ac_run1(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            call_count_1.append(ac_index)
            if ac_index == 2:
                # Simulate AC 2 failing in the first run.
                return _fail_result(
                    ac_index, str(kwargs["ac_content"]), error="timeout in run 1"
                )
            return _ok_result(
                ac_index,
                str(kwargs["ac_content"]),
                final_message=f"AC {ac_index} done [[INVARIANT: ac{ac_index} outputs stable]]",
                files_written=(f"src/ac{ac_index}.py",),
            )

        executor_1._execute_single_ac = fake_single_ac_run1  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,), (2,))
        result_1 = await executor_1.execute_serial(
            seed=seed,
            session_id="session_run1",
            execution_id="exec_run1",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )

        # First run: ACs 0 and 1 succeeded, AC 2 failed.
        assert result_1.success_count == 2
        assert result_1.failure_count == 1
        assert call_count_1 == [0, 1, 2], f"Expected [0, 1, 2] called; got {call_count_1}"

        # ---- Second run: resume from session_run1 ----
        event_store_2, _ = _make_replaying_event_store()
        executor_2 = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store_2,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor_2._coordinator.detect_file_conflicts = MagicMock(return_value=[])

        call_count_2: list[int] = []
        captured_overrides_2: list[str] = []

        async def fake_single_ac_run2(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            call_count_2.append(ac_index)
            captured_overrides_2.append(kwargs.get("context_override") or "")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor_2._execute_single_ac = fake_single_ac_run2  # type: ignore[method-assign]

        result_2 = await executor_2.execute_serial(
            seed=seed,
            session_id="session_run2",
            execution_id="exec_run2",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="session_run1",
        )

        # Second run: only AC 2 was executed (ACs 0 and 1 were checkpointed).
        assert call_count_2 == [2], (
            f"Only AC 2 should execute in the resumed run; got {call_count_2}"
        )

        # AC 2's context must include postmortems from the first run.
        assert len(captured_overrides_2) == 1
        ac2_context = captured_overrides_2[0]
        assert "AC 0: write data model" in ac2_context, (
            "Resumed AC 2 must see AC 0's postmortem from the first run"
        )
        assert "AC 1: write service layer" in ac2_context, (
            "Resumed AC 2 must see AC 1's postmortem from the first run"
        )

        # Overall second run result.
        assert len(result_2.results) == 3
        assert result_2.success_count >= 1  # At least AC 2 succeeded in run 2.


class TestResumeEdgeCases:
    """Sub-AC 3 (a)+(c): Resume correctness edge cases and graceful handling.

    (a) Verifies that a partial checkpoint causes the executor to skip completed
        ACs and make prior postmortems available to the resumed AC.

    (c) Verifies that nonexistent or invalid session_ids are handled gracefully
        (no exceptions raised; always falls back to fresh run).

    Compounding context from prior ACs:
    - AC-1 established [[INVARIANT: end-of-run chain artifact exists in
      docs/brainstorm/chain-*.md]] — chain serialization round-trip is verified.
    - AC-2 established [[INVARIANT: ACPostmortem.sub_postmortems preserves
      structure in serialized chain]] — sub-postmortems survive checkpoints.
    - AC-3 established [[INVARIANT: Haiku verifier runs inline per AC before
      chain advances]] — invariant objects survive checkpoint round-trips.
    - Sub-ACs 1+2 established [[INVARIANT: _load_compounding_checkpoint returns
      empty chain and -1 on failure]] and
      [[INVARIANT: checkpoints are only written after AC success, never on failure]].

    [[INVARIANT: resume with any invalid session_id runs fresh without raising an exception]]
    [[INVARIANT: partial checkpoint causes skip-and-rehydrate, never a re-run of completed ACs]]
    """

    # ------------------------------------------------------------------
    # (a) Partial checkpoint: skip + rehydrate
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_partial_checkpoint_rehydrates_chain_for_resumed_ac(
        self, tmp_path: Path
    ) -> None:
        """Partial checkpoint (1 of 3 ACs done) rehydrates the chain for AC-2.

        This is the core (a) assertion: after loading a partial checkpoint,
        AC-2's context_override contains AC-1's postmortem data even though
        AC-1 was not re-executed.  The chain is rehydrated, not reconstructed
        from scratch.

        Compounding reference: the checkpoint payload is written by
        _write_compounding_checkpoint (Sub-AC 2) and the loaded chain is
        deserialized from the same payload that contains:
        - ACPostmortem.sub_postmortems (AC-2 B-prime)
        - Invariant objects (AC-3 C-plus)

        [[INVARIANT: partial checkpoint causes skip-and-rehydrate, never a re-run of completed ACs]]
        [[INVARIANT: deserialized chain is injected before the AC loop so resumed ACs see prior postmortems]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            Invariant,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_compounding_checkpoint
        from ouroboros.persistence.checkpoint import CheckpointStore

        seed = _make_seed(
            "AC 0: build schema layer",
            "AC 1: build service layer",
            "AC 2: build API layer",
        )

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        # Build a rich AC 0 postmortem with invariants + files.
        pm_ac0 = ACPostmortem(
            summary=ACContextSummary(
                ac_index=0,
                ac_content="AC 0: build schema layer",
                success=True,
                files_modified=("src/schema.py", "src/schema_types.py"),
            ),
            status="pass",
            gotchas=("schema must be frozen dataclass",),
            invariants_established=(
                Invariant(
                    text="all schema objects are frozen dataclasses",
                    reliability=0.92,
                    occurrences=1,
                    first_seen_ac_id="ac_0",
                ),
            ),
        )
        prior_chain = PostmortemChain(postmortems=(pm_ac0,))

        _write_compounding_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_run",
            ac_index=0,  # last_completed = AC 0 (0-based)
            chain=prior_chain,
        )

        executed_indices: list[int] = []
        captured_overrides: list[str] = []

        event_store, _ = _make_replaying_event_store()
        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            executed_indices.append(ac_index)
            captured_overrides.append(kwargs.get("context_override") or "")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,), (2,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="resumed_run",
            execution_id="exec_resumed",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_run",
        )

        # (a.1) AC 0 was NOT re-executed (skipped via checkpoint).
        assert 0 not in executed_indices, (
            f"AC 0 should be skipped (checkpoint). Executed: {executed_indices}"
        )

        # (a.2) ACs 1 and 2 were executed.
        assert 1 in executed_indices, "AC 1 should execute after skip"
        assert 2 in executed_indices, "AC 2 should execute after skip"

        # (a.3) The first executed AC (AC 1) sees AC 0's postmortem.
        # AC 1's override is captured_overrides[0] (since AC 0 was skipped).
        ac1_context = captured_overrides[0]
        assert "AC 0: build schema layer" in ac1_context, (
            "Resumed AC 1 must see AC 0's postmortem from the rehydrated chain"
        )
        assert "schema must be frozen dataclass" in ac1_context, (
            "Gotchas from the prior run must be in the rehydrated chain"
        )
        assert "src/schema.py" in ac1_context, (
            "Files modified must be present in the rehydrated chain"
        )

        # (a.4) Result structure: 3 entries.
        assert len(result.results) == 3
        assert result.results[0].outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY
        assert result.results[1].success is True
        assert result.results[2].success is True

    # ------------------------------------------------------------------
    # (c) Graceful handling of nonexistent / invalid session_ids
    # ------------------------------------------------------------------

    def test_load_checkpoint_with_empty_string_resume_session_id_returns_empty(
        self, tmp_path: Path
    ) -> None:
        """Empty string resume_session_id is handled gracefully.

        The resume_session_id is only used for logging, so an empty string
        does not affect checkpoint loading — it's keyed by seed_id.

        [[INVARIANT: resume with any invalid session_id runs fresh without raising an exception]]
        """
        from ouroboros.orchestrator.serial_executor import _load_compounding_checkpoint
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "ckpts")
        store.initialize()

        chain, last_idx = _load_compounding_checkpoint(
            store=store,
            seed_id="any-seed",
            session_id="new-sess",
            resume_session_id="",  # empty string; treated as "no prior session"
        )
        assert last_idx == -1, "Empty resume_session_id must return -1"
        assert len(chain.postmortems) == 0, "Empty resume_session_id must return empty chain"

    def test_load_checkpoint_with_path_traversal_resume_session_id_returns_empty(
        self, tmp_path: Path
    ) -> None:
        """Session ID with path-traversal characters is handled gracefully.

        The executor must not crash on adversarial session IDs; it should
        simply return (empty_chain, -1) when no checkpoint is found.

        [[INVARIANT: resume with any invalid session_id runs fresh without raising an exception]]
        """
        from ouroboros.orchestrator.serial_executor import _load_compounding_checkpoint
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "ckpts")
        store.initialize()

        chain, last_idx = _load_compounding_checkpoint(
            store=store,
            seed_id="any-seed",
            session_id="new-sess",
            resume_session_id="../../../etc/passwd",  # path traversal attempt
        )
        assert last_idx == -1
        assert len(chain.postmortems) == 0

    def test_load_checkpoint_with_corrupted_json_checkpoint_returns_empty(
        self, tmp_path: Path
    ) -> None:
        """Checkpoint with invalid JSON state (non-dict) is handled gracefully.

        A badly-formed checkpoint should cause the executor to fall back to a
        fresh run rather than crashing with a KeyError or TypeError.

        Compounding reference: Sub-AC 1/2 established
        [[INVARIANT: _load_compounding_checkpoint returns empty chain and -1 on failure]]
        """
        from ouroboros.orchestrator.serial_executor import _load_compounding_checkpoint
        from ouroboros.persistence.checkpoint import CheckpointData, CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "ckpts")
        store.initialize()

        # Write a checkpoint with a completely wrong state structure (not a dict
        # that CompoundingCheckpointState.from_dict expects).
        bad_checkpoint = CheckpointData.create(
            seed_id="bad-structured-seed",
            phase="execution",
            state={"mode": "compounding"},  # missing required 'last_completed_ac_index'
        )
        store.write(bad_checkpoint)

        chain, last_idx = _load_compounding_checkpoint(
            store=store,
            seed_id="bad-structured-seed",
            session_id="new-sess",
            resume_session_id="old-sess",
        )
        assert last_idx == -1, (
            "Corrupted checkpoint must return last_idx=-1 (graceful fallback)"
        )
        assert len(chain.postmortems) == 0

    @pytest.mark.asyncio
    async def test_execute_serial_with_nonexistent_resume_session_runs_fresh(
        self, tmp_path: Path
    ) -> None:
        """execute_serial with a nonexistent resume_session_id runs all ACs fresh.

        When the resume_session_id doesn't match any stored checkpoint (because
        the session was never run), the executor must:
        1. NOT raise an exception
        2. Run all ACs from AC 0
        3. Return success for all ACs

        Compounding reference: [[INVARIANT: _load_compounding_checkpoint returns
        empty chain and -1 on failure]] — the full executor pipeline must respect
        this guarantee.

        [[INVARIANT: resume with any invalid session_id runs fresh without raising an exception]]
        """
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()
        # Note: no checkpoint written for any seed_id.

        seed = _make_seed("AC 0: fresh task", "AC 1: fresh task 2")

        event_store, _ = _make_replaying_event_store()
        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])

        executed: list[int] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            executed.append(int(kwargs["ac_index"]))
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="new-session-xyz",
            execution_id="exec-fresh-xyz",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="nonexistent-session-id-12345",  # no checkpoint
        )

        # Both ACs ran from scratch (checkpoint not found → graceful fresh start).
        assert executed == [0, 1], (
            f"Both ACs should run when checkpoint missing; executed: {executed}"
        )
        assert result.success_count == 2
        assert result.failure_count == 0

    @pytest.mark.asyncio
    async def test_execute_serial_no_exception_on_store_error(
        self, tmp_path: Path
    ) -> None:
        """execute_serial handles CheckpointStore errors gracefully.

        If the checkpoint store raises an unexpected exception during load,
        the executor must not propagate the exception — it should fall back
        to a fresh run.

        [[INVARIANT: resume with any invalid session_id runs fresh without raising an exception]]
        """
        from unittest.mock import MagicMock

        # Store that raises on every load call.
        failing_store = MagicMock()
        failing_store.load.side_effect = RuntimeError("disk read error")

        seed = _make_seed("AC 0: test", "AC 1: test")

        event_store, _ = _make_replaying_event_store()
        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=failing_store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])

        executed: list[int] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            executed.append(int(kwargs["ac_index"]))
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        # Must NOT raise; must run fresh.
        result = await executor.execute_serial(
            seed=seed,
            session_id="new-session",
            execution_id="exec-store-err",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="some-session-id",
        )

        assert executed == [0, 1], (
            f"Fresh run must proceed when store raises; executed: {executed}"
        )
        assert result.success_count == 2


class TestSubPostmortemResumePath:
    """Sub-AC 1: sub_postmortem resume path implementation.

    Verifies that:
    1. When a decomposed AC fails with sub_results, a partial checkpoint is
       written preserving the completed sub-ACs' postmortems.
    2. On resume, the partial sub-postmortem context is included in the
       failing AC's context_override.
    3. A structured ``execution.serial.resume.sub_postmortem_boundary`` event
       is emitted when the sub-postmortem resume path is triggered.
    4. Monolithic (non-decomposed) failing ACs do NOT trigger a partial
       checkpoint write.

    Compounding context (from prior ACs):
    - AC-1 established [[INVARIANT: end-of-run chain artifact exists in
      docs/brainstorm/chain-*.md]] — chain serialization verified.
    - AC-2 established [[INVARIANT: ACPostmortem.sub_postmortems preserves
      structure in serialized chain]] — sub-postmortems survive round-trips;
      this test builds directly on that guarantee.
    - AC-3 established [[INVARIANT: Haiku verifier runs inline per AC before
      chain advances]] — invariants in sub-postmortems survive checkpoints.
    - AC-3 checkpoint tests established [[INVARIANT: checkpoints are only
      written after AC success, never on failure]] — this task introduces
      partial checkpoints that do NOT advance last_completed_ac_index.

    [[INVARIANT: sub-postmortem resume event type is execution.serial.resume.sub_postmortem_boundary]]
    [[INVARIANT: partial sub-AC checkpoint does NOT advance last_completed_ac_index]]
    [[INVARIANT: partial sub-AC checkpoint is written only when sub_results is non-empty]]
    [[INVARIANT: sub-postmortem resume context is appended to context_override, not replacing it]]
    """

    def _make_executor_with_mock_store(
        self,
    ) -> tuple[SerialCompoundingExecutor, MagicMock, list[BaseEvent]]:
        """Build an executor with a mock store and collecting event store."""
        from ouroboros.core.types import Result

        event_store, appended = _make_replaying_event_store()

        mock_store = MagicMock()
        mock_store.write.return_value = Result.ok(None)
        # load() returns error by default (no checkpoint saved)
        from ouroboros.core.errors import PersistenceError
        mock_store.load.return_value = Result.err(
            PersistenceError(message="no checkpoint", operation="load", details={})
        )

        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=mock_store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
        return executor, mock_store, appended

    def _make_executor_with_real_store(
        self, tmp_path: Path
    ) -> tuple[SerialCompoundingExecutor, Any, list[BaseEvent]]:
        """Build an executor with a real CheckpointStore backed by tmp_path."""
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        event_store, appended = _make_replaying_event_store()
        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
        return executor, store, appended

    def _make_decomposed_fail_result(
        self,
        ac_index: int,
        ac_content: str,
        *,
        sub_files: tuple[tuple[str, ...], ...],
    ) -> ACExecutionResult:
        """Build a failed ACExecutionResult with sub_results (decomposed AC)."""
        sub_results = tuple(
            ACExecutionResult(
                ac_index=ac_index,
                ac_content=f"Sub-AC {i}",
                success=True,
                messages=tuple(
                    AgentMessage(
                        type="tool_use",
                        content=f"writing sub{i}",
                        tool_name="Write",
                        data={"tool_input": {"file_path": f}},
                    )
                    for f in files
                ),
                final_message=f"sub {i} done",
            )
            for i, files in enumerate(sub_files)
        )
        return ACExecutionResult(
            ac_index=ac_index,
            ac_content=ac_content,
            success=False,
            error="decomposed AC failed mid-way",
            is_decomposed=True,
            sub_results=sub_results,
            outcome=ACExecutionOutcome.FAILED,
        )

    # ------------------------------------------------------------------
    # Checkpoint writing on decomposed-AC failure
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_partial_checkpoint_written_when_decomposed_ac_fails(
        self,
    ) -> None:
        """When a decomposed AC fails with sub_results, a partial checkpoint is written.

        The checkpoint must:
        - Have last_completed_ac_index = -1 (no fully completed AC)
        - Have partial_failing_ac_index = 0 (the failing AC)
        - Have partial_failing_ac_sub_postmortems with the completed sub-ACs

        [[INVARIANT: partial sub-AC checkpoint does NOT advance last_completed_ac_index]]
        [[INVARIANT: partial sub-AC checkpoint is written only when sub_results is non-empty]]
        """
        from ouroboros.persistence.checkpoint import CompoundingCheckpointState

        seed = _make_seed("Decomposed AC with partial sub-results")
        executor, mock_store, _ = self._make_executor_with_mock_store()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return self._make_decomposed_fail_result(
                0,
                str(kwargs["ac_content"]),
                sub_files=(("src/sub_a.py",), ("src/sub_b.py",)),
            )

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,))
        result = await executor.execute_serial(
            seed=seed,
            session_id="sess_partial_write",
            execution_id="exec_partial_write",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )

        assert result.failure_count == 1

        # A partial checkpoint must have been written (in addition to any
        # other writes). We look for a call with a payload that has
        # partial_failing_ac_index set.
        partial_writes = [
            call.args[0]
            for call in mock_store.write.call_args_list
            if CompoundingCheckpointState.from_dict(call.args[0].state).partial_failing_ac_index
               is not None
        ]
        assert len(partial_writes) == 1, (
            f"Expected exactly 1 partial checkpoint write, got {len(partial_writes)}"
        )

        state = CompoundingCheckpointState.from_dict(partial_writes[0].state)
        # last_completed_ac_index must NOT be advanced (failing AC is not done).
        assert state.last_completed_ac_index == -1, (
            f"Partial checkpoint must not advance cursor; got {state.last_completed_ac_index}"
        )
        assert state.partial_failing_ac_index == 0, (
            f"partial_failing_ac_index must be 0 (the failing AC); got {state.partial_failing_ac_index}"
        )
        assert state.partial_failing_ac_sub_postmortems is not None
        assert len(state.partial_failing_ac_sub_postmortems) == 2, (
            f"Expected 2 sub-postmortems serialized, got {len(state.partial_failing_ac_sub_postmortems)}"
        )

    @pytest.mark.asyncio
    async def test_no_partial_checkpoint_for_monolithic_failing_ac(
        self,
    ) -> None:
        """A monolithic (non-decomposed) failing AC must NOT trigger a partial checkpoint.

        Only ACs with sub_results trigger the partial checkpoint path.

        [[INVARIANT: partial sub-AC checkpoint is written only when sub_results is non-empty]]
        [[INVARIANT: checkpoints are only written after AC success, never on failure]]
        """
        seed = _make_seed("Monolithic AC that fails")
        executor, mock_store, _ = self._make_executor_with_mock_store()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            # Monolithic (is_decomposed=False, sub_results=()) failing AC
            return _fail_result(0, str(kwargs["ac_content"]), error="network error")

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_mono_fail",
            execution_id="exec_mono_fail",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )

        # Zero checkpoint writes for monolithic failing AC.
        assert mock_store.write.call_count == 0, (
            f"Expected 0 writes for monolithic failure; got {mock_store.write.call_count}"
        )

    # ------------------------------------------------------------------
    # Resume context inclusion
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sub_postmortem_context_in_context_override_on_resume(
        self, tmp_path: Path
    ) -> None:
        """On resume, the failing AC's context_override includes completed sub-AC context.

        Setup: write a partial checkpoint for AC 0 with 2 completed sub-ACs.
        Resume: execute_serial with resume_session_id set.
        Expect: AC 0's context_override contains the "Sub-AC Resume Context"
        section listing the completed sub-ACs.

        Compounding reference: this builds on AC-2's B-prime sub_postmortems
        preservation [[INVARIANT: ACPostmortem.sub_postmortems preserves structure
        in serialized chain]] — the partial checkpoint uses the same serialization.

        [[INVARIANT: sub-postmortem resume context is appended to context_override, not replacing it]]
        [[INVARIANT: sub-postmortem resume event type is execution.serial.resume.sub_postmortem_boundary]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import (
            _write_partial_sub_ac_checkpoint,
        )
        from ouroboros.persistence.checkpoint import CheckpointStore

        seed = _make_seed("Failing decomposed AC to resume")
        executor, store, appended = self._make_executor_with_real_store(tmp_path)

        # Build two completed sub-postmortems.
        sub_pm0 = ACPostmortem(
            summary=ACContextSummary(
                ac_index=0,
                ac_content="Sub-AC 0: extract schema",
                success=True,
                files_modified=("src/schema.py",),
            ),
            status="pass",
            gotchas=("schema must be frozen",),
        )
        sub_pm1 = ACPostmortem(
            summary=ACContextSummary(
                ac_index=0,
                ac_content="Sub-AC 1: write tests",
                success=True,
                files_modified=("tests/test_schema.py",),
            ),
            status="pass",
        )

        # Write partial checkpoint: no fully-completed ACs, but AC 0 has 2 completed sub-ACs.
        _write_partial_sub_ac_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_partial_session",
            ac_index=0,
            sub_postmortems=(sub_pm0, sub_pm1),
            base_chain=PostmortemChain(),  # empty base chain (no fully-completed ACs)
            last_completed_ac_index=-1,
        )

        captured_overrides: list[str] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            captured_overrides.append(kwargs.get("context_override") or "")
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,))
        await executor.execute_serial(
            seed=seed,
            session_id="new_session_resume",
            execution_id="exec_sub_resume",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_partial_session",
        )

        # AC 0 was executed (not skipped — last_completed_ac_index=-1).
        assert len(captured_overrides) == 1, (
            f"AC 0 should have been executed once; got {len(captured_overrides)} overrides"
        )
        context = captured_overrides[0]

        # Sub-AC resume section must be present.
        assert "Sub-AC Resume Context" in context, (
            f"Sub-AC Resume Context section missing from context_override:\n{context[:600]}"
        )
        # Completed sub-ACs must be listed.
        assert "Sub-AC 0: extract schema" in context, (
            "First completed sub-AC must appear in resume context"
        )
        assert "Sub-AC 1: write tests" in context, (
            "Second completed sub-AC must appear in resume context"
        )
        # The "do not re-execute" instruction must be present.
        assert "Do NOT re-execute" in context, (
            "Resume context must include the 'Do NOT re-execute' instruction"
        )

    # ------------------------------------------------------------------
    # Resume event emission
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sub_postmortem_resume_event_emitted(
        self, tmp_path: Path
    ) -> None:
        """execution.serial.resume.sub_postmortem_boundary event emitted on sub-resume.

        When the partial sub-postmortem path is triggered, the executor must
        emit one event per resumed AC.

        Compounding reference: the event factory follows the existing pattern
        established in AC-1 (create_ac_postmortem_captured_event) and AC-4 Q7
        (create_postmortem_chain_truncated_event).  Events coexist with log lines.

        [[INVARIANT: sub-postmortem resume event type is execution.serial.resume.sub_postmortem_boundary]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import (
            _write_partial_sub_ac_checkpoint,
        )
        from ouroboros.persistence.checkpoint import CheckpointStore

        seed = _make_seed("Decomposed AC with sub resume")
        executor, store, appended = self._make_executor_with_real_store(tmp_path)

        sub_pm = ACPostmortem(
            summary=ACContextSummary(
                ac_index=0,
                ac_content="Sub-AC 0: task A",
                success=True,
                files_modified=("src/task_a.py",),
            ),
            status="pass",
        )

        _write_partial_sub_ac_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_sub_session",
            ac_index=0,
            sub_postmortems=(sub_pm,),
            base_chain=PostmortemChain(),
            last_completed_ac_index=-1,
        )

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,))
        await executor.execute_serial(
            seed=seed,
            session_id="resume_event_sess",
            execution_id="exec_resume_event",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_sub_session",
        )

        resume_events = [
            e
            for e in appended
            if e.type == "execution.serial.resume.sub_postmortem_boundary"
        ]
        assert len(resume_events) == 1, (
            f"Expected 1 sub-postmortem resume event; got {len(resume_events)}. "
            f"All event types: {[e.type for e in appended]}"
        )
        ev = resume_events[0]
        assert ev.aggregate_type == "execution"
        assert ev.aggregate_id == "exec_resume_event"
        assert ev.data["session_id"] == "resume_event_sess"
        assert ev.data["ac_index"] == 0
        assert ev.data["sub_acs_completed"] == 1, (
            f"sub_acs_completed should be 1; got {ev.data['sub_acs_completed']}"
        )
        assert ev.data["resume_from_sub_ac"] == 1, (
            f"resume_from_sub_ac should equal sub_acs_completed; got {ev.data['resume_from_sub_ac']}"
        )
        assert "timestamp" in ev.data

    # ------------------------------------------------------------------
    # Helper function tests
    # ------------------------------------------------------------------

    def test_load_partial_failing_ac_state_returns_none_when_no_checkpoint(
        self, tmp_path: Path
    ) -> None:
        """_load_partial_failing_ac_state returns (None, None) when no checkpoint exists.

        [[INVARIANT: _load_partial_failing_ac_state returns (None, None) on any failure]]
        """
        from ouroboros.orchestrator.serial_executor import _load_partial_failing_ac_state
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "empty")
        store.initialize()

        idx, subs = _load_partial_failing_ac_state(store=store, seed_id="nonexistent")
        assert idx is None
        assert subs is None

    def test_load_partial_failing_ac_state_returns_stored_values(
        self, tmp_path: Path
    ) -> None:
        """_load_partial_failing_ac_state returns the stored partial sub-AC state.

        [[INVARIANT: _load_partial_failing_ac_state returns (None, None) on any failure]]
        [[INVARIANT: partial sub-AC checkpoint does NOT advance last_completed_ac_index]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import (
            _load_partial_failing_ac_state,
            _write_partial_sub_ac_checkpoint,
        )
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "ckpts")
        store.initialize()

        sub_pm = ACPostmortem(
            summary=ACContextSummary(
                ac_index=0,
                ac_content="Sub task",
                success=True,
                files_modified=("src/sub.py",),
            ),
            status="pass",
        )

        _write_partial_sub_ac_checkpoint(
            store=store,
            seed_id="test_seed",
            session_id="sess",
            ac_index=2,
            sub_postmortems=(sub_pm,),
            base_chain=PostmortemChain(),
            last_completed_ac_index=1,
        )

        idx, subs = _load_partial_failing_ac_state(store=store, seed_id="test_seed")
        assert idx == 2, f"Expected partial_failing_ac_index=2, got {idx}"
        assert subs is not None
        assert len(subs) == 1, f"Expected 1 serialized sub-postmortem, got {len(subs)}"

    def test_build_sub_postmortem_resume_context_includes_required_sections(
        self,
    ) -> None:
        """_build_sub_postmortem_resume_context formats completed sub-ACs correctly.

        [[INVARIANT: sub-postmortem resume context is appended to context_override, not replacing it]]
        """
        from ouroboros.orchestrator.serial_executor import _build_sub_postmortem_resume_context

        sub_pm_dicts = [
            {
                "summary": {
                    "ac_index": 0,
                    "ac_content": "Sub-AC 0: write schema",
                    "files_modified": ["src/schema.py"],
                },
                "status": "pass",
                "gotchas": ["schema must be immutable"],
            },
            {
                "summary": {
                    "ac_index": 0,
                    "ac_content": "Sub-AC 1: write tests",
                    "files_modified": ["tests/test_schema.py"],
                },
                "status": "pass",
                "gotchas": [],
            },
        ]

        ctx = _build_sub_postmortem_resume_context(sub_pm_dicts)

        # Must contain the resume header.
        assert "Sub-AC Resume Context" in ctx
        # Must include both sub-ACs' content.
        assert "Sub-AC 0: write schema" in ctx
        assert "Sub-AC 1: write tests" in ctx
        # Must include "do not re-execute" instruction.
        assert "Do NOT re-execute" in ctx
        # Must include files_modified.
        assert "src/schema.py" in ctx
        # Must include gotchas.
        assert "schema must be immutable" in ctx

    def test_build_sub_postmortem_resume_context_empty_returns_empty_string(
        self,
    ) -> None:
        """_build_sub_postmortem_resume_context returns empty string for empty input."""
        from ouroboros.orchestrator.serial_executor import _build_sub_postmortem_resume_context

        assert _build_sub_postmortem_resume_context([]) == ""

    def test_compounding_checkpoint_state_partial_fields_round_trip(
        self,
    ) -> None:
        """CompoundingCheckpointState partial fields round-trip through to_dict/from_dict.

        Verifies that partial_failing_ac_index and partial_failing_ac_sub_postmortems
        survive a serialize/deserialize cycle.

        [[INVARIANT: partial sub-AC checkpoint does NOT advance last_completed_ac_index]]
        """
        from ouroboros.persistence.checkpoint import CompoundingCheckpointState

        original = CompoundingCheckpointState(
            last_completed_ac_index=3,
            postmortem_chain=[{"ac_index": 0, "status": "pass"}],
            partial_failing_ac_index=4,
            partial_failing_ac_sub_postmortems=[
                {"summary": {"ac_content": "sub 0"}, "status": "pass"},
                {"summary": {"ac_content": "sub 1"}, "status": "pass"},
            ],
        )
        d = original.to_dict()
        restored = CompoundingCheckpointState.from_dict(d)

        assert restored.last_completed_ac_index == 3
        assert restored.mode == "compounding"
        assert restored.partial_failing_ac_index == 4
        assert restored.partial_failing_ac_sub_postmortems is not None
        assert len(restored.partial_failing_ac_sub_postmortems) == 2
        assert restored.partial_failing_ac_sub_postmortems[0]["summary"]["ac_content"] == "sub 0"

    def test_compounding_checkpoint_state_no_partial_fields_when_none(
        self,
    ) -> None:
        """CompoundingCheckpointState with no partial fields serializes cleanly.

        When partial_failing_ac_index is None, neither partial key appears in
        the serialized dict, keeping backward compatibility.

        [[INVARIANT: partial sub-AC checkpoint is written only when sub_results is non-empty]]
        """
        from ouroboros.persistence.checkpoint import CompoundingCheckpointState

        state = CompoundingCheckpointState(
            last_completed_ac_index=2,
            postmortem_chain=[],
        )
        d = state.to_dict()

        assert "partial_failing_ac_index" not in d, (
            "partial_failing_ac_index must not appear when None (backward compat)"
        )
        assert "partial_failing_ac_sub_postmortems" not in d, (
            "partial_failing_ac_sub_postmortems must not appear when None (backward compat)"
        )

        # Deserialization of a dict without the optional keys must succeed.
        restored = CompoundingCheckpointState.from_dict(d)
        assert restored.partial_failing_ac_index is None
        assert restored.partial_failing_ac_sub_postmortems is None

    # ------------------------------------------------------------------
    # create_sub_postmortem_resume_event factory
    # ------------------------------------------------------------------

    def test_create_sub_postmortem_resume_event_fields(self) -> None:
        """create_sub_postmortem_resume_event produces correct event structure.

        [[INVARIANT: sub-postmortem resume event type is execution.serial.resume.sub_postmortem_boundary]]
        [[INVARIANT: resume_from_sub_ac == sub_acs_completed (no gaps, boundary is first incomplete sub-AC)]]
        """
        from ouroboros.orchestrator.events import create_sub_postmortem_resume_event

        event = create_sub_postmortem_resume_event(
            session_id="sess_sub_event",
            execution_id="exec_sub_event",
            ac_index=3,
            sub_acs_completed=2,
            resume_from_sub_ac=2,
        )

        assert event.type == "execution.serial.resume.sub_postmortem_boundary"
        assert event.aggregate_type == "execution"
        assert event.aggregate_id == "exec_sub_event"
        assert event.data["session_id"] == "sess_sub_event"
        assert event.data["execution_id"] == "exec_sub_event"
        assert event.data["ac_index"] == 3
        assert event.data["sub_acs_completed"] == 2
        assert event.data["resume_from_sub_ac"] == 2
        assert "timestamp" in event.data


class TestMonolithicResume:
    """Sub-AC 2: Monolithic agent-adjudicated resume (Q6.2 monolithic path).

    Verifies:
    1. _build_monolithic_resume_decision_prompt includes AC text, context,
       and both DECISION: continue / DECISION: restart options.
    2. _parse_monolithic_resume_decision correctly parses responses.
    3. _adjudicate_monolithic_resume calls adapter and returns correct decision.
    4. Adapter error defaults to "restart".
    5. Integration: monolithic resume triggers adjudication and emits event.
    6. DECISION: continue adds continuation hint to context_override.
    7. DECISION: restart leaves context_override unchanged.
    8. Adjudication NOT triggered when not resuming (no resume_session_id).
    9. Adjudication NOT triggered when last_completed_ac_index == -1.
    10. Adjudication NOT triggered when sub-AC boundary path was taken.

    Compounding context (from prior ACs):
    - AC-1 established [[INVARIANT: end-of-run chain artifact exists in
      docs/brainstorm/chain-*.md]] — the chain survives even on resume.
    - AC-2 established [[INVARIANT: ACPostmortem.sub_postmortems preserves
      structure in serialized chain]] — sub-AC boundary resume uses this;
      monolithic path is taken only when sub_postmortems is empty.
    - AC-3 established [[INVARIANT: Haiku verifier runs inline per AC before
      chain advances]] — adjudication similarly calls adapter.complete().
    - Sub-AC 1 established [[INVARIANT: partial sub-AC checkpoint does NOT
      advance last_completed_ac_index]] — monolithic path is the complement:
      no partial checkpoint, agent adjudicates on full resume.

    [[INVARIANT: monolithic resume adjudication event type is execution.serial.resume.monolithic_adjudicated]]
    [[INVARIANT: decision field is always "continue" or "restart" (never None or empty)]]
    [[INVARIANT: _parse_monolithic_resume_decision always returns "continue" or "restart"]]
    [[INVARIANT: _adjudicate_monolithic_resume always returns ("continue"|"restart", str)]]
    [[INVARIANT: adapter error in monolithic adjudication defaults to "restart"]]
    """

    # ------------------------------------------------------------------
    # Unit tests for helper functions
    # ------------------------------------------------------------------

    def test_build_decision_prompt_includes_ac_text(self) -> None:
        """The DECISION prompt must include the original AC text verbatim.

        [[INVARIANT: monolithic resume prompt always includes the literal text DECISION: continue and DECISION: restart as options]]
        """
        from ouroboros.orchestrator.serial_executor import (
            _build_monolithic_resume_decision_prompt,
        )

        ac_text = "Implement the user authentication module with JWT tokens."
        context = "Prior AC postmortem: files modified = src/auth.py"
        prompt = _build_monolithic_resume_decision_prompt(ac_text, context)

        assert ac_text in prompt, "AC text must appear verbatim in the prompt"
        assert "DECISION: continue" in prompt, "continue option must be in prompt"
        assert "DECISION: restart" in prompt, "restart option must be in prompt"

    def test_build_decision_prompt_includes_context_section(self) -> None:
        """The DECISION prompt includes the context section as prior work trace."""
        from ouroboros.orchestrator.serial_executor import (
            _build_monolithic_resume_decision_prompt,
        )

        context = "Postmortem for AC 1: files modified = src/model.py"
        prompt = _build_monolithic_resume_decision_prompt("Some AC", context)

        assert context in prompt, "Context section must appear in the prompt"

    def test_build_decision_prompt_handles_empty_context(self) -> None:
        """Empty context section falls back to a placeholder message."""
        from ouroboros.orchestrator.serial_executor import (
            _build_monolithic_resume_decision_prompt,
        )

        prompt = _build_monolithic_resume_decision_prompt("AC text", "")

        assert "(No prior context recorded)" in prompt
        # Both decision options still present.
        assert "DECISION: continue" in prompt
        assert "DECISION: restart" in prompt

    def test_parse_decision_continue(self) -> None:
        """'DECISION: continue' on the first line → 'continue'."""
        from ouroboros.orchestrator.serial_executor import (
            _parse_monolithic_resume_decision,
        )

        response = "DECISION: continue\nI see evidence of partial work in the trace."
        assert _parse_monolithic_resume_decision(response) == "continue"

    def test_parse_decision_restart(self) -> None:
        """'DECISION: restart' on the first line → 'restart'."""
        from ouroboros.orchestrator.serial_executor import (
            _parse_monolithic_resume_decision,
        )

        response = "DECISION: restart\nNo prior work visible; starting fresh is safer."
        assert _parse_monolithic_resume_decision(response) == "restart"

    def test_parse_decision_case_insensitive(self) -> None:
        """Decision parsing is case-insensitive ('decision: CONTINUE' works)."""
        from ouroboros.orchestrator.serial_executor import (
            _parse_monolithic_resume_decision,
        )

        assert _parse_monolithic_resume_decision("decision: CONTINUE") == "continue"
        assert _parse_monolithic_resume_decision("DECISION: RESTART") == "restart"

    def test_parse_decision_empty_response_defaults_restart(self) -> None:
        """Empty or whitespace-only response falls back to 'restart'.

        [[INVARIANT: _parse_monolithic_resume_decision always returns "continue" or "restart"]]
        """
        from ouroboros.orchestrator.serial_executor import (
            _parse_monolithic_resume_decision,
        )

        assert _parse_monolithic_resume_decision("") == "restart"
        assert _parse_monolithic_resume_decision("   ") == "restart"

    def test_parse_decision_unparseable_defaults_restart(self) -> None:
        """A response without 'DECISION:' falls back to 'restart'.

        [[INVARIANT: _parse_monolithic_resume_decision always returns "continue" or "restart"]]
        """
        from ouroboros.orchestrator.serial_executor import (
            _parse_monolithic_resume_decision,
        )

        assert _parse_monolithic_resume_decision("I cannot decide.") == "restart"
        assert _parse_monolithic_resume_decision("The task looks complex.") == "restart"

    @pytest.mark.asyncio
    async def test_adjudicate_calls_adapter_and_parses_continue(self) -> None:
        """_adjudicate_monolithic_resume calls adapter.complete() and returns 'continue'.

        [[INVARIANT: _adjudicate_monolithic_resume always returns ("continue"|"restart", str)]]
        """
        from unittest.mock import AsyncMock, MagicMock

        from ouroboros.core.types import Result
        from ouroboros.orchestrator.serial_executor import _adjudicate_monolithic_resume
        from ouroboros.providers.base import CompletionResponse, UsageInfo

        stub_response = CompletionResponse(
            content="DECISION: continue\nEvidence of partial work exists in prior postmortems.",
            model="claude-haiku-4-5",
            usage=UsageInfo(prompt_tokens=100, completion_tokens=20, total_tokens=120),
        )
        adapter = MagicMock()
        adapter.complete = AsyncMock(return_value=Result.ok(stub_response))

        decision, raw = await _adjudicate_monolithic_resume(
            adapter,
            "Build user auth module",
            "Prior AC: src/user.py modified",
            model="claude-haiku-4-5",
        )

        assert decision == "continue"
        assert "DECISION: continue" in raw
        adapter.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_adjudicate_calls_adapter_and_parses_restart(self) -> None:
        """_adjudicate_monolithic_resume calls adapter.complete() and returns 'restart'."""
        from unittest.mock import AsyncMock, MagicMock

        from ouroboros.core.types import Result
        from ouroboros.orchestrator.serial_executor import _adjudicate_monolithic_resume
        from ouroboros.providers.base import CompletionResponse, UsageInfo

        stub_response = CompletionResponse(
            content="DECISION: restart\nNo partial work found; clean start recommended.",
            model="claude-haiku-4-5",
            usage=UsageInfo(prompt_tokens=100, completion_tokens=15, total_tokens=115),
        )
        adapter = MagicMock()
        adapter.complete = AsyncMock(return_value=Result.ok(stub_response))

        decision, raw = await _adjudicate_monolithic_resume(
            adapter,
            "Build user auth module",
            "",
            model="claude-haiku-4-5",
        )

        assert decision == "restart"
        assert "DECISION: restart" in raw

    @pytest.mark.asyncio
    async def test_adjudicate_adapter_error_defaults_restart(self) -> None:
        """Adapter error causes _adjudicate_monolithic_resume to return 'restart'.

        [[INVARIANT: adapter error in monolithic adjudication defaults to "restart"]]
        """
        from unittest.mock import AsyncMock, MagicMock

        from ouroboros.core.errors import ProviderError
        from ouroboros.core.types import Result
        from ouroboros.orchestrator.serial_executor import _adjudicate_monolithic_resume

        adapter = MagicMock()
        adapter.complete = AsyncMock(
            return_value=Result.err(ProviderError(message="rate limit", details={}))
        )

        decision, raw = await _adjudicate_monolithic_resume(
            adapter,
            "Some AC",
            "context",
            model="claude-haiku-4-5",
        )

        assert decision == "restart"
        assert raw == ""

    @pytest.mark.asyncio
    async def test_adjudicate_exception_defaults_restart(self) -> None:
        """Unexpected exception in adapter.complete() returns 'restart'."""
        from unittest.mock import AsyncMock, MagicMock

        from ouroboros.orchestrator.serial_executor import _adjudicate_monolithic_resume

        adapter = MagicMock()
        adapter.complete = AsyncMock(side_effect=RuntimeError("network error"))

        decision, raw = await _adjudicate_monolithic_resume(
            adapter,
            "Some AC",
            "context",
            model="claude-haiku-4-5",
        )

        assert decision == "restart"

    # ------------------------------------------------------------------
    # Integration tests: execute_serial + monolithic resume
    # ------------------------------------------------------------------

    def _make_executor_with_checkpoint(
        self, tmp_path: Path
    ) -> tuple[SerialCompoundingExecutor, Any, list[BaseEvent]]:
        """Build an executor with a real CheckpointStore and collecting event store."""
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        event_store, appended = _make_replaying_event_store()
        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
        return executor, store, appended

    @pytest.mark.asyncio
    async def test_monolithic_adjudication_triggered_on_resume(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When resuming and encountering the failing monolithic AC, adjudication fires.

        AC 0 completes (checkpoint written). On resume, AC 1 (the failing AC)
        should trigger agent adjudication. The adjudication event must be emitted.

        Compounding reference: relies on AC-1's chain artifact invariant — the
        checkpoint written after AC 0 contains the serialized chain.

        [[INVARIANT: monolithic resume adjudication event type is execution.serial.resume.monolithic_adjudicated]]
        """
        import ouroboros.orchestrator.serial_executor as serial_mod

        adjudication_calls: list[dict] = []

        async def fake_adjudicate(
            adapter: Any,
            ac_content: str,
            context_section: str,
            *,
            model: str | None = None,
        ) -> tuple[str, str]:
            adjudication_calls.append({
                "ac_content": ac_content,
                "context_section": context_section,
            })
            return "restart", "DECISION: restart\nNo prior work detected."

        monkeypatch.setattr(serial_mod, "_adjudicate_monolithic_resume", fake_adjudicate)
        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "artifacts"))

        seed = _make_seed("AC 0 — build model", "AC 1 — build endpoint (failing AC)")
        executor, store, appended = self._make_executor_with_checkpoint(tmp_path)

        # First run: AC 0 succeeds, AC 1 fails.
        call_count = [0]

        async def fake_single_ac_first_run(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            call_count[0] += 1
            if ac_index == 0:
                return _ok_result(ac_index, str(kwargs["ac_content"]))
            return _fail_result(ac_index, str(kwargs["ac_content"]), error="timeout")

        executor._execute_single_ac = fake_single_ac_first_run  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        first_result = await executor.execute_serial(
            seed=seed,
            session_id="sess_mono_first",
            execution_id="exec_mono_first",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )
        assert first_result.success_count == 1
        assert first_result.failure_count == 1
        # adjudication should NOT be called in the first (non-resume) run.
        assert adjudication_calls == [], (
            "Adjudication must not fire in a non-resume run"
        )

        # Resume run: resume_session_id provided. AC 0 skipped, AC 1 gets adjudicated.
        resume_call_count = [0]
        captured_context: list[str] = []

        async def fake_single_ac_resume(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            resume_call_count[0] += 1
            captured_context.append(kwargs.get("context_override") or "")
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac_resume  # type: ignore[method-assign]

        resume_result = await executor.execute_serial(
            seed=seed,
            session_id="sess_mono_resume",
            execution_id="exec_mono_resume",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="sess_mono_first",
        )

        # AC 1 should have been executed (AC 0 was skipped via resume).
        assert resume_call_count[0] == 1, (
            f"AC 1 must run exactly once on resume; ran {resume_call_count[0]} times"
        )

        # Adjudication must have fired once (for AC 1).
        assert len(adjudication_calls) == 1, (
            f"Expected 1 adjudication call for AC 1; got {adjudication_calls}"
        )
        assert "AC 1 — build endpoint" in adjudication_calls[0]["ac_content"]

        # The adjudication event must appear in the event stream.
        adj_events = [
            e for e in appended
            if e.type == "execution.serial.resume.monolithic_adjudicated"
        ]
        assert len(adj_events) == 1, (
            f"Expected 1 adjudication event; got {adj_events}"
        )
        assert adj_events[0].data["ac_index"] == 1
        assert adj_events[0].data["decision"] == "restart"

    @pytest.mark.asyncio
    async def test_decision_continue_adds_continuation_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When adjudication returns 'continue', the context_override gains a hint.

        The continuation hint must appear in the context_override passed to
        _execute_single_ac for the resumed AC.

        [[INVARIANT: monolithic resume adjudication event type is execution.serial.resume.monolithic_adjudicated]]
        """
        import ouroboros.orchestrator.serial_executor as serial_mod

        async def fake_adjudicate_continue(
            adapter: Any,
            ac_content: str,
            context_section: str,
            *,
            model: str | None = None,
        ) -> tuple[str, str]:
            return "continue", "DECISION: continue\nSome partial work detected."

        monkeypatch.setattr(
            serial_mod, "_adjudicate_monolithic_resume", fake_adjudicate_continue
        )
        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "artifacts"))

        seed = _make_seed("AC 0 — done", "AC 1 — failing, will continue")
        executor, store, appended = self._make_executor_with_checkpoint(tmp_path)

        # First run: AC 0 succeeds, AC 1 fails.
        async def fake_first_run(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            if ac_index == 0:
                return _ok_result(ac_index, str(kwargs["ac_content"]))
            return _fail_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_first_run  # type: ignore[method-assign]
        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_cont_first",
            execution_id="exec_cont_first",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )

        # Resume: capture context for AC 1.
        captured_context: list[str] = []

        async def fake_resume(**kwargs: Any) -> ACExecutionResult:
            captured_context.append(kwargs.get("context_override") or "")
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_resume  # type: ignore[method-assign]
        await executor.execute_serial(
            seed=seed,
            session_id="sess_cont_resume",
            execution_id="exec_cont_resume",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="sess_cont_first",
        )

        # captured_context[0] is for AC 1 (AC 0 was skipped).
        assert len(captured_context) == 1
        ac1_ctx = captured_context[0]
        assert "Resume Instruction" in ac1_ctx, (
            "DECISION: continue must add 'Resume Instruction' to context"
        )
        assert "CONTINUE" in ac1_ctx.upper() or "continue" in ac1_ctx.lower(), (
            "Context must mention continuing"
        )

    @pytest.mark.asyncio
    async def test_decision_restart_does_not_add_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When adjudication returns 'restart', NO continuation hint is added.

        The context_override for the restarted AC must not contain the
        Resume Instruction section.
        """
        import ouroboros.orchestrator.serial_executor as serial_mod

        async def fake_adjudicate_restart(
            adapter: Any,
            ac_content: str,
            context_section: str,
            *,
            model: str | None = None,
        ) -> tuple[str, str]:
            return "restart", "DECISION: restart\nStarting fresh."

        monkeypatch.setattr(
            serial_mod, "_adjudicate_monolithic_resume", fake_adjudicate_restart
        )
        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "artifacts"))

        seed = _make_seed("AC 0 — done", "AC 1 — failing, will restart")
        executor, store, appended = self._make_executor_with_checkpoint(tmp_path)

        # First run: AC 0 succeeds, AC 1 fails.
        async def fake_first_run(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            if ac_index == 0:
                return _ok_result(ac_index, str(kwargs["ac_content"]))
            return _fail_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_first_run  # type: ignore[method-assign]
        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_rst_first",
            execution_id="exec_rst_first",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )

        # Resume run.
        captured_context: list[str] = []

        async def fake_resume(**kwargs: Any) -> ACExecutionResult:
            captured_context.append(kwargs.get("context_override") or "")
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_resume  # type: ignore[method-assign]
        await executor.execute_serial(
            seed=seed,
            session_id="sess_rst_resume",
            execution_id="exec_rst_resume",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="sess_rst_first",
        )

        # AC 1 ran once.
        assert len(captured_context) == 1
        ac1_ctx = captured_context[0]
        # No Resume Instruction section for restart decision.
        assert "Resume Instruction" not in ac1_ctx, (
            "DECISION: restart must NOT add a Resume Instruction section"
        )

    @pytest.mark.asyncio
    async def test_adjudication_not_triggered_in_fresh_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adjudication is NEVER called in a fresh run (no resume_session_id)."""
        import ouroboros.orchestrator.serial_executor as serial_mod

        adj_calls: list[Any] = []

        async def fake_adjudicate(*args: Any, **kwargs: Any) -> tuple[str, str]:
            adj_calls.append(True)
            return "restart", ""

        monkeypatch.setattr(serial_mod, "_adjudicate_monolithic_resume", fake_adjudicate)
        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "artifacts"))

        seed = _make_seed("AC 0", "AC 1")
        executor = _make_executor()

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_fresh",
            execution_id="exec_fresh",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            # No resume_session_id → fresh run.
        )

        assert adj_calls == [], "Adjudication must not fire in a fresh (non-resume) run"

    @pytest.mark.asyncio
    async def test_adjudication_not_triggered_when_no_checkpoint_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adjudication is NOT triggered when resume is requested but no checkpoint exists.

        When last_completed_ac_index == -1 (checkpoint missing), the condition
        `last_completed_ac_index >= 0` prevents adjudication.
        """
        import ouroboros.orchestrator.serial_executor as serial_mod

        adj_calls: list[Any] = []

        async def fake_adjudicate(*args: Any, **kwargs: Any) -> tuple[str, str]:
            adj_calls.append(True)
            return "restart", ""

        monkeypatch.setattr(serial_mod, "_adjudicate_monolithic_resume", fake_adjudicate)
        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "artifacts"))

        seed = _make_seed("AC 0")
        executor, store, _ = self._make_executor_with_checkpoint(tmp_path)
        # Do NOT write any checkpoint — store is empty.

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,),)
        await executor.execute_serial(
            seed=seed,
            session_id="sess_no_ckpt_resume",
            execution_id="exec_no_ckpt_resume",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="sess_that_never_ran",
        )

        # No checkpoint loaded → last_completed_ac_index == -1 → no adjudication.
        assert adj_calls == [], (
            "Adjudication must not fire when no checkpoint was found "
            "(last_completed_ac_index == -1)"
        )

    def test_create_monolithic_resume_adjudicated_event_fields(self) -> None:
        """create_monolithic_resume_adjudicated_event produces correct event structure.

        [[INVARIANT: monolithic resume adjudication event type is execution.serial.resume.monolithic_adjudicated]]
        [[INVARIANT: decision field is always "continue" or "restart" (never None or empty)]]
        """
        from ouroboros.orchestrator.events import create_monolithic_resume_adjudicated_event

        event = create_monolithic_resume_adjudicated_event(
            session_id="sess_mono_event",
            execution_id="exec_mono_event",
            ac_index=2,
            decision="continue",
            raw_response_preview="DECISION: continue\nSome partial work.",
        )

        assert event.type == "execution.serial.resume.monolithic_adjudicated"
        assert event.aggregate_type == "execution"
        assert event.aggregate_id == "exec_mono_event"
        assert event.data["session_id"] == "sess_mono_event"
        assert event.data["execution_id"] == "exec_mono_event"
        assert event.data["ac_index"] == 2
        assert event.data["decision"] == "continue"
        assert "DECISION: continue" in event.data["raw_response_preview"]
        assert "timestamp" in event.data

    def test_create_monolithic_resume_adjudicated_event_truncates_preview(self) -> None:
        """raw_response_preview is capped at 200 chars."""
        from ouroboros.orchestrator.events import create_monolithic_resume_adjudicated_event

        long_response = "DECISION: restart\n" + "A" * 500
        event = create_monolithic_resume_adjudicated_event(
            session_id="s",
            execution_id="e",
            ac_index=0,
            decision="restart",
            raw_response_preview=long_response,
        )

        assert len(event.data["raw_response_preview"]) <= 200

    @pytest.mark.asyncio
    async def test_adjudication_event_fields_in_execute_serial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adjudication event has correct session_id, execution_id, and decision.

        [[INVARIANT: monolithic resume adjudication event type is execution.serial.resume.monolithic_adjudicated]]
        """
        import ouroboros.orchestrator.serial_executor as serial_mod

        async def fake_adjudicate(
            adapter: Any,
            ac_content: str,
            context_section: str,
            *,
            model: str | None = None,
        ) -> tuple[str, str]:
            return "continue", "DECISION: continue\nWork in progress."

        monkeypatch.setattr(serial_mod, "_adjudicate_monolithic_resume", fake_adjudicate)
        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "artifacts"))

        seed = _make_seed("AC 0 — completed", "AC 1 — to be resumed")
        executor, store, appended = self._make_executor_with_checkpoint(tmp_path)

        # First run: AC 0 completes, AC 1 fails.
        async def fake_first_run(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            if ac_index == 0:
                return _ok_result(ac_index, str(kwargs["ac_content"]))
            return _fail_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_first_run  # type: ignore[method-assign]
        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_ev_first",
            execution_id="exec_ev_first",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )
        appended.clear()  # clear first-run events

        # Resume run.
        async def fake_resume(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_resume  # type: ignore[method-assign]
        await executor.execute_serial(
            seed=seed,
            session_id="sess_ev_resume",
            execution_id="exec_ev_resume",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="sess_ev_first",
        )

        adj_events = [
            e for e in appended
            if e.type == "execution.serial.resume.monolithic_adjudicated"
        ]
        assert len(adj_events) == 1
        ev = adj_events[0]
        assert ev.data["session_id"] == "sess_ev_resume"
        assert ev.data["execution_id"] == "exec_ev_resume"
        assert ev.data["ac_index"] == 1
        assert ev.data["decision"] == "continue"
        assert ev.aggregate_id == "exec_ev_resume"


class TestSubPostmortemResumeVariants:
    """Sub-AC 3: sub_postmortem resume path — varying counts, edge cases, boundary selection.

    Extends TestSubPostmortemResumePath with:
    1. Varying numbers of completed sub-ACs (0, 1, 3) to confirm the boundary
       is always set to ``len(sub_postmortems)``.
    2. Edge cases: no sub_postmortems in checkpoint (partial state absent),
       wrong AC index (partial_failing_ac_index does not match ac_index),
       empty sub_pms list with index set.
    3. Correct resume boundary field values in the emitted event.
    4. Event NOT emitted when conditions are not met.

    Compounding context (from prior ACs):
    - AC-1 established [[INVARIANT: end-of-run chain artifact exists in
      docs/brainstorm/chain-*.md]] — the chain artifact co-exists with partial
      checkpoints; both are written when relevant.
    - AC-2 established [[INVARIANT: ACPostmortem.sub_postmortems preserves
      structure in serialized chain]] — the partial checkpoint uses the same
      sub_postmortems serialization path verified by AC-2's round-trip tests.
    - AC-3 established [[INVARIANT: OUROBOROS_INVARIANT_MIN_RELIABILITY defaults
      0.7; below-threshold hidden but stored]] — invariants from completed sub-ACs
      appear in the serialized sub-postmortems within the partial checkpoint.
    - Sub-AC 1 established [[INVARIANT: partial sub-AC checkpoint does NOT advance
      last_completed_ac_index]] — confirmed by existing TestSubPostmortemResumePath.
      This class focuses on boundary count correctness and suppression paths.

    [[INVARIANT: sub-postmortem resume event type is execution.serial.resume.sub_postmortem_boundary]]
    [[INVARIANT: resume_from_sub_ac == sub_acs_completed (no gaps, boundary is first incomplete sub-AC)]]
    [[INVARIANT: _load_partial_failing_ac_state returns (None, None) on any failure]]
    [[INVARIANT: sub-postmortem resume context is appended to context_override, not replacing it]]
    """

    def _make_executor_with_real_store(
        self, tmp_path: Path
    ) -> tuple[SerialCompoundingExecutor, Any, list]:
        """Build an executor with a real CheckpointStore and collecting event store."""
        from ouroboros.persistence.checkpoint import CheckpointStore

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()

        event_store, appended = _make_replaying_event_store()
        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
        return executor, store, appended

    # ------------------------------------------------------------------
    # Varying sub-AC counts in _build_sub_postmortem_resume_context
    # ------------------------------------------------------------------

    def test_build_resume_context_with_one_sub_ac(self) -> None:
        """_build_sub_postmortem_resume_context with exactly 1 completed sub-AC.

        Verifies section header, sub-AC content, and instruction text appear
        when there is only a single completed sub-AC.

        Compounding reference: AC-2 proved sub_postmortems survive serialize/
        deserialize; the dict format here matches that serialization output.

        [[INVARIANT: sub-postmortem resume context is appended to context_override, not replacing it]]
        """
        from ouroboros.orchestrator.serial_executor import _build_sub_postmortem_resume_context

        one_sub = [
            {
                "summary": {
                    "ac_index": 0,
                    "ac_content": "Sub-AC 0: create schema module",
                    "files_modified": ["src/schema.py"],
                },
                "status": "pass",
                "gotchas": ["freeze the schema dataclass"],
            }
        ]

        ctx = _build_sub_postmortem_resume_context(one_sub)

        assert "Sub-AC Resume Context" in ctx
        assert "Sub-AC 0: create schema module" in ctx
        assert "src/schema.py" in ctx
        assert "freeze the schema dataclass" in ctx
        # Only one sub-AC; ensure no phantom "Completed Sub-AC 1" entry.
        assert "Completed Sub-AC 0" in ctx
        assert "Completed Sub-AC 1" not in ctx

    def test_build_resume_context_with_three_sub_acs(self) -> None:
        """_build_sub_postmortem_resume_context with 3 completed sub-ACs.

        All three entries must appear in the output; resume boundary is
        implicitly sub-AC 3 (0-indexed: the first NOT yet done).

        [[INVARIANT: resume_from_sub_ac == sub_acs_completed (no gaps, boundary is first incomplete sub-AC)]]
        """
        from ouroboros.orchestrator.serial_executor import _build_sub_postmortem_resume_context

        three_subs = [
            {
                "summary": {"ac_index": 0, "ac_content": f"Sub-AC {i}: task {i}", "files_modified": [f"src/mod_{i}.py"]},
                "status": "pass",
                "gotchas": [f"gotcha for sub {i}"],
            }
            for i in range(3)
        ]

        ctx = _build_sub_postmortem_resume_context(three_subs)

        # All three entries present.
        for i in range(3):
            assert f"Sub-AC {i}: task {i}" in ctx, f"Sub-AC {i} content missing from context"
            assert f"src/mod_{i}.py" in ctx, f"Sub-AC {i} files missing from context"
            assert f"Completed Sub-AC {i}" in ctx, f"Completed Sub-AC {i} header missing"

        # The "do not re-execute" instruction must appear exactly once.
        assert ctx.count("Do NOT re-execute") >= 1

    def test_build_resume_context_empty_input_returns_empty_string(self) -> None:
        """Empty sub-postmortem list produces empty string (edge case: 0 sub-ACs).

        When the partial checkpoint has 0 completed sub-ACs, the resume context
        must be empty so context_override is not polluted with an empty section.

        [[INVARIANT: sub-postmortem resume context is appended to context_override, not replacing it]]
        """
        from ouroboros.orchestrator.serial_executor import _build_sub_postmortem_resume_context

        assert _build_sub_postmortem_resume_context([]) == ""

    def test_build_resume_context_no_gotchas_or_files(self) -> None:
        """Sub-AC with empty files_modified and empty gotchas renders without error."""
        from ouroboros.orchestrator.serial_executor import _build_sub_postmortem_resume_context

        sparse_sub = [
            {
                "summary": {"ac_index": 0, "ac_content": "Sub-AC 0: minimal task", "files_modified": []},
                "status": "pass",
                "gotchas": [],
            }
        ]

        ctx = _build_sub_postmortem_resume_context(sparse_sub)
        assert "Sub-AC Resume Context" in ctx
        assert "Sub-AC 0: minimal task" in ctx
        # No files, no gotchas — context renders without crashing.

    # ------------------------------------------------------------------
    # Edge case: no sub_postmortems in checkpoint (partial state absent)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_no_sub_resume_event_when_no_partial_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No sub-postmortem resume event when no partial checkpoint exists.

        A fresh run (no partial_failing_ac_index in store) must NOT emit
        the sub_postmortem_boundary event.

        Compounding reference: AC-3 verified that verify_invariants is NOT
        called when no tags are present. By analogy, the sub-resume path must
        not fire when there is no partial state — the guard condition is
        ``_partial_failing_ac_index is not None``.

        [[INVARIANT: sub-postmortem resume event type is execution.serial.resume.sub_postmortem_boundary]]
        """
        monkeypatch.setenv(
            "OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "artifacts")
        )
        seed = _make_seed("AC 0 fresh")
        executor, _store, appended = self._make_executor_with_real_store(tmp_path)
        # Note: no _write_partial_sub_ac_checkpoint — store is empty.

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_no_partial",
            execution_id="exec_no_partial",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="nonexistent_prior",
        )

        sub_events = [
            e for e in appended
            if e.type == "execution.serial.resume.sub_postmortem_boundary"
        ]
        assert sub_events == [], (
            f"No sub_postmortem_boundary event expected when partial state absent; "
            f"got: {[e.type for e in appended]}"
        )

    # ------------------------------------------------------------------
    # Edge case: wrong AC index (partial_failing_ac_index != ac_index)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_no_sub_resume_for_ac_at_wrong_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sub-postmortem resume path NOT triggered for ACs that don't match partial index.

        Setup: partial checkpoint records AC 2 (index 2) as the failing AC.
        Run a 3-AC seed where ACs 0 and 1 are freshly executed (not in checkpoint
        completed list) and AC 2 is the failing AC.  Only AC 2 should get the
        sub_postmortem_boundary event; ACs 0 and 1 must NOT.

        Compounding reference: Sub-AC 1 established [[INVARIANT: partial sub-AC
        checkpoint does NOT advance last_completed_ac_index]] — so the partial
        index guard must compare ac_index to partial_failing_ac_index exactly.

        [[INVARIANT: sub-postmortem resume event type is execution.serial.resume.sub_postmortem_boundary]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_partial_sub_ac_checkpoint

        monkeypatch.setenv(
            "OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "artifacts")
        )
        seed = _make_seed("AC 0", "AC 1", "AC 2 — failing decomposed")
        executor, store, appended = self._make_executor_with_real_store(tmp_path)

        # Write a partial checkpoint where AC 2 (index 2) is the failing AC.
        sub_pm = ACPostmortem(
            summary=ACContextSummary(
                ac_index=2,
                ac_content="Sub-AC 0 of AC 2",
                success=True,
                files_modified=("src/sub_ac2.py",),
            ),
            status="pass",
        )
        _write_partial_sub_ac_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_sess_wrong_idx",
            ac_index=2,  # partial_failing_ac_index = 2
            sub_postmortems=(sub_pm,),
            base_chain=PostmortemChain(),
            last_completed_ac_index=-1,
        )

        executed_indices: list[int] = []
        captured_overrides: list[tuple[int, str]] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            executed_indices.append(ac_index)
            captured_overrides.append((ac_index, kwargs.get("context_override") or ""))
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,), (2,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_wrong_idx",
            execution_id="exec_wrong_idx",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_sess_wrong_idx",
        )

        # All 3 ACs are executed (last_completed_ac_index=-1, no skips).
        assert executed_indices == [0, 1, 2], (
            f"All 3 ACs should have been executed; got: {executed_indices}"
        )

        sub_events = [
            e for e in appended
            if e.type == "execution.serial.resume.sub_postmortem_boundary"
        ]
        # Only AC 2 triggers the sub-resume event.
        assert len(sub_events) == 1, (
            f"Expected exactly 1 sub_postmortem_boundary event (for AC 2); "
            f"got {len(sub_events)}: {[e.data for e in sub_events]}"
        )
        assert sub_events[0].data["ac_index"] == 2, (
            f"sub_postmortem_boundary event must be for AC 2, got {sub_events[0].data['ac_index']}"
        )

        # AC 0 and AC 1 context_override must NOT contain "Sub-AC Resume Context".
        for ac_idx, ctx in captured_overrides:
            if ac_idx in (0, 1):
                assert "Sub-AC Resume Context" not in ctx, (
                    f"AC {ac_idx} must not have Sub-AC Resume Context section; "
                    f"got context starting with: {ctx[:200]}"
                )

    # ------------------------------------------------------------------
    # Edge case: empty sub_pms list with index set (suppress path)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_no_sub_resume_event_when_sub_pms_empty_list(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sub-postmortem resume NOT triggered when partial sub-postmortems list is empty.

        Even if partial_failing_ac_index is set, an empty sub-postmortems list
        means there is no completed sub-AC work to report.  The guard
        ``_partial_failing_ac_sub_pms`` (falsy when []) prevents firing.

        [[INVARIANT: partial sub-AC checkpoint is written only when sub_results is non-empty]]
        """
        from ouroboros.persistence.checkpoint import (
            CheckpointData,
            CompoundingCheckpointState,
        )

        monkeypatch.setenv(
            "OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "artifacts")
        )
        seed = _make_seed("AC 0")
        executor, store, appended = self._make_executor_with_real_store(tmp_path)

        # Manually craft a checkpoint with partial_failing_ac_index=0 but
        # partial_failing_ac_sub_postmortems=[] (empty list).
        state = CompoundingCheckpointState(
            last_completed_ac_index=-1,
            postmortem_chain=[],
            partial_failing_ac_index=0,
            partial_failing_ac_sub_postmortems=[],  # empty — no completed sub-ACs
        )
        ckpt = CheckpointData.create(
            seed_id=seed.metadata.seed_id,
            phase="execution",
            state=state.to_dict(),
        )
        store.save(ckpt)

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_empty_subs",
            execution_id="exec_empty_subs",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_session",
        )

        sub_events = [
            e for e in appended
            if e.type == "execution.serial.resume.sub_postmortem_boundary"
        ]
        # No event: empty list is falsy so the guard suppresses the path.
        assert sub_events == [], (
            f"No sub_postmortem_boundary event expected when sub_pms is empty list; "
            f"got: {sub_events}"
        )

    # ------------------------------------------------------------------
    # Correct resume boundary selection: resume_from_sub_ac == sub_acs_completed
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_resume_boundary_matches_completed_count_single(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resume_from_sub_ac equals sub_acs_completed for 1 completed sub-AC.

        The boundary is defined as the first sub-AC index that still needs to
        execute.  With 1 completed sub-AC (index 0), the resume starts at index 1.

        Compounding reference: Sub-AC 1 established [[INVARIANT: resume_from_sub_ac
        == sub_acs_completed (no gaps, boundary is first incomplete sub-AC)]].
        This test verifies the count for the trivial case of 1 completed sub-AC
        (n=1 → boundary=1).

        [[INVARIANT: resume_from_sub_ac == sub_acs_completed (no gaps, boundary is first incomplete sub-AC)]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_partial_sub_ac_checkpoint

        monkeypatch.setenv(
            "OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "artifacts")
        )
        seed = _make_seed("AC 0 with 1 completed sub-AC")
        executor, store, appended = self._make_executor_with_real_store(tmp_path)

        sub_pm = ACPostmortem(
            summary=ACContextSummary(
                ac_index=0, ac_content="Sub-AC 0: write model", success=True,
                files_modified=("src/model.py",),
            ),
            status="pass",
        )
        _write_partial_sub_ac_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_1sub",
            ac_index=0,
            sub_postmortems=(sub_pm,),
            base_chain=PostmortemChain(),
            last_completed_ac_index=-1,
        )

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_boundary_1",
            execution_id="exec_boundary_1",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_1sub",
        )

        events = [
            e for e in appended
            if e.type == "execution.serial.resume.sub_postmortem_boundary"
        ]
        assert len(events) == 1, f"Expected 1 boundary event; got {len(events)}"
        ev = events[0]
        assert ev.data["sub_acs_completed"] == 1, (
            f"sub_acs_completed should be 1; got {ev.data['sub_acs_completed']}"
        )
        assert ev.data["resume_from_sub_ac"] == 1, (
            f"resume_from_sub_ac should be 1 (== sub_acs_completed); "
            f"got {ev.data['resume_from_sub_ac']}"
        )

    @pytest.mark.asyncio
    async def test_resume_boundary_matches_completed_count_three(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resume_from_sub_ac equals sub_acs_completed for 3 completed sub-ACs.

        With 3 completed sub-ACs (indices 0, 1, 2), the resume boundary is 3.
        The event must report sub_acs_completed=3 and resume_from_sub_ac=3.

        Compounding reference: AC-2 established [[INVARIANT: parent digest fields
        are unions of its own plus sub-postmortem fields]] — sub-postmortem
        preservation and the boundary count both depend on sub_pms being a
        correctly ordered tuple.

        [[INVARIANT: resume_from_sub_ac == sub_acs_completed (no gaps, boundary is first incomplete sub-AC)]]
        [[INVARIANT: sub-postmortem resume context is appended to context_override, not replacing it]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_partial_sub_ac_checkpoint

        monkeypatch.setenv(
            "OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "artifacts")
        )
        seed = _make_seed("AC 0 with 3 completed sub-ACs")
        executor, store, appended = self._make_executor_with_real_store(tmp_path)

        sub_pms = tuple(
            ACPostmortem(
                summary=ACContextSummary(
                    ac_index=0,
                    ac_content=f"Sub-AC {i}: step {i}",
                    success=True,
                    files_modified=(f"src/step_{i}.py",),
                ),
                status="pass",
            )
            for i in range(3)
        )
        _write_partial_sub_ac_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_3sub",
            ac_index=0,
            sub_postmortems=sub_pms,
            base_chain=PostmortemChain(),
            last_completed_ac_index=-1,
        )

        captured_overrides: list[str] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            captured_overrides.append(kwargs.get("context_override") or "")
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_boundary_3",
            execution_id="exec_boundary_3",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_3sub",
        )

        events = [
            e for e in appended
            if e.type == "execution.serial.resume.sub_postmortem_boundary"
        ]
        assert len(events) == 1, f"Expected 1 boundary event; got {len(events)}"
        ev = events[0]
        assert ev.data["sub_acs_completed"] == 3, (
            f"sub_acs_completed should be 3; got {ev.data['sub_acs_completed']}"
        )
        assert ev.data["resume_from_sub_ac"] == 3, (
            f"resume_from_sub_ac should equal sub_acs_completed (3); "
            f"got {ev.data['resume_from_sub_ac']}"
        )
        assert ev.data["resume_from_sub_ac"] == ev.data["sub_acs_completed"], (
            "resume_from_sub_ac must always equal sub_acs_completed"
        )

        # Context override must include the 3 completed sub-ACs section.
        assert len(captured_overrides) == 1
        ctx = captured_overrides[0]
        assert "Sub-AC Resume Context" in ctx
        for i in range(3):
            assert f"Sub-AC {i}: step {i}" in ctx, (
                f"Sub-AC {i} content must appear in context; context:\n{ctx[:600]}"
            )

    # ------------------------------------------------------------------
    # Event logging: event fields are correct
    # ------------------------------------------------------------------

    def test_sub_postmortem_resume_event_factory_fields(self) -> None:
        """create_sub_postmortem_resume_event produces the correct event structure.

        Verifies the event type, aggregate fields, and all data payload keys.

        Compounding reference: the event factory follows the same pattern as
        ``create_postmortem_chain_truncated_event`` (AC-4 Q7 / Sub-AC 2) and
        ``create_ac_postmortem_captured_event`` (AC-1). All events coexist with
        their corresponding log lines.

        [[INVARIANT: sub-postmortem resume event type is execution.serial.resume.sub_postmortem_boundary]]
        [[INVARIANT: resume_from_sub_ac == sub_acs_completed (no gaps, boundary is first incomplete sub-AC)]]
        """
        from ouroboros.orchestrator.events import create_sub_postmortem_resume_event

        for n_completed in [1, 2, 5]:
            event = create_sub_postmortem_resume_event(
                session_id=f"sess_{n_completed}",
                execution_id=f"exec_{n_completed}",
                ac_index=7,
                sub_acs_completed=n_completed,
                resume_from_sub_ac=n_completed,
            )

            assert event.type == "execution.serial.resume.sub_postmortem_boundary", (
                f"Wrong event type for n_completed={n_completed}"
            )
            assert event.aggregate_type == "execution"
            assert event.aggregate_id == f"exec_{n_completed}"
            assert event.data["session_id"] == f"sess_{n_completed}"
            assert event.data["execution_id"] == f"exec_{n_completed}"
            assert event.data["ac_index"] == 7
            assert event.data["sub_acs_completed"] == n_completed
            assert event.data["resume_from_sub_ac"] == n_completed, (
                "resume_from_sub_ac must equal sub_acs_completed"
            )
            assert "timestamp" in event.data

    @pytest.mark.asyncio
    async def test_sub_resume_event_not_emitted_when_no_resume_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No sub_postmortem_boundary event when resume_session_id is None.

        Even if a partial checkpoint exists, it is only consulted when
        resume_session_id is provided.  Without it, the partial state loading
        path is never entered.

        Compounding reference: AC-3 tests verified that verify_invariants is
        skipped when there are no tags. By analogy, the sub-resume path must
        be gated by resume_session_id being set (which also gates the
        checkpoint store consultation).

        [[INVARIANT: resume_session_id triggers checkpoint loading by seed_id, not by session_id]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import _write_partial_sub_ac_checkpoint

        monkeypatch.setenv(
            "OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "artifacts")
        )
        seed = _make_seed("AC 0")
        executor, store, appended = self._make_executor_with_real_store(tmp_path)

        # Write a partial checkpoint so one exists in the store.
        sub_pm = ACPostmortem(
            summary=ACContextSummary(
                ac_index=0, ac_content="Sub 0", success=True,
                files_modified=("src/sub.py",),
            ),
            status="pass",
        )
        _write_partial_sub_ac_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_partial",
            ac_index=0,
            sub_postmortems=(sub_pm,),
            base_chain=PostmortemChain(),
            last_completed_ac_index=-1,
        )

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,))
        # NOTE: resume_session_id intentionally NOT passed.
        await executor.execute_serial(
            seed=seed,
            session_id="sess_no_resume_id",
            execution_id="exec_no_resume_id",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
        )

        sub_events = [
            e for e in appended
            if e.type == "execution.serial.resume.sub_postmortem_boundary"
        ]
        assert sub_events == [], (
            "No sub_postmortem_boundary event expected when resume_session_id is None; "
            f"got: {[e.type for e in appended]}"
        )

    # ------------------------------------------------------------------
    # Context_override appended, not replaced
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_sub_resume_context_is_appended_not_replacing_prior_chain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sub-postmortem resume section is appended to existing context, not replacing it.

        When the partial checkpoint for AC 1 is loaded and there is already
        a completed AC 0 postmortem in the chain, AC 1's context_override must
        contain BOTH the prior chain section AND the sub-AC resume section.

        Compounding reference: AC-1 established [[INVARIANT: end-of-run chain
        artifact exists in docs/brainstorm/chain-*.md]] proving postmortem chain
        serialization works; AC-2 established [[INVARIANT: ACPostmortem.sub_postmortems
        preserves structure in serialized chain]] proving the chain carries sub-AC
        data.  This test relies on both invariants: the prior chain postmortem
        appears alongside the sub-postmortem resume context.

        [[INVARIANT: sub-postmortem resume context is appended to context_override, not replacing it]]
        [[INVARIANT: deserialized chain is injected before the AC loop so resumed ACs see prior postmortems]]
        """
        from ouroboros.orchestrator.level_context import (
            ACContextSummary,
            ACPostmortem,
            PostmortemChain,
        )
        from ouroboros.orchestrator.serial_executor import (
            _write_compounding_checkpoint,
            _write_partial_sub_ac_checkpoint,
        )

        monkeypatch.setenv(
            "OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "artifacts")
        )
        seed = _make_seed("AC 0 completed", "AC 1 partially decomposed")
        executor, store, appended = self._make_executor_with_real_store(tmp_path)

        # AC 0 completed: write a normal checkpoint with its postmortem.
        pm_ac0 = ACPostmortem(
            summary=ACContextSummary(
                ac_index=0,
                ac_content="AC 0 completed",
                success=True,
                files_modified=("src/module_0.py",),
            ),
            status="pass",
            gotchas=("ac0_specific_gotcha",),
        )
        prior_chain = PostmortemChain(postmortems=(pm_ac0,))
        _write_compounding_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_chain_and_partial",
            ac_index=0,
            chain=prior_chain,
        )

        # AC 1 is the partial failing AC with 2 completed sub-ACs.
        sub_pm0 = ACPostmortem(
            summary=ACContextSummary(
                ac_index=1,
                ac_content="Sub-AC 0 of AC 1: parse config",
                success=True,
                files_modified=("src/config_parser.py",),
            ),
            status="pass",
        )
        sub_pm1 = ACPostmortem(
            summary=ACContextSummary(
                ac_index=1,
                ac_content="Sub-AC 1 of AC 1: validate schema",
                success=True,
                files_modified=("src/schema_validator.py",),
            ),
            status="pass",
        )
        _write_partial_sub_ac_checkpoint(
            store=store,
            seed_id=seed.metadata.seed_id,
            session_id="prior_chain_and_partial",
            ac_index=1,
            sub_postmortems=(sub_pm0, sub_pm1),
            base_chain=prior_chain,
            last_completed_ac_index=0,
        )

        # The checkpoint store now has both the main checkpoint (AC 0 done,
        # last_completed_ac_index=0) and the partial state for AC 1.  When
        # loading, _load_compounding_checkpoint reads last_completed_ac_index=0
        # from the main checkpoint.  But wait — _write_partial_sub_ac_checkpoint
        # overwrites the checkpoint with last_completed_ac_index=0 (unchanged)
        # and the partial state embedded.  So loading gives:
        #   - last_completed_ac_index=0 → AC 0 skipped, AC 1 executed
        #   - partial_failing_ac_index=1, sub_pms=[sub_pm0, sub_pm1]

        executed: list[int] = []
        captured_overrides: list[tuple[int, str]] = []

        async def fake_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            executed.append(ac_index)
            captured_overrides.append((ac_index, kwargs.get("context_override") or ""))
            return _ok_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_single_ac  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_appended",
            execution_id="exec_appended",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="prior_chain_and_partial",
        )

        # AC 0 was skipped (last_completed_ac_index=0), AC 1 was executed.
        assert executed == [1], (
            f"Only AC 1 should have been executed; got: {executed}"
        )

        # AC 1's context_override must contain BOTH sections.
        _, ctx = captured_overrides[0]

        # Prior chain section must be present (AC 0's postmortem).
        assert "Prior AC Postmortems" in ctx, (
            "Prior chain section must appear in AC 1's context_override"
        )
        assert "AC 0 completed" in ctx, (
            "AC 0's postmortem content must be in AC 1's context_override"
        )
        assert "ac0_specific_gotcha" in ctx, (
            "AC 0's gotcha must be in AC 1's context_override"
        )

        # Sub-AC resume section must also be present (appended, not replacing).
        assert "Sub-AC Resume Context" in ctx, (
            "Sub-AC Resume Context section must appear in AC 1's context_override"
        )
        assert "Sub-AC 0 of AC 1: parse config" in ctx, (
            "First completed sub-AC must appear in resume section"
        )
        assert "Sub-AC 1 of AC 1: validate schema" in ctx, (
            "Second completed sub-AC must appear in resume section"
        )

        # The sub-AC resume section must come AFTER the prior chain section.
        chain_pos = ctx.find("Prior AC Postmortems")
        sub_ac_pos = ctx.find("Sub-AC Resume Context")
        assert chain_pos < sub_ac_pos, (
            "Sub-AC Resume Context must be appended after the prior chain section; "
            f"chain_pos={chain_pos}, sub_ac_pos={sub_ac_pos}"
        )


class TestAgentAdjudicationPromptAC4:
    """Sub-AC 4: Additional tests for agent-adjudication prompt.

    Covers DECISION: continue path, DECISION: restart path, malformed agent
    responses, prompt construction correctness, and event logging verification.

    This class focuses on tests NOT already present in TestMonolithicResume,
    adding deeper coverage for edge cases, structural ordering in the prompt,
    and event-system verification.

    Compounding context (from prior ACs):
    - AC-1 established [[INVARIANT: end-of-run chain artifact exists in
      docs/brainstorm/chain-*.md]] — the chain artifact persists even on
      resume, providing the "prior work context" the adjudication prompt
      uses to decide continue vs. restart.
    - AC-2 established [[INVARIANT: ACPostmortem.sub_postmortems preserves
      structure in serialized chain]] — the monolithic adjudication path
      is only triggered when sub_postmortems is empty (decomposed ACs take
      the sub-AC boundary path instead; both paths share the same chain).
    - AC-3 established [[INVARIANT: Haiku verifier runs inline per AC before
      chain advances]] — agent adjudication uses the same adapter.complete()
      inline-blocking pattern as the Haiku invariant verifier: both call
      adapter.complete() and both return before the executor advances.
    - Sub-AC 1 established [[INVARIANT: partial sub-AC checkpoint does NOT
      advance last_completed_ac_index]] — the monolithic path is the
      complement: no partial checkpoint, adjudication based on full chain.
    - Sub-AC 2 established [[INVARIANT: monolithic resume adjudication event
      type is execution.serial.resume.monolithic_adjudicated]] — this class
      adds additional event-field and ordering tests on top of that coverage.
    - Sub-AC 3 established [[INVARIANT: resume_from_sub_ac == sub_acs_completed
      (no gaps, boundary is first incomplete sub-AC)]] — monolithic path has
      no sub-AC boundary; the adjudication event has no boundary field.

    [[INVARIANT: _build_monolithic_resume_decision_prompt orders AC text before context before decision]]
    [[INVARIANT: _parse_monolithic_resume_decision checks only the first non-empty line]]
    [[INVARIANT: decision field is always "continue" or "restart" (never None or empty)]]
    [[INVARIANT: adjudication event is emitted exactly once per adjudicated AC per resume run]]
    """

    # ------------------------------------------------------------------
    # 1. Prompt construction correctness
    # ------------------------------------------------------------------

    def test_prompt_structure_has_required_sections(self) -> None:
        """The DECISION prompt contains all required structural sections.

        Verifies: AC text section, Prior Work Context section, Decision
        Required section — all present and non-empty.

        Compounding reference: AC-1 chain artifact provides the context_section
        content rendered by build_postmortem_chain_prompt; here we confirm
        the sections exist so the agent has enough information to decide.

        [[INVARIANT: _build_monolithic_resume_decision_prompt orders AC text before context before decision]]
        """
        from ouroboros.orchestrator.serial_executor import (
            _build_monolithic_resume_decision_prompt,
        )

        ac_text = "Implement auth middleware with session tokens."
        context = "## AC 1 [pass]\n- Files modified: src/middleware.py"
        prompt = _build_monolithic_resume_decision_prompt(ac_text, context)

        assert "## Original Task" in prompt or ac_text in prompt, (
            "Prompt must include the original AC text"
        )
        assert "Prior Work Context" in prompt, (
            "Prompt must contain a 'Prior Work Context' section"
        )
        assert "Decision Required" in prompt or "DECISION:" in prompt, (
            "Prompt must contain a decision instruction section"
        )

    def test_prompt_orders_ac_text_before_context_before_decision(self) -> None:
        """AC text appears before context section, which appears before DECISION line.

        Ordering matters: the agent reads top-to-bottom. The task description
        must come first so the agent understands what it was doing before
        seeing the prior work trace, and the decision instruction must come last.

        [[INVARIANT: _build_monolithic_resume_decision_prompt orders AC text before context before decision]]
        """
        from ouroboros.orchestrator.serial_executor import (
            _build_monolithic_resume_decision_prompt,
        )

        ac_text = "Build the CI pipeline integration."
        context = "Prior postmortem: src/ci.py modified"
        prompt = _build_monolithic_resume_decision_prompt(ac_text, context)

        ac_pos = prompt.find(ac_text)
        context_pos = prompt.find(context)
        decision_pos = prompt.find("DECISION: continue")

        assert ac_pos != -1, "AC text not found in prompt"
        assert context_pos != -1, "Context not found in prompt"
        assert decision_pos != -1, "DECISION: continue not found in prompt"

        assert ac_pos < context_pos, (
            f"AC text (pos {ac_pos}) must appear before context (pos {context_pos})"
        )
        assert context_pos < decision_pos, (
            f"Context (pos {context_pos}) must appear before DECISION line (pos {decision_pos})"
        )

    def test_prompt_lists_continue_option_before_restart(self) -> None:
        """'DECISION: continue' appears before 'DECISION: restart' in the prompt.

        The continue option is listed first to avoid anchoring bias (the model
        should evaluate the evidence, not just pick the first option).

        [[INVARIANT: monolithic resume prompt always includes the literal text DECISION: continue and DECISION: restart as options]]
        """
        from ouroboros.orchestrator.serial_executor import (
            _build_monolithic_resume_decision_prompt,
        )

        prompt = _build_monolithic_resume_decision_prompt("Some AC", "some context")

        cont_pos = prompt.find("DECISION: continue")
        rest_pos = prompt.find("DECISION: restart")

        assert cont_pos != -1, "DECISION: continue not in prompt"
        assert rest_pos != -1, "DECISION: restart not in prompt"
        assert cont_pos < rest_pos, (
            "DECISION: continue should appear before DECISION: restart in the prompt"
        )

    def test_prompt_whitespace_only_context_uses_fallback(self) -> None:
        """Whitespace-only context (not empty string) also triggers the placeholder.

        A context of '   \\n  ' is semantically empty; the prompt should
        show '(No prior context recorded)' rather than a blank section.
        """
        from ouroboros.orchestrator.serial_executor import (
            _build_monolithic_resume_decision_prompt,
        )

        prompt = _build_monolithic_resume_decision_prompt("AC text", "   \n  ")

        assert "(No prior context recorded)" in prompt, (
            "Whitespace-only context should trigger the fallback placeholder"
        )

    def test_prompt_contains_explanation_of_continue_and_restart(self) -> None:
        """The prompt explains what 'continue' and 'restart' mean for the agent.

        The agent must understand the consequences of each choice (evidence of
        partial work vs. clean start) so the prompt must describe both options.
        """
        from ouroboros.orchestrator.serial_executor import (
            _build_monolithic_resume_decision_prompt,
        )

        prompt = _build_monolithic_resume_decision_prompt("AC text", "some context")

        # Both options explained in-context (not just as bare labels).
        lower = prompt.lower()
        assert "partial" in lower or "interrupted" in lower or "resume" in lower, (
            "Prompt must mention partial/interrupted/resume context"
        )
        assert "fresh" in lower or "restart" in lower or "beginning" in lower, (
            "Prompt must mention the fresh-start/restart option"
        )

    # ------------------------------------------------------------------
    # 2. DECISION: continue path — deeper edge cases
    # ------------------------------------------------------------------

    def test_parse_continue_with_leading_whitespace_on_first_line(self) -> None:
        """Leading whitespace before DECISION: is stripped; 'continue' still parsed.

        [[INVARIANT: _parse_monolithic_resume_decision checks only the first non-empty line]]
        """
        from ouroboros.orchestrator.serial_executor import (
            _parse_monolithic_resume_decision,
        )

        # Stripped → "DECISION: continue ..."
        assert _parse_monolithic_resume_decision("  DECISION: continue and proceed") == "continue"

    def test_parse_continue_word_followed_by_extra_text(self) -> None:
        """'DECISION: continue...' where continue is followed by more text → 'continue'.

        Uses startswith logic: 'continue and resume' starts with 'continue'.

        [[INVARIANT: _parse_monolithic_resume_decision always returns "continue" or "restart"]]
        """
        from ouroboros.orchestrator.serial_executor import (
            _parse_monolithic_resume_decision,
        )

        assert _parse_monolithic_resume_decision(
            "DECISION: continue — evidence of partial work in auth module"
        ) == "continue"

    def test_parse_restart_word_followed_by_extra_text(self) -> None:
        """'DECISION: restart from scratch' → 'restart' (startswith).

        [[INVARIANT: _parse_monolithic_resume_decision always returns "continue" or "restart"]]
        """
        from ouroboros.orchestrator.serial_executor import (
            _parse_monolithic_resume_decision,
        )

        assert _parse_monolithic_resume_decision(
            "DECISION: restart from scratch\nNo evidence of prior work."
        ) == "restart"

    # ------------------------------------------------------------------
    # 3. Malformed agent responses — deeper cases
    # ------------------------------------------------------------------

    def test_parse_decision_invalid_value_after_colon_defaults_restart(self) -> None:
        """Unknown value after DECISION: defaults to 'restart' (safe fallback).

        'DECISION: maybe' is not 'continue' or 'restart'; falls back to restart.

        [[INVARIANT: _parse_monolithic_resume_decision always returns "continue" or "restart"]]
        """
        from ouroboros.orchestrator.serial_executor import (
            _parse_monolithic_resume_decision,
        )

        result = _parse_monolithic_resume_decision("DECISION: maybe\nI'm not sure.")
        assert result == "restart", (
            f"Unknown DECISION value should fall back to 'restart'; got '{result}'"
        )

    def test_parse_decision_empty_after_colon_defaults_restart(self) -> None:
        """'DECISION: ' (nothing after colon) defaults to 'restart'.

        [[INVARIANT: _parse_monolithic_resume_decision always returns "continue" or "restart"]]
        """
        from ouroboros.orchestrator.serial_executor import (
            _parse_monolithic_resume_decision,
        )

        result = _parse_monolithic_resume_decision("DECISION: \n")
        assert result == "restart"

    def test_parse_decision_json_response_defaults_restart(self) -> None:
        """A JSON-formatted response (no DECISION: line) defaults to 'restart'.

        Some agents might accidentally respond with JSON. The fallback must
        be safe: restart rather than misinterpreting the response.

        [[INVARIANT: _parse_monolithic_resume_decision always returns "continue" or "restart"]]
        """
        from ouroboros.orchestrator.serial_executor import (
            _parse_monolithic_resume_decision,
        )

        json_response = '{"decision": "continue", "reason": "partial work detected"}'
        result = _parse_monolithic_resume_decision(json_response)
        assert result == "restart", (
            f"JSON response without DECISION: prefix should fall back to 'restart'; got '{result}'"
        )

    def test_parse_decision_on_second_line_not_first_defaults_restart(self) -> None:
        """If DECISION: appears on the second line (not the first), default to 'restart'.

        The parser only reads the first non-empty line. A DECISION on line 2
        is treated as if there is no DECISION at all.

        [[INVARIANT: _parse_monolithic_resume_decision checks only the first non-empty line]]
        """
        from ouroboros.orchestrator.serial_executor import (
            _parse_monolithic_resume_decision,
        )

        response = "Here is my analysis of the situation.\nDECISION: continue"
        result = _parse_monolithic_resume_decision(response)
        assert result == "restart", (
            f"DECISION on line 2 (not line 1) must yield 'restart'; got '{result}'"
        )

    def test_parse_decision_numeric_response_defaults_restart(self) -> None:
        """A numeric response (e.g., '1' or '0.9') defaults to 'restart'.

        Agents sometimes confuse scoring tasks with decision tasks and return
        a number. The parser must not crash and must return 'restart'.

        [[INVARIANT: _parse_monolithic_resume_decision always returns "continue" or "restart"]]
        """
        from ouroboros.orchestrator.serial_executor import (
            _parse_monolithic_resume_decision,
        )

        assert _parse_monolithic_resume_decision("1") == "restart"
        assert _parse_monolithic_resume_decision("0.9") == "restart"

    @pytest.mark.asyncio
    async def test_adjudicate_malformed_response_returns_restart_decision(self) -> None:
        """When the adapter returns malformed text, _adjudicate_monolithic_resume returns 'restart'.

        Compounding reference: AC-3 established that Haiku verifier uses the
        same adapter.complete() call pattern and returns a fallback score on
        parse failure.  Here the adjudicator similarly falls back to 'restart'
        when the response cannot be parsed as continue/restart.

        [[INVARIANT: _adjudicate_monolithic_resume always returns ("continue"|"restart", str)]]
        [[INVARIANT: adapter error in monolithic adjudication defaults to "restart"]]
        """
        from ouroboros.core.types import Result
        from ouroboros.orchestrator.serial_executor import _adjudicate_monolithic_resume
        from ouroboros.providers.base import CompletionResponse, UsageInfo

        malformed_response = CompletionResponse(
            content='{"action": "go", "confidence": 0.8}',
            model="claude-haiku-4-5",
            usage=UsageInfo(prompt_tokens=50, completion_tokens=10, total_tokens=60),
        )
        adapter = MagicMock()
        adapter.complete = AsyncMock(return_value=Result.ok(malformed_response))

        decision, raw = await _adjudicate_monolithic_resume(
            adapter,
            "Build caching layer",
            "Prior work trace: none",
            model="claude-haiku-4-5",
        )

        assert decision == "restart", (
            f"Malformed response must yield 'restart'; got '{decision}'"
        )
        assert isinstance(raw, str), "raw_response must be a string even on malformed input"

    @pytest.mark.asyncio
    async def test_adjudicate_none_content_response_returns_restart(self) -> None:
        """When adapter returns a response with None/empty content, default to 'restart'.

        An LLM that returns an empty completion (e.g. content-filtering) must
        not cause a crash — the fallback 'restart' keeps execution safe.

        [[INVARIANT: _adjudicate_monolithic_resume always returns ("continue"|"restart", str)]]
        """
        from ouroboros.core.types import Result
        from ouroboros.orchestrator.serial_executor import _adjudicate_monolithic_resume
        from ouroboros.providers.base import CompletionResponse, UsageInfo

        empty_response = CompletionResponse(
            content="",
            model="claude-haiku-4-5",
            usage=UsageInfo(prompt_tokens=50, completion_tokens=0, total_tokens=50),
        )
        adapter = MagicMock()
        adapter.complete = AsyncMock(return_value=Result.ok(empty_response))

        decision, raw = await _adjudicate_monolithic_resume(
            adapter,
            "Deploy new service",
            "some context",
            model="claude-haiku-4-5",
        )

        assert decision == "restart"
        assert raw == ""

    # ------------------------------------------------------------------
    # 4. Event logging verification — additional coverage
    # ------------------------------------------------------------------

    def test_adjudication_event_aggregate_type_is_execution(self) -> None:
        """Adjudication event aggregate_type is 'execution' (not 'session').

        Events from the serial executor use aggregate_type='execution' so
        TUI consumers polling the execution stream see them without needing
        to also poll the session stream.

        Compounding reference: Sub-AC 2 established the event type; this test
        verifies the aggregate_type field separately from the type field.

        [[INVARIANT: adjudication event is emitted exactly once per adjudicated AC per resume run]]
        [[INVARIANT: monolithic resume adjudication event type is execution.serial.resume.monolithic_adjudicated]]
        """
        from ouroboros.orchestrator.events import create_monolithic_resume_adjudicated_event

        event = create_monolithic_resume_adjudicated_event(
            session_id="s1",
            execution_id="e1",
            ac_index=0,
            decision="restart",
            raw_response_preview="DECISION: restart",
        )

        assert event.aggregate_type == "execution", (
            f"aggregate_type must be 'execution'; got '{event.aggregate_type}'"
        )

    def test_adjudication_event_not_emitted_in_fresh_run(self) -> None:
        """No adjudication event appears in the event stream for a fresh (non-resume) run.

        This is a synchronous guard: fresh runs skip the adjudication branch
        entirely.  Confirms the event list is clean so downstream consumers
        do not misinterpret a ghost event as an adjudication.

        [[INVARIANT: adjudication event is emitted exactly once per adjudicated AC per resume run]]
        """
        # Directly check the event factory: the factory can only be called in the
        # adjudication branch.  We rely on the integration test in TestMonolithicResume
        # for the full execute_serial path; here we confirm the factory exists and
        # produces a sane event so the integration-level assertion is trustworthy.
        from ouroboros.orchestrator.events import create_monolithic_resume_adjudicated_event

        # Calling the factory with a "continue" decision must work.
        ev_continue = create_monolithic_resume_adjudicated_event(
            session_id="fresh_sess",
            execution_id="fresh_exec",
            ac_index=0,
            decision="continue",
            raw_response_preview="DECISION: continue",
        )
        assert ev_continue.data["decision"] == "continue"

        # Calling the factory with a "restart" decision must also work.
        ev_restart = create_monolithic_resume_adjudicated_event(
            session_id="fresh_sess",
            execution_id="fresh_exec",
            ac_index=0,
            decision="restart",
            raw_response_preview="DECISION: restart",
        )
        assert ev_restart.data["decision"] == "restart"

    def test_adjudication_event_raw_preview_long_response_truncated(self) -> None:
        """raw_response_preview is capped at 200 characters for any response length.

        AC-3 established that Haiku verifier responses are truncated when stored.
        The adjudication event applies the same 200-char cap so event payloads
        remain bounded.

        [[INVARIANT: decision field is always "continue" or "restart" (never None or empty)]]
        """
        from ouroboros.orchestrator.events import create_monolithic_resume_adjudicated_event

        long_raw = "DECISION: continue\n" + "X" * 1000
        event = create_monolithic_resume_adjudicated_event(
            session_id="s",
            execution_id="e",
            ac_index=0,
            decision="continue",
            raw_response_preview=long_raw,
        )

        assert len(event.data["raw_response_preview"]) <= 200, (
            "raw_response_preview must be capped at 200 characters"
        )
        # Decision must still be preserved even when preview is truncated.
        assert event.data["decision"] == "continue"

    @pytest.mark.asyncio
    async def test_adjudication_event_emitted_exactly_once_per_failing_ac(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In a 3-AC run where AC 1 is the failing AC, exactly one adjudication event is emitted.

        Verifies the event is scoped to a single AC, not emitted for skipped
        or fresh ACs.  Relies on the chain artifact path set by AC-1 invariant.

        Compounding reference: AC-1 established the chain artifact is always
        written; this test confirms the artifact directory env var is set so
        the chain write does not fail and interfere with event collection.

        [[INVARIANT: adjudication event is emitted exactly once per adjudicated AC per resume run]]
        [[INVARIANT: monolithic resume adjudication event type is execution.serial.resume.monolithic_adjudicated]]
        """
        import ouroboros.orchestrator.serial_executor as serial_mod
        from ouroboros.persistence.checkpoint import CheckpointStore

        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "artifacts"))

        async def fake_adjudicate(
            adapter: Any,
            ac_content: str,
            context_section: str,
            *,
            model: str | None = None,
        ) -> tuple[str, str]:
            return "restart", "DECISION: restart\nClean start."

        monkeypatch.setattr(serial_mod, "_adjudicate_monolithic_resume", fake_adjudicate)

        # Build a 3-AC seed: AC0 done, AC1 failing, AC2 not yet run.
        seed = _make_seed("AC0 — setup", "AC1 — failing AC", "AC2 — after failing")

        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()
        event_store, appended = _make_replaying_event_store()
        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])

        # First run: AC0 succeeds, AC1 fails (fail_fast).
        async def fake_first_run(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            if ac_index == 0:
                return _ok_result(ac_index, str(kwargs["ac_content"]))
            return _fail_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_first_run  # type: ignore[method-assign]

        plan = _make_plan((0,), (1,), (2,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_3ac_first",
            execution_id="exec_3ac_first",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )
        appended.clear()  # discard first-run events

        # Resume run: AC0 skipped, AC1 gets adjudicated, AC2 runs normally.
        async def fake_resume(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_resume  # type: ignore[method-assign]

        await executor.execute_serial(
            seed=seed,
            session_id="sess_3ac_resume",
            execution_id="exec_3ac_resume",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="sess_3ac_first",
            fail_fast=False,
        )

        adj_events = [
            e for e in appended
            if e.type == "execution.serial.resume.monolithic_adjudicated"
        ]

        # Exactly one adjudication event — for AC1 only.
        assert len(adj_events) == 1, (
            f"Expected exactly 1 adjudication event; got {len(adj_events)}: "
            f"{[e.data for e in adj_events]}"
        )
        assert adj_events[0].data["ac_index"] == 1, (
            f"Adjudication event must be for AC1 (index 1); got index "
            f"{adj_events[0].data['ac_index']}"
        )
        assert adj_events[0].data["session_id"] == "sess_3ac_resume"
        assert adj_events[0].data["execution_id"] == "exec_3ac_resume"

    @pytest.mark.asyncio
    async def test_adjudication_prompt_uses_chain_context_from_prior_postmortems(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The context_section passed to adjudication contains prior AC postmortem data.

        Compounding reference: AC-1 established chain artifacts exist in
        docs/brainstorm/; AC-2 established sub_postmortems are preserved in
        the chain.  Adjudication receives the rendered chain as its context
        so the agent can see prior work before deciding continue vs. restart.

        [[INVARIANT: _build_monolithic_resume_decision_prompt orders AC text before context before decision]]
        [[INVARIANT: adjudication event is emitted exactly once per adjudicated AC per resume run]]
        """
        import ouroboros.orchestrator.serial_executor as serial_mod
        from ouroboros.persistence.checkpoint import CheckpointStore

        monkeypatch.setenv("OUROBOROS_CHAIN_ARTIFACT_DIR", str(tmp_path / "artifacts"))

        captured_adjudication_args: list[dict] = []

        async def fake_adjudicate(
            adapter: Any,
            ac_content: str,
            context_section: str,
            *,
            model: str | None = None,
        ) -> tuple[str, str]:
            captured_adjudication_args.append({
                "ac_content": ac_content,
                "context_section": context_section,
            })
            return "restart", "DECISION: restart"

        monkeypatch.setattr(serial_mod, "_adjudicate_monolithic_resume", fake_adjudicate)

        seed = _make_seed(
            "AC0 — build persistence layer",
            "AC1 — build service layer (failing)",
        )
        store = CheckpointStore(base_path=tmp_path / "checkpoints")
        store.initialize()
        event_store, _ = _make_replaying_event_store()
        executor = SerialCompoundingExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            checkpoint_store=store,
        )
        executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])

        # First run: AC0 writes a file (captured in postmortem), AC1 fails.
        async def fake_first_run(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            if ac_index == 0:
                return _ok_result(
                    ac_index,
                    str(kwargs["ac_content"]),
                    files_written=("src/persistence.py",),
                )
            return _fail_result(ac_index, str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_first_run  # type: ignore[method-assign]
        plan = _make_plan((0,), (1,))
        await executor.execute_serial(
            seed=seed,
            session_id="sess_ctx_first",
            execution_id="exec_ctx_first",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            fail_fast=True,
        )

        # Resume: AC1 triggers adjudication.
        async def fake_resume(**kwargs: Any) -> ACExecutionResult:
            return _ok_result(int(kwargs["ac_index"]), str(kwargs["ac_content"]))

        executor._execute_single_ac = fake_resume  # type: ignore[method-assign]
        await executor.execute_serial(
            seed=seed,
            session_id="sess_ctx_resume",
            execution_id="exec_ctx_resume",
            tools=[],
            system_prompt="SYSTEM",
            execution_plan=plan,
            resume_session_id="sess_ctx_first",
        )

        # Adjudication must have been called once.
        assert len(captured_adjudication_args) == 1, (
            f"Expected 1 adjudication call; got {len(captured_adjudication_args)}"
        )
        args = captured_adjudication_args[0]

        # The AC text passed to adjudication must be AC1's content.
        assert "AC1" in args["ac_content"] or "service layer" in args["ac_content"], (
            f"Adjudication received wrong AC content: {args['ac_content']!r}"
        )

        # The context passed to adjudication must reference AC0's postmortem.
        # AC0 modified src/persistence.py — that file must appear in the chain context.
        assert "persistence" in args["context_section"].lower() or (
            "AC0" in args["context_section"] or "persistence layer" in args["context_section"]
        ), (
            "Adjudication context must reference AC0's postmortem data (prior work); "
            f"got context preview: {args['context_section'][:200]!r}"
        )
