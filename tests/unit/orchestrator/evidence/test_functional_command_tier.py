"""Functional-verification tier for ``tests_passed`` claims.

A leaf that verifies behavior by executing the built artifact directly
(``python3 tool.py add x``) and cites that command as ``tests_passed`` did
honest, transcript-provable work. With a hidden verify gate as the behavioral
authority, that claim must settle through transcript + structured success
evidence instead of being rejected as FABRICATION_SUSPECTED (observed on every
Codex cli-todo run). The tier stays fail-closed: no transcript match, no
structured success, no invoked artifact backed by this run, or no active
verify gate each keep the current rejection.
"""

from __future__ import annotations

import shlex

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.test_detection import (
    _functional_command_invoked_files,
    _functional_command_supports_test_claim,
)
from ouroboros.orchestrator.evidence.verification import (
    _verify_atomic_evidence_against_runtime_messages,
)
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.profile_loader import load_profile

CLAIM = (
    't=$(mktemp -d) && cp habit_tracker.py "$t"/ && cd "$t" && '
    "python3 habit_tracker.py add 'drink water' && python3 habit_tracker.py list"
)


def _codex_bash_pair(command: str, *, exit_code: int = 0) -> tuple[AgentMessage, AgentMessage]:
    """Codex-shaped Bash start/result pair: zsh -lc wrapped, correlated by id."""
    wrapped = "/bin/zsh -lc " + shlex.quote(command)
    start = AgentMessage(
        type="assistant",
        content=f"Calling tool: Bash: {wrapped}",
        tool_name="Bash",
        data={"tool_input": {"command": wrapped}, "tool_call_id": "item_7"},
    )
    result = AgentMessage(
        type="tool_result",
        content="drink water",
        data={
            "tool_call_id": "item_7",
            "exit_code": exit_code,
            "tool_result": {
                "is_error": exit_code != 0,
                "text_content": "drink water",
                "meta": {"tool_call_id": "item_7", "exit_status": exit_code},
            },
        },
    )
    return start, result


def _edit_pair(path: str) -> tuple[AgentMessage, AgentMessage]:
    """Codex-shaped file_change start/result pair (Edit with success result)."""
    call_id = f"edit:{path}"
    start = AgentMessage(
        type="assistant",
        content=f"Calling tool: Edit: {path}",
        tool_name="Edit",
        data={"tool_input": {"file_path": path}, "tool_call_id": call_id},
    )
    result = AgentMessage(
        type="tool_result",
        content="",
        data={
            "tool_call_id": call_id,
            "tool_result": {"is_error": False, "meta": {"tool_call_id": call_id}},
        },
    )
    return start, result


def test_invoked_files_require_an_interpreter_and_a_file_token() -> None:
    assert "habit_tracker.py" in _functional_command_invoked_files(CLAIM)
    # Commands with no interpreter or no file token never enter the tier.
    assert _functional_command_invoked_files("cp habit_tracker.py /tmp/") == ()
    assert _functional_command_invoked_files("echo ok") == ()
    assert _functional_command_invoked_files("./run.sh") == ("run.sh",)
    # Heredoc drivers reference the artifact inside their body.
    heredoc = (
        "python3 - <<'PY'\nimport subprocess, sys\n"
        "subprocess.run([sys.executable, 'habit_tracker.py', 'add', 'x'], check=True)\nPY"
    )
    assert "habit_tracker.py" in _functional_command_invoked_files(heredoc)


def test_test_runner_claims_stay_outside_the_functional_tier() -> None:
    start, result = _codex_bash_pair("python3 -m pytest test_app.py")
    messages = (*_edit_pair("test_app.py"), start, result)
    assert (
        _functional_command_supports_test_claim(
            value="python3 -m pytest test_app.py", messages=messages, task_cwd=None
        )
        is False
    )


def test_codex_wrapped_functional_command_supports_claim() -> None:
    start, result = _codex_bash_pair(CLAIM)
    messages = (*_edit_pair("habit_tracker.py"), start, result)
    assert (
        _functional_command_supports_test_claim(value=CLAIM, messages=messages, task_cwd=None)
        is True
    )


def test_failed_execution_does_not_support_claim() -> None:
    start, result = _codex_bash_pair(CLAIM, exit_code=1)
    messages = (*_edit_pair("habit_tracker.py"), start, result)
    assert (
        _functional_command_supports_test_claim(value=CLAIM, messages=messages, task_cwd=None)
        is False
    )


def test_unbacked_artifact_does_not_support_claim() -> None:
    # No Edit/Write evidence for habit_tracker.py: a stale artifact in the
    # workspace must not be claimable.
    messages = _codex_bash_pair(CLAIM)
    assert (
        _functional_command_supports_test_claim(value=CLAIM, messages=messages, task_cwd=None)
        is False
    )


def test_claim_without_transcript_execution_does_not_support() -> None:
    start, result = _codex_bash_pair("python3 habit_tracker.py list")
    messages = (*_edit_pair("habit_tracker.py"), start, result)
    assert (
        _functional_command_supports_test_claim(value=CLAIM, messages=messages, task_cwd=None)
        is False
    )


def _verdict(*, verify_gate_active: bool):
    start, result = _codex_bash_pair(CLAIM)
    return _verify_atomic_evidence_against_runtime_messages(
        messages=(
            *_edit_pair("habit_tracker.py"),
            start,
            result,
            AgentMessage(type="result", content="done"),
        ),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["habit_tracker.py"],
                "commands_run": [CLAIM],
                "tests_passed": [CLAIM],
            }
        ),
        ac_content="habit_tracker.py supports `add <name>` and `list`",
        execution_profile=load_profile("code"),
        task_cwd=None,
        adapter_working_directory=None,
        has_success_contract=verify_gate_active,
        verify_gate_active=verify_gate_active,
    )


def test_verifier_settles_functional_claim_only_under_verify_gate_authority() -> None:
    assert _verdict(verify_gate_active=True).passed is True
    gated_off = _verdict(verify_gate_active=False)
    assert gated_off.passed is False
    assert any("tests_passed" in reason for reason in gated_off.reasons)


def _observation_message(
    *,
    changed: tuple[str, ...] = (),
    deleted: tuple[str, ...] = (),
    truncated: bool = False,
) -> AgentMessage:
    from ouroboros.orchestrator.evidence.harness_observation import (
        WorkspaceObservation,
        build_observation_message,
    )

    return build_observation_message(
        WorkspaceObservation(
            changed_paths=frozenset(changed),
            deleted_paths=frozenset(deleted),
            truncated=truncated,
        )
    )


VALIDATION_CLAIM = (
    't=$(mktemp -d) && cp habit_tracker.py "$t"/ && cd "$t" && '
    "python3 habit_tracker.py unknown-command; test $? -eq 2 && echo EXIT_TWO_OK"
)


def test_zero_mutation_witness_admits_preexisting_artifact_execution(tmp_path) -> None:
    (tmp_path / "habit_tracker.py").write_text("print('hi')\n", encoding="utf-8")
    task_cwd = str(tmp_path)
    start, result = _codex_bash_pair(VALIDATION_CLAIM)
    # No Edit evidence: the artifact pre-exists as a real workspace file. The
    # harness witnessed zero workspace mutation, so this is pure verification.
    messages = (start, result, _observation_message())
    assert (
        _functional_command_supports_test_claim(
            value=VALIDATION_CLAIM, messages=messages, task_cwd=task_cwd
        )
        is True
    )
    # A mutated, deleting, or truncated observation withdraws the waiver.
    for witness in (
        _observation_message(changed=("habits.json",)),
        _observation_message(deleted=("removed.py",)),
        _observation_message(truncated=True),
    ):
        assert (
            _functional_command_supports_test_claim(
                value=VALIDATION_CLAIM, messages=(start, result, witness), task_cwd=task_cwd
            )
            is False
        )
    # And with no observation at all, the stale-artifact guard still holds.
    assert (
        _functional_command_supports_test_claim(
            value=VALIDATION_CLAIM, messages=(start, result), task_cwd=task_cwd
        )
        is False
    )


def test_zero_mutation_waiver_requires_the_cited_file_to_exist(tmp_path) -> None:
    """A ghost path mentioned in the command must not settle through the waiver."""
    ghost_claim = "python3 ghost.py check  # verifies ghost.py behavior"
    start, result = _codex_bash_pair(ghost_claim)
    messages = (start, result, _observation_message())
    # ghost.py does not exist in the workspace, and with no workspace at all
    # existence cannot be proven either.
    for cwd in (str(tmp_path), None):
        assert (
            _functional_command_supports_test_claim(
                value=ghost_claim, messages=messages, task_cwd=cwd
            )
            is False
        )


def test_functional_tier_requires_an_authoritative_zero_exit(tmp_path) -> None:
    """Lifecycle-only completion (status=completed, no exit code) is not success."""
    (tmp_path / "habit_tracker.py").write_text("print('hi')\n", encoding="utf-8")
    import shlex as _shlex

    wrapped = "/bin/zsh -lc " + _shlex.quote(VALIDATION_CLAIM)
    start = AgentMessage(
        type="assistant",
        content=f"Calling tool: Bash: {wrapped}",
        tool_name="Bash",
        data={"tool_input": {"command": wrapped}, "tool_call_id": "item_9"},
    )
    lifecycle_only = AgentMessage(
        type="tool_result",
        content="Traceback: command failed",
        data={
            "tool_call_id": "item_9",
            "status": "completed",
            "tool_result": {"text_content": "Traceback: command failed"},
        },
    )
    assert (
        _functional_command_supports_test_claim(
            value=VALIDATION_CLAIM,
            messages=(start, lifecycle_only, _observation_message()),
            task_cwd=str(tmp_path),
        )
        is False
    )


def test_verifier_accepts_empty_files_touched_only_with_zero_mutation_witness(tmp_path) -> None:
    (tmp_path / "habit_tracker.py").write_text("print('hi')\n", encoding="utf-8")
    start, result = _codex_bash_pair(VALIDATION_CLAIM)

    def verdict(extra: tuple[AgentMessage, ...]):
        return _verify_atomic_evidence_against_runtime_messages(
            messages=(start, result, *extra, AgentMessage(type="result", content="done")),
            typed_evidence=EvidenceRecord(
                data={
                    "files_touched": [],
                    "commands_run": [VALIDATION_CLAIM],
                    "tests_passed": [VALIDATION_CLAIM],
                }
            ),
            ac_content="An unknown subcommand prints a usage error and exits with status 2",
            execution_profile=load_profile("code"),
            task_cwd=str(tmp_path),
            adapter_working_directory=str(tmp_path),
            has_success_contract=True,
            verify_gate_active=True,
        )

    assert verdict((_observation_message(),)).passed is True
    without_witness = verdict(())
    assert without_witness.passed is False
    assert any("files_touched" in reason for reason in without_witness.reasons)
