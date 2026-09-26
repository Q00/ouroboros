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
    assert "hello.exe" in _functional_command_invoked_files(r".\hello.exe")
    assert "hello.exe" in _functional_command_invoked_files(
        r"Start-Process -FilePath .\hello.exe -Wait"
    )
    assert _functional_command_invoked_files("echo fake.exe") == ()
    assert _functional_command_invoked_files("echo -FilePath fake.exe") == ()
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


def test_windows_executable_functional_claim_is_admitted(tmp_path) -> None:
    """A Windows artifact invoked directly is valid functional evidence."""
    artifact = tmp_path / "hello.exe"
    artifact.write_bytes(b"MZ")
    claim = r".\hello.exe"
    start, result = _codex_bash_pair(claim)
    verdict = _verify_atomic_evidence_against_runtime_messages(
        messages=(
            *_edit_pair("hello.exe"),
            start,
            result,
            AgentMessage(type="result", content="done"),
        ),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["hello.exe"],
                "commands_run": [claim],
                "tests_passed": [claim],
            }
        ),
        ac_content="hello.exe prints the required output",
        execution_profile=load_profile("code"),
        task_cwd=str(tmp_path),
        adapter_working_directory=str(tmp_path),
        has_success_contract=True,
        verify_gate_active=True,
    )
    assert verdict.passed is True, verdict.reasons


def test_windows_powershell_wrapper_functional_claim_is_admitted(tmp_path) -> None:
    """The native Codex PowerShell wrapper can prove a produced executable."""
    artifact = tmp_path / "hello.exe"
    artifact.write_bytes(b"MZ")
    claim = r"Start-Process -FilePath .\hello.exe -Wait -PassThru"
    wrapped = (
        '"C:\\Users\\runner\\pwsh.exe" -NoProfile -Command '
        "'Start-Process -FilePath .\\\\hello.exe -Wait -PassThru'"
    )
    start = AgentMessage(
        type="assistant",
        content=f"Calling tool: Bash: {wrapped}",
        tool_name="Bash",
        data={"tool_input": {"command": wrapped}, "tool_call_id": "item-win"},
    )
    result = AgentMessage(
        type="tool_result",
        content="hello.exe",
        data={"tool_call_id": "item-win", "exit_code": 0, "is_error": False},
    )
    verdict = _verify_atomic_evidence_against_runtime_messages(
        messages=(
            *_edit_pair("hello.exe"),
            start,
            result,
            AgentMessage(type="result", content="done"),
        ),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["hello.exe"],
                "commands_run": [claim],
                "tests_passed": [claim],
            }
        ),
        ac_content="hello.exe prints the required output",
        execution_profile=load_profile("code"),
        task_cwd=str(tmp_path),
        adapter_working_directory=str(tmp_path),
        has_success_contract=True,
        verify_gate_active=True,
    )
    assert verdict.passed is True, verdict.reasons


def test_windows_executable_functional_claim_rejects_missing_artifact(tmp_path) -> None:
    """A direct Windows command cannot prove a ghost artifact."""
    claim = r".\missing.exe"
    start, result = _codex_bash_pair(claim)
    verdict = _verify_atomic_evidence_against_runtime_messages(
        messages=(start, result, AgentMessage(type="result", content="done")),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": [],
                "commands_run": [claim],
                "tests_passed": [claim],
            }
        ),
        ac_content="hello.exe prints the required output",
        execution_profile=load_profile("code"),
        task_cwd=str(tmp_path),
        adapter_working_directory=str(tmp_path),
        has_success_contract=True,
        verify_gate_active=True,
    )
    assert verdict.passed is False


def test_windows_option_form_rejects_non_powershell_command(tmp_path) -> None:
    """An option-shaped mention must not certify an artifact that was not run."""
    artifact = tmp_path / "fake.exe"
    artifact.write_bytes(b"MZ")
    claim = "echo -FilePath fake.exe"
    start, result = _codex_bash_pair(claim)
    verdict = _verify_atomic_evidence_against_runtime_messages(
        messages=(
            *_edit_pair("fake.exe"),
            start,
            result,
            AgentMessage(type="result", content="done"),
        ),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["fake.exe"],
                "commands_run": [claim],
                "tests_passed": [claim],
            }
        ),
        ac_content="fake.exe prints the required output",
        execution_profile=load_profile("code"),
        task_cwd=str(tmp_path),
        adapter_working_directory=str(tmp_path),
        has_success_contract=True,
        verify_gate_active=True,
    )
    assert verdict.passed is False


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


def test_preexisting_artifact_execution_is_admitted_with_or_without_witness(tmp_path) -> None:
    """A ``tests_passed`` claim vouches for a behaviour check, not authorship.

    The artifact pre-exists as a real workspace file and this leaf never
    edited it. That is the dependent-AC shape (verify a sibling's artifact);
    the zero-mutation witness used to be the only way through, which rejected
    every such leaf that also edited its own test file.
    """
    (tmp_path / "habit_tracker.py").write_text("print('hi')\n", encoding="utf-8")
    task_cwd = str(tmp_path)
    start, result = _codex_bash_pair(VALIDATION_CLAIM)
    for extra in (
        (_observation_message(),),
        (),
        (_observation_message(changed=("habits.json",)),),
    ):
        assert (
            _functional_command_supports_test_claim(
                value=VALIDATION_CLAIM, messages=(start, result, *extra), task_cwd=task_cwd
            )
            is True
        )


def test_sibling_artifact_check_from_real_rejected_session(tmp_path) -> None:
    """Frozen from bench session exec_be93c8bc71e9/node_V6ALL3NPCODS4 (2026-09-03).

    The leaf edited only ``test_habit_tracker.py``, ran the AC's own check
    against the sibling-built ``habit_tracker.py`` (recorded as a structured
    Bash call, correlated completion exit 0), and cited that command as
    ``tests_passed``. main rejected it as FABRICATION_SUSPECTED because the
    invoked file was not this run's mutation and the test-file edit voided the
    zero-mutation waiver.
    """
    (tmp_path / "habit_tracker.py").write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    claim = "python3 habit_tracker.py unknown-command; test $? -eq 2 && echo EXIT_TWO_OK"
    start, result = _codex_bash_pair(claim)
    messages = (
        *_edit_pair("test_habit_tracker.py"),
        start,
        result,
        AgentMessage(type="result", content="done"),
    )
    verdict = _verify_atomic_evidence_against_runtime_messages(
        messages=messages,
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["test_habit_tracker.py"],
                "commands_run": [claim],
                "tests_passed": [claim],
            }
        ),
        ac_content="An unknown subcommand prints a usage error and exits with status 2",
        execution_profile=load_profile("code"),
        task_cwd=str(tmp_path),
        adapter_working_directory=str(tmp_path),
        has_success_contract=True,
        verify_gate_active=True,
    )
    assert verdict.passed is True, verdict.reasons

    # Adversarial probes: the same shape stays rejected when the claim is not
    # what the transcript shows, when the check failed, or when the artifact
    # is a ghost.
    absent_start, absent_result = _codex_bash_pair("python3 habit_tracker.py list")
    assert (
        _functional_command_supports_test_claim(
            value=claim, messages=(absent_start, absent_result), task_cwd=str(tmp_path)
        )
        is False
    )
    failed_start, failed_result = _codex_bash_pair(claim, exit_code=1)
    assert (
        _functional_command_supports_test_claim(
            value=claim, messages=(failed_start, failed_result), task_cwd=str(tmp_path)
        )
        is False
    )
    (tmp_path / "habit_tracker.py").unlink()
    assert (
        _functional_command_supports_test_claim(
            value=claim, messages=(start, result), task_cwd=str(tmp_path)
        )
        is False
    )


def test_verifier_admits_functional_claim_on_prose_ac() -> None:
    """A prose AC (no success contract) is where functional evidence is the
    only behavioural evidence; the tier no longer requires a contract."""
    start, result = _codex_bash_pair(CLAIM)
    verdict = _verify_atomic_evidence_against_runtime_messages(
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
        has_success_contract=False,
        verify_gate_active=True,
    )
    assert verdict.passed is True, verdict.reasons


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


def test_named_files_touched_on_verification_only_run_is_admitted(tmp_path) -> None:
    """Frozen from bench run orch_777fb6248525 / AC 4 (2026-09-08).

    A sibling AC had already written ``habit_tracker.py`` and
    ``test_habit_tracker.py``; this leaf only ran ``python -m pytest -q``
    (4 passed, exit 0) and listed both files as ``files_touched``. main rejected
    the run's last AC as FABRICATION_SUSPECTED twice and the run ended 3/4.
    The harness snapshot diff witnessed zero mutation, so the claim is a
    mislabelled verification of real workspace files.
    """
    (tmp_path / "habit_tracker.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "test_habit_tracker.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    command = "python -m pytest -q"
    start = AgentMessage(
        type="assistant",
        content="Calling tool: Bash",
        tool_name="Bash",
        data={
            "tool_input": {"command": "/bin/zsh -lc " + shlex.quote(command)},
            "tool_call_id": "item_3",
        },
    )
    result = AgentMessage(
        type="tool_result",
        content="....                                    [100%]\n4 passed in 0.05s",
        data={
            "tool_call_id": "item_3",
            "exit_code": 0,
            "tool_result": {
                "is_error": False,
                "text_content": "....                                    [100%]\n4 passed in 0.05s",
                "meta": {"tool_call_id": "item_3", "exit_status": 0},
            },
        },
    )
    evidence = EvidenceRecord(
        data={
            "files_touched": ["habit_tracker.py", "test_habit_tracker.py"],
            "commands_run": [command],
            "tests_passed": [command],
        }
    )

    def verdict(extra: tuple[AgentMessage, ...], evidence=evidence, contract: bool = True):
        return _verify_atomic_evidence_against_runtime_messages(
            messages=(start, result, *extra, AgentMessage(type="result", content="done")),
            typed_evidence=evidence,
            ac_content="test_habit_tracker.py covers add, list, done and the suite passes",
            execution_profile=load_profile("code"),
            task_cwd=str(tmp_path),
            adapter_working_directory=str(tmp_path),
            has_success_contract=contract,
            verify_gate_active=True,
        )

    assert verdict((_observation_message(),)).passed is True
    # A prose AC has no hidden verify gate to make the mislabel harmless: a
    # stale workspace file must not prove this run touched it.
    prose = verdict((_observation_message(),), contract=False)
    assert prose.passed is False
    assert any("files_touched" in reason for reason in prose.reasons)
    # No witness, a mutated witness, or a truncated witness: still rejected.
    for extra in (
        (),
        (_observation_message(changed=("habits.json",)),),
        (_observation_message(truncated=True),),
    ):
        v = verdict(extra)
        assert v.passed is False
        assert any("files_touched" in reason for reason in v.reasons)
    # A ghost path stays rejected even with a clean witness.
    ghost = EvidenceRecord(
        data={"files_touched": ["ghost.py"], "commands_run": [command], "tests_passed": [command]}
    )
    v = verdict((_observation_message(),), evidence=ghost)
    assert v.passed is False
    assert any("ghost.py" in reason for reason in v.reasons)
