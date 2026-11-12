"""Public observations follow live events without erasing cleanup receipts."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.orchestrator.adapter import RuntimeHandle
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.opencode_runtime import OpenCodeRuntime


@pytest.mark.parametrize("runtime_type", [CodexCliRuntime, OpenCodeRuntime])
async def test_rebound_handle_observes_running_after_starting(runtime_type) -> None:
    runtime = runtime_type(cli_path="test-cli")
    process = SimpleNamespace(returncode=None)
    state = {"runtime_status": "starting"}
    starting = RuntimeHandle(backend="opencode", metadata={"runtime_event_type": "session.created"})
    bound = runtime._bind_runtime_handle_controls(starting, process=process, control_state=state)
    assert bound is not None
    assert (await bound.observe())["lifecycle_state"] == "starting"

    running = RuntimeHandle(
        backend="opencode",
        native_session_id="s",
        metadata={"runtime_event_type": "session.started"},
    )
    rebound = runtime._bind_runtime_handle_controls(running, process=process, control_state=state)
    assert rebound is not None
    assert (await rebound.observe())["lifecycle_state"] == "running"


@pytest.mark.parametrize(
    "status",
    ["cancelled", "completed", "failed", "terminated", "terminating", "termination_unconfirmed"],
)
async def test_opencode_rebinding_keeps_cleanup_or_terminal_status(status) -> None:
    runtime = OpenCodeRuntime(cli_path="test-cli")
    state = {"runtime_status": status}
    handle = runtime._bind_runtime_handle_controls(
        RuntimeHandle(backend="opencode", native_session_id="s"),
        process=SimpleNamespace(returncode=None),
        control_state=state,
    )
    assert handle is not None
    assert (await handle.observe())["lifecycle_state"] == status


async def test_opencode_stream_exposes_running_public_observation(tmp_path) -> None:
    runtime = OpenCodeRuntime(cli_path="test-cli", cwd=tmp_path)
    process = SimpleNamespace(returncode=None, pid=None, stdin=None, stdout=object(), stderr=None)

    async def events(*_args, **_kwargs):
        yield '{"type":"session.created","sessionID":"synthetic-session"}'
        yield '{"type":"text","sessionID":"synthetic-session","part":{"text":"working"}}'
        raise asyncio.CancelledError

    with (
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)),
        patch.object(runtime, "_iter_stream_lines", side_effect=events),
        patch.object(runtime, "_collect_stream_lines", new=AsyncMock(return_value=[])),
        patch.object(runtime, "_terminate_process", new=AsyncMock(return_value=False)),
    ):
        stream = runtime.execute_task("work")
        message = await anext(stream)
        assert message.resume_handle is not None
        assert message.resume_handle.lifecycle_state == "running"
        assert (await message.resume_handle.observe())["lifecycle_state"] == "running"
        with pytest.raises(asyncio.CancelledError):
            await anext(stream)
        assert (await message.resume_handle.observe())[
            "lifecycle_state"
        ] == "termination_unconfirmed"
