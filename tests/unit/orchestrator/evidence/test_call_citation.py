"""Evidence by transcript call number.

A worker's own passing check says nothing about correctness. A citation is evidence only when the
controller can show more than "a call passed": the call is a base-tree test, it names the changed
code, it passes again when replayed on the frozen artifact, and that replay runs a changed line.
Anything less is no evidence, never fabrication.
"""

from __future__ import annotations

from pathlib import Path
import shlex

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.call_citation import (
    CallState,
    CitationDecision,
    CitationPolicy,
    ReplayOutcome,
    decide_citations,
    describe_command,
    qualify_call,
    recorded_calls,
    touches_change,
)
from ouroboros.orchestrator.evidence.harness_observation import (
    CommandObservation,
    WorkspaceObservation,
    build_observation_message,
)
from ouroboros.orchestrator.evidence.verification import (
    _verify_atomic_evidence_against_runtime_messages,
)
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.failure_taxonomy import FailureClass
from ouroboros.orchestrator.profile_loader import load_profile
from ouroboros.orchestrator.verifier import RetryAdmission, VerifierStatus

CHANGED = frozenset({"pkg/serializer.py"})
BASE_TREE = frozenset({"tests/test_serializer.py", "tests/runtests.py", "pkg/serializer.py"})
PASSING_REPLAY = ReplayOutcome(returncode=0, executed_changed_line=True)


def _bash(command: str, call_id: str, exit_code: int) -> tuple[AgentMessage, AgentMessage]:
    wrapped = "/bin/bash -lc " + shlex.quote(command)
    call = AgentMessage(
        type="assistant",
        content=f"Calling tool: Bash: {wrapped}",
        tool_name="Bash",
        data={"tool_input": {"command": wrapped}, "tool_call_id": call_id},
    )
    result = AgentMessage(
        type="tool_result",
        content="",
        data={
            "tool_call_id": call_id,
            "exit_code": exit_code,
            "tool_result": {
                "is_error": exit_code != 0,
                "meta": {"tool_call_id": call_id, "exit_status": exit_code},
            },
        },
    )
    return call, result


def _call(command: str, exit_status: int | None = 0, number: int = 1):
    return describe_command(number, command, exit_status, task_cwd="/work")


DEFAULT_POLICY = CitationPolicy()


def _qualify(call, replay=PASSING_REPLAY, policy=DEFAULT_POLICY):
    return qualify_call(
        call, policy=policy, replay=replay, changed_paths=CHANGED, base_tree_paths=BASE_TREE
    )


def test_table_numbers_bash_calls_and_reads_recorded_exit() -> None:
    messages = (
        *_bash("ls", "c1", 0),
        *_bash("python -m pytest tests/test_serializer.py -q", "c2", 1),
        *_bash("python -m pytest tests/test_serializer.py -q", "c3", 0),
    )
    calls = recorded_calls(messages, task_cwd="/work")
    assert [(c.number, c.exit_status, c.kind) for c in calls] == [
        (1, 0, "other"),
        (2, 1, "test"),
        (3, 0, "test"),
    ]


def test_output_filter_exit_is_not_the_test_exit() -> None:
    call = _call("python -m pytest tests/test_serializer.py -q 2>&1 | tail -5")
    assert call.masked
    assert call.replay_command == "python -m pytest tests/test_serializer.py -q 2>&1"
    # Without a replay the recorded 0 is tail's exit: no evidence.
    assert _qualify(call, replay=None) == (CallState.UNQUALIFIED, "exit_belongs_to_output_filter")
    # A replay of the command in front of the filter decides.
    assert _qualify(call)[0] is CallState.QUALIFIED
    assert _qualify(call, replay=ReplayOutcome(returncode=1))[0] is CallState.EXECUTED_FAILURE


def test_compound_call_replays_only_the_implied_execution() -> None:
    call = _call(
        "apply_patch <<'PATCH'\n*** Begin Patch\n*** Update File: pkg/serializer.py\n@@\n-a\n+b\n"
        "*** End Patch\nPATCH\nPYTHONPATH=lib python -m pytest tests/test_serializer.py -q 2>&1"
    )
    assert call.kind == "test"
    assert call.replay_command == "PYTHONPATH=lib python -m pytest tests/test_serializer.py -q"
    assert call.files == ("tests/test_serializer.py",)


def test_non_final_list_element_is_not_implied() -> None:
    # ``a; b`` exits with b's status: a zero exit says nothing about ``python repro.py``.
    call = _call("python repro.py; git status --short")
    assert call.kind == "other"
    assert _qualify(call)[0] is CallState.UNQUALIFIED


def test_worker_written_check_is_not_evidence_under_default_policy() -> None:
    script = _call("python repro_issue.py")
    assert script.kind == "script"
    assert _qualify(script) == (CallState.UNQUALIFIED, "not_a_base_tree_test")
    new_test = _call("python -m pytest tests/test_new_behavior.py -q")
    assert _qualify(new_test) == (CallState.UNQUALIFIED, "not_a_base_tree_test")
    inline = _call("python - <<'PY'\nimport pkg.serializer\nassert pkg.serializer.x() == 1\nPY")
    assert inline.kind == "inline"
    assert _qualify(inline)[0] is CallState.UNQUALIFIED


def test_base_tree_test_needs_touch_replay_and_changed_line() -> None:
    call = _call("python -m pytest tests/test_serializer.py -q")
    assert touches_change(call, CHANGED)
    assert _qualify(call) == (CallState.QUALIFIED, None)
    assert _qualify(call, replay=None) == (CallState.UNQUALIFIED, "not_replayed")
    unrun = ReplayOutcome(returncode=0, executed_changed_line=False)
    assert _qualify(call, replay=unrun) == (CallState.UNQUALIFIED, "no_changed_line_executed")
    unrelated = _call("python -m pytest tests/test_other.py -q")
    assert not touches_change(unrelated, CHANGED)
    dotted = _call("python tests/runtests.py pkg.test_serializer")
    assert touches_change(dotted, CHANGED)


def test_failing_or_irrelevant_citations_never_accept() -> None:
    calls = [
        _call("ls -la", number=1),
        _call("python -m pytest tests/test_serializer.py -q", exit_status=1, number=2),
        _call("python -m pytest tests/test_serializer.py -q", number=3),
    ]
    decide = lambda cited, replays: decide_citations(  # noqa: E731
        cited,
        calls,
        policy=CitationPolicy(),
        replays=replays,
        changed_paths=CHANGED,
        base_tree_paths=BASE_TREE,
    )
    assert decide([1], {}).decision is CitationDecision.NO_EVIDENCE
    assert decide([2], {2: PASSING_REPLAY}).decision is CitationDecision.NO_EVIDENCE
    assert decide([], {}).decision is CitationDecision.NO_EVIDENCE
    assert decide([9], {}).reasons == ("call 9: not in transcript",)
    accepted = decide([1, 3], {3: PASSING_REPLAY})
    assert accepted.decision is CitationDecision.ACCEPT and accepted.evidence == (3,)
    # One cited call that fails on the artifact withholds the criterion even when another qualifies.
    failing = [*calls, _call("python -m pytest tests/test_serializer.py -k x", number=4)]
    verdict = decide_citations(
        [3, 4],
        failing,
        policy=CitationPolicy(),
        replays={3: PASSING_REPLAY, 4: ReplayOutcome(returncode=1)},
        changed_paths=CHANGED,
        base_tree_paths=BASE_TREE,
    )
    assert verdict.decision is CitationDecision.NO_EVIDENCE


def _verify(workspace: Path, transcript: tuple[AgentMessage, ...], record: dict):
    return _verify_atomic_evidence_against_runtime_messages(
        messages=(
            *transcript,
            AgentMessage(type="result", content="done", data={"subtype": "success"}),
        ),
        typed_evidence=EvidenceRecord(data=record),
        ac_content="Generated migrations import models when a base class needs it",
        execution_profile=load_profile("code"),
        task_cwd=str(workspace),
        adapter_working_directory=str(workspace),
        verify_gate_active=True,
    )


def test_verifier_hook_withholds_without_qualified_call(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_serializer.py").write_text("def test_ok():\n    assert True\n")
    command = "python -m pytest tests/test_serializer.py -q"
    observation = WorkspaceObservation(
        changed_paths=frozenset({"pkg/serializer.py"}),
        command_runs=(CommandObservation(command=command, returncode=0, output_tail="1 passed"),),
    )
    transcript = (*_bash(command, "c1", 0), build_observation_message(observation))
    verdict = _verify(tmp_path, transcript, {"evidence_calls": [1]})
    # The replay passed, but the harness replay does not report executed lines yet: no evidence,
    # not fabrication. The verdict is unavailable, so the attempt is kept, never retried.
    assert not verdict.passed
    assert verdict.failure_class == FailureClass.NO_CALL_EVIDENCE.value
    assert verdict.status is VerifierStatus.UNAVAILABLE
    assert verdict.retry_admission is RetryAdmission.ACCEPT
    assert "no_changed_line_executed" in verdict.reasons[0]
    # A citation of a call that is not even a test is no evidence too, never fabrication.
    unrelated = _verify(tmp_path, (*_bash("ls -la", "c2", 0),), {"evidence_calls": [1]})
    assert unrelated.failure_class == FailureClass.NO_CALL_EVIDENCE.value
    # A record without ``evidence_calls`` keeps the claim-string path.
    legacy = _verify(
        tmp_path,
        transcript,
        {"files_touched": [], "commands_run": [command], "tests_passed": [command]},
    )
    assert legacy.failure_class != FailureClass.NO_CALL_EVIDENCE.value
