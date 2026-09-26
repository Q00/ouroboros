"""Executor hooks for the check package: inert when unset, advisory legacy when set."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import re
from typing import Any
from unittest.mock import MagicMock

import pytest

from ouroboros.boundary.binding import entry_points_request
from ouroboros.mcp.types import MCPToolDefinition
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.ac_classification import _scoped_evidence_record_for_ac
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.parallel_executor_models import ACExecutionResult
from ouroboros.orchestrator.profile_loader import load_profile
from ouroboros.orchestrator.retry_hints import build_ac_retry_prompt, failure_class_for_result
from tests.unit.orchestrator.test_parallel_executor import (
    _FinalMessageRuntime,
    _make_replaying_event_store,
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

    async def gate(*, seed: Any, ac_index: int, result: ACExecutionResult) -> ACExecutionResult:
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
