"""One shared worker attempt sees every criterion it is judged against (#3072).

An ``ooo auto`` Seed whose criteria do not bind runs one worker attempt (root
0), and every other criterion is judged from it (#2737). Verify commands are
hidden from workers by design, so a criterion's ``expected_artifacts`` is the
only way the worker learns which file the harness will look for. While the
shared attempt's prompt carried root 0's contract only, the worker never
learned that AC 2 needs ``tests/test_galaxy_rotation.py``, wrote a different
test, and the run failed with ``Failure Cause: unknown``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    OntologySchema,
    Seed,
    SeedMetadata,
    SourcedScope,
    ac_texts,
)
from ouroboros.mcp.tools.run_failure_meta import derive_run_failure_meta, failure_cause_text
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.atomic_prompt_builder import (
    SEED_CRITERION_SPECS_ATTR,
    _build_covered_criteria_block,
    _build_success_contract_block,
    _shared_attempt_criteria,
    install_sourced_scope,
)
from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
    inherit_check_package_hooks,
)
from ouroboros.orchestrator.profile_loader import load_profile
from ouroboros.orchestrator.run_failure_cause import RunFailure, derive_run_failure
from ouroboros.orchestrator.session import SessionStatus
from ouroboros.orchestrator.shared_attempt import shared_attempt_graph

USER_REQUEST = "Make a beautiful 3D galaxy I can rotate with the mouse in the browser."
RENDER_CRITERION = "Opening index.html renders a 3D galaxy of glowing particles."
ROTATION_CRITERION = "Dragging the mouse shows the same galaxy from a different angle."
ROTATION_TEST = "tests/test_galaxy_rotation.py"
# The hidden grader names a test function the worker is never told about.
HIDDEN_TEST_NAME = "test_mouse_changes_3d_view"
ROTATION_VERIFY = f"grep -q {HIDDEN_TEST_NAME} {ROTATION_TEST} && echo HIDDEN_GRADER_OK"
ROTATION_ASSERTION = "HIDDEN_GRADER_OK"
COVERED_HEADING = "ACCEPTANCE CRITERIA judged from this attempt:"
# The sibling-scope wording a per-criterion leaf gets; none of it may tell the
# shared attempt that a criterion judged from it is someone else's work.
SIBLING_SCOPE_WORDING = (
    "OUT OF SCOPE",
    "Current AC Scope Boundary",
    "Sibling/future ACs",
    "You are responsible only for the current acceptance criterion",
    "Parallel Execution Notice",
)
SESSION = "orch_galaxy"
EXECUTION = "exec_galaxy"


def _rotation_spec() -> AcceptanceCriterionSpec:
    return AcceptanceCriterionSpec(
        description=ROTATION_CRITERION,
        verify_command=ROTATION_VERIFY,
        output_assertion=ROTATION_ASSERTION,
        expected_artifacts=(ROTATION_TEST,),
    )


_REQUEST_ONLY = SourcedScope(goal=USER_REQUEST)


def _seed(scope: SourcedScope | None = _REQUEST_ONLY) -> Seed:
    """AC 1 has no verify command; AC 2 has one and a required test file (the #3072 Seed)."""
    return Seed(
        goal="A paraphrase of the request the generator wrote.",
        acceptance_criteria=(RENDER_CRITERION, _rotation_spec()),
        ontology_schema=OntologySchema(name="n", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.05),
        sourced_scope=scope,
    )


class _GalaxyRuntime:
    """Writes the page, and the declared rotation test only when ``writes_rotation_test``."""

    runtime_backend = "opencode"
    permission_mode = "acceptEdits"

    def __init__(self, working_directory: str, *, writes_rotation_test: bool) -> None:
        self.working_directory = working_directory
        self._writes_rotation_test = writes_rotation_test
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
        workspace = Path(self.working_directory)
        (workspace / "index.html").write_text("<canvas id='galaxy'></canvas>\n", encoding="utf-8")
        written = ["index.html"]
        if self._writes_rotation_test:
            (workspace / "tests").mkdir(exist_ok=True)
            (workspace / ROTATION_TEST).write_text(
                f"def {HIDDEN_TEST_NAME}():\n    assert True\n", encoding="utf-8"
            )
            written.append(ROTATION_TEST)
        else:
            # The #3072 worker: a test of its own choosing, under another name.
            (workspace / "browser.test.cjs").write_text("// rotation\n", encoding="utf-8")
            written.append("browser.test.cjs")
        for path in written:
            yield AgentMessage(
                type="tool",
                content=f"Write {path}",
                tool_name="Write",
                data={
                    "input": {"file_path": path},
                    "subtype": "success",
                    "runtime_event_type": "tool.completed",
                },
            )
        yield AgentMessage(
            type="tool",
            content="pytest passed\n1 passed in 0.01s",
            tool_name="Bash",
            data={"input": {"command": "pytest"}, "output": "1 passed in 0.01s"},
        )
        files = ", ".join(f'"{path}"' for path in written)
        yield AgentMessage(
            type="result",
            content=(
                "```json\n"
                f'{{"files_touched":[{files}],"commands_run":["pytest"],"tests_passed":["pytest"]}}'
                "\n```"
            ),
            data={"subtype": "success"},
            resume_handle=RuntimeHandle(
                backend="opencode",
                kind="implementation_session",
                native_session_id=f"session-{len(self.prompts)}",
                cwd=self.working_directory,
                metadata={},
            ),
        )


def _event_store() -> AsyncMock:
    """An async event store that keeps what was appended and answers queries by type."""
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

    async def _query_events(
        aggregate_id: str | None = None, event_type: str | None = None, **_: Any
    ) -> list[Any]:
        return [
            e
            for e in appended
            if (aggregate_id is None or e.aggregate_id == aggregate_id)
            and (event_type is None or e.type == event_type)
        ]

    store.append.side_effect = _append
    store.replay.side_effect = _replay
    store.query_events.side_effect = _query_events
    store.appended = appended
    return store


async def _passthrough_gate(*, result: ACExecutionResult, **_: Any) -> ACExecutionResult:
    return result


async def _run(
    runtime: _GalaxyRuntime,
    seed: Seed,
    plan: DependencyGraph | None = None,
    *,
    gated: bool = False,
) -> tuple[ParallelACExecutor, Any]:
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=_event_store(),
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
        transcript_verifier="decide",
        run_verify_commands=True,
        ac_retry_attempts=0,
        task_cwd=runtime.working_directory,
    )
    # What the runner installs: the Seed's criteria as written.
    install_sourced_scope(executor, seed.sourced_scope, seed.acceptance_criteria)
    if gated:
        # An admitted check package installs its gate; the verify gate still decides.
        executor.check_package_gate = _passthrough_gate  # type: ignore[attr-defined]
    graph = plan or shared_attempt_graph(seed)
    assert graph is not None
    result = await executor.execute_parallel(
        seed=seed,
        execution_plan=graph.to_execution_plan(),
        session_id=SESSION,
        execution_id=EXECUTION,
        tools=["Read", "Write", "Bash"],
        tool_catalog=None,
        system_prompt="system",
    )
    return executor, result


@pytest.mark.asyncio
@pytest.mark.parametrize("gated", [False, True])
async def test_the_shared_attempt_prompt_names_every_criterion_and_its_artifacts(
    tmp_path, gated: bool
) -> None:
    runtime = _GalaxyRuntime(str(tmp_path), writes_rotation_test=True)
    _, result = await _run(runtime, _seed(), gated=gated)

    assert len(runtime.prompts) == 1
    prompt = runtime.prompts[0]
    assert USER_REQUEST in prompt
    assert _build_covered_criteria_block(_seed().acceptance_criteria) in prompt
    assert f"1. {RENDER_CRITERION}" in prompt
    assert f"2. {ROTATION_CRITERION}" in prompt
    assert f"   - Expected artifacts: {ROTATION_TEST}" in prompt
    # The grader's answer key stays hidden.
    for hidden in (ROTATION_VERIFY, HIDDEN_TEST_NAME, ROTATION_ASSERTION, "grep -q"):
        assert hidden not in prompt
    # No wording makes AC 2 a sibling's work in the attempt it is judged from.
    for wording in SIBLING_SCOPE_WORDING:
        assert wording not in prompt
    # A worker that writes the declared artifact passes AC 2.
    outcomes = {r.ac_index: r.outcome for r in result.results}
    assert outcomes == {
        0: ACExecutionOutcome.SUCCEEDED,
        1: ACExecutionOutcome.SETTLED_FROM_SHARED_ATTEMPT,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("gated", [False, True])
async def test_a_missing_declared_artifact_fails_with_a_cause_that_names_it(
    tmp_path, gated: bool
) -> None:
    runtime = _GalaxyRuntime(str(tmp_path), writes_rotation_test=False)
    executor, result = await _run(runtime, _seed(), gated=gated)

    by_index = {r.ac_index: r for r in result.results}
    assert by_index[0].outcome is ACExecutionOutcome.SUCCEEDED
    assert by_index[1].outcome is ACExecutionOutcome.FAILED
    assert by_index[1].error is not None
    assert by_index[1].error.startswith(
        f"Verify gate failed: expected_artifacts missing: {ROTATION_TEST}"
    )

    store = executor._event_store
    # AC 2 was judged once, from the shared attempt: no retry, no recovery record.
    assert not [e for e in store.appended if e.type == "execution.ac.recovery_exhausted"]
    # Its verify gate's rejection is recorded, with or without a check package gate.
    rejections = [e.data for e in store.appended if e.type == "execution.verify.failed"]
    assert [(d["ac_index"], d["verify_cause"], d["missing_artifacts"]) for d in rejections] == [
        (1, "artifacts_missing", [ROTATION_TEST])
    ]
    failure = derive_run_failure(store.appended, session_id=SESSION, terminal_status="failed")
    assert failure == RunFailure("verify_artifacts_missing", (ROTATION_TEST,))

    meta = await derive_run_failure_meta(
        store, session_id=SESSION, execution_id=EXECUTION, session_status=SessionStatus.FAILED
    )
    assert meta == {
        "failure_cause": "verify_artifacts_missing",
        "failure_reason_code": "validation",
        "failure_missing_artifacts": [ROTATION_TEST],
    }
    assert failure_cause_text(meta) == f"verify_artifacts_missing (missing: {ROTATION_TEST})"


@pytest.mark.asyncio
async def test_per_criterion_prompts_without_a_shared_attempt_are_unchanged(tmp_path) -> None:
    """A Seed without sourced scope, or with aligned binding criteria, dispatches each AC itself."""
    rotation_block = _build_success_contract_block(_rotation_spec())
    criteria_texts = ac_texts(_seed().acceptance_criteria)
    aligned = SourcedScope(goal=USER_REQUEST, acceptance_criteria=criteria_texts)
    for index, scope in enumerate((None, aligned)):
        workspace = tmp_path / str(index)
        workspace.mkdir()
        runtime = _GalaxyRuntime(str(workspace), writes_rotation_test=True)
        seed = _seed(scope)
        assert shared_attempt_graph(seed) is None
        plan = DependencyGraph(
            nodes=(ACNode(index=0, content=RENDER_CRITERION), ACNode(index=1, content="r")),
            execution_levels=((0,), (1,)),
        )
        await _run(runtime, seed, plan)

        assert len(runtime.prompts) == 2
        render_prompt, rotation_prompt = runtime.prompts
        for prompt in runtime.prompts:
            assert COVERED_HEADING not in prompt
        # Each AC carries its own contract only, exactly as the single-AC block renders it.
        assert rotation_block in rotation_prompt
        assert "SUCCESS CONTRACT for this AC" not in render_prompt
        assert ROTATION_TEST not in render_prompt
        assert ROTATION_VERIFY not in rotation_prompt


def test_the_covered_block_lists_text_and_artifacts_and_hides_the_answer_key() -> None:
    block = _build_covered_criteria_block(
        (
            RENDER_CRITERION,
            AcceptanceCriterionSpec(description="Build ships", verify_command="make build"),
            AcceptanceCriterionSpec(
                description="Docs ship",
                verify_command="make docs",
                output_assertion="DOCS_OK",
                expected_artifacts=("docs/index.md", "docs/api.md"),
            ),
        )
    )

    assert block.splitlines() == [
        COVERED_HEADING,
        "This one attempt is the work for every criterion below, and the harness judges "
        "each of them from it. None of them is sibling or future work: all of them are "
        "the current AC of this dispatch.",
        f"1. {RENDER_CRITERION}",
        "2. Build ships",
        "3. Docs ship",
        "   - Expected artifacts: docs/index.md, docs/api.md — ensure they exist in the workspace",
        "- The harness independently verifies each criterion; complete the work from the "
        "goal and these criteria and provide truthful evidence.",
    ]


@pytest.mark.asyncio
async def test_criteria_recorded_as_texts_only_are_listed_without_artifacts(tmp_path) -> None:
    """An executor given only the criteria texts still lists every criterion, never a mismatch."""
    runtime = _GalaxyRuntime(str(tmp_path), writes_rotation_test=True)
    seed = _seed()
    executor = ParallelACExecutor(
        adapter=runtime,  # type: ignore[arg-type]
        event_store=_event_store(),
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
        transcript_verifier="decide",
        run_verify_commands=True,
        ac_retry_attempts=0,
        task_cwd=runtime.working_directory,
    )
    install_sourced_scope(executor, seed.sourced_scope, ac_texts(seed.acceptance_criteria))
    graph = shared_attempt_graph(seed)
    assert graph is not None
    await executor.execute_parallel(
        seed=seed,
        execution_plan=graph.to_execution_plan(),
        session_id=SESSION,
        execution_id=EXECUTION,
        tools=["Read", "Write", "Bash"],
        tool_catalog=None,
        system_prompt="system",
    )

    assert _build_covered_criteria_block(ac_texts(seed.acceptance_criteria)) in runtime.prompts[0]

    # Specs of other criteria than the recorded texts are never shown.
    stale = SimpleNamespace()
    install_sourced_scope(stale, seed.sourced_scope, seed.acceptance_criteria)
    setattr(stale, SEED_CRITERION_SPECS_ATTR, (_rotation_spec(), RENDER_CRITERION))
    assert _shared_attempt_criteria(stale, ac_texts(seed.acceptance_criteria)) == ac_texts(
        seed.acceptance_criteria
    )


def test_a_derived_executor_inherits_the_criteria_as_written() -> None:
    parent = SimpleNamespace()
    install_sourced_scope(parent, _REQUEST_ONLY, _seed().acceptance_criteria)
    child = SimpleNamespace()

    inherit_check_package_hooks(parent, child)

    assert getattr(child, SEED_CRITERION_SPECS_ATTR) == _seed().acceptance_criteria
