from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
import ouroboros.orchestrator.pi_runtime as pi_runtime_module
from ouroboros.orchestrator.pi_runtime import PiRuntime


def _write_skill(
    skills_dir: Path,
    skill_name: str,
    frontmatter_lines: list[str],
) -> Path:
    skill_dir = skills_dir / skill_name
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    frontmatter = "\n".join(frontmatter_lines)
    skill_md.write_text(
        f"---\n{frontmatter}\n---\n\n# {skill_name}\n",
        encoding="utf-8",
    )
    return skill_md


class _FakeCommandProcess:
    def __init__(self) -> None:
        self.returncode = 0
        self.communicate_input = b""
        self.killed = False
        self.waited = False

    async def communicate(self, input_bytes: bytes) -> tuple[bytes, bytes]:
        self.communicate_input = input_bytes
        return b'{"success": true, "content": "ok"}', b""

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        return self.returncode


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
    assert runtime.capabilities.skill_dispatch is True


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


@pytest.mark.asyncio
async def test_execute_task_dispatches_skill_before_invoking_bridge(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "run",
        [
            "name: run",
            "mcp_tool: ouroboros_execute_seed",
            "mcp_args:",
            '  seed_path: "$1"',
        ],
    )
    events: list[tuple[str, str]] = []

    async def dispatch_success(
        intercept: Any,
        current_handle: RuntimeHandle | None,
    ) -> tuple[AgentMessage, ...]:
        events.append((intercept.skill_name, str(current_handle)))
        return (
            AgentMessage(type="assistant", content="Dispatching Pi skill"),
            AgentMessage(type="result", content="Intercepted", data={"subtype": "success"}),
        )

    async def bridge(_request: dict[str, Any]) -> dict[str, Any]:
        pytest.fail("Pi bridge should not be invoked when skill dispatch succeeds")

    dispatcher = AsyncMock(side_effect=dispatch_success)
    runtime = PiRuntime(
        bridge=bridge,
        cwd=tmp_path,
        skills_dir=tmp_path,
        skill_dispatcher=dispatcher,
    )

    messages = [message async for message in runtime.execute_task("ooo run seed.yaml")]

    dispatcher.assert_awaited_once()
    assert events == [("run", "None")]
    assert messages[-1].content == "Intercepted"


def test_factory_can_construct_pi_runtime(tmp_path: Path) -> None:
    from ouroboros.orchestrator.runtime_factory import create_agent_runtime

    runtime = create_agent_runtime(backend="pi", cwd=tmp_path, llm_backend="codex")

    assert isinstance(runtime, PiRuntime)
    assert runtime.working_directory == str(tmp_path)
    assert runtime.bridge_module == "pi.runtime.bridge:execute"


def test_factory_forwards_pi_timeout_overrides(tmp_path: Path) -> None:
    from ouroboros.orchestrator.runtime_factory import create_agent_runtime

    runtime = create_agent_runtime(
        backend="pi",
        cwd=tmp_path,
        startup_output_timeout_seconds=1.25,
        stdout_idle_timeout_seconds=0,
    )

    assert isinstance(runtime, PiRuntime)
    assert runtime.startup_output_timeout_seconds == 1.25
    assert runtime.stdout_idle_timeout_seconds == 0


@pytest.mark.asyncio
async def test_command_bridge_uses_stdout_idle_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeCommandProcess()
    timeouts: list[float] = []

    async def fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> _FakeCommandProcess:
        return process

    async def fake_wait_for(awaitable: Any, timeout: float) -> Any:
        timeouts.append(timeout)
        return await awaitable

    monkeypatch.setattr(
        pi_runtime_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(pi_runtime_module.asyncio, "wait_for", fake_wait_for)
    runtime = PiRuntime(
        bridge_command="pi-bridge",
        bridge_module=None,
        cwd=tmp_path,
        stdout_idle_timeout_seconds=7,
    )

    response = await runtime._invoke_bridge({"prompt": "ship"})

    assert response == {"success": True, "content": "ok"}
    assert timeouts == [7.0]
    assert process.communicate_input


@pytest.mark.asyncio
async def test_command_bridge_allows_disabling_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeCommandProcess()

    async def fake_create_subprocess_exec(*_args: object, **_kwargs: object) -> _FakeCommandProcess:
        return process

    async def fail_if_wait_for_called(awaitable: Any, timeout: float) -> Any:
        pytest.fail(f"wait_for should not run when timeout is disabled: {timeout}")

    monkeypatch.setattr(
        pi_runtime_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(pi_runtime_module.asyncio, "wait_for", fail_if_wait_for_called)
    runtime = PiRuntime(
        bridge_command="pi-bridge",
        bridge_module=None,
        cwd=tmp_path,
        stdout_idle_timeout_seconds=0,
    )

    response = await runtime._invoke_bridge({"prompt": "ship"})

    assert response == {"success": True, "content": "ok"}


def test_production_source_does_not_hardcode_local_pi_clone() -> None:
    forbidden = "/Users/jaegyu.lee/Project/pi"
    production_files = Path("src").rglob("*.py")

    offenders = [
        str(path) for path in production_files if forbidden in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
