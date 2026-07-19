"""Tests for staged result handling in ParallelACExecutor."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    InvestmentSpec,
    OntologySchema,
    Seed,
    SeedMetadata,
    derive_semantic_ac_key,
)
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.types import MCPToolDefinition
from ouroboros.orchestrator.ac_execution_capsule import (
    AC_EXECUTION_CAPSULE_VERSION,
    ACExecutionCapsule,
    ACExecutionCapsuleManifest,
    compile_ac_execution_capsule,
)
from ouroboros.orchestrator.ac_runtime_handle_manager import (
    AmbiguousACExecutionError,
    CompletedACExecutionError,
)
from ouroboros.orchestrator.adapter import (
    FULL_CAPABILITIES,
    AgentMessage,
    ParamSupport,
    RuntimeCapabilities,
    RuntimeHandle,
)
from ouroboros.orchestrator.coordinator import CoordinatorReview, FileConflict
from ouroboros.orchestrator.decomposition_policy import DecompositionDisposition
from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph
from ouroboros.orchestrator.evidence.claims import _runtime_messages_support_file_claim
from ouroboros.orchestrator.evidence_schema import EvidenceRecord, ValidationResult
from ouroboros.orchestrator.execution_runtime_scope import (
    ACRuntimeIdentity,
    ExecutionNodeIdentity,
    build_ac_runtime_identity,
)
from ouroboros.orchestrator.leaf_dispatcher import _correlated_tool_result_name
from ouroboros.orchestrator.level_context import ACContextSummary, LevelContext
from ouroboros.orchestrator.parallel_executor import (
    _STALL_SENTINEL,
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
    _runtime_messages_have_masked_test_command_form,
    _runtime_messages_support_command_claim,
    _runtime_messages_support_test_claim,
    _VerifyGateOutcome,
    render_parallel_completion_message,
    render_parallel_verification_report,
)
from ouroboros.orchestrator.profile_loader import EvidenceSchema, load_profile
from ouroboros.orchestrator.rate_limit import RateLimitGate, SharedRateLimitBucket
from ouroboros.orchestrator.verifier import VerifierVerdict


def test_stall_timeout_default_allows_realistic_test_suites() -> None:
    """The default stall watchdog should not kill long quiet test commands too early."""
    assert STALL_TIMEOUT_SECONDS == 900.0


class _RateGateStubAdapter:
    """Minimal adapter exposing only what the dispatch rate gate inspects."""

    def __init__(self, *, runtime_backend: str, self_governs: bool) -> None:
        self.runtime_backend = runtime_backend
        self.self_governs_rate_limit = self_governs
        self.working_directory = "/workspace"
        self.permission_mode = "acceptEdits"


def _make_rate_gate_executor(adapter: _RateGateStubAdapter) -> ParallelACExecutor:
    return ParallelACExecutor(
        adapter=adapter,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
    )


class TestDispatchRateGate:
    """The executor governs delivery fan-out within the backend's rate budget."""

    def test_gate_dormant_for_self_governing_adapter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Claude self-governs via its own bucket — the executor must not add a
        # second gate, even though "claude" declares an RPM in the registry.
        monkeypatch.setenv("OUROBOROS_BACKEND_LIMITS", str(tmp_path / "absent.yaml"))
        adapter = _RateGateStubAdapter(runtime_backend="claude", self_governs=True)

        executor = _make_rate_gate_executor(adapter)

        assert executor._dispatch_rate_gate.enabled is False

    def test_gate_dormant_for_cli_backend_without_configuration(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Default behavior is unchanged: no configured RPM/TPM → no pacing.
        monkeypatch.setenv("OUROBOROS_BACKEND_LIMITS", str(tmp_path / "absent.yaml"))
        monkeypatch.delenv("OUROBOROS_OPENCODE_RPM", raising=False)
        adapter = _RateGateStubAdapter(runtime_backend="opencode", self_governs=False)

        executor = _make_rate_gate_executor(adapter)

        assert executor._dispatch_rate_gate.enabled is False

    def test_gate_activates_for_cli_backend_with_configured_rpm(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("OUROBOROS_BACKEND_LIMITS", str(tmp_path / "absent.yaml"))
        monkeypatch.setenv("OUROBOROS_OPENCODE_RPM", "2")
        adapter = _RateGateStubAdapter(runtime_backend="opencode", self_governs=False)

        executor = _make_rate_gate_executor(adapter)

        assert executor._dispatch_rate_gate.enabled is True

    @pytest.mark.asyncio
    async def test_await_dispatch_rate_budget_no_op_when_dormant(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("OUROBOROS_BACKEND_LIMITS", str(tmp_path / "absent.yaml"))
        adapter = _RateGateStubAdapter(runtime_backend="opencode", self_governs=False)
        executor = _make_rate_gate_executor(adapter)

        # Dormant gate: returns immediately, no error.
        await executor._await_dispatch_rate_budget(prompt="hello", system_prompt=None)

    @pytest.mark.asyncio
    async def test_await_dispatch_rate_budget_paces_through_active_gate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setenv("OUROBOROS_BACKEND_LIMITS", str(tmp_path / "absent.yaml"))
        adapter = _RateGateStubAdapter(runtime_backend="opencode", self_governs=False)
        executor = _make_rate_gate_executor(adapter)

        # Swap in a deterministic gate (rpm=1, fake clock + sleep) to prove the
        # executor's dispatch hook actually waits on the shared budget.
        clock = {"now": 0.0}
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            clock["now"] += seconds

        bucket = SharedRateLimitBucket(
            runtime_backend="opencode",
            request_limit=1,
            token_limit=None,
            time_provider=lambda: clock["now"],
        )
        executor._dispatch_rate_gate = RateLimitGate(
            bucket, heartbeat_seconds=120.0, sleep=fake_sleep
        )

        await executor._await_dispatch_rate_budget(prompt="a", system_prompt=None)
        await executor._await_dispatch_rate_budget(prompt="b", system_prompt=None)

        assert slept == [60.0]  # second dispatch waited a full window


def _make_seed(*acceptance_criteria: str | AcceptanceCriterionSpec) -> Seed:
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


def _compile_test_capsule(
    *,
    executor: ParallelACExecutor,
    ac_index: int,
    ac_content: str,
    session_id: str,
    seed_goal: str,
    retry_attempt: int = 0,
    workspace: str = "/tmp/project",
    node_identity: ExecutionNodeIdentity | None = None,
    ac_spec: AcceptanceCriterionSpec | None = None,
) -> tuple[ACRuntimeIdentity, ACExecutionCapsule]:
    identity = build_ac_runtime_identity(
        ac_index,
        execution_context_id=session_id,
        node_identity=node_identity,
        retry_attempt=retry_attempt,
    )
    capsule = compile_ac_execution_capsule(
        runtime_identity=identity,
        execution_id=session_id,
        semantic_ac_key=derive_semantic_ac_key(ac_spec or ac_content),
        workspace=os.path.realpath(workspace),
        authority_scope=executor._build_ac_capsule_authority_scope(
            execution_context_id=session_id,
            tools=["Read", "Edit"],
            tool_catalog=None,
            system_prompt="system",
            level_contexts=None,
            is_sub_ac=False,
            decomposition_trustworthy=False,
            force_frontier_routing=False,
            investment_spec=None,
        ),
        seed_goal=seed_goal,
        ac_content=ac_content,
        ac_spec=ac_spec,
    )
    return identity, capsule


def _compiled_capsule_event(
    identity: ACRuntimeIdentity,
    capsule: ACExecutionCapsule,
) -> BaseEvent:
    return BaseEvent(
        type="execution.ac.capsule.compiled",
        aggregate_type="execution",
        aggregate_id=identity.session_scope_id,
        data={
            **identity.to_metadata(),
            "capsule_fingerprint": capsule.fingerprint,
            "capsule_manifest": capsule.manifest.to_contract_data(),
        },
    )


def _dispatched_capsule_event(
    identity: ACRuntimeIdentity,
    capsule: ACExecutionCapsule,
    *,
    dispatch_id: str = "d" * 32,
    previous_dispatch_id: str | None = None,
    session_origin: str = "fresh",
    runtime_handle: RuntimeHandle | None = None,
    dispatch_kind: str | None = None,
    signal_id: str | None = None,
    signal_mode: str | None = None,
    follow_up_input_digest: str | None = None,
) -> BaseEvent:
    if runtime_handle is not None:
        runtime_handle = replace(
            runtime_handle,
            metadata={**runtime_handle.metadata, "ac_dispatch_id": dispatch_id},
        )
    data: dict[str, Any] = {
        **identity.to_metadata(),
        "ac_dispatch_id": dispatch_id,
        "previous_ac_dispatch_id": previous_dispatch_id,
        "capsule_fingerprint": capsule.fingerprint,
        "session_origin": session_origin,
        "runtime": (
            runtime_handle.to_dispatch_recovery_dict() if runtime_handle is not None else None
        ),
    }
    # Only stamp the phase-identity fields when a caller opts in, so the default
    # event shape reproduces a legacy dispatch (no ``dispatch_kind``) — the
    # backward-compatible "treat absent as primary" recovery path.
    if dispatch_kind is not None:
        data["dispatch_kind"] = dispatch_kind
        data["signal_id"] = signal_id
        data["signal_mode"] = signal_mode
        data["follow_up_input_digest"] = follow_up_input_digest
    return BaseEvent(
        type="execution.ac.attempt.dispatched",
        aggregate_type="execution",
        aggregate_id=identity.session_scope_id,
        data=data,
    )


def _sealed_dispatch_event(
    identity: ACRuntimeIdentity,
    capsule: ACExecutionCapsule,
    *,
    dispatch_id: str,
) -> BaseEvent:
    return BaseEvent(
        type="execution.ac.dispatch.sealed",
        aggregate_type="execution",
        aggregate_id=identity.session_scope_id,
        data={
            **identity.to_metadata(),
            "ac_dispatch_id": dispatch_id,
            "capsule_fingerprint": capsule.fingerprint,
        },
    )


def _dispatch_lifecycle_event(
    identity: ACRuntimeIdentity,
    event_type: str,
    *,
    dispatch_id: str,
    runtime_handle: RuntimeHandle | None,
    timestamp: datetime | None = None,
    result_summary: str | None = None,
    session_id: str | None = None,
    extra_data: dict[str, Any] | None = None,
) -> BaseEvent:
    if runtime_handle is not None:
        runtime_handle = replace(
            runtime_handle,
            metadata={**runtime_handle.metadata, "ac_dispatch_id": dispatch_id},
        )
    return BaseEvent(
        type=event_type,
        timestamp=timestamp or datetime.now(UTC),
        aggregate_type="execution",
        aggregate_id=identity.session_scope_id,
        data={
            **identity.to_metadata(),
            "ac_dispatch_id": dispatch_id,
            "success": True
            if event_type == "execution.session.completed"
            else False
            if event_type == "execution.session.failed"
            else None,
            "result_summary": result_summary,
            "session_id": session_id,
            "runtime": runtime_handle.to_persisted_dict() if runtime_handle is not None else None,
            **(extra_data or {}),
        },
    )


_GLOBAL_VERIFIER_PASS = True


def _global_state_verifier(**_kwargs: Any) -> VerifierVerdict:
    return VerifierVerdict(passed=_GLOBAL_VERIFIER_PASS)


def test_capsule_authority_binds_subprocess_executable_path() -> None:
    """R2 blocker #3: which binary the runtime launches is part of authority.

    Two otherwise-identical adapters whose ``cli_path`` points at different
    executables must produce different capsule dispatch authority, so a restart
    cannot resume the same capsule under a different executable.
    """

    class _Runtime:
        runtime_backend = "codex_cli"
        working_directory = "/tmp/project"
        permission_mode = "acceptEdits"

        def __init__(self, cli_path: str) -> None:
            self._cli_path = cli_path

    def _authority(cli_path: str) -> dict[str, object]:
        executor = ParallelACExecutor(
            adapter=_Runtime(cli_path),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        return executor._build_capsule_dispatch_authority_contract(
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
            level_contexts=None,
        )

    true_authority = _authority("/bin/true")
    false_authority = _authority("/bin/false")

    true_executable = true_authority["runtime"]["executable"]  # type: ignore[index]
    false_executable = false_authority["runtime"]["executable"]  # type: ignore[index]
    assert true_executable["observed"] is True
    assert true_executable["executable"]["path"] == "/bin/true"
    assert false_executable["executable"]["path"] == "/bin/false"
    # The whole dispatch authority contract — the fingerprint input — must differ.
    assert true_authority != false_authority


def test_runtime_executable_identity_binds_zcode_launcher() -> None:
    """R2 blocker #3: the Electron/Node launcher is part of what executes."""

    class _ZcodeLike:
        _cli_path = "/opt/zcode/cli.js"
        _electron_node_path = "/opt/zcode/node"

    identity = ParallelACExecutor._runtime_executable_identity(_ZcodeLike())
    assert identity["observed"] is True
    assert identity["executable"]["path"] == "/opt/zcode/cli.js"
    assert identity["launcher"]["path"] == "/opt/zcode/node"


def test_runtime_executable_identity_follows_delegated_transport() -> None:
    """R3 blocker #4: a runtime whose real CLI lives on a wrapped transport.

    ``LeaderDrivenWorkerRuntime`` keeps the launched binary under
    ``_transport._cli_path``. Two such runtimes at different transport binaries
    must not both fingerprint as ``observed=False`` / identical authority.
    """

    class _Transport:
        def __init__(self, cli_path: str) -> None:
            self._cli_path = cli_path

    class _WrappingRuntime:
        # Mirrors LeaderDrivenWorkerRuntime.executable_identity_contract delegating
        # to its transport's executable.
        def __init__(self, cli_path: str) -> None:
            self._transport = _Transport(cli_path)

        def executable_identity_contract(self) -> dict[str, str | None]:
            return {"executable": self._transport._cli_path, "launcher": None}

    true_identity = ParallelACExecutor._runtime_executable_identity(_WrappingRuntime("/bin/true"))
    false_identity = ParallelACExecutor._runtime_executable_identity(_WrappingRuntime("/bin/false"))
    assert true_identity["observed"] is True
    assert false_identity["observed"] is True
    assert true_identity["executable"]["path"] == "/bin/true"
    assert false_identity["executable"]["path"] == "/bin/false"
    assert true_identity != false_identity


def test_runtime_executable_identity_probes_transport_without_contract() -> None:
    """R3 blocker #4: even without an explicit contract, a wrapped transport's
    ``_cli_path`` is probed one delegation level so the launcher still binds."""

    class _Transport:
        _cli_path = "/usr/local/bin/worker-cli"

    class _WrappingRuntime:
        _transport = _Transport()

    identity = ParallelACExecutor._runtime_executable_identity(_WrappingRuntime())
    assert identity["observed"] is True
    assert identity["executable"]["path"] == "/usr/local/bin/worker-cli"


def test_capsule_authority_covers_prompt_gate_runtime_and_verifier_inputs() -> None:
    """Every input that can alter provider work or acceptance must change authority."""

    class _Runtime:
        runtime_backend = "codex_cli"
        working_directory = "/tmp/project"
        permission_mode = "acceptEdits"

        def __init__(self, profile: str) -> None:
            self.profile = profile

        def execution_identity_contract(self) -> dict[str, object]:
            return {"profile": self.profile, "effective_model_observed": True}

    def verifier_a(**_kwargs: Any) -> VerifierVerdict:
        return VerifierVerdict(passed=True)

    def verifier_b(**_kwargs: Any) -> VerifierVerdict:
        return VerifierVerdict(passed=False)

    base = ParallelACExecutor(
        adapter=_Runtime("profile-a"),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        atomic_verifier=verifier_a,
    )

    def scope(executor: ParallelACExecutor, **overrides: Any) -> str:
        kwargs: dict[str, Any] = {
            "execution_context_id": "exec-authority",
            "tools": ["Read"],
            "tool_catalog": None,
            "system_prompt": "system",
            "level_contexts": None,
            "is_sub_ac": False,
            "decomposition_trustworthy": False,
            "force_frontier_routing": False,
            "investment_spec": None,
            "sibling_acs": [(0, "current"), (1, "sibling A")],
            "retry_prompt_extra": "retry A",
        }
        kwargs.update(overrides)
        return executor._build_ac_capsule_authority_scope(**kwargs)

    baseline = scope(base)
    assert scope(base, retry_prompt_extra="retry B") != baseline
    assert scope(base, sibling_acs=[(0, "current"), (1, "sibling B")]) != baseline

    fat = ParallelACExecutor(
        adapter=_Runtime("profile-a"),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        fat_harness_mode=True,
        atomic_verifier=verifier_a,
    )
    assert scope(fat) != baseline

    verify_off = ParallelACExecutor(
        adapter=_Runtime("profile-a"),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        run_verify_commands=False,
        atomic_verifier=verifier_a,
    )
    assert scope(verify_off) != baseline

    runtime_drift = ParallelACExecutor(
        adapter=_Runtime("profile-b"),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        atomic_verifier=verifier_a,
    )
    assert scope(runtime_drift) != baseline

    verifier_drift = ParallelACExecutor(
        adapter=_Runtime("profile-a"),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        atomic_verifier=verifier_b,
    )
    assert scope(verifier_drift) != baseline


def test_capsule_authority_distinguishes_verifier_closure_state() -> None:
    """Factory-created judges with the same source must not share authority."""

    def configured_verifier(passed: bool):
        def verifier(**_kwargs: Any) -> VerifierVerdict:
            return VerifierVerdict(passed=passed)

        return verifier

    runtime = SimpleNamespace(
        runtime_backend="codex_cli",
        working_directory="/tmp/project",
        permission_mode="acceptEdits",
    )
    passing = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        atomic_verifier=configured_verifier(True),
    )
    rejecting = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        atomic_verifier=configured_verifier(False),
    )

    assert passing._atomic_verifier_authority != rejecting._atomic_verifier_authority
    assert passing._atomic_verifier_authority["behavioral_state"]["stability"] == "process_local"


def test_capsule_authority_distinguishes_callable_verifier_state() -> None:
    """Callable instances bind their configured acceptance behavior."""

    class ConfiguredVerifier:
        def __init__(self, passed: bool) -> None:
            self.passed = passed

        def __call__(self, **_kwargs: Any) -> VerifierVerdict:
            return VerifierVerdict(passed=self.passed)

        def verification_identity_contract(self) -> dict[str, object]:
            return {"version": 1, "passed": self.passed}

    runtime = SimpleNamespace(
        runtime_backend="codex_cli",
        working_directory="/tmp/project",
        permission_mode="acceptEdits",
    )
    passing = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        atomic_verifier=ConfiguredVerifier(True),
    )
    rejecting = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        atomic_verifier=ConfiguredVerifier(False),
    )
    matching = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        atomic_verifier=ConfiguredVerifier(True),
    )

    assert passing._atomic_verifier_authority != rejecting._atomic_verifier_authority
    assert passing._atomic_verifier_authority == matching._atomic_verifier_authority
    assert passing._atomic_verifier_authority["behavioral_state"]["stability"] == "durable"


def test_capsule_authority_distinguishes_referenced_verifier_globals(monkeypatch) -> None:
    """Changing a module-global policy cannot preserve acceptance authority."""
    runtime = SimpleNamespace(
        runtime_backend="codex_cli",
        working_directory="/tmp/project",
        permission_mode="acceptEdits",
    )
    monkeypatch.setitem(_global_state_verifier.__globals__, "_GLOBAL_VERIFIER_PASS", True)
    passing = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        atomic_verifier=_global_state_verifier,
    )
    monkeypatch.setitem(_global_state_verifier.__globals__, "_GLOBAL_VERIFIER_PASS", False)
    rejecting = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        atomic_verifier=_global_state_verifier,
    )

    assert passing._atomic_verifier_authority != rejecting._atomic_verifier_authority
    assert passing._atomic_verifier_authority["behavioral_state"]["stability"] == "process_local"


def test_capsule_authority_reuses_large_context_and_catalog_digests(monkeypatch) -> None:
    """Sibling ACs and retries must not reserialize the same authority inputs."""
    import ouroboros.orchestrator.mcp_tools as mcp_tools
    import ouroboros.orchestrator.parallel_executor as pe

    runtime = SimpleNamespace(
        runtime_backend="codex_cli",
        working_directory="/tmp/project",
        permission_mode="acceptEdits",
    )
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
    )
    level_contexts = [
        LevelContext(
            level_number=index,
            completed_acs=(
                ACContextSummary(
                    ac_index=index,
                    ac_content=f"Dependency {index}",
                    success=True,
                ),
            ),
        )
        for index in range(8)
    ]
    tool_catalog = (
        MCPToolDefinition(name="Read", description="Read files"),
        MCPToolDefinition(name="Edit", description="Edit files"),
    )
    real_serialize_levels = pe.serialize_level_contexts
    real_serialize_tools = mcp_tools.serialize_tool_catalog
    level_batch_sizes: list[int] = []
    tool_calls = 0

    def count_levels(contexts):
        level_batch_sizes.append(len(contexts))
        return real_serialize_levels(contexts)

    def count_tools(catalog):
        nonlocal tool_calls
        tool_calls += 1
        return real_serialize_tools(catalog)

    monkeypatch.setattr(pe, "serialize_level_contexts", count_levels)
    monkeypatch.setattr(mcp_tools, "serialize_tool_catalog", count_tools)

    for _ in range(3):
        executor._build_ac_capsule_authority_scope(
            execution_context_id="exec-cache",
            tools=["Read", "Edit"],
            tool_catalog=tool_catalog,
            system_prompt="system",
            level_contexts=level_contexts,
            is_sub_ac=False,
            decomposition_trustworthy=False,
            force_frontier_routing=False,
            investment_spec=None,
        )
        executor._capsule_dependency_references(
            execution_id="exec-cache",
            level_contexts=level_contexts,
        )

    assert tool_calls == 1
    assert level_batch_sizes == [1] * len(level_contexts)


def test_level_context_authority_digest_extends_incrementally(monkeypatch) -> None:
    """Sequential stage growth serializes each accepted handoff exactly once."""
    import ouroboros.orchestrator.parallel_executor as pe

    executor = ParallelACExecutor(
        adapter=SimpleNamespace(
            runtime_backend="codex_cli",
            working_directory="/tmp/project",
            permission_mode="acceptEdits",
        ),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
    )
    real_serialize = pe.serialize_level_contexts
    serialized_level_numbers: list[int] = []

    def count_levels(contexts):
        serialized_level_numbers.extend(context.level_number for context in contexts)
        return real_serialize(contexts)

    monkeypatch.setattr(pe, "serialize_level_contexts", count_levels)
    contexts: list[LevelContext] = []
    for level_number in range(20):
        contexts = executor._merge_level_context(
            contexts,
            LevelContext(level_number=level_number, completed_acs=()),
        )
        executor._level_context_chain_digest(contexts)

    assert serialized_level_numbers == list(range(20))


@pytest.mark.parametrize(
    ("content", "expected"),
    (
        ("3 passed in 1.2s", True),
        ("0 failed, 3 passed", True),
        ("0 failed, 0 errors, 1 passed", True),
        ("Tests run: 3, Failures: 0, Errors: 0, Skipped: 0", False),
        ("Tests run: 3, Failures: 1, Errors: 0, Skipped: 0", False),
        ("Tests run: 3, Failures: 0, Errors: 0, Skipped: 0\n[INFO] BUILD SUCCESS", True),
        ("Tests run: 3, Failures=0, Errors=0, Skipped=0\n[INFO] BUILD SUCCESS", True),
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


@pytest.mark.parametrize("python_executable", ("python3", "python3.12", "/usr/bin/python3"))
def test_tests_passed_accepts_versioned_python_codex_command(
    python_executable: str,
) -> None:
    """Sol's shell-wrapped versioned Python pytest run is formal test evidence."""
    claim = f"{python_executable} -m pytest --doctest-modules -q hello.py"
    message = AgentMessage(
        type="assistant",
        content=f"Calling tool: Bash: /bin/zsh -lc '{claim}'",
        tool_name="Bash",
        data={
            "tool_input": {"command": f"/bin/zsh -lc '{claim}'"},
            "output": ". [100%]\n1 passed in 0.01s",
            "exit_code": 0,
            "status": "completed",
        },
    )

    assert _runtime_messages_support_test_claim(
        value=claim,
        backed_commands=(claim,),
        messages=(message,),
        task_cwd=None,
    )


@pytest.mark.parametrize(("is_error", "expected"), ((False, True), (True, False)))
def test_tests_passed_respects_correlated_bash_result_status(
    is_error: bool,
    expected: bool,
) -> None:
    """A failed Bash result vetoes success text from the same tool call."""
    command = "pytest tests/test_app.py"
    started = AgentMessage(
        type="tool",
        content="run tests",
        tool_name="Bash",
        data={
            "tool_call_id": "bash_test_1",
            "tool_input": {"command": command},
        },
    )
    completed = AgentMessage(
        type="tool_result",
        content="1 passed in 0.01s",
        data={
            "subtype": "tool_result",
            "tool_call_id": "bash_test_1",
            "is_error": is_error,
            "output": "1 passed in 0.01s",
        },
    )

    assert (
        _runtime_messages_support_test_claim(
            value=command,
            backed_commands=(command,),
            messages=(started, completed),
            task_cwd=None,
        )
        is expected
    )


def test_tests_passed_rejects_node_id_from_assistant_narration_only() -> None:
    """A broad successful run cannot prove a node-id mentioned only by the agent."""
    started = AgentMessage(
        type="tool",
        content="run suite",
        tool_name="Bash",
        data={
            "tool_input": {"command": "pytest"},
            "exit_code": 0,
            "output": "1 passed in 0.01s",
        },
    )
    narration = AgentMessage(
        type="assistant",
        content="tests/test_unobserved.py::test_unobserved passed",
        data={},
    )

    assert not _runtime_messages_support_test_claim(
        value="tests/test_unobserved.py::test_unobserved",
        backed_commands=("pytest",),
        messages=(started, narration),
        task_cwd=None,
    )


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
        success: bool = True,
    ) -> None:
        self._final_message = final_message
        self._native_session_id = native_session_id
        self._support_messages = support_messages
        self._cwd = cwd
        self._success = success
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
            if (
                message.tool_name in {"Edit", "Write", "NotebookEdit"}
                and "subtype" not in message.data
                and "runtime_event_type" not in message.data
                and "exit_code" not in message.data
            ):
                # These scripted support messages model already-completed
                # OpenCode/Codex file-change events, not bare dispatch starts.
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
            data={"subtype": "success" if self._success else "error"},
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


def test_gradle_command_claim_supports_quoted_target_and_tail_pipe() -> None:
    """A clean Gradle claim matches a quoted runtime command with output plumbing."""
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={
            "tool_input": {
                "command": (
                    "/bin/bash -lc 'set -o pipefail && ./gradlew test "
                    '--tests "com.example.app.unit.SomeNewTest" -i 2>&1 | tail -100\''
                )
            }
        },
    )

    assert _runtime_messages_support_command_claim(
        "./gradlew test --tests com.example.app.unit.SomeNewTest -i",
        (message,),
    )


def test_gradle_tests_passed_claim_supports_class_target_and_build_success() -> None:
    """Gradle BUILD SUCCESSFUL output can back a class-level tests_passed claim."""
    command_message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={
            "tool_input": {
                "command": (
                    "/bin/bash -lc 'set -o pipefail && ./gradlew test "
                    '--tests "com.example.app.unit.SomeNewTest" -i 2>&1 | tail -100\''
                )
            }
        },
    )
    result_message = AgentMessage(
        type="tool_result",
        content="> Task :test\nBUILD SUCCESSFUL in 8s",
        tool_name=None,
        data={
            "subtype": "tool_result",
            "output": "> Task :test\nBUILD SUCCESSFUL in 8s",
        },
    )

    assert _runtime_messages_support_test_claim(
        value="com.example.app.unit.SomeNewTest",
        backed_commands=("./gradlew test --tests com.example.app.unit.SomeNewTest -i",),
        messages=(command_message, result_message),
        task_cwd=None,
    )


def test_unprotected_tail_pipe_is_form_mismatch_not_command_proof() -> None:
    """A bare output-filter pipe is visible but still not trusted as proof."""
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={
            "tool_input": {
                "command": (
                    './gradlew test --tests "com.example.app.unit.SomeNewTest" -i 2>&1 | tail -100'
                )
            }
        },
    )

    assert not _runtime_messages_support_command_claim(
        "./gradlew test --tests com.example.app.unit.SomeNewTest -i",
        (message,),
    )
    assert _runtime_messages_have_masked_test_command_form(
        "./gradlew test --tests com.example.app.unit.SomeNewTest -i",
        (message,),
    )


def test_atomic_verifier_classifies_masked_test_command_as_form_mismatch() -> None:
    """Masked test commands are contract mismatches, not fabricated work."""
    profile = load_profile("code").model_copy(
        update={"evidence_schema": EvidenceSchema(required=("commands_run",))}
    )
    executor = ParallelACExecutor(
        adapter=MagicMock(working_directory="/workspace"),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=profile,
        fat_harness_mode=True,
    )

    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=(
            AgentMessage(
                type="tool",
                content="Bash command started",
                tool_name="Bash",
                data={
                    "tool_input": {
                        "command": (
                            './gradlew test --tests "com.example.app.unit.SomeNewTest" '
                            "-i 2>&1 | tail -100"
                        )
                    }
                },
            ),
        ),
        typed_evidence=EvidenceRecord(
            data={"commands_run": ["./gradlew test --tests com.example.app.unit.SomeNewTest -i"]}
        ),
        ac_content="Run SomeNewTest.",
    )

    assert verdict.passed is False
    assert verdict.failure_class == "EVIDENCE_FORM_MISMATCH"
    assert "unprotected output-filter pipeline" in " ".join(verdict.reasons)


def test_atomic_verifier_classifies_dependent_masked_test_evidence_as_form_mismatch(
    tmp_path,
) -> None:
    """Full code evidence with masked test output is one evidence-form mismatch."""
    source_file = tmp_path / "src" / "app.py"
    executor = ParallelACExecutor(
        adapter=MagicMock(working_directory=str(tmp_path)),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
    )

    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=(
            AgentMessage(
                type="tool",
                content="Edit src/app.py",
                tool_name="Edit",
                data={
                    "tool_call_id": "edit_app",
                    "tool_input": {"file_path": str(source_file)},
                },
            ),
            AgentMessage(
                type="tool_result",
                content="updated",
                data={
                    "subtype": "tool_result",
                    "tool_call_id": "edit_app",
                    "is_error": False,
                },
            ),
            AgentMessage(
                type="tool",
                content="Bash command started",
                tool_name="Bash",
                data={
                    "tool_input": {
                        "command": (
                            './gradlew test --tests "com.example.app.unit.SomeNewTest" '
                            "-i 2>&1 | tail -100"
                        )
                    }
                },
            ),
            AgentMessage(
                type="tool_result",
                content="> Task :test\nBUILD SUCCESSFUL in 8s",
                tool_name=None,
                data={
                    "subtype": "tool_result",
                    "output": "> Task :test\nBUILD SUCCESSFUL in 8s",
                },
            ),
        ),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["src/app.py"],
                "commands_run": ["./gradlew test --tests com.example.app.unit.SomeNewTest -i"],
                "tests_passed": ["com.example.app.unit.SomeNewTest"],
            }
        ),
        ac_content="Update app.py and run SomeNewTest.",
    )

    assert verdict.passed is False
    assert verdict.failure_class == "EVIDENCE_FORM_MISMATCH"
    assert (
        "commands_run: ./gradlew test --tests com.example.app.unit.SomeNewTest -i"
        in (verdict.reasons[0])
    )
    assert "tests_passed: com.example.app.unit.SomeNewTest" in verdict.reasons[0]


def _greeting_repro_messages(edit_path: str) -> tuple[AgentMessage, ...]:
    """Build the maintainer's live codex transcript for the greeting seed.

    The Edit event records an absolute disposable-repo path while the Bash test
    command is wrapped in ``/bin/zsh -lc "..."`` with the inner ``python3 -c``
    payload requoted, matching the exact shapes that broke fat-harness matching.
    """
    wrapped_command = (
        '/bin/zsh -lc "python3 -c \\"from hello import greet; '
        "assert greet('World') == 'Hello, World!'; print('OK')\\\"\""
    )
    return (
        AgentMessage(
            type="tool",
            content="Edit hello.py",
            tool_name="Edit",
            data={
                "tool_call_id": "edit_hello",
                "tool_input": {"file_path": edit_path},
            },
        ),
        AgentMessage(
            type="tool_result",
            content="updated hello.py",
            data={
                "subtype": "tool_result",
                "tool_call_id": "edit_hello",
                "is_error": False,
            },
        ),
        AgentMessage(
            type="tool",
            content="run verification",
            tool_name="Bash",
            data={"tool_input": {"command": wrapped_command}},
        ),
        AgentMessage(
            type="tool_result",
            content="OK",
            tool_name=None,
            data={"subtype": "tool_result", "output": "OK", "exit_code": 0},
        ),
        AgentMessage(type="result", content="done", data={}),
    )


_GREETING_AC = "hello.py defines greet(name) returning the string Hello, <name>"
_GREETING_INNER_COMMAND = (
    "python3 -c \"from hello import greet; assert greet('World') == 'Hello, World!'; print('OK')\""
)


def test_atomic_verifier_rejects_absolute_transcript_path_when_cwd_is_unknown() -> None:
    """An arbitrary absolute Edit path cannot prove workspace ownership."""
    executor = ParallelACExecutor(
        adapter=MagicMock(working_directory=None),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
        task_cwd=None,
    )

    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=_greeting_repro_messages("/private/tmp/ooo-repro-blos/hello.py"),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["hello.py"],
                "commands_run": [_GREETING_INNER_COMMAND],
            }
        ),
        ac_content=_GREETING_AC,
        has_success_contract=True,
        verify_gate_active=True,
    )

    assert verdict.passed is False
    assert verdict.failure_class == "FABRICATION_SUSPECTED"
    assert "files_touched: hello.py" in verdict.reasons[0]


def test_atomic_verifier_accepts_symlinked_cwd_against_resolved_transcript_path(
    tmp_path,
) -> None:
    """The resolve tier matches a symlinked run cwd against a resolved transcript path.

    Portable stand-in for the macOS ``/tmp`` -> ``/private/tmp`` layout: create a
    real symlink inside ``tmp_path``, run with ``task_cwd`` set to the symlinked
    directory, and have the transcript record the Edit under the resolved real
    directory. ``Path.resolve`` on both sides must treat them as the same file.
    """
    real_dir = tmp_path / "real_workspace"
    real_dir.mkdir()
    (real_dir / "hello.py").write_text("def greet(name):\n    return f'Hello, {name}!'\n")
    link_dir = tmp_path / "linked_workspace"
    try:
        os.symlink(real_dir, link_dir, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - unprivileged/Windows
        pytest.skip("symlink creation not permitted in this environment")

    assert link_dir.resolve() == real_dir.resolve()
    assert str(link_dir) != str(real_dir)

    executor = ParallelACExecutor(
        adapter=MagicMock(working_directory=str(link_dir)),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
        # Run cwd is the symlink; the transcript path below is under the real dir.
        task_cwd=str(link_dir),
    )
    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=_greeting_repro_messages(str(real_dir / "hello.py")),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["hello.py"],
                "commands_run": [_GREETING_INNER_COMMAND],
            }
        ),
        ac_content=_GREETING_AC,
        has_success_contract=True,
        verify_gate_active=True,
    )
    assert verdict.passed is True, verdict.reasons


def test_atomic_verifier_rejects_fabricated_greeting_claims_without_transcript() -> None:
    """A near-miss filename and a near-miss command payload still fail (no real event)."""
    executor = ParallelACExecutor(
        adapter=MagicMock(working_directory=None),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
        task_cwd=None,
    )

    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=_greeting_repro_messages("/private/tmp/ooo-repro-blos/hello.py"),
        typed_evidence=EvidenceRecord(
            data={
                # Different filename than the transcript Edit event.
                "files_touched": ["goodbye.py"],
                # Different command payload than the transcript Bash event.
                "commands_run": ["python3 -c \"from hello import greet; print(greet('x'))\""],
                "tests_passed": [
                    "python3 -c \"from hello import greet; assert greet('World') == 'WRONG'\""
                ],
            }
        ),
        ac_content=_GREETING_AC,
    )

    assert verdict.passed is False
    assert verdict.failure_class == "FABRICATION_SUSPECTED"
    assert "files_touched: goodbye.py" in verdict.reasons[0]


def _file_scope_executor(task_cwd: str | None) -> ParallelACExecutor:
    return ParallelACExecutor(
        adapter=MagicMock(working_directory=task_cwd),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
        task_cwd=task_cwd,
    )


def test_files_touched_rejects_absolute_outside_workspace_claim_with_touch(tmp_path) -> None:
    """Scope guard: an outside-workspace absolute claim cannot be backed by ``touch``.

    The bot's repro: ``task_cwd`` is a subdir, ``files_touched`` names a sibling
    file outside it, and a ``touch <outside>`` command + exit 0 must NOT satisfy
    the claim. ``files_touched`` is contractually workspace-scoped.
    """
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "hello.py").write_text("x = 1\n")
    outside = tmp_path / "outside.py"
    executor = _file_scope_executor(str(workspace))

    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=(
            AgentMessage(
                type="tool",
                content="touch outside",
                tool_name="Bash",
                data={"tool_input": {"command": f"touch {outside}"}, "exit_code": 0},
            ),
            AgentMessage(
                type="tool",
                content="pytest",
                tool_name="Bash",
                data={"tool_input": {"command": "pytest"}, "exit_code": 0, "output": "1 passed"},
            ),
            AgentMessage(type="result", content="done", data={}),
        ),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": [str(outside)],
                "commands_run": [f"touch {outside}", "pytest"],
                "tests_passed": ["pytest"],
            }
        ),
        ac_content="Implement the module.",
    )

    assert verdict.passed is False
    assert "files_touched: " in verdict.reasons[0]


def test_files_touched_rejects_parent_traversal_claim_escaping_cwd(tmp_path) -> None:
    """Scope guard: a ``../`` claim escaping the workspace is rejected."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    executor = _file_scope_executor(str(workspace))

    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=(
            AgentMessage(
                type="tool",
                content="touch escape",
                tool_name="Bash",
                data={"tool_input": {"command": "touch ../outside.py"}, "exit_code": 0},
            ),
            AgentMessage(type="result", content="done", data={}),
        ),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["../outside.py"],
                "commands_run": ["touch ../outside.py"],
                "tests_passed": ["pytest"],
            }
        ),
        ac_content="Implement the module.",
    )

    assert verdict.passed is False
    assert "files_touched: ../outside.py" in verdict.reasons[0]


def test_files_touched_accepts_in_workspace_relative_vs_absolute_edit(tmp_path) -> None:
    """In-workspace relative claim matches an absolute Edit path (form mismatch)."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "hello.py").write_text("def greet(name):\n    return f'Hello, {name}!'\n")
    edit = AgentMessage(
        type="tool",
        content="edit",
        tool_name="Edit",
        data={
            "tool_call_id": "edit_hello",
            "tool_input": {"file_path": str(workspace / "hello.py")},
        },
    )
    completed = AgentMessage(
        type="tool_result",
        content="updated",
        data={
            "subtype": "tool_result",
            "tool_call_id": "edit_hello",
            "is_error": False,
        },
    )

    assert _runtime_messages_support_file_claim(
        "hello.py",
        (edit, completed),
        task_cwd=str(workspace),
    )


def test_files_touched_task_cwd_none_rejects_touch_command_text() -> None:
    """Workspace unknown: only structured Edit/Write proves files, not ``touch`` text."""
    touch = AgentMessage(
        type="tool",
        content="touch outside",
        tool_name="Bash",
        data={"tool_input": {"command": "touch /tmp/evil/outside.py"}, "exit_code": 0},
    )
    # Command-text mutation is not trusted when the workspace is unknown.
    assert not _runtime_messages_support_file_claim("outside.py", (touch,), task_cwd=None)
    # Absolute structured paths are also out-of-scope without a trusted cwd.
    edit = AgentMessage(
        type="tool",
        content="edit",
        tool_name="Edit",
        data={
            "tool_call_id": "edit_hello",
            "tool_input": {"file_path": "/private/tmp/ooo-run/hello.py"},
        },
    )
    completed = AgentMessage(
        type="tool_result",
        content="updated",
        data={
            "subtype": "tool_result",
            "tool_call_id": "edit_hello",
            "is_error": False,
        },
    )
    messages = (edit, completed)
    assert not _runtime_messages_support_file_claim("hello.py", messages, task_cwd=None)
    assert not _runtime_messages_support_file_claim("goodbye.py", messages, task_cwd=None)

    relative_edit = replace(
        edit,
        data={
            "tool_call_id": "edit_relative",
            "tool_input": {"file_path": "hello.py"},
        },
    )
    relative_completed = replace(
        completed,
        data={**completed.data, "tool_call_id": "edit_relative"},
    )
    assert _runtime_messages_support_file_claim(
        "hello.py",
        (relative_edit, relative_completed),
        task_cwd=None,
    )


@pytest.mark.parametrize("is_error", [True, None])
def test_files_touched_rejects_failed_or_missing_edit_completion(
    tmp_path,
    is_error: bool | None,
) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    edit = AgentMessage(
        type="tool",
        content="edit",
        tool_name="Edit",
        data={
            "tool_call_id": "edit_hello",
            "tool_input": {"file_path": str(workspace / "hello.py")},
        },
    )
    messages: tuple[AgentMessage, ...] = (edit,)
    if is_error is not None:
        messages += (
            AgentMessage(
                type="tool_result",
                content="edit failed",
                data={
                    "subtype": "tool_result",
                    "tool_call_id": "edit_hello",
                    "is_error": is_error,
                },
            ),
        )

    assert not _runtime_messages_support_file_claim(
        "hello.py",
        messages,
        task_cwd=str(workspace),
    )


def test_files_touched_rejects_duplicate_start_or_completion_correlation(tmp_path) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    start = AgentMessage(
        type="tool",
        content="edit",
        tool_name="Edit",
        data={
            "tool_call_id": "edit_hello",
            "tool_input": {"file_path": str(workspace / "hello.py")},
        },
    )
    success = AgentMessage(
        type="tool_result",
        content="updated",
        data={
            "subtype": "tool_result",
            "tool_call_id": "edit_hello",
            "is_error": False,
        },
    )
    failure = replace(success, content="failed", data={**success.data, "is_error": True})

    assert not _runtime_messages_support_file_claim(
        "hello.py",
        (start, replace(start), success),
        task_cwd=str(workspace),
    )
    assert not _runtime_messages_support_file_claim(
        "hello.py",
        (start, success, failure),
        task_cwd=str(workspace),
    )
    self_completed = replace(
        start,
        data={
            **start.data,
            "subtype": "success",
            "runtime_event_type": "tool.completed",
        },
    )
    assert not _runtime_messages_support_file_claim(
        "hello.py",
        (self_completed, replace(self_completed)),
        task_cwd=str(workspace),
    )


def test_files_touched_rejects_malformed_is_error_even_with_completed_status(tmp_path) -> None:
    workspace = tmp_path / "work"
    workspace.mkdir()
    start = AgentMessage(
        type="tool",
        content="edit",
        tool_name="Edit",
        data={
            "tool_call_id": "edit_hello",
            "tool_input": {"file_path": str(workspace / "hello.py")},
        },
    )
    malformed = AgentMessage(
        type="tool_result",
        content="unknown",
        data={
            "subtype": "tool_result",
            "tool_call_id": "edit_hello",
            "is_error": "true",
            "runtime_event_type": "tool.completed",
        },
    )

    assert not _runtime_messages_support_file_claim(
        "hello.py",
        (start, malformed),
        task_cwd=str(workspace),
    )


def test_correlated_tool_result_name_requires_one_exact_call_id_match() -> None:
    start = AgentMessage(
        type="tool",
        content="edit",
        tool_name="Edit",
        data={"tool_call_id": "edit_1"},
    )
    result = AgentMessage(
        type="tool_result",
        content="updated",
        data={"subtype": "tool_result", "tool_call_id": "edit_1", "is_error": False},
    )

    assert _correlated_tool_result_name([start, result], result) == "Edit"
    assert (
        _correlated_tool_result_name(
            [replace(start, data={"tool_call_id": "other"}), result],
            result,
        )
        is None
    )
    assert (
        _correlated_tool_result_name(
            [start, replace(start, tool_name="Write"), result],
            result,
        )
        is None
    )


def test_effective_schema_delegates_contract_command_evidence() -> None:
    """An active contract gate replaces transcript command and test evidence."""
    profile = load_profile("code")

    schema = _effective_evidence_schema_for_ac(
        profile,
        "Implement the module.",
        has_success_contract=True,
        verify_gate_active=True,
    )

    assert schema.required == ("files_touched",)

    artifact_schema = _effective_evidence_schema_for_ac(
        profile,
        "Implement the module.",
        has_success_contract=True,
        has_expected_artifacts=True,
        verify_gate_active=True,
    )

    assert artifact_schema.required == ()


def test_legacy_ac_keeps_transcript_backed_evidence() -> None:
    """Legacy ACs retain every transcript-backed required evidence field."""
    profile = load_profile("code")

    schema = _effective_evidence_schema_for_ac(
        profile,
        "Implement the module.",
        has_success_contract=False,
        has_expected_artifacts=True,
    )

    assert schema.required == ("files_touched", "commands_run", "tests_passed")


def test_contract_ac_retains_transcript_backed_evidence_when_verify_gate_inactive() -> None:
    """A disabled contract gate cannot replace transcript-backed evidence."""
    profile = load_profile("code")

    schema = _effective_evidence_schema_for_ac(
        profile,
        "Implement the module.",
        has_success_contract=True,
        has_expected_artifacts=True,
        verify_gate_active=False,
    )

    assert schema.required == ("files_touched", "commands_run", "tests_passed")


def test_contract_ac_with_artifacts_delegates_all_evidence_when_verify_gate_active() -> None:
    """An active contract gate replaces all evidence when it checks artifacts too."""
    profile = load_profile("code")

    schema = _effective_evidence_schema_for_ac(
        profile,
        "Implement the module.",
        has_success_contract=True,
        has_expected_artifacts=True,
        verify_gate_active=True,
    )

    assert schema.required == ()


def test_contract_ac_verifier_delegates_command_evidence() -> None:
    """Contract ACs do not transcript-gate commands_run or tests_passed.

    Only files_touched is checked without expected artifacts; command execution and
    test success are delegated to the orchestrator verify gate.
    """
    executor = _file_scope_executor("/private/tmp/ooo-repro-blos")
    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=_greeting_repro_messages("/private/tmp/ooo-repro-blos/hello.py"),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["hello.py"],
                "commands_run": [_GREETING_INNER_COMMAND],
            }
        ),
        ac_content=_GREETING_AC,
        has_success_contract=True,
        verify_gate_active=True,
    )
    assert verdict.passed is True, verdict.reasons


def test_contract_ac_with_expected_artifacts_verifier_delegates_all_evidence() -> None:
    """Contract AC artifacts and command execution are verified by the gate."""
    executor = _file_scope_executor("/private/tmp/ooo-repro-blos")
    verdict = executor._verify_atomic_evidence_against_runtime_messages(
        messages=_greeting_repro_messages("/private/tmp/ooo-repro-blos/hello.py"),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["not-backed-by-transcript.py"],
                "commands_run": [_GREETING_INNER_COMMAND],
            }
        ),
        ac_content=_GREETING_AC,
        has_success_contract=True,
        has_expected_artifacts=True,
        verify_gate_active=True,
    )

    assert verdict.passed is True, verdict.reasons


def test_legacy_ac_verifier_keeps_strict_formal_runner_tests_passed() -> None:
    """Legacy AC (no verify_command): tests_passed keeps strict formal-runner semantics."""
    executor = _file_scope_executor("/private/tmp/ooo-repro-blos")

    # Inline python3 -c is not a formal test runner -> tests_passed unsupported.
    rejected = executor._verify_atomic_evidence_against_runtime_messages(
        messages=_greeting_repro_messages("/private/tmp/ooo-repro-blos/hello.py"),
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["hello.py"],
                "commands_run": [_GREETING_INNER_COMMAND],
                "tests_passed": [_GREETING_INNER_COMMAND],
            }
        ),
        ac_content=_GREETING_AC,
        has_success_contract=False,
    )
    assert rejected.passed is False
    assert "tests_passed:" in rejected.reasons[0]

    # A real pytest run with success output backs tests_passed for a legacy AC.
    messages = (
        AgentMessage(
            type="tool",
            content="edit",
            tool_name="Edit",
            data={
                "subtype": "success",
                "runtime_event_type": "tool.completed",
                "tool_input": {"file_path": "/private/tmp/ooo-repro-blos/src/mod.py"},
            },
        ),
        AgentMessage(
            type="tool_result",
            content="updated",
            tool_name="Edit",
            data={"subtype": "tool_result", "is_error": False},
        ),
        AgentMessage(
            type="tool",
            content="pytest",
            tool_name="Bash",
            data={
                "tool_input": {"command": "pytest tests/test_mod.py"},
                "exit_code": 0,
                "output": "1 passed in 0.01s",
            },
        ),
        AgentMessage(type="result", content="done", data={}),
    )
    accepted = executor._verify_atomic_evidence_against_runtime_messages(
        messages=messages,
        typed_evidence=EvidenceRecord(
            data={
                "files_touched": ["src/mod.py"],
                "commands_run": ["pytest tests/test_mod.py"],
                "tests_passed": ["pytest tests/test_mod.py"],
            }
        ),
        ac_content="Implement src/mod.py and run pytest.",
        has_success_contract=False,
    )
    assert accepted.passed is True, accepted.reasons


@pytest.mark.asyncio
async def test_verify_gate_flips_contract_ac_to_failed_when_declared_command_fails(
    tmp_path,
) -> None:
    """Delegation is ENFORCED: a broken contract AC fails via the orchestrator gate.

    A successful AC result whose declared verify_command exits non-zero is flipped
    to FAILED by ``_apply_verify_gate`` — the orchestrator runs the real command,
    so a fabricated tests_passed claim cannot pass a broken implementation.
    """
    executor = _file_scope_executor(str(tmp_path))
    passing_result = ACExecutionResult(
        ac_index=0,
        ac_content="greet works",
        success=True,
        outcome=ACExecutionOutcome.SUCCEEDED,
    )

    failing_seed = Seed(
        goal="greeting",
        constraints=(),
        acceptance_criteria=(
            AcceptanceCriterionSpec(
                description="greet works",
                verify_command='python3 -c "import sys; sys.exit(1)"',
            ),
        ),
        ontology_schema=OntologySchema(name="Greeting", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.1),
    )
    gated = await executor._apply_verify_gate(
        seed=failing_seed,
        ac_index=0,
        result=passing_result,
        session_id="s",
        execution_id="e",
    )
    assert gated.success is False
    assert gated.outcome == ACExecutionOutcome.FAILED
    assert "Verify gate failed" in (gated.error or "")

    passing_seed = Seed(
        goal="greeting",
        constraints=(),
        acceptance_criteria=(
            AcceptanceCriterionSpec(
                description="greet works",
                verify_command="python3 -c \"print('OK')\"",
                output_assertion="OK",
            ),
        ),
        ontology_schema=OntologySchema(name="Greeting", description="d"),
        metadata=SeedMetadata(ambiguity_score=0.1),
    )
    kept = await executor._apply_verify_gate(
        seed=passing_seed,
        ac_index=0,
        result=passing_result,
        session_id="s",
        execution_id="e",
    )
    assert kept.success is True


@pytest.mark.parametrize(
    ("runtime_command", "claimed_command"),
    (
        ("./gradlew test -x test", "./gradlew test -x test"),
        ("./gradlew check -x test", "./gradlew check -x test"),
        ("./gradlew test --exclude-task test", "./gradlew test --exclude-task test"),
        ("./gradlew check --exclude-task :test", "./gradlew check --exclude-task :test"),
        ("mvn -DskipTests verify", "mvn -DskipTests verify"),
        ("mvn -D skipTests test", "mvn -D skipTests test"),
        ("mvn -Dmaven.test.skip=true test", "mvn -Dmaven.test.skip=true test"),
        ("mvn --define skipTests test", "mvn --define skipTests test"),
        ("mvn --define=skipTests=true test", "mvn --define=skipTests=true test"),
    ),
)
def test_gradle_maven_tests_passed_rejects_skip_test_invocations(
    runtime_command: str,
    claimed_command: str,
) -> None:
    """Build success cannot prove tests_passed when the command skipped tests."""
    command_message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={"tool_input": {"command": runtime_command}},
    )
    result_message = AgentMessage(
        type="tool_result",
        content="BUILD SUCCESSFUL in 8s",
        tool_name=None,
        data={"subtype": "tool_result", "output": "BUILD SUCCESSFUL in 8s"},
    )

    assert not _runtime_messages_support_test_claim(
        value=claimed_command,
        backed_commands=(claimed_command,),
        messages=(command_message, result_message),
        task_cwd=None,
    )


@pytest.mark.parametrize(
    "command",
    (
        "mvn -DskipTests=false test",
        "mvn -Dmaven.test.skip=false test",
    ),
)
def test_maven_tests_passed_supports_explicit_false_skip_properties(command: str) -> None:
    """Explicit false Maven skip properties still run tests and can prove success."""
    command_message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={"tool_input": {"command": command}},
    )
    result_message = AgentMessage(
        type="tool_result",
        content="[INFO] BUILD SUCCESS",
        tool_name=None,
        data={"subtype": "tool_result", "output": "[INFO] BUILD SUCCESS"},
    )

    assert _runtime_messages_support_test_claim(
        value=command,
        backed_commands=(command,),
        messages=(command_message, result_message),
        task_cwd=None,
    )


@pytest.mark.parametrize(
    "output",
    (
        "> Task :test NO-SOURCE\nBUILD SUCCESSFUL in 1s",
        "> Task :test SKIPPED\nBUILD SUCCESSFUL in 1s",
        "0 tests completed\nBUILD SUCCESSFUL",
    ),
)
def test_gradle_tests_passed_rejects_successful_build_with_no_tests(output: str) -> None:
    """Gradle build success without executed tests cannot prove tests_passed."""
    command_message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={"tool_input": {"command": "./gradlew test"}},
    )
    result_message = AgentMessage(
        type="tool_result",
        content=output,
        tool_name=None,
        data={"subtype": "tool_result", "output": output},
    )

    assert not _runtime_messages_support_test_claim(
        value="./gradlew test",
        backed_commands=("./gradlew test",),
        messages=(command_message, result_message),
        task_cwd=None,
    )


def test_maven_tests_passed_supports_surefire_zero_failure_summary() -> None:
    """Standard Surefire zero-failure fields plus build success prove Maven tests."""
    output = "[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0\n[INFO] BUILD SUCCESS"
    command_message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={"tool_input": {"command": "mvn test"}},
    )
    result_message = AgentMessage(
        type="tool_result",
        content=output,
        tool_name=None,
        data={"subtype": "tool_result", "output": output},
    )

    assert _runtime_messages_support_test_claim(
        value="mvn test",
        backed_commands=("mvn test",),
        messages=(command_message, result_message),
        task_cwd=None,
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


def test_test_invocation_rejects_pipefail_text_without_shell_option() -> None:
    """The pipefail guard must prove a shell option, not arbitrary command text."""
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={
            "tool_input": {
                "command": (
                    "/bin/bash -lc 'cd /workspace && "
                    "pytest tests/test_pipefail.py 2>&1 | cat # pipefail mentioned'"
                )
            },
            "exit_code": 0,
        },
    )

    assert not _runtime_messages_support_command_claim("pytest tests/test_pipefail.py", (message,))


def test_test_invocation_rejects_pipefail_set_after_output_pipe() -> None:
    """Pipefail must be enabled before the pipeline it is meant to protect."""
    message = AgentMessage(
        type="tool",
        content="Bash command started",
        tool_name="Bash",
        data={
            "tool_input": {
                "command": (
                    "/bin/bash -lc 'cd /workspace && "
                    "pytest tests/test_foo.py 2>&1 | cat && set -o pipefail'"
                )
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
            "min_unit": (
                "a cohesive change verified by one test-command run — typically a "
                "function or module plus its tests; never split below a single function"
            ),
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


class TestInfraFatalExemption:
    """Fix 4 (BLOCKING, PR #1648 review): a genuinely infra-fatal exception
    (adapter crash, auth failure, network partition) raised by the runtime
    mid-dispatch must be marked so recovery logic can never feed it back
    into the ordinary retry loop or the lateral-escalation ladder — even
    though ``_execute_atomic_ac`` catches it and returns an ordinary-looking
    ``ACExecutionResult`` (kept only so existing logging/event-emission code
    keeps working).
    """

    @pytest.mark.asyncio
    async def test_adapter_exception_marks_result_infra_fatal(self) -> None:
        """Drive the REAL production catch boundary: the adapter's
        ``execute_task`` async generator raises before yielding anything,
        exactly like a real auth failure (401) or adapter crash would. This
        must propagate through ``LeafDispatcher.stream`` (no catch there)
        into ``_execute_atomic_ac``'s own ``except Exception`` handler — not
        a shortcut that hands a raw ``BaseException`` straight to a helper
        function."""

        class _CrashingRuntime:
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
                if False:  # pragma: no cover - keeps this an async generator
                    yield
                raise PermissionError("401 Unauthorized: invalid API key")

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_CrashingRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_infra",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_infra",
        )

        assert result.success is False
        assert result.infra_fatal is True
        assert result.error is not None
        assert "401" in result.error
        assert executor._is_retryable_failure(result) is False

        failed_event = next(
            event for event in appended_events if event.type == "execution.session.failed"
        )
        assert "401" in failed_event.data["error"]

    @pytest.mark.asyncio
    async def test_cli_not_found_result_message_marks_infra_fatal(self) -> None:
        """Fix 4 (round 3, BLOCKING): reproduction of the actual reported gap.

        ``claude_worker_runtime.py`` (via ``worker_runtime.LeaderDrivenWorkerRuntime``)
        never raises for a missing CLI binary -- it catches ``FileNotFoundError``
        internally and yields an ordinary final ``AgentMessage`` with
        ``subtype="error"`` and content ``"claude CLI not found: ..."``. Before
        this fix, that structured result always produced ``infra_fatal=False``
        (only a RAISED exception set it), so a missing CLI entered the
        infinite retry/parking loop instead of surfacing immediately.

        Fix 4 redo: the classifier now only scans the structured
        ``data["error"]`` field (never free-text ``content``), so this mock
        mirrors what ``worker_runtime.py``'s real translation layer produces
        for this case -- ``WorkerTurn.error`` is mirrored into BOTH
        ``content`` and ``data["error"]`` (see ``worker_runtime.py``'s
        ``**({"error": turn.error} if turn.error else {})``).
        """

        class _CliMissingRuntime:
            _runtime_handle_backend = "claude"
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
                error_text = "claude CLI not found: [Errno 2] No such file or directory: 'claude'"
                yield AgentMessage(
                    type="result",
                    content=error_text,
                    data={"subtype": "error", "error": error_text},
                )

        event_store, _appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_CliMissingRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_infra_cli",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_infra_cli",
        )

        assert result.success is False
        assert result.infra_fatal is True
        assert executor._is_retryable_failure(result) is False

    @pytest.mark.asyncio
    async def test_sdk_not_installed_result_message_marks_infra_fatal(self) -> None:
        """Round-5 Finding #5 reproduction: ``adapter.py``'s SDK-missing path.

        When the ``claude_agent_sdk`` Python package is not importable,
        ``adapter.py`` yields a final error result. Before this fix its
        ``data`` carried ONLY ``{"subtype": "error"}`` — no ``error_type``,
        no ``error`` — and the classifier (which deliberately never scans
        free-text ``content``) returned ``False``, so a condition retrying
        can never cure (the SDK cannot install itself) entered the ordinary
        retry/escalation ladder forever. The adapter now tags the result
        with the specific ``SDKNotInstalledError`` type; this mock mirrors
        the adapter's exact post-fix message shape.
        """

        class _SdkMissingRuntime:
            _runtime_handle_backend = "claude"
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
                error_text = "Claude Agent SDK is not installed. Run: pip install claude-agent-sdk"
                yield AgentMessage(
                    type="result",
                    content=error_text,
                    data={
                        "subtype": "error",
                        "error_type": "SDKNotInstalledError",
                        "error": error_text,
                    },
                )

        event_store, _appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_SdkMissingRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_infra_sdk",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_infra_sdk",
        )

        assert result.success is False
        assert result.infra_fatal is True
        assert executor._is_retryable_failure(result) is False

    @pytest.mark.asyncio
    async def test_runtime_handle_error_result_message_marks_infra_fatal(self) -> None:
        """Round-7 Finding #1 reproduction: ``adapter.py``'s
        ``_execution_dispatch_error_message`` already tags a stale /
        backend-incompatible runtime-handle dispatch failure with the
        purpose-built ``RuntimeHandleError`` type, but the classifier's
        allowlist was never updated, so the correctly structured signal
        classified ``infra_fatal=False`` and the unusable handle entered the
        ordinary retry / parking ladder forever. This mock mirrors the
        adapter's exact message shape (structured ``error_type`` only, no
        ``error`` detail field)."""

        class _StaleHandleRuntime:
            _runtime_handle_backend = "claude"
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
                yield AgentMessage(
                    type="result",
                    content=(
                        "Task execution failed: runtime handle is incompatible with this runtime."
                    ),
                    data={
                        "subtype": "error",
                        "error_type": "RuntimeHandleError",
                    },
                )

        event_store, _appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_StaleHandleRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_infra_handle",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_infra_handle",
        )

        assert result.success is False
        assert result.infra_fatal is True
        assert executor._is_retryable_failure(result) is False

    @pytest.mark.asyncio
    async def test_sdk_cli_not_found_exception_result_marks_infra_fatal(self) -> None:
        """Round-7 Finding #1 ("exhausted SDK exceptions" half): when the
        Claude Agent SDK raises ``CLINotFoundError`` (the ``claude`` CLI
        binary cannot be located), ``adapter.py``'s terminal error path emits
        a final result tagged ``error_type=type(e).__name__`` =
        ``"CLINotFoundError"`` — the SDK's own purpose-built exception class,
        never raised by task-level code. Retrying can never install the CLI,
        so this must classify infra-fatal instead of entering the
        retry/escalation ladder. This mock mirrors the adapter's exact
        raised-exception message shape (``error_type`` only, no structured
        ``error`` detail field)."""

        class _CliMissingRuntime:
            _runtime_handle_backend = "claude"
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
                yield AgentMessage(
                    type="result",
                    content=(
                        "Task execution failed: Claude Code not found at: claude. "
                        "Install with: npm install -g @anthropic-ai/claude-code"
                    ),
                    data={
                        "subtype": "error",
                        "error_type": "CLINotFoundError",
                    },
                )

        event_store, _appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_CliMissingRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_infra_cli_missing",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_infra_cli_missing",
        )

        assert result.success is False
        assert result.infra_fatal is True
        assert executor._is_retryable_failure(result) is False

    @pytest.mark.asyncio
    async def test_ordinary_runtime_error_result_is_not_infra_fatal(self) -> None:
        """Round-7 Finding #1 negative control: a generic builtin
        ``RuntimeError`` raised for a NON-infra reason (ordinary task/business
        failure) reaches the adapter's terminal error path as
        ``error_type="RuntimeError"``. Blanket-allowlisting the generic
        builtin name (as the review suggested) would have swept exactly this
        case into ``infra_fatal=True`` and skipped real retry/escalation
        opportunities — the forbidden false-negative — mirroring the earlier
        round's caution that kept generic ``"PiError"`` out of the
        allowlist. It must remain retryable.

        Extended for Round-8 Finding #1: the adapter's terminal error path
        now also mirrors the exception's message into the structured
        ``error`` field — this control mirrors that exact shape and proves
        an ordinary message with no vetted infra-fatal phrase STILL
        classifies retryable (the fix did not blanket-mark this path)."""

        class _OrdinaryRuntimeErrorRuntime:
            _runtime_handle_backend = "claude"
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
                yield AgentMessage(
                    type="result",
                    content="Task execution failed: business validation failed",
                    data={
                        "subtype": "error",
                        "error_type": "RuntimeError",
                        "error": "business validation failed",
                    },
                )

        event_store, _appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_OrdinaryRuntimeErrorRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_infra_runtime_error",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_infra_runtime_error",
        )

        assert result.success is False
        assert result.infra_fatal is False
        assert executor._is_retryable_failure(result) is True

    @pytest.mark.asyncio
    async def test_sdk_exception_with_fatal_message_and_generic_type_marks_infra_fatal(
        self,
    ) -> None:
        """Round-8 Finding #1 regression: an SDK-raised exception reaching
        ``adapter.py``'s non-transient terminal fallback with a TYPE outside
        the narrow ``_INFRA_FATAL_ERROR_TYPES`` allowlist but a MESSAGE
        carrying a vetted infra-fatal phrase (a permanent auth failure that
        happened to raise a generic exception type) used to classify
        ``infra_fatal=False`` unconditionally: the fallback populated only
        ``error_type``, never the structured ``error`` field the
        classifier's content scan reads. With the adapter now mirroring the
        exception message into ``data["error"]`` (the same convention
        kiro_adapter/pi_runtime/worker_runtime follow), the vetted narrow
        content patterns can evaluate it and the condition fails immediately
        instead of retrying/escalating forever."""

        class _AuthFailureRuntime:
            _runtime_handle_backend = "claude"
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
                # The adapter's EXACT post-fix terminal shape for
                # ``raise RuntimeError("authentication failed: ...")``:
                # generic error_type (not allowlisted) + the exception's own
                # message mirrored into the structured ``error`` field.
                yield AgentMessage(
                    type="result",
                    content=(
                        "Task execution failed: authentication failed: "
                        "invalid credentials for configured account"
                    ),
                    data={
                        "subtype": "error",
                        "error_type": "RuntimeError",
                        "error": (
                            "authentication failed: invalid credentials for configured account"
                        ),
                    },
                )

        event_store, _appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_AuthFailureRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_infra_auth_failure",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_infra_auth_failure",
        )

        assert result.success is False
        assert result.infra_fatal is True
        assert executor._is_retryable_failure(result) is False

    @pytest.mark.asyncio
    async def test_kiro_cli_not_found_result_message_marks_infra_fatal(self) -> None:
        """Round-5 Finding #5 audit: ``kiro_adapter.py``'s CLI-missing path.

        ``kiro_adapter.py`` catches ``FileNotFoundError`` internally and used
        to yield a final error result whose ``data`` carried only
        ``{"subtype": "error"}`` — invisible to the structured-field-only
        classifier. It now tags ``error_type``/``error``; this mock mirrors
        the adapter's exact post-fix message shape.
        """

        class _KiroCliMissingRuntime:
            _runtime_handle_backend = "kiro"
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
                error_text = "Kiro CLI not found at: /usr/local/bin/kiro"
                yield AgentMessage(
                    type="result",
                    content=error_text,
                    data={
                        "subtype": "error",
                        "error_type": "FileNotFoundError",
                        "error": error_text,
                    },
                )

        event_store, _appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_KiroCliMissingRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_infra_kiro",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_infra_kiro",
        )

        assert result.success is False
        assert result.infra_fatal is True
        assert executor._is_retryable_failure(result) is False

    @pytest.mark.asyncio
    async def test_pi_auth_failure_result_message_marks_infra_fatal(self) -> None:
        """The second reproduction named by the finding: ``pi_runtime.py``
        reports model/auth failures as an ordinary final error message
        (``error_type`` stays the adapter's generic tag, not a distinguishing
        exception class), so the classifier must also recognize well-known
        auth-failure phrasing in the message's structured error field.

        Fix 4 redo: the classifier now only scans the structured
        ``data["error"]`` field (never free-text ``content``), so this mock
        mirrors what ``pi_runtime.py``'s real translation layer produces for
        a ``stopReason: "error"`` turn -- the extracted ``errorMessage`` is
        mirrored into BOTH ``content`` and ``data["error"]``.
        """

        class _AuthFailingRuntime:
            _runtime_handle_backend = "pi"
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
                error_text = "Request failed: 401 Unauthorized - invalid api key provided"
                yield AgentMessage(
                    type="result",
                    content=error_text,
                    data={"subtype": "error", "error_type": "PiError", "error": error_text},
                )

        event_store, _appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_AuthFailingRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_infra_pi",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_infra_pi",
        )

        assert result.success is False
        assert result.infra_fatal is True
        assert executor._is_retryable_failure(result) is False

    @pytest.mark.asyncio
    async def test_pi_real_openai_401_error_shape_marks_infra_fatal(self) -> None:
        """Round-4 Finding #1 reproduction: Pi's ACTUAL auth-failure payload.

        ``pi_runtime.py`` reports a genuine authentication failure with the
        generic ``error_type="PiError"`` and mirrors the extracted
        ``errorMessage`` into ``data["error"]``. The real string Pi's
        underlying provider library produces (verified against the installed
        ``@earendil-works/pi-ai`` openai-responses provider:
        ``\\`OpenAI API error (${statusCode}): ${error.message}\\```) is
        ``"OpenAI API error (401)"`` -- which matched NONE of the existing
        content patterns (``"401 unauthorized"``/``"unauthorized"`` are not
        substrings of it), so a genuine Pi 401 was classified
        ``infra_fatal=False`` and retried forever instead of failing
        immediately.
        """

        class _PiRealAuthFailureRuntime:
            _runtime_handle_backend = "pi"
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
                # Exactly what pi_runtime.py yields for a ``stopReason:
                # "error"`` turn whose ``errorMessage`` is the real
                # pi-ai-formatted 401 (see tests/unit/orchestrator/
                # test_pi_runtime.py's fixtures using the same string).
                error_text = "OpenAI API error (401)"
                yield AgentMessage(
                    type="result",
                    content=error_text,
                    data={
                        "subtype": "error",
                        "returncode": 0,
                        "error_type": "PiError",
                        "error": error_text,
                    },
                )

        event_store, _appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_PiRealAuthFailureRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_infra_pi_401",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_infra_pi_401",
        )

        assert result.success is False
        assert result.infra_fatal is True
        assert executor._is_retryable_failure(result) is False

    @pytest.mark.asyncio
    async def test_ordinary_pi_task_failure_is_not_infra_fatal(self) -> None:
        """Round-4 Finding #1 negative control: an ordinary Pi task failure
        (generic ``error_type="PiError"``, no auth phrasing in the structured
        error field) must still classify ``infra_fatal=False`` -- adding
        ``"PiError"`` to ``_INFRA_FATAL_ERROR_TYPES`` would have been too
        broad and would wrongly skip real retry/escalation opportunities for
        exactly this case.
        """

        class _PiOrdinaryFailureRuntime:
            _runtime_handle_backend = "pi"
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
                error_text = "Pi exited with code 1."
                yield AgentMessage(
                    type="result",
                    content=error_text,
                    data={
                        "subtype": "error",
                        "returncode": 1,
                        "error_type": "PiError",
                        "error": error_text,
                    },
                )

        event_store, _appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_PiOrdinaryFailureRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_infra_pi_ordinary",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_infra_pi_ordinary",
        )

        assert result.success is False
        assert result.infra_fatal is False
        assert executor._is_retryable_failure(result) is True

    @pytest.mark.asyncio
    async def test_infra_fatal_phrase_in_narrative_content_only_is_not_infra_fatal(self) -> None:
        """Fix 4 redo (round 3 follow-up review): regression test for the
        exact false-positive scenario the review found. The agent's own
        free-text final message (``content``) quotes a failing build's
        stderr containing an infra-fatal-sounding phrase ("no such file or
        directory"), but the structured ``data["error"]`` field is absent --
        this is an ORDINARY task failure, not an infra-fatal one. Before this
        redo, the classifier scanned ``content`` too and would have wrongly
        set ``infra_fatal=True`` here, short-circuiting
        ``_is_retryable_failure()`` to ``False`` and skipping every retry and
        lateral-escalation option -- exactly what this project's mandate
        forbids.
        """

        class _OrdinaryBuildFailureRuntime:
            _runtime_handle_backend = "claude"
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
                yield AgentMessage(
                    type="result",
                    content=(
                        "the build failed: npm ERR! path /app\n"
                        "npm ERR! no such file or directory, open '/app/package.json'"
                    ),
                    data={"subtype": "error"},
                )

        event_store, _appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_OrdinaryBuildFailureRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_infra_narrative",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_infra_narrative",
        )

        assert result.success is False
        assert result.infra_fatal is False
        assert executor._is_retryable_failure(result) is True

    @pytest.mark.asyncio
    async def test_ordinary_verify_failure_result_message_is_not_infra_fatal(self) -> None:
        """Negative control: an ordinary AC-level failure message (no
        infra-fatal error_type or phrasing) must NOT be misclassified --
        false positives here would wrongly skip real retry/escalation
        opportunities."""

        class _OrdinaryFailingRuntime:
            _runtime_handle_backend = "claude"
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
                yield AgentMessage(
                    type="result",
                    content="Tests failed: 2 assertions did not pass.",
                    data={"subtype": "error"},
                )

        event_store, _appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_OrdinaryFailingRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement AC 1",
            session_id="orch_infra_ordinary",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            execution_id="exec_infra_ordinary",
        )

        assert result.success is False
        assert result.infra_fatal is False
        assert executor._is_retryable_failure(result) is True

    def test_is_retryable_failure_excludes_infra_fatal_even_when_shape_matches_ordinary_failure(
        self,
    ) -> None:
        """Direct unit check: ``infra_fatal`` must gate ``_is_retryable_failure``
        even though every OTHER field (``success=False``, not blocked, a
        non-stall error string) looks identical to an ordinary, retryable
        verify-gate/quality failure — the two must not be distinguishable by
        shape alone, only by the explicit flag."""
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        ordinary_failure = ACExecutionResult(
            ac_index=0,
            ac_content="x",
            success=False,
            error="verify_command failed",
        )
        infra_fatal_failure = ACExecutionResult(
            ac_index=0,
            ac_content="x",
            success=False,
            error="verify_command failed",
            infra_fatal=True,
        )

        assert executor._is_retryable_failure(ordinary_failure) is True
        assert executor._is_retryable_failure(infra_fatal_failure) is False

    @pytest.mark.asyncio
    async def test_infra_fatal_result_is_exempt_from_lateral_escalation_ladder(self) -> None:
        """End-to-end through the actual gate the ladder uses: an infra-fatal
        result must make ``_maybe_run_lateral_escalation_ladder`` bail out
        immediately (``None``) even when the ladder is opted in, rather than
        engaging retries for a failure the runtime itself caused."""
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        executor._lateral_escalation_enabled = True
        seed = Seed(
            goal="Implement the widget",
            constraints=(),
            acceptance_criteria=(AcceptanceCriterionSpec(description="Implement the widget"),),
            ontology_schema=OntologySchema(name="Infra", description="Test schema"),
            metadata=SeedMetadata(ambiguity_score=0.05),
        )
        infra_fatal_failure = ACExecutionResult(
            ac_index=0,
            ac_content="Implement the widget",
            success=False,
            error="401 Unauthorized",
            infra_fatal=True,
        )

        escalated = await executor._maybe_run_lateral_escalation_ladder(
            seed=seed,
            ac_idx=0,
            result=infra_fatal_failure,
            ac_retry_attempts={0: 1},
            session_id="s1",
            execution_id="exec-1",
            tools=[],
            tool_catalog=None,
            system_prompt="",
            level_contexts=[],
            execution_counters=None,
        )

        assert escalated is None


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
        assert resume_handle.metadata["ac_capsule_version"] == AC_EXECUTION_CAPSULE_VERSION
        assert resume_handle.metadata["ac_capsule_fingerprint"].startswith("sha256:")
        assert resume_handle.metadata["ac_session_origin"] == "fresh"
        assert (
            resume_handle.metadata["session_state_path"]
            == "execution.workflows.orch_123.acceptance_criteria.ac_3.implementation_session"
        )
        started_event = next(
            event for event in appended_events if event.type == "execution.session.started"
        )
        capsule_event = next(
            event for event in appended_events if event.type == "execution.ac.capsule.compiled"
        )
        restored_manifest = ACExecutionCapsuleManifest.from_contract_data(
            capsule_event.data["capsule_manifest"]
        )
        assert capsule_event.data["capsule_fingerprint"] == restored_manifest.fingerprint
        assert capsule_event.data["session_origin"] == "fresh"
        assert isinstance(runtime_call["prompt"], str)
        assert "Ouroboros AC Runtime" in runtime_call["prompt"]
        assert "Implement AC 3" in runtime_call["prompt"]
        assert restored_manifest.ac_content_digest.startswith("sha256:")
        assert restored_manifest.workspace_digest.startswith("sha256:")
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
    async def test_provider_handle_keeps_capsule_metadata_when_runtime_returns_none(self) -> None:
        """Bare provider handles inherit the bound same-attempt authority metadata."""

        class _BareHandleRuntime:
            runtime_backend = "codex_cli"
            working_directory = "/tmp/project"
            permission_mode = "acceptEdits"

            async def execute_task(self, **_kwargs: object):
                yield AgentMessage(
                    type="system",
                    content="session ready",
                    data={"session_id": "bare-provider-session"},
                    resume_handle=RuntimeHandle(
                        backend="codex_cli",
                        kind="agent_runtime",
                        native_session_id="bare-provider-session",
                        cwd="/tmp/project",
                        approval_mode="acceptEdits",
                        metadata={},
                    ),
                )
                yield AgentMessage(
                    type="result",
                    content="[TASK_COMPLETE]",
                    data={"subtype": "success"},
                )

        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_BareHandleRuntime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        node_identity = ExecutionNodeIdentity.root(
            execution_context_id="session-bare-provider",
            ac_index=0,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Preserve capsule authority on provider handles",
            session_id="session-bare-provider",
            tools=["Read"],
            system_prompt="system",
            seed_goal="Ship",
            depth=0,
            start_time=datetime.now(UTC),
            node_identity=node_identity,
        )

        assert result.runtime_handle is not None
        fingerprint = result.runtime_handle.metadata["ac_capsule_fingerprint"]
        expected_identity = build_ac_runtime_identity(
            0,
            execution_context_id="session-bare-provider",
            node_identity=node_identity,
            retry_attempt=0,
        )
        assert (
            result.runtime_handle.metadata["session_attempt_id"]
            == expected_identity.session_attempt_id
        )
        started = next(
            event for event in appended_events if event.type == "execution.session.started"
        )
        assert started.data["runtime"]["metadata"]["ac_capsule_fingerprint"] == fingerprint

        replay_store, replay_events = _make_replaying_event_store()
        replay_events.extend(
            event
            for event in appended_events
            if event.type
            in {
                "execution.ac.capsule.compiled",
                "execution.ac.attempt.dispatched",
                "execution.session.started",
            }
        )
        restarted = ParallelACExecutor(
            adapter=_BareHandleRuntime(),
            event_store=replay_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        restored = await restarted._load_persisted_ac_runtime_handle(
            0,
            execution_context_id="session-bare-provider",
            retry_attempt=0,
            node_identity=node_identity,
            expected_capsule_fingerprint=fingerprint,
            expected_capsule_workspace=os.path.realpath("/tmp/project"),
        )
        assert restored is not None
        assert restored.native_session_id == "bare-provider-session"
        assert restored.metadata["ac_capsule_fingerprint"] == fingerprint

    @pytest.mark.asyncio
    async def test_atomic_ac_does_not_dispatch_when_capsule_persistence_fails(self) -> None:
        """An authority-bearing capsule must be durable before provider effects."""

        class _Runtime:
            runtime_backend = "codex_cli"
            working_directory = "/tmp/project"
            permission_mode = "acceptEdits"

            def __init__(self) -> None:
                self.calls = 0

            async def execute_task(self, **_kwargs: object):
                self.calls += 1
                yield AgentMessage(type="result", content="[TASK_COMPLETE]")

        runtime = _Runtime()
        event_store = AsyncMock()
        event_store.replay.return_value = []
        event_store.append.side_effect = OSError("event store unavailable")
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        with pytest.raises(OSError, match="event store unavailable"):
            await executor._execute_atomic_ac(
                ac_index=0,
                ac_content="Implement the AC",
                session_id="session-capsule-failure",
                tools=["Read", "Edit"],
                system_prompt="system",
                seed_goal="Ship",
                depth=0,
                start_time=datetime.now(UTC),
            )

        assert runtime.calls == 0

    @pytest.mark.asyncio
    async def test_atomic_ac_persists_dispatch_transition_before_provider_call(self) -> None:
        """The uncertain provider boundary must be durable before execute_task starts."""

        class _Runtime:
            runtime_backend = "codex_cli"
            working_directory = "/tmp/project"
            permission_mode = "acceptEdits"

            def __init__(self, appended_events: list[BaseEvent]) -> None:
                self.appended_events = appended_events
                self.calls = 0

            async def execute_task(self, **_kwargs: object):
                self.calls += 1
                assert self.appended_events[-1].type == "execution.ac.attempt.dispatched"
                yield AgentMessage(type="result", content="[TASK_COMPLETE]")

        event_store, appended_events = _make_replaying_event_store()
        runtime = _Runtime(appended_events)
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Persist provider dispatch uncertainty",
            session_id="session-dispatch-transition",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert runtime.calls == 1
        assert result.success is True
        dispatch_event = next(
            event for event in appended_events if event.type == "execution.ac.attempt.dispatched"
        )
        capsule_event = next(
            event for event in appended_events if event.type == "execution.ac.capsule.compiled"
        )
        assert (
            dispatch_event.data["capsule_fingerprint"] == capsule_event.data["capsule_fingerprint"]
        )
        dispatch_id = dispatch_event.data["ac_dispatch_id"]
        assert isinstance(dispatch_id, str) and len(dispatch_id) == 32
        dispatch_runtime = dispatch_event.data["runtime"]
        assert "cwd" not in dispatch_runtime
        assert "transcript_path" not in dispatch_runtime
        assert "tool_catalog" not in dispatch_runtime["metadata"]
        assert "capability_graph" not in dispatch_runtime["metadata"]
        assert "control_plane" not in dispatch_runtime["metadata"]
        completed_event = next(
            event for event in appended_events if event.type == "execution.session.completed"
        )
        assert completed_event.data["ac_dispatch_id"] == dispatch_id

    @pytest.mark.asyncio
    async def test_atomic_ac_does_not_call_provider_when_dispatch_transition_fails(self) -> None:
        """Failure to persist DISPATCHED must leave the provider untouched."""

        class _Runtime:
            runtime_backend = "codex_cli"
            working_directory = "/tmp/project"
            permission_mode = "acceptEdits"

            def __init__(self) -> None:
                self.calls = 0

            async def execute_task(self, **_kwargs: object):
                self.calls += 1
                yield AgentMessage(type="result", content="[TASK_COMPLETE]")

        runtime = _Runtime()
        event_store, appended_events = _make_replaying_event_store()

        async def _append(event: BaseEvent) -> None:
            if event.type == "execution.ac.attempt.dispatched":
                raise OSError("dispatch transition unavailable")
            appended_events.append(event)

        event_store.append.side_effect = _append
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Block unsafe provider dispatch",
            session_id="session-dispatch-persist-failure",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert runtime.calls == 0
        assert result.success is False
        assert result.infra_fatal is True
        assert result.error == "dispatch transition unavailable"
        assert any(event.type == "execution.ac.capsule.compiled" for event in appended_events)
        assert not any(event.type == "execution.session.failed" for event in appended_events)

        event_store.append.side_effect = appended_events.append
        retried = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Block unsafe provider dispatch",
            session_id="session-dispatch-persist-failure",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert retried.success is True
        assert runtime.calls == 1

    @pytest.mark.asyncio
    async def test_atomic_ac_refuses_fresh_dispatch_after_pre_message_crash(self) -> None:
        """A durable dispatch with no handle/terminal successor is effect-ambiguous."""

        class _Runtime:
            runtime_backend = "codex_cli"
            working_directory = "/tmp/project"
            permission_mode = "acceptEdits"

            def __init__(self) -> None:
                self.calls = 0

            async def execute_task(self, **_kwargs: object):
                self.calls += 1
                yield AgentMessage(type="result", content="[TASK_COMPLETE]")

        runtime = _Runtime()
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Apply one external effect before the first message",
            session_id="session-pre-message-crash",
            seed_goal="Ship",
        )
        appended_events.extend(
            [
                _compiled_capsule_event(runtime_identity, persisted_capsule),
                _dispatched_capsule_event(runtime_identity, persisted_capsule),
            ]
        )

        with pytest.raises(
            AmbiguousACExecutionError,
            match="crossed the provider dispatch boundary",
        ):
            await executor._execute_atomic_ac(
                ac_index=0,
                ac_content="Apply one external effect before the first message",
                session_id="session-pre-message-crash",
                tools=["Read", "Edit"],
                system_prompt="system",
                seed_goal="Ship",
                depth=0,
                start_time=datetime.now(UTC),
            )

        assert runtime.calls == 0

    @pytest.mark.asyncio
    async def test_capsule_loader_correlates_dispatch_across_timestamp_rollback(self) -> None:
        """Recovery uses dispatch identity, never timestamp/list successor order."""

        runtime = SimpleNamespace(
            runtime_backend="codex_cli",
            working_directory="/tmp/project",
            permission_mode="acceptEdits",
        )
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Recover by dispatch identity despite clock rollback",
            session_id="session-dispatch-order",
            seed_goal="Ship",
        )
        dispatch_id = "a" * 32
        persisted_handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            native_session_id="codex-clock-rollback",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                **runtime_identity.to_metadata(),
                "ac_capsule_version": persisted_capsule.version,
                "ac_capsule_fingerprint": persisted_capsule.fingerprint,
                "ac_dispatch_id": "e" * 32,
                "ac_session_origin": "fresh",
            },
        )
        lifecycle = _dispatch_lifecycle_event(
            runtime_identity,
            "execution.session.started",
            dispatch_id=dispatch_id,
            runtime_handle=persisted_handle,
            timestamp=datetime(2020, 1, 1, tzinfo=UTC),
        )
        event_store.replay.return_value = [
            lifecycle,
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            _dispatched_capsule_event(
                runtime_identity,
                persisted_capsule,
                dispatch_id=dispatch_id,
            ),
        ]

        restored = await executor._load_persisted_ac_runtime_handle(
            0,
            execution_context_id="session-dispatch-order",
            retry_attempt=0,
            expected_capsule_fingerprint=persisted_capsule.fingerprint,
            expected_capsule_workspace=persisted_capsule.workspace,
        )

        assert restored is not None
        assert restored.native_session_id == "codex-clock-rollback"

    @pytest.mark.asyncio
    async def test_capsule_loader_rebinds_dispatch_workspace_before_resume(self) -> None:
        """Minimal dispatch payloads regain cwd only from current capsule authority."""

        runtime = SimpleNamespace(
            runtime_backend="codex_cli",
            working_directory="/tmp/project",
            permission_mode="acceptEdits",
        )
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Resume from a minimal provider-boundary record",
            session_id="session-dispatch-minimal-resume",
            seed_goal="Ship",
        )
        persisted_handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            native_session_id="codex-minimal-resume",
            cwd="/must-not-be-persisted",
            approval_mode="acceptEdits",
            metadata={
                **runtime_identity.to_metadata(),
                "ac_capsule_version": persisted_capsule.version,
                "ac_capsule_fingerprint": persisted_capsule.fingerprint,
                "ac_session_origin": "restored_same_attempt",
            },
        )
        dispatch_event = _dispatched_capsule_event(
            runtime_identity,
            persisted_capsule,
            dispatch_id="9" * 32,
            session_origin="restored_same_attempt",
            runtime_handle=persisted_handle,
        )
        assert "cwd" not in dispatch_event.data["runtime"]
        event_store.replay.return_value = [
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            dispatch_event,
        ]

        restored = await executor._load_persisted_ac_runtime_handle(
            0,
            execution_context_id="session-dispatch-minimal-resume",
            retry_attempt=0,
            expected_capsule_fingerprint=persisted_capsule.fingerprint,
            expected_capsule_workspace=persisted_capsule.workspace,
        )

        assert restored is not None
        assert restored.native_session_id == "codex-minimal-resume"
        assert restored.cwd == persisted_capsule.workspace

    @pytest.mark.asyncio
    async def test_capsule_loader_rejects_nonminimal_dispatch_runtime_payload(self) -> None:
        """Unknown dispatch fields cannot re-enter recovery through persisted corruption."""

        runtime = SimpleNamespace(
            runtime_backend="codex_cli",
            working_directory="/tmp/project",
            permission_mode="acceptEdits",
        )
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Reject a nonminimal dispatch recovery payload",
            session_id="session-dispatch-nonminimal",
            seed_goal="Ship",
        )
        handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            native_session_id="codex-nonminimal",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                **runtime_identity.to_metadata(),
                "ac_capsule_version": persisted_capsule.version,
                "ac_capsule_fingerprint": persisted_capsule.fingerprint,
                "ac_session_origin": "restored_same_attempt",
            },
        )
        dispatch_event = _dispatched_capsule_event(
            runtime_identity,
            persisted_capsule,
            dispatch_id="4" * 32,
            session_origin="restored_same_attempt",
            runtime_handle=handle,
        )
        dispatch_event.data["runtime"]["cwd"] = "/smuggled/worktree"
        event_store.replay.return_value = [
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            dispatch_event,
        ]

        with pytest.raises(ValueError, match="dispatch recovery handle is malformed"):
            await executor._load_persisted_ac_runtime_handle(
                0,
                execution_context_id="session-dispatch-nonminimal",
                retry_attempt=0,
                expected_capsule_fingerprint=persisted_capsule.fingerprint,
                expected_capsule_workspace=persisted_capsule.workspace,
            )

    @pytest.mark.asyncio
    async def test_capsule_loader_keeps_failed_dispatch_ambiguous_without_handle(self) -> None:
        """Adapter failure does not prove the provider performed no external effect."""

        class _Runtime:
            runtime_backend = "codex_cli"
            working_directory = "/tmp/project"
            permission_mode = "acceptEdits"

        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=_Runtime(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Close dispatch uncertainty with a terminal successor",
            session_id="session-dispatch-terminal-successor",
            seed_goal="Ship",
        )
        dispatch_id = "b" * 32
        event_store.replay = AsyncMock(
            return_value=[
                _compiled_capsule_event(runtime_identity, persisted_capsule),
                _dispatched_capsule_event(
                    runtime_identity,
                    persisted_capsule,
                    dispatch_id=dispatch_id,
                ),
                _dispatch_lifecycle_event(
                    runtime_identity,
                    "execution.session.failed",
                    dispatch_id=dispatch_id,
                    runtime_handle=None,
                ),
            ]
        )

        with pytest.raises(AmbiguousACExecutionError, match="terminally failed dispatch head"):
            await executor._load_persisted_ac_runtime_handle(
                0,
                execution_context_id="session-dispatch-terminal-successor",
                retry_attempt=0,
                expected_capsule_fingerprint=persisted_capsule.fingerprint,
                expected_capsule_workspace=persisted_capsule.workspace,
            )

    @pytest.mark.asyncio
    async def test_capsule_loader_fails_closed_on_failed_head_even_with_handle(self) -> None:
        """R3 blocker #1: a terminally failed head is not resumed even with a handle.

        A durable ``execution.session.failed`` head ran a provider turn whose
        effects may precede the reported failure; resuming its session would send
        another provider turn and could repeat non-idempotent effects. Without an
        explicit ``execution.session.recovered`` linkage, recovery must fail closed
        rather than reconnect to the failed session.
        """
        runtime = SimpleNamespace(
            runtime_backend="codex_cli",
            working_directory="/tmp/project",
            permission_mode="acceptEdits",
        )
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Do not resume a terminally failed provider session",
            session_id="session-dispatch-failed-resume",
            seed_goal="Ship",
        )
        dispatch_id = "8" * 32
        handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            native_session_id="codex-failed-resume",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                **runtime_identity.to_metadata(),
                "ac_capsule_version": persisted_capsule.version,
                "ac_capsule_fingerprint": persisted_capsule.fingerprint,
                "ac_session_origin": "fresh",
            },
        )
        event_store.replay.return_value = [
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            _dispatched_capsule_event(
                runtime_identity,
                persisted_capsule,
                dispatch_id=dispatch_id,
            ),
            _dispatch_lifecycle_event(
                runtime_identity,
                "execution.session.failed",
                dispatch_id=dispatch_id,
                runtime_handle=handle,
            ),
        ]

        with pytest.raises(AmbiguousACExecutionError, match="terminally failed dispatch head"):
            await executor._load_persisted_ac_runtime_handle(
                0,
                execution_context_id="session-dispatch-failed-resume",
                retry_attempt=0,
                expected_capsule_fingerprint=persisted_capsule.fingerprint,
                expected_capsule_workspace=persisted_capsule.workspace,
            )

    @pytest.mark.asyncio
    async def test_capsule_loader_resumes_only_latest_boundary_in_multi_dispatch_attempt(
        self,
    ) -> None:
        """A later signal dispatch owns recovery even when an older event ranks higher."""
        runtime = SimpleNamespace(
            runtime_backend="codex_cli",
            working_directory="/tmp/project",
            permission_mode="acceptEdits",
        )
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Resume the exact latest provider boundary",
            session_id="session-multi-dispatch-latest",
            seed_goal="Ship",
        )
        first_dispatch_id = "1" * 32
        second_dispatch_id = "2" * 32

        def _handle(previous_response_id: str) -> RuntimeHandle:
            return RuntimeHandle(
                backend="codex_cli",
                kind="implementation_session",
                native_session_id="codex-shared-session",
                previous_response_id=previous_response_id,
                cwd="/tmp/project",
                approval_mode="acceptEdits",
                metadata={
                    **runtime_identity.to_metadata(),
                    "ac_capsule_version": persisted_capsule.version,
                    "ac_capsule_fingerprint": persisted_capsule.fingerprint,
                    "ac_session_origin": "restored_same_attempt",
                },
            )

        event_store.replay.return_value = [
            # Both boundaries are resumable (started) — this test isolates
            # latest-boundary head selection, not failed-head recovery, which is
            # covered by test_capsule_loader_fails_closed_on_failed_head_even_with_handle.
            _dispatch_lifecycle_event(
                runtime_identity,
                "execution.session.started",
                dispatch_id=second_dispatch_id,
                runtime_handle=_handle("response-after-signal"),
            ),
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            _dispatch_lifecycle_event(
                runtime_identity,
                "execution.session.started",
                dispatch_id=first_dispatch_id,
                runtime_handle=_handle("response-before-signal"),
            ),
            _dispatched_capsule_event(
                runtime_identity,
                persisted_capsule,
                dispatch_id=second_dispatch_id,
                previous_dispatch_id=first_dispatch_id,
                session_origin="restored_same_attempt",
            ),
            _dispatched_capsule_event(
                runtime_identity,
                persisted_capsule,
                dispatch_id=first_dispatch_id,
            ),
        ]

        restored = await executor._load_persisted_ac_runtime_handle(
            0,
            execution_context_id="session-multi-dispatch-latest",
            retry_attempt=0,
            expected_capsule_fingerprint=persisted_capsule.fingerprint,
            expected_capsule_workspace=persisted_capsule.workspace,
        )

        assert restored is not None
        assert restored.previous_response_id == "response-after-signal"
        assert restored.metadata["ac_dispatch_id"] == second_dispatch_id

    @pytest.mark.asyncio
    async def test_capsule_loader_fails_closed_on_signal_followup_dispatch_head(self) -> None:
        """A crashed SessionSignal follow-up head must not resume the AC prompt.

        Blocker #1: the follow-up dispatch persists an explicit
        ``session_signal_followup`` phase. Resuming it would let the caller
        replay the ORIGINAL AC prompt in a session that is mid-signal-turn,
        repeating the AC's (possibly non-idempotent) acceptance work. With no
        safe exact reconstruction the loader must fail closed instead.
        """
        runtime = SimpleNamespace(
            runtime_backend="codex_cli",
            working_directory="/tmp/project",
            permission_mode="acceptEdits",
        )
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Do not repeat the AC after a signal-turn crash",
            session_id="session-signal-followup-head",
            seed_goal="Ship",
        )
        primary_dispatch_id = "1" * 32
        followup_dispatch_id = "2" * 32

        def _handle(previous_response_id: str, origin: str) -> RuntimeHandle:
            return RuntimeHandle(
                backend="codex_cli",
                kind="implementation_session",
                native_session_id="codex-shared-session",
                previous_response_id=previous_response_id,
                cwd="/tmp/project",
                approval_mode="acceptEdits",
                metadata={
                    **runtime_identity.to_metadata(),
                    "ac_capsule_version": persisted_capsule.version,
                    "ac_capsule_fingerprint": persisted_capsule.fingerprint,
                    "ac_session_origin": origin,
                },
            )

        event_store.replay.return_value = [
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            _dispatched_capsule_event(
                runtime_identity,
                persisted_capsule,
                dispatch_id=primary_dispatch_id,
                dispatch_kind="primary",
            ),
            _dispatch_lifecycle_event(
                runtime_identity,
                "execution.session.started",
                dispatch_id=primary_dispatch_id,
                runtime_handle=_handle("response-before-signal", "fresh"),
            ),
            _dispatched_capsule_event(
                runtime_identity,
                persisted_capsule,
                dispatch_id=followup_dispatch_id,
                previous_dispatch_id=primary_dispatch_id,
                session_origin="restored_same_attempt",
                dispatch_kind="session_signal_followup",
                signal_id="sig-123",
                signal_mode="inform",
                follow_up_input_digest="sha256:" + "a" * 64,
            ),
            _dispatch_lifecycle_event(
                runtime_identity,
                "execution.session.started",
                dispatch_id=followup_dispatch_id,
                runtime_handle=_handle("response-after-signal", "restored_same_attempt"),
            ),
        ]

        with pytest.raises(AmbiguousACExecutionError, match="non-primary provider-entry phase"):
            await executor._load_persisted_ac_runtime_handle(
                0,
                execution_context_id="session-signal-followup-head",
                retry_attempt=0,
                expected_capsule_fingerprint=persisted_capsule.fingerprint,
                expected_capsule_workspace=persisted_capsule.workspace,
            )

    @pytest.mark.asyncio
    async def test_capsule_loader_rejects_signal_followup_missing_phase_identity(self) -> None:
        """A follow-up dispatch without its signal identity is corrupt, not resumable.

        Blocker #1: a ``session_signal_followup`` phase that lost its signal
        id/mode/input digest cannot prove which phase it opened, so the loader
        rejects the record rather than guessing.
        """
        runtime = SimpleNamespace(
            runtime_backend="codex_cli",
            working_directory="/tmp/project",
            permission_mode="acceptEdits",
        )
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Reject a follow-up missing its phase identity",
            session_id="session-signal-followup-corrupt",
            seed_goal="Ship",
        )
        primary_dispatch_id = "1" * 32
        followup_dispatch_id = "2" * 32

        event_store.replay.return_value = [
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            _dispatched_capsule_event(
                runtime_identity,
                persisted_capsule,
                dispatch_id=primary_dispatch_id,
                dispatch_kind="primary",
            ),
            _dispatched_capsule_event(
                runtime_identity,
                persisted_capsule,
                dispatch_id=followup_dispatch_id,
                previous_dispatch_id=primary_dispatch_id,
                session_origin="restored_same_attempt",
                dispatch_kind="session_signal_followup",
                signal_id=None,
                signal_mode=None,
                follow_up_input_digest=None,
            ),
        ]

        with pytest.raises(ValueError, match="missing phase identity"):
            await executor._load_persisted_ac_runtime_handle(
                0,
                execution_context_id="session-signal-followup-corrupt",
                retry_attempt=0,
                expected_capsule_fingerprint=persisted_capsule.fingerprint,
                expected_capsule_workspace=persisted_capsule.workspace,
            )

    @pytest.mark.asyncio
    async def test_capsule_loader_fails_closed_on_sealed_primary_head(self) -> None:
        """A sealed primary whose follow-up write never landed must not replay.

        R2 blocker #1: after the primary provider turn completes, a SessionSignal
        follow-up seals the primary and then persists its own dispatch. If that
        follow-up write fails (or a crash lands before the follow-up terminal),
        the primary is the chain head again — but it is sealed, so recovery must
        fail closed instead of resuming it and replaying the original AC prompt.
        """
        runtime = SimpleNamespace(
            runtime_backend="codex_cli",
            working_directory="/tmp/project",
            permission_mode="acceptEdits",
        )
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Do not replay a sealed primary after a lost follow-up write",
            session_id="session-sealed-primary-head",
            seed_goal="Ship",
        )
        primary_dispatch_id = "1" * 32
        handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            native_session_id="codex-sealed-primary",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                **runtime_identity.to_metadata(),
                "ac_capsule_version": persisted_capsule.version,
                "ac_capsule_fingerprint": persisted_capsule.fingerprint,
                "ac_session_origin": "fresh",
            },
        )
        event_store.replay.return_value = [
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            _dispatched_capsule_event(
                runtime_identity,
                persisted_capsule,
                dispatch_id=primary_dispatch_id,
                dispatch_kind="primary",
            ),
            _dispatch_lifecycle_event(
                runtime_identity,
                "execution.session.started",
                dispatch_id=primary_dispatch_id,
                runtime_handle=handle,
            ),
            # The follow-up sealed the primary but its own dispatch write was lost.
            _sealed_dispatch_event(
                runtime_identity,
                persisted_capsule,
                dispatch_id=primary_dispatch_id,
            ),
        ]

        with pytest.raises(AmbiguousACExecutionError, match="sealed provider-entry phase"):
            await executor._load_persisted_ac_runtime_handle(
                0,
                execution_context_id="session-sealed-primary-head",
                retry_attempt=0,
                expected_capsule_fingerprint=persisted_capsule.fingerprint,
                expected_capsule_workspace=persisted_capsule.workspace,
            )

    @pytest.mark.asyncio
    async def test_capsule_loader_follows_explicit_recovery_session_successor(self) -> None:
        """A durable recovered linkage supersedes its failed provider session."""

        runtime = SimpleNamespace(
            runtime_backend="codex_cli",
            working_directory="/tmp/project",
            permission_mode="acceptEdits",
        )
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Follow the explicit replacement provider session",
            session_id="session-dispatch-recovered-successor",
            seed_goal="Ship",
        )
        dispatch_id = "6" * 32

        def _handle(session: str) -> RuntimeHandle:
            return RuntimeHandle(
                backend="codex_cli",
                kind="implementation_session",
                native_session_id=session,
                cwd="/tmp/project",
                approval_mode="acceptEdits",
                metadata={
                    **runtime_identity.to_metadata(),
                    "ac_capsule_version": persisted_capsule.version,
                    "ac_capsule_fingerprint": persisted_capsule.fingerprint,
                    "ac_session_origin": "fresh",
                },
            )

        event_store.replay.return_value = [
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            _dispatched_capsule_event(
                runtime_identity,
                persisted_capsule,
                dispatch_id=dispatch_id,
            ),
            _dispatch_lifecycle_event(
                runtime_identity,
                "execution.session.started",
                dispatch_id=dispatch_id,
                runtime_handle=_handle("codex-failed-session"),
            ),
            _dispatch_lifecycle_event(
                runtime_identity,
                "execution.session.recovered",
                dispatch_id=dispatch_id,
                runtime_handle=_handle("codex-replacement-session"),
                extra_data={
                    "recovery_discontinuity": {
                        "reason": "replacement_session",
                        "failed": {"resume_session_id": "codex-failed-session"},
                        "replacement": {"resume_session_id": "codex-replacement-session"},
                    }
                },
            ),
        ]

        restored = await executor._load_persisted_ac_runtime_handle(
            0,
            execution_context_id="session-dispatch-recovered-successor",
            retry_attempt=0,
            expected_capsule_fingerprint=persisted_capsule.fingerprint,
            expected_capsule_workspace=persisted_capsule.workspace,
        )

        assert restored is not None
        assert restored.native_session_id == "codex-replacement-session"

    @pytest.mark.asyncio
    async def test_capsule_loader_rejects_tied_conflicting_continuation_handles(self) -> None:
        """Timestamp order cannot choose between divergent response-chain cursors."""

        runtime = SimpleNamespace(
            runtime_backend="codex_cli",
            working_directory="/tmp/project",
            permission_mode="acceptEdits",
        )
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Reject divergent continuation cursors",
            session_id="session-dispatch-cursor-conflict",
            seed_goal="Ship",
        )
        dispatch_id = "5" * 32

        def _handle(previous_response_id: str) -> RuntimeHandle:
            return RuntimeHandle(
                backend="codex_cli",
                kind="implementation_session",
                native_session_id="codex-same-session",
                previous_response_id=previous_response_id,
                cwd="/tmp/project",
                approval_mode="acceptEdits",
                metadata={
                    **runtime_identity.to_metadata(),
                    "ac_capsule_version": persisted_capsule.version,
                    "ac_capsule_fingerprint": persisted_capsule.fingerprint,
                    "ac_session_origin": "fresh",
                },
            )

        event_store.replay.return_value = [
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            _dispatched_capsule_event(
                runtime_identity,
                persisted_capsule,
                dispatch_id=dispatch_id,
            ),
            _dispatch_lifecycle_event(
                runtime_identity,
                "execution.session.resumed",
                dispatch_id=dispatch_id,
                runtime_handle=_handle("response-a"),
            ),
            _dispatch_lifecycle_event(
                runtime_identity,
                "execution.session.resumed",
                dispatch_id=dispatch_id,
                runtime_handle=_handle("response-b"),
            ),
        ]

        with pytest.raises(AmbiguousACExecutionError, match="equally authoritative"):
            await executor._load_persisted_ac_runtime_handle(
                0,
                execution_context_id="session-dispatch-cursor-conflict",
                retry_attempt=0,
                expected_capsule_fingerprint=persisted_capsule.fingerprint,
                expected_capsule_workspace=persisted_capsule.workspace,
            )

    @pytest.mark.asyncio
    async def test_capsule_loader_rejects_lifecycle_for_unknown_dispatch(self) -> None:
        """A resumable handle cannot be borrowed from another provider entry."""

        runtime = SimpleNamespace(
            runtime_backend="codex_cli",
            working_directory="/tmp/project",
            permission_mode="acceptEdits",
        )
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Reject a mismatched lifecycle dispatch",
            session_id="session-dispatch-mismatch",
            seed_goal="Ship",
        )
        handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            native_session_id="codex-foreign-dispatch",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                **runtime_identity.to_metadata(),
                "ac_capsule_version": persisted_capsule.version,
                "ac_capsule_fingerprint": persisted_capsule.fingerprint,
                "ac_session_origin": "fresh",
            },
        )
        event_store.replay.return_value = [
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            _dispatched_capsule_event(
                runtime_identity,
                persisted_capsule,
                dispatch_id="1" * 32,
            ),
            _dispatch_lifecycle_event(
                runtime_identity,
                "execution.session.started",
                dispatch_id="2" * 32,
                runtime_handle=handle,
            ),
        ]

        with pytest.raises(ValueError, match="unknown dispatch id"):
            await executor._load_persisted_ac_runtime_handle(
                0,
                execution_context_id="session-dispatch-mismatch",
                retry_attempt=0,
                expected_capsule_fingerprint=persisted_capsule.fingerprint,
                expected_capsule_workspace=persisted_capsule.workspace,
            )

    @pytest.mark.parametrize(
        ("corruption", "error_match"),
        [
            ("missing_id", "dispatch id is missing"),
            ("duplicate_id", "dispatch id is duplicated"),
            ("fingerprint", "dispatch fingerprint disagrees"),
            ("origin", "dispatch session origin is invalid"),
            ("missing_predecessor", "dispatch predecessor is missing"),
            ("unknown_predecessor", "predecessor references an unknown"),
        ],
    )
    @pytest.mark.asyncio
    async def test_capsule_loader_rejects_corrupt_dispatch_contract(
        self,
        corruption: str,
        error_match: str,
    ) -> None:
        """Every authority-bearing dispatch field is validated fail-closed."""
        executor = ParallelACExecutor(
            adapter=SimpleNamespace(
                runtime_backend="codex_cli",
                working_directory="/tmp/project",
                permission_mode="acceptEdits",
            ),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Reject corrupted durable dispatch authority",
            session_id=f"session-corrupt-dispatch-{corruption}",
            seed_goal="Ship",
        )
        dispatch_event = _dispatched_capsule_event(
            runtime_identity,
            persisted_capsule,
            dispatch_id="3" * 32,
        )
        dispatch_events = [dispatch_event]
        if corruption == "missing_id":
            dispatch_event.data.pop("ac_dispatch_id")
        elif corruption == "duplicate_id":
            dispatch_events.append(dispatch_event)
        elif corruption == "fingerprint":
            dispatch_event.data["capsule_fingerprint"] = "sha256:" + "0" * 64
        elif corruption == "origin":
            dispatch_event.data["session_origin"] = "foreign_attempt"
        elif corruption == "missing_predecessor":
            dispatch_event.data.pop("previous_ac_dispatch_id")
        elif corruption == "unknown_predecessor":
            dispatch_event.data["previous_ac_dispatch_id"] = "4" * 32
        executor._event_store.replay.return_value = [
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            *dispatch_events,
        ]

        with pytest.raises(ValueError, match=error_match):
            await executor._load_persisted_ac_runtime_handle(
                0,
                execution_context_id=f"session-corrupt-dispatch-{corruption}",
                retry_attempt=0,
                expected_capsule_fingerprint=persisted_capsule.fingerprint,
                expected_capsule_workspace=persisted_capsule.workspace,
            )

    @pytest.mark.parametrize(
        ("topology", "links", "error_match"),
        [
            (
                "branch",
                (("1", None), ("2", "1"), ("3", "1")),
                "branches from one predecessor",
            ),
            (
                "multiple_roots",
                (("1", None), ("2", None)),
                "not one linear chain",
            ),
            (
                "cycle",
                (("1", "2"), ("2", "1")),
                "not one linear chain",
            ),
            (
                "disconnected",
                (("1", None), ("2", "1"), ("3", "4"), ("4", "3")),
                "is disconnected",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_capsule_loader_rejects_non_linear_dispatch_topology(
        self,
        topology: str,
        links: tuple[tuple[str, str | None], ...],
        error_match: str,
    ) -> None:
        """Branching, root ambiguity, cycles, and disconnected chains fail closed."""
        executor = ParallelACExecutor(
            adapter=SimpleNamespace(
                runtime_backend="codex_cli",
                working_directory="/tmp/project",
                permission_mode="acceptEdits",
            ),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        execution_id = f"session-corrupt-dispatch-topology-{topology}"
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Reject non-linear durable dispatch history",
            session_id=execution_id,
            seed_goal="Ship",
        )
        executor._event_store.replay.return_value = [
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            *[
                _dispatched_capsule_event(
                    runtime_identity,
                    persisted_capsule,
                    dispatch_id=dispatch_digit * 32,
                    previous_dispatch_id=(
                        predecessor_digit * 32 if predecessor_digit is not None else None
                    ),
                )
                for dispatch_digit, predecessor_digit in links
            ],
        ]

        with pytest.raises(ValueError, match=error_match):
            await executor._load_persisted_ac_runtime_handle(
                0,
                execution_context_id=execution_id,
                retry_attempt=0,
                expected_capsule_fingerprint=persisted_capsule.fingerprint,
                expected_capsule_workspace=persisted_capsule.workspace,
            )

    @pytest.mark.parametrize(
        ("corruption", "error_type", "error_match"),
        [
            ("malformed", ValueError, "linkage is malformed"),
            ("cycle", AmbiguousACExecutionError, "replacement cycle"),
            ("conflict", AmbiguousACExecutionError, "conflicting replacement"),
        ],
    )
    @pytest.mark.asyncio
    async def test_capsule_loader_rejects_corrupt_recovery_links(
        self,
        corruption: str,
        error_type: type[Exception],
        error_match: str,
    ) -> None:
        """Malformed, cyclic, and branching replacement histories never pick a session."""
        executor = ParallelACExecutor(
            adapter=SimpleNamespace(
                runtime_backend="codex_cli",
                working_directory="/tmp/project",
                permission_mode="acceptEdits",
            ),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Reject corrupt provider-session replacement history",
            session_id=f"session-corrupt-recovery-{corruption}",
            seed_goal="Ship",
        )
        dispatch_id = "5" * 32

        def _recovered_event(failed: str, replacement: str) -> BaseEvent:
            handle = RuntimeHandle(
                backend="codex_cli",
                kind="implementation_session",
                native_session_id=replacement,
                cwd="/tmp/project",
                approval_mode="acceptEdits",
                metadata={
                    **runtime_identity.to_metadata(),
                    "ac_capsule_version": persisted_capsule.version,
                    "ac_capsule_fingerprint": persisted_capsule.fingerprint,
                    "ac_session_origin": "restored_same_attempt",
                },
            )
            return _dispatch_lifecycle_event(
                runtime_identity,
                "execution.session.recovered",
                dispatch_id=dispatch_id,
                runtime_handle=handle,
                extra_data={
                    "recovery_discontinuity": {
                        "failed": {"resume_session_id": failed},
                        "replacement": {"resume_session_id": replacement},
                    }
                },
            )

        if corruption == "malformed":
            malformed = _recovered_event("session-a", "session-b")
            malformed.data["recovery_discontinuity"] = {"failed": {}}
            recovery_events = [malformed]
        elif corruption == "cycle":
            recovery_events = [
                _recovered_event("session-a", "session-b"),
                _recovered_event("session-b", "session-a"),
            ]
        else:
            recovery_events = [
                _recovered_event("session-a", "session-b"),
                _recovered_event("session-a", "session-c"),
            ]
        executor._event_store.replay.return_value = [
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            _dispatched_capsule_event(
                runtime_identity,
                persisted_capsule,
                dispatch_id=dispatch_id,
            ),
            *recovery_events,
        ]

        with pytest.raises(error_type, match=error_match):
            await executor._load_persisted_ac_runtime_handle(
                0,
                execution_context_id=f"session-corrupt-recovery-{corruption}",
                retry_attempt=0,
                expected_capsule_fingerprint=persisted_capsule.fingerprint,
                expected_capsule_workspace=persisted_capsule.workspace,
            )

    @pytest.mark.asyncio
    async def test_capsule_loader_blocks_same_attempt_after_completed_dispatch(self) -> None:
        """A completed dispatch is terminal, not permission for fresh redispatch."""

        runtime = SimpleNamespace(
            runtime_backend="codex_cli",
            working_directory="/tmp/project",
            permission_mode="acceptEdits",
        )
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Do not repeat an already completed provider dispatch",
            session_id="session-dispatch-completed",
            seed_goal="Ship",
        )
        dispatch_id = "c" * 32
        event_store.replay.return_value = [
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            _dispatched_capsule_event(
                runtime_identity,
                persisted_capsule,
                dispatch_id=dispatch_id,
            ),
            _dispatch_lifecycle_event(
                runtime_identity,
                "execution.session.completed",
                dispatch_id=dispatch_id,
                runtime_handle=None,
            ),
        ]

        with pytest.raises(CompletedACExecutionError, match="already completed"):
            await executor._load_persisted_ac_runtime_handle(
                0,
                execution_context_id="session-dispatch-completed",
                retry_attempt=0,
                expected_capsule_fingerprint=persisted_capsule.fingerprint,
                expected_capsule_workspace=persisted_capsule.workspace,
            )

    @pytest.mark.parametrize(
        ("corruption", "error_match"),
        [
            ("facts", "lifecycle events disagree"),
            ("verify_shape", "verify outcome has an invalid shape"),
            ("verify_facts", "verify outcomes disagree"),
        ],
    )
    @pytest.mark.asyncio
    async def test_capsule_loader_rejects_corrupt_completed_contract(
        self,
        corruption: str,
        error_match: str,
    ) -> None:
        """Duplicate terminal facts must agree exactly before success is reconstructed."""
        executor = ParallelACExecutor(
            adapter=SimpleNamespace(
                runtime_backend="codex_cli",
                working_directory="/tmp/project",
                permission_mode="acceptEdits",
            ),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Reject conflicting durable completion facts",
            session_id=f"session-corrupt-completed-{corruption}",
            seed_goal="Ship",
        )
        dispatch_id = "6" * 32
        passed_outcome = {
            "passed": True,
            "reason": None,
            "output_tail": "",
            "missing_artifacts": [],
        }
        first = _dispatch_lifecycle_event(
            runtime_identity,
            "execution.session.completed",
            dispatch_id=dispatch_id,
            runtime_handle=None,
            result_summary="done",
            session_id="completed-session",
            extra_data={"verify_gate_outcome": passed_outcome},
        )
        second = _dispatch_lifecycle_event(
            runtime_identity,
            "execution.session.completed",
            dispatch_id=dispatch_id,
            runtime_handle=None,
            result_summary="done",
            session_id="completed-session",
            extra_data={"verify_gate_outcome": passed_outcome},
        )
        if corruption == "facts":
            second.data["result_summary"] = "different"
        elif corruption == "verify_shape":
            first.data["verify_gate_outcome"] = {"passed": True}
        else:
            second.data["verify_gate_outcome"] = {
                **passed_outcome,
                "output_tail": "different",
            }
        executor._event_store.replay.return_value = [
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            _dispatched_capsule_event(
                runtime_identity,
                persisted_capsule,
                dispatch_id=dispatch_id,
            ),
            first,
            second,
        ]

        with pytest.raises(ValueError, match=error_match):
            await executor._load_persisted_ac_runtime_handle(
                0,
                execution_context_id=f"session-corrupt-completed-{corruption}",
                retry_attempt=0,
                expected_capsule_fingerprint=persisted_capsule.fingerprint,
                expected_capsule_workspace=persisted_capsule.workspace,
            )

    @pytest.mark.asyncio
    async def test_capsule_loader_preserves_legacy_no_dispatch_completion(self) -> None:
        """Pre-dispatch-id completed streams remain terminal and retain their result."""
        executor = ParallelACExecutor(
            adapter=SimpleNamespace(
                runtime_backend="codex_cli",
                working_directory="/tmp/project",
                permission_mode="acceptEdits",
            ),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Recover a legacy completed attempt",
            session_id="session-legacy-completed",
            seed_goal="Ship",
        )
        executor._event_store.replay.return_value = [
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            BaseEvent(
                type="execution.session.completed",
                aggregate_type="execution",
                aggregate_id=runtime_identity.session_scope_id,
                data={
                    **runtime_identity.to_metadata(),
                    "success": True,
                    "result_summary": "legacy done",
                    "session_id": "legacy-session",
                    "runtime": None,
                },
            ),
        ]

        with pytest.raises(CompletedACExecutionError) as completed:
            await executor._load_persisted_ac_runtime_handle(
                0,
                execution_context_id="session-legacy-completed",
                retry_attempt=0,
                expected_capsule_fingerprint=persisted_capsule.fingerprint,
                expected_capsule_workspace=persisted_capsule.workspace,
            )

        assert completed.value.result_summary == "legacy done"
        assert completed.value.session_id == "legacy-session"

    @pytest.mark.asyncio
    async def test_capsule_loader_preserves_legacy_no_dispatch_failed_resume(self) -> None:
        """A legacy failed stream may resume only its exact capsule-bound handle."""
        executor = ParallelACExecutor(
            adapter=SimpleNamespace(
                runtime_backend="codex_cli",
                working_directory="/tmp/project",
                permission_mode="acceptEdits",
            ),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Resume a legacy failed attempt",
            session_id="session-legacy-failed-resume",
            seed_goal="Ship",
        )
        handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            native_session_id="legacy-failed-session",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                **runtime_identity.to_metadata(),
                "ac_capsule_version": persisted_capsule.version,
                "ac_capsule_fingerprint": persisted_capsule.fingerprint,
                "ac_session_origin": "restored_same_attempt",
            },
        )
        executor._event_store.replay.return_value = [
            _compiled_capsule_event(runtime_identity, persisted_capsule),
            BaseEvent(
                type="execution.session.failed",
                aggregate_type="execution",
                aggregate_id=runtime_identity.session_scope_id,
                data={
                    **runtime_identity.to_metadata(),
                    "success": False,
                    "runtime": handle.to_persisted_dict(),
                },
            ),
        ]

        restored = await executor._load_persisted_ac_runtime_handle(
            0,
            execution_context_id="session-legacy-failed-resume",
            retry_attempt=0,
            expected_capsule_fingerprint=persisted_capsule.fingerprint,
            expected_capsule_workspace=persisted_capsule.workspace,
        )

        assert restored is not None
        assert restored.native_session_id == "legacy-failed-session"

    @pytest.mark.asyncio
    async def test_single_ac_recovers_completed_dispatch_as_success(self) -> None:
        """Crash recovery returns the already-gated result instead of failing the workflow."""

        class _Runtime:
            runtime_backend = "codex_cli"
            working_directory = "/tmp/project"
            permission_mode = "acceptEdits"

            def __init__(self) -> None:
                self.calls = 0

            async def execute_task(self, **_kwargs: object):
                self.calls += 1
                yield AgentMessage(type="result", content="[TASK_COMPLETE]")

        runtime = _Runtime()
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        ac_content = "Recover one already completed AC"
        node_identity = ExecutionNodeIdentity.root(
            execution_context_id="session-dispatch-completed-recovery",
            ac_index=0,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content=ac_content,
            session_id="session-dispatch-completed-recovery",
            seed_goal="Ship",
            node_identity=node_identity,
        )
        dispatch_id = "7" * 32
        appended_events.extend(
            [
                _compiled_capsule_event(runtime_identity, persisted_capsule),
                _dispatched_capsule_event(
                    runtime_identity,
                    persisted_capsule,
                    dispatch_id=dispatch_id,
                ),
                _dispatch_lifecycle_event(
                    runtime_identity,
                    "execution.session.completed",
                    dispatch_id=dispatch_id,
                    runtime_handle=None,
                    result_summary="[TASK_COMPLETE] recovered",
                    session_id="codex-completed-session",
                ),
            ]
        )

        result = await executor._execute_single_ac(
            ac_index=0,
            ac_content=ac_content,
            session_id="session-dispatch-completed-recovery",
            tools=["Read", "Edit"],
            tool_catalog=None,
            system_prompt="system",
            seed_goal="Ship",
            node_identity=node_identity,
        )

        assert result.success is True
        assert result.final_message == "[TASK_COMPLETE] recovered"
        assert result.session_id == "codex-completed-session"
        assert runtime.calls == 0
        assert (
            len(
                [
                    event
                    for event in appended_events
                    if event.type == "execution.ac.attempt.dispatched"
                ]
            )
            == 1
        )

    @pytest.mark.parametrize(
        "terminal_event_type",
        [None, "execution.session.failed"],
    )
    @pytest.mark.asyncio
    async def test_atomic_ac_refuses_fresh_dispatch_after_unresumable_tool_effect(
        self,
        terminal_event_type: str | None,
    ) -> None:
        """A crash after a tool effect cannot be recovered by duplicating the attempt."""

        class _Runtime:
            runtime_backend = "codex_cli"
            working_directory = "/tmp/project"
            permission_mode = "acceptEdits"

            def __init__(self) -> None:
                self.calls = 0

            async def execute_task(self, **_kwargs: object):
                self.calls += 1
                yield AgentMessage(type="result", content="[TASK_COMPLETE]")

        runtime = _Runtime()
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Apply one non-idempotent migration",
            session_id="session-capsule-effect",
            seed_goal="Ship",
        )
        appended_events.extend(
            [
                _compiled_capsule_event(runtime_identity, persisted_capsule),
                BaseEvent(
                    type="execution.tool.completed",
                    aggregate_type="execution",
                    aggregate_id=runtime_identity.session_scope_id,
                    data={
                        **runtime_identity.to_metadata(),
                        "tool_name": "Bash",
                        "tool_result_text": "migration applied",
                    },
                ),
            ]
        )
        if terminal_event_type is not None:
            appended_events.append(
                BaseEvent(
                    type=terminal_event_type,
                    aggregate_type="execution",
                    aggregate_id=runtime_identity.session_scope_id,
                    data={
                        **runtime_identity.to_metadata(),
                        "runtime": None,
                    },
                )
            )

        with pytest.raises(
            AmbiguousACExecutionError,
            match="recorded tool effects without a reusable runtime handle",
        ):
            await executor._execute_atomic_ac(
                ac_index=0,
                ac_content="Apply one non-idempotent migration",
                session_id="session-capsule-effect",
                tools=["Read", "Edit"],
                system_prompt="system",
                seed_goal="Ship",
                depth=0,
                start_time=datetime.now(UTC),
            )

        assert runtime.calls == 0

    @pytest.mark.asyncio
    async def test_atomic_ac_refuses_durable_capsule_drift_before_resume(self) -> None:
        """Recovery cannot apply an old provider session to a new capsule."""

        class _Runtime:
            runtime_backend = "codex_cli"
            working_directory = "/tmp/project"
            permission_mode = "acceptEdits"

            def __init__(self) -> None:
                self.calls = 0

            async def execute_task(self, **_kwargs: object):
                self.calls += 1
                yield AgentMessage(type="result", content="[TASK_COMPLETE]")

        runtime = _Runtime()
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Original AC authority",
            session_id="session-capsule-mismatch",
            seed_goal="Ship",
        )
        appended_events.append(
            BaseEvent(
                type="execution.ac.capsule.compiled",
                aggregate_type="execution",
                aggregate_id="session-capsule-mismatch_ac_1",
                data={
                    "ac_id": "session-capsule-mismatch_ac_1",
                    "session_attempt_id": "session-capsule-mismatch_ac_1_attempt_1",
                    "capsule_fingerprint": persisted_capsule.fingerprint,
                    "capsule_manifest": persisted_capsule.manifest.to_contract_data(),
                },
            )
        )
        with pytest.raises(ValueError, match="capsule fingerprint disagrees"):
            await executor._execute_atomic_ac(
                ac_index=0,
                ac_content="Changed AC authority",
                session_id="session-capsule-mismatch",
                tools=["Read", "Edit"],
                system_prompt="system",
                seed_goal="Ship",
                depth=0,
                start_time=datetime.now(UTC),
            )

        assert runtime.calls == 0

    @pytest.mark.asyncio
    async def test_atomic_ac_refuses_dispatch_authority_drift_before_resume(self) -> None:
        """Tools, prompt, runtime, and routing authority are capsule identity."""

        class _Runtime:
            runtime_backend = "codex_cli"
            working_directory = "/tmp/project"
            permission_mode = "acceptEdits"

            def __init__(self) -> None:
                self.calls = 0

            async def execute_task(self, **_kwargs: object):
                self.calls += 1
                yield AgentMessage(type="result", content="[TASK_COMPLETE]")

        runtime = _Runtime()
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Implement the AC",
            session_id="session-capsule-dispatch-drift",
            seed_goal="Ship",
        )
        appended_events.append(_compiled_capsule_event(runtime_identity, persisted_capsule))

        with pytest.raises(ValueError, match="capsule fingerprint disagrees"):
            await executor._execute_atomic_ac(
                ac_index=0,
                ac_content="Implement the AC",
                session_id="session-capsule-dispatch-drift",
                tools=["Read", "Edit"],
                system_prompt="changed-system-prompt",
                seed_goal="Ship",
                depth=0,
                start_time=datetime.now(UTC),
            )

        assert runtime.calls == 0

    @pytest.mark.asyncio
    async def test_atomic_ac_resumes_only_with_matching_durable_capsule(self) -> None:
        """A native handle may resume only after the exact capsule is durable."""

        class _Runtime:
            runtime_backend = "codex_cli"
            working_directory = "/tmp/project"
            permission_mode = "acceptEdits"

            def __init__(self) -> None:
                self.resume_handles: list[RuntimeHandle | None] = []

            async def execute_task(self, **kwargs: object):
                self.resume_handles.append(kwargs.get("resume_handle"))
                yield AgentMessage(type="result", content="[TASK_COMPLETE]")

        runtime = _Runtime()
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Implement the AC",
            session_id="session-capsule-resume",
            seed_goal="Ship",
        )
        persisted_handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            native_session_id="codex-same-attempt",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                **runtime_identity.to_metadata(),
                "ac_capsule_version": persisted_capsule.version,
                "ac_capsule_fingerprint": persisted_capsule.fingerprint,
                "ac_session_origin": "fresh",
            },
        )
        appended_events.extend(
            [
                BaseEvent(
                    type="execution.ac.capsule.compiled",
                    aggregate_type="execution",
                    aggregate_id=runtime_identity.session_scope_id,
                    data={
                        **runtime_identity.to_metadata(),
                        "capsule_fingerprint": persisted_capsule.fingerprint,
                        "capsule_manifest": persisted_capsule.manifest.to_contract_data(),
                    },
                ),
                BaseEvent(
                    type="execution.session.started",
                    aggregate_type="execution",
                    aggregate_id=runtime_identity.session_scope_id,
                    data={
                        **runtime_identity.to_metadata(),
                        "runtime": persisted_handle.to_dict(),
                    },
                ),
            ]
        )
        await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement the AC",
            session_id="session-capsule-resume",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert runtime.resume_handles[0] is not None
        assert runtime.resume_handles[0].native_session_id == "codex-same-attempt"
        compiled_events = [
            event for event in appended_events if event.type == "execution.ac.capsule.compiled"
        ]
        assert compiled_events[-1].data["session_origin"] == "restored_same_attempt"

    @pytest.mark.asyncio
    async def test_atomic_ac_does_not_resume_legacy_handle_without_capsule(self) -> None:
        """Pre-capsule runtime history can seed no provider continuity."""

        class _Runtime:
            runtime_backend = "codex_cli"
            working_directory = "/tmp/project"
            permission_mode = "acceptEdits"

            def __init__(self) -> None:
                self.resume_handles: list[RuntimeHandle | None] = []

            async def execute_task(self, **kwargs: object):
                self.resume_handles.append(kwargs.get("resume_handle"))
                yield AgentMessage(type="result", content="[TASK_COMPLETE]")

        runtime = _Runtime()
        event_store, appended_events = _make_replaying_event_store()
        current_identity = build_ac_runtime_identity(
            0,
            execution_context_id="session-capsule-legacy",
            retry_attempt=1,
        )
        legacy_handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            native_session_id="legacy-retry-session",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "ac_id": current_identity.ac_id,
                "scope": "ac",
                "session_role": "implementation",
                "ac_index": 0,
                "session_scope_id": current_identity.session_scope_id,
            },
        )
        appended_events.append(
            BaseEvent(
                type="execution.session.started",
                aggregate_type="execution",
                aggregate_id=current_identity.session_scope_id,
                data={
                    "ac_id": current_identity.ac_id,
                    "session_scope_id": current_identity.session_scope_id,
                    "runtime": legacy_handle.to_dict(),
                },
            )
        )
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Implement the AC",
            session_id="session-capsule-legacy",
            tools=["Read", "Edit"],
            system_prompt="system",
            seed_goal="Ship",
            depth=0,
            start_time=datetime.now(UTC),
            retry_attempt=1,
            semantic_ac_key="semantic-key",
        )

        assert runtime.resume_handles[0] is not None
        assert runtime.resume_handles[0].native_session_id is None
        compiled_event = next(
            event for event in appended_events if event.type == "execution.ac.capsule.compiled"
        )
        assert compiled_event.data["session_origin"] == "fresh"

    @pytest.mark.asyncio
    async def test_capsule_loader_skips_legacy_scope_replay(self) -> None:
        """Capsule-era attempts are authored only in the canonical AC aggregate."""
        event_store = AsyncMock()
        event_store.replay.return_value = []
        executor = ParallelACExecutor(
            adapter=SimpleNamespace(
                runtime_backend="codex_cli",
                working_directory="/tmp/project",
                permission_mode="acceptEdits",
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        identity = build_ac_runtime_identity(
            3,
            execution_context_id="exec-canonical-replay",
            retry_attempt=2,
        )

        restored = await executor._load_persisted_ac_runtime_handle(
            3,
            execution_context_id="exec-canonical-replay",
            retry_attempt=2,
            expected_capsule_fingerprint="sha256:" + "0" * 64,
            expected_capsule_workspace=os.path.realpath("/tmp/project"),
        )

        assert restored is None
        event_store.replay.assert_awaited_once_with(
            "execution",
            identity.session_scope_id,
        )

    @pytest.mark.asyncio
    async def test_capsule_loader_incrementally_fetches_retry_history(self) -> None:
        """Growing retry history is fetched once, not replayed from row zero per attempt."""

        class _IncrementalStore:
            def __init__(self) -> None:
                self.events: list[BaseEvent] = []
                self.returned_events = 0
                self.replay_calls = 0

            async def get_events_after(
                self,
                aggregate_type: str,
                aggregate_id: str,
                last_row_id: int,
            ) -> tuple[list[BaseEvent], int]:
                matching = [
                    event
                    for event in self.events
                    if event.aggregate_type == aggregate_type and event.aggregate_id == aggregate_id
                ]
                new_events = matching[last_row_id:]
                self.returned_events += len(new_events)
                return new_events, len(matching)

            async def replay(self, _aggregate_type: str, _aggregate_id: str):
                self.replay_calls += 1
                raise AssertionError("full replay should not be used")

        store = _IncrementalStore()
        executor = ParallelACExecutor(
            adapter=SimpleNamespace(
                runtime_backend="codex_cli",
                working_directory="/tmp/project",
                permission_mode="acceptEdits",
            ),
            event_store=store,  # type: ignore[arg-type]
            console=MagicMock(),
            enable_decomposition=False,
        )

        for retry_attempt in range(5):
            restored = await executor._load_persisted_ac_runtime_handle(
                0,
                execution_context_id="exec-incremental-replay",
                retry_attempt=retry_attempt,
                expected_capsule_fingerprint="sha256:" + "0" * 64,
                expected_capsule_workspace=os.path.realpath("/tmp/project"),
            )
            assert restored is None
            identity = build_ac_runtime_identity(
                0,
                execution_context_id="exec-incremental-replay",
                retry_attempt=retry_attempt,
            )
            store.events.append(
                BaseEvent(
                    type="execution.session.failed",
                    aggregate_type="execution",
                    aggregate_id=identity.session_scope_id,
                    data=identity.to_metadata(),
                )
            )

        assert store.replay_calls == 0
        assert store.returned_events == 4

    @pytest.mark.asyncio
    async def test_capsule_resume_rejects_foreign_workspace_handle(self) -> None:
        """A matching fingerprint cannot authorize continuity from another checkout."""
        runtime = SimpleNamespace(
            runtime_backend="codex_cli",
            working_directory="/tmp/project",
            permission_mode="acceptEdits",
        )
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity, capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content="Implement the AC",
            session_id="session-capsule-foreign-workspace",
            seed_goal="Ship",
        )
        foreign_handle = RuntimeHandle(
            backend="codex_cli",
            kind="implementation_session",
            native_session_id="foreign-workspace-session",
            cwd="/tmp/foreign-project",
            approval_mode="acceptEdits",
            metadata={
                **runtime_identity.to_metadata(),
                "ac_capsule_fingerprint": capsule.fingerprint,
            },
        )
        event_store.replay.return_value = [
            _compiled_capsule_event(runtime_identity, capsule),
            BaseEvent(
                type="execution.session.started",
                aggregate_type="execution",
                aggregate_id=runtime_identity.session_scope_id,
                data={
                    **runtime_identity.to_metadata(),
                    "runtime": foreign_handle.to_dict(),
                },
            ),
        ]
        with pytest.raises(ValueError, match="workspace authority changed"):
            await executor._load_persisted_ac_runtime_handle(
                0,
                execution_context_id="session-capsule-foreign-workspace",
                retry_attempt=0,
                expected_capsule_fingerprint=capsule.fingerprint,
                expected_capsule_workspace=capsule.workspace,
            )

    @pytest.mark.asyncio
    async def test_atomic_ac_refuses_dispatch_when_capsule_replay_is_unreadable(self) -> None:
        """A fresh dispatch cannot guess that no authority-bearing capsule exists."""

        class _Runtime:
            runtime_backend = "codex_cli"
            working_directory = "/tmp/project"
            permission_mode = "acceptEdits"

            def __init__(self) -> None:
                self.calls = 0

            async def execute_task(self, **_kwargs: object):
                self.calls += 1
                yield AgentMessage(type="result", content="[TASK_COMPLETE]")

        runtime = _Runtime()
        event_store = AsyncMock()
        event_store.replay.side_effect = OSError("replay unavailable")
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )

        with pytest.raises(OSError, match="replay unavailable"):
            await executor._execute_atomic_ac(
                ac_index=0,
                ac_content="Implement the AC",
                session_id="session-capsule-replay",
                tools=["Read", "Edit"],
                system_prompt="system",
                seed_goal="Ship",
                depth=0,
                start_time=datetime.now(UTC),
            )

        assert runtime.calls == 0
        event_store.append.assert_not_awaited()

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
    async def test_contract_ac_with_artifacts_ignores_transcript_claims(
        self,
        tmp_path,
    ) -> None:
        """The verify gate owns artifact and command proof for contract ACs."""
        command = "python -c \"print('OK')\""
        (tmp_path / "hello.py").write_text(
            "def greet(name):\n    return f'Hello, {name}'\n",
            encoding="utf-8",
        )
        event_store, appended_events = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            "Done.\n"
            "```json\n"
            "{"
            '"files_touched":["not-backed-by-transcript.py"],'
            '"commands_run":0'
            "}\n"
            "```",
            native_session_id="codex-session-contract-artifact-delegation",
            support_messages=(
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Bash: {command}",
                    tool_name="Bash",
                    data={"tool_input": {"command": command}},
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
            ac_content=_GREETING_AC,
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            ac_spec=AcceptanceCriterionSpec(
                description=_GREETING_AC,
                verify_command=command,
                expected_artifacts=("hello.py",),
            ),
        )

        assert result.success is True
        assert result.error is None
        assert result.typed_evidence is not None
        assert result.typed_evidence.data == {}
        assert runtime.last_prompt is not None
        assert "directly (commands_run)" not in runtime.last_prompt
        assert "ensure they exist in the workspace" in runtime.last_prompt
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == []
        assert evidence_event.data["typed_evidence_valid"] is True
        assert evidence_event.data["verifier_passed"] is True
        assert evidence_event.data["ignored_out_of_scope_evidence_fields"] == [
            "files_touched",
            "commands_run",
        ]

    @pytest.mark.asyncio
    async def test_contract_ac_missing_artifact_rejected_when_verify_gate_disabled(
        self,
        tmp_path,
    ) -> None:
        """Reproduced blocker: with the verify gate off, delegation must not fire.

        ``run_verify_commands=False`` makes ``_apply_verify_gate`` return early, so
        neither the filesystem oracle nor command exit status verifies the contract.
        If the schema still dropped ``commands_run``/``tests_passed``/``files_touched``,
        a contract AC could complete without transcript-backed evidence or an
        artifact on disk. The verify-gate-active guard keeps those fields required,
        so the worker's self-reported evidence fails and the AC is not accepted.
        """
        command = "python -c \"print('OK')\""
        command_json = command.replace("\\", "\\\\").replace('"', '\\"')
        # Deliberately do NOT write hello.py: the artifact is missing on disk.
        event_store, appended_events = _make_replaying_event_store()
        runtime = _FinalMessageRuntime(
            f'Done.\n```json\n{{"commands_run":["{command_json}"]}}\n```',
            native_session_id="codex-session-verify-gate-off-missing-artifact",
            support_messages=(
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: Bash: {command}",
                    tool_name="Bash",
                    data={"tool_input": {"command": command}},
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
            run_verify_commands=False,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content=_GREETING_AC,
            session_id="orch_123",
            tools=["Read", "Edit", "Bash"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the feature",
            depth=0,
            start_time=datetime.now(UTC),
            ac_spec=AcceptanceCriterionSpec(
                description=_GREETING_AC,
                verify_command=command,
                expected_artifacts=("hello.py",),
            ),
        )

        assert result.success is False
        assert result.error is not None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        # files_touched, commands_run, and tests_passed remain required because the gate is off.
        assert "files_touched" in evidence_event.data["required_fields"]
        assert "commands_run" in evidence_event.data["required_fields"]
        assert "tests_passed" in evidence_event.data["required_fields"]
        assert evidence_event.data["typed_evidence_valid"] is False

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
                        type="tool_result",
                        content="command completed with exit code 0",
                        data={"subtype": "tool_result", "exit_code": 0},
                    ),
                    AgentMessage(
                        type="tool",
                        content="Bash: pytest tests/test_generated.py",
                        tool_name="Bash",
                        data={"tool_input": {"command": "pytest tests/test_generated.py"}},
                    ),
                    AgentMessage(
                        type="tool_result",
                        content="tests/test_generated.py passed",
                        data={"subtype": "tool_result", "is_error": False},
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

        with patch("ouroboros.orchestrator.parallel_executor.log") as log_mock:
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

        log_mock.warning.assert_any_call(
            "parallel_executor.ac.verifier_rejected",
            session_id="orch_123",
            execution_id="",
            ac_index=0,
            depth=0,
            reason="Fat-harness verifier failed (claimed test command did not support the AC).",
            typed_evidence_present=True,
            typed_evidence_valid=True,
            verifier_ran=True,
            verifier_passed=False,
            verifier_reasons=["claimed test command did not support the AC"],
            verifier_failure_class="FABRICATION_SUSPECTED",
            verifier_status="FAIL",
            retry_admission="ESCALATE_MODEL",
            verifier_evidence_used=[],
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
        assert evidence_event.data["verifier_status"] == "FAIL"
        assert evidence_event.data["retry_admission"] == "ESCALATE_MODEL"
        assert evidence_event.data["verifier_evidence_used"] == []
        assert evidence_event.data["verifier_reasons"] == [
            "claimed test command did not support the AC"
        ]

    @pytest.mark.asyncio
    async def test_fat_harness_accepts_artifact_success_contract_with_incomplete_typed_evidence(
        self, tmp_path: Any
    ) -> None:
        """Artifact ACs may be proven by expected_artifacts + verify_command."""
        (tmp_path / "output.txt").write_text("OZO_RUN_SMOKE_OK\n", encoding="utf-8")
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                'Done.\n```json\n{"files_touched":["output.txt"]}\n```',
                native_session_id="opencode-session-artifact-contract",
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("artifact"),
            fat_harness_mode=True,
        )

        with patch("ouroboros.orchestrator.parallel_executor.log") as log_mock:
            result = await executor._execute_atomic_ac(
                ac_index=0,
                ac_content="Create output.txt with the smoke marker.",
                session_id="orch_123",
                tools=["Read"],
                tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
                system_prompt="system",
                seed_goal="Ship the artifact",
                depth=0,
                start_time=datetime.now(UTC),
                ac_spec=AcceptanceCriterionSpec(
                    description="Create output.txt with the smoke marker.",
                    expected_artifacts=("output.txt",),
                    verify_command="test -f output.txt",
                ),
            )

        assert result.success is True
        assert result.error is None
        assert result.typed_evidence is not None
        assert result.typed_evidence_validation is not None
        assert result.typed_evidence_validation.ok is True
        assert result.typed_evidence_validation.missing_fields == ()
        assert result.atomic_verifier_verdict is not None
        assert result.atomic_verifier_verdict.passed is True
        assert not any(
            call.args and call.args[0] == "parallel_executor.ac.verifier_rejected"
            for call in log_mock.warning.call_args_list
        )

        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["typed_evidence_present"] is True
        assert evidence_event.data["typed_evidence_valid"] is True
        assert evidence_event.data["enforcement_error"] is None
        assert evidence_event.data["has_success_contract"] is True
        assert evidence_event.data["has_expected_artifacts"] is True
        assert evidence_event.data["verify_gate_active"] is True
        assert evidence_event.data["verifier_ran"] is True
        assert evidence_event.data["verifier_passed"] is True

    @pytest.mark.asyncio
    async def test_fat_harness_accepts_artifact_contract_without_typed_evidence(
        self, tmp_path: Any
    ) -> None:
        """A passing artifact gate may replace every profile evidence field."""
        (tmp_path / "output.txt").write_text("OZO_RUN_SMOKE_OK\n", encoding="utf-8")
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "[TASK_COMPLETE] artifact created without a JSON evidence block",
                native_session_id="opencode-session-artifact-no-evidence",
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("artifact"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create output.txt with the smoke marker.",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the artifact",
            depth=0,
            start_time=datetime.now(UTC),
            ac_spec=AcceptanceCriterionSpec(
                description="Create output.txt with the smoke marker.",
                expected_artifacts=("output.txt",),
                verify_command="test -f output.txt",
            ),
        )

        assert result.success is True
        assert result.error is None
        assert result.typed_evidence is None
        assert result.atomic_verifier_verdict is None
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == []
        assert evidence_event.data["typed_evidence_present"] is False
        assert evidence_event.data["enforcement_error"] is None

    @pytest.mark.asyncio
    async def test_verify_only_code_contract_still_requires_files_touched_evidence(
        self, tmp_path: Any
    ) -> None:
        """A command gate cannot replace code-profile filesystem evidence."""
        (tmp_path / "hello.py").write_text("VALUE = 1\n", encoding="utf-8")
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "[TASK_COMPLETE] command passed without a JSON evidence block",
                native_session_id="opencode-session-code-verify-only-no-evidence",
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
            ac_content="Implement hello.py.",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the code",
            depth=0,
            start_time=datetime.now(UTC),
            ac_spec=AcceptanceCriterionSpec(
                description="Implement hello.py.",
                verify_command="test -f hello.py",
            ),
        )

        assert result.success is False
        assert result.error is not None
        assert "Evidence is not valid JSON" in result.error
        evidence_event = next(
            event
            for event in appended_events
            if event.type == "execution.ac.typed_evidence.observed"
        )
        assert evidence_event.data["required_fields"] == ["files_touched"]
        assert evidence_event.data["typed_evidence_present"] is False
        assert evidence_event.data["enforcement_error"] is not None

    @pytest.mark.asyncio
    async def test_contract_verify_gate_is_single_shot_across_atomic_and_final_gate(
        self, tmp_path: Any
    ) -> None:
        """A cached atomic verify outcome prevents duplicate shell side effects."""
        (tmp_path / "output.txt").write_text("OZO_RUN_SMOKE_OK\n", encoding="utf-8")
        counter = tmp_path / "verify-count.txt"
        command = (
            "python3 -c \"from pathlib import Path; p=Path('verify-count.txt'); "
            "n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); "
            'raise SystemExit(0 if n == 0 else 7)"'
        )
        seed = Seed.from_dict(
            {
                "goal": "Create output.txt",
                "task_type": "artifact",
                "acceptance_criteria": [
                    {
                        "description": "Create output.txt with the smoke marker.",
                        "expected_artifacts": ["output.txt"],
                        "verify_command": command,
                    }
                ],
                "ontology_schema": {
                    "name": "SmokeArtifact",
                    "description": "Smoke artifact contract.",
                    "fields": [],
                },
                "metadata": {
                    "ambiguity_score": 0.0,
                    "generation_mode": "test",
                },
            }
        )
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                'Done.\n```json\n{"files_touched":["output.txt"]}\n```',
                native_session_id="opencode-session-single-shot-contract",
                cwd=str(tmp_path),
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("artifact"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Create output.txt with the smoke marker.",
            session_id="orch_123",
            tools=["Read"],
            tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
            system_prompt="system",
            seed_goal="Ship the artifact",
            depth=0,
            start_time=datetime.now(UTC),
            ac_spec=seed.acceptance_criteria[0],
        )
        assert result.success is True
        assert result.verify_gate_outcome is not None
        assert counter.read_text(encoding="utf-8") == "1"
        completed_event = next(
            event for event in appended_events if event.type == "execution.session.completed"
        )
        assert completed_event.data["verify_gate_outcome"] == {
            "passed": True,
            "reason": None,
            "output_tail": "",
            "missing_artifacts": [],
        }

        finalized = await executor._apply_verify_gate(
            seed=seed,
            ac_index=0,
            result=result,
            session_id="orch_123",
            execution_id="exec_123",
        )

        assert finalized.success is True
        assert counter.read_text(encoding="utf-8") == "1"

    @pytest.mark.asyncio
    async def test_completed_recovery_reuses_durable_verify_outcome_without_command_replay(
        self, tmp_path: Any
    ) -> None:
        """Crash recovery never repeats a verify command that already ran once."""
        (tmp_path / "output.txt").write_text("done\n", encoding="utf-8")
        counter = tmp_path / "verify-count.txt"
        counter.write_text("1", encoding="utf-8")
        command = (
            "python3 -c \"from pathlib import Path; p=Path('verify-count.txt'); "
            "n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); "
            'raise SystemExit(0 if n == 0 else 7)"'
        )
        seed = Seed.from_dict(
            {
                "goal": "Create output.txt",
                "task_type": "artifact",
                "acceptance_criteria": [
                    {
                        "description": "Create output.txt once.",
                        "expected_artifacts": ["output.txt"],
                        "verify_command": command,
                    }
                ],
                "ontology_schema": {
                    "name": "RecoveredArtifact",
                    "description": "Recovered artifact contract.",
                    "fields": [],
                },
                "metadata": {"ambiguity_score": 0.0, "generation_mode": "test"},
            }
        )

        class _Runtime:
            runtime_backend = "codex_cli"
            working_directory = str(tmp_path)
            permission_mode = "acceptEdits"

            def __init__(self) -> None:
                self.calls = 0

            async def execute_task(self, **_kwargs: object):
                self.calls += 1
                yield AgentMessage(type="result", content="must not run")

        runtime = _Runtime()
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            task_cwd=str(tmp_path),
        )
        ac_spec = seed.acceptance_criteria[0]
        assert isinstance(ac_spec, AcceptanceCriterionSpec)
        node_identity = ExecutionNodeIdentity.root(
            execution_context_id="session-completed-verify-recovery",
            ac_index=0,
        )
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=0,
            ac_content=ac_spec.description,
            session_id="session-completed-verify-recovery",
            seed_goal=seed.goal,
            workspace=str(tmp_path),
            node_identity=node_identity,
            ac_spec=ac_spec,
        )
        dispatch_id = "7" * 32
        appended_events.extend(
            [
                _compiled_capsule_event(runtime_identity, persisted_capsule),
                _dispatched_capsule_event(
                    runtime_identity,
                    persisted_capsule,
                    dispatch_id=dispatch_id,
                ),
                _dispatch_lifecycle_event(
                    runtime_identity,
                    "execution.session.completed",
                    dispatch_id=dispatch_id,
                    runtime_handle=None,
                    result_summary="already verified",
                    session_id="completed-provider-session",
                    extra_data={
                        "verify_gate_outcome": {
                            "passed": True,
                            "reason": None,
                            "output_tail": "",
                            "missing_artifacts": [],
                        }
                    },
                ),
            ]
        )

        recovered = await executor._execute_single_ac(
            ac_index=0,
            ac_content=ac_spec.description,
            session_id="session-completed-verify-recovery",
            tools=["Read", "Edit"],
            tool_catalog=None,
            system_prompt="system",
            seed_goal=seed.goal,
            node_identity=node_identity,
            ac_spec=ac_spec,
        )
        finalized = await executor._apply_verify_gate(
            seed=seed,
            ac_index=0,
            result=recovered,
            session_id="session-completed-verify-recovery",
            execution_id="session-completed-verify-recovery",
        )

        assert finalized.success is True
        assert finalized.verify_gate_outcome is not None
        assert finalized.verify_gate_outcome.passed is True
        assert runtime.calls == 0
        assert counter.read_text(encoding="utf-8") == "1"

    @pytest.mark.asyncio
    async def test_completed_recovery_without_verify_outcome_refuses_command_replay(
        self,
    ) -> None:
        """Legacy completion without a cached command verdict fails closed."""
        executor = ParallelACExecutor(
            adapter=SimpleNamespace(
                runtime_backend="codex_cli",
                working_directory="/tmp/project",
                permission_mode="acceptEdits",
            ),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
        )
        executor._execute_atomic_ac = AsyncMock(
            side_effect=CompletedACExecutionError(
                "already completed",
                result_summary="legacy completion",
                session_id="legacy-provider-session",
            )
        )

        with pytest.raises(
            AmbiguousACExecutionError,
            match="missing its non-idempotent verify-command outcome",
        ):
            await executor._execute_single_ac(
                ac_index=0,
                ac_content="Run the command once",
                session_id="session-missing-verify-outcome",
                tools=[],
                tool_catalog=None,
                system_prompt="system",
                seed_goal="Ship",
                ac_spec=AcceptanceCriterionSpec(
                    description="Run the command once",
                    verify_command="apply-once",
                ),
            )

    @pytest.mark.asyncio
    async def test_verify_gate_recovers_failed_artifact_result_when_contract_passes(
        self, tmp_path: Any
    ) -> None:
        """Artifact contracts can recover runtime false-negatives."""
        (tmp_path / "output.txt").write_text("OZO_RUN_SMOKE_OK\n", encoding="utf-8")
        seed = Seed.from_dict(
            {
                "goal": "Create output.txt",
                "task_type": "artifact",
                "acceptance_criteria": [
                    {
                        "description": "Create output.txt with the smoke marker.",
                        "expected_artifacts": ["output.txt"],
                        "verify_command": "test -f output.txt",
                    }
                ],
                "ontology_schema": {
                    "name": "SmokeArtifact",
                    "description": "Smoke artifact contract.",
                    "fields": [],
                },
                "metadata": {
                    "ambiguity_score": 0.0,
                    "generation_mode": "test",
                },
            }
        )
        event_store, appended_events = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=_FinalMessageRuntime(
                "memory pressure timeout after artifact write",
                native_session_id="codex-session-artifact-false-negative",
                cwd=str(tmp_path),
                success=False,
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            execution_profile=load_profile("artifact"),
            fat_harness_mode=True,
            task_cwd=str(tmp_path),
        )
        failed = ACExecutionResult(
            ac_index=0,
            ac_content="Create output.txt with the smoke marker.",
            success=False,
            error="memory pressure timeout after artifact write",
            final_message="memory pressure timeout after artifact write",
            outcome=ACExecutionOutcome.FAILED,
        )

        recovered = await executor._apply_verify_gate(
            seed=seed,
            ac_index=0,
            result=failed,
            session_id="orch_123",
            execution_id="exec_123",
        )

        assert recovered.success is True
        assert recovered.error is None
        assert recovered.outcome == ACExecutionOutcome.SUCCEEDED
        recovery_event = next(
            event for event in appended_events if event.type == "execution.verify.recovered"
        )
        assert recovery_event.data["prior_error"] == "memory pressure timeout after artifact write"
        assert recovery_event.data["expected_artifacts"] == ["output.txt"]

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

        assert result.disposition is DecompositionDisposition.UNKNOWN
        assert result.reasons == ("decomposition_timeout",)
        assert result.trustworthy is False
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
    async def test_execute_parallel_drains_deferred_durable_writes_before_returning(
        self,
    ) -> None:
        """Adversarial-review Bug #1 (part b): ``execute_parallel`` must give
        in-flight deferred durable writes a bounded final shot BEFORE it
        returns — the CLI's ``asyncio.run`` teardown cancels every pending
        task the moment the run coroutine completes, so a write scheduled
        near the end of the run would otherwise be silently cancelled with
        zero real attempts."""
        import asyncio

        seed = _make_seed("AC 1")
        dependency_graph = DependencyGraph(
            nodes=(ACNode(index=0, content="AC 1", depends_on=()),),
            execution_levels=((0,),),
        )
        executor = _make_executor()
        landed = asyncio.Event()

        async def slow_write() -> bool:
            # Slower than the rest of the (fully mocked) run, so ONLY the
            # drain step can be what awaited its completion.
            await asyncio.sleep(0.2)
            landed.set()
            return True

        async def batch_and_schedule(**kwargs: object) -> list[ACExecutionResult]:
            executor._schedule_deferred_durable_write(
                write=slow_write, on_persisted=None, log_key="test.deferred"
            )
            return [
                ACExecutionResult(
                    ac_index=0,
                    ac_content="AC 1",
                    success=True,
                    final_message="Implemented AC 1",
                )
            ]

        executor._execute_ac_batch = AsyncMock(side_effect=batch_and_schedule)

        result = await executor.execute_parallel(
            seed=seed,
            execution_plan=dependency_graph.to_execution_plan(),
            session_id="orch_drain",
            execution_id="exec_drain",
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
        )

        assert result.success_count == 1
        # The deferred write landed BEFORE execute_parallel returned.
        assert landed.is_set()

    @pytest.mark.asyncio
    async def test_execute_parallel_logs_dependency_edges(self) -> None:
        """The inferred dependency graph should be visible before cascaded skips."""
        seed = _make_seed("AC 0 foundation", "AC 1 dependent flow")
        dependency_graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=seed.acceptance_criteria[0], depends_on=()),
                ACNode(index=1, content=seed.acceptance_criteria[1], depends_on=(0,)),
            ),
            execution_levels=((0,), (1,)),
        )
        executor = _make_executor()
        executor._execute_ac_batch = AsyncMock(
            return_value=[
                ACExecutionResult(
                    ac_index=0,
                    ac_content=seed.acceptance_criteria[0],
                    success=False,
                    error="Foundation failed",
                    outcome=ACExecutionOutcome.FAILED,
                )
            ]
        )

        with patch("ouroboros.orchestrator.parallel_executor.log") as log_mock:
            await executor.execute_parallel(
                seed=seed,
                execution_plan=dependency_graph.to_execution_plan(),
                session_id="orch_dependency_log",
                execution_id="exec_dependency_log",
                tools=["Read"],
                tool_catalog=None,
                system_prompt="system",
            )

        log_mock.info.assert_any_call(
            "parallel_executor.dependency_graph",
            session_id="orch_dependency_log",
            execution_id="exec_dependency_log",
            total_acs=2,
            dependency_edges=[{"ac_index": 1, "depends_on": (0,)}],
        )

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
            # Isolate the stall→failure conversion: cross-harness redispatch is a
            # separate recovery path (default on) that would otherwise intercept
            # the abandoned stall and surface an alternate backend's own failure.
            cross_harness_redispatch=False,
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
    async def test_effectful_stall_fails_closed_without_redispatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Blocker #3: a stall AFTER a tool effect must not take the retry path.

        The provider emits a durable tool effect, then goes silent. The ordinary
        stall retry would bump ``retry_attempt`` and re-dispatch, whose recovery
        filters this attempt's effect events and bypasses the ambiguity guard —
        potentially duplicating a non-idempotent effect. The effectful stall must
        instead fail closed as an ``AmbiguousACExecutionError`` with the provider
        entered exactly once.
        """
        import anyio

        from ouroboros.orchestrator import leaf_dispatcher

        monkeypatch.setattr(leaf_dispatcher, "STALL_TIMEOUT_SECONDS", 0.15)

        class _EffectfulThenStallRuntime:
            runtime_backend = "codex_cli"
            working_directory = "/tmp/project"
            permission_mode = "acceptEdits"

            def __init__(self) -> None:
                self.calls = 0

            async def execute_task(self, **_kwargs: object):
                self.calls += 1
                yield AgentMessage(
                    type="tool_use",
                    content="edit a file",
                    tool_name="Edit",
                    data={"tool_input": {"file_path": "app.py"}},
                )
                await anyio.sleep_forever()

        event_store, appended = _make_replaying_event_store()
        runtime = _EffectfulThenStallRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            cross_harness_redispatch=False,
        )

        with pytest.raises(AmbiguousACExecutionError, match="stalled after emitting tool effects"):
            await executor._execute_atomic_ac(
                ac_index=0,
                ac_content="Do not duplicate the effect after an effectful stall",
                session_id="session-effectful-stall",
                tools=["Edit"],
                system_prompt="system",
                seed_goal="Ship",
                depth=0,
                start_time=datetime.now(UTC),
            )

        assert runtime.calls == 1
        # R3 blocker #2: the ambiguity is persisted (sealed) BEFORE the raise, so a
        # cold restart fails closed on the sealed head instead of re-entering the
        # provider using the dispatch's still-resumable session handle.
        assert any(e.type == "execution.ac.dispatch.sealed" for e in appended)

    @pytest.mark.asyncio
    async def test_effect_free_stall_stays_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Blocker #3: a stall with NO observed tool effect keeps the retry sentinel.

        The complement of the effectful-stall guard: when no effect was
        dispatched, a fresh redispatch cannot duplicate anything, so the existing
        retryable stall behavior is preserved (the sentinel error the batch/leaf
        retry loop keys on).
        """
        import anyio

        from ouroboros.orchestrator import leaf_dispatcher

        monkeypatch.setattr(leaf_dispatcher, "STALL_TIMEOUT_SECONDS", 0.15)

        class _SilentStallRuntime:
            runtime_backend = "codex_cli"
            working_directory = "/tmp/project"
            permission_mode = "acceptEdits"

            def __init__(self) -> None:
                self.calls = 0

            async def execute_task(self, **_kwargs: object):
                self.calls += 1
                await anyio.sleep_forever()
                yield  # pragma: no cover - never reached; keeps this an async generator

        event_store, _ = _make_replaying_event_store()
        runtime = _SilentStallRuntime()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
            cross_harness_redispatch=False,
        )

        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="A silent stall stays retryable",
            session_id="session-silent-stall",
            tools=["Edit"],
            system_prompt="system",
            seed_goal="Ship",
            depth=0,
            start_time=datetime.now(UTC),
        )

        assert result.success is False
        assert result.error == _STALL_SENTINEL
        assert runtime.calls == 1

    @pytest.mark.asyncio
    async def test_verify_gate_single_shot_consumes_durable_outcome_on_recovery(self) -> None:
        """R2 blocker #2: a recovered verify gate consumes its outcome, never reruns.

        The first gate persists an intent and outcome and runs the (non-idempotent)
        command once. A second run for the SAME attempt/key — the crash-recovery
        case — consumes the durable outcome and must not run the command again.
        """
        event_store, _ = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=SimpleNamespace(
                runtime_backend="codex_cli",
                working_directory="/tmp/project",
                permission_mode="acceptEdits",
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        run_count = 0

        async def _counting_gate(*, spec: Any, cwd: str) -> _VerifyGateOutcome:
            nonlocal run_count
            run_count += 1
            return _VerifyGateOutcome(passed=True, reason=None, output_tail="ok")

        executor._run_ac_verify_gate = _counting_gate  # type: ignore[method-assign]
        spec = AcceptanceCriterionSpec(description="AC", verify_command="./deploy.sh")

        kwargs: dict[str, Any] = {
            "spec": spec,
            "cwd": "/tmp/project",
            "aggregate_id": "exec-verify-oracle",
            "verify_key": "atomic:ac-0:attempt-0:0",
            "execution_id": "exec-verify-oracle",
            "session_id": "sess",
            "identity_metadata": {"ac_id": "ac-0"},
        }

        first = await executor._run_verify_gate_single_shot(**kwargs)
        assert first.passed is True
        assert run_count == 1

        # Crash-recovery re-entry: same key + store → consume the durable outcome.
        second = await executor._run_verify_gate_single_shot(**kwargs)
        assert second.passed is True
        assert run_count == 1  # command did NOT run again

    @pytest.mark.asyncio
    async def test_verify_gate_single_shot_fails_closed_on_intent_without_outcome(self) -> None:
        """R2 blocker #2: a crash after the command ran but before the outcome
        persisted leaves an intent with no outcome; recovery must fail closed
        rather than replay a possibly-executed non-idempotent command."""
        from ouroboros.events.base import BaseEvent

        event_store, _ = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=SimpleNamespace(
                runtime_backend="codex_cli",
                working_directory="/tmp/project",
                permission_mode="acceptEdits",
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        spec = AcceptanceCriterionSpec(description="AC", verify_command="./deploy.sh")
        command_digest = executor._verify_gate_command_digest(spec)
        # Simulate a durable intent whose outcome write never landed.
        await event_store.append(
            BaseEvent(
                type="execution.ac.verify.intent",
                aggregate_type="execution",
                aggregate_id="exec-verify-orphan",
                data={
                    "verify_key": "atomic:ac-0:attempt-0:0",
                    "verify_command_digest": command_digest,
                },
            )
        )

        run_count = 0

        async def _counting_gate(*, spec: Any, cwd: str) -> _VerifyGateOutcome:
            nonlocal run_count
            run_count += 1
            return _VerifyGateOutcome(passed=True, reason=None, output_tail="ok")

        executor._run_ac_verify_gate = _counting_gate  # type: ignore[method-assign]

        with pytest.raises(AmbiguousACExecutionError, match="never recorded"):
            await executor._run_verify_gate_single_shot(
                spec=spec,
                cwd="/tmp/project",
                aggregate_id="exec-verify-orphan",
                verify_key="atomic:ac-0:attempt-0:0",
                execution_id="exec-verify-orphan",
                session_id="sess",
                identity_metadata={"ac_id": "ac-0"},
            )
        assert run_count == 0  # the possibly-executed command was NOT run again

    @pytest.mark.asyncio
    async def test_apply_verify_gate_recovery_is_single_shot(self) -> None:
        """R3 blocker #3: the failed-runtime recovery gate uses the single-shot oracle.

        A failed atomic result carries no cached outcome, so `_apply_verify_gate`
        runs the verify command. Replaying the SAME failed result must consume the
        durable outcome instead of running the (side-effecting) command twice.
        """
        event_store, _ = _make_replaying_event_store()
        executor = ParallelACExecutor(
            adapter=SimpleNamespace(
                runtime_backend="codex_cli",
                working_directory="/tmp/project",
                permission_mode="acceptEdits",
            ),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        run_count = 0

        async def _counting_gate(*, spec: Any, cwd: str) -> _VerifyGateOutcome:
            nonlocal run_count
            run_count += 1
            return _VerifyGateOutcome(passed=True, reason=None, output_tail="ok")

        executor._run_ac_verify_gate = _counting_gate  # type: ignore[method-assign]
        seed = _make_seed(
            AcceptanceCriterionSpec(description="AC", verify_command="./deploy.sh"),
        )
        failed_result = ACExecutionResult(
            ac_index=0,
            ac_content="AC",
            success=False,
            error="runtime failed",
            retry_attempt=0,
        )

        first = await executor._apply_verify_gate(
            seed=seed,
            ac_index=0,
            result=failed_result,
            session_id="sess",
            execution_id="exec-apply-gate",
        )
        assert run_count == 1
        assert first.success is True  # contract passed → recovered

        # Replaying the same failed result must not run the command again.
        await executor._apply_verify_gate(
            seed=seed,
            ac_index=0,
            result=failed_result,
            session_id="sess",
            execution_id="exec-apply-gate",
        )
        assert run_count == 1

    @pytest.mark.asyncio
    async def test_alt_harness_defers_until_same_runtime_retry_budget_spent(self) -> None:
        """Cross-harness redispatch must not fire until same-runtime retries are spent.

        Regression for the ordering blocker: with ``ac_retry_attempts > 0`` the
        batch retry loop owns the same-runtime recovery budget. Each worker's
        alt-harness hook is gated on ``same_runtime_budget_exhausted``, which the
        batch layer sets ``True`` only on the AC's final attempt. So across the
        initial dispatch plus each configured retry, the flag stays ``False``
        until the last attempt — the alternate harness never pre-empts the
        configured same-runtime retries.
        """
        seed = _make_seed("AC 0 flow")
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
            ac_retry_attempts=2,
            cross_harness_redispatch=True,
        )

        exhausted_flags: list[bool] = []

        async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
            exhausted_flags.append(bool(kwargs["same_runtime_budget_exhausted"]))
            return [
                ACExecutionResult(
                    ac_index=idx,
                    ac_content=seed.acceptance_criteria[idx],
                    success=False,
                    error="non-stall failure",
                    outcome=ACExecutionOutcome.FAILED,
                )
                for idx in kwargs["batch_indices"]
            ]

        executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

        results = await executor._run_batch_with_verify_and_retry(
            seed=seed,
            batch_executable=[0],
            session_id="sess",
            execution_id="exec",
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
            level_contexts=[],
            ac_retry_attempts={0: 0},
            execution_counters=None,
        )

        # Initial dispatch + 2 configured retries = 3 batch calls. The
        # same-runtime budget is only 'exhausted' (so alt-harness may run) on
        # the final attempt.
        assert exhausted_flags == [False, False, True]
        assert isinstance(results[0], ACExecutionResult)
        assert results[0].success is False

    @pytest.mark.asyncio
    async def test_alt_harness_opens_on_retry_early_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Repeated-failure-class early-stop must still reach cross-harness recovery.

        Regression for the narrowed ordering blocker: with ``ac_retry_attempts=2``
        and the SAME alt-harness-eligible class (FABRICATION_SUSPECTED) on the
        initial attempt and retry 1, the retry loop early-stops before the counter
        cap. The same-runtime path has given up, so its budget is spent — the
        alt-harness hook must open on that early-stopped attempt instead of never
        firing, and the failed alternate must be surfaced as authoritative.
        """
        from ouroboros.orchestrator import cross_harness_redispatch as chr

        investment = InvestmentSpec(
            difficulty="high",
            stakes="high",
            provenance="declared",
            confidence="high",
        )
        ac_spec = AcceptanceCriterionSpec(
            description="AC 0 flow",
            investment=investment,
        )
        seed = _make_seed(ac_spec)
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=AsyncMock(),
            console=MagicMock(),
            enable_decomposition=False,
            ac_retry_attempts=2,
            cross_harness_redispatch=True,
        )
        executor._adapter.runtime_backend = "claude"
        monkeypatch.setattr(chr, "pick_alternative_runtime", lambda *_a, **_k: "codex")

        fab_verdict = VerifierVerdict(
            passed=False,
            reasons=("claimed a file that does not exist",),
            failure_class="FABRICATION_SUSPECTED",
        )

        async def fake_batch(**kwargs: Any) -> list[ACExecutionResult]:
            return [
                ACExecutionResult(
                    ac_index=idx,
                    ac_content="AC 0 flow",
                    success=False,
                    error="fabricated claim",
                    outcome=ACExecutionOutcome.FAILED,
                    atomic_verifier_verdict=fab_verdict,
                )
                for idx in kwargs["batch_indices"]
            ]

        executor._execute_ac_batch = fake_batch  # type: ignore[method-assign]

        alt_backends: list[str] = []
        alternate_rerun_kwargs: list[dict[str, Any]] = []

        async def fake_run_single(backend: str, **kwargs: Any) -> ACExecutionResult:
            alt_backends.append(backend)
            alternate_rerun_kwargs.append(kwargs["rerun_kwargs"])
            return ACExecutionResult(
                ac_index=0,
                ac_content="AC 0 flow",
                success=False,
                error="codex also failed verification",
                session_id="alt-sess",
            )

        executor._run_single_ac_on_backend = fake_run_single  # type: ignore[method-assign]

        results = await executor._run_batch_with_verify_and_retry(
            seed=seed,
            batch_executable=[0],
            session_id="sess",
            execution_id="exec",
            tools=["Read"],
            tool_catalog=None,
            system_prompt="system",
            level_contexts=[],
            ac_retry_attempts={0: 0},
            execution_counters=None,
        )

        # Early-stop fired after retry 1 (initial FAB + retry-1 FAB), before the
        # counter cap — yet the alternate harness was still consulted exactly once.
        assert alt_backends == ["codex"]
        assert alternate_rerun_kwargs[0]["ac_spec"] == seed.acceptance_criteria[0]
        assert alternate_rerun_kwargs[0]["ac_spec"].semantic_ac_key is not None
        assert alternate_rerun_kwargs[0]["investment_spec"] is investment
        assert alternate_rerun_kwargs[0]["decomposition_trustworthy"] is False
        # The failed alternate is surfaced as the authoritative result.
        assert isinstance(results[0], ACExecutionResult)
        assert results[0].success is False
        assert "alt-harness" in (results[0].error or "")
        assert "codex" in (results[0].error or "")

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
    async def test_completed_verify_outcome_rejects_oversized_durable_projection(self) -> None:
        """Verify command output cannot amplify the durable completion stream."""
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=MagicMock(),
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        runtime_identity = executor._resolve_ac_runtime_identity(
            0,
            execution_context_id="exec_oversized_verify_outcome",
            retry_attempt=0,
        )

        with pytest.raises(ValueError, match="verify outcome exceeds the size limit"):
            await executor._emit_ac_runtime_event(
                event_type="execution.session.completed",
                runtime_identity=runtime_identity,
                ac_content="Bound the durable verify outcome",
                runtime_handle=None,
                execution_id="exec_oversized_verify_outcome",
                session_id="server-oversized",
                result_summary="[TASK_COMPLETE]",
                success=True,
                verify_gate_outcome=_VerifyGateOutcome(
                    passed=True,
                    reason=None,
                    output_tail="x" * 65_536,
                ),
            )

        event_store.append.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_restarted_executor_loads_persisted_runtime_handle_for_same_attempt(self) -> None:
        """A fresh executor should rehydrate the same-attempt runtime handle from events."""

        class _StubPersistedResumeRuntime:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self._runtime_handle_backend = "opencode"
                self._cwd = "/tmp/project"
                self._permission_mode = "bypassPermissions"

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

        runtime = _StubPersistedResumeRuntime()
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        ac_content = "Resume the interrupted AC implementation session"
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=1,
            ac_content=ac_content,
            session_id="orch_123",
            seed_goal="Ship the feature",
        )
        persisted_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-9",
            cwd="/tmp/project",
            approval_mode="bypassPermissions",
            metadata={
                **runtime_identity.to_metadata(),
                "server_session_id": "server-99",
                "ac_capsule_version": persisted_capsule.version,
                "ac_capsule_fingerprint": persisted_capsule.fingerprint,
                "ac_session_origin": "fresh",
            },
        )
        event_store.replay = AsyncMock(
            return_value=[
                _compiled_capsule_event(runtime_identity, persisted_capsule),
                _dispatched_capsule_event(
                    runtime_identity,
                    persisted_capsule,
                    dispatch_id="e" * 32,
                ),
                _dispatch_lifecycle_event(
                    runtime_identity,
                    "execution.session.started",
                    dispatch_id="e" * 32,
                    runtime_handle=persisted_handle,
                ),
            ]
        )
        event_store.append = AsyncMock()

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content=ac_content,
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
        assert resume_handle.approval_mode == "bypassPermissions"
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
    async def test_restarted_executor_rejects_conflicting_runtime_sessions_for_same_dispatch(
        self,
    ) -> None:
        """Replay order cannot choose between conflicting active provider sessions."""

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

        runtime = _StubResumedHandleRuntime()
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        ac_content = "Resume the latest persisted implementation session"
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=1,
            ac_content=ac_content,
            session_id="orch_123",
            seed_goal="Ship the feature",
        )
        started_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-started",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                **runtime_identity.to_metadata(),
                "server_session_id": "server-started",
                "ac_capsule_version": persisted_capsule.version,
                "ac_capsule_fingerprint": persisted_capsule.fingerprint,
                "ac_dispatch_id": "f" * 32,
                "ac_session_origin": "fresh",
            },
        )
        resumed_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-resumed",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                **runtime_identity.to_metadata(),
                "server_session_id": "server-resumed",
                "ac_capsule_version": persisted_capsule.version,
                "ac_capsule_fingerprint": persisted_capsule.fingerprint,
                "ac_dispatch_id": "f" * 32,
                "ac_session_origin": "restored_same_attempt",
            },
        )
        event_store.replay = AsyncMock(
            return_value=[
                _compiled_capsule_event(runtime_identity, persisted_capsule),
                _dispatched_capsule_event(
                    runtime_identity,
                    persisted_capsule,
                    dispatch_id="f" * 32,
                ),
                _dispatch_lifecycle_event(
                    runtime_identity,
                    "execution.session.started",
                    dispatch_id="f" * 32,
                    runtime_handle=started_handle,
                ),
                _dispatch_lifecycle_event(
                    runtime_identity,
                    "execution.session.resumed",
                    dispatch_id="f" * 32,
                    runtime_handle=resumed_handle,
                ),
            ]
        )
        event_store.append = AsyncMock()

        with pytest.raises(AmbiguousACExecutionError, match="conflicting reusable"):
            await executor._load_persisted_ac_runtime_handle(
                1,
                execution_context_id="orch_123",
                retry_attempt=0,
                expected_capsule_fingerprint=persisted_capsule.fingerprint,
                expected_capsule_workspace=persisted_capsule.workspace,
            )

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
        outcome_events = [
            event for event in appended_events if event.type == "execution.ac.outcome_finalized"
        ]
        coordinator_events = [
            event for event in appended_events if event.type.startswith("execution.coordinator.")
        ]

        assert result.success_count == 2
        assert len(result.stages) == 1
        assert result.stages[0].coordinator_review is not None
        assert result.stages[0].coordinator_review.review_summary == "Resolved shared.py conflict"
        assert result.stages[0].coordinator_review.artifact_scope == "level"
        assert result.stages[0].coordinator_review.artifact_owner == "coordinator"
        assert result.stages[0].coordinator_review.artifact_owner_id == "level_1_coordinator"

        assert len(outcome_events) == 2
        assert all(event.data["success"] is True for event in outcome_events)
        assert [event.type for event in coordinator_events] == [
            "execution.coordinator.started",
            "execution.coordinator.tool.started",
            "execution.coordinator.thinking",
            "execution.coordinator.completed",
        ]
        for event in coordinator_events:
            assert event.aggregate_id == "exec_coord_scope:l0:coord"
            assert event.data["scope"] == "level"
            assert event.data["session_role"] == "coordinator"
            assert event.data["level_number"] == 1
            assert event.data["stage_index"] == 0
            assert "ac_id" not in event.data
            assert "ac_index" not in event.data
            assert "acceptance_criterion" not in event.data

        assert coordinator_events[-1].data["artifact_type"] == "coordinator_review"
        assert coordinator_events[-1].data["artifact_scope"] == "level"
        assert coordinator_events[-1].data["artifact_owner"] == "coordinator"
        assert coordinator_events[-1].data["artifact_owner_id"] == "level_1_coordinator"
        assert (
            coordinator_events[-1].data["artifact"]
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

        runtime = _StubResumeAfterInvalidRuntime()
        event_store = AsyncMock()
        executor = ParallelACExecutor(
            adapter=runtime,
            event_store=event_store,
            console=MagicMock(),
            enable_decomposition=False,
        )
        ac_content = "Resume after skipping invalid persisted event"
        runtime_identity, persisted_capsule = _compile_test_capsule(
            executor=executor,
            ac_index=1,
            ac_content=ac_content,
            session_id="orch_123",
            seed_goal="Ship the feature",
        )
        valid_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            native_session_id="opencode-session-valid",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                **runtime_identity.to_metadata(),
                "server_session_id": "server-valid",
                "ac_capsule_version": persisted_capsule.version,
                "ac_capsule_fingerprint": persisted_capsule.fingerprint,
                "ac_session_origin": "fresh",
            },
        )
        event_store.replay = AsyncMock(
            return_value=[
                _compiled_capsule_event(runtime_identity, persisted_capsule),
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

        result = await executor._execute_atomic_ac(
            ac_index=1,
            ac_content=ac_content,
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

    assert result.disposition is DecompositionDisposition.SPLIT
    assert [child.description for child in result.children] == [
        "Sub-AC 1: inspect",
        "Sub-AC 2: test",
    ]
    assert result.trustworthy is False


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

    assert result.disposition is DecompositionDisposition.SPLIT
    assert [child.description for child in result.children] == [
        "Sub-AC 1: inspect the implementation",
        "Sub-AC 2: write a focused regression test",
        "Sub-AC 3: document the result",
    ]
    assert result.trustworthy is False


@pytest.mark.asyncio
async def test_try_decompose_ac_announces_same_empty_tools_allowlist_it_dispatches() -> None:
    class _CapturingRuntime:
        runtime_backend = "codex_cli"
        capabilities = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            tool_restriction_support=ParamSupport.TRANSLATED,
        )

        def __init__(self) -> None:
            self.dispatched_tools: list[str] | None = None

        async def execute_task(
            self,
            prompt: str,
            tools: list[str] | None = None,
            system_prompt: str | None = None,
            resume_handle: RuntimeHandle | None = None,
            resume_session_id: str | None = None,
        ):
            del prompt, system_prompt, resume_handle, resume_session_id
            self.dispatched_tools = tools
            yield AgentMessage(type="assistant", content='["Sub-AC 1: inspect", "Sub-AC 2: test"]')

    runtime = _CapturingRuntime()
    console = MagicMock()
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=AsyncMock(),
        console=console,
        enable_decomposition=True,
    )

    result = await executor._try_decompose_ac(
        ac_content="Investigate and test sub-AC behavior.",
        ac_index=0,
        seed_goal="Verify decomposition tool handling",
        tools=["Read"],
        system_prompt="system",
    )

    assert result.disposition is DecompositionDisposition.SPLIT
    assert [child.description for child in result.children] == [
        "Sub-AC 1: inspect",
        "Sub-AC 2: test",
    ]
    assert result.trustworthy is False
    assert runtime.dispatched_tools == []
    console.print.assert_called_once()
    notice = console.print.call_args.args[0]
    assert "tools" in notice
    assert "ignored" in notice


class _ParamCapsStubAdapter:
    """Minimal adapter exposing the attributes the param-degradation hook reads."""

    def __init__(self, capabilities: RuntimeCapabilities) -> None:
        self.capabilities = capabilities
        self.runtime_backend = "hermes_cli"
        self.permission_mode = "acceptEdits"
        self.working_directory = "/workspace"


def _make_param_executor(capabilities: RuntimeCapabilities) -> ParallelACExecutor:
    return ParallelACExecutor(
        adapter=_ParamCapsStubAdapter(capabilities),
        event_store=AsyncMock(),
        console=MagicMock(),
        enable_decomposition=False,
    )


class TestParamDegradationNotice:
    """The executor surfaces non-native param handling once per run."""

    def test_translated_system_prompt_surfaces_one_notice(self) -> None:
        caps = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            system_prompt_support=ParamSupport.TRANSLATED,
        )
        executor = _make_param_executor(caps)

        # Two dispatches with the same degraded param → surfaced once (deduped).
        executor._announce_param_degradations(system_prompt="be terse", tools=None)
        executor._announce_param_degradations(system_prompt="be terse", tools=None)

        assert executor._console.print.call_count == 1
        notice = executor._console.print.call_args.args[0]
        assert "system_prompt" in notice
        assert "hermes_cli" in notice

    def test_all_native_adapter_is_silent(self) -> None:
        executor = _make_param_executor(FULL_CAPABILITIES)

        executor._announce_param_degradations(system_prompt="be terse", tools=["Read"])

        executor._console.print.assert_not_called()

    def test_absent_system_prompt_produces_no_notice(self) -> None:
        caps = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            system_prompt_support=ParamSupport.TRANSLATED,
        )
        executor = _make_param_executor(caps)

        executor._announce_param_degradations(system_prompt=None, tools=None)

        executor._console.print.assert_not_called()

    def test_empty_tools_allowlist_surfaces_one_notice(self) -> None:
        caps = RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            tool_restriction_support=ParamSupport.TRANSLATED,
        )
        executor = _make_param_executor(caps)

        executor._announce_param_degradations(system_prompt=None, tools=[])
        executor._announce_param_degradations(system_prompt=None, tools=[])

        assert executor._console.print.call_count == 1
        notice = executor._console.print.call_args.args[0]
        assert "tools" in notice
        assert "ignored" in notice
