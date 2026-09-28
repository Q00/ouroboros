"""Executor hooks for the check package: inert when unset, advisory legacy when set."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.boundary.acceptance import Governor, PackageCriterionStatus
from ouroboros.boundary.authority import (
    LEGACY_DECIDED_FAILURE_CLASS_PREFIX,
    PACKAGE_FAILURE_CLASS_PREFIX,
    CheckPackageAuthority,
)
from ouroboros.boundary.binding import entry_points_request
from ouroboros.boundary.events import RunContract
from ouroboros.boundary.package import seed_criterion_keys, seed_digest
from ouroboros.boundary.run_wiring import CheckPackageSettings
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.types import MCPToolDefinition
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.evidence.ac_classification import _scoped_evidence_record_for_ac
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
    governing_verifier_verdict,
)
from ouroboros.orchestrator.profile_loader import load_profile
from ouroboros.orchestrator.retry_hints import build_ac_retry_prompt, failure_class_for_result


def _make_replaying_event_store() -> tuple[AsyncMock, list[BaseEvent]]:
    """An async event-store mock that replays previously appended events."""
    event_store = AsyncMock()
    appended: list[BaseEvent] = []

    async def _append(event: BaseEvent) -> None:
        appended.append(event)

    async def _replay(aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:
        return [
            event
            for event in appended
            if event.aggregate_type == aggregate_type and event.aggregate_id == aggregate_id
        ]

    event_store.append.side_effect = _append
    event_store.replay.side_effect = _replay
    return event_store, appended


class _FinalMessageRuntime:
    """A runtime that replays scripted tool messages, then one final message."""

    runtime_backend = "opencode"
    working_directory = "/tmp/project"
    permission_mode = "acceptEdits"

    def __init__(
        self,
        final_message: str,
        *,
        native_session_id: str,
        support_messages: tuple[AgentMessage, ...] = (),
    ) -> None:
        self._final_message = final_message
        self._native_session_id = native_session_id
        self._support_messages = support_messages
        self.last_prompt: str | None = None

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> Any:
        del tools, system_prompt, resume_session_id
        self.last_prompt = prompt
        for message in self._support_messages:
            if message.tool_name in {"Edit", "Write"} and "subtype" not in message.data:
                message = replace(
                    message,
                    data={
                        **message.data,
                        "subtype": "success",
                        "runtime_event_type": "tool.completed",
                    },
                )
            yield message
        yield AgentMessage(
            type="result",
            content=self._final_message,
            data={"subtype": "success"},
            resume_handle=RuntimeHandle(
                backend=resume_handle.backend if resume_handle is not None else "opencode",
                kind="implementation_session",
                native_session_id=self._native_session_id,
                cwd="/tmp/project",
                metadata={},
            ),
        )


EVIDENCE = (
    "```json\n"
    '{"files_touched":["src/app.py"],"commands_run":["pytest"],"tests_passed":["pytest"],'
    '"entry_points":[{"symbol":"app.run","call_kind":"function"}]}'
    "\n```"
)


def _runtime(final: str = EVIDENCE, *, support: bool = True) -> _FinalMessageRuntime:
    edit = AgentMessage(
        type="tool",
        content="Edit src/app.py",
        tool_name="Edit",
        data={"input": {"file_path": "src/app.py"}},
    )
    test_run = AgentMessage(
        type="tool",
        content="pytest passed\n1 passed in 0.01s",
        tool_name="Bash",
        data={"input": {"command": "pytest"}, "output": "1 passed in 0.01s"},
    )
    # Without the test run, the tests_passed claim has no transcript support
    # and the legacy verifier rejects the attempt.
    return _FinalMessageRuntime(
        final,
        native_session_id="session-hooks",
        support_messages=(edit, test_run) if support else (edit,),
    )


def _executor(runtime: Any) -> ParallelACExecutor:
    event_store, _ = _make_replaying_event_store()
    return ParallelACExecutor(
        adapter=runtime,
        event_store=event_store,
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
    )


async def _run(executor: ParallelACExecutor) -> ACExecutionResult:
    return await executor._execute_atomic_ac(
        ac_index=0,
        ac_content="Implement AC 1",
        session_id="orch_hooks",
        tools=["Read", "Edit", "Bash"],
        tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
        system_prompt="system",
        seed_goal="Ship the feature",
        depth=0,
        start_time=datetime.now(UTC),
    )


def test_code_profile_declares_entry_points_optional_and_scoping_keeps_it() -> None:
    profile = load_profile("code")
    assert profile.evidence_schema.optional == ("entry_points",)
    assert "entry_points" not in profile.evidence_schema.required
    record = EvidenceRecord(
        data={
            "files_touched": ["a.py"],
            "commands_run": ["pytest"],
            "tests_passed": ["pytest"],
            "entry_points": [{"symbol": "a.f"}],
            "other": 1,
        }
    )
    scoped = _scoped_evidence_record_for_ac(profile, "Implement AC 1", record)
    assert scoped.data["entry_points"] == [{"symbol": "a.f"}]
    assert "other" not in scoped.data


@pytest.mark.asyncio
async def test_entry_points_request_is_added_only_with_the_check_package_on() -> None:
    off_runtime = _runtime()
    await _run(_executor(off_runtime))
    on_runtime = _runtime()
    executor = _executor(on_runtime)
    interface = {"call_kind": "function", "params": ["value", "low", "high"]}
    executor.check_package_interfaces = {0: interface}  # type: ignore[attr-defined]
    await _run(executor)
    off, on = off_runtime.last_prompt, on_runtime.last_prompt
    assert off is not None and on is not None
    assert "entry_points" not in off
    note = entry_points_request(interface)
    assert note in on and "(value, low, high)" in on

    # Flag off: the prompt is exactly the prompt without the note (per-run
    # digests aside).
    def masked(text: str) -> str:
        return re.sub(r"sha256:[0-9a-f]{64}", "sha256:*", text)

    assert masked(on.replace(note, "")) == masked(off)


@pytest.mark.asyncio
async def test_legacy_rejection_is_advisory_only_with_a_gate_installed() -> None:
    # No transcript support for the tests_passed claim: the legacy verifier rejects.
    rejected = await _run(_executor(_runtime(support=False)))
    assert rejected.success is False and rejected.error

    calls: list[int] = []

    async def gate(
        *, seed: Any, ac_index: int, result: ACExecutionResult, **_run: Any
    ) -> ACExecutionResult:
        calls.append(ac_index)
        return result

    executor = _executor(_runtime(support=False))
    executor.check_package_gate = gate  # type: ignore[attr-defined]
    advisory = await _run(executor)
    assert advisory.success is True
    # The legacy verdict stays on the result for telemetry annotation.
    assert advisory.atomic_verifier_verdict is not None
    assert advisory.atomic_verifier_verdict.passed is False
    assert advisory.typed_evidence is not None
    assert advisory.typed_evidence.get("entry_points") == [
        {"symbol": "app.run", "call_kind": "function"}
    ]

    seed = MagicMock()
    seed.acceptance_criteria = ("Implement AC 1",)
    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=advisory, session_id="s", execution_id="e"
    )
    assert calls == [0] and gated is advisory


def _one_criterion_seed() -> Any:
    from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

    return Seed(
        goal="g",
        acceptance_criteria=("AC",),
        ontology_schema=OntologySchema(name="n", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


def _unadmitted_authority(seed: Any = None) -> CheckPackageAuthority:
    """An authority whose run admitted no package (construction failed or all uncovered)."""
    seed = seed if seed is not None else _one_criterion_seed()
    state = SimpleNamespace(
        admitted=False,
        package=None,
        admission=None,
        execution_id="exec",
        seed_digest=seed_digest(seed),
        criterion_keys=seed_criterion_keys(seed),
        boundary_id="exec/check_package/v1",
        contract=RunContract(check_timeout_seconds=120),
    )
    return CheckPackageAuthority(
        state,  # type: ignore[arg-type]
        CheckPackageSettings(True),
        event_store=MagicMock(),
        candidate_checkout=MagicMock(),
    )


@pytest.mark.asyncio
async def test_without_an_admitted_package_install_leaves_the_executor_legacy() -> None:
    authority = _unadmitted_authority()
    runtime = _runtime(support=False)
    executor = _executor(runtime)
    authority.install(executor)
    assert getattr(executor, "check_package_gate", None) is None
    assert getattr(executor, "check_package_interfaces", None) is None
    assert authority.installed is False
    # The legacy verifier's rejection fails the attempt (and drives the retry),
    # exactly as with the check package off; the prompt asks for no entry_points.
    rejected = await _run(executor)
    assert rejected.success is False and rejected.error
    assert rejected.legacy_rejection is None
    assert runtime.last_prompt is not None and "entry_points" not in runtime.last_prompt


@pytest.mark.asyncio
async def test_the_gate_without_an_admitted_package_lets_the_legacy_verifier_decide() -> None:
    seed = _one_criterion_seed()
    authority = _unadmitted_authority(seed)
    advisory = ACExecutionResult(
        ac_index=0,
        ac_content="AC",
        success=True,
        outcome=ACExecutionOutcome.SUCCEEDED,
        legacy_rejection="evidence form mismatch",
    )
    decided = await authority.gate(seed=seed, ac_index=0, result=advisory, execution_id="exec")
    assert decided.success is False and decided.outcome is ACExecutionOutcome.FAILED
    assert str(decided.check_package_failure_class).startswith(LEGACY_DECIDED_FAILURE_CLASS_PREFIX)


@pytest.mark.asyncio
async def test_a_root_without_an_interface_gets_no_entry_points_request() -> None:
    off_runtime = _runtime()
    await _run(_executor(off_runtime))
    runtime = _runtime()
    executor = _executor(runtime)
    # An admitted oracle checks another root only.
    executor.check_package_interfaces = {1: {"call_kind": "function", "params": ["x"]}}  # type: ignore[attr-defined]
    await _run(executor)
    assert runtime.last_prompt is not None and off_runtime.last_prompt is not None
    assert "entry_points" not in runtime.last_prompt

    def masked(text: str) -> str:
        return re.sub(r"sha256:[0-9a-f]{64}", "sha256:*", text)

    assert masked(runtime.last_prompt) == masked(off_runtime.last_prompt)


def test_retry_prompt_carries_the_package_counterexample_and_class() -> None:
    base = ACExecutionResult(ac_index=0, ac_content="AC", success=False, error="legacy said no")
    assert failure_class_for_result(base) is None
    repaired = replace(
        base,
        error="check package failed",
        check_package_repair="It called your declared entry point: function m.lerp.\n- lerp(0, 10, 0.5): expected 5, observed 0",
        check_package_failure_class="CHECK_PACKAGE_FAIL:abc123",
    )
    assert failure_class_for_result(repaired) == "CHECK_PACKAGE_FAIL:abc123"
    prompt = build_ac_retry_prompt(
        failure_class=failure_class_for_result(repaired),
        outcome=None,
        result=repaired,
        ac_content="AC",
        is_final_attempt=False,
    )
    assert "### Check package counterexample" in prompt
    assert "function m.lerp" in prompt and "expected 5, observed 0" in prompt
    assert "Last error" not in prompt


@pytest.mark.asyncio
async def test_resume_never_restores_legacy_rejected_work_as_succeeded(tmp_path: Any) -> None:
    # in an arm-on run the gate keeps the legacy rejection as an
    # annotation (on the root, or on a sub-AC of a decomposed root) and the
    # package decides after the worker stops. A resumed process has no
    # package, so the checkpoint records that work as failed and the resumed
    # run restores it as failed (the legacy verdict decides).
    from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
    from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph
    from ouroboros.orchestrator.parallel_executor_models import (
        ACExecutionOutcome,
        checkpoint_outcome,
    )

    def _make_executor(*, working_directory: str, run_verify_commands: bool) -> Any:
        adapter = MagicMock()
        adapter.working_directory = working_directory
        adapter.runtime_backend = "claude"
        return ParallelACExecutor(
            adapter=adapter,
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
            run_verify_commands=run_verify_commands,
            ac_retry_attempts=0,
        )

    seed = Seed(
        goal="resume",
        acceptance_criteria=("rejected root", "rejected sub-AC", "clean"),
        ontology_schema=OntologySchema(name="n", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )
    plan = DependencyGraph(
        nodes=tuple(ACNode(index=i, content=f"ac {i}", depends_on=()) for i in range(3)),
        execution_levels=((0, 1, 2),),
    ).to_execution_plan()
    rejected_sub = ACExecutionResult(
        ac_index=1, ac_content="sub", success=True, legacy_rejection="evidence form mismatch"
    )
    results = [
        ACExecutionResult(
            ac_index=0, ac_content="ac 0", success=True, legacy_rejection="form mismatch"
        ),
        ACExecutionResult(
            ac_index=1,
            ac_content="ac 1",
            success=True,
            is_decomposed=True,
            sub_results=(
                ACExecutionResult(ac_index=1, ac_content="sub ok", success=True),
                rejected_sub,
            ),
        ),
        ACExecutionResult(ac_index=2, ac_content="ac 2", success=True),
    ]
    assert [checkpoint_outcome(result) for result in results] == [
        "failed",
        "failed",
        "succeeded",
    ]
    checkpoint_store = MagicMock()
    checkpoint_store.load.return_value = type("LoadResult", (), {"is_ok": False})()
    checkpoint_store.save.return_value = type("SaveResult", (), {"is_ok": True})()
    executor = _make_executor(working_directory=str(tmp_path), run_verify_commands=False)
    executor._checkpoint_store = checkpoint_store
    executor._execute_ac_batch = AsyncMock(return_value=results)
    await executor.execute_parallel(
        seed=seed,
        execution_plan=plan,
        session_id="session-arm-on",
        execution_id="execution-arm-on",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="system",
    )
    checkpoint = checkpoint_store.save.call_args.args[0]
    assert checkpoint.state["ac_outcomes"] == {"0": "failed", "1": "failed", "2": "succeeded"}

    restore_store = MagicMock()
    restore_store.load.return_value = type("LoadResult", (), {"is_ok": True, "value": checkpoint})()
    restore_store.save.return_value = type("SaveResult", (), {"is_ok": True})()
    resumed = _make_executor(working_directory=str(tmp_path), run_verify_commands=False)
    resumed._checkpoint_store = restore_store
    resumed._execute_ac_batch = AsyncMock()
    recovered = await resumed.execute_parallel(
        seed=seed,
        execution_plan=plan,
        session_id="session-arm-on",
        execution_id="execution-arm-on",
        tools=["Read"],
        tool_catalog=None,
        system_prompt="system",
    )
    outcomes = {result.ac_index: result.outcome for result in recovered.results}
    assert outcomes == {
        0: ACExecutionOutcome.FAILED,
        1: ACExecutionOutcome.FAILED,
        2: ACExecutionOutcome.SUCCEEDED,
    }
    assert not recovered.all_succeeded
    resumed._execute_ac_batch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("command, gate_runs", [("exit 3", False), ("exit 0", True)])
async def test_the_package_gate_runs_after_the_verify_command_on_a_passing_result(
    tmp_path: Any, command: str, gate_runs: bool
) -> None:
    # M2: a result the package gate fails always passed the verify command, so
    # the final decision may accept it again without skipping any other gate;
    # a result the verify command failed never reaches the gate.
    from ouroboros.core.seed import AcceptanceCriterionSpec, OntologySchema, Seed, SeedMetadata
    from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome

    executor = ParallelACExecutor(
        adapter=MagicMock(working_directory=str(tmp_path), runtime_backend="claude"),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=True,
    )
    executor._task_cwd = str(tmp_path)  # type: ignore[attr-defined]
    seen: list[Any] = []

    async def gate(
        *, seed: Any, ac_index: int, result: ACExecutionResult, **_run: Any
    ) -> ACExecutionResult:
        seen.append(result.verify_gate_outcome)
        return replace(
            result,
            success=False,
            outcome=ACExecutionOutcome.FAILED,
            check_package_failure_class="CHECK_PACKAGE_FAIL:abc",
        )

    executor.check_package_gate = gate  # type: ignore[attr-defined]
    seed = Seed(
        goal="g",
        acceptance_criteria=(AcceptanceCriterionSpec(description="ac", verify_command=command),),
        ontology_schema=OntologySchema(name="n", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )
    attempt = ACExecutionResult(
        ac_index=0, ac_content="ac", success=True, outcome=ACExecutionOutcome.SUCCEEDED
    )
    gated = await executor._apply_verify_gate(
        seed=seed, ac_index=0, result=attempt, session_id="s", execution_id="e"
    )
    assert gated.success is False
    if gate_runs:
        (outcome,) = seen
        assert outcome is not None and outcome.passed is True
        assert gated.check_package_failure_class == "CHECK_PACKAGE_FAIL:abc"
    else:
        assert seen == [] and gated.check_package_failure_class is None


def test_only_a_verdict_handed_back_to_the_legacy_verifier_routes_control_flow() -> None:
    from ouroboros.orchestrator.verifier import VerifierVerdict

    legacy = VerifierVerdict(passed=False, reasons=("r",), failure_class="FABRICATION_SUSPECTED")
    ungated = ACExecutionResult(
        ac_index=0, ac_content="AC", success=False, atomic_verifier_verdict=legacy
    )
    assert governing_verifier_verdict(ungated) is legacy
    # The package gate decided: the legacy verdict is advisory and routes nothing.
    package_failed = replace(
        ungated,
        legacy_rejection="evidence form mismatch",
        check_package_failure_class=f"{PACKAGE_FAILURE_CLASS_PREFIX}:abc",
    )
    assert governing_verifier_verdict(package_failed) is None
    # The gate handed the criterion back to the legacy verifier: its verdict routes again.
    handed_back = replace(
        package_failed,
        check_package_failure_class=f"{LEGACY_DECIDED_FAILURE_CLASS_PREFIX}:FABRICATION_SUSPECTED",
    )
    assert governing_verifier_verdict(handed_back) is legacy


@pytest.mark.asyncio
async def test_cross_harness_redispatch_is_routed_by_the_package_decision_not_the_legacy_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ouroboros.orchestrator.cross_harness_redispatch as redispatch
    from ouroboros.orchestrator.verifier import VerifierVerdict

    seen: list[Any] = []

    def decide(**kwargs: Any) -> Any:
        seen.append(kwargs["failure"])
        return redispatch.AltHarnessDecision(False, "opencode", None, None, "not_eligible")

    monkeypatch.setattr(redispatch, "decide_alt_harness_redispatch", decide)
    executor = _executor(_runtime())
    executor._cross_harness_redispatch_enabled = True  # type: ignore[attr-defined]
    result = ACExecutionResult(
        ac_index=0,
        ac_content="AC",
        success=False,
        atomic_verifier_verdict=VerifierVerdict(
            passed=False, reasons=("r",), failure_class="FABRICATION_SUSPECTED"
        ),
        legacy_rejection="the legacy verifier rejected the evidence",
        check_package_failure_class=f"{PACKAGE_FAILURE_CLASS_PREFIX}:abc",
    )
    rerun = {
        "ac_index": 0,
        "is_sub_ac": False,
        "parent_ac_index": None,
        "sub_ac_index": None,
        "node_identity": None,
    }
    decided = await executor._maybe_redispatch_alt_harness(
        result=result,
        execution_context_id="exec",
        rerun_kwargs=rerun,
        atomic_retry_attempt=0,
        stall_retries_exhausted=False,
    )
    assert decided is None and seen == [None]


@pytest.mark.asyncio
async def test_the_cross_harness_alternate_runs_under_the_installed_package_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ouroboros.boundary.acceptance import CriterionVerdict, reconcile_acceptance
    from ouroboros.boundary.authority import existing_outcomes_from_results
    from ouroboros.boundary.binding import CheckTier
    from ouroboros.orchestrator.cross_harness_redispatch import AltHarnessDecision
    import ouroboros.orchestrator.parallel_executor as executor_module
    from ouroboros.orchestrator.parallel_executor_models import ParallelExecutionResult
    import ouroboros.orchestrator.runtime_factory as runtime_factory

    calls: list[int] = []

    async def gate(
        *, seed: Any, ac_index: int, result: ACExecutionResult, **_run: Any
    ) -> ACExecutionResult:
        calls.append(ac_index)
        return result

    interfaces = {0: {"call_kind": "function", "params": ["x"]}}
    parent = _executor(_runtime())
    parent.check_package_gate = gate  # type: ignore[attr-defined]
    parent.check_package_interfaces = interfaces  # type: ignore[attr-defined]
    # The alternate backend's transcript does not support the tests_passed
    # claim: the legacy verifier rejects the alternate attempt.
    alternate_runtime = _runtime(support=False)

    async def create(_factory: Any, **_kwargs: Any) -> Any:
        return alternate_runtime

    derived: list[ParallelACExecutor] = []
    invoke = executor_module._invoke_execution_authority_entry

    def entry(executor: ParallelACExecutor, which: Any, *args: Any, **kwargs: Any) -> Any:
        # The registry returns what the entry returns (a coroutine for async
        # entries); only the single-criterion replay is replaced.
        if which != executor_module._FOUNDATION_A_ENTRY_EXECUTE_SINGLE_AC:
            return invoke(executor, which, *args, **kwargs)
        # The alternate replays the criterion as an atomic attempt.
        derived.append(executor)
        return _run(executor)

    monkeypatch.setattr(runtime_factory, "create_agent_runtime_async", create)
    monkeypatch.setattr(executor_module, "_invoke_execution_authority_entry", entry)
    alternate = await parent._run_single_ac_on_backend(
        "claude",
        rerun_kwargs={"session_id": "orch_hooks", "ac_index": 0, "execution_id": "exec"},
        retry_attempt=1,
        decision=AltHarnessDecision(True, "opencode", "claude", None, "test"),
        runtime_identity=SimpleNamespace(ac_id="exec:0"),  # type: ignore[arg-type]
        failure_class=None,
    )
    # The derived executor carries the parent's hooks: the same gate, and the
    # entry_points request for the covered root in its prompt.
    [child] = derived
    assert getattr(child, "check_package_gate", None) is gate
    assert getattr(child, "check_package_interfaces", None) is interfaces
    assert alternate_runtime.last_prompt is not None
    assert "entry_points" in alternate_runtime.last_prompt
    # The legacy rejection is advisory there too, so the attempt reaches the gate.
    assert alternate is not None and alternate.success is True
    assert alternate.legacy_rejection
    seed = MagicMock()
    seed.acceptance_criteria = ("Implement AC 1",)
    gated = await parent._apply_verify_gate(
        seed=seed, ac_index=0, result=alternate, session_id="s", execution_id="e"
    )
    assert calls == [0]
    # The terminal package PASS of the covered criterion is the durable decision.
    parallel = ParallelExecutionResult(results=(gated,), success_count=1, failure_count=0)
    legacy = existing_outcomes_from_results(parallel, gated=True)
    reconciliation = reconcile_acceptance(
        ("k0",),
        {
            "k0": CriterionVerdict(
                "k0",
                PackageCriterionStatus.PASS,
                CheckTier.A,
                "passed",
                declared_binding_pass=False,
            )
        },
        legacy,
        existing_run_accepted=True,
        legacy_decides_unverified=True,
    )
    [decision] = reconciliation.decisions
    assert decision.accepted and decision.governed_by is Governor.CHECK_PACKAGE
