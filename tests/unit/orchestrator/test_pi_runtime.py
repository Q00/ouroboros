from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ouroboros.orchestrator.adapter import RuntimeHandle
from ouroboros.orchestrator.pi_runtime import PiRuntime


def test_pi_runtime_satisfies_protocol_properties(tmp_path: Path) -> None:
    runtime = PiRuntime(
        bridge=lambda _request: {"content": "ok"},
        cwd=tmp_path,
        permission_mode="bypassPermissions",
        llm_backend="litellm",
        model="claude-sonnet-4-5",
    )

    assert runtime.runtime_backend == "pi"
    assert runtime.working_directory == str(tmp_path)
    assert runtime.permission_mode == "bypassPermissions"
    assert runtime.llm_backend == "litellm"
    assert runtime.capabilities.structured_output is True


@pytest.mark.asyncio
async def test_fixture_bridge_captures_pi_compatible_request(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    async def bridge(request: dict[str, Any]) -> dict[str, Any]:
        captured.update(request)
        return {
            "success": True,
            "final_message": "done",
            "session_id": "pi-session-1",
            "session_metadata": {"turn_id": "turn-2"},
        }

    resume_handle = RuntimeHandle(
        backend="pi",
        native_session_id="pi-session-0",
        conversation_id="conversation-1",
        metadata={"turn_id": "turn-1", "ac_id": "AC-1"},
    )
    runtime = PiRuntime(
        bridge=bridge,
        cwd=tmp_path,
        permission_mode="acceptEdits",
        llm_backend="codex",
        model="gpt-5.4",
    )

    messages = [
        message
        async for message in runtime.execute_task(
            "implement the task",
            tools=["Read", "Edit"],
            system_prompt="stay scoped",
            resume_handle=resume_handle,
        )
    ]

    assert captured == {
        "prompt": "implement the task",
        "system_prompt": "stay scoped",
        "cwd": str(tmp_path),
        "permission_mode": "acceptEdits",
        "llm_backend": "codex",
        "model_config": {"model": "gpt-5.4"},
        "tool_policy": {
            "allowed_tools": ["Read", "Edit"],
            "mode": "explicit_allowlist",
        },
        "resume_handle": {
            "backend": "pi",
            "kind": "agent_runtime",
            "native_session_id": "pi-session-0",
            "conversation_id": "conversation-1",
            "previous_response_id": None,
            "transcript_path": None,
            "cwd": None,
            "approval_mode": None,
            "updated_at": None,
            "metadata": {"turn_id": "turn-1", "ac_id": "AC-1"},
        },
        "session_metadata": {
            "turn_id": "turn-1",
            "ac_id": "AC-1",
            "resume_session_id": "pi-session-0",
        },
        "runtime_config_source": {"kind": "fixture", "value": "callable"},
    }
    assert messages[-1].content == "done"
    assert messages[-1].resume_handle is not None
    assert messages[-1].resume_handle.native_session_id == "pi-session-1"
    assert messages[-1].resume_handle.metadata == {"turn_id": "turn-2", "ac_id": "AC-1"}


@pytest.mark.asyncio
async def test_execute_task_to_result_invokes_deterministic_bridge(tmp_path: Path) -> None:
    runtime = PiRuntime(
        bridge=lambda request: {
            "success": True,
            "content": f"cwd={request['cwd']} prompt={request['prompt']}",
            "session_id": "pi-session-2",
        },
        cwd=tmp_path,
    )

    result = await runtime.execute_task_to_result("ship")

    assert result.is_ok
    assert result.value.success is True
    assert result.value.final_message == f"cwd={tmp_path} prompt=ship"
    assert result.value.session_id == "pi-session-2"


def test_factory_can_construct_pi_runtime(tmp_path: Path) -> None:
    from ouroboros.orchestrator.runtime_factory import create_agent_runtime

    runtime = create_agent_runtime(backend="pi", cwd=tmp_path, llm_backend="codex")

    assert isinstance(runtime, PiRuntime)
    assert runtime.working_directory == str(tmp_path)
    assert runtime.bridge_module == "pi.runtime.bridge:execute"


def test_production_source_does_not_hardcode_local_pi_clone() -> None:
    forbidden = "/Users/jaegyu.lee/Project/pi"
    production_files = Path("src").rglob("*.py")

    offenders = [
        str(path) for path in production_files if forbidden in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
