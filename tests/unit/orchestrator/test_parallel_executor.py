"""Tests for staged result handling in ParallelACExecutor."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.types import MCPToolDefinition
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.coordinator import CoordinatorReview, FileConflict
from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph
from ouroboros.orchestrator.evidence_schema import EvidenceRecord, ValidationResult
from ouroboros.orchestrator.execution_runtime_scope import ExecutionNodeIdentity
from ouroboros.orchestrator.level_context import ACContextSummary, LevelContext
from ouroboros.orchestrator.parallel_executor import (
    MAX_STALL_RETRIES,
    STALL_TIMEOUT_SECONDS,
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelACExecutor,
    ParallelExecutionResult,
    StageExecutionOutcome,
    _build_governed_parent_summary,
    _complete_sibling_acs_from_evidence,
    _criterion_satisfied_by_evidence,
    _effective_evidence_schema_for_ac,
    _message_contains_test_success,
    _runtime_messages_support_command_claim,
    render_parallel_completion_message,
    render_parallel_verification_report,
)
from ouroboros.orchestrator.profile_loader import EvidenceSchema, load_profile
from ouroboros.orchestrator.verifier import VerifierVerdict


def _make_seed(*acceptance_criteria: str) -> Seed:
    """Build a minimal seed for parallel executor tests."""
    return Seed(
        goal="Implement staged AC execution",
        constraints=(),
        acceptance_criteria=acceptance_criteria,
        ontology_schema=OntologySchema(
            name="ParallelExecution",
            description="Test schema",
        ),
        metadata=SeedMetadata(ambiguity_score=0.05),
    )


def _make_executor() -> ParallelACExecutor:
    """Create an executor with mocked dependencies and muted event emitters."""
    executor = ParallelACExecutor(
        adapter=MagicMock(),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
    )
    executor._coordinator.detect_file_conflicts = MagicMock(return_value=[])
    executor._emit_workflow_progress = AsyncMock()
    executor._emit_level_started = AsyncMock()
    executor._emit_level_completed = AsyncMock()
    executor._emit_subtask_event = AsyncMock()
    return executor


def test_criterion_satisfied_by_exact_runtime_evidence() -> None:
    files = {"hello_auto.py", "tests/test_hello_auto.py"}
    commands = {"uv run pytest tests/test_hello_auto.py"}

    assert _criterion_satisfied_by_evidence("`hello_auto.py` exists.", files, commands)
    assert _criterion_satisfied_by_evidence("`tests/test_hello_auto.py` exists.", files, commands)
    assert not _criterion_satisfied_by_evidence("`src/hello_auto.py` exists.", files, commands)
    assert not _criterion_satisfied_by_evidence("`Hello_Auto.py` exists.", files, commands)
    assert not _criterion_satisfied_by_evidence(
        "`tests/test_hello_auto.py` exists and imports `hello_auto`.",
        files,
        commands,
    )
    assert not _criterion_satisfied_by_evidence(
        "`hello_auto.py` is created with exact content.",
        files,
        commands,
    )
    assert not _criterion_satisfied_by_evidence(
        "`tests/test_hello_auto.py` imports `hello_auto` and asserts the exact return value.",
        files,
        commands,
    )
    assert _criterion_satisfied_by_evidence(
        "The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        files,
        commands,
        commands,
    )
    assert _criterion_satisfied_by_evidence(
        "Run the exact command `uv run pytest tests/test_hello_auto.py`.",
        files,
        commands,
    )
    assert _criterion_satisfied_by_evidence(
        "Run `uv run pytest tests/test_hello_auto.py`.",
        files,
        commands,
    )
    assert _criterion_satisfied_by_evidence(
        "Execute `uv run pytest tests/test_hello_auto.py`.",
        files,
        commands,
    )
    assert _criterion_satisfied_by_evidence(
        "The exact command `uv run pytest tests/test_hello_auto.py` exits with code 0.",
        files,
        commands,
        commands,
    )
    assert not _criterion_satisfied_by_evidence(
        "The exact command `uv run pytest tests/test_hello_auto.py` passes and covers edge cases.",
        files,
        commands,
        commands,
    )
    assert not _criterion_satisfied_by_evidence(
        "The exact command `uv run pytest Tests/test_hello_auto.py` passes.",
        files,
        commands,
        commands,
    )
    assert not _criterion_satisfied_by_evidence(
        "Run the exact command `uv run pytest tests/test_hello_auto.py` and inspect output.",
        files,
        commands,
    )
    assert not _criterion_satisfied_by_evidence(
        "The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        files,
        commands,
        set(),
    )
    assert not _criterion_satisfied_by_evidence("`other.py` exists.", files, commands)


def test_complete_sibling_acs_from_successful_runtime_evidence(tmp_path: Any) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_hello_auto.py").write_text("def test_hello(): pass\n")
    success = ACExecutionResult(
        ac_index=0,
        ac_content="`hello_auto.py` defines `hello_auto() -> str` returning exactly `hello from ooo auto`.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="write hello_auto",
                tool_name="Write",
                data={"tool_input": {"file_path": "hello_auto.py"}},
            ),
            AgentMessage(
                type="tool_use",
                content="write test",
                tool_name="Write",
                data={"tool_input": {"file_path": "tests/test_hello_auto.py"}},
            ),
            AgentMessage(
                type="tool_use",
                content="run pytest",
                tool_name="Bash",
                data={"tool_input": {"command": "uv run pytest tests/test_hello_auto.py"}},
            ),
        ),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["hello_auto.py", "tests/test_hello_auto.py"],
                "commands_run": ["uv run pytest tests/test_hello_auto.py"],
                "tests_passed": ["uv run pytest tests/test_hello_auto.py"],
            }
        ),
        runtime_handle=RuntimeHandle(backend="codex_cli", cwd=str(tmp_path)),
    )
    failed_test_file = ACExecutionResult(
        ac_index=1,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )
    failed_pytest = ACExecutionResult(
        ac_index=2,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )
    failed_indices = {1, 2}
    ac_statuses = {0: "completed", 1: "failed", 2: "failed"}

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success, failed_test_file, failed_pytest],
        ac_statuses=ac_statuses,
        failed_indices=failed_indices,
        completed_count=1,
        level_success=1,
        level_failed=2,
    )

    assert completed_count == 3
    assert level_success == 3
    assert level_failed == 0
    assert failed_indices == set()
    assert ac_statuses == {0: "completed", 1: "completed", 2: "completed"}
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.SATISFIED_EXTERNALLY,
        ACExecutionOutcome.SATISFIED_EXTERNALLY,
    ]


def test_complete_sibling_acs_requires_runtime_test_success_for_pass_claim() -> None:
    success_without_test_output = ACExecutionResult(
        ac_index=0,
        ac_content="`hello_auto.py` defines `hello_auto() -> str`.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="run pytest",
                tool_name="Bash",
                data={
                    "tool_input": {
                        "command": "/bin/zsh -lc 'uv run pytest tests/test_hello_auto.py'"
                    }
                },
            ),
            AgentMessage(
                type="result",
                content="success",
                data={"subtype": "success"},
            ),
        ),
    )
    failed_pytest = ACExecutionResult(
        ac_index=1,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_without_test_output, failed_pytest],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def test_complete_sibling_acs_does_not_use_later_tool_success_as_test_proof() -> None:
    success_with_unrelated_tool_success = ACExecutionResult(
        ac_index=0,
        ac_content="Run pytest and edit a file.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="run pytest",
                tool_name="Bash",
                data={"tool_input": {"command": "uv run pytest tests/test_hello_auto.py"}},
            ),
            AgentMessage(
                type="tool_use",
                content="write file",
                tool_name="Write",
                data={"tool_input": {"file_path": "hello_auto.py"}},
            ),
            AgentMessage(
                type="tool_result",
                content="success",
                data={"subtype": "tool_result", "stdout": "success"},
            ),
        ),
    )
    failed_pytest = ACExecutionResult(
        ac_index=1,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_unrelated_tool_success, failed_pytest],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def test_complete_sibling_acs_keeps_exact_command_case_sensitive() -> None:
    success = ACExecutionResult(
        ac_index=0,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=True,
        typed_evidence=EvidenceRecord(
            data={"tests_passed": ["uv run pytest tests/test_hello_auto.py"]}
        ),
    )
    failed_wrong_case_pytest = ACExecutionResult(
        ac_index=1,
        ac_content="The exact command `uv run pytest Tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success, failed_wrong_case_pytest],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def test_complete_sibling_acs_normalizes_absolute_typed_file_evidence(tmp_path: Any) -> None:
    test_file = tmp_path / "tests" / "test_hello_auto.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_hello(): pass\n")
    success = ACExecutionResult(
        ac_index=0,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=True,
        typed_evidence=EvidenceRecord(data={"files_touched": [str(test_file)]}),
        runtime_handle=RuntimeHandle(backend="codex_cli", cwd=str(tmp_path)),
    )
    failed_file_presence = ACExecutionResult(
        ac_index=1,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success, failed_file_presence],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 2
    assert level_success == 2
    assert level_failed == 0
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.SATISFIED_EXTERNALLY,
    ]


def test_complete_sibling_acs_requires_file_end_state_existence(tmp_path: Any) -> None:
    success_without_current_file = ACExecutionResult(
        ac_index=0,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=True,
        typed_evidence=EvidenceRecord(data={"files_touched": ["tests/test_hello_auto.py"]}),
        runtime_handle=RuntimeHandle(backend="codex_cli", cwd=str(tmp_path)),
    )
    failed_file_presence = ACExecutionResult(
        ac_index=1,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_without_current_file, failed_file_presence],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def test_complete_sibling_acs_rejects_invalid_typed_evidence() -> None:
    success_with_invalid_typed_evidence = ACExecutionResult(
        ac_index=0,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=True,
        typed_evidence=EvidenceRecord(data={"files_touched": ["tests/test_hello_auto.py"]}),
        typed_evidence_validation=ValidationResult(ok=False, missing_fields=("tests_passed",)),
    )
    failed_file_presence = ACExecutionResult(
        ac_index=1,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_invalid_typed_evidence, failed_file_presence],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def test_complete_sibling_acs_rejects_verifier_failed_typed_evidence() -> None:
    success_with_rejected_typed_evidence = ACExecutionResult(
        ac_index=0,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=True,
        typed_evidence=EvidenceRecord(
            data={"tests_passed": ["uv run pytest tests/test_hello_auto.py"]}
        ),
        atomic_verifier_verdict=VerifierVerdict(
            passed=False,
            reasons=("fabricated test output",),
            failure_class="FABRICATION_SUSPECTED",
        ),
    )
    failed_pytest = ACExecutionResult(
        ac_index=1,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_rejected_typed_evidence, failed_pytest],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def test_complete_sibling_acs_accepts_shell_wrapped_successful_test_command() -> None:
    success_with_test_output = ACExecutionResult(
        ac_index=0,
        ac_content="`hello_auto.py` defines `hello_auto() -> str`.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="run pytest",
                tool_name="Bash",
                data={
                    "tool_input": {
                        "command": "/bin/zsh -lc 'uv run pytest tests/test_hello_auto.py'"
                    }
                },
            ),
            AgentMessage(
                type="tool_result",
                content="1 passed in 0.01s",
                data={"stdout": "1 passed in 0.01s"},
            ),
        ),
    )
    failed_pytest = ACExecutionResult(
        ac_index=1,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_test_output, failed_pytest],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 2
    assert level_success == 2
    assert level_failed == 0
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.SATISFIED_EXTERNALLY,
    ]


def test_complete_sibling_acs_accepts_named_tool_result_test_output() -> None:
    success_with_named_tool_result = ACExecutionResult(
        ac_index=0,
        ac_content="Run the requested test command.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="run pytest",
                tool_name="Bash",
                data={"tool_input": {"command": "uv run pytest tests/test_hello_auto.py"}},
            ),
            AgentMessage(
                type="tool",
                content="1 passed in 0.01s",
                tool_name="Bash",
                data={"subtype": "tool_result", "stdout": "1 passed in 0.01s"},
            ),
        ),
    )
    failed_pytest = ACExecutionResult(
        ac_index=1,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_named_tool_result, failed_pytest],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 2
    assert level_success == 2
    assert level_failed == 0
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.SATISFIED_EXTERNALLY,
    ]


def test_complete_sibling_acs_rejects_compound_runtime_command_alias() -> None:
    success_with_compound_runtime_command = ACExecutionResult(
        ac_index=0,
        ac_content="Run the requested test command.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="run pytest and postprocess",
                tool_name="Bash",
                data={
                    "tool_input": {
                        "command": (
                            "/bin/zsh -lc 'uv run pytest tests/test_hello_auto.py "
                            "&& python scripts/postprocess.py'"
                        )
                    }
                },
            ),
            AgentMessage(
                type="tool_result",
                content="1 passed in 0.01s",
                data={"stdout": "1 passed in 0.01s"},
            ),
        ),
    )
    failed_pytest = ACExecutionResult(
        ac_index=1,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_compound_runtime_command, failed_pytest],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def test_complete_sibling_acs_rejects_compound_typed_command_alias() -> None:
    success_with_compound_typed_command = ACExecutionResult(
        ac_index=0,
        ac_content="Run the requested test command.",
        success=True,
        typed_evidence=EvidenceRecord(
            data={
                "tests_passed": [
                    (
                        "/bin/zsh -lc 'uv run pytest tests/test_hello_auto.py "
                        "&& python scripts/postprocess.py'"
                    )
                ]
            }
        ),
    )
    failed_pytest = ACExecutionResult(
        ac_index=1,
        ac_content="The exact command `uv run pytest tests/test_hello_auto.py` passes.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_compound_typed_command, failed_pytest],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def test_complete_sibling_acs_reuses_structured_command_aliases() -> None:
    success_with_goose_command_shape = ACExecutionResult(
        ac_index=0,
        ac_content="Run the requested test command.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="run pytest",
                tool_name="Bash",
                data={"tool_input": {"cmd": ["uv", "run", "pytest", "tests/test_hello_auto.py"]}},
            ),
        ),
    )
    failed_run_command = ACExecutionResult(
        ac_index=1,
        ac_content="Run the exact command `uv run pytest tests/test_hello_auto.py`.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_goose_command_shape, failed_run_command],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 2
    assert level_success == 2
    assert level_failed == 0
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.SATISFIED_EXTERNALLY,
    ]


def test_complete_sibling_acs_does_not_rewrite_blocked_results() -> None:
    success = ACExecutionResult(
        ac_index=0,
        ac_content="`hello_auto.py` exists.",
        success=True,
        typed_evidence=EvidenceRecord(data={"files_touched": ["tests/test_hello_auto.py"]}),
    )
    blocked = ACExecutionResult(
        ac_index=1,
        ac_content="`tests/test_hello_auto.py` exists.",
        success=False,
        error="Skipped: dependency failed",
        outcome=ACExecutionOutcome.BLOCKED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success, blocked],
        ac_statuses={0: "completed", 1: "blocked"},
        failed_indices=set(),
        completed_count=1,
        level_success=1,
        level_failed=0,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 0
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.BLOCKED,
    ]


def test_complete_sibling_acs_does_not_use_bare_write_call_as_file_proof() -> None:
    success_with_write_call_only = ACExecutionResult(
        ac_index=0,
        ac_content="Attempt a file write.",
        success=True,
        messages=(
            AgentMessage(
                type="tool_use",
                content="write file",
                tool_name="Write",
                data={"tool_input": {"file_path": "hello_auto.py"}},
            ),
        ),
    )
    failed_file_presence = ACExecutionResult(
        ac_index=1,
        ac_content="`hello_auto.py` exists.",
        success=False,
        error="worker did not update this AC separately",
        outcome=ACExecutionOutcome.FAILED,
    )

    completed_count, level_success, level_failed, results = _complete_sibling_acs_from_evidence(
        level_results=[success_with_write_call_only, failed_file_presence],
        ac_statuses={0: "completed", 1: "failed"},
        failed_indices={1},
        completed_count=1,
        level_success=1,
        level_failed=1,
    )

    assert completed_count == 1
    assert level_success == 1
    assert level_failed == 1
    assert [result.outcome for result in results] == [
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
    ]


def _make_replaying_event_store() -> tuple[AsyncMock, list[BaseEvent]]:
    """Create an async event-store mock that replays previously appended events."""
    event_store = AsyncMock()
    appended_events: list[BaseEvent] = []

    async def _append(event: BaseEvent) -> None:
        appended_events.append(event)

    async def _replay(aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:
        return [
            event
            for event in appended_events
            if event.aggregate_type == aggregate_type and event.aggregate_id == aggregate_id
        ]

    event_store.append.side_effect = _append
    event_store.replay.side_effect = _replay
    return event_store, appended_events


@pytest.mark.parametrize(
    ("content", "expected"),
    (
        ("3 passed in 1.2s", True),
        ("0 failed, 3 passed", True),
        ("0 failed, 0 errors, 1 passed", True),
        ("no errors, 3 passed", True),
        ("no tests failed, 3 passed", True),
        ("exit code 0", True),
        ("Ran 4 tests in 0.000s\nOK", True),
        ("python -m unittest test_slugify.py: Ran 4 tests in 0.000s OK", True),
        ("success", True),
        ("FAILED (failures=1)\nRan 4 tests in 0.000s", False),
        ("1 failed, 3 passed", False),
        ("2 errors, 1 passed", False),
        ("FAILED tests/test_app.py::test_auth", False),
        ("tests failed", False),
    ),
)
def test_message_contains_test_success_handles_zero_failure_summaries(
    content: str,
    expected: bool,
) -> None:
    """Verifier accepts explicit zero-failure summaries without allowing failures."""
    message = AgentMessage(type="result", content=content, data={})
    assert _message_contains_test_success(message) is expected


@pytest.mark.parametrize(
    "ac_content",
    (
        "Run python -m unittest test_todo.py successfully.",
        "Verify the unit tests pass.",
        "Ensure test_todo.py passes.",
        "Validate the test suite.",
        "Without modifying any files, verify the existing todo implementation by running python -m unittest test_todo.py successfully.",
        "Confirm the current implementation by running python -m unittest test_todo.py.",
        "Check the already-satisfied test suite with python -m unittest test_todo.py.",
    ),
)
def test_validation_only_ac_drops_files_touched_requirement(ac_content: str) -> None:
    """Validation-only ACs prove command/test evidence without requiring file mutation."""
    schema = _effective_evidence_schema_for_ac(load_profile("code"), ac_content)
    assert schema.required == ("commands_run", "tests_passed")


@pytest.mark.parametrize(
    "ac_content",
    (
        "Create test_todo.py with unittest coverage.",
        "Add tests for invalid index handling.",
        "Update test_todo.py to cover the done command.",
        "Implement TodoList.add and run tests.",
        "Modify parser.py and ensure tests pass.",
        "Update parser.py and verify the existing test suite passes.",
        "Modify parser.py and confirm the current implementation by running pytest.",
        "Without modifying files, update parser.py and run pytest.",
        "Without modifying any files, ensure tests cover invalid inputs.",
        "Verify the existing test_todo.py coverage without modifying any files. Run python -m unittest test_todo.py successfully.",
        "Document CLI usage in README.md and update tests for invalid inputs.",
        "Update README.md with usage and run python -m unittest test_todo.py.",
        "Document CLI usage in README.md and verify the test suite.",
        "Document CLI usage in README.md; update tests for invalid inputs.",
        "Update README.md with usage: run python -m unittest test_todo.py.",
        "Refactor the validator and verify unit tests pass.",
        "Change the runtime workflow and run pytest.",
        "Ensure tests cover invalid inputs.",
        "Check tests into the repo for the new parser.",
        "Check the existing tests into the repo for the parser.",
        "Check in tests for the new parser.",
        "Update existing test_todo.py coverage for invalid index handling.",
        "Add coverage to test_todo.py and run python -m unittest test_todo.py.",
    ),
)
def test_test_writing_and_implementation_acs_keep_files_touched_required(
    ac_content: str,
) -> None:
    """ACs that mutate code/tests must still prove files_touched."""
    schema = _effective_evidence_schema_for_ac(load_profile("code"), ac_content)
    assert schema.required == ("files_touched", "commands_run", "tests_passed")


def test_build_governed_parent_summary_preserves_embedded_wrapper_headings() -> None:
    """Only orchestrator-owned wrappers are normalized for governed dispatch."""
    level_context = LevelContext(
        level_number=0,
        completed_acs=(
            ACContextSummary(
                ac_index=0,
                ac_content="Prepare helper",
                success=True,
                key_output=(
                    "Helper is ready\n"
                    "## User Heading\n"
                    "## Previous Work Context\n"
                    "## Coordinator Review (Level 12)\n"
                    "Prior result detail"
                ),
            ),
        ),
        coordinator_review=CoordinatorReview(
            level_number=12,
            review_summary=(
                "No conflicts remain\n## Previous Work Context\n## Coordinator Review (Level 12)"
            ),
        ),
    )

    normalized = _build_governed_parent_summary([level_context])

    assert normalized.splitlines() == [
        "Previous Work Context:",
        "The following ACs have already been completed. Use this context to inform your work.",
        "",
        "- AC 1: Prepare helper",
        "  Result: Helper is ready",
        "## User Heading",
        "## Previous Work Context",
        "## Coordinator Review (Level 12)",
        "Prior result detail",
        "",
        "Coordinator Review (Level 12):",
        "**Review**: No conflicts remain",
        "## Previous Work Context",
        "## Coordinator Review (Level 12)",
    ]


class _FinalMessageRuntime:
    """Minimal runtime that returns one successful final message with a handle."""

    _runtime_handle_backend = "opencode"
    _cwd = "/tmp/project"
    _permission_mode = "acceptEdits"

    def __init__(
        self,
        final_message: str,
        *,
        native_session_id: str,
        support_messages: tuple[AgentMessage, ...] = (),
        cwd: str = "/tmp/project",
    ) -> None:
        self._final_message = final_message
        self._native_session_id = native_session_id
        self._support_messages = support_messages
        self._cwd = cwd
        self.last_prompt: str | None = None
        self.last_system_prompt: str | None = None

    @property
    def runtime_backend(self) -> str:
        return self._runtime_handle_backend

    @property
    def working_directory(self) -> str | None:
        return self._cwd

    @property
    def permission_mode(self) -> str | None:
        return self._permission_mode

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ):
        del tools, resume_session_id
        self.last_prompt = prompt
        self.last_system_prompt = system_prompt
        for message in self._support_messages:
            yield message
        yield AgentMessage(
            type="result",
            content=self._final_message,
            data={"subtype": "success"},
            resume_handle=RuntimeHandle(
                backend=resume_handle.backend if resume_handle is not None else "opencode",
                kind=resume_handle.kind if resume_handle is not None else "implementation_session",
                native_session_id=self._native_session_id,
                cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
            ),
        )


def test_command_claim_supports_exact_structured_shell_body() -> None:
    """Regression for #978 broader observation: read-only command claims may be shell-wrapped."""
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={
            "tool_input": {"command": "/bin/zsh -lc \"rg --files -g 'AGENTS.md' -g '!**/.git/**'\""}
        },
    )

    assert _runtime_messages_support_command_claim(
        "rg --files -g 'AGENTS.md' -g '!**/.git/**'",
        (message,),
    )


def test_command_claim_does_not_support_partial_shell_body() -> None:
    """Generic commands_run aliases stay exact; partial shell scripts are not proof."""
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={"tool_input": {"command": "/bin/zsh -lc 'pwd && rg --files'"}},
    )

    assert not _runtime_messages_support_command_claim("rg --files", (message,))


def test_command_claim_supports_goose_cmd_and_list_shapes() -> None:
    """Goose Bash tool_input may use cmd and list argv forms instead of command."""
    cmd_message = AgentMessage(
        type="tool",
        content="Calling tool: Bash: pytest tests/test_a.py",
        tool_name="Bash",
        data={"tool_input": {"cmd": "pytest tests/test_a.py"}},
    )
    list_message = AgentMessage(
        type="tool",
        content="Calling tool: Bash: python -m unittest test_slugify.py",
        tool_name="Bash",
        data={"tool_input": {"cmd": ["python", "-m", "unittest", "test_slugify.py"]}},
    )

    assert _runtime_messages_support_command_claim("pytest tests/test_a.py", (cmd_message,))
    assert _runtime_messages_support_command_claim(
        "python -m unittest test_slugify.py",
        (list_message,),
    )


def test_command_claim_supports_inner_command_after_safe_shell_preamble() -> None:
    """Wrapped production commands may cite the inner command after setup preambles."""
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={
            "tool_input": {"command": "/bin/bash -lc 'cd /workspace && python scripts/generate.py'"}
        },
    )

    assert _runtime_messages_support_command_claim(
        "python scripts/generate.py",
        (message,),
    )


def test_command_claim_rejects_inner_command_after_non_setup_preamble() -> None:
    """Non-test aliases must not treat arbitrary shell-script tails as proof."""
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={
            "tool_input": {
                "command": "/bin/zsh -lc 'python setup.py && python scripts/generate.py'"
            }
        },
    )

    assert not _runtime_messages_support_command_claim(
        "python scripts/generate.py",
        (message,),
    )


def test_command_claim_supports_command_with_output_redirection_and_pager_pipe() -> None:
    """A clean ``commands_run`` claim matches a run wrapped in ``2>&1 | tail``.

    Regression: agents routinely run ``<cmd> 2>&1 | tail -20`` while citing the
    clean ``<cmd>`` in evidence. The trailing redirection and output-only pager
    pipe must not block the match (which previously failed the whole AC as
    FABRICATION_SUSPECTED).
    """
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={
            "tool_input": {
                "command": (
                    "python -m ruff check src/poc/structure_extractor.py "
                    "tests/test_structure_and_draft_substance.py 2>&1 | tail -20"
                )
            }
        },
    )

    assert _runtime_messages_support_command_claim(
        "python -m ruff check src/poc/structure_extractor.py "
        "tests/test_structure_and_draft_substance.py",
        (message,),
    )


def test_command_claim_supports_inner_command_after_safe_preamble_with_output_plumbing() -> None:
    """Safe shell preambles still peel presentation-only output plumbing."""
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={
            "tool_input": {
                "command": (
                    "/bin/bash -lc 'cd /workspace && python -m ruff check "
                    "src/foo.py tests/test_foo.py 2>&1 | tail -20'"
                )
            }
        },
    )

    assert _runtime_messages_support_command_claim(
        "python -m ruff check src/foo.py tests/test_foo.py",
        (message,),
    )


def test_command_claim_rejects_inner_command_after_safe_preamble_with_grep_filter() -> None:
    """Shell-wrapper peeling must not strip evidence-transforming filters."""
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={
            "tool_input": {
                "command": "/bin/bash -lc 'cd /workspace && pytest tests/test_foo.py | grep PASSED'"
            }
        },
    )

    assert not _runtime_messages_support_command_claim("pytest tests/test_foo.py", (message,))


def test_test_invocation_supports_shell_preamble_with_pipefail_output_plumbing() -> None:
    """Test proof can strip pager plumbing only when pipefail preserves status."""
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={
            "tool_input": {
                "command": (
                    "/bin/bash -lc 'set -o pipefail && cd /workspace && "
                    "pytest tests/test_foo.py 2>&1 | tail -20'"
                )
            }
        },
    )

    assert _runtime_messages_support_command_claim("pytest tests/test_foo.py", (message,))


def test_test_invocation_rejects_status_masking_output_pipe() -> None:
    """A clean pytest claim is not proven by a pipeline whose final filter can mask failure."""
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={
            "tool_input": {
                "command": "/bin/bash -lc 'cd /workspace && pytest tests/test_foo.py | cat'"
            },
            "exit_code": 0,
        },
    )

    assert not _runtime_messages_support_command_claim("pytest tests/test_foo.py", (message,))


def test_command_claim_keeps_meaningful_pipeline_segments() -> None:
    """Only output-filter pipes are stripped; real pipelines are not over-matched."""
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={"tool_input": {"command": "python gen.py | python process.py"}},
    )

    # ``process.py`` is not an output filter, so the pipe stays and the partial
    # ``python gen.py`` claim is not proven by this runtime command.
    assert not _runtime_messages_support_command_claim("python gen.py", (message,))


def test_command_claim_rejects_grep_filtered_run_as_clean_command() -> None:
    """A ``... | grep <token>`` run must not back a clean ``commands_run`` claim.

    ``grep`` can hide failure output and rewrite the evidence stream the
    verifier sees, so treating it as removable presentation plumbing would
    weaken the anti-fabrication boundary: a filtered ``pytest tests/foo.py |
    grep passed`` run could "prove" the clean ``pytest tests/foo.py`` claim
    even when the unfiltered run had failures.
    """
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={"tool_input": {"command": "pytest tests/unit/test_foo.py | grep passed"}},
    )

    assert not _runtime_messages_support_command_claim("pytest tests/unit/test_foo.py", (message,))


def test_command_claim_rejects_grep_filtered_run_as_tests_passed_claim() -> None:
    """A grep-filtered test run must not back a ``tests_passed`` claim either.

    ``_normalized_command_claim_aliases`` is also consumed on the
    ``tests_passed`` path, so the same anti-fabrication invariant has to hold
    there: a grep-filtered run is not equivalent to the unfiltered test
    command for evidence-matching purposes.
    """
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={"tool_input": {"command": "pytest tests/unit/test_foo.py -x | grep PASSED"}},
    )

    assert not _runtime_messages_support_command_claim(
        "pytest tests/unit/test_foo.py -x", (message,)
    )


def test_command_claim_rejects_wc_collapsed_run_as_clean_command() -> None:
    """``... | wc -l`` collapses the evidence stream to a count and must not match.

    ``wc`` discards every line of the underlying output, so a verifier looking
    at the runtime transcript would no longer see the unfiltered command's
    output. Treating ``wc`` as removable plumbing would let a filtered run
    silently back a clean ``commands_run`` claim.
    """
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={"tool_input": {"command": "pytest tests/unit/test_foo.py | wc -l"}},
    )

    assert not _runtime_messages_support_command_claim("pytest tests/unit/test_foo.py", (message,))


def test_command_claim_rejects_tee_redirected_run_as_clean_command() -> None:
    """``... | tee out.log`` diverts the evidence stream and must not back a claim.

    ``tee`` is a side-effecting redirector, not presentation-only output
    filtering — the file write means the unfiltered runtime stream is no
    longer the only observable evidence. Keep alias matching strict so the
    filtered command does not prove a clean ``commands_run`` claim.
    """
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={"tool_input": {"command": "pytest tests/unit/test_foo.py | tee pytest.log"}},
    )

    assert not _runtime_messages_support_command_claim("pytest tests/unit/test_foo.py", (message,))


class TestProfileAwareDecompositionAudit:
    @pytest.mark.asyncio
    async def test_level_started_event_records_active_decomposition_profile(self) -> None:
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            execution_profile=load_profile("code"),
        )

        await executor._emit_level_started(
            session_id="sess_profile",
            level=1,
            ac_indices=[0, 1],
            total_levels=2,
        )

        event = event_store.append.await_args.args[0]
        assert event.type == "execution.decomposition.level_started"
        assert event.data["decomposition_profile"] == {
            "profile": "code",
            "axis": "testable_unit",
            "min_unit": "single function or module with at least one runnable test",
            "cut_signal": "sub-AC produces an independently runnable test",
            "max_branching": 5,
        }

    @pytest.mark.asyncio
    async def test_level_started_event_records_legacy_decomposition_fallback(self) -> None:
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
        )

        await executor._emit_level_started(
            session_id="sess_legacy",
            level=1,
            ac_indices=[0],
            total_levels=1,
        )

        event = event_store.append.await_args.args[0]
        assert event.data["decomposition_profile"] is None


class TestProfileAwareContextGovernance:
    @pytest.mark.asyncio
    async def test_profile_backed_atomic_dispatch_uses_context_governor(self) -> None:
        class _StubRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        event_store, appended_events = _make_replaying_event_store()
        runtime = _StubRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
        )
        level_context = LevelContext(
            level_number=0,
            completed_acs=(
                ACContextSummary(
                    ac_index=0,
                    ac_content="Prepare helper",
                    success=True,
                    key_output=(
                        "Helper is ready\n"
                        "## User Heading\n"
                        "## Previous Work Context\n"
                        "## Coordinator Review (Level 1)\n"
                        "Prior result detail"
                    ),
                ),
            ),
            coordinator_review=CoordinatorReview(
                level_number=1,
                review_summary="No conflicts remain\n## Previous Work Context",
                warnings_for_next_level=("Keep edits localized",),
            ),
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Implement duplicate leaf",
            session_id="sess_context",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship context governance",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_context",
            level_contexts=[level_context],
            sibling_acs=[(1, "Implement duplicate leaf"), (2, "Implement duplicate leaf")],
        )

        assert result.success is True
        prompt = runtime.calls[0]["prompt"]
        assert "## Governed Dispatch Context (AC 2)" in prompt
        assert "## Parent context" in prompt
        assert "## Previous Work Context\nThe following ACs" not in prompt
        assert "## Coordinator Review (Level 1)\n**Review**" not in prompt
        assert "Previous Work Context:" in prompt
        assert "Coordinator Review (Level 1):" in prompt
        assert "Helper is ready" in prompt
        assert "## User Heading" in prompt
        assert "User Heading:" not in prompt
        assert "## Previous Work Context" in prompt
        assert "## Coordinator Review (Level 1)" in prompt
        assert "Prior result detail" in prompt
        assert "No conflicts remain" in prompt
        assert "Keep edits localized" in prompt
        assert "## Sibling status" in prompt
        assert "… sibling-1: Implement duplicate leaf" in prompt
        assert "## AC\nImplement duplicate leaf" in prompt
        assert "## Parallel Execution Notice" in prompt
        assert "Avoid modifying files that other agents are likely editing." in prompt
        assert "summarized in the governed sibling-status section above" in prompt

        context_events = [
            event for event in appended_events if event.type == "execution.ac.context_governed"
        ]
        assert len(context_events) == 1
        assert context_events[0].data["context_governed"] is True
        assert context_events[0].data["context_acceptance_enforced"] is False
        assert context_events[0].data["context_default_flipped"] is False
        assert context_events[0].data["profile"] == "code"
        assert context_events[0].data["context_sibling_status_count"] == 1

    @pytest.mark.asyncio
    async def test_legacy_atomic_dispatch_keeps_existing_context_prompt_shape(self) -> None:
        class _StubRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        event_store, appended_events = _make_replaying_event_store()
        runtime = _StubRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        level_context = LevelContext(
            level_number=0,
            completed_acs=(
                ACContextSummary(
                    ac_index=0,
                    ac_content="Prepare helper",
                    success=True,
                    key_output="Helper is ready",
                ),
            ),
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Implement legacy leaf",
            session_id="sess_legacy_context",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship legacy context",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_legacy_context",
            level_contexts=[level_context],
            sibling_acs=[(1, "Implement legacy leaf"), (2, "Update sibling docs")],
        )

        assert result.success is True
        prompt = runtime.calls[0]["prompt"]
        assert "## Your Task (AC 2)\nImplement legacy leaf" in prompt
        assert "## Previous Work Context" in prompt
        assert "## Parallel Execution Notice" in prompt
        assert "## Governed Dispatch Context" not in prompt
        assert not any(event.type == "execution.ac.context_governed" for event in appended_events)

    @pytest.mark.asyncio
    async def test_profile_context_governor_budget_error_falls_back_without_failing_ac(
        self,
    ) -> None:
        class _StubRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        event_store, appended_events = _make_replaying_event_store()
        runtime = _StubRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
        )
        oversized_ac = "x" * 13_000

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content=oversized_ac,
            session_id="sess_context_fallback",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship context governance fallback",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_context_fallback",
        )

        assert result.success is True
        prompt = runtime.calls[0]["prompt"]
        assert "## Your Task (AC 1)" in prompt
        assert "## Governed Dispatch Context" not in prompt
        context_events = [
            event for event in appended_events if event.type == "execution.ac.context_governed"
        ]
        assert len(context_events) == 1
        assert context_events[0].data["context_governed"] is False
        assert context_events[0].data["context_fallback"] == "legacy_prompt"
        assert (
            "AC alone exceeds context budget" in context_events[0].data["context_governance_error"]
        )


class TestParallelACExecutor:
    """Tests for staged hybrid result handling."""

    def test_verification_report_uses_task_completion_terms(self) -> None:
        parallel_result = ParallelExecutionResult(
            stages=(),
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content="Create tasks",
                    success=True,
                    is_decomposed=True,
                    sub_results=(
                        ACExecutionResult(
                            ac_index=100,
                            ac_content="Create task storage",
                            success=False,
                            final_message="Storage failed",
                        ),
                    ),
                ),
            ),
            success_count=0,
            failure_count=1,
        )

        report = render_parallel_verification_report(parallel_result, 1)
        completion = render_parallel_completion_message(parallel_result, 1)

        assert "## Task Results" in report
        assert "### Task 1: [COMPLETED] Create tasks" in report
        assert "#### Subtask 1.1: [FAILED] Create task storage" in report
        assert "## AC Results" not in report
        assert "[PASS]" not in report
        assert "[FAIL]" not in report
        assert "Task Status:" in completion
        assert "- Task 1: [COMPLETED] Create tasks (1 subtasks)" in completion

    @pytest.mark.asyncio
    async def test_emit_subtask_event_preserves_full_content_with_compact_label(self) -> None:
        """Sub-AC events should retain full replay content plus compact display text."""
        event_store = AsyncMock()
        appended_events: list[BaseEvent] = []

        async def _append(event: BaseEvent) -> None:
            appended_events.append(event)

        event_store.append.side_effect = _append
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
        )
        full_content = (
            "Define baseline_source_branch as a single authoritative baseline identity "
            "with repository URL, exact ref, commit SHA, capture timestamp, operator, "
            "and artifact bundle IDs."
        )

        await executor._emit_subtask_event(
            execution_id="exec_subtask_event",
            ac_index=0,
            sub_task_index=1,
            sub_task_content=full_content,
            status="executing",
        )

        assert len(appended_events) == 1
        data = appended_events[0].data
        assert data["content"] == full_content
        assert data["label"] == "Define baseline_source_branch as a single authorit"
        assert len(data["label"]) == 50
        assert data["sub_task_id"] == "ac_1_sub_1"
        assert data["status"] == "executing"

    @pytest.mark.asyncio
    async def test_emit_subtask_event_emits_node_identity_with_legacy_event(self) -> None:
        """New Sub-AC events should expose canonical node identity and legacy fields."""
        event_store = AsyncMock()
        appended_events: list[BaseEvent] = []

        async def _append(event: BaseEvent) -> None:
            appended_events.append(event)

        event_store.append.side_effect = _append
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
        )
        node_identity = ExecutionNodeIdentity.root(
            execution_context_id="exec_subtask_event",
            ac_index=0,
        ).child(1)

        await executor._emit_subtask_event(
            execution_id="exec_subtask_event",
            ac_index=0,
            sub_task_index=2,
            sub_task_content="Populate the baseline source branch evidence ledger.",
            status="pending",
            node_identity=node_identity,
        )

        assert [event.type for event in appended_events] == [
            "execution.node.created",
            "execution.subtask.updated",
        ]
        node_event, legacy_event = appended_events
        assert node_event.data["identity_model"] == "execution_node_v1"
        assert node_event.data["node_id"] == node_identity.node_id
        assert node_event.data["parent_node_id"] == node_identity.parent_node_id
        assert node_event.data["legacy_parent_node_id"] == "ac_0"
        assert node_event.data["display_path"] == "1.2"
        assert node_event.data["legacy_ac_index"] == 1
        assert node_event.data["legacy_sub_task_id"] == "ac_1_sub_2"
        assert legacy_event.data["node_id"] == node_identity.node_id
        assert legacy_event.data["parent_node_id"] == node_identity.parent_node_id
        assert legacy_event.data["legacy_parent_node_id"] == "ac_0"
        assert legacy_event.data["sub_task_id"] == "ac_1_sub_2"

    @pytest.mark.asyncio
    async def test_node_runtime_load_falls_back_to_legacy_scope_events(self) -> None:
        """Node-aware resume lookup should still find pre-node runtime events."""
        node_identity = ExecutionNodeIdentity.root(
            execution_context_id="orch_123",
            ac_index=1,
        )
        legacy_scope_id = "orch_123_ac_2"
        legacy_state_path = (
            "execution.workflows.orch_123.acceptance_criteria.ac_2.implementation_session"
        )
        persisted_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-legacy",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "ac_index": 1,
                "session_scope_id": legacy_scope_id,
                "session_state_path": legacy_state_path,
                "server_session_id": "server-legacy",
            },
        )
        replayed_scope_ids: list[str] = []

        async def _replay(_aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:
            replayed_scope_ids.append(aggregate_id)
            if aggregate_id != legacy_scope_id:
                return []
            return [
                BaseEvent(
                    type="execution.session.started",
                    aggregate_type="execution",
                    aggregate_id=legacy_scope_id,
                    data={
                        "retry_attempt": 0,
                        "session_scope_id": legacy_scope_id,
                        "session_state_path": legacy_state_path,
                        "runtime": persisted_handle.to_dict(),
                    },
                )
            ]

        event_store = AsyncMock()
        event_store.replay.side_effect = _replay
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
        )

        resume_handle = await executor._load_persisted_ac_runtime_handle(
            1,
            execution_context_id="orch_123",
            node_identity=node_identity,
        )

        assert resume_handle is not None
        assert replayed_scope_ids[0] == f"orch_123_{node_identity.node_id}"
        assert legacy_scope_id in replayed_scope_ids
        assert resume_handle.native_session_id == "opencode-session-legacy"
        assert resume_handle.metadata["server_session_id"] == "server-legacy"
        assert resume_handle.metadata["node_id"] == node_identity.node_id
        assert resume_handle.metadata["legacy_node_id"] == "ac_1"
        assert resume_handle.metadata["session_scope_id"] == f"orch_123_{node_identity.node_id}"
        assert resume_handle.metadata["legacy_session_scope_id"] == legacy_scope_id

    @pytest.mark.asyncio
    async def test_deep_sub_ac_runtime_identity_does_not_require_legacy_indices(self) -> None:
        """Grandchild Sub-AC execution should not crash while building runtime identity."""

        class _StubRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        grandchild_identity = (
            ExecutionNodeIdentity.root(
                execution_context_id="exec_deep_runtime",
                ac_index=0,
            )
            .child(0)
            .child(1)
        )
        event_store, _appended_events = _make_replaying_event_store()
        runtime = _StubRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=10000,
            ac_content="Implement deep recursive leaf",
            session_id="sess_deep_runtime",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Support recursive decomposition",
            depth=2,
            start_time=datetime.now(UTC),
            execution_id="exec_deep_runtime",
            is_sub_ac=True,
            node_identity=grandchild_identity,
        )

        assert result.success is True
        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.metadata["node_id"] == grandchild_identity.node_id
        assert resume_handle.metadata["parent_node_id"] == grandchild_identity.parent_node_id
        assert resume_handle.metadata["session_scope_id"] == (
            f"exec_deep_runtime_{grandchild_identity.node_id}"
        )
        assert "legacy_session_scope_id" not in resume_handle.metadata
        assert "legacy_session_scope_ids" not in resume_handle.metadata
        event_store.replay.assert_awaited_once_with(
            "execution",
            f"exec_deep_runtime_{grandchild_identity.node_id}",
        )

    @pytest.mark.asyncio
    async def test_batch_fans_out_in_parallel_regardless_of_tool_catalog(self) -> None:
        """Batch scheduling is tool-catalog-agnostic.

        The control plane exists as declarative audit/metadata, not as a
        batch-level scheduler.  Cross-AC safety is enforced by the
        file-conflict guard (static) and by the provider runtime at
        tool-invocation time (dynamic); the scheduler must not degrade
        a batch to serial execution based on session-level tool
        availability, because "tool is in the catalog" does not imply
        "every AC in this batch will invoke it".

        This test mixes read-only and write-capable tools in the same
        catalog to pin that mixed catalogs also fan out in parallel.
        """
        seed = _make_seed("AC alpha", "AC beta")
        executor = _make_executor()
        active_count = 0
        max_active_count = 0

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            nonlocal active_count, max_active_count
            ac_index = int(kwargs["ac_index"])
            active_count += 1
            max_active_count = max(max_active_count, active_count)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            active_count -= 1
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=str(kwargs["ac_content"]),
                success=True,
                final_message=f"AC {ac_index} complete",
            )

        with patch.object(executor, "_execute_single_ac", side_effect=fake_execute_single_ac):
            results = await executor._execute_ac_batch(
                seed=seed,
                batch_indices=[0, 1],
                session_id="sess_batch_parallel",
                execution_id="exec_batch_parallel",
                tools=["Read", "Edit", "Bash"],
                tool_catalog=(
                    MCPToolDefinition(name="Read", description="Read files"),
                    MCPToolDefinition(name="Edit", description="Edit files"),
                    MCPToolDefinition(name="Bash", description="Run shell"),
                ),
                system_prompt="test",
                level_contexts=[],
                ac_retry_attempts={0: 0, 1: 0},
            )

        assert [result.ac_index for result in results if isinstance(result, ACExecutionResult)] == [
            0,
            1,
        ]
        # Regression guard: even a catalog containing SERIALIZED (Edit)
        # and ISOLATED_SESSION_REQUIRED (Bash) tools must not collapse
        # a batch to serial execution.
        assert max_active_count == 2

    @pytest.mark.asyncio
    async def test_atomic_ac_uses_ac_scoped_runtime_handle(self) -> None:
        """Atomic AC execution should seed a fresh AC-scoped runtime handle."""

        class _StubImplementationRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                bound_handle = RuntimeHandle(
                    backend=resume_handle.backend if resume_handle is not None else "opencode",
                    kind=resume_handle.kind
                    if resume_handle is not None
                    else "implementation_session",
                    native_session_id="opencode-session-1",
                    cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                    approval_mode=(
                        resume_handle.approval_mode if resume_handle is not None else "acceptEdits"
                    ),
                    metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=bound_handle,
                )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_StubImplementationRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=2,
            ac_content="Implement AC 3",
            session_id="orch_123",
            tools=["Read", "Edit"],
            tool_catalog=(
                MCPToolDefinition(name="Read", description="Read a file from the workspace."),
                MCPToolDefinition(
                    name="Edit", description="Edit an existing file in the workspace."
                ),
            ),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        runtime_call = executor._adapter.calls[0]
        resume_handle = runtime_call["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.backend == "opencode"
        assert resume_handle.kind == "implementation_session"
        assert resume_handle.native_session_id is None
        assert resume_handle.cwd == "/tmp/project"
        assert resume_handle.approval_mode == "acceptEdits"
        assert resume_handle.metadata["ac_id"] == "orch_123_ac_3"
        assert resume_handle.metadata["scope"] == "ac"
        assert resume_handle.metadata["session_role"] == "implementation"
        assert resume_handle.metadata["retry_attempt"] == 0
        assert resume_handle.metadata["attempt_number"] == 1
        assert resume_handle.metadata["ac_index"] == 2
        assert [tool["name"] for tool in resume_handle.metadata["tool_catalog"]] == [
            "Read",
            "Edit",
        ]
        assert [tool["name"] for tool in resume_handle.metadata["capability_graph"]] == [
            "Read",
            "Edit",
        ]
        assert [hint["name"] for hint in resume_handle.metadata["control_plane"]] == [
            "Read",
            "Edit",
        ]
        assert resume_handle.metadata["session_scope_id"] == "orch_123_ac_3"
        assert resume_handle.metadata["session_attempt_id"] == "orch_123_ac_3_attempt_1"
        assert (
            resume_handle.metadata["session_state_path"]
            == "execution.workflows.orch_123.acceptance_criteria.ac_3.implementation_session"
        )
        started_event = next(
            event for event in appended_events if event.type == "execution.session.started"
        )
        assert [tool["name"] for tool in started_event.data["tool_catalog"]] == ["Read", "Edit"]
        assert [
            tool["name"] for tool in started_event.data["runtime"]["metadata"]["tool_catalog"]
        ] == ["Read", "Edit"]
        assert [
            tool["name"] for tool in started_event.data["runtime"]["metadata"]["capability_graph"]
        ] == [
            "Read",
            "Edit",
        ]
        assert started_event.data["session_attempt_id"] == "orch_123_ac_3_attempt_1"
        assert result.success is True
        assert result.session_id == "opencode-session-1"
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == "opencode-session-1"

    @pytest.mark.asyncio
    async def test_atomic_ac_terminates_live_runtime_handle_after_completion(self) -> None:
        """Completed AC runs should best-effort terminate live runtime handles."""
        terminate_calls = 0

        async def _terminate(_handle: RuntimeHandle) -> bool:
            nonlocal terminate_calls
            terminate_calls += 1
            return True

        class _StubImplementationRuntime:
            def __init__(self) -> None:
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                del prompt, tools, system_prompt, resume_session_id
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend=resume_handle.backend if resume_handle is not None else "opencode",
                        kind=resume_handle.kind
                        if resume_handle is not None
                        else "implementation_session",
                        native_session_id="opencode-session-live",
                        cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                        approval_mode=(
                            resume_handle.approval_mode
                            if resume_handle is not None
                            else "acceptEdits"
                        ),
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ).bind_controls(terminate_callback=_terminate),
                )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_StubImplementationRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(
                MCPToolDefinition(name="Read", description="Read a file from the workspace."),
            ),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert terminate_calls == 1

    @pytest.mark.asyncio
    async def test_atomic_ac_observes_profile_typed_evidence_without_changing_success(self) -> None:
        """Profile-backed atomic completion records typed evidence observe-only."""

        class _StubImplementationRuntime:
            _runtime_handle_backend = "opencode"
            _cwd = "/tmp/project"
            _permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                del prompt, tools, system_prompt, resume_session_id
                yield AgentMessage(
                    type="result",
                    content=(
                        "Done.\n"
                        "```json\n"
                        '{"files_touched":["src/app.py"],'
                        '"commands_run":["pytest"],'
                        '"tests_passed":["tests/test_app.py"]}\n'
                        "```"
                    ),
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend=resume_handle.backend if resume_handle is not None else "opencode",
                        kind=resume_handle.kind
                        if resume_handle is not None
                        else "implementation_session",
                        native_session_id="opencode-session-evidence",
                        cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_StubImplementationRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.typed_evidence is not None
        assert result.typed_evidence.data["files_touched"] == ["src/app.py"]
        assert result.typed_evidence_validation is not None
        assert result.typed_evidence_validation.ok is True
        assert result.typed_evidence_error is None

        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["observe_only"] is True
        assert evidence_event.data["enforced"] is False
        assert evidence_event.data["fat_harness_mode"] is False
        assert evidence_event.data["enforcement_error"] is None
        assert evidence_event.data["typed_evidence_present"] is True
        assert evidence_event.data["typed_evidence_valid"] is True
        assert evidence_event.data["verifier_ran"] is False
        assert evidence_event.data["verifier_passed"] is False
        assert evidence_event.data["required_fields"] == [
            "files_touched",
            "commands_run",
            "tests_passed",
        ]
        assert evidence_event.data["typed_evidence_fields"] == [
            "commands_run",
            "files_touched",
            "tests_passed",
        ]

    @pytest.mark.asyncio
    async def test_atomic_ac_records_typed_evidence_error_without_default_flip(self) -> None:
        """Malformed typed evidence is observed but does not change legacy success."""

        class _StubImplementationRuntime:
            _runtime_handle_backend = "opencode"
            _cwd = "/tmp/project"
            _permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                del prompt, tools, system_prompt, resume_session_id
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE] no JSON evidence yet",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend=resume_handle.backend if resume_handle is not None else "opencode",
                        kind=resume_handle.kind
                        if resume_handle is not None
                        else "implementation_session",
                        native_session_id="opencode-session-no-evidence",
                        cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_StubImplementationRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.typed_evidence is None
        assert result.typed_evidence_validation is None
        assert result.typed_evidence_error is not None

        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["observe_only"] is True
        assert evidence_event.data["enforced"] is False
        assert evidence_event.data["typed_evidence_present"] is False
        assert evidence_event.data["typed_evidence_valid"] is False
        assert evidence_event.data["verifier_ran"] is False
        assert "Evidence is not valid JSON" in evidence_event.data["typed_evidence_error"]

    @pytest.mark.asyncio
    async def test_fat_harness_atomic_prompt_requests_json_evidence_without_task_complete(
        self,
    ) -> None:
        """Fat-harness atomic prompts must not ask for prose [TASK_COMPLETE]."""
        event_store, _ = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "```json\n"
            '{"files_touched":["src/app.py"],"commands_run":["pytest"],"tests_passed":["pytest"]}'
            "\n```",
            native_session_id="opencode-session-prompt",
            support_messages=(
                AgentMessage(
                    type="tool",
                    content="Edit src/app.py",
                    tool_name="Edit",
                    data={"input": {"file_path": "src/app.py"}},
                ),
                AgentMessage(
                    type="tool",
                    content="pytest passed",
                    tool_name="Bash",
                    data={"input": {"command": "pytest"}},
                ),
            ),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert runtime.last_prompt is not None
        assert "emit exactly ONE fenced JSON evidence record" in runtime.last_prompt
        assert "files_touched, commands_run, tests_passed" in runtime.last_prompt
        assert "do not emit a generic command_result wrapper" in runtime.last_prompt
        assert "Do not prefix it with [TASK_COMPLETE]" in runtime.last_prompt
        assert "You are responsible only for the current acceptance criterion" in (
            runtime.last_prompt
        )
        assert "Do not implement, test, document, or pre-create work" in runtime.last_prompt
        assert "sibling or future ACs" in runtime.last_prompt
        assert "current AC in this runtime session" in runtime.last_prompt
        assert "workspace-relative paths only" in runtime.last_prompt
        assert "never absolute paths" in runtime.last_prompt
        assert "omit exploratory" in runtime.last_prompt
        assert "rg, grep, sed, cat, ls, find, or pwd" in runtime.last_prompt
        assert "Auto Recursion Guard" in runtime.last_prompt
        assert "ouroboros_auto" in runtime.last_prompt
        assert "nested auto session" in runtime.last_prompt
        assert "explicitly state: [TASK_COMPLETE]" not in runtime.last_prompt

    @pytest.mark.asyncio
    async def test_fat_harness_docs_only_ac_uses_docs_evidence_contract(self, tmp_path) -> None:
        """Regression for #961: README-only ACs must not require prior test IDs."""
        readme = tmp_path / "README.md"
        readme.write_text("# String utils\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "```json\n"
            "{\n"
            '  "files_touched": ["README.md"],\n'
            '  "commands_run": ["grep -n slugify README.md"]\n'
            "}\n"
            "```",
            native_session_id="codex-session-docs-only-current-ac",
            support_messages=(
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Edit: {readme}",
                    tool_name="Edit",
                    data={"tool_input": {"file_path": str(readme)}},
                ),
                AgentMessage(
                    type="assistant",
                    content="Calling tool: Bash: grep -n slugify README.md",
                    tool_name="Bash",
                    data={
                        "tool_input": {"command": "grep -n slugify README.md"},
                        "output": "12:slugify('Hello World') -> hello-world",
                        "exit_code": 0,
                    },
                ),
            ),
            cwd=str(tmp_path),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=2,
            ac_content="Document slugify and truncate usage in README.md.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
            sibling_acs=[
                (0, "Create string_utils.py with slugify(text) and test_slugify.py."),
                (1, "Add truncate(text, max_length) and test_truncate.py."),
                (2, "Document slugify and truncate usage in README.md."),
            ],
        )

        assert result.success is True
        assert result.error is None
        assert runtime.last_prompt is not None
        assert "documentation-only current AC" in runtime.last_prompt
        assert "read/grep/diff command when that command is the validation" in runtime.last_prompt
        assert "Do not include tests_passed at all for documentation-only ACs" in (
            runtime.last_prompt
        )
        assert "do not list individual test names or prior test IDs" in runtime.last_prompt
        assert "files_touched, commands_run" in runtime.last_prompt
        assert "files_touched, commands_run, tests_passed" not in runtime.last_prompt
        assert result.typed_evidence is not None
        assert "tests_passed" not in result.typed_evidence.data
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == ["files_touched", "commands_run"]
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.parametrize(
        ("ac_content", "doc_path"),
        [
            ("Document the API in docs/api.md.", "docs/api.md"),
            ("Write a CLI flag guide in README.md.", "README.md"),
            ("Update the changelog for the parser bug.", "CHANGELOG.md"),
            ("Document test setup in README.md.", "README.md"),
            ("Write a unit test guide in docs/testing.md.", "docs/testing.md"),
            (
                "Create README.md documenting how to run the CLI and the required test command.",
                "README.md",
            ),
            (
                "Document CLI usage and the required test command in README.md.",
                "README.md",
            ),
            (
                "Update README.md with usage and verification instructions for python -m unittest test_todo.py.",
                "README.md",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_fat_harness_docs_only_ac_allows_code_subject_documentation(
        self, tmp_path, ac_content: str, doc_path: str
    ) -> None:
        """Docs about code subjects are still docs-only when they do not mutate code."""
        doc_file = tmp_path / doc_path
        doc_file.parent.mkdir(parents=True, exist_ok=True)
        doc_file.write_text("Documentation\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "```json\n"
            "{\n"
            f'  "files_touched": ["{doc_path}"],\n'
            f'  "commands_run": ["grep -n Documentation {doc_path}"]\n'
            "}\n"
            "```",
            native_session_id="codex-session-docs-only-code-subject",
            support_messages=(
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Edit: {doc_file}",
                    tool_name="Edit",
                    data={"tool_input": {"file_path": str(doc_file)}},
                ),
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Bash: grep -n Documentation {doc_path}",
                    tool_name="Bash",
                    data={
                        "tool_input": {"command": f"grep -n Documentation {doc_path}"},
                        "output": "1:Documentation",
                        "exit_code": 0,
                    },
                ),
            ),
            cwd=str(tmp_path),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content=ac_content,
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship docs",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert runtime.last_prompt is not None
        assert "documentation-only current AC" in runtime.last_prompt
        assert "read/grep/diff command when that command is the validation" in runtime.last_prompt
        assert "Do not include tests_passed at all for documentation-only ACs" in (
            runtime.last_prompt
        )
        assert "do not list individual test names or prior test IDs" in runtime.last_prompt
        assert "files_touched, commands_run" in runtime.last_prompt
        assert "files_touched, commands_run, tests_passed" not in runtime.last_prompt
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == ["files_touched", "commands_run"]
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_markdown_code_ac_keeps_test_evidence_required(
        self, tmp_path
    ) -> None:
        """A markdown-related implementation AC must not be misclassified as docs-only."""
        parser_file = tmp_path / "src" / "markdown_parser.py"
        parser_file.parent.mkdir()
        parser_file.write_text("def parse(text):\n    return text\n", encoding="utf-8")
        test_file = tmp_path / "tests" / "test_markdown_parser.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_parse():\n    assert True\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "```json\n"
            "{\n"
            '  "files_touched": ["src/markdown_parser.py", "tests/test_markdown_parser.py"],\n'
            '  "commands_run": ["python -m pytest tests/test_markdown_parser.py"],\n'
            '  "tests_passed": ["tests/test_markdown_parser.py::test_parse"]\n'
            "}\n"
            "```",
            native_session_id="codex-session-markdown-code-ac",
            support_messages=(
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Edit: {parser_file}",
                    tool_name="Edit",
                    data={"tool_input": {"file_path": str(parser_file)}},
                ),
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Edit: {test_file}",
                    tool_name="Edit",
                    data={"tool_input": {"file_path": str(test_file)}},
                ),
                AgentMessage(
                    type="assistant",
                    content="Calling tool: Bash: python -m pytest tests/test_markdown_parser.py",
                    tool_name="Bash",
                    data={
                        "tool_input": {"command": "python -m pytest tests/test_markdown_parser.py"},
                        "output": "tests/test_markdown_parser.py::test_parse passed; 1 passed",
                        "exit_code": 0,
                    },
                ),
            ),
            cwd=str(tmp_path),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement a markdown parser and usage examples.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship markdown support",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert runtime.last_prompt is not None
        assert "documentation-only current AC" not in runtime.last_prompt
        assert "files_touched, commands_run, tests_passed" in runtime.last_prompt
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == [
            "files_touched",
            "commands_run",
            "tests_passed",
        ]
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.parametrize(
        "ac_content",
        [
            "Add slugify() and update README.md.",
            "Fix parser bug and document it in docs/.",
            "Update README.md and fix parser bug.",
            "Write docs/api.md and add endpoint validation.",
            "Document README.md, then create parser.py.",
            "Run pytest and update README.md.",
            "Add docs command to CLI.",
            "Create docs endpoint.",
            "Fix docs parser bug.",
            "Update README.md while fixing parser bug.",
            "Update README.md plus fix parser bug.",
            "Fix documentation parser bug.",
        ],
    )
    @pytest.mark.asyncio
    async def test_fat_harness_mixed_code_and_docs_ac_keeps_test_evidence_required(
        self, tmp_path, ac_content: str
    ) -> None:
        """Mixed implementation/docs ACs must not drop tests_passed from code profile evidence."""
        source_file = tmp_path / "src" / "string_utils.py"
        source_file.parent.mkdir()
        source_file.write_text("def slugify(text):\n    return text.lower()\n", encoding="utf-8")
        test_file = tmp_path / "tests" / "test_string_utils.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_slugify():\n    assert True\n", encoding="utf-8")
        readme = tmp_path / "README.md"
        readme.write_text("# String utils\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "```json\n"
            "{\n"
            '  "files_touched": ["src/string_utils.py", "tests/test_string_utils.py", "README.md"],\n'
            '  "commands_run": ["python -m pytest tests/test_string_utils.py"],\n'
            '  "tests_passed": ["tests/test_string_utils.py::test_slugify"]\n'
            "}\n"
            "```",
            native_session_id="codex-session-mixed-code-docs-ac",
            support_messages=(
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Edit: {source_file}",
                    tool_name="Edit",
                    data={"tool_input": {"file_path": str(source_file)}},
                ),
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Edit: {test_file}",
                    tool_name="Edit",
                    data={"tool_input": {"file_path": str(test_file)}},
                ),
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Edit: {readme}",
                    tool_name="Edit",
                    data={"tool_input": {"file_path": str(readme)}},
                ),
                AgentMessage(
                    type="assistant",
                    content="Calling tool: Bash: python -m pytest tests/test_string_utils.py",
                    tool_name="Bash",
                    data={
                        "tool_input": {"command": "python -m pytest tests/test_string_utils.py"},
                        "output": "tests/test_string_utils.py::test_slugify passed; 1 passed",
                        "exit_code": 0,
                    },
                ),
            ),
            cwd=str(tmp_path),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content=ac_content,
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert runtime.last_prompt is not None
        assert "documentation-only current AC" not in runtime.last_prompt
        assert "files_touched, commands_run, tests_passed" in runtime.last_prompt
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == [
            "files_touched",
            "commands_run",
            "tests_passed",
        ]
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_docs_only_ac_ignores_out_of_scope_test_id_bleed(
        self, tmp_path
    ) -> None:
        """Docs-only ACs ignore extra tests_passed instead of failing required docs evidence."""
        readme = tmp_path / "README.md"
        readme.write_text("# String utils\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["README.md"],\n'
                '  "commands_run": ["grep -n slugify README.md"],\n'
                '  "tests_passed": ["test_slugify.py::test_slugify"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-docs-only-prior-test-bleed",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {readme}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(readme)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: Bash: grep -n slugify README.md",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": "grep -n slugify README.md"},
                            "output": "12:slugify('Hello World') -> hello-world",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=2,
            ac_content="Document slugify and truncate usage in README.md.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        assert result.typed_evidence is not None
        assert result.typed_evidence.data == {
            "files_touched": ["README.md"],
            "commands_run": ["grep -n slugify README.md"],
        }
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == ["files_touched", "commands_run"]
        assert evidence_event.data["ignored_out_of_scope_evidence_fields"] == ["tests_passed"]
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_docs_only_ac_passes_consistent_profile_to_injected_verifier(
        self, tmp_path
    ) -> None:
        """Docs-only AC profile overrides must keep must_produce within required evidence."""
        readme = tmp_path / "README.md"
        readme.write_text("# String utils\n", encoding="utf-8")
        verifier_profiles: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        verifier_records: list[dict[str, object]] = []

        def _recording_verifier(**kwargs: object) -> VerifierVerdict:
            profile = kwargs["profile"]
            record = kwargs["record"]
            verifier_profiles.append(
                (
                    tuple(profile.evidence_schema.required),  # type: ignore[attr-defined]
                    tuple(profile.must_produce),  # type: ignore[attr-defined]
                )
            )
            verifier_records.append(dict(record.data))  # type: ignore[attr-defined]
            return VerifierVerdict(passed=True)

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["README.md"],\n'
                '  "commands_run": ["grep -n slugify README.md"],\n'
                '  "tests_passed": ["test_slugify.py::test_slugify"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-docs-only-injected-verifier",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {readme}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(readme)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: Bash: grep -n slugify README.md",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": "grep -n slugify README.md"},
                            "output": "12:slugify('Hello World') -> hello-world",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            atomic_verifier=_recording_verifier,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=2,
            ac_content="Document slugify and truncate usage in README.md.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert verifier_profiles == [(("files_touched", "commands_run"), ("files_touched",))]
        assert set(verifier_profiles[0][1]).issubset(verifier_profiles[0][0])
        assert verifier_records == [
            {
                "files_touched": ["README.md"],
                "commands_run": ["grep -n slugify README.md"],
            }
        ]
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == ["files_touched", "commands_run"]
        assert evidence_event.data["ignored_out_of_scope_evidence_fields"] == ["tests_passed"]
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_sibling_context_marks_siblings_out_of_scope(self) -> None:
        """Fat-harness sibling context must be a boundary, not an invitation."""
        event_store, _ = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "```json\n"
            '{"files_touched":["string_utils.py","test_slugify.py"],'
            '"commands_run":["python -m pytest test_slugify.py"],'
            '"tests_passed":["python -m pytest test_slugify.py"]}'
            "\n```",
            native_session_id="opencode-session-scope-boundary",
            support_messages=(
                AgentMessage(
                    type="tool",
                    content="Write string_utils.py",
                    tool_name="Write",
                    data={"tool_input": {"file_path": "string_utils.py"}},
                ),
                AgentMessage(
                    type="tool",
                    content="Write test_slugify.py",
                    tool_name="Write",
                    data={"tool_input": {"file_path": "test_slugify.py"}},
                ),
                AgentMessage(
                    type="tool",
                    content="Bash: python -m pytest test_slugify.py",
                    tool_name="Bash",
                    data={"tool_input": {"command": "python -m pytest test_slugify.py"}},
                ),
                AgentMessage(
                    type="result",
                    content="test_slugify.py passed",
                    data={"subtype": "success"},
                ),
            ),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create string_utils.py with slugify(text) and test_slugify.py.",
            session_id="orch_123",
            tools=["Read", "Write", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
            sibling_acs=[
                (0, "Create string_utils.py with slugify(text) and test_slugify.py."),
                (1, "Add truncate(text, max_length) and test_truncate.py."),
                (2, "Document slugify and truncate usage in README.md."),
            ],
        )

        assert result.success is True
        assert runtime.last_prompt is not None
        assert "## Current AC Scope Boundary" in runtime.last_prompt
        assert "outside the current dispatch" in runtime.last_prompt
        assert "Do not satisfy those criteria now" in runtime.last_prompt
        assert "do not pre-create their files, tests, docs, or evidence" in runtime.last_prompt
        assert "Sibling/future ACs are summarized in the governed sibling-status" in (
            runtime.last_prompt
        )
        assert "as out-of-scope boundary context" in runtime.last_prompt
        assert "Sibling tasks in progress" not in runtime.last_prompt

    @pytest.mark.asyncio
    async def test_fat_harness_accepts_validation_evidence_after_code_fence(self) -> None:
        """Regression for #978 batch 2b: parser must skip earlier code fences."""
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "[AC_COMPLETE: 1]\n\n"
                "`hello.py` contains:\n\n"
                "```python\n"
                "def hello():\n"
                '    return "hello"\n'
                "```\n\n"
                "Validation evidence:\n\n"
                "```json\n"
                "{\n"
                '  "files_touched": ["hello.py", "test_hello.py"],\n'
                '  "commands_run": ["pytest test_hello.py"],\n'
                '  "tests_passed": ["test_hello.py::test_hello"]\n'
                "}\n"
                "```",
                native_session_id="opencode-session-evidence-code-fence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Write: hello.py created",
                        tool_name="Write",
                        data={"tool_input": {"file_path": "hello.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Write: test_hello.py created",
                        tool_name="Write",
                        data={"tool_input": {"file_path": "test_hello.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest test_hello.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest test_hello.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="test_hello.py::test_hello passed",
                        data={"subtype": "success"},
                    ),
                ),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content='Create hello.py with hello() returning "hello".',
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        assert result.typed_evidence is not None
        assert result.typed_evidence.data["files_touched"] == ["hello.py", "test_hello.py"]
        assert result.typed_evidence_validation is not None
        assert result.typed_evidence_validation.ok is True
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.passed is True
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["typed_evidence_present"] is True
        assert evidence_event.data["typed_evidence_valid"] is True
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_accepts_codex_runtime_evidence_shape(
        self, tmp_path
    ) -> None:
        """Regression for #978 post-#1025: Codex emits abs paths and same-message output."""
        hello_file = tmp_path / "hello.py"
        test_file = tmp_path / "test_hello.py"
        hello_file.write_text('def hello():\n    return "hello"\n', encoding="utf-8")
        test_file.write_text(
            "from hello import hello\n\n"
            "def test_hello_returns_hello():\n"
            "    assert hello() == 'hello'\n",
            encoding="utf-8",
        )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["hello.py", "test_hello.py"],\n'
                '  "commands_run": ["pytest"],\n'
                '  "tests_passed": ["test_hello.py::test_hello_returns_hello"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-post-1025-observation",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {hello_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(hello_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {test_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(test_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: Bash: pytest",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": "pytest"},
                            "output": "1 passed in 0.01s",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content='Create hello.py with hello() returning "hello".',
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.passed is True
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["typed_evidence_present"] is True
        assert evidence_event.data["typed_evidence_valid"] is True
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_accepts_notebookedit_notebook_path(self, tmp_path) -> None:
        """NotebookEdit reports its target as notebook_path, not file_path."""
        notebook_file = tmp_path / "analysis.ipynb"
        notebook_file.write_text("{}\n", encoding="utf-8")
        test_file = tmp_path / "tests" / "test_analysis.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_analysis():\n    assert True\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["analysis.ipynb"],\n'
                '  "commands_run": ["pytest tests/test_analysis.py"],\n'
                '  "tests_passed": ["tests/test_analysis.py"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-notebook-path",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: NotebookEdit",
                        tool_name="NotebookEdit",
                        data={"tool_input": {"notebook_path": str(notebook_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: Bash: pytest tests/test_analysis.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_analysis.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_analysis.py passed; 1 passed",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Update notebook and tests.",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_bare_pytest_for_unmentioned_stale_test(
        self, tmp_path
    ) -> None:
        """A bare pytest success must not prove arbitrary existing test files."""
        generated_file = tmp_path / "src" / "generated.py"
        generated_file.parent.mkdir()
        generated_file.write_text("VALUE = 1\n", encoding="utf-8")
        stale_test = tmp_path / "tests" / "test_other.py"
        stale_test.parent.mkdir()
        stale_test.write_text("def test_other():\n    assert True\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["src/generated.py"],\n'
                '  "commands_run": ["pytest"],\n'
                '  "tests_passed": ["tests/test_other.py::test_other"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-bare-pytest-unrelated-test",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {generated_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(generated_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: Bash: pytest",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": "pytest"},
                            "output": "1 passed in 0.01s",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement generated module.",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "tests_passed: tests/test_other.py::test_other" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_rejects_command_result_wrapper_after_parsing_json_fence(
        self,
    ) -> None:
        """Actual #978 failing shape parses, then fails schema without verifier."""
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "[AC_COMPLETE: 1]\n\n"
                "```python\n"
                "def hello():\n"
                '    return "hello"\n'
                "```\n\n"
                "Validation evidence:\n\n"
                "```json\n"
                "{\n"
                '  "type": "command_result",\n'
                '  "command": "pytest test_hello.py",\n'
                '  "cwd": "/Users/jh0927/character-chat",\n'
                '  "exit_code": 0,\n'
                '  "result": "1 passed in 0.01s"\n'
                "}\n"
                "```",
                native_session_id="opencode-session-command-result-wrapper",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest test_hello.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest test_hello.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="1 passed in 0.01s",
                        data={"subtype": "success"},
                    ),
                ),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content='Create hello.py with hello() returning "hello".',
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.typed_evidence is not None
        assert result.typed_evidence.data["type"] == "command_result"
        assert result.typed_evidence_validation is not None
        assert result.typed_evidence_validation.ok is False
        assert result.typed_evidence_error is None
        assert "Fat-harness typed evidence validation failed" in (result.error or "")
        assert result.atomic_verifier_verdict is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["typed_evidence_present"] is True
        assert evidence_event.data["typed_evidence_valid"] is False
        assert evidence_event.data["typed_evidence_error"] is None
        assert evidence_event.data["missing_fields"] == [
            "files_touched",
            "commands_run",
            "tests_passed",
        ]
        assert evidence_event.data["verifier_ran"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_mode_rejects_missing_typed_evidence(self) -> None:
        """Fat-harness mode gates atomic success on profile evidence."""
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "[TASK_COMPLETE] no JSON evidence yet",
                native_session_id="opencode-session-no-evidence",
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "Evidence is not valid JSON" in result.error
        assert result.final_message.startswith("Evidence is not valid JSON")

        report = render_parallel_verification_report(
            ParallelExecutionResult(
                results=(result,),
                success_count=0,
                failure_count=1,
                total_messages=len(result.messages),
            ),
            total_acceptance_criteria=1,
        )
        assert "[FAILED]" in report
        assert "Evidence is not valid JSON" in report
        assert "Runtime final message:" in report

        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["observe_only"] is False
        assert evidence_event.data["enforced"] is True
        assert evidence_event.data["fat_harness_mode"] is True
        assert "Evidence is not valid JSON" in evidence_event.data["enforcement_error"]
        assert evidence_event.data["verifier_ran"] is False

        terminal_event = next(
            event for event in appended_events if event.type == "execution.session.failed"
        )
        assert "Evidence is not valid JSON" in terminal_event.data["error"]

    @pytest.mark.asyncio
    async def test_fat_harness_mode_accepts_valid_typed_evidence(self) -> None:
        """Valid profile evidence keeps the opt-in fat-harness leaf accepted."""
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/app.py"],'
                '"commands_run":["pytest"],'
                '"tests_passed":["tests/test_app.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Edit: src/app.py",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": "src/app.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_app.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_app.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_app.py passed",
                        data={"subtype": "success"},
                    ),
                ),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.passed is True
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["observe_only"] is False
        assert evidence_event.data["enforced"] is True
        assert evidence_event.data["fat_harness_mode"] is True
        assert evidence_event.data["enforcement_error"] is None
        assert evidence_event.data["typed_evidence_valid"] is True
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_validation_only_ac_accepts_no_files_touched(
        self,
    ) -> None:
        """Validation-only ACs can pass with command and test evidence only."""
        event_store, appended_events = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "Done.\n"
            "```json\n"
            '{"commands_run":["python -m unittest test_todo.py"],'
            '"tests_passed":["python -m unittest test_todo.py"]}\n'
            "```",
            native_session_id="opencode-session-validation-only",
            support_messages=(
                AgentMessage(
                    type="tool",
                    content="Bash: python -m unittest test_todo.py",
                    tool_name="Bash",
                    data={"tool_input": {"command": "python -m unittest test_todo.py"}},
                ),
                AgentMessage(
                    type="result",
                    content="Ran 6 tests in 0.002s\n\nOK",
                    data={"subtype": "success", "exit_code": 0},
                ),
            ),
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Run python -m unittest test_todo.py successfully.",
            session_id="orch_123",
            tools=["Read", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        assert runtime.last_prompt is not None
        assert "validation-only current AC" in runtime.last_prompt
        assert "Do not include files_touched unless you actually edited" in runtime.last_prompt
        assert "Read-only inspection or running tests does not count as files_touched" in (
            runtime.last_prompt
        )
        assert "commands_run, tests_passed" in runtime.last_prompt
        assert "files_touched, commands_run, tests_passed" not in runtime.last_prompt
        assert result.typed_evidence is not None
        assert result.typed_evidence.data == {
            "commands_run": ["python -m unittest test_todo.py"],
            "tests_passed": ["python -m unittest test_todo.py"],
        }
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == ["commands_run", "tests_passed"]
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_validation_only_ac_ignores_files_touched_overclaim(
        self,
    ) -> None:
        """Validation-only ACs record but ignore extra files_touched overclaims."""
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["test_todo.py"],'
                '"commands_run":["python -m unittest test_todo.py"],'
                '"tests_passed":["python -m unittest test_todo.py"]}\n'
                "```",
                native_session_id="opencode-session-validation-only-overclaim",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: sed -n '1,240p' test_todo.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "sed -n '1,240p' test_todo.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: python -m unittest test_todo.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "python -m unittest test_todo.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="Ran 6 tests in 0.002s\n\nOK",
                        data={"subtype": "success", "exit_code": 0},
                    ),
                ),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Run python -m unittest test_todo.py successfully.",
            session_id="orch_123",
            tools=["Read", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        assert result.typed_evidence is not None
        assert result.typed_evidence.data == {
            "commands_run": ["python -m unittest test_todo.py"],
            "tests_passed": ["python -m unittest test_todo.py"],
        }
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == ["commands_run", "tests_passed"]
        assert evidence_event.data["ignored_out_of_scope_evidence_fields"] == ["files_touched"]
        assert evidence_event.data["ignored_out_of_scope_evidence"] == {
            "files_touched": ["test_todo.py"]
        }
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_test_writing_ac_still_requires_files_touched(
        self,
    ) -> None:
        """Test-writing ACs must still prove file mutation."""
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"commands_run":["python -m unittest test_todo.py"],'
                '"tests_passed":["python -m unittest test_todo.py"]}\n'
                "```",
                native_session_id="opencode-session-test-writing-missing-file",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: python -m unittest test_todo.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "python -m unittest test_todo.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="Ran 6 tests in 0.002s\n\nOK",
                        data={"subtype": "success", "exit_code": 0},
                    ),
                ),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create test_todo.py with unittest coverage.",
            session_id="orch_123",
            tools=["Read", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "missing fields: files_touched" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == [
            "files_touched",
            "commands_run",
            "tests_passed",
        ]
        assert "files_touched" in evidence_event.data["missing_fields"]

    @pytest.mark.asyncio
    async def test_fat_harness_mode_rejects_unbacked_typed_evidence(self) -> None:
        """Default verifier rejects final-message-only self-reported evidence."""
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/app.py"],'
                '"commands_run":["pytest"],'
                '"tests_passed":["tests/test_app.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "Fat-harness verifier failed" in result.error
        assert "no runtime transcript evidence supports" in result.error

        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["typed_evidence_valid"] is True
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is False
        assert evidence_event.data["verifier_failure_class"] == "EVIDENCE_MISSING"

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_allows_bash_generated_file_and_whole_suite_test(
        self, tmp_path
    ) -> None:
        """Bash-backed generation plus whole-suite pytest can support evidence."""
        generated_file = tmp_path / "src" / "generated.py"
        generated_file.parent.mkdir()
        generated_file.write_text("VALUE = 1\n", encoding="utf-8")
        generated_test = tmp_path / "tests" / "test_generated.py"
        generated_test.parent.mkdir()
        generated_test.write_text("def test_generated():\n    assert True\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/generated.py"],'
                '"commands_run":["python scripts/generate.py","pytest"],'
                '"tests_passed":["tests/test_generated.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: python scripts/generate.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "python scripts/generate.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest"}},
                    ),
                    AgentMessage(
                        type="result",
                        content=(
                            "generated.py updated; tests/test_generated.py passed; "
                            "0 failed, 0 errors, 1 passed"
                        ),
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_normalizes_workspace_absolute_file_claim(
        self, tmp_path
    ) -> None:
        """Absolute files_touched claims under task_cwd are normalized before matching."""
        touched_file = tmp_path / "test_todo.py"
        touched_file.write_text("import unittest\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                f'{{"files_touched":["{touched_file}"],'
                '"commands_run":["python -m unittest test_todo.py"],'
                '"tests_passed":["python -m unittest test_todo.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content=f"Edit {touched_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(touched_file)}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: python -m unittest test_todo.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "python -m unittest test_todo.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="Ran 1 test in 0.001s\n\nOK",
                        data={"subtype": "success", "exit_code": 0},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_unscoped_file_and_failed_test_command(
        self, tmp_path
    ) -> None:
        """Workspace path scope and test success are required for verifier support."""
        outside_file = tmp_path.parent / "outside.py"
        outside_file.write_text("VALUE = 1\n", encoding="utf-8")
        test_file = tmp_path / "tests" / "test_generated.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_generated():\n    assert False\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                f'{{"files_touched":["{outside_file}"],'
                '"commands_run":["pytest"],'
                '"tests_passed":["tests/test_generated.py"]}}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="1 failed, 3 passed",
                        data={"subtype": "success"},
                    ),
                ),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "files_touched:" in result.error
        assert "tests_passed: tests/test_generated.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_preexisting_file_without_transcript_support(
        self, tmp_path
    ) -> None:
        """A stale workspace file must not prove this run touched that file."""
        preexisting_file = tmp_path / "src" / "preexisting.py"
        preexisting_file.parent.mkdir()
        preexisting_file.write_text("VALUE = 1\n", encoding="utf-8")
        test_file = tmp_path / "tests" / "test_preexisting.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_preexisting():\n    assert True\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/preexisting.py"],'
                '"commands_run":["pytest tests/test_preexisting.py"],'
                '"tests_passed":["tests/test_preexisting.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="result",
                        content="Read src/preexisting.py for context only.",
                        data={"subtype": "success"},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_preexisting.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_preexisting.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_preexisting.py passed",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "files_touched: src/preexisting.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_read_only_file_reference(self, tmp_path) -> None:
        """Mentioning a path in a read-only command is not files_touched proof."""
        preexisting_file = tmp_path / "src" / "preexisting.py"
        preexisting_file.parent.mkdir()
        preexisting_file.write_text("VALUE = 1\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/preexisting.py"],'
                '"commands_run":["pytest tests/test_preexisting.py"],'
                '"tests_passed":["tests/test_preexisting.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: cat src/preexisting.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "cat src/preexisting.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_preexisting.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_preexisting.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_preexisting.py passed",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "files_touched: src/preexisting.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_read_only_bash_command_with_write_word(
        self, tmp_path
    ) -> None:
        """Read-only Bash command text cannot prove files_touched via mutation words."""
        preexisting_file = tmp_path / "src" / "preexisting.py"
        preexisting_file.parent.mkdir()
        preexisting_file.write_text("VALUE = 1\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/preexisting.py"],'
                '"commands_run":["grep updated src/preexisting.py"],'
                '"tests_passed":["tests/test_preexisting.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: grep updated src/preexisting.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "grep updated src/preexisting.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_preexisting.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_preexisting.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_preexisting.py passed",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "files_touched: src/preexisting.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        (
            "touch src/generated.py",
            "printf 'VALUE = 1' > src/generated.py",
            "sed -i '' 's/1/2/' src/generated.py",
        ),
    )
    async def test_fat_harness_verifier_allows_explicit_bash_file_mutation_without_output(
        self, tmp_path, command
    ) -> None:
        """Explicit shell writes can prove files_touched even without path-specific output."""
        generated_file = tmp_path / "src" / "generated.py"
        generated_file.parent.mkdir()
        generated_file.write_text("VALUE = 1\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                f'{{"files_touched":["src/generated.py"],'
                f'"commands_run":["{command}","pytest tests/test_generated.py"],'
                '"tests_passed":["tests/test_generated.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content=f"Bash: {command}",
                        tool_name="Bash",
                        data={"tool_input": {"command": command}},
                    ),
                    AgentMessage(
                        type="result",
                        content="command completed with exit code 0",
                        data={"subtype": "success", "exit_code": 0},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_generated.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_generated.py passed",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_bash_mutation_of_different_file(
        self, tmp_path
    ) -> None:
        """A mutating Bash pipeline must not prove a separately read file was touched."""
        preexisting_file = tmp_path / "src" / "preexisting.py"
        generated_file = tmp_path / "src" / "generated.py"
        preexisting_file.parent.mkdir()
        preexisting_file.write_text("VALUE = 1\n", encoding="utf-8")
        generated_file.write_text("VALUE = 2\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/preexisting.py"],'
                '"commands_run":["cat src/preexisting.py | tee src/generated.py",'
                '"pytest tests/test_generated.py"],'
                '"tests_passed":["tests/test_generated.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: cat src/preexisting.py | tee src/generated.py",
                        tool_name="Bash",
                        data={
                            "tool_input": {
                                "command": "cat src/preexisting.py | tee src/generated.py"
                            }
                        },
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_generated.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_generated.py passed",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "files_touched: src/preexisting.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_failed_explicit_bash_file_mutation(
        self, tmp_path
    ) -> None:
        """An explicit shell write command must also have a successful result."""
        generated_file = tmp_path / "src" / "generated.py"
        generated_file.parent.mkdir()

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/generated.py"],'
                '"commands_run":["touch src/generated.py","pytest tests/test_generated.py"],'
                '"tests_passed":["tests/test_generated.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: touch src/generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "touch src/generated.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="touch: src/generated.py: permission denied",
                        data={"subtype": "error", "exit_code": 1},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_generated.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_generated.py passed",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "files_touched: src/generated.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_bash_command_basename_fallback(
        self, tmp_path
    ) -> None:
        """Bash command-text proof must not use basename fallback for another path."""
        generated_file = tmp_path / "src" / "generated.py"
        generated_file.parent.mkdir()
        generated_file.write_text("VALUE = 1\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/generated.py"],'
                '"commands_run":["touch generated.py","pytest tests/test_generated.py"],'
                '"tests_passed":["tests/test_generated.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: touch generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "touch generated.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="command completed with exit code 0",
                        data={"subtype": "success", "exit_code": 0},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_generated.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_generated.py passed",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "files_touched: src/generated.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_accepts_exit_code_only_test_success(self, tmp_path) -> None:
        """Regression for #978 observation: Codex may omit pytest stdout but keep exit_code=0."""
        hello_file = tmp_path / "hello.py"
        test_file = tmp_path / "test_hello.py"
        hello_file.write_text('def hello():\n    return "hello"\n', encoding="utf-8")
        test_file.write_text(
            "from hello import hello\n\n"
            "def test_hello_returns_hello():\n"
            "    assert hello() == 'hello'\n",
            encoding="utf-8",
        )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["hello.py", "test_hello.py"],\n'
                '  "commands_run": ["python -m pytest test_hello.py"],\n'
                '  "tests_passed": ["test_hello.py::test_hello_returns_hello"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-exit-code-only-pytest",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {hello_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(hello_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {test_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(test_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: Bash: /bin/zsh -lc 'python -m pytest test_hello.py'",
                        tool_name="Bash",
                        data={
                            "tool_input": {
                                "command": "/bin/zsh -lc 'python -m pytest test_hello.py'"
                            },
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content='Create hello.py with hello() returning "hello".',
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.passed is True
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_accepts_unittest_command_summary_claim(
        self, tmp_path
    ) -> None:
        """Regression for #961: Codex may put unittest command + OK summary in tests_passed."""
        source_file = tmp_path / "string_utils.py"
        test_file = tmp_path / "test_slugify.py"
        source_file.write_text(
            "def slugify(text):\n    return text.lower().replace(' ', '-')\n",
            encoding="utf-8",
        )
        test_file.write_text(
            "import unittest\n\n"
            "from string_utils import slugify\n\n"
            "class SlugifyTest(unittest.TestCase):\n"
            "    def test_slugify(self):\n"
            "        self.assertEqual(slugify('Hello World'), 'hello-world')\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["string_utils.py", "test_slugify.py"],\n'
                '  "commands_run": ["python -m unittest test_slugify.py"],\n'
                '  "tests_passed": ['
                '"python -m unittest test_slugify.py: Ran 4 tests in 0.000s OK"'
                "]\n"
                "}\n"
                "```",
                native_session_id="codex-session-unittest-summary-claim",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {source_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(source_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {test_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(test_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: Bash: python -m unittest test_slugify.py",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": "python -m unittest test_slugify.py"},
                            "output": "Ran 4 tests in 0.000s\n\nOK",
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create slugify and unittest coverage.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_accepts_unittest_command_bare_ok_claim(
        self, tmp_path
    ) -> None:
        """A backed unittest command plus bare OK can rely on real Bash unittest output."""
        source_file = tmp_path / "string_utils.py"
        test_file = tmp_path / "test_slugify.py"
        source_file.write_text(
            "def slugify(text):\n    return text.lower().replace(' ', '-')\n",
            encoding="utf-8",
        )
        test_file.write_text(
            "import unittest\n\n"
            "from string_utils import slugify\n\n"
            "class SlugifyTest(unittest.TestCase):\n"
            "    def test_slugify_spaces(self):\n"
            "        self.assertEqual(slugify('Hello World'), 'hello-world')\n"
            "    def test_slugify_lowercase(self):\n"
            "        self.assertEqual(slugify('Already Lower'), 'already-lower')\n"
            "    def test_slugify_empty(self):\n"
            "        self.assertEqual(slugify(''), '')\n"
            "    def test_slugify_one_word(self):\n"
            "        self.assertEqual(slugify('Hello'), 'hello')\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["string_utils.py", "test_slugify.py"],\n'
                '  "commands_run": ["python -m unittest test_slugify.py"],\n'
                '  "tests_passed": ["python -m unittest test_slugify.py: OK"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-unittest-bare-ok-claim",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {source_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(source_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {test_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(test_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: Bash: python -m unittest test_slugify.py",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": "python -m unittest test_slugify.py"},
                            "output": "Ran 4 tests in 0.000s\n\nOK",
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create slugify and unittest coverage.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_accepts_shell_wrapped_unittest_bare_ok_claim(
        self, tmp_path
    ) -> None:
        """Shell-wrapped unittest commands can back concise unittest claims."""
        source_file = tmp_path / "string_utils.py"
        test_file = tmp_path / "test_slugify.py"
        source_file.write_text(
            "def slugify(text):\n    return text.lower().replace(' ', '-')\n",
            encoding="utf-8",
        )
        test_file.write_text(
            "import unittest\n\n"
            "from string_utils import slugify\n\n"
            "class SlugifyTest(unittest.TestCase):\n"
            "    def test_slugify_spaces(self):\n"
            "        self.assertEqual(slugify('Hello World'), 'hello-world')\n"
            "    def test_slugify_lowercase(self):\n"
            "        self.assertEqual(slugify('Already Lower'), 'already-lower')\n"
            "    def test_slugify_empty(self):\n"
            "        self.assertEqual(slugify(''), '')\n"
            "    def test_slugify_one_word(self):\n"
            "        self.assertEqual(slugify('Hello'), 'hello')\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )

        shell_command = "/bin/zsh -lc 'python -m unittest \"test_slugify.py\"'"
        escaped_shell_command = shell_command.replace('"', '\\"')
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["string_utils.py", "test_slugify.py"],\n'
                f'  "commands_run": ["{escaped_shell_command}"],\n'
                '  "tests_passed": ["python -m unittest test_slugify.py: OK"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-shell-wrapped-unittest-bare-ok",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {source_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(source_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {test_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(test_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Bash: {shell_command}",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": shell_command},
                            "output": "Ran 4 tests in 0.000s\n\nOK",
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create slugify and unittest coverage.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_accepts_inner_unittest_claim_for_shell_wrapped_cd_command(
        self, tmp_path
    ) -> None:
        """Codex shell wrappers may run setup before the claimed inner unittest command."""
        source_file = tmp_path / "string_utils.py"
        test_file = tmp_path / "test_slugify.py"
        source_file.write_text(
            "def slugify(text):\n    return text.lower().replace(' ', '-')\n",
            encoding="utf-8",
        )
        test_file.write_text(
            "import unittest\n\n"
            "from string_utils import slugify\n\n"
            "class SlugifyTest(unittest.TestCase):\n"
            "    def test_slugify_spaces(self):\n"
            "        self.assertEqual(slugify('Hello World'), 'hello-world')\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )

        inner_command = "python -m unittest test_slugify.py"
        shell_command = f"/bin/bash --noprofile --norc -lc 'cd {tmp_path} && python -m unittest \"test_slugify.py\"'"
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["string_utils.py", "test_slugify.py"],\n'
                f'  "commands_run": ["{inner_command}"],\n'
                f'  "tests_passed": ["{inner_command}: OK"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-shell-wrapped-cd-unittest-inner-claim",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {source_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(source_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {test_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(test_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Bash: {shell_command}",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": shell_command},
                            "output": "Ran 1 test in 0.000s\n\nOK",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create slugify and unittest coverage.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_accepts_inner_unittest_claim_for_shell_wrapped_export_command(
        self, tmp_path
    ) -> None:
        """Shell env setup preambles may precede the claimed inner unittest command."""
        source_file = tmp_path / "string_utils.py"
        test_file = tmp_path / "test_slugify.py"
        source_file.write_text(
            "def slugify(text):\n    return text.lower().replace(' ', '-')\n",
            encoding="utf-8",
        )
        test_file.write_text(
            "import unittest\n\n"
            "from string_utils import slugify\n\n"
            "class SlugifyTest(unittest.TestCase):\n"
            "    def test_slugify_spaces(self):\n"
            "        self.assertEqual(slugify('Hello World'), 'hello-world')\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n",
            encoding="utf-8",
        )

        inner_command = "python -m unittest test_slugify.py"
        shell_command = (
            f"/bin/zsh -lc 'export PYTHONPATH={tmp_path} && python -m unittest \"test_slugify.py\"'"
        )
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["string_utils.py", "test_slugify.py"],\n'
                f'  "commands_run": ["{inner_command}"],\n'
                f'  "tests_passed": ["{inner_command}: OK"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-shell-wrapped-export-unittest-inner-claim",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {source_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(source_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {test_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(test_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Bash: {shell_command}",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": shell_command},
                            "output": "Ran 1 test in 0.000s\n\nOK",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create slugify and unittest coverage.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_shell_wrapped_unittest_summary_missing_from_runtime(
        self, tmp_path
    ) -> None:
        """Shell wrappers must not let assistant prose prove a unittest summary."""
        source_file = tmp_path / "string_utils.py"
        test_file = tmp_path / "test_slugify.py"
        source_file.write_text("def slugify(text):\n    return text\n", encoding="utf-8")
        test_file.write_text("import unittest\n", encoding="utf-8")

        shell_command = "/bin/zsh -lc 'python -m unittest \"test_slugify.py\"'"
        escaped_shell_command = shell_command.replace('"', '\\"')
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["string_utils.py", "test_slugify.py"],\n'
                f'  "commands_run": ["{escaped_shell_command}"],\n'
                '  "tests_passed": ["python -m unittest test_slugify.py: Ran 4 tests in 0.000s OK"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-shell-wrapped-unittest-invented-summary",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {source_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(source_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {test_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(test_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Bash: {shell_command}",
                        tool_name="Bash",
                        data={"tool_input": {"command": shell_command}, "exit_code": 0},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=(
                            "Tests passed: python -m unittest test_slugify.py: "
                            "Ran 4 tests in 0.000s OK"
                        ),
                        data={},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create slugify and unittest coverage.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "tests_passed:" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_failure_class"] == "FABRICATION_SUSPECTED"

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_accepts_pytest_node_id_claim_backed_by_transcript_command(
        self, tmp_path
    ) -> None:
        """A transcript ``pytest <file>`` run backs node-id ``tests_passed`` claims.

        Regression: candidate test commands were sourced only from
        ``commands_run`` evidence. When the agent listed lint in
        ``commands_run`` but ran ``pytest`` (recorded in the transcript) without
        echoing it into ``commands_run``, every node-id ``tests_passed`` claim
        was rejected as FABRICATION_SUSPECTED even though the run is real and
        green. The Bash message's own command is now also a candidate, so a
        transcript-proven test run supports the claim.
        """
        source_file = tmp_path / "string_utils.py"
        test_file = tmp_path / "test_slugify.py"
        source_file.write_text(
            "def slugify(text):\n    return text.lower().replace(' ', '-')\n",
            encoding="utf-8",
        )
        test_file.write_text(
            "from string_utils import slugify\n\n"
            "def test_spaces():\n"
            "    assert slugify('Hello World') == 'hello-world'\n",
            encoding="utf-8",
        )

        lint_command = "python -m ruff check string_utils.py test_slugify.py"
        pytest_command = "python -m pytest test_slugify.py -q"
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["string_utils.py", "test_slugify.py"],\n'
                f'  "commands_run": ["{lint_command}"],\n'
                '  "tests_passed": ["test_slugify.py::test_spaces"]\n'
                "}\n"
                "```",
                native_session_id="session-pytest-node-id-transcript-only",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {source_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(source_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {test_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(test_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Bash: {lint_command}",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": lint_command},
                            "output": "All checks passed!",
                            "exit_code": 0,
                        },
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Bash: {pytest_command}",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": pytest_command},
                            "output": "1 passed in 0.01s",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create slugify and pytest coverage.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_node_id_claim_backed_only_by_non_test_command(
        self, tmp_path
    ) -> None:
        """The new candidate source must not let a non-test command back a test claim.

        Guards the message-command candidate path added for node-id ``tests_passed``
        support: a Bash message whose command merely prints a fake success line
        (``cat fake_results.txt`` whose output is ``test_x.py::test_y passed``) is
        not a test command, so ``_looks_like_test_command`` must exclude it and the
        node-id claim stays unsupported (FABRICATION_SUSPECTED) — even though the
        recorded output literally contains the node-id-plus-"passed" marker.
        """
        source_file = tmp_path / "string_utils.py"
        source_file.write_text("def slugify(text):\n    return text\n", encoding="utf-8")

        fake_command = "cat fake_results.txt"
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["string_utils.py"],\n'
                f'  "commands_run": ["{fake_command}"],\n'
                '  "tests_passed": ["test_slugify.py::test_spaces"]\n'
                "}\n"
                "```",
                native_session_id="session-node-id-non-test-command-only",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {source_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(source_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Bash: {fake_command}",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": fake_command},
                            "output": "test_slugify.py::test_spaces passed",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create slugify and pytest coverage.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "tests_passed:" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_failure_class"] == "FABRICATION_SUSPECTED"

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_unittest_summary_missing_from_runtime(
        self, tmp_path
    ) -> None:
        """A tests_passed summary must be backed by runtime output, not claim text."""
        source_file = tmp_path / "string_utils.py"
        test_file = tmp_path / "test_slugify.py"
        source_file.write_text("def slugify(text):\n    return text\n", encoding="utf-8")
        test_file.write_text("import unittest\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["string_utils.py", "test_slugify.py"],\n'
                '  "commands_run": ["python -m unittest test_slugify.py"],\n'
                '  "tests_passed": ['
                '"python -m unittest test_slugify.py: Ran 4 tests in 0.000s OK"'
                "]\n"
                "}\n"
                "```",
                native_session_id="codex-session-unittest-invented-summary",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {source_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(source_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {test_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(test_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: Bash: python -m unittest test_slugify.py",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": "python -m unittest test_slugify.py"},
                            "exit_code": 0,
                        },
                    ),
                    AgentMessage(
                        type="assistant",
                        content=(
                            "Tests passed: python -m unittest test_slugify.py: "
                            "Ran 4 tests in 0.000s OK"
                        ),
                        data={},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create slugify and unittest coverage.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "tests_passed:" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_failure_class"] == "FABRICATION_SUSPECTED"

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_bare_unittest_word_as_test_command(
        self, tmp_path
    ) -> None:
        """Commands merely mentioning unittest must not back tests_passed."""
        source_file = tmp_path / "string_utils.py"
        source_file.write_text("def slugify(text):\n    return text\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["string_utils.py"],\n'
                '  "commands_run": ["echo unittest docs"],\n'
                '  "tests_passed": ["unittest docs"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-bare-unittest-word",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {source_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(source_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: Bash: echo unittest docs",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": "echo unittest docs"},
                            "output": "unittest docs\nsuccess",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create slugify and unittest coverage.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "tests_passed: unittest docs" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_failure_class"] == "FABRICATION_SUSPECTED"

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_echoed_unittest_command_as_test_command(
        self, tmp_path
    ) -> None:
        """Echoing a unittest command string must not count as running unittest."""
        source_file = tmp_path / "string_utils.py"
        source_file.write_text("def slugify(text):\n    return text\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["string_utils.py"],\n'
                '  "commands_run": ["echo python -m unittest test_slugify.py"],\n'
                '  "tests_passed": ["python -m unittest test_slugify.py: OK"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-echoed-unittest-command",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {source_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(source_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Calling tool: Bash: echo python -m unittest test_slugify.py",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": "echo python -m unittest test_slugify.py"},
                            "output": "python -m unittest test_slugify.py\nsuccess\nRan 4 tests in 0.000s\n\nOK",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create slugify and unittest coverage.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "tests_passed:" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_failure_class"] == "FABRICATION_SUSPECTED"

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_echoed_shell_wrapped_unittest_command(
        self, tmp_path
    ) -> None:
        """Echoing a shell-wrapped unittest command must not count as running it."""
        source_file = tmp_path / "string_utils.py"
        source_file.write_text("def slugify(text):\n    return text\n", encoding="utf-8")

        shell_command = "echo /bin/zsh -lc 'python -m unittest \"test_slugify.py\"'"
        escaped_shell_command = shell_command.replace('"', '\\"')
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "```json\n"
                "{\n"
                '  "files_touched": ["string_utils.py"],\n'
                f'  "commands_run": ["{escaped_shell_command}"],\n'
                '  "tests_passed": ["python -m unittest test_slugify.py: OK"]\n'
                "}\n"
                "```",
                native_session_id="codex-session-echoed-shell-wrapped-unittest-command",
                support_messages=(
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Edit: {source_file}",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": str(source_file)}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content=f"Calling tool: Bash: {shell_command}",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": shell_command},
                            "output": "/bin/zsh -lc 'python -m unittest \"test_slugify.py\"'\nsuccess\nRan 4 tests in 0.000s\n\nOK",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create slugify and unittest coverage.",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship string utilities",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "tests_passed:" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_failure_class"] == "FABRICATION_SUSPECTED"

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_accepts_wrapped_broad_pytest_for_current_test_file(
        self, tmp_path
    ) -> None:
        """Wrapped bare pytest should behave like unwrapped broad pytest for current files."""
        source_file = tmp_path / "src" / "generated.py"
        source_file.parent.mkdir()
        source_file.write_text("VALUE = 1\n", encoding="utf-8")
        test_file = tmp_path / "tests" / "test_generated.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_generated():\n    assert True\n", encoding="utf-8")

        shell_command = f"/bin/zsh -lc 'cd {tmp_path} && pytest'"
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/generated.py", "tests/test_generated.py"],'
                f'"commands_run":["{shell_command}"],'
                '"tests_passed":["tests/test_generated.py"]}\n'
                "```",
                native_session_id="codex-session-wrapped-broad-pytest",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Edit: src/generated.py",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": "src/generated.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Edit: tests/test_generated.py",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": "tests/test_generated.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content=f"Bash: {shell_command}",
                        tool_name="Bash",
                        data={
                            "tool_input": {"command": shell_command},
                            "output": "tests/test_generated.py passed\n1 passed in 0.01s",
                            "exit_code": 0,
                        },
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.error is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_test_not_covered_by_success_chunk(
        self, tmp_path
    ) -> None:
        """A successful test command must cover the claimed tests_passed entry."""
        touched_file = tmp_path / "src" / "generated.py"
        touched_file.parent.mkdir()
        touched_file.write_text("VALUE = 1\n", encoding="utf-8")
        test_a = tmp_path / "tests" / "test_a.py"
        test_b = tmp_path / "tests" / "test_b.py"
        test_a.parent.mkdir()
        test_a.write_text("def test_a():\n    assert True\n", encoding="utf-8")
        test_b.write_text("def test_b():\n    assert True\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/generated.py"],'
                '"commands_run":["pytest tests/test_a.py"],'
                '"tests_passed":["tests/test_b.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_a.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_a.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_a.py passed",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "tests_passed: tests/test_b.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_zero_passed_test_output(self, tmp_path) -> None:
        """A zero-passed test run is not proof for a claimed passing test."""
        touched_file = tmp_path / "src" / "generated.py"
        touched_file.parent.mkdir()
        touched_file.write_text("VALUE = 1\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/generated.py"],'
                '"commands_run":["pytest tests/test_generated.py"],'
                '"tests_passed":["tests/test_generated.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Edit: src/generated.py",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": "src/generated.py"}},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_generated.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_generated.py collected, 0 passed, 0 failed",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "tests_passed: tests/test_generated.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_verifier_rejects_targeted_failed_test_command(
        self, tmp_path
    ) -> None:
        """A targeted test command mentioning the claim is not proof without success."""
        touched_file = tmp_path / "src" / "generated.py"
        touched_file.parent.mkdir()
        touched_file.write_text("VALUE = 1\n", encoding="utf-8")
        test_file = tmp_path / "tests" / "test_generated.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_generated():\n    assert False\n", encoding="utf-8")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/generated.py"],'
                '"commands_run":["pytest tests/test_generated.py"],'
                '"tests_passed":["tests/test_generated.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
                support_messages=(
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_generated.py"}},
                    ),
                    AgentMessage(
                        type="result",
                        content="tests/test_generated.py failed",
                        data={"subtype": "success"},
                    ),
                ),
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "tests_passed: tests/test_generated.py" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_passed"] is False

    @pytest.mark.asyncio
    async def test_fat_harness_mode_rejects_verifier_fail(self) -> None:
        """Fat harness requires a separate verifier PASS after typed evidence."""

        def _rejecting_verifier(**kwargs: object) -> VerifierVerdict:
            del kwargs
            return VerifierVerdict(
                passed=False,
                reasons=("claimed test command did not support the AC",),
                failure_class="FABRICATION_SUSPECTED",
            )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/app.py"],'
                '"commands_run":["pytest"],'
                '"tests_passed":["tests/test_app.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            atomic_verifier=_rejecting_verifier,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "Fat-harness verifier failed" in result.error
        assert "claimed test command did not support the AC" in result.error
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.passed is False

        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["typed_evidence_valid"] is True
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is False
        assert evidence_event.data["verifier_failure_class"] == "FABRICATION_SUSPECTED"
        assert evidence_event.data["verifier_reasons"] == [
            "claimed test command did not support the AC"
        ]

    @pytest.mark.asyncio
    async def test_fat_harness_mode_surfaces_operational_verifier_error(self) -> None:
        """Operational verifier failures remain typed verifier rejections."""

        def _timeout_verifier(**kwargs: object) -> VerifierVerdict:
            del kwargs
            raise TimeoutError("verifier timed out")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/app.py"],'
                '"commands_run":["pytest"],'
                '"tests_passed":["tests/test_app.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            fat_harness_mode=True,
            atomic_verifier=_timeout_verifier,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "verifier raised TimeoutError: verifier timed out" in result.error
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.failure_class == "STALL"

        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is False
        assert evidence_event.data["verifier_failure_class"] == "STALL"
        assert evidence_event.data["verifier_reasons"] == [
            "verifier raised TimeoutError: verifier timed out"
        ]

    @pytest.mark.asyncio
    async def test_observe_only_mode_does_not_run_injected_verifier(self) -> None:
        """Non-enforced profile evidence telemetry must stay observe-only."""

        def _raising_verifier(**kwargs: object) -> VerifierVerdict:
            del kwargs
            raise AssertionError("observe-only mode must not invoke the verifier")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "Done.\n"
                "```json\n"
                '{"files_touched":["src/app.py"],'
                '"commands_run":["pytest"],'
                '"tests_passed":["tests/test_app.py"]}\n'
                "```",
                native_session_id="opencode-session-evidence",
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
            atomic_verifier=_raising_verifier,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.atomic_verifier_verdict is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["verifier_ran"] is False

    @pytest.mark.asyncio
    async def test_atomic_ac_typed_evidence_event_failure_does_not_fail_success(self) -> None:
        """Observe-only typed-evidence telemetry must not change AC success."""

        class _StubImplementationRuntime:
            _runtime_handle_backend = "opencode"
            _cwd = "/tmp/project"
            _permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                del prompt, tools, system_prompt, resume_session_id
                yield AgentMessage(
                    type="result",
                    content=(
                        "Done.\n"
                        "```json\n"
                        '{"files_touched":["src/app.py"],'
                        '"commands_run":["pytest"],'
                        '"tests_passed":["tests/test_app.py"]}\n'
                        "```"
                    ),
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend=resume_handle.backend if resume_handle is not None else "opencode",
                        kind=resume_handle.kind
                        if resume_handle is not None
                        else "implementation_session",
                        native_session_id="opencode-session-evidence",
                        cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        event_store, appended_events = _make_replaying_event_store()
        original_append = event_store.append

        async def _append(event: BaseEvent) -> None:
            if event.type == "execution.ac.typed_evidence.observed":
                raise RuntimeError("typed evidence telemetry failed")
            await original_append(event)

        event_store.append = AsyncMock(side_effect=_append)
        executor = ParallelACExecutor(
            adapter=_StubImplementationRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("code"),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is True
        assert result.typed_evidence is not None
        assert all(
            event.type != "execution.ac.typed_evidence.observed" for event in appended_events
        )

    @pytest.mark.asyncio
    async def test_atomic_ac_profile_evidence_config_error_remains_loud(self) -> None:
        """Profile-authored evidence-schema bugs must not be downgraded to telemetry."""

        class _StubImplementationRuntime:
            _runtime_handle_backend = "opencode"
            _cwd = "/tmp/project"
            _permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                del prompt, tools, system_prompt, resume_session_id
                yield AgentMessage(
                    type="result",
                    content=(
                        "Done.\n"
                        "```json\n"
                        '{"files_touched":["src/app.py"],'
                        '"commands_run":["pytest"],'
                        '"tests_passed":["tests/test_app.py"]}\n'
                        "```"
                    ),
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend=resume_handle.backend if resume_handle is not None else "opencode",
                        kind=resume_handle.kind
                        if resume_handle is not None
                        else "implementation_session",
                        native_session_id="opencode-session-evidence",
                        cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        profile = load_profile("code").model_copy(
            update={
                "evidence_schema": EvidenceSchema(
                    required=("files_touched", "commands_run", "tests_passed"),
                    rejected_if=("tests_passed != []",),
                )
            }
        )
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_StubImplementationRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=profile,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error is not None
        assert "Unsupported rejected_if expression" in result.error
        assert "execution.session.completed" not in {event.type for event in appended_events}
        assert "execution.session.failed" in {event.type for event in appended_events}

    @pytest.mark.asyncio
    async def test_remembered_runtime_handle_preserves_live_controls(self) -> None:
        """AC-scope rebinding should preserve live observe/terminate callbacks."""
        executor = _make_executor()
        control_calls = {"observe": 0, "terminate": 0}

        async def _observe(handle: RuntimeHandle) -> dict[str, object]:
            control_calls["observe"] += 1
            snapshot = handle.snapshot()
            snapshot["observed"] = True
            return snapshot

        async def _terminate(_handle: RuntimeHandle) -> bool:
            control_calls["terminate"] += 1
            return True

        rebound = executor._remember_ac_runtime_handle(
            0,
            RuntimeHandle(
                backend="opencode",
                kind="implementation_session",
                native_session_id="oc-session-1",
                metadata={"server_session_id": "server-1"},
            ).bind_controls(
                observe_callback=_observe,
                terminate_callback=_terminate,
            ),
            execution_context_id="orch_ctrl",
        )

        assert rebound is not None
        assert rebound.metadata["session_scope_id"] == "orch_ctrl_ac_1"
        assert rebound.can_terminate is True

        observed = await rebound.observe()
        assert observed["observed"] is True
        assert observed["control_session_id"] == "server-1"
        assert await rebound.terminate() is True
        assert control_calls == {"observe": 1, "terminate": 1}

    @pytest.mark.asyncio
    async def test_completed_ac_attempt_does_not_reuse_cached_runtime_handle(self) -> None:
        """Terminal AC attempts should drop the cached session before the next invocation."""

        class _StubResumeRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                native_session_id = f"opencode-session-{len(self.calls)}"
                bound_handle = RuntimeHandle(
                    backend=resume_handle.backend if resume_handle is not None else "opencode",
                    kind=resume_handle.kind
                    if resume_handle is not None
                    else "implementation_session",
                    native_session_id=native_session_id,
                    cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                    approval_mode=(
                        resume_handle.approval_mode if resume_handle is not None else "acceptEdits"
                    ),
                    metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=bound_handle,
                )

        runtime = _StubResumeRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )

        first_attempt = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Implement AC 2",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )
        resumed_attempt = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Implement AC 2",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        first_handle = runtime.calls[0]["resume_handle"]
        second_handle = runtime.calls[1]["resume_handle"]
        assert isinstance(first_handle, RuntimeHandle)
        assert isinstance(second_handle, RuntimeHandle)
        assert first_handle.native_session_id is None
        assert second_handle.native_session_id is None
        assert second_handle.metadata["session_scope_id"] == "orch_123_ac_2"
        assert second_handle.metadata["retry_attempt"] == 0
        assert second_handle.metadata["session_attempt_id"] == "orch_123_ac_2_attempt_1"
        assert first_attempt.runtime_handle is not None
        assert resumed_attempt.runtime_handle is not None
        assert first_attempt.runtime_handle.native_session_id == "opencode-session-1"
        assert resumed_attempt.runtime_handle.native_session_id == "opencode-session-2"
        assert executor._ac_runtime_handles == {}

    @pytest.mark.asyncio
    async def test_atomic_ac_skips_memory_gate_for_mocked_backend_runtime(self) -> None:
        """Mocked runtimes should not block on low-memory gating without explicit opt-in."""

        class _StubRuntime:
            def __init__(self) -> None:
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                del prompt, tools, system_prompt, resume_session_id
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-1",
                        cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                        approval_mode=(
                            resume_handle.approval_mode
                            if resume_handle is not None
                            else "acceptEdits"
                        ),
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        executor = ParallelACExecutor(
            adapter=_StubRuntime(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )

        with (
            patch(
                "ouroboros.orchestrator.parallel_executor._get_available_memory_gb",
                return_value=0.5,
            ),
            patch(
                "ouroboros.orchestrator.parallel_executor.asyncio.sleep",
                new_callable=AsyncMock,
            ) as sleep_mock,
        ):
            result = await executor._execute_atomic_ac(
                ac_index=0,
                ac_content="Implement AC 1",
                session_id="orch_123",
                tools=["Read", "Edit"],
                system_prompt="system",
                seed_goal="Ship the feature",
                depth=0,
                start_time=datetime.now(UTC),
            )

        assert result.success is True
        sleep_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_try_decompose_ac_times_out_and_falls_back_to_atomic(self) -> None:
        """A hung decomposition child should time out and fall back to atomic execution."""

        class _HangingRuntime:
            def __init__(self) -> None:
                self.cancelled = False

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                del prompt, tools, system_prompt, resume_handle, resume_session_id
                try:
                    await asyncio.Future()
                    if False:  # pragma: no cover
                        yield AgentMessage(type="assistant", content="")
                finally:
                    self.cancelled = True

        runtime = _HangingRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=True,
        )

        with patch(
            "ouroboros.orchestrator.parallel_executor.DECOMPOSITION_TIMEOUT_SECONDS",
            0.01,
        ):
            result = await executor._try_decompose_ac(
                ac_content="Implement the full OpenCode runtime adapter.",
                ac_index=0,
                seed_goal="Ship OpenCode support",
                tools=["Read", "Edit"],
                system_prompt="system",
            )

        assert result is None
        assert runtime.cancelled is True

    @pytest.mark.asyncio
    async def test_decomposed_ac_inlines_sub_ac_dispatch_into_single_ac(self) -> None:
        """Decomposed execution should recurse through _execute_single_ac without a helper path."""
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=True,
        )
        executor._emit_subtask_event = AsyncMock()
        executor._try_decompose_ac = AsyncMock(
            side_effect=[["Extract parser", "Wire parser"], None, None]
        )

        async def fake_execute_atomic_ac(**kwargs: Any) -> ACExecutionResult:
            return ACExecutionResult(
                ac_index=int(kwargs["ac_index"]),
                ac_content=str(kwargs["ac_content"]),
                success=True,
                final_message=f"{kwargs['ac_content']} complete",
                depth=int(kwargs["depth"]),
            )

        executor._execute_atomic_ac = AsyncMock(side_effect=fake_execute_atomic_ac)

        with patch.object(
            executor,
            "_execute_single_ac",
            wraps=executor._execute_single_ac,
        ) as execute_single_ac_spy:
            result = await executor._execute_single_ac(
                ac_index=1,
                ac_content="Implement parser workflow",
                session_id="sess_decompose",
                tools=["Read", "Edit"],
                tool_catalog=None,
                system_prompt="system",
                seed_goal="Ship parser workflow",
                depth=0,
                execution_id="exec_decompose",
            )

        assert hasattr(executor, "_execute_sub_acs") is False
        assert result.success is True
        assert result.is_decomposed is True
        assert [sub_result.ac_content for sub_result in result.sub_results] == [
            "Extract parser",
            "Wire parser",
        ]
        assert [sub_result.depth for sub_result in result.sub_results] == [1, 1]
        assert [
            (
                int(call.kwargs["ac_index"]),
                str(call.kwargs["ac_content"]),
                int(call.kwargs["depth"]),
            )
            for call in execute_single_ac_spy.await_args_list
        ] == [
            (1, "Implement parser workflow", 0),
            (100, "Extract parser", 1),
            (101, "Wire parser", 1),
        ]
        assert executor._try_decompose_ac.await_count == 3
        assert executor._execute_atomic_ac.await_count == 2

    @pytest.mark.asyncio
    async def test_top_level_decomposition_preserves_sub_ac_runtime_identity(self) -> None:
        """First-level decomposed children should still execute with sub-AC runtime metadata."""
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=True,
        )
        executor._emit_subtask_event = AsyncMock()
        executor._try_decompose_ac = AsyncMock(
            side_effect=[["Extract parser", "Wire parser"], None, None]
        )

        async def fake_execute_atomic_ac(**kwargs: Any) -> ACExecutionResult:
            return ACExecutionResult(
                ac_index=int(kwargs["ac_index"]),
                ac_content=str(kwargs["ac_content"]),
                success=True,
                final_message=f"{kwargs['ac_content']} complete",
                depth=int(kwargs["depth"]),
            )

        executor._execute_atomic_ac = AsyncMock(side_effect=fake_execute_atomic_ac)

        await executor._execute_single_ac(
            ac_index=1,
            ac_content="Implement parser workflow",
            session_id="sess_sub_ac_runtime",
            tools=["Read", "Edit"],
            tool_catalog=None,
            system_prompt="system",
            seed_goal="Ship parser workflow",
            depth=0,
            execution_id="exec_sub_ac_runtime",
        )

        assert [
            (
                int(call.kwargs["ac_index"]),
                bool(call.kwargs["is_sub_ac"]),
                int(call.kwargs["parent_ac_index"]),
                int(call.kwargs["sub_ac_index"]),
            )
            for call in executor._execute_atomic_ac.await_args_list
        ] == [
            (100, True, 1, 0),
            (101, True, 1, 1),
        ]

    @pytest.mark.asyncio
    async def test_depth_three_forces_atomic_without_further_decomposition(self) -> None:
        """Depth 2 may still recurse, but depth 3 must execute atomically."""
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=True,
            max_decomposition_depth=3,
        )
        executor._emit_subtask_event = AsyncMock()
        executor._try_decompose_ac = AsyncMock(
            side_effect=[
                ["Depth 3 child A", "Depth 3 child B"],
            ]
        )

        async def fake_execute_atomic_ac(**kwargs: Any) -> ACExecutionResult:
            return ACExecutionResult(
                ac_index=int(kwargs["ac_index"]),
                ac_content=str(kwargs["ac_content"]),
                success=True,
                final_message=f"{kwargs['ac_content']} complete",
                depth=int(kwargs["depth"]),
            )

        executor._execute_atomic_ac = AsyncMock(side_effect=fake_execute_atomic_ac)

        result = await executor._execute_single_ac(
            ac_index=0,
            ac_content="Root AC",
            session_id="sess_depth_limit",
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
            seed_goal="Ship recursive decomposition",
            depth=2,
            execution_id="exec_depth_limit",
        )

        assert result.is_decomposed is True
        assert result.decomposition_depth_warning is False
        assert [sub_result.ac_content for sub_result in result.sub_results] == [
            "Depth 3 child A",
            "Depth 3 child B",
        ]
        assert [sub_result.depth for sub_result in result.sub_results] == [3, 3]
        assert [sub_result.decomposition_depth_warning for sub_result in result.sub_results] == [
            True,
            True,
        ]
        executor._try_decompose_ac.assert_awaited_once()
        assert executor._execute_atomic_ac.await_count == 2
        assert [call.kwargs["depth"] for call in executor._execute_atomic_ac.await_args_list] == [
            3,
            3,
        ]

    @pytest.mark.asyncio
    async def test_execute_parallel_skips_externally_satisfied_acs(self) -> None:
        """Top-level ACs flagged by --skip-completed should not be re-executed."""
        seed = _make_seed("AC 1", "AC 2")
        dependency_graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content="AC 1", depends_on=()),
                ACNode(index=1, content="AC 2", depends_on=()),
            ),
            execution_levels=((0, 1),),
        )
        executor = _make_executor()
        executor._execute_ac_batch = AsyncMock(
            return_value=[
                ACExecutionResult(
                    ac_index=1,
                    ac_content="AC 2",
                    success=True,
                    final_message="Implemented AC 2",
                )
            ]
        )

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=dependency_graph.to_execution_plan(),
            session_id="orch_skip_completed",
            execution_id="exec_skip_completed",
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
            externally_satisfied_acs={
                0: {"reason": "Implemented manually", "commit": "abc1234"},
            },
        )

        assert result.success_count == 1
        assert result.externally_satisfied_count == 1
        assert result.failure_count == 0
        assert result.results[0].outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY
        assert "Implemented manually" in result.results[0].final_message
        assert "abc1234" in result.results[0].final_message
        executor._execute_ac_batch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_externally_satisfied_ac_blocked_when_dependency_failed(self) -> None:
        """Externally satisfied ACs must be BLOCKED when an upstream dep failed.

        Regression guard for #401: a stale --skip-completed marker must never
        bypass dependency validation. If AC0 fails and AC1 (which depends on
        AC0) is flagged externally_satisfied, AC1 must be BLOCKED — not
        SATISFIED_EXTERNALLY — because the supposed satisfied state is stale
        relative to the current failed run.
        """
        seed = _make_seed("AC 0 foundation", "AC 1 dependent flow")
        dependency_graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=(0,)),
            ),
            execution_levels=((0,), (1,)),
        )
        executor = _make_executor()
        executed_batches: list[list[int]] = []

        async def fake_execute_ac_batch(**kwargs: Any) -> list[ACExecutionResult]:
            batch_indices = list(kwargs["batch_indices"])
            executed_batches.append(batch_indices)
            return [
                ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=seed.acceptance_criteria[ac_index],
                    success=False,
                    error="Foundation failed",
                    outcome=ACExecutionOutcome.FAILED,
                )
                for ac_index in batch_indices
            ]

        executor._execute_ac_batch = fake_execute_ac_batch  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=dependency_graph.to_execution_plan(),
            session_id="orch_stale_external_satisfied",
            execution_id="exec_stale_external_satisfied",
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
            externally_satisfied_acs={
                1: {"reason": "Previously satisfied", "commit": "deadbeef"},
            },
        )

        # Only AC0 should be executed (and fails). AC1 must NOT run even
        # though it was flagged externally satisfied — its upstream dep failed.
        assert executed_batches == [[0]]

        ac1_result = next(r for r in result.results if r.ac_index == 1)
        assert ac1_result.outcome == ACExecutionOutcome.BLOCKED
        assert ac1_result.success is False
        assert ac1_result.error == "Skipped: dependency failed"

        assert result.externally_satisfied_count == 0
        assert result.blocked_count == 1
        assert result.failure_count == 1

    def test_verification_report_emits_depth_warning_feedback_metadata(self) -> None:
        """Verification report should expose depth warnings as structured metadata."""
        parallel_result = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content="Root AC",
                    success=True,
                    is_decomposed=True,
                    sub_results=(
                        ACExecutionResult(
                            ac_index=100,
                            ac_content="Depth-limited leaf",
                            success=True,
                            final_message="Leaf complete",
                            depth=3,
                            decomposition_depth_warning=True,
                        ),
                    ),
                ),
            ),
            success_count=1,
            failure_count=0,
        )

        report = render_parallel_verification_report(
            parallel_result,
            1,
            max_decomposition_depth=3,
        )

        assert "## Feedback Metadata" in report
        assert '"code": "decomposition_depth_warning"' in report
        assert '"affected_ac_paths": ["1.1"]' in report
        assert '"max_depth": 3' in report

    @pytest.mark.asyncio
    async def test_stall_retry_is_scoped_to_atomic_leaf_execution(self) -> None:
        """Leaf retries should not re-run composite decomposition or sibling dispatch."""
        event_store = AsyncMock()
        event_store.append = AsyncMock()
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=True,
        )
        executor._emit_subtask_event = AsyncMock()
        executor._try_decompose_ac = AsyncMock(
            side_effect=[["Retry leaf", "Stable leaf"], None, None]
        )

        async def fake_execute_atomic_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            retry_attempt = int(kwargs["retry_attempt"])
            if ac_index == 100 and retry_attempt == 0:
                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=str(kwargs["ac_content"]),
                    success=False,
                    error="__STALL_DETECTED__",
                    retry_attempt=retry_attempt,
                    depth=int(kwargs["depth"]),
                )

            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=str(kwargs["ac_content"]),
                success=True,
                final_message="retry leaf complete",
                retry_attempt=retry_attempt,
                depth=int(kwargs["depth"]),
            )

        executor._execute_atomic_ac = AsyncMock(side_effect=fake_execute_atomic_ac)

        result = await executor._execute_single_ac(
            ac_index=1,
            ac_content="Composite AC",
            session_id="sess_atomic_retry_scope",
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
            seed_goal="Retry only stalled leaves",
            depth=0,
            execution_id="exec_atomic_retry_scope",
        )

        assert result.success is True
        assert result.is_decomposed is True
        assert [sub_result.retry_attempt for sub_result in result.sub_results] == [1, 0]
        assert executor._try_decompose_ac.await_count == 3
        assert [
            (
                int(call.kwargs["ac_index"]),
                int(call.kwargs["depth"]),
                int(call.kwargs["retry_attempt"]),
            )
            for call in executor._execute_atomic_ac.await_args_list
        ] == [
            (100, 1, 0),
            (100, 1, 1),
            (101, 1, 0),
        ]

        stall_events = [
            call.args[0]
            for call in event_store.append.await_args_list
            if call.args and call.args[0].type == "execution.ac.stall_detected"
        ]
        assert len(stall_events) == 1
        first_leaf_identity = executor._execute_atomic_ac.await_args_list[0].kwargs["node_identity"]
        assert (
            stall_events[0].aggregate_id == f"exec_atomic_retry_scope_{first_leaf_identity.node_id}"
        )
        assert stall_events[0].data["node_id"] == first_leaf_identity.node_id
        assert stall_events[0].data["parent_node_id"] == first_leaf_identity.parent_node_id
        assert stall_events[0].data["legacy_parent_node_id"] == "ac_1"
        assert stall_events[0].data["display_path"] == "2.1"
        assert stall_events[0].data["attempt"] == 1
        assert stall_events[0].data["max_attempts"] == MAX_STALL_RETRIES + 1
        assert stall_events[0].data["action"] == "restart"

    @pytest.mark.asyncio
    async def test_stall_retry_exhaustion_returns_terminal_failure_from_single_ac(self) -> None:
        """Single-AC execution should convert an unrecoverable stall into a normal failure."""
        event_store = AsyncMock()
        event_store.append = AsyncMock()
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        async def always_stall(**kwargs: Any) -> ACExecutionResult:
            return ACExecutionResult(
                ac_index=int(kwargs["ac_index"]),
                ac_content=str(kwargs["ac_content"]),
                success=False,
                error="__STALL_DETECTED__",
                retry_attempt=int(kwargs["retry_attempt"]),
                depth=int(kwargs["depth"]),
            )

        executor._execute_atomic_ac = AsyncMock(side_effect=always_stall)

        result = await executor._execute_single_ac(
            ac_index=2,
            ac_content="Leaf AC",
            session_id="sess_atomic_retry_exhausted",
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
            seed_goal="Normalize terminal stall failures",
            depth=0,
            execution_id="exec_atomic_retry_exhausted",
        )

        assert result.success is False
        assert result.error == f"Stalled (no activity for {STALL_TIMEOUT_SECONDS:.0f}s)"
        assert result.retry_attempt == MAX_STALL_RETRIES
        assert executor._execute_atomic_ac.await_count == MAX_STALL_RETRIES + 1
        assert [
            int(call.kwargs["retry_attempt"])
            for call in executor._execute_atomic_ac.await_args_list
        ] == list(range(MAX_STALL_RETRIES + 1))

        stall_events = [
            call.args[0]
            for call in event_store.append.await_args_list
            if call.args and call.args[0].type == "execution.ac.stall_detected"
        ]
        assert [event.data["attempt"] for event in stall_events] == [1, 2, 3]
        assert [event.data["action"] for event in stall_events] == [
            "restart",
            "restart",
            "abandon",
        ]
        assert all(event.data["max_attempts"] == MAX_STALL_RETRIES + 1 for event in stall_events)

    @pytest.mark.asyncio
    async def test_runtime_handle_cache_isolated_between_acceptance_criteria(self) -> None:
        """Completing one AC must not seed a different AC with its prior runtime session."""

        class _StubCrossACRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                bound_handle = RuntimeHandle(
                    backend=resume_handle.backend if resume_handle is not None else "opencode",
                    kind=resume_handle.kind
                    if resume_handle is not None
                    else "implementation_session",
                    native_session_id=f"opencode-session-{len(self.calls)}",
                    cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                    approval_mode=(
                        resume_handle.approval_mode if resume_handle is not None else "acceptEdits"
                    ),
                    metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=bound_handle,
                )

        runtime = _StubCrossACRuntime()
        event_store, _ = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        first_result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )
        second_result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Implement AC 2",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        first_handle = runtime.calls[0]["resume_handle"]
        second_handle = runtime.calls[1]["resume_handle"]
        assert isinstance(first_handle, RuntimeHandle)
        assert isinstance(second_handle, RuntimeHandle)
        assert first_handle.native_session_id is None
        assert second_handle.native_session_id is None
        assert first_handle.metadata["session_scope_id"] == "orch_123_ac_1"
        assert second_handle.metadata["session_scope_id"] == "orch_123_ac_2"
        assert first_handle.metadata["session_attempt_id"] == "orch_123_ac_1_attempt_1"
        assert second_handle.metadata["session_attempt_id"] == "orch_123_ac_2_attempt_1"
        assert second_handle.metadata["ac_index"] == 1
        assert first_result.runtime_handle is not None
        assert second_result.runtime_handle is not None
        assert first_result.runtime_handle.native_session_id == "opencode-session-1"
        assert second_result.runtime_handle.native_session_id == "opencode-session-2"
        assert executor._ac_runtime_handles == {}

    @pytest.mark.asyncio
    async def test_restarted_executor_rejects_persisted_runtime_handle_from_another_ac(
        self,
    ) -> None:
        """A persisted runtime handle must not resume when its metadata belongs to another AC."""

        class _StubFreshRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-fresh",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        current_state_path = (
            "execution.workflows.orch_123.acceptance_criteria.ac_2.implementation_session"
        )
        current_attempt_id = "orch_123_ac_2_attempt_1"
        foreign_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-foreign",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "ac_id": "orch_123_ac_1",
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "attempt_number": 1,
                "ac_index": 0,
                "session_scope_id": "orch_123_ac_1",
                "session_attempt_id": "orch_123_ac_1_attempt_1",
                "session_state_path": (
                    "execution.workflows.orch_123.acceptance_criteria.ac_1.implementation_session"
                ),
                "server_session_id": "server-foreign",
            },
        )
        event_store = AsyncMock()
        event_store.replay = AsyncMock(
            return_value=[
                BaseEvent(
                    type="execution.session.started",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "attempt_number": 1,
                        "session_scope_id": "orch_123_ac_2",
                        "session_attempt_id": current_attempt_id,
                        "session_state_path": current_state_path,
                        "runtime": foreign_handle.to_dict(),
                    },
                )
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubFreshRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Keep AC sessions isolated",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.native_session_id is None
        assert resume_handle.metadata["ac_index"] == 1
        assert resume_handle.metadata["session_scope_id"] == "orch_123_ac_2"
        assert resume_handle.metadata["session_attempt_id"] == current_attempt_id
        assert "server_session_id" not in resume_handle.metadata
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == "opencode-session-fresh"

    @pytest.mark.asyncio
    async def test_cached_runtime_handle_from_another_ac_is_not_reused(self) -> None:
        """An in-memory runtime-handle cache entry must not leak a foreign AC session."""

        class _StubFreshRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-current",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        runtime = _StubFreshRuntime()
        event_store = AsyncMock()
        event_store.replay = AsyncMock(return_value=[])
        event_store.append = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity = executor._resolve_ac_runtime_identity(
            1,
            execution_context_id="orch_123",
            retry_attempt=0,
        )
        executor._ac_runtime_handles[runtime_identity.cache_key] = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-foreign",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "ac_id": "orch_123_ac_1",
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "attempt_number": 1,
                "ac_index": 0,
                "session_scope_id": "orch_123_ac_1",
                "session_attempt_id": "orch_123_ac_1_attempt_1",
                "session_state_path": (
                    "execution.workflows.orch_123.acceptance_criteria.ac_1.implementation_session"
                ),
                "server_session_id": "server-foreign",
            },
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Keep AC sessions isolated",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.native_session_id is None
        assert resume_handle.metadata["ac_index"] == 1
        assert resume_handle.metadata["session_scope_id"] == "orch_123_ac_2"
        assert resume_handle.metadata["session_attempt_id"] == "orch_123_ac_2_attempt_1"
        assert "server_session_id" not in resume_handle.metadata
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == "opencode-session-current"

    @pytest.mark.asyncio
    async def test_atomic_ac_persists_reconnectable_handle_before_native_session_id(self) -> None:
        """OpenCode AC lifecycle should persist once the runtime exposes a resumable handle."""

        class _StubReconnectableRuntime:
            def __init__(self) -> None:
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                assert isinstance(resume_handle, RuntimeHandle)
                reconnectable_handle = RuntimeHandle(
                    backend=resume_handle.backend,
                    kind=resume_handle.kind,
                    conversation_id="conversation-9",
                    previous_response_id="response-9",
                    transcript_path="/tmp/opencode-runtime.jsonl",
                    cwd=resume_handle.cwd,
                    approval_mode=resume_handle.approval_mode,
                    updated_at="2026-03-13T09:00:00+00:00",
                    metadata={
                        **dict(resume_handle.metadata),
                        "server_session_id": "server-42",
                        "runtime_event_type": "session.ready",
                    },
                )
                yield AgentMessage(
                    type="system",
                    content="OpenCode session ready for reconnect.",
                    data={"server_session_id": "server-42"},
                    resume_handle=reconnectable_handle,
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=reconnectable_handle,
                )

        event_store = AsyncMock()
        event_store.replay = AsyncMock(return_value=[])
        event_store.append = AsyncMock()
        executor = ParallelACExecutor(
            adapter=_StubReconnectableRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Persist reconnectable OpenCode implementation handles",
            session_id="orch_123",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_ac_progress",
        )

        appended_events = [call.args[0] for call in event_store.append.await_args_list]
        started_event = next(
            event for event in appended_events if event.type == "execution.session.started"
        )
        completed_event = next(
            event for event in appended_events if event.type == "execution.session.completed"
        )
        execution_completed_event = next(
            event for event in appended_events if event.type == "execution.ac.completed"
        )

        assert result.success is True
        assert result.session_id is None
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id is None
        assert result.runtime_handle.conversation_id == "conversation-9"
        assert result.runtime_handle.previous_response_id == "response-9"
        assert result.runtime_handle.transcript_path == "/tmp/opencode-runtime.jsonl"
        assert result.runtime_handle.metadata["server_session_id"] == "server-42"
        assert started_event.data["session_id"] == "server-42"
        assert started_event.data["server_session_id"] == "server-42"
        assert started_event.data["runtime"]["native_session_id"] is None
        assert started_event.data["runtime"]["metadata"]["server_session_id"] == "server-42"
        assert "conversation_id" not in started_event.data["runtime"]
        assert "previous_response_id" not in started_event.data["runtime"]
        assert "transcript_path" not in started_event.data["runtime"]
        assert "updated_at" not in started_event.data["runtime"]
        assert completed_event.data["session_id"] == "server-42"
        assert execution_completed_event.aggregate_id == "exec_ac_progress"
        assert execution_completed_event.data["success"] is True
        assert execution_completed_event.data["acceptance_criterion"] == (
            "Persist reconnectable OpenCode implementation handles"
        )

    @pytest.mark.asyncio
    async def test_execution_scoped_ac_completion_append_is_best_effort(self) -> None:
        """Root AC evidence must not corrupt an already persisted successful AC lifecycle."""

        event_store = AsyncMock()
        event_store.replay = AsyncMock(return_value=[])
        appended_events: list[BaseEvent] = []

        async def _append(event: BaseEvent) -> None:
            if event.type == "execution.ac.completed":
                raise RuntimeError("root aggregate temporarily unavailable")
            appended_events.append(event)

        event_store.append = AsyncMock(side_effect=_append)
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity = executor._resolve_ac_runtime_identity(
            0,
            execution_context_id="exec_ac_progress",
            retry_attempt=0,
        )

        await executor._emit_ac_runtime_event(
            event_type="execution.session.completed",
            runtime_identity=runtime_identity,
            ac_content="Persist AC completion evidence",
            runtime_handle=None,
            execution_id="exec_ac_progress",
            session_id="server-42",
            result_summary="[TASK_COMPLETE]",
            success=True,
        )

        assert [event.type for event in appended_events] == ["execution.session.completed"]
        assert event_store.append.await_count == 2

    @pytest.mark.asyncio
    async def test_restarted_executor_loads_persisted_runtime_handle_for_same_attempt(self) -> None:
        """A fresh executor should rehydrate the same-attempt runtime handle from events."""

        class _StubPersistedResumeRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        persisted_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-9",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "ac_index": 1,
                "session_scope_id": "orch_123_ac_2",
                "session_state_path": (
                    "execution.workflows.orch_123.acceptance_criteria.ac_2.implementation_session"
                ),
                "server_session_id": "server-99",
            },
        )
        event_store = AsyncMock()
        event_store.replay = AsyncMock(
            return_value=[
                BaseEvent(
                    type="execution.session.started",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_2.implementation_session"
                        ),
                        "runtime": persisted_handle.to_dict(),
                    },
                )
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubPersistedResumeRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Resume the interrupted AC implementation session",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.native_session_id == "opencode-session-9"
        assert resume_handle.metadata["server_session_id"] == "server-99"
        event_store.replay.assert_awaited_once_with("execution", "orch_123_ac_2")
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == resume_handle.native_session_id
        assert result.runtime_handle.metadata == resume_handle.metadata

    @pytest.mark.asyncio
    async def test_restarted_executor_ignores_invalid_persisted_runtime_handle_for_same_attempt(
        self,
    ) -> None:
        """Malformed persisted runtime payloads should be skipped in favor of a fresh handle."""

        class _StubInvalidPersistedHandleRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        event_store = AsyncMock()
        event_store.replay = AsyncMock(
            return_value=[
                BaseEvent(
                    type="execution.session.started",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_2.implementation_session"
                        ),
                        "runtime": {
                            "kind": "implementation_session",
                            "cwd": "/tmp/project",
                            "approval_mode": "acceptEdits",
                            "metadata": {
                                "scope": "ac",
                                "session_role": "implementation",
                                "retry_attempt": 0,
                                "ac_index": 1,
                                "session_scope_id": "orch_123_ac_2",
                                "session_state_path": (
                                    "execution.workflows.orch_123.acceptance_criteria."
                                    "ac_2.implementation_session"
                                ),
                                "server_session_id": "server-invalid",
                            },
                        },
                    },
                )
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubInvalidPersistedHandleRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Recover from malformed persisted runtime state",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.backend == "opencode"
        assert resume_handle.native_session_id is None
        assert resume_handle.metadata["session_scope_id"] == "orch_123_ac_2"
        assert resume_handle.metadata["session_role"] == "implementation"
        assert "server_session_id" not in resume_handle.metadata
        event_store.replay.assert_awaited_once_with("execution", "orch_123_ac_2")
        # Compare handles ignoring updated_at (timestamp set at creation time
        # may differ by microseconds from the one stored in the result).
        result_handle = replace(result.runtime_handle, updated_at=None)  # type: ignore[type-var]
        expected_handle = replace(resume_handle, updated_at=None)  # type: ignore[type-var]
        assert result_handle == expected_handle

    @pytest.mark.asyncio
    async def test_restarted_executor_prefers_latest_resumed_runtime_handle_for_same_attempt(
        self,
    ) -> None:
        """Resume should hydrate from the newest active lifecycle event for the same attempt."""

        class _StubResumedHandleRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        started_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-started",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "ac_index": 1,
                "session_scope_id": "orch_123_ac_2",
                "session_state_path": (
                    "execution.workflows.orch_123.acceptance_criteria.ac_2.implementation_session"
                ),
                "server_session_id": "server-started",
            },
        )
        resumed_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-resumed",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "ac_index": 1,
                "session_scope_id": "orch_123_ac_2",
                "session_state_path": (
                    "execution.workflows.orch_123.acceptance_criteria.ac_2.implementation_session"
                ),
                "server_session_id": "server-resumed",
            },
        )
        event_store = AsyncMock()
        event_store.replay = AsyncMock(
            return_value=[
                BaseEvent(
                    type="execution.session.started",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_2.implementation_session"
                        ),
                        "runtime": started_handle.to_dict(),
                    },
                ),
                BaseEvent(
                    type="execution.session.resumed",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_2.implementation_session"
                        ),
                        "runtime": resumed_handle.to_dict(),
                    },
                ),
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubResumedHandleRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Resume the latest persisted implementation session",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.native_session_id == "opencode-session-resumed"
        assert resume_handle.metadata["server_session_id"] == "server-resumed"
        event_store.replay.assert_awaited_once_with("execution", "orch_123_ac_2")
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == resume_handle.native_session_id
        assert result.runtime_handle.metadata == resume_handle.metadata

    @pytest.mark.asyncio
    async def test_restarted_executor_does_not_cross_resume_into_another_execution_context(
        self,
    ) -> None:
        """Persisted AC handles must stay bound to the parent execution/session context."""

        class _StubFreshRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-fresh",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        event_store = AsyncMock()
        event_store.replay = AsyncMock(return_value=[])
        event_store.append = AsyncMock()
        runtime = _StubFreshRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Start a new implementation session in a different execution context",
            session_id="orch_new",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.native_session_id is None
        assert resume_handle.metadata["session_scope_id"] == "orch_new_ac_2"
        assert (
            resume_handle.metadata["session_state_path"]
            == "execution.workflows.orch_new.acceptance_criteria.ac_2.implementation_session"
        )
        event_store.replay.assert_awaited_once_with("execution", "orch_new_ac_2")
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == "opencode-session-fresh"

    @pytest.mark.asyncio
    async def test_restarted_executor_ignores_terminal_runtime_handle_for_same_attempt(
        self,
    ) -> None:
        """Persisted terminal events should not revive a completed AC attempt."""

        class _StubTerminalAwareRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-fresh",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        persisted_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-terminal",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "ac_index": 1,
                "session_scope_id": "orch_123_ac_2",
                "session_state_path": (
                    "execution.workflows.orch_123.acceptance_criteria.ac_2.implementation_session"
                ),
            },
        )
        event_store = AsyncMock()
        event_store.replay = AsyncMock(
            return_value=[
                BaseEvent(
                    type="execution.session.started",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_2.implementation_session"
                        ),
                        "runtime": persisted_handle.to_dict(),
                    },
                ),
                BaseEvent(
                    type="execution.session.completed",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_2.implementation_session"
                        ),
                        "runtime": persisted_handle.to_dict(),
                        "success": True,
                    },
                ),
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubTerminalAwareRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Start a fresh session after terminal completion",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.native_session_id is None
        assert resume_handle.metadata["session_scope_id"] == "orch_123_ac_2"
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == "opencode-session-fresh"
        assert executor._ac_runtime_handles == {}

    @pytest.mark.asyncio
    async def test_retry_reopens_failed_ac_with_same_scope_and_new_attempt_audit(self) -> None:
        """Retry attempts should start a fresh session while emitting a new attempt identity."""

        class _StubRetryRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"
                self._attempt = 0

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                native_session_id = f"opencode-session-{self._attempt}"
                is_error = self._attempt == 0
                self._attempt += 1
                bound_handle = RuntimeHandle(
                    backend=resume_handle.backend if resume_handle is not None else "opencode",
                    kind=resume_handle.kind
                    if resume_handle is not None
                    else "implementation_session",
                    native_session_id=native_session_id,
                    cwd=resume_handle.cwd if resume_handle is not None else "/tmp/project",
                    approval_mode=(
                        resume_handle.approval_mode if resume_handle is not None else "acceptEdits"
                    ),
                    metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                )
                yield AgentMessage(
                    type="result",
                    content="retry me" if is_error else "[TASK_COMPLETE]",
                    data={"subtype": "error" if is_error else "success"},
                    resume_handle=bound_handle,
                )

        runtime = _StubRetryRuntime()
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        first_attempt = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )
        retry_attempt = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=1,
        )

        first_handle = runtime.calls[0]["resume_handle"]
        second_handle = runtime.calls[1]["resume_handle"]
        assert isinstance(first_handle, RuntimeHandle)
        assert isinstance(second_handle, RuntimeHandle)
        assert first_handle.native_session_id is None
        assert second_handle.native_session_id is None
        assert first_handle.metadata["session_scope_id"] == "orch_123_ac_1"
        assert second_handle.metadata["session_scope_id"] == "orch_123_ac_1"
        assert first_handle.metadata["session_attempt_id"] == "orch_123_ac_1_attempt_1"
        assert second_handle.metadata["session_attempt_id"] == "orch_123_ac_1_attempt_2"
        assert (
            first_handle.metadata["session_state_path"]
            == second_handle.metadata["session_state_path"]
            == "execution.workflows.orch_123.acceptance_criteria.ac_1.implementation_session"
        )
        assert first_handle.metadata["retry_attempt"] == 0
        assert second_handle.metadata["retry_attempt"] == 1
        assert first_attempt.ac_index == retry_attempt.ac_index == 0
        assert first_attempt.success is False
        assert retry_attempt.success is True
        assert first_attempt.session_id == "opencode-session-0"
        assert retry_attempt.session_id == "opencode-session-1"
        assert first_attempt.retry_attempt == 0
        assert retry_attempt.retry_attempt == 1
        assert first_attempt.runtime_handle is not None
        assert retry_attempt.runtime_handle is not None
        assert first_attempt.runtime_handle.native_session_id == "opencode-session-0"
        assert retry_attempt.runtime_handle.native_session_id == "opencode-session-1"
        lifecycle_events = [
            event
            for event in appended_events
            if event.type
            in {
                "execution.session.started",
                "execution.session.failed",
                "execution.session.completed",
            }
        ]
        assert [event.type for event in lifecycle_events] == [
            "execution.session.started",
            "execution.session.failed",
            "execution.session.started",
            "execution.session.completed",
        ]
        assert [event.data["session_attempt_id"] for event in lifecycle_events] == [
            "orch_123_ac_1_attempt_1",
            "orch_123_ac_1_attempt_1",
            "orch_123_ac_1_attempt_2",
            "orch_123_ac_1_attempt_2",
        ]
        assert executor._ac_runtime_handles == {}

    @pytest.mark.asyncio
    async def test_retry_executes_on_reconciled_workspace_context(self) -> None:
        """Retry prompts should include prior reconciled workspace context."""

        class _StubContextRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-retry",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        runtime = _StubContextRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        reconciled_context = LevelContext(
            level_number=1,
            completed_acs=(
                ACContextSummary(
                    ac_index=1,
                    ac_content="Reconcile the shared auth helpers",
                    success=True,
                    files_modified=("src/auth.py",),
                    key_output="Shared auth helpers are reconciled",
                ),
            ),
            coordinator_review=CoordinatorReview(
                level_number=1,
                review_summary="Merged the auth helper edits into the shared workspace",
                fixes_applied=("Merged src/auth.py conflict",),
                warnings_for_next_level=("Continue from the reconciled src/auth.py state",),
            ),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Finish wiring the auth retry flow",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            level_contexts=[reconciled_context],
            retry_attempt=1,
        )

        prompt = runtime.calls[0]["prompt"]
        assert isinstance(prompt, str)
        assert "## Previous Work Context" in prompt
        assert "Shared auth helpers are reconciled" in prompt
        assert "## Coordinator Review (Level 1)" in prompt
        assert "Merged the auth helper edits into the shared workspace" in prompt
        assert "Continue from the reconciled src/auth.py state" in prompt
        assert "## Retry Context" in prompt
        assert "retry attempt 1" in prompt
        assert "current shared workspace state" in prompt
        assert result.success is True
        assert result.retry_attempt == 1
        assert result.session_id == "opencode-session-retry"

    @pytest.mark.asyncio
    async def test_atomic_ac_prompt_uses_adapter_working_directory(self) -> None:
        """Prompt workspace context should come from the runtime adapter, not the server cwd."""

        class _StubPromptRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/requested-workspace"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        kind="implementation_session",
                        native_session_id="opencode-session-prompt",
                        cwd=self._cwd,
                        approval_mode="acceptEdits",
                        metadata=dict(resume_handle.metadata) if resume_handle is not None else {},
                    ),
                )

        runtime = _StubPromptRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        listed_paths: list[str] = []

        def _listdir(path: str) -> list[str]:
            listed_paths.append(path)
            return [".git", "README.md", "src"]

        with (
            patch("os.getcwd", return_value="/tmp/server-cwd"),
            patch("os.listdir", side_effect=_listdir),
        ):
            result = await executor._execute_atomic_ac(
                ac_index=0,
                ac_content="Implement the requested feature",
                session_id="orch_prompt",
                tools=["Read"],
                system_prompt="system",
                seed_goal="Ship the feature",
                depth=0,
                start_time=datetime.now(UTC),
            )

        assert listed_paths == ["/tmp/requested-workspace"]
        prompt = runtime.calls[0]["prompt"]
        assert isinstance(prompt, str)
        assert "## Working Directory" in prompt
        assert "`/tmp/requested-workspace`" in prompt
        assert "- README.md" in prompt
        assert "- src" in prompt
        assert "/tmp/server-cwd" not in prompt
        assert result.success is True
        assert result.session_id == "opencode-session-prompt"

    @pytest.mark.asyncio
    async def test_aggregates_mixed_stage_outcomes(self) -> None:
        """A later stage may be partially executable while blocked dependents are withheld."""
        seed = _make_seed(
            "Build the shared model",
            "Implement the fragile integration",
            "Add endpoint on top of the model",
            "Wire reporting to the fragile integration",
        )
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
                ACNode(index=2, content=seed.acceptance_criteria[2], depends_on=(0,)),
                ACNode(index=3, content=seed.acceptance_criteria[3], depends_on=(1,)),
            ),
            execution_levels=((0, 1), (2, 3)),
        )
        executor = _make_executor()

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = kwargs["ac_index"]
            ac_content = kwargs["ac_content"]
            if ac_index == 0:
                return ACExecutionResult(
                    ac_index=0,
                    ac_content=str(ac_content),
                    success=True,
                    final_message="Shared model complete",
                )
            if ac_index == 1:
                return ACExecutionResult(
                    ac_index=1,
                    ac_content=str(ac_content),
                    success=False,
                    error="Integration step failed",
                )
            if ac_index == 2:
                return ACExecutionResult(
                    ac_index=2,
                    ac_content=str(ac_content),
                    success=True,
                    final_message="Endpoint complete",
                )
            msg = f"AC {ac_index} should have been blocked before execution"
            raise AssertionError(msg)

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="sess_stage_mixed",
            execution_id="exec_stage_mixed",
            tools=["Read", "Edit"],
            system_prompt="test",
        )

        assert result.success_count == 2
        assert result.failure_count == 1
        assert result.blocked_count == 1
        assert result.invalid_count == 0
        assert result.skipped_count == 1
        assert [r.outcome for r in result.results] == [
            ACExecutionOutcome.SUCCEEDED,
            ACExecutionOutcome.FAILED,
            ACExecutionOutcome.SUCCEEDED,
            ACExecutionOutcome.BLOCKED,
        ]

        assert len(result.stages) == 2
        assert result.stages[0].outcome == StageExecutionOutcome.PARTIAL
        assert result.stages[0].started is True
        assert result.stages[1].outcome == StageExecutionOutcome.PARTIAL
        assert result.stages[1].success_count == 1
        assert result.stages[1].blocked_count == 1
        executor._emit_level_started.assert_awaited()

    @pytest.mark.asyncio
    async def test_fully_blocked_stage_does_not_start(self) -> None:
        """If all ACs in a later stage depend on a failed AC, that stage is blocked but recorded."""
        seed = _make_seed(
            "Create the foundational abstraction",
            "Build the first dependent flow",
            "Build the second dependent flow",
        )
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=(0,)),
                ACNode(index=2, content=seed.acceptance_criteria[2], depends_on=(0,)),
            ),
            execution_levels=((0,), (1, 2)),
        )
        executor = _make_executor()
        executed_indices: list[int] = []

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            executed_indices.append(ac_index)
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=str(kwargs["ac_content"]),
                success=False,
                error="Foundation failed",
            )

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="sess_stage_blocked",
            execution_id="exec_stage_blocked",
            tools=["Read", "Edit"],
            system_prompt="test",
        )

        assert executed_indices == [0]
        assert result.success_count == 0
        assert result.failure_count == 1
        assert result.blocked_count == 2
        assert result.skipped_count == 2
        assert len(result.stages) == 2
        assert result.stages[0].outcome == StageExecutionOutcome.FAILED
        assert result.stages[1].started is False
        assert result.stages[1].outcome == StageExecutionOutcome.BLOCKED
        assert result.stages[1].blocked_count == 2

        assert executor._emit_level_started.await_count == 1
        assert executor._emit_level_completed.await_count == 2
        blocked_completion = executor._emit_level_completed.await_args_list[1].kwargs
        assert blocked_completion["started"] is False
        assert blocked_completion["blocked_count"] == 2
        assert blocked_completion["outcome"] == StageExecutionOutcome.BLOCKED.value

    @pytest.mark.asyncio
    async def test_runs_serial_stages_in_order(self) -> None:
        """The executor should not dispatch the next stage until the current one finishes."""
        seed = _make_seed("Implement parser", "Implement formatter", "Wire runner")
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
                ACNode(index=2, content=seed.acceptance_criteria[2], depends_on=(0, 1)),
            ),
            execution_levels=((0, 1), (2,)),
        )
        executor = _make_executor()

        stage_one_started: set[int] = set()
        stage_one_completed: list[int] = []
        release_stage_one = asyncio.Event()
        all_stage_one_started = asyncio.Event()
        stage_two_started = asyncio.Event()
        stage_two_started_after: frozenset[int] | None = None

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            nonlocal stage_two_started_after
            ac_index = int(kwargs["ac_index"])
            ac_content = str(kwargs["ac_content"])

            if ac_index in (0, 1):
                stage_one_started.add(ac_index)
                if stage_one_started == {0, 1}:
                    all_stage_one_started.set()
                await release_stage_one.wait()
                stage_one_completed.append(ac_index)
            elif ac_index == 2:
                stage_two_started_after = frozenset(stage_one_completed)
                stage_two_started.set()

            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=True,
                final_message=f"AC {ac_index} complete",
            )

        with patch.object(executor, "_execute_single_ac", side_effect=fake_execute_single_ac):
            execution_task = asyncio.create_task(
                executor.execute_parallel(
                    seed=seed,
                    execution_plan=graph.to_execution_plan(),
                    session_id="sess_stage_order",
                    execution_id="exec_stage_order",
                    tools=["Read"],
                    system_prompt="test",
                )
            )

            await asyncio.wait_for(all_stage_one_started.wait(), timeout=1)
            assert stage_two_started.is_set() is False

            release_stage_one.set()
            result = await asyncio.wait_for(execution_task, timeout=1)

        assert result.all_succeeded is True
        assert result.success_count == 3
        assert stage_two_started.is_set() is True
        assert stage_two_started_after == frozenset({0, 1})

    @pytest.mark.asyncio
    async def test_consumes_stage_batches_sequentially_within_stage_boundaries(self) -> None:
        """Batch-aware stages should run batch-by-batch without crossing stage boundaries."""
        seed = _make_seed(
            "Build parser core",
            "Build formatter core",
            "Assemble shared CLI",
            "Wire end-to-end runner",
        )
        executor = _make_executor()

        execution_plan = SimpleNamespace(
            stages=(
                SimpleNamespace(
                    index=0,
                    ac_indices=(),
                    batches=(
                        SimpleNamespace(ac_indices=(0, 1)),
                        SimpleNamespace(ac_indices=(2,)),
                    ),
                ),
                SimpleNamespace(
                    index=1,
                    ac_indices=(),
                    batches=(SimpleNamespace(ac_indices=(3,)),),
                ),
            ),
            total_stages=2,
            execution_levels=((0, 1, 2), (3,)),
            get_dependencies=lambda ac_index: {3: (2,)}.get(ac_index, ()),
        )

        first_batch_started: set[int] = set()
        release_first_batch = asyncio.Event()
        all_first_batch_started = asyncio.Event()
        second_batch_started = asyncio.Event()
        stage_two_started = asyncio.Event()

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            ac_content = str(kwargs["ac_content"])

            if ac_index in (0, 1):
                first_batch_started.add(ac_index)
                if first_batch_started == {0, 1}:
                    all_first_batch_started.set()
                await release_first_batch.wait()
            elif ac_index == 2:
                second_batch_started.set()
            elif ac_index == 3:
                stage_two_started.set()

            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=True,
                final_message=f"AC {ac_index} complete",
            )

        with patch.object(executor, "_execute_single_ac", side_effect=fake_execute_single_ac):
            execution_task = asyncio.create_task(
                executor.execute_parallel(
                    seed=seed,
                    execution_plan=execution_plan,
                    session_id="sess_stage_batches",
                    execution_id="exec_stage_batches",
                    tools=["Read"],
                    system_prompt="test",
                )
            )

            await asyncio.wait_for(all_first_batch_started.wait(), timeout=1)
            assert second_batch_started.is_set() is False
            assert stage_two_started.is_set() is False

            release_first_batch.set()
            result = await asyncio.wait_for(execution_task, timeout=1)

        assert result.all_succeeded is True
        assert result.success_count == 4
        assert second_batch_started.is_set() is True
        assert stage_two_started.is_set() is True

    @pytest.mark.asyncio
    async def test_aggregates_stage_batch_results_with_failures_and_blocked_dependents(
        self,
    ) -> None:
        """Stage aggregation should include all batch outcomes before moving to the next stage."""
        seed = _make_seed(
            "Build parser core",
            "Build formatter core",
            "Wire parser command",
            "Wire formatter command",
        )
        executor = _make_executor()

        execution_plan = SimpleNamespace(
            stages=(
                SimpleNamespace(
                    index=0,
                    ac_indices=(),
                    batches=(
                        SimpleNamespace(ac_indices=(0,)),
                        SimpleNamespace(ac_indices=(1,)),
                    ),
                ),
                SimpleNamespace(
                    index=1,
                    ac_indices=(),
                    batches=(SimpleNamespace(ac_indices=(2, 3)),),
                ),
            ),
            total_stages=2,
            execution_levels=((0, 1), (2, 3)),
            get_dependencies=lambda ac_index: {2: (0,), 3: (1,)}.get(ac_index, ()),
        )

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            ac_content = str(kwargs["ac_content"])
            if ac_index == 0:
                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=False,
                    error="Parser core failed",
                )

            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=True,
                final_message=f"AC {ac_index} complete",
            )

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=execution_plan,
            session_id="sess_stage_batch_outcomes",
            execution_id="exec_stage_batch_outcomes",
            tools=["Read"],
            system_prompt="test",
        )

        assert [r.outcome for r in result.results] == [
            ACExecutionOutcome.FAILED,
            ACExecutionOutcome.SUCCEEDED,
            ACExecutionOutcome.BLOCKED,
            ACExecutionOutcome.SUCCEEDED,
        ]
        assert result.success_count == 2
        assert result.failure_count == 1
        assert result.blocked_count == 1
        assert result.invalid_count == 0
        assert len(result.stages) == 2
        assert result.stages[0].ac_indices == (0, 1)
        assert result.stages[0].outcome == StageExecutionOutcome.PARTIAL
        assert result.stages[1].ac_indices == (2, 3)
        assert result.stages[1].outcome == StageExecutionOutcome.PARTIAL

    @pytest.mark.asyncio
    async def test_records_coordinator_results_at_level_scope_without_ac_attribution(self) -> None:
        """Coordinator reconciliation should persist level-scoped events and artifacts only."""
        seed = _make_seed(
            "Update the shared module imports",
            "Wire the shared module into the runtime",
        )
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
            ),
            execution_levels=((0, 1),),
        )
        executor = _make_executor()
        executor._coordinator.detect_file_conflicts = MagicMock(
            return_value=[FileConflict(file_path="src/shared.py", ac_indices=(0, 1))]
        )
        executor._coordinator.run_review = AsyncMock(
            return_value=CoordinatorReview(
                level_number=1,
                conflicts_detected=(
                    FileConflict(
                        file_path="src/shared.py",
                        ac_indices=(0, 1),
                        resolved=True,
                        resolution_description="Merged by coordinator",
                    ),
                ),
                review_summary="Resolved shared.py conflict",
                fixes_applied=("Merged overlapping import edits",),
                warnings_for_next_level=("Verify shared.py integration paths",),
                duration_seconds=1.5,
                session_id="coord-session-1",
                session_scope_id="level_1_coordinator",
                session_state_path=".ouroboros/execution_runtime/level_1_coordinator/session.json",
                final_output=(
                    '{"review_summary":"Resolved shared.py conflict",'
                    '"fixes_applied":["Merged overlapping import edits"],'
                    '"warnings_for_next_level":["Verify shared.py integration paths"],'
                    '"conflicts_resolved":["src/shared.py"]}'
                ),
                messages=(
                    AgentMessage(
                        type="assistant",
                        content="Inspecting shared file",
                        tool_name="Read",
                        data={"tool_input": {"file_path": "src/shared.py"}},
                    ),
                    AgentMessage(
                        type="assistant",
                        content="Reconciling overlap",
                        data={"thinking": "Merge the import changes without changing behavior."},
                    ),
                ),
            )
        )

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=str(kwargs["ac_content"]),
                success=True,
                messages=(
                    AgentMessage(
                        type="assistant",
                        content="Editing shared module",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": "src/shared.py"}},
                    ),
                ),
                final_message=f"AC {ac_index + 1} complete",
            )

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="sess_coord_scope",
            execution_id="exec_coord_scope",
            tools=["Read", "Edit"],
            system_prompt="test",
        )

        appended_events = [call.args[0] for call in executor._event_store.append.await_args_list]

        assert result.success_count == 2
        assert len(result.stages) == 1
        assert result.stages[0].coordinator_review is not None
        assert result.stages[0].coordinator_review.review_summary == "Resolved shared.py conflict"
        assert result.stages[0].coordinator_review.artifact_scope == "level"
        assert result.stages[0].coordinator_review.artifact_owner == "coordinator"
        assert result.stages[0].coordinator_review.artifact_owner_id == "level_1_coordinator"

        assert [event.type for event in appended_events] == [
            "execution.coordinator.started",
            "execution.coordinator.tool.started",
            "execution.coordinator.thinking",
            "execution.coordinator.completed",
        ]
        for event in appended_events:
            assert event.aggregate_id == "exec_coord_scope:l0:coord"
            assert event.data["scope"] == "level"
            assert event.data["session_role"] == "coordinator"
            assert event.data["level_number"] == 1
            assert event.data["stage_index"] == 0
            assert "ac_id" not in event.data
            assert "ac_index" not in event.data
            assert "acceptance_criterion" not in event.data

        assert appended_events[-1].data["artifact_type"] == "coordinator_review"
        assert appended_events[-1].data["artifact_scope"] == "level"
        assert appended_events[-1].data["artifact_owner"] == "coordinator"
        assert appended_events[-1].data["artifact_owner_id"] == "level_1_coordinator"
        assert (
            appended_events[-1].data["artifact"]
            == '{"review_summary":"Resolved shared.py conflict","fixes_applied":["Merged overlapping import edits"],"warnings_for_next_level":["Verify shared.py integration paths"],"conflicts_resolved":["src/shared.py"]}'
        )

    @pytest.mark.asyncio
    async def test_returns_reconciled_level_contexts_for_retry_handoff(self) -> None:
        """Completed stage contexts should be returned for retry workspace handoff."""
        seed = _make_seed(
            "Land the shared runtime update",
            "Repair the follow-up integration",
        )
        graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=()),
            ),
            execution_levels=((0, 1),),
        )
        executor = _make_executor()
        executor._coordinator.detect_file_conflicts = MagicMock(
            return_value=[FileConflict(file_path="src/shared.py", ac_indices=(0, 1))]
        )
        executor._coordinator.run_review = AsyncMock(
            return_value=CoordinatorReview(
                level_number=1,
                review_summary="Reconciled shared workspace",
                fixes_applied=("Merged shared.py edits",),
                warnings_for_next_level=("Retry AC 2 against the merged shared.py state",),
            )
        )

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            ac_index = int(kwargs["ac_index"])
            ac_content = str(kwargs["ac_content"])
            if ac_index == 0:
                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=True,
                    messages=(
                        AgentMessage(
                            type="assistant",
                            content="Updated shared module",
                            tool_name="Edit",
                            data={"tool_input": {"file_path": "src/shared.py"}},
                        ),
                    ),
                    final_message="Shared runtime landed",
                )
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=False,
                messages=(
                    AgentMessage(
                        type="assistant",
                        content="Need to revisit integration",
                        tool_name="Edit",
                        data={"tool_input": {"file_path": "src/shared.py"}},
                    ),
                ),
                error="Integration failed",
            )

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="sess_retry_handoff",
            execution_id="exec_retry_handoff",
            tools=["Read", "Edit"],
            system_prompt="test",
        )

        assert len(result.reconciled_level_contexts) == 1
        handoff = result.reconciled_level_contexts[0]
        assert handoff.level_number == 1
        assert handoff.coordinator_review is not None
        assert handoff.coordinator_review.review_summary == "Reconciled shared workspace"
        assert handoff.completed_acs[0].success is True

    @pytest.mark.asyncio
    async def test_reopened_execution_uses_reconciled_workspace_handoff(self) -> None:
        """Retries should seed reopened ACs with the latest reconciled workspace context."""
        seed = _make_seed("Retry the failed shared runtime integration")
        graph = DependencyGraph(
            nodes=(ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),),
            execution_levels=((0,),),
        )
        executor = _make_executor()
        handoff = LevelContext(
            level_number=1,
            completed_acs=(),
            coordinator_review=CoordinatorReview(
                level_number=1,
                review_summary="Workspace was reconciled after the previous failure",
                fixes_applied=("Merged shared.py before retry",),
                warnings_for_next_level=(
                    "Build on the reconciled shared.py, not the earlier draft",
                ),
            ),
        )
        captured_contexts: list[LevelContext] = []

        async def fake_execute_single_ac(**kwargs: Any) -> ACExecutionResult:
            captured_contexts.extend(kwargs["level_contexts"])
            return ACExecutionResult(
                ac_index=int(kwargs["ac_index"]),
                ac_content=str(kwargs["ac_content"]),
                success=True,
                final_message="Retried successfully",
            )

        executor._execute_single_ac = fake_execute_single_ac  # type: ignore[method-assign]

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=graph.to_execution_plan(),
            session_id="sess_retry_reopen",
            execution_id="exec_retry_reopen",
            tools=["Read", "Edit"],
            system_prompt="test",
            reconciled_level_contexts=[handoff],
        )

        assert result.success_count == 1
        assert captured_contexts == [handoff]

    @pytest.mark.asyncio
    async def test_atomic_ac_events_include_retry_attempt_metadata(self) -> None:
        """AC-scoped runtime events should preserve AC id while recording retry attempts."""

        class StubRuntime:
            _runtime_handle_backend = "opencode"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return "/tmp/project"

            @property
            def permission_mode(self) -> str | None:
                return "acceptEdits"

            async def execute_task(self, **kwargs: Any):
                resume_handle = kwargs["resume_handle"]
                assert isinstance(resume_handle, RuntimeHandle)
                assert resume_handle.metadata["retry_attempt"] == 2
                yield AgentMessage(
                    type="assistant",
                    content="Retrying the implementation",
                    tool_name="Edit",
                    data={
                        "tool_input": {"file_path": "src/app.py"},
                        "thinking": "Reopen the same AC with a fresh runtime session.",
                    },
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                )

        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=StubRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=3,
            ac_content="Fix the failing AC",
            session_id="sess_retry",
            tools=["Edit"],
            system_prompt="test",
            seed_goal="Ship the fix",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=2,
        )

        appended_events = [call.args[0] for call in event_store.append.await_args_list]

        assert result.success is True
        assert result.retry_attempt == 2
        assert result.attempt_number == 3
        tool_event = next(
            event for event in appended_events if event.type == "execution.tool.started"
        )
        thinking_event = next(
            event for event in appended_events if event.type == "execution.agent.thinking"
        )
        completed_event = next(
            event for event in appended_events if event.type == "execution.session.completed"
        )

        assert tool_event.aggregate_id == "sess_retry_ac_4"
        assert tool_event.data["ac_id"] == "sess_retry_ac_4"
        assert tool_event.data["retry_attempt"] == 2
        assert tool_event.data["attempt_number"] == 3
        assert tool_event.data["session_attempt_id"] == "sess_retry_ac_4_attempt_3"
        assert thinking_event.aggregate_id == "sess_retry_ac_4"
        assert thinking_event.data["ac_id"] == "sess_retry_ac_4"
        assert thinking_event.data["retry_attempt"] == 2
        assert thinking_event.data["attempt_number"] == 3
        assert thinking_event.data["session_attempt_id"] == "sess_retry_ac_4_attempt_3"
        assert completed_event.aggregate_id == "sess_retry_ac_4"
        assert completed_event.data["ac_id"] == "sess_retry_ac_4"
        assert completed_event.data["retry_attempt"] == 2
        assert completed_event.data["attempt_number"] == 3
        assert completed_event.data["session_attempt_id"] == "sess_retry_ac_4_attempt_3"
        assert completed_event.data["success"] is True

    @pytest.mark.asyncio
    async def test_atomic_ac_events_capture_opencode_tool_metadata_and_results(self) -> None:
        """OpenCode AC sessions should emit normalized tool start/completion metadata."""
        from ouroboros.orchestrator.mcp_tools import (
            normalize_runtime_tool_definition,
            normalize_runtime_tool_result,
        )

        class StubRuntime:
            _runtime_handle_backend = "opencode"
            _cwd = "/tmp/project"
            _permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(self, **kwargs: Any):
                resume_handle = kwargs["resume_handle"]
                assert isinstance(resume_handle, RuntimeHandle)
                runtime_handle = RuntimeHandle(
                    backend="opencode",
                    native_session_id="oc-session-7",
                    cwd="/tmp/project",
                    approval_mode="acceptEdits",
                    metadata={"runtime_event_type": "tool.started"},
                )
                yield AgentMessage(
                    type="assistant",
                    content="Calling tool: Edit: src/app.py",
                    tool_name="Edit",
                    data={
                        "tool_input": {"file_path": "src/app.py"},
                        "tool_definition": normalize_runtime_tool_definition(
                            "Edit",
                            {"file_path": "src/app.py"},
                        ),
                    },
                    resume_handle=runtime_handle,
                )
                yield AgentMessage(
                    type="assistant",
                    content="Updated src/app.py",
                    data={
                        "subtype": "tool_result",
                        "tool_name": "Edit",
                        "tool_result": normalize_runtime_tool_result("Updated src/app.py"),
                    },
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        native_session_id="oc-session-7",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata={"runtime_event_type": "tool.completed"},
                    ),
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                )

        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=StubRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Wire OpenCode runtime events",
            session_id="sess_opencode",
            tools=["Edit"],
            system_prompt="test",
            seed_goal="Ship the adapter",
            depth=0,
            start_time=datetime.now(UTC),
        )

        appended_events = [call.args[0] for call in event_store.append.await_args_list]
        tool_started = next(
            event for event in appended_events if event.type == "execution.tool.started"
        )
        tool_completed = next(
            event for event in appended_events if event.type == "execution.tool.completed"
        )

        assert result.success is True
        assert tool_started.data["tool_definition"]["name"] == "Edit"
        assert tool_started.data["runtime_backend"] == "opencode"
        assert tool_started.data["runtime"]["native_session_id"] == "oc-session-7"
        assert tool_completed.data["tool_name"] == "Edit"
        assert tool_completed.data["tool_result"]["text_content"] == "Updated src/app.py"
        assert tool_completed.data["runtime_event_type"] == "tool.completed"

    @pytest.mark.asyncio
    async def test_atomic_ac_projects_empty_tool_result_content_into_completion_events(
        self,
    ) -> None:
        """Tool-result projection should preserve completion text even when message content is empty."""
        from ouroboros.orchestrator.mcp_tools import normalize_runtime_tool_result

        class StubRuntime:
            _runtime_handle_backend = "opencode"
            _cwd = "/tmp/project"
            _permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(self, **kwargs: Any):
                resume_handle = kwargs["resume_handle"]
                assert isinstance(resume_handle, RuntimeHandle)
                yield AgentMessage(
                    type="assistant",
                    content="",
                    data={
                        "subtype": "tool_result",
                        "tool_name": "Edit",
                        "tool_result": normalize_runtime_tool_result("[AC_COMPLETE: 1] Done!"),
                    },
                    resume_handle=RuntimeHandle(
                        backend="opencode",
                        native_session_id="oc-session-8",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata={"runtime_event_type": "tool.completed"},
                    ),
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                )

        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=StubRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Project OpenCode completion markers",
            session_id="sess_projection",
            tools=["Edit"],
            system_prompt="test",
            seed_goal="Ship the projection wiring",
            depth=0,
            start_time=datetime.now(UTC),
        )

        appended_events = [call.args[0] for call in event_store.append.await_args_list]
        tool_completed = next(
            event for event in appended_events if event.type == "execution.tool.completed"
        )

        assert result.success is True
        assert tool_completed.data["tool_result_text"] == "[AC_COMPLETE: 1] Done!"
        assert tool_completed.data["tool_result"]["text_content"] == "[AC_COMPLETE: 1] Done!"

    @pytest.mark.asyncio
    async def test_restarted_executor_skips_invalid_event_and_resumes_from_valid_one(
        self,
    ) -> None:
        """When an invalid persisted event precedes a valid one, resume from the valid event."""

        class _StubResumeAfterInvalidRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "acceptEdits"

            @property
            def runtime_backend(self) -> str:
                return self._runtime_handle_backend

            @property
            def working_directory(self) -> str | None:
                return self._cwd

            @property
            def permission_mode(self) -> str | None:
                return self._permission_mode

            async def execute_task(
                self,
                prompt: str,
                tools: list[str] | None = None,
                system_prompt: str | None = None,
                resume_handle: RuntimeHandle | None = None,
                resume_session_id: str | None = None,
            ):
                self.calls.append(
                    {
                        "prompt": prompt,
                        "tools": tools,
                        "system_prompt": system_prompt,
                        "resume_handle": resume_handle,
                        "resume_session_id": resume_session_id,
                    }
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                    resume_handle=resume_handle,
                )

        valid_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-valid",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "scope": "ac",
                "session_role": "implementation",
                "retry_attempt": 0,
                "ac_index": 1,
                "session_scope_id": "orch_123_ac_2",
                "session_state_path": (
                    "execution.workflows.orch_123.acceptance_criteria.ac_2.implementation_session"
                ),
                "server_session_id": "server-valid",
            },
        )
        event_store = AsyncMock()
        event_store.replay = AsyncMock(
            return_value=[
                # First event: valid handle
                BaseEvent(
                    type="execution.session.started",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_2.implementation_session"
                        ),
                        "runtime": valid_handle.to_dict(),
                    },
                ),
                # Second event: invalid handle (no backend/provider)
                BaseEvent(
                    type="execution.session.resumed",
                    aggregate_type="execution",
                    aggregate_id="orch_123_ac_2",
                    data={
                        "retry_attempt": 0,
                        "session_state_path": (
                            "execution.workflows.orch_123.acceptance_criteria."
                            "ac_2.implementation_session"
                        ),
                        "runtime": {
                            "kind": "implementation_session",
                            "cwd": "/tmp/project",
                            "metadata": {},
                        },
                    },
                ),
            ]
        )
        event_store.append = AsyncMock()
        runtime = _StubResumeAfterInvalidRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content="Resume after skipping invalid persisted event",
            session_id="orch_123",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=0,
        )

        resume_handle = runtime.calls[0]["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        # Should have resumed from the valid (first) event, not the invalid (second) one
        assert resume_handle.native_session_id == "opencode-session-valid"
        assert resume_handle.metadata["server_session_id"] == "server-valid"
        assert result.runtime_handle is not None
        assert result.runtime_handle.native_session_id == resume_handle.native_session_id
        assert result.runtime_handle.metadata == resume_handle.metadata


@pytest.mark.asyncio
async def test_try_decompose_ac_replaces_goose_chunks_with_final_result() -> None:
    """Goose can emit deltas plus a final full answer; decomposition should not duplicate."""

    class _GooseChunkAndFinalRuntime:
        runtime_backend = "goose"

        async def execute_task(
            self,
            prompt: str,
            tools: list[str] | None = None,
            system_prompt: str | None = None,
            resume_handle: RuntimeHandle | None = None,
            resume_session_id: str | None = None,
        ):
            del prompt, tools, system_prompt, resume_handle, resume_session_id
            yield AgentMessage(type="assistant", content='["Sub-AC 1: inspect", ')
            yield AgentMessage(type="assistant", content='"Sub-AC 2: test"]')
            yield AgentMessage(
                type="result",
                content='["Sub-AC 1: inspect", "Sub-AC 2: test"]',
            )

    executor = ParallelACExecutor(
        adapter=_GooseChunkAndFinalRuntime(),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=True,
    )

    result = await executor._try_decompose_ac(
        ac_content="Investigate and test sub-AC behavior.",
        ac_index=0,
        seed_goal="Verify Goose final result handling",
        tools=[],
        system_prompt="system",
    )

    assert result == ["Sub-AC 1: inspect", "Sub-AC 2: test"]


@pytest.mark.asyncio
async def test_try_decompose_ac_accumulates_goose_stream_chunks() -> None:
    """Goose stream-json emits token chunks; decomposition must parse accumulated output."""

    class _GooseChunkRuntime:
        runtime_backend = "goose"

        async def execute_task(
            self,
            prompt: str,
            tools: list[str] | None = None,
            system_prompt: str | None = None,
            resume_handle: RuntimeHandle | None = None,
            resume_session_id: str | None = None,
        ):
            del prompt, tools, system_prompt, resume_handle, resume_session_id
            yield AgentMessage(type="system", content="Session initialized: sess-1")
            for chunk in (
                '["Sub-AC 1: inspect the implementation", ',
                '"Sub-AC 2: write a focused regression test", ',
                '"Sub-AC 3: document the result"]',
            ):
                yield AgentMessage(type="assistant", content=chunk)

    executor = ParallelACExecutor(
        adapter=_GooseChunkRuntime(),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=True,
    )

    result = await executor._try_decompose_ac(
        ac_content="Investigate, test, and document sub-AC behavior.",
        ac_index=0,
        seed_goal="Verify Goose sub-AC support",
        tools=[],
        system_prompt="system",
    )

    assert result == [
        "Sub-AC 1: inspect the implementation",
        "Sub-AC 2: write a focused regression test",
        "Sub-AC 3: document the result",
    ]
