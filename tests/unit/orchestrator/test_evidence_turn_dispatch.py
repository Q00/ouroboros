"""The evidence turn runs on the worker's own session through the executor's boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import shlex
from typing import Any
from unittest.mock import MagicMock

import pytest

from ouroboros.mcp.types import MCPToolDefinition
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.evidence import evidence_turn
from ouroboros.orchestrator.evidence.evidence_turn import EvidenceTurnSettings
from ouroboros.orchestrator.parallel_executor import ParallelACExecutor
from ouroboros.orchestrator.profile_loader import load_profile
from ouroboros.orchestrator.verifier import EVIDENCE_PATH_CITATIONS
from tests.unit.orchestrator.test_parallel_executor import (
    _FinalMessageRuntime,
    _make_replaying_event_store,
)

EVIDENCE = (
    "```json\n"
    '{"files_touched": ["mod.py"], "commands_run": ["see the evidence turn"], '
    '"tests_passed": ["see the evidence turn"]}\n'
    "```"
)


class _CitingRuntime(_FinalMessageRuntime):
    """Answers the evidence turn with call numbers."""

    def __init__(self, reply: str, **kwargs: Any) -> None:
        super().__init__(EVIDENCE, **kwargs)
        self.reply = reply

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ):
        if prompt.startswith("[EVIDENCE TURN"):
            self.call_count += 1
            self.evidence_turn_prompts.append(prompt)
            result = self._final_result(resume_handle)
            yield AgentMessage(
                type="result",
                content=self.reply,
                data=result.data,
                resume_handle=result.resume_handle,
            )
            return
        async for message in super().execute_task(
            prompt,
            tools=tools,
            system_prompt=system_prompt,
            resume_handle=resume_handle,
            resume_session_id=resume_session_id,
        ):
            yield message


def _support(workspace: Path, *, exit_code: int = 0) -> tuple[AgentMessage, ...]:
    command = "/bin/bash -lc " + shlex.quote("pytest -q test_mod.py")
    return (
        AgentMessage(
            type="assistant",
            content="Calling tool: Edit",
            tool_name="Edit",
            data={"tool_input": {"file_path": str(workspace / "mod.py")}},
        ),
        AgentMessage(
            type="assistant",
            content=f"Calling tool: Bash: {command}",
            tool_name="Bash",
            data={"tool_input": {"command": command}, "tool_call_id": "item_2"},
        ),
        AgentMessage(
            type="tool_result",
            content="",
            tool_name="Bash",
            data={
                "subtype": "tool_result",
                "tool_call_id": "item_2",
                "exit_code": exit_code,
                "is_error": exit_code != 0,
                "tool_result": {
                    "is_error": exit_code != 0,
                    "meta": {"tool_call_id": "item_2", "exit_status": exit_code},
                },
            },
        ),
    )


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        evidence_turn.EvidenceTurnSettings,
        "from_config",
        staticmethod(lambda: EvidenceTurnSettings(relevance_veto=False)),
    )


async def _run(runtime: _FinalMessageRuntime, workspace: Path) -> tuple[Any, list[Any]]:
    (workspace / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / "test_mod.py").write_text("def test_value():\n    pass\n", encoding="utf-8")
    event_store, events = _make_replaying_event_store()
    executor = ParallelACExecutor(
        adapter=runtime,
        event_store=event_store,
        console=MagicMock(),
        enable_decomposition=False,
        execution_profile=load_profile("code"),
        fat_harness_mode=True,
        task_cwd=str(workspace),
        run_verify_commands=False,
    )
    result = await executor._execute_atomic_ac(
        ac_index=0,
        ac_content="mod.VALUE is 1",
        session_id="orch_evidence_turn",
        tools=["Read", "Edit", "Bash"],
        tool_catalog=(MCPToolDefinition(name="Read", description="Read a file."),),
        system_prompt="system",
        seed_goal="Ship the feature",
        depth=0,
        start_time=datetime.now(UTC),
    )
    return result, events


@pytest.mark.asyncio
async def test_cited_calls_decide_and_the_turn_is_a_durable_dispatch(tmp_path: Path) -> None:
    runtime = _CitingRuntime(
        '{"tests_passed": [1], "commands_run": [1]}',
        native_session_id="codex-evidence-turn",
        support_messages=_support(tmp_path),
        cwd=str(tmp_path),
    )
    result, events = await _run(runtime, tmp_path)

    assert result.success is True, result.error
    verdict = result.atomic_verifier_verdict
    assert verdict is not None and verdict.passed
    assert verdict.decided_by == EVIDENCE_PATH_CITATIONS
    assert len(runtime.evidence_turn_prompts) == 1
    assert "[1] exit 0 :: /bin/bash -lc 'pytest -q test_mod.py'" in runtime.evidence_turn_prompts[0]
    # The transcript and final message stay the worker's own turn.
    assert result.final_message == EVIDENCE
    assert all(message.content != runtime.reply for message in result.messages)
    kinds = [
        event.data["dispatch_kind"]
        for event in events
        if event.type == "execution.ac.attempt.dispatched"
    ]
    assert kinds == ["primary", "evidence_turn"]
    typed = next(e for e in events if e.type == "execution.ac.typed_evidence.observed")
    assert typed.data["verifier_decided_by"] == EVIDENCE_PATH_CITATIONS
    assert typed.data["evidence_turn"]["tests_passed"] == [1]


@pytest.mark.asyncio
async def test_an_unknown_number_is_fabrication(tmp_path: Path) -> None:
    runtime = _CitingRuntime(
        '{"tests_passed": [5], "commands_run": [1]}',
        native_session_id="codex-evidence-turn",
        support_messages=_support(tmp_path),
        cwd=str(tmp_path),
    )
    result, _events = await _run(runtime, tmp_path)
    assert result.success is False
    verdict = result.atomic_verifier_verdict
    assert verdict is not None and verdict.failure_class == "FABRICATION_SUSPECTED"


@pytest.mark.asyncio
async def test_an_unusable_reply_keeps_the_command_string_path(tmp_path: Path) -> None:
    runtime = _CitingRuntime(
        "I ran the tests.",
        native_session_id="codex-evidence-turn",
        support_messages=_support(tmp_path),
        cwd=str(tmp_path),
    )
    result, _events = await _run(runtime, tmp_path)
    verdict = result.atomic_verifier_verdict
    assert verdict is not None and verdict.decided_by == ""
    # The worker's strings are not in the transcript: the string path rejects them.
    assert verdict.failure_class == "FABRICATION_SUSPECTED"


@pytest.mark.asyncio
async def test_no_evidence_turn_after_a_failed_turn(tmp_path: Path) -> None:
    runtime = _CitingRuntime(
        '{"tests_passed": [1], "commands_run": [1]}',
        native_session_id="codex-evidence-turn",
        support_messages=_support(tmp_path),
        cwd=str(tmp_path),
        success=False,
    )
    result, _events = await _run(runtime, tmp_path)
    assert result.success is False
    assert runtime.evidence_turn_prompts == []
