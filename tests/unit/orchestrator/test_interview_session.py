"""The interview control-turn contract must hold at every runtime entry point."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.types import Result
from ouroboros.interview_calibration import infer_interview_calibration
from ouroboros.mcp.types import MCPToolResult
from ouroboros.orchestrator.adapter import RuntimeHandle
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.command_dispatcher import CodexCommandDispatcher
from ouroboros.orchestrator.copilot_cli_runtime import CopilotCliRuntime
from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime
from ouroboros.orchestrator.gjc_runtime import GjcRuntime
from ouroboros.orchestrator.hermes_runtime import HermesCliRuntime
from ouroboros.orchestrator.interview_session import (
    INTERVIEW_CALIBRATION_METADATA_KEY,
    INTERVIEW_SESSION_METADATA_KEY,
)
from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter
from ouroboros.orchestrator.omp_runtime import OmpRuntime
from ouroboros.orchestrator.opencode_runtime import OpenCodeRuntime
from ouroboros.orchestrator.pi_runtime import PiRuntime

RUNTIMES = [
    CodexCliRuntime,
    CopilotCliRuntime,
    GeminiCLIRuntime,
    OpenCodeRuntime,
    HermesCliRuntime,
    PiRuntime,
    GjcRuntime,
    KiroAgentAdapter,
    OmpRuntime,
]
SKILLS_DIR = Path(__file__).resolve().parents[3] / "skills"
EVIDENCE = "I do not know idempotency; I built REST APIs"
CALIBRATION = infer_interview_calibration(EVIDENCE).model_dump(mode="json")
RECALIBRATION = infer_interview_calibration("I cannot explain event sourcing").model_dump(
    mode="json"
)


@pytest.fixture(params=RUNTIMES, ids=lambda runtime: runtime.__name__)
def runtime(request, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "ouroboros.orchestrator.pi_runtime._probe_pi_native_param_flags",
        lambda _: (False, False, False),
    )
    # A broken intercept must fail the test instead of invoking a real agent.
    monkeypatch.setattr(
        "asyncio.create_subprocess_exec",
        AsyncMock(side_effect=AssertionError("Interview control turns must stay in MCP")),
    )
    return request.param(cli_path="test-runtime", cwd=tmp_path, skills_dir=SKILLS_DIR)


def install_handler(runtime, results, shared_dispatcher):
    handler = SimpleNamespace(definition={"name": "ouroboros_interview"})
    handler.handle = AsyncMock(
        side_effect=[Result.ok(MCPToolResult(meta=meta)) for meta in results]
    )
    if shared_dispatcher:
        dispatcher = CodexCommandDispatcher(
            cwd=runtime.working_directory, runtime_backend=runtime.runtime_backend
        )
        server = MagicMock()

        async def call_tool(_name, arguments):
            return await handler.handle(arguments)

        server.call_tool = AsyncMock(side_effect=call_tool)
        dispatcher._server = server
        runtime._skill_dispatcher = dispatcher.dispatch
        if hasattr(runtime, "_interceptor"):
            runtime._interceptor._skill_dispatcher = dispatcher.dispatch
    else:
        interceptor = getattr(runtime, "_interceptor", runtime)
        interceptor._builtin_mcp_handlers = {"ouroboros_interview": handler}
    return handler.handle


async def turn(runtime, prompt, handle):
    messages = [message async for message in runtime.execute_task(prompt, resume_handle=handle)]
    assert messages
    assert not any(message.data.get("subtype") == "error" for message in messages)
    assert messages[-1].resume_handle is not None
    return messages[-1].resume_handle


@pytest.mark.parametrize("shared_dispatcher", [False, True], ids=["direct", "dispatcher"])
@pytest.mark.parametrize("native_session", [False, True], ids=["new", "existing"])
async def test_interview_calibrate_answer_resume_and_recalibrate(
    runtime, shared_dispatcher, native_session
):
    session = {"session_id": "interview-123"}
    call = install_handler(
        runtime,
        [
            session,
            {**session, "interview_calibration": CALIBRATION},
            session,
            session,
            {**session, "interview_calibration": RECALIBRATION},
            session,
        ],
        shared_dispatcher,
    )
    handle = (
        RuntimeHandle(
            backend=runtime.runtime_backend,
            native_session_id="native-thread",
            cwd=runtime.working_directory,
            approval_mode="acceptEdits",
            metadata={"unrelated": "preserved"},
        )
        if native_session
        else None
    )
    handle = await turn(runtime, "ooo interview Design payment failure handling", handle)
    before = handle
    handle = await turn(runtime, f"ooo idk {EVIDENCE}", handle)
    assert "answer" not in call.call_args_list[1].args[0]
    assert handle.metadata[INTERVIEW_CALIBRATION_METADATA_KEY] == CALIBRATION
    assert INTERVIEW_CALIBRATION_METADATA_KEY not in before.metadata
    handle = await turn(runtime, "ooo interview Retry once", handle)
    assert call.call_args_list[2].args[0] == {
        **session,
        "answer": "Retry once",
        "interview_calibration": CALIBRATION,
        "cwd": runtime.working_directory,
    }
    handle = await turn(runtime, "ooo interview", handle)
    assert call.call_args_list[3].args[0] == {
        **session,
        "interview_calibration": CALIBRATION,
        "cwd": runtime.working_directory,
    }
    handle = await turn(runtime, "ooo idk I cannot explain event sourcing", handle)
    assert "answer" not in call.call_args_list[4].args[0]
    handle = await turn(runtime, "ooo interview Keep the original payment record", handle)
    assert call.call_args_list[5].args[0]["interview_calibration"] == RECALIBRATION
    assert call.call_args_list[0].args[0]["initial_context"] == "Design payment failure handling"
    assert call.call_args_list[1].args[0] == {**session, "calibration_input": EVIDENCE}
    assert handle.metadata[INTERVIEW_SESSION_METADATA_KEY] == "interview-123"
    assert INTERVIEW_CALIBRATION_METADATA_KEY not in handle.to_persisted_dict()["metadata"]
    if native_session:
        assert handle.native_session_id == "native-thread"
        assert handle.approval_mode == "acceptEdits"
        assert handle.metadata["unrelated"] == "preserved"


@pytest.mark.parametrize("shared_dispatcher", [False, True], ids=["direct", "dispatcher"])
async def test_calibration_before_start_survives_first_question(runtime, shared_dispatcher):
    session = {"session_id": "interview-123"}
    call = install_handler(
        runtime, [{"interview_calibration": CALIBRATION}, session, session], shared_dispatcher
    )
    handle = await turn(runtime, f"ooo idk {EVIDENCE}", None)
    assert handle.metadata[INTERVIEW_CALIBRATION_METADATA_KEY] == CALIBRATION
    handle = await turn(runtime, "ooo interview Design payments", handle)
    assert call.call_args_list[1].args[0] == {
        "initial_context": "Design payments",
        "interview_calibration": CALIBRATION,
        "cwd": runtime.working_directory,
    }
    await turn(runtime, "ooo interview Retry once", handle)
    assert call.call_args_list[2].args[0] == {
        **session,
        "answer": "Retry once",
        "interview_calibration": CALIBRATION,
        "cwd": runtime.working_directory,
    }


def test_runtime_handle_to_persisted_dict_strips_calibration_non_opencode() -> None:
    """Calibration metadata must not be persisted on any runtime."""
    from ouroboros.orchestrator.interview_session import INTERVIEW_CALIBRATION_METADATA_KEY

    handle = RuntimeHandle(
        backend="codex",
        cwd="/tmp/test",
        metadata={
            INTERVIEW_CALIBRATION_METADATA_KEY: {
                "level": "foundational",
                "confidence": "medium",
                "evidence": "I do not know event sourcing",
            },
            "ouroboros_interview_session_id": "session-123",
        },
    )
    persisted = handle.to_persisted_dict()
    assert INTERVIEW_CALIBRATION_METADATA_KEY not in persisted["metadata"]
    # Session ID should still be present
    assert persisted["metadata"]["ouroboros_interview_session_id"] == "session-123"


def test_runtime_handle_to_persisted_dict_strips_calibration_opencode() -> None:
    """OpenCode already filters to allowed keys — calibration must not sneak in."""
    from ouroboros.orchestrator.interview_session import INTERVIEW_CALIBRATION_METADATA_KEY

    handle = RuntimeHandle(
        backend="opencode",
        cwd="/tmp/test",
        metadata={
            INTERVIEW_CALIBRATION_METADATA_KEY: {
                "level": "foundational",
                "confidence": "medium",
                "evidence": "I do not know event sourcing",
            },
        },
    )
    persisted = handle.to_persisted_dict()
    assert INTERVIEW_CALIBRATION_METADATA_KEY not in persisted["metadata"]
