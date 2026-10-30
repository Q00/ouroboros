"""A verify command that fails on the untouched base carries no signal.

The gate runs a failing ``verify_command`` once on a fresh copy of the check
package's base snapshot. A command that fails there too is recorded as
``no_signal`` and neither rejects nor retries; a command that passes on the
base still rejects work that makes it fail.
"""

from __future__ import annotations

from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.boundary.binding_flow import snapshot_base
from ouroboros.boundary.tree import tree_digest
from ouroboros.core.seed import AcceptanceCriterionSpec, OntologySchema, Seed, SeedMetadata
from ouroboros.orchestrator.parallel_executor import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelACExecutor,
    _deserialize_verify_gate_outcome,
    _serialize_verify_gate_outcome,
)
from ouroboros.orchestrator.verify_base_signal import VerifyBaseSignal, no_signal_outcome


class _StubAdapter:
    def __init__(self, working_directory: str) -> None:
        self.runtime_backend = "claude"
        self.self_governs_rate_limit = True
        self.working_directory = working_directory
        self.permission_mode = "acceptEdits"


@pytest.mark.asyncio
async def test_base_signal_compares_same_cwd_and_caches_each_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    (workspace / "app").mkdir(parents=True)
    (workspace / "site").mkdir()
    (workspace / "app" / "proof.txt").write_text("original")
    snapshot = snapshot_base(workspace, tmp_path / "store")
    signal = VerifyBaseSignal(snapshot, tree_digest(snapshot))
    (workspace / "app" / "proof.txt").unlink()
    executor = _executor(workspace, signal)

    for cwd, no_signal in [(None, True), ("app", False), ("site", True), ("app", False)]:
        spec = AcceptanceCriterionSpec(
            description="proof exists", verify_command="test -f proof.txt", verify_cwd=cwd
        )
        outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(workspace))
        assert outcome.passed is False
        assert outcome.no_signal is no_signal


@pytest.mark.asyncio
@pytest.mark.parametrize("relative_cwd", ["../outside", "/tmp", "missing", "link"])
async def test_base_signal_rejects_invalid_or_symlink_cwd(
    tmp_path: Path, relative_cwd: str
) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    snapshot = snapshot_base(workspace, tmp_path / "store")
    signal = VerifyBaseSignal(snapshot, tree_digest(snapshot))

    assert (
        await signal.fails_on_base(
            "exit 1", shell_path="/bin/sh", timeout_seconds=5, relative_cwd=relative_cwd
        )
        is False
    )


@pytest.mark.asyncio
async def test_base_signal_rejects_cwd_replaced_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.orchestrator import verify_base_signal

    workspace = tmp_path / "work"
    (workspace / "app").mkdir(parents=True)
    snapshot = snapshot_base(workspace, tmp_path / "store")
    signal = VerifyBaseSignal(snapshot, tree_digest(snapshot))
    outside = tmp_path / "outside"
    outside.mkdir()
    real_run = verify_base_signal.run_with_shell

    async def replace_before_spawn(*args: object, **kwargs: object) -> object:
        cwd = Path(str(kwargs["cwd"]))
        cwd.rename(cwd.with_name("old-app"))
        cwd.symlink_to(outside, target_is_directory=True)
        return await real_run(*args, **kwargs)

    monkeypatch.setattr(verify_base_signal, "run_with_shell", replace_before_spawn)
    assert (
        await signal.fails_on_base(
            "touch escaped; exit 1", shell_path="/bin/sh", timeout_seconds=5, relative_cwd="app"
        )
        is False
    )
    assert not (outside / "escaped").exists()


def _executor(workspace: Path, base_signal: VerifyBaseSignal | None = None) -> ParallelACExecutor:
    executor = ParallelACExecutor(
        adapter=_StubAdapter(str(workspace)),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=True,
        ac_retry_attempts=0,
        verify_command_timeout_seconds=30,
    )
    if base_signal is not None:
        executor._verify_base_signal = base_signal
    return executor


def _workspace_and_signal(tmp_path: Path) -> tuple[Path, VerifyBaseSignal, Path]:
    """A base checkout snapshotted before the patch, then patched in place."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    snapshot = snapshot_base(workspace, tmp_path / "store")
    signal = VerifyBaseSignal(snapshot, tree_digest(snapshot))
    (workspace / "patched.txt").write_text("patched\n", encoding="utf-8")
    return workspace, signal, snapshot


def _seed(spec: AcceptanceCriterionSpec) -> Seed:
    return Seed(
        goal="base signal",
        acceptance_criteria=(spec,),
        ontology_schema=OntologySchema(name="n", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


@pytest.mark.asyncio
async def test_command_failing_on_base_and_after_patch_is_undecided_not_passed(
    tmp_path: Path,
) -> None:
    workspace, signal, _snapshot = _workspace_and_signal(tmp_path)
    executor = _executor(workspace, signal)
    spec = AcceptanceCriterionSpec(description="whole suite", verify_command="exit 3")

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(workspace))

    assert outcome.passed is False
    assert outcome.no_signal is True
    assert outcome.environment_unverifiable is True
    assert outcome.cause == "exit_nonzero"
    assert "status 3" in (outcome.reason or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_budget", [0, 2])
async def test_fails_on_base_and_after_patch_is_never_accepted_and_spends_no_retry(
    tmp_path: Path, retry_budget: int
) -> None:
    """The discriminating case: the fix did not land, and the base fails too.

    The criterion never comes out with ``verify_gate.passed == True``, and the
    retry loop never re-dispatches the worker for it, whatever the budget.
    """
    workspace, signal, _snapshot = _workspace_and_signal(tmp_path)
    executor = ParallelACExecutor(
        adapter=_StubAdapter(str(workspace)),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=True,
        ac_retry_attempts=retry_budget,
        verify_command_timeout_seconds=30,
    )
    executor._verify_base_signal = signal
    seed = _seed(
        AcceptanceCriterionSpec(
            description="regression test passes",
            verify_command="python3 -c 'import sys; sys.exit(1)'",
        )
    )
    dispatches: list[int] = []

    async def fake_batch(**_kwargs: object) -> list[ACExecutionResult]:
        dispatches.append(1)
        return [ACExecutionResult(ac_index=0, ac_content="ac", success=True, retry_attempt=0)]

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
    assert result.verify_gate_outcome is not None
    assert result.verify_gate_outcome.passed is False
    assert result.verify_gate_outcome.no_signal is True
    assert result.outcome != ACExecutionOutcome.FAILED
    assert dispatches == [1]
    emitted = [call.args[0] for call in executor._event_store.append.await_args_list]
    assert not any(event.type == "execution.ac.recovery_exhausted" for event in emitted)


@pytest.mark.asyncio
async def test_no_signal_command_is_quarantined_as_unverified(tmp_path: Path) -> None:
    workspace, signal, _snapshot = _workspace_and_signal(tmp_path)
    executor = _executor(workspace, signal)
    seed = _seed(AcceptanceCriterionSpec(description="whole suite", verify_command="exit 1"))
    result = ACExecutionResult(ac_index=0, ac_content="whole suite", success=True)

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    # Undecided: the worker result stands for confirmation, nothing is retried,
    # and the gate itself never reports a pass.
    assert gated.success is True
    assert gated.outcome != ACExecutionOutcome.FAILED
    assert gated.verify_gate_outcome is not None
    assert gated.verify_gate_outcome.passed is False
    assert gated.verify_gate_outcome.no_signal is True
    assert gated.verify_gate_outcome.environment_unverifiable is True


@pytest.mark.asyncio
async def test_command_passing_on_base_still_rejects_the_patch(tmp_path: Path) -> None:
    workspace, signal, _snapshot = _workspace_and_signal(tmp_path)
    executor = _executor(workspace, signal)
    seed = _seed(
        AcceptanceCriterionSpec(description="regression", verify_command="test ! -f patched.txt")
    )
    result = ACExecutionResult(ac_index=0, ac_content="regression", success=True)

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert gated.success is False
    assert "Verify gate failed" in (gated.error or "")
    assert gated.verify_gate_outcome is not None
    assert gated.verify_gate_outcome.cause == "exit_nonzero"


@pytest.mark.asyncio
async def test_without_a_base_snapshot_the_gate_rejects_as_before(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    spec = AcceptanceCriterionSpec(description="whole suite", verify_command="exit 1")

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(tmp_path))

    assert outcome.passed is False
    assert outcome.cause == "exit_nonzero"


@pytest.mark.asyncio
async def test_a_changed_snapshot_is_not_trusted(tmp_path: Path) -> None:
    workspace, signal, snapshot = _workspace_and_signal(tmp_path)
    (snapshot / "module.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
    executor = _executor(workspace, signal)
    spec = AcceptanceCriterionSpec(description="import", verify_command="exit 1")

    outcome = await executor._run_ac_verify_gate(spec=spec, cwd=str(workspace))

    assert outcome.passed is False
    assert outcome.cause == "exit_nonzero"


@pytest.mark.asyncio
async def test_base_run_is_cached_per_command_and_never_touches_the_snapshot(
    tmp_path: Path,
) -> None:
    _workspace, signal, snapshot = _workspace_and_signal(tmp_path)
    counter = tmp_path / "base_runs.log"
    command = f"echo run >> {counter} && touch scratch.txt && exit 1"
    shell = shutil.which("bash") or "/bin/sh"
    digest_before = tree_digest(snapshot)

    first = await signal.fails_on_base(command, shell_path=shell, timeout_seconds=30)
    second = await signal.fails_on_base(command, shell_path=shell, timeout_seconds=30)

    assert first is True and second is True
    assert counter.read_text(encoding="utf-8").splitlines() == ["run"]
    assert not (snapshot / "scratch.txt").exists()
    assert tree_digest(snapshot) == digest_before


@pytest.mark.asyncio
async def test_an_unjudged_base_run_keeps_the_rejection(tmp_path: Path) -> None:
    _workspace, signal, _snapshot = _workspace_and_signal(tmp_path)
    shell = shutil.which("bash") or "/bin/sh"

    timed_out = await signal.fails_on_base("sleep 5; exit 1", shell_path=shell, timeout_seconds=1)

    assert timed_out is False
    assert VerifyBaseSignal().available is False
    assert await VerifyBaseSignal().fails_on_base(
        "exit 1", shell_path=shell, timeout_seconds=5
    ) is (False)


def test_signal_reads_the_snapshot_and_pinned_digest_from_the_authority(tmp_path: Path) -> None:
    snapshot = tmp_path / "base_snapshot"
    snapshot.mkdir()
    unpinned = SimpleNamespace(state=SimpleNamespace(base_snapshot=snapshot, admission=None))
    pinned = SimpleNamespace(
        state=SimpleNamespace(base_snapshot=snapshot, admission=None, base_snapshot_digest="d" * 64)
    )
    admitted = SimpleNamespace(
        state=SimpleNamespace(
            base_snapshot=snapshot,
            admission=SimpleNamespace(base_tree_digest="e" * 64),
            base_snapshot_digest=None,
        )
    )

    assert VerifyBaseSignal.from_acceptance_authority(None).available is False
    assert VerifyBaseSignal.from_acceptance_authority(unpinned).available is False
    assert VerifyBaseSignal.from_acceptance_authority(pinned).available is True
    assert VerifyBaseSignal.from_acceptance_authority(admitted)._base_digest == "e" * 64


def test_no_signal_outcome_round_trips_through_a_checkpoint() -> None:
    outcome = no_signal_outcome(1, "1 failed", "a" * 64, False)

    serialized = _serialize_verify_gate_outcome(outcome)

    assert serialized is not None
    assert _deserialize_verify_gate_outcome(serialized) == outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_budget", [0, 2])
async def test_unprepared_check_package_makes_a_failing_command_undecided_not_failed(
    tmp_path: Path, retry_budget: int
) -> None:
    """The check package was attempted but not prepared, so no base snapshot exists.

    A failing command takes the same route as one that fails on both base and
    patch: unverified, no rejection, no retry spent.
    """
    executor = ParallelACExecutor(
        adapter=_StubAdapter(str(tmp_path)),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=True,
        ac_retry_attempts=retry_budget,
        verify_command_timeout_seconds=30,
    )
    executor._verify_base_signal = VerifyBaseSignal.for_run(None, check_package_unprepared=True)
    seed = _seed(AcceptanceCriterionSpec(description="regression", verify_command="exit 1"))
    dispatches: list[int] = []

    async def fake_batch(**_kwargs: object) -> list[ACExecutionResult]:
        dispatches.append(1)
        return [ACExecutionResult(ac_index=0, ac_content="ac", success=True, retry_attempt=0)]

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
    assert result.outcome != ACExecutionOutcome.FAILED
    assert result.verify_gate_outcome is not None
    assert result.verify_gate_outcome.passed is False
    assert result.verify_gate_outcome.no_signal is True
    assert result.verify_gate_outcome.environment_unverifiable is True
    assert "no base snapshot" in (result.verify_gate_outcome.reason or "")
    assert dispatches == [1]


@pytest.mark.asyncio
async def test_check_package_off_keeps_rejecting_a_failing_command(tmp_path: Path) -> None:
    executor = _executor(tmp_path, VerifyBaseSignal.for_run(None, check_package_unprepared=False))
    seed = _seed(AcceptanceCriterionSpec(description="regression", verify_command="exit 1"))
    result = ACExecutionResult(ac_index=0, ac_content="regression", success=True)

    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=result, session_id="s", execution_id="e"
    )

    assert gated.success is False
    assert gated.verify_gate_outcome is not None
    assert gated.verify_gate_outcome.cause == "exit_nonzero"
    assert gated.verify_gate_outcome.no_signal is False


def test_a_prepared_authority_is_read_even_when_marked_unprepared(tmp_path: Path) -> None:
    snapshot = tmp_path / "base_snapshot"
    snapshot.mkdir()
    authority = SimpleNamespace(
        state=SimpleNamespace(base_snapshot=snapshot, admission=None, base_snapshot_digest="d" * 64)
    )

    signal = VerifyBaseSignal.for_run(authority, check_package_unprepared=True)

    assert signal.available is True
    assert signal.base_missing is False
