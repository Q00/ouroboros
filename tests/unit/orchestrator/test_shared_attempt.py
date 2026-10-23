"""A Seed whose criteria have no known source runs one worker attempt.

Every ``ooo auto`` Seed carries ``sourced_scope``, and its worker sees only the
request and the criteria that bind by ledger provenance. When the Seed's own
criteria (written by the Seed generator) are not those binding criteria,
position for position, one leaf per criterion would hand every worker the same
request. Such a Seed runs root 0 once, and every other criterion is judged from
that attempt against its own text (``SETTLED_FROM_SHARED_ATTEMPT``). When the
binding criteria are the Seed's criteria, each leaf sees only its own.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata, SourcedScope
from ouroboros.core.seed_contract import GOAL_OBJECTIVE_CRITERION
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph
from ouroboros.orchestrator.evidence.ac_classification import _is_documentation_only_ac
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome, ACExecutionResult
from ouroboros.orchestrator.profile_loader import load_profile
from ouroboros.orchestrator.shared_attempt import (
    shared_attempt_graph,
    shares_one_attempt,
    sourced_criteria_aligned,
)

USER_REQUEST = "The export command needs a json flag."
DOC_CRITERION = "Document the --json flag in README.md"
CODE_CRITERION = "Implement the --json flag in src/app.py"
TEST_CRITERION = "Add a regression test for the --json flag"
MODEL_CRITERIA = (DOC_CRITERION, CODE_CRITERION, TEST_CRITERION)
EVIDENCE = (
    "```json\n"
    '{"files_touched":["src/app.py"],"commands_run":["pytest"],"tests_passed":["pytest"],'
    '"entry_points":[{"symbol":"app.run","call_kind":"function"}]}'
    "\n```"
)


class _CountingRuntime:
    """Replays an edit, a passing test run and the evidence; counts dispatches."""

    runtime_backend = "opencode"
    permission_mode = "acceptEdits"

    def __init__(self, working_directory: str, *, support: bool = True) -> None:
        self.working_directory = working_directory
        self._support = support
        self.prompts: list[str] = []

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> Any:
        del tools, system_prompt, resume_handle, resume_session_id
        self.prompts.append(prompt)
        yield AgentMessage(
            type="tool",
            content="Edit src/app.py",
            tool_name="Edit",
            data={
                "input": {"file_path": "src/app.py"},
                "subtype": "success",
                "runtime_event_type": "tool.completed",
            },
        )
        if self._support:
            yield AgentMessage(
                type="tool",
                content="pytest passed\n1 passed in 0.01s",
                tool_name="Bash",
                data={"input": {"command": "pytest"}, "output": "1 passed in 0.01s"},
            )
        yield AgentMessage(
            type="result",
            content=EVIDENCE,
            data={"subtype": "success"},
            resume_handle=RuntimeHandle(
                backend="opencode",
                kind="implementation_session",
                native_session_id=f"session-{len(self.prompts)}",
                cwd=self.working_directory,
                metadata={},
            ),
        )


_REQUEST_ONLY = SourcedScope(goal=USER_REQUEST)


def _seed(
    criteria: tuple[str, ...] = MODEL_CRITERIA,
    scope: SourcedScope | None = _REQUEST_ONLY,
) -> Seed:
    return Seed(
        goal="A paraphrase of the request the generator wrote.",
        acceptance_criteria=criteria,
        ontology_schema=OntologySchema(name="n", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.05),
        sourced_scope=scope,
    )


def _executor(runtime: _CountingRuntime) -> ParallelACExecutor:
    return ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=_event_store(),
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
        transcript_verifier="decide",
        run_verify_commands=False,
        ac_retry_attempts=0,
        task_cwd=runtime.working_directory,
    )


def _event_store() -> AsyncMock:
    """An async event store that replays what was appended."""
    store = AsyncMock()
    appended: list[Any] = []

    async def _append(event: Any) -> None:
        appended.append(event)

    async def _replay(aggregate_type: str, aggregate_id: str) -> list[Any]:
        return [
            e
            for e in appended
            if e.aggregate_type == aggregate_type and e.aggregate_id == aggregate_id
        ]

    store.append.side_effect = _append
    store.replay.side_effect = _replay
    return store


def _plan(seed: Seed) -> Any:
    graph = shared_attempt_graph(seed)
    if graph is None:
        graph = DependencyGraph(
            nodes=tuple(
                ACNode(index=i, content=str(c)) for i, c in enumerate(seed.acceptance_criteria)
            ),
            execution_levels=(tuple(range(len(seed.acceptance_criteria))),),
        )
    return graph.to_execution_plan()


async def _execute(executor: ParallelACExecutor, seed: Seed) -> Any:
    from ouroboros.core.seed import ac_texts
    from ouroboros.orchestrator.atomic_prompt_builder import install_sourced_scope

    install_sourced_scope(executor, seed.sourced_scope, ac_texts(seed.acceptance_criteria))
    return await executor.execute_parallel(
        seed=seed,
        execution_plan=_plan(seed),
        session_id="orch_shared",
        execution_id="exec_shared",
        tools=["Read", "Edit", "Bash"],
        tool_catalog=None,
        system_prompt="system",
    )


def test_one_attempt_is_decided_by_structure() -> None:
    scope = SourcedScope(goal=USER_REQUEST)
    assert shares_one_attempt(scope, MODEL_CRITERIA)
    # One criterion: one leaf, as before.
    assert not shares_one_attempt(scope, (CODE_CRITERION,))
    # No sourced scope (normal interview, older Seed): one leaf per criterion.
    assert not shares_one_attempt(None, MODEL_CRITERIA)
    assert shared_attempt_graph(_seed(scope=None)) is None
    # Binding criteria that are the Seed's own, as lists: one leaf each.
    aligned = SourcedScope(goal=USER_REQUEST, acceptance_criteria=MODEL_CRITERIA)
    assert sourced_criteria_aligned(aligned, MODEL_CRITERIA)
    assert not shares_one_attempt(aligned, MODEL_CRITERIA)
    # Binding criteria that are not the Seed's own as lists: one attempt.
    reordered = SourcedScope(goal=USER_REQUEST, acceptance_criteria=MODEL_CRITERIA[::-1])
    assert shares_one_attempt(reordered, MODEL_CRITERIA)
    graph = shared_attempt_graph(_seed())
    assert graph is not None and graph.execution_levels == ((0, 1, 2),)


@pytest.mark.asyncio
async def test_a_shared_attempt_seed_runs_one_worker_and_judges_every_criterion(
    tmp_path,
) -> None:
    runtime = _CountingRuntime(str(tmp_path))
    executor = _executor(runtime)
    judged: list[tuple[int, str]] = []

    async def gate(*, seed: Any, ac_index: int, result: ACExecutionResult, **_: Any) -> Any:
        judged.append((ac_index, result.ac_content))
        return result

    executor.check_package_gate = gate  # type: ignore[attr-defined]
    result = await _execute(executor, _seed())

    assert len(runtime.prompts) == 1
    prompt = runtime.prompts[0]
    assert USER_REQUEST in prompt and GOAL_OBJECTIVE_CRITERION in prompt
    for criterion in MODEL_CRITERIA:
        assert criterion not in prompt
    # Root 0's criterion is documentation-only, the others are not: the one
    # worker is asked for every field a criterion judged from it requires.
    assert _is_documentation_only_ac(DOC_CRITERION)
    assert "(files_touched, commands_run, tests_passed)" in prompt
    assert "documentation-only current AC" not in prompt

    by_index = {r.ac_index: r for r in result.results}
    assert sorted(by_index) == [0, 1, 2]
    assert by_index[0].outcome is ACExecutionOutcome.SUCCEEDED
    for index in (1, 2):
        settled = by_index[index]
        assert settled.outcome is ACExecutionOutcome.SETTLED_FROM_SHARED_ATTEMPT
        assert settled.ac_content == MODEL_CRITERIA[index]
        assert settled.messages == by_index[0].messages
        # The entry point was declared for root 0's check only.
        assert settled.typed_evidence is not None
        assert "entry_points" not in settled.typed_evidence.data
    assert by_index[0].typed_evidence is not None
    assert "entry_points" in by_index[0].typed_evidence.data
    # The check package gate judged each criterion once, on its own text.
    assert sorted(judged) == [(0, DOC_CRITERION), (1, CODE_CRITERION), (2, TEST_CRITERION)]
    assert result.success_count == 3 and result.all_succeeded


@pytest.mark.asyncio
async def test_a_criterion_its_own_check_rejects_fails_without_a_dispatch(tmp_path) -> None:
    runtime = _CountingRuntime(str(tmp_path))
    executor = _executor(runtime)

    async def gate(*, seed: Any, ac_index: int, result: ACExecutionResult, **_: Any) -> Any:
        if ac_index == 2:
            return replace(
                result, success=False, outcome=ACExecutionOutcome.FAILED, error="check failed"
            )
        return result

    executor.check_package_gate = gate  # type: ignore[attr-defined]
    result = await _execute(executor, _seed())

    assert len(runtime.prompts) == 1
    outcomes = {r.ac_index: r.outcome for r in result.results}
    assert outcomes == {
        0: ACExecutionOutcome.SUCCEEDED,
        1: ACExecutionOutcome.SETTLED_FROM_SHARED_ATTEMPT,
        2: ACExecutionOutcome.FAILED,
    }


@pytest.mark.asyncio
async def test_a_failed_shared_attempt_leaves_the_other_criteria_not_attempted(tmp_path) -> None:
    # No test run in the transcript: the legacy verifier rejects root 0.
    runtime = _CountingRuntime(str(tmp_path), support=False)
    result = await _execute(_executor(runtime), _seed())

    assert len(runtime.prompts) == 1
    outcomes = {r.ac_index: r.outcome for r in result.results}
    assert outcomes == {
        0: ACExecutionOutcome.FAILED,
        1: ACExecutionOutcome.BLOCKED,
        2: ACExecutionOutcome.BLOCKED,
    }


@pytest.mark.asyncio
async def test_aligned_binding_criteria_run_one_leaf_each_with_its_own_criterion(
    tmp_path,
) -> None:
    criteria = (CODE_CRITERION, TEST_CRITERION)
    seed = _seed(criteria, SourcedScope(goal=USER_REQUEST, acceptance_criteria=criteria))
    runtime = _CountingRuntime(str(tmp_path))
    result = await _execute(_executor(runtime), seed)

    assert len(runtime.prompts) == 2
    by_criterion = {
        criterion: [prompt for prompt in runtime.prompts if criterion in prompt]
        for criterion in criteria
    }
    assert all(len(prompts) == 1 for prompts in by_criterion.values())
    assert by_criterion[CODE_CRITERION] != by_criterion[TEST_CRITERION]
    assert {r.outcome for r in result.results} == {ACExecutionOutcome.SUCCEEDED}


@pytest.mark.asyncio
async def test_a_seed_without_sourced_scope_runs_one_leaf_per_criterion(tmp_path) -> None:
    runtime = _CountingRuntime(str(tmp_path))
    result = await _execute(_executor(runtime), _seed(scope=None))

    assert len(runtime.prompts) == 3
    assert {r.outcome for r in result.results} == {ACExecutionOutcome.SUCCEEDED}
