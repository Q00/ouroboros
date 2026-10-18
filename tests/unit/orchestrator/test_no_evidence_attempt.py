"""A code-task attempt without evidence is never complete on its own.

An attempt whose workspace has no diff against the ``HEAD`` the run started
from and that ran no verify command left no evidence (pytest-6197: a check
that already passed on the unchanged code; scikit-learn-25973: a diagnosis
only). It is re-dispatched once with a short factual note, then takes the
undecided route (``no_evidence``) and is ``BLOCKED`` instead of succeeded.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.seed import AcceptanceCriterionSpec, OntologySchema, Seed, SeedMetadata
from ouroboros.orchestrator.attempt_evidence import (
    NO_EVIDENCE_NOTE,
    workspace_diff_evidence,
    workspace_head,
    workspace_unchanged,
)
from ouroboros.orchestrator.parallel_executor import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelACExecutor,
    _deserialize_verify_gate_outcome,
    _serialize_verify_gate_outcome,
)
from ouroboros.orchestrator.verify_quarantine import VERIFY_UNVERIFIABLE_EVENT


class _StubAdapter:
    def __init__(self, working_directory: str) -> None:
        self.runtime_backend = "claude"
        self.self_governs_rate_limit = True
        self.working_directory = working_directory
        self.permission_mode = "acceptEdits"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(("git", "-C", str(cwd), *args), check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "module.py")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _seed(spec: AcceptanceCriterionSpec, *, task_type: str = "code") -> Seed:
    return Seed(
        goal="fix the bug",
        task_type=task_type,
        acceptance_criteria=(spec,),
        ontology_schema=OntologySchema(name="n", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


def _executor(repo: Path, retry_budget: int) -> ParallelACExecutor:
    executor = ParallelACExecutor(
        adapter=_StubAdapter(str(repo)),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=True,
        ac_retry_attempts=retry_budget,
        verify_command_timeout_seconds=30,
    )
    executor._no_evidence.base_head = workspace_head(repo)
    return executor


async def _run(
    executor: ParallelACExecutor,
    seed: Seed,
    worker: Any = None,
) -> tuple[ACExecutionResult, list[dict[int, str] | None]]:
    dispatches: list[dict[int, str] | None] = []

    async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
        dispatches.append(kwargs.get("retry_prompts"))
        if worker is not None:
            worker(len(dispatches))
        return [
            ACExecutionResult(
                ac_index=0,
                ac_content="ac",
                success=True,
                retry_attempt=len(dispatches) - 1,
                outcome=ACExecutionOutcome.SUCCEEDED,
            )
        ]

    executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]
    results = await executor._run_batch_with_verify_and_retry(
        seed=seed,
        batch_executable=[0],
        session_id="s",
        execution_id="e",
        tools=[],
        tool_catalog=None,
        system_prompt="sys",
        level_contexts=[],
        ac_retry_attempts={0: 0},
        execution_counters=None,
    )
    result = results[0]
    assert isinstance(result, ACExecutionResult)
    return result, dispatches


@pytest.mark.asyncio
async def test_attempt_without_change_or_verify_is_redispatched_once_then_blocked(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    executor = _executor(repo, retry_budget=2)
    seed = _seed(AcceptanceCriterionSpec(description="explain why the check passes"))

    result, dispatches = await _run(executor, seed)

    # One re-dispatch, carrying the factual note, and no further retry.
    assert len(dispatches) == 2
    assert dispatches[1] is not None and NO_EVIDENCE_NOTE in dispatches[1][0]
    # Never complete on its own: the undecided route, then BLOCKED.
    assert result.success is False
    assert result.outcome == ACExecutionOutcome.BLOCKED
    assert result.verify_gate_outcome is not None
    assert result.verify_gate_outcome.passed is False
    assert result.verify_gate_outcome.environment_unverifiable is True
    assert result.verify_gate_outcome.cause == "no_evidence"
    emitted = [call.args[0] for call in executor._event_store.append.await_args_list]
    assert any(event.type == VERIFY_UNVERIFIABLE_EVENT for event in emitted)


@pytest.mark.asyncio
async def test_a_redispatch_that_makes_a_change_succeeds(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    executor = _executor(repo, retry_budget=2)
    seed = _seed(AcceptanceCriterionSpec(description="fix the bug"))

    def worker(dispatch: int) -> None:
        if dispatch == 2:
            (repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")

    result, dispatches = await _run(executor, seed, worker)

    assert len(dispatches) == 2
    assert result.success is True
    assert result.outcome == ACExecutionOutcome.SUCCEEDED


@pytest.mark.asyncio
async def test_without_retry_budget_it_is_blocked_at_once(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    executor = _executor(repo, retry_budget=0)
    seed = _seed(AcceptanceCriterionSpec(description="fix the bug"))

    result, dispatches = await _run(executor, seed)

    assert len(dispatches) == 1
    assert result.outcome == ACExecutionOutcome.BLOCKED
    assert result.verify_gate_outcome is not None
    assert result.verify_gate_outcome.cause == "no_evidence"


@pytest.mark.asyncio
async def test_an_attempt_with_a_change_is_untouched(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    executor = _executor(repo, retry_budget=2)
    seed = _seed(AcceptanceCriterionSpec(description="fix the bug"))

    def worker(_dispatch: int) -> None:
        (repo / "new_test.py").write_text("def test(): pass\n", encoding="utf-8")

    result, dispatches = await _run(executor, seed, worker)

    assert len(dispatches) == 1
    assert result.success is True
    assert result.verify_gate_outcome is None


@pytest.mark.asyncio
async def test_an_executed_verify_command_is_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    executor = _executor(repo, retry_budget=2)
    seed = _seed(AcceptanceCriterionSpec(description="check", verify_command="true"))

    result, dispatches = await _run(executor, seed)

    assert len(dispatches) == 1
    assert result.success is True
    assert result.verify_gate_outcome is not None
    assert result.verify_gate_outcome.passed is True


@pytest.mark.asyncio
async def test_a_non_code_task_is_untouched(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    executor = _executor(repo, retry_budget=2)
    seed = _seed(AcceptanceCriterionSpec(description="summarize"), task_type="research")

    result, dispatches = await _run(executor, seed)

    assert len(dispatches) == 1
    assert result.success is True


@pytest.mark.asyncio
async def test_outside_a_git_work_tree_the_check_is_off(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    executor = _executor(plain, retry_budget=2)
    assert executor._no_evidence.base_head is None
    seed = _seed(AcceptanceCriterionSpec(description="fix the bug"))

    result, dispatches = await _run(executor, seed)

    assert len(dispatches) == 1
    assert result.success is True


def test_a_commit_since_the_run_started_is_a_change(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = workspace_head(repo)
    assert workspace_unchanged(workspace_diff_evidence(repo), base) is True

    (repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "worker commit")

    assert workspace_unchanged(workspace_diff_evidence(repo), base) is False


def test_no_evidence_outcome_round_trips_through_a_checkpoint() -> None:
    from ouroboros.orchestrator.verify_gate_outcome import _VerifyGateOutcome

    outcome = _VerifyGateOutcome(
        passed=False,
        reason=NO_EVIDENCE_NOTE,
        output_tail="",
        environment_unverifiable=True,
        cause="no_evidence",
    )

    serialized = _serialize_verify_gate_outcome(outcome)

    assert serialized is not None
    assert _deserialize_verify_gate_outcome(serialized) == outcome
