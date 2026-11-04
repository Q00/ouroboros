"""Live runtime handles must not report unconfirmed process cleanup as success."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.orchestrator.adapter import RuntimeHandle
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.opencode_runtime import OpenCodeRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_type", [CodexCliRuntime, OpenCodeRuntime])
async def test_handle_keeps_unconfirmed_termination_retryable(runtime_type) -> None:
    runtime = runtime_type(cli_path="test-cli")
    process = SimpleNamespace(returncode=None)
    state = {"process_id": 123}
    handle = runtime._bind_runtime_handle_controls(
        RuntimeHandle(backend="codex_cli", native_session_id="s"),
        process=process,
        control_state=state,
    )
    assert handle is not None
    with patch.object(runtime, "_terminate_process", new=AsyncMock(side_effect=[False, True])):
        assert await handle.terminate() is False
        snapshot = await handle.observe()
        assert snapshot["lifecycle_state"] == "termination_unconfirmed"
        assert snapshot.get("terminated") is not True
        assert await handle.terminate() is True
        assert (await handle.observe())["terminated"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_type", [CodexCliRuntime, OpenCodeRuntime])
async def test_handle_does_not_claim_already_exited_process_was_terminated(runtime_type) -> None:
    runtime = runtime_type(cli_path="test-cli")
    process = SimpleNamespace(returncode=0)
    handle = runtime._bind_runtime_handle_controls(
        RuntimeHandle(backend="codex_cli", native_session_id="s"),
        process=process,
        control_state={"process_id": 123},
    )
    assert handle is not None
    with patch.object(runtime, "_terminate_process", new=AsyncMock()) as terminate:
        assert await handle.terminate() is False
        terminate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_type", [CodexCliRuntime, OpenCodeRuntime])
async def test_handle_propagates_cancellation_without_claiming_termination(runtime_type) -> None:
    import asyncio

    runtime = runtime_type(cli_path="test-cli")
    state = {"process_id": 123}
    handle = runtime._bind_runtime_handle_controls(
        RuntimeHandle(backend="codex_cli", native_session_id="s"),
        process=SimpleNamespace(returncode=None),
        control_state=state,
    )
    assert handle is not None
    with patch.object(
        runtime, "_terminate_process", new=AsyncMock(side_effect=asyncio.CancelledError)
    ):
        with pytest.raises(asyncio.CancelledError):
            await handle.terminate()
    assert state.get("terminated") is not True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_type", "module"),
    [
        (CodexCliRuntime, "ouroboros.orchestrator.codex_cli_runtime"),
        (OpenCodeRuntime, "ouroboros.orchestrator.opencode_runtime"),
    ],
)
@pytest.mark.parametrize("confirmed", [False, True])
async def test_process_cleanup_propagates_tree_confirmation(
    runtime_type, module, confirmed
) -> None:
    runtime = runtime_type(cli_path="test-cli")
    with patch(f"{module}.terminate_runtime_process", new=AsyncMock(return_value=confirmed)):
        assert await runtime._terminate_process(SimpleNamespace(returncode=None)) is confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_type", [CodexCliRuntime, OpenCodeRuntime])
async def test_handle_does_not_claim_termination_when_cleanup_raises(runtime_type) -> None:
    runtime = runtime_type(cli_path="test-cli")
    state = {"process_id": 123}
    handle = runtime._bind_runtime_handle_controls(
        RuntimeHandle(backend="codex_cli", native_session_id="s"),
        process=SimpleNamespace(returncode=None),
        control_state=state,
    )
    assert handle is not None
    with patch.object(
        runtime, "_terminate_process", new=AsyncMock(side_effect=OSError("cleanup failed"))
    ):
        with pytest.raises(OSError, match="cleanup failed"):
            await handle.terminate()
    assert state.get("terminated") is not True


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_type", [CodexCliRuntime, OpenCodeRuntime])
@pytest.mark.parametrize("pid", [None, 123456789])
async def test_unowned_or_pidless_live_process_never_receives_success_receipt(
    runtime_type, pid
) -> None:
    runtime = runtime_type(cli_path="test-cli")
    process = SimpleNamespace(returncode=None, pid=pid)
    process.terminate = lambda: (_ for _ in ()).throw(PermissionError("denied"))
    handle = runtime._bind_runtime_handle_controls(
        RuntimeHandle(backend="codex_cli", native_session_id="s"), process=process, control_state={}
    )
    assert handle is not None
    assert await handle.terminate() is False
    assert (await handle.observe())["lifecycle_state"] == "termination_unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_type", [CodexCliRuntime, OpenCodeRuntime])
async def test_unconfirmed_tree_remains_unconfirmed_if_leader_exited(runtime_type) -> None:
    runtime = runtime_type(cli_path="test-cli")
    process = SimpleNamespace(returncode=None)
    state: dict[str, object] = {"process_id": 123}
    handle = runtime._bind_runtime_handle_controls(
        RuntimeHandle(backend="codex_cli", native_session_id="s"),
        process=process,
        control_state=state,
    )
    assert handle is not None

    async def failed_cleanup(_process):
        process.returncode = -15
        return False

    with patch.object(
        runtime, "_terminate_process", new=AsyncMock(side_effect=failed_cleanup)
    ) as cleanup:
        assert await handle.terminate() is False
        assert await handle.terminate() is False
        assert cleanup.await_count == 1
    assert (await handle.observe())["lifecycle_state"] == "termination_unconfirmed"
    assert state.get("terminated") is not True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_type", "module"),
    [
        (CodexCliRuntime, "ouroboros.orchestrator.codex_cli_runtime"),
        (OpenCodeRuntime, "ouroboros.orchestrator.opencode_runtime"),
    ],
)
async def test_timeout_does_not_report_unconfirmed_cleanup_as_terminated(
    runtime_type, module
) -> None:
    runtime = runtime_type(cli_path="test-cli", cwd="/tmp")
    process = SimpleNamespace(
        returncode=None, pid=None, stdout=object(), stderr=object(), stdin=None
    )

    async def timeout_stream(*args, **kwargs):
        if False:
            yield ""
        raise TimeoutError("stream timeout")

    with (
        patch(f"{module}.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)),
        patch.object(runtime, "_iter_stream_lines", side_effect=timeout_stream),
        patch.object(runtime, "_collect_stream_lines", new=AsyncMock(return_value=[])),
        patch.object(runtime, "_terminate_process", new=AsyncMock(return_value=False)),
    ):
        messages = [
            message async for message in runtime.execute_task("work", resume_session_id="s")
        ]
    result = messages[-1]
    assert result.is_error
    assert result.data["termination_confirmed"] is False
    assert "termination was not confirmed" in result.content
    assert result.resume_handle is not None
    assert (await result.resume_handle.observe())["lifecycle_state"] == "termination_unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_type", "module"),
    [
        (CodexCliRuntime, "ouroboros.orchestrator.codex_cli_runtime"),
        (OpenCodeRuntime, "ouroboros.orchestrator.opencode_runtime"),
    ],
)
async def test_cancel_path_keeps_unconfirmed_cleanup_visible_on_live_handle(
    runtime_type, module
) -> None:
    import asyncio

    runtime = runtime_type(cli_path="test-cli", cwd="/tmp")
    process = SimpleNamespace(
        returncode=None, pid=None, stdout=object(), stderr=object(), stdin=None
    )
    handles = []
    bind = runtime._bind_runtime_handle_controls

    def capture(*args, **kwargs):
        handle = bind(*args, **kwargs)
        if handle is not None:
            handles.append(handle)
        return handle

    async def cancelled_stream(*args, **kwargs):
        if False:
            yield ""
        raise asyncio.CancelledError

    with (
        patch(f"{module}.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)),
        patch.object(runtime, "_bind_runtime_handle_controls", side_effect=capture),
        patch.object(runtime, "_iter_stream_lines", side_effect=cancelled_stream),
        patch.object(runtime, "_collect_stream_lines", new=AsyncMock(return_value=[])),
        patch.object(runtime, "_terminate_process", new=AsyncMock(return_value=False)),
    ):
        with pytest.raises(asyncio.CancelledError):
            _ = [message async for message in runtime.execute_task("work", resume_session_id="s")]
    assert handles
    assert (await handles[-1].observe())["lifecycle_state"] == "termination_unconfirmed"


@pytest.mark.asyncio
async def test_codex_cleanup_confirms_owned_real_child_exit() -> None:
    import asyncio
    import os
    import sys

    if os.name != "posix":
        pytest.skip("POSIX ownership scan is required for this receipt")
    runtime = CodexCliRuntime(cli_path="test-cli")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True
    )
    try:
        assert await runtime._terminate_process(process) is True
        assert process.returncode is not None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_strict_process_receipt_rejects_empty_unverified_capture() -> None:
    from ouroboros.providers.codex_cli_stream import terminate_runtime_process
    from ouroboros.providers.worker_process_tree import WorkerTree

    # Covers platforms without process scans and PID-less/non-child captures:
    # a helper reporting an empty set is not proof that this process tree existed.
    process = SimpleNamespace(pid=123456789, returncode=-9)
    with (
        patch(
            "ouroboros.providers.codex_cli_stream.capture_worker_tree",
            new=AsyncMock(return_value=WorkerTree(leader_pid=process.pid)),
        ),
        patch("ouroboros.providers.codex_cli_stream.terminate_frozen_tree"),
        patch("ouroboros.providers.codex_cli_stream._terminate_runtime_leader", new=AsyncMock()),
        patch("ouroboros.providers.codex_cli_stream.watch_worker_tree", new=AsyncMock()),
        patch(
            "ouroboros.providers.codex_cli_stream.finish_worker_tree",
            new=AsyncMock(return_value=True),
        ),
    ):
        assert await terminate_runtime_process(process, require_owned_tree=True) is False


def test_non_posix_capture_without_scan_is_not_owned() -> None:
    from ouroboros.providers import worker_process_tree

    with (
        patch.object(worker_process_tree, "os", SimpleNamespace(name="nt")),
        patch.object(worker_process_tree, "_scan_processes", return_value=None),
    ):
        tree = worker_process_tree._capture(123456789, None, None)
    assert tree.members == {}
    # This empty capture must not be accepted by require_owned_tree.
    assert tree.leader_pid not in tree.members
