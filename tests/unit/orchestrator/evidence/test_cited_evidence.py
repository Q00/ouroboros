"""Evidence cited by call number in the evidence turn.

The worker cites the controller's own numbered record of its shell calls; the
controller reads its own record of a cited call (recorded command, recorded
result) and never compares the worker's text with the transcript.

The diagnosis of eight officially resolved SWE-bench artifacts rejected as
``FABRICATION_SUSPECTED`` on v0.55.4 found no fabrication: 14 claims the
transcript recorded running were unsupported only because of their command
shape (a test runner inside a compound call, a ``PYTHONPATH=lib`` prefix, a
non-final element of a command list, a here-document program or ``-k``
narrowing), and 2 were worker-record problems (a run whose latest invocation
failed, a lint command cited as a test). Each shape is encoded below as a
Codex-shaped transcript and cited by number.
"""

from __future__ import annotations

from pathlib import Path
import shlex

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.call_ledger import (
    CallResult,
    ResultSource,
    build_call_ledger,
)
from ouroboros.orchestrator.evidence.cited_evidence import (
    Citations,
    CitedEvidence,
    RelevanceDecision,
    ReplayOutcome,
)
from ouroboros.orchestrator.evidence.harness_observation import (
    WorkspaceObservation,
    build_observation_message,
)
from ouroboros.orchestrator.evidence.verification import (
    _verify_atomic_evidence_against_runtime_messages,
)
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.failure_taxonomy import FailureClass
from ouroboros.orchestrator.profile_loader import load_profile
from ouroboros.orchestrator.verifier import (
    EVIDENCE_PATH_CITATIONS,
    RetryAdmission,
    VerifierStatus,
    VerifierVerdict,
)

AC = "The fix makes the reported case behave as the issue expects"
FABRICATION = FailureClass.FABRICATION_SUSPECTED.value
WITHHELD = FailureClass.CITED_EVIDENCE_WITHHELD.value

PATCH = "apply_patch <<'PATCH'\n*** Begin Patch\n*** Update File: pkg/mod.py\n*** End Patch\nPATCH"


def codex_call(
    command: str, call_id: str, exit_code: int, *, output: str = ""
) -> tuple[AgentMessage, AgentMessage]:
    """A Codex-shaped shell call and its result (integer exit status)."""
    wrapped = "/bin/bash -lc " + shlex.quote(command)
    return (
        AgentMessage(
            type="assistant",
            content=f"Calling tool: Bash: {wrapped}",
            tool_name="Bash",
            data={"tool_input": {"command": wrapped}, "tool_call_id": call_id},
        ),
        AgentMessage(
            type="tool_result",
            content="",
            tool_name="Bash",
            data={
                "subtype": "tool_result",
                "tool_call_id": call_id,
                "output": output,
                "exit_code": exit_code,
                "is_error": exit_code != 0,
                "tool_result": {
                    "is_error": exit_code != 0,
                    "meta": {"tool_call_id": call_id, "exit_status": exit_code},
                },
            },
        ),
    )


def claude_call(
    command: str, call_id: str, *, is_error: bool, text: str = ""
) -> tuple[AgentMessage, AgentMessage]:
    """A Claude-shaped shell call: the result records only ``is_error``."""
    return (
        AgentMessage(
            type="assistant",
            content=f"Calling tool: Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command}, "tool_call_id": call_id},
        ),
        AgentMessage(
            type="tool_result",
            content=text,
            data={
                "subtype": "tool_result",
                "tool_call_id": call_id,
                "is_error": is_error,
                "tool_result": {
                    "content": [],
                    "text_content": text,
                    "is_error": is_error,
                    "meta": {"tool_call_id": call_id},
                },
            },
        ),
    )


def transcript(*calls: tuple[AgentMessage, AgentMessage]) -> tuple[AgentMessage, ...]:
    messages: list[AgentMessage] = []
    for call in calls:
        messages.extend(call)
    messages.append(
        build_observation_message(WorkspaceObservation(changed_paths=frozenset({"pkg/mod.py"})))
    )
    messages.append(AgentMessage(type="result", content="done"))
    return tuple(messages)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    for relative in (
        "pkg/mod.py",
        "tests/runtests.py",
        "bin/test",
        "reproduce_subfigure_legend.py",
        "lib/matplotlib/tests/test_figure.py",
        "sklearn/tree/tests/test_export.py",
        "sympy/combinatorics/tests/test_perm_groups.py",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n", encoding="utf-8")
    return tmp_path


def verify(
    workspace: Path,
    messages: tuple[AgentMessage, ...],
    *,
    tests_passed: tuple[int, ...] = (),
    commands_run: tuple[int, ...] = (),
    is_error_is_exit_verdict: bool = False,
    replays: tuple[ReplayOutcome, ...] = (),
    relevance: tuple[RelevanceDecision, ...] = (),
    citations: Citations | None = None,
) -> VerifierVerdict:
    cited = CitedEvidence(
        citations=citations
        if citations is not None
        else Citations(tests_passed=tests_passed, commands_run=commands_run),
        is_error_is_exit_verdict=is_error_is_exit_verdict,
        replays=replays,
        relevance=relevance,
    )
    record = EvidenceRecord(
        data={
            "files_touched": ["pkg/mod.py"],
            # The worker's strings are not read on this path: deliberately junk.
            "commands_run": ["(see citations)"],
            "tests_passed": ["(see citations)"],
        },
        cited=cited,
    )
    return _verify_atomic_evidence_against_runtime_messages(
        messages=messages,
        typed_evidence=record,
        ac_content=AC,
        execution_profile=load_profile("code"),
        task_cwd=str(workspace),
        adapter_working_directory=str(workspace),
        verify_gate_active=True,
    )


# Each diagnosed claim: (id, shape, the cited call, the field it was claimed in).
# The cited call is the call the transcript recorded; a ``tests_passed`` claim is
# cited under both fields (a passing test run was also run).
DIAGNOSED_CLAIMS = [
    # Test runner inside a compound call (4).
    ("django-runtests", f"{PATCH}\npython tests/runtests.py migrations.test_writer", "tests"),
    (
        "sklearn-pytest-ac1",
        "python _repro_export_text.py && pytest -q sklearn/tree/tests/test_export.py -k export_text",
        "tests",
    ),
    (
        "sklearn-pytest-ac2",
        f"{PATCH}\npytest -q sklearn/tree/tests/test_export.py -k export_text",
        "tests",
    ),
    (
        "sklearn-node-ids",
        f"{PATCH}\npython -m pytest sklearn/tree/tests/test_export.py::test_export_text "
        "sklearn/tree/tests/test_export.py::test_export_text_errors -q",
        "tests",
    ),
    # ``PYTHONPATH=lib`` prefix (5).
    ("mpl-under-repro", "PYTHONPATH=lib python reproduce_subfigure_legend.py", "tests"),
    (
        "mpl-under-pytest",
        "PYTHONPATH=lib python -m pytest lib/matplotlib/tests/test_figure.py::test_figure_legend -q",
        "tests",
    ),
    ("mpl-full-repro", "PYTHONPATH=lib python reproduce_subfigure_legend.py", "tests"),
    (
        "mpl-full-inline",
        "PYTHONPATH=lib python -c 'import matplotlib.pyplot as plt; fig = plt.figure(); "
        'subfig = fig.subfigures(); ax = subfig.subplots(); ax.plot([0, 1, 2], label="x"); '
        "subfig.legend()'",
        "tests",
    ),
    (
        "mpl-full-commands",
        "PYTHONPATH=lib python -m pytest lib/matplotlib/tests/test_figure.py -q",
        "commands",
    ),
    # Non-final element of a command list (3).
    ("sympy-diff-check-first", "git diff --check\ngit status --short", "commands"),
    (
        "sympy-diff-check-before-test",
        "git diff --check; bin/test sympy/combinatorics/tests/test_perm_groups.py",
        "commands",
    ),
    (
        "sympy-tmp-repro-first",
        "python /tmp/reproduce_sylow.py\nbin/test sympy/combinatorics/tests/test_perm_groups.py",
        "commands",
    ),
    # Here-document program and ``-k`` narrowing (2).
    (
        "sympy-heredoc",
        "python - <<'PY'\nfrom sympy.combinatorics.named_groups import DihedralGroup\n"
        "for n in range(3, 31):\n    DihedralGroup(n).sylow_subgroup(2)\nPY",
        "tests",
    ),
    (
        "sympy-k-narrowing",
        "bin/test sympy/combinatorics/tests/test_perm_groups.py -k minimal_blocks sylow_subgroup",
        "tests",
    ),
]


class TestDiagnosedShapes:
    """The 14 claims the string path rejected are proven by citation, none fabricated."""

    def test_the_diagnosis_has_fourteen_claims(self) -> None:
        assert len(DIAGNOSED_CLAIMS) == 14

    @pytest.mark.parametrize(
        ("command", "field"),
        [(command, field) for _id, command, field in DIAGNOSED_CLAIMS],
        ids=[claim_id for claim_id, _command, _field in DIAGNOSED_CLAIMS],
    )
    def test_cited_claim_is_proven(self, workspace: Path, command: str, field: str) -> None:
        edit = codex_call(PATCH, "item_1", 0)
        run = codex_call(command, "item_2", 0)
        # A separate passing test call carries tests_passed for a commands_run claim.
        test = codex_call("pytest -q pkg", "item_3", 0)
        messages = transcript(edit, run, test)
        if field == "tests":
            verdict = verify(workspace, messages, tests_passed=(2,), commands_run=(2,))
        else:
            verdict = verify(workspace, messages, tests_passed=(3,), commands_run=(2,))
        assert verdict.passed, verdict.reasons
        assert verdict.decided_by == EVIDENCE_PATH_CITATIONS
        assert "call:commands_run:2" in verdict.evidence_used
        if field == "tests":
            assert "call:tests_passed:2" in verdict.evidence_used

    def test_a_script_outside_the_artifact_is_not_replayed_and_another_citation_carries(
        self, workspace: Path
    ) -> None:
        repro = codex_call("python /tmp/reproduce_sylow.py", "item_1", 0)
        suite = codex_call("bin/test sympy/combinatorics/tests/test_perm_groups.py", "item_2", 0)
        verdict = verify(
            workspace, transcript(repro, suite), tests_passed=(1, 2), commands_run=(1,)
        )
        assert verdict.passed, verdict.reasons
        assert verdict.not_replayed == (
            "script_absent_from_artifact: tests_passed: [1] script_absent_from_artifact",
        )

    def test_only_an_absent_script_cited_is_no_evidence_not_fabrication(
        self, workspace: Path
    ) -> None:
        repro = codex_call("python /tmp/reproduce_sylow.py", "item_1", 0)
        verdict = verify(workspace, transcript(repro), tests_passed=(1,), commands_run=(1,))
        assert verdict.failure_class == FailureClass.SCRIPT_ABSENT_FROM_ARTIFACT.value
        assert verdict.status is VerifierStatus.UNAVAILABLE
        assert verdict.retry_admission is RetryAdmission.ACCEPT


class TestWorkerRecordProblems:
    """The two worker-record problems are withheld: no evidence, never fabrication."""

    def test_a_run_whose_latest_invocation_failed_is_withheld(self, workspace: Path) -> None:
        first = codex_call("bin/test sympy/combinatorics/tests/test_perm_groups.py", "item_1", 0)
        rerun = codex_call("bin/test sympy/combinatorics/tests/test_perm_groups.py", "item_2", 1)
        verdict = verify(workspace, transcript(first, rerun), tests_passed=(1,), commands_run=(1,))
        assert verdict.failure_class == WITHHELD
        assert verdict.status is VerifierStatus.UNAVAILABLE
        assert verdict.withheld == ("withheld: tests_passed: [1] defeated_by_later_run",)

    def test_a_lint_command_cited_as_a_test_is_withheld(self, workspace: Path) -> None:
        lint = codex_call("git diff --check", "item_1", 0)
        verdict = verify(workspace, transcript(lint), tests_passed=(1,), commands_run=(1,))
        assert verdict.failure_class == WITHHELD
        assert verdict.withheld == ("withheld: tests_passed: [1] not_a_verification_invocation",)

    def test_a_withheld_citation_does_not_demote_a_proven_one(self, workspace: Path) -> None:
        lint = codex_call("git diff --check", "item_1", 0)
        test = codex_call("pytest -q pkg", "item_2", 0)
        verdict = verify(workspace, transcript(lint, test), tests_passed=(1, 2), commands_run=(1,))
        assert verdict.passed
        assert verdict.withheld == ("withheld: tests_passed: [1] not_a_verification_invocation",)


class TestFabrication:
    def test_an_unknown_call_number_is_fabrication(self, workspace: Path) -> None:
        test = codex_call("pytest -q pkg", "item_1", 0)
        verdict = verify(workspace, transcript(test), tests_passed=(7,), commands_run=(1,))
        assert verdict.failure_class == FABRICATION
        assert "tests_passed: [7] unknown_call" in verdict.reasons[0]

    def test_a_failed_call_cited_as_passing_is_fabrication(self, workspace: Path) -> None:
        test = codex_call("pytest -q pkg", "item_1", 1)
        verdict = verify(workspace, transcript(test), tests_passed=(1,), commands_run=(1,))
        assert verdict.failure_class == FABRICATION
        assert "failed_call_cited_as_passing" in verdict.reasons[0]

    def test_an_unknown_commands_run_number_is_fabrication(self, workspace: Path) -> None:
        test = codex_call("pytest -q pkg", "item_1", 0)
        verdict = verify(workspace, transcript(test), tests_passed=(1,), commands_run=(9,))
        assert verdict.failure_class == FABRICATION

    def test_a_failed_call_may_be_cited_as_run(self, workspace: Path) -> None:
        failing = codex_call("pytest -q pkg/other", "item_1", 1)
        test = codex_call("pytest -q pkg", "item_2", 0)
        verdict = verify(workspace, transcript(failing, test), tests_passed=(2,), commands_run=(1,))
        assert verdict.passed


class TestLaterRerun:
    def test_a_later_failing_rerun_defeats_an_earlier_pass(self, workspace: Path) -> None:
        passing = codex_call("pytest -q pkg", "item_1", 0)
        failing = codex_call("pytest -q pkg", "item_2", 2)
        verdict = verify(
            workspace, transcript(passing, failing), tests_passed=(1,), commands_run=(1,)
        )
        assert not verdict.passed
        assert verdict.failure_class == WITHHELD

    def test_a_later_passing_rerun_does_not_defeat(self, workspace: Path) -> None:
        failing = codex_call("pytest -q pkg", "item_1", 1)
        passing = codex_call("pytest -q pkg", "item_2", 0)
        verdict = verify(
            workspace, transcript(failing, passing), tests_passed=(2,), commands_run=(2,)
        )
        assert verdict.passed


class TestClaudeIsErrorMapping:
    """Claude records no exit status; its structured ``is_error`` is the verdict."""

    def test_is_error_false_is_a_pass(self, workspace: Path) -> None:
        call = claude_call("pytest -q pkg", "toolu_1", is_error=False)
        ledger = build_call_ledger(
            transcript(call), task_cwd=str(workspace), is_error_is_exit_verdict=True
        )
        assert ledger[0].result is CallResult.PASSED
        assert ledger[0].source is ResultSource.IS_ERROR
        verdict = verify(
            workspace,
            transcript(call),
            tests_passed=(1,),
            commands_run=(1,),
            is_error_is_exit_verdict=True,
        )
        assert verdict.passed, verdict.reasons

    def test_is_error_true_cited_as_passing_is_fabrication(self, workspace: Path) -> None:
        call = claude_call("pytest -q pkg", "toolu_1", is_error=True, text="Exit code 1")
        verdict = verify(
            workspace,
            transcript(call),
            tests_passed=(1,),
            commands_run=(1,),
            is_error_is_exit_verdict=True,
        )
        assert verdict.failure_class == FABRICATION

    def test_exit_text_is_never_read(self, workspace: Path) -> None:
        call = claude_call("pytest -q pkg", "toolu_1", is_error=True, text="Exit code 0")
        ledger = build_call_ledger(
            transcript(call), task_cwd=str(workspace), is_error_is_exit_verdict=True
        )
        assert ledger[0].result is CallResult.FAILED

    def test_is_error_alone_is_no_verdict_for_an_exit_status_runtime(self, workspace: Path) -> None:
        call = claude_call("pytest -q pkg", "toolu_1", is_error=False)
        verdict = verify(workspace, transcript(call), tests_passed=(1,), commands_run=(1,))
        assert verdict.failure_class == WITHHELD
        assert "result_unknown" in verdict.withheld[0]


class TestRelevanceVeto:
    """The relevance check can withhold a proven citation, never prove one."""

    def test_a_veto_withholds(self, workspace: Path) -> None:
        test = codex_call("pytest -q pkg", "item_1", 0)
        verdict = verify(
            workspace,
            transcript(test),
            tests_passed=(1,),
            commands_run=(1,),
            relevance=(RelevanceDecision(1, relevant=False, status="not_relevant"),),
        )
        assert verdict.failure_class == WITHHELD
        assert verdict.withheld == ("withheld: tests_passed: [1] relevance_veto",)

    @pytest.mark.parametrize(
        ("command", "exit_code", "expected"),
        [
            ("git diff --check", 0, WITHHELD),
            ("pytest -q pkg", 1, FABRICATION),
        ],
    )
    def test_relevant_never_grants(
        self, workspace: Path, command: str, exit_code: int, expected: str
    ) -> None:
        call = codex_call(command, "item_1", exit_code)
        verdict = verify(
            workspace,
            transcript(call),
            tests_passed=(1,),
            commands_run=(1,),
            relevance=(RelevanceDecision(1, relevant=True, status="relevant"),),
        )
        assert verdict.failure_class == expected

    def test_an_undecided_check_changes_nothing(self, workspace: Path) -> None:
        test = codex_call("pytest -q pkg", "item_1", 0)
        verdict = verify(
            workspace,
            transcript(test),
            tests_passed=(1,),
            commands_run=(1,),
            relevance=(RelevanceDecision(1, relevant=None, status="unavailable"),),
        )
        assert verdict.passed


class TestReplay:
    def test_a_failed_replay_on_the_final_artifact_withholds(self, workspace: Path) -> None:
        test = codex_call("pytest -q pkg", "item_1", 0)
        verdict = verify(
            workspace,
            transcript(test),
            tests_passed=(1,),
            commands_run=(1,),
            replays=(ReplayOutcome(number=1, succeeded=False),),
        )
        assert verdict.failure_class == WITHHELD
        assert "replay_failed" in verdict.withheld[0]

    def test_a_successful_replay_keeps_the_pass(self, workspace: Path) -> None:
        test = codex_call("pytest -q pkg", "item_1", 0)
        verdict = verify(
            workspace,
            transcript(test),
            tests_passed=(1,),
            commands_run=(1,),
            replays=(ReplayOutcome(number=1, succeeded=True),),
        )
        assert verdict.passed


class TestNoCitation:
    def test_an_empty_field_is_no_evidence(self, workspace: Path) -> None:
        test = codex_call("pytest -q pkg", "item_1", 0)
        verdict = verify(workspace, transcript(test), tests_passed=(), commands_run=(1,))
        assert verdict.failure_class == WITHHELD
        assert verdict.withheld == ("withheld: tests_passed: no_citation",)

    def test_other_fields_still_use_the_existing_verifier(self, workspace: Path) -> None:
        test = codex_call("pytest -q pkg", "item_1", 0)
        messages = (*transcript(test)[:-2], AgentMessage(type="result", content="done"))
        verdict = verify(workspace, messages, tests_passed=(1,), commands_run=(1,))
        # No observation and no edit: files_touched is unsupported.
        assert not verdict.passed
        assert "files_touched: pkg/mod.py" in verdict.reasons[0]
        assert verdict.decided_by == EVIDENCE_PATH_CITATIONS


class TestLegacyRecords:
    """A record produced without an evidence turn keeps the command-string path."""

    def test_a_backed_string_claim_passes_as_before(self, workspace: Path) -> None:
        test = codex_call("pytest -q pkg", "item_1", 0, output="1 passed in 0.01s")
        verdict = _verify_atomic_evidence_against_runtime_messages(
            messages=transcript(test),
            typed_evidence=EvidenceRecord(
                data={
                    "files_touched": ["pkg/mod.py"],
                    "commands_run": ["pytest -q pkg"],
                    "tests_passed": ["pytest -q pkg"],
                }
            ),
            ac_content=AC,
            execution_profile=load_profile("code"),
            task_cwd=str(workspace),
            adapter_working_directory=str(workspace),
            verify_gate_active=True,
        )
        assert verdict.passed
        assert verdict.decided_by == ""

    def test_an_unbacked_string_claim_is_fabrication_as_before(self, workspace: Path) -> None:
        test = codex_call("pytest -q pkg", "item_1", 0)
        verdict = _verify_atomic_evidence_against_runtime_messages(
            messages=transcript(test),
            typed_evidence=EvidenceRecord(
                data={
                    "files_touched": ["pkg/mod.py"],
                    "commands_run": ["pytest -q other"],
                    "tests_passed": ["pytest -q other"],
                }
            ),
            ac_content=AC,
            execution_profile=load_profile("code"),
            task_cwd=str(workspace),
            adapter_working_directory=str(workspace),
            verify_gate_active=True,
        )
        assert verdict.failure_class == FABRICATION
