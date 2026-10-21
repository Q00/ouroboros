"""Recorded runs of scratch scripts the worker deleted are not fabrication.

Live Django runs (product v0.55.3) were rejected as ``FABRICATION_SUSPECTED``
for ``python repro_issue.py``: the worker created the script, ran it as the
last command of a Bash call that first applied a patch, and deleted it before
the artifact was frozen. The transcript recorded the run with exit 0. Such a
run cannot be replayed, so it yields no evidence
(``SCRIPT_ABSENT_FROM_ARTIFACT``); fabrication stays reserved for claims the
transcript does not support.
"""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.command_replay import select_replay_candidates
from ouroboros.orchestrator.evidence.harness_observation import (
    WorkspaceObservation,
    build_observation_message,
    insert_observation_message,
)
from ouroboros.orchestrator.evidence.observed_runs import (
    SCRIPT_ABSENT_FROM_ARTIFACT,
    observed_zero_exit_run,
)
from ouroboros.orchestrator.evidence.verification import (
    _verify_atomic_evidence_against_runtime_messages,
)
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.failure_taxonomy import FailureClass
from ouroboros.orchestrator.leaf_dispatcher import LeafDispatcher, LeafDispatchState
from ouroboros.orchestrator.profile_loader import load_profile
from ouroboros.orchestrator.verifier import RetryAdmission, VerifierStatus, VerifierVerdict

AC = "Generated migrations import models when a base class needs it"
CLAIM = "python repro_issue.py"
# Shaped like the live Codex call: a patch applied through a here-document,
# then the scratch script run on the next line of the same Bash call.
PATCH_THEN_RUN = (
    "apply_patch <<'PATCH'\n"
    "*** Begin Patch\n"
    "*** Update File: writer.py\n"
    "@@\n"
    "-    imports = set()\n"
    "+    imports = {'from django.db import models'}\n"
    "*** End Patch\n"
    "PATCH\n"
    "python repro_issue.py"
)
DELETE_SCRATCH = (
    "apply_patch <<'PATCH'\n*** Begin Patch\n*** Delete File: repro_issue.py\n"
    "*** End Patch\nPATCH\ngit status --short"
)


def _bash(command: str, call_id: str, exit_code: int) -> tuple[AgentMessage, AgentMessage]:
    """A Codex-shaped Bash call and its correlated result."""
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


def _workspace(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "writer.py").write_text("imports = {'from django.db import models'}\n")
    return root


def _verify(
    workspace: Path,
    transcript: tuple[AgentMessage, ...],
    *,
    claim: str = CLAIM,
    verify_gate_active: bool = True,
) -> VerifierVerdict:
    messages = (
        *transcript,
        build_observation_message(WorkspaceObservation(changed_paths=frozenset({"writer.py"}))),
        AgentMessage(type="result", content="done"),
    )
    return _verify_atomic_evidence_against_runtime_messages(
        messages=messages,
        typed_evidence=EvidenceRecord(
            data={"files_touched": ["writer.py"], "commands_run": [claim], "tests_passed": [claim]}
        ),
        ac_content=AC,
        execution_profile=load_profile("code"),
        task_cwd=str(workspace),
        adapter_working_directory=str(workspace),
        verify_gate_active=verify_gate_active,
    )


class TestDeletedScratchScript:
    """(a) Created, run, deleted: not fabrication; not replayed, with a reason."""

    def test_recorded_run_of_a_deleted_script_has_no_evidence(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        transcript = (
            *_bash(PATCH_THEN_RUN, "item_20", 0),
            *_bash(DELETE_SCRATCH, "item_31", 0),
        )

        verdict = _verify(workspace, transcript)

        assert verdict.passed is False
        assert verdict.failure_class == FailureClass.SCRIPT_ABSENT_FROM_ARTIFACT.value
        assert verdict.status is VerifierStatus.UNAVAILABLE
        assert verdict.retry_admission is RetryAdmission.ACCEPT
        assert verdict.reasons == (
            f"not_replayed: {SCRIPT_ABSENT_FROM_ARTIFACT}: tests_passed: {CLAIM}",
        )

    def test_the_run_is_not_a_replay_candidate(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        transcript = _bash(PATCH_THEN_RUN, "item_20", 0)
        final = json.dumps({"commands_run": [CLAIM], "tests_passed": [CLAIM]})

        candidates = select_replay_candidates(
            final_message=final, messages=transcript, task_cwd=str(workspace)
        )

        assert candidates == ()

    def test_the_latest_run_decides(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        first_failed = (*_bash(PATCH_THEN_RUN, "item_17", 1), *_bash(PATCH_THEN_RUN, "item_20", 0))

        observed = observed_zero_exit_run(CLAIM, first_failed, task_cwd=str(workspace))

        assert observed is not None
        assert observed.script == "repro_issue.py"
        assert observed.script_present is False

    def test_without_the_verify_gate_it_is_a_form_mismatch(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")

        verdict = _verify(workspace, _bash(PATCH_THEN_RUN, "item_20", 0), verify_gate_active=False)

        assert verdict.passed is False
        assert verdict.failure_class == FailureClass.EVIDENCE_FORM_MISMATCH.value
        assert SCRIPT_ABSENT_FROM_ARTIFACT in verdict.reasons[0]

    def test_a_command_run_inside_a_compound_call_backs_commands_run_only(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path / "ws")
        transcript = _bash(DELETE_SCRATCH + " && git diff --check", "item_31", 0)

        observed = observed_zero_exit_run("git diff --check", transcript, task_cwd=str(workspace))
        verdict = _verify(workspace, transcript, claim="git diff --check")

        assert observed is not None
        assert observed.script is None
        # commands_run is backed; the same text as tests_passed runs no
        # script, so it is still an unsupported test claim.
        assert verdict.failure_class == FailureClass.FABRICATION_SUSPECTED.value
        assert verdict.reasons == ("unsupported evidence claims: tests_passed: git diff --check",)


class TestUnsupportedClaimsStayFabrication:
    """(b) No transcript event that ran the claim: still fabrication."""

    @pytest.mark.parametrize(
        ("command", "exit_code"),
        [
            pytest.param("python other.py", 0, id="different-command"),
            pytest.param(PATCH_THEN_RUN, 1, id="recorded-failure"),
            pytest.param(PATCH_THEN_RUN + " || true", 0, id="exit-not-implied"),
            pytest.param("cat <<'X'\npython repro_issue.py", 0, id="unterminated-heredoc"),
            pytest.param("cat <<'X'\npython repro_issue.py\nX", 0, id="heredoc-body-text"),
        ],
    )
    def test_unsupported_claim_is_fabrication(
        self, tmp_path: Path, command: str, exit_code: int
    ) -> None:
        workspace = _workspace(tmp_path / "ws")

        verdict = _verify(workspace, _bash(command, "item_1", exit_code))

        assert verdict.passed is False
        assert verdict.failure_class == FailureClass.FABRICATION_SUSPECTED.value
        assert f"commands_run: {CLAIM}" in verdict.reasons[0]

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("python repro_issue.py | tail -3", id="pipeline-stage"),
            pytest.param("cd sub && python repro_issue.py", id="changes-directory"),
        ],
    )
    def test_runs_whose_exit_is_not_the_claims_are_not_observed(
        self, tmp_path: Path, command: str
    ) -> None:
        workspace = _workspace(tmp_path / "ws")
        transcript = _bash(command, "item_1", 0)

        assert observed_zero_exit_run(CLAIM, transcript, task_cwd=str(workspace)) is None
        verdict = _verify(workspace, transcript)
        assert verdict.failure_class != FailureClass.SCRIPT_ABSENT_FROM_ARTIFACT.value

    def test_lifecycle_completion_without_an_exit_code_is_not_observed(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path / "ws")
        call, _ = _bash(PATCH_THEN_RUN, "item_20", 0)
        completed = AgentMessage(
            type="tool_result",
            content="",
            data={"tool_call_id": "item_20", "status": "completed"},
        )

        observed = observed_zero_exit_run(CLAIM, (call, completed), task_cwd=str(workspace))

        assert observed is None

    def test_later_failed_run_withdraws_an_earlier_pass(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        transcript = (*_bash(PATCH_THEN_RUN, "item_17", 0), *_bash(PATCH_THEN_RUN, "item_20", 1))

        assert observed_zero_exit_run(CLAIM, transcript, task_cwd=str(workspace)) is None


class TestScriptPresent:
    """(c) The script is in the artifact: replayed (or proven) as before."""

    async def test_standalone_run_is_replayed_as_before(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        (workspace / "repro_issue.py").write_text("import sys\nsys.exit(0)\n")
        claim = f"{sys.executable} repro_issue.py"
        transcript = _bash(claim, "item_20", 0)
        evidence = {
            "files_touched": ["writer.py"],
            "commands_run": [claim],
            "tests_passed": [claim],
        }
        final = AgentMessage(type="result", content=json.dumps(evidence))
        messages = [*transcript, final]
        state = LeafDispatchState(
            messages=messages, runtime_handle=None, final_message=final.content, success=True
        )
        executor = SimpleNamespace(_run_verify_commands=True, _verify_command_timeout_seconds=60)
        observation = await LeafDispatcher(executor)._attach_test_reexecution(  # type: ignore[arg-type]
            WorkspaceObservation(changed_paths=frozenset({"writer.py"})),
            state=state,
            task_cwd=str(workspace),
            tools=["Bash"],
        )
        insert_observation_message(messages, observation)

        verdict = _verify_atomic_evidence_against_runtime_messages(
            messages=tuple(messages),
            typed_evidence=EvidenceRecord(data=evidence),
            ac_content=AC,
            execution_profile=load_profile("code"),
            task_cwd=str(workspace),
            adapter_working_directory=str(workspace),
            verify_gate_active=True,
        )

        assert [run.command for run in observation.command_runs] == [claim]
        assert observation.command_runs[0].succeeded
        assert verdict.passed is True, verdict.reasons

    def test_compound_run_of_a_present_script_is_supported(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        (workspace / "repro_issue.py").write_text("print('ok')\n")

        verdict = _verify(workspace, _bash(PATCH_THEN_RUN, "item_20", 0))

        assert verdict.passed is True, verdict.reasons


class TestMixedClaims:
    """A proven tests_passed claim carries the criterion; absent ones are recorded."""

    @staticmethod
    def _verify_claims(workspace: Path, transcript: tuple[AgentMessage, ...], tests: list[str]):
        messages = (
            *transcript,
            build_observation_message(WorkspaceObservation(changed_paths=frozenset({"writer.py"}))),
            AgentMessage(type="result", content="done"),
        )
        return _verify_atomic_evidence_against_runtime_messages(
            messages=messages,
            typed_evidence=EvidenceRecord(
                data={"files_touched": ["writer.py"], "commands_run": tests, "tests_passed": tests}
            ),
            ac_content=AC,
            execution_profile=load_profile("code"),
            task_cwd=str(workspace),
            adapter_working_directory=str(workspace),
            verify_gate_active=True,
        )

    def test_one_proven_and_one_absent_passes_with_a_not_replayed_record(
        self, tmp_path: Path
    ) -> None:
        workspace = _workspace(tmp_path / "ws")
        (workspace / "check.py").write_text("print('ok')\n")
        transcript = (
            *_bash(PATCH_THEN_RUN, "item_20", 0),
            *_bash("python check.py", "item_21", 0),
        )

        verdict = self._verify_claims(workspace, transcript, [CLAIM, "python check.py"])

        assert verdict.passed is True, verdict.reasons
        assert verdict.status is VerifierStatus.PASS
        assert verdict.not_replayed == (f"{SCRIPT_ABSENT_FROM_ARTIFACT}: tests_passed: {CLAIM}",)

    def test_only_absent_claims_take_the_no_evidence_path(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")

        verdict = self._verify_claims(workspace, _bash(PATCH_THEN_RUN, "item_20", 0), [CLAIM])

        assert verdict.passed is False
        assert verdict.failure_class == FailureClass.SCRIPT_ABSENT_FROM_ARTIFACT.value
        assert verdict.status is VerifierStatus.UNAVAILABLE
        assert verdict.not_replayed == (f"{SCRIPT_ABSENT_FROM_ARTIFACT}: tests_passed: {CLAIM}",)

    def test_proven_and_fabricated_is_fabrication(self, tmp_path: Path) -> None:
        workspace = _workspace(tmp_path / "ws")
        (workspace / "check.py").write_text("print('ok')\n")
        transcript = (
            *_bash(PATCH_THEN_RUN, "item_20", 0),
            *_bash("python check.py", "item_21", 0),
        )

        verdict = self._verify_claims(
            workspace, transcript, [CLAIM, "python check.py", "python never_ran.py"]
        )

        assert verdict.passed is False
        assert verdict.failure_class == FailureClass.FABRICATION_SUSPECTED.value
        assert "python never_ran.py" in verdict.reasons[0]
