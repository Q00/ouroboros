"""OMP tool lifecycles must reach the journal and verifier (issue #2535)."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.harness.deliver_gate import _event_has_explicit_tool_success
from ouroboros.orchestrator.adapter import AgentMessage
from ouroboros.orchestrator.evidence.verification import (
    _verify_atomic_evidence_against_runtime_messages,
)
from ouroboros.orchestrator.evidence_schema import EvidenceRecord
from ouroboros.orchestrator.omp_runtime import OmpRuntime
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.profile_loader import EvidenceSchema, load_profile
from ouroboros.orchestrator.runtime_message_projection import project_runtime_message
from tests.unit.orchestrator.test_omp_runtime import _FakeProcess


def _start(call_id: str, command: str) -> dict[str, Any]:
    return {
        "type": "tool_execution_start",
        "toolCallId": call_id,
        "toolName": "bash",
        "args": {"command": command},
    }


def _end(call_id: str, text: str = "", **fields: Any) -> dict[str, Any]:
    # OMP 18.7.0 omits exitCode for a successful synchronous Bash call.
    return {
        "type": "tool_execution_end",
        "toolCallId": call_id,
        "toolName": "bash",
        "result": {"content": [{"type": "text", "text": text}], "details": {}},
        "isError": False,
        **fields,
    }


def _process(events: list[dict[str, Any]], final: str = "done") -> _FakeProcess:
    return _FakeProcess(
        stdout_lines=[
            json.dumps({"type": "session", "id": "omp-evidence"}),
            *(json.dumps(event) for event in events),
            json.dumps(
                {
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "content": final}],
                }
            ),
        ],
        stderr_lines=[],
    )


async def _messages(events: list[dict[str, Any]], cwd: Path) -> tuple[AgentMessage, ...]:
    with patch(
        "ouroboros.orchestrator.omp_runtime.asyncio.create_subprocess_exec",
        return_value=_process(events),
    ):
        return tuple([message async for message in OmpRuntime(cwd=cwd).execute_task("verify")])


def _test_verdict(messages: tuple[AgentMessage, ...]):
    return _verify_atomic_evidence_against_runtime_messages(
        messages=messages,
        typed_evidence=EvidenceRecord(data={"tests_passed": ["pytest -q"]}),
        ac_content="Verify the implementation with pytest",
        execution_profile=load_profile("code").model_copy(
            update={"evidence_schema": EvidenceSchema(required=("tests_passed",))}
        ),
        task_cwd=None,
        adapter_working_directory=None,
    )


@pytest.mark.parametrize("claim", ["git diff --check", "git status --porcelain"])
async def test_omp_command_evidence_reaches_journal_and_ac_verdict(
    tmp_path: Path, claim: str
) -> None:
    runtime = OmpRuntime(cwd=tmp_path)
    store = AsyncMock()
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=store,
        console=MagicMock(),
        enable_decomposition=False,
        fat_harness_mode=True,
        task_cwd=str(tmp_path),
        execution_profile=load_profile("code").model_copy(
            update={"evidence_schema": EvidenceSchema(required=("commands_run",))}
        ),
    )
    with patch(
        "ouroboros.orchestrator.omp_runtime.asyncio.create_subprocess_exec",
        return_value=_process(
            [_start("call-1", "git diff --check"), _end("call-1")],
            json.dumps({"commands_run": [claim]}),
        ),
    ):
        result = await executor._execute_atomic_ac(
            ac_index=0,
            ac_content="Run git diff --check and report the command executed",
            session_id="session-omp-evidence",
            tools=["Bash"],
            system_prompt="Verify the workspace",
            seed_goal="Verify whitespace",
            depth=0,
            start_time=datetime.now(UTC),
        )

    events = [call.args[0] for call in store.append.await_args_list]
    starts = [event for event in events if event.type == "execution.tool.started"]
    ends = [event for event in events if event.type == "execution.tool.completed"]
    assert len(starts) == len(ends) == 1
    assert starts[0].data["tool_call_id"] == ends[0].data["tool_call_id"] == "call-1"
    assert starts[0].data["tool_input"]["command"] == "git diff --check"
    assert ends[0].data["tool_result"]["meta"]["exit_status"] == 0
    assert _event_has_explicit_tool_success(ends[0])
    observed = next(
        event for event in events if event.type == "execution.ac.typed_evidence.observed"
    )
    if claim == "git diff --check":
        assert result.success, result.error
        assert observed.data["verifier_passed"] is True
    else:
        assert not result.success
        assert observed.data["verifier_failure_class"] == "FABRICATION_SUSPECTED"


async def test_interleaved_omp_calls_keep_inputs_outputs_and_test_success(tmp_path: Path) -> None:
    messages = await _messages(
        [
            _start("tests", "pytest -q"),
            _start("diff", "git diff --check"),
            {"type": "tool_execution_update", "toolCallId": "tests", "toolName": "bash"},
            _end("diff"),
            _end("tests", "1 passed in 0.01s\n"),
        ],
        tmp_path,
    )
    projected = [project_runtime_message(message) for message in messages]
    results = [item for item in projected if item.is_tool_result]
    assert [item.runtime_metadata["tool_call_id"] for item in results] == ["diff", "tests"]
    assert [item.tool_input["command"] for item in results] == ["git diff --check", "pytest -q"]
    assert all(item.tool_name == "Bash" for item in results)
    assert _test_verdict(messages).passed


@pytest.mark.parametrize(
    "case",
    [
        "error",
        "nested-error",
        "nonzero",
        "string-exit",
        "boolean-exit",
        "missing-error",
        "string-error",
        "nested-string-error",
        "missing-result",
        "malformed-details",
        "background",
        "no-end",
        "wrong-id",
        "wrong-tool",
    ],
)
async def test_incomplete_or_failed_omp_results_never_prove_test_success(
    tmp_path: Path, case: str
) -> None:
    completion = _end("tests", "1 passed in 0.01s\n")
    payload = completion["result"]
    if case == "error":
        completion["isError"] = True
    elif case == "nested-error":
        payload["isError"] = True
    elif case in {"nonzero", "string-exit", "boolean-exit"}:
        payload["details"]["exitCode"] = {"nonzero": 7, "string-exit": "0", "boolean-exit": False}[
            case
        ]
    elif case == "missing-error":
        del completion["isError"]
    elif case == "string-error":
        completion["isError"] = "false"
    elif case == "nested-string-error":
        payload["isError"] = "false"
    elif case == "missing-result":
        del completion["result"]
    elif case == "malformed-details":
        payload["details"] = []
    elif case == "background":
        payload["details"]["async"] = {"state": "running", "jobId": "bg_1", "type": "bash"}
    elif case == "wrong-id":
        completion["toolCallId"] = "other"
    elif case == "wrong-tool":
        completion["toolName"] = "read"
    events = [_start("tests", "pytest -q")]
    if case != "no-end":
        events.append(completion)
    messages = await _messages(events, tmp_path)
    verdict = _test_verdict(messages)
    assert not verdict.passed, case
    if case == "nonzero":
        result = next(message for message in messages if message.type == "tool_result")
        assert result.data["is_error"] is True
        assert result.data["exit_code"] == 7
        assert project_runtime_message(result).tool_result["meta"]["exit_status"] == 7


@pytest.mark.parametrize("name,normalized", [("read", "Read"), ("mcp__custom", "mcp__custom")])
async def test_non_bash_results_preserve_tool_identity_without_inventing_exit_status(
    tmp_path: Path, name: str, normalized: str
) -> None:
    start, end = _start("call", "ignored"), _end("call", "tool output")
    start.update(toolName=name, args={"path": "README.md"})
    end["toolName"] = name
    messages = await _messages([start, end], tmp_path)
    assert [message.tool_name for message in messages[:2]] == [normalized, normalized]
    assert messages[1].data["tool_input"] == {"path": "README.md"}
    assert "exit_code" not in messages[1].data
    assert messages[1].content == "tool output"


async def test_pending_tool_inputs_do_not_leak_between_runtime_tasks(tmp_path: Path) -> None:
    runtime = OmpRuntime(cwd=tmp_path)
    with patch(
        "ouroboros.orchestrator.omp_runtime.asyncio.create_subprocess_exec",
        side_effect=[_process([_start("reused", "pytest -q")]), _process([_end("reused")])],
    ):
        first = [message async for message in runtime.execute_task("first")]
        second = [message async for message in runtime.execute_task("second")]
    assert first[0].data["tool_input"]["command"] == "pytest -q"
    assert "tool_input" not in second[0].data
