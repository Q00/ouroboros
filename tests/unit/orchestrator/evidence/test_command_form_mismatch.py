"""A completed command is related work, not necessarily proof of passing tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.verification import (
    _verify_atomic_evidence_against_runtime_messages,
)
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.failure_taxonomy import FailureClass, RecoveryAction, policy_for
from ouroboros.orchestrator.profile_loader import EvidenceSchema, load_profile
from ouroboros.orchestrator.verifier import VerifierVerdict

TAP_OUTPUT = """TAP version 13
# Subtest: addition
ok 1 - addition
1..1
# tests 1
# suites 0
# pass 1
# fail 0
# cancelled 0
# skipped 0
# todo 0
"""


def _pair(command: str, output: str = TAP_OUTPUT) -> tuple[AgentMessage, AgentMessage]:
    return (
        AgentMessage(
            type="tool",
            content="Command started",
            tool_name="Bash",
            data={"tool_call_id": "run", "tool_input": {"command": command}},
        ),
        AgentMessage(
            type="tool_result",
            content=output,
            data={"tool_call_id": "run", "exit_code": 0, "is_error": False},
        ),
    )


def _verify(
    messages: tuple[AgentMessage, ...],
    claim: str,
    *,
    extra_claim: str | None = None,
) -> VerifierVerdict:
    return _verify_atomic_evidence_against_runtime_messages(
        messages=(*messages, AgentMessage(type="result", content="done")),
        typed_evidence=EvidenceRecord(
            data={"tests_passed": [claim, *([extra_claim] if extra_claim else [])]}
        ),
        ac_content="Verify the implementation",
        execution_profile=load_profile("code").model_copy(
            update={"evidence_schema": EvidenceSchema(required=("tests_passed",))}
        ),
        task_cwd=None,
        adapter_working_directory=None,
    )


@pytest.mark.parametrize(
    "command,output",
    [
        ("npm test", TAP_OUTPUT),
        ("npm.cmd test", TAP_OUTPUT),
        ("npm run verify:ac1", TAP_OUTPUT),
        ("node --test checks.test.cjs", TAP_OUTPUT),
        ("custom-check --all", "Checks completed."),
        ("pytest -q", "expected failure case PASSED\n1 passed"),
        ("npm run build", "Build complete."),
        ("npm test", "No tests found"),
        ("npm test", "0 passed"),
    ],
)
def test_completed_command_with_unproven_test_result_is_mismatch(command, output) -> None:
    verdict = _verify(_pair(command, output), command)

    assert verdict.passed is False
    assert verdict.failure_class == FailureClass.EVIDENCE_FORM_MISMATCH.value
    reason = " ".join(verdict.reasons)
    assert "cannot prove tests_passed" in reason
    assert "retry with contract-compliant" in reason
    assert "unprotected output-filter pipeline" not in reason
    assert policy_for(FailureClass(verdict.failure_class)).action is RecoveryAction.RETRY


@pytest.mark.parametrize("exit_code", [1, -1, "0", False, None])
def test_failed_or_malformed_completion_is_not_reclassified(exit_code) -> None:
    start, result = _pair("npm test")
    result.data["exit_code"] = exit_code

    verdict = _verify((start, result), "npm test")

    assert verdict.passed is False
    assert verdict.failure_class == FailureClass.FABRICATION_SUSPECTED.value


def test_missing_completion_is_not_reclassified() -> None:
    start, _ = _pair("npm test")
    verdict = _verify((start,), "npm test")
    assert verdict.passed is False
    assert verdict.failure_class == FailureClass.FABRICATION_SUSPECTED.value


def test_unrelated_completion_is_not_reclassified() -> None:
    start, result = _pair("npm test")
    result.data["tool_call_id"] = "different-run"
    verdict = _verify((start, result), "npm test")
    assert verdict.passed is False
    assert verdict.failure_class == FailureClass.FABRICATION_SUSPECTED.value


def test_unrelated_or_additional_unbacked_claim_keeps_fabrication_precedence() -> None:
    for claim, extra in [("npm run invented", None), ("npm test", "invented test")]:
        verdict = _verify(_pair("npm test"), claim, extra_claim=extra)
        assert verdict.passed is False
        assert verdict.failure_class == FailureClass.FABRICATION_SUSPECTED.value


def test_existing_supported_test_proof_still_passes() -> None:
    verdict = _verify(_pair("npm test", "1 passed"), "npm test")
    assert verdict.passed is True


@pytest.mark.parametrize(
    "contradiction",
    [
        {"is_error": True},
        {"is_error": "false"},
        {"is_error_invalid": True},
        {"status": "failed"},
        {"status": 42},
        {"runtime_event_type": "tool.failed"},
        {"tool_result": "unstructured"},
        {"tool_result": {"is_error": True}},
        {"tool_result": {"is_error": "false"}},
        {"tool_result": {"is_error_invalid": True}},
        {"tool_result": {"status": "failed"}},
        {"tool_result": {"status": " ERROR "}},
        {"tool_result": {"runtime_event_type": "tool.failed"}},
        {"tool_result": {"runtime_event_type": " TOOL.ERROR "}},
        {"tool_result": {"exit_code": 1}},
        {"tool_result": {"exit_code": False}},
        {"tool_result": {"exit_code": "0"}},
        {"tool_result": {"meta": {"exit_status": 1}}},
        {"tool_result": {"meta": {"exit_status": False}}},
        {"call_id": "conflicting-run"},
        {"tool_result": {"meta": {"tool_call_id": "conflicting-run"}}},
    ],
)
def test_contradictory_completion_is_not_reclassified(contradiction) -> None:
    start, result = _pair("npm test")
    result.data.update(contradiction)
    verdict = _verify((start, result), "npm test")
    assert verdict.passed is False
    assert verdict.failure_class == FailureClass.FABRICATION_SUSPECTED.value


@pytest.mark.parametrize("duplicate", ["start", "result", "result-with-inline-success"])
def test_duplicate_correlation_is_not_reclassified(duplicate) -> None:
    start, result = _pair("npm test")
    second_start, second_result = _pair("npm test")
    if duplicate == "start":
        messages = (start, second_start, result)
    else:
        if duplicate == "result-with-inline-success":
            start.data["exit_code"] = 0
        messages = (start, result, second_result)
    verdict = _verify(messages, "npm test")
    assert verdict.passed is False
    assert verdict.failure_class == FailureClass.FABRICATION_SUSPECTED.value


def test_completion_before_start_cannot_hide_a_reused_id() -> None:
    start, result = _pair("npm test")
    _, earlier_result = _pair("npm test")
    earlier_result.data["exit_code"] = 1
    verdict = _verify((earlier_result, start, result), "npm test")
    assert verdict.passed is False
    assert verdict.failure_class == FailureClass.FABRICATION_SUSPECTED.value


def test_invalid_error_marker_on_start_is_not_reclassified() -> None:
    start, result = _pair("npm test")
    start.data["is_error_invalid"] = True
    verdict = _verify((start, result), "npm test")
    assert verdict.passed is False
    assert verdict.failure_class == FailureClass.FABRICATION_SUSPECTED.value


@pytest.mark.parametrize(
    "missing_proof",
    ["no-exit", "no-ids", "wrong-tool", "inline-only", "conflicting-start", "start-failed"],
)
def test_incomplete_or_conflicting_execution_is_not_reclassified(missing_proof) -> None:
    start, result = _pair("npm test")
    messages = (start, result)
    if missing_proof == "no-exit":
        result.data.pop("exit_code")
    elif missing_proof == "no-ids":
        start.data.pop("tool_call_id")
        result.data.pop("tool_call_id")
    elif missing_proof == "wrong-tool":
        messages = (start, replace(result, tool_name="Read"))
    elif missing_proof == "inline-only":
        start.data["exit_code"] = 0
        messages = (start,)
    elif missing_proof == "conflicting-start":
        start.data["call_id"] = "other-run"
    else:
        start.data["exit_code"] = 1
    verdict = _verify(messages, "npm test")
    assert verdict.passed is False
    assert verdict.failure_class == FailureClass.FABRICATION_SUSPECTED.value


def test_narration_and_terminal_self_report_cannot_supply_completion() -> None:
    start, _ = _pair("npm test")
    narration = AgentMessage(type="assistant", content=TAP_OUTPUT, data={"exit_code": 0})
    verdict = _verify((start, narration), "npm test")
    assert verdict.passed is False
    assert verdict.failure_class == FailureClass.FABRICATION_SUSPECTED.value
    # The terminal result must be removed before looking for correlated completion.
    terminal = AgentMessage(
        type="result",
        content=TAP_OUTPUT,
        data={"tool_call_id": "run", "exit_code": 0, "tool_result": {"is_error": False}},
    )
    verdict = _verify_atomic_evidence_against_runtime_messages(
        messages=(start, terminal),
        typed_evidence=EvidenceRecord(data={"tests_passed": ["npm test"]}),
        ac_content="Verify implementation",
        execution_profile=load_profile("code").model_copy(
            update={"evidence_schema": EvidenceSchema(required=("tests_passed",))}
        ),
        task_cwd=None,
        adapter_working_directory=None,
    )
    assert verdict.passed is False
    assert verdict.failure_class == FailureClass.FABRICATION_SUSPECTED.value


@pytest.mark.parametrize("claim", ["checks.test.cjs", "addition", "npm test: 99 passed"])
def test_command_completion_cannot_reclassify_names_or_invented_summaries(claim) -> None:
    verdict = _verify(_pair("npm test"), claim)
    assert verdict.passed is False
    assert verdict.failure_class == FailureClass.FABRICATION_SUSPECTED.value


def test_default_code_profile_keeps_unbacked_file_claim_precedence() -> None:
    verdict = _verify_atomic_evidence_against_runtime_messages(
        messages=(*_pair("npm test"), AgentMessage(type="result", content="done")),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["invented.cjs"],
                "commands_run": ["npm test"],
                "tests_passed": ["npm test"],
            }
        ),
        ac_content="Implement and test addition",
        execution_profile=load_profile("code"),
        task_cwd=None,
        adapter_working_directory=None,
    )
    assert verdict.passed is False
    assert verdict.failure_class == FailureClass.FABRICATION_SUSPECTED.value


@pytest.mark.parametrize(
    "wrapped",
    [
        '/bin/sh -lc "npm test"',
        'powershell.exe -NoProfile -Command "npm test"',
    ],
)
def test_existing_command_wrapper_equivalence_is_reused(wrapped) -> None:
    verdict = _verify(_pair(wrapped), "npm test")
    assert verdict.passed is False
    assert verdict.failure_class == FailureClass.EVIDENCE_FORM_MISMATCH.value


def test_mixed_masked_and_completed_commands_keep_both_diagnostics() -> None:
    start, result = _pair("pytest -q | tail -100", "1 passed")
    start.data["tool_call_id"] = "filtered"
    result.data["tool_call_id"] = "filtered"
    verdict = _verify((*_pair("npm test"), start, result), "npm test", extra_claim="pytest -q")
    assert verdict.passed is False
    assert verdict.failure_class == FailureClass.EVIDENCE_FORM_MISMATCH.value
    reason = " ".join(verdict.reasons)
    assert "unprotected output-filter pipeline" in reason
    assert "recorded command completion cannot prove tests_passed" in reason
